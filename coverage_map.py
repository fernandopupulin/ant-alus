"""Record dependencies by running the unmodified SQLGlot test suite."""

import gzip
import json
import os
import pickle
import sys
from collections import defaultdict
from concurrent import futures
from functools import wraps
from pathlib import Path

import pytest


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
        self.subprocesses = set()
        self.class_users = defaultdict(set)

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
        if path and code.co_name != '<module>' and os.getpid() == self.pid:
            key = (path, code.co_firstlineno)
            if self.current is None:
                self.import_calls.add(key)
            else:
                calls = self.setup_functions if self.setup else self.functions
                calls[key].add(self.current)
        return sys.monitoring.DISABLE

    def audit(self, event, args):
        if self.current is None or os.getpid() != self.pid:
            return
        if event == 'open':
            path = self.relative(args[0])
            if path:
                readers = self.setup_readers if self.setup else self.readers
                readers[path].add(self.current)
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
            'subprocesses': bits(self.subprocesses),
            'class_users': {key: bits(nodes) for key, nodes in sorted(self.class_users.items())},
        }

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
    tool = 3
    monitoring.use_tool_id(tool, 'ant-alus')
    monitoring.register_callback(tool, monitoring.events.PY_START, observer.call)
    monitoring.register_callback(tool, monitoring.events.PY_RESUME, observer.call)
    monitoring.set_events(tool, monitoring.events.PY_START | monitoring.events.PY_RESUME)
    sys.addaudithook(observer.audit)
    from sqlglot import exp
    from sqlglot.dialects.dialect import Dialect
    from sqlglot.parser import Parser

    for base in (Dialect, Parser, exp.Expr):
        observer.record_instances(base)
    pool = futures.ProcessPoolExecutor
    futures.ProcessPoolExecutor = RecordingPool
    try:
        status = pytest.main(['-q', '-p', 'no:cacheprovider', 'tests'], plugins=[observer])
    finally:
        futures.ProcessPoolExecutor = pool
    monitoring.set_events(tool, 0)
    if status not in (0, 1) or len(observer.started) != len(observer.ids):
        raise SystemExit('Could not measure the complete pinned suite')
    with gzip.open(sys.argv[1], 'wt', encoding='utf-8') as file:
        json.dump(observer.export(), file, separators=(',', ':'))


if __name__ == '__main__':
    main()
