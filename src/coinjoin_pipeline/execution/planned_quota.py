"""The planned-quota annotation of the S3 emulation Job.

The emulator creates its wallet pods over minutes, so the namespace quota's ``used`` lags the
run's real share. A launcher that knows the run's pod sizes (the Scenario Lab, from its
src/domain/quota.ts) passes the run's peak as ``kubernetes.planned_quota``; the Job carries it, so a
queue in any Lab reading the namespace can reserve that space before the pods exist. The
pipeline does not model pod sizes and only checks the shape.
"""

from __future__ import annotations

import json
import re

PLANNED_QUOTA_ANNOTATION = "coinjoin.io/planned-quota"
PLANNED_QUOTA_RESOURCES = ("requests.cpu", "limits.cpu", "requests.memory", "limits.memory", "pods")
_QUANTITY = re.compile(r"(?:\d+(?:\.\d*)?|\.\d+)(?:m|k|M|G|T|Ki|Mi|Gi|Ti)?")


def planned_quota_annotation(text: str) -> str:
    """The annotation value of ``kubernetes.planned_quota``: compact JSON of known resources.

    Raises ValueError when the text is not a JSON object of ResourceQuota resources and
    non-negative Kubernetes quantities.
    """
    try:
        value = json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError(f"not JSON: {error.msg}") from error
    if not isinstance(value, dict) or not value:
        raise ValueError("expected a non-empty JSON object")
    unknown = sorted(set(value) - set(PLANNED_QUOTA_RESOURCES))
    if unknown:
        raise ValueError(f"unknown resources {', '.join(unknown)}; use {', '.join(PLANNED_QUOTA_RESOURCES)}")
    for resource, quantity in value.items():
        if not isinstance(quantity, str) or not _QUANTITY.fullmatch(quantity):
            raise ValueError(f'{resource} must be a Kubernetes quantity string such as "1.5" or "2Gi"')
    return json.dumps({key: value[key] for key in PLANNED_QUOTA_RESOURCES if key in value}, separators=(",", ":"))
