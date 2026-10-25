"""Identity handling at the shared host/exporter boundary, without a container daemon."""

import subprocess
from unittest.mock import patch

import pytest

from exporters.common import docker_image_digest
from exporters.image_provenance import inspect_image
from exporters.integration_diagnostics import docker_image_provenance

from coinjoin_pipeline.execution.containers import inspect_image_provenance


@pytest.mark.parametrize(
    ("output", "image_id", "digest"),
    [
        ('sha256:local\n["image@sha256:first", "image@sha256:second"]\n', "sha256:local", "image@sha256:first"),
        ("sha256:local\n[]\n", "sha256:local", None),
        ("sha256:local\nnull\n", "sha256:local", None),
        ("sha256:local\nmalformed\n", "sha256:local", None),
        ("", None, None),
    ],
)
def test_host_diagnostics_and_manifest_share_the_image_identity(output, image_id, digest):
    with patch("exporters.image_provenance.subprocess.run", return_value=subprocess.CompletedProcess([], 0, output)):
        assert inspect_image_provenance("image", "podman") == (image_id, digest)
        assert docker_image_provenance("image", "podman") == {
            "reference": "image",
            "image_id": image_id,
            "repo_digest": digest,
            "inspect_error": None,
        }
        assert docker_image_digest("image") == (digest or image_id)


def test_runtime_and_host_timeout_reach_the_inspection_boundary():
    with patch("exporters.image_provenance.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "")) as run:
        inspect_image_provenance("image", "podman")
    assert run.call_args.args[0] == [
        "podman",
        "image",
        "inspect",
        "image",
        "--format",
        "{{.Id}}\n{{json .RepoDigests}}",
    ]
    assert run.call_args.kwargs["timeout"] == 5


@pytest.mark.parametrize(
    "error",
    [
        FileNotFoundError("no daemon"),
        subprocess.CalledProcessError(1, "docker"),
        subprocess.TimeoutExpired("docker", 5),
    ],
)
def test_unavailable_inspection_preserves_the_diagnostic_reason(error):
    with patch("exporters.image_provenance.subprocess.run", side_effect=error):
        provenance = inspect_image("image", timeout=5)
        assert provenance.image_id is provenance.repo_digest is None
        assert provenance.error == str(error)
        assert inspect_image_provenance("image", "docker") == (None, None)
        assert docker_image_provenance("image")["inspect_error"] == str(error)
        assert docker_image_digest("image") is None


def test_missing_reference_does_not_call_the_daemon():
    with patch("exporters.image_provenance.subprocess.run") as run:
        assert docker_image_provenance(None)["inspect_error"] == "image reference missing"
        assert docker_image_digest(None) is None
    run.assert_not_called()
