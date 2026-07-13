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
```

By default the wrapper leaves `--min-input-count` unset, so BlockSci applies
its height-aware production threshold (or its test threshold when
`--test-values` is explicitly selected). Pass `--min-input-count N` only for
an intentional override. Independent emulator labels, raw detector metrics,
and provenance rules are defined in
[Analysis semantics](docs/analysis-semantics.md).

Outputs default to `./coinjoin-runs`; override this with `--runs-root`. Images
default to the explicit `latest` tag. Use `--version TAG` to apply one coordinated
tag to every default image, or `--local-build` for local development tags.
Individual overrides (`--pipeline-image`, `--emulator-image`,
`--coinjoin-analysis-image`, `--blocksci-image`, `--mappings-image`, and
`--sake-image`) take precedence over the coordinated tag.

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

Host commands are `doctor`, `pull`, `version`, and `builder`. All merged parser
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

Submit Kubernetes emulation and upload:

```bash
coinjoin-pipeline recreate \
  --driver kubernetes \
  --artifact-backend s3 \
  --artifact-uri s3://coinjoin-thesis/runs \
  --s3-endpoint-url https://s3.cl4.du.cesnet.cz \
  --s3-secret-name coinjoin-s3-credentials \
  --run-id wasabi-test-001 \
  --engine wasabi
```

Submit PBS analysis for the existing run:

```bash
PBS_FRONTEND_DIRECT=1 coinjoin-pipeline pbs-from-s3 \
  --run-id wasabi-test-001 \
  --artifact-uri s3://coinjoin-thesis/runs \
  --s3-endpoint-url https://s3.cl4.du.cesnet.cz \
  --s3-credentials-file /storage/brno2/home/xman/.aws/credentials \
  --s3-profile coinjoin \
  --engine wasabi \
  --analysisPbs \
  --blocksciPbs
```

`s5cmd` must be available on PBS compute nodes and is included in the pipeline
image used by the Kubernetes uploader. S3-compatible `full-run`, mappings, and
frontend marker polling are deferred.

When both PBS stages are selected, BlockSci is submitted with an `afterok`
dependency on coinjoin-analysis. `--blocksciPbs` by itself requires the remote
run to already contain `coinjoin-analysis_data/coinjoin_tx_info.json`.

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
