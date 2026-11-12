# coinjoin-pipeline — findings from the history rebuild

State of the code reviewed: `main` 9aa44e9 ("test(e2e): run the parallel variants as wrappers of
the serial tests"), 2026-11-12. The `file:line` references hold for that state (newer findings name
a function instead of a line). The review only recorded findings; the fixes came afterwards as new
commits on the `clean-history` branch, without changing any existing commit.

Severity: **M** medium (can give a wrong result or block a run), **L** low (readability, cosmetics,
small inconsistency). Confidence: *verified* (read in the code, every place traced), *suspected*
(follows from the code, not confirmed by a run).

## Status (2026-11-12)

Every finding ends with its status: **Fixed** with the subject of the commit that fixed it,
**Refuted** when checking the code disproved it, or **Kept** with the reason. Summary:

- All three potential bugs and every medium finding are fixed.
- Refuted: inconsistencies 1 and 15, dead code 4.
- Kept by the user's decision: dead code 2 and refactoring 17, the deprecated JoinMarket
  subset-matching scan with its options, variables, compose defaults, report section, Markdown and
  report schema version; and the Lab's script template that calls it.
- Kept on purpose: the `run-all-*.sh` default of `RUN_SUITE` (part of inconsistency 38), which
  the workspace's suite-watch skill relies on.
- Ships together: the shared index backup format (refactoring 1) changed the Lab's saved-index
  check, which lives on the `index-backup-format` branch of `scenario-lab` ("refactor(notebook):
  check a saved index in the shared backup format"). Run caches parsed before it cannot be reused;
  those runs need a new parse.
- Not verified beyond the unit and pipeline suites, ruff, mypy, pylint and the mocked shell tests:
  no k3d/MinIO end-to-end run, no MetaCentrum run. Two results were checked against a local MinIO:
  `s5cmd sync --delete` of the staged exporters, and one `rm` of two markers when one is absent.
- Extra: the rebuild tooling commit put `.peel/` under `ruff check .`, fixed in "style(history): …".

## Potential bugs

1. **M, verified** — `src/coinjoin_pipeline/execution/containers.py:cleanup_peer_containers` +
   `execution/settings.py:41` `PEER_CONTAINERS`: when the CLI is interrupted it stops containers by
   the names `blocksci_analyzer`, `emulator_manager`, `btc_data_wiper`, `isolated_docker_daemon`…
   `pipeline/compose.yaml` sets no `container_name`, though, so Compose names them
   `<project>-<service>-1` and `docker stop` hits nothing. Only the traps in
   `emulate.sh`/`analysis.sh` actually clean up.
   **Fixed:** "fix(cleanup): stop the Compose project's containers when the CLI is interrupted".
   It also showed that `tests/test-wrapper-signal-cleanup.sh` always failed its TERM case: both
   cases ran in the same minute and the second collided on the run ID ("fix(test): give each
   signal-cleanup case its own run ID").
2. **M, suspected** — `src/coinjoin_pipeline/configuration.py:312` + `arguments.py:344`: YAML without
   an `action` key silently falls back to the default `full-run`; a typo or a missing key starts a
   whole emulation.
   **Fixed:** "feat(config): require an action in YAML configurations"; the command line keeps its
   default `full-run`, and the Lab and every example name the action.
3. **M, suspected** — `execution/orchestrator.py:169-171,193-200` + `execution/pbs/kerberos.py:385-396`:
   `pbs-from-s3` and `mainnet-sync` only submit and return, yet they require a successful
   `kinit -R`. With MetaCentrum's default non-renewable ticket `qsub` would work, but the command
   refuses to start.
   **Fixed:** "fix(pbs): require a renewable Kerberos ticket only for the S3 full-run", with the
   contract in `docs/pbs-stage-contract.md` updated.

## Inconsistencies (code against docs, help or tests)

1. **M** — `execution/kubernetes_launch.py:80-88,174`: the Kubernetes path without S3 takes the
   btc-node image, pull policy, control IP, network and storage UID from environment variables
   (`COINJOIN_BTC_NODE_IMAGE`, `KUBERNETES_IMAGE_PULL_POLICY`…) and ignores
   `kubernetes.btc_node_image` / `kubernetes.image_pull_policy`, which the S3 path reads from the
   configuration.
   **Refuted:** the options are not silently ignored; `option_rules` refuses them outside the S3
   emulation, and the help lists them as S3-only.
2. `configuration.py:204-210`: the `PBSResourceConfiguration` fields declare `option(name="ncpus"…)`,
   but `option_specs` (467) replaces the name with the path for this model, so the `name` metadata is
   dead and misleading (the real flags are `--pbs-analysis-ncpus` and so on).
   **Fixed:** "refactor(arguments): drop dead option names and duplicated argument helpers".
3. `arguments.py:335`: after the aliases went, `_canonical_action` only validates; its name promises
   canonicalization.
   **Fixed:** "refactor(arguments): drop dead option names and duplicated argument helpers".
4. `context.py:45-46`: `pbs-from-s3`/`mainnet-sync` force `artifacts.backend=s3` even when the user
   explicitly set shared-storage, a silent override instead of an error.
   **Fixed:** "fix(config): reject shared storage for the actions that read only S3".
5. `run_layout.py:219-223` + docstring 204-206: the docstring says every name comes from the module
   that defines it (the stage graph included), but the stage names are string literals copied from
   `execution/stages.py`.
   **Fixed:** "refactor(stages): name the stages once and let run_layout import them".
6. `execution/scenarios.py:13` against `context.py:134`: the scenario directory has two environment
   variables, `SCENARIOS_ROOT` (read by scenarios.py) and `SCENARIOS_DIR` (set by
   host_environment, read by compose, containers and kubernetes_launch).
   **Fixed:** "refactor(scenarios): read the scenario catalogue from SCENARIOS_DIR alone".
7. `execution/run_context.py:302-315`, `scenarios.py:160-165`, `containers.py:265-267,379-384,508-510`:
   library functions print `[ERROR]` and call `sys.exit(2)` instead of raising like the rest of the
   code; this bypasses the error handling of `cli.py` and the state of the launch record.
   **Fixed:** "refactor(execution): raise errors from library functions instead of exiting". The
   part about the launch record did not hold (`cli.main` catches `SystemExit` and records the code);
   the real gain is that the local analysis now names the stage that failed.
8. `containers.py:162-166`: `compose_env` defaults the runs root to `<checkout>/coinjoin-runs`, while
   `paths.default_runs_root` uses `$PWD/coinjoin-runs`; the scenario directory is derived again
   instead of using `paths.SCENARIOS_ROOT`.
   **Fixed:** "refactor(containers): use the CLI's default runs root in compose_env and the scripts"
   (the scenario directory with finding 6).
9. `pipeline/delete.sh:31-39`: the comment says the Compose project name carries the PID of the
   launching process; the CLI names it `blocksci-emulator-<checkout hash>`
   (`containers.compose_project_name`), and only the tests use a PID.
   **Fixed** in the first wave of fixes.
10. `execution/kubernetes.py:1`: the module docstring speaks of "The S3 emulation Job", but the
    module also holds the non-S3 preflight and generic kubectl helpers.
    **Fixed:** "refactor(pbs): name the index manifest copies once and drop hard-coded output names".
11. `storage/s3.py:283` against `s5client.py:188` + `docs/architecture.md`: the frontend requires
    s5cmd 2.1 or later (`sync --exclude`), but uploading archives needs `s5cmd pipe` from 2.2; this
    works only as long as the frontend never streams an archive. *Suspected.*
    **Fixed:** "fix(s3): check the s5cmd version that streaming an archive needs on the node". Only
    the PBS jobs call `pipe`; the frontend keeps 2.1, and a job checks for 2.2 before it packs.
12. `storage/s3.py:107`: the message "--artifact-uri is required (or set ARTIFACT_URI)"; the main
    CLI never reads `$ARTIFACT_URI` (only the utilities' `add_s3_arguments` do).
    **Fixed:** "fix(s3): mention $ARTIFACT_URI only where the utilities read it".
13. `storage/s3.py`: the module also holds the run ID validation (`is_valid_run_id`,
    `validate_run_id`), which the local paths import (`context`, `option_rules`, `run_context`), so
    local code depends on a module whose name promises S3.
    **Fixed:** "refactor(runs): move run ID validation out of the S3 module".
14. `execution/pbs_settings.py`: the local Docker export takes `resolve_unified_report_image` and
    `read_image_lock` from the PBS settings module; a generic `truthy_env` (218) lives there too.
    **Fixed:** "refactor(images): keep the image lock readers with the other image resolution".
15. `execution/pbs_settings.py:263-270`: `images.registry` moves the default uploader image
    (`in_registry`) but not the default unified-report image, so a fork's registry still runs the
    project's report image.
    **Refuted:** the default report image is `python:3.12-slim-bookworm`, not a project image, so a
    fork's registry has no reason to move it.
16. `execution/s3_workflow.py:452-458`: the closing message hard-codes the output directory names
    instead of the `artifact_paths` constants.
    **Fixed:** "refactor(pbs): name the index manifest copies once and drop hard-coded output names".
17. `execution/pbs/analysis_modes.py:82`: the notebook job's output says "Open JupyterLab from
    Scenario Lab", which ties the pipeline's output to the separate Lab UI.
    **Fixed:** "refactor(pbs): name the index manifest copies once and drop hard-coded output names".
18. `execution/pbs/blocksci_jobs.py:123-124` against 347: the combined job checks for `zstd` by hand
    with the same message `require_tools` gives; the other jobs call `require_tools`.
    **Fixed** in the first wave of fixes.
19. `execution/pbs/blocksci_jobs.py:blocksci` against `blocksci_parse`/`run_parser`: the combined job
    calls singularity itself with `cleanenv=True`, parse/update go through `run_parser` without
    `cleanenv`, so two paths to the same parser differ in the container's environment.
    **Fixed:** "fix(pbs): parse and verify the BlockSci index without the host environment".
20. `execution/pbs/blocksci_jobs.py:205,471`: the object names `blocksci-index-manifest.json` /
    `blocksci-index-backup.json` in the run prefix (the Lab reads them) are literals, not in
    `artifact_paths`/`run_layout`.
    **Fixed:** "refactor(pbs): name the index manifest copies once and drop hard-coded output names".
21. `execution/pbs/templates/mappings_run.sh:20-21,44`: `enumerator_image_digest`/`sake_image_digest`
    are the sha256 of the locally pulled SIF file, not the registry digest; they differ between two
    pulls of the same image.
    **Fixed:** "fix(mappings): record the registry digest of the enumerator and Sake images" (a
    local SIF file records its hash, an unpinned tag nothing).
22. `execution/pbs/templates_s3.py:_index_job_settings` + `blocksci_jobs.publish_persistent`:
    `pbs_resources` is rendered into every parse/update job, but only the persistent index writes it
    to its manifest; the S3 cache manifest does not record the job's resources, although the
    `index_manifest` docstring names them in general.
    **Fixed:** "feat(blocksci): record the job's resources in a run index's manifest".
23. `option_rules.py:_run_selection_errors` + `context.py` (`validate_run_id`): the run ID (and
    `--blocksci-cache-source-run-id`) are validated twice.
    **Fixed:** "refactor(config): validate run IDs once, in the option rules".
24. `arguments.py:HELP[blocksci.script]`: the help says "Python script of --blocksci-task script",
    but the local Docker BlockSci stage (`containers.run_blocksci_docker_stage`) takes the same
    option without the script task too.
    **Fixed** in the first wave of fixes.
25. `arguments.py:HELP[kubernetes.distributor_startup_timeout]` against
    `tests/pipeline/test_s3_emulation_options.py`: the help promises a fallback to
    `$COINJOIN_DISTRIBUTOR_STARTUP_TIMEOUT`, but the S3 Job ignores the environment variable on
    purpose; the fallback applies only to the local compose.
    **Fixed** in the first wave of fixes.
26. `arguments.py:257`: the epilog lists the utilities with their own `--help` (watch,
    download-report, clean-s3, runs, scenarios) and misses `status` and `stop`
    (`DELEGATING_UTILITIES`).
    **Fixed** in the first wave of fixes.
27. `execution/research.py:133,140,142,327-333`: `runs validate` ends with codes 3/4 and `main`
    returns the literals 5/2; `console.EXIT_CODES` describes 3 as "clean-s3 was not confirmed" and
    does not know 4.
    **Fixed** in the first wave of fixes.
28. `execution/research.py:346-363`: `run_external_configuration` is defined after the
    `if __name__ == "__main__"` guard.
    **Fixed** in the first wave of fixes.
29. `execution/coinjoin_tools.py:run_local` against `pbs/analyzer_jobs.coinjoin_tools`: local result
    directories are named by uuid4, PBS ones by UTC time and job ID.
    **Fixed:** "refactor(coinjoin-analysis): name local tool results like the PBS ones".
30. `pipeline/exporters/parameters.py:Detector.artifact_parameters`: the analysis artifact records
    the JoinMarket subset detector's parameters for Wasabi runs too, so changing any of them
    invalidates a Wasabi artifact ("parameters do not match"), while `Detector.manifest` keeps only
    the selected type's keys.
    **Fixed:** "fix(report): keep a Wasabi analysis valid when a JoinMarket-only option changes".
31. `containers.py:export_command` + `compose.yaml`: the local Docker export forwards
    `--uploader-image` and compose `COINJOIN_UPLOADER_IMAGE`, although a local run never uses the
    S3 uploader.
    **Fixed:** "refactor(containers): build the local export argv as a list without the uploader
    image".
32. `pipeline/exporters/integration_diagnostics.py:IMAGE_COMPONENTS`: a second `IMAGE_COMPONENTS`
    constant with a different meaning than `parameters.IMAGE_COMPONENTS`.
    **Fixed:** "refactor(exporters): name the image component sets and drop repeated artifact names".
33. `pipeline/exporters/integration_diagnostics.py:build_image_diagnostics`: the analysis inspects
    the images through the runtime again, although the host already inspected them and passed
    `*_IMAGE_ID`/`*_IMAGE_DIGEST` (two paths: `containers.add_image_provenance_env` and
    `docker_image_provenance`).
    **Fixed:** "fix(exporters): inspect an image again only when the host passed no identity for it".
34. `pipeline/exporters/manifest.py:build_run_manifest` (`source_commits.exporters`): `git rev-parse`
    runs in the report container, which mounts only the exporters directory without `.git`, so the
    exporters commit is `null` in every local and PBS report; only the tree hash identifies the code.
    **Fixed:** "refactor(report): drop the exporters commit and dirty flag the report container
    cannot read" (the dirty flag had the same defect; report schema 3.1).
35. `pipeline/exporters/cli.py` (`--engine`): the exporter CLI takes `--engine` only for the manifest
    and otherwise guesses it from `coinjoin_type`.
    **Fixed:** "fix(pbs): pass the engine to the BlockSci worker instead of guessing it". Checking it
    showed the real defect: `--cleanenv` dropped `$COINJOIN_ENGINE`, so every PBS analysis and
    report recorded a guessed engine.
36. `pipeline/exporters/markdown_report.py:save_text`: a generic file writer lives in the Markdown
    renderer, and the exporter CLI imports it from there.
    **Fixed:** "refactor(exporters): keep save_text with the other file helpers".
37. `README.md:9-11`: the introduction says the mainnet synchronization "uses two dependent PBS
    jobs"; there are six (restore, producer, blocksci, checkpoint, pack, clear).
    **Fixed** in the first wave of fixes.
38. `MIGRATION.md`, `MAINNET-STATUS.md`, `TODO.md` §2/§5, `run-all-local.sh:11`,
    `run-all-github.sh:11`: documents and wrappers point outside the repository (`../docs`,
    `../scenario-lab`, `../.agents/skills`); TODO §2 still speaks of shell fragments in the S3
    renderers, which the Python stage runtime has replaced.
    **Fixed:** "docs(repo): drop the pointer file outside the repository and stale migration notes"
    (MAINNET-STATUS.md removed, MIGRATION.md and TODO.md §2 updated). **Kept:** the `run-all-*.sh`
    default of `RUN_SUITE`, which the workspace's suite-watch skill relies on; without that wrapper
    the scripts fall back to `run-all.sh`.
39. `tests/pipeline/test_refactor_regressions.py`, `tests/pipeline/test_wrapper.py`: the test module
    names describe history (a refactor, the removed wrapper) instead of the behaviour they check.
    **Fixed:** "test(pipeline): name two test modules after what they check" (now
    `test_input_boundaries.py` and `test_execution.py`).
40. `docs/mainnet-synchronization.md`, `docs/mainnet-sync-local-validation-2026-10-25.md`: Czech,
    while the rest of the documentation is English.
    **Fixed:** "docs(mainnet): translate the synchronization runbook and its validation record"; this
    file and the rebuild notes are English too.
41. `.dockerignore`: no build in the repository uses the root context with this file (the runtime
    image has `container/runtime.Dockerfile.dockerignore`, the uploader builds from `container/`).
    *Suspected.*
    **Fixed:** "chore(container): drop the root .dockerignore that no build reads".

## Unnecessary and dead code

1. `pipeline/exporters/unified_report.py` + `paths.py:24` + `execution/pbs/analyzer_jobs.py:45`:
   `unified_report.py` never runs (every report goes through `worker.py report`, and the tests even
   check that), yet it is in `REQUIRED_EXPORTERS` and the report job refuses to start without it.
   *Verified (rg).*
   **Fixed** in the first wave of fixes.
2. `configuration.py:JoinMarketConfiguration`: four CLI/YAML options, four environment variables and
   the compose defaults exist only for the deprecated subset-matching scan of the report.
   **Kept:** the scan stays in the project by the user's decision, with all of the above.
3. `execution/kubernetes.py:499-500`: the Namespace is rendered when `not reuse_namespace`, but
   `kubernetes_s3_auth_preflight` (127-130) refuses the S3 mode without `--reuse-namespace`, so the
   branch is reachable only from tests and the dry run. *Verified:* `option_rules._s3_errors`
   requires `--reuse-namespace` for the S3 emulation.
   **Fixed** in the first wave of fixes.
4. `execution/k8s_s3_uploader.sh:2`: calls `write_s3_credentials` again, although the shared header
   `k8s_s3_access.sh:19` already wrote the file.
   **Refuted:** the shared header only defines `write_s3_credentials`; the preflight and the
   uploader each call it once.
5. `execution/s3_emulation.py:184,209`: the fallback `or settings.DEFAULT_EMULATOR_IMAGE` is
   unreachable, since `RunContext.prepare` always resolves `images.emulator`.
   **Fixed:** "refactor(images): drop the fallback images that RunContext always resolves".
6. `execution/s3_submission.py:195`: `self.args.stages.blocksci and` is always true inside
   `_blocksci_work` (the stage exists only with `blocksci_pbs`); the condition is just
   `task != "detect"`.
   **Fixed** in the first wave of fixes.
7. `execution/s3_submission.py:_blocksci_work` (`report_here`): the `blocksci` node exists only with
   `--blocksci-pbs`, and the combined workflow allows only the detect task, so `report_here` is
   always False and the `include_report=True` branch never happens on the S3 path.
   **Fixed** in the first wave of fixes.
8. `execution/pbs_settings.py:247`: the fallback `DEFAULT_PBS_IMAGES[component]` is unreachable once
   `context.resolve_run_images` fills every `pbs.*_image`.
   **Fixed:** "refactor(images): drop the fallback images that RunContext always resolves".
9. `execution/pbs/validation.py:21-28`: the length branch of `walltime_to_seconds` and its `else`
   are unreachable, since the regex admits only 3 or 4 parts.
   **Fixed** in the first wave of fixes.
10. `execution/coinjoin_tools.py:run_local`: the image fallback to `image_names(...):version` is
    unreachable, since `RunContext` always resolves `images.coinjoin_analysis`.
    **Fixed:** "refactor(images): drop the fallback images that RunContext always resolves".
11. `doctor.py:139,170`: no caller passes the `check_images` parameter (always True).
    **Fixed** in the first wave of fixes.
12. `pipeline/delete.sh:29`: removes the volumes `<project>_blocksci_cache` and
    `<project>_emulation_logs`, which `compose.yaml` no longer defines (only `btc_data`, `dind_cache`).
    **Fixed** in the first wave of fixes.
13. `pipeline/analysis.sh:21-23`, `emulate.sh:15-17`, `delete.sh:6-8`: the fallback to `/compose.yaml`
    exists for the removed containerized wrapper (no backward compatibility is wanted).
    **Fixed:** "refactor(pipeline): share the Compose runtime setup and name the scenario only in
    Python".
14. `pipeline/analysis.sh:39`: `compose up --build`, although no service has a `build` section.
    **Fixed** in the first wave of fixes.
15. `pipeline/compose.yaml:3,21`: the comment "The Python Manager that controls the emulator" sits
    above the `dind` service; a commented-out `daemon.json` mount.
    **Fixed** in the first wave of fixes.
16. `execution/locks.py:378-381` (`acquire_lock`): the message on `PermissionError` speaks of
    root-owned files from "the old containerized wrapper" and advises `chown`, a leftover of
    compatibility with old runs.
    **Fixed** in the first wave of fixes.
17. `execution/s3_staging.py:128-131`: the error message explains an incomplete exporter tree by
    "predates the blocksci_export rename" (old runs; no compatibility is wanted).
    **Fixed** in the first wave of fixes.
18. `pipeline/exporters/parameters.py:197-215`: `--coinjoin-emulator-image*` fall back to
    `$EMULATOR_IMAGE` / `$EMULATOR_IMAGE_DIGEST` / `$EMULATOR_IMAGE_ID`, which nothing in the project
    sets any more (old aliases).
    **Fixed** in the first wave of fixes.
19. `pipeline/exporters/integration_diagnostics.py:TARGET_DETAIL_LIMIT`: defined, never used.
    **Fixed** in the first wave of fixes.
20. `tests/pipeline/test_report_assembly.py`, `tests/pipeline/test_refactor_regressions.py`: the
    analysis-artifact fixtures carry a top-level `"skipped_txids"` that `BlockSciAnalysisArtifact`
    never defines (a leftover of the subset-matching shape).
    **Fixed** in the first wave of fixes.
21. `tests/pipeline/config_support.py:configuration` (`pbs_submission_dir`): the helper's option is
    not used anywhere (a leftover of the shared-storage tests).
    **Fixed** in the first wave of fixes.

## Overengineering

1. `storage/s3.py:52-102,212-230`: three layers for one s5cmd call, `S3Target` → `S3Access` →
   `S5Client`, plus the `_s5`/`run_s5cmd` wrappers that only translate errors.
   **Fixed:** "refactor(s3): let S3Target hold its S3Access instead of copying its fields". The
   layers stay, each with its own job, which the class now documents: `S5Client` ships to the PBS
   jobs on its own and cannot depend on the frontend types, and `_s5` is the one place that turns
   its errors into `ArtifactTransportError`.
2. `storage/s3.py:203-209`: `shell_value` is `shlex.quote` under another name; `shell_assignment`
   is a one-liner.
   **Fixed** in the first wave of fixes (`shell_value` removed; `shell_assignment` stays).
3. `execution/stages.py:40-66`: `RESOURCE_GROUPS` + `resource_group()`, a one-line wrapper around a
   dict lookup.
   **Fixed:** "refactor(stages): order the graph once and keep one rule for the report stage".
4. `pipeline/exporters/artifact_paths.py:94-111`: `_tool_dir` plus four one-line wrappers of
   `run_dir / CONSTANT`; `report_dir` does not even use `_tool_dir`.
   **Fixed:** "refactor(exporters): join run paths directly instead of through one-line wrappers".
5. `arguments.py:37`: `CONFIGURATION_FLAGS` is a one-item tuple treated as a collection everywhere.
   **Fixed:** "refactor(arguments): drop dead option names and duplicated argument helpers".
6. `cli.py:90`: every utility (watch/status/stop/…) goes through `RunContext.prepare`, image
   resolution and full validation included, only to get the runs root.
   **Fixed:** "refactor(cli): resolve only the runs root for utilities that need nothing else".

## Refactoring candidates

1. **M** — `execution/pbs/blocksci_index.py:25,61-89` against `index_store.py:42-44` +
   `s5client.replace_backup/restore_backup`: two archive formats for one BlockSci index: the run
   cache (`blocksci_data.tar.zst` + `.sha256` + manifest, restored by plain `tar` through
   `restore_archive`, checks in `restore_index`) and the persistent backup
   (`blocksci_index.tar.zst`, a manifest with members, its own verifying extractor). Two manifests,
   two validation paths.
   **Fixed:** "refactor(blocksci): publish a run's index in the persistent index's backup format".
   The run cache is now that backup (`config.json`, `files.txt`, `manifest.json`, `parsed/`),
   written by `replace_backup` and restored by `IndexStore.restore`; `restore_archive` is gone.
   The Lab change ships with it (see the status above).
2. `execution/pbs/blocksci_jobs.py:blocksci_parse`/`blocksci_update`: both functions have their own
   `if settings["persistent"]` branch and `if index is not None: publish_persistent … else:
   publish_index`; two index stores go through the same functions by a None flag instead of one
   interface.
   **Fixed:** "refactor(blocksci): write the persistent and the run index through one interface"
   (`WrittenIndex`).
3. `arguments.py:27-34`: `field_type` re-implements the `Optional` unwrapping that
   `configuration.decode_value` already has.
   **Fixed:** "refactor(arguments): drop dead option names and duplicated argument helpers".
4. `execution/stages.py:16` + `configuration.py:BlockSciWorkflow`: the same Literal
   `["combined", "reusable", "cached"]` is defined twice.
   **Fixed** in the first wave of fixes.
5. `execution/stages.py:161-168` against 278: the rule "a report exists exactly when `blocksci_pbs`
   and task == detect" is written twice (`runs_unified_report` and `s3_full_run_plan`).
   **Fixed:** "refactor(stages): order the graph once and keep one rule for the report stage".
6. `execution/containers.py:460-495` + 530: `export_command` joins argv with `shlex.quote` into a
   string that `run_export_only` immediately `shlex.split`s again.
   **Fixed:** "refactor(containers): build the local export argv as a list without the uploader
   image".
7. `pipeline/analysis.sh`, `emulate.sh`, `delete.sh`: every script re-implements the
   `CONTAINER_RUNTIME` validation and the `CONTAINER_COMPOSE_COMMAND` parsing that
   `execution/runtime.py` already has.
   **Fixed:** "refactor(pipeline): share the Compose runtime setup and name the scenario only in
   Python" (one sourced `pipeline/compose-runtime.sh` for a script run by hand; the CLI passes the
   values it resolved).
8. The default scenario names `overactive-local.json` / `defaultJoinMarket.json` are written in
   `settings.py:23-24`, `runs.py:145`, `emulate.sh:5-8` and `compose.yaml:127`.
   **Fixed:** "refactor(pipeline): share the Compose runtime setup and name the scenario only in
   Python".
9. `execution/pbs/defaults.py:9,11`: `PBS_SCRATCH_TYPES` / `PBS_MULTIJOB_LOCATIONS` repeat the
   literals `configuration.PBSScratchType` / `PBSMultijob`.
   **Fixed** in the first wave of fixes (the literals live in `stages.py` and `pbs/defaults.py`, and
   the configuration imports them; the other direction would make an import cycle).
10. `execution/pbs/commands.py:42,51,87,185`: the container run root `"/runs/emulation/logs/"` is a
    literal in every builder (`settings.RUNS_ROOT_CONTAINER` exists).
    **Fixed** in the first wave of fixes.
11. `execution/pbs/scripts.py:61-64`: only tests (5 modules) use `embedded_file`, a test helper in a
    production module.
    **Fixed:** "refactor(pbs): move the test-only embedded_file reader into the tests"
    (`tests/job_scripts.py`).
12. `execution/pbs/blocksci_jobs.py:310-311` + `execution/block_archive.py`: `block_archive` writes
    the manifest fields as a JSON fragment starting with a comma, and the job wraps it in
    `"{" + lstrip(",") + "}"`.
    **Fixed:** "refactor(blocksci): write the block archive's manifest fields as a JSON object".
13. `execution/pbs/templates/mappings_run.sh:43-45`: assembling the result is a ~600-character Python
    one-liner inside shell.
    **Fixed:** "refactor(mappings): combine the results in a readable script and decide the status
    once".
14. `execution/pbs/templates/mappings_run.sh:44` + `pipeline/exporters/report_builder.py:with_effective_mapping_status`:
    the partial/complete status of the mappings artifact is computed twice by the same rules.
    **Fixed:** "refactor(mappings): combine the results in a readable script and decide the status
    once" (the stage decides; the report shows it).
15. `status.py:16-44`, `stop.py:135-161`, `watch.py`: every utility declares `--run-id`,
    `--namespace`, `--kubeconfig`, `--search-root`, `--json` again, with slightly different help and
    defaults.
    **Fixed:** "refactor(utilities): declare the shared status, stop and watch options once".
16. `pipeline/exporters/verify_chain.py:is_block_hash`, `execution/block_archive.py:SHA256_HEX`,
    `pipeline/exporters/emulator_data.py:SHA256_HEX_RE`: the same 64-hex regex is written three
    times.
    **Fixed:** "refactor(exporters): share the SHA-256 hex pattern where imports allow it". The
    exporters share one; `block_archive.py` and `storage/s5client.py` run on their own and keep a
    commented copy.
17. `configuration.py:JoinMarketConfiguration` / `pipeline/exporters/common.py:DEFAULT_JOINMARKET_*` /
    `compose.yaml`: the same JoinMarket detector defaults (definite, 5000, 4e-05, 200000) are written
    three times.
    **Kept:** they belong to the subset-matching scan, which stays unchanged by the user's decision
    (dead code 2).
18. `pipeline/exporters/blocksci_export/analysis.py:INSPECTED_IMAGE_NAMES/PROVENANCE_IMAGE_NAMES`:
    the slices `IMAGE_COMPONENTS[:3]`/`[:4]`; reordering the tuple silently changes which images are
    inspected and recorded.
    **Fixed:** "refactor(exporters): name the image component sets and drop repeated artifact names".
19. `pipeline/exporters/manifest.py:resolve_manifest_images`: spells out the component tuple and its
    Literal type again instead of using `parameters.IMAGE_COMPONENTS`.
    **Fixed:** "refactor(exporters): name the image component sets and drop repeated artifact names".
20. `pipeline/exporters/analysis_artifact.py:ARTIFACT_NAME`, `emulator_data.JOINMARKET_LABEL_SOURCE`:
    aliases or repeats of names `artifact_paths` already defines (`ANALYSIS_ARTIFACT_NAME`, the file
    name of `ROUND_EVENTS_FILE`).
    **Fixed:** "refactor(exporters): name the image component sets and drop repeated artifact names".

## Small improvements

1. `arguments.py:106-221`: `HELP` covers only part of the options; `joinmarket.*`, `mappings.*`,
   `mainnet.*`, `coinjoin_analysis.program/cjtype/…`, `research.*` have no description, only
   "[YAML: …]".
   **Fixed:** "docs(cli): describe every configuration option in --help"; a test keeps every option
   described.
2. `cli.py:170`: `start_host_manifest` creates `<runs_root>/.notebooks` for every action (mainnet,
   clean… included), tying the notebooks directory to unrelated commands.
   **Fixed:** "refactor(cli): create the notebooks directory only for the Compose stages that mount
   it".
3. `process.py:349`: `run()` returns 127 on any `OSError` (permission denied included), and the error
   text is lost.
   **Fixed** in the first wave of fixes.
4. `images.py:232-233`: missing blank lines between `in_registry` and `LOCAL_IMAGES`;
   `storage/s3.py:44-46` has three blank lines (ruff format is not enforced by the lint).
   **Fixed** in the first wave of fixes.
5. `images.py:301`: `IMAGE_NAMES` is computed from the environment at import; consumers of
   `IMAGE_NAMES` ignore an `images.registry` configured later.
   **Fixed:** "fix(images): resolve default images when they are used, not at import".
6. `runs.py:153`: run IDs have minute resolution; two launches in the same minute collide (the second
   is refused because the first already recorded a launch). A known source of CI flakes.
   **Fixed:** "feat(runs): put seconds into generated run IDs"; the report reads the run time from
   the minute IDs of older runs too.
7. `runs.py:145`: the fallback scenario names `default-joinmarket` / `overactive-local` copy the
   emulator's default scenarios; a drift risk.
   **Fixed:** "refactor(pipeline): share the Compose runtime setup and name the scenario only in
   Python" (the run ID reads the name from the default scenario file).
8. `execution/stages.py:104`: `StageGraph.__iter__` sorts again on every iteration (O(n²)); the order
   could be computed once in `__post_init__`.
   **Fixed:** "refactor(stages): order the graph once and keep one rule for the report stage".
9. `execution/containers.py:49,315,343`: `DEFAULT_CONFIGURATION` in `run_coinjoin_analysis` /
   `initialize_images`: the Compose environment of these calls ignores the run's engine, timezone
   and images (harmless today, the analysis service reads none of them).
   **Fixed:** "fix(containers): give the local coinjoin-analysis and image steps the run's
   configuration".
10. `execution/kubernetes.py:14,16`: two separate imports from `coinjoin_pipeline.paths`.
    **Fixed** in the first wave of fixes.
11. `execution/k8s_s3_uploader.sh:3-30`: the timeout counts only seconds of sleep (`remaining -= 2`);
    the two kubectl calls per loop are not counted, so the real timeout is longer than
    `EMULATION_TIMEOUT_SECONDS`.
    **Fixed** in the first wave of fixes (a deadline; with a slowed kubectl the 5 s timeout took
    18 s, now 6 s).
12. `storage/s3.py:263-279`: `delete_s3_object_if_present` = ls + rm + ls; clearing a stage's two
    markers costs 6 s5cmd calls.
    **Fixed:** "refactor(s3): clear a stage's markers with one rm and one listing" (checked against
    MinIO: `rm` of an absent key succeeds).
13. `storage/s5client.py:262-271`: `restore_archive` (the emulation cache) extracts with a plain
    `tar -x`, without the member-root and link checks `restore_backup` does.
    **Fixed** with refactoring 1: `restore_archive` is gone, and every index restores through
    `restore_backup`.
14. `execution/s3_staging.py:73-82,117-121`: `s5cmd sync` without `--delete`: refreshing a fixed state
    prefix keeps exporters removed from the checkout, and the tree hash on the node then differs
    from the logged one.
    **Fixed:** "fix(s3): drop staged exporter files the checkout no longer has" (checked against
    MinIO).
15. `execution/pbs/submission.py:64-71`: `qsub` has no timeout (`qstat`/`qdel` have
    `PBS_COMMAND_TIMEOUT_SECONDS`); a hung `qsub` blocks the frontend while it holds the submit lock.
    **Fixed:** "fix(pbs): give qsub a timeout so a hung server cannot block the frontend" (120 s; the
    message warns the job may have been accepted).
16. `execution/pbs/blocksci_index.py:41-50`: `canonicalize_index` rewrites `dataDirectory` in
    `config.json` with a regex instead of a json load/dump.
    **Fixed:** "fix(blocksci): edit config.json through json instead of regexes" (`set_max_block`
    and the external index's `maxBlockNum` too).
17. `execution/pbs/blocksci_index.py:97`: `open_index` requires `zstd` also for read-only jobs that
    restore nothing.
    **Fixed:** "fix(blocksci): require zstd only where an index archive is packed or unpacked".

## Notes on the new history (trade-offs)

The new history (`clean-history`, 179 commits up to the findings, a tree identical to `main`) was
built by peeling features off the target state one by one. The intermediate states were cleaned of
residue (comments and docstrings that mention features that do not exist yet, orphaned helpers and
fixtures), but a few things were kept on purpose:

- The `EXPERIMENTAL … Not part of the Kubernetes → S3 → PBS path` headers in the local modules and
  scripts exist from the first commit, although that path arrives only later.
- Podman, JoinMarket and the YAML configuration are part of the initial commit (they run through
  every shell script and the configuration model; untangling them would be artificial).
- `README.md` describes the finished project from the start; it only gains sections over time.
- The test `tests/pipeline/test_unified_report.py` arrives with the detector tests before the report
  itself exists (the file name comes from the target state).
- `process.run` (with the `replace` option and signal handling) arrives with `cjp pull` already,
  although only later utilities use those abilities.
- `tests/pipeline/test_delete_profiles.sh:47` mentions the k3d suite as the historical reason for
  cleaning up abandoned projects, also in commits before Kubernetes.
