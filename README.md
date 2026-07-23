# coinjoin-pipeline

Consolidated control layer for reproducible CoinJoin emulation, BlockSci
analysis, baseline `coinjoin-analysis`, and unified reports. The installed CLI
is thin; heavy work runs in explicitly versioned container images.

This is a local prototype and is not published to PyPI.

## Install and run

```bash
pipx install /home/administrator/diplomka/coinjoin-pipeline
# development install with the optional interactive builder and tests
python3 -m pip install -e '/home/administrator/diplomka/coinjoin-pipeline[builder,test]'

coinjoin-pipeline version
cjp doctor
coinjoin-pipeline pull
coinjoin-pipeline full-run --engine joinmarket --dry-run
coinjoin-pipeline full-run --engine joinmarket
# Small regtest Wasabi rounds need an explicit test-threshold opt-in.
coinjoin-pipeline full-run --engine wasabi \
  --scenario scenarios/overactive-local.json \
  --test-values \
  --min-input-count 15

# CI-style live output for a Kubernetes run.
RUN_ID='<run-id>'
coinjoin-pipeline watch --run-id "$RUN_ID"
# Multiplex controller, uploader, coordinator, and an existing frontend tee log.
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

By default the wrapper leaves `--min-input-count` unset, so BlockSci applies
its height-aware production threshold (or its test threshold when
`--test-values` is explicitly selected). Pass `--min-input-count N` only for
an intentional positive override. Values below 1 are rejected. Small Wasabi
regtest scenarios normally require explicit `--test-values` and may need a
scenario-appropriate `--min-input-count`; otherwise a zero-detection report
includes a prominent production-threshold warning.
Independent emulator labels, raw detector metrics,
and provenance rules are defined in
[Analysis semantics](docs/analysis-semantics.md).
When verified emulator labels are available, unified report schema 1.7
evaluates both BlockSci and the filtered, normalized `coinjoin-analysis`
baseline against the same exported transaction universe while preserving the
legacy BlockSci confusion-matrix field.

Outputs default to `./coinjoin-runs`; override this with `--runs-root`. Images
default to the explicit `latest` tag. Use `--version TAG` to apply one coordinated
tag to every default image, or `--local-build` for local development tags.
Individual overrides (`--pipeline-image`, `--emulator-image`,
`--coinjoin-analysis-image`, `--blocksci-image`, `--mappings-image`, and
`--sake-image`) take precedence over the coordinated tag.

When publishing component images separately, publish and verify the BlockSci
and coinjoin-emulator images before the coinjoin-pipeline image. The pipeline
requires BlockSci's raw CoinJoin binding and the emulator's in-cluster
networking plus producer-label manifest contract. A coordinated `--version`
tag avoids mixing incompatible generations.

Developer compatibility entrypoints use the same CLI:

```bash
./runIt.sh full-run --engine joinmarket --local-build --dry-run
./run-all.sh local --build-only
```

`run-pipeline-image.sh` is also a compatibility shim. Its argument parsing,
validation, environment forwarding, and Docker/Podman command construction live
in the testable `coinjoin_pipeline.pipeline_image` module and are exposed as the
`coinjoin-pipeline-image` console command. It uses the selected host runtime
socket; `--build` requires a source checkout (use `--source-root` when running
from an installed wheel).

## Commands and contracts

The report label provenance, BlockSci threshold precedence, and distinction
between raw and linked detector results are documented in
[Analysis semantics](docs/analysis-semantics.md). The PBS filesystem, marker,
and scheduler contract is documented in
[PBS stage contract](docs/pbs-stage-contract.md).

Host commands are `doctor`, `pull`, `version`, `builder`, `watch`, and
`download-report`. `watch` discovers the outer Kubernetes pod from `--run-id`
and streams prefixed controller output without starting a container. Pass
`--all` to multiplex the controller, S3 uploader, and Wasabi coordinator;
the namespace defaults to the pipeline default `coinjoin` (pass
`--namespace man5-ns` for the remote examples below). The host-level
`--runs-root` is also used when discovering `.pbs/*.jobid` files.
`--frontend-log` adds an existing `tee` transcript, and `--save` preserves the
unified output. `--pbs` adds PBS stdout/stderr to the Kubernetes stream, while
`--pbs-only` needs no kubeconfig. PBS job IDs are discovered from
`.pbs/*.jobid`, from `$HOME/<run-id>-full-run.log`, or supplied explicitly with
repeatable `--pbs-job STAGE=JOB_ID`; job state changes are included in the
prefixed stream. All merged parser
commands remain represented by `command_metadata.json`: `full-run`, `recreate`,
`clean`, `analyze`, `export`, `coinjoin-analysis`, `pbs-from-s3`, `mappings`, `initialize`, and
the `runs`, `scenarios`, and `external` command groups.

Install the `builder` extra and run `coinjoin-pipeline builder` for the existing
interactive paste/edit, completion, contextual-help, validation, and preflight
workflow. Verify metadata/parser parity with:

```bash
./tests/test-command-builder-contract.sh
```

Before a new full run, the CLI atomically creates `research_manifest.json` in
the expected run directory. Stage-only commands update the explicitly selected
run. Host data is namespaced as `host_launcher`, preserving established
wrapper/report fields. It records structured arguments, the exact rendered
runtime command, requested version, effective images, runtime, timestamps,
working directory, status, and exit code. Sensitive-looking keys are redacted.
The wrapper report separately records resolved image IDs and repository digests.

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

Provision the Kubernetes Secret separately:

```bash
kubectl create secret generic coinjoin-s3-credentials \
  --from-literal=S3_ACCESS_KEY_ID='<access-key>' \
  --from-literal=S3_SECRET_ACCESS_KEY='<secret-key>' \
  --from-literal=S3_DEFAULT_REGION='us-east-1'
```

### Single-command full run

`full-run --artifact-backend s3` orchestrates the whole chain from a
MetaCentrum frontend and waits for every stage: it submits the Kubernetes
emulation Job, polls the bucket for `.k8s/upload.done`, submits both PBS
analyzers in parallel followed by a report job dependent on both, and polls
all three `.pbs/*.done` markers until the results land in the bucket.
Requirements: kubeconfig + `qsub` + `s5cmd` on
the frontend, `PBS_FRONTEND_DIRECT=1`, and a pre-created namespace with the
Secret (`--reuse-namespace` is required). Run it inside `screen`/`tmux` —
the process blocks for the whole emulation and analysis.

```bash
PBS_FRONTEND_DIRECT=1 coinjoin-pipeline full-run \
  --engine wasabi \
  --coinjoin-type wasabi2 \
  --driver kubernetes \
  --namespace man5-ns --reuse-namespace \
  --artifact-backend s3 \
  --artifact-uri s3://coinjoin-thesis/runs \
  --s3-endpoint-url https://s3.cl4.du.cesnet.cz \
  --s3-secret-name coinjoin-s3-credentials \
  --s3-credentials-file /storage/brno2/home/xman/.aws/credentials \
  --s3-profile coinjoin \
  --run-id '<id>' \
  --test-values \
  --analysisPbs \
  --blocksciPbs \
  --mappingsPbs \
  --emulation-timeout 21600
```

The orchestrator refuses a `--run-id` whose bucket prefix contains any object,
cancels dependent mappings/report jobs if a prerequisite fails, and prints `kubectl`
diagnostics if the emulation fails. A still-running sibling analyzer job is
deliberately left in place on failure — its results upload to the bucket
independently and remain usable; cancel it with `qdel` if they are not needed. `--parallel`
and full-run `--blocksci-script` are not supported in S3 mode. Optional
`--mappingsPbs` is supported for Wasabi 2: it runs after coinjoin-analysis and
the unified-report job waits for and embeds its uploaded mapping results. The
`.pipeline.lock` in the runs root is held for the whole duration — one S3
full-run at a time per frontend. The end-to-end test for this path is
`tests/test-kubernetes-s3-minio.sh` (k3d + local PBS rig + MinIO).

The same run can be described in YAML. `--fromConfiguration` is retained as a
compatibility spelling; new scripts may use `--from-configuration`. The
configuration is translated to the normal CLI arguments before validation, so
the same S3/PBS checks and research manifest apply. For an S3 `full-run`, a
missing `run_id` is generated automatically; `pbs.analysis`, `pbs.blocksci`,
and `pbs.mappings` enable their corresponding PBS stages.

```bash
PBS_FRONTEND_DIRECT=1 ./runIt.sh \
  --fromConfiguration examples/metacentrum-regtest-s3.yaml
```

`examples/metacentrum-regtest-s3.yaml` is the recommended regtest full-run
configuration; `examples/metacentrum-s3.yaml` remains as its compatibility
predecessor. Mainnet is deliberately split into two submissions:

```bash
# Produces a checksummed reusable BlockSci cache from the external mainnet datadir.
PBS_FRONTEND_DIRECT=1 ./runIt.sh \
  --from-configuration examples/metacentrum-mainnet-parse.yaml

# Reuses that run ID and cache to produce joinmarket-mainnet-summary.json.
PBS_FRONTEND_DIRECT=1 ./runIt.sh \
  --from-configuration examples/metacentrum-mainnet-analyze.yaml
```

Before submission, set the same `run_id` and immutable `images.version` in
both mainnet files, update the inclusive `blocksci.max_block`, and replace the
shared-storage paths. The analyze job writes the summary under
`blocksci-custom-analysis_data/` in the same S3 run. It is detector output
without emulator ground truth, not a precision/recall unified report.

An explicit CLI option after `--fromConfiguration FILE` overrides the same
value from YAML. Programmatic consumers can load the schema through the
immutable typed `PipelineConfiguration.from_yaml()` model exported by
`coinjoin_pipeline` and call `to_arguments()` when they need the public CLI
representation.

The typed schema also covers the advanced modes used by decomposed and mainnet
workflows:

- `blocksci`: `workflow`, `task`, custom script/notebook settings, cache source,
  external Bitcoin or BlockSci source, network, and inclusive maximum block;
- `joinmarket`: detector selection, base fee, percentage fee, and search depth;
- `mappings`: fee model, decomposition limit, mode, timeouts, and Sake seed;
- `external`: external Bitcoin datadir, baseline JSON, repeatable false-positive
  files, network, minimum free disk space, and resume mode for
  `action: external-analyze` (also accepts `action: external analyze`);
- `images`: coordinated version/local-build selection and every component image
  override;
- `pbs`: shared and per-stage resources, PBS image overrides, Bitcoin datadir,
  and unified-report resources;
- top-level operational fields such as `action`, `runtime`, `runs_root`,
  `run_dir`, `analysis_action`, and `emulation_timeout`.

External mainnet analysis can therefore use the same typed configuration path:

```bash
./runIt.sh --from-configuration examples/external-analysis.yaml
```

For an existing external run, retain `run_id`, remove `bitcoin_datadir` and
`baseline`, and set `external.resume: true`.

Cross-field restrictions remain identical to CLI validation. For example,
external Bitcoin and external BlockSci sources are mutually exclusive, and
external parsing is valid only for the appropriate `pbs-from-s3` reusable or
cached task.

### Decomposed two-command workflow

The same chain can run as two independent commands — emulation from any
machine with a kubeconfig, analysis later from the frontend.

Submit Kubernetes emulation and upload:

```bash
coinjoin-pipeline recreate \
  --driver kubernetes \
  --namespace man5-ns --reuse-namespace \
  --artifact-backend s3 \
  --artifact-uri s3://coinjoin-thesis/runs \
  --s3-endpoint-url https://s3.cl4.du.cesnet.cz \
  --s3-secret-name coinjoin-s3-credentials \
  --run-id '<id>' \
  --engine wasabi
```

Submit PBS analysis for the existing run:

```bash
PBS_FRONTEND_DIRECT=1 coinjoin-pipeline pbs-from-s3 \
  --run-id '<id>' \
  --artifact-uri s3://coinjoin-thesis/runs \
  --s3-endpoint-url https://s3.cl4.du.cesnet.cz \
  --s3-credentials-file /storage/brno2/home/xman/.aws/credentials \
  --s3-profile coinjoin \
  --engine wasabi \
  --test-values \
  --analysisPbs \
  --blocksciPbs
```

`s5cmd` must be available on PBS compute nodes and is included in the pipeline
image used by the Kubernetes uploader; the single-command full run additionally
needs it on the frontend PATH for marker polling. In the two-command workflow
nothing waits — monitor the jobs
with `qstat` and check markers in the bucket yourself.

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

For Wasabi 2, add `--mappingsPbs` to either S3 analysis command. The mappings
job uses `afterok` on coinjoin-analysis when both are submitted together,
uploads `coinjoin-mappings_data/{enumerator.json,sake.json,coinjoin_mappings.json}`
and becomes a third dependency of the report job. To run mappings later against
an existing baseline without rerunning either analyzer:

```bash
PBS_FRONTEND_DIRECT=1 ./runIt.sh pbs-from-s3 \
  --run-id '<id>' \
  --artifact-uri s3://coinjoin-thesis/runs \
  --s3-endpoint-url https://s3.cl4.du.cesnet.cz \
  --s3-credentials-file /storage/brno2/home/<login>/.aws/credentials \
  --s3-profile coinjoin \
  --engine wasabi \
  --coinjoin-type wasabi2 \
  --mappingsPbs
```

This mappings-only form expects
`coinjoin-analysis_data/coinjoin_tx_info.json` to already exist in the run
prefix and does not regenerate the unified report by itself.

### Reusable BlockSci parse cache

S3 PBS runs can separate immutable parsing from repeatable analysis. The
default `--blocksci-workflow combined` keeps the original one-job behavior.
`reusable` submits `blocksci-parse` and then a dependent BlockSci task;
`cached` skips parsing and consumes an existing `blocksci-parse_data` cache.
The cache is a gzip-compressed tar archive with a SHA-256 sidecar and a schema
1.0 manifest recording the run, image, and exported maximum block.

Use the split graph in a complete run:

```bash
./runIt.sh full-run \
  ... \
  --run-id '<id>' \
  --artifact-backend s3 \
  --analysisPbs --blocksciPbs \
  --blocksci-workflow reusable
```

Or publish only the parsed cache for an existing S3 run:

```bash
PBS_FRONTEND_DIRECT=1 ./runIt.sh pbs-from-s3 \
  --run-id '<id>' \
  --artifact-uri s3://coinjoin-thesis/runs \
  --s3-endpoint-url https://s3.cl4.du.cesnet.cz \
  --s3-credentials-file /storage/brno2/home/<login>/.aws/credentials \
  --s3-profile coinjoin \
  --engine wasabi \
  --blocksciPbs \
  --blocksci-workflow reusable \
  --blocksci-task parse
```

The cache producer can also start from data already available to MetaCentrum
under `/storage`. To parse an external Bitcoin Core coin directory (the
directory that directly contains `blocks/`), provide the network and an
inclusive maximum block height:

```bash
PBS_FRONTEND_DIRECT=1 ./runIt.sh pbs-from-s3 \
  ... \
  --run-id '<id>' \
  --engine wasabi \
  --blocksciPbs \
  --blocksci-workflow reusable \
  --blocksci-task parse \
  --blocksci-external-bitcoin-datadir /storage/brno2/home/<login>/bitcoin \
  --blocksci-network bitcoin \
  --blocksci-max-block 850000
```

To reuse an index parsed elsewhere, point at a BlockSci directory containing
`config.json` and `parsed/`. The import job copies it into the canonical run
layout, rewrites the parsed-data path, verifies the expected files, and
publishes the same checksummed cache:

```bash
PBS_FRONTEND_DIRECT=1 ./runIt.sh pbs-from-s3 \
  ... \
  --run-id '<id>' \
  --engine wasabi \
  --blocksciPbs \
  --blocksci-workflow reusable \
  --blocksci-task parse \
  --blocksci-external-blocksci-dir /storage/brno2/home/<login>/blocksci_data
```

To incrementally advance an external mainnet cache after Bitcoin Core has
received newer blocks, use the existing cache run as the source and a fresh
run ID as the target:

```bash
PBS_FRONTEND_DIRECT=1 ./runIt.sh pbs-from-s3 \
  --run-id 'mainnet-850100' \
  --artifact-uri s3://coinjoin-thesis/runs \
  --s3-endpoint-url https://s3.cl4.du.cesnet.cz \
  --s3-credentials-file /storage/brno2/home/<login>/.aws/credentials \
  --s3-profile coinjoin \
  --engine joinmarket \
  --blocksciPbs \
  --blocksci-workflow cached \
  --blocksci-task update \
  --blocksci-cache-source-run-id 'mainnet-850000' \
  --blocksci-external-bitcoin-datadir /storage/brno2/home/<login>/bitcoin \
  --blocksci-network bitcoin \
  --blocksci-max-block 850100
```

The target maximum is inclusive and must be greater than the maximum recorded
by the source cache. The target prefix must be completely empty. The job
verifies and extracts the source cache, runs only the incremental
`blocksci_parser update`, and uploads a new checksummed cache under the target
run. It never overwrites the source cache, so a parser or upload failure leaves
the last successful generation intact. Use the new target run ID for later
`cached` script or notebook jobs.

After either command finishes, use the normal `cached` `notebook` or `script`
command for the same `<id>`. Those tasks need only the cache. The standard
`detect` task additionally needs the run's emulator labels and exported block
metadata, so an otherwise empty external-data run is intended for notebook or
custom-script analysis rather than the unified emulator comparison report.

Run the default detector later without invoking `blocksci_parser`:

```bash
PBS_FRONTEND_DIRECT=1 ./runIt.sh pbs-from-s3 \
  ... \
  --run-id '<id>' \
  --engine wasabi \
  --blocksciPbs \
  --blocksci-workflow cached \
  --blocksci-task detect \
  --test-values
```

Custom scripts must be on shared `/storage` so the PBS compute node can bind
them read-only. They receive `ACTIVE_RUN_ID`, `BLOCKSCI_CONFIG`,
`BLOCKSCI_RUN_DIR`, and `BLOCKSCI_OUTPUT_DIR`; outputs under the latter are
uploaded to `blocksci-custom-analysis_data/`:

```bash
PBS_FRONTEND_DIRECT=1 ./runIt.sh pbs-from-s3 \
  ... \
  --run-id '<id>' \
  --engine wasabi \
  --blocksciPbs \
  --blocksci-workflow cached \
  --blocksci-task script \
  --blocksci-script /storage/brno2/home/<login>/analysis.py
```

Jupyter is a separate long-lived PBS task and never blocks `full-run` or the
unified report. The current BlockSci image installs Jupyter at image-build
time, so this task does not call `build.sh`, compile BlockSci, or parse again:

```bash
PBS_FRONTEND_DIRECT=1 ./runIt.sh pbs-from-s3 \
  ... \
  --run-id '<id>' \
  --engine wasabi \
  --blocksciPbs \
  --blocksci-workflow cached \
  --blocksci-task notebook \
  --blocksci-notebook-port 8888 \
  --blocksci-notebooks-dir /storage/brno2/home/<login>/notebooks
```

The PBS output prints the assigned node, port, and Jupyter token. Stream that
output with `./runIt.sh watch --run-id '<id>' --pbs-only --pbs` and create an
SSH tunnel to the assigned compute node through the normal MetaCentrum access
path. Stop the PBS job when finished; notebook files are uploaded to
`blocksci-notebooks_data/` (or written directly to the supplied shared
directory).

After `.pbs/unified-report.done` appears, download the canonical report
directly on the frontend without starting Docker or Podman:

```bash
./runIt.sh download-report \
  --run-id '<id>' \
  --artifact-uri s3://coinjoin-thesis/runs \
  --s3-endpoint-url https://s3.cl4.du.cesnet.cz \
  --s3-credentials-file /storage/brno2/home/xman/.aws/credentials \
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
  --artifact-uri s3://xman-coinjoin/runs \
  --s3-endpoint-url https://s3.cl4.du.cesnet.cz \
  --s3-credentials-file /storage/brno2/home/xman/.aws/credentials \
  --s3-profile coinjoin

# Delete a single run non-interactively.
./runIt.sh clean-s3 --yes \
  --run-id '<id>' \
  --artifact-uri s3://xman-coinjoin/runs \
  --s3-endpoint-url https://s3.cl4.du.cesnet.cz \
  --s3-credentials-file /storage/brno2/home/xman/.aws/credentials \
  --s3-profile coinjoin
```

Omit `--run-id` to clean everything under `--artifact-uri`; point `--artifact-uri`
at the bucket root (`s3://xman-coinjoin`) to wipe the whole bucket. The
`--artifact-uri`, `--s3-*` options also read the `ARTIFACT_URI`, `S3_ENDPOINT_URL`,
`S3_CREDENTIALS_FILE`, and `S3_PROFILE` environment variables.

`--blocksciPbs` by itself remains available only to `pbs-from-s3`; it retains
the combined BlockSci-plus-report behavior and requires the remote run to
already contain `coinjoin-analysis_data/coinjoin_tx_info.json`. Both S3 report
paths require and upload the canonical `coinjoinPipeline_data` output.

The general `--pbs-{ncpus,mem,scratch,walltime}` values are backward-compatible
fallbacks. Use `--pbs-blocksci-*`, `--pbs-analysis-*`, and `--pbs-mappings-*`
to size those allocations independently. A stage-specific value takes
precedence over the general fallback and otherwise retains that stage's
existing default.

The report-only job defaults to 2 CPUs, 8 GB RAM, 10 GB scratch, and a one-hour
walltime. Use `--pbs-unified-report-ncpus`,
`--pbs-unified-report-mem`, `--pbs-unified-report-scratch`, and
`--pbs-unified-report-walltime` to override only that job; the shared
`--pbs-*` resource options remain the fallback.

## Security

The wrapper container receives the Docker or Podman API socket so it can manage
component containers. Socket access is effectively host-level container
control; only run trusted pipeline images. Rootless Podman is selected with
`--runtime podman`; set `CONTAINER_SOCKET` if automatic discovery is insufficient.
Kubernetes and PBS options retain their current wrapper semantics.

## Layout

- `src/coinjoin_pipeline/`: installed CLI, metadata, builder, and runtime logic.
- `pipeline/client/`: wrapper, PBS, research, and run-catalog implementation.
- `pipeline/exporters/`: unified JSON/Markdown report implementation.
- `container/`: compatibility launcher and pipeline-image entrypoint.
- `scenarios/`: canonical scenarios.
- `tests/`: host, wrapper/report, PBS, Podman, and Kubernetes coverage.

Historical emulation logs were deliberately not copied. See `MIGRATION.md`.
