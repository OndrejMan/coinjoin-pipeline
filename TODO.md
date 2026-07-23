# TODO

## 1. Fix committed dind healthcheck failure in compose (docker-overactive-local job)

**Status:** fix present in working tree (`pipeline/compose.yaml`), verified locally, needs commit + push.

**Symptom:** `docker-overactive-local` CI job fails at "Docker emulation" with
`dependency failed to start: container isolated_docker_daemon exited (143)` → `runIt.sh exited with code 5`.

**Root cause:** `docker:29-dind` intentionally delays binding the plain-TCP API on
2375 (~17s) when started without an explicit `--tls=false`. The committed healthcheck
(`interval: 2s`, `retries: 10`, no `start_period`) exhausts its retries before the daemon
is up, so Compose marks dind unhealthy, aborts `up`, and SIGTERMs it (143 = 128+15).
Appeared with no related code change because the dind service uses `pull_policy: always`
on the floating `docker:29-dind` tag — a newly published 29.x image changed the behavior.

**Fix (already staged in working tree):**
- `command: ["--tls=false"]` on the `dind` service — documented explicit opt-out; daemon
  binds 2375 in ~1.5s instead of ~17s.
- `start_period: 20s` on the healthcheck — headroom for future slow starts.

Verified: `docker compose --profile emulate up -d --wait dind` reaches `healthy`, no
slowdown warning in logs.

**Follow-up:** pin `docker:29-dind` to a digest (or specific minor) so `pull_policy: always`
on a floating tag can't reintroduce this class of surprise. Note docker 29 still logs
"in future versions this will be a hard failure" about the unauthenticated API even with
`--tls=false`.

## 2. Fix run-directory race between pipeline CLI and emulator (kubernetes-pbs-wasabi job)

**Status:** root cause confirmed, not yet fixed. Spans two subrepos.

**Symptom:** `kubernetes-pbs-wasabi` job fails with
`RuntimeError: Run log directory already exists: ./logs/<ts>_overactive-local`.

**Root cause:** two independent components compute the **same** run-directory name
(`%Y-%m-%d_%H-%M_<scenario>`, Europe/Prague) in the shared `EMULATION_LOGS_DIR`:
1. Host CLI (`src/coinjoin_pipeline/cli.py`) writes a pre-run `research_manifest.json` via
   `atomic_write`, which `mkdir(parents=True)`s the run dir **before** launch.
2. Emulator (`coinjoin-emulator/manager/engine/engine_base.py`
   `ensure_log_run_path_available()`) later hard-fails if that dir already exists. The
   direct-kubeconfig path in `pipeline/client/wrapper.py:kubernetes_emulator_command()`
   does **not** pass `--run-id` (unlike the S3 / in-cluster path, which passes
   `--run-id "$RUN_ID"`).

Latent flake, **not** a regression: only fires when k8s setup completes within the same
wall-clock minute (fast/warm CI). Confirmed by leftover CI storage: a Jul-6 run where setup
took ~15 min left an orphan dir containing only `research_manifest.json` (`..._17-31_...`)
separate from the emulator's data dir (`..._17-46_...`).

**Fix:**
- Compute the run id once in `cli.py` (`run_id_for()` already exists), export it (e.g.
  `ACTIVE_RUN_ID`) through `launcher.sh` → `wrapper.py` → pass `manager.py --run-id` on the
  direct-kubeconfig path.
- In `coinjoin-emulator`, when `--run-id` is explicitly given, make
  `ensure_log_run_path_available` accept a pre-existing dir that contains no emulator
  artifacts (host manifest present is expected, not an error).

Bonus: silences the pervasive `"ACTIVE_RUN_ID" variable is not set` compose warnings and
unifies the host manifest + emulation evidence into a single run directory.

**Caveat:** coordinate with emulator commit `3a05b42` ("Allow no-log reruns with existing
log directory"), which touched the same check; rebuild/publish `coinjoin-emulator:latest`
so CI picks up the change.
