"""The copy of a run's launch record (``research_manifest.json``) in its S3 prefix.

The record is written in the run directory of the frontend that drives the run. Its
copy keeps the command, the resolved configuration and the pipeline commit beside the
artifacts, so a run can still be traced once that directory is gone.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from exporters.artifact_paths import RUN_MANIFEST

from coinjoin_pipeline.configuration import PipelineConfiguration
from coinjoin_pipeline.runs import adopt_recorded_launches
from coinjoin_pipeline.storage.s3 import (
    ArtifactTransportError,
    S3Access,
    S3Target,
    run_manifest_uri,
    run_prefix_uri,
    s3_json,
    s3_prefix_has_objects,
    upload_s3_file,
)

#: The run has already ended, so its last upload must not hold the command for long.
FINAL_UPLOAD_TIMEOUT_SECONDS = 120

#: Records this process uploaded, by local path: the ones it uploads again when it ends.
_PUBLISHED: dict[Path, tuple[S3Access, str]] = {}


def publish_run_manifest(args: PipelineConfiguration, *, continued: bool = False) -> None:
    """Upload the run's launch record once its prefix is known to belong to this run.

    A new run calls this after its prefix was found empty and the exporters were staged.
    A continued run publishes only into a prefix that holds something, so a mistyped run
    ID leaves no prefix behind, and first takes over the launches the S3 copy records:
    a resume from another frontend would otherwise replace the original launch with its
    own. A caller that recorded no launch has nothing to publish.
    """
    target = S3Target.from_args(args)
    record = args.runs_path / target.run_id / RUN_MANIFEST
    if not record.is_file():
        return
    run_prefix = run_prefix_uri(target.artifact_uri, target.run_id)
    uri = run_manifest_uri(run_prefix)
    if continued:
        if not s3_prefix_has_objects(target.access, run_prefix):
            return
        try:
            recorded = s3_json(target.access, uri)
        except json.JSONDecodeError as error:
            print(f"[stage] warning: {uri} is unreadable and is replaced: {error}", file=sys.stderr)
            recorded = None
        if recorded:
            adopt_recorded_launches(record, recorded)
    upload_s3_file(target.access, record, uri)
    _PUBLISHED[record] = (target.access, uri)
    print(f"[stage] Launch record uploaded to {uri}")


def republish_run_manifests() -> None:
    """Upload the records this process published again, now that they hold its outcome.

    The outcome is decided by then, so a failed upload only warns: the copy in S3 keeps
    the state of the launch, and the local record stays complete.
    """
    while _PUBLISHED:
        record, (access, uri) = _PUBLISHED.popitem()
        try:
            upload_s3_file(access, record, uri, timeout=FINAL_UPLOAD_TIMEOUT_SECONDS)
        except ArtifactTransportError as error:
            print(f"[stage] warning: {uri} still records a running launch: {error}", file=sys.stderr)
