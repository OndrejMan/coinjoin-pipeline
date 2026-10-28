# Refactor follow-ups (2026-10-28)

What the October CLI/execution/exporter refactor left open. Each item is independent; strike it
when done.

## Verification still owed

- [ ] Shell integration suite (`./run-all.sh`, `tests/test-*.sh`) has not run on the refactor. It is
  the only check of the new `k8s_s3_access.sh` header and the shared S3 marker helpers outside
  fake S3.
- [ ] One real Kubernetes → S3 → PBS run, to confirm markers, `.jobid` reads and rollback/qdel.
- [ ] Re-export a run and confirm it shows `stale` once (the Dumplings snapshot is now a hashed
  report input), then `current`.
- [ ] `ruff format` is still dirty in files the refactor did not own: `arguments.py` (formatting
  collapses the multi-line usage string; needs `# fmt: off` or a reshaped string),
  `coinjoin_research.py`, `manifest.py`, `test_index_store.py`, `test_kubernetes_diagnostics.py`,
  `test_liquidity.py`, `test_coinjoin_tools.py`.

## Small, non-breaking

- [ ] `download_report.py` builds `.pbs/unified-report.{done,failed}` keys by hand; use
  `storage.s3.stage_marker_uri` / `run_prefix_uri`.
- [ ] `watch.py` globs `.pbs/*.jobid` itself; use `pbs.submission.read_pbs_job_id`.
- [ ] `analysis_modes.py` and `templates/s3_coinjoin_analysis_dumplings.sh` spell out
  `dumplings-snapshot.json`; import `DUMPLINGS_SNAPSHOT_FILE`.
- [ ] `coinjoin_tools.py` literals `"coinjoin-analysis_data"` and `"coinjoin_emulator_data"/"data"`
  should use `COINJOIN_ANALYSIS_DIR` / `EMULATOR_DIR`.
- [ ] `s3_submission.py`: drop the two `blocksci.notebook_port or 8888` fallbacks (the model
  defaults to 8888 now).
- [ ] `execution/kubernetes.resolve_kubeconfig` ignores `$KUBECONFIG` while `watch` honours it;
  honour it in one place and reuse.
- [ ] `execution/research.py`: `DEFAULT_RUNS_ROOT` differs from the CLI default (use
  `paths.default_runs_root()`); parsers lack `prog="cjp"` and help for `runs …` / `scenarios …`.
- [ ] `option_rules._action_errors` is one long function; split per concern without changing
  messages.
- [ ] Unit test that the `AWS_*` scrub lists in `s3_stage.sh` and `k8s_s3_access.sh` match
  `AWS_SCRUB_VARIABLES`.
- [ ] `docs/mainnet-synchronization.md`: say that `.pbs/mainnet-*` markers carry JSON bodies,
  unlike other stages.
- [ ] `docs/analysis-semantics.md`: list `postmix_spends.json` and `dumplings-snapshot.json` as
  report inputs; add a top-level keys table (`coverage`, `baseline_filter`, `warnings[].code`).
- [ ] Decide on `src/coinjoin_pipeline/resources/scenarios/`: execution never reads it; drop it or
  make it the single catalogue.
- [ ] Optional: hash `wallets_info.json` and `wallets_coins.json` as report inputs.

## Needs a decision (breaking)

- [ ] `pipeline_logging.exit_with_error` exits 2 for every failure; use `console.EXIT_FAILURE` (5)
  so scripts tell bad input from a failed run. scenario-lab only checks `code == 0`.
- [ ] kebab-case aliases for `--analysisPbs`, `--blocksciPbs`, `--mappingsPbs`, `--blocksciScript`,
  `--fromConfiguration`; consider renaming `--version` (image tag).
- [ ] Report schema 2: drop `detection_confusion_matrix` and the duplicated `run.joinmarket_*` /
  `run.first_wasabi2_block`; rename `transactions[*].blocksci.broadcast_time` (holds block time)
  to `block_time`. scenario-lab `src/domain/evaluation.ts` must read
  `detector_evaluations.blocksci` first.
