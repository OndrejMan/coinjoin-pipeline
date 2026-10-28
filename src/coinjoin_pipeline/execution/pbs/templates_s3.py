"""S3-backed PBS script renderers.

Every job renders ``directives`` + the stage identity + ``templates/s3_stage.sh`` (the
``s5``, ``require_file`` and marker helpers) as its preamble and installs the exit
trap of ``templates/stage_trap.sh`` in ``{start}``. Stage templates add only their
own work.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import NamedTuple

from exporters.artifact_paths import COINJOIN_ANALYSIS_TOOLS_DIR
from exporters.verify_chain import is_block_hash

from coinjoin_pipeline.configuration import CoinjoinAnalysisConfiguration, MappingsConfiguration
from coinjoin_pipeline.execution.coinjoin_tools import (
    RESEARCH_PROGRAMS,
    command as coinjoin_tools_command,
    command_argv,
    uses_run_input,
)
from coinjoin_pipeline.paths import EXPORTERS_ROOT
from coinjoin_pipeline.storage.s3 import (
    S3Target,
    shell_value,
    validate_artifact_uri,
    validate_credentials_file,
    validate_run_id,
    validate_s3_endpoint_url,
    validate_s3_profile,
)

from .analysis_modes import MODE_OUTPUTS, prepare_analysis_mode
from .defaults import BLOCKSCI_IMAGE_PYTHON_COMMAND, DEFAULT_RESOURCES, PBS_MULTIJOB_LOCATIONS, PBSResources
from .scripts import directives, download_run_directory, mappings_run, template, validated_s3_uri
from .validation import (
    PBSError,
    require_existing_path,
    require_notebook_port,
    require_safe_image,
    require_safe_pbs_token,
    require_storage_path,
    walltime_to_seconds,
)

EXECUTION_DIR = Path(__file__).parent.parent
EXTERNAL_NETWORKS = {"bitcoin", "bitcoin_testnet", "bitcoin_regtest"}
BLOCKSCI = DEFAULT_RESOURCES["blocksci"]


def _run_id(value: str) -> str:
    try:
        return validate_run_id(value)
    except ValueError as error:
        raise PBSError(str(error)) from error


def _upload(source: str, name: str) -> str:
    return f's5 sync {source} "$ARTIFACT_URI/$RUN_ID/{name}/"'


def _render_stage(
    template_name: str,
    stage: str,
    target: S3Target,
    resources: PBSResources,
    /,
    *,
    image: str | None = None,
    multijob: str | None = None,
    **values: object,
) -> str:
    """Validate the shared job inputs and render one stage's script template."""
    require_safe_pbs_token(stage, "PBS stage")
    identity = [
        f"ARTIFACT_URI={shell_value(validate_artifact_uri(target.artifact_uri))}",
        f"RUN_ID={shell_value(validate_run_id(target.run_id))}",
        f"S3_ENDPOINT_URL={shell_value(validate_s3_endpoint_url(target.endpoint_url))}",
        f"S3_CREDENTIALS_FILE={shell_value(validate_credentials_file(target.credentials_file))}",
        f"S3_PROFILE={shell_value(validate_s3_profile(target.profile))}",
        f"STAGE={stage}",
    ]
    if image is not None:
        require_safe_image(image)
        identity.append(f"IMAGE={shell_value(image)}")
    preamble = "\n".join(
        (
            directives(stage.replace("-", "_") + "_s3", resources, multijob),
            "set -euo pipefail\n",
            *identity,
            template("s3_stage.sh"),
            template("index_archive.sh") if template_name.startswith("s3_blocksci") else "",
        )
    )
    values.setdefault("finalize_index", ":")
    return template(template_name).format(
        preamble=preamble,
        start=template("stage_trap.sh") + template("s3_setup.sh"),
        **values,
    )


def _require_external_chain(network: str | None, max_block: int | None) -> str:
    if network is None or network not in EXTERNAL_NETWORKS:
        raise PBSError("External BlockSci network must be bitcoin, bitcoin_testnet, or bitcoin_regtest")
    if isinstance(max_block, bool) or not isinstance(max_block, int) or max_block < 0:
        raise PBSError("External Bitcoin parsing requires a non-negative --blocksci-max-block")
    return network


def _bitcoin_datadir_source(path: Path) -> str:
    """Point ``$BITCOIN_DATADIR`` at a shared Bitcoin Core coin directory."""
    bitcoin_path = path.expanduser().resolve()
    require_storage_path(bitcoin_path)
    require_existing_path(bitcoin_path, "external Bitcoin coin directory")
    if not (bitcoin_path / "blocks").is_dir():
        raise PBSError(f"External Bitcoin coin directory must contain blocks/: {bitcoin_path}")
    return (
        f"BITCOIN_DATADIR={shell_value(str(bitcoin_path))}\n"
        'test -d "$BITCOIN_DATADIR/blocks" || { '
        'echo "External Bitcoin coin directory must contain blocks/: $BITCOIN_DATADIR" >&2; exit 1; }'
    )


def _block_archive_source(bitcoin_blocks_uri: str) -> str:
    return f"BITCOIN_BLOCKS_URI={shell_value(validated_s3_uri(bitcoin_blocks_uri, 'Bitcoin block archive URI'))}"


def _download_block_archive(source_manifest: str | None = None) -> str:
    """Fetch only the archive files a parse or an update of `source_manifest` needs.

    Expects ``$BITCOIN_BLOCKS_URI``, ``$EXPORTED_MAX_BLOCK`` and ``$RUN_WORK``;
    leaves the files in ``$BITCOIN_DATADIR/blocks`` and the cache manifest
    fields that let a later update resume in ``$MANIFEST_EXTRA``.
    """
    helper = (EXECUTION_DIR / "block_archive.py").read_text(encoding="utf-8")
    update = f' --source-manifest "{source_manifest}"' if source_manifest is not None else ""
    return f"""BITCOIN_DATADIR="$RUN_WORK/bitcoin_data"
ARCHIVE_WORK="$RUN_WORK/block-archive"
mkdir -p "$BITCOIN_DATADIR/blocks" "$ARCHIVE_WORK/sidecars"
cat >"$ARCHIVE_WORK/block_archive.py" <<'PY'
{helper}PY
s5 cp "$BITCOIN_BLOCKS_URI/blk*.dat.json" "$ARCHIVE_WORK/sidecars/"
python3 "$ARCHIVE_WORK/block_archive.py" select "$ARCHIVE_WORK/sidecars" "$BITCOIN_DATADIR/blocks" \\
  --uri "$BITCOIN_BLOCKS_URI" --max-block "$EXPORTED_MAX_BLOCK"{update} \\
  --commands "$ARCHIVE_WORK/download.s5cmd" --manifest-fields "$ARCHIVE_WORK/manifest-fields.json"
s5 run "$ARCHIVE_WORK/download.s5cmd"
python3 "$ARCHIVE_WORK/block_archive.py" verify "$BITCOIN_DATADIR/blocks" --max-block "$EXPORTED_MAX_BLOCK"{update}
MANIFEST_EXTRA="$(cat "$ARCHIVE_WORK/manifest-fields.json")\""""


def _verify_block_archive_chain(network: str, expected_hash: str | None, *, update: bool = False) -> str:
    if network == "bitcoin" and expected_hash is None:
        raise PBSError("Mainnet block archives require --blocksci-expected-block-hash at --blocksci-max-block")
    if expected_hash is not None and not is_block_hash(expected_hash):
        raise PBSError("Expected block hash must be 64 lowercase hexadecimal characters")
    checkpoint = f" --expected-block-hash {shell_value(expected_hash)}" if expected_hash is not None else ""
    source = (
        ' --source-manifest "/runs/emulation/logs/$RUN_ID/source-blocksci-parse_data/manifest.json"' if update else ""
    )
    return f"""echo "[block-archive] verifying the parsed chain before cache publication"
singularity exec \\
  --bind "$RUNS_ROOT:/runs/emulation/logs:rw" \\
  --bind "$RUN_WORK/.pipeline/exporters:/mnt/exporters:ro" "${{INDEX_BINDS[@]}}" "$IMAGE" \\
  env {BLOCKSCI_IMAGE_PYTHON_COMMAND} /mnt/exporters/verify_chain.py "/runs/emulation/logs/$RUN_ID/blocksci_data/config.json" \\
  --max-block "$EXPORTED_MAX_BLOCK" --network {shell_value(network)}{checkpoint}{source} \\
  --output "/runs/emulation/logs/$RUN_ID/blocksci_data/chain-verification.json"
VERIFIED_CHAIN="$(python3 -c 'import json, sys; d = json.load(open(sys.argv[1])); print(d["exported_max_block"], d["exported_block_hash"])' "$RUN_WORK/blocksci_data/chain-verification.json")"
read -r EXPORTED_MAX_BLOCK EXPORTED_BLOCK_HASH <<< "$VERIFIED_CHAIN"
MANIFEST_EXTRA="$MANIFEST_EXTRA,
  \\"exported_block_hash\\": \\"$EXPORTED_BLOCK_HASH\\""
"""


def _parse_chain(message: str, command: str, *, stage: str = "blocksci-parse") -> str:
    """Run the parser over ``$BITCOIN_DATADIR`` inside the BlockSci image."""
    return (
        f'echo "[{stage}] {message}"\n'
        "singularity exec \\\n"
        '  --bind "$RUNS_ROOT:/runs/emulation/logs:rw" \\\n'
        '  --bind "$RUN_WORK/.pipeline/exporters:/mnt/exporters:ro" \\\n'
        '  --bind "$BITCOIN_DATADIR:/mnt/data:ro" "${INDEX_BINDS[@]}" \\\n'
        '  --env PBS_RUN_ID="$RUN_ID" --env PBS_EXPORTED_MAX_BLOCK="$EXPORTED_MAX_BLOCK" "$IMAGE" \\\n'
        f"  bash -c 'cd \"/runs/emulation/logs/$PBS_RUN_ID\" && {command}'"
    )


def _manifest_checks(manifest: str, label: str, run_id: str | None = None) -> list[str]:
    """Reject a cache or index manifest of another schema, run or BlockSci image."""
    checks = [
        f'grep -Fq \'"schema_version": "1.1"\' {manifest} || '
        f'{{ echo "{label} manifest has an unsupported schema" >&2; exit 1; }}',
    ]
    if run_id is not None:
        checks.append(
            f'grep -Fq "\\"run_id\\": \\"{run_id}\\"" {manifest} || '
            f'{{ echo "{label} run ID does not match {run_id}" >&2; exit 1; }}'
        )
    checks.append(
        f'grep -Fq "\\"blocksci_image\\": \\"$IMAGE\\"" {manifest} || '
        f'{{ echo "{label} was produced by a different BlockSci image" >&2; exit 1; }}'
    )
    return checks


def _restore_cache(cache_dir: str, cache_uri: str, run_id: str, label: str, checks: str = "") -> str:
    """Verify metadata and stream the archive into the run's working index.

    Only small metadata lands in cache_dir. The incomplete marker stays on any failure;
    no consumer runs until extraction and verification of the entire stream succeed.
    """
    manifest = f'"{cache_dir}/manifest.json"'
    sidecar = f'"{cache_dir}/blocksci_data.tar.zst.sha256"'
    return "\n".join(
        (
            f's5 cp "{cache_uri}/manifest.json" {manifest}',
            f's5 cp "{cache_uri}/blocksci_data.tar.zst.sha256" {sidecar}',
            f'require_file {manifest} "{label} is missing manifest.json"',
            *_manifest_checks(manifest, label, run_id),
            f'grep -Fq \'"archive": "blocksci_data.tar.zst"\' {manifest} || '
            f'{{ echo "{label} has an unsupported archive" >&2; exit 1; }}',
            *([checks] if checks else []),
            f"SIDECAR_SHA=\"$(cut -d ' ' -f 1 {sidecar})\"",
            f'grep -Fq "\\"archive_sha256\\": \\"$SIDECAR_SHA\\"" {manifest} || '
            f'{{ echo "{label} manifest SHA-256 does not match its sidecar" >&2; exit 1; }}',
            'mkdir -p "$RUN_WORK/blocksci_data"',
            'touch "$RUN_WORK/blocksci_data/.writing"',
            f'index_archive_restore "{cache_uri}/blocksci_data.tar.zst" "$RUN_WORK" "$SIDECAR_SHA"',
            f'require_file "$RUN_WORK/blocksci_data/config.json" "{label} did not contain blocksci_data/config.json"',
            f'require_file "$RUN_WORK/blocksci_data/parsed/chain/block.dat" '
            f'"{label} did not contain parsed/chain/block.dat"',
            "canonicalize_index",
            'rm -f "$RUN_WORK/blocksci_data/.writing"',
        )
    )


def _manifest_format(last_field: str) -> str:
    """printf format of a schema-1.1 index manifest; ``$MANIFEST_EXTRA`` follows the height."""
    return (
        r"'{\n"
        r'  "schema_version": "1.1",\n'
        r'  "run_id": "%s",\n'
        r'  "blocksci_image": "%s",\n'
        r'  "source_kind": "%s",\n'
        r'  "network": "%s",\n'
        r'  "exported_max_block": %s%s,\n'
        f"{last_field}\\n"
        r"}\n'"
    )


def _publish_cache(stage: str, source_kind: str, network: str, ncpus: int) -> str:
    """Stream the run's index to S3 and publish its checksum and manifest last.

    zstd gets the requested ncpus because MetaCentrum's process environment may expose
    only one CPU. MANIFEST_EXTRA carries additional source-specific provenance.
    """
    manifest_format = _manifest_format(r'  "archive": "blocksci_data.tar.zst",\n' + r'  "archive_sha256": "%s"')
    return "\n".join(
        (
            f'echo "[{stage}] archiving reusable index"',
            'mkdir -p "$CACHE_DIR"',
            's5 rm "$ARTIFACT_URI/$RUN_ID/blocksci-parse_data/manifest.json" >/dev/null 2>&1 || true',
            f'index_archive_upload "$RUN_WORK" "$ARTIFACT_URI/$RUN_ID/blocksci-parse_data/blocksci_data.tar.zst" '
            f'"{ncpus}" blocksci_data',
            f"printf {manifest_format} \\",
            f'  "$RUN_ID" "$IMAGE" "{source_kind}" "{network}" "$EXPORTED_MAX_BLOCK" "$MANIFEST_EXTRA" '
            '"$ARCHIVE_SHA256" \\',
            '  > "$CACHE_DIR/manifest.json"',
            's5 cp "$CACHE_DIR/manifest.json" "$ARTIFACT_URI/$RUN_ID/blocksci-parse_data/manifest.json"',
        )
    )


INDEX_ROOTS = ("/storage/", "/auto/", *(f"/scratch/multijob-{place}/" for place in PBS_MULTIJOB_LOCATIONS))
INDEX_UNLOCK = "index_unlock 2>/dev/null || :"
# Persistent manifests keep the S3 cache fields an update reads, without the archive.
PERSISTENT_STORAGE_FIELD = r'  "storage": "persistent"'


@dataclass(frozen=True)
class PersistentIndex:
    """A BlockSci index kept in a directory shared by jobs instead of the S3 cache.

    ``index_dir`` is the working copy jobs read and update in place; ``backup_uri`` is the
    S3 prefix of its backup generations, written by the backup job and read to restore an
    incomplete working copy.
    """

    index_dir: str
    backup_uri: str | None = None

    def __post_init__(self) -> None:
        if not self.index_dir.startswith(INDEX_ROOTS) or ".." in Path(self.index_dir).parts:
            raise PBSError(
                f"The index directory must be under /storage/ or /scratch/multijob-<location>/: {self.index_dir}"
            )
        if self.backup_uri is not None:
            validated_s3_uri(self.backup_uri, "index backup URI")


def _index_setup(index: PersistentIndex, walltime: str, *, writer: bool) -> str:
    """Define the index variables and helpers, then take the reader or writer lock."""
    seconds = walltime_to_seconds(walltime)
    return "\n".join(
        (
            "require_zstd",
            f"INDEX_DIR={shell_value(index.index_dir)}",
            f"INDEX_BACKUP={shell_value((index.backup_uri or '').rstrip('/'))}",
            # Past the walltime the job is gone, so its lock entry may be cleared.
            f"INDEX_LOCK_EXPIRES=$(($(date +%s) + {seconds} + 600))",
            f"INDEX_LOCK_WAIT={max(60, seconds // 2)}",
            "INDEX_LOCK_POLL=60",
            template("index_store.sh"),
            'mkdir -p "$INDEX_DIR"',
            "index_lock_exclusive" if writer else "index_lock_shared",
        )
    )


def _require_complete_index(remedy: str) -> str:
    """Fail a reader on an incomplete working copy; readers never restore it themselves."""
    return f'index_complete "$INDEX_DIR" || {{ echo "[index] working copy $INDEX_DIR is incomplete; {remedy}" >&2; exit 1; }}'


def _index_reader(index: PersistentIndex, walltime: str) -> str:
    """Read-only access for an analysis: shared lock, complete copy, run-local config."""
    return "\n".join(
        (
            _index_setup(index, walltime, writer=False),
            _require_complete_index("a persistent update restores it from the S3 backup"),
            *_manifest_checks('"$INDEX_DIR/manifest.json"', "Persistent BlockSci index"),
            "index_link_into_run",
            'cp "$INDEX_DIR/config.json" "$RUN_WORK/blocksci_data/config.json"',
            "canonicalize_index",
            'INDEX_BINDS=(--bind "$INDEX_DIR:$INDEX_DIR:ro")',
            'echo "[index] reading $INDEX_DIR at height $(index_height "$INDEX_DIR")"',
        )
    )


# Under the writer lock: mark the working copy incomplete until index_publish, and link it into the run.
INDEX_BEGIN_WRITE = ('touch "$INDEX_DIR/.writing"', "index_link_into_run")


def _index_publish(stage: str, source_kind: str, network: str, resources: PBSResources, index_dir: str) -> str:
    """Record the parsed index in place (manifest, file list), publish its height, unlock."""
    last_fields = PERSISTENT_STORAGE_FIELD + r',\n  "index_dir": %s,\n  "pbs_resources": %s,\n  "published_at": "%s"'
    resource_record = shell_value(json.dumps({"ncpus": resources.ncpus, "mem": resources.mem}))
    return "\n".join(
        (
            f'echo "[{stage}] recording persistent index at height $EXPORTED_MAX_BLOCK"',
            f"printf {_manifest_format(last_fields)} \\",
            f'  "$RUN_ID" "$IMAGE" "{source_kind}" "{network}" "$EXPORTED_MAX_BLOCK" "$MANIFEST_EXTRA" \\',
            f'  {shell_value(json.dumps(index_dir))} {resource_record} "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \\',
            '  > "$RUN_WORK/index-manifest.json"',
            'index_publish "$RUN_WORK/index-manifest.json"',
            # The lab cannot see the multijob scratch; it reads the index height from here.
            's5 cp "$INDEX_DIR/manifest.json" "$ARTIFACT_URI/$RUN_ID/blocksci-index-manifest.json"',
            "index_unlock",
        )
    )


def render_blocksci_index_backup_s3_pbs(
    target: S3Target,
    *,
    persistent: PersistentIndex,
    multijob: str | None = None,
    resources: PBSResources = BLOCKSCI,
) -> str:
    """Render a job that publishes a new immutable backup generation of the working index.

    It only reads the index, so it holds the shared lock: analyses go on and only an update
    waits. The previous generation stays the restore source until the new archive's S3 size
    matches the uploaded stream and ``latest.json`` is switched to it.
    """
    if persistent.backup_uri is None:
        raise PBSError("An index backup needs --blocksci-index-backup-uri")
    return _render_stage(
        "s3_blocksci_backup.sh",
        "blocksci-backup",
        target,
        resources,
        multijob=multijob,
        ncpus=resources.ncpus,
        index_reader="\n".join(
            (_index_setup(persistent, resources.walltime, writer=False), _require_complete_index("nothing to back up"))
        ),
    )


def render_coinjoin_analysis_s3_pbs(
    target: S3Target, image: str, command: str, resources: PBSResources = DEFAULT_RESOURCES["analysis"]
) -> str:
    return _render_stage(
        "s3_coinjoin_analysis.sh", "coinjoin-analysis", target, resources, image=image, command=command
    )


def render_coinjoin_tools_s3_pbs(
    target: S3Target,
    image: str,
    options: CoinjoinAnalysisConfiguration,
    resources: PBSResources = DEFAULT_RESOURCES["analysis"],
) -> str:
    """Run a shipped analyzer CLI on a copy; publish all files apart from the baseline."""
    if options.input_uri:
        source = validated_s3_uri(options.input_uri, "analysis input URI").rstrip("/")
        download = f's5 sync {shell_value(source + "/*")} "$OUTPUT/input/"'
    elif not uses_run_input(options):
        download = ":"
    else:
        download = "\n".join(
            (
                'EXPERIMENT="$OUTPUT/input/$RUN_ID"',
                'mkdir -p "$EXPERIMENT"',
                'download_optional coinjoin-analysis_data "$EXPERIMENT"',
                'download_optional coinjoin_emulator_data/data "$EXPERIMENT/data"',
                'test -n "$(find "$EXPERIMENT" -type f -print -quit)" || {',
                '  echo "The run has no baseline or emulator data; provide analysis_input_uri" >&2; exit 1;',
                "}",
            )
        )
    adapter = ""
    if options.program in RESEARCH_PROGRAMS:
        adapter = (
            "cat > \"$OUTPUT/coinjoin_research.py\" <<'COINJOIN_RESEARCH_ADAPTER'\n"
            + (EXPORTERS_ROOT / "coinjoin_research.py").read_text(encoding="utf-8")
            + "\nCOINJOIN_RESEARCH_ADAPTER\n"
        )
        if options.program == "update_txids_with_wabisator":
            adapter += (
                "cat > \"$OUTPUT/wabisator_source.py\" <<'COINJOIN_WABISATOR_SOURCE'\n"
                + template("wabisator_source.txt")
                + "\nCOINJOIN_WABISATOR_SOURCE\n"
            )
    return _render_stage(
        "s3_coinjoin_tools.sh",
        "coinjoin-analysis",
        target,
        resources,
        image=image,
        program=options.program,
        output_directory=COINJOIN_ANALYSIS_TOOLS_DIR,
        adapter=adapter,
        download=download,
        command=shell_value("cd /work && " + coinjoin_tools_command(options, target.run_id)),
        invocation=json.dumps(
            {"argv": command_argv(options, target.run_id), "image": image, "input_uri": options.input_uri}, indent=2
        ),
    )


# coinjoin-analysis names per protocol: --cjtype, the Scanner/ output folder, data/<name>/false_cjtxs.json.
DUMPLINGS_PROTOCOLS = {
    "wasabi2": ("ww2", "wasabi2", "wasabi2"),
    "joinmarket": ("jm", "joinmarket_all", "joinmarket"),
}


def render_coinjoin_analysis_dumplings_s3_pbs(
    target: S3Target,
    image: str,
    *,
    dumplings_uri: str,
    coinjoin_type: str,
    resources: PBSResources = DEFAULT_RESOURCES["analysis"],
) -> str:
    """Render the mainnet baseline: coinjoin-analysis over the latest published Dumplings snapshot.

    The snapshot is found through ``<dumplings_uri>/latest.json`` (written by the Dumplings
    service next to the archive node). The run gets ``coinjoin_tx_info.json``, the image's
    curated ``false_cjtxs.json`` and ``dumplings-snapshot.json`` for the external comparison.
    """
    uri = validated_s3_uri(dumplings_uri, "Dumplings snapshot URI")
    if coinjoin_type not in DUMPLINGS_PROTOCOLS:
        raise PBSError(f"Dumplings baselines cover wasabi2 and joinmarket, not {coinjoin_type}")
    cjtype, mix_id, false_cjtxs = DUMPLINGS_PROTOCOLS[coinjoin_type]
    return _render_stage(
        "s3_coinjoin_analysis_dumplings.sh",
        "coinjoin-analysis",
        target,
        resources,
        image=image,
        dumplings_uri=shell_value(uri),
        cjtype=cjtype,
        mix_id=mix_id,
        false_cjtxs=false_cjtxs,
        command=f"python -m cj_process.parse_dumplings --cjtype {cjtype} --action process_dumplings "
        "--target-path /work/dumplings",
    )


def render_mappings_s3_pbs(
    target: S3Target,
    enumerator_image: str,
    sake_image: str,
    settings: MappingsConfiguration = MappingsConfiguration(),
    resources: PBSResources = DEFAULT_RESOURCES["mappings"],
) -> str:
    """Render the Wasabi mappings/Sake stage over S3-backed inputs."""
    require_safe_image(enumerator_image, "enumerator image")
    require_safe_image(sake_image, "Sake image")
    return _render_stage(
        "s3_mappings.sh",
        "coinjoin-mappings",
        target,
        resources,
        enumerator_image=shell_value(enumerator_image),
        sake_image=shell_value(sake_image),
        mappings_run=mappings_run(settings),
    )


def render_blocksci_s3_pbs(
    target: S3Target,
    image: str,
    command: str,
    resources: PBSResources = BLOCKSCI,
    *,
    include_report: bool = True,
    export_analysis: bool = False,
) -> str:
    """Parse and analyze in one job, publishing the same index archive as the split workflow."""
    report = "coinjoinPipeline_data/unified_report.json"
    analysis = "blocksci-analysis_data/blocksci_analysis.json"
    baseline = "coinjoin-analysis_data/coinjoin_tx_info.json"
    after = []
    if include_report:
        after.append('REPORT_DIR="$RUN_WORK/coinjoinPipeline_data"')
        after.append(f'require_file "$RUN_WORK/{report}" "BlockSci S3-compatible reporting did not produce {report}"')
    if export_analysis:
        after.append(f'require_file "$RUN_WORK/{analysis}" "BlockSci analysis did not produce {analysis}"')
    after.extend(
        (
            'CACHE_DIR="$RUN_WORK/blocksci-parse_data"',
            'MANIFEST_EXTRA=""',
            _publish_cache("blocksci", "emulator", "bitcoin_regtest", resources.ncpus),
        )
    )
    if export_analysis:
        after.append(_upload('"$RUN_WORK/blocksci-analysis_data/"', "blocksci-analysis_data"))
    if include_report:
        after.append(_upload('"$REPORT_DIR/"', "coinjoinPipeline_data"))
    before: list[str] = []
    if include_report:
        before.extend(
            (
                download_run_directory("coinjoin-analysis_data"),
                # Retain optional mappings and prior-report provenance without fetching an index.
                'download_optional coinjoin-mappings_data "$RUN_WORK/coinjoin-mappings_data"',
                'download_optional coinjoinPipeline_data "$RUN_WORK/coinjoinPipeline_data"',
                f'require_file "$RUN_WORK/{baseline}" "BlockSci S3-compatible reporting requires {baseline}"',
            )
        )
    return _render_stage(
        "s3_blocksci.sh",
        "blocksci",
        target,
        resources,
        image=image,
        command=command,
        before="\n".join(before),
        after="\n".join(after),
    )


class _ParseSource(NamedTuple):
    """Where a parse job's index comes from and the script fragments that produce it."""

    kind: str  # the cache manifest's source_kind
    network: str
    description: str
    prepare: str
    produce: str
    verify: str  # the chain check before publication; empty when nothing is checked


def _parse_source(
    command: str,
    *,
    external_bitcoin_datadir: Path | None,
    bitcoin_blocks_uri: str | None,
    expected_block_hash: str | None,
    external_blocksci_dir: Path | None,
    external_network: str | None,
    external_max_block: int | None,
) -> _ParseSource:
    """Validate the parse's single source and render how its index is produced."""
    if sum(value is not None for value in (external_bitcoin_datadir, bitcoin_blocks_uri, external_blocksci_dir)) > 1:
        raise PBSError("Choose only one external Bitcoin or BlockSci source")
    if external_bitcoin_datadir is not None:
        network = _require_external_chain(external_network, external_max_block)
        return _ParseSource(
            "external-bitcoin",
            network,
            "external Bitcoin Core block directory",
            f"{_bitcoin_datadir_source(external_bitcoin_datadir)}\nEXPORTED_MAX_BLOCK={external_max_block}",
            _parse_chain("parsing external chain through block $EXPORTED_MAX_BLOCK", command),
            "",
        )
    if bitcoin_blocks_uri is not None:
        network = _require_external_chain(external_network, external_max_block)
        return _ParseSource(
            "bitcoin-blocks-s3",
            network,
            "verified Bitcoin block archive from S3",
            f"{_block_archive_source(bitcoin_blocks_uri)}\nEXPORTED_MAX_BLOCK={external_max_block}\n"
            f"{_download_block_archive()}",
            _parse_chain("parsing verified S3 block archive through block $EXPORTED_MAX_BLOCK", command),
            _verify_block_archive_chain(network, expected_block_hash),
        )
    if external_blocksci_dir is not None:
        blocksci_path = external_blocksci_dir.expanduser().resolve()
        require_storage_path(blocksci_path)
        require_existing_path(blocksci_path, "external BlockSci directory")
        for relative in ("config.json", "parsed/chain/block.dat"):
            if not (blocksci_path / relative).is_file():
                raise PBSError(f"External BlockSci directory must contain {relative}: {blocksci_path}")
        return _ParseSource(
            "external-blocksci",
            "from-config",
            "existing external BlockSci index",
            f"EXTERNAL_BLOCKSCI_DIR={shell_value(str(blocksci_path))}\n"
            'test -f "$EXTERNAL_BLOCKSCI_DIR/config.json"\n'
            'test -f "$EXTERNAL_BLOCKSCI_DIR/parsed/chain/block.dat"',
            'cp -a "$EXTERNAL_BLOCKSCI_DIR" "$RUN_WORK/blocksci_data"\n'
            "canonicalize_index\n"
            'MAX_BLOCK_NUM="$(sed -nE \'s/.*"maxBlockNum"[[:space:]]*:[[:space:]]*([0-9]+).*/\\1/p\' '
            '"$RUN_WORK/blocksci_data/config.json" | head -n 1)"\n'
            'test -n "$MAX_BLOCK_NUM" && [ "$MAX_BLOCK_NUM" -gt 0 ] || { '
            'echo "External BlockSci config has no positive parser.maxBlockNum" >&2; exit 1; }\n'
            'EXPORTED_MAX_BLOCK="$((MAX_BLOCK_NUM - 1))"\n'
            'echo "[blocksci-parse] imported external index through block $EXPORTED_MAX_BLOCK"',
            "",
        )
    return _ParseSource(
        "emulator",
        "bitcoin_regtest",
        "emulator Bitcoin and exported-block inputs",
        "\n".join(
            (
                download_run_directory("bitcoin_data"),
                download_run_directory("coinjoin_emulator_data/data/btc-node"),
                "locate_emulator_bitcoin_datadir",
                'EXPORTED_MAX_BLOCK="$(emulator_max_block)"',
                'test -n "$EXPORTED_MAX_BLOCK" || { '
                'echo "BlockSci parsing could not determine the exported maximum block" >&2; exit 1; }',
            )
        ),
        _parse_chain("parsing through exported block $EXPORTED_MAX_BLOCK", command),
        "",
    )


def render_blocksci_parse_s3_pbs(
    target: S3Target,
    image: str,
    command: str,
    resources: PBSResources = BLOCKSCI,
    *,
    external_bitcoin_datadir: Path | None = None,
    bitcoin_blocks_uri: str | None = None,
    expected_block_hash: str | None = None,
    external_blocksci_dir: Path | None = None,
    external_network: str | None = None,
    external_max_block: int | None = None,
    persistent: PersistentIndex | None = None,
    multijob: str | None = None,
) -> str:
    """Render a parser-only job that publishes a checksummed reusable index.

    With ``persistent`` the index is parsed straight into that directory and recorded
    there instead of being archived to S3.
    """
    source = _parse_source(
        command,
        external_bitcoin_datadir=external_bitcoin_datadir,
        bitcoin_blocks_uri=bitcoin_blocks_uri,
        expected_block_hash=expected_block_hash,
        external_blocksci_dir=external_blocksci_dir,
        external_network=external_network,
        external_max_block=external_max_block,
    )
    produce_index = source.produce
    publish = _publish_cache("blocksci-parse", source.kind, source.network, resources.ncpus)
    finalize = ":"
    if persistent is not None:
        if source.kind not in {"external-bitcoin", "bitcoin-blocks-s3"}:
            raise PBSError("A persistent index is parsed from an external Bitcoin datadir or block archive")
        # The lock is taken after the block download, which has no index to protect yet.
        produce_index = "\n".join(
            (
                _index_setup(persistent, resources.walltime, writer=True),
                'if [ -f "$INDEX_DIR/manifest.json" ] || index_backup_available; then',
                '  echo "[index] an index already exists for $INDEX_DIR; use --blocksci-task update" >&2',
                "  exit 1",
                "fi",
                'rm -rf "$INDEX_DIR/parsed" "$INDEX_DIR/config.json" "$INDEX_DIR/files.txt"',
                *INDEX_BEGIN_WRITE,
                'INDEX_BINDS=(--bind "$INDEX_DIR:$INDEX_DIR:rw")',
                produce_index,
            )
        )
        publish = _index_publish("blocksci-parse", source.kind, source.network, resources, persistent.index_dir)
        finalize = INDEX_UNLOCK
    return _render_stage(
        "s3_blocksci_parse.sh",
        "blocksci-parse",
        target,
        resources,
        image=image,
        multijob=multijob,
        source_description=source.description,
        prepare_source=source.prepare,
        produce_index=produce_index,
        verify_index=source.verify,
        publish_cache=publish,
        finalize_index=finalize,
    )


def render_blocksci_update_s3_pbs(
    target: S3Target,
    source_run_id: str | None,
    image: str,
    command: str,
    resources: PBSResources = BLOCKSCI,
    *,
    external_network: str,
    external_max_block: int,
    external_bitcoin_datadir: Path | None = None,
    bitcoin_blocks_uri: str | None = None,
    expected_block_hash: str | None = None,
    persistent: PersistentIndex | None = None,
    multijob: str | None = None,
) -> str:
    """Render a job that incrementally updates one S3 cache into a fresh run.

    The cache must come from the same source: BlockSci remembers each block by
    its file number and offset, which only that Bitcoin datadir or block
    archive can resolve. With ``persistent`` the job instead updates that index
    in place under the writer lock, first restoring it from the S3 backup
    when the working copy is incomplete.
    """
    if persistent is None:
        source_run_id = _run_id(source_run_id or "")
        if source_run_id == target.run_id:
            raise PBSError("Incremental BlockSci update requires different source and target run IDs")
    elif source_run_id is not None:
        raise PBSError("A persistent index update reads its own index, not a source run")
    _require_external_chain(external_network, external_max_block)
    verify_index = ""
    prepare_blocks = ""
    if external_bitcoin_datadir is not None and bitcoin_blocks_uri is None:
        source_kind = "external-bitcoin"
        prepare_source = _bitcoin_datadir_source(external_bitcoin_datadir)
    elif bitcoin_blocks_uri is not None and external_bitcoin_datadir is None:
        source_kind = "bitcoin-blocks-s3"
        verify_index = _verify_block_archive_chain(external_network, expected_block_hash, update=True)
        prepare_source = _block_archive_source(bitcoin_blocks_uri)
        prepare_blocks = (
            'echo "[blocksci-update] downloading block archive files after block $SOURCE_MAX_BLOCK"\n'
            + _download_block_archive("$SOURCE_CACHE_DIR/manifest.json")
        )
    else:
        raise PBSError("Incremental BlockSci update requires exactly one external Bitcoin datadir or block archive")
    source_checks = (
        f'grep -Fq \'"source_kind": "{source_kind}"\' "$SOURCE_CACHE_DIR/manifest.json" || '
        f'{{ echo "Incremental update requires a {source_kind} cache" >&2; exit 1; }}\n'
        'grep -Fq "\\"network\\": \\"$NETWORK\\"" "$SOURCE_CACHE_DIR/manifest.json" || '
        '{ echo "Source cache network does not match $NETWORK" >&2; exit 1; }'
    )
    if persistent is None:
        restore_source = _restore_cache(
            "$SOURCE_CACHE_DIR",
            "$ARTIFACT_URI/$SOURCE_RUN_ID/blocksci-parse_data",
            "$SOURCE_RUN_ID",
            "Source cache",
            source_checks,
        )
        publish = _publish_cache("blocksci-update", source_kind, "$NETWORK", resources.ncpus)
        finalize = ":"
    else:
        # The template reads the source manifest from $SOURCE_CACHE_DIR, as for an S3 cache.
        restore_source = "\n".join(
            (
                _index_setup(persistent, resources.walltime, writer=True),
                "index_reconcile",
                *_manifest_checks('"$INDEX_DIR/manifest.json"', "Persistent BlockSci index"),
                source_checks.replace("$SOURCE_CACHE_DIR", "$INDEX_DIR"),
                'cp "$INDEX_DIR/manifest.json" "$SOURCE_CACHE_DIR/manifest.json"',
                'SOURCE_RUN_ID="$(sed -nE \'s/.*"run_id"[[:space:]]*:[[:space:]]*"([^"]+)".*/\\1/p\' '
                '"$INDEX_DIR/manifest.json" | head -n 1)"',
                *INDEX_BEGIN_WRITE,
                'cp "$INDEX_DIR/config.json" "$RUN_WORK/blocksci_data/config.json"',
                'INDEX_BINDS=(--bind "$INDEX_DIR:$INDEX_DIR:rw")',
                'echo "[index] updating $INDEX_DIR from height $(index_height "$INDEX_DIR")"',
            )
        )
        publish = _index_publish("blocksci-update", source_kind, "$NETWORK", resources, persistent.index_dir)
        finalize = INDEX_UNLOCK
    return _render_stage(
        "s3_blocksci_update.sh",
        "blocksci-update",
        target,
        resources,
        image=image,
        multijob=multijob,
        source_run_id=shell_value(source_run_id or "persistent-index"),
        network=shell_value(external_network),
        exported_max_block=external_max_block,
        prepare_source=prepare_source,
        prepare_blocks=prepare_blocks,
        verify_index=verify_index,
        restore_source=restore_source,
        parse_chain=_parse_chain(
            "parsing blocks $((SOURCE_MAX_BLOCK + 1)) through $EXPORTED_MAX_BLOCK", command, stage="blocksci-update"
        ),
        publish_cache=publish,
        finalize_index=finalize,
    )


def render_blocksci_analyze_s3_pbs(
    target: S3Target,
    image: str,
    command: str,
    resources: PBSResources = BLOCKSCI,
    *,
    mode: str = "blocksci-analyze",
    source_run_id: str | None = None,
    persistent: PersistentIndex | None = None,
    multijob: str | None = None,
    user_script: Path | None = None,
    external_baseline_uri: str | None = None,
    external_false_cjtxs_uri: str | None = None,
    notebooks_dir: Path | None = None,
    notebook_port: int = 8888,
) -> str:
    """Render analysis, custom-script, or notebook work over a cached index.

    With ``source_run_id`` the index comes read-only from that run's cache while
    outputs, markers, and logs stay under the target run, so several analyses
    can share one published index without touching it. With ``persistent`` the
    index is read in place under the shared lock instead of being downloaded.
    """
    if mode not in MODE_OUTPUTS:
        raise PBSError(f"Unsupported reusable BlockSci mode: {mode}")
    require_notebook_port(notebook_port)
    if source_run_id is not None and persistent is not None:
        raise PBSError("Choose one index source: a source run's cache or a persistent index")
    if mode == "blocksci-analyze" and persistent is not None:
        raise PBSError("BlockSci detect reads emulator data of its own run and cannot use a persistent index")
    if mode == "blocksci-analyze" and source_run_id is not None:
        raise PBSError("BlockSci detect reads emulator data of its own run and cannot use a source cache")
    if external_false_cjtxs_uri is not None and mode != "blocksci-external":
        raise PBSError("A false-positive list belongs to the external BlockSci report")

    setup = prepare_analysis_mode(
        mode,
        user_script=user_script,
        external_baseline_uri=external_baseline_uri,
        external_false_cjtxs_uri=external_false_cjtxs_uri,
        notebooks_dir=notebooks_dir,
        notebook_port=notebook_port,
    )

    if persistent is not None:
        restore = _index_reader(persistent, resources.walltime)
        setup.prepare.append('cp "$INDEX_DIR/manifest.json" "$OUTPUT_DIR/blocksci-index-manifest.json"')
    else:
        cache_run = "$RUN_ID"
        cache = []
        if source_run_id is not None:
            source_run_id = _run_id(source_run_id)
            if source_run_id == target.run_id:
                raise PBSError("A read-only source cache must come from a different run")
            cache_run = "$CACHE_RUN_ID"
            cache.append(f"CACHE_RUN_ID={shell_value(source_run_id)}")
            # Record which published index the outputs were computed from.
            setup.prepare.append('cp "$CACHE_DIR/manifest.json" "$OUTPUT_DIR/blocksci-cache-manifest.json"')
        setup.downloads = cache + setup.downloads
        restore = _restore_cache(
            "$CACHE_DIR", f"$ARTIFACT_URI/{cache_run}/blocksci-parse_data", cache_run, "BlockSci index"
        )
        if source_run_id is not None:
            setup.prepare.append(
                'printf \'{"source_run_id":"%s"}\\n\' "$CACHE_RUN_ID" > "$OUTPUT_DIR/blocksci-source.json"'
            )
            setup.extra_binds += (
                '\nINDEX_BINDS+=(--bind "$RUN_WORK/blocksci_data/parsed:'
                '/runs/emulation/logs/$RUN_ID/blocksci_data/parsed:ro")'
            )
    return _render_stage(
        "s3_blocksci_analyze.sh",
        mode,
        target,
        resources,
        image=image,
        multijob=multijob,
        command=command,
        output_dir=setup.output,
        download_inputs="\n".join(setup.downloads),
        restore_cache=restore,
        finalize_index=":" if persistent is None else INDEX_UNLOCK,
        prepare_mode="\n".join(setup.prepare),
        extra_binds=setup.extra_binds,
        connection_help=setup.connection_help,
        output_check=setup.output_check,
        upload_outputs=f's5 sync "$OUTPUT_DIR/" "$ARTIFACT_URI/$RUN_ID/{MODE_OUTPUTS[mode]}/" || upload_status=$?',
    )


def render_unified_report_s3_pbs(
    target: S3Target,
    image: str,
    command: str,
    resources: PBSResources = DEFAULT_RESOURCES["unified_report"],
    *,
    include_mappings: bool = False,
) -> str:
    """Render the S3 report-only job that joins both analyzer outputs."""
    return _render_stage(
        "s3_unified_report.sh",
        "unified-report",
        target,
        resources,
        image=image,
        command=command,
        download_inputs="\n".join(
            download_run_directory(name)
            for name in (
                ".pipeline/exporters",
                "coinjoin_emulator_data",
                "coinjoin-analysis_data",
                "blocksci-analysis_data",
                *(("coinjoin-mappings_data",) if include_mappings else ()),
            )
        ),
    )
