# PBS stage contract

How the pipeline frontend and PBS compute jobs coordinate. The implementation
lives in `src/coinjoin_pipeline/execution/pbs/`, with the job scripts in its
`templates/`; this page documents the on-disk protocol so that changes on either
side stay compatible.

Every S3 job is `directives()` (`pbs/scripts.py`), the stage identity
(`ARTIFACT_URI`, `RUN_ID`, credentials, `STAGE`, `IMAGE`), `templates/s3_stage.sh`
and then its own template. `s3_stage.sh` defines the helpers every job uses: `s5`
(s5cmd with the stage credentials and no `AWS_*` variable of the node),
`require_file`, `download_optional` (sync a run directory only when it exists),
`emulator_max_block`, `locate_emulator_bitcoin_datadir`, `require_zstd`,
`canonicalize_index`, the marker publishers and a no-op `stage_finalize` that a
template redefines when it publishes outputs even on failure. BlockSci templates
also get `templates/index_archive.sh`. `{start}` installs `templates/stage_trap.sh` and
runs `templates/s3_setup.sh`. Shared-storage jobs use `templates/local_preamble.sh`
instead; both mappings jobs share `templates/mappings_run.sh`.

## Stages

| Stage name          | Submitted by                          | Template                          |
|---------------------|---------------------------------------|-----------------------------------|
| `blocksci`          | `--blocksciPbs`                       | `local_blocksci.sh`               |
| `coinjoin-analysis` | `--analysisPbs`                       | `local_coinjoin_analysis.sh`      |
| `coinjoin-mappings` | `--mappingsPbs`                       | `local_mappings.sh`               |
| `unified-report`    | serial or parallel, after analyzers    | `local_blocksci.sh` (report-only command) |
| S3 analyzer variants | `pbs-from-s3`, `full-run --artifact-backend s3` | `s3_coinjoin_analysis.sh`, `s3_blocksci.sh` |
| S3 mainnet baseline | `pbs-from-s3 --analysisPbs --dumplings-uri` | `s3_coinjoin_analysis_dumplings.sh` |
| S3 `coinjoin-mappings` | Wasabi `--mappingsPbs` | `s3_mappings.sh` |
| S3 `blocksci-parse` | reusable BlockSci workflow | `s3_blocksci_parse.sh` |
| S3 `blocksci-update` | versioned incremental cache update from a coin directory or the block archive | `s3_blocksci_update.sh` |
| S3 cached work | `detect`, `script`, `external` or `notebook` over a reusable parse | `s3_blocksci_analyze.sh` |
| S3 `blocksci-backup` | `--blocksci-task backup` of a persistent index | `s3_blocksci_backup.sh` |
| S3 `unified-report` | BlockSci detect selected, after selected analyzers; a `full-run` also needs coinjoin-analysis | `s3_unified_report.sh` |

## Marker files

All coordination state lives in the run directory under `.pbs/`:

- `.pbs/<stage>.pbs` — the rendered job script (kept for reproducibility;
  shared-storage jobs only, S3 jobs are piped to `qsub`).
- `.pbs/<stage>.jobid` — the `qsub` job id and a newline, written atomically
  right after submission. Watch, overlap detection, cancellation and
  scenario-lab read it; it is never pruned.
- `.pbs/<stage>.done` — written by the job's `on_exit` trap on exit status 0.
- `.pbs/<stage>.failed` — written by the trap on any non-zero exit status.

Shared-storage scripts remove stale local `done`/`failed` markers at startup.
For S3 submissions, the frontend strictly removes each stage's two exact
remote marker keys immediately before `qsub`; a missing object is accepted,
but permission, endpoint, or failed-deletion errors abort submission. PBS
compute jobs never remove remote markers, avoiding a queued old attempt
deleting a newer attempt's marker. S3 jobs upload markers to
`<artifact-uri>/<run-id>/.pbs/` instead of writing them locally.

For `pbs-from-s3 --analysisPbs --blocksciPbs`, the analyzer jobs have no
dependency on each other. The report-only job uses
`afterok:<coinjoin-analysis-job>:<blocksci-job>` and therefore runs only after
both analyzers have uploaded their S3 outputs successfully. The BlockSci job
persists `blocksci-analysis_data/blocksci_analysis.json` containing normalized
detector records, integration diagnostics, skipped txids, and address-cluster
assignments. The report job consumes that artifact and does not load BlockSci,
its parsed index, or raw Bitcoin data. A blocksci-only S3 submission also submits
a report job after BlockSci, consuming the baseline already stored in that run.
The artifact reader accepts schema 1.0 (emulator mode) and 1.1 (explicit mode).
Parse and update jobs now stage and mount the shared `exporters/worker.py` payload.
The S3 exit trap is `templates/stage_trap.sh`; finalization, failed transfers
and signals preserve a nonzero exit status, and terminal markers follow outputs.
After the markers are published the trap empties `$SCRATCHDIR` with MetaCentrum's
`clean_scratch`, or by deleting its contents where that tool is missing.

For Wasabi 2 with `--mappingsPbs`, the S3 mappings job downloads
`coinjoin-analysis_data/coinjoin_tx_info.json`, preserves the shared-storage
enumerator/Sake partial-result semantics, and uploads
`coinjoin-mappings_data/`. When coinjoin-analysis is submitted in the same
command, mappings uses `afterok:<coinjoin-analysis-job>`. When BlockSci is also
selected, report assembly is decoupled and depends on coinjoin-analysis,
BlockSci, and mappings so `coinjoin_mappings.json` is included in the report.
A mappings-only `pbs-from-s3` submission consumes an existing baseline and does
not create a report.

Every S3 parser publishes
`blocksci-parse_data/{blocksci_data.tar.zst,blocksci_data.tar.zst.sha256,manifest.json}`;
there is no loose `blocksci_data/` upload. `combined` parses and analyzes in one
job, while `reusable` publishes the index from a separate `blocksci-parse` job
before dependent analysis. Both use the same schema-1.1 manifest and archive,
compressed with `zstd -3` using the job's requested `ncpus` threads.
The dependent `blocksci-analyze` job never invokes `blocksci_parser`.
The report job depends on `blocksci-analyze`, not on `blocksci-parse` directly.
With `cached`, parsing is omitted and the saved index is restored.

`index_archive.sh` is the shared transport used by both run-local indexes and
the persistent mainnet backup. Upload hashes the compressed stream and uses
`s5 pipe` when available, with a temporary scratch archive as the existing
fallback. The checksum uploads after the archive and the manifest uploads last.
Before replacing an index, the old completion manifest is removed. Restore
fetches only metadata first, then streams the archive into scratch while hashing
and extracting it. An incomplete marker remains until the complete stream and
required index files are verified. No analysis or notebook starts on failure.
No emulation index is retained on multijob storage; persistent mainnet index
locations, locking, update and backup lifecycle remain unchanged.

`--blocksci-workflow cached --blocksci-task update` is the only S3 parser
resume path. It requires a source cache run, a different fresh target run,
and the source cache's block source: an external Bitcoin coin directory under
`/storage` for an `external-bitcoin` cache, or the `bitcoin-block-archive` S3
prefix for a `bitcoin-blocks-s3` cache (BlockSci remembers each block by file
number and offset, which only that source resolves). Before submission,
the frontend verifies that the source manifest exists and that the entire
target run prefix is empty. The `blocksci-update` job then verifies the source
manifest and archive checksum, requires matching BlockSci image and network,
extracts the index, rewrites its run-local parsed-data path and inclusive
maximum height, and invokes only `blocksci_parser ... update`—never
`generate-config`. On success it publishes a new schema-1.1 cache whose
manifest records `cache_operation`, `source_run_id`, and
`source_exported_max_block`. From the block archive the job downloads every
sidecar, refuses the update unless the digest of the files the source cache
scanned (`block_archive_index_sha256`, through `block_archive_last_file`) still
matches, and downloads only that last file onwards plus older files holding a
height above the source maximum. The source run is never modified. A failed target
is not reusable as a target; choose another fresh `--run-id` after diagnosis.

The `script`, `external`, and `notebook` tasks of the `cached` workflow may read
another run's cache read-only with the same `--blocksci-cache-source-run-id`.
The frontend applies the same preflight (source manifest present, target
prefix empty); the job streams the source archive, requires its
manifest `run_id` to be the source run, and writes outputs, `.pbs` markers,
logs, and a copy of the source manifest (`blocksci-cache-manifest.json` in the
output directory) under the target run only. `detect` cannot use a source cache
because it reads the emulator data of its own run. For the `script` task,
`blocksci.pin_height` and the half-open `blocksci.scan_start_height` /
`blocksci.scan_end_height` are exported as `BLOCKSCI_MAX_BLOCK`
(`pin_height + 1`, the block count `Blockchain(config, n)` opens),
`SCAN_START_HEIGHT`, and `SCAN_END_HEIGHT`.

A persistent index replaces the S3 cache for mainnet: `blocksci.index_dir`
(`--blocksci-index-dir`) names a working copy under `/scratch/multijob-<location>/`
or `/storage/`, optionally with an S3 backup prefix `blocksci.index_backup_uri`
(`--blocksci-index-backup-uri`). A working copy on multijob scratch requires
`pbs.blocksci.multijob: <location>`, which adds `:multijob=<location>` to the
job's `select`. `parse` (from a block archive or an external Bitcoin datadir)
parses straight into the directory and refuses one that already holds an index;
`update` needs no `--blocksci-cache-source-run-id` and updates in place; `backup`,
`script`, `external`, and `notebook` read it. Every job binds the directory at its own
path, links `parsed/` into the run and keeps `config.json` run-local
(`execution/pbs/templates/index_store.sh`).

Writers (`parse`, `update`) take an exclusive lock and readers (`backup` and the
analyses) a shared one,
held as directories under `<index>/.lock` that are renamed into place (atomic on
NFS and BeeGFS without `flock`). Each entry holds the PBS job ID and an expiry
600 s past the walltime, after which another job clears it. A job waits up to
half its walltime for the lock, then fails. A copy is complete when it has no
`.writing` marker and its files match `files.txt`; writers set `.writing` until
they record `config.json`, `manifest.json` (the S3 cache fields an update reads,
with `"storage": "persistent"`) and `files.txt`. `parse` refuses a directory
with a manifest and a prefix that already holds a backup. An `update` first
reconciles: a missing or incomplete working copy is replaced by the backup,
streamed from S3, checked against its SHA-256 and then against `files.txt`.
Readers never restore; they fail and name the update that does.

The prefix holds one backup: `<index_backup_uri>/blocksci_index.tar.zst`
(`config.json`, `files.txt`, `manifest.json`, `parsed/`), its `.sha256`, and
`<index_backup_uri>/manifest.json`. A `backup` job first deletes the manifest
and the old archive, so S3 never holds two copies and there is no valid backup
until it finishes. The archive is streamed `tar | zstd` into `s5cmd pipe` (an
s5cmd without `pipe` stages it in scratch); its S3 size must match the uploaded
stream before the manifest is written: the index manifest plus `archive_uri`,
`archive_sha256`, `archive_size`, `upload_validation` and `uploaded_at`. The
archive is not read back. A restore reads that manifest and verifies the
SHA-256 while extracting. The job records the index manifest as `blocksci-index-backup.json`
in its run. Neither `parse` nor
`update` writes a backup. Analyses copy the index manifest next to their outputs as
`blocksci-index-manifest.json`. `tests/test-persistent-index-local.sh` runs the
rendered jobs on one host with Docker in place of Singularity.

For mainnet, `dumplings_uri` (`--dumplings-uri`, with `--analysisPbs`) replaces
the emulator input of the `coinjoin-analysis` stage. The job reads
`<dumplings_uri>/manifest.json` (written last by mainnet-sync, so a copy being
replaced has none), downloads `<dumplings_uri>/Scanner/` into scratch, checks
the file sizes against the manifest and deletes stale `*.txt.json` caches, which
coinjoin-analysis would otherwise read in place of the Dumplings files. It then
runs `parse_dumplings --action process_dumplings` (`ww2` → `Scanner/wasabi2`,
`jm` → `Scanner/joinmarket_all`) and uploads `coinjoin_tx_info.json`, the
image's curated `false_cjtxs.json` and `dumplings-snapshot.json` to
`coinjoin-analysis_data/`. The external report consumes them through
`--external-baseline-uri` and `--external-false-cjtxs-uri`. With
`blocksci.pin_height` and the scan range, the external report scans only that
window, looks up every baseline transaction's height in BlockSci (Dumplings
records hold the position in the block, not the height) and compares only the
baseline inside the window; `coverage` in the report counts the rest.
`tests/test-dumplings-baseline-local.sh` runs the rendered baseline job locally.

For parse-only `pbs-from-s3` submissions, the producer may replace emulator
inputs with exactly one source. An external Bitcoin source is either a coin
directory under `/storage` containing `blocks/`, or a
`bitcoin-block-archive` S3 prefix containing `blk*.dat` files, each with its
schema-1 `blkNNNNN.dat.json` sidecar. For the latter the job downloads every
sidecar, then only the file prefix holding heights up to the requested maximum
plus six (`execution/block_archive.py`), and fails closed unless the files are
contiguous from `blk00000.dat`, every size and SHA-256 matches its sidecar, and
the sidecars' `height_ranges` cover every height from 0 through the requested
inclusive maximum. These ranges do not prove a connected or canonical chain.
After parsing and before creating/uploading the cache tarball,
`exporters/verify_chain.py` opens the actual index with no height limit and
requires exactly `max_block + 1` blocks. It compares the target block hash to
`--blocksci-expected-block-hash` (`blocksci.expected_block_hash` in YAML),
which is mandatory for mainnet archive sources and must come from a trusted
source such as `bitcoin-cli getblockhash HEIGHT`. The PBS job makes no network
request for the checkpoint. A short/overlong index or hash mismatch fails the
stage instead of publishing a cache. Its manifest records the observed
`exported_max_block` / `exported_block_hash` and adds `block_archive_last_file`
and `block_archive_index_sha256` for a later update. Updates require a source
cache with a recorded block hash and verify that the new index preserves that
hash at the source height. Older archive caches without this hash require a
fresh parse. An external BlockSci source is a
directory under `/storage` containing `config.json` and
`parsed/chain/block.dat`; it is copied into the run layout and its
`chainConfig.dataDirectory` is canonicalized before archiving. The cache
manifest records `source_kind` and `network`. Cached `script` and `notebook`
jobs download only this cache, while `blocksci-analyze` also downloads the
emulator and exporter inputs required by the standard detector contract.

`--blocksci-task parse` submits only the cache producer. `script` and
`notebook` submit `blocksci-script` and `blocksci-notebook` work jobs and do
not submit a unified report. `external` downloads a Dumplings
`coinjoin_tx_info.json` from `--external-baseline-uri`, reuses a verified cache,
and writes an external-mode unified report. It compares agreement with that
baseline; it never claims emulator ground-truth precision/recall. A custom
script is bound read-only from shared
`/storage` and its output is uploaded under
`blocksci-custom-analysis_data/`. The notebook is intentionally a long-lived
independent PBS job; its termination marker describes notebook shutdown, not
completion of the standard analysis graph.

PBS resources resolve independently for each stage. The
`--pbs-{blocksci,analysis,mappings}-{ncpus,mem,scratch,scratch-type,walltime}`
options take precedence for their stage, followed by the backward-compatible
shared `--pbs-{ncpus,mem,scratch,scratch-type,walltime}` fallback, followed by
the existing stage default. `scratch_type` picks the `#PBS -l` scratch resource:
`ssd` (`scratch_ssd`, the default) or `local` (`scratch_local`, a spinning disk
on most MetaCentrum nodes); `$SCRATCHDIR` points at whichever PBS granted. The S3 report-only job has independent defaults: 2 CPUs, 8 GB RAM,
10 GB scratch, and a 1-hour walltime. Report-specific
`--pbs-unified-report-*` options use the same precedence over the shared
fallback.

## Frontend waiting (`wait_for_pbs_marker`)

The frontend polls every `POLL_INTERVAL_SECONDS` (30 s):

1. `failed` marker exists → raise `PBSError` (stage failed).
2. `done` marker exists → stage finished successfully.
3. Deadline exceeded → raise. The timeout is the stage walltime plus one hour
   of queue margin (`pbs_wait_timeout`).
4. If a job id is known, `qstat -x -f <jobid>` (or `qstat -f` where `-x` is
   refused) is consulted as a fallback:
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
- `<artifact-uri>/<run-id>/.pbs/blocksci-external.done` | `.failed`
- `<artifact-uri>/<run-id>/.pbs/blocksci-notebook.done` | `.failed`
- `<artifact-uri>/<run-id>/.pbs/blocksci-backup.done` | `.failed`
- `<artifact-uri>/<run-id>/.pbs/unified-report.done` | `.failed`
  — uploaded by the S3 PBS job traps. A full run returns successfully only
  after the report marker is present.

`mainnet-sync` jobs publish `.pbs/mainnet-producer.*` and
`.pbs/mainnet-blocksci.*` from their own worker (`mainnet_runtime.py`); their
bodies are JSON state records, not the `done`/`failed` lines above. Nothing in
the frontend waits for them; scenario-lab reads them.

Semantics mirror `wait_for_pbs_marker`: `failed` raises and `done` returns.
The probe distinguishes queued from executing work. A queued job may extend
its start deadline, but after the job is observed running its execution
deadline is absolute and is never extended (emulation:
`--emulation-timeout`, default 21600 s; PBS stages: walltime + one hour).
The Kubernetes uploader has the same independent emulation timeout, so a
deadlocked controller produces `upload.failed` instead of leaving the
sidecar alive forever. A liveness *probe* replaces the local qstat fallback —
`qstat -x -f` for PBS stages and the Job plus its pods for Kubernetes — and a
terminal probe report without a marker fails the stage after one extra grace
cycle (marker uploads race job termination). Probe errors are inconclusive
and polling continues until the applicable deadline.

Extra rules for the S3 chain:

- Before submitting anything, the orchestrator refuses run prefixes that
  contain any object. This prevents stale partial artifacts as well as stale
  markers from being merged into a reused `--run-id`.
- Full S3 runs require both analyzer stages and an existing namespace selected
  with `--reuse-namespace`; the S3 credentials Secret must predate the Job.
- S3 job scripts are submitted to `qsub` via **stdin**
  (`submit_pbs_text`). Job IDs live in orchestrator memory and are also
  persisted under the frontend's shared
  `<runs-root>/<run-id>/.pbs/*.jobid` directory for watching and overlap
  detection.
- `pbs-from-s3` holds
  `<runs-root>/<run-id>/.pbs-submit.lock` while preparing and submitting the
  graph. A later invocation refuses to overlap recorded queued/running jobs
  or jobs whose state is inconclusive. If marker cleanup or a later `qsub`
  fails, every job ID already obtained in that invocation is `qdel`ed in
  reverse submission order.
- When either analyzer wait fails, the orchestrator `qdel`s the dependent
  unified-report job so it does not stay held forever.
- When the emulation wait fails, PBS is never submitted; `kubectl describe`
  and container logs are printed before the frontend requests Job deletion.
  The per-run ServiceAccount, ConfigMap, Role, and RoleBinding carry Job owner
  references, so explicit deletion or the Job TTL garbage-collects them too.
  The uploader does not self-delete the Job and therefore cannot race
  frontend diagnostic collection; `activeDeadlineSeconds` remains the
  disconnected-frontend fallback.

## Requirements checked before submission

- The run directory, logs root, Bitcoin datadir, and exporters directory must
  resolve under `/storage/` (MetaCentrum shared storage).
- Before a shared-storage BlockSci submission, the frontend snapshots the
  checkout exporter tree into `<run>/.pipeline/exporters/` and binds that
  shared path on the compute node. A complete existing snapshot is reused so
  retries execute the same exporter code; an incomplete snapshot is rejected
  rather than mixed with a newer checkout. Python bytecode caches are excluded.
- The BlockSci stage requires the Bitcoin datadir to contain `regtest/blocks`.
- The mappings stage requires
  `coinjoin-analysis_data/coinjoin_tx_info.json` to already exist.
- `qsub` must be on `PATH` (a MetaCentrum frontend, or the local shims from
  `scripts/pbs-env.sh` in the meta-repo).
- When `kinit` is on `PATH`, `pbs-from-s3`, `mainnet-sync` and the S3
  `full-run` first run `kinit -R` and refuse to start if it fails, then renew hourly in the
  background so `qsub`/`qstat`/`qdel` keep working past the ticket's 10-hour
  lifetime. This needs a renewable ticket (`kinit -r 7d <login>@META`); the
  ticket the frontend login script obtains is not renewable. A later renewal
  failure only warns. Hosts without `kinit` skip this, and so does the local
  PBS container: `pbs-env.sh` exports `PBS_CONTAINER_NAME`, and its `qsub`
  shim never authenticates.

## Cancellation

When one stage of a parallel run fails, the frontend calls `qdel` on the other
still-running stages using the persisted `.pbs/<stage>.jobid`
(`qdel_pbs_stage`).
