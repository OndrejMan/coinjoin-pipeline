# Manual mainnet synchronization

The whole local chain Core/Dumplings/archiver → OpenPBS/Apptainer → BlockSci and
the restore of both backups were verified on vm207; see the
[local run record](mainnet-sync-local-validation-2026-10-25.md).
That does not establish a full-chain mainnet experiment or a deployment on MetaCentrum.
No cron job is introduced. Scenario Lab offers **Start synchronization**, separate
CPU/RAM budgets and the progress of the current phase. **Notebook over the index** opens
the existing notebook form; that notebook does not parse the index and only reads it.

## Optional target height

In the Lab, **Synchronization target height (optional)** is prefilled with `400000`.
That is the recommended minimum target for a run in which Dumplings actually processes
blocks, since on mainnet it starts at height 336861. The example
`examples/mainnet-sync.yaml` uses the same target: `mainnet.target_height: 400000`
(CLI `--mainnet-target-height 400000`). Core still synchronizes the history from genesis;
400000 is the target height, not where the download starts.
Smaller targets remain available for infrastructure tests and the empty checkpoint.
An empty field or an omitted setting means the current chain tip.
With a limit, nothing waits for the IBD to finish or for all headers to catch up: once
the threshold is reached, the network is switched off, Core restarts cleanly without the
network for Dumplings, and the whole chain continues, indexing and checkpoint included.
In this mode the Core percentage expresses the height relative to the requested target;
it is not an estimate of the remaining time.

The limit is a threshold for switching off the network, not an exact cut of the data.
The check runs at most once a second plus the time of the RPC and of publishing progress;
blocks already downloaded and in progress can push the resulting height further.
Switching off the network does not wait for their validation: the final height is
therefore read only after Core shut down cleanly and started again with
`networkactive=0`. Every later step uses the actual frozen height and hash; `ready.json`
also keeps `requested_target_height`.
An existing higher chain is not shortened. For a smaller test, use a separate Core
dataset, index and backup prefixes. For a target before its default start 336861,
Dumplings publishes an empty checkpoint at the actual height and resumes safely next time.
For a short mainnet test below height 100000, pruning is skipped because Core does not
allow it yet; indexing and the backup continue and keep every block.
This option also requires a newly built Dumplings with `--frozen-height`.

## Six jobs

The synchronization submits six jobs chained with `afterok`: `mainnet-restore` →
`mainnet-producer` → `mainnet-blocksci` → `mainnet-checkpoint` → `mainnet-pack` →
`mainnet-clear`. All but `mainnet-pack` take and release the dataset lock themselves.
When one fails, PBS cancels the ones that depend on it. The synchronization is complete
with the checkpoint's final `done.json` already; a failure of `mainnet-pack` or
`mainnet-clear` does not change that and shows in the Lab only on that job.

`mainnet-restore` (8 CPU / 8 GB) clears the state of the previous run and checks whether
working data exists on the multijob (`bitcoin/`, `dumplings/`, `archive-state/`, or the
index's `config.json`, `files.txt`, `manifest.json` or `parsed/`; locks, `sync/` and logs
do not count).

- **Everything exists:** it ends at once with `restore.json: skipped-not-empty`.
- **Core exists, but `dumplings/` or the index is missing:** the missing part was deleted
  on its own (the Lab offers that), for example to scan again after a change of the
  Dumplings detection. The job restores only that part from its backup and writes
  `restored-missing`. The index must continue from the block files that are here: its
  `files.txt` and the parser cursor `resume_file_*` are checked against them before
  anything takes its place. The Scanner is checked against its manifest; the producer
  then checks its chain anchor and that Core still has the blocks after its height.
- **Core exists, a part is missing and has no backup either:** the Lab deleted the backup
  too, to scan the whole history again. The job takes the pruned block files back from
  `blocks_uri` (see "Rebuilding from the block archive" below), writes
  `rebuilt-from-archive` and the part is built again. Without archive markers of pruned
  files the part stays missing (an unfinished first synchronization).
- **Core is missing, but something else exists:** `skipped-not-empty`, and the producer
  refuses the dataset. Core is never restored alone beside a Scanner or index, because
  blocks downloaded again lie elsewhere in the `blk*.dat` files than the index expects.
- **Something exists, but the index has `.writing`:** a BlockSci job failed in the middle
  of writing, so nothing can continue. If the backup manifests in S3 pass the same checks
  as below, it deletes the whole working dataset and restores it from S3; the work of the
  unfinished run is lost. Without usable backups it fails and leaves the dataset alone.
- **Nothing exists and S3 has no backups:** this is the first synchronization. It writes
  `skipped-no-backup` and the producer builds from scratch. A non-empty `blocks_uri`
  without backups is an error, because a new IBD would lay the blocks out in different
  `blk*.dat` files.
- **Nothing exists and S3 has backups:** it checks that all three manifests
  (Core checkpoint, index and Dumplings) belong together:
  - Core refers to this index,
  - the `run_id`s are the same,
  - the Core height = `exported_max_block` = `last_processed_height`,
  - the parser cursor `resume_file_*` matches.

  It then downloads all three into `.restore-<run-id>/`, checking SHA-256 and size,
  and only then moves them into place. It checks `files.txt` and the parser cursor
  against the restored blocks and writes `restored`.

- **Nothing exists and S3 has the Core checkpoint, but not the Dumplings or index
  backup:** the Lab deleted them to scan again. The job restores the Core checkpoint alone,
  but only after its `archive-state/` and the block files in it make up the whole history
  without a gap and every pruned file is in `blocks_uri`; otherwise it fails and the
  dataset stays empty. A backup that is still there is restored as beside Core above, the
  deleted part is rebuilt from the archive, and the job writes `rebuilt-from-archive`.

### Rebuilding from the block archive

The archiver's marker of every uploaded file (`archive-state/blkNNNNN.dat.json`) gives its
size, SHA-256 and mtime. The restore job downloads each pruned file, raw or as the `.zst`
the pack job left, checks size and SHA-256 and gives it back its mtime, so the archiver
recognises it and uploads nothing again; the archiver's pruning check accepts the packed
object as the archived copy. Each file takes its place once it checks out, so a killed job
loses only the files in flight. The multijob must have room for the files and Core's undo
data (a sixth more). Before the first file it writes `<root>/core-reindex.json`.

With that marker the producer first runs Core offline with `-reindex
-stopafterblockimport`: Core rebuilds its block index and chainstate from the files and
exits by itself, and only a clean exit removes the marker. Then it continues as usual.
With every block file on disk `require_complete_dataset` accepts a missing Scanner or
index: Dumplings scans from `scan_start_height` and the BlockSci job parses the whole
history (`generate-config`). The checkpoint backs both up and prunes again; the pack job
finds nothing new. One step of the whole mainnet does not fit the default walltimes
(reindex, a full Dumplings scan and a full parse): a producer killed after the reindex
continues the scan in the next synchronization, and the parse takes the BlockSci job's
resources (`MAINNET_SYNC_RESOURCES`).

`mainnet-producer` first checks that the dataset is complete: Core chainstate and blocks,
`archive-state/`, the Scanner height and the index's `config.json`.

- **Everything is missing:** it passes only when this run's `restore.json` says
  `skipped-no-backup`.
- **Part is missing** that the restore job could not refill: it stops before it starts
  Core and lists what is missing. The fix is to delete the whole `mainnet.root` and
  `blocksci.index_dir`; the next synchronization restores them.
- **A first synchronization under way** (no index, but `blk00000.dat`) continues.

The producer then synchronizes Core with `prune=1`, `txindex=0`, `blocksxor=0`.
Once synchronized, it switches off the network, restarts Core cleanly without the network
and captures a stable height H and its hash. It runs one Dumplings pass and checks that
the height and checkpoint match, file lengths and an empty `Tx0CandidateMisses.txt`
included. It archives and verifies the completed blk files; by default the archiver keeps
the last two as the open end. It does not touch the Dumplings backup: a failure of the
producer or of the BlockSci job thus leaves all three backups in S3 from one run.
It shuts Core down cleanly and publishes `ready.json`.
The last block files stay on the shared disk for the parser and in the resulting Core
checkpoint. `ready.json` alone does not mean a completed synchronization.
Before the scan it also saves `Scanner/ChainCheckpoint.json` with the hash of the frozen
target. The next pass checks the original branch even when a partial scan is restored.
A reorganization, or importing an old Scanner without this link, requires an explicit
check or restore; detections do not carry over between branches automatically.

`mainnet-blocksci` (160 GB) checks the exact generation and, under the exclusive lock,
updates the index directly over the same blk files. It checks the block count H+1 and the
hash at H, writes `manifest.json` and `files.txt` and deletes `.writing`: the index is
complete even though it has no backup yet. Core does not run in this job.

`mainnet-checkpoint` (4 CPU for `zstd -T`, 4 GB) holds the index lock for reading only,
so notebooks can keep reading the index and a manual index job cannot change it.

1. It checks that this run's BlockSci job completed the index: `run_id` and
   `exported_max_block` in the manifest, `files.txt`, no `.writing`, and the parser
   cursor against Core.
2. It backs up the index and checks that the uploaded object exists and has the right
   size. It does not download the archive again.
3. It mirrors the Scanner incrementally, Tx0Candidates included, to
   `<dumplings_uri>/Scanner/` (`s5cmd sync --delete`, only changed files are transferred),
   compares the object sizes with the local files and finally writes
   `<dumplings_uri>/manifest.json` with the height and sizes. A new copy deletes the
   manifest first, so the baseline never reads a half-replaced Scanner.
4. It starts Core offline with `dbcache` of at most 1024 MB, checks H and its hash, runs
   `--prune-only` and shuts Core down cleanly.
5. Only then does it back up Core's resulting state. During the copy it holds the POSIX
   lock of the datadir and refuses a present RPC cookie.

Only a completed checkpoint upload lets the final `<state_uri>/done.json` be published.

Pruning does not upload anything. It checks the existing archived copies. The height
limit is the smaller progress of the two consumers minus a reserve (2016 blocks by
default). It also protects the file of the exact parser cursor and every newer blk/rev
pair. A missing, empty or unreadable protected file blocks pruning.

BlockSci saves `parsed/parser/blockList.dat.resume.json` from the actual
`newestBlock.nFile/nDataPos/size`. This cursor can differ from the file of the last
canonical block. The checkpoint is usable only after the parser exited successfully and
the resulting index was checked. An older image without this file does not allow
pruning. The index manifest also holds the SHA-256 of that file's prefix up to the cursor.
The next update checks the same bytes; new data can be appended after the cursor, but a
different Core layout must not pass for the same source just because it has the same path.

`mainnet-pack` (4 CPU, 4 GB, 24 h) compresses the block archive. It does not take the
dataset lock, so it can run alongside the next synchronization. It lists S3 first and the
disk second and takes only raw `blkNNNNN.dat` files that Core no longer has on disk:
for files on disk, pruning checks the raw object. It refuses a run without this run's
`done.json` and an empty blocks directory.
It handles every file on its own: it checks it against the SHA-256 of its sidecar, packs it
with `zstd -19`, uploads `blkNNNNN.dat.zst`, reads it back, compares the unpacked content
with the sidecar again and only then deletes the raw object. The sidecar does not change,
so `block_archive_index_sha256` stays valid. Repeating is safe: a file in the raw + `.zst`
state is processed again and reads take the raw one. Half an hour before the end of the
walltime (at most a quarter of the walltime) the job stops taking further files; the rest
stays raw until the next run. The summary is in `pack.json`
(`packed`, `raw_bytes`, `packed_bytes`, `remaining`, `failed`, `seconds`). The block
download of parse and update reads both forms and unpacks `.zst` before `verify`. A pack
job running during a download can fail the parse, because the raw object disappears
between the listing and the download; a repeated parse already sees the `.zst`.

`mainnet-clear` (1 CPU, 2 GB, 2 h) empties the multijob: Core, Dumplings, the archive
state, the index and everything else in the dataset root, such as old logs in `runs/`.
It runs after `mainnet-pack`, because that job compares S3 with the blocks on disk. It
takes the dataset lock and the index write lock and refuses a dataset whose `done.json`
belongs to another run. It first moves everything aside into `.clear-<run-id>/`, so a
failure does not leave a partial dataset; the next restore job removes the leftovers. Only
the `sync/` directory stays. When `mainnet-pack` fails, `mainnet-clear` does not start and
the data stays until the next synchronization. The next run restores it from S3. It runs
only with `mainnet.clear_after_sync: true`; by default the data stays on the multijob.

Because the multijob is empty between synchronizations, the jobs that read the index
(`script`, `external`, `notebook`) do not read it from `blocksci.index_dir`. With
`blocksci.index_in_scratch: true` they download the backup from
`blocksci.index_backup_uri` into their own `$SCRATCHDIR`, where nobody else reads it, so
they need no lock. With `blocksci.dumplings_uri` they add the Scanner, which the container
sees at `/mnt/dumplings/Scanner`. The scratch must hold the unpacked index
(135 GB at height 500 027). The Lab takes the size from `storage.multijob.index` in the
`done.json` of the last synchronization and adds 10 % and 20 GB; without a measurement it
asks for 200 GB. The manual `index-update` and `index-backup` jobs still work on
`index_dir` and need a restore on an empty multijob.

A notebook can instead keep the data on the multijob: with `blocksci.restore_index: true`
it fills an incomplete `index_dir` from the backup under the write lock and reads it under
the shared one. The Lab uses its own folder `mainnet-notebook/{index,dumplings/Scanner}`
for that, which neither the synchronization nor `mainnet-clear` sees; a copy in the
synchronization's `index_dir` would make the restore job see a non-empty dataset and the
producer would refuse it. The copy stays, and the next notebook uses it without a
download, even when a newer backup appeared meanwhile. It is deleted only by hand in the
Lab.

The third option is to read the synchronization folder directly, without a download: the
synchronization's `blocksci.index_dir` and `blocksci.dumplings_dir` on its
`dumplings/Scanner`. The data is there only after a synchronization without
`mainnet.clear_after_sync: true` (in the Lab, "Empty the multijob after the
synchronization" left off, the default); otherwise the job fails. The notebook holds the index under
the shared lock, so another BlockSci job or `mainnet-clear` fails while the notebook runs.
The Scanner has no lock: a running `mainnet-producer` changes it under the notebook.

After every synchronization job, a failed one too, the worker measures `mainnet.root`,
`blocksci.index_dir` and `mainnet.notebook_root` and writes them to
`<state_uri>/multijob-storage.json`. A notebook with a kept copy writes its size to
`blocksci.storage_report_uri`. From these reports the Lab takes the newest for each folder
and records a deletion in the Lab itself as zero, so the regular overview needs no
inspection job. A failed report does not fail the job.

## Working data and backups

```
mainnet.root/
  bitcoin/{blocks,chainstate}
  dumplings/Scanner/
  archive-state/
  sync/{restore,producer,blocksci,checkpoint}.json, sync/{ready.json,done.json}
  current-ready.json
  .sync-active/owner.json
blocksci.index_dir/
  config.json
  parsed/
  manifest.json
  files.txt
  .lock/
```

All jobs must use the same multijob location. Its directory is separate from
`$SCRATCHDIR`, but multijob storage is temporary and can disappear. Only
`mainnet-restore` downloads from S3, and only into a completely empty dataset. The
initial parse needs the complete block history. A pruned Core without an index cannot be
used for the initial parse directly.
An existing index can be resumed only with the same block source and image; a foreign
index is not reconfigured automatically.

Each prefix holds a single backup: an archive with a fixed name
(`<backup-uri>/checkpoint.tar.zst`, `<index-backup-uri>/blocksci_index.tar.zst`),
its `.sha256` and `manifest.json`. Only `S5Client.replace_backup` and `restore_backup`
(`storage/s5client.py`) write and read the format, as for the `--blocksci-task backup`
job. A new backup deletes the manifest and the old archive first, so S3 never holds two
copies and there is no valid backup until the upload ends.
The archive is streamed to S3 with its SHA-256 computed on the way; after the upload only
the object size is checked, and finally the manifest is written
(`upload_validation: size-only`, `uploaded_at`). The content is not read again; the
SHA-256 is checked only when restoring. A failed upload leaves the backup missing until
the next run; the working copy on the multijob stays.
The Core backup holds `bitcoin/blocks`, `bitcoin/chainstate` and the archiver state;
the Scanner is not in it and is restored separately from its incremental copy
(`mainnet_runtime … restore-dumplings <dumplings_uri>/manifest.json <target>/dumplings/Scanner`).
It holds no wallets, credentials or Core configuration.
The job always sets the configuration for continuing. Core with old XOR blocks or several
chainstates requires an explicit migration.

The manifest of every backup lists `member_bytes`, the apparent size of every backed-up
root. The final `done.json` assembles `storage` from them: the multijob usage
(`multijob.core`, `multijob.index`, by root) and the size of the newly uploaded archives
(`s3.core_checkpoint`, `s3.index_backup`). It is measured while Core is stopped and the
index write lock is held, so no separate measuring job is needed for these numbers.
The total S3 usage, block archive included, only a listing of the prefixes tells.

The synchronization state has a fixed place, not a folder per run ID: locally
`mainnet.root/sync/`, in S3 `mainnet.state_uri` (`ready.json`, `done.json`,
`{restore,producer,blocksci,checkpoint}.json`, `blocksci-index-manifest.json`,
`.pbs/mainnet-{restore,producer,blocksci,checkpoint}.{done,failed}`). Unlike the markers
of the other stages, which hold only `done`/`failed`, the mainnet markers carry JSON:
`.done` the same content as the job's result (the restore action and backups, the
producer's `ready.json`, the BlockSci index manifest, the checkpoint's `done.json`),
`.failed` an object with `run_id`. The restore job of a new run deletes them first under
the dataset lock, so the prefix always describes the last run; the `run_id` in every JSON
tells which one it belongs to. The logs of each run stay in `<state_uri>/logs/<run-id>/`.
The synchronization configuration therefore has no `artifacts.uri`; the `artifacts`
section carries only the endpoint, the credentials file and the profile. The manual index
jobs (`parse`, `update`, `backup`) likewise have a fixed prefix
`blocksci.index_state_uri` (`bitcoin-mainnet/index/`), see `docs/pbs-stage-contract.md`.

The progress in `<state_uri>/{restore,producer,blocksci,checkpoint}.json` holds the phase,
the height, a percentage or null and the time of the update. For Core it is the RPC
estimate; the parser percentage applies to the phase it currently reports. The streamed
backup and its check have an indeterminate percentage. Neither the producer marker nor
`blocksci.json: indexed/done` replaces the final `done.json`; `restore.json` also carries
`action`. The Lab fills in the PBS state even after a failure before progress was
published.

`Live log` in the Lab (CLI `watch --pbs-only --run-id ID --run-dir RUN_DIR`) also reads
the actual stdout/stderr of Core, Dumplings, the archiver and BlockSci.
Newly submitted jobs write these small files directly to
`<runs_root>/<run-id>/logs/mainnet-{restore,producer,blocksci,checkpoint}/*.log` on the
storage shared with the frontend; `runs_root` must be reachable from the PBS nodes under
the same path. The working blockchain and index stay in the multijob directories.
The watcher also follows files that appear during the run, labels the output with its
component and, when it attaches, replays the last 200 lines of each source (`--tail`).
It waits neither for PBS to deliver stdout nor for the upload to S3. When a phase ends,
its logs are still archived to S3 under `<state_uri>/logs/<run-id>/`. PBS scripts
submitted earlier contain the original worker, and this continuous saving does not apply
to them retroactively.

The job's own stdout/stderr goes only to the PBS output file, which PBS cannot copy back to
a frontend in a container (`cj-frontend`), and the container does not see `runs_root` on
the nodes either. A failed job therefore writes its cause into the `error` of its
`<state_uri>/<stage>.json`, which the Lab shows at the phase: the exception, and for a
failed Core, archiver or BlockSci the last line of that component's log (for example the
Apptainer `FATAL:` line). The traceback goes to `worker-<stage>.log` beside the component logs and
is uploaded with them to `<state_uri>/logs/<run-id>/`. A job that fails before the worker
starts (missing credentials, `s5cmd` or `zstd`) reports only the generic message.

## Failures and concurrency

Every job holds the exclusive dataset lock while it works. When the queue hands over,
`current-ready.json` stays; the BlockSci and checkpoint jobs check that it belongs to
exactly their generation. The producer of the next generation invalidates this marker
before it changes Core, so an old waiting job must not process newer or partly changed
data. The lock is not held between jobs: cancelling a job that has not started leaves no
dataset lock.
The index uses the same writer/readers lock as the existing notebooks. The restore and
BlockSci jobs take the write lock, the checkpoint job the read lock. If a notebook holds
the index when a restore or BlockSci job starts, the job ends at once; it does not hold
RAM by waiting. Stop the notebook and start a new synchronization.

After an error the dataset is unlocked only if the worker confirmed that its processes
ended, or if they never started. A non-zero exit code of a Core that already ended does
not prevent unlocking after an error; it still forbids publishing the checkpoint. A
timeout or a failed stop that does not confirm the end of the process keeps the lock.
After SIGKILL/OOM, walltime or a node failure, `.sync-active` remains, and `afterok`
cancels the rest of that chain. Every job therefore checks a lock it finds when taking the
dataset: when `qstat -x -f` reports the owner in `owner.json` ended (`F`, `X`, `C`) or no
longer knows it, the job renames the lock to `.sync-active.ended-<job>-<run-id>` for an
audit, removes the RPC cookie its Core left and takes the dataset; PBS has killed every
process of an ended job, Core included.
A lock of the job's own ID is the previous attempt of a PBS rerun and is taken without
asking. A queued or running owner, a qstat that does not answer, or a lock without a job
ID stop the job. The lock is never judged by its age.

On MetaCentrum, `/scratch/multijob-brno` is a symlink to BeeGFS. The worker therefore
binds the working directories under both the original path and the real one from
`Path.resolve()`, so symlinks in the container reach the same data as on the host. The
paths in commands and stored manifests stay the original ones. If Core reports a
non-existent datadir although the directory exists on the host, check both binds in the
current worker.
The upload of the persistent logs is tried even after a cleanup error; such an error does
not confirm that the processes ended, though, and creates no `quiescent` marker.

An incomplete index has `.writing`, and the next pass refuses it. It arises only when the
BlockSci update itself fails; then delete the whole dataset and let the restore job
restore the last valid backups. A failed checkpoint job does not damage the index: the
index and Core stay usable on the multijob, S3 lacks only the backup that the start of the
upload deleted, and the next synchronization makes it again. Until then, though, an empty
multijob cannot be restored, because the backups in S3 do not belong together.

## Local checks without MetaCentrum services

The short tests use small files, mocks of PBS/Core/the parser and a directory standing in
for S3. They also check a real small tar/zstd stream, SHA-256 and the restore. They send
nothing to the network and start neither Core, the parser nor containers:

```bash
cd /home/administrator/diplomka/coinjoin-pipeline
.venv/bin/python -m pytest -q tests/unit/test_mainnet_sync.py \
  tests/pipeline/test_index_store.py \
  -k 'not readers_share and not reader_waits and not killed and not readers_finish'

cd /home/administrator/diplomka/bitcoin-block-archive
.venv/bin/python -m pytest -q tests/test_parser_pruning.py

cd /home/administrator/diplomka/scenario-lab
VITE_CONFIG_NATIVE_IGNORE_WARNING=true npx vitest run \
  src/domain/mainnet-sync.test.ts server/mainnet-sync.test.ts \
  src/components/mainnet/MainnetPage.test.tsx
```

A local integration run needs images that contain these changes. Building pushes
nothing:

```bash
cd /home/administrator/diplomka
docker build -t bitcoin-block-archive:sync-local bitcoin-block-archive
docker build -t dumplings:sync-local Dumplings
docker build --target complete --build-arg NTHREADS=4 \
  -t blocksci-complete:sync-local blocksci
# Parser regression including an empty tail file and a missing resume file:
docker run --rm --entrypoint bash blocksci-complete:sync-local -lc \
  'cd /mnt/blocksci/test/blockscipy && uv run --project /blocksci --locked pytest -q --btc test_parser_resume.py'
```

A real local OpenPBS/Apptainer pass needs a separate configuration with a local S3
(such as MinIO), local credentials, shared paths under `/storage/`, a smaller test budget
and matching image URIs. Docker tags alone are not reachable from Apptainer in the PBS
container: the images must be exported or converted and available to the node. Do not run
the cloud example as a local test. The reproducible integration test prepares an isolated
MinIO and PBS, converts the local images, generates regtest and performs two passes and a
restore:

```bash
cd /home/administrator/diplomka/coinjoin-pipeline
bash tests/test-mainnet-sync-local.sh
```

It also requires the existing local PBS and MinIO images named at the top of the script.
It uses `mainnet.network: bitcoin_regtest`, `scan_start_height: 1` and a disabled P2P
network. The default production mode is still `bitcoin`; changing the scan start is
allowed only for regtest. The generated data, MinIO storage and logs stay under
`/storage/github-runner/mainnet-sync-*`. The test uses neither MetaCentrum nor a remote
S3. The confirmed results and their limits are in the linked record.

### Bounded mainnet E2E in CI

`tests/test-mainnet-sync-mainnet.sh` uses the same test chain as the regtest variant, but
starts with an empty data directory, and a real Bitcoin Core downloads mainnet over the
public P2P network. The default first target is **400000**; Dumplings thus actually
processes blocks from its start height 336861.
The second pass continues from the actual frozen height and asks for another 1000 blocks.
It is not an exact cut at height 400000: the overshoot by blocks already in progress
described above applies. A separate check refuses a Core height above 450000 and more than
256 GiB in total of working data, index, MinIO and restored copies. The height reserve
allows the Core overshoot and the second generation above 400000. Image tar/SIF files do
not count toward this data limit. Each generation is waited for at most 34200 seconds
(9.5 hours); CI limits the whole test, preparing the images for Apptainer included, to
1200 minutes and the whole job, builds included, to 1260 minutes. The producer has a
budget of 8 GiB RAM / 6 hours, BlockSci 16 GiB RAM / 3 hours. The original 90 minutes
were not enough for the producer: with the 400000 target, the Core synchronization alone
took about 80 minutes, and PBS then killed the Dumplings scan in progress.
The generation limit covers both consecutive PBS jobs and a half-hour reserve; the test
limit covers two generations and an hour for preparation and checking the restore.

The Dumplings progress reports the frozen Core height in `height` and the scanner
checkpoint separately in `scanner_height`. A missing checkpoint therefore cannot pass the
scanner's default height 336860 off as the height the blockchain reached.

The test requires both production PBS jobs to succeed in both generations. It checks the
incremental progress, the archive of the completed block files in the local MinIO, the
restore of both backups with SHA-256, an actual start of the restored Core and loading the
restored index in BlockSci.
After the restore both must return the same height, tip hash and mainnet genesis; the Core
checkpoint must refer to the matching index backup. The default target 400000 also covers
an actual Dumplings scan. The test does not prescribe how many CoinJoins are found and,
without ground truth, does not measure detection quality. Actually deleting old blk files
remains a required check of the regtest variant, where the small dataset reliably triggers
it.
The archiver keeps the two newest block files. If a short history fits into them, the test
does not require a separate archive of `blk00000.dat`; it checks that the file is in the
restored Core checkpoint and records `block_archive_verified: false`.
As soon as a third file appears or an archive marker exists, it requires the remote copy of
`blk00000.dat`. Regtest always requires it.

The **`mainnet-sync-bounded`** job of the `Tests` workflow runs automatically on push and
pull request, behind the shared CI gate. It checks out and builds Core/archiver, Dumplings
and BlockSci from their repositories, uses a local OpenPBS/Apptainer and MinIO and needs
neither MetaCentrum nor CESNET credentials. The artifact `mainnet-sync-bounded-results`
keeps the configurations, `result.json`, the exit status, the PBS history and the ends of
the logs for 14 days, also on failure. The large data and backups stay in the working
directory under `/storage/github-runner/`; `working-directory.txt` names the path. Neither
the test nor CI deletes the retained data evidence.

With the images built in the previous section and the PBS and MinIO images available:

```bash
cd /home/administrator/diplomka/coinjoin-pipeline
timeout --signal=TERM --kill-after=120s 1200m bash tests/test-mainnet-sync-mainnet.sh
# Optionally a smaller first target (a positive integer of at most 400000):
MAINNET_TEST_TARGET_HEIGHT=10000 timeout --signal=TERM --kill-after=120s 1200m \
  bash tests/test-mainnet-sync-mainnet.sh
```

The images can be chosen with `MAINNET_CORE_LOCAL_IMAGE`,
`MAINNET_DUMPLINGS_LOCAL_IMAGE`, `MAINNET_BLOCKSCI_LOCAL_IMAGE` and `PBS_IMAGE`.
`MAINNET_TEST_MAX_DATA_MB` and `MAINNET_TEST_GENERATION_TIMEOUT` change the limits.
The short tests `tests/unit/test_mainnet_sync_e2e.py` check the configurations, the
watchers, the negative restore cases and the automatic CI wiring without downloads and
without containers.
Adding the test does not by itself prove a successful mainnet pass; the output of the new
CI job has to.

To check the configuration and the generated scripts without access to the services:

```bash
cd /home/administrator/diplomka/coinjoin-pipeline
./runIt.sh run examples/mainnet-sync.yaml --dry-run > /tmp/mainnet-sync-plan.txt
```

## Checking the backups without deleting anything

`scripts/render-mainnet-restore-check.py [config.yaml]` prints a standalone PBS job that
runs the real `mainnet-restore` into its own `$SCRATCHDIR` (2 CPU, 4 GB RAM, 200 GB
scratch). It checks the pairing of the three manifests, the SHA-256 of the archives, the
Scanner sizes, the index inventory and the parser cursor. It changes neither the multijob,
the backups nor `state_uri`: the state goes to `<state_uri>-restore-check/<run-id>/`, and
the job deletes the unpacked data at the end.
It does not check that Core or BlockSci opens the restored data. The result is
`restore.json: restored` and a zero exit code.

In `parts`, `restore.json` has for Core, Dumplings and the index the duration, CPU time,
downloaded and unpacked bytes, the file count and the rates; the job log has the same
lines. The download, unpacking and checks run concurrently, so each part has a single
time. The job also first reads both archives and discards them (`raw_download_*`), so the
comparison shows whether S3 or the unpacking is the bottleneck. The archives are thus
downloaded twice. A real restore writes `parts` too, but without this pass.

## Explicit checkpoint restore

`mainnet-restore` does the regular restore of an empty dataset. This chapter describes a
manual restore of a single backup, for example for a check.

The helper `coinjoin_pipeline.execution.mainnet_runtime` can restore a verified manifest
into a **non-existent** directory. It uses a staging directory beside the target; a failed
restore exposes no partial directory. It refuses links and paths outside the declared
roots. Prepare a small JSON without the keys themselves (adjust the paths to the
environment):

```json
{
  "root": "/storage/local/mainnet",
  "index_dir": "/storage/local/index",
  "run_id": "explicit-recovery",
  "state_uri": "s3://local/sync",
  "endpoint": "http://127.0.0.1:9000",
  "credentials": "/storage/local/credentials",
  "profile": "local"
}
```

```bash
python -m coinjoin_pipeline.execution.mainnet_runtime recovery.json restore \
  s3://local/core/manifest.json /storage/local/restored-core
```

The same helper restores the index when it gets the manifest of its backup and a new
target directory. The original copy is not overwritten. `index_store.py` can restore the
index backup too (an update over an incomplete working copy).
The same run writes the index and Core, the index first. If a run fails only at the Core
backup, the Core manifest is missing and the index backup is a generation newer than the
Core working copy; such a pair cannot be combined blindly, and the synchronization has to
complete first.
