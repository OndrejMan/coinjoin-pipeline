"""Every module a PBS job ships to a compute node is linted for the node's Python 3.8."""

from __future__ import annotations

import re
from fnmatch import fnmatch
from pathlib import Path

from coinjoin_pipeline.execution.mainnet_sync import MAINNET_RUNTIME_MODULES
from coinjoin_pipeline.execution.pbs.scripts import STAGE_RUNTIME_MODULES
from coinjoin_pipeline.execution.pbs.templates_s3 import BLOCK_ARCHIVE, NOTEBOOK_SERVER

REPO = Path(__file__).resolve().parents[2]


def py38_targets() -> set[str]:
    """The paths ``[tool.ruff.per-file-target-version]`` sets to py38 (tomllib needs Python 3.11)."""
    text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    section = text.split("[tool.ruff.per-file-target-version]\n", 1)[1].split("\n[", 1)[0]
    return set(re.findall(r'^"([^"]+)" = "py38"$', section, flags=re.MULTILINE))


def linted_for_python_38(path: str, targets: set[str]) -> bool:
    # Ruff's "**/" also matches no directory at all.
    return any(fnmatch(path, target) or fnmatch(path, target.replace("**/", "")) for target in targets)


def test_every_shipped_module_is_linted_for_python_38() -> None:
    shipped = (*STAGE_RUNTIME_MODULES, BLOCK_ARCHIVE, NOTEBOOK_SERVER, *MAINNET_RUNTIME_MODULES)
    targets = py38_targets()
    unchecked = {
        relative
        for module in shipped
        if not linted_for_python_38(relative := module.resolve().relative_to(REPO).as_posix(), targets)
    }
    assert not unchecked, f"add to [tool.ruff.per-file-target-version] as py38: {sorted(unchecked)}"
