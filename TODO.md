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
- The S3 parse/analyze renderers keep their mode-specific shell fragments in Python;
  the templates themselves are `.sh` files.
- `exporters` stays the installed top-level package name: renaming it changes the
  staged S3 layout (`.pipeline/exporters/`) and every container mount.

## 3. Incremental update from the S3 block archive

`--blocksci-task update` accepts only an `external-bitcoin` cache
(`blocksci_update_s3_template.sh`). A cache parsed from the bitcoin-block-archive
S3 prefix records `source_kind: bitcoin-blocks-s3`, so every height bump means a full
archive download and re-parse (~700 GB on mainnet). Either teach the update job to
extend a `bitcoin-blocks-s3` cache from newly archived block files, or document the full
re-parse as the supported path. (From the 2026-09-18 archive review; its XOR and
prune-verification findings are fixed in bitcoin-block-archive.)

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
