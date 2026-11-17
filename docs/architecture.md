# Current execution architecture

Updated 2026-11-17. The thesis results come from the Kubernetes → S3 → PBS
path. Every module outside that path carries an `EXPERIMENTAL` header and is
listed under "Experimental paths" below.

Contents: [input and host layer](#input-and-host-layer) ·
[module ownership](#module-ownership) ·
[a Kubernetes → S3 → PBS run](#a-kubernetes--s3--pbs-run-step-by-step) ·
[what a run consists of](#what-a-run-consists-of) ·
[a PBS job on the compute node](#a-pbs-job-on-the-compute-node) ·
[analyzer/report flow](#one-analyzerreport-flow) ·
[images](#images-and-their-digests) ·
[mainnet synchronization](#mainnet-synchronization) ·
[operating a run](#operating-a-run) ·
[verification boundary](#verification-boundary) ·
[experimental paths](#experimental-paths) ·
[related documents](#related-documents)

```text
cjp run FILE.yaml [options]   |   cjp <action> [options]   |   cjp <utility> ...
  → PipelineConfiguration (one schema)                         └─ version, doctor, pull, watch, status, stop,
  → RunContext.prepare (defaults, validation, identity, images)   download-report, clean-s3, runs, scenarios
  → host preflight (option errors, then tools and images)         (no experiment; cli._utility)
  → command lock, research_manifest.json
  → execution.orchestrator (same process)
       ├─ Kubernetes → S3 → PBS stage graph            (orchestrator.py, s3_*.py)
       │    full-run and emulate with the S3 backend, pbs-from-s3
       ├─ mainnet-sync: six chained PBS jobs            (mainnet_sync.py)
       └─ EXPERIMENTAL local Docker/Podman, Kubernetes
          without S3, shared-storage PBS                (local.py)
```

The host CLI and the orchestration are one Python process. The heavy work runs
elsewhere: the emulation in a Kubernetes Job, the analyses in PBS jobs under
Apptainer, each from a versioned image. The process that started a run only
submits, waits and cancels, and everything it knows about the run is also
recorded outside it, which is what `status`, `stop` and `--resume-run` read.

## Input and host layer

`arguments.py` is an input adapter. It merges explicitly provided values into
YAML before decoding, and owns the `--help` text (grouped by configuration
section, each option with its YAML key). `configuration.py` owns field types,
option spellings, action aliases and value bounds; `option_rules.py` owns every
cross-field rule (`PipelineConfiguration.validate` only calls it).
`console.py` holds the exit codes and `ERROR:` output shared by `cli.py`, the
utilities (`watch`, `status`, `stop`, `download-report`, `clean-s3`) and
`pipeline_logging.exit_with_error`, which ends a failed run. The existing CLI
flags remain available to scripts, but there is no YAML-to-argv conversion,
metadata snapshot or second host Python process. `cjp run FILE.yaml` is the
preferred entry point. An explicit `stages.<name>: false` is respected; assigning
PBS resources to that disabled stage is an error.
Cross-field rules read typed configuration attributes directly. The `provided`
field set distinguishes explicit values from defaults; CLI option names are
used only in validation messages.

`context.py` resolves run ID, output root and image selection, and restores
environment, bytecode policy, signal handlers and locks after execution. For
`full-run` and `emulate` the host computes the run ID (`runs.run_id_for`) and
exports it as `PIPELINE_RUN_ID`; `run_ids.py` holds the one rule a run ID must
satisfy, because every path, S3 prefix, Kubernetes name and PBS job is built
from it. `cli.preflight_errors` reports option errors first and then, for a real
run, what `doctor.py` finds missing: the host tools the action needs
(`required_capabilities`) and, on the local paths, the images.

`research_manifest.json` records top-level schema, mode and run ID, with the
redacted launcher record of the latest invocation under `host_launcher`. A later
invocation over the same run (`--resume-run`, a stage with `--run-dir`) moves the
previous record to `earlier_host_launchers`. The record is written only once the
command lock is held, and a new run refuses a run ID that already records a
launch. External import manifests keep their input provenance. The report's
manifest remains a distinct analytical artifact with image digests and exporter
hashes.

An S3 `full-run` or `emulate` copies the record to
`<run prefix>/research_manifest.json` (`execution/s3_run_manifest.py`): after
staging, with status `prepared`, and again when the command ends, with its
outcome. The command, the resolved configuration and the pipeline commit
(`pipeline_source`) thus stay beside the artifacts when the frontend's run
directory is gone. The last upload only warns when it fails, because the run's
exit code is decided by then. `pbs-from-s3` records no launch.

The exit codes are part of the script interface: 0 success, 2 invalid
configuration or failed preflight, 3 `clean-s3` not confirmed or `runs validate`
found a missing run or stage, 4 `runs validate` found an incomplete report,
5 a failed run or transfer, 130 interrupted. `console.EXIT_CODES` is the source
of the list in `--help`.

## Module ownership

Host and configuration:

| Responsibility | Owner |
|---|---|
| Configuration and loading | `configuration.py`, `arguments.py`, `option_rules.py` |
| Exit codes and error output of the host commands | `console.py` |
| Resolved run and host lifetime | `context.py`, `runs.py`, `manifest.py` |
| What a run ID may look like | `run_ids.py` |
| Host preflight: tools an action needs, runs root, images | `doctor.py` |
| Image names, registry, release or local tag, the committed uploader and report references | `images.py`, `container/*.image` |
| Pinning registry tags to digests at submission | `registry.py` |
| Checkout locations, required exporter files, default runs root | `paths.py` |
| Subprocess boundary of the host | `process.py` |
| Scenario lookup and host/container scenario paths | `execution/scenarios.py` |

Orchestration of the Kubernetes → S3 → PBS path:

| Responsibility | Owner |
|---|---|
| Action dispatch and the S3 `emulate`/`full-run`/`pbs-from-s3` and `mainnet-sync` branches | `execution/orchestrator.py` |
| Graph definitions and dependency queries | `execution/stages.py` |
| S3 full-run: staging, emulation Job, PBS graph, marker waits, rollback | `execution/s3_workflow.py`, `s3_staging.py`, `s3_emulation.py`, `s3_submission.py`, `s3_markers.py` |
| Continuing a full-run whose driving process died | `execution/s3_resume.py` (`decide_resume`) |
| The copy of the launch record in the run prefix, and the launches a resume takes over from it | `execution/s3_run_manifest.py`, `runs.py` (`adopt_recorded_launches`) |
| Kubernetes resource generation and diagnosis | `execution/kubernetes.py` |
| In-cluster prefix preflight and artifact uploader | `execution/k8s_s3_prefix_preflight.sh`, `k8s_s3_uploader.sh`, their shared `k8s_s3_access.sh` header |
| Frontend marker wait and the remote-job probe states it decides by | `execution/s3_markers.py` (`wait_for_s3_marker`), `execution/probes.py` |
| Command locks and S3 PBS overlap detection | `execution/locks.py` |
| Kerberos ticket renewal while a full-run waits on PBS | `execution/pbs/kerberos.py` |
| Required exporter files and their staging into a run prefix | `paths.py` (`REQUIRED_EXPORTERS`), `execution/s3_staging.py` |
| Failure exit of a run; stage progress and per-stage evidence logs of the local paths | `execution/pipeline_logging.py` |

PBS, on the frontend:

| Responsibility | Owner |
|---|---|
| PBS submit, qdel, state probes; S3 job scripts | `execution/pbs/submission.py`, `templates_s3.py`, `analysis_modes.py`, `scripts.py`, `templates/`, `commands.py` |
| qstat transport and what a qstat state means (queued, running, terminal) to every client | `execution/pbs/status.py` (`job_details`, `job_phase`) |
| Recorded PBS job IDs of a run (`.pbs/<stage>.jobid`) | `execution/pbs/submission.py` (`recorded_pbs_job_ids`) |
| PBS resources and image references | `execution/pbs/defaults.py` (`DEFAULT_RESOURCES`), `execution/pbs_settings.py` |
| Values interpolated into a job script or its `#PBS` directives | `execution/pbs/validation.py` |
| Standalone coinjoin-analysis commands: CLI arguments and input rules | `execution/coinjoin_tools.py` |
| Mainnet synchronization: validation, rendering and submission of the six jobs | `execution/mainnet_sync.py` |

PBS, on the compute node (shipped modules, standard library only, Python 3.8):

| Responsibility | Owner |
|---|---|
| Every S3 PBS job (shell only writes and starts it): entry, job frame, settings types, jobs | `execution/pbs/stage_runtime.py`, `stage.py`, `stage_settings.py`, `analyzer_jobs.py`, `blocksci_jobs.py` (index writers), `blocksci_analysis_jobs.py` (index reader), `blocksci_index.py` (what both share) |
| Mainnet synchronization jobs on the node: entry, the six jobs, their shared dataset/Core/Dumplings services | `execution/mainnet_runtime.py`, `mainnet_jobs.py`, `mainnet_worker.py` (rendered by `mainnet_sync.py`) |
| Apptainer/Singularity on the node: runtime, `exec` command line, binds of multijob aliases | `execution/pbs/apptainer.py` (S3 stages and mainnet jobs alike) |
| Persistent index lock, completeness, backup and restore; the index manifest every parse and update records | `execution/pbs/index_store.py` (`IndexStore`, `index_manifest`) |
| Selecting and verifying bitcoin-block-archive files for a parse or update | `execution/block_archive.py` |
| JupyterLab over an existing index | `execution/pbs/templates/blocksci_notebook.py` |

Storage and layout:

| Responsibility | Owner |
|---|---|
| Run-directory layout (every run-relative path, marker sets, report inputs), imported by the host, the exporters and the PBS jobs | `pipeline/exporters/artifact_paths.py` (stdlib only; the jobs ship it) |
| The same layout as JSON for clients that are not Python (scenario-lab's `shared/run-layout.json`) | `run_layout.py` (`python -m coinjoin_pipeline.run_layout`) |
| S3 targets, validation, listing and exact-key existence for run, watch/download/cleanup consumers | `storage/s3.py` |
| s5cmd calls shared by the frontend, the mainnet worker and the Python PBS stages | `storage/s5client.py` |
| Format of a single backup (a run's index, the persistent index, Core checkpoint): archive, `.sha256`, manifest written last | `storage/s5client.py` (`replace_backup`, `restore_backup`) |

Operating a run:

| Responsibility | Owner |
|---|---|
| What a run recorded outside any process: job IDs, labelled Kubernetes resources, S3 markers | `run_records.py` |
| Read-only state of runs; stopping a run; following its logs | `status.py`, `stop.py`, `watch.py` |
| Downloading a finished report; deleting S3 artifacts | `download_report.py`, `clean_s3.py` |

Exporters (`pipeline/exporters/`, a standalone tree that never imports the host package):

| Responsibility | Owner |
|---|---|
| Container workload shared by Docker and Apptainer | `worker.py` |
| Detector and image options shared by the analysis and report input boundaries | `parameters.py` |
| Heavy BlockSci results: detectors, clustering, diagnostics | `blocksci_export/analysis.py`, `blocksci_export/detector.py` |
| Artifact reader independent of BlockSci | `analysis_artifact.py` |
| Parsed block-archive cache height, checkpoint and update continuity verification | `verify_chain.py` |
| Emulator ground truth: verified label manifest, exported blocks, wallet addresses | `emulator_data.py`, `script_metadata.py` |
| Baseline and exported-block normalization, scenario consistency checks | `normalization.py`, `scenario.py`, `common.py` |
| Agreement, divergences, detection and clustering metrics | `comparison.py` |
| Pool liquidity of an emulated run | `liquidity.py` |
| Image provenance and BlockSci's view of the exported targets | `integration_diagnostics.py` |
| Container image inspection shared by the host and standalone exporters | `image_provenance.py` |
| Report manifest: input hashes, images, reproducibility diff | `manifest.py` |
| Report assembly and presentation | `cli.py`, `report_builder.py`, `markdown_report.py`, `report_types.py` |
| Python explanations of the BlockSci detector rules | `heuristics.py` |
| Research adapters copied into a coinjoin-analysis invocation | `coinjoin_research.py` |

## A Kubernetes → S3 → PBS run, step by step

`cjp run examples/metacentrum-regtest-s3.yaml` is an S3 `full-run`
(`s3_workflow.run_s3_full_run`). The same pieces serve the two-command form:
`emulate` with the S3 backend runs steps 2 and 3 and returns, and `pbs-from-s3`
runs step 5 for a run that is already in the bucket and returns once the jobs are
submitted. Neither waits; only `full-run` does.

1. **Pin.** `pin_emulation_images` replaces the emulator and uploader tags with
   their current digests. When any analyzer runs, a missing `qsub` stops the run
   here, before anything is created.
2. **Stage** (`s3_staging.stage_kubernetes_s3_run`). In this order: the S3 access
   preflight, the Kubernetes permission and Secret preflight, the check that the
   run prefix is empty, and only then the upload of `pipeline/exporters/` to
   `<run prefix>/.pipeline/exporters/`, followed by the launch record
   `research_manifest.json`. A failed preflight therefore leaves nothing behind
   under the run ID.
3. **Emulate** (`s3_emulation.py`, `kubernetes.render_s3_emulation_resources`).
   One `kubectl apply` creates a ServiceAccount, a ConfigMap with the scenario, a
   Role, a RoleBinding and the Job; the Job is then made the owner of the other
   four, so deleting it removes them. The namespace and the credentials Secret
   must already exist (`--reuse-namespace`). The Job's pod has three containers
   over one `emptyDir`:
   - `prefix-preflight` (init, uploader image) refuses a run prefix that holds
     anything besides the staged exporters and the launch record at its root, and
     one whose exporters are incomplete. Whatever else the frontend writes before
     the Job has to be added to that filter;
   - `controller` (emulator image) runs `manager.py --driver kubernetes
     --in-cluster`, which creates the engine pods as children of this pod and
     ends by writing `.controller.done` or `.controller.failed`;
   - `uploader` (uploader image) waits for that file, a terminated controller or
     the emulation timeout, syncs the run directory to the run prefix and writes
     `.k8s/upload.done` last, or `.k8s/upload.failed` first.

   The Job has no retries, a deadline of scheduling timeout + emulation timeout +
   60 s, and is removed an hour after it ends.
4. **Wait for the upload** (`wait_for_emulation_upload`). The frontend polls the
   two markers every 30 s and probes the Job with `kubectl`
   (`kubernetes_job_probe`). On a failure it prints the collected Kubernetes
   diagnostics and deletes the Job.
5. **Submit the PBS graph** (`s3_submission.submit_s3_pbs_graph`). Under the
   run's `.pbs-submit.lock`, and only when no recorded job of the run is still
   queued, running or unverifiable. It checks what the graph will read (another
   run's index manifest for `blocksci.cache_source_run_id`, an existing baseline
   when the report or the mappings run without the analyzer) and stages the
   exporters when the prefix has none. `S3StageRunner` then submits the stages in
   graph order through one `_submit`: clear the stage's stale markers, `qsub` the
   script through stdin with `depend=afterok` on its PBS dependencies, record the
   job ID in `<runs-root>/<run-id>/.pbs/<stage>.jobid` and copy it to the run
   prefix. Any error rolls back the jobs submitted so far and names those it
   could not cancel.
6. **Wait for the stages** (`wait_for_pbs_graph`), in graph order, each for its
   `.pbs/<stage>.done` or `.failed` marker with a `qstat` probe. A failed stage
   cancels exactly its transitive dependents; independent jobs keep running and
   still upload their results.

`full-run` holds `kerberos_ticket_renewal` around all of this: it refuses to start
without a renewable ticket and renews it hourly, because `qstat` and `qdel` must
still work after the ten-hour ticket of a login. Hosts without `kinit` and the
local PBS container skip it; `pbs-from-s3` and `mainnet-sync` only submit and
return, so they need no renewal.

### The stage graph

`stages.s3_full_run_plan` declares the graph once; submission takes its scheduler
dependencies from it, the wait its order and its cancellation policy, and
`--dry-run` prints it.

```text
kubernetes-emulation ─┬─ coinjoin-analysis ─┬─ coinjoin-mappings ─┐
  (Kubernetes Job)    │                     └─────────────────────┤
                      └─ blocksci … ──────────────────────────────┴─ unified-report
```

Only the emulation is not a PBS job: `full-run` waits for its upload before the
first `qsub`, and `pbs-from-s3` assumes it. The two analyzers are independent;
the report depends on both and on the mappings when they are enabled. The
BlockSci part depends on `blocksci.workflow` and `blocksci.task`:

| Selection | Stages |
|---|---|
| `combined` | `blocksci`: parse and detect in one job |
| `reusable` | `blocksci-parse`, then one work stage that depends on it |
| `cached` | the work stage alone, over an index published earlier |
| task `parse` | `blocksci-parse` alone |
| task `update` | `blocksci-update` alone |

The work stage is `blocksci-analyze` for `detect` and `blocksci-<task>` for
`script`, `notebook`, `external` and `backup`. The report exists only for
`detect`, and in a `full-run` only when the baseline runs too
(`runs_unified_report`). Stage names are part of the marker and log protocol, so
runners dispatch on `StageKind`, and several names share one PBS budget
(`RESOURCE_GROUPS`).

### Waiting

`wait_for_s3_marker` is the one wait of the path
([details](pbs-stage-contract.md#s3-marker-wait-contract-wait_for_s3_marker)).
`failed` wins over `done`. The probe answers queued, running, terminal or
unknown. The execution budget starts when the job is first seen running: the
stage walltime plus one hour for a PBS job, the emulation timeout for the Job.
Time in the PBS queue only extends the start deadline and never consumes that
budget; the emulation Job has a fixed 30-minute scheduling deadline instead. After a terminal answer one more poll runs, so a marker
uploaded while the probe ran still counts. A frontend that briefly cannot spawn
`s5cmd` or `kubectl` (`EAGAIN` at its task limit) retries.

### Resume

`full-run --resume-run` (`s3_resume.py`) continues a run whose driving process
died. It never recreates the emulation: an uploaded run goes on, an active Job is
waited for, and a Job that ended or vanished without a marker has lost its data.
For the PBS part it probes every recorded job before reading the markers and
refuses when PBS cannot confirm a state. A stage with a `done` marker is kept. A
live job is adopted when it has no `failed` marker and every PBS stage it depends
on is kept or adopted; any other live job is cancelled, because it still points
at a job ID that will be replaced, and the rest of the graph is submitted again.
With the same configuration a second resume adopts what the first one submitted.
The emulator digests are not pinned again, so the report flags them as missing.

Right after its S3 access preflight a resume uploads its own launch record. It
first takes over
the launches that the copy in the prefix records and its run directory lacks
(told apart by `launch_id`), so a resume from another frontend keeps the original
launch under `earlier_host_launchers`. It uploads nothing into a prefix that
holds no object, so a mistyped run ID does not create one.

## What a run consists of

A run is the directory `<runs-root>/<run-id>/` and, on the S3 path, the prefix
`<artifact-uri>/<run-id>/` with the same relative layout. Every name below comes
from `pipeline/exporters/artifact_paths.py`.

| Path | Written by | Content |
|---|---|---|
| `research_manifest.json` | host | the launcher record of each invocation; an S3 `full-run` or `emulate` copies it to the prefix |
| `coinjoin_emulator_data/` | emulation | `scenario.json`; `data/` with the wallet and coordinator logs, `coinjoin_label_manifest.json`, `joinmarket_round_events.json`, `postmix_spends.json` and the exported blocks `btc-node/block_<height>.json` |
| `bitcoin_data/` | emulation (S3 runs) | the emulator's Bitcoin datadir, which the BlockSci parse reads |
| `coinjoin-analysis_data/` | baseline | `coinjoin_tx_info.json`, the `false_cjtxs.json*` sidecars, `dumplings-snapshot.json` of an external baseline |
| `coinjoin-analysis-tools_data/` | standalone analyzer operations | one output directory per invocation |
| `blocksci_data/` | BlockSci parse | the working index (`config.json`, `parsed/`); on the S3 path it stays on job scratch |
| `blocksci-parse_data/` | BlockSci parse (S3 runs) | the published index: archive, `.sha256`, manifest |
| `blocksci-analysis_data/` | BlockSci analysis | `blocksci_analysis.json`, the only BlockSci input of the report |
| `blocksci-custom-analysis_data/`, `blocksci-notebooks_data/` | `script` and `notebook` tasks | their outputs |
| `coinjoin-mappings_data/` | mappings | `coinjoin_mappings.json` |
| `coinjoinPipeline_data/` | report | `unified_report.json` and `.md`, `emulator_data.json`, `liquidity_dataset.json`, `liquidity_events.csv` |
| `logs/` | PBS stages | `<stage>.pbs.log`, the whole output of each job; under a fixed state prefix `logs/<run-id>/` |
| `.pbs/` | PBS stages | `<stage>.jobid`, `<stage>.done`, `<stage>.failed` |
| `.k8s/` | uploader | `upload.done`, `upload.failed` |
| `.pipeline/exporters/` | frontend (S3 prefix only) | the exporters the BlockSci and report jobs run |

On the S3 path the frontend's run directory holds only the manifest, the job IDs
and the locks, and the first two have a copy in the prefix; the artifacts are in
the bucket, and `download-report` fetches the report. The persistent index jobs use a fixed prefix (`blocksci.index_state_uri`,
`S3Target.state_prefix`) in place of `<artifact-uri>/<run-id>/`: every run
replaces the state there, refreshes the staged exporters and keeps its own logs.

A run leaves three records outside any process, and `run_records.py` reads all
three: the PBS job IDs (local, with a copy in the S3 prefix so another frontend
finds them), the Kubernetes resources labelled `coinjoin.run-id=<run-id>`, and
the terminal markers in the prefix.

Locks are advisory `flock` files with the holder's PID, non-blocking and
re-entrant within one process (`locks.command_lock_path`):

| Lock | Held by |
|---|---|
| `<run>/.pipeline.lock` | S3 `full-run` and `emulate`; runs with different IDs proceed side by side |
| `<run>/.pbs-submit.lock` | `pbs-from-s3`, `mainnet-sync`, and the submission step of a `full-run` |
| `<run>/.research.lock` | `analyze`, `export`, `coinjoin-analysis`, `mappings` with `--run-dir` |
| `<runs-root>/.pipeline.lock` | every other action |

## A PBS job on the compute node

A PBS node sees neither the frontend's checkout nor a shared run directory, so a
job script carries its own code. `templates_s3.render_python_stage` renders the
`#PBS` directives and a few lines of shell that write three things to
`$SCRATCHDIR/.stage-runtime` and start them with `python3 -s run.py settings.json`:

- the worker modules (`scripts.STAGE_RUNTIME_MODULES`), each at its path below
  `src/` or `pipeline/` with empty `__init__.py` markers (`package_files`), so
  they import each other as on the frontend;
- `settings.json`: the fields every job has (`stage` names the markers, `job` the
  function, run prefix, endpoint, credentials path, image) and those of the job,
  typed in `stage_settings.py`. It holds the path of the credentials file, never
  the keys;
- `run.py`, which calls `stage_runtime.main`.

`stage_runtime` looks the job up in `JOBS` and runs it over a `Stage`, which owns
the run tree on scratch, the S3 prefix and the image command. The frame is the
same for every job: download the inputs from the run prefix, run the image under
Apptainer, upload the outputs, then the finalizers (the job log, the release of
an index lock) whatever the outcome, then the terminal marker, then an emptied
scratch. A failure of any kind, `SIGTERM` at the walltime included, still
publishes `failed`; a `done` marker that cannot be uploaded turns into a failure.
The whole output also goes to `logs/<stage>.pbs.log` in the prefix, so no stage
depends on PBS copying its output file back.

BlockSci and report jobs take the exporters from `.pipeline/exporters/` of their
own prefix, so every stage of a run uses the tree its first stage staged.
`ensure_staged_exporters` uploads only into a prefix that has none and refuses a
partial tree instead of mixing versions.

The modules a job ships (`scripts.STAGE_RUNTIME_MODULES`,
`mainnet_sync.MAINNET_RUNTIME_MODULES` and the extra helpers) are linted as
Python 3.8 through ruff's `per-file-target-version`;
`tests/unit/test_shipped_modules.py` fails when a shipped module is missing there.

## One analyzer/report flow

For emulator comparisons, both analyzers produce artifacts; the report depends
on both. Mappings, when enabled, depends on baseline analysis and also gates the
report. Serial execution uses one worker; parallel execution allows several
ready stages. Report is an ordinary node executed by the same executor.

The S3 frontend submits `PBSJobSpec(stage, script, dependencies)` through one
scheduler boundary. The ordered graph supplies dependencies. Before submission,
each stage's stale terminal markers are cleared; job IDs are recorded immediately
for overlap detection and rollback. Shared-storage failure cancels running
siblings; S3 wait failure cancels only dependent jobs. These policies remain
explicit and distinct.
Stage adapters call their script renderers directly and pass a completed
`PBSJobSpec` to submission. PBS worker commands consume the typed configuration;
`pbs/commands.py` renders detector arguments for both analysis and report phases.
`pbs/analysis_modes.py` prepares each cached-index consumer's inputs, mounts and
output checks; `templates_s3.py` adds the shared cache/index access and job lifecycle.
The BlockSci job renderers take the validated `BlockSciConfiguration` itself and
write its values into `settings.json`, whose parts are typed in `stage_settings.py`.
They do not repeat the rules of `option_rules.py`; what they still check is what
only submission can know or what protects the job script: shared-storage paths
that must exist, S3 URIs no configuration validator covers, and values
interpolated into the script or its `#PBS` directives.

`worker.py` supports parse, update, analyze, report, or parse+analyze (`run`).
Docker and PBS choose mounts and interpreter, then invoke this same payload.
The report always consumes `blocksci-analysis_data/blocksci_analysis.json`.
It never performs an implicit detector scan. The reader accepts only artifact
schema 1.3, which records the mode and preserves detector parameters, the
deprecated JoinMarket subset-matching scan, diagnostics and clustering; older runs
need `cjp analyze` again.

`exporters/cli.assemble_report` joins three sources. The baseline
(`coinjoin_tx_info.json`, less the false-positive sidecars) is normalized. In
emulator mode `emulator_data.py` builds the ground truth from the exported blocks
and the verified producer-label manifest; a label source that fails verification
makes the labels unavailable for the whole run instead of partly trusted. The
BlockSci artifact supplies the detector records, clustering and mix events.
`report_builder.build_report` compares them per transaction, and
`markdown_report.py` renders the same document. The report hashes its inputs
(`artifact_paths.REPORT_INPUT_FILES`), so a later change to any of them marks it
stale. [Analysis semantics](analysis-semantics.md) defines the labels,
thresholds and report keys.

All S3 parsers publish `blocksci-parse_data/` as a zstd archive, manifest and
SHA-256; `combined` and `reusable` differ only in job arrangement. The working
`blocksci_data/` stays on job scratch. `S5Client` shares the streaming
upload/restore with the persistent index backup and the mainnet checkpoints;
uploads need `s5cmd pipe` (2.2 or later), which the PBS job checks before it packs anything; the frontend
never streams an archive and needs 2.1 for `sync --exclude`. Consumers run only after verification.
Persistent mainnet indexes still stay in shared storage and are read/updated in
place; emulations do not introduce a persistent working copy. Their lock
(`<index>/.lock`, one writer or many readers, each entry with its job ID and an
expiry past the job's walltime) is shared by the S3 index jobs, the mainnet
synchronization and scenario-lab's multijob cleanup.

A parse from a block archive in S3 (`blocksci.bitcoin_blocks_uri`) goes through
`execution/block_archive.py`: it reads the `blkNNNNN.dat.json` sidecars, selects
only the block files the requested height needs, and verifies size, SHA-256 and
height coverage before BlockSci runs. Coverage is a preflight, not proof of a
connected chain; `exporters/verify_chain.py` checks the parsed index before the
job publishes it.

The host requires Python 3.10+. Exporter code retains Python 3.8 syntax for the
BlockSci image. Their input options share `exporters/parameters.py`; exporters
are staged as a standalone tree and do not import the host package.

Standalone [coinjoin-analysis operations](coinjoin-analysis-operations.md) select
the analyzer's existing public commands through `coinjoin_analysis.*`.
`execution/coinjoin_tools.py` owns their CLI arguments and input rules; the S3
runner uses the ordinary analysis job lifecycle. These jobs retain separate
invocation outputs in `coinjoin-analysis-tools_data/` and do not replace the
comparison baseline or run the report. Local Docker/Podman uses the same CLI
arguments on a copy of its input dataset.
The optional research adapters invoke existing functions for transaction size,
labeled emulation sequences and mainnet coordinator refresh. They are delivered
with each invocation rather than rebuilt into the analyzer image. Data-origin
validation keeps wallet-ground-truth operations in emulation and public-chain
lookups/coordinator refresh in mainnet.

## Images and their digests

Each component image is its explicit option, else its environment variable, else
the `--local-build` tag, else the release tag `--version`, in the configured
registry (`images.resolve_images`). The uploader and the report image are not
versioned with the others: their references are committed in
`container/uploader.image` and `container/unified-report.image`. A reference with
a real transport (`docker-archive:`, `oras://`) is passed to Apptainer as it is;
a registry reference gets the `docker://` prefix (`pbs_settings.py`).

On the S3 path nothing can inspect an image afterwards: the MetaCentrum frontend
and PBS nodes have no Docker. `registry.py` therefore pins each tag to its current
registry digest at submission, and the jobs run that `name@sha256:` reference, so
the report reads the digest straight from it. `full-run` also pins the emulator and
uploader of its Kubernetes Job. A separate `pbs-from-s3` does not know what the
emulation ran, so it keeps those tags and the report flags their digests as missing.
Index manifests keep the configured BlockSci name (`image_name` in
`settings.json`, `blocksci_image` in the manifest), so a rebuild under the same
tag still reuses an existing index. A failed lookup only warns and
runs the tag.

The pipeline itself is published as
[the runtime image](runtime-image.md) after the tests pass on `main`.

## Mainnet synchronization

`mainnet-sync` is the second consumer of the PBS layer and shares no data with
the emulation runs. `mainnet_sync.submit_sync` submits up to six jobs, each
`afterok` the previous one: `mainnet-restore`, `-producer` (Bitcoin Core and
Dumplings), `-blocksci`, `-checkpoint`, `-pack` and, unless `mainnet.clear_after_sync` is
off, `-clear`.
They ship `MAINNET_RUNTIME_MODULES` the same way as the S3 stages, work in place
on one multijob scratch, and keep single backups in S3 in the format of
`s5client.py`. Submission records every job ID at once and rolls back a partial
chain. [Manual mainnet synchronization](mainnet-synchronization.md) describes the
jobs, the working data and the failure rules.

## Operating a run

These utilities take no experiment configuration; each parses its own arguments
and reads the run's records.

- `status` is read-only: the recorded jobs with their scheduler state from one
  `qstat`, the emulation Job by run label, and with `--markers` the upload and
  stage markers. `--s3-job-ids` reads the job IDs from the run prefix when the
  local record is missing.
- `stop` deletes the run's labelled Kubernetes resources and cancels every
  recorded PBS job; `--save-pod-logs` keeps the pod output first. It does not
  signal a process that still drives the run, which would report the cancelled
  stages as failures.
- `watch` follows the controller, uploader and engine pods, the PBS job output
  and the frontend transcript in one stream.
- `download-report` fetches a finished report; `clean-s3` deletes the artifacts
  of one run or of a whole run root after a confirmation.
- `full-run --resume-run` is described under [Resume](#resume).

Against MetaCentrum each of them reaches Kubernetes, PBS or S3.

## Verification boundary

Focused tests use fake scheduler, process and storage boundaries. They verify
configuration errors, graph order, rollback/cancellation, job persistence,
rendered shell behavior, artifact validation and report calculations. They do
not prove a live emulator, image, parser or MetaCentrum run.

`tests/pipeline/test_rendered_jobs.py` renders every S3 example in `examples/`
and a few index-job configurations through the real renderers and compares each
job's `settings.json` and `#PBS` directives with `tests/pipeline/rendered_jobs/`.
A change in how an option reaches a job shows up there as a diff; after an
intended one, refresh the stored files with `UPDATE_RENDERED_JOBS=1` and review
them. `tests/unit/test_index_job_rules.py` names the configuration rule each
unsupported index job fails on. `tests/unit/test_run_layout_json.py` keeps the
exported layout in step with the modules that define it.

The live paths are covered by the local suite (`run-all.sh local`): Docker,
k3d, shared-storage PBS and Kubernetes → MinIO → PBS end-to-end tests. The
`Tests` workflow runs the same groups as separate jobs, among them
`kubernetes-s3-minio` (the thesis path against k3d, MinIO and a local OpenPBS
with Apptainer), `bitcoin-block-archive-s3-minio` and the bounded
`mainnet-sync-bounded`. A pass there is evidence for that rig, not for
MetaCentrum. Historic reports retain their original provenance; new exporter
source hashes differ.

## Experimental paths

Kept for local development and reproduction, not read for the thesis review:

| Path | Modules |
|---|---|
| Dispatch of every non-S3 action | `execution/local.py` |
| Local Docker/Podman commands and Compose | `execution/containers.py`, `runtime.py`, `pipeline/compose.yaml`, `pipeline/*.sh` |
| Kubernetes emulation without S3 | `execution/kubernetes_launch.py` |
| Shared-storage PBS stages and graph | `execution/shared_storage_pbs.py`, `workflow.py`, `pbs/submission_local.py`, `pbs/templates_local.py`, `pbs/templates/local_*.sh` |
| `runs`, `scenarios` and `external-analyze` commands | `execution/research.py`, `run_catalog.py` |

A few modules without the header serve mostly these paths, because the S3 path
imports a part of each: `stage_executor.py` (the bounded-concurrency executor;
only `local.py` and `workflow.py` use it), `stages.analysis_plan` (the local
graph), `run_context.py` (run-directory discovery; the S3 path uses it only for
the `--run-dir` lock), `settings.py` (Compose and container path constants) and
the stage logs of `pipeline_logging.py`.

## Related documents

- [PBS stage contract](pbs-stage-contract.md): marker files, both waits, the
  requirements checked before submission, cancellation.
- [Analysis semantics and provenance](analysis-semantics.md): labels, thresholds,
  detector APIs, report keys and statuses, liquidity evaluation.
- [Existing coinjoin-analysis operations](coinjoin-analysis-operations.md).
- [Manual mainnet synchronization](mainnet-synchronization.md).
- [Runtime image](runtime-image.md).
- `README.md`: installation, commands and the S3 workflows from the operator's
  side. `TODO.md` and [refactor follow-ups](refactor-followups.md): open work and
  the fixes a rewrite must keep.
