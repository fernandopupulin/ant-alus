"""Record dependencies by running the unmodified SQLGlot test suite."""

import gzip
import json
import os
import pickle
import subprocess
import sys
from collections import defaultdict
from concurrent import futures
from functools import wraps
from pathlib import Path

import pytest
from comparisons import Loader, RecordImports, agreement, numeric_signature

MONITOR = 3


class RecordingPool(futures.Executor):
    """Expose task coverage in the recorder while preserving its pickle boundary."""

    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kwargs):
        self.initializer = initializer
        self.initargs = initargs
        self.closed = False

    def submit(self, fn, /, *args, **kwargs):
        if self.closed:
            raise RuntimeError('pool is shut down')
        future = futures.Future()
        try:
            task, positional, keywords = pickle.loads(pickle.dumps((fn, args, kwargs)))
            if self.initializer is not None:
                initializer, self.initializer = self.initializer, None
                initializer(*self.initargs)
            value = pickle.loads(pickle.dumps(task(*positional, **keywords)))
        except Exception as exc:
            future.set_exception(exc)
        else:
            future.set_result(value)
        return future

    def shutdown(self, wait=True, *, cancel_futures=False):
        self.closed = True


class Observe:
    def __init__(self, root):
        self.root = str(root) + '/'
        self.pid = os.getpid()
        self.current = None
        self.setup = False
        self.ids = set()
        self.started = set()
        self.inconclusive = set()
        self.import_calls = set()
        self.functions = defaultdict(set)
        self.setup_functions = defaultdict(set)
        self.readers = defaultdict(set)
        self.setup_readers = defaultdict(set)
        self.import_readers = defaultdict(set)
        self.subprocesses = set()
        self.import_probes = set()
        self.class_users = defaultdict(set)
        self.compared = set()
        self.disagreement = defaultdict(set)
        self.shared_comparisons = set()
        self.numeric_tables = set()
        self.numeric_users = defaultdict(set)
        self.numeric_shared = set()
        self.week_users = defaultdict(set)
        self.week_shared = set()
        self.branch_lines = defaultdict(set)
        self.shared_lines = set()

    def week_offset(self, dialect):
        cls = dialect if isinstance(dialect, type) else type(dialect)
        owner = next((base for base in cls.__mro__ if 'WEEK_OFFSET' in vars(base)), cls)
        key = f'{owner.__module__}:{owner.__qualname__}'
        if self.current is None or self.setup:
            self.week_shared.add(key)
        else:
            self.week_users[key].add(self.current)
        return dialect.WEEK_OFFSET

    def record_escapes(self, core):
        init = core.__init__
        read_byte = core._read_byte_escape
        scan = core._scan_numeric_escape
        signatures = {}

        def table(instance):
            entries = instance.numeric_escapes
            key = id(entries)
            if key not in signatures:
                signatures[key] = entries, numeric_signature(entries)
            return signatures[key][1]

        def record(instance, peek):
            key = table(instance), '0' if peek and peek in '01234567' else peek
            if self.current is None or self.setup:
                self.numeric_shared.add(key)
            else:
                self.numeric_users[key].add(self.current)

        @wraps(init)
        def start(instance, /, *args, **kwargs):
            init(instance, *args, **kwargs)
            self.numeric_tables.add(table(instance))

        def read(instance, pos):
            record(instance, instance.sql[pos + 1:pos + 2])
            return read_byte(instance, pos)

        def scan_escape(instance):
            record(instance, instance._peek)
            return scan(instance)

        core.__init__ = start
        core._read_byte_escape = read
        core._scan_numeric_escape = scan_escape

    def compare(self, path, key, left, right, kind):
        location = (path, key)
        if self.current is None or self.setup:
            self.shared_comparisons.add(location)
        else:
            self.compared.add(location)
            if not agreement(left, right, kind):
                self.disagreement[location].add(self.current)

    def relative(self, path):
        if not isinstance(path, str):
            return None
        if path.startswith(self.root):
            path = path[len(self.root):]
        elif path.startswith('/'):
            return None
        if path.startswith(('sqlglot/', 'tests/')) and '/__pycache__/' not in path:
            return path
        return None

    def call(self, code, _offset):
        path = self.relative(code.co_filename)
        if path and path.startswith('sqlglot/parsers/') and not sys.monitoring.get_local_events(MONITOR, code):
            sys.monitoring.set_local_events(MONITOR, code, sys.monitoring.events.LINE)
        if path and code.co_name != '<module>' and os.getpid() == self.pid:
            key = (path, code.co_firstlineno)
            if self.current is None:
                self.import_calls.add(key)
            else:
                calls = self.setup_functions if self.setup else self.functions
                calls[key].add(self.current)
        return sys.monitoring.DISABLE

    def line(self, code, lineno):
        key = (self.relative(code.co_filename), lineno)
        if self.current is None or self.setup:
            self.shared_lines.add(key)
        elif os.getpid() == self.pid:
            self.branch_lines[key].add(self.current)
        return sys.monitoring.DISABLE

    def audit(self, event, args):
        if self.current is None or os.getpid() != self.pid:
            return
        if event == 'open':
            path = self.relative(args[0])
            if path:
                frame = sys._getframe(1)
                is_import = False
                while frame:
                    code = frame.f_code
                    if code in (Loader.get_code.__code__, RecordImports.find_spec.__code__):
                        is_import = True
                        break
                    module = frame.f_globals.get('__name__', '')
                    if not module.startswith(('pathlib', 'importlib', '_frozen_importlib')):
                        break
                    if module in ('importlib._bootstrap_external', '_frozen_importlib_external') and code.co_name == 'get_code':
                        is_import = True
                        break
                    frame = frame.f_back
                readers = self.import_readers if is_import else self.setup_readers if self.setup else self.readers
                readers[path].add(self.current)
        elif (event == 'subprocess.Popen' and not self.setup and args[1] == [
            'python', '-c', "import sqlglot; assert len(sqlglot.Dialect._classes) == 1; print('Success')",
        ] and args[2] is None and args[3] is None):
            self.import_probes.add(self.current)
        elif event in ('subprocess.Popen', 'os.fork', 'os.posix_spawn'):
            self.subprocesses.add(self.current)

    def pytest_collection_finish(self, session):
        self.ids.update(item.nodeid for item in session.items)

    def pytest_runtest_logreport(self, report):
        if report.failed:
            self.inconclusive.add(report.nodeid)

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_runtest_protocol(self, item):
        self.current = item.nodeid
        self.started.add(self.current)
        sys.monitoring.restart_events()
        try:
            return (yield)
        finally:
            self.current = None

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_runtest_setup(self):
        self.setup = True
        try:
            return (yield)
        finally:
            self.setup = False

    def export(self):
        ids = sorted(self.ids)
        number = {node: n for n, node in enumerate(ids)}
        children = defaultdict(set)
        for node in ids:
            children[node.split('::', 1)[0]].add(node)
        for path, tests in self.setup_readers.items():
            for node in tests:
                self.readers[path].update(children[node.split('::', 1)[0]])
        for key, tests in self.setup_functions.items():
            for node in tests:
                self.functions[key].update(children[node.split('::', 1)[0]])
        def bits(nodes):
            return hex(sum(1 << number[n] for n in nodes))
        functions = defaultdict(dict)
        for (path, line), nodes in sorted(self.functions.items()):
            functions[path][line] = bits(nodes)
        imports = defaultdict(list)
        for path, line in sorted(self.import_calls):
            imports[path].append(line)
        return {
            'ids': ids, 'unobserved': bits((self.ids - self.started) | self.inconclusive), 'functions': functions,
            'imports': imports, 'readers': {p: bits(n) for p, n in sorted(self.readers.items())},
            'import_readers': {p: bits(n) for p, n in sorted(self.import_readers.items())},
            'subprocesses': bits(self.subprocesses),
            'import_probes': bits(self.import_probes),
            'import_paths': self.import_paths(),
            'class_users': {key: bits(nodes) for key, nodes in sorted(self.class_users.items())},
            'comparison_risks': {
                f'{path}:{key}': bits(self.disagreement.get((path, key), ()))
                for path, key in sorted(self.compared - self.shared_comparisons)
            } if not self.inconclusive else {},
            'numeric_tables': sorted(self.numeric_tables) if not self.inconclusive else [],
            'numeric_users': {repr(key): bits(nodes) for key, nodes in self.numeric_users.items()},
            'numeric_shared': [repr(key) for key in self.numeric_shared],
            'week_users': {key: bits(nodes) for key, nodes in self.week_users.items() if key not in self.week_shared}
                if not self.inconclusive else {},
            'branch_lines': {f'{path}:{line}': bits(nodes) for (path, line), nodes in self.branch_lines.items()
                             if (path, line) not in self.shared_lines} if not self.inconclusive else {},
        }

    def import_paths(self):
        if not self.import_probes:
            return None
        program = '''
import json, sys
paths = set()
def audit(event, args):
    if event == 'open' and isinstance(args[0], str):
        paths.add(args[0])
sys.addaudithook(audit)
import sqlglot
assert len(sqlglot.Dialect._classes) == 1
print(json.dumps(sorted(paths)))
'''
        try:
            result = subprocess.run([sys.executable, '-c', program], capture_output=True, check=True, timeout=5)
            return sorted({relative for path in json.loads(result.stdout) if (relative := self.relative(path))})
        except (OSError, ValueError, subprocess.SubprocessError):
            return None

    def record_instances(self, base):
        original = base.__init__
        owner_keys = {}
        last_seen = {}

        @wraps(original)
        def init(instance, /, *args, **kwargs):
            active = self.current
            if active is not None:
                kind = type(instance)
                if last_seen.get(kind) != active:
                    last_seen[kind] = active
                    keys = owner_keys.get(kind)
                    if keys is None:
                        keys = owner_keys[kind] = tuple(
                            f'{cls.__module__}:{cls.__qualname__}' for cls in kind.__mro__
                            if cls.__module__.startswith('sqlglot.') and cls is not base
                        )
                    for key in keys:
                        self.class_users[key].add(active)
            return original(instance, *args, **kwargs)

        base.__init__ = init


def main():
    observer = Observe(Path.cwd())
    monitoring = sys.monitoring
    tool = MONITOR
    monitoring.use_tool_id(tool, 'ant-alus')
    monitoring.register_callback(tool, monitoring.events.PY_START, observer.call)
    monitoring.register_callback(tool, monitoring.events.PY_RESUME, observer.call)
    monitoring.register_callback(tool, monitoring.events.LINE, observer.line)
    monitoring.set_events(tool, monitoring.events.PY_START | monitoring.events.PY_RESUME)
    sys.addaudithook(observer.audit)
    loader = RecordImports(Path.cwd(), observer.compare, observer.week_offset)
    sys.meta_path.insert(0, loader)
    from sqlglot import exp
    from sqlglot.dialects.dialect import Dialect
    from sqlglot.parser import Parser
    from sqlglot.tokenizer_core import TokenizerCore

    observer.record_escapes(TokenizerCore)
    for base in (Dialect, Parser, exp.Expr):
        observer.record_instances(base)
    pool = futures.ProcessPoolExecutor
    futures.ProcessPoolExecutor = RecordingPool
    try:
        status = pytest.main(['-q', '-p', 'no:cacheprovider', 'tests'], plugins=[observer])
    finally:
        futures.ProcessPoolExecutor = pool
        sys.meta_path.remove(loader)
    monitoring.set_events(tool, 0)
    if status not in (0, 1) or len(observer.started) != len(observer.ids):
        raise SystemExit('Could not measure the complete pinned suite')
    with gzip.open(sys.argv[1], 'wt', encoding='utf-8') as file:
        json.dump(observer.export(), file, separators=(',', ':'))


if __name__ == '__main__':
    main()
