# coinjoin-pipeline

Consolidated control layer for reproducible CoinJoin emulation, BlockSci
analysis, baseline `coinjoin-analysis`, and unified reports. The installed CLI
is thin; heavy work runs in explicitly versioned container images.

This is a local prototype and is not published to PyPI.

Manual mainnet Core/Dumplings → BlockSci synchronization is described in
[the synchronization runbook](docs/mainnet-synchronization.md). It chains six
dependent PBS jobs (restore, producer, BlockSci, checkpoint, pack, clear) and
carries the data from one run to the next in its S3 backups.

## Install and run

Run these from the repository checkout:

```bash
pipx install --editable .
# development install with tests
python3 -m pip install -e '.[test]'
# static checks: Ruff (with fixes), mypy and pylint (fail-under 10)
uv run lint

coinjoin-pipeline version
cjp doctor
coinjoin-pipeline pull

# An experiment is a YAML file; the command line only adds launch values.
cat > joinmarket.yaml <<'YAML'
action: full-run
engine: joinmarket
scenario: scenarios/defaultJoinMarket.json
YAML
cjp run joinmarket.yaml --dry-run
cjp run joinmarket.yaml
# Small regtest Wasabi rounds need an explicit input threshold.
printf 'action: full-run\nengine: wasabi\nscenario: scenarios/overactive-local.json\nmin_input_count: 15\n' > wasabi.yaml
cjp run wasabi.yaml

# CI-style live output for a Kubernetes run.
RUN_ID='<run-id>'
coinjoin-pipeline watch --run-id "$RUN_ID"
# Multiplex controller, uploader, selected engine service, and a frontend log.
coinjoin-pipeline watch --run-id "$RUN_ID" --all \
  --frontend-log "$HOME/$RUN_ID-full-run.log" \
  --save "$HOME/$RUN_ID-unified.log"
# Follow PBS stdout/stderr discovered from that conventional frontend log.
coinjoin-pipeline watch --run-id "$RUN_ID" --pbs-only
# Or identify jobs explicitly when no saved submission log is available.
coinjoin-pipeline watch --run-id "$RUN_ID" --pbs-only \
  --pbs-job coinjoin-analysis=12345.server \
  --pbs-job blocksci=12346.server
```

### Experiment scenarios

The private experiment catalogue is maintained in `OndrejMan/coinjoin-scenarios`.
Clone it beside this checkout and pass its JSON files by path, for example:

```yaml
action: full-run
engine: wasabi
scenario: ../coinjoin-scenarios/fin-cliff.json
min_input_count: 10
```

Relative `scenario` paths are resolved from
the directory where the CLI is invoked. Local Docker/Podman and Kubernetes
launchers mount the selected file's directory read-only; Kubernetes → S3 embeds
its contents in the Job's ConfigMap. No copy into the emulator checkout is needed.
`emulate` and `full-run` always need a scenario; there is no default. The pipeline's
own `scenarios/` holds only the scenarios its tests pass explicitly.
Optional top-level `description` and `category` fields are catalogue metadata:
they are sent unchanged to the emulator but do not affect the pipeline,
coinjoin-analysis, or BlockSci.

### Update an existing checkout

The `pipx` installation above is editable, so the installed CLI uses the
current files from this checkout. To update the pipeline, fetch the new source
and confirm the installed command:

```bash
git pull --ff-only
cjp version
```

If `pyproject.toml` changes, recreate the `pipx` environment to install the
updated dependencies:

```bash
pipx reinstall coinjoin-pipeline
cjp doctor
```

If `git pull --ff-only` refuses to run because the checkout has local commits
or modifications, resolve those changes before updating; do not force the
pull.

By default the pipeline leaves `min_input_count` unset, so BlockSci applies
its height-aware production threshold. Set `min_input_count: N` for an
intentional positive override. Values below 1 are rejected. Small Wasabi
regtest scenarios normally need a scenario-appropriate explicit value;
otherwise a zero-detection report includes a prominent production-threshold
warning.
Independent emulator labels, raw detector metrics,
and provenance rules are defined in
[Analysis semantics](docs/analysis-semantics.md).
When verified emulator labels are available, the unified report (schema 3.0)
evaluates both BlockSci and the filtered, normalized `coinjoin-analysis`
baseline against the same exported transaction universe under
`detector_evaluations`. For JoinMarket, BlockSci detection uses the protocol
heuristic; the deprecated subset-matching scan appears only as the
`blocksci_subset_matching` comparison.

### BlockSci multi-chain caveat

BlockSci currently stores its CoinJoin activation-height configuration in
process-global state. Constructing a `blocksci.Blockchain` copies the
configuration from that chain into global values used by the CoinJoin
heuristics. Consequently, a custom Python program or notebook that opens two
chains with different configurations (for example mainnet and regtest) and
then reuses the first chain can evaluate it using the thresholds of the second
chain.

The normal pipeline workflow is unaffected: each analysis process works with
one run configuration, and the record export, diagnostics, and clustering
objects created during report assembly all receive that same `config.json`.
Separate containers, Kubernetes Jobs, and PBS Jobs are also separate processes.
For custom analysis, use one chain configuration per Python process; run
cross-network comparisons in separate processes. A future in-process
multi-chain workflow must first make these thresholds chain-specific in
BlockSci.

Outputs default to `./coinjoin-runs`; override this with `--runs-root`. Images
default to the explicit `latest` tag. Use `--version TAG` (YAML `images.version`)
to apply one coordinated tag to every default image, or `--local-build` for local
development tags. Individual images (`images.emulator`, `images.coinjoin_analysis`,
`images.blocksci`, `images.mappings` and `images.sake`, or `--<component>-image`
at launch) take precedence over the coordinated tag and over `--local-build`
defaults. The corresponding environment overrides also apply in local mode;
explicit images win over the environment. The in-cluster uploader and the pinned
Python image for the PBS report step are not part of that coordinated set: they
carry their own immutable references in `container/uploader.image` and
`container/unified-report.image`, overridable per run with `images.uploader`
and `images.unified_report`.

The uploader image is published by the manual `Publish Uploader Image`
workflow, not by the test pipeline, so **it has to exist in the registry before
the first S3 run**: the Kubernetes Job pulls it for both its preflight and its
uploader container. Run the workflow, put the digest it prints into
`container/uploader.image`, and only then start an S3 run — otherwise the Job
sits in `ImagePullBackOff` (the frontend now aborts on that instead of waiting
out `emulation_timeout`, but the run is still lost).

When publishing component images separately, publish and verify the BlockSci
and coinjoin-emulator images together. The pipeline requires BlockSci's raw
CoinJoin binding and the emulator's in-cluster networking plus producer-label
manifest contract. A coordinated `images.version` tag avoids mixing incompatible
generations.

Developer compatibility entrypoints use the same CLI:

```bash
./runIt.sh run joinmarket.yaml --local-build --dry-run
./run-all.sh local --build-only
```

The host CLI and orchestration run in one Python process from an editable
checkout (`pip install -e .` or `pipx install --editable .`). Container workloads
remain separate processes. `pipeline/compose.yaml`, the standalone exporters
and scenarios are resolved relative to the checkout; a relocated wheel alone
is not a complete execution installation.

The only entry point for an experiment is `cjp run experiment.yaml`. The file
loads into one typed configuration; the command line adds only what belongs to
one launch or one host: `--dry-run`, `--run-id`, `--run-dir`, `--resume-run`,
`--runs-root`, `--runtime` and the image options. Defaults, image selection and
run identity are resolved before execution. The stage-only actions have small
files in `examples/stages/`, for example
`cjp run examples/stages/analyze.yaml --run-dir <run-id>`.
See [Current architecture](docs/architecture.md).

The `--scenario` workflow of `run-all.sh` keeps the generated YAML in
`$EMULATION_LOGS_DIR/.run-all-experiments/` beside the run directories. The
manifest's launch command refers to that file and can start a fresh run from it.

## Commands and contracts

The report label provenance, BlockSci threshold precedence, and distinction
between raw and linked detector results are documented in
[Analysis semantics](docs/analysis-semantics.md). The PBS filesystem, marker,
and scheduler contract is documented in
[PBS stage contract](docs/pbs-stage-contract.md).

Host commands are `doctor`, `pull`, `version`, `watch`, `clean-s3`, and
`download-report`. `watch` discovers the outer Kubernetes pod from `--run-id`
and streams prefixed controller output without starting a container. Pass
`--all` to multiplex the controller, S3 uploader, and the selected engine
service (Wasabi coordinator or the stable JoinMarket distributor);
the namespace defaults to the pipeline default `coinjoin` (pass
`--namespace <namespace>` for the remote examples below). The host-level
`--runs-root` is also used when discovering `.pbs/*.jobid` files.
`--frontend-log` adds an existing `tee` transcript, replayed from its start
rather than its last `--tail` lines, and `--save` preserves the
unified output. `--pbs` adds PBS stdout/stderr to the Kubernetes stream, while
`--pbs-only` needs no kubeconfig. PBS job IDs are discovered from
`.pbs/*.jobid`, from `$HOME/<run-id>-full-run.log`, or supplied explicitly with
repeatable `--pbs-job STAGE=JOB_ID`; job state changes are included in the
prefixed stream. Pipeline actions are `full-run`, `emulate`, `clean`, `analyze`,
`export`, `coinjoin-analysis`, `pbs-from-s3`, `mappings`, `mainnet-sync`, and
`initialize`, plus the `runs`, `scenarios`, and `external` command groups.

`cjp --help` lists every YAML key grouped by configuration section, each with
its description and default, and the launch overrides; every utility answers
`--help` too. Overrides given after `cjp run CONFIG.yaml` replace the file's
value. A `--dry-run` prints the resolved
run directory and component images before the execution plan. Exit codes:
`0` success; `2` invalid configuration or arguments, a failed preflight check
(each problem on its own `ERROR:` line), or an execution failure reported as
`[ERROR] ...`; `3` `clean-s3` not confirmed; `5` an unexpected runtime error,
or a failed `pull`, S3 transfer or log source of a utility; `130` interrupted.
A failed local container may also pass its own exit code through.

Before a new full run, the CLI atomically creates `research_manifest.json` in
the expected run directory. Stage-only commands update the explicitly selected
run. Host data is namespaced as `host_launcher`, preserving established
report fields. It records the resolved configuration, original user command,
requested version, effective images, runtime, timestamps,
working directory, status, and exit code. Sensitive-looking keys are redacted.
`host_launcher.environment` records effective `BLOCKSCI_`, `COINJOIN_`,
`KUBERNETES_`, `MAPPINGS_`, `SAKE_`, and `PBS_` environment options, with resolved
launcher values taking precedence over inherited values. Secret-bearing keys
are omitted from this environment snapshot.
The analytical report separately records resolved image IDs and repository digests.

An S3 `full-run` or `emulate` also copies this record to
`<run-prefix>/research_manifest.json`: once after the exporters are staged, with
status `prepared`, and again when the command ends, with its outcome. The command,
the configuration and the pipeline commit of a run therefore stay with its artifacts
after the frontend's run directory is gone. `--resume-run` first takes over the
launches the S3 copy records and its run directory lacks, so a resume from another
frontend keeps the original launch. `pbs-from-s3` records no launch.

## Artifact backends

`shared-storage` remains the default. Kubernetes and PBS continue to use the
same `/storage` paths in this mode.

The optional `s3` backend uses CESNET/MetaCentrum S3-compatible object storage,
not Amazon AWS. Kubernetes uploads from inside its Job. PBS compute nodes
download into `$SCRATCHDIR`, analyze locally, and upload results. The frontend
only submits `kubectl` and `qsub`.

The bucket must already exist. MetaCentrum PBS jobs use `s5cmd` with a named
profile in a credentials file such as
`/storage/brno2/home/<login>/.aws/credentials`:

```ini
[coinjoin]
aws_access_key_id = <access_key_from_gatekeeper>
aws_secret_access_key = <secret_key_from_gatekeeper>
max_concurrent_requests = 200
max_queue_size = 20000
multipart_threshold = 128MB
multipart_chunksize = 32MB
```

Those two `aws_*` names are credentials-file fields consumed by `s5cmd`, not
supported environment-variable configuration. The pipeline never accepts
credential values through CLI arguments.

The emulation Job reads the same key from a Kubernetes Secret. By default the
frontend creates one per run: a Secret named like the run's Job, made from the
`artifacts.profile` of `artifacts.credentials_file` and owned by the Job. It is deleted as soon
as `full-run` sees the upload marker (or the upload fails), and with the Job in any
case, so a namespace shared with other users holds the key only while a run needs it.
The kubeconfig then needs `create`, `list`, `patch` and `delete` on `secrets`. A Secret
of that name that already exists is never replaced, and the deletion selects the run's
labels, not the name alone. Before anything is created the frontend also refuses a run
ID that already labels a Job, pod or temporary Secret in the namespace
(`coinjoin.run-id=<id>`), the Kubernetes counterpart of the empty-run-prefix check.
`kubernetes.owner: <login>` adds a `coinjoin.owner` label to the Job, its
support objects, the Secret and the emulator's pods, so a namespace shared by several
people shows whose run each object belongs to; a rollback then selects that owner too. A standalone
`emulate` does not wait for the upload, so its Secret lives until the finished Job
expires an hour later.

To use a Secret that is kept in the namespace instead, create it once and set
`artifacts.secret_name`:

```bash
kubectl create secret generic coinjoin-s3-credentials \
  --from-literal=S3_ACCESS_KEY_ID='<access-key>' \
  --from-literal=S3_SECRET_ACCESS_KEY='<secret-key>'
```

### Single-command full run

A `full-run` with `artifacts.backend: s3` orchestrates the whole chain from a
MetaCentrum frontend and waits for every stage: it submits the Kubernetes
emulation Job, polls the bucket for `.k8s/upload.done`, submits both PBS
analyzers in parallel followed by a report job dependent on both, and polls
all three `.pbs/*.done` markers until the results land in the bucket.
The analyzers are selectable: drop `stages.analysis`, `stages.blocksci` or both to run
only part of the chain. Without coinjoin-analysis there is no baseline, so BlockSci
publishes its results without the unified report and `stages.mappings` is refused;
without either analyzer the run ends at the emulation upload and never calls `qsub`.
Requirements: kubeconfig + `qsub` + `s5cmd` on
the frontend, and a pre-created namespace (`kubernetes.reuse_namespace: true` is required). Run it inside `screen`/`tmux` —
the process blocks for the whole emulation and analysis.

```yaml
action: full-run
engine: wasabi
coinjoin_type: wasabi2
driver: kubernetes
min_input_count: 15
emulation_timeout: 21600
kubernetes:
  namespace: <namespace>
  reuse_namespace: true
artifacts:
  backend: s3
  uri: s3://coinjoin-thesis/runs
  endpoint_url: https://s3.cl4.du.cesnet.cz
  credentials_file: /storage/brno2/home/<login>/.aws/credentials
  profile: coinjoin
stages:
  analysis: true
  blocksci: true
  mappings: true
```

```bash
cjp run s3-full-run.yaml --run-id '<id>'
```

The orchestrator refuses a run ID whose bucket prefix contains any object,
cancels dependent mappings/report jobs if a prerequisite fails, and prints `kubectl`
diagnostics if the emulation fails. A still-running sibling analyzer job is
deliberately left in place on failure — its results upload to the bucket
independently and remain usable; cancel it with `qdel` if they are not needed. `parallel`
and a full-run `blocksci.script` are not supported in S3 mode. Optional
`stages.mappings` is supported for Wasabi 2: it runs after coinjoin-analysis and
the unified-report job waits for and embeds its uploaded mapping results. The
`.pipeline.lock` in the runs root is held for the whole duration — one S3
full-run at a time per frontend. The end-to-end test for this path is
`tests/test-kubernetes-s3-minio.sh` (k3d + local PBS rig + MinIO). The
separate archive contract is `tests/test-bitcoin-block-archive-s3-minio.sh`:
it downloads only public mainnet heights 0 and 1, builds and runs the
`bitcoin-block-archive` Docker image to archive them into MinIO using S3
environment secrets, and verifies the PBS BlockSci S3 restore and parse.

`tests/test-mainnet-sync-mainnet.sh` exercises real Bitcoin Core P2P synchronization
near height 400000, Dumplings scanning beyond its mainnet start height 336861,
completed-file archival, two PBS/BlockSci generations,
and recovery of both Core and the index against local MinIO. Histories that fit
in the two active block files recover those files from the Core checkpoint.
The automatic
`mainnet-sync-bounded` CI job builds its components and retains diagnostics on
failure. See [the bounded mainnet E2E runbook](docs/mainnet-synchronization.md#bounded-mainnet-e2e-in-ci)
for limits, retained evidence and the standalone command.

For an S3 `full-run`, a missing `run_id` is generated automatically;
`stages.analysis`, `stages.blocksci` and `stages.mappings` enable their
corresponding PBS stages.

```bash
./runIt.sh run examples/metacentrum-regtest-s3.yaml
```

`examples/metacentrum-regtest-s3.yaml` is the recommended regtest full-run
configuration. The examples hold placeholders, not a real account: copy the ones you use
to `examples/local/` (ignored by Git) and replace `<login>`, `your-bucket`,
`your-namespace` and `<namespace-kubeconfig>` with your MetaCentrum login, S3 bucket,
Kubernetes namespace and kubeconfig, then run that copy. Mainnet is deliberately split into two submissions:

For the default JoinMarket regtest scenario, use the measured MetaCentrum
profile. It requests 2 CPUs, 16 GB RAM, and 8 GB scratch for BlockSci, plus
2 CPUs and 8 GB RAM for coinjoin-analysis:

```bash
./runIt.sh run examples/metacentrum-joinmarket-regtest-s3.yaml
```

The profile uses `scenarios/defaultJoinMarket.json`, enables regtest detector
thresholds, and leaves the larger global/mainnet PBS defaults unchanged.

```bash
# Produces a checksummed reusable BlockSci cache from the external mainnet datadir.
./runIt.sh run examples/metacentrum-mainnet-parse.yaml

# Reuses that run ID and cache to produce joinmarket-mainnet-summary.json.
./runIt.sh run examples/metacentrum-mainnet-analyze.yaml
```

When the Bitcoin Core node is pruned after archival by
`bitcoin-block-archive`, use the S3 archive variant instead. The parser job
downloads every schema-1 `blkNNNNN.dat.json` sidecar, then only the prefix
`blk00000.dat…` of block files that holds heights up to the requested maximum
(plus a six-block selection margin).
It checks each downloaded file's size and SHA-256 against its sidecar and
refuses a requested height that the sidecars' `height_ranges` do not cover from
height 0. Height ranges are only a preflight: they can include different forks.
Before publishing the cache, the job opens the actual parsed BlockSci index,
requires exactly `max_block + 1` blocks, and verifies the target block hash.
Mainnet archive parses and updates require `blocksci.expected_block_hash`
pinned independently from a trusted Core
with `bitcoin-cli getblockhash HEIGHT`. Change this hash whenever `max_block`
changes. PBS does not fetch a checkpoint from an external service.
The manifest records the observed `exported_max_block` and `exported_block_hash`,
the last archive file scanned and a digest
of the scanned files, which an incremental update needs. The second job compares the cached BlockSci index with a
`coinjoin_tx_info.json` produced from Dumplings; it reports baseline agreement,
not ground-truth precision/recall.

```bash
./runIt.sh run examples/metacentrum-mainnet-s3-blocks-parse.yaml
./runIt.sh run examples/metacentrum-mainnet-dumplings-report.yaml
```

Before submission, set the same `run_id` and immutable `images.version` in
both mainnet files, update the inclusive `blocksci.max_block`, and replace the
shared-storage paths. The analyze job writes the summary under
`blocksci-custom-analysis_data/` in the same S3 run. It is detector output
without emulator ground truth, not a precision/recall unified report.

A YAML file must name its `action`; there is no default, so a file without one
never starts a full emulation by accident. A launch override after `cjp run FILE`
replaces the same value from YAML. Programmatic consumers load the schema through
the immutable typed `PipelineConfiguration.from_mapping()`, or
`PipelineConfiguration.from_paths()` for YAML paths such as `artifacts.uri`.

The typed schema also covers the advanced modes used by decomposed and mainnet
workflows:

- `blocksci`: `workflow`, `task`, custom script/notebook settings, cache source,
  external Bitcoin or BlockSci source, network, and inclusive maximum block;
- `joinmarket`: detector selection, base fee, percentage fee, and search depth;
- `mappings`: fee model, decomposition limit, mode, timeouts, and Sake seed;
- `external`: external Bitcoin datadir, baseline JSON, repeatable false-positive
  files, network, minimum free disk space, and resume mode for
  `action: external-analyze`;
- `images`: coordinated version/local-build selection and every component image
  override, including the separately versioned `uploader` and
  `unified_report` images;
- `pbs`: shared and per-stage resources, PBS image overrides, Bitcoin datadir,
  and unified-report resources;
- top-level operational fields such as `action`, `runtime`, `runs_root`,
  `run_dir`, `analysis_action`, and `emulation_timeout`.

External mainnet analysis can therefore use the same typed configuration path:

```bash
./runIt.sh run examples/external-analysis.yaml
```

For an existing external run, run `examples/stages/external-resume.yaml` with
`--run-id` of that run.

Cross-field restrictions apply to every file. For example,
external Bitcoin and external BlockSci sources are mutually exclusive, and
external parsing is valid only for the appropriate `pbs-from-s3` reusable or
cached task.

### Decomposed two-command workflow

The same chain can run as two independent commands — emulation from any
machine with a kubeconfig, analysis later from the frontend.

Submit Kubernetes emulation and upload (`emulate.yaml`):

```yaml
action: emulate
engine: wasabi
driver: kubernetes
kubernetes:
  namespace: <namespace>
  reuse_namespace: true
artifacts:
  backend: s3
  uri: s3://coinjoin-thesis/runs
  endpoint_url: https://s3.cl4.du.cesnet.cz
  credentials_file: /storage/brno2/home/<login>/.aws/credentials
  profile: coinjoin
```

Submit PBS analysis for the existing run (`analyze.yaml`):

```yaml
action: pbs-from-s3
engine: wasabi
min_input_count: 15
artifacts:
  backend: s3
  uri: s3://coinjoin-thesis/runs
  endpoint_url: https://s3.cl4.du.cesnet.cz
  credentials_file: /storage/brno2/home/<login>/.aws/credentials
  profile: coinjoin
stages:
  analysis: true
  blocksci: true
```

```bash
cjp run emulate.yaml --run-id '<id>'
cjp run analyze.yaml --run-id '<id>'
```

The remaining `pbs-from-s3` examples show only what differs from `analyze.yaml`;
each keeps its `action`, `engine` and `artifacts` settings and runs with
`cjp run FILE.yaml --run-id '<id>'`.

`s5cmd` must be available on PBS compute nodes and is included in the pipeline
image used by the Kubernetes uploader; the single-command full run additionally
needs it on the frontend PATH for marker polling. In the two-command workflow
nothing waits — monitor the jobs with `qstat` and check markers in the bucket
yourself. Submission is serialized per run under
`<runs-root>/<run-id>/.pbs-submit.lock`; every obtained job ID is persisted
under `.pbs/`, and a later invocation refuses to overlap jobs that are still
queued/running or whose state cannot be verified.

When both PBS stages are selected, coinjoin-analysis and BlockSci analysis run
independently. The BlockSci job persists detector, diagnostics, and clustering
results in `blocksci-analysis_data/blocksci_analysis.json`. A lightweight
report-only PBS job is submitted with an `afterok` dependency on both analyzer
job IDs; it merges precomputed artifacts without loading BlockSci or raw
Bitcoin data and uploads `coinjoinPipeline_data`. The single-command
`full-run` waits for the emulation upload, both analyzer jobs, and that final
report job before returning. The decomposed `pbs-from-s3` command submits this
three-job analysis graph without waiting; adding mappings expands it to four
jobs.

For Wasabi 2, add `stages.mappings: true` to either S3 analysis file. The mappings
job uses `afterok` on coinjoin-analysis when both are submitted together,
uploads `coinjoin-mappings_data/{enumerator.json,sake.json,coinjoin_mappings.json}`
and becomes a third dependency of the report job. To run mappings later against
an existing baseline without rerunning either analyzer:

```yaml
coinjoin_type: wasabi2
stages:
  mappings: true
```

This mappings-only form expects
`coinjoin-analysis_data/coinjoin_tx_info.json` to already exist in the run
prefix and does not regenerate the unified report by itself.

### Saved BlockSci index

All S3 PBS parsing workflows publish the same
`blocksci-parse_data/{blocksci_index.tar.zst,blocksci_index.tar.zst.sha256,manifest.json}`,
a backup in the format of the persistent index's: the zstd archive holds the index's
`config.json`, `files.txt`, `manifest.json` and `parsed/`, and the schema 1.1
manifest records its run, image, source, exported maximum block, members and SHA-256.
The default `blocksci.workflow: combined` parses and analyzes in one job.
`reusable` splits parsing and analysis into dependent jobs; `cached` skips
parsing and reads a saved index. The job arrangement does not change the S3 format.
S3 never receives the loose working index.

Archive transfer shares the persistent mainnet backup's implementation. Uploads
stream through tar, zstd and SHA-256 into S3 when `s5 pipe` is supported, otherwise
through a temporary archive on job scratch. Restores stream directly into job
scratch and verify the checksum before analysis or Jupyter can use the index.
The manifest is uploaded last; a failed publication is not a reusable index.
Emulation indexes do not use persistent or multijob storage. The persistent
mainnet index retains its existing location, direct reads, updates and backup lifecycle.

Use the split graph in a complete run:

```yaml
# added to the S3 full-run file
blocksci:
  workflow: reusable
```

Or publish only the parsed cache for an existing S3 run:

```yaml
stages:
  blocksci: true
blocksci:
  workflow: reusable
  task: parse
```

The cache producer can also start from data already available to MetaCentrum
under `/storage`. To parse an external Bitcoin Core coin directory (the
directory that directly contains `blocks/`), provide the network and an
inclusive maximum block height:

```yaml
stages:
  blocksci: true
blocksci:
  workflow: reusable
  task: parse
  external_bitcoin_datadir: /storage/brno2/home/<login>/bitcoin
  network: bitcoin
  max_block: 850000
```

To reuse an index parsed elsewhere, point at a BlockSci directory containing
`config.json` and `parsed/`. The import job copies it into the canonical run
layout, rewrites the parsed-data path, verifies the expected files, and
publishes the same checksummed cache:

```yaml
stages:
  blocksci: true
blocksci:
  workflow: reusable
  task: parse
  external_blocksci_dir: /storage/brno2/home/<login>/blocksci_data
```

To incrementally advance an external mainnet cache after Bitcoin Core has
received newer blocks, use the existing cache run as the source and a fresh
run ID as the target. Use the same block source as the source cache: a cache
parsed from a coin directory updates from that directory, and a cache parsed
from the block archive updates with `blocksci.bitcoin_blocks_uri` instead of
`blocksci.external_bitcoin_datadir`:

```yaml
engine: joinmarket
stages:
  blocksci: true
blocksci:
  workflow: cached
  task: update
  cache_source_run_id: mainnet-850000
  external_bitcoin_datadir: /storage/brno2/home/<login>/bitcoin
  network: bitcoin
  max_block: 850100
```

```bash
cjp run update.yaml --run-id mainnet-850100
```

The target maximum is inclusive and must be greater than the maximum recorded
by the source cache. The target prefix must be completely empty. The job
verifies and extracts the source cache, runs only the incremental
`blocksci_parser update`, and uploads a new checksummed cache under the target
run. It never overwrites the source cache, so a parser or upload failure leaves
the last successful generation intact. From the block archive it downloads only
the last file the source cache scanned, the newer files, and any older file that
holds a height above the source maximum, after checking that the files the
source cache indexed are unchanged. Archive updates also verify that the
result preserves the source cache's block hash at its recorded height. Older
archive caches without `exported_block_hash` must be parsed again; they are
rejected before block downloads. A mainnet archive update needs a new
`blocksci.expected_block_hash` for its target `blocksci.max_block`.
Use the new target run ID for later
`cached` script or notebook jobs.

After either command finishes, use the normal `cached` `notebook` or `script`
command for the same `<id>`. Those tasks need only the cache. The standard
`detect` task additionally needs the run's emulator labels and exported block
metadata, so an otherwise empty external-data run is intended for notebook or
custom-script analysis rather than the unified emulator comparison report.

An analysis can read such a cache without touching it: give the script,
external, or notebook task a fresh run ID, `blocksci.workflow: cached`
and `blocksci.cache_source_run_id` of the cache run. The job checks the
source manifest, writes outputs and markers under its own run, and stores the
source manifest next to the outputs as `blocksci-cache-manifest.json`. For the
script task, `blocksci.pin_height` and the half-open
`blocksci.scan_start_height`/`blocksci.scan_end_height` reach the script as
`BLOCKSCI_MAX_BLOCK` (pin + 1), `SCAN_START_HEIGHT` and `SCAN_END_HEIGHT`;
`examples/metacentrum-mainnet-analyze.yaml` uses all of them.

For the mainnet index, keep the working copy out of S3:
`blocksci.index_dir` points the parse, update and analysis jobs at one
persistent directory, usually on MetaCentrum multijob scratch
(`pbs.blocksci.multijob: brno`). A manually started `blocksci.task: backup`
job archives it into one S3 backup (`blocksci.index_backup_uri`), deleting the
previous one first. Updates take a writer lock and analyses and the backup a shared
one; an update restores a missing or incomplete working copy from the backup
first. See `examples/metacentrum-mainnet-index-update.yaml`,
`examples/metacentrum-mainnet-index-backup.yaml` and the persistent-index
section of `docs/pbs-stage-contract.md`.

Run the default detector later without invoking `blocksci_parser`:

```yaml
min_input_count: 15
stages:
  blocksci: true
blocksci:
  workflow: cached
  task: detect
```

Custom scripts must be on shared `/storage` so the PBS compute node can bind
them read-only. They receive `ACTIVE_RUN_ID`, `BLOCKSCI_CONFIG`,
`BLOCKSCI_RUN_DIR`, and `BLOCKSCI_OUTPUT_DIR`; outputs under the latter are
uploaded to `blocksci-custom-analysis_data/`:

```yaml
stages:
  blocksci: true
blocksci:
  workflow: cached
  task: script
  script: /storage/brno2/home/<login>/analysis.py
```

Jupyter is a separate long-lived PBS task and never blocks `full-run` or the
unified report. The current BlockSci image installs Jupyter at image-build
time, so this task does not call `build.sh`, compile BlockSci, or parse again:

```yaml
stages:
  blocksci: true
blocksci:
  workflow: cached
  task: notebook
  notebook_port: 8888
  notebooks_dir: /storage/brno2/home/<login>/notebooks
```

The PBS output prints the assigned node, port, and Jupyter token. Stream that
output with `./runIt.sh watch --run-id '<id>' --pbs-only --pbs` and create an
SSH tunnel to the assigned compute node through the normal MetaCentrum access
path. Stop the PBS job when finished. Without `blocksci.notebooks_dir` Jupyter
saves to `~/coinjoin-notebooks/<run-id>` in the submitter's home; when the job
ends it uploads that directory to `blocksci-notebooks_data/` and removes it from
the home once S3 lists every file with its size and none changed during the
upload. A supplied shared directory is uploaded too and always stays.

After `.pbs/unified-report.done` appears, download the canonical report
directly on the frontend without starting Docker or Podman:

```bash
./runIt.sh download-report \
  --run-id '<id>' \
  --artifact-uri s3://coinjoin-thesis/runs \
  --s3-endpoint-url https://s3.cl4.du.cesnet.cz \
  --s3-credentials-file /storage/brno2/home/<login>/.aws/credentials \
  --s3-profile coinjoin
```

The default destination is
`coinjoin-runs/<run-id>/coinjoinPipeline_data`; use the host
`--runs-root PATH` option or `--output-dir PATH` to change it. The command
refuses a failed or incomplete report stage and requires `s5cmd` on `PATH`.

### Cleaning the S3 backend

To remove artifacts from the bucket, `clean-s3` deletes every object under an
S3 run root (or a single run) directly on the frontend, without Docker/Podman.
It is irreversible, so it first lists the matching objects and, unless `--yes`
is given, requires you to retype the target prefix at an interactive prompt
(non-interactive stdin is refused without `--yes`). Use `--dry-run` to preview.

```bash
# Preview what a full wipe of the runs root would remove.
./runIt.sh clean-s3 --dry-run \
  --artifact-uri s3://<bucket>/runs \
  --s3-endpoint-url https://s3.cl4.du.cesnet.cz \
  --s3-credentials-file /storage/brno2/home/<login>/.aws/credentials \
  --s3-profile coinjoin

# Delete a single run non-interactively.
./runIt.sh clean-s3 --yes \
  --run-id '<id>' \
  --artifact-uri s3://<bucket>/runs \
  --s3-endpoint-url https://s3.cl4.du.cesnet.cz \
  --s3-credentials-file /storage/brno2/home/<login>/.aws/credentials \
  --s3-profile coinjoin
```

Omit `--run-id` to clean everything under `--artifact-uri`; point `--artifact-uri`
at the bucket root (`s3://<bucket>`) to wipe the whole bucket. The
`--artifact-uri`, `--s3-*` options also read the `ARTIFACT_URI`, `S3_ENDPOINT_URL`,
`S3_CREDENTIALS_FILE`, and `S3_PROFILE` environment variables.

`stages.blocksci` by itself remains available only to `pbs-from-s3`; it retains
the combined BlockSci-plus-report behavior and requires the remote run to
already contain `coinjoin-analysis_data/coinjoin_tx_info.json`. Both S3 report
paths require and upload the canonical `coinjoinPipeline_data` output.

The general `pbs.{ncpus,mem,scratch,walltime}` values are fallbacks. Use
`pbs.blocksci.*`, `pbs.analysis.*` and `pbs.mappings.*` to size those
allocations independently. A stage-specific value takes
precedence over the general fallback and otherwise retains that stage's
existing default.

Every PBS job asks for `scratch_ssd` by default. `pbs.scratch_type: local`
(or per stage, such as `pbs.blocksci.scratch_type`) requests `scratch_local` instead, which is a
spinning disk on most MetaCentrum nodes but available on every node.

The report-only job defaults to 2 CPUs, 8 GB RAM, 10 GB scratch, and a one-hour
walltime. Use `pbs.unified_report.{ncpus,mem,scratch,walltime}` to override
only that job; the shared `pbs.*` resources remain the fallback.

## Rootless Podman test host

The Podman E2E test checks the host's inotify limits before launching DinD.
Rootless containers share the host user's budget with IDEs and other processes;
exhausting it can stop container startup with `too many open files` even when
disk space and `ulimit -n` are sufficient. Configure the runner once:

```bash
sudo sysctl -w fs.inotify.max_user_instances=1024 fs.inotify.max_user_watches=524288
```

To keep these values after reboot, put the following in
`/etc/sysctl.d/90-coinjoin-runner.conf` and run `sudo sysctl --system`:

```text
fs.inotify.max_user_instances = 1024
fs.inotify.max_user_watches = 524288
```

## Security

The host process invokes the selected Docker or Podman CLI to manage component
containers. Run trusted component images. Rootless Podman is selected with
`--runtime podman`; Kubernetes and PBS work uses `kubectl`, `qsub`, `qstat`,
`qdel` and the shared S3 client on the frontend.

## Layout

- `src/coinjoin_pipeline/`: CLI, typed configuration, run context and host commands.
- `src/coinjoin_pipeline/execution/`: stage graph, container/Kubernetes/PBS adapters, templates and run catalog.
- `src/coinjoin_pipeline/storage/`: shared S3 transport.
- `pipeline/exporters/`: shared container worker, analytical artifact and JSON/Markdown report.
- `container/`: uploader image build plus the committed image references
  (`uploader.image`, `unified-report.image`).
- `scenarios/`: canonical scenarios.
- `tests/`: configuration, worker/report, scheduler, Podman and Kubernetes coverage.

Historical emulation logs were deliberately not copied. See `MIGRATION.md`.
