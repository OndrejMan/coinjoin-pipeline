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

The tree contains pre-existing user work plus these refactor modules
(sizes as of the stage-graph pass, `wc -l`):

| module | lines | owns |
| --- | --- | --- |
| `client/stages.py` | 369 | `StageKind`, `StagePlan`, `StageGraph`, both plan builders |
| `client/stage_executor.py` | 107 | backend-neutral serial/parallel graph traversal |
| `client/workflow.py` | 166 | shared-storage plan + `SharedStorageStageRunner` |
| `client/shared_storage_pbs.py` | 209 | concrete shared-storage PBS stage adapters |
| `client/s3_workflow.py` | 220 | `S3PBSJobs`, full-run sequence, wait/cancel policy |
| `client/s3_submission.py` | 424 | the concrete S3 qsub graph, tracker, lock/rollback |
| `client/s3_markers.py` | 97 | marker waiting, dependent cancellation, rollback |
| `client/s3_staging.py` | 81 | exporter staging and Kubernetes-to-S3 run staging |
| `client/s3_emulation.py` | 72 | Kubernetes S3 emulation-job sequence |
| `client/pbs_settings.py` | 174 | pure image/resource/walltime resolution |
| `client/artifact_validation.py` | 218 | cross-option artifact argument validation |
| `client/locks.py` | 105 | advisory locks, active-submission detection |
| `client/run_context.py` | 92 | run-directory resolution and discovery |
| `client/cli_*.py` | 768 | declaration-only CLI surface |
| `client/pbs/` | 2,149 | defaults, validation, commands, templates, submission |
| `client/wrapper.py` | 2,027 | executable entry, Compose environment, compatibility façade |

`wrapper.py` is 2,027 lines: 1,272 fewer than the pre-refactor snapshot in
`ab3b0e9` (3,299) and 26 fewer than the last commit `66fcfb3` (2,053), while
preserving its public/patchable surface.  This pass moved coordination, not
line count: the reduction it produced is in the number of places that encode
the pipeline, not in `wc -l`.

### PBS package follow-up

`pipeline/client/pbs.py` is now the `pipeline/client/pbs/` package.  Its
compatibility façade (`pbs/__init__.py`) preserves the `from client.pbs import
...` API used by the wrapper and callers, while implementation ownership is
explicit:

- `defaults.py` owns scheduler and image defaults;
- `validation.py` owns template and filesystem validation;
- `templates_local.py` and `templates_s3.py` render their respective scripts;
- `commands.py` builds in-container commands; and
- `submission.py` owns qsub/qstat/qdel, marker waits, and local/S3 submission.

Tests must patch the owning module (for example
`client.pbs.submission.subprocess`), not the façade.  This prevents a mock
from silently missing the name resolved by the production function.  The
focused PBS/S3, wrapper, and CLI-contract test groups passed after the split;
the local PBS integration test remains the required gate before changing
template or scheduler behavior.

## Completed extractions

### Plans and generic execution

`stages.py` owns the immutable stage graph and is the single declaration of
the pipeline; see "Follow-up: one stage graph as the source of truth" below
for what derives from it.  Both plan builders (`analysis_plan` for
shared storage, `s3_full_run_plan` for Kubernetes -> S3 -> PBS) return a
`StageGraph`.  The combined S3 `blocksci` stage explicitly depends on
`kubernetes-emulation`; this is covered by
`test_s3_combined_plan_waits_for_the_emulation_upload`.

`stage_executor.py` owns serial/parallel graph traversal and knows no stage by
name.  The wrapper supplies its existing submit/wait functions, so tests that
patch wrapper names continue to work.

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

Superseded by "Resume here" at the end of this document, which reflects the
stage-graph pass.  `compose_env` remains intentionally in the executable
wrapper: it is the single environment contract shared by every remaining
host/Compose action, and splitting it would add a large callback bundle with
no clearer ownership.

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

## Follow-up: CLI parser and S3 target boundary

The wrapper remains the executable compatibility façade, but declaration-only
CLI code now lives in `client.cli_defaults`, `client.cli_validation`, and
`client.cli_parser`.  `build_parser` and the option helper names are still
re-exported from `client.wrapper`, so callers and command metadata retain the
same public surface.

`client.artifacts.S3Target` is an immutable value object for the five S3 run
parameters (`artifact_uri`, `run_id`, endpoint URL, credentials file, and
profile).  The S3 PBS render and submission APIs accept that target rather
than five parallel strings.  `S3Target.from_args` is deliberately called only
after existing CLI validation; the command-line names, marker/key layout,
credential handling, and generated PBS scripts therefore remain unchanged.

Focused verification after this follow-up:

```text
230 tests: test_pbs.py, test_s3_backend.py, test_wrapper.py, test_cli_contract.py
41 command-builder contract tests; both metadata snapshots match
Ruff: all checks passed
Mypy: no issues found in 13 source files
git diff --check: clean
```

## Follow-up: one stage graph as the source of truth

The earlier passes left the pipeline declared in one place and *executed* from
three others: `stages.py` described the DAG (used only for `--dry-run`),
`s3_submission.py` wired qsub dependencies by hand, and `s3_workflow.py`
repeated the same edges again as wait order plus a hand-listed cancellation
policy.  Those three could drift.  They are now one declaration.

- `client.stages` owns `StageKind`, `StagePlan`, and `StageGraph`
  (`get`, `of_kind`, `scheduled`, `dependents_of`, `dependency_ids`,
  `dependency_id`).  `s3_full_run_plan()` takes the full flag matrix
  (`--analysisPbs`, `--blocksciPbs`, `--mappingsPbs`, `--blocksci-workflow`,
  `--blocksci-task`), so the graph describes exactly the stages an invocation
  submits, in the order they are waited for.
- `s3_workflow.run_s3_full_run` iterates `plan.scheduled()` instead of five
  hand-written `if jobs.X:` blocks.  Cancellation is derived:
  `plan.dependents_of(failed_stage)` is cancelled, everything else still
  queued keeps running and is reported as such.  The established policy is
  unchanged; it is now a consequence of the declared edges rather than a
  parallel list.
- `s3_submission.submit_s3_pbs_stages` derives every scheduler dependency from
  the same graph (`dependency_id` for mappings and BlockSci work,
  `dependency_ids` plus the expected-count check for the report), and returns
  `S3PBSJobs.from_plan(...)`.  Stage submission goes through
  `S3SubmissionTracker.submit()`, which clears stale markers, submits, and
  records the job as one step.
- `StageKind` replaces stringly-typed dispatch: `SharedStorageStageRunner`
  and `S3PBSJobs.job_for` key on the kind, so a renamed marker cannot silently
  miss a dispatch branch.  `RESOURCE_GROUPS`/`resource_group()` map a kind to
  its PBS budget, and `pbs_settings.stage_pbs_walltime` resolves the wait
  budget for both orchestrators.
- `AnalysisPlan` is gone: `analysis_plan()` returns a `StageGraph` too, and
  `stage_executor` schedules whatever the graph declares ready instead of
  naming baseline/BlockSci/mappings.  The shared-storage parallel run keeps its
  broader cancellation policy (a failure cancels every running sibling,
  because its single report needs all analyzers), which is deliberately not
  the S3 dependents-only policy.

Injection was narrowed to real boundaries: `S3StageSubmissionOperations` went
from 37 fields to 12 (bucket preflight/staging plus the `submit_*` calls).
Command construction, image/resource resolution, defaults and
`persist_pbs_job_id` are imported directly, and `S3FullRunOperations` lost its
four walltime callables.  The wrapper patch surface used by the tests
(`client.wrapper.submit_*`, marker and preflight helpers) is unchanged.

One behaviour difference is intentional: a full run that submits
`--blocksci-task update` now waits for the `blocksci-update` marker.  The
previous wait chain had no branch for that job and reported "Completed" while
it was still running.

New regression cover: `StageGraph` queries and flag-driven plans
(`tests/pipeline/test_stages.py`), reusable-workflow cancellation
(`tests/pipeline/test_wrapper.py`), and the executor's dependency gating,
refused submissions, sibling cancellation and "no cancel for finished work"
(`tests/pipeline/test_stage_executor.py`).

```text
494 pipeline/unit tests passed
Ruff: all checks passed
```

Still outstanding: `./tests/test-local-pbs-analysis.sh` (the local PBS
integration gate) has not been run in this session, and mypy is not installed
in the checkout's `.venv`.

## Resume here (next session)

Everything below reflects the state after the stage-graph pass.  Nothing is
committed; `git status` should show modified files under `pipeline/client/`,
`tests/pipeline/` and this document, on branch `fullS3`.

### How to verify quickly

The checkout's virtualenv has pytest and ruff (no mypy):

```bash
cd /home/administrator/diplomka/coinjoin-pipeline
.venv/bin/python -m pytest tests/pipeline tests/unit -q   # 494 passed
.venv/bin/ruff check pipeline tests                       # All checks passed
```

### Where the pipeline is declared

`client/stages.py` is the only place that declares stages and edges.  Read
`s3_full_run_plan()` and `analysis_plan()` first; `StageGraph.dependents_of`
is the cancellation policy, `dependency_ids` is the qsub wiring, and
`scheduled()` is the wait/dry-run order.

### Work items, in the order they should be taken

1. **Run the local PBS integration gate.**  `./tests/test-local-pbs-analysis.sh`
   still has no recorded result in this refactor.  Do this before touching
   PBS templates, resource semantics, or shell submission behaviour -- and
   before item 2, which is the first change that would benefit from it.
2. **Give `StagePlan` an operation (the remaining architectural debt).**
   `s3_submission.submit_s3_pbs_stages()` is still a ~250-line `if` chain that
   selects a command builder and a submit function per stage.  The graph
   already knows which stages exist and how they depend on each other, so the
   chain can become a per-kind operation the graph carries, leaving one
   submission loop.  Do not start this without item 1: it is the first pass
   whose failure mode is a wrong PBS script rather than a failing unit test.
   Acceptance criterion: rendered PBS scripts stay byte-identical.
3. **Only then consider retiring the wrapper façade.**  `client.wrapper.*` is
   still the monkeypatch surface the test suite targets; removing the
   re-exports is a test-migration project, not a refactor step.  Until then,
   treat the façade as migration debt, not as architecture.

### Rules that still hold

- Never commit or push automatically; the user commits.
- New modules must not import `client.wrapper`; they take operation bundles.
- Inject only real side effects (bucket I/O, qsub, Compose/Kubernetes).  Pure
  helpers -- command construction, image/resource resolution, defaults -- are
  imported directly.  `S3StageSubmissionOperations` is the reference for the
  size such a bundle should stay at (12 fields, all side effects).
- Preserve marker/key layout, command strings, lock and rollback behaviour,
  dry-run output and the two distinct cancellation policies (S3:
  dependents-only; shared-storage parallel: every running sibling).
