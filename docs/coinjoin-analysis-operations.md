# Existing coinjoin-analysis operations

`coinjoin_analysis.program` selects a shipped command or an adapter over an existing
research function. The analyzer repository and its image are not changed. The
ordinary baseline stages keep their existing behavior.

| Program | Operations (`actions`) |
| --- | --- |
| `parse_cj_logs` | `collect_local`, `collect_docker`, `analyze_only` (exactly one) |
| `parse_dumplings` | `process_dumplings`, `detect_false_positives`, `detect_coordinators`, `split_coordinators`, `plot_coinjoins` |
| `ww2_analyze_client` | `process_as25as38`, `process_emulations`, `compute_num_predict_matrix` |
| `scan_results_plot` | No actions; takes `filename` without `.json` |
| `file_check` | No actions; checks the files under the selected target |
| `visualize_coordinators` | No actions; plots flows between coordinators |
| `perf_processing` (`helpers.perf_processing`) | No actions; plots CPU/memory from the target CSV file |
| `analyze_tx_size` | Adapter for `cj_analysis.compute_partial_vsize`, emulation and mainnet |
| `generate_llm_inputs` | Adapter for the existing generator, emulation with known wallets only |
| `update_txids_with_wabisator` | Adapter for the existing metadata updater, mainnet only |
| `coinjoin_statistics` | `wasabi2`, `wasabi1`, `joinmarket`: the CRoCS daily statistics workflow, mainnet only |

The first seven are existing command-line entry points. The next three receive
explicit parameters through `research` and save results the original fixed-input
entry points do not expose. `coinjoin_statistics` runs a sequence of existing
`parse_dumplings` commands; see [CoinJoin Statistics](#coinjoin-statistics). `env_vars` exposes the parsing commands' existing
configuration switches, including analyses enabled through those switches.
Their interpretation, dependencies and supported protocols remain the analyzer's.

## Scenario Lab

Open a finished run, then **coinjoin-analysis operations**. Choose the program,
one or more operations, the protocol, inputs, existing `--env_vars` switches,
and PBS resources. The panel previews the exact YAML before submission.
It also works for mainnet runs without an emulator scenario.

**Data origin** filters the program/actions: emulation offers the labeled sequence
generator; mainnet offers Dumplings and coordinator refresh; transaction size and
diagnostic utilities are available for both. The mainnet page also has a separate
**Additional mainnet analyses** card, which accepts a run ID without a scenario.

The run's **coinjoin-analysis** artifact card includes the operation outputs for
download. Nothing is submitted until the user presses **Run the analysis**.

## PBS with S3 input

```yaml
action: pbs-from-s3
run_id: existing-run
artifacts:
  backend: s3
  uri: s3://my-bucket/runs
  endpoint_url: https://my-s3-endpoint
  credentials_file: ~/.aws/credentials
  profile: my-profile
stages:
  analysis: true
  blocksci: false
  mappings: false
coinjoin_analysis:
  program: parse_dumplings
  data_mode: mainnet
  actions: [detect_coordinators, plot_coinjoins]
  cjtype: ww2
  input_uri: s3://my-bucket/dumplings-dataset
  env_vars: 'DEBUG=False'
pbs:
  analysis:
    ncpus: 4
    mem: 16gb
    scratch: 50gb
    walltime: '04:00:00'
```

Run with `cjp run operations.yaml` (or first use `--dry-run`). Normal S3 stage
locking, job bookkeeping, waiting and failure markers apply. This is a standalone
operation: BlockSci, mappings and the unified report do not run alongside it.

`input_uri` is a dataset prefix, not a Dumplings `manifest.json`. Its files
are copied to `/work/input`, retaining the original directory layout:

- `parse_dumplings`: the selected target contains `Scanner/`, including raw
  Dumplings files or the processed protocol directories needed by the action.
- `ww2_analyze_client`: supply the original experiment layout. In particular,
  as25/as38 operations require those experiments, and matrix computation requires
  their previously computed statistics. Emulation artifacts do not substitute for
  these inputs.
- `scan_results_plot`: the selected target contains
  `results_YYYYMMDD/Scanner/<filename>.json` folders.
- `visualize_coordinators`: the selected target contains the processed
  `Scanner/wasabi2_others/` and `Scanner/wasabi2_zksnacks/` datasets, including
  the coordinator attribution files expected by the existing script.
- `file_check`: the selected target contains the protocol result directories;
  preserve the original `summary.log` two directories above the target if the
  upstream processing-log check is needed.
- `perf_processing`: set `target_paths: [memlog.csv]` (or a nested relative CSV
  path). The dataset must include the CSV columns `timestamp`, `used_MB`,
  `cpu_percent` and `operation`; the script writes a PNG next to that file.
- `parse_cj_logs`: the selected targets are parents of experiment directories,
  as required by the existing command. `collect_local` still needs reachable live
  clients/RPC and the analyzer's existing configuration; stored dumps alone do not
  provide live clients. Supply a dataset containing the expected
  `<experiment>/WalletWasabi/` layout; this action cannot use emulator dumps as
  a replacement for local-client files.

Without `input_uri`, emulation analysis and `process_emulations` copy this run's
`coinjoin-analysis_data` and `coinjoin_emulator_data/data` into
`input/<run-id>/`. The emulation command selects `input/`; client emulation analysis
selects `input/<run-id>/`. Other programs require an explicit dataset.

`target_paths` overrides these defaults with paths relative to `/work/input`.
Only `parse_cj_logs` accepts multiple paths. Absolute paths and `..` components
are refused. Example: `target_paths: [experiment-a, experiment-b]`.

`cjtype` accepts `ww1`, `ww2`, `sw`, or `jm` for Dumplings/client commands;
it is independent of the pipeline's narrower baseline `coinjoin_type` setting.
Multiple `actions` for Dumplings/client commands are passed as repeated
`--action` options. Their execution order is determined by the existing analyzer.
`env_vars` is passed literally as one argument, with the analyzer's existing
semicolon-separated `KEY=VALUE` syntax; shell expressions are not executed.
For CLI compatibility, `load_config` passes a relative path as `--load-config`
to the two parsing commands. The current analyzer source accepts that flag but
does not read the file; configure actual switches with `env_vars`.

## Local Docker or Podman

```yaml
action: coinjoin-analysis
run_dir: existing-run
runtime: docker
coinjoin_analysis:
  program: parse_cj_logs
  actions: [analyze_only]
  env_vars: 'DEBUG=False'
```

For a separate local dataset, add `input_dir: /absolute/path/to/dataset` to
`coinjoin_analysis`. The directory layout and `target_paths` mean the same thing
as on PBS. `input_uri` is for PBS; `input_dir` is for local execution. Each CLI
field is also available as `--analysis-program`, repeated `--analysis-operation`,
`--analysis-cjtype`, repeated `--analysis-target-path`, `--analysis-env-vars`,
`--analysis-input-dir`/`--analysis-input-uri`, `--analysis-load-config`, and
`--analysis-filename`.

## Research adapters

`data_mode` defaults to `emulation`. Set `mainnet` explicitly for mainnet data;
mode validation also applies to CLI/YAML submissions outside Scenario Lab.
Emulation client analysis offers `process_emulations`; the real as25/as38 client
experiments and their prediction matrix belong to mainnet mode.

For a size calculation (both modes):

```yaml
coinjoin_analysis:
  program: analyze_tx_size
  data_mode: emulation
  research:
    transaction_file: transaction.hex
    input_indices: [0, 2]
    output_indices: [1]
```

`transaction_file` is a relative hex-text file or JSON with a `hex` field.
Alternatively supply `tx_hex` directly. `txid` verifies the raw transaction's
identity and can find raw hex in this run's `block_*.json` exports. In mainnet
mode only, `allow_network: true` permits the original helper's mempool.space
lookup if no local hex is found. Regtest never uses this public mainnet source.
Indices are zero-based, checked against the decoded transaction, and must not
repeat. Results are `results/result.json` (selected/total vsize in vbytes) and
`results/transaction.hex`. This does not calculate a fee or infer wallet ownership.

For labeled emulation sequences:

```yaml
coinjoin_analysis:
  program: generate_llm_inputs
  data_mode: emulation
  research:
    min_wallets: 1
    depth: 100
    permute_inputs: false
    permute_outputs: false
    force_limit_anon_score: 100
    plot: true
```

The default source is this run's `coinjoin_tx_info.json`. For an external dataset,
the default is `input/coinjoin_tx_info.json`; override it with one `target_paths`
entry naming the file or its containing directory. Inputs and outputs must carry
known wallet names. Mainnet and unknown labels are refused, rather than treating
missing ground truth as known wallets. Output is `results/llm_inputs.json` plus
an optional leaving-distribution PNG and a summary JSON. No model is trained.

The adapter calls the existing generator directly and persists its return value,
bypassing the original hard-coded main's premature `exit(1)`. It binds the existing
link helper from its current `cj_analysis` location. The unused distance-printing
function contains Python 3.12 syntax in the original source, so only the original
generator/plot definitions before it are loaded on the image's Python 3.11.

For coordinator refresh (mainnet only):

```yaml
coinjoin_analysis:
  program: update_txids_with_wabisator
  data_mode: mainnet
  research:
    allow_network: true
    # Optional dataset-relative override of the image's Wasabi 2 mapping:
    # coordinator_file: txid_coord.json
```

This calls the original updater with explicit input/output files. It fetches
Wabisator metadata and verifies previously unknown TXIDs using mempool.space.
Outputs include `results/txid_coord_source.json`, `results/txid_coord_updated.json`
and the HTTP response bodies/statuses under `results/sources/`; each HTTP request
has a 30-second timeout. The source mapping is preserved. To use the new mapping
in a later analysis, select its output prefix as the next dataset and provide the
path expected by that existing analyzer operation; a refresh does not silently
rewrite a previously published comparison baseline.

All research settings have corresponding `--analysis-*` flags, for example
`--analysis-txid`, repeated `--analysis-input-index`/`--analysis-output-index`,
`--analysis-depth`, `--analysis-plot` and `--analysis-allow-network`.

`coinjoin_research.py` is copied with each invocation. The analyzer image excludes
`scripts/`, so the existing Wabisator updater is packaged unchanged as
`execution/pbs/templates/wabisator_source.txt` and copied alongside the adapter.
A source-parity test checks it against the sibling analyzer checkout when present.
The original script's unconditional bottom-level call is not executed.

The older `emulation/{scenarios,anonymity_score,init_regtest}.py` process-control
scripts remain outside the analysis menu: they start clients/backends, manipulate
wallets and mine blocks. Emulations continue through the existing emulator flow.

## Results and evidence

Each invocation writes to
`coinjoin-analysis-tools_data/<program>/<invocation-id>/`. It retains the input
copy, generated JSON/PNG/PDF files, `analysis.log` and `invocation.json` (argv,
image reference and explicit input source). Separate invocations retain their
previous outputs. The canonical comparison baseline is not modified.
On PBS the exit trap uploads partial results and the log even when analysis fails;
an upload failure prevents a successful stage marker. This is artifact provenance,
not proof that the chosen image or dataset has been tested on a live cluster.

## CoinJoin Statistics

`coinjoin_statistics` reproduces the daily workflow behind the CRoCS CoinJoin
Statistics site in one analysis job. The coinjoin-analysis scripts
`process_ww2.sh`, `process_ww1.sh`, `process_jm.sh` and `visualize_*.sh` are
run in the order of `process_daily.sh`, followed by `file_check`. The selected
`actions` are the protocols to process. JoinMarket always runs last. It restores
the Wasabi false positives only when both Wasabi protocols are selected.

```yaml
coinjoin_analysis:
  program: coinjoin_statistics
  data_mode: mainnet
  actions: [wasabi2, wasabi1, joinmarket]
  input_uri: s3://my-bucket/bitcoin-mainnet/dumplings/Scanner
```

`input_uri` is a Dumplings snapshot, either its flat files or a dataset root
containing `Scanner/`. Converted `*.txt.json` caches are deleted first. The
workflow differs from the CRoCS scripts in these ways:

- Fee rates and false positives come from the image's `data/` instead of
  mempool.space.
- Wallet-count predictions and image montages are skipped. The image has no
  estimation matrices and no ImageMagick.

The job publishes `site/` with the file selection of `backup_and_montage.sh`:
plots, summaries, `summary.json` with the Dumplings height, and the pool
liquidity fragments in `site/texts/`. It also publishes `summary.log`,
`operation.txt` and the job log. The processed `coinjoin_tx_info.json` files
and the snapshot copy are deleted. Scenario Lab builds the statistics pages
from a downloaded `site/`. A full mainnet snapshot needs several hours and
tens of GB of memory, so size `pbs.analysis` accordingly.
