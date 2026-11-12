# Local check of the synchronization — 2026-10-25

**Result: PASS on vm207.** This is the whole local chain with real components over
regtest, not a full-chain mainnet experiment. MetaCentrum, its PBS, Kubernetes and CESNET
S3 were not used. This validation run made no commits and no push.

## Checks performed

| Check | Result |
|---|---|
| Building the archiver, Dumplings and BlockSci, Python bindings included | PASS |
| Dumplings tests (.NET 8 SDK, net6 tests with `DOTNET_ROLL_FORWARD=Major`) | 27 PASS |
| Short pipeline tests, locks / archiver / Lab / launcher / service included | 66 / 80 / 23 / 10 / 6 PASS |
| Parser: an empty trailing blk file, a repeated update and a missing actual cursor | 1 PASS |
| Real OpenPBS → Apptainer: Core + Dumplings + archiver | PASS |
| Real OpenPBS → Apptainer: BlockSci + backup + prune-only + Core checkpoint | PASS |
| Incremental continuation after pruning | PASS, height 1200 → 1400 |
| Restoring Core and actually starting it | PASS |
| Restoring the index and actually loading it in BlockSci | PASS |
| JupyterLab through the local bridge/proxy, a real Python kernel | PASS |
| Chart, widget and saved notebook outputs | PASS |
| Browser: Start/Stop, CPU/RAM, progress, error and switching to the notebook | PASS with local API responses |
| The original persistent-index integration test: parse/update/restore/pinned analysis/backup | PASS |
| Ruff, mypy and pylint of the new modules; TypeScript, ESLint and formatting of the touched Lab code | PASS |

The local stack used Bitcoin Core 29.1, OpenPBS 23.06.06, Apptainer 1.5.1 and MinIO.
Core had neither txindex nor automatic pruning. Regtest used `-fastprune` and a reserve
of 288 blocks, so actually deleting files could be checked on a small dataset.
The default mainnet reserve in the configuration stays 2016 blocks.

The PBS requests in this regtest were 2 CPU / 4 GiB for the producer and 4 CPU / 8 GiB
for indexing. They are neither a measurement nor a recommendation of mainnet resources.
The notebook asked for 1 CPU / 2 GiB.

## The specific result of the second generation

- Core and BlockSci both end at height **1400**; the index holds **1401 blocks, genesis
  included**.
- After the restore both return the hash
  `3be507b108606b8246fa9da754308bed3105febcaa8d78aa455e639cd96fd5ec`.
- The pruner asked for height **1015**; the restored Core reports `pruneheight: 1016`.
- The file `blk00000.dat` is deleted locally, and its object stays in MinIO.
- The exact parser cursor is in `blk00005.dat`; its prefix up to a length of **33798**
  bytes is preserved. The following indexing used the existing index after the previous
  pruning.
- The Core checkpoint refers to the generation backup of exactly the matching index.
- The restored Core reports `automatic_pruning: false`, and `getindexinfo` returns `{}`.

Following the chosen policy, the upload of the large archives was checked by existence,
key and size, not by reading the whole object back. The SHA-256 is computed during the
upload. In this test **both archives were actually restored and their streams checked
with SHA-256**. That does not claim that every regular upload reads back.

## Fixes from the real run

- PBS did not start the shell with `/usr/local/bin`; the job now adds that path before it
  checks `s5cmd` and before it starts the processes.
- The live Dumplings scanner deliberately does not save the tip it just processed. A new
  explicit `--include-tip` also saves the frozen target of the PBS pass. The regular live
  mode does not change.
- The local notebook launcher finds the GID of the Docker socket inside the PBS container,
  because the host sandbox may show it as an unassigned GID. The PBS job asks for the
  matching primary group. The host permissions of the socket do not change.
- The names of the local notebook containers include the run ID and a random part; `$$`
  alone is not unique across PID namespaces.

## Artifacts

The main run has two generations `local-sync-20261025T154453Z-242318-{1,2}`.

- [Working data, index, MinIO data and PBS history](/storage/github-runner/mainnet-sync-20261025T154453Z-242318.ihGIwP)
- [Result of the restored index](/storage/github-runner/mainnet-sync-20261025T154453Z-242318.ihGIwP/recovered-index-info.json)
- [Result of the restored Core](/storage/github-runner/mainnet-sync-20261025T154453Z-242318.ihGIwP/recovered-core-info.json)
- [Manifest of the resulting checkpoint](/storage/github-runner/mainnet-sync-20261025T154453Z-242318.ihGIwP/core-latest.json)
- [Logs of the builds, tests and earlier failed attempts](/home/administrator/diplomka/coinjoin-pipeline/emulation_logs/_test-results/mainnet-sync-local-20261025)
- [The saved notebook with executed cells](/home/administrator/diplomka/coinjoin-pipeline/coinjoin-runs/notebook-local-20261025-170032-3.MsZqSu/notebooks/BlockSci.ipynb)
- [Chart from the notebook — synthetic regtest](/home/administrator/diplomka/coinjoin-pipeline/coinjoin-runs/notebook-local-20261025-170032-3.MsZqSu/notebooks/local-regtest-index.png)
- [The original persistent-index test with its retained data](/tmp/tmp.ALis9oWVsI)
- [Results of the browser controls and the captured requests](/home/administrator/diplomka/coinjoin-pipeline/emulation_logs/_test-results/mainnet-sync-local-20261025/ui/browser-result.json)

The tests kept the data and logs. Core and the integration PBS/MinIO containers were
stopped after the run. The separate notebook session ran to its one-hour PBS limit after
the interactive check; the saved notebook and the image remain. The index source had a
read-only mount in the kernel, and writes went to a separate directory.

## What the result does not show

The new two-job test holds coinbase-only regtest blocks; it shows neither the mainnet
completeness of the Tx0 candidates nor the quality of the CoinJoin heuristics. It checks
neither the multijob capacity, the full-chain memory, the CESNET S3 throughput nor an
actual login and submission from the Lab to MetaCentrum. Mock tests cover the submission
API locally. The browser also checked the real MainnetPage components over an isolated
Vite server without a backend: passing on edited CPU/RAM, refusing a duplicate start and
resource changes during a run, Stop, the progress estimate, a backup without invented
percentages, an error and completion, and choosing a notebook that takes over the index
resources. Playwright captured every API request; no frontend bridge or remote launcher
loaded during this check. A deployment needs images that contain these changes; this run
did not update the remote `latest` tags.
