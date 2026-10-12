"""Which S3 PBS submissions require a coinjoin-analysis baseline already in the bucket."""

from unittest import mock

import pytest
from config_support import configuration as Namespace

from coinjoin_pipeline.execution import s3_submission
from coinjoin_pipeline.storage.s3 import ArtifactTransportError


def _args(**overrides):
    values = dict(
        artifact_uri="s3://bucket/runs",
        run_id="run-1",
        s3_endpoint_url="https://s3.example",
        s3_credentials_file="/credentials",
        s3_profile="coinjoin",
        s3_secret_name="coinjoin-s3",
        namespace="coinjoin-ns",
        reuse_namespace=True,
        dry_run=False,
        analysisPbs=False,
        blocksciPbs=True,
        mappingsPbs=False,
    )
    values.update(overrides)
    return Namespace(**values)


@pytest.fixture
def submission():
    with (
        mock.patch.object(s3_submission, "s3_access_preflight"),
        mock.patch.object(s3_submission, "s3_object_exists", return_value=False) as exists,
        mock.patch.object(s3_submission, "ensure_staged_exporters"),
        mock.patch.object(s3_submission, "S3StageRunner") as runner,
    ):
        yield exists, runner


def test_resumed_detect_still_requires_the_stored_baseline(submission) -> None:
    exists, runner = submission
    with pytest.raises(ArtifactTransportError, match="requires an existing"):
        s3_submission._submit_s3_pbs_stages(_args(action="pbs-from-s3"), [])
    exists.assert_called_once()
    runner.assert_not_called()


def test_full_run_blocksci_without_analysis_reads_no_baseline(submission) -> None:
    exists, runner = submission
    s3_submission._submit_s3_pbs_stages(_args(), [])
    exists.assert_not_called()
    runner.return_value.submit_all.assert_called_once()
