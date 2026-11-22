"""Select regression tests for independent patches against pinned SQLGlot."""

import ast
import gzip
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path


ROOT = Path('/src')
APP = Path('/app')
DATA = Path('/data')
FEE = 2000
HUNK = re.compile(r'^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@')


def patch_files(text, root):
    """Materialize ordinary same-path Git patches after checking the old context."""
    files = {}
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        fields = shlex.split(lines[index])
        if len(fields) != 4 or fields[:2] != ['diff', '--git']:
            raise ValueError('unsupported patch header')
        old, new = fields[2:]
        if not old.startswith('a/') or not new.startswith('b/') or old[2:] != new[2:]:
            raise ValueError('unsupported path changes')
        path = old[2:]
        if any(part in ('..', '.', '') for part in path.split('/')) or path in files:
            raise ValueError('invalid patch path')
        index += 1
        if index < len(lines) and lines[index].startswith('index '):
            index += 1
        if lines[index:index+2] != ['--- a/' + path, '+++ b/' + path]:
            raise ValueError('unsupported file operation')
        index += 2
        source = (root / path).read_text(encoding='utf-8')
        original = source.split('\n')
        contents = []
        cursor = 0
        while index < len(lines) and not lines[index].startswith('diff --git '):
            hunk = HUNK.match(lines[index])
            if not hunk:
                raise ValueError('invalid hunk')
            offset, count, _, added = (int(value) if value is not None else 1 for value in hunk.groups())
            start = offset - bool(count)
            if not cursor <= start <= len(original):
                raise ValueError('invalid hunk offset')
            contents.extend(original[cursor:start])
            cursor = start
            index += 1
            left, right = count, added
            while left or right:
                line = lines[index]
                index += 1
                tag, value = line[:1], line[1:]
                if tag in (' ', '-'):
                    if left <= 0 or cursor >= len(original) or original[cursor] != value:
                        raise ValueError('old context differs')
                    left -= 1
                    cursor += 1
                if tag in (' ', '+'):
                    if right <= 0:
                        raise ValueError('invalid new hunk length')
                    right -= 1
                    contents.append(value)
                if tag not in (' ', '-', '+'):
                    raise ValueError('invalid hunk body')
            if index < len(lines) and lines[index].startswith('\\ No newline'):
                raise ValueError('unsupported line-ending change')
        contents.extend(original[cursor:])
        files[path] = (source, '\n'.join(contents))
    if not files:
        raise ValueError('empty diff')
    return files


def code_shape(source):
    """Separate declaration structure from top-level and class method bodies."""
    tree = ast.parse(source)
    functions = []

    def visit(body):
        for node in body:
            if isinstance(node, ast.ClassDef):
                visit(node.body)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                start = min([node.lineno, *(d.lineno for d in node.decorator_list)])
                functions.append((start, [ast.dump(statement) for statement in node.body]))
                first = node.body[0]
                doc = isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str)
                node.body = [first] if doc else []

    visit(tree.body)

    class Deferred(ast.NodeTransformer):
        def visit_Lambda(self, node):
            # Defaults execute where the lambda is defined. Only its deferred
            # body may be compared by calls to the lambda itself.
            self.visit(node.args)
            functions.append((node.lineno, [ast.dump(node.body)]))
            node.body = ast.Constant(value=None)
            return node

    Deferred().visit(tree)
    return ast.dump(tree), functions


class Index:
    def __init__(self, profile, tests):
        self.profile = profile
        self.tests = tests
        numbered = {test['id']: test['number'] for test in tests}
        self.positions = [numbered.get(node) for node in profile['ids']]
        self.unmapped = set(numbered.values()) - set(self.positions)
        self.files = {}
        for t in tests:
            self.files.setdefault(t['file'], set()).add(t['number'])
        self.all = set(numbered.values())
        self.unobserved = int(profile['unobserved'], 16)
        self.subprocesses = int(profile['subprocesses'], 16)

    def numbers(self, bits):
        numbers = set()
        while bits:
            bit = bits & -bits
            n = self.positions[bit.bit_length() - 1]
            if n is not None:
                numbers.add(n)
            bits ^= bit
        return numbers

    def direct_readers(self, files):
        bits = self.unobserved
        for path in files:
            bits |= int(self.profile['readers'].get(path, '0'), 16)
        return self.unmapped | self.numbers(bits)

    def candidates(self, files):
        bits = 0
        direct = self.direct_readers(files)
        for path, (before, after) in files.items():
            if path == 'tests/conftest.py' or not path.startswith(('tests/', 'sqlglot/')):
                return self.all
            if path.startswith('tests/') and path.endswith('.py'):
                if path not in self.files:
                    return self.all
                direct.update(self.files[path])
                continue
            if not path.endswith('.py'):
                bits |= self.subprocesses
                continue
            old_shape, old = code_shape(before)
            new_shape, new = code_shape(after)
            if old_shape != new_shape or len(old) != len(new):
                return self.all
            import_calls = set(self.profile['imports'].get(path, ()))
            calls = self.profile['functions'].get(path, {})
            for (line, body), (_, revised) in zip(old, new):
                if body == revised:
                    continue
                if line in import_calls:
                    return self.all
                bits |= int(calls.get(str(line), '0'), 16) | self.subprocesses
        return direct | self.numbers(bits)


def schedule(numbers, milliseconds):
    """Keep the cheapest valid assignment across worker counts."""
    if not numbers:
        return []
    order = sorted(numbers, key=lambda n: (-milliseconds[n], n))
    total = sum(milliseconds[n] for n in order)
    best_cost, best = FEE + total, [order]
    for count in range(2, min(8, len(order)) + 1):
        if max(milliseconds[order[0]], (total + count - 1) // count) + FEE * count >= best_cost:
            continue
        loads = [0] * count
        groups = [[] for _ in loads]
        for n in order:
            worker = loads.index(min(loads))
            loads[worker] += milliseconds[n]
            groups[worker].append(n)
        groups = [group for group in groups if group]
        cost = max(loads) + FEE * len(groups)
        if cost < best_cost:
            best_cost, best = cost, groups
    half = total // 2
    if len(order) > 2 and half <= 50000 and max(milliseconds[order[0]], total - half) + 2 * FEE < best_cost:
        # A bit represents a reachable load on one worker. Recover the split
        # only if its exact cost beats the current schedule.
        reach = [1]
        mask = (1 << (half + 1)) - 1
        for n in order:
            previous = reach[-1]
            reach.append((previous | previous << milliseconds[n]) & mask)
        target = reach[-1].bit_length() - 1
        if total - target + 2 * FEE < best_cost:
            groups = [[], []]
            for n, previous in zip(reversed(order), reversed(reach[:-1])):
                if (previous >> target) & 1:
                    groups[1].append(n)
                else:
                    groups[0].append(n)
                    target -= milliseconds[n]
            return groups
    return best


def schedule_cost(groups, milliseconds):
    return max((sum(milliseconds[n] for n in group) for group in groups), default=0) + FEE * len(groups)


def materialize(source, target, edits):
    """Make a small private source overlay without copying the unchanged tree."""
    if not edits:
        target.symlink_to(source, target_is_directory=source.is_dir())
    elif '' in edits:
        target.write_text(edits[''], encoding='utf-8')
    else:
        children = {}
        for path, text in edits.items():
            name, _, tail = path.partition('/')
            children.setdefault(name, {})[tail] = text
        target.mkdir()
        for child in source.iterdir():
            materialize(child, target / child.name, children.get(child.name, {}))


def refine(selected, files, tests, milliseconds, deadline):
    """Use remaining test reports as positive proof of passage for cheap candidates."""
    if not selected or len(selected) > 220:
        return selected
    if any(not path.startswith('sqlglot/') or not path.endswith('.py') for path in files):
        return selected
    ordered = sorted(selected, key=lambda n: (milliseconds[n], n))
    ready = [n for n in ordered if milliseconds[n] <= 5500]
    ready = ready[:90]
    if not ready or time.monotonic() + 2 >= deadline:
        return selected
    without = selected - set(ready)
    potential = schedule_cost(schedule(selected, milliseconds), milliseconds) - schedule_cost(schedule(without, milliseconds), milliseconds)
    if potential < 1000:
        return selected
    edits = {path: after for path, (_, after) in files.items()}
    with tempfile.TemporaryDirectory(dir='/tmp') as temporary:
        temp = Path(temporary)
        source = temp / 'tree'
        result = temp / 'result.json'
        materialize(ROOT, source, edits)
        ids = [tests[n]['id'] for n in ready]
        env = {**os.environ, 'PYTHONPATH': str(source)}
        try:
            proc = subprocess.Popen(
                [sys.executable, str(APP / 'selector.py'), 'check', str(result), *ids],
                cwd=source, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            try:
                status = proc.wait(timeout=max(0, min(6.0, deadline - time.monotonic())))
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait()
                return selected
            if status in (0, 1) and result.is_file():
                passed = set(json.loads(result.read_bytes()))
                return selected - {n for n in ready if tests[n]['id'] in passed}
        except (OSError, ValueError):
            pass
    return selected


def check(path, ids):
    import pytest

    class Verdict:
        def __init__(self):
            self.passed = set()
            self.failed = set()
            self.finished = set()

        def pytest_runtest_logreport(self, report):
            if report.failed or report.skipped:
                self.failed.add(report.nodeid)
            if report.when == 'call' and report.passed:
                self.passed.add(report.nodeid)
            if report.when == 'teardown' and report.passed:
                self.finished.add(report.nodeid)

    verdict = Verdict()
    status = pytest.main(['-q', '-p', 'no:cacheprovider', *ids], plugins=[verdict])
    if status in (0, 1):
        Path(path).write_text(json.dumps(sorted((verdict.passed & verdict.finished) - verdict.failed)))
    return status


def main():
    started = time.monotonic()
    tests = sorted(json.loads((DATA / 'tests.json').read_bytes())['tests'], key=lambda test: test['number'])
    with gzip.open(APP / 'coverage.json.gz', 'rt', encoding='utf-8') as stream:
        index = Index(json.load(stream), tests)
    milliseconds = [round(t['seconds'] * 1000) for t in tests]
    deadline = started + 70
    patches = sorted((DATA / 'commits').glob('*.patch'))
    for patch in patches:
        try:
            files = patch_files(patch.read_text(encoding='utf-8'), ROOT)
            numbers = index.candidates(files)
            if numbers and os.environ.get('ANTALUS_DYNAMIC') == '1':
                direct = index.direct_readers(files) & numbers
                numbers = direct | refine(numbers - direct, files, tests, milliseconds, deadline)
        except (OSError, IndexError, ValueError, SyntaxError, RecursionError):
            numbers = index.all
        print(json.dumps({'commit': patch.stem, 'workers': schedule(numbers, milliseconds)}, separators=(',', ':')), flush=True)
    cpu = sum(os.times()[:4])
    print(f'done {len(patches)} commits in {time.monotonic() - started:.3f}s cpu {cpu:.3f}s', file=sys.stderr)


if __name__ == '__main__':
    if len(sys.argv) > 2 and sys.argv[1] == 'check':
        raise SystemExit(check(sys.argv[2], sys.argv[3:]))
    main()
