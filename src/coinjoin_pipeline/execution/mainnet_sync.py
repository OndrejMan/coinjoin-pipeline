"""Manual Core/Dumplings → BlockSci PBS graph over a shared working directory."""

from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
from typing import TYPE_CHECKING

from coinjoin_pipeline.paths import EXPORTERS_ROOT
from coinjoin_pipeline.storage.s3 import (
    S3Target,
    ensure_empty_run_prefix,
    shell_value,
    validate_artifact_uri,
    validate_run_id,
)
from .pbs.defaults import MAINNET_SYNC_RESOURCES, PBSResources
from .pbs.scripts import directives, template
from .pbs.submission import PBSJobSpec, persist_pbs_job_id, submit_pbs_job
from .pbs.validation import PBSError, require_safe_image, require_safe_pbs_resources, walltime_to_seconds
from .pbs_settings import pbs_image, stage_pbs_resources, with_singularity_scheme
from .s3_markers import rollback_s3_pbs_submissions

if TYPE_CHECKING:
    from coinjoin_pipeline.configuration import PipelineConfiguration


def configuration_errors(config: PipelineConfiguration) -> list[str]:
    errors: list[str] = []
    allowed = {
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
        "artifacts.uri",
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
    for path in sorted(config.provided):
        if path not in allowed and not path.startswith(("mainnet.", "pbs.producer.", "pbs.blocksci.")):
            errors.append(f"{path} is not used by mainnet-sync")
    for label, value, validator in (
        ("run_id", config.run_id, validate_run_id),
        ("mainnet.backup_uri", config.mainnet.backup_uri, validate_artifact_uri),
        ("mainnet.blocks_uri", config.mainnet.blocks_uri, validate_artifact_uri),
        ("mainnet.dumplings_uri", config.mainnet.dumplings_uri, validate_artifact_uri),
        ("blocksci.index_backup_uri", config.blocksci.index_backup_uri, validate_artifact_uri),
        ("artifacts.uri", config.artifacts.uri, validate_artifact_uri),
    ):
        if value is None:
            errors.append(f"mainnet-sync requires {label}")
            continue
        try:
            validator(value)
        except (ValueError, PBSError) as error:
            errors.append(f"{label}: {error}")
    for name in ("endpoint_url", "credentials_file", "profile"):
        if not getattr(config.artifacts, name):
            errors.append(f"mainnet-sync requires artifacts.{name}")
    backups = (config.mainnet.backup_uri, config.blocksci.index_backup_uri, config.mainnet.dumplings_uri)
    if len({value.rstrip("/") for value in backups if value}) != len(backups):
        errors.append("Core, index and Dumplings backup prefixes must be distinct")
    root, index = config.mainnet.root, config.blocksci.index_dir
    if not root or not index:
        errors.append("mainnet-sync requires mainnet.root and blocksci.index_dir")
    else:
        for path in (root, index):
            if not Path(path).is_absolute() or ".." in Path(path).parts or len(Path(path).parts) < 3:
                errors.append("Working directories must be absolute, below a parent directory, without '..'")
        if Path(root) == Path(index) or Path(root) in Path(index).parents or Path(index) in Path(root).parents:
            errors.append("Core and index working directories must be separate, without nesting")
        locations = {config.pbs.producer.multijob, config.pbs.blocksci.multijob}
        if len(locations) != 1:
            errors.append("Both jobs must request the same multijob location")
        for path in (root, index):
            if path.startswith("/scratch/multijob-"):
                location = path.split("/")[2].removeprefix("multijob-")
                if locations != {location}:
                    errors.append(f"{path} requires multijob: {location} on both jobs")
    if any((config.stages.analysis, config.stages.blocksci, config.stages.mappings)):
        errors.append("mainnet-sync owns its two stages; omit stages.*")
    if config.driver != "docker":
        errors.append("mainnet-sync runs Apptainer PBS jobs; omit driver: kubernetes")
    if config.mainnet.network == "bitcoin" and config.mainnet.scan_start_height is not None:
        errors.append("scan_start_height is only for isolated regtest validation")
    for stage in ("producer", "blocksci"):
        try:
            require_safe_pbs_resources(resources(config, stage), getattr(config.pbs, stage).multijob)
        except (ValueError, PBSError) as error:
            errors.append(str(error))
    for image in (config.mainnet.core_image, config.mainnet.dumplings_image):
        try:
            require_safe_image(image)
        except (ValueError, PBSError) as error:
            errors.append(str(error))
    return errors


def resources(config: PipelineConfiguration, stage: str) -> PBSResources:
    return stage_pbs_resources(config, stage, default=MAINNET_SYNC_RESOURCES[stage])


def render_sync(config: PipelineConfiguration, stage: str) -> str:
    """Self-contained job: only small helpers/config are copied, data stay in place."""
    budget = resources(config, stage)
    target = S3Target.from_args(config)
    settings = {
        **asdict(config.mainnet),
        "run_id": target.run_id,
        "artifact_uri": target.artifact_uri,
        "endpoint": target.endpoint_url,
        "credentials": target.credentials_file,
        "profile": target.profile,
        "index_dir": config.blocksci.index_dir,
        "index_backup_uri": config.blocksci.index_backup_uri,
        "blocksci_image": pbs_image(config, "blocksci"),
        "pbs_resources": {"ncpus": budget.ncpus, "mem": budget.mem},
        "core_image": with_singularity_scheme(config.mainnet.core_image),
        "dumplings_image": with_singularity_scheme(config.mainnet.dumplings_image),
    }
    runtime = Path(__file__).with_name("mainnet_runtime.py").read_text(encoding="utf-8")
    verifier = (EXPORTERS_ROOT / "verify_chain.py").read_text(encoding="utf-8")
    identity = f"STAGE=mainnet-{stage}\nRUN_ID={shell_value(target.run_id)}\n"
    index_lock = ""
    if stage == "blocksci":
        index_lock = f"""
INDEX_DIR={shell_value(str(config.blocksci.index_dir))}
INDEX_LOCK_EXPIRES=$(($(date +%s) + {walltime_to_seconds(budget.walltime)} + 600))
# Fail immediately on an active notebook instead of holding a large allocation waiting.
INDEX_LOCK_WAIT=0
INDEX_LOCK_POLL=1
{template("index_store.sh")}
index_lock_exclusive
"""
    return f"""{directives("mainnet_" + stage, budget, getattr(config.pbs, stage).multijob)}
set -euo pipefail
umask 077
export PATH="/usr/local/bin:$PATH"
{identity}
test -n "${{SCRATCHDIR:-}}" || {{ echo 'PBS SCRATCHDIR is required' >&2; exit 1; }}
WORK=$(mktemp -d "$SCRATCHDIR/mainnet.XXXXXX")
export TMPDIR="$WORK" SINGULARITY_CACHEDIR="$WORK/images" SINGULARITY_TMPDIR="$WORK"
export APPTAINER_CACHEDIR="$WORK/images" APPTAINER_TMPDIR="$WORK"
cat >"$WORK/settings.json" <<'MAINNET_SETTINGS'
{json.dumps(settings)}
MAINNET_SETTINGS
cat >"$WORK/worker.py" <<'MAINNET_WORKER'
{runtime}
MAINNET_WORKER
cat >"$WORK/verify_chain.py" <<'MAINNET_VERIFY'
{verifier}
MAINNET_VERIFY
STARTED=0
finish() {{
  status=$?
  trap - EXIT TERM INT
  if [ "$status" -ne 0 ]; then
    python3 "$WORK/worker.py" "$WORK/settings.json" failed "$STAGE" "$STARTED" || true
  fi
  if declare -F index_unlock >/dev/null; then index_unlock; fi
  # WORK is job-private; never clean the shared data directories.
  exit "$status"
}}
trap finish EXIT
trap 'exit 143' TERM
trap 'exit 130' INT
test -r {shell_value(target.credentials_file)} || {{ echo 'S3 credentials file is not readable' >&2; exit 1; }}
for tool in s5cmd tar zstd; do command -v "$tool" >/dev/null || {{ echo "Required tool missing: $tool" >&2; exit 1; }}; done
s5cmd pipe --help >/dev/null || {{ echo 's5cmd with pipe support is required' >&2; exit 1; }}
{index_lock}
STARTED=1
python3 "$WORK/worker.py" "$WORK/settings.json" {stage}
"""


def submit_sync(config: PipelineConfiguration) -> None:
    """Persist both IDs immediately and roll back partial submissions."""
    target = S3Target.from_args(config)
    scripts = {stage: render_sync(config, stage) for stage in ("producer", "blocksci")}
    if not config.dry_run:
        ensure_empty_run_prefix(target.access, target.artifact_uri, target.run_id)
    submitted: list[tuple[str, str]] = []
    try:
        for stage, script in scripts.items():
            spec = PBSJobSpec(f"mainnet-{stage}", script, tuple(job for _, job in submitted[-1:]))
            job = submit_pbs_job(spec, config.dry_run)
            if job:
                submitted.append((spec.stage, job))
                persist_pbs_job_id(config.runs_path / target.run_id, spec.stage, job)
    except BaseException as error:
        failed = rollback_s3_pbs_submissions(submitted)
        if failed:
            raise RuntimeError(f"Submission failed; could not cancel PBS jobs {[job for _, job in failed]}") from error
        raise
