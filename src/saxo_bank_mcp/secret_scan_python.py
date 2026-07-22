from __future__ import annotations

import ast
from textwrap import dedent


def python_credential_candidates(text: str) -> tuple[str, ...] | None:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    candidates: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            candidates.extend(_string_candidates(node.value))
        candidates.extend(f"{key}={value}" for key, value in _literal_credential_assignments(node))
    return tuple(candidates)


def python_line_credential_candidates(line: str) -> tuple[str, ...] | None:
    candidates = python_credential_candidates(line)
    if candidates is not None:
        return candidates
    stripped = line.strip()
    if not stripped:
        return ()
    return python_credential_candidates("{" + stripped + "}")


def _string_candidates(value: str) -> tuple[str, ...]:
    if "\n" not in value:
        return (value,)
    nested = python_credential_candidates(dedent(value))
    if nested is not None:
        return nested
    line_candidates: list[str] = []
    for line in value.splitlines():
        candidates = python_line_credential_candidates(line)
        if candidates is None:
            return (value,)
        line_candidates.extend(candidates)
    return tuple(line_candidates)


def _literal_credential_assignments(node: ast.AST) -> tuple[tuple[str, str], ...]:
    if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
        pair = _assignment_pair(node.targets[0], node.value)
        return () if pair is None else (pair,)
    if isinstance(node, ast.AnnAssign) and isinstance(node.value, ast.Constant):
        pair = _assignment_pair(node.target, node.value)
        return () if pair is None else (pair,)
    if isinstance(node, ast.keyword) and node.arg is not None:
        pair = _literal_pair(node.arg, node.value)
        return () if pair is None else (pair,)
    if isinstance(node, ast.Dict):
        pairs = (_dict_pair(key, value) for key, value in zip(node.keys, node.values, strict=True))
        return tuple(pair for pair in pairs if pair is not None)
    return ()


def _assignment_pair(target: ast.expr, value: ast.Constant) -> tuple[str, str] | None:
    key = target.id if isinstance(target, ast.Name) else None
    if isinstance(target, ast.Attribute):
        key = target.attr
    return _literal_pair(key, value) if key is not None else None


def _dict_pair(key: ast.expr | None, value: ast.expr) -> tuple[str, str] | None:
    if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
        return None
    return _literal_pair(key.value, value)


def _literal_pair(key: str, value: ast.expr) -> tuple[str, str] | None:
    if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
        return None
    return key, value.value
