"""Identify isolated identity/equality edits and record their pinned operands."""

import ast
import enum
import importlib.abc
import importlib.machinery
from pathlib import Path


def numeric_signature(entries):
    return repr(sorted(entries.items()))


def numeric_edit(before, after):
    """Find a sole value change in a literal NUMERIC_ESCAPES table."""
    def normalize(text):
        tree = ast.parse(text)
        found = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                continue
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id == 'NUMERIC_ESCAPES' and isinstance(node.value, ast.Dict):
                found.append(ast.literal_eval(node.value))
                node.value = ast.Constant(None)
        return ast.dump(tree), found

    try:
        old, prior = normalize(before)
        new, current = normalize(after)
    except (ValueError, TypeError):
        return None
    if old != new or len(prior) != 1 or len(current) != 1:
        return None
    a, b = prior[0], current[0]
    if tuple(a) != tuple(b):
        return None
    diff = [key for key in a if a[key] != b[key]]
    if len(diff) != 1:
        return None
    key = diff[0]
    if type(key) is not str or len(key) != 1:
        return None
    for value in (a[key], b[key]):
        if (type(value) is not tuple or len(value) != 4 or any(type(x) is not int for x in value)
                or not 2 <= value[0] <= 36 or not 1 <= value[1] <= value[2] <= 16 or value[3] < 0):
            return None
    return numeric_signature(a), key


OPS = {ast.Is: 'is', ast.Eq: 'eq', ast.IsNot: 'isnot', ast.NotEq: 'ne'}
PAIR = {'is': 'eq', 'eq': 'is', 'isnot': 'ne', 'ne': 'isnot'}


def site(node):
    if len(node.ops) != 1 or type(node.ops[0]) not in OPS or not isinstance(node.left, ast.Attribute):
        return None
    right = node.comparators[0]
    if isinstance(right, ast.Constant) and right.value is None:
        return 'none'
    if isinstance(right, ast.Attribute) and isinstance(right.value, ast.Name) and right.attr.isupper():
        return 'member'
    return None


def single_swap(before, after):
    def normalized(code):
        tree = ast.parse(code)
        locations = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Compare) and site(node):
                locations.append((node.lineno, node.col_offset, OPS[type(node.ops[0])]))
                node.ops[0] = ast.Is()
        return ast.dump(tree), locations

    a, old = normalized(before)
    b, new = normalized(after)
    if a != b or len(old) != len(new):
        return None
    changed = [(x, y) for x, y in zip(old, new) if x[2] != y[2]]
    if len(changed) != 1:
        return None
    (line, column, op), (_, _, replacement) = changed[0]
    return f'{line}:{column}' if PAIR[op] == replacement else None


def boolean_swap(before, after):
    def shape(source):
        tree = ast.parse(source)
        conditions = []
        for node in ast.walk(tree):
            if (isinstance(node, ast.If) and isinstance(node.test, ast.BoolOp)
                    and all(value.lineno == node.lineno for value in node.test.values)):
                conditions.append((node.lineno, type(node.test.op)))
                node.test.op = ast.And()
        return ast.dump(tree), conditions

    old, prior = shape(before)
    new, current = shape(after)
    if old == new and len(prior) == len(current):
        changed = [line for (line, op), (_, replacement) in zip(prior, current) if op is not replacement]
        if len(changed) == 1:
            return changed[0]
    return None


def agreement(left, right, kind):
    """Only compare domains whose equality cannot invoke a user hook."""
    cls = type(left)
    if kind == 'none' and right is None:
        if left is None or (cls.__eq__ is object.__eq__ and cls.__ne__ is object.__ne__):
            return True
    if kind == 'member' and cls is type(right) and isinstance(right, enum.Enum):
        if cls.__eq__ is str.__eq__ and cls.__ne__ is str.__ne__:
            return (left is right) == str.__eq__(left, right)
        if cls.__eq__ is object.__eq__ and cls.__ne__ is object.__ne__:
            return True
    return False


class Transform(ast.NodeTransformer):
    def __init__(self, comparisons):
        self.comparisons = comparisons

    def visit_Attribute(self, node):
        self.generic_visit(node)
        if node.attr == 'WEEK_OFFSET' and isinstance(node.ctx, ast.Load):
            return ast.copy_location(ast.Call(
                func=ast.Name(id='_antalus_week_offset', ctx=ast.Load()), args=[node.value], keywords=[],
            ), node)
        return node

    def visit_Compare(self, node):
        self.generic_visit(node)
        if not self.comparisons:
            return node
        kind = site(node)
        if kind is None:
            return node
        return ast.copy_location(ast.Call(
            func=ast.Name(id='_antalus_record_comparison', ctx=ast.Load()),
            args=[ast.Constant(f'{node.lineno}:{node.col_offset}'), ast.Constant(kind),
                  ast.Constant(OPS[type(node.ops[0])]), node.left, node.comparators[0]], keywords=[],
        ), node)


class Loader(importlib.machinery.SourceFileLoader):
    def exec_module(self, module):
        callback = self.callback
        path = self.relative

        def compare(key, kind, op, left, right):
            callback(path, key, left, right, kind)
            if op == 'is':
                return left is right
            if op == 'isnot':
                return left is not right
            if op == 'eq':
                return left == right
            return left != right

        module._antalus_record_comparison = compare
        module._antalus_week_offset = self.week_offset
        super().exec_module(module)

    def get_code(self, fullname):
        text = self.get_data(self.path)
        tree = Transform(self.comparisons).visit(ast.parse(text, self.path))
        return compile(ast.fix_missing_locations(tree), self.path, 'exec', dont_inherit=True)


class RecordImports(importlib.abc.MetaPathFinder):
    def __init__(self, root, callback, week_offset):
        self.root = Path(root)
        self.callback = callback
        self.week_offset = week_offset

    def find_spec(self, fullname, path=None, target=None):
        if fullname != 'sqlglot' and not fullname.startswith('sqlglot.'):
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None or type(spec.loader) is not importlib.machinery.SourceFileLoader:
            return None
        source = Path(spec.origin)
        if not source.is_relative_to(self.root):
            return None
        relative = source.relative_to(self.root).as_posix()
        comparisons = relative.startswith(('sqlglot/dialects/', 'sqlglot/executor/'))
        if not comparisons and b'WEEK_OFFSET' not in source.read_bytes():
            return None
        loader = Loader(fullname, str(source))
        loader.relative = relative
        loader.callback = self.callback
        loader.comparisons = comparisons
        loader.week_offset = self.week_offset
        spec.loader = loader
        return spec
