# Core module refactor plan: `wrapper.py` and `pbs.py`

Status: proposed (2026-07-26). Scope: `coinjoin-pipeline` only.

## 1. Problem

Two modules under `pipeline/client/` carry most of the implementation complexity:

| File | Lines | Bytes |
| --- | --- | --- |
| `pipeline/client/wrapper.py` | 3537 | 145 KB |
| `pipeline/client/pbs.py` | 1832 | 68 KB |

Together they are 5369 of the 12 205 Python lines in `pipeline/`. The architecture
around them is already layered (`artifacts.py`, `kubernetes.py`, `research.py`,
`run_catalog.py`, `pipeline_logging.py` are separate), but these two absorbed
every new backend: shared-storage PBS, S3 PBS, the reusable/cached BlockSci
workflows, Kubernetes S3 emulation, and the parallel analysis graph.

Concretely, `wrapper.py` currently mixes eight unrelated concerns in one flat
namespace: process lifecycle, Compose environment construction, run-directory
discovery, Docker stage execution, Kubernetes emulation, PBS resource/image
resolution, the workflow graph, S3 orchestration with marker polling and
cancellation policy, and the whole argparse surface (~600 lines of it).

## 2. Constraints discovered before planning

These are load-bearing and shape the plan more than aesthetics do.

1. **`client/wrapper.py` must keep its path and stay executable.**
   `src/coinjoin_pipeline/commands.py:434` builds the child invocation as
   `runtime_root / "client" / "wrapper.py"`. The file stays; only its contents
   shrink to imports plus `main()`.
2. **Tests patch module internals, so patch targets move with the code.**
   `mock.patch("client.wrapper.<name>")` appears 51 times (27 in
   `test_s3_backend.py`, 24 in `test_wrapper.py`) and `client.pbs.<name>` 52
   times (36 in `test_pbs.py`, 16 in `test_s3_backend.py`), including
   `client.pbs.subprocess`, `client.pbs.shutil`, `client.pbs.time` and
   `client.pbs._qstat_job_state`. A patch only takes effect where the *caller*
   resolves the name, so every moved symbol whose caller also moves needs its
   patch target updated to the new owning module. This is mechanical but it is
   the single largest source of churn and the main breakage risk.
3. **Re-export surface must survive.** `wrapper.py:22` documents deliberate
   self-aliases ("Explicit self-aliases preserve wrapper's historical re-export
   surface"); tests import `compose_command`, `container_runtime`,
   `captured_pipeline_stage`, `kubernetes_auth_preflight` and others *through*
   `client.wrapper`. `client/wrapper.py` and `client/pbs/__init__.py` stay as
   facades that re-export the public names.
4. **Baseline to preserve:** `.venv/bin/python -m pytest tests/unit tests/pipeline`
   gives **398 passed, 1 failed**. The one failure is pre-existing and unrelated:
   `tests/unit/test_publish_workflow.py::test_test_workflow_no_longer_publishes_a_wrapper_image`
   asserts a `concurrency.group` string that `.github/workflows/tests.yaml` no
   longer uses. Do not fix it as part of this refactor; do not let the count of
   other failures rise above zero.
5. Lint gate is `pipeline/lint.py`: `ruff check . --fix`, then `mypy .`, then
   `pylint .`. Every step must leave all three clean.

## 3. Target layout

### 3.1 `wrapper.py` → 12 modules + facade

Following the requested split (s3 orchestration / Kubernetes staging / workflow
graph / marker polling / failure-cancellation policy), with the incidental
concerns that are currently tangled in also given homes:

```
pipeline/client/
├── wrapper.py              ~250  facade: re-exports + main() dispatch only
├── process_control.py      ~120  advisory lock, peer-container cleanup, signal
│                                 handlers, run_command
├── compose.py              ~330  compose_env, image provenance, container/host
│                                 scenario paths, run_script, initialize_images
├── run_layout.py           ~200  run-dir discovery, resolve_run_id,
│                                 blocksci_* paths, exists_or_unreadable,
│                                 stage_blocksci_script
├── docker_stages.py        ~250  coinjoin-analysis + BlockSci Compose stages,
│                                 export_command, export preflight, export-only
├── kubernetes_staging.py   ~380  local-driver emulation, emulator command,
│                                 btc-data volume, S3 emulation, run staging
├── pbs_settings.py         ~180  image resolution (locks, singularity scheme),
│                                 stage/report resource resolution, wait timeout
├── pbs_stages.py           ~200  the four shared-storage PBS stage runners
├── workflow.py             ~180  run_parallel_analysis, run_serial_analysis
├── s3_workflow.py          ~330  S3PBSJobs, run_pbs_from_s3, exporter staging
├── s3_markers.py           ~220  marker polling + failure/cancellation policy,
│                                 run_full_run_s3
├── cli_parser.py           ~450  every add_*_arguments helper, build_parser
└── cli_validation.py       ~280  validate_artifact_arguments, normalize_argv,
                                  positive_int / non_negative_int / run_timezone
```

Rationale for the two least obvious boundaries:

- **`s3_workflow.py` vs `s3_markers.py`.** Submission (building the job graph and
  its `depend=afterok` edges) is decided once, up front, and is pure; waiting on
  markers and cancelling dependents is a policy that runs over time and needs
  `qdel`. Splitting them separates "what to submit" from "what to do when a
  stage dies", which is exactly where the current code is hardest to read.
- **`cli_parser.py` vs `cli_validation.py`.** `build_parser` is consumed by
  metadata generation (`scripts/generate-command-metadata.py`,
  `tests/pipeline/test_cli_contract.py`), so keeping it free of the 190-line
  cross-flag validation block makes both readable and keeps the metadata
  contract obvious.

### 3.2 `pbs.py` → package `client/pbs/`

Requested split (resource validation / template rendering / stage definitions /
dependency graph / submission and result handling) maps to:

```
pipeline/client/pbs/
├── __init__.py         ~90  re-exports the public API (keeps `client.pbs.X`
│                            imports in wrapper and external callers working)
├── defaults.py         ~40  DEFAULT_* resources and images, poll interval,
│                            PBS state sets
├── validation.py      ~120  PBSError, walltime_to_seconds, SAFE_* regexes,
│                            require_* guards, external-Bitcoin validation
├── templates_local.py ~130  render_{blocksci,coinjoin_analysis,mappings}_pbs
├── templates_s3.py    ~520  the seven render_*_s3_pbs functions
├── commands.py        ~230  in-container command builders (*_pbs_command)
└── submission.py      ~330  qsub/qstat/qdel, job-id persistence, job probe,
                             wait_for_pbs_marker, submit_* for local and S3
```

`templates_s3.py` stays the largest module because the embedded shell fragments
are irreducible text, but §4 cuts roughly 120 lines of repetition out of it.

## 4. Duplication to remove

This is where the readability gain actually comes from — the split alone just
moves lines. Each item below is a real repeated construct found in the current
code, with the call sites counted.

### 4.1 In `wrapper.py`

| # | Duplication | Sites | Fix | Est. saved |
| --- | --- | --- | --- | --- |
| A | `compose_env(run_dir.name, args.engine, args.coinjoin_type, args.min_input_count, args.scenario, args.joinmarket_detector, args.joinmarket_min_base_fee, args.joinmarket_percentage_fee, args.joinmarket_max_depth)` — a 9-positional-argument call | 6 | `compose_env_from_args(args, run_id=None)` in `compose.py` | ~110 |
| B | `[*compose_command(env), "-f", str(COMPOSE_FILE), "-p", COMPOSE_PROJECT]` | 4 | `compose_base_command(env)` | ~20 |
| C | The seven detector arguments threaded into `blocksci_*_pbs_command(...)` | 4 | frozen `DetectionSettings` dataclass with `from_args`, consumed by the command builders | ~60 |
| D | Four `resolve_stage_pbs_resource(args, stage, name, DEFAULT_*)` calls in a row to fill `ncpus/mem/scratch/walltime` | 6 | `stage_pbs_resources(args, stage) -> PBSResources`, plus `unified_report_pbs_resources(args)` | ~90 |
| E | `wait_for_s3_marker(...)` wrapped in `except (ArtifactTransportError, PBSError):` that prints and `qdel`s a hard-coded dependent list | 5 | one `await_stage(stage, access, run_prefix, job_id, timeout, dependents)`, driven by a declared stage table | ~130 |
| F | Emulation stage boilerplate: snapshot `run_dirs`, `captured_pipeline_stage`, `detect_active_run`, then `relocate_to_host`/`_failed` + exit | 4 (emulate ×2, full-run ×2) | `run_emulation_stage(...)` returning the active run dir | ~70 |
| G | `if args.analysisPbs: <pbs stage> else: run_coinjoin_analysis(...)` in `run_serial_analysis` | 2 | single `run_baseline_stage(args, run_dir, logs_root)` | ~15 |
| H | `S3Access(endpoint_url=args.s3_endpoint_url, credentials_file=args.s3_credentials_file, profile=args.s3_profile)` | 5 | `s3_access_from_args(args)` | ~25 |
| I | `run_full_run_s3`'s dry-run branch prints a marker list that restates the live wait sequence, and drifts from it | 1 pair | derive both from the same stage table introduced in E | ~20 |
| J | **The `try: from client.pbs import (...) except ImportError: from pbs import (...)` block at `wrapper.py:117-212` lists 40+ names twice** | 1 pair | verify the fallback is dead (`client/__init__.py` exists and `parents[1]` is prepended to `sys.path`, so `client.pbs` always resolves), then delete the `except` arm | ~48 |

Item J is worth calling out: it is 96 lines of import statements for 48 symbols,
and the fallback arm appears to be unreachable in every supported invocation
(installed CLI, `runIt.sh` shim, direct `python3 client/wrapper.py`). Step 0 of
the sequencing below proves that before deleting it.

### 4.2 In `pbs.py`

| # | Duplication | Sites | Fix | Est. saved |
| --- | --- | --- | --- | --- |
| K | `shell_assignment("NAME", value).split("=", 1)[1]` — extracting the quoted value back out of an assignment | ~15 | `shell_value(value)` helper in `client/artifacts.py` next to `shell_assignment` | ~25 |
| L | `clear_markers="\n".join((render_s5cmd_rm('".../<stage>.done"') + " \|\| true", render_s5cmd_rm('".../<stage>.failed"') + " \|\| true"))` | 5 | `clear_stage_markers(stage)` | ~45 |
| M | `upload_failed=render_s5cmd_cp('"$FAILED_MARKER"', ...)` + `upload_done=render_s5cmd_cp('"$DONE_MARKER"', ...)` | 6 pairs | `stage_marker_uploads(stage) -> dict` merged into the `format(**...)` call | ~40 |
| N | The four local `submit_*_pbs` bodies: `require_storage_path` guards, `mkdir logs`, render, write `.pbs/<stage>.pbs`, dry-run print, `require_qsub`, `submit_pbs`, `persist_pbs_job_id`, print | 4 | `submit_local_script(run_dir, stage, script, dry_run)` doing everything after render | ~60 |
| O | `submit_pbs` and `submit_pbs_text` each build the same `-W depend=afterok:` argument with the same empty-ID check | 2 | shared `qsub_command(dependency_job_id)` | ~20 |
| P | External-Bitcoin validation (path under `/storage`, `blocks/` present, network in the 3-value set, non-negative max block) | 2 (`render_blocksci_parse_s3_pbs`, `render_blocksci_update_s3_pbs`) | `require_external_bitcoin(path, network, max_block)` in `validation.py` | ~35 |
| Q | Five S3 identity parameters (`artifact_uri`, `run_id`, `endpoint_url`, `credentials_file`, `profile`) threaded positionally through 7 `render_*_s3_pbs` + 7 `submit_*_s3_pbs` | 14 | **Deferred — see §6.** A frozen `S3Target` would collapse 70 parameters to 14, but it changes every render/submit signature and the tests' `COMMON = dict(...)` splat. Not in this pass. |

Estimated net effect: `wrapper.py`'s concerns land in modules of 120–450 lines,
`pbs.py`'s in 40–520, and roughly 600–700 lines of duplication disappear rather
than being relocated.

## 5. Sequencing

Each step ends with `pytest` green (398 passed, the 1 known failure) and
`pipeline/lint.py` clean, so any regression is bisectable to one step. No step
mixes a move with a behaviour change.

- **Step 0 — prove the dead import fallback (item J).** Confirm the `except
  ImportError` arm in `wrapper.py` is unreachable for the installed CLI, the
  `runIt.sh`/`run-pipeline-image.sh` shims, and direct script execution; delete
  it. Small, self-contained, and removes 48 duplicated names before anything
  moves.
- **Step 1 — pure extractions from `wrapper.py`, no dedupe yet.** Create
  `process_control.py`, `compose.py`, `run_layout.py`. These have the fewest
  inbound patch targets, so they validate the facade pattern cheaply.
- **Step 2 — `pbs.py` → `client/pbs/` package.** Move code verbatim into the
  six submodules, add the re-exporting `__init__.py`, then update the 52
  `client.pbs.*` patch targets in `test_pbs.py` and `test_s3_backend.py` to the
  owning submodule (e.g. `client.pbs.submission.subprocess`).
- **Step 3 — `pbs` dedupe (items K–P).** Now that the package is in place, apply
  the helpers. Template output must be byte-identical; the existing
  `bash -n` template tests and the `assert "..." in script` checks are the
  guard, and `tests/test-local-pbs-analysis.sh` is the end-to-end check.
- **Step 4 — remaining `wrapper.py` extractions.** `docker_stages.py`,
  `kubernetes_staging.py`, `pbs_settings.py`, `pbs_stages.py`, `workflow.py`,
  then `s3_workflow.py` + `s3_markers.py`, then `cli_parser.py` +
  `cli_validation.py`. Update the 51 `client.wrapper.*` patch targets as each
  group moves.
- **Step 5 — `wrapper.py` dedupe (items A–I).** Land the shared helpers and the
  declarative stage table that collapses the five marker-wait blocks and the
  dry-run print list.
- **Step 6 — verification beyond unit tests.** `./tests/test-runIt-overactive-local.sh`
  and `./tests/test-podman-no-host-docker.sh` (dry-run paths through the real
  CLI), `./tests/test-command-builder-contract.sh` (parser/metadata parity —
  this is the one that catches an accidental change to `build_parser`), and
  `./tests/test-local-pbs-analysis.sh` for a real PBS submission.

## 6. Explicitly out of scope

- **Item Q (`S3Target` bundle).** The largest remaining structural improvement in
  `pbs`, but it rewrites 14 public signatures and the tests' `COMMON` splat.
  Worth a follow-up pass once the module split has settled; doing both at once
  would make a regression hard to attribute.
- **Behaviour changes of any kind** — no new flags, no changed defaults, no
  altered marker or template text. This is a structural refactor; the generated
  PBS scripts and CLI surface must be identical before and after.
- The pre-existing `test_publish_workflow` failure (constraint 4).
- The `client/{wrapper,pbs}.py` copies under `deprecated/blocksciEmulatorAnalysis`
  and `build/lib/coinjoin_pipeline/_runtime/` — historical/generated copies, left
  untouched.

## 7. Risks

| Risk | Mitigation |
| --- | --- |
| A missed `mock.patch` target silently stops patching and a test passes against real behaviour (e.g. actually shelling out to `qsub`) | After each step, grep for `mock.patch("client\.` and confirm every target resolves to a module that really defines the name; `test_frontend_submit_does_not_invoke_s5cmd` and the `subprocess.run` assertions are the tripwires |
| Circular imports between the new `wrapper` modules (`compose` ↔ `run_layout` ↔ `pbs_stages`) | Enforce a one-way layering: `process_control` → `compose` → `run_layout` → stage modules → workflow/orchestration → CLI. `wrapper.py` imports everything; nothing imports `wrapper` |
| Template text drift during step 3 | Snapshot every `render_*` output to a temp file before the step and diff after; require byte-identical output |
| `build_parser` reordering changes generated command metadata | `tests/test-command-builder-contract.sh` in step 6; keep the `add_*` call order inside `build_parser` exactly as-is |
| Scope creep into `deprecated/` or the top-level meta-repo | Work only inside `coinjoin-pipeline/`, per the working rules |
