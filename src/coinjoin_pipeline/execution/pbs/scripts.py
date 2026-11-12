"""Shared pieces of every rendered PBS job script."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from coinjoin_pipeline.paths import EXPORTERS_ROOT
from coinjoin_pipeline.storage.s3 import validate_artifact_uri

from .defaults import PBSResources
from .validation import PBSError, require_safe_pbs_resources

if TYPE_CHECKING:
    from coinjoin_pipeline.configuration import MappingsConfiguration

TEMPLATES = Path(__file__).parent / "templates"
# src/ and pipeline/: a job ships each module at its path below the one that holds it,
# so the node imports it as the frontend does.
SOURCE_ROOTS = (Path(__file__).resolve().parents[3], EXPORTERS_ROOT.parent)
# Modules a job ships to the node; each stays stdlib-only Python 3.8.
S5CLIENT = Path(__file__).parents[2] / "storage" / "s5client.py"
INDEX_STORE = Path(__file__).with_name("index_store.py")
APPTAINER = Path(__file__).with_name("apptainer.py")
ARTIFACT_PATHS = EXPORTERS_ROOT / "artifact_paths.py"
# The S3 stage worker: its entry point and every module it imports.
STAGE_RUNTIME_MODULES = (
    S5CLIENT,
    INDEX_STORE,
    APPTAINER,
    ARTIFACT_PATHS,
    *(Path(__file__).with_name(name) for name in ("stage_settings.py", "stage.py", "analyzer_jobs.py")),
    *(
        Path(__file__).with_name(name)
        for name in ("blocksci_index.py", "blocksci_jobs.py", "blocksci_analysis_jobs.py", "stage_runtime.py")
    ),
)


def template(name: str) -> str:
    return (TEMPLATES / name).read_text(encoding="utf-8")


def _delimiter(name: str) -> str:
    return "COINJOIN_FILE_" + name.upper().replace(".", "_").replace("-", "_").replace("/", "_")


def write_files(directory: str, files: dict[str, str]) -> str:
    """Shell that writes each file, a name relative to directory, from a quoted here-document."""
    parents = sorted({str(PurePosixPath(name).parent) for name in files} - {"."})
    lines = [" ".join(("mkdir -p", *(f'"{directory}/{parent}"' for parent in parents)))] if parents else []
    for name, content in files.items():
        delimiter = _delimiter(name)
        if f"\n{delimiter}\n" in f"\n{content}\n":
            raise PBSError(f"{name} contains its own here-document delimiter")
        lines.append(f"cat >\"{directory}/{name}\" <<'{delimiter}'\n{content.rstrip(chr(10))}\n{delimiter}")
    return "\n".join(lines)


def embedded_file(script: str, name: str) -> str:
    """The content write_files embedded for name, read back from a rendered script."""
    delimiter = _delimiter(name)
    return script.split(f"<<'{delimiter}'\n", 1)[1].split(f"\n{delimiter}\n", 1)[0]


def runtime_source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _source_path(module: Path) -> PurePosixPath:
    module = module.resolve()
    for root in SOURCE_ROOTS:
        if module.is_relative_to(root):
            return PurePosixPath(module.relative_to(root).as_posix())
    raise ValueError(f"{module} is outside every shipped source root")


def package_files(*modules: Path) -> dict[str, str]:
    """Each module under its path below src/ or pipeline/, with the package markers its imports need.

    The markers are empty: the real ``__init__.py`` of a package imports frontend code.
    """
    files: dict[str, str] = {}
    for module in modules:
        relative = _source_path(module)
        for package in reversed(relative.parents[:-1]):
            files.setdefault(f"{package}/__init__.py", "")
        files[str(relative)] = runtime_source(module)
    return files


def launcher(module: str) -> str:
    """``run.py``, which runs ``main`` of module; Python puts its directory first on sys.path."""
    return f"import sys\n\nfrom {module} import main\n\nraise SystemExit(main(sys.argv[1:]))\n"


def directives(job_name: str, resources: PBSResources, multijob: str | None = None) -> str:
    """The ``#PBS`` header: one chunk with the requested resources, output joined to the job log."""
    require_safe_pbs_resources(resources, multijob)
    select = (
        f"select=1:ncpus={resources.ncpus}:mem={resources.mem}:scratch_{resources.scratch_type}={resources.scratch}"
    )
    return "\n".join(
        (
            "#!/bin/bash",
            f"#PBS -N {job_name}",
            f"#PBS -l {select}{f':multijob={multijob}' if multijob else ''}",
            f"#PBS -l walltime={resources.walltime}",
            "#PBS -j oe",
        )
    )


def mappings_run(settings: MappingsConfiguration) -> str:
    """The enumerator and Sake run shared by the S3 and the shared-storage mappings jobs."""
    return template("mappings_run.sh").format(**asdict(settings))


def validated_s3_uri(value: str, description: str) -> str:
    try:
        return validate_artifact_uri(value)
    except ValueError as error:
        raise PBSError(f"Invalid {description}: {error}") from error
