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

## 3. Consolidate run-directory layout knowledge (`run_catalog.py` and friends)

**Status:** review findings only, nothing implemented. Scope: `coinjoin-pipeline`.
Origin: architecture review of `pipeline/client/run_catalog.py` (2026-09-03).

Baseline: the module itself is healthy — leaf module (stdlib only, no `wrapper`/`pbs`/
`kubernetes` imports), pure functions, `frozen` `RunState`, single consumer
(`research.py`). The items below are about the contract it encodes, not its style.

### 3.1 Run-directory layout is not a single source of truth (highest impact)

Constants are only half extracted: `MANIFEST_NAME`, `REPORT_DIR`, `BASELINE_FILE`,
`FALSE_CJTXS_FILE` exist, but `"coinjoin_emulator_data"`, `"blocksci_data/config.json"`,
`"coinjoin-mappings_data/coinjoin_mappings.json"` and `"unified_report.json"` are raw
literals repeated in three places inside the module (`is_run_dir`, `stage_state`,
`REPORT_INPUT_PATHS`) and again in `research.py`, `wrapper.py`,
`exporters/emulator_data.py`, `pbs/templates_s3.py` and the shell templates
(`unified_report_s3_template.sh`, `coinjoin_analysis_s3_template.sh`).

The run-directory layout is the pipeline's actual contract; when one copy drifts,
`runs list` reports `missing` for a stage that in fact completed.

**Fix:** one `pipeline/client/run_layout.py` owning every relative path and marker set;
`run_catalog`, `run_context`, `research` and the exporters read from it. Shell templates
should receive the paths from Python rather than restating them.

### 3.2 Two independent definitions of "what is a run dir"

`run_catalog.is_run_dir()` hardcodes its markers; `run_context.is_run_dir(path,
marker_files)` takes them as a parameter. Two answers to the same question that can
diverge silently between run-time discovery and `runs list`.

**Fix:** collapse onto the marker set from 3.1.

### 3.3 mtime-based staleness is factually fragile, not just inelegant

`report_is_stale()` compares `st_mtime` against `REPORT_INPUT_PATHS`. For
`coinjoin_emulator_data` that is a **directory** mtime, which only changes when entries
are added/removed directly in it — a rerun that overwrites nested files leaves the report
looking fresh. `git checkout`, `rsync`, S3 downloads and container copies also reset or
preserve mtimes arbitrarily.

Inconsistent with the rest of the module: inputs are verified by `sha256_file` in
`create_external_manifest`, but result validity is decided by filesystem metadata. For a
thesis whose main claim is reproducibility, staleness should be derived from hashes
recorded in the manifest.

**Fix:** record input hashes when the report is written; compare hashes, not mtimes.
Larger change than 3.1/3.2 but the one that affects defensibility of the results.

### 3.4 `stage_state` conflates presence and freshness

Every key means "exists" except `"report"`, which means "exists and is not stale" — so
`False` cannot distinguish missing from stale and callers must consult `report_status`
separately. `report_is_stale()` is also computed twice per run (once in `stage_state`,
once in `report_status`), duplicating `stat()` work and allowing the two answers to
straddle a concurrent write. `report_status` returns bare strings that
`docs/analysis-semantics.md` documents as an API.

**Fix:** return tri-state `missing|stale|present` per stage, compute staleness once, and
type the status as a `Literal`/enum.

### 3.5 One file, two incompatible manifest schemas

`research_manifest.json` has two shapes: emulator runs contain only
`{"host_launcher": {...}}` (written by `src/coinjoin_pipeline/runs.py:store_host_manifest`,
which merges under that key), external runs are flat with top-level
`schema_version`/`mode`/`run_id`/`inputs` (written by `create_external_manifest` +
`write_manifest`). Consequence: `mode_for_run()` never finds `"mode"` on emulator runs and
always falls back to sniffing `coinjoin_emulator_data/` — the declared schema does not
actually work there. Verified across all manifests in `coinjoin-runs/`.

`dict[str, object]` also forces every consumer to cast even though the schema is versioned.

**Fix:** have the launcher write the top-level `mode`/`schema_version` too (keeping
`host_launcher` as a sub-object), and parse the manifest into a `TypedDict`/dataclass once.

### 3.6 Minor

- `write_manifest()` raises an error mentioning the CLI flag `--resume` — the module knows
  about the UX layer above it.
- `write_manifest(overwrite=True)` is never called; dead parameter, and its `write_text`
  would drop the `host_launcher` block that `store_host_manifest` otherwise merges.
- `sha256_file()` is a generic utility living in a catalog module.

**Suggested order:** 3.1 → 3.2 → 3.4 (mechanical, low risk), then 3.5, then 3.3.
