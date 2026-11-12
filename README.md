# coinjoin-pipeline

Consolidated control layer for reproducible CoinJoin emulation, BlockSci
analysis, baseline `coinjoin-analysis`, and unified reports. The installed CLI
is thin; heavy work runs in explicitly versioned container images.

This is a local prototype and is not published to PyPI.

Manual mainnet Core/Dumplings → BlockSci synchronization is described in
[the synchronization runbook](docs/mainnet-synchronization.md). It uses two
dependent PBS jobs and keeps the working data in place between runs.

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
coinjoin-pipeline full-run --engine joinmarket --dry-run
coinjoin-pipeline full-run --engine joinmarket
# Small regtest Wasabi rounds need an explicit input threshold.
coinjoin-pipeline full-run --engine wasabi \
  --scenario scenarios/overactive-local.json \
  --min-input-count 15

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

```bash
cjp full-run --engine wasabi --scenario ../coinjoin-scenarios/fin-cliff.json --min-input-count 10
```

The YAML `scenario` field accepts the same path. Relative paths are resolved from
the directory where the CLI is invoked. Local Docker/Podman and Kubernetes
launchers mount the selected file's directory read-only; Kubernetes → S3 embeds
its contents in the Job's ConfigMap. No copy into the emulator checkout is needed.
The pipeline's own `scenarios/` remains available for defaults and test fixtures.
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

By default the pipeline leaves `--min-input-count` unset, so BlockSci applies
its height-aware production threshold. Pass `--min-input-count N` for an
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
default to the explicit `latest` tag. Use `--version TAG` to apply one coordinated
tag to every default image, or `--local-build` for local development tags.
Individual overrides (`--emulator-image`, `--coinjoin-analysis-image`,
`--blocksci-image`, `--mappings-image`, and `--sake-image`) take precedence
over the coordinated tag and over `--local-build` defaults. The corresponding
environment overrides also apply in local mode; explicit CLI images win over
the environment. The in-cluster uploader and the pinned Python image
for the PBS report step are not part of that coordinated set: they carry their
own immutable references in `container/uploader.image` and
`container/unified-report.image`, overridable per run with `--uploader-image`
and `--unified-report-image`.

The uploader image is published by the manual `Publish Uploader Image`
workflow, not by the test pipeline, so **it has to exist in the registry before
the first S3 run**: the Kubernetes Job pulls it for both its preflight and its
uploader container. Run the workflow, put the digest it prints into
`container/uploader.image`, and only then start an S3 run — otherwise the Job
sits in `ImagePullBackOff` (the frontend now aborts on that instead of waiting
out `--emulation-timeout`, but the run is still lost).

When publishing component images separately, publish and verify the BlockSci
and coinjoin-emulator images together. The pipeline requires BlockSci's raw
CoinJoin binding and the emulator's in-cluster networking plus producer-label
manifest contract. A coordinated `--version` tag avoids mixing incompatible
generations.

Developer compatibility entrypoints use the same CLI:

```bash
./runIt.sh full-run --engine joinmarket --local-build --dry-run
./run-all.sh local --build-only
```

The host CLI and orchestration run in one Python process from an editable
checkout (`pip install -e .` or `pipx install --editable .`). Container workloads
remain separate processes. `pipeline/compose.yaml`, the standalone exporters
and scenarios are resolved relative to the checkout; a relocated wheel alone
is not a complete execution installation.

The recommended entry point is `cjp run experiment.yaml`. Both YAML and the
existing command flags load the same typed configuration directly. Defaults,
image selection and run identity are resolved before execution; YAML is never
translated to a second command line. The interactive builder and generated
command metadata have been removed. See [Current architecture](docs/architecture.md).
