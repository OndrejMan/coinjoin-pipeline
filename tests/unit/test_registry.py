import io
import urllib.error
from email.message import Message

import pytest

from coinjoin_pipeline import registry
from coinjoin_pipeline.configuration import PipelineConfiguration
from coinjoin_pipeline.execution.pbs.commands import analysis_image_arguments
from coinjoin_pipeline.execution.pbs.defaults import DEFAULT_RESOURCES
from coinjoin_pipeline.execution.pbs.templates_s3 import render_blocksci_parse_s3_pbs
from coinjoin_pipeline.execution.s3_emulation import pin_emulation_images
from coinjoin_pipeline.storage.s3 import S3Target

# The conftest fixture replaces the module attribute; this keeps the real lookup.
REAL_REGISTRY_DIGEST = registry.registry_digest
DIGEST = "sha256:" + "d" * 64


@pytest.fixture
def registry_digests(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    looked_up: list[str] = []

    def digest(reference: str, timeout: float = 0) -> str:
        looked_up.append(reference)
        return DIGEST

    monkeypatch.setattr(registry, "registry_digest", digest)
    registry.pinned_reference.cache_clear()
    return looked_up


@pytest.mark.parametrize(
    ("reference", "expected"),
    [
        (
            "docker://ghcr.io/ondrejman/blocksci-complete:latest",
            ("ghcr.io/ondrejman/blocksci-complete", "ghcr.io", "ondrejman/blocksci-complete", "latest"),
        ),
        ("ghcr.io/ondrejman/emulator-manager", ("ghcr.io/ondrejman/emulator-manager", "ghcr.io", "ondrejman/emulator-manager", "latest")),
        ("python:3.12-slim-bookworm", ("python", "registry-1.docker.io", "library/python", "3.12-slim-bookworm")),
        ("pgsty/minio:v1", ("pgsty/minio", "registry-1.docker.io", "pgsty/minio", "v1")),
        ("localhost:5000/repo:t", ("localhost:5000/repo", "localhost:5000", "repo", "t")),
        ("ghcr.io/x/y@" + DIGEST, None),
        ("docker-archive:/tmp/image.tar", None),
        ("/storage/images/blocksci.sif", None),
        ("oras://ghcr.io/x/y:1", None),
    ],
)
def test_split_reference(reference, expected) -> None:
    assert registry.split_reference(reference) == expected


def test_registry_digest_answers_the_bearer_challenge(monkeypatch: pytest.MonkeyPatch) -> None:
    requests = []

    class Response(io.BytesIO):
        def __init__(self, body: bytes, headers: dict[str, str]) -> None:
            super().__init__(body)
            self.headers = Message()
            for name, value in headers.items():
                self.headers[name] = value

    def urlopen(request, timeout):
        url = request if isinstance(request, str) else request.full_url
        requests.append((url, None if isinstance(request, str) else request.get_header("Authorization")))
        if url.startswith("https://ghcr.io/token"):
            return Response(b'{"token": "anonymous"}', {})
        if len(requests) == 1:
            headers = Message()
            headers["WWW-Authenticate"] = 'Bearer realm="https://ghcr.io/token",service="ghcr.io",scope="repository:x/y:pull"'
            raise urllib.error.HTTPError(url, 401, "Unauthorized", headers, None)
        return Response(b"{}", {"Docker-Content-Digest": DIGEST})

    monkeypatch.setattr(registry.urllib.request, "urlopen", urlopen)

    assert REAL_REGISTRY_DIGEST("docker://ghcr.io/x/y:v1") == DIGEST
    assert requests == [
        ("https://ghcr.io/v2/x/y/manifests/v1", None),
        ("https://ghcr.io/token?service=ghcr.io&scope=repository%3Ax%2Fy%3Apull", None),
        ("https://ghcr.io/v2/x/y/manifests/v1", "Bearer anonymous"),
    ]


def test_pinned_reference_drops_the_tag_and_keeps_the_scheme(registry_digests) -> None:
    assert registry.pinned_reference("docker://ghcr.io/x/y:latest") == f"docker://ghcr.io/x/y@{DIGEST}"
    assert registry.pinned_reference("python:3.12") == f"python@{DIGEST}"
    assert registry.pinned_reference("docker://ghcr.io/x/y:latest") == f"docker://ghcr.io/x/y@{DIGEST}"
    assert registry_digests == ["docker://ghcr.io/x/y:latest", "python:3.12"]


def test_unresolvable_reference_runs_unpinned_with_a_warning(capsys) -> None:
    assert registry.pinned_reference("ghcr.io/x/y:latest") == "ghcr.io/x/y:latest"
    assert "running ghcr.io/x/y:latest unpinned" in capsys.readouterr().err


def test_pbs_job_runs_the_digest_but_keeps_the_index_identity(registry_digests) -> None:
    script = render_blocksci_parse_s3_pbs(
        target=S3Target(
            artifact_uri="s3://bucket/runs",
            run_id="run-1",
            endpoint_url="https://s3.cl4.du.cesnet.cz",
            credentials_file="/storage/user/.aws/credentials",
            profile="coinjoin",
        ),
        image="docker://ghcr.io/x/blocksci:latest",
        command="parse",
        resources=DEFAULT_RESOURCES["blocksci"],
    )

    assert f"IMAGE=docker://ghcr.io/x/blocksci@{DIGEST}\n" in script
    assert "IMAGE_NAME=docker://ghcr.io/x/blocksci:latest\n" in script
    assert '"$RUN_ID" "$IMAGE_NAME"' in script


def test_analysis_records_the_pinned_images(registry_digests) -> None:
    config = pin_emulation_images(
        PipelineConfiguration.from_flat(
            {
                "pbs_blocksci_image": "ghcr.io/x/blocksci:latest",
                "pbs_coinjoin_analysis_image": "ghcr.io/x/analysis:latest",
                "emulator_image": "ghcr.io/x/emulator:latest",
                "uploader_image": "ghcr.io/x/uploader:latest",
            }
        )
    )

    arguments = analysis_image_arguments(config)

    assert arguments == [
        "--blocksci-image",
        f"docker://ghcr.io/x/blocksci@{DIGEST}",
        "--coinjoin-analysis-image",
        f"docker://ghcr.io/x/analysis@{DIGEST}",
        "--coinjoin-emulator-image",
        f"ghcr.io/x/emulator@{DIGEST}",
        "--uploader-image",
        f"ghcr.io/x/uploader@{DIGEST}",
    ]
