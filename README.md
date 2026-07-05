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

Host commands are `doctor`, `pull`, `version`, and `builder`. All merged parser
commands remain represented by `command_metadata.json`: `full-run`, `recreate`,
`clean`, `analyze`, `export`, `coinjoin-analysis`, `mappings`, `initialize`, and
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
