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

from comparisons import boolean_swap, numeric_edit, single_swap

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
        files[path] = (source, '\n'.join(contents) + ('\n' if source.endswith('\n') else ''))
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


def configuration_change(before, after, module):
    """Recognize a same-shape class configuration update with a known consumer."""
    def shape(text):
        tree = ast.parse(text)
        entries = {}

        def walk(body, parents=()):
            for node in body:
                if isinstance(node, ast.ClassDef):
                    walk(node.body, parents + (node.name,))
                elif parents and isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                    name = node.targets[0].id
                    kind = (
                        (module.startswith('sqlglot.dialects.') and name in ('WEEK_OFFSET', 'NUMERIC_ESCAPES'))
                        or (module.startswith('sqlglot.parsers.') and name == 'SHOW_PARSERS')
                        or (module.startswith('sqlglot.expressions.') and name == 'arg_types')
                    )
                    if not kind:
                        continue
                    if isinstance(node.value, ast.Dict):
                        if any((key is None and name != 'SHOW_PARSERS') or
                               (key is not None and (not isinstance(key, ast.Constant) or type(key.value) is not str))
                               for key in node.value.keys):
                            continue
                        keys = tuple(key.value if key is not None else (ast.dump(value),)
                                     for key, value in zip(node.value.keys, node.value.values))
                    elif isinstance(node.value, (ast.Constant, ast.UnaryOp)) and name == 'WEEK_OFFSET':
                        if type(ast.literal_eval(node.value)) is not int:
                            continue
                        keys = None
                    else:
                        continue
                    if name == 'SHOW_PARSERS':
                        values = [v for k, v in zip(node.value.keys, node.value.values) if k is not None]
                        if any(not (isinstance(v, ast.Call) and isinstance(v.func, ast.Name)
                                    and v.func.id == '_show_parser') for v in values):
                            continue
                        for value in values:
                            for arg in (*value.args, *(kw.value for kw in value.keywords)):
                                ast.literal_eval(arg)
                    else:
                        literal = ast.literal_eval(node.value)
                        if name == 'arg_types' and any(type(v) is not bool for v in literal.values()):
                            continue
                    entries[(parents, name, keys)] = ast.dump(node.value)
                    node.value = ast.Constant(value=None)

        walk(tree.body)
        return ast.dump(tree), entries

    old, a = shape(before)
    new, b = shape(after)
    if old != new or a.keys() != b.keys():
        return None
    differences = [key for key in a if a[key] != b[key]]
    if len(differences) != 1:
        return None
    parents, name, _ = differences[0]
    if module.startswith('sqlglot.dialects.'):
        parents = parents[:1]
    return f'{module}:{".".join(parents)}', name


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
        self.import_probes = int(profile.get('import_probes', '0'), 16)
        self.import_readers = profile.get('import_readers', {})
        paths = profile.get('import_paths')
        self.import_paths = set(paths) if paths else None

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
        subprocesses = self.subprocesses
        if self.import_paths is None or self.import_paths.intersection(files):
            subprocesses |= self.import_probes
        for path, (before, after) in files.items():
            if path == 'tests/conftest.py' or not path.startswith(('tests/', 'sqlglot/')):
                return self.all
            if path.startswith('tests/') and path.endswith('.py'):
                if path not in self.files:
                    return self.all
                direct.update(self.files[path])
                continue
            if not path.endswith('.py'):
                bits |= subprocesses
                continue
            compile(after, path, 'exec')
            if path in self.import_readers:
                old_ast = ast.dump(ast.parse(before))
                new_ast = ast.dump(ast.parse(after))
                if old_ast != new_ast:
                    bits |= int(self.import_readers[path], 16)
            lines = self.profile.get('branch_lines', {})
            if len(files) == 1 and lines and path.startswith('sqlglot/parsers/'):
                key = f'{path}:{boolean_swap(before, after)}'
                if key in lines:
                    bits |= int(lines[key], 16) | subprocesses
                    continue
            risks = self.profile.get('comparison_risks', {})
            if len(files) == 1 and risks and path.startswith(('sqlglot/dialects/', 'sqlglot/executor/')):
                key = single_swap(before, after)
                if key is not None and f'{path}:{key}' in risks:
                    bits |= int(risks[f'{path}:{key}'], 16) | subprocesses
                    continue
            old_shape, old = code_shape(before)
            new_shape, new = code_shape(after)
            if old_shape != new_shape or len(old) != len(new):
                tables = self.profile.get('numeric_tables', ())
                if len(files) == 1 and tables and path.startswith('sqlglot/dialects/'):
                    escape = numeric_edit(before, after)
                    if escape is not None and escape[0] in tables and repr(escape) not in self.profile['numeric_shared']:
                        bits |= int(self.profile['numeric_users'].get(repr(escape), '0'), 16) | subprocesses
                        continue
                try:
                    config = configuration_change(before, after, path[:-3].replace('/', '.'))
                except (TypeError, ValueError):
                    config = None
                if config is not None:
                    owner, name = config
                    users = self.profile.get('class_users', {})
                    attribute_users = self.profile.get('week_users', {})
                    if name == 'WEEK_OFFSET' and len(files) == 1 and owner in attribute_users:
                        users = attribute_users
                    if owner in users:
                        bits |= int(users[owner], 16) | subprocesses
                        continue
                return self.all
            import_calls = set(self.profile['imports'].get(path, ()))
            calls = self.profile['functions'].get(path, {})
            for (line, body), (_, revised) in zip(old, new):
                if body == revised:
                    continue
                if line in import_calls:
                    return self.all
                bits |= int(calls.get(str(line), '0'), 16) | subprocesses
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


def refine(selected, protected, files, tests, milliseconds):
    """Verify one small group when it can repay the cost of starting pytest."""
    if (not selected or selected & protected or len(selected) > 40
            or any(not p.startswith('sqlglot/') or not p.endswith('.py') for p in files)):
        return selected
    if sum(milliseconds[n] + 5 for n in selected) > 1100:
        return selected
    edits = {path: after for path, (_, after) in files.items()}
    with tempfile.TemporaryDirectory(dir='/tmp') as temporary:
        temp = Path(temporary)
        source = temp / 'tree'
        result = temp / 'result.json'
        materialize(ROOT, source, edits)
        ids = [tests[n]['id'] for n in sorted(selected)]
        env = {**os.environ, 'PYTHONPATH': str(source)}
        try:
            proc = subprocess.Popen(
                [sys.executable, str(APP / 'selector.py'), 'check', str(result), *ids],
                cwd=source, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            try:
                status = proc.wait(timeout=2.5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait()
                return selected
            if status in (0, 1) and result.is_file():
                passed = set(json.loads(result.read_bytes()))
                return {n for n in selected if tests[n]['id'] not in passed}
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
    patches = sorted((DATA / 'commits').glob('*.patch'))
    for patch in patches:
        try:
            files = patch_files(patch.read_text(encoding='utf-8'), ROOT)
            numbers = index.candidates(files)
            if not os.environ.get('ANTALUS_STATIC'):
                protected = set(index.unmapped)
                if any(path.startswith('sqlglot/') for path in files):
                    protected.update(index.direct_readers(files))
                numbers = refine(numbers, protected, files, tests, milliseconds)
        except (OSError, IndexError, ValueError, SyntaxError, RecursionError):
            numbers = index.all
        print(json.dumps({'commit': patch.stem, 'workers': schedule(numbers, milliseconds)}, separators=(',', ':')), flush=True)
    cpu = sum(os.times()[:4])
    print(f'done {len(patches)} commits in {time.monotonic() - started:.3f}s cpu {cpu:.3f}s', file=sys.stderr)


if __name__ == '__main__':
    if len(sys.argv) > 2 and sys.argv[1] == 'check':
        raise SystemExit(check(sys.argv[2], sys.argv[3:]))
    main()
