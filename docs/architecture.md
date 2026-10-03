# Current execution architecture

Updated 2026-10-03. The thesis results come from the Kubernetes → S3 → PBS
path. Every module outside that path carries an `EXPERIMENTAL` header and is
listed under "Experimental paths" below.

```text
YAML or CLI options
  → PipelineConfiguration (one schema)
  → RunContext.prepare (defaults, validation, identity, images)
  → host preflight
  → execution.orchestrator (same process)
       ├─ Kubernetes → S3 → PBS stage graph            (orchestrator.py, s3_*.py)
       └─ EXPERIMENTAL local Docker/Podman, Kubernetes
          without S3, shared-storage PBS                (local.py)
```

`arguments.py` is an input adapter. It merges explicitly provided values into
YAML before decoding. `configuration.py` owns field types, option spellings,
and value bounds; `option_rules.py` owns cross-field policy. The existing CLI
flags remain available to scripts, but there is no YAML-to-argv conversion,
metadata snapshot or second host Python process. `cjp run FILE.yaml` is the
preferred entry point. An explicit `stages.<name>: false` is respected; assigning
PBS resources to that disabled stage is an error.
Cross-field rules read typed configuration attributes directly. The `provided`
field set distinguishes explicit values from defaults; CLI option names are
used only in validation messages.

`context.py` resolves run ID, output root and image selection, and restores
environment, bytecode policy, signal handlers and locks after execution.
`research_manifest.json` records top-level schema, mode and run ID, with the
redacted launcher record under `host_launcher`. External import manifests keep
their input provenance. The report's manifest remains a distinct analytical
artifact with image digests and exporter hashes.

## Module ownership

| Responsibility | Owner |
|---|---|
| Configuration and loading | `configuration.py`, `arguments.py`, `option_rules.py` |
| Resolved run and host lifetime | `context.py`, `runs.py`, `manifest.py` |
| Action dispatch and the S3 `emulate`/`full-run`/`pbs-from-s3` branches | `execution/orchestrator.py` |
| S3 full-run: staging, emulation Job, PBS graph, marker waits, rollback | `execution/s3_workflow.py`, `s3_staging.py`, `s3_emulation.py`, `s3_submission.py`, `s3_markers.py` |
| In-cluster prefix preflight and artifact uploader | `execution/k8s_s3_prefix_preflight.sh`, `k8s_s3_uploader.sh` |
| Kubernetes resource generation and diagnosis | `execution/kubernetes.py` |
| Graph definitions and dependency queries | `execution/stages.py`, `stage_executor.py` |
| Run-directory layout (every run-relative path, marker sets, report inputs) | `pipeline/exporters/artifact_paths.py` |
| Command locks and S3 PBS overlap detection | `execution/locks.py` |
| PBS submit, qdel, state probes; S3 job scripts | `execution/pbs/submission.py`, `templates_s3.py`, `commands.py` |
| Scenario lookup and host/container scenario paths | `execution/scenarios.py` |
| S3 access for run, watch/download/cleanup consumers | `storage/s3.py` |
| Container workload shared by Docker and Apptainer | `pipeline/exporters/worker.py` |
| Parsed block-archive cache height, checkpoint and update continuity verification | `pipeline/exporters/verify_chain.py` |
| Heavy BlockSci results | `pipeline/exporters/blocksci_export/analysis.py` |
| Artifact reader independent of BlockSci | `pipeline/exporters/analysis_artifact.py` |
| Report assembly and presentation | `pipeline/exporters/cli.py`, `report_builder.py`, `markdown_report.py` |

## One analyzer/report flow

For emulator comparisons, both analyzers produce artifacts; the report depends
on both. Mappings, when enabled, depends on baseline analysis and also gates the
report. Serial execution uses one worker; parallel execution allows several
ready stages. Report is an ordinary node executed by the same executor.

The S3 frontend submits `PBSJobSpec(stage, script, dependencies)` through one
scheduler boundary. The ordered graph supplies dependencies. Before submission,
each stage's stale terminal markers are cleared; job IDs are recorded immediately
for overlap detection and rollback. Shared-storage failure cancels running
siblings; S3 wait failure cancels only dependent jobs. These policies remain
explicit and distinct.
Stage adapters call their script renderers directly and pass a completed
`PBSJobSpec` to submission. PBS worker commands consume the typed configuration;
`pbs/commands.py` renders detector arguments for both analysis and report phases.

`worker.py` supports parse, update, analyze, report, or parse+analyze (`run`).
Docker and PBS choose mounts and interpreter, then invoke this same payload.
The report always consumes `blocksci-analysis_data/blocksci_analysis.json`.
It never performs an implicit detector scan. Old runs with only a parsed index
need one analytical export before report assembly. The reader accepts artifact
schema 1.0 for emulator runs; newly written schema 1.1 includes the mode and
preserves detector parameters, skipped transactions, diagnostics and clustering.

The host requires Python 3.10+. Exporter code retains Python 3.8 syntax for the
BlockSci image. Their input options share `exporters/parameters.py`; exporters
are staged as a standalone tree and do not import the host package.

## Verification boundary

Focused tests use fake scheduler, process and storage boundaries. They verify
configuration errors, graph order, rollback/cancellation, job persistence,
rendered shell behavior, artifact validation and report calculations. They do
not prove a live emulator, image, parser or MetaCentrum run.

The live paths are covered by the local suite (`run-all.sh local`): Docker,
k3d, shared-storage PBS and Kubernetes → MinIO → PBS end-to-end tests. Historic
reports retain their original provenance; new exporter source hashes differ.

## Experimental paths

Kept for local development and reproduction, not read for the thesis review:

| Path | Modules |
|---|---|
| Dispatch of every non-S3 action | `execution/local.py` |
| Local Docker/Podman commands and Compose | `execution/containers.py`, `runtime.py`, `cli_defaults.py`, `pipeline/compose.yaml`, `pipeline/*.sh` |
| Kubernetes emulation without S3 | `execution/kubernetes_launch.py` |
| Shared-storage PBS stages and graph | `execution/shared_storage_pbs.py`, `workflow.py`, `pbs/submission_local.py`, `pbs/templates_local.py`, `*_template.sh` without `_s3` |
| `runs`, `scenarios` and `external analyze` commands | `execution/research.py`, `run_catalog.py` |
