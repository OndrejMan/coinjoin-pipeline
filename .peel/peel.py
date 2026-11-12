#!/usr/bin/env python3
"""Small AST-guided removals for peeling features out of Python files.

    peel.py def FILE NAME...            top-level def/class/assignment
    peel.py member FILE CLASS NAME...   method, field or assignment inside a class
    peel.py key FILE VAR KEY...         entry of a dict/set/tuple literal bound to VAR (top level or class)
    peel.py import FILE NAME...         drop NAME from import statements (whole statement when empty)

Each removal takes the node's decorators and the comment lines directly above it, then
collapses the blank lines left at that spot so the surrounding layout stays as it was.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path


def leading_comments(lines: list[str], start: int) -> int:
    """First line index of the comment block directly above line index start."""
    index = start
    while index > 0 and lines[index - 1].lstrip().startswith("#"):
        index -= 1
    return index


def node_span(lines: list[str], node: ast.AST) -> tuple[int, int]:
    first = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])]) - 1
    return leading_comments(lines, first), node.end_lineno  # type: ignore[attr-defined]


def _top_level(line: str) -> bool:
    """A def, class or decorator at column 0: two blank lines belong before it."""
    return line.startswith(("def ", "class ", "async def ", "@"))


def remove_spans(path: Path, spans: list[tuple[int, int]], blank: int) -> None:
    """Delete each span and leave as many blank lines as the tighter side of it had."""
    lines = path.read_text().splitlines(keepends=True)
    for start, end in sorted(spans, reverse=True):
        before = 0
        while start - before - 1 >= 0 and lines[start - before - 1].strip() == "":
            before += 1
        after = 0
        while end + after < len(lines) and lines[end + after].strip() == "":
            after += 1
        lo, hi = start - before, end + after
        if hi >= len(lines):
            keep = 0
        elif lo > 0 and lines[lo - 1].rstrip().endswith(":") and not lines[lo - 1].lstrip().startswith("#"):
            keep = 0
        else:
            keep = min(before, after)
            if lines[hi].startswith((" ", "\t")):
                keep = min(keep, 1)
            elif _top_level(lines[hi]):
                # Two blank lines before a top-level def or class, as far as the cut had them.
                keep = min(2, before + after)
            elif lo > 0 and lines[lo - 1].startswith((" ", "\t")):
                # A top-level statement after a def or class body keeps its two blank lines.
                keep = 2
        lines[lo:hi] = ["\n"] * keep
    text = "".join(lines)
    text = text.rstrip("\n") + "\n" if text.strip() else ""
    path.write_text(text)


def names_of(node: ast.AST) -> set[str]:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return {node.name}
    if isinstance(node, ast.Assign):
        return {t.id for t in node.targets if isinstance(t, ast.Name)}
    if isinstance(node, (ast.AnnAssign, ast.AugAssign)) and isinstance(node.target, ast.Name):
        return {node.target.id}
    return set()


def cmd_def(path: Path, names: list[str]) -> None:
    lines = path.read_text().splitlines(keepends=True)
    tree = ast.parse("".join(lines))
    spans, found = [], set()
    for node in tree.body:
        hit = names_of(node) & set(names)
        if hit:
            spans.append(node_span(lines, node))
            found |= hit
    missing = set(names) - found
    if missing:
        sys.exit(f"{path}: not found at top level: {', '.join(sorted(missing))}")
    remove_spans(path, spans, 2)


def find_class(tree: ast.Module, name: str) -> ast.ClassDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    sys.exit(f"class {name} not found")


def cmd_member(path: Path, cls: str, names: list[str]) -> None:
    lines = path.read_text().splitlines(keepends=True)
    tree = ast.parse("".join(lines))
    node = find_class(tree, cls)
    spans, found = [], set()
    for item in node.body:
        hit = names_of(item) & set(names)
        if hit:
            spans.append(node_span(lines, item))
            found |= hit
    missing = set(names) - found
    if missing:
        sys.exit(f"{path}:{cls}: not found: {', '.join(sorted(missing))}")
    remove_spans(path, spans, 1)


def literal_entries(value: ast.AST) -> list[tuple[object, ast.AST, ast.AST | None]]:
    """(key, first node, last node) of each entry of a dict, set, tuple or list literal."""
    if isinstance(value, ast.Call) and value.args:
        value = value.args[0]
    if isinstance(value, ast.Dict):
        out = []
        for key, item in zip(value.keys, value.values):
            if key is None:
                continue
            label = key.value if isinstance(key, ast.Constant) else ast.unparse(key)
            out.append((label, key, item))
        return out
    if isinstance(value, (ast.Set, ast.Tuple, ast.List)):
        out = []
        for item in value.elts:
            if isinstance(item, ast.Constant):
                label = item.value
            elif isinstance(item, ast.Tuple) and item.elts and isinstance(item.elts[0], ast.Constant):
                label = item.elts[0].value
            else:
                label = ast.unparse(item)
            out.append((label, item, item))
        return out
    sys.exit("value is not a dict, set, tuple or list literal")


def cmd_key(path: Path, var: str, keys: list[str]) -> None:
    text = path.read_text()
    lines = text.splitlines(keepends=True)
    tree = ast.parse(text)
    target = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and var in names_of(node):
            target = node.value
            break
    if target is None:
        sys.exit(f"{path}: {var} not found")
    spans, found = [], set()
    for label, first, last in literal_entries(target):
        if str(label) in keys:
            start, end = first.lineno - 1, last.end_lineno  # type: ignore[union-attr]
            # Whole lines only: the entry must own its lines.
            if lines[start][: first.col_offset].strip() or lines[end - 1][last.end_col_offset :].strip() not in ("", ","):  # type: ignore[union-attr]
                sys.exit(f"{path}: entry {label} shares a line; edit by hand")
            spans.append((leading_comments(lines, start), end))
            found.add(str(label))
    missing = set(keys) - found
    if missing:
        sys.exit(f"{path}:{var}: keys not found: {', '.join(sorted(missing))}")
    for start, end in sorted(spans, reverse=True):
        del lines[start:end]
    path.write_text("".join(lines))


def cmd_import(path: Path, names: list[str]) -> None:
    text = path.read_text()
    lines = text.splitlines(keepends=True)
    tree = ast.parse(text)
    edits: list[tuple[int, int, str | None]] = []
    found = set()
    nodes = []
    for node in tree.body:
        if isinstance(node, ast.If):
            # ``if TYPE_CHECKING:`` blocks hold imports too.
            nodes.extend(child for child in node.body if isinstance(child, (ast.Import, ast.ImportFrom)))
        else:
            nodes.append(node)
    for node in nodes:
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        def hit(alias: ast.alias) -> bool:
            return bool({alias.name, alias.name.split(".")[-1], alias.asname or ""} & set(names))

        keep = [alias for alias in node.names if not hit(alias)]
        dropped = {alias.name.split(".")[-1] for alias in node.names if hit(alias)} | {
            alias.asname for alias in node.names if hit(alias) and alias.asname
        }
        if not dropped:
            continue
        found |= dropped
        if not keep:
            edits.append((node.lineno - 1, node.end_lineno, None))  # type: ignore[arg-type]
            continue
        rendered = ", ".join(alias.name + (f" as {alias.asname}" if alias.asname else "") for alias in keep)
        first = lines[node.lineno - 1]
        indent = first[: len(first) - len(first.lstrip())]
        comment = "  " + first[first.index("#"):].rstrip("\n") if "#" in first else ""
        if isinstance(node, ast.ImportFrom):
            head = indent + f"from {'.' * node.level}{node.module or ''} import "
            single = head + rendered + comment + "\n"
            if len(single) <= 121 and node.lineno == node.end_lineno:
                replacement = single
            elif len(single) <= 121 and len(keep) == 1:
                replacement = single
            else:
                body = "".join(
                    f"    {alias.name}{f' as {alias.asname}' if alias.asname else ''},\n" for alias in keep
                )
                replacement = head + "(" + comment + "\n" + body + ")\n"
        else:
            replacement = indent + "import " + rendered + comment + "\n"
        edits.append((node.lineno - 1, node.end_lineno, replacement))  # type: ignore[arg-type]
    missing = set(names) - found
    if missing:
        sys.exit(f"{path}: imports not found: {', '.join(sorted(missing))}")
    for start, end, replacement in sorted(edits, reverse=True):
        lines[start:end] = [replacement] if replacement else []
    text = "".join(lines)
    # An ``if TYPE_CHECKING:`` block left without imports goes, and so does its import.
    import re as _re

    emptied = _re.sub(r"\n*if TYPE_CHECKING:\n(?=\S|\n|$)", "\n\n", text)
    if emptied != text:
        text = emptied
        if "TYPE_CHECKING" not in text.replace("from typing import TYPE_CHECKING\n", ""):
            text = text.replace("from typing import TYPE_CHECKING\n", "")
    path.write_text(text)


def main() -> None:
    command, file, *rest = sys.argv[1:]
    path = Path(file)
    if command == "def":
        cmd_def(path, rest)
    elif command == "member":
        cmd_member(path, rest[0], rest[1:])
    elif command == "key":
        cmd_key(path, rest[0], rest[1:])
    elif command == "import":
        cmd_import(path, rest)
    else:
        sys.exit(f"unknown command {command}")


if __name__ == "__main__":
    main()
