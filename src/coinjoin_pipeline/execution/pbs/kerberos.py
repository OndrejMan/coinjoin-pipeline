"""Keep the submitting user's Kerberos ticket alive while PBS work is pending.

MetaCentrum's ``qsub``/``qstat``/``qdel`` authenticate with the frontend's
Kerberos ticket, which expires ten hours after ``kinit``. An S3 full run first
waits for the Kubernetes emulation and then for every PBS stage, so it easily
outlives the ticket. Renewal needs a renewable ticket (``kinit -r 7d
<login>@META``); the one the frontend login script obtains is not renewable.
Hosts without ``kinit`` (CI, k3d) and the local PBS container, whose client
shims run ``qsub`` inside the container without Kerberos, skip all of this.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager

from .validation import PBSError

KERBEROS_RENEW_INTERVAL_SECONDS = 3600
KINIT_TIMEOUT_SECONDS = 60


def uses_local_pbs_container() -> bool:
    """Whether qsub/qstat/qdel are the local PBS container shims (``pbs-env.sh`` sets the name)."""
    return bool(os.environ.get("PBS_CONTAINER_NAME"))


def renew_kerberos_ticket() -> str | None:
    """Run ``kinit -R`` without a password prompt; return the error on failure."""
    try:
        result = subprocess.run(
            ["kinit", "-R"],
            check=False,
            text=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=KINIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return str(error)
    if result.returncode != 0:
        return (result.stderr or result.stdout or "").strip() or f"exit {result.returncode}"
    return None


def _renew_periodically(stop: threading.Event, interval_seconds: float) -> None:
    """Renew every interval; warn once per run of consecutive failures."""
    failing = False
    while not stop.wait(interval_seconds):
        error = renew_kerberos_ticket()
        if error is None:
            if failing:
                print("[kerberos] Ticket renewal recovered.", file=sys.stderr)
            failing = False
        elif not failing:
            failing = True
            print(
                f"[kerberos] WARNING: kinit -R failed ({error}); PBS commands stop working "
                "when the current ticket expires. Run: kinit -r 7d <login>@META",
                file=sys.stderr,
            )


@contextmanager
def kerberos_ticket_renewal(interval_seconds: float = KERBEROS_RENEW_INTERVAL_SECONDS) -> Iterator[None]:
    """Fail fast without a renewable ticket, then keep renewing it in the background."""
    if shutil.which("kinit") is None or uses_local_pbs_container():
        yield
        return
    error = renew_kerberos_ticket()
    if error is not None:
        raise PBSError(
            f"Cannot renew the Kerberos ticket ({error}). PBS needs a valid renewable "
            "ticket for the whole run; run: kinit -r 7d <login>@META"
        )
    stop = threading.Event()
    renewer = threading.Thread(
        target=_renew_periodically,
        args=(stop, interval_seconds),
        name="kerberos-renewal",
        daemon=True,
    )
    renewer.start()
    try:
        yield
    finally:
        stop.set()
        renewer.join()
