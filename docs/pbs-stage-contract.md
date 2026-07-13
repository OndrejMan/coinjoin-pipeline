# PBS stage contract

How the pipeline frontend and PBS compute jobs coordinate. The implementation
lives in `pipeline/client/pbs.py` and the `pipeline/client/*_template.sh`
scripts; this page documents the on-disk protocol so that changes on either
side stay compatible.

## Stages

| Stage name          | Submitted by                          | Template                          |
|---------------------|---------------------------------------|-----------------------------------|
| `blocksci`          | `--blocksciPbs`                       | `blocksci_template.sh`            |
| `coinjoin-analysis` | `--analysisPbs`                       | `coinjoin_analysis_template.sh`   |
| `coinjoin-mappings` | `--mappingsPbs`                       | `mappings_template.sh`            |
| `unified-report`    | parallel mode after both analyzers    | `blocksci_template.sh` (report-only command) |
| S3 variants         | `pbs-from-s3`                         | `*_s3_template.sh`                |

## Marker files

All coordination state lives in the run directory under `.pbs/`:

- `.pbs/<stage>.pbs` — the rendered job script (kept for reproducibility).
- `.pbs/<stage>.jobid` — the `qsub` job id, written right after submission.
- `.pbs/<stage>.done` — written by the job's `on_exit` trap on exit status 0.
- `.pbs/<stage>.failed` — written by the trap on any non-zero exit status.

The job script removes stale `done`/`failed` markers at startup, so a rerun of
the same stage in the same run directory starts clean. S3 jobs upload the
markers to `<artifact-uri>/<run-id>/.pbs/` instead of writing them locally.

## Frontend waiting (`wait_for_pbs_marker`)

The frontend polls every `POLL_INTERVAL_SECONDS` (30 s):

1. `failed` marker exists → raise `PBSError` (stage failed).
2. `done` marker exists → stage finished successfully.
3. Deadline exceeded → raise. The timeout is the stage walltime plus one hour
   of queue margin (`pbs_wait_timeout`).
4. If a job id is known, `qstat -x -f <jobid>` is consulted as a fallback:
   a terminal state (`C`/`F`) or a missing job without a marker means the job
   ended without writing its marker, which is treated as a failure.

Robustness rules:

- A non-zero `qstat` exit only counts as job death when stderr explicitly says
  the job is unknown/finished; any other failure (PBS server restart, network
  hiccup) is inconclusive and polling continues.
- Markers are written to shared storage by the compute node, and NFS attribute
  caching can briefly show a finished (`F`) job before its `done` marker is
  visible; the frontend therefore waits one extra poll cycle after seeing a
  terminal state before declaring "ended without marker".

## Requirements checked before submission

- The run directory, logs root, Bitcoin datadir, and exporters directory must
  resolve under `/storage/` (MetaCentrum shared storage).
- The BlockSci stage requires the Bitcoin datadir to contain `regtest/blocks`.
- The mappings stage requires
  `coinjoin-analysis_data/coinjoin_tx_info.json` to already exist.
- `qsub` must be on `PATH` (a MetaCentrum frontend, or the local shims from
  `scripts/pbs-env.sh` in the meta-repo).

## Cancellation

When one stage of a parallel run fails, the frontend calls `qdel` on the other
still-running stages using the persisted `.pbs/<stage>.jobid`
(`qdel_pbs_stage`).
