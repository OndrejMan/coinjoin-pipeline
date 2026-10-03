"""S3-backed PBS script renderers."""

from __future__ import annotations

from pathlib import Path

from exporters.verify_chain import is_block_hash

from coinjoin_pipeline.storage.s3 import (
    S3Target,
    render_s5cmd_check,
    render_s5cmd_cp,
    render_s5cmd_run,
    render_s5cmd_sync,
    shell_value,
    validate_artifact_uri,
    validate_credentials_file,
    validate_run_id,
    validate_s3_endpoint_url,
    validate_s3_profile,
)

from .defaults import (
    BLOCKSCI_IMAGE_PYTHON_COMMAND,
    DEFAULT_BLOCKSCI_MEM,
    DEFAULT_BLOCKSCI_NCPUS,
    DEFAULT_BLOCKSCI_SCRATCH,
    DEFAULT_BLOCKSCI_WALLTIME,
    DEFAULT_COINJOIN_ANALYSIS_MEM,
    DEFAULT_COINJOIN_ANALYSIS_NCPUS,
    DEFAULT_COINJOIN_ANALYSIS_SCRATCH,
    DEFAULT_COINJOIN_ANALYSIS_WALLTIME,
    DEFAULT_UNIFIED_REPORT_MEM,
    DEFAULT_UNIFIED_REPORT_NCPUS,
    DEFAULT_UNIFIED_REPORT_SCRATCH,
    DEFAULT_UNIFIED_REPORT_WALLTIME,
)
from .validation import (
    PBSError,
    require_existing_path,
    require_safe_image,
    require_safe_pbs_resources,
    require_safe_pbs_token,
    require_storage_path,
)


def _s3_values(
    target: S3Target,
) -> dict[str, str]:
    return {
        "artifact_uri": shell_value(validate_artifact_uri(target.artifact_uri)),
        "run_id": shell_value(validate_run_id(target.run_id)),
        "endpoint_url": shell_value(validate_s3_endpoint_url(target.endpoint_url)),
        "credentials_file": shell_value(validate_credentials_file(target.credentials_file)),
        "profile": shell_value(validate_s3_profile(target.profile)),
    }


EXECUTION_DIR = Path(__file__).parent.parent
EXTERNAL_NETWORKS = {"bitcoin", "bitcoin_testnet", "bitcoin_regtest"}


def _render_stage(
    template_name: str,
    stage_name: str,
    target: S3Target,
    /,
    *,
    ncpus: int,
    mem: str,
    scratch: str,
    walltime: str,
    image: str | None = None,
    **values,
) -> str:
    """Validate the shared job inputs and render one stage's script template."""
    require_safe_pbs_resources(ncpus, mem, scratch, walltime)
    if image is not None:
        require_safe_image(image)
        values["image"] = shell_value(image)
    template = (EXECUTION_DIR / template_name).read_text(encoding="utf-8")
    return render_template(
        template,
        **_s3_values(target),
        ncpus=ncpus,
        mem=mem,
        scratch=scratch,
        walltime=walltime,
        s5cmd_check=render_s5cmd_check(),
        upload_failed=render_s5cmd_cp('"$FAILED_MARKER"', f'"$ARTIFACT_URI/$RUN_ID/.pbs/{stage_name}.failed"'),
        upload_done=render_s5cmd_cp('"$DONE_MARKER"', f'"$ARTIFACT_URI/$RUN_ID/.pbs/{stage_name}.done"'),
        **values,
    )


def _require_external_chain(network: str | None, max_block: int | None) -> str:
    if network is None or network not in EXTERNAL_NETWORKS:
        raise PBSError("External BlockSci network must be bitcoin, bitcoin_testnet, or bitcoin_regtest")
    if isinstance(max_block, bool) or not isinstance(max_block, int) or max_block < 0:
        raise PBSError("External Bitcoin parsing requires a non-negative --blocksci-max-block")
    return network


def _require_bitcoin_datadir(path: Path) -> Path:
    bitcoin_path = path.expanduser().resolve()
    require_storage_path(bitcoin_path)
    require_existing_path(bitcoin_path, "external Bitcoin coin directory")
    if not (bitcoin_path / "blocks").is_dir():
        raise PBSError(f"External Bitcoin coin directory must contain blocks/: {bitcoin_path}")
    return bitcoin_path


def _block_archive_uri(bitcoin_blocks_uri: str) -> str:
    try:
        return validate_artifact_uri(bitcoin_blocks_uri)
    except ValueError as error:
        raise PBSError(f"Invalid Bitcoin block archive URI: {error}") from error


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
{render_s5cmd_cp('"$BITCOIN_BLOCKS_URI/blk*.dat.json"', '"$ARCHIVE_WORK/sidecars/"')}
python3 "$ARCHIVE_WORK/block_archive.py" select "$ARCHIVE_WORK/sidecars" "$BITCOIN_DATADIR/blocks" \\
  --uri "$BITCOIN_BLOCKS_URI" --max-block "$EXPORTED_MAX_BLOCK"{update} \\
  --commands "$ARCHIVE_WORK/download.s5cmd" --manifest-fields "$ARCHIVE_WORK/manifest-fields.json"
{render_s5cmd_run('"$ARCHIVE_WORK/download.s5cmd"')}
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
  --bind "$RUN_WORK/.pipeline/exporters:/mnt/exporters:ro" "$IMAGE" \\
  env {BLOCKSCI_IMAGE_PYTHON_COMMAND} /mnt/exporters/verify_chain.py "/runs/emulation/logs/$RUN_ID/blocksci_data/config.json" \\
  --max-block "$EXPORTED_MAX_BLOCK" --network {shell_value(network)}{checkpoint}{source} \\
  --output "/runs/emulation/logs/$RUN_ID/blocksci_data/chain-verification.json"
VERIFIED_CHAIN="$(python3 -c 'import json, sys; d = json.load(open(sys.argv[1])); print(d["exported_max_block"], d["exported_block_hash"])' "$RUN_WORK/blocksci_data/chain-verification.json")"
read -r EXPORTED_MAX_BLOCK EXPORTED_BLOCK_HASH <<< "$VERIFIED_CHAIN"
MANIFEST_EXTRA="$MANIFEST_EXTRA,
  \\"exported_block_hash\\": \\"$EXPORTED_BLOCK_HASH\\""
"""


def _parse_chain(message: str, command: str) -> str:
    """Run the parser over ``$BITCOIN_DATADIR`` inside the BlockSci image."""
    return (
        f'echo "[blocksci-parse] {message}"\n'
        "singularity exec \\\n"
        '  --bind "$RUNS_ROOT:/runs/emulation/logs:rw" \\\n'
        '  --bind "$RUN_WORK/.pipeline/exporters:/mnt/exporters:ro" \\\n'
        '  --bind "$BITCOIN_DATADIR:/mnt/data:ro" \\\n'
        '  --env PBS_RUN_ID="$RUN_ID" --env PBS_EXPORTED_MAX_BLOCK="$EXPORTED_MAX_BLOCK" "$IMAGE" \\\n'
        f"  bash -c 'cd \"/runs/emulation/logs/$PBS_RUN_ID\" && {command}'"
    )


def render_coinjoin_analysis_s3_pbs(
    target: S3Target,
    image: str,
    command: str,
    *,
    ncpus: int = DEFAULT_COINJOIN_ANALYSIS_NCPUS,
    mem: str = DEFAULT_COINJOIN_ANALYSIS_MEM,
    scratch: str = DEFAULT_COINJOIN_ANALYSIS_SCRATCH,
    walltime: str = DEFAULT_COINJOIN_ANALYSIS_WALLTIME,
) -> str:
    return _render_stage(
        "coinjoin_analysis_s3_template.sh",
        "coinjoin-analysis",
        target,
        image=image,
        command=command,
        ncpus=ncpus,
        mem=mem,
        scratch=scratch,
        walltime=walltime,
        download_run=render_s5cmd_sync('"$ARTIFACT_URI/$RUN_ID/*"', '"$RUN_WORK/"'),
        upload_results=render_s5cmd_sync(
            '"$RUN_WORK/coinjoin-analysis_data/"',
            '"$ARTIFACT_URI/$RUN_ID/coinjoin-analysis_data/"',
        ),
    )


def render_mappings_s3_pbs(
    target: S3Target,
    enumerator_image: str,
    sake_image: str,
    *,
    mining_fee_rate: int = 1,
    coordination_fee_rate: float = 0.003,
    max_decomposition_fee: int = 6000,
    mode: str = "numeric",
    timeout: int = 60,
    retry_timeout: int = 600,
    sake_seed: int = 20260704,
    ncpus: int = DEFAULT_COINJOIN_ANALYSIS_NCPUS,
    mem: str = DEFAULT_COINJOIN_ANALYSIS_MEM,
    scratch: str = DEFAULT_COINJOIN_ANALYSIS_SCRATCH,
    walltime: str = DEFAULT_COINJOIN_ANALYSIS_WALLTIME,
) -> str:
    """Render the Wasabi mappings/Sake stage over S3-backed inputs."""
    require_safe_image(enumerator_image, "enumerator image")
    require_safe_image(sake_image, "Sake image")
    if mode not in {"numeric", "all"}:
        raise PBSError("CoinJoin mappings mode must be numeric or all")
    for value, description in (
        (mining_fee_rate, "mapping mining fee rate"),
        (max_decomposition_fee, "mapping maximum decomposition fee"),
        (sake_seed, "Sake seed"),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise PBSError(f"{description} must be a non-negative integer")
    for value, description in (
        (timeout, "mapping timeout"),
        (retry_timeout, "mapping retry timeout"),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise PBSError(f"{description} must be a positive integer")
    if (
        isinstance(coordination_fee_rate, bool)
        or not isinstance(coordination_fee_rate, (int, float))
        or coordination_fee_rate < 0
    ):
        raise PBSError("mapping coordination fee rate must be non-negative")

    return _render_stage(
        "mappings_s3_template.sh",
        "coinjoin-mappings",
        target,
        enumerator_image=shell_value(enumerator_image),
        sake_image=shell_value(sake_image),
        enumerator_image_value=enumerator_image,
        sake_image_value=sake_image,
        mining_fee_rate=mining_fee_rate,
        coordination_fee_rate=coordination_fee_rate,
        max_decomposition_fee=max_decomposition_fee,
        mode=mode,
        timeout=timeout,
        retry_timeout=retry_timeout,
        sake_seed=sake_seed,
        ncpus=ncpus,
        mem=mem,
        scratch=scratch,
        walltime=walltime,
        download_input=render_s5cmd_sync(
            '"$ARTIFACT_URI/$RUN_ID/coinjoin-analysis_data/*"',
            '"$RUN_WORK/coinjoin-analysis_data/"',
        ),
        upload_outputs=render_s5cmd_sync('"$OUT/"', '"$ARTIFACT_URI/$RUN_ID/coinjoin-mappings_data/"'),
    )


def render_blocksci_s3_pbs(
    target: S3Target,
    image: str,
    command: str,
    *,
    ncpus: int = DEFAULT_BLOCKSCI_NCPUS,
    mem: str = DEFAULT_BLOCKSCI_MEM,
    scratch: str = DEFAULT_BLOCKSCI_SCRATCH,
    walltime: str = DEFAULT_BLOCKSCI_WALLTIME,
    include_report: bool = True,
    export_analysis: bool = False,
) -> str:
    return _render_stage(
        "blocksci_s3_template.sh",
        "blocksci",
        target,
        image=image,
        command=command,
        ncpus=ncpus,
        mem=mem,
        scratch=scratch,
        walltime=walltime,
        download_run=render_s5cmd_sync('"$ARTIFACT_URI/$RUN_ID/*"', '"$RUN_WORK/"'),
        coinjoin_analysis_check=(
            'test -f "$RUN_WORK/coinjoin-analysis_data/coinjoin_tx_info.json" || {\n'
            '  echo "BlockSci S3-compatible reporting requires '
            'coinjoin-analysis_data/coinjoin_tx_info.json" >&2\n'
            "  exit 1\n"
            "}"
            if include_report
            else ""
        ),
        report_output_check=(
            'REPORT_DIR="$RUN_WORK/coinjoinPipeline_data"\n'
            'test -f "$REPORT_DIR/unified_report.json" || {\n'
            '  echo "BlockSci S3-compatible reporting did not produce '
            'coinjoinPipeline_data/unified_report.json" >&2\n'
            "  exit 1\n"
            "}"
            if include_report
            else ""
        ),
        analysis_output_check=(
            'test -f "$RUN_WORK/blocksci-analysis_data/blocksci_analysis.json" || {\n'
            '  echo "BlockSci analysis did not produce '
            'blocksci-analysis_data/blocksci_analysis.json" >&2\n'
            "  exit 1\n"
            "}"
            if export_analysis
            else ""
        ),
        upload_blocksci=render_s5cmd_sync('"$RUN_WORK/blocksci_data/"', '"$ARTIFACT_URI/$RUN_ID/blocksci_data/"'),
        upload_analysis=(
            render_s5cmd_sync(
                '"$RUN_WORK/blocksci-analysis_data/"',
                '"$ARTIFACT_URI/$RUN_ID/blocksci-analysis_data/"',
            )
            if export_analysis
            else ""
        ),
        upload_report=(
            render_s5cmd_sync(
                '"$REPORT_DIR/"',
                '"$ARTIFACT_URI/$RUN_ID/coinjoinPipeline_data/"',
            )
            if include_report
            else ""
        ),
        upload_logs=render_s5cmd_sync('"$RUN_WORK/logs/"', '"$ARTIFACT_URI/$RUN_ID/logs/"'),
    )


def render_blocksci_parse_s3_pbs(
    target: S3Target,
    image: str,
    command: str,
    *,
    ncpus: int = DEFAULT_BLOCKSCI_NCPUS,
    mem: str = DEFAULT_BLOCKSCI_MEM,
    scratch: str = DEFAULT_BLOCKSCI_SCRATCH,
    walltime: str = DEFAULT_BLOCKSCI_WALLTIME,
    external_bitcoin_datadir: Path | None = None,
    bitcoin_blocks_uri: str | None = None,
    expected_block_hash: str | None = None,
    external_blocksci_dir: Path | None = None,
    external_network: str | None = None,
    external_max_block: int | None = None,
) -> str:
    """Render a parser-only job that publishes a checksummed reusable index."""
    if (
        sum(
            value is not None
            for value in (
                external_bitcoin_datadir,
                bitcoin_blocks_uri,
                external_blocksci_dir,
            )
        )
        > 1
    ):
        raise PBSError("Choose only one external Bitcoin or BlockSci source")

    download_inputs = ""
    verify_index = ""
    source_kind = "emulator"
    network = "bitcoin_regtest"
    source_description = "emulator Bitcoin and exported-block inputs"
    if external_bitcoin_datadir is not None:
        bitcoin_path = _require_bitcoin_datadir(external_bitcoin_datadir)
        network = _require_external_chain(external_network, external_max_block)
        source_kind = "external-bitcoin"
        source_description = "external Bitcoin Core block directory"
        prepare_source = (
            f"BITCOIN_DATADIR={shell_value(str(bitcoin_path))}\n"
            f"EXPORTED_MAX_BLOCK={external_max_block}\n"
            'test -d "$BITCOIN_DATADIR/blocks"'
        )
        produce_index = _parse_chain("parsing external chain through block $EXPORTED_MAX_BLOCK", command)
    elif bitcoin_blocks_uri is not None:
        blocks_uri = _block_archive_uri(bitcoin_blocks_uri)
        network = _require_external_chain(external_network, external_max_block)
        source_kind = "bitcoin-blocks-s3"
        verify_index = _verify_block_archive_chain(network, expected_block_hash)
        source_description = "verified Bitcoin block archive from S3"
        prepare_source = (
            f"BITCOIN_BLOCKS_URI={shell_value(blocks_uri)}\n"
            f"EXPORTED_MAX_BLOCK={external_max_block}\n"
            f"{_download_block_archive()}"
        )
        produce_index = _parse_chain(
            "parsing verified S3 block archive through block $EXPORTED_MAX_BLOCK",
            command,
        )
    elif external_blocksci_dir is not None:
        blocksci_path = external_blocksci_dir.expanduser().resolve()
        require_storage_path(blocksci_path)
        require_existing_path(blocksci_path, "external BlockSci directory")
        if not (blocksci_path / "config.json").is_file():
            raise PBSError(f"External BlockSci directory must contain config.json: {blocksci_path}")
        if not (blocksci_path / "parsed" / "chain" / "block.dat").is_file():
            raise PBSError(f"External BlockSci directory must contain parsed/chain/block.dat: {blocksci_path}")
        source_kind = "external-blocksci"
        network = "from-config"
        source_description = "existing external BlockSci index"
        prepare_source = (
            f"EXTERNAL_BLOCKSCI_DIR={shell_value(str(blocksci_path))}\n"
            'test -f "$EXTERNAL_BLOCKSCI_DIR/config.json"\n'
            'test -f "$EXTERNAL_BLOCKSCI_DIR/parsed/chain/block.dat"'
        )
        produce_index = (
            'cp -a "$EXTERNAL_BLOCKSCI_DIR" "$RUN_WORK/blocksci_data"\n'
            'CANONICAL_PARSED="/runs/emulation/logs/$RUN_ID/blocksci_data/parsed"\n'
            'sed -i -E \'s#("dataDirectory"[[:space:]]*:[[:space:]]*)"[^"]*"#\\1"\''
            '"$CANONICAL_PARSED"'
            '\'"#\' "$RUN_WORK/blocksci_data/config.json"\n'
            'grep -Fq "$CANONICAL_PARSED" "$RUN_WORK/blocksci_data/config.json" || { '
            'echo "Could not canonicalize external BlockSci dataDirectory" >&2; exit 1; }\n'
            'MAX_BLOCK_NUM="$(sed -nE \'s/.*"maxBlockNum"[[:space:]]*:[[:space:]]*([0-9]+).*/\\1/p\' '
            '"$RUN_WORK/blocksci_data/config.json" | head -n 1)"\n'
            'test -n "$MAX_BLOCK_NUM" && [ "$MAX_BLOCK_NUM" -gt 0 ] || { '
            'echo "External BlockSci config has no positive parser.maxBlockNum" >&2; exit 1; }\n'
            'EXPORTED_MAX_BLOCK="$((MAX_BLOCK_NUM - 1))"\n'
            'echo "[blocksci-parse] imported external index through block $EXPORTED_MAX_BLOCK"'
        )
    else:
        download_inputs = "\n".join(
            (
                render_s5cmd_sync(
                    '"$ARTIFACT_URI/$RUN_ID/bitcoin_data/*"',
                    '"$RUN_WORK/bitcoin_data/"',
                ),
                render_s5cmd_sync(
                    '"$ARTIFACT_URI/$RUN_ID/coinjoin_emulator_data/data/btc-node/*"',
                    '"$RUN_WORK/coinjoin_emulator_data/data/btc-node/"',
                ),
            )
        )
        prepare_source = (
            f"{download_inputs}\n"
            'BITCOIN_DATADIR="$RUN_WORK/bitcoin_data"\n'
            'if [ ! -d "$BITCOIN_DATADIR/regtest/blocks" ] && '
            '[ -d "$BITCOIN_DATADIR/data/regtest/blocks" ]; then\n'
            '  BITCOIN_DATADIR="$BITCOIN_DATADIR/data"\n'
            "fi\n"
            'test -d "$BITCOIN_DATADIR/regtest/blocks" || {\n'
            '  echo "BlockSci parsing requires a Bitcoin datadir containing regtest/blocks" >&2\n'
            "  exit 1\n"
            "}\n"
            'EXPORTED_MAX_BLOCK="$(find "$RUN_WORK/coinjoin_emulator_data/data/btc-node" '
            "-maxdepth 1 -type f -name 'block_*.json' -printf '%f\\n' | "
            "sed -nE 's/^block_([0-9]+)\\.json$/\\1/p' | sort -n | tail -n 1)" + '"\n'
            'test -n "$EXPORTED_MAX_BLOCK" || { '
            'echo "BlockSci parsing could not determine the exported maximum block" >&2; exit 1; }'
        )
        produce_index = _parse_chain("parsing through exported block $EXPORTED_MAX_BLOCK", command)
    return _render_stage(
        "blocksci_parse_s3_template.sh",
        "blocksci-parse",
        target,
        image=image,
        command=command,
        ncpus=ncpus,
        mem=mem,
        scratch=scratch,
        walltime=walltime,
        source_description=source_description,
        source_kind=source_kind,
        network=network,
        prepare_source=prepare_source,
        produce_index=produce_index,
        verify_index=verify_index,
        upload_log=render_s5cmd_cp('"$JOB_LOG"', '"$ARTIFACT_URI/$RUN_ID/logs/blocksci-parse.pbs.log"'),
        upload_cache=render_s5cmd_sync('"$CACHE_DIR/"', '"$ARTIFACT_URI/$RUN_ID/blocksci-parse_data/"'),
    )


def render_blocksci_update_s3_pbs(
    target: S3Target,
    source_run_id: str,
    image: str,
    command: str,
    *,
    external_network: str,
    external_max_block: int,
    external_bitcoin_datadir: Path | None = None,
    bitcoin_blocks_uri: str | None = None,
    expected_block_hash: str | None = None,
    ncpus: int = DEFAULT_BLOCKSCI_NCPUS,
    mem: str = DEFAULT_BLOCKSCI_MEM,
    scratch: str = DEFAULT_BLOCKSCI_SCRATCH,
    walltime: str = DEFAULT_BLOCKSCI_WALLTIME,
) -> str:
    """Render a job that incrementally updates one S3 cache into a fresh run.

    The cache must come from the same source: BlockSci remembers each block by
    its file number and offset, which only that Bitcoin datadir or block
    archive can resolve.
    """
    try:
        source_run_id = validate_run_id(source_run_id)
    except ValueError as error:
        raise PBSError(str(error)) from error
    if source_run_id == target.run_id:
        raise PBSError("Incremental BlockSci update requires different source and target run IDs")
    _require_external_chain(external_network, external_max_block)
    verify_index = ""
    if external_bitcoin_datadir is not None and bitcoin_blocks_uri is None:
        bitcoin_path = _require_bitcoin_datadir(external_bitcoin_datadir)
        source_kind = "external-bitcoin"
        prepare_source = (
            f"BITCOIN_DATADIR={shell_value(str(bitcoin_path))}\n"
            'test -d "$BITCOIN_DATADIR/blocks" || { '
            'echo "External Bitcoin coin directory must contain blocks/: $BITCOIN_DATADIR" >&2; exit 1; }'
        )
        prepare_blocks = ""
    elif bitcoin_blocks_uri is not None and external_bitcoin_datadir is None:
        source_kind = "bitcoin-blocks-s3"
        verify_index = _verify_block_archive_chain(external_network, expected_block_hash, update=True)
        prepare_source = f"BITCOIN_BLOCKS_URI={shell_value(_block_archive_uri(bitcoin_blocks_uri))}"
        prepare_blocks = (
            'echo "[blocksci-update] downloading block archive files after block $SOURCE_MAX_BLOCK"\n'
            + _download_block_archive("$SOURCE_CACHE_DIR/manifest.json")
        )
    else:
        raise PBSError("Incremental BlockSci update requires exactly one external Bitcoin datadir or block archive")
    return _render_stage(
        "blocksci_update_s3_template.sh",
        "blocksci-update",
        target,
        image=image,
        source_run_id=shell_value(source_run_id),
        network=shell_value(external_network),
        exported_max_block=external_max_block,
        source_kind=source_kind,
        prepare_source=prepare_source,
        prepare_blocks=prepare_blocks,
        verify_index=verify_index,
        command=command,
        ncpus=ncpus,
        mem=mem,
        scratch=scratch,
        walltime=walltime,
        download_source_cache=render_s5cmd_sync(
            '"$ARTIFACT_URI/$SOURCE_RUN_ID/blocksci-parse_data/*"',
            '"$SOURCE_CACHE_DIR/"',
        ),
        upload_cache=render_s5cmd_sync('"$CACHE_DIR/"', '"$ARTIFACT_URI/$RUN_ID/blocksci-parse_data/"'),
    )


def render_blocksci_analyze_s3_pbs(
    target: S3Target,
    image: str,
    command: str,
    *,
    mode: str = "blocksci-analyze",
    user_script: Path | None = None,
    external_baseline_uri: str | None = None,
    notebooks_dir: Path | None = None,
    notebook_port: int = 8888,
    ncpus: int = DEFAULT_BLOCKSCI_NCPUS,
    mem: str = DEFAULT_BLOCKSCI_MEM,
    scratch: str = DEFAULT_BLOCKSCI_SCRATCH,
    walltime: str = DEFAULT_BLOCKSCI_WALLTIME,
) -> str:
    """Render analysis, custom-script, or notebook work over a cached index."""
    if mode not in {
        "blocksci-analyze",
        "blocksci-script",
        "blocksci-notebook",
        "blocksci-external",
    }:
        raise PBSError(f"Unsupported reusable BlockSci mode: {mode}")
    require_safe_pbs_token(mode, "PBS stage")
    if isinstance(notebook_port, bool) or not isinstance(notebook_port, int) or not 1024 <= notebook_port <= 65535:
        raise PBSError("BlockSci notebook port must be between 1024 and 65535")

    extra_binds = ""
    prepare_mode = ""
    connection_help = ""
    output_check = ""
    upload_sources: list[tuple[str, str]] = []
    if mode == "blocksci-analyze":
        prepare_mode = 'mkdir -p "$RUN_WORK/blocksci-analysis_data"'
        output_check = (
            'test -f "$RUN_WORK/blocksci-analysis_data/blocksci_analysis.json" || {\n'
            '  echo "Reusable BlockSci analysis did not produce blocksci-analysis_data/blocksci_analysis.json" >&2\n'
            "  exit 1\n"
            "}"
        )
        upload_sources.append(
            (
                '"$RUN_WORK/blocksci-analysis_data/"',
                '"$ARTIFACT_URI/$RUN_ID/blocksci-analysis_data/"',
            )
        )
    elif mode == "blocksci-external":
        if external_baseline_uri is None:
            raise PBSError("External BlockSci report requires a Dumplings baseline URI")
        try:
            baseline_uri = validate_artifact_uri(external_baseline_uri)
        except ValueError as error:
            raise PBSError(f"Invalid Dumplings baseline URI: {error}") from error
        output_check = (
            'test -f "$RUN_WORK/coinjoinPipeline_data/unified_report.json" || {\n'
            '  echo "External BlockSci report did not produce unified_report.json" >&2\n'
            "  exit 1\n"
            "}"
        )
        upload_sources.append(
            (
                '"$RUN_WORK/coinjoinPipeline_data/"',
                '"$ARTIFACT_URI/$RUN_ID/coinjoinPipeline_data/"',
            )
        )
    elif mode == "blocksci-script":
        if user_script is None:
            raise PBSError("Reusable BlockSci script mode requires --blocksci-script")
        script_path = user_script.expanduser().resolve()
        require_storage_path(script_path)
        require_existing_path(script_path, "BlockSci user script")
        if not script_path.is_file():
            raise PBSError(f"BlockSci user script is not a file: {script_path}")
        prepare_mode = (
            'mkdir -p "$RUN_WORK/blocksci-custom-analysis_data"\n'
            f"USER_SCRIPT={shell_value(str(script_path))}\n"
            'cp "$USER_SCRIPT" "$RUN_WORK/blocksci-custom-analysis_data/script.py"\n'
            'sha256sum "$USER_SCRIPT" > "$RUN_WORK/blocksci-custom-analysis_data/script.py.sha256"'
        )
        extra_binds = 'EXTRA_BINDS+=(--bind "$USER_SCRIPT:/mnt/user-analysis.py:ro")'
        upload_sources.append(
            (
                '"$RUN_WORK/blocksci-custom-analysis_data/"',
                '"$ARTIFACT_URI/$RUN_ID/blocksci-custom-analysis_data/"',
            )
        )
    else:
        if notebooks_dir is not None:
            notebook_path = notebooks_dir.expanduser().resolve()
            require_storage_path(notebook_path)
            require_existing_path(notebook_path, "BlockSci notebooks directory")
            if not notebook_path.is_dir():
                raise PBSError(f"BlockSci notebooks path is not a directory: {notebook_path}")
            prepare_mode = f"NOTEBOOK_DIR={shell_value(str(notebook_path))}"
        else:
            prepare_mode = 'NOTEBOOK_DIR="$RUN_WORK/blocksci-notebooks_data"\nmkdir -p "$NOTEBOOK_DIR"'
        extra_binds = 'EXTRA_BINDS+=(--bind "$NOTEBOOK_DIR:/mnt/notebooks:rw")'
        connection_help = (
            f'echo "[blocksci-notebook] Jupyter port: {notebook_port}"\n'
            'LOGIN="${PBS_O_LOGNAME:-${USER:-<login>}}"\n'
            'FRONTEND="${PBS_O_HOST:-<frontend>}"\n'
            f'echo "[blocksci-notebook] Tunnel: ssh -N -J $LOGIN@$FRONTEND '
            f'-L {notebook_port}:127.0.0.1:{notebook_port} $LOGIN@$(hostname -f)"\n'
            'echo "[blocksci-notebook] The Jupyter token follows below."'
        )
        upload_sources.append(('"$NOTEBOOK_DIR/"', '"$ARTIFACT_URI/$RUN_ID/blocksci-notebooks_data/"'))

    upload_outputs = "\n  ".join(
        f"{render_s5cmd_sync(source, destination)} || upload_status=$?" for source, destination in upload_sources
    )
    downloads = [
        render_s5cmd_sync(
            '"$ARTIFACT_URI/$RUN_ID/blocksci-parse_data/*"',
            '"$CACHE_DIR/"',
        )
    ]
    if mode == "blocksci-analyze":
        downloads.extend(
            (
                render_s5cmd_sync(
                    '"$ARTIFACT_URI/$RUN_ID/.pipeline/exporters/*"',
                    '"$RUN_WORK/.pipeline/exporters/"',
                ),
                render_s5cmd_sync(
                    '"$ARTIFACT_URI/$RUN_ID/coinjoin_emulator_data/*"',
                    '"$RUN_WORK/coinjoin_emulator_data/"',
                ),
            )
        )
    elif mode == "blocksci-external":
        downloads.extend(
            (
                render_s5cmd_sync(
                    '"$ARTIFACT_URI/$RUN_ID/.pipeline/exporters/*"',
                    '"$RUN_WORK/.pipeline/exporters/"',
                ),
                'mkdir -p "$RUN_WORK/coinjoin-analysis_data"',
                render_s5cmd_cp(
                    shell_value(baseline_uri),
                    '"$RUN_WORK/coinjoin-analysis_data/coinjoin_tx_info.json"',
                ),
            )
        )
    return _render_stage(
        "blocksci_analyze_s3_template.sh",
        mode,
        target,
        image=image,
        command=command,
        mode=shell_value(mode),
        stage=mode,
        job_name=mode.replace("-", "_") + "_s3",
        ncpus=ncpus,
        mem=mem,
        scratch=scratch,
        walltime=walltime,
        download_inputs="\n".join(downloads),
        prepare_mode=prepare_mode,
        extra_binds=extra_binds,
        connection_help=connection_help,
        output_check=output_check,
        upload_outputs=upload_outputs,
    )


def render_unified_report_s3_pbs(
    target: S3Target,
    image: str,
    command: str,
    *,
    ncpus: int = DEFAULT_UNIFIED_REPORT_NCPUS,
    mem: str = DEFAULT_UNIFIED_REPORT_MEM,
    scratch: str = DEFAULT_UNIFIED_REPORT_SCRATCH,
    walltime: str = DEFAULT_UNIFIED_REPORT_WALLTIME,
    include_mappings: bool = False,
) -> str:
    """Render the S3 report-only job that joins both analyzer outputs."""
    downloads = [
        render_s5cmd_sync(
            '"$ARTIFACT_URI/$RUN_ID/.pipeline/exporters/*"',
            '"$RUN_WORK/.pipeline/exporters/"',
        ),
        render_s5cmd_sync(
            '"$ARTIFACT_URI/$RUN_ID/coinjoin_emulator_data/*"',
            '"$RUN_WORK/coinjoin_emulator_data/"',
        ),
        render_s5cmd_sync(
            '"$ARTIFACT_URI/$RUN_ID/coinjoin-analysis_data/*"',
            '"$RUN_WORK/coinjoin-analysis_data/"',
        ),
        render_s5cmd_sync(
            '"$ARTIFACT_URI/$RUN_ID/blocksci-analysis_data/*"',
            '"$RUN_WORK/blocksci-analysis_data/"',
        ),
    ]
    if include_mappings:
        downloads.append(
            render_s5cmd_sync(
                '"$ARTIFACT_URI/$RUN_ID/coinjoin-mappings_data/*"',
                '"$RUN_WORK/coinjoin-mappings_data/"',
            )
        )
    return _render_stage(
        "unified_report_s3_template.sh",
        "unified-report",
        target,
        image=image,
        command=command,
        ncpus=ncpus,
        mem=mem,
        scratch=scratch,
        walltime=walltime,
        download_inputs="\n".join(downloads),
        upload_report=render_s5cmd_sync(
            '"$REPORT_DIR/"',
            '"$ARTIFACT_URI/$RUN_ID/coinjoinPipeline_data/"',
        ),
    )


def render_template(template: str, **values) -> str:
    bootstrap = (EXECUTION_DIR / "pbs_stage.sh").read_text(encoding="utf-8")
    download_exporters = render_s5cmd_sync(
        '"$ARTIFACT_URI/$RUN_ID/.pipeline/exporters/*"',
        '"$RUN_WORK/.pipeline/exporters/"',
    )
    return template.format(bootstrap=bootstrap, download_exporters=download_exporters, **values)
