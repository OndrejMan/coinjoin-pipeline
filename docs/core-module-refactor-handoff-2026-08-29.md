# Core-module refactor: local handoff (2026-08-29)

> Local implementation note. It accompanies uncommitted source changes and
> must not be treated as a specification of a behavior change.

## Goal and non-goal

The goal is to reduce *coordination complexity* without changing the public
CLI, PBS templates, artifact layout, marker names, or execution semantics.
The target architecture is one workflow graph with Local, Kubernetes, and PBS
as runner implementations.  The existing host layer remains thin; it is not a
second orchestration plane.

S3 is the transport contract for cross-environment runs (Kubernetes to PBS).
It is **not** yet authorization to delete the shared-storage path: local and
legacy PBS workflows still use it and must remain supported until a separate
product decision and migration are made.

The load-bearing compatibility constraints are in
[`core-module-refactor-plan.md`](core-module-refactor-plan.md):

- `pipeline/client/wrapper.py` must remain executable at this exact path.
- `client.wrapper.*` and `client.pbs.*` stay a stable re-export/mocking
  surface while code is extracted.
- This pass changes structure only.  Do not alter flags, defaults, marker
  paths/text, PBS-script output, cancellation policy, or report semantics.
- Do not touch `deprecated/` or generated runtime copies.

## Current working-tree state (uncommitted)

Modified files:

```text
pipeline/client/artifacts.py
pipeline/client/pbs.py
pipeline/client/wrapper.py
tests/pipeline/test_wrapper.py
```

New files:

```text
pipeline/client/stages.py
pipeline/client/stage_executor.py
pipeline/client/s3_markers.py
pipeline/client/s3_staging.py
pipeline/client/s3_submission.py
pipeline/client/s3_workflow.py
pipeline/client/workflow.py
pipeline/client/pbs_settings.py
tests/pipeline/test_stages.py
tests/pipeline/test_stage_executor.py
tests/pipeline/test_s3_markers.py
```

No commit has been created and no historical/generated artifacts were changed.

## Changes already implemented

### 1. A declarative analysis graph

`pipeline/client/stages.py` introduces small, dependency-only data structures:

- `StagePlan` — stage name, runner name, dependencies, and whether it creates
  the final report;
- `AnalysisPlan`;
- `analysis_plan(...)` for serial/parallel shared-storage analysis;
- `s3_full_run_plan(...)` for the S3 full-run graph, including the reusable
  BlockSci parse → analyze path.

The graph deliberately describes only dependencies.  It does not know Docker,
PBS, Kubernetes, shell quoting, or artifact transport.

### 2. A runner-neutral executor

`pipeline/client/stage_executor.py` contains:

- `StageRunner` protocol;
- `StageSubmission` with `wait` and optional `cancel` hooks;
- serial dependency execution;
- parallel baseline/BlockSci execution with failure cancellation.

The executor intentionally leaves unified-report submission to the current
caller.  This preserves the existing difference between local/shared-storage
report behavior and the S3 PBS dependency graph.

### 3. `wrapper.py` adapts existing behavior to the graph

`SharedStorageStageRunner` maps graph stage names back to the existing local
and PBS helper functions.  `run_serial_analysis` and
`run_parallel_analysis` now create a plan and use the executor.

Important compatibility detail: serial local analysis still retains the
historical `analysis.sh` dispatch.  Do not replace it with the parallel Docker
stage just because the graph makes that look simpler; that would be a behavior
change.

### 4. Small, behavior-preserving deduplication

The wrapper now owns these argument-to-value helpers:

- `compose_env_from_args(...)` and `compose_base_command(...)`;
- `s3_access_from_args(...)`;
- `stage_pbs_resources(...)`.

`wait_for_s3_pbs_stage(...)` removes repeated marker-wait setup while each
call site keeps its existing cancellation/error policy.  The S3 full-run
dry-run list is derived from `s3_full_run_plan(...)`.

In `pbs.py`:

- `artifacts.shell_value(...)` replaces extracting the right-hand side from
  `shell_assignment(...).split("=", 1)[1]`;
- `qsub_command(...)` centralizes `depend=afterok` construction for file and
  stdin submissions.

## Checks already run

All are short, non-container checks run from `coinjoin-pipeline/`:

```text
uv --cache-dir /tmp/cjp-uv-cache run --locked --extra test pytest \
  tests/pipeline/test_stages.py tests/pipeline/test_stage_executor.py \
  tests/pipeline/test_wrapper.py tests/pipeline/test_pbs.py \
  tests/pipeline/test_s3_backend.py -q
# 231 passed in 5.69s

uv --cache-dir /tmp/cjp-uv-cache run --locked --group dev ruff check \
  pipeline/client/artifacts.py pipeline/client/pbs.py \
  pipeline/client/stages.py pipeline/client/stage_executor.py \
  pipeline/client/wrapper.py tests/pipeline/test_stages.py \
  tests/pipeline/test_stage_executor.py tests/pipeline/test_wrapper.py
# All checks passed

uv --cache-dir /tmp/cjp-uv-cache run --locked --group dev mypy \
  pipeline/client/artifacts.py pipeline/client/pbs.py \
  pipeline/client/stages.py pipeline/client/stage_executor.py \
  pipeline/client/wrapper.py
# Success: no issues found in 5 source files

git diff --check
# clean
```

`pylint` was not installed in the available development environment, so it was
not run.  No long/integration test, image build, Kubernetes run, PBS job, or
emulator/BlockSci parse was run.

## Local wrapper validation (completed)

The user ran the required real CLI-to-bare-wrapper validation on 2026-08-29:

```text
./tests/test-runIt-overactive-local.sh
PASS: the host CLI renders a bare, checkout-backed wrapper invocation.
```

The shared-storage adapter is therefore no longer blocked on this milestone.

## Next implementation steps

Do these in order; stop after each focused change for the relevant fast tests.

1. **Preserve the completed compatibility gates.** The user-run bare-wrapper
   test, focused Python suite, Ruff/mypy, and command-builder contract are
   green.  Rerun the relevant subset after every edit; preserve the
   serial-local `analysis.sh` path.
2. **Finish S3 submission extraction by responsibility.** `s3_markers.py`
   and the high-level wait/cancellation orchestration in `s3_workflow.py` are
   complete.  The remaining `run_pbs_from_s3` / `_run_pbs_from_s3` portion
   still owns locking, stale-job resolution, marker clearing, exporter staging,
   and concrete PBS graph submission.  Extract that as a separate submission
   adapter only with wrapper-injected operations; retain the per-run lock and
   rollback semantics.
3. **Keep the shared-storage runner boundary stable.** `workflow.py` is
   complete for this pass.  Do not make it import `wrapper.py`, and do not
   replace the serial `analysis.sh` path with the parallel Docker helper.
4. **Extract the remaining pure helpers only when they remove a clear local
   duplication.** `pbs_settings.py` is complete.  Candidates are `compose.py`
   and `run_layout.py`; move test patch targets together with their callers.
5. **Only then consider splitting `pbs.py`.** The package split has a high
   mocking churn cost (many patches target `client.pbs`).  Snapshot rendered
   scripts before and after; byte-identical output is the acceptance rule.
6. **Run integration validations at milestones:** the local wrapper flow and
   command-builder contract are complete.  Before touching a PBS/S3 template
   or execution policy, run the appropriate local PBS/S3 integration path;
   never run full emulator, parse, Kubernetes, PBS, or image-build operations
   from the agent.

## Invariants for the next chat

- Keep the run ID, artifact directory names, S3 layout, and `.pbs` marker
  protocol unchanged.
- Keep `PBS_TERMINAL_STATES`, report dependencies, reusable/cached BlockSci
  semantics, and image/provenance behavior unchanged.
- Do not simplify the workflow to a naive `for stage: runner.run(stage)`:
  the graph has dependencies, parallel branches, report joins, and different
  cancellation rules.# Core-module refactor: local handoff (2026-08-29)

> Local implementation note. It accompanies uncommitted source changes and
> must not be treated as a specification of a behavior change.

## Goal and non-goal

The goal is to reduce *coordination complexity* without changing the public
CLI, PBS templates, artifact layout, marker names, or execution semantics.
The target architecture is one workflow graph with Local, Kubernetes, and PBS
as runner implementations.  The existing host layer remains thin; it is not a
second orchestration plane.

S3 is the transport contract for cross-environment runs (Kubernetes to PBS).
It is **not** yet authorization to delete the shared-storage path: local and
legacy PBS workflows still use it and must remain supported until a separate
product decision and migration are made.

The load-bearing compatibility constraints are in
[`core-module-refactor-plan.md`](core-module-refactor-plan.md):

- `pipeline/client/wrapper.py` must remain executable at this exact path.
- `client.wrapper.*` and `client.pbs.*` stay a stable re-export/mocking
  surface while code is extracted.
- This pass changes structure only.  Do not alter flags, defaults, marker
  paths/text, PBS-script output, cancellation policy, or report semantics.
- Do not touch `deprecated/` or generated runtime copies.

## Current working-tree state (uncommitted)

Modified files:

```text
pipeline/client/artifacts.py
pipeline/client/pbs.py
pipeline/client/wrapper.py
tests/pipeline/test_wrapper.py
```

New files:

```text
pipeline/client/stages.py
pipeline/client/stage_executor.py
tests/pipeline/test_stages.py
tests/pipeline/test_stage_executor.py
```

No commit has been created and no historical/generated artifacts were changed.

## Changes already implemented

### 1. A declarative analysis graph

`pipeline/client/stages.py` introduces small, dependency-only data structures:

- `StagePlan` — stage name, runner name, dependencies, and whether it creates
  the final report;
- `AnalysisPlan`;
- `analysis_plan(...)` for serial/parallel shared-storage analysis;
- `s3_full_run_plan(...)` for the S3 full-run graph, including the reusable
  BlockSci parse → analyze path.

The graph deliberately describes only dependencies.  It does not know Docker,
PBS, Kubernetes, shell quoting, or artifact transport.

### 2. A runner-neutral executor

`pipeline/client/stage_executor.py` contains:

- `StageRunner` protocol;
- `StageSubmission` with `wait` and optional `cancel` hooks;
- serial dependency execution;
- parallel baseline/BlockSci execution with failure cancellation.

The executor intentionally leaves unified-report submission to the current
caller.  This preserves the existing difference between local/shared-storage
report behavior and the S3 PBS dependency graph.

### 3. `wrapper.py` adapts existing behavior to the graph

`SharedStorageStageRunner` maps graph stage names back to the existing local
and PBS helper functions.  `run_serial_analysis` and
`run_parallel_analysis` now create a plan and use the executor.

Important compatibility detail: serial local analysis still retains the
historical `analysis.sh` dispatch.  Do not replace it with the parallel Docker
stage just because the graph makes that look simpler; that would be a behavior
change.

### 4. Small, behavior-preserving deduplication

The wrapper now owns these argument-to-value helpers:

- `compose_env_from_args(...)` and `compose_base_command(...)`;
- `s3_access_from_args(...)`;
- `stage_pbs_resources(...)`.

`wait_for_s3_pbs_stage(...)` removes repeated marker-wait setup while each
call site keeps its existing cancellation/error policy.  The S3 full-run
dry-run list is derived from `s3_full_run_plan(...)`.

In `pbs.py`:

- `artifacts.shell_value(...)` replaces extracting the right-hand side from
  `shell_assignment(...).split("=", 1)[1]`;
- `qsub_command(...)` centralizes `depend=afterok` construction for file and
  stdin submissions.

## Checks already run

All are short, non-container checks run from `coinjoin-pipeline/`:

```text
uv --cache-dir /tmp/cjp-uv-cache run --locked --extra test pytest \
  tests/pipeline/test_stages.py tests/pipeline/test_stage_executor.py \
  tests/pipeline/test_wrapper.py tests/pipeline/test_pbs.py \
  tests/pipeline/test_s3_backend.py -q
# 231 passed in 5.69s

uv --cache-dir /tmp/cjp-uv-cache run --locked --group dev ruff check \
  pipeline/client/artifacts.py pipeline/client/pbs.py \
  pipeline/client/stages.py pipeline/client/stage_executor.py \
  pipeline/client/wrapper.py tests/pipeline/test_stages.py \
  tests/pipeline/test_stage_executor.py tests/pipeline/test_wrapper.py
# All checks passed

uv --cache-dir /tmp/cjp-uv-cache run --locked --group dev mypy \
  pipeline/client/artifacts.py pipeline/client/pbs.py \
  pipeline/client/stages.py pipeline/client/stage_executor.py \
  pipeline/client/wrapper.py
# Success: no issues found in 5 source files

git diff --check
# clean
```

`pylint` was not installed in the available development environment, so it was
not run.  No long/integration test, image build, Kubernetes run, PBS job, or
emulator/BlockSci parse was run.

## Required user-run validation before a larger extraction

Run this manually and paste the result into the next chat:

```bash
cd /home/administrator/diplomka/coinjoin-pipeline
./tests/test-runIt-overactive-local.sh
```

It exercises the real CLI-to-bare-wrapper route.  Do not have the coding
agent run it: it is an end-to-end operation and repository instructions keep
those runs with the user.

## Next implementation steps

Do these in order; stop after each focused change for the relevant fast tests.

1. **Inspect the user-run result.** If it fails, diagnose the changed graph
   adapter first.  Preserve the old serial-local `analysis.sh` path.
2. **Extract S3 lifecycle by responsibility, not all at once.** Create
   `client/s3_workflow.py` for graph construction/submission and
   `client/s3_markers.py` for polling and dependent-job cancellation.  Keep a
   thin wrapper-level compatibility facade or update every
   `mock.patch("client.wrapper...")` target in the same patch.  Retain the
   per-run submit lock, stale-job resolution, failure markers, and the rule
   that a failed analyzer cancels only its dependents—not its useful sibling.
3. **Extract shared-storage stage adapters.** Move the runner adapter and
   its local/PBS dispatch into `client/workflow.py`, using injected callables
   or an explicit adapter dependency.  Modules below it must not import
   `wrapper.py`; wrapper remains the facade and CLI dispatch point.
4. **Extract pure settings/layout helpers only after the above stabilizes.**
   Candidates are `compose.py`, `run_layout.py`, and `pbs_settings.py` from
   the original plan.  Move tests' patch targets together with their callers.
5. **Only then consider splitting `pbs.py`.** The package split has a high
   mocking churn cost (many patches target `client.pbs`).  Snapshot rendered
   scripts before and after; byte-identical output is the acceptance rule.
6. **Run user-owned integration validations at milestones:** first the local
   wrapper flow above, then `tests/test-command-builder-contract.sh`, and
   finally the appropriate local PBS/S3 integration path if its behavior was
   touched.  Never run full emulator, parse, Kubernetes, PBS, or image-build
   operations from the agent.

## Invariants for the next chat

- Keep the run ID, artifact directory names, S3 layout, and `.pbs` marker
  protocol unchanged.
- Keep `PBS_TERMINAL_STATES`, report dependencies, reusable/cached BlockSci
  semantics, and image/provenance behavior unchanged.
- Do not simplify the workflow to a naive `for stage: runner.run(stage)`:
  the graph has dependencies, parallel branches, report joins, and different
  cancellation rules.
- Do not change CLI metadata unless a public option changes (which is out of
  scope).
- Use `apply_patch` for edits, never commit or push, and preserve unrelated
  user work.

## Expected end state and size expectation

The intended result is a legible orchestration core with one declared workflow
graph and runner adapters.  It should remove approximately 2–4 kSLOC over the
complete refactor through real duplication removal; merely moving code into
more files does not count as simplification.  The current step has made the
workflow explicit but is deliberately an intermediate structural state, not
the final LOC reduction.

## Continuation notes — source review (2026-08-29)

### Verified observations

- The first extraction is a sound boundary: `stages.py` has no execution or
  artifact imports, and `stage_executor.py` only operates on submitted
  lifecycle handles.  This is the right direction for reducing the
  `wrapper.py` coordination surface without prematurely replacing mature
  Docker/PBS helpers.
- The S3 graph must record the Kubernetes upload as an input dependency of
  **both** independent analyzer branches.  The reusable graph already did
  this via `blocksci-parse`; the combined BlockSci node initially omitted it.
  The plan now declares `blocksci → kubernetes-emulation`, matching the actual
  S3 artifact contract.  This is a declarative-model correction only:
  `run_full_run_s3` already waits for `.k8s/upload.done` before submitting any
  PBS work.
- `run_full_run_s3` is still the real S3 orchestration implementation.  The
  plan currently drives its dry-run marker list, but PBS submission, marker
  waits, and dependent-job cancellation remain hand-written conditionals in
  `wrapper.py`.  Therefore the graph is not yet a single source of truth; do
  not claim that it is until the S3 adapter executes it.
- The existing S3 tests deliberately patch the `client.wrapper.*` surface.
  Any move of S3 code must either retain wrapper-resolved injected callables or
  update those patch targets in the same patch.  Moving a function by import
  alone would cause mocks to stop intercepting the caller.

### Focus for the next safe extraction

Extract `s3_markers.py` before a broad `s3_workflow.py` move.  Its narrow API
should own only marker waiting and best-effort dependent cancellation; it must
receive `wait_for_s3_marker`, `pbs_job_probe`, and `qdel_pbs_job` as injected
callables (or be called through wrapper-level adapters).  That keeps the
historical mocking surface and the asymmetric cancellation rule explicit:

- failed `coinjoin-analysis` cancels mappings and the report, but leaves the
  independent BlockSci job running so its uploaded result remains useful;
- failed `blocksci-parse` cancels its analyze descendant and the report;
- failed BlockSci analyze or mappings cancels only the report.

Do not make the generic `StageExecutor` enforce this policy.  It is specific
to the S3 PBS DAG and should remain visible in a small policy table next to the
marker code.

### Current extraction: `s3_markers.py`

The first S3 lifecycle unit now lives in `client/s3_markers.py`:

- `wait_for_s3_pbs_marker(...)` owns the exact marker-key construction and
  PBS probe wiring;
- `cancel_dependent_pbs_job(...)` and `rollback_s3_pbs_submissions(...)` own
  the best-effort cancellation messages and recovery commands.

`wrapper.py` retains same-named facade functions and injects its
`wait_for_s3_marker`, `pbs_job_probe`, `pbs_wait_timeout`, and `qdel_pbs_job`
references.  Existing `client.wrapper.*` monkeypatches therefore continue to
intercept the real call sites.  No marker name, timeout, cancellation target,
or stderr text changed.

### Current extraction: `s3_workflow.py`

`client/s3_workflow.py` now owns the high-level `full-run --artifact-backend
s3` sequence: Kubernetes upload wait, PBS-stage wait order, and the existing
asymmetric dependent-job cancellation policy.  It receives an immutable
`S3FullRunOperations` bundle from the wrapper.  The bundle deliberately
contains wrapper-resolved operations rather than importing the wrapper, which
avoids a cycle and keeps all established `client.wrapper.*` patches effective.

`S3PBSJobs` moved with that workflow and remains re-exported through
`client.wrapper`.  PBS graph creation, per-run submission locking, stale-job
resolution, marker clearing, and rollback during submission failure remain in
`wrapper.py` for the next focused slice; they were not mixed into this move.

### Current extraction: `workflow.py`

`client/workflow.py` now owns `SharedStorageStageRunner` and its
`shared_storage_analysis_plan(...)` factory.  It receives
`SharedStorageOperations` from the wrapper, so it has no import back to
`wrapper.py`.  The wrapper still resolves every operation under its historical
name before constructing that bundle; existing patches of
`client.wrapper.run_script`, `run_*_pbs_stage`, `wait_for_pbs_marker`, and
`qdel_pbs_stage` therefore retain their meaning.

The adapter preserves the intentionally different local paths:

- serial local BlockSci remains the legacy `analysis.sh` dispatch;
- parallel local BlockSci uses the separate Docker-stage helper;
- PBS waits retain `stage walltime + 1 hour` and cancellation uses the same
  `.pbs/<stage>` job target.

### Current extraction: `pbs_settings.py`

`client/pbs_settings.py` now contains the pure image/resource resolution
rules: per-stage versus shared PBS image precedence, committed uploader/report
image locks, Singularity URI normalization, PBS resource precedence, and the
walltime-plus-one-hour wait timeout.  `wrapper.py` imports/re-exports the old
names so callers and mock targets do not need to change.

This is a structural move only: it does not render a template, submit a job,
or alter any option/default.  `wrapper.py` has shrunk by roughly 450 physical
lines relative to the tracked base after the four completed module boundaries,
but the repository-wide LOC reduction must come from the later deduplication
pass—not from moving code between files.

### First deduplication inside the extracted S3 workflow

`s3_workflow.py` now has one private marker-wait primitive that receives the
declared dependent jobs for each upstream stage.  This removes the repeated
try/wait/cancel blocks while retaining the visible policy at every call site:
coinjoin-analysis cancels mappings + report and leaves independent BlockSci
work alive; the parser cancels its BlockSci-work descendant + report; BlockSci
work or mappings cancels only the report.  The existing wrapper/S3 tests cover
all four failure paths.

### Current extraction: `s3_staging.py`

`client/s3_staging.py` now owns the three staging responsibilities that were
previously interleaved with submission dispatch:

- deciding whether selected PBS stages require the exporter tree;
- refusing a partial exporter tree and uploading only a missing one;
- S3/Kubernetes preflight plus fresh-prefix/exporter staging before the
  Kubernetes emulation Job exists.

The wrapper facades inject their existing artifact/Kubernetes functions, so
all current `client.wrapper.*` patch targets still intercept the executed
operation.  In particular, a failed Kubernetes authorization preflight still
happens before any exporter is written to the run prefix.

### Submission boundary preparation: `s3_submission.py`

`S3SubmissionTracker` now owns the two pieces of mutable state that every
concrete S3 PBS submission shares: clearing exactly that stage's stale remote
markers before submission and recording each returned job ID both for rollback
and for `.pbs/*.jobid` watch/overlap detection.  The wrapper injects
`clear_s3_stage_markers` and `persist_pbs_job_id`, preserving all patch
targets and persistence paths.

The individual qsub branches still live in `_run_pbs_from_s3`; this small
step removes its nested closures first, so the next extraction can move the
graph with an explicit state object instead of a hidden closure capture.

### Continuation validation

After the S3 combined-graph dependency correction:

```text
uv --cache-dir /tmp/cjp-uv-cache run --locked --extra test pytest \
  tests/pipeline/test_stages.py tests/pipeline/test_stage_executor.py \
  tests/pipeline/test_wrapper.py tests/pipeline/test_pbs.py \
  tests/pipeline/test_s3_backend.py -q
# 232 passed in 5.66s

uv --cache-dir /tmp/cjp-uv-cache run --locked --group dev ruff check \
  pipeline/client/stages.py pipeline/client/stage_executor.py \
  tests/pipeline/test_stages.py tests/pipeline/test_stage_executor.py
# All checks passed

uv --cache-dir /tmp/cjp-uv-cache run --locked --group dev mypy \
  pipeline/client/stages.py pipeline/client/stage_executor.py
# Success: no issues found in 2 source files
```

After extracting `s3_markers.py`:

```text
uv --cache-dir /tmp/cjp-uv-cache run --locked --extra test pytest \
  tests/pipeline/test_s3_markers.py tests/pipeline/test_stages.py \
  tests/pipeline/test_stage_executor.py tests/pipeline/test_wrapper.py \
  tests/pipeline/test_pbs.py tests/pipeline/test_s3_backend.py -q
# 234 passed in 5.80s

uv --cache-dir /tmp/cjp-uv-cache run --locked --group dev ruff check \
  pipeline/client/s3_markers.py pipeline/client/stages.py \
  pipeline/client/stage_executor.py pipeline/client/wrapper.py \
  tests/pipeline/test_s3_markers.py tests/pipeline/test_stages.py \
  tests/pipeline/test_stage_executor.py tests/pipeline/test_wrapper.py
# All checks passed

uv --cache-dir /tmp/cjp-uv-cache run --locked --group dev mypy \
  pipeline/client/s3_markers.py pipeline/client/stages.py \
  pipeline/client/stage_executor.py pipeline/client/wrapper.py
# Success: no issues found in 4 source files
```

After extracting `s3_workflow.py`:

```text
uv --cache-dir /tmp/cjp-uv-cache run --locked --extra test pytest \
  tests/pipeline/test_s3_markers.py tests/pipeline/test_stages.py \
  tests/pipeline/test_stage_executor.py tests/pipeline/test_wrapper.py \
  tests/pipeline/test_pbs.py tests/pipeline/test_s3_backend.py -q
# 234 passed in 5.13s

uv --cache-dir /tmp/cjp-uv-cache run --locked --group dev ruff check \
  pipeline/client/s3_markers.py pipeline/client/s3_workflow.py \
  pipeline/client/stages.py pipeline/client/stage_executor.py \
  pipeline/client/wrapper.py tests/pipeline/test_s3_markers.py \
  tests/pipeline/test_stages.py tests/pipeline/test_stage_executor.py \
  tests/pipeline/test_wrapper.py
# All checks passed

uv --cache-dir /tmp/cjp-uv-cache run --locked --group dev mypy \
  pipeline/client/s3_markers.py pipeline/client/s3_workflow.py \
  pipeline/client/stages.py pipeline/client/stage_executor.py \
  pipeline/client/wrapper.py
# Success: no issues found in 5 source files

./tests/test-command-builder-contract.sh
# 41 tests passed; both generated metadata snapshots match the live parsers
```

After extracting `workflow.py`:

```text
uv --cache-dir /tmp/cjp-uv-cache run --locked --group dev ruff check \
  pipeline/client/s3_markers.py pipeline/client/s3_workflow.py \
  pipeline/client/stages.py pipeline/client/stage_executor.py \
  pipeline/client/workflow.py pipeline/client/wrapper.py \
  tests/pipeline/test_s3_markers.py tests/pipeline/test_stages.py \
  tests/pipeline/test_stage_executor.py tests/pipeline/test_wrapper.py
# All checks passed

uv --cache-dir /tmp/cjp-uv-cache run --locked --group dev mypy \
  pipeline/client/s3_markers.py pipeline/client/s3_workflow.py \
  pipeline/client/stages.py pipeline/client/stage_executor.py \
  pipeline/client/workflow.py pipeline/client/wrapper.py
# Success: no issues found in 6 source files

uv --cache-dir /tmp/cjp-uv-cache run --locked --extra test pytest \
  tests/pipeline/test_s3_markers.py tests/pipeline/test_stages.py \
  tests/pipeline/test_stage_executor.py tests/pipeline/test_wrapper.py \
  tests/pipeline/test_pbs.py tests/pipeline/test_s3_backend.py -q
# 234 passed in 5.13s

git diff --check
# clean
```

After extracting the S3 submission tracker:

```text
uv --cache-dir /tmp/cjp-uv-cache run --locked --group dev ruff check \
  pipeline/client/s3_submission.py pipeline/client/wrapper.py
# All checks passed

uv --cache-dir /tmp/cjp-uv-cache run --locked --group dev mypy \
  pipeline/client/s3_submission.py pipeline/client/wrapper.py
# Success: no issues found in 2 source files

uv --cache-dir /tmp/cjp-uv-cache run --locked --extra test pytest \
  tests/pipeline/test_s3_markers.py tests/pipeline/test_stages.py \
  tests/pipeline/test_stage_executor.py tests/pipeline/test_wrapper.py \
  tests/pipeline/test_pbs.py tests/pipeline/test_s3_backend.py -q
# 234 passed in 5.01s

git diff --check
# clean
```

After extracting `s3_staging.py`:

```text
uv --cache-dir /tmp/cjp-uv-cache run --locked --group dev ruff check \
  pipeline/client/s3_staging.py pipeline/client/wrapper.py
# All checks passed

uv --cache-dir /tmp/cjp-uv-cache run --locked --group dev mypy \
  pipeline/client/s3_staging.py pipeline/client/wrapper.py
# Success: no issues found in 2 source files

uv --cache-dir /tmp/cjp-uv-cache run --locked --extra test pytest \
  tests/pipeline/test_s3_markers.py tests/pipeline/test_stages.py \
  tests/pipeline/test_stage_executor.py tests/pipeline/test_wrapper.py \
  tests/pipeline/test_pbs.py tests/pipeline/test_s3_backend.py -q
# 234 passed in 4.95s

git diff --check
# clean
```

After the S3 wait/cancellation deduplication:

```text
uv --cache-dir /tmp/cjp-uv-cache run --locked --group dev ruff check \
  pipeline/client/s3_workflow.py pipeline/client/wrapper.py
# All checks passed

uv --cache-dir /tmp/cjp-uv-cache run --locked --group dev mypy \
  pipeline/client/s3_workflow.py pipeline/client/wrapper.py
# Success: no issues found in 2 source files

uv --cache-dir /tmp/cjp-uv-cache run --locked --extra test pytest \
  tests/pipeline/test_wrapper.py tests/pipeline/test_s3_backend.py -q
# 169 passed in 5.11s

git diff --check
# clean
```

After extracting `pbs_settings.py`:

```text
uv --cache-dir /tmp/cjp-uv-cache run --locked --extra test pytest \
  tests/pipeline/test_s3_markers.py tests/pipeline/test_stages.py \
  tests/pipeline/test_stage_executor.py tests/pipeline/test_wrapper.py \
  tests/pipeline/test_pbs.py tests/pipeline/test_s3_backend.py -q
# 234 passed in 5.10s

./tests/test-command-builder-contract.sh
# 41 tests passed; both metadata snapshots match live parsers

git diff --check
# clean
```
