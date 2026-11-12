# TODO

## 1. Pin the dind image

`pipeline/compose.yaml` pulls the floating `docker:29-dind` tag with `pull_policy: always`.
A new 29.x image once changed the TCP API startup and broke the healthcheck (fixed with
`--tls=false` and `start_period: 20s`). Pin it to a digest or a specific minor so the same
class of surprise cannot return. Docker 29 still warns that the unauthenticated API will
become a hard failure.

## 2. Decided against, for now (2026-10-03)

The run-directory layout work and the experimental-path simplification are done
(`artifact_paths.py`, hashed report inputs, tri-state stage status, typed run
manifest; configuration-typed `compose_env`/`run_script`/`run_kubernetes_emulation`,
shared graph nodes, in-cluster scripts as `.sh` files). These were left on purpose:

- BlockSci modes `script`, `notebook`, `reusable`, `cached` stay: no functionality is
  removed (user decision 2026-10-03).
- Local and S3 graphs stay separate builders sharing their report/mappings nodes;
  one builder would change the local serial order.
- `RunContext.activate()` still hands settings to Compose, the shell scripts and the
  emulator subprocess through the process environment, because that is how those
  children read them; passing a dict around would only move where it is built.
- Every S3 PBS job runs one Python stage worker (`execution/pbs/stage_runtime.py`); only
  the mappings tools, the `coinjoin_statistics` workflow and the shared-storage jobs keep
  `.sh` templates.
- `exporters` stays the installed top-level package name: renaming it changes the
  staged S3 layout (`.pipeline/exporters/`) and every container mount.

## 3. Incremental update from the S3 block archive

Implemented: a `bitcoin-blocks-s3` cache updates with `--blocksci-task update
--blocksci-bitcoin-blocks-uri`, and both parse and update download only the
block files they need (`execution/block_archive.py`). Still unverified: the
extended `tests/test-bitcoin-block-archive-s3-minio.sh` (parse → archive grows →
update, compared with a full parse) has not run yet, and no update has run on
MetaCentrum. The update relies on BlockSci reading only `newestBlock.nFile`
onwards for headers and each new block from its own `nFile`
(`tools/parser/chain_index.cpp`, `block_processor.cpp`).

## 4. Verification still owed

- Golden comparison: compare analytic results (transactions, labels, skipped txids,
  clustering, metrics, diagnostics) of new reports against pre-refactor runs. The exporter
  source hash (`exporters_sha256`) changes on purpose; nothing else should.
- One MetaCentrum run with a valid Kerberos ticket (emulation → PBS BlockSci → report).

## 5. Fixes hidden in the code that a rewrite must not lose

- NFS lag grace cycle and liveness probe in the PBS marker protocol; the frontend never removes remote markers of a compute job.
- `PIPELINE_RUN_ID` contract (minute-collision run-directory race between the CLI and the emulator); push emulator before pipeline.
- Fail-closed report (`report_status` accepts only an explicit `"ok"`), all-or-nothing producer labels, `ensure_staged_exporters` rejects a partial prefix.
- JoinMarket k8s IRC 512-byte truncation and the `-blocksxor=0` node argument for BlockSci-readable regtest datadirs.
- BlockSci import shadowing (never name a directory `blocksci` under exporters); `PYTHONDONTWRITEBYTECODE` so 3.14 bytecode does not reach S3.
- Container image pinning: `filter_coinjoin_txes_raw` needs the matching blocksci image; private `joinmarket-base` build args.
- Wasabi key-cache race (`getnewaddress -32603`) is retried in the emulator, not here.

## 6. Possible, not needed now: extract index archives with native tar

`S5Client.restore_backup` (every index restore: run caches, the persistent index, the mainnet
checkpoint) unpacks with Python `tarfile` and checks every member. The alternative would keep the
streamed SHA-256 and the `files.txt` check and unpack with `zstd -d | tar -x` instead.

Measured on vm207 on 2026-11-12 (synthetic index, three rounds, variants alternating, alongside a
CI job, so the ratios hold better than the absolute times):

| Archive | plain `tar` | SHA-256 + `tar` | SHA-256 + `tarfile` (now) |
|---|---|---|---|
| 3.0 GiB, 4 large + 4,000 small files | 24.1 s | 26.2 s | 27.5 s (+5 %) |
| 3.3 GiB, 4 large + 40,000 files of 32 KiB | 27.2 s | 31.0 s | 38.7 s (+25 %, twice the CPU) |

An emulation run's index is about 600 MB in 70–85 files, so the change would save a fraction of a
second per restore; the mainnet restore through `tarfile` already reached about 254 MB/s on
MetaCentrum.

What native tar would cost against the current code:

- **Checks only after the fact.** `tarfile` refuses an absolute path, a `..` component, a link, a
  special file or a path outside the declared members before anything is written. With `tar`, such a
  member would already be on disk when a check after extraction finds it, and the restore would have
  to clean up after it instead of never writing it.
- **A host tool's behaviour instead of the standard library.** `s5client.py` ships to PBS nodes on
  its own and needs only Python. Native extraction depends on the node's `tar` (GNU against BusyBox,
  versions, flags for links, owners and timestamps), which the jobs do not control.
- **Timestamps and errors.** The extractor sets nanosecond mtimes from the pax headers, which the
  block archive markers compare, and names the member that broke a restore. GNU tar keeps the
  mtimes too, but BusyBox and older tars may not, and `tar` reports a broken member only through
  its exit code and stderr.
- **Tests and code to rewrite.** The member-check tests of `test_index_store.py` and the restore
  path would change, for a gain only an index of tens of thousands of small files would notice.

Worth it only if a restore becomes the bottleneck of a job, for example an index with very many
small files restored often.
