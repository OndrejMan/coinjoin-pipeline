"""Pin registry image tags to the digest they name right now, without a container daemon.

Kubernetes and Singularity resolve a tag when the pod or job starts, which can be
hours after submission, and the MetaCentrum frontend and PBS nodes have no Docker
to inspect afterwards. Pinning at submission makes every stage run the image the
run records.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from functools import lru_cache

from coinjoin_pipeline.images import IMAGE_RE

DOCKER_SCHEME = "docker://"
DOCKER_HUB = "docker.io"
DOCKER_HUB_API = "registry-1.docker.io"
MANIFEST_MEDIA_TYPES = (
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
)
REQUEST_TIMEOUT_SECONDS = 15
CHALLENGE_PARAM_RE = re.compile(r'(\w+)="([^"]*)"')


class RegistryError(RuntimeError):
    """The registry did not name a digest for the reference."""


def split_reference(reference: str) -> tuple[str, str, str, str] | None:
    """Return (name, registry API host, repository, tag) of a tagged registry reference.

    None for anything Singularity or Kubernetes would not pull by tag from a
    registry: other transports, files, and references that already carry a digest.
    """
    name = reference.removeprefix(DOCKER_SCHEME)
    if "@" in name or not IMAGE_RE.fullmatch(name):
        return None
    if ":" in name.rsplit("/", 1)[-1]:
        name, tag = name.rsplit(":", 1)
    else:
        tag = "latest"
    first, _, rest = name.partition("/")
    if rest and ("." in first or ":" in first or first == "localhost"):
        registry, repository = first, rest
    else:
        registry, repository = DOCKER_HUB, name
    if registry == DOCKER_HUB:
        registry = DOCKER_HUB_API
        if "/" not in repository:
            repository = f"library/{repository}"
    return name, registry, repository, tag


def _bearer_token(challenge: str, timeout: float) -> str:
    scheme, _, params = challenge.partition(" ")
    if scheme.lower() != "bearer":
        raise RegistryError(f"unsupported registry authentication: {challenge}")
    values = dict(CHALLENGE_PARAM_RE.findall(params))
    realm = values.pop("realm", None)
    if not realm:
        raise RegistryError(f"registry authentication challenge has no realm: {challenge}")
    with urllib.request.urlopen(f"{realm}?{urllib.parse.urlencode(values)}", timeout=timeout) as response:
        body = json.load(response)
    token = body.get("token") or body.get("access_token")
    if not token:
        raise RegistryError(f"registry {realm} returned no anonymous pull token")
    return str(token)


def registry_digest(reference: str, timeout: float = REQUEST_TIMEOUT_SECONDS) -> str:
    """Ask the registry which manifest digest the reference's tag points to (anonymous pull)."""
    parts = split_reference(reference)
    if parts is None:
        raise RegistryError(f"not a tagged registry reference: {reference}")
    _, registry, repository, tag = parts
    url = f"https://{registry}/v2/{repository}/manifests/{tag}"
    headers = {"Accept": ", ".join(MANIFEST_MEDIA_TYPES)}
    try:
        try:
            response = urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout)
        except urllib.error.HTTPError as error:
            challenge = error.headers.get("WWW-Authenticate") if error.code == 401 else None
            if not challenge:
                raise
            headers["Authorization"] = f"Bearer {_bearer_token(challenge, timeout)}"
            response = urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout)
        with response:
            body = response.read()
            digest = response.headers.get("Docker-Content-Digest")
    except (OSError, ValueError) as error:
        raise RegistryError(f"{url}: {error}") from error
    if not digest:
        digest = f"sha256:{hashlib.sha256(body).hexdigest()}"
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise RegistryError(f"registry returned an unexpected digest for {reference}: {digest}")
    return digest


@lru_cache(maxsize=None)
def pinned_reference(reference: str) -> str:
    """The reference with its tag replaced by the current digest; unchanged when that is impossible.

    One process resolves each tag once, so every stage it submits runs the same
    image even if the tag moves meanwhile. A failure only warns: the run still
    starts, and the unified report flags the missing digest.
    """
    parts = split_reference(reference)
    if parts is None:
        return reference
    try:
        digest = registry_digest(reference)
    except RegistryError as error:
        print(f"[images] warning: running {reference} unpinned, digest lookup failed: {error}", file=sys.stderr)
        return reference
    scheme = DOCKER_SCHEME if reference.startswith(DOCKER_SCHEME) else ""
    # Without the tag: Singularity's docker transport rejects a tag and a digest together.
    return f"{scheme}{parts[0]}@{digest}"
