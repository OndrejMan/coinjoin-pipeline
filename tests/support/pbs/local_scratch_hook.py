import os
import pwd
import shutil

import pbs


event = pbs.event()  # type: ignore[attr-defined]
job = event.job
scratch_dir = f"/scratch/{job.id.replace('/', '_')}"

if event.type == pbs.EXECJOB_LAUNCH:  # type: ignore[attr-defined]
    account = pwd.getpwnam(job.euser)
    os.makedirs(scratch_dir, mode=0o700, exist_ok=True)
    os.chown(scratch_dir, account.pw_uid, account.pw_gid)
    event.env["SCRATCHDIR"] = scratch_dir
elif event.type == pbs.EXECJOB_END:  # type: ignore[attr-defined]
    shutil.rmtree(scratch_dir, ignore_errors=True)
