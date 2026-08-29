"""Public wrapper CLI declaration, kept separate from runtime orchestration."""

from __future__ import annotations

import argparse
import os

from client.cli_defaults import (
    DEFAULT_DRIVER,
    DEFAULT_JOINMARKET_DETECTOR,
    DEFAULT_JOINMARKET_MAX_DEPTH,
    DEFAULT_JOINMARKET_MIN_BASE_FEE,
    DEFAULT_JOINMARKET_PERCENTAGE_FEE,
    DEFAULT_K8S_IMAGE_PREFIX,
    DEFAULT_K8S_NAMESPACE,
    DEFAULT_MIN_INPUT_COUNT,
    DEFAULT_RUN_TIMEZONE,
    VALID_DRIVERS,
)
from client.cli_options import (
    add_coinjoin_type_argument,
    add_dry_run_argument,
    add_engine_argument,
    add_runtime_argument,
)
from client.cli_validation import (
    non_negative_float,
    non_negative_int,
    positive_int,
    run_timezone,
)
from client.pbs import (
    DEFAULT_BLOCKSCI_IMAGE as DEFAULT_PBS_BLOCKSCI_IMAGE,
)
from client.pbs import (
    DEFAULT_BLOCKSCI_MEM,
    DEFAULT_BLOCKSCI_NCPUS,
    DEFAULT_BLOCKSCI_SCRATCH,
    DEFAULT_BLOCKSCI_WALLTIME,
    DEFAULT_COINJOIN_ANALYSIS_MEM,
    DEFAULT_COINJOIN_ANALYSIS_NCPUS,
    DEFAULT_COINJOIN_ANALYSIS_SCRATCH,
    DEFAULT_COINJOIN_ANALYSIS_WALLTIME,
    DEFAULT_MAPPINGS_ENUMERATOR_IMAGE,
    DEFAULT_SAKE_IMAGE,
    DEFAULT_UNIFIED_REPORT_MEM,
    DEFAULT_UNIFIED_REPORT_NCPUS,
    DEFAULT_UNIFIED_REPORT_SCRATCH,
    DEFAULT_UNIFIED_REPORT_WALLTIME,
)
from client.pbs import (
    DEFAULT_COINJOIN_ANALYSIS_IMAGE as DEFAULT_PBS_COINJOIN_ANALYSIS_IMAGE,
)
from client.runtime import CONTAINER_RUNTIME_ENV, DEFAULT_CONTAINER_RUNTIME


def add_kubernetes_arguments(arg_parser: argparse.ArgumentParser) -> None:
    """Add Kubernetes-specific arguments to a subparser."""
    arg_parser.add_argument(
        "--driver",
        choices=VALID_DRIVERS,
        default=DEFAULT_DRIVER,
        help=(
            "Container orchestration driver for the emulation step. "
            "'docker' uses Docker-in-Docker via compose (default). "
            "'kubernetes' runs the emulation pods on a Kubernetes cluster."
        ),
    )
    arg_parser.add_argument(
        "--namespace",
        type=str,
        default=DEFAULT_K8S_NAMESPACE,
        help=f"Kubernetes namespace for emulation pods (default: {DEFAULT_K8S_NAMESPACE}).",
    )
    arg_parser.add_argument(
        "--reuse-namespace",
        action="store_true",
        default=False,
        help="Reuse an existing Kubernetes namespace instead of creating a new one.",
    )
    arg_parser.add_argument(
        "--kubeconfig",
        type=str,
        default=None,
        help="Path to kubeconfig file (default: ~/.kube/config).",
    )
    arg_parser.add_argument(
        "--image-prefix",
        type=str,
        default=DEFAULT_K8S_IMAGE_PREFIX,
        help=f"Image registry prefix for Kubernetes pods (default: {DEFAULT_K8S_IMAGE_PREFIX}).",
    )
    arg_parser.add_argument(
        "--kubernetes-btc-datadir",
        type=str,
        default=None,
        help=(
            "Shared absolute Bitcoin Core datadir mounted directly into the "
            "Kubernetes btc-node pod. Defaults to --pbs-bitcoin-datadir when set."
        ),
    )
    arg_parser.add_argument(
        "--copy-to-host",
        action="store_true",
        default=False,
        help=("Use the legacy pod-to-wrapper Bitcoin datadir download instead of writing directly to shared storage."),
    )
    add_emulator_infrastructure_image_arguments(arg_parser)


def add_artifact_arguments(
    arg_parser: argparse.ArgumentParser,
    *,
    pbs_credentials: bool = False,
    kubernetes_secret: bool = False,
) -> None:
    arg_parser.add_argument(
        "--artifact-backend",
        choices=("shared-storage", "s3"),
        default="shared-storage",
        help="Artifact transport backend (default: shared-storage).",
    )
    arg_parser.add_argument("--artifact-uri", help="S3-compatible run prefix, for example s3://bucket/runs.")
    arg_parser.add_argument(
        "--uploader-image",
        type=str,
        default=None,
        help="Override the in-cluster uploader/preflight image (default: container/uploader.image).",
    )
    arg_parser.add_argument(
        "--unified-report-image",
        type=str,
        default=None,
        help="Override the pinned Python image for the PBS unified-report step "
             "(default: container/unified-report.image).",
    )
    arg_parser.add_argument("--s3-endpoint-url", help="CESNET/MetaCentrum S3-compatible endpoint URL.")
    arg_parser.add_argument("--run-id", help="Deterministic artifact run identifier.")
    if pbs_credentials:
        arg_parser.add_argument("--s3-credentials-file", help="Absolute s5cmd credentials-file path for PBS jobs.")
        arg_parser.add_argument("--s3-profile", help="Named profile in the s5cmd credentials file.")
    if kubernetes_secret:
        arg_parser.add_argument("--s3-secret-name", help="Pre-created Kubernetes Secret for S3-compatible upload.")


def add_emulator_infrastructure_image_arguments(arg_parser: argparse.ArgumentParser) -> None:
    arg_parser.add_argument(
        "--coinjoin-infrastructure-local-build",
        action="store_true",
        default=False,
        help="Build btc-node, JoinMarket client-server, and IRC server locally inside the emulator runtime.",
    )


def add_run_timezone_argument(arg_parser: argparse.ArgumentParser) -> None:
    arg_parser.add_argument(
        "--run-timezone",
        type=run_timezone,
        default=DEFAULT_RUN_TIMEZONE,
        metavar="IANA_ZONE",
        help=f"IANA timezone used in newly created run directory names (default: {DEFAULT_RUN_TIMEZONE}).",
    )


def add_joinmarket_detector_arguments(arg_parser: argparse.ArgumentParser) -> None:
    arg_parser.add_argument(
        "--joinmarket-detector",
        choices=("possible", "definite"),
        default=DEFAULT_JOINMARKET_DETECTOR,
        help=(
            "BlockSci JoinMarket subset detector to use when --coinjoin-type joinmarket "
            f"(default: {DEFAULT_JOINMARKET_DETECTOR})."
        ),
    )
    arg_parser.add_argument(
        "--joinmarket-min-base-fee",
        type=int,
        default=DEFAULT_JOINMARKET_MIN_BASE_FEE,
        help=f"Minimum base fee for the JoinMarket detector (default: {DEFAULT_JOINMARKET_MIN_BASE_FEE}).",
    )
    arg_parser.add_argument(
        "--joinmarket-percentage-fee",
        type=float,
        default=DEFAULT_JOINMARKET_PERCENTAGE_FEE,
        help=f"Percentage fee for the JoinMarket detector (default: {DEFAULT_JOINMARKET_PERCENTAGE_FEE}).",
    )
    arg_parser.add_argument(
        "--joinmarket-max-depth",
        type=int,
        default=DEFAULT_JOINMARKET_MAX_DEPTH,
        help=f"Maximum subset-search depth for the JoinMarket detector (default: {DEFAULT_JOINMARKET_MAX_DEPTH}).",
    )


def add_blocksci_script_argument(arg_parser: argparse.ArgumentParser) -> None:
    arg_parser.add_argument(
        "--blocksci-script",
        "--blocksciScript",
        dest="blocksci_script",
        metavar="PATH",
        help=(
            "Run a Python script after BlockSci parsing. The script receives ACTIVE_RUN_ID, "
            "BLOCKSCI_CONFIG, and BLOCKSCI_RUN_DIR in its environment and runs with the run directory as cwd."
        ),
    )


def add_blocksci_reusable_arguments(arg_parser: argparse.ArgumentParser) -> None:
    """Add the opt-in parse-once/analyze-many S3 BlockSci workflow."""
    arg_parser.add_argument(
        "--blocksci-workflow",
        choices=("combined", "reusable", "cached"),
        default="combined",
        help=(
            "BlockSci S3 workflow: combined parses and analyzes in one PBS job; "
            "reusable publishes a cache then submits dependent work; cached reuses "
            "an already published blocksci-parse_data cache."
        ),
    )
    arg_parser.add_argument(
        "--blocksci-task",
        choices=("detect", "parse", "update", "script", "notebook", "external"),
        default="detect",
        help=(
            "BlockSci work to run; parse publishes a reusable cache and update "
            "incrementally advances an external-Bitcoin cache; external builds a "
            "BlockSci-vs-Dumplings mainnet report (default: detect)."
        ),
    )
    arg_parser.add_argument(
        "--blocksci-cache-source-run-id",
        default=None,
        help=(
            "Existing S3 run ID whose verified blocksci-parse_data cache is the "
            "input to --blocksci-task update. The updated cache is written to --run-id."
        ),
    )
    arg_parser.add_argument(
        "--blocksci-notebook-port",
        type=positive_int,
        default=None,
        help="Jupyter port on the assigned PBS node (default: 8888).",
    )
    arg_parser.add_argument(
        "--blocksci-notebooks-dir",
        default=None,
        help=(
            "Optional shared /storage directory mounted read-write as /mnt/notebooks; "
            "otherwise notebooks are uploaded under blocksci-notebooks_data/."
        ),
    )
    arg_parser.add_argument(
        "--blocksci-external-bitcoin-datadir",
        metavar="PATH",
        help=(
            "Parse an external Bitcoin Core coin directory under /storage; "
            "the directory must contain blocks/."
        ),
    )
    arg_parser.add_argument(
        "--blocksci-bitcoin-blocks-uri",
        metavar="S3_URI",
        help=(
            "S3 prefix produced by bitcoin-block-archive containing "
            "archive-manifest.json, blk*.dat, and SHA-256 sidecars."
        ),
    )
    arg_parser.add_argument(
        "--blocksci-external-blocksci-dir",
        metavar="PATH",
        help=(
            "Import an existing BlockSci directory under /storage containing "
            "config.json and parsed/."
        ),
    )
    arg_parser.add_argument(
        "--blocksci-network",
        choices=("bitcoin", "bitcoin_testnet", "bitcoin_regtest"),
        default=None,
        help="BlockSci network for --blocksci-external-bitcoin-datadir.",
    )
    arg_parser.add_argument(
        "--blocksci-max-block",
        type=non_negative_int,
        default=None,
        help="Inclusive maximum block height for external Bitcoin parsing.",
    )
    arg_parser.add_argument(
        "--external-baseline-uri",
        metavar="S3_URI",
        help=(
            "S3 object containing Dumplings coinjoin_tx_info.json for "
            "--blocksci-task external."
        ),
    )


def add_pbs_arguments(arg_parser: argparse.ArgumentParser) -> None:
    """Add PBS-related arguments to a subparser.

    --analysisPbs:  run coinjoin-analysis stage through PBS
    --blocksciPbs:  run BlockSci parser/index/report stage through PBS
    """
    arg_parser.add_argument(
        "--analysisPbs",
        action="store_true",
        default=False,
        help="Submit coinjoin-analysis as a PBS job on MetaCentrum.",
    )
    arg_parser.add_argument(
        "--blocksciPbs",
        action="store_true",
        default=False,
        help="Submit BlockSci analysis as a PBS job on MetaCentrum.",
    )
    arg_parser.add_argument(
        "--mappingsPbs",
        action="store_true",
        default=False,
        help="Submit the Wasabi mapping enumerator and Sake as one PBS job.",
    )
    arg_parser.add_argument(
        "--pbs-ncpus",
        type=int,
        default=None,
        help=(
            "Number of CPUs for the PBS job "
            f"(default: {DEFAULT_BLOCKSCI_NCPUS} for BlockSci, "
            f"{DEFAULT_COINJOIN_ANALYSIS_NCPUS} for coinjoin-analysis)."
        ),
    )
    arg_parser.add_argument(
        "--pbs-mem",
        type=str,
        default=None,
        help=(
            "Memory for the PBS job "
            f"(default: {DEFAULT_BLOCKSCI_MEM} for BlockSci, {DEFAULT_COINJOIN_ANALYSIS_MEM} for coinjoin-analysis)."
        ),
    )
    arg_parser.add_argument(
        "--pbs-scratch",
        type=str,
        default=None,
        help=(
            "Scratch storage for the PBS job "
            f"(default: {DEFAULT_BLOCKSCI_SCRATCH} for BlockSci, "
            f"{DEFAULT_COINJOIN_ANALYSIS_SCRATCH} for coinjoin-analysis)."
        ),
    )
    arg_parser.add_argument(
        "--pbs-walltime",
        type=str,
        default=None,
        help=(
            "Walltime for the PBS job "
            f"(default: {DEFAULT_BLOCKSCI_WALLTIME} for BlockSci, "
            f"{DEFAULT_COINJOIN_ANALYSIS_WALLTIME} for coinjoin-analysis)."
        ),
    )
    stage_defaults = {
        "blocksci": (
            DEFAULT_BLOCKSCI_NCPUS,
            DEFAULT_BLOCKSCI_MEM,
            DEFAULT_BLOCKSCI_SCRATCH,
            DEFAULT_BLOCKSCI_WALLTIME,
        ),
        "analysis": (
            DEFAULT_COINJOIN_ANALYSIS_NCPUS,
            DEFAULT_COINJOIN_ANALYSIS_MEM,
            DEFAULT_COINJOIN_ANALYSIS_SCRATCH,
            DEFAULT_COINJOIN_ANALYSIS_WALLTIME,
        ),
        "mappings": (
            DEFAULT_COINJOIN_ANALYSIS_NCPUS,
            DEFAULT_COINJOIN_ANALYSIS_MEM,
            DEFAULT_COINJOIN_ANALYSIS_SCRATCH,
            DEFAULT_COINJOIN_ANALYSIS_WALLTIME,
        ),
    }
    for stage, (ncpus, mem, scratch, walltime) in stage_defaults.items():
        label = "coinjoin-analysis" if stage == "analysis" else stage
        arg_parser.add_argument(
            f"--pbs-{stage}-ncpus",
            type=int,
            default=None,
            help=(
                f"CPU count for the {label} PBS job "
                f"(default: {ncpus}; overrides --pbs-ncpus)."
            ),
        )
        arg_parser.add_argument(
            f"--pbs-{stage}-mem",
            default=None,
            help=(
                f"Memory for the {label} PBS job "
                f"(default: {mem}; overrides --pbs-mem)."
            ),
        )
        arg_parser.add_argument(
            f"--pbs-{stage}-scratch",
            default=None,
            help=(
                f"Scratch storage for the {label} PBS job "
                f"(default: {scratch}; overrides --pbs-scratch)."
            ),
        )
        arg_parser.add_argument(
            f"--pbs-{stage}-walltime",
            default=None,
            help=(
                f"Walltime for the {label} PBS job "
                f"(default: {walltime}; overrides --pbs-walltime)."
            ),
        )
    arg_parser.add_argument(
        "--pbs-image",
        type=str,
        default=None,
        help="Singularity image override for either PBS stage.",
    )
    arg_parser.add_argument(
        "--pbs-blocksci-image",
        type=str,
        default=None,
        help=f"Singularity image for BlockSci PBS jobs (default: {DEFAULT_PBS_BLOCKSCI_IMAGE}).",
    )
    arg_parser.add_argument(
        "--pbs-coinjoin-analysis-image",
        type=str,
        default=None,
        help=f"Singularity image for coinjoin-analysis PBS jobs (default: {DEFAULT_PBS_COINJOIN_ANALYSIS_IMAGE}).",
    )
    arg_parser.add_argument("--pbs-mappings-enumerator-image", default=DEFAULT_MAPPINGS_ENUMERATOR_IMAGE)
    arg_parser.add_argument("--pbs-sake-image", default=DEFAULT_SAKE_IMAGE)

    arg_parser.add_argument("--mapping-mining-fee-rate", type=non_negative_int, default=1)
    arg_parser.add_argument("--mapping-coordination-fee-rate", type=non_negative_float, default=0.003)
    arg_parser.add_argument("--mapping-max-decomposition-fee", type=non_negative_int, default=6000)
    arg_parser.add_argument("--mapping-mode", choices=("numeric", "all"), default="numeric")
    arg_parser.add_argument("--mapping-timeout", type=positive_int, default=60)
    arg_parser.add_argument("--mapping-retry-timeout", type=positive_int, default=600)
    arg_parser.add_argument("--sake-seed", type=non_negative_int, default=20260704)
    arg_parser.add_argument(
        "--pbs-bitcoin-datadir",
        type=str,
        default=os.environ.get("PBS_BITCOIN_DATADIR"),
        help=(
            "Shared /storage Bitcoin Core datadir for BlockSci PBS jobs. "
            "It must contain regtest/blocks. Can also be set with PBS_BITCOIN_DATADIR."
        ),
    )


def add_unified_report_pbs_arguments(arg_parser: argparse.ArgumentParser) -> None:
    """Add resource overrides for the S3 report-only PBS job."""
    arg_parser.add_argument(
        "--pbs-unified-report-ncpus",
        type=int,
        default=None,
        help=(
            "CPU count for the unified-report PBS job "
            f"(default: {DEFAULT_UNIFIED_REPORT_NCPUS}; overrides --pbs-ncpus)."
        ),
    )
    arg_parser.add_argument(
        "--pbs-unified-report-mem",
        default=None,
        help=(
            "Memory for the unified-report PBS job "
            f"(default: {DEFAULT_UNIFIED_REPORT_MEM}; overrides --pbs-mem)."
        ),
    )
    arg_parser.add_argument(
        "--pbs-unified-report-scratch",
        default=None,
        help=(
            "Scratch storage for the unified-report PBS job "
            f"(default: {DEFAULT_UNIFIED_REPORT_SCRATCH}; overrides --pbs-scratch)."
        ),
    )
    arg_parser.add_argument(
        "--pbs-unified-report-walltime",
        default=None,
        help=(
            "Walltime for the unified-report PBS job "
            f"(default: {DEFAULT_UNIFIED_REPORT_WALLTIME}; overrides --pbs-walltime)."
        ),
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the public wrapper parser for CLI and metadata consumers."""
    parser = argparse.ArgumentParser(description="Run analysis pipeline via project shell scripts.")

    add_runtime_argument(
        parser,
        default=os.environ.get(CONTAINER_RUNTIME_ENV, DEFAULT_CONTAINER_RUNTIME),
        help_text=(
            "Container runtime for host commands "
            f"(default: {DEFAULT_CONTAINER_RUNTIME}; can also be set with {CONTAINER_RUNTIME_ENV})."
        ),
    )

    subparsers = parser.add_subparsers(dest="action", required=True)

    emulate_parser = subparsers.add_parser("emulate", help="Run emulate.sh with optional JSON scenario.")
    add_runtime_argument(emulate_parser)
    add_engine_argument(emulate_parser, required=True)
    add_dry_run_argument(emulate_parser)
    emulate_parser.add_argument("--scenario", help="JSON scenario path.")
    add_run_timezone_argument(emulate_parser)
    add_kubernetes_arguments(emulate_parser)
    # Frontend credentials are no longer PBS-only here: standalone S3 emulation
    # checks the run prefix and stages the exporters with the frontend's own
    # s5cmd before creating the Job, exactly as full-run does.
    add_artifact_arguments(emulate_parser, kubernetes_secret=True, pbs_credentials=True)
    emulate_parser.add_argument(
        "--pbs-bitcoin-datadir",
        default=None,
        help="Shared-storage-only PBS Bitcoin datadir; rejected by Kubernetes S3-compatible mode.",
    )

    clean_parser = subparsers.add_parser("clean", help="Run delete.sh (remove containers + volumes).")
    add_runtime_argument(clean_parser)
    add_dry_run_argument(clean_parser)
    clean_parser.add_argument("--yes", action="store_true", help="Confirm removal of runtime containers and volumes.")
    analyze_parser = subparsers.add_parser("analyze", help="Run analysis.sh.")
    add_runtime_argument(analyze_parser)
    add_engine_argument(analyze_parser, required=True)
    add_dry_run_argument(analyze_parser)
    analyze_parser.add_argument("--run-dir", help="Emulation run folder name or path.")
    analyze_parser.add_argument("--scenario", help="Fallback scenario JSON if the run folder has no scenario.json.")
    add_coinjoin_type_argument(analyze_parser)
    analyze_parser.add_argument(
        "--min-input-count",
        type=positive_int,
        default=DEFAULT_MIN_INPUT_COUNT,
        help="Minimum transaction input count considered by detection (default: BlockSci height-aware threshold).",
    )
    add_joinmarket_detector_arguments(analyze_parser)
    add_blocksci_script_argument(analyze_parser)
    add_pbs_arguments(analyze_parser)
    mappings_parser = subparsers.add_parser("mappings", help="Run Wasabi mapping enumerator and Sake via PBS.")
    add_runtime_argument(mappings_parser)
    add_engine_argument(mappings_parser, required=True)
    add_dry_run_argument(mappings_parser)
    mappings_parser.add_argument("--run-dir", required=True)
    add_coinjoin_type_argument(mappings_parser)
    add_pbs_arguments(mappings_parser)
    export_parser = subparsers.add_parser(
        "export",
        help="Run only unified_report.json export against existing analysis outputs.",
    )
    add_runtime_argument(export_parser)
    add_engine_argument(export_parser, required=True)
    add_dry_run_argument(export_parser)
    export_parser.add_argument("--run-dir", help="Emulation run folder name or path.")
    export_parser.add_argument("--scenario", help="Fallback scenario JSON if the run folder has no scenario.json.")
    add_coinjoin_type_argument(export_parser)
    export_parser.add_argument(
        "--min-input-count",
        type=positive_int,
        default=DEFAULT_MIN_INPUT_COUNT,
        help="Minimum transaction input count considered by detection (default: BlockSci height-aware threshold).",
    )
    add_joinmarket_detector_arguments(export_parser)
    coinjoin_parser = subparsers.add_parser(
        "coinjoin-analysis",
        aliases=["coinjoin"],
        help="Run only coinjoin-analysis against one collected emulator run.",
    )
    add_runtime_argument(coinjoin_parser)
    add_dry_run_argument(coinjoin_parser)
    coinjoin_target = coinjoin_parser.add_mutually_exclusive_group()
    coinjoin_target.add_argument("--run-dir", help="Emulation run folder name or path.")
    coinjoin_target.add_argument(
        "--all-runs",
        action="store_true",
        help="Analyze every run folder under the emulation logs root.",
    )
    coinjoin_parser.add_argument(
        "--analysis-action",
        choices=("collect_docker", "analyze_only"),
        default="collect_docker",
        help=(
            "Extract emulator artifacts and analyze them (collect_docker), or rerun "
            "analysis from an existing coinjoin_tx_info.json (analyze_only)."
        ),
    )
    add_pbs_arguments(coinjoin_parser)
    initialize_parser = subparsers.add_parser(
        "initialize", help="Download all required images for emulate/analyze ahead of time."
    )
    add_runtime_argument(initialize_parser)
    add_dry_run_argument(initialize_parser)

    s3_pbs_parser = subparsers.add_parser(
        "pbs-from-s3",
        help="Submit PBS analysis for an existing CESNET/MetaCentrum S3-compatible run.",
    )
    add_runtime_argument(s3_pbs_parser)
    add_engine_argument(s3_pbs_parser, required=True)
    add_dry_run_argument(s3_pbs_parser)
    add_artifact_arguments(s3_pbs_parser, pbs_credentials=True)
    add_coinjoin_type_argument(s3_pbs_parser)
    s3_pbs_parser.add_argument("--min-input-count", type=positive_int, default=DEFAULT_MIN_INPUT_COUNT)
    add_joinmarket_detector_arguments(s3_pbs_parser)
    add_blocksci_script_argument(s3_pbs_parser)
    add_blocksci_reusable_arguments(s3_pbs_parser)
    # Staging follows the stages this invocation submits; pre-staging for a
    # later detect/report run is deliberate, never a side effect.
    s3_pbs_parser.add_argument(
        "--stage-exporters",
        action="store_true",
        help=(
            "Upload the checkout's exporters into the run prefix even when the "
            "selected stages do not run them, so a later detect or report stage "
            "finds the tree this invocation was launched from."
        ),
    )
    add_pbs_arguments(s3_pbs_parser)
    add_unified_report_pbs_arguments(s3_pbs_parser)

    full_parser = subparsers.add_parser("full-run", help="Run delete.sh, then emulate.sh, then analysis.sh.")
    add_runtime_argument(full_parser)
    add_engine_argument(full_parser, required=True)
    add_dry_run_argument(full_parser)
    full_parser.add_argument("--scenario", help="JSON scenario path.")
    add_run_timezone_argument(full_parser)
    add_coinjoin_type_argument(full_parser)
    full_parser.add_argument(
        "--min-input-count",
        type=positive_int,
        default=DEFAULT_MIN_INPUT_COUNT,
        help="Minimum transaction input count considered by detection (default: BlockSci height-aware threshold).",
    )
    add_joinmarket_detector_arguments(full_parser)
    add_blocksci_script_argument(full_parser)
    add_blocksci_reusable_arguments(full_parser)
    add_kubernetes_arguments(full_parser)
    add_pbs_arguments(full_parser)
    add_unified_report_pbs_arguments(full_parser)
    add_artifact_arguments(full_parser, pbs_credentials=True, kubernetes_secret=True)
    full_parser.add_argument(
        "--parallel",
        action="store_true",
        default=False,
        help="Run BlockSci and coinjoin-analysis concurrently after emulation.",
    )
    full_parser.add_argument(
        "--emulation-timeout",
        type=positive_int,
        default=21600,
        help="Seconds to wait for the Kubernetes S3-compatible emulation upload marker (S3 backend only).",
    )

    return parser

