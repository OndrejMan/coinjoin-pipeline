# Core-module refactor: local handoff (2026-08-29)

> Local implementation note for the uncommitted source changes.  This records a
> behavior-preserving refactor; it is not a product-specification change.

## Goal and constraints

The immediate goal is to make the client orchestration understandable and
testable without changing the supported execution paths:

- preserve local, Docker/Compose, Kubernetes, shared-storage PBS and S3/PBS
  behaviour;
- preserve CLI arguments, command strings, artifact layout, lock/rollback and
  cancellation policies;
- keep `pipeline.client.wrapper` as the compatibility façade while moving
  coherent orchestration units into small modules;
- retain wrapper-level patch points, because the existing tests deliberately
  monkeypatch `client.wrapper.*`;
- do not modify PBS templates or submit a real PBS/Kubernetes job as part of
  this refactor.

This is intentionally separate from a later product decision to support only
Kubernetes -> S3 -> PBS.  That later deletion would need its own migration and
validation plan.

## Current working state

Work is in `/home/administrator/diplomka/coinjoin-pipeline` on `fullS3`.  Do
not commit or push the changes from this handoff automatically.

The tree contains pre-existing user work plus these refactor modules:

- `pipeline/client/stages.py`
- `pipeline/client/stage_executor.py`
- `pipeline/client/artifact_validation.py`
- `pipeline/client/locks.py`
- `pipeline/client/run_context.py`
- `pipeline/client/workflow.py`
- `pipeline/client/pbs_settings.py`
- `pipeline/client/s3_markers.py`
- `pipeline/client/s3_emulation.py`
- `pipeline/client/s3_staging.py`
- `pipeline/client/s3_submission.py`
- `pipeline/client/s3_workflow.py`
- `pipeline/client/shared_storage_pbs.py`
- focused tests under `tests/pipeline/`

At this point `wrapper.py` is about 2,674 lines, roughly 1,080 lines smaller
than the tracked base, while preserving its public/patchable surface.

## Completed extractions

### Plans and generic execution

`stages.py` owns immutable stage/analysis plans.  In particular the combined
S3 `blocksci` stage now explicitly depends on `kubernetes-emulation`; this is
covered by `test_s3_combined_plan_waits_for_the_emulation_upload`.

`stage_executor.py` owns serial/parallel plan traversal and records submitted
stage job ids.  The wrapper supplies its existing submit/wait functions, so
tests that patch wrapper names continue to work.

### Shared-storage analysis orchestration

`workflow.py` owns the shared-storage plan, stage runner and operation bundle.
`wrapper.run_parallel_analysis` and `wrapper.run_serial_analysis` now only
construct the bundle from wrapper-level functions and execute it.  The PBS
wait timeout still derives from the stage walltime via `pbs_wait_timeout`.

`shared_storage_pbs.py` owns the concrete shared-storage PBS adapters for
BlockSci, coinjoin-analysis, mappings and the report-only stage.  It imports
PBS command/resource construction directly, while the wrapper injects the
filesystem/Compose, qsub, exporter-staging and marker-wait operations.  Thus
the existing wrapper-level mocks still exercise the same calls.

### PBS settings

`pbs_settings.py` contains pure settings and resource helpers: image-lock
resolution, uploader/unified-report image derivation, truthy environment
parsing, resource defaults/overrides, and PBS wait-timeout calculation.  The
wrapper imports and re-exports the historical names for compatibility.

### Artifact argument validation

`artifact_validation.py` owns the cross-option validation and normalization
for S3 artifact commands.  It retains every `parser.error()` message and
normalizes the same URI, credentials, profile and run-id attributes; the
wrapper keeps the historical `validate_artifact_arguments` entry point.

### Locking and S3 overlap detection

`locks.py` owns the process-reentrant advisory lock registry, explicit closing
at shutdown, `.pbs-submit.lock` path selection, command lock-path selection
and active S3/PBS graph detection.  Wrapper façades still provide the existing
patch points and inject the run-directory resolver and PBS job probe.

### Run-directory context

`run_context.py` owns grouped-run discovery, the `PIPELINE_RUN_ID` contract,
safe `--run-dir` resolution and newest-run fallback.  The wrapper retains its
historical façade functions and passes the canonical run marker set.

### S3 marker, staging and submission mechanics

`s3_markers.py` owns waiting for PBS marker files, dependent-job cancellation,
and rollback.  Wrapper facades inject the historic wrapper functions.

`s3_staging.py` owns exporter-staging decisions and Kubernetes-to-S3 staging.
It receives S3/Kubernetes/Compose operations explicitly, avoiding an import
back into `wrapper`.

`s3_emulation.py` owns the Kubernetes S3 emulation-job sequence: scenario
resolution, manifest rendering, dry-run printing and resource application.
The wrapper supplies the established scenario, image and Kubernetes functions,
so the rendered manifest and mock surface are unchanged.

`s3_submission.py` owns two layers:

- per-stage marker preparation and submitted-job persistence through
  `S3SubmissionTracker`;
- the S3/PBS submission lifecycle: resolve/create the submission directory,
  acquire `.pbs-submit.lock`, reject another active submission, submit the
  graph, then roll back every recorded job on `BaseException`.

`wrapper.run_pbs_from_s3` is now a small compatibility adapter.  The concrete
qsub graph is implemented by `submit_s3_pbs_stages()` in this module and gets
its historical wrapper patch points through `S3StageSubmissionOperations`.

### S3 full-run policy

`s3_workflow.py` owns `S3PBSJobs`, the full-run operation bundle and the
high-level full-run sequence.  It also centralizes PBS-stage waiting and keeps
the established cancellation semantics:

- a failed baseline cancels mappings and report, but leaves BlockSci work
  running and prints the recovery message;
- a failed BlockSci parse cancels BlockSci work and report;
- failed BlockSci work or mappings cancels the report.

The wrapper remains the injected compatibility layer, so existing patch-based
tests retain their original targets.

## Compatibility pattern

New modules must not import `pipeline.client.wrapper`.  Instead they take
small operation dataclasses or callables.  The wrapper constructs those bundles
from its own names.  This gives a real dependency boundary without breaking
the test suite's existing mocks.

When moving code, preserve all of the following exactly unless a separately
approved behaviour change is being made:

- command construction and argument validation;
- output/error text used by tests and operators;
- S3 key layout and `.pbs` marker naming;
- lock location and stale/active submission checks;
- rollback and selective cancellation policy;
- dry-run behavior;
- PBS dependency graph and resource selection.

## Validation completed

Fast, focused checks have passed after the latest lifecycle extraction:

```text
rtk uv --cache-dir /tmp/cjp-uv-cache run --locked --extra test pytest \
  tests/pipeline/test_s3_markers.py tests/pipeline/test_stages.py \
  tests/pipeline/test_stage_executor.py tests/pipeline/test_wrapper.py \
  tests/pipeline/test_pbs.py tests/pipeline/test_s3_backend.py -q
# 234 passed

rtk uv --cache-dir /tmp/cjp-uv-cache run --locked --group dev ruff check ...
# All checks passed

rtk uv --cache-dir /tmp/cjp-uv-cache run --locked --group dev mypy ...
# Success: no issues found

./tests/test-command-builder-contract.sh
# 41 tests passed; metadata snapshots match

git diff --check
# clean
```

The host-wrapper contract has additionally been run successfully by the user:

```text
./tests/test-runIt-overactive-local.sh
PASS: the host CLI renders a bare, checkout-backed wrapper invocation.
```

## Required local integration gate

Before changing PBS templates, resource semantics, or shell submission
behavior, run and retain the result of:

```bash
cd /home/administrator/diplomka/coinjoin-pipeline
./tests/test-local-pbs-analysis.sh
```

That test has not yet been supplied in this refactor session.  The current
changes deliberately avoid those behavioural areas.

## Next implementation steps

1. Treat the current behavior-preserving extraction pass as structurally
   complete.  `compose_env` remains intentionally in the executable wrapper:
   it is the single environment contract shared by every remaining
   host/Compose action.  Splitting it would add a large callback bundle with
   no clearer ownership.
2. Retain the focused unit/type/lint checks and obtain the local PBS integration
   result before any PBS-template or shell behaviour is touched.
3. Once that integration gate is recorded, make a separate decision
   document for retiring legacy/shared-storage paths.  Do not silently fold
   that product change into this work.

## Closing audit (current pass)

The wrapper now keeps executable entry, CLI parsing, Compose environment
assembly and small compatibility façades.  Extracted modules own plans,
execution, PBS settings and shared-storage stages, S3 staging/emulation/
submission/full-run policy, argument validation, locks, and run discovery.
No remaining large wrapper section was moved merely to lower its line count.

The final fast verification of this pass passed:

```text
234 focused pipeline tests passed
41 command-builder contract tests passed
Ruff: all checks passed
Mypy: no issues found in 7 refactor modules
git diff --check: clean
```

## Scope/size expectation

The current refactor should be net-negative but is primarily about clearer
ownership and tests.  A realistic final result remains approximately
1,000--2,500 fewer physical lines (including removed wrapper duplication),
depending on how much dead code becomes safely provable.  Removing legacy
shared-storage PBS or local Compose is a separate, higher-risk deletion and
should not be counted as already complete.
