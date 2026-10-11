import io
import os
import subprocess
import threading
import unittest
from contextlib import redirect_stderr
from unittest import mock

from coinjoin_pipeline.execution.pbs import kerberos
from coinjoin_pipeline.execution.pbs.kerberos import (
    _renew_periodically,
    kerberos_ticket_renewal,
    renew_kerberos_ticket,
)
from coinjoin_pipeline.execution.pbs.validation import PBSError

RUN = "coinjoin_pipeline.execution.pbs.kerberos.subprocess.run"
WHICH = "coinjoin_pipeline.execution.pbs.kerberos.shutil.which"


def _result(returncode: int, stderr: str = "") -> mock.Mock:
    return mock.Mock(returncode=returncode, stdout="", stderr=stderr)


class RenewKerberosTicketTest(unittest.TestCase):
    def test_success_returns_none_and_never_prompts(self):
        with mock.patch(RUN, return_value=_result(0)) as run:
            self.assertIsNone(renew_kerberos_ticket())
        self.assertEqual(run.call_args.args[0], ["kinit", "-R"])
        self.assertIs(run.call_args.kwargs["stdin"], subprocess.DEVNULL)

    def test_failure_returns_kinit_error(self):
        error = "kinit: krb5_get_kdc_cred: KDC can't fulfill requested option"
        with mock.patch(RUN, return_value=_result(1, error + "\n")):
            self.assertEqual(renew_kerberos_ticket(), error)

    def test_timeout_is_reported_as_failure(self):
        with mock.patch(RUN, side_effect=subprocess.TimeoutExpired(["kinit", "-R"], 60)):
            self.assertIsNotNone(renew_kerberos_ticket())


class KerberosTicketRenewalTest(unittest.TestCase):
    def setUp(self):
        environment = mock.patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("PBS_CONTAINER_NAME", None)

    def test_hosts_without_kinit_skip_renewal(self):
        with mock.patch(WHICH, return_value=None), mock.patch(RUN) as run:
            with kerberos_ticket_renewal():
                pass
        run.assert_not_called()

    def test_local_pbs_container_skips_renewal_even_with_kinit(self):
        """The local container's qsub never authenticates, so a missing MetaCentrum ticket must not stop it."""
        os.environ["PBS_CONTAINER_NAME"] = "pbs-s3-itest-1"
        with mock.patch(WHICH, return_value="/usr/bin/kinit"), mock.patch(RUN) as run:
            with kerberos_ticket_renewal():
                pass
        run.assert_not_called()

    def test_unrenewable_ticket_fails_before_the_run_starts(self):
        """The frontend login ticket is not renewable; catch it before hours of emulation."""
        body = mock.Mock()
        with (
            mock.patch(WHICH, return_value="/usr/bin/kinit"),
            mock.patch(RUN, return_value=_result(1, "KDC can't fulfill requested option")),
            self.assertRaisesRegex(PBSError, "kinit -r 7d"),
        ):
            with kerberos_ticket_renewal():
                body()
        body.assert_not_called()

    def test_renews_periodically_until_the_run_ends(self):
        renewed = threading.Event()
        calls = []

        def fake_run(*_args, **_kwargs):
            calls.append(1)
            if len(calls) >= 3:
                renewed.set()
            return _result(0)

        with mock.patch(WHICH, return_value="/usr/bin/kinit"), mock.patch(RUN, side_effect=fake_run):
            with kerberos_ticket_renewal(interval_seconds=0.01):
                self.assertTrue(renewed.wait(5))
            count_at_exit = len(calls)
            threading.Event().wait(0.05)
        self.assertEqual(len(calls), count_at_exit)
        self.assertFalse(any(thread.name == "kerberos-renewal" for thread in threading.enumerate()))


class RenewPeriodicallyTest(unittest.TestCase):
    def test_warns_once_per_failure_streak(self):
        stop = mock.Mock()
        stop.wait.side_effect = [False, False, False, False, True]
        outcomes = ["expired", "expired", None, "expired"]
        stderr = io.StringIO()
        with mock.patch.object(kerberos, "renew_kerberos_ticket", side_effect=outcomes), redirect_stderr(stderr):
            _renew_periodically(stop, 3600)
        output = stderr.getvalue()
        self.assertEqual(output.count("WARNING: kinit -R failed (expired)"), 2)
        self.assertEqual(output.count("renewal recovered"), 1)


if __name__ == "__main__":
    unittest.main()
