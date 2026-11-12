"""Container image inspection shared by the host and standalone exporters."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class ImageProvenance:
    """Locally inspectable identity and the reason inspection failed, if any."""

    image_id: str | None = None
    repo_digest: str | None = None
    error: str | None = None


def inspect_image(image: str, runtime: str = "docker", *, timeout: float | None = None) -> ImageProvenance:
    """Read the image ID and first repository digest from Docker or Podman."""
    try:
        result = subprocess.run(
            [runtime, "image", "inspect", image, "--format", "{{.Id}}\n{{json .RepoDigests}}"],
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        return ImageProvenance(error=str(error))
    lines = result.stdout.splitlines()
    image_id = lines[0].strip() if lines else None
    repo_digest = None
    if len(lines) > 1:
        try:
            repo_digests = json.loads(lines[1])
        except json.JSONDecodeError:
            repo_digests = []
        if repo_digests:
            repo_digest = str(repo_digests[0])
    return ImageProvenance(image_id or None, repo_digest)
