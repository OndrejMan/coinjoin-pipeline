"""Manual restore → Core/Dumplings → BlockSci → checkpoint PBS chain over a shared working directory."""

from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from coinjoin_pipeline.configuration import required
from coinjoin_pipeline.paths import EXPORTERS_ROOT
from coinjoin_pipeline.storage.s3 import shell_value, validate_artifact_uri, validate_run_id
from .pbs.defaults import MAINNET_SYNC_RESOURCES, PBSResources
from .pbs.scripts import INDEX_STORE, S5CLIENT, directives, runtime_source, write_files
from .pbs.submission import PBSJobSpec, persist_pbs_job_id, submit_pbs_job
from .pbs.validation import PBSError, require_safe_image, require_safe_pbs_resources, walltime_to_seconds
from .pbs_settings import pbs_image, stage_pbs_resources, with_singularity_scheme
from .s3_markers import rollback_s3_pbs_submissions

if TYPE_CHECKING:
    from coinjoin_pipeline.configuration import PipelineConfiguration

# Submission order; each job depends on the previous one.
SYNC_STAGES = ("restore", "producer", "blocksci", "checkpoint")


# Top-level settings mainnet-sync reads besides its mainnet.* and per-job pbs.<stage>.* groups.
MAINNET_SYNC_OPTIONS = frozenset(
    {
        "action",
        "run_id",
        "runs_root",
        "dry_run",
        "run_timezone",
        "driver",
        "images.blocksci",
        "images.version",
        "blocksci.index_dir",
        "blocksci.index_backup_uri",
        "artifacts.backend",
        "artifacts.endpoint_url",
        "artifacts.credentials_file",
        "artifacts.profile",
        "pbs.ncpus",
        "pbs.mem",
        "pbs.scratch",
        "pbs.scratch_type",
        "pbs.walltime",
        "pbs.image",
        "pbs.blocksci_image",
    }
)


def configuration_errors(config: PipelineConfiguration) -> list[str]:
    errors: list[str] = []
    stage_groups = tuple(f"pbs.{stage}." for stage in SYNC_STAGES)
    for path in sorted(config.provided):
        if path not in MAINNET_SYNC_OPTIONS and not path.startswith(("mainnet.", *stage_groups)):
            errors.append(f"{path} is not used by mainnet-sync")
    errors.extend(_location_errors(config))
    errors.extend(_working_directory_errors(config))
    if any((config.stages.analysis, config.stages.blocksci, config.stages.mappings)):
        errors.append("mainnet-sync owns its jobs; omit stages.*")
    if config.driver != "docker":
        errors.append("mainnet-sync runs Apptainer PBS jobs; omit driver: kubernetes")
    if config.mainnet.network == "bitcoin" and config.mainnet.scan_start_height is not None:
        errors.append("scan_start_height is only for isolated regtest validation")
    for stage in SYNC_STAGES:
        errors.extend(
            _failure(require_safe_pbs_resources, resources(config, stage), getattr(config.pbs, stage).multijob)
        )
    for image in (config.mainnet.core_image, config.mainnet.dumplings_image):
        errors.extend(_failure(require_safe_image, image))
    return errors


def _failure(check: Callable[..., object], *values: object) -> list[str]:
    """The message of a validator that raises on a bad value, or nothing."""
    try:
        check(*values)
    except (ValueError, PBSError) as error:
        return [str(error)]
    return []


def _location_errors(config: PipelineConfiguration) -> list[str]:
    """The run ID, the S3 prefixes and the credentials the jobs read them with."""
    errors: list[str] = []
    mainnet = config.mainnet
    for label, value, validator in (
        ("run_id", config.run_id, validate_run_id),
        ("mainnet.state_uri", mainnet.state_uri, validate_artifact_uri),
        ("mainnet.backup_uri", mainnet.backup_uri, validate_artifact_uri),
        ("mainnet.blocks_uri", mainnet.blocks_uri, validate_artifact_uri),
        ("mainnet.dumplings_uri", mainnet.dumplings_uri, validate_artifact_uri),
        ("blocksci.index_backup_uri", config.blocksci.index_backup_uri, validate_artifact_uri),
    ):
        if value is None:
            errors.append(f"mainnet-sync requires {label}")
        else:
            errors.extend(f"{label}: {message}" for message in _failure(validator, value))
    for name in ("endpoint_url", "credentials_file", "profile"):
        if not getattr(config.artifacts, name):
            errors.append(f"mainnet-sync requires artifacts.{name}")
    prefixes = (
        mainnet.state_uri,
        mainnet.backup_uri,
        config.blocksci.index_backup_uri,
        mainnet.dumplings_uri,
        mainnet.blocks_uri,
    )
    present = [value.rstrip("/") for value in prefixes if value]
    if any(a == b or b.startswith(a + "/") for a in present for b in present if a is not b):
        errors.append("The state, Core, index, Dumplings and block prefixes must be distinct, without nesting")
    return errors


def _working_directory_errors(config: PipelineConfiguration) -> list[str]:
    """The Core and index directories share one multijob scratch but must not overlap."""
    root, index = config.mainnet.root, config.blocksci.index_dir
    if not root or not index:
        return ["mainnet-sync requires mainnet.root and blocksci.index_dir"]
    errors: list[str] = []
    for path in (root, index):
        if not Path(path).is_absolute() or ".." in Path(path).parts or len(Path(path).parts) < 3:
            errors.append("Working directories must be absolute, below a parent directory, without '..'")
    if Path(root) == Path(index) or Path(root) in Path(index).parents or Path(index) in Path(root).parents:
        errors.append("Core and index working directories must be separate, without nesting")
    locations = {getattr(config.pbs, stage).multijob for stage in SYNC_STAGES}
    if len(locations) != 1:
        errors.append("All jobs must request the same multijob location")
    for path in (root, index):
        if path.startswith("/scratch/multijob-"):
            location = path.split("/")[2].removeprefix("multijob-")
            if locations != {location}:
                errors.append(f"{path} requires multijob: {location} on all jobs")
    return errors


def resources(config: PipelineConfiguration, stage: str) -> PBSResources:
    return stage_pbs_resources(config, stage, default=MAINNET_SYNC_RESOURCES[stage])


def render_sync(config: PipelineConfiguration, stage: str) -> str:
    """Self-contained job: only small helpers/config are copied, data stay in place."""
    budget = resources(config, stage)
    run_id = required(config.run_id, "run_id")
    credentials = required(config.artifacts.credentials_file, "artifacts.credentials_file")
    settings = {
        **asdict(config.mainnet),
        "run_id": run_id,
        "log_root": str(config.runs_path / run_id / "logs"),
        "endpoint": config.artifacts.endpoint_url,
        "credentials": credentials,
        "profile": config.artifacts.profile,
        "index_dir": config.blocksci.index_dir,
        "index_backup_uri": config.blocksci.index_backup_uri,
        "blocksci_image": pbs_image(config, "blocksci"),
        "pbs_resources": {"ncpus": budget.ncpus, "mem": budget.mem},
        "core_image": with_singularity_scheme(config.mainnet.core_image),
        "dumplings_image": with_singularity_scheme(config.mainnet.dumplings_image),
        # Restore and BlockSci write the index; the checkpoint only reads it, so readers stay.
        "index_lock": {"restore": "exclusive", "blocksci": "exclusive", "checkpoint": "shared"}.get(stage),
        # Past the walltime the job is gone, so its lock entry may be cleared.
        "index_lock_seconds": walltime_to_seconds(budget.walltime) + 600,
    }
    files = {
        "settings.json": json.dumps(settings),
        "worker.py": runtime_source(Path(__file__).with_name("mainnet_runtime.py")),
        "s5client.py": runtime_source(S5CLIENT),
        "index_store.py": runtime_source(INDEX_STORE),
        "verify_chain.py": runtime_source(EXPORTERS_ROOT / "verify_chain.py"),
    }
    identity = f"STAGE=mainnet-{stage}\nRUN_ID={shell_value(run_id)}\n"
    return f"""{directives("mainnet_" + stage, budget, getattr(config.pbs, stage).multijob)}
set -euo pipefail
umask 077
export PATH="/usr/local/bin:$PATH"
{identity}
test -n "${{SCRATCHDIR:-}}" || {{ echo 'PBS SCRATCHDIR is required' >&2; exit 1; }}
WORK=$(mktemp -d "$SCRATCHDIR/mainnet.XXXXXX")
export TMPDIR="$WORK" SINGULARITY_CACHEDIR="$WORK/images" SINGULARITY_TMPDIR="$WORK"
export APPTAINER_CACHEDIR="$WORK/images" APPTAINER_TMPDIR="$WORK"
{write_files("$WORK", files)}
STARTED=0
finish() {{
  status=$?
  trap - EXIT TERM INT
  if [ "$status" -ne 0 ]; then
    python3 "$WORK/worker.py" "$WORK/settings.json" failed "$STAGE" "$STARTED" || true
  fi
  # WORK is job-private; never clean the shared data directories.
  exit "$status"
}}
trap finish EXIT
trap 'exit 143' TERM
trap 'exit 130' INT
test -r {shell_value(credentials)} || {{ echo 'S3 credentials file is not readable' >&2; exit 1; }}
for tool in s5cmd tar zstd; do command -v "$tool" >/dev/null || {{ echo "Required tool missing: $tool" >&2; exit 1; }}; done
s5cmd pipe --help >/dev/null || {{ echo 's5cmd with pipe support is required' >&2; exit 1; }}
STARTED=1
python3 "$WORK/worker.py" "$WORK/settings.json" {stage}
"""


def submit_sync(config: PipelineConfiguration) -> None:
    """Persist every ID immediately and roll back partial submissions.

    The S3 state prefix is fixed: the restore job clears the previous run's state once it
    holds the dataset lock, so submission checks nothing there.
    """
    run_id = required(config.run_id, "run_id")
    scripts = {stage: render_sync(config, stage) for stage in SYNC_STAGES}
    submitted: list[tuple[str, str]] = []
    try:
        for stage, script in scripts.items():
            spec = PBSJobSpec(f"mainnet-{stage}", script, tuple(job for _, job in submitted[-1:]))
            job = submit_pbs_job(spec, config.dry_run)
            if job:
                submitted.append((spec.stage, job))
                persist_pbs_job_id(config.runs_path / run_id, spec.stage, job)
    except BaseException as error:
        failed = rollback_s3_pbs_submissions(submitted)
        if failed:
            raise RuntimeError(f"Submission failed; could not cancel PBS jobs {[job for _, job in failed]}") from error
        raise
