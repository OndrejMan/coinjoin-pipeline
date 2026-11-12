"""The manifest every parsed BlockSci index records.

Standard library only and Python 3.8 syntax, like every module a PBS job ships.
"""

from __future__ import annotations

from typing import Mapping

# Schema of the manifest every parsed index records.
MANIFEST_SCHEMA = "1.1"


def index_manifest(
    run_id: str,
    blocksci_image: str,
    source_kind: str,
    network: str,
    exported_max_block: int,
    extra: Mapping[str, object] | None = None,
) -> dict:
    """What a parse or update records about the index it produced.

    An update resumes only an index of the same image, source kind and network, from
    exported_max_block. ``extra`` holds what one kind of index adds: the archive of an
    S3 cache.
    """
    return {
        "schema_version": MANIFEST_SCHEMA,
        "run_id": run_id,
        "blocksci_image": blocksci_image,
        "source_kind": source_kind,
        "network": network,
        "exported_max_block": exported_max_block,
        **(extra or {}),
    }
