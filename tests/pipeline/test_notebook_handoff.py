"""Notebook settings handed to the compute node; no PBS, network or containers."""

from coinjoin_pipeline.configuration import BlockSciConfiguration
from coinjoin_pipeline.execution.pbs.analysis_modes import analysis_mode_settings


def test_notebook_directory_is_validated_on_compute_node_not_submitting_container():
    settings = analysis_mode_settings(
        "blocksci-notebook", BlockSciConfiguration(notebooks_dir="/storage/not-present-locally/notebooks")
    )
    assert settings["output"]["notebooks_dir"] == "/storage/not-present-locally/notebooks"
