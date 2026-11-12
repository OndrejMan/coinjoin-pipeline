# coinjoin-pipeline — nálezy z průchodu při přestavbě historie

Stav kódu: `main` 9aa44e9 („test(e2e): run the parallel variants as wrappers of the serial tests“), 2026-11-12.
Odkazy `soubor:řádek` platí pro tento stav (u novějších nálezů je místo řádku jméno funkce).
Nic z toho jsem neopravoval, jen zapsal.

Závažnost: **S** střední (může dát špatný výsledek nebo zablokovat běh), **N** nízká
(čitelnost, kosmetika, drobná nekonzistence). Jistota: *ověřeno* (přečteno v kódu a dohledáno
všechna místa), *podezření* (vyplývá z kódu, neověřeno během).

## Stav oprav (2026-11-12)

Opravy jsou nové commity nad historií ve větvi `clean-history`; existující commity jsem neměnil.
Čísla odkazují na položky v sekcích níže.

**Opraveno**

- Bugy 1 — úklid při přerušení hledá kontejnery podle labelu Compose projektu
  („fix(cleanup): stop the Compose project's containers when the CLI is interrupted“).
  Při tom se ukázalo, že `tests/test-wrapper-signal-cleanup.sh` vždy padal na případu TERM:
  oba případy běžely ve stejné minutě a druhý narazil na kolizi run ID
  („fix(test): give each signal-cleanup case its own run ID“).
- Nesrovnalosti 9 (komentář v `delete.sh`), 18 (`zstd` v kombinovaném jobu), 24 a 25 (nápověda
  `--blocksci-script` a `--distributor-startup-timeout`), 26 (epilog utilit s vlastním `--help`),
  27 (exit kódy 3/4), 28 (funkce za `__main__`), 37 (README: šest jobů mainnetu).
- Zbytečný kód 1 (`unified_report.py`), 3 (Namespace v S3 Jobu), 6 a 7 (`report_here` v
  kombinovaném BlockSci jobu), 9 (`walltime_to_seconds`), 11 (`check_images`), 12 (volumy
  v `delete.sh`), 14 (`--build`), 15 (komentář v `compose.yaml`), 16 a 17 (hlášky o starém
  wrapperu a přejmenování), 18 (aliasy `$EMULATOR_IMAGE*`), 19 (`TARGET_DETAIL_LIMIT`),
  20 (`skipped_txids` ve fixturách), 21 (`pbs_submission_dir`).
- Overengineering 2 (`shell_value`; `shell_assignment` zůstal).
- Refaktoring 4 (`BlockSciWorkflow`), 9 (`PBS_SCRATCH_TYPES`/`PBS_MULTIJOB_LOCATIONS`; literály teď
  žijí v `stages.py` a `pbs/defaults.py` a konfigurace je importuje, jinak by vznikl cyklický
  import), 10 (kořeny běhů v kontejneru).
- Zlepšení 3 (`process.run` vypíše důvod), 4 (formátování `images.py` a `storage/s3.py`),
  10 (importy v `kubernetes.py`), 11 (timeout uploaderu jako deadline; se zpomaleným kubectl
  trval 5s timeout 18 s, teď 6 s).
- Navíc: commit nástrojů přestavby dostal `.peel/` pod `ruff check .`, opraveno v „style(history): …“.

**Nepotvrdilo se**

- Nesrovnalosti 1 — volby `kubernetes.btc_node_image` a `image_pull_policy` nejsou tiše
  ignorované: `option_rules` je mimo S3 emulaci výslovně odmítne a nápověda je uvádí jako S3-only.
- Zbytečný kód 4 — sdílená hlavička `k8s_s3_access.sh` funkci `write_s3_credentials` jen
  definuje; preflight i uploader ji volají každý jednou.

**Ponecháno záměrně / k rozhodnutí**

- Bugy 2 — výchozí akce `full-run` je dokumentovaná (i pro `runIt.sh`); změna je na tobě.
- Bugy 3 — povinné `kinit -R` u `pbs-from-s3`/`mainnet-sync` je popsané jako kontrakt
  v `docs/pbs-stage-contract.md:408`; změna je rozhodnutí o provozu na MetaCentru.
- Zbytečný kód 5, 8, 10 — fallbacky image chrání volání mimo `RunContext` (testy a přímé volání
  knihoven); přínos odstranění je malý.
- Refaktoring 11 — `embedded_file` je inverze `write_files` a sdílí s ním `_delimiter`.
- Zlepšení 13 — `restore_archive` čte archiv z vlastního bucketu a ověřuje SHA-256; tvrdší
  kontrola členů zůstává jako zlepšení.

Ostatní položky jsou dál otevřené.

## Potenciální bugy

1. **S, ověřeno** — `src/coinjoin_pipeline/execution/containers.py:cleanup_peer_containers` +
   `execution/settings.py:41` `PEER_CONTAINERS`: při přerušení CLI zastavuje kontejnery jménem
   `blocksci_analyzer`, `emulator_manager`, `btc_data_wiper`, `isolated_docker_daemon`… Jenže
   `pipeline/compose.yaml` nikde nenastavuje `container_name`, takže Compose kontejnery pojmenuje
   `<projekt>-<služba>-1` a `docker stop` netrefí nic. Úklid ve skutečnosti obstarávají jen trapy
   v `emulate.sh`/`analysis.sh`.
2. **S, podezření** — `src/coinjoin_pipeline/configuration.py:312` + `arguments.py:344`: YAML bez
   klíče `action` tiše spadne na výchozí `full-run`; překlep nebo chybějící klíč tak spustí celou
   emulaci.
3. **S, podezření** — `execution/orchestrator.py:169-171,193-200` + `execution/pbs/kerberos.py:385-396`:
   `pbs-from-s3` a `mainnet-sync` joby jen odešlou a skončí, přesto vyžadují úspěšné `kinit -R`.
   S výchozím neobnovitelným ticketem MetaCentra by `qsub` prošel, ale příkaz se odmítne spustit.

## Nesrovnalosti (kód vs. dokumentace, nápověda, testy)

1. **S** — `execution/kubernetes_launch.py:80-88,174`: Kubernetes cesta bez S3 bere btc-node image,
   pull policy, control IP, síť i UID úložiště z proměnných prostředí (`COINJOIN_BTC_NODE_IMAGE`,
   `KUBERNETES_IMAGE_PULL_POLICY`…) a ignoruje `kubernetes.btc_node_image` /
   `kubernetes.image_pull_policy`, které S3 cesta čte z konfigurace.
2. `configuration.py:204-210`: pole `PBSResourceConfiguration` deklarují `option(name="ncpus"…)`,
   ale `option_specs` (467) jméno pro tento model přepíše cestou — metadata `name` jsou mrtvá a
   matoucí (skutečné přepínače jsou `--pbs-analysis-ncpus` atd.).
3. `arguments.py:335`: `_canonical_action` po odstranění aliasů jen validuje, jméno slibuje kanonizaci.
4. `context.py:45-46`: `pbs-from-s3`/`mainnet-sync` vynutí `artifacts.backend=s3` i když uživatel
   výslovně zadal shared-storage — tichý override místo chyby.
5. `run_layout.py:219-223` + docstring 204-206: docstring tvrdí, že každé jméno pochází z modulu,
   který ho definuje (včetně grafu stage), ale jména stage jsou řetězcové literály zkopírované
   z `execution/stages.py`.
6. `execution/scenarios.py:13` vs. `context.py:134`: adresář scénářů má dvě proměnné prostředí —
   `SCENARIOS_ROOT` (čte scenarios.py) a `SCENARIOS_DIR` (nastavuje host_environment, čte compose,
   containers, kubernetes_launch).
7. `execution/run_context.py:302-315`, `scenarios.py:160-165`, `containers.py:265-267,379-384,508-510`:
   knihovní funkce tisknou `[ERROR]` a volají `sys.exit(2)` místo výjimky jako zbytek kódu; obchází
   to chybové zpracování v `cli.py` i stav v záznamu spuštění.
8. `containers.py:162-166`: `compose_env` bere jako výchozí runs root `<checkout>/coinjoin-runs`,
   `paths.default_runs_root` ale `$PWD/coinjoin-runs`; adresář scénářů se znovu odvozuje místo
   `paths.SCENARIOS_ROOT`.
9. `pipeline/delete.sh:31-39`: komentář říká, že jméno Compose projektu nese PID spouštějícího
   procesu; CLI ho ale jmenuje `blocksci-emulator-<hash checkoutu>`
   (`containers.compose_project_name`), PID používají jen testy.
10. `execution/kubernetes.py:1`: docstring modulu mluví o „The S3 emulation Job“, modul ale drží i
    preflight bez S3 a obecné kubectl pomocníky.
11. `storage/s3.py:283` vs. `s5client.py:188` + `docs/architecture.md`: frontend vyžaduje s5cmd
    alespoň 2.1 (`sync --exclude`), nahrávání archivů ale potřebuje `s5cmd pipe` z 2.2+; funguje,
    jen dokud frontend archiv nestreamuje. *Podezření.*
12. `storage/s3.py:107`: hláška „--artifact-uri is required (or set ARTIFACT_URI)“ — hlavní CLI
    `$ARTIFACT_URI` nikdy nečte (jen `add_s3_arguments` utilit).
13. `storage/s3.py`: modul obsahuje i validaci run ID (`is_valid_run_id`, `validate_run_id`), kterou
    importují lokální cesty (`context`, `option_rules`, `run_context`) — lokální kód tak závisí na
    modulu, jehož jméno slibuje S3.
14. `execution/pbs_settings.py`: lokální Docker export bere `resolve_unified_report_image` a
    `read_image_lock` z modulu PBS nastavení; obecný generický `truthy_env` (218) tu žije také.
15. `execution/pbs_settings.py:263-270`: `images.registry` přesune výchozí image uploaderu
    (`in_registry`), ale ne výchozí image unified-reportu; registry forku tak dál spouští reportový
    image projektu.
16. `execution/s3_workflow.py:452-458`: závěrečná zpráva má jména výstupních adresářů natvrdo
    místo konstant z `artifact_paths`.
17. `execution/pbs/analysis_modes.py:82`: výstup notebook jobu radí „Open JupyterLab from Scenario
    Lab“ — výstup pipeline je svázaný se samostatným Lab UI.
18. `execution/pbs/blocksci_jobs.py:123-124` vs. 347: kombinovaný job kontroluje `zstd` ručně se
    stejnou hláškou, jakou dává `require_tools`; ostatní joby volají `require_tools`.
19. `execution/pbs/blocksci_jobs.py:blocksci` vs. `blocksci_parse`/`run_parser`: kombinovaný job
    volá singularity sám s `cleanenv=True`, parse/update jdou přes `run_parser` bez `cleanenv` —
    dvě cesty ke stejnému parseru se liší prostředím kontejneru.
20. `execution/pbs/blocksci_jobs.py:205,471`: jména objektů `blocksci-index-manifest.json` /
    `blocksci-index-backup.json` v prefixu běhu (čte je Lab) jsou literály, ne v `artifact_paths`/
    `run_layout`.
21. `execution/pbs/templates/mappings_run.sh:20-21,44`: `enumerator_image_digest`/`sake_image_digest`
    jsou sha256 lokálně staženého SIF souboru, ne digest z registry; liší se mezi dvěma pully
    téhož image.
22. `execution/pbs/templates_s3.py:_index_job_settings` + `blocksci_jobs.publish_persistent`:
    `pbs_resources` se renderuje do každého parse/update jobu, ale do manifestu ho zapíše jen
    perzistentní index; manifest S3 cache prostředky jobu nezaznamená, ačkoli docstring
    `index_manifest` je uvádí obecně.
23. `option_rules.py:_run_selection_errors` + `context.py` (`validate_run_id`): run ID (i
    `--blocksci-cache-source-run-id`) se validuje dvakrát.
24. `arguments.py:HELP[blocksci.script]`: nápověda říká „Python script of --blocksci-task script“,
    lokální Docker BlockSci stage (`containers.run_blocksci_docker_stage`) ale stejnou volbu bere
    i bez úlohy `script`.
25. `arguments.py:HELP[kubernetes.distributor_startup_timeout]` vs.
    `tests/pipeline/test_s3_emulation_options.py`: nápověda slibuje fallback na
    `$COINJOIN_DISTRIBUTOR_STARTUP_TIMEOUT`, S3 Job ale proměnnou prostředí záměrně ignoruje;
    fallback platí jen pro lokální compose.
26. `arguments.py:257`: epilog vyjmenovává utility s vlastním `--help` (watch, download-report,
    clean-s3, runs, scenarios), chybí `status` a `stop` (`DELEGATING_UTILITIES`).
27. `execution/research.py:133,140,142,327-333`: `runs validate` končí kódy 3/4 a `main` vrací
    literály 5/2; `console.EXIT_CODES` popisuje 3 jako „clean-s3 was not confirmed“ a 4 nezná.
28. `execution/research.py:346-363`: `run_external_configuration` je definovaná až za
    `if __name__ == "__main__"`.
29. `execution/coinjoin_tools.py:run_local` vs. `pbs/analyzer_jobs.coinjoin_tools`: lokální
    výsledkové adresáře se jmenují podle uuid4, PBS podle UTC času + ID jobu.
30. `pipeline/exporters/parameters.py:Detector.artifact_parameters`: artefakt analýzy zapisuje
    parametry JoinMarket subset detektoru i pro Wasabi běhy, takže změna kteréhokoli z nich zneplatní
    Wasabi artefakt („parameters do not match“), zatímco `Detector.manifest` drží jen klíče
    zvoleného typu.
31. `containers.py:export_command` + `compose.yaml`: lokální Docker export předává
    `--uploader-image` a compose `COINJOIN_UPLOADER_IMAGE`, ačkoli lokální běh S3 uploader nikdy
    nepoužívá.
32. `pipeline/exporters/integration_diagnostics.py:IMAGE_COMPONENTS`: druhá konstanta
    `IMAGE_COMPONENTS` s jiným významem než `parameters.IMAGE_COMPONENTS`.
33. `pipeline/exporters/integration_diagnostics.py:build_image_diagnostics`: analýza znovu
    inspektuje image přes runtime, i když host je už prohlédl a předal `*_IMAGE_ID`/`*_IMAGE_DIGEST`
    (dvě cesty: `containers.add_image_provenance_env` a `docker_image_provenance`).
34. `pipeline/exporters/manifest.py:build_run_manifest` (`source_commits.exporters`): `git rev-parse`
    běží v report kontejneru, který mountuje jen adresář exportérů bez `.git`, takže commit exportérů
    je u každého lokálního i PBS reportu `null`; kód identifikuje jen hash stromu.
35. `pipeline/exporters/cli.py` (`--engine`): CLI exportérů bere `--engine` jen pro manifest a jinak
    ho odhaduje z `coinjoin_type`.
36. `pipeline/exporters/markdown_report.py:save_text`: obecný zapisovač souborů žije v Markdown
    rendereru a CLI exportérů ho importuje odtud.
37. `README.md:9-11`: úvod tvrdí, že synchronizace mainnetu „uses two dependent PBS jobs“; jobů je
    šest (restore, producer, blocksci, checkpoint, pack, clear).
38. `MIGRATION.md`, `MAINNET-STATUS.md`, `TODO.md` §2/§5, `run-all-local.sh:11`,
    `run-all-github.sh:11`: dokumenty a wrappery odkazují mimo repozitář (`../docs`,
    `../scenario-lab`, `../.agents/skills`); TODO §2 stále mluví o shellových fragmentech
    v S3 rendererech, které dnes nahradil Python stage runtime.
39. `tests/pipeline/test_refactor_regressions.py`, `tests/pipeline/test_wrapper.py`: jména testových
    modulů popisují historii (refactor, odstraněný wrapper) místo chování, které testují.
40. `docs/mainnet-synchronization.md`, `docs/mainnet-sync-local-validation-2026-10-25.md`: česky,
    zbytek dokumentace anglicky.
41. `.dockerignore`: žádný build v repozitáři nepoužívá kořenový kontext s tímto souborem (runtime
    image má `container/runtime.Dockerfile.dockerignore`, uploader se builduje z `container/`).
    *Podezření.*

## Zbytečný a mrtvý kód

1. `pipeline/exporters/unified_report.py` + `paths.py:24` + `execution/pbs/analyzer_jobs.py:45`:
   `unified_report.py` se nikdy nespouští (každý report jde přes `worker.py report`, testy to
   dokonce kontrolují), přesto je v `REQUIRED_EXPORTERS` a report job bez něj odmítne start.
   *Ověřeno (rg).*
2. `configuration.py:JoinMarketConfiguration`: čtyři CLI/YAML volby, čtyři proměnné prostředí a
   výchozí hodnoty v compose existují jen pro deprecated subset-matching sken v reportu.
3. `execution/kubernetes.py:499-500`: Namespace se renderuje, když `not reuse_namespace`, ale
   `kubernetes_s3_auth_preflight` (127-130) S3 režim bez `--reuse-namespace` odmítne — větev je
   dosažitelná jen z testů a dry-runu. *Ověřeno:* `option_rules._s3_errors` `--reuse-namespace`
   pro S3 emulaci vyžaduje.
4. `execution/k8s_s3_uploader.sh:2`: znovu volá `write_s3_credentials`, i když sdílená hlavička
   `k8s_s3_access.sh:19` soubor už zapsala.
5. `execution/s3_emulation.py:184,209`: fallback `or settings.DEFAULT_EMULATOR_IMAGE` je
   nedosažitelný — `RunContext.prepare` vždy vyřeší `images.emulator`.
6. `execution/s3_submission.py:195`: `self.args.stages.blocksci and` je uvnitř `_blocksci_work` vždy
   pravda (stage existuje jen s `blocksci_pbs`); podmínka je jen `task != "detect"`.
7. `execution/s3_submission.py:_blocksci_work` (`report_here`): uzel `blocksci` vzniká jen
   s `--blocksci-pbs` a kombinovaný workflow povolí jen úlohu detect, takže `report_here` je vždy
   False a větev s `include_report=True` v S3 cestě nikdy nenastane.
8. `execution/pbs_settings.py:247`: fallback `DEFAULT_PBS_IMAGES[component]` je nedosažitelný, když
   `context.resolve_run_images` vyplní každý `pbs.*_image`.
9. `execution/pbs/validation.py:21-28`: větev podle délky ve `walltime_to_seconds` i její `else` jsou
   nedosažitelné, regex pustí jen 3 nebo 4 části.
10. `execution/coinjoin_tools.py:run_local`: fallback image na `image_names(...):version` je
    nedosažitelný, `RunContext` `images.coinjoin_analysis` vždy vyřeší.
11. `doctor.py:139,170`: parametr `check_images` žádný volající nepředává (vždy True).
12. `pipeline/delete.sh:29`: maže volumy `<projekt>_blocksci_cache` a `<projekt>_emulation_logs`,
    které `compose.yaml` už nedefinuje (jen `btc_data`, `dind_cache`).
13. `pipeline/analysis.sh:21-23`, `emulate.sh:15-17`, `delete.sh:6-8`: fallback na `/compose.yaml`
    existuje kvůli odstraněnému kontejnerizovanému wrapperu (kompatibilita se nechce).
14. `pipeline/analysis.sh:39`: `compose up --build`, ačkoli žádná služba nemá sekci `build`.
15. `pipeline/compose.yaml:3,21`: komentář „The Python Manager that controls the emulator“ stojí nad
    službou `dind`; zakomentovaný mount `daemon.json`.
16. `execution/locks.py:378-381` (`acquire_lock`): hláška při `PermissionError` mluví o root-owned
    souborech „the old containerized wrapper“ a radí `chown` — pozůstatek kompatibility se starými
    běhy.
17. `execution/s3_staging.py:128-131`: chybová hláška vysvětluje neúplný strom exportérů tím, že
    „predates the blocksci_export rename“ (staré běhy, kompatibilita se nechce).
18. `pipeline/exporters/parameters.py:197-215`: `--coinjoin-emulator-image*` padají zpět na
    `$EMULATOR_IMAGE` / `$EMULATOR_IMAGE_DIGEST` / `$EMULATOR_IMAGE_ID`, které už nic v projektu
    nenastavuje (staré aliasy).
19. `pipeline/exporters/integration_diagnostics.py:TARGET_DETAIL_LIMIT`: definovaná, nikde
    nepoužitá.
20. `tests/pipeline/test_report_assembly.py`, `tests/pipeline/test_refactor_regressions.py`: fixtury
    artefaktu analýzy nesou top-level `"skipped_txids"`, které `BlockSciAnalysisArtifact` nikdy
    nedefinuje (pozůstatek tvaru subset-matching).
21. `tests/pipeline/config_support.py:configuration` (`pbs_submission_dir`): volba helperu se nikde
    nepoužívá (pozůstatek shared-storage testů).

## Overengineering

1. `storage/s3.py:52-102,212-230`: tři vrstvy pro jedno volání s5cmd — `S3Target` → `S3Access` →
   `S5Client`, plus obaly `_s5`/`run_s5cmd`, které jen převádějí chyby.
2. `storage/s3.py:203-209`: `shell_value` je `shlex.quote` pod jiným jménem; `shell_assignment`
   je jednořádek.
3. `execution/stages.py:40-66`: `RESOURCE_GROUPS` + `resource_group()` — jednořádkový obal
   kolem dict lookupu.
4. `pipeline/exporters/artifact_paths.py:94-111`: `_tool_dir` plus čtyři jednořádkové obaly
   `run_dir / KONSTANTA`; `report_dir` přitom `_tool_dir` ani nepoužívá.
5. `arguments.py:37`: `CONFIGURATION_FLAGS` je jednoprvková n-tice, se kterou se všude zachází
   jako s kolekcí.
6. `cli.py:90`: každá utilita (watch/status/stop/…) prochází `RunContext.prepare` včetně
   rozlišení image a plné validace jen kvůli runs rootu.

## Kandidáti na refaktoring

1. **S** — `execution/pbs/blocksci_index.py:25,61-89` vs. `index_store.py:42-44` +
   `s5client.replace_backup/restore_backup`: dva formáty archivu pro jeden BlockSci index —
   cache běhu (`blocksci_data.tar.zst` + `.sha256` + manifest, obnova obyčejným `tar` přes
   `restore_archive`, kontroly v `restore_index`) a perzistentní záloha (`blocksci_index.tar.zst`,
   manifest s členy, vlastní ověřující extraktor). Dva manifesty, dvě validační cesty.
2. `execution/pbs/blocksci_jobs.py:blocksci_parse`/`blocksci_update`: obě funkce mají vlastní větev
   `if settings["persistent"]` a `if index is not None: publish_persistent … else: publish_index` —
   dvě úložiště indexu procházejí stejnými funkcemi přes None-flag místo jednoho rozhraní.
3. `arguments.py:27-34`: `field_type` znovu implementuje rozbalování `Optional`, které už je
   v `configuration.decode_value`.
4. `execution/stages.py:16` + `configuration.py:BlockSciWorkflow`: stejný Literal
   `["combined", "reusable", "cached"]` je definovaný dvakrát.
5. `execution/stages.py:161-168` vs. 278: pravidlo „report existuje právě když `blocksci_pbs` a
   task == detect“ je napsané dvakrát (`runs_unified_report` a `s3_full_run_plan`).
6. `execution/containers.py:460-495` + 530: `export_command` spojí argv přes `shlex.quote` do
   řetězce, který `run_export_only` hned zase `shlex.split`.
7. `pipeline/analysis.sh`, `emulate.sh`, `delete.sh`: každý skript znovu implementuje validaci
   `CONTAINER_RUNTIME` a parsování `CONTAINER_COMPOSE_COMMAND`, které už je
   v `execution/runtime.py`.
8. Výchozí jména scénářů `overactive-local.json` / `defaultJoinMarket.json` jsou napsaná v
   `settings.py:23-24`, `runs.py:145`, `emulate.sh:5-8` a `compose.yaml:127`.
9. `execution/pbs/defaults.py:9,11`: `PBS_SCRATCH_TYPES` / `PBS_MULTIJOB_LOCATIONS` opakují
   literály `configuration.PBSScratchType` / `PBSMultijob`.
10. `execution/pbs/commands.py:42,51,87,185`: kořen běhů v kontejneru `"/runs/emulation/logs/"` je
    literál v každém builderu (`settings.RUNS_ROOT_CONTAINER` existuje).
11. `execution/pbs/scripts.py:61-64`: `embedded_file` používají jen testy (5 modulů) — testovací
    helper v produkčním modulu.
12. `execution/pbs/blocksci_jobs.py:310-311` + `execution/block_archive.py`: `block_archive` zapisuje
    pole manifestu jako JSON fragment začínající čárkou a job ho obalí `"{" + lstrip(",") + "}"`.
13. `execution/pbs/templates/mappings_run.sh:43-45`: sestavení reportu je ~600znakový Python
    one-liner uvnitř shellu.
14. `execution/pbs/templates/mappings_run.sh:44` + `pipeline/exporters/report_builder.py:with_effective_mapping_status`:
    stav partial/complete artefaktu mappings se počítá dvakrát stejnými pravidly.
15. `status.py:16-44`, `stop.py:135-161`, `watch.py`: každá utilita znovu deklaruje `--run-id`,
    `--namespace`, `--kubeconfig`, `--search-root`, `--json` s mírně jinou nápovědou a výchozími
    hodnotami.
16. `pipeline/exporters/verify_chain.py:is_block_hash`, `execution/block_archive.py:SHA256_HEX`,
    `pipeline/exporters/emulator_data.py:SHA256_HEX_RE`: tentýž regex 64 hex znaků je třikrát.
17. `configuration.py:JoinMarketConfiguration` / `pipeline/exporters/common.py:DEFAULT_JOINMARKET_*` /
    `compose.yaml`: stejné výchozí hodnoty JoinMarket detektoru (definite, 5000, 4e-05, 200000) jsou
    napsané třikrát.
18. `pipeline/exporters/blocksci_export/analysis.py:INSPECTED_IMAGE_NAMES/PROVENANCE_IMAGE_NAMES`:
    řezy `IMAGE_COMPONENTS[:3]`/`[:4]` — přeházení n-tice tiše změní, které image se inspektují
    a zapisují.
19. `pipeline/exporters/manifest.py:resolve_manifest_images`: znovu vypisuje n-tici komponent i její
    Literal typ místo `parameters.IMAGE_COMPONENTS`.
20. `pipeline/exporters/analysis_artifact.py:ARTIFACT_NAME`, `emulator_data.JOINMARKET_LABEL_SOURCE`:
    aliasy/opakování jmen, která už definuje `artifact_paths` (`ANALYSIS_ARTIFACT_NAME`, jméno
    souboru z `ROUND_EVENTS_FILE`).

## Drobná zlepšení

1. `arguments.py:106-221`: `HELP` pokrývá jen část voleb; `joinmarket.*`, `mappings.*`, `mainnet.*`,
   `coinjoin_analysis.program/cjtype/…`, `research.*` nemají popis, jen „[YAML: …]“.
2. `cli.py:170`: `start_host_manifest` vytváří `<runs_root>/.notebooks` pro každou akci (i mainnet,
   clean…) — adresář notebooků je svázaný s nesouvisejícími příkazy.
3. `process.py:349`: `run()` vrací 127 při jakémkoli `OSError` (i permission denied), text chyby se
   ztratí.
4. `images.py:232-233`: chybí prázdné řádky mezi `in_registry` a `LOCAL_IMAGES`; `storage/s3.py:44-46`
   má tři prázdné řádky (ruff format se v lintu nevynucuje).
5. `images.py:301`: `IMAGE_NAMES` se počítá při importu z prostředí; později nakonfigurované
   `images.registry` konzumenti `IMAGE_NAMES` ignorují.
6. `runs.py:153`: run ID má minutovou granularitu; dvě spuštění v téže minutě kolidují (druhé se
   odmítne, protože první už zapsalo launch). Známý zdroj CI flaky.
7. `runs.py:145`: záložní jména scénářů `default-joinmarket` / `overactive-local` natvrdo kopírují
   výchozí scénáře emulátoru; riziko rozjetí.
8. `execution/stages.py:104`: `StageGraph.__iter__` při každé iteraci znovu řadí (O(n²));
   pořadí by šlo spočítat jednou v `__post_init__`.
9. `execution/containers.py:49,315,343`: `DEFAULT_CONFIGURATION` v `run_coinjoin_analysis` /
   `initialize_images` — compose prostředí těchto volání ignoruje engine, časovou zónu i image
   běhu (dnes neškodné, analytická služba nic z toho nečte).
10. `execution/kubernetes.py:14,16`: dva samostatné importy z `coinjoin_pipeline.paths`.
11. `execution/k8s_s3_uploader.sh:3-30`: timeout počítá jen sekundy spánku (`remaining -= 2`),
    dvě volání kubectl ve smyčce se nepočítají — skutečný timeout je delší než
    `EMULATION_TIMEOUT_SECONDS`.
12. `storage/s3.py:263-279`: `delete_s3_object_if_present` = ls + rm + ls; smazání dvou markerů
    stage stojí 6 volání s5cmd.
13. `storage/s5client.py:262-271`: `restore_archive` (cache emulace) rozbaluje obyčejným `tar -x` bez
    kontrol kořene členů a odkazů, které dělá `restore_backup`.
14. `execution/s3_staging.py:73-82,117-121`: `s5cmd sync` bez `--delete`: obnova pevného state
    prefixu ponechá exportéry smazané z checkoutu; hash stromu na uzlu se pak liší od zalogovaného.
15. `execution/pbs/submission.py:64-71`: `qsub` nemá timeout (`qstat`/`qdel` mají
    `PBS_COMMAND_TIMEOUT_SECONDS`); zaseknutý `qsub` zablokuje frontend se drženým submit zámkem.
16. `execution/pbs/blocksci_index.py:41-50`: `canonicalize_index` přepisuje `dataDirectory`
    v `config.json` regexem místo `json` load/dump.
17. `execution/pbs/blocksci_index.py:97`: `open_index` vyžaduje `zstd` i pro read-only joby, které
    nic neobnovují.

## Poznámky k nové historii (kompromisy)

Nová historie (`clean-history`, 179 commitů, strom shodný s `main`) vznikla postupným odlupováním
funkcí od cílového stavu. Mezistavy jsem čistil od reziduí (komentáře a docstringy zmiňující ještě
neexistující funkce, osiřelé helpery a fixtury), ale pár věcí jsem vědomě nechal:

- Hlavičky `EXPERIMENTAL … Not part of the Kubernetes → S3 → PBS path` v lokálních modulích a
  skriptech existují od prvního commitu, ačkoli ta cesta přibude až později.
- Podman, JoinMarket a YAML konfigurace jsou součástí výchozího commitu (prolínají se všemi
  shellovými skripty a konfiguračním modelem; rozpletení by bylo umělé).
- `README.md` popisuje výsledný projekt od začátku; postupně se rozšiřuje jen o sekce.
- Test `tests/pipeline/test_unified_report.py` vzniká s detektorovými testy dřív, než existuje
  samotný report (jméno souboru je z cílového stavu).
- `process.run` (s volbou `replace` a obsluhou signálů) přibude už s `cjp pull`, i když tyhle
  schopnosti využijí až pozdější utility.
- `tests/pipeline/test_delete_profiles.sh:47` zmiňuje k3d sadu jako historický důvod úklidu
  opuštěných projektů i v commitech před Kubernetes.
