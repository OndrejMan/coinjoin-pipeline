# PBS stage contract

How the pipeline frontend and PBS compute jobs coordinate. The implementation
lives in `pipeline/client/pbs.py` and the `pipeline/client/*_template.sh`
scripts; this page documents the on-disk protocol so that changes on either
side stay compatible.

## Stages

| Stage name          | Submitted by                          | Template                          |
|---------------------|---------------------------------------|-----------------------------------|
| `blocksci`          | `--blocksciPbs`                       | `blocksci_template.sh`            |
| `coinjoin-analysis` | `--analysisPbs`                       | `coinjoin_analysis_template.sh`   |
| `coinjoin-mappings` | `--mappingsPbs`                       | `mappings_template.sh`            |
| `unified-report`    | parallel mode after both analyzers    | `blocksci_template.sh` (report-only command) |
| S3 analyzer variants | `pbs-from-s3`, `full-run --artifact-backend s3` | `coinjoin_analysis_s3_template.sh`, `blocksci_s3_template.sh` |
| S3 `coinjoin-mappings` | Wasabi `--mappingsPbs` | `mappings_s3_template.sh` |
| S3 `blocksci-parse` | reusable BlockSci workflow | `blocksci_parse_s3_template.sh` |
| S3 `blocksci-update` | versioned incremental external-Bitcoin cache update | `blocksci_update_s3_template.sh` |
| S3 cached work | `detect`, `script`, or `notebook` over a reusable parse | `blocksci_analyze_s3_template.sh` |
| S3 `unified-report` | both S3 analyzer flags, after both jobs | `unified_report_s3_template.sh` |

## Marker files

All coordination state lives in the run directory under `.pbs/`:

- `.pbs/<stage>.pbs` — the rendered job script (kept for reproducibility).
- `.pbs/<stage>.jobid` — the `qsub` job id, written right after submission.
- `.pbs/<stage>.done` — written by the job's `on_exit` trap on exit status 0.
- `.pbs/<stage>.failed` — written by the trap on any non-zero exit status.

Shared-storage scripts remove stale local `done`/`failed` markers at startup.
Reusable/cached S3 BlockSci jobs remove only their two exact remote stage
marker keys before starting, so repeated work over one cache starts clean.
The original S3 full-run still relies on its enforced empty, unique run prefix.
S3 jobs upload markers to `<artifact-uri>/<run-id>/.pbs/` instead of writing
them locally.

For `pbs-from-s3 --analysisPbs --blocksciPbs`, the analyzer jobs have no
dependency on each other. The report-only job uses
`afterok:<coinjoin-analysis-job>:<blocksci-job>` and therefore runs only after
both analyzers have uploaded their S3 outputs successfully. The BlockSci job
persists `blocksci-analysis_data/blocksci_analysis.json` containing normalized
detector records, integration diagnostics, skipped txids, and address-cluster
assignments. The report job consumes that artifact and does not load BlockSci,
its parsed index, or raw Bitcoin data. A blocksci-only S3 submission keeps the
combined parser-and-report behavior for compatibility.

For Wasabi 2 with `--mappingsPbs`, the S3 mappings job downloads
`coinjoin-analysis_data/coinjoin_tx_info.json`, preserves the shared-storage
enumerator/Sake partial-result semantics, and uploads
`coinjoin-mappings_data/`. When coinjoin-analysis is submitted in the same
command, mappings uses `afterok:<coinjoin-analysis-job>`. When BlockSci is also
selected, report assembly is decoupled and depends on coinjoin-analysis,
BlockSci, and mappings so `coinjoin_mappings.json` is included in the report.
A mappings-only `pbs-from-s3` submission consumes an existing baseline and does
not create a report.

With `--blocksci-workflow reusable`, the `blocksci-parse` job downloads raw
Bitcoin data, parses once, and uploads
`blocksci-parse_data/{blocksci_data.tar.gz,blocksci_data.tar.gz.sha256,manifest.json}`.
The dependent `blocksci-analyze` job verifies and extracts that archive before
running detector queries; it never invokes `blocksci_parser`. The report job
depends on `blocksci-analyze`, not on `blocksci-parse` directly. With
`--blocksci-workflow cached`, the parse job is omitted and the work job fails
closed if the archive or checksum is absent or invalid.

`--blocksci-workflow cached --blocksci-task update` is the only S3 parser
resume path. It requires a source cache run, a different fresh target run,
and an external Bitcoin coin directory under `/storage`. Before submission,
the frontend verifies that the source manifest exists and that the entire
target run prefix is empty. The `blocksci-update` job then verifies the source
manifest and archive checksum, requires matching BlockSci image and network,
extracts the index, rewrites its run-local parsed-data path and inclusive
maximum height, and invokes only `blocksci_parser ... update`—never
`generate-config`. On success it publishes a new schema-1.0 cache whose
manifest records `cache_operation`, `source_run_id`, and
`source_exported_max_block`. The source run is never modified. A failed target
is not reusable as a target; choose another fresh `--run-id` after diagnosis.

For parse-only `pbs-from-s3` submissions, the producer may replace emulator
inputs with exactly one shared-storage source. An external Bitcoin source is a
coin directory under `/storage` containing `blocks/`; the caller supplies a
BlockSci network and inclusive maximum height. An external BlockSci source is
a directory under `/storage` containing `config.json` and
`parsed/chain/block.dat`; it is copied into the run layout and its
`chainConfig.dataDirectory` is canonicalized before archiving. The cache
manifest records `source_kind` and `network`. Cached `script` and `notebook`
jobs download only this cache, while `blocksci-analyze` also downloads the
emulator and exporter inputs required by the standard detector contract.

`--blocksci-task parse` submits only the cache producer. `script` and
`notebook` submit `blocksci-script` and `blocksci-notebook` work jobs and do
not submit a unified report. A custom script is bound read-only from shared
`/storage` and its output is uploaded under
`blocksci-custom-analysis_data/`. The notebook is intentionally a long-lived
independent PBS job; its termination marker describes notebook shutdown, not
completion of the standard analysis graph.

PBS resources resolve independently for each stage. The
`--pbs-{blocksci,analysis,mappings}-{ncpus,mem,scratch,walltime}` options take
precedence for their stage, followed by the backward-compatible shared
`--pbs-{ncpus,mem,scratch,walltime}` fallback, followed by the existing stage
default. The S3 report-only job has independent defaults: 2 CPUs, 8 GB RAM,
10 GB scratch, and a 1-hour walltime. Report-specific
`--pbs-unified-report-*` options use the same precedence over the shared
fallback.

## Frontend waiting (`wait_for_pbs_marker`)

The frontend polls every `POLL_INTERVAL_SECONDS` (30 s):

1. `failed` marker exists → raise `PBSError` (stage failed).
2. `done` marker exists → stage finished successfully.
3. Deadline exceeded → raise. The timeout is the stage walltime plus one hour
   of queue margin (`pbs_wait_timeout`).
4. If a job id is known, `qstat -x -f <jobid>` is consulted as a fallback:
   a terminal state (`C`/`F`) or a missing job without a marker means the job
   ended without writing its marker, which is treated as a failure.

Robustness rules:

- A non-zero `qstat` exit only counts as job death when stderr explicitly says
  the job is unknown/finished; any other failure (PBS server restart, network
  hiccup) is inconclusive and polling continues.
- Markers are written to shared storage by the compute node, and NFS attribute
  caching can briefly show a finished (`F`) job before its `done` marker is
  visible; the frontend therefore waits one extra poll cycle after seeing a
  terminal state before declaring "ended without marker".

## S3 marker wait contract (`wait_for_s3_marker`)

`full-run --artifact-backend s3` is the only frontend-side consumer of the
markers uploaded to the bucket. It polls these exact keys every 30 s with
`s5cmd ls` (credentials file + profile + endpoint from the CLI, AWS_* env vars
scrubbed first):

- `<artifact-uri>/<run-id>/.k8s/upload.done` | `.k8s/upload.failed`
  — written by the Kubernetes uploader container.
- `<artifact-uri>/<run-id>/.pbs/coinjoin-analysis.done` | `.failed`
- `<artifact-uri>/<run-id>/.pbs/coinjoin-mappings.done` | `.failed`
- `<artifact-uri>/<run-id>/.pbs/blocksci.done` | `.failed`
- `<artifact-uri>/<run-id>/.pbs/blocksci-parse.done` | `.failed`
- `<artifact-uri>/<run-id>/.pbs/blocksci-update.done` | `.failed`
- `<artifact-uri>/<run-id>/.pbs/blocksci-analyze.done` | `.failed`
- `<artifact-uri>/<run-id>/.pbs/blocksci-script.done` | `.failed`
- `<artifact-uri>/<run-id>/.pbs/blocksci-notebook.done` | `.failed`
- `<artifact-uri>/<run-id>/.pbs/unified-report.done` | `.failed`
  — uploaded by the S3 PBS job traps. A full run returns successfully only
  after the report marker is present.

Semantics mirror `wait_for_pbs_marker`: `failed` raises, `done` returns, the
deadline raises (emulation: `--emulation-timeout`, default 21600 s; PBS
stages: walltime + one hour). A liveness *probe* replaces the local qstat
fallback — `qstat -x -f` for PBS stages, `kubectl get job -o json` for the
emulation Job — and a terminal probe report without a marker fails the stage
after one extra grace cycle (marker uploads race job termination). Probe
errors are inconclusive and polling continues.

Extra rules for the S3 chain:

- Before submitting anything, the orchestrator refuses run prefixes that
  contain any object. This prevents stale partial artifacts as well as stale
  markers from being merged into a reused `--run-id`.
- Full S3 runs require both analyzer stages and an existing namespace selected
  with `--reuse-namespace`; the S3 credentials Secret must predate the Job.
- S3 job scripts are submitted to `qsub` via **stdin**
  (`submit_pbs_text`) — there is no shared run directory to hold a script
  file, and no `.jobid` file is persisted; job ids live in orchestrator
  memory and are printed at submission.
- When either analyzer wait fails, the orchestrator `qdel`s the dependent
  unified-report job so it does not stay held forever.
- When the emulation wait fails, PBS is never submitted; `kubectl describe`
  and container logs are printed and the Kubernetes resources stay in place
  for inspection.

## Requirements checked before submission

- The run directory, logs root, Bitcoin datadir, and exporters directory must
  resolve under `/storage/` (MetaCentrum shared storage).
- The BlockSci stage requires the Bitcoin datadir to contain `regtest/blocks`.
- The mappings stage requires
  `coinjoin-analysis_data/coinjoin_tx_info.json` to already exist.
- `qsub` must be on `PATH` (a MetaCentrum frontend, or the local shims from
  `scripts/pbs-env.sh` in the meta-repo).

## Cancellation

When one stage of a parallel run fails, the frontend calls `qdel` on the other
still-running stages using the persisted `.pbs/<stage>.jobid`
(`qdel_pbs_stage`).
