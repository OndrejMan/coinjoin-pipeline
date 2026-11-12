# Forward commit plan (story order)

Peeling runs from the bottom of this list to the top. Each line: subject | what it adds.
"H:" marks hub files that grow in that commit (configuration.py, arguments.py, option_rules.py,
cli.py, context.py, pyproject.toml, paths.py, artifact_paths.py, ...). Tests that cover one
feature land with it; contract-style test modules land as their own test(...) commit after the
last feature they need. Bodies are one sentence, written at peel time.

## Block 1 — package, configuration, CLI skeleton
1  build: add the coinjoin-pipeline package skeleton | pyproject (core), uv.lock, .gitignore, README stub, __init__, console, conftest
2  feat(config): declare the experiment configuration as typed dataclasses | configuration.py core models + decoding
3  feat(cli): derive command-line options from the configuration model | arguments.py build_parser/option_help/sections core
4  feat(cli): load an experiment from YAML with cjp run | load_configuration + action word
5  feat(images): resolve component images from a release tag | images.py
6  feat(cli): print the CLI version and the default component images | cli.print_version, utilities skeleton
7  feat(layout): name every run-directory path in one module | exporters/__init__, artifact_paths.py (core)
8  feat(runs): generate run IDs from the timestamp and the scenario | runs.py run_id_for/slugify
9  feat(manifest): record every launch in a redacted research manifest | manifest.py + runs host manifest store
10 feat(context): resolve one run's configuration, images and environment | context.py core
11 feat(cli): validate an experiment and print its dry-run plan | cli.main, preflight
12 feat(doctor): check the container runtime, runs root and images | doctor.py, cjp doctor
13 feat(cli): pull every component image | cli.pull
14 test(cli): cover configuration decoding and the CLI contract | tests/unit/test_configuration.py etc. (parts that need only block 1)

## Block 2 — local Docker/Podman execution
15 feat(execution): define the analysis stage graph | stages.py (analysis_plan, StageGraph)
16 feat(execution): run ready stages with bounded concurrency | stage_executor.py
17 feat(logging): tee every pipeline stage into its own log file | pipeline_logging.py
18 feat(scenarios): find, validate and mount scenario files | scenarios.py, settings.py core, scenarios/*.json
19 feat(execution): select the container runtime and its compose command | runtime.py
20 feat(compose): describe the local emulation and analysis services | pipeline/compose.yaml, anonymous-docker-config.json
21 feat(emulate): run the emulator in Docker-in-Docker | pipeline/emulate.sh, containers.run_script, local emulate
22 feat(clean): remove the local pipeline containers and volumes | pipeline/delete.sh, clean action
23 feat(analyze): parse the emulated chain with BlockSci locally | pipeline/analysis.sh, analyze action
24 feat(locks): serialise commands on one runs root | locks.py (command locks only)
25 feat(execution): dispatch local actions from the orchestrator | orchestrator.py (local only), local.py, cli.execute
26 feat(full-run): emulate and analyze in one command | local full-run + run_analysis graph
27 feat(full-run): run the local analysis stages in parallel | --parallel
28 feat(coinjoin-analysis): run the baseline analyzer over a run | containers.run_coinjoin_analysis, all_runs, analysis_action
29 feat(execution): initialize the emulation and analysis images | initialize action
30 feat(cli): run a checkout without installing it | runIt.sh
31 test(execution): cover the stage graph, executor, logging and scenarios

## Block 3 — exporters and the unified report
32 feat(exporters): share JSON, hashing and git helpers | common.py, report_types.py (core)
33 feat(exporters): normalize the coinjoin-analysis baseline | normalization.py
34 feat(exporters): build emulator labels from exported blocks | emulator_data.py, script_metadata.py
35 feat(exporters): validate the emulator's producer labels | emulator_data verified sources
36 feat(exporters): normalize the scenario and check its funds | scenario.py
37 feat(exporters): explain BlockSci's Wasabi and JoinMarket rules | heuristics.py
38 feat(exporters): compare analyzers and compute detection metrics | comparison.py
39 feat(exporters): read BlockSci through a typed adapter | blocksci_export/detector.py
40 feat(exporters): persist a versioned BlockSci analysis artifact | blocksci_export/analysis.py, analysis_artifact.py, parameters.py
41 feat(exporters): run parse, analyze and report from one worker | worker.py
42 feat(exporters): record images and exporter hashes in the run manifest | manifest.py, image_provenance.py
43 feat(exporters): diagnose BlockSci's view of the exported targets | integration_diagnostics.py
44 feat(report): assemble the unified JSON report | report_builder.py, exporters/cli.py, unified_report.py
45 feat(report): render the report as Markdown | markdown_report.py
46 feat(report): evaluate BlockSci clustering against wallet labels | clustering parts
47 feat(report): restrict an external baseline to the scanned window | external mode/coverage
48 feat(report): filter curated false positives from the baseline | false_cjtxs
49 feat(export): export the unified report of a local run | containers.run_export_only, export action
50 test(report): cover the unified report assembly | test_unified_report.py, test_report_assembly.py, ...

## Block 4 — PBS on shared storage (experimental)
51 feat(pbs): validate values interpolated into PBS scripts | pbs/defaults.py, pbs/validation.py
52 feat(pbs): submit jobs and record their IDs | pbs/submission.py, pbs/status.py, probes.py
53 feat(pbs): resolve per-stage PBS resources and images | pbs_settings.py, config PBS sections
54 feat(pbs): render shared-storage job scripts | pbs/scripts.py, templates_local, templates/local_*.sh
55 feat(pbs): build the in-container analyzer commands | pbs/commands.py
56 feat(pbs): run analyzers as PBS jobs over a shared run | submission_local.py, shared_storage_pbs.py, workflow.py
57 feat(mappings): enumerate Wasabi mappings and run Sake on PBS | mappings_run.sh, mappings action
58 feat(pbs): keep the Kerberos ticket alive while PBS work is pending | pbs/kerberos.py
59 test(pbs): cover shared-storage submission and markers

## Block 5 — Kubernetes
60 feat(kubernetes): resolve the kubeconfig and preflight permissions | kubernetes.py core
61 feat(kubernetes): run the emulation on a cluster | kubernetes_launch.py
62 test(kubernetes): ...

## Block 6 — Kubernetes → S3 → PBS
63 feat(storage): access S3 through s5cmd with scrubbed credentials | storage/s5client.py core
64 feat(storage): validate S3 run targets | storage/s3.py
65 feat(s3): stage the exporters into the run prefix | s3_staging.py
66 feat(kubernetes): render the S3 emulation Job and its uploader | kubernetes.py S3 part, k8s_s3_*.sh, container/uploader.*
67 feat(s3): wait for stage markers with liveness probes | s3_markers.py
68 feat(registry): pin image tags to registry digests at submission | registry.py
69 feat(pbs): run every S3 job through one Python stage runtime | stage.py, stage_settings.py, stage_runtime.py, apptainer.py
70 feat(pbs): run coinjoin-analysis and the report as S3 jobs | analyzer_jobs.py, templates_s3 core
71 feat(pbs): parse and detect BlockSci in one S3 job | blocksci_jobs.blocksci, blocksci_index.py
72 feat(s3): submit the S3 PBS graph with rollback | s3_submission.py, stages S3 plan
73 feat(full-run): run the Kubernetes → S3 → PBS full run | s3_workflow.py, s3_emulation.py, orchestrator S3
74 feat(pbs-from-s3): submit analysis jobs for a run already in S3 | pbs-from-s3 action
75 feat(mappings): run the mappings stage over S3 | mappings S3
76 feat(blocksci): publish a reusable index and analyze from it | blocksci-parse, blocksci-analysis, workflows
77 feat(blocksci): update a cached index incrementally | blocksci-update
78 feat(blocksci): run a user script over a cached index | script task
79 feat(blocksci): serve a Jupyter notebook over a cached index | notebook task, templates/blocksci_notebook.py
80 feat(blocksci): parse a verified block archive from S3 | block_archive.py, verify_chain.py
81 feat(blocksci): keep a persistent index with a single S3 backup | index_store.py, s5client backups
82 feat(blocksci): compare an external chain against a Dumplings baseline | external task, dumplings baseline
83 feat(report): download the finished report of an S3 run | download_report.py
84 feat(s3): delete the S3 artifacts of a run or a runs root | clean_s3.py
85 test(s3): ...

## Block 7 — run control
86 feat(watch): stream a run's Kubernetes and PBS logs | watch.py
87 feat(status): report recorded jobs, emulation and markers | run_records.py, status.py
88 feat(stop): delete a run's resources and cancel its jobs | stop.py
89 feat(full-run): resume an S3 full-run whose driver died | s3_resume.py
90 feat(layout): print the run layout as JSON | run_layout.py

## Block 8 — research tools
91 feat(runs): list, inspect and validate local runs | run_catalog.py, research.py runs/scenarios
92 feat(external): analyze an external Bitcoin datadir | external-analyze
93 feat(coinjoin-analysis): run upstream coinjoin-analysis programs | coinjoin_tools.py
94 feat(coinjoin-analysis): adapt research functions of the analyzer | coinjoin_research.py, wabisator
95 feat(statistics): build CoinJoin Statistics from Dumplings | coinjoin_statistics.sh
96 feat(report): evaluate pool liquidity | liquidity.py

## Block 9 — mainnet
97..  mainnet_sync, worker, jobs, runtime, examples, scripts

## Block 10 — runtime image, test suite, CI
...

## Block 11 — docs
...
