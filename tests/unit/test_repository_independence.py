from __future__ import annotations

from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
TEXT_SUFFIXES = {".md", ".py", ".sh", ".yaml", ".yml", ".toml", ".json"}
GENERATED_DIRS = {
    "build",
    ".venv",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "__pycache__",
    "coinjoin-runs",
    "emulation_logs",
    "gitlab-test-results",
}
FORBIDDEN = (
    "../bitcoinAnalysis",
    "../blocksciEmulatorAnalysis",
    "/bitcoinAnalysis/",
    "/blocksciEmulatorAnalysis/",
    "ghcr.io/ondrejman/blocksciemulatoranalysis",
    "ghcr.io/ondrejman/bitcoin-analysis-pipeline",
)


class RepositoryIndependenceTests(unittest.TestCase):
    def test_no_runtime_or_test_dependency_on_original_controller_repositories(self) -> None:
        violations: list[str] = []
        for path in PROJECT_ROOT.rglob("*"):
            if (
                not path.is_file()
                or path.suffix not in TEXT_SUFFIXES
                or path.resolve() == Path(__file__).resolve()
                or ".git" in path.parts
                or any(part in GENERATED_DIRS for part in path.parts)
                or path.name == "MIGRATION.md"
            ):
                continue
            content = path.read_text(encoding="utf-8", errors="replace")
            # This directory name is a stable public report contract, not a
            # source-repository dependency.
            content = content.replace("blocksciEmulatorAnalysis_data", "REPORT_DATA")
            for forbidden in FORBIDDEN:
                if forbidden in content:
                    violations.append(f"{path.relative_to(PROJECT_ROOT)}: {forbidden}")
        self.assertEqual(violations, [], "Original controller dependency found:\n" + "\n".join(violations))


if __name__ == "__main__":
    unittest.main()
