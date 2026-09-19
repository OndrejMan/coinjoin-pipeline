# FINDINGS — coinjoin-pipeline × bitcoin-block-archive

Datum: 2026-09-18. Přečteny oba projekty celé (zdroj, testy, šablony, docs, CI,
compose), plus místa v `blocksci/` a `coinjoin-emulator/`, na která se odkazují.
Nic nebylo měněno ani commitnuto. Odkazy na řádky platí pro `fullS3` @ `237e229`
(pipeline) a `main` @ `c778d4d` (archiv).

Ověřeno spuštěním: `coinjoin-pipeline` `pytest` = 523 passed (38 s);
`bitcoin-block-archive` `pytest` = 54 passed, `mypy --strict` čistý, `ruff`
čistý. E2E `tests/test-bitcoin-block-archive-s3-minio.sh` je v suite zelený od
2026-09-05 (poslední PASS 09-15 07:50, 09-18 06:53).

---

## 0. Verdikt ve třech větách

1. **coinjoin-pipeline má dobrý design tam, kde na tom záleží** — kontrakty
   (marker protokol, DAG stage, fail-closed report, all-or-nothing labely,
   provenance digestů, `PIPELINE_RUN_ID`) jsou promyšlené a otestované. Kontrolní
   vrstva nad nimi je ale **přerostlá a zdvojená** (tři validační vrstvy, bash
   šablony skládané přes `str.format`, `wrapper.py` jako fasáda se 65 re-exporty)
   a potřebuje **konsolidaci, ne nové featury**.
2. **bitcoin-block-archive je čistý malý balík se správně zvolenou hranicí
   vlastnictví** (producent archivu vs. konzument v pipeline). Jeho *nasazení*,
   jak je zabalené v `compose.yaml`, by ale vyrobilo datadir, který BlockSci
   neumí přečíst (XOR), a celý design zatím **postrádá inkrementální cestu**, což
   je jediný důvod, proč 700 GB bloků do S3 vůbec dávat.
3. Udělal bych jinak hlavně tři věci: (a) restore modelovat jako „materializuj
   bloky jednou“ + existující `external-bitcoin` parse/update, nebo aspoň
   stahovat jen ocasní soubory pro `update`; (b) před prořezáním ověřit, že
   objekty v S3 opravdu existují; (c) sjednotit vlastnictví validace voleb do
   jednoho místa.

---

## 1. Co je dobře (a nechal bych to tak)

### coinjoin-pipeline

- **Stage DAG jako single source of truth** — `pipeline/client/stages.py`:
  submit, čekání, rollback i `--dry-run` odvozují z jednoho grafu; `StageKind`
  odděluje název markeru od dispatch tabulky. To je správná abstrakce.
- **Marker protokol** (`docs/pbs-stage-contract.md`) — `.pbs/<stage>.{pbs,jobid,done,failed}`,
  frontend nikdy nemaže vzdálené markery z compute jobu, grace cyklus pro NFS lag,
  liveness probe místo slepého čekání. Dobře promyšlené proti reálným chybám.
- **Fail-closed sémantika** — `run_catalog.report_status` bere jen explicitní
  `"ok"`; labely jsou all-or-nothing; `ensure_staged_exporters` odmítne
  „partial“ prefix místo míchání stromů; verifikace archivu odmítne cokoliv,
  co manifest neprokáže.
- **Provenance** — `research_manifest.json` s redakcí, image ID + digest do
  reportu, `REPRODUCTION_COMMAND`, `tree_sha256` exporterů, `PYTHONDONTWRITEBYTECODE`
  aby bytecode 3.14 nešel do S3.
- **Doctor odvozuje capabilities z akce** (`doctor.required_capabilities`) místo
  env přepínače; komentáře vysvětlují *proč* (např. `runs validate` není
  dockerless). Vzorový kód.
- **Testovací matice** — 16 CI jobů, preflight suite, reap opuštěných Compose
  projektů, e2e k8s→S3→PBS s MinIO. Málokterá diplomka má tohle.
- **Bare wrapper místo wrapper image** — správné rozhodnutí; jedna vrstva
  kontejnerizace pryč, zůstala reprodukovatelnost přes vyrenderovaný env.

### bitcoin-block-archive

- **Prune handshake** (`prune.py`) — `prune=1` + `pruneblockchain` řízené
  archiverem je jediný design, který nemá závod; `min(first_height) - 1` je
  korektní dolní odhad. Design log to poctivě označuje za neověřené vůči Core.
- **Rozdělení producent/konzument** je správně: archiv vlastní pojmenování a
  „kdy se smí mazat“, pipeline vlastní „odkud bloky bere a jak je ověří“.
  Restore v PBS bash skriptu opravdu nemá co dělat v tomhle balíku.
- **Malé moduly s jednou odpovědností**, `Uploader` protokol místo patchování
  subprocessu, atomické markery, `flock` guard, strict mypy. 1 872 řádků včetně
  testů — přiměřené.
- **`archive-manifest.json`** vyřešil otevřené rozhodnutí z design logu správným
  směrem (producent říká, co ví; konzument neodhaduje přes `s5cmd ls`).

---

## 2. Rozhraní pipeline × archiv — nálezy (nejvyšší dopad)

### I-1 · Compose archivu vyrobí XOR-obfuskovaný datadir, který BlockSci nepřečte  **[kritické]**

`bitcoin-block-archive/compose.yaml` startuje `bitcoin/bitcoin:29.1-alpine` s
`-prune=1`, ale **bez `-blocksxor=0`**. Od Bitcoin Core 28.0 jsou `blk*.dat`
při čerstvé inicializaci XORované klíčem z `blocks/xor.dat` (default
`-blocksxor=1`). BlockSci čte surové soubory (`tools/parser/`, žádná zmínka o
XOR — ověřeno grepem v `blocksci/`), a emulátor i pipeline všude explicitně
posílají `--btc-node-arg=-blocksxor=0` (`pipeline/compose.yaml:126`,
`kubernetes.py:389`, `wrapper.py:1160`). Archiv na to zapomněl.

Důsledky:
- Po mnohadenním mainnet IBD by archiver spadl na sanity checku velikosti
  záznamu (`blockfile.py:47`), tedy alespoň hlasitě — ale až po IBD.
- Přepnutí `blocksxor` na existujícím datadiru vyžaduje reindex.
- `blockfile.py` nekontroluje síťová magic bytes (`f9beb4d9`), jen padding;
  a `find_archivable_blocks` by `xor.dat` neuložil, takže by restore v pipeline
  nešel ani teoreticky.

Návrh: (1) `-blocksxor=0` do `compose.yaml` a do README „před prvním startem“;
(2) v archiveru ověřit magic první záznamu proti očekávané síti a odmítnout
běh, pokud existuje `blocks/xor.dat` s nenulovým klíčem; (3) v pipeline
verifikátoru (`templates_s3.py:322`) přidat kontrolu prvních 4 bajtů
`blk00000.dat` vůči `--blocksci-network`, protože „parse proběhl, 0 bloků“ je
přesně ta tichá chyba, kterou design log popisuje.

### I-2 · Z S3 archivu neexistuje inkrementální cesta  **[design, vysoké]**

`blocksci_update_s3_template.sh:54` vyžaduje `"source_kind": "external-bitcoin"`
a `commands.py` pro `--blocksci-task update` vynucuje
`--blocksci-external-bitcoin-datadir` pod `/storage`. Parse z archivu zapisuje
`source_kind: bitcoin-blocks-s3`. Tedy: **každé zvýšení `max_block` = plné
stažení celého archivu (700+ GB) do scratch + plný re-parse (48 h walltime)**.
Přitom design log sám cituje `chain_index.cpp:104` — inkrementální update čte
jen soubory od `newestBlock.nFile`, a manifest archivu už nese
`first_block`/`last_block` výšky per soubor, takže „stáhni jen ocas“ je
spočitatelné bez RPC.

Dvě možnosti, doporučuji (a):

(a) **Restore jako samostatná stage „materializuj bloky do `/storage`“**, potom
existující `external-bitcoin` parse/update beze změny. Restore stahuje jen
soubory, které lokálně chybí nebo mají jiný SHA (sync podle manifestu), ne
celý prefix. Jedna nová stage, nula nových větví v parse/update šablonách,
`source_kind` zůstane `external-bitcoin` a `update` funguje hned. Cena: kvóta
na `/storage` (700 GB+) — nutno ověřit na MetaCentru.

(b) Přidat `bitcoin-blocks-s3` větev do `update` šablony, která podle
zdrojového cache manifestu (`exported_max_block`) a archivního manifestu
(`last_block.height`) stáhne jen soubory s `last_block.height >= zdrojová
výška` (BlockSci hledá od `newestBlock.nFile`; soubor s tímto nFile musí být
přítomen celý). Menší změna, ale třetí kopie verifikační logiky (viz I-3).

### I-3 · Verifikace archivu je Python heredoc v bash šabloně v Python f-stringu  **[udržovatelnost, vysoké]**

`pipeline/client/pbs/templates_s3.py:317-360`: ~40 řádků Pythonu, escapované
`{{}}`, vložené do bash šablony, běžící **na node `python3` mimo Singularity**
(spouští se v `prepare_source`, před `singularity exec`). Používá walrus
(`:=`, ≥3.8) — na Rocky 8 uzlu s default `python3` = 3.6 to je `SyntaxError`
(padne fail-closed, ale nesrozumitelně). Producent (`manifest.py`) a konzument
(heredoc) sdílí schéma bez sdíleného kódu, testovací fixture ani dokumentu
schématu; `test_s3_backend.py:401-406` testuje jen, že *text* šablony obsahuje
`"contiguous from blk00000.dat"`, nikoliv chování.

Návrh: verifikátor jako soubor `pipeline/exporters/verify_block_archive.py`
(exportery se už stagují do run prefixu i do `.pipeline/exporters/`), spouštěný
uvnitř BlockSci image (známý Python). Přímé unit testy na: mezera v číslování,
špatný SHA, špatný sidecar, `archived_max_height < max_block`, chybějící
soubor. Kontraktní test, který vezme *skutečný* výstup `build_manifest()`
z archivu a prožene ho verifikátorem (dnes to dělá jen e2e s jedním souborem).

### I-4 · Před `pruneblockchain` se nikdy neověřuje, že objekty v S3 existují  **[bezpečnost dat, vysoké]**

`already_archived()` = existuje lokální marker. Marker se nikdy neporovná
s bucketem. Lifecycle pravidlo, omylem smazaný objekt, nebo pass, který uploadl
a marker zapsal do jiného `--state-dir`, znamená: soubor je lokálně
„archivovaný“, prořeže se, **a jediná další kopie neexistuje**. Restore to sice
odhalí (fail-closed), ale už není z čeho obnovit.

Návrh: před `prune_archived_blocks()` udělat `s5cmd ls` (nebo `head`) nad
každým souborem, který se má tímto prořezáním stát nedostupným, a porovnat
velikost s markerem; volitelný `--verify-remote` pro periodický audit celého
archivu. Je to jeden `ls` na prefix, ne N requestů.

### I-5 · Drift názvů a konfigurace mezi repozitáři  **[nízké, ale matoucí]**

- Bucket: archiv defaultuje `s3://xman-coinjoin/bitcoin-mainnet/blocks`, každý
  pipeline example používá `coinjoin-thesis`. Design log to má jako „open
  decision“ — pořád otevřené.
- `examples/metacentrum-mainnet-s3-blocks-parse.yaml:25` mluví o
  `bitcoin-core-s3-archive`; v archivu leží stará `bitcoin_core_s3_archive.egg-info`.
- Archivní CI publikuje image do `ghcr.io/ondrejman/bitcoin-block-archive`, ale
  nikdo ji nepinuje — pipeline e2e buildí ze zdrojů. Buď image nepublikovat, nebo
  ji v e2e použít (pin digestem jako u uploaderu).

### I-6 · Sémantika `archived_max_height` není to, co název říká  **[nízké]**

`archive()` předává `archived_max_height=safe_prune_height(config)`: když nic
nečeká, je to `chain_height` (tip!), jinak `min(first_height nearchivovaných) - 1`.
S defaultním `--keep-latest-files 2` tedy hodnota vždy zaostává za tipem
o ~2 soubory, ale se `--keep-latest-files 0` a prázdnou frontou hlásí
manifest výšku tipu, i když poslední soubor byl archivován před chvílí a Core
do něj mezitím mohl dopsat. Pro pruning je to správný dolní odhad; jako
„coverage promise“ pro `--blocksci-max-block` je to lehce optimistické. Stačí
přejmenovat na `prune_safe_height` a coverage počítat z `max(last_block.height)`
archivovaných souborů, který `build_manifest()` už stejně počítá a pak přepíše.

### I-7 · Kontrakt archivu žije jen v próze  **[dokumentace]**

Schéma `archive-manifest.json` (klíče, `schema_version`, pravidla verzování)
není nikde jako dokument — je odvoditelné jen z `manifest.py` a z heredocu.
Dva repozitáře na něm teď závisí; stačí `docs/block-archive-contract.md` v
pipeline (konzument definuje, co vyžaduje) + odkaz z README archivu.

---

## 3. bitcoin-block-archive — další nálezy

| ID | Nález | Dopad |
|---|---|---|
| A-1 | **Nic neběželo proti realitě** (design log „Not verified“ stále platí): žádný reálný `bitcoind`, žádné reálné CESNET S3, prune predikát Core neověřen na regtestu. Pipeline e2e používá `fake-bitcoin-cli`. | vysoký — blokuje mainnet `--prune-after-archive` |
| A-2 | Compose `archiver.command` nemá `--prune-after-archive` ani `--min-free-space`, má `--no-stop-on-error`. S `prune=1` nikdo nikdy neprořeže → disk se plní jako u full nodu. README to zmiňuje, ale „steady state“ nasazení není nikde zapsané jako celek (timer + flagy). | střední |
| A-3 | `archive_block()` uploaduje objekt (128 MB) **před** zjištěním výšek přes RPC; selhání RPC po uploadu = re-upload v dalším passu. Pořadí: hash → first/last hash → výšky → upload → marker. | nízký |
| A-4 | `blockfile.py` nekontroluje síťová magic bytes (viz I-1). | střední |
| A-5 | `Uploader` Protocol definován dvakrát (`s3.py`, `manifest.py`). | kosmetika |
| A-6 | Hygiena repa: `.idea/` je trackované; všech 5 commitů „Update“ (porušuje vlastní pravidlo `type(scope): subject`); `.DS_Store`/`._.DS_Store` netrackované v pracovním stromu; stará `bitcoin_core_s3_archive.egg-info`. | nízký |
| A-7 | `script.py` shim — nikdo ho nevolá (pipeline e2e používá image + console script). Smazat. | kosmetika |
| A-8 | `uv sync` z README nenainstaluje `pytest` (je v `optional-dependencies.test`, ne v `dependency-groups.dev`); `uv run pytest` hned po `uv sync` selže. Přesunout pytest do `dev`. | nízký |
| A-9 | Chybí systemd timer / cron unit (design log krok 5). Bez něj je „intended to run periodically“ jen věta. | nízký |
| A-10 | Compose spouští archiver jako `0:0` kvůli vytvoření `/state` na named volume. Lepší init kontejner nebo `chown` v entrypointu s drop privilegií; root proces s R/W na `archive-state` a čtením celého datadiru není nutný. | nízký |

---

## 4. coinjoin-pipeline — architektura, co pročistit

### P-1 · Tři vrstvy validují totéž  **[vysoké]**

| Vrstva | Soubor | Počet pravidel* |
|---|---|---|
| host CLI (metadata-driven) | `src/coinjoin_pipeline/commands.py` (490 ř.) | 53 |
| wrapper cross-option | `pipeline/client/artifact_validation.py` (218 ř.) + `cli_parser.py` (659 ř.) | 42 |
| PBS renderer | `pipeline/client/pbs/templates_s3.py` | 25 |
| typované YAML | `src/coinjoin_pipeline/configuration.py` (1 040 ř.) | schéma znovu |

\* `errors.append` / `parser.error` / `raise PBSError`.

Konkrétně „choose only one BlockSci source“ je na třech místech
(`commands.py:230`, `artifact_validation.py:68`, `templates_s3.py:265`).
`command_metadata.json` je ve dvou bit-identických kopiích (root + package) a
`command_metadata.py` také dvakrát. `cli_entrypoint._validate_request` navíc
volá host `validate_passthrough` *znovu* uvnitř wrapperu.

Doporučení: jeden vlastník sémantiky. Host `commands.py` ať dělá jen „známá
akce / známý flag / hodnota z choices“ (to metadata umí) a preflight hostu;
sémantická pravidla (exkluzivita zdrojů, S3 povinné flagy, PBS kombinace) jen
ve wrapperu — host je stejně spouští jako subprocess a chybu vrátí. Root kopie
metadata JSON nahradit symlinkem nebo build krokem. `configuration.py` pak jen
mapuje YAML → argv a nic neověřuje.

### P-2 · Refaktor wrapperu skončil v polovině  **[střední]**

`wrapper.py` má 1 806 řádků, 65 `from client.…` re-exportů „pro historickou
kompatibilitu“, a `cli_entrypoint.WrapperOperations` je dataclass se ~30
callables injektovanými z wrapperu. Moduly jsou vytažené, ale wrapper zůstal
bohem přes DI. `docs/core-module-refactor-handoff-2026-08-29.md` to přiznává.
Dokončit: `cli_entrypoint` importuje moduly přímo, monkeypatch body v testech se
přesunou na moduly, `wrapper.py` = 30 řádků `main()`.

### P-3 · PBS skripty = bash šablony přes `str.format`  **[střední]**

11 `*_template.sh` + 785 řádků `templates_s3.py` skládajících bash text,
`{{}}` escapování, vnořené heredocy (viz I-3). Testy ověřují substringy.
Alternativa v duchu toho, co už existuje: exportery se stagují do run prefixu →
přidat tam `pbs_stage.sh` (společné `on_exit`, markery, s5cmd sync/upload,
singularity exec) a šablony zmenšit na „nastav proměnné, source lib, zavolej
stage“. Renderer pak generuje jen hlavičku `#PBS` + proměnné.

### P-4 · Run-directory layout není single-sourced  **[střední, známé]**

`TODO.md` §3.1–3.6 to popisuje přesně; podepisuji pořadí 3.1 → 3.2 → 3.4 →
3.5 → 3.3. Doplňuji: `run_catalog.sha256_file` vs `exporters/common.tree_sha256`
vs archivní `hashing.sha256_file` — tři hashery; `is_run_dir` dvakrát
(`run_catalog.py:47`, `run_context.py:13`).

### P-5 · Dva Python toolchainy v jednom repu, s protichůdnými verzemi  **[střední]**

Root `pyproject.toml`: `requires-python >= 3.10`, jen ruff.
`pipeline/pyproject.toml`: `requires-python >= 3.14,<3.15`, ruff + mypy + pylint,
zákaz `typing.Any`, jiný line-length, jméno `blocksci-emulator-analysis`
(pozůstatek). Bare wrapper ale běží pod `sys.executable` host CLI — tedy pod
tím, co je na MetaCentrum frontendu — takže kód v `pipeline/` *musí* být
≥3.10 kompatibilní a jeho vlastní pyproject lže. (Paměť: „Python 3.14 host
masks container syntax“ je přesně tenhle problém.) Sjednotit do jednoho
`pyproject.toml`, jeden lint config, CI pytest i na 3.10/3.12.

### P-6 · Dokumentace: kontrakty smíchané se session logy  **[střední]**

`docs/` má 4 532 řádků, z toho **2 900 jsou session logy** (`wrapper-removal-*`,
`core-module-refactor-*`, `uncommitted-changes-review-2026-07-26.md`,
`pip-ready-wrapper-removal-plan.md` 1 485 ř.). Trvalé kontrakty jsou tři:
`analysis-semantics.md`, `pbs-stage-contract.md`, plus README (655 ř. = manuál
+ design + ops). Zastaralosti:
- `pbs-stage-contract.md:3` — „implementace žije v `pipeline/client/pbs.py`“ (je to package).
- README ~ř. 470 — „s5cmd … included in the pipeline image used by the Kubernetes uploader“ (pipeline image neexistuje).
- `TODO.md` §2 — fix popisuje `launcher.sh`, který je smazaný; problém sám je ale
  už vyřešen `PIPELINE_RUN_ID` kontraktem → položku uzavřít.
- Root `docs/coinjoin-pipeline-architecture.md` §2 a §9 popisují pre-refactor
  wrapper image (skill to eviduje od 07-26).

Návrh: `docs/history/` pro logy, `docs/block-archive-contract.md` (I-7),
README rozdělit na README (quickstart + odkazy) a `docs/usage.md`.

### P-7 · Trackované a netrackované pozůstatky  **[nízké]**

Trackované: `btc-rpc-explorer.compose.yaml`, `run-btc-rpc-explorer.sh`,
`.btc-rpc-explorer.env`, `import-emulation-blocks.py`, `run-kub-local.sh`
(hardcoded `sudo -E`), `notebooks/.ipynb_checkpoints/`, binární
`tests/pbs-overactive-local-emulation.zip`, root `command_builder.py` /
`command_metadata.py` (duplikáty package modulů),
`scripts/metacentrum_blocksci_mainnet.pbs` + `metacentrum_mainnet_coinjoin_scan.py`
(druhá, ručně psaná mainnet cesta paralelní k `--blocksci-task script`;
odkazuje na ni `thesis/metacentrum_fullchain_runbook.md` — buď z ní udělat
`examples/` pro `script` task, nebo označit legacy).
Netrackované v checkoutu: `build/`, `dist/`, `github-test-results/`,
`coinjoin-runs/`, `pipeline/emulation_logs/`, 429 adresářů v `emulation_logs/`.

### P-8 · Compose: floating tagy s `pull_policy: always`, 40řádkový shell v YAML  **[nízké]**

`docker:29-dind`, `docker:29-cli`, `alpine` s `pull_policy: always` — TODO §1
už jednou kouslo. Pinovat digestem jako `container/*.image`. Příkaz služby
`blocksci` v `pipeline/compose.yaml:181-230` je celý program v YAML stringu —
přesunout do `pipeline/blocksci-stage.sh` (a ideálně sdílet s PBS šablonou,
viz P-3, protože dělají totéž).

### P-9 · Drobnosti

- `cli.py:241` normalizuje exit kód na `{0,2,3,4,5,130}` else `5` — kód 1
  z wrapperu se ztratí jako 5. Buď dokumentovat mapu, nebo propouštět.
- `research_manifest.json` má dvě nekompatibilní schémata (TODO §3.5) —
  potvrzeno.
- Git: `fullS3` je 4 ahead `origin/fullS3` a de facto hlavní větev; `main` je
  „Wasabi+pipeline line“ (paměť). Commity „Update“ a „chore(scheduled-audit):
  pre-run snapshot – <300 znaků>“ porušují vlastní pravidlo z `~/.claude/CLAUDE.md`.
  Zvážit merge `fullS3 → main` před odevzdáním, ať thesis odkazuje na `main`.

---

## 5. Co bych udělal jinak (design, ne úklid)

1. **Restore = materializace do `/storage`, ne součást parse jobu.** Viz I-2(a).
   Parse/update šablony pak nemají třetí větev, `update` funguje z archivu
   zdarma, a verifikace se dělá jednou, ne při každém parse. Pokud kvóta
   `/storage` nedovolí, I-2(b).
2. **Ověřit vzdálené objekty před prořezáním** (I-4). U nástroje, jehož
   jediný účel je „smaž lokální kopii, protože existuje vzdálená“, je to
   jádro kontraktu, ne nice-to-have.
3. **Verifikátor jako sdílený, testovatelný Python** (I-3), ne heredoc.
4. **Jeden vlastník validace voleb** (P-1). Dnes každé nové pravidlo znamená
   3–4 místa + 2 kopie JSON + kontraktní test, který hlídá, že kopie souhlasí —
   test hlídá symptom.
5. **Jeden pyproject, jeden interpreter kontrakt** (P-5).
6. Zvážit, jestli vůbec archivovat *bloky* místo *BlockSci indexu*: pipeline už
   má „reusable parse cache“ (`blocksci-parse_data/blocksci_data.tar.gz` +
   manifest) a `update` z něj. Kdyby archiver (nebo cron u nodu) periodicky
   pouštěl `blocksci_parser update` lokálně a nahrával cache, PBS job by
   nikdy nepotřeboval 2 TB scratch ani 700 GB download. Cena: BlockSci image
   u nodu a vazba na verzi BlockSci (manifest to už řeší přes `blocksci_image`).
   Není to nutně lepší — ale pro diplomku je to menší kus infrastruktury než
   dva synchronizované archivy.

---

## 6. Doporučené pořadí

1. I-1 (`-blocksxor=0` + magic check) — pět řádků, brání promarněnému IBD.
2. A-1 smoke run proti reálnému `bitcoind` (regtest stačí pro prune predikát)
   a reálnému CESNET S3 s malým prefixem.
3. I-4 remote verify před prune.
4. I-3 verifikátor jako modul + kontraktní test producent→konzument.
5. I-2 rozhodnout (a)/(b) podle kvóty `/storage`; implementovat.
6. P-1 → P-2 → P-3 konsolidace kontrolní vrstvy (každý samostatný PR, suite
   mezi nimi).
7. P-6/P-7/A-6 úklid docs a pozůstatků; I-5 sjednotit bucket a názvy.
8. TODO §3 (run layout) — podle pořadí v TODO.
