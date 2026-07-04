#!/usr/bin/env python3
"""Generate or verify command metadata from the merged live parsers."""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path
from types import ModuleType
from typing import Any


SCHEMA_VERSION = 1
PROJECT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_WRAPPER_ROOT = PROJECT_DIR / "pipeline"
DEFAULT_OUTPUT = PROJECT_DIR / "command_metadata.json"
PARSER_ENVIRONMENT_KEYS = (
    "PBS_BITCOIN_DATADIR",
    "CONTAINER_RUNTIME",
    "EMULATION_LOGS_DIR",
)


@contextlib.contextmanager
def sanitized_parser_environment():
    """Hide machine-specific parser defaults and restore the exact environment."""
    missing = object()
    previous = {key: os.environ.get(key, missing) for key in PARSER_ENVIRONMENT_KEYS}
    for key in PARSER_ENVIRONMENT_KEYS:
        os.environ.pop(key, None)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is missing:
                os.environ.pop(key, None)
            else:
                os.environ[key] = str(value)


def load_module(name: str, path: Path, import_root: Path) -> ModuleType:
    if not path.is_file():
        raise FileNotFoundError(f"Parser source not found: {path}")
    root = str(import_root)
    if root not in sys.path:
        sys.path.insert(0, root)
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load parser source: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def subparsers(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    return {}


def normalize_default(value: object) -> object:
    if value == argparse.SUPPRESS:
        return None
    if isinstance(value, Path):
        return str(value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def option_record(action: argparse.Action[Any]) -> tuple[str, dict[str, object]] | None:
    long_flags = [flag for flag in action.option_strings if flag.startswith("--")]
    if not long_flags or "--help" in long_flags or "--runtime" in long_flags:
        return None
    canonical = "--blocksci-script" if "--blocksci-script" in long_flags else long_flags[0]
    aliases = sorted(set(long_flags))
    takes_value = not isinstance(
        action,
        (argparse._StoreTrueAction, argparse._StoreFalseAction, argparse._HelpAction),
    )
    return canonical, {
        "aliases": aliases,
        "choices": [str(choice) for choice in (action.choices or ())],
        "default": normalize_default(action.default),
        "flag": canonical,
        "help": "" if action.help in (None, argparse.SUPPRESS) else str(action.help),
        "metavar": str(action.metavar) if action.metavar is not None else None,
        "required": bool(action.required),
        "takes_value": takes_value,
    }


def parser_options(parser: argparse.ArgumentParser) -> dict[str, dict[str, object]]:
    options: dict[str, dict[str, object]] = {}
    for action in parser._actions:
        record = option_record(action)
        if record is not None:
            flag, metadata = record
            options[flag] = metadata
    return dict(sorted(options.items()))


def generate_snapshot(wrapper_root: Path) -> dict[str, object]:
    with sanitized_parser_environment():
        wrapper = load_module(
            "_metadata_generator_wrapper",
            wrapper_root / "client" / "wrapper.py",
            wrapper_root,
        )
        research = load_module(
            "_metadata_generator_research",
            wrapper_root / "client" / "research.py",
            wrapper_root,
        )
        commands: dict[str, dict[str, object]] = {}
        for action, parser in subparsers(wrapper.build_parser()).items():
            if action != "coinjoin":
                commands[action] = {"options": parser_options(parser)}
        research_groups = subparsers(research.parser())
        for group in ("runs", "scenarios", "external"):
            for subcommand, parser in subparsers(research_groups[group]).items():
                commands[f"{group} {subcommand}"] = {"options": parser_options(parser)}
    return {
        "commands": dict(sorted(commands.items())),
        "schema_version": SCHEMA_VERSION,
        "sources": ["client/research.py", "client/wrapper.py"],
    }


def rendered(snapshot: dict[str, object]) -> str:
    return json.dumps(snapshot, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def changed_items(old: dict[str, object], new: dict[str, object]) -> list[str]:
    old_commands = old.get("commands", {}) if isinstance(old, dict) else {}
    new_commands = new.get("commands", {})
    if not isinstance(old_commands, dict) or not isinstance(new_commands, dict):
        return ["snapshot structure"]
    changes: list[str] = []
    for action in sorted(set(old_commands) | set(new_commands)):
        if action not in old_commands:
            changes.append(f"added command: {action}")
            continue
        if action not in new_commands:
            changes.append(f"removed command: {action}")
            continue
        old_command = old_commands[action]
        new_command = new_commands[action]
        if not isinstance(old_command, dict) or not isinstance(new_command, dict):
            changes.append(f"invalid command record: {action}")
            continue
        old_options = old_command.get("options", {})
        new_options = new_command.get("options", {})
        if not isinstance(old_options, dict) or not isinstance(new_options, dict):
            changes.append(f"invalid options record: {action}")
            continue
        for flag in sorted(set(old_options) | set(new_options)):
            if flag not in old_options:
                changes.append(f"added option: {action} {flag}")
            elif flag not in new_options:
                changes.append(f"removed option: {action} {flag}")
            elif old_options[flag] != new_options[flag]:
                changes.append(f"changed option: {action} {flag}")
    if not changes and old != new:
        changes.append("snapshot metadata")
    return changes


def read_existing(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as stream:
        stream.write(content)
        temporary = Path(stream.name)
    os.replace(temporary, path)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Fail if the snapshot is stale.")
    parser.add_argument("--wrapper-root", type=Path, default=DEFAULT_WRAPPER_ROOT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        snapshot = generate_snapshot(args.wrapper_root.expanduser().resolve())
    except (FileNotFoundError, ImportError, RuntimeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    output = args.output.expanduser().resolve()
    existing = read_existing(output)
    changes = changed_items(existing, snapshot)
    if args.check:
        if changes:
            print(f"ERROR: command metadata snapshot is stale: {output}", file=sys.stderr)
            for change in changes:
                print(f"  - {change}", file=sys.stderr)
            print("Run: python3 scripts/generate-command-metadata.py", file=sys.stderr)
            return 1
        print(f"OK: command metadata snapshot matches live parsers: {output}")
        return 0
    write_atomic(output, rendered(snapshot))
    if changes:
        print(f"Updated command metadata snapshot: {output}")
        for change in changes:
            print(f"  - {change}")
    else:
        print(f"Command metadata snapshot already current: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
