"""Call signatures of the I/O operations the wrapper injects into its adapters.

The orchestration modules receive their Compose, PBS, S3, and Kubernetes side
effects as operation bundles so ``client.wrapper`` can stay the patch point for
tests.  A bare ``Callable[..., T]`` field let a renamed keyword or a dropped
argument through type checking until the call failed at run time; each
protocol here restates the signature of the one production function bound to
it, so the binding in ``wrapper`` and every call site are checked against the
same parameter list.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from client.artifacts import S3Access, S3Target
    from client.pipeline_logging import StageLog


# --- Compose environment and scripts -------------------------------------


class ComposeEnvironment(Protocol):
    """``wrapper.compose_env``."""

    def __call__(
        self,
        active_run_id: str | None = ...,
        engine: str = ...,
        coinjoin_type: str = ...,
        min_input_count: int | None = ...,
        scenario: str | None = ...,
        joinmarket_detector: str = ...,
        joinmarket_min_base_fee: int = ...,
        joinmarket_percentage_fee: float = ...,
        joinmarket_max_depth: int = ...,
        run_timezone_name: str = ...,
    ) -> dict[str, str]: ...


class ComposeEnvironmentFromArgs(Protocol):
    """``wrapper.compose_env_from_args``."""

    def __call__(
        self,
        args: argparse.Namespace,
        active_run_id: str | None = ...,
        *,
        include_scenario: bool = ...,
    ) -> dict[str, str]: ...


class RunScript(Protocol):
    """``wrapper.run_script``."""

    def __call__(
        self,
        script: Path,
        *args: str,
        active_run_id: str | None = ...,
        engine: str = ...,
        coinjoin_type: str = ...,
        min_input_count: int | None = ...,
        scenario: str | None = ...,
        joinmarket_detector: str = ...,
        joinmarket_min_base_fee: int = ...,
        joinmarket_percentage_fee: float = ...,
        joinmarket_max_depth: int = ...,
        run_timezone_name: str = ...,
        blocksci_script: str | None = ...,
    ) -> None: ...


class CapturedPipelineStage(Protocol):
    """``pipeline_logging.captured_pipeline_stage``."""

    def __call__(
        self,
        logs_root: Path,
        stage_name: str,
        run_dir: Path | None = ...,
    ) -> AbstractContextManager[StageLog]: ...


# --- Stage runners --------------------------------------------------------


class RunCoinjoinAnalysis(Protocol):
    """``wrapper.run_coinjoin_analysis``."""

    def __call__(
        self,
        run_dir_arg: str | None = ...,
        all_runs: bool = ...,
        analysis_action: str = ...,
    ) -> None: ...


class RunPBSStage(Protocol):
    """Shared-storage baseline and mappings PBS stages."""

    def __call__(
        self,
        args: argparse.Namespace,
        run_dir: Path,
        *,
        wait: bool = ...,
    ) -> None: ...


class RunBlockSciPBSStage(Protocol):
    """``wrapper.run_blocksci_pbs_stage``."""

    def __call__(
        self,
        args: argparse.Namespace,
        run_dir: Path,
        *,
        wait: bool = ...,
        include_report: bool = ...,
    ) -> None: ...


class RunBlockSciDockerStage(Protocol):
    """``wrapper.run_blocksci_docker_stage``."""

    def __call__(
        self,
        args: argparse.Namespace,
        run_dir: Path,
        *,
        include_report: bool,
    ) -> None: ...


class RunKubernetesEmulation(Protocol):
    """``wrapper.run_kubernetes_emulation``."""

    def __call__(
        self,
        scenario: str | None = ...,
        engine: str = ...,
        namespace: str = ...,
        reuse_namespace: bool = ...,
        image_prefix: str = ...,
        kubeconfig: str | None = ...,
        coinjoin_infrastructure_local_build: bool = ...,
        run_timezone_name: str = ...,
        kubernetes_btc_datadir: str | None = ...,
        copy_to_host: bool = ...,
        prepare_local_analysis: bool = ...,
    ) -> None: ...


# --- Marker waits ---------------------------------------------------------


class WaitForPBSMarker(Protocol):
    """``pbs.wait_for_pbs_marker`` (shared-storage markers)."""

    def __call__(
        self,
        run_dir: Path,
        stage: str,
        poll_interval: int = ...,
        *,
        job_id: str | None = ...,
        timeout_seconds: int | None = ...,
    ) -> None: ...


class WaitForS3Marker(Protocol):
    """``artifacts.wait_for_s3_marker``."""

    def __call__(
        self,
        stage: str,
        done_uri: str,
        failed_uri: str,
        access: S3Access,
        *,
        timeout_seconds: int,
        start_timeout_seconds: int | None = ...,
        poll_interval: int = ...,
        probe: Callable[[], str] | None = ...,
    ) -> None: ...


class WaitForS3PBSStage(Protocol):
    """``wrapper.wait_for_s3_pbs_stage``."""

    def __call__(
        self,
        *,
        stage: str,
        job_id: str,
        run_prefix: str,
        access: S3Access,
        walltime: str,
    ) -> None: ...


# --- Shared-storage PBS submissions ---------------------------------------


class SubmitBlockSciPBS(Protocol):
    """``pbs.submit_blocksci_pbs``."""

    def __call__(
        self,
        run_dir: Path,
        logs_root: Path,
        bitcoin_datadir: Path,
        exporters_dir: Path,
        image: str,
        command: str,
        *,
        ncpus: int = ...,
        mem: str = ...,
        scratch: str = ...,
        walltime: str = ...,
        dry_run: bool = ...,
        stage: str = ...,
        job_name: str = ...,
    ) -> str | None: ...


class SubmitCoinjoinAnalysisPBS(Protocol):
    """``pbs.submit_coinjoin_analysis_pbs``."""

    def __call__(
        self,
        run_dir: Path,
        output_dir: Path,
        input_data_dir: Path,
        image: str,
        command: str,
        *,
        ncpus: int = ...,
        mem: str = ...,
        scratch: str = ...,
        walltime: str = ...,
        dry_run: bool = ...,
    ) -> str | None: ...


class SubmitMappingsPBS(Protocol):
    """``pbs.submit_mappings_pbs``."""

    def __call__(
        self,
        run_dir: Path,
        enumerator_image: str,
        sake_image: str,
        *,
        mining_fee_rate: int = ...,
        coordination_fee_rate: float = ...,
        max_decomposition_fee: int = ...,
        mode: str = ...,
        timeout: int = ...,
        retry_timeout: int = ...,
        sake_seed: int = ...,
        ncpus: int = ...,
        mem: str = ...,
        scratch: str = ...,
        walltime: str = ...,
        dry_run: bool = ...,
    ) -> str | None: ...


# --- S3 PBS submissions ---------------------------------------------------


class SubmitCoinjoinAnalysisS3PBS(Protocol):
    """``pbs.submit_coinjoin_analysis_s3_pbs``."""

    def __call__(
        self,
        target: S3Target,
        image: str,
        command: str,
        *,
        ncpus: int = ...,
        mem: str = ...,
        scratch: str = ...,
        walltime: str = ...,
        dry_run: bool = ...,
    ) -> str | None: ...


class SubmitMappingsS3PBS(Protocol):
    """``pbs.submit_mappings_s3_pbs``."""

    def __call__(
        self,
        target: S3Target,
        enumerator_image: str,
        sake_image: str,
        *,
        mining_fee_rate: int = ...,
        coordination_fee_rate: float = ...,
        max_decomposition_fee: int = ...,
        mode: str = ...,
        timeout: int = ...,
        retry_timeout: int = ...,
        sake_seed: int = ...,
        ncpus: int = ...,
        mem: str = ...,
        scratch: str = ...,
        walltime: str = ...,
        dry_run: bool = ...,
        dependency_job_id: str | None = ...,
    ) -> str | None: ...


class SubmitBlockSciS3PBS(Protocol):
    """``pbs.submit_blocksci_s3_pbs`` (the combined BlockSci job)."""

    def __call__(
        self,
        target: S3Target,
        image: str,
        command: str,
        *,
        ncpus: int = ...,
        mem: str = ...,
        scratch: str = ...,
        walltime: str = ...,
        dry_run: bool = ...,
        dependency_job_id: str | None = ...,
        include_report: bool = ...,
        export_analysis: bool = ...,
    ) -> str | None: ...


class SubmitBlockSciParseS3PBS(Protocol):
    """``pbs.submit_blocksci_parse_s3_pbs``."""

    def __call__(
        self,
        target: S3Target,
        image: str,
        command: str,
        *,
        ncpus: int = ...,
        mem: str = ...,
        scratch: str = ...,
        walltime: str = ...,
        external_bitcoin_datadir: Path | None = ...,
        bitcoin_blocks_uri: str | None = ...,
        external_blocksci_dir: Path | None = ...,
        external_network: str | None = ...,
        external_max_block: int | None = ...,
        dry_run: bool = ...,
    ) -> str | None: ...


class SubmitBlockSciUpdateS3PBS(Protocol):
    """``pbs.submit_blocksci_update_s3_pbs``."""

    def __call__(
        self,
        target: S3Target,
        source_run_id: str,
        image: str,
        command: str,
        *,
        external_bitcoin_datadir: Path,
        external_network: str,
        external_max_block: int,
        ncpus: int = ...,
        mem: str = ...,
        scratch: str = ...,
        walltime: str = ...,
        dry_run: bool = ...,
    ) -> str | None: ...


class SubmitBlockSciAnalyzeS3PBS(Protocol):
    """``pbs.submit_blocksci_analyze_s3_pbs`` (detect, script, notebook, external)."""

    def __call__(
        self,
        target: S3Target,
        image: str,
        command: str,
        *,
        mode: str = ...,
        user_script: Path | None = ...,
        external_baseline_uri: str | None = ...,
        notebooks_dir: Path | None = ...,
        notebook_port: int = ...,
        ncpus: int = ...,
        mem: str = ...,
        scratch: str = ...,
        walltime: str = ...,
        dry_run: bool = ...,
        dependency_job_id: str | None = ...,
    ) -> str | None: ...


class SubmitUnifiedReportS3PBS(Protocol):
    """``pbs.submit_unified_report_s3_pbs``."""

    def __call__(
        self,
        target: S3Target,
        image: str,
        command: str,
        *,
        ncpus: int = ...,
        mem: str = ...,
        scratch: str = ...,
        walltime: str = ...,
        dry_run: bool = ...,
        dependency_job_ids: Sequence[str] = ...,
        include_mappings: bool = ...,
    ) -> str | None: ...


# --- Kubernetes -----------------------------------------------------------


class RenderS3EmulationResources(Protocol):
    """``kubernetes.render_s3_emulation_resources``."""

    def __call__(
        self,
        *,
        namespace: str,
        run_id: str,
        scenario_json: str,
        engine: str,
        image_prefix: str,
        emulator_image: str,
        uploader_image: str,
        artifact_uri: str,
        endpoint_url: str,
        secret_name: str,
        emulation_timeout_seconds: int = ...,
        scheduling_timeout_seconds: int = ...,
        reuse_namespace: bool = ...,
        distributor_startup_timeout: str | None = ...,
        btc_node_image: str | None = ...,
        kubernetes_image_pull_policy: str | None = ...,
        btc_node_initial_block_count: str | None = ...,
    ) -> str: ...
