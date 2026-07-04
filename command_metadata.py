"""Load the versioned command metadata snapshot used by the interactive UI."""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
PROJECT_DIR = Path(__file__).resolve().parent
SNAPSHOT_PATH = PROJECT_DIR / "command_metadata.json"
REGENERATION_HINT = (
    "Regenerate it from a complete workspace with: "
    "python3 scripts/generate-command-metadata.py"
)


@dataclass(frozen=True)
class OptionMetadata:
    flag: str
    aliases: tuple[str, ...]
    takes_value: bool
    required: bool
    choices: tuple[str, ...]
    default: object
    help: str
    metavar: str | None

    def default_text(self) -> str | None:
        if self.default in (None, False):
            return None
        return str(self.default)


@dataclass(frozen=True)
class CommandMetadata:
    action: str
    options: dict[str, OptionMetadata]


def _error(message: str, path: Path) -> RuntimeError:
    return RuntimeError(f"Invalid command metadata snapshot at {path}: {message}. {REGENERATION_HINT}")


def _string_list(value: Any, field: str, path: Path) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise _error(f"{field} must be a list of strings", path)
    return tuple(value)


def load_metadata_snapshot(path: Path = SNAPSHOT_PATH) -> dict[str, CommandMetadata]:
    """Load and validate a snapshot without importing parser-owner repositories."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise _error("file is missing", path) from error
    except (OSError, json.JSONDecodeError) as error:
        raise _error(f"cannot be read as JSON ({error})", path) from error
    if not isinstance(raw, dict):
        raise _error("top-level value must be an object", path)
    if raw.get("schema_version") != SCHEMA_VERSION:
        raise _error(
            f"unsupported schema_version {raw.get('schema_version')!r}; expected {SCHEMA_VERSION}",
            path,
        )
    sources = _string_list(raw.get("sources"), "sources", path)
    if not sources:
        raise _error("sources must not be empty", path)
    commands_raw = raw.get("commands")
    if not isinstance(commands_raw, dict) or not commands_raw:
        raise _error("commands must be a non-empty object", path)

    commands: dict[str, CommandMetadata] = {}
    for action, command_raw in commands_raw.items():
        if not isinstance(action, str) or not isinstance(command_raw, dict):
            raise _error("each command must be an object keyed by its action", path)
        options_raw = command_raw.get("options")
        if not isinstance(options_raw, dict):
            raise _error(f"commands.{action}.options must be an object", path)
        options: dict[str, OptionMetadata] = {}
        for flag, option_raw in options_raw.items():
            field = f"commands.{action}.options.{flag}"
            if not isinstance(flag, str) or not flag.startswith("--") or not isinstance(option_raw, dict):
                raise _error(f"{field} must be an option object with a long flag key", path)
            if option_raw.get("flag") != flag:
                raise _error(f"{field}.flag must match its object key", path)
            takes_value = option_raw.get("takes_value")
            required = option_raw.get("required")
            help_text = option_raw.get("help")
            metavar = option_raw.get("metavar")
            if not isinstance(takes_value, bool) or not isinstance(required, bool):
                raise _error(f"{field} takes_value and required must be booleans", path)
            if not isinstance(help_text, str):
                raise _error(f"{field}.help must be a string", path)
            if metavar is not None and not isinstance(metavar, str):
                raise _error(f"{field}.metavar must be a string or null", path)
            aliases = _string_list(option_raw.get("aliases"), f"{field}.aliases", path)
            choices = _string_list(option_raw.get("choices"), f"{field}.choices", path)
            default = option_raw.get("default")
            if default is not None and not isinstance(default, (bool, int, float, str)):
                raise _error(f"{field}.default must be a JSON scalar or null", path)
            if flag not in aliases:
                raise _error(f"{field}.aliases must contain the canonical flag", path)
            options[flag] = OptionMetadata(
                flag=flag,
                aliases=aliases,
                takes_value=takes_value,
                required=required,
                choices=choices,
                default=default,
                help=help_text,
                metavar=metavar,
            )
        commands[action] = CommandMetadata(action=action, options=options)
    return commands


@lru_cache(maxsize=1)
def command_metadata() -> dict[str, CommandMetadata]:
    return load_metadata_snapshot()


def option_metadata(action: str, flag: str) -> OptionMetadata | None:
    command = command_metadata().get(action)
    if command is None:
        return None
    normalized = "--blocksci-script" if flag == "--blocksciScript" else flag
    for option in command.options.values():
        if normalized == option.flag or normalized in option.aliases:
            return option
    return None


def all_option_metadata(flag: str) -> list[OptionMetadata]:
    found: list[OptionMetadata] = []
    for command in command_metadata().values():
        option = option_metadata(command.action, flag)
        if option is not None and option not in found:
            found.append(option)
    return found


def parser_flags(action: str) -> set[str]:
    command = command_metadata().get(action)
    return set() if command is None else set(command.options)


def takes_value(flag: str) -> bool:
    options = all_option_metadata(flag)
    if not options:
        return False
    values = {option.takes_value for option in options}
    if len(values) != 1:
        raise RuntimeError(f"Metadata disagreement for {flag}: takes_value={sorted(values)}")
    return values.pop()
