# Implementační log — odstranění wrapper image

Pracovní deník k `docs/pip-ready-wrapper-removal-plan.md`. Je psaný tak,
aby se implementace dala přerušit a v další session navázat bez čtení
celé historie: každá fáze má stav, co je hotové, čím se ověřila, a co
zbývá.

**Pravidlo pro dlouhé testy:** v session se pouští jen rychlé věci
(`pytest tests/unit`, `--dry-run`, cílené shell asserty). Dlouhé
integrační běhy (`full-run --driver docker`, k3d, MinIO) se **nespouští
automaticky** — jsou vypsané v „Fronta dlouhých testů" a pouští je
uživatel.

**Zákaz commitů:** nic se necommituje ani nepushuje (viz `CLAUDE.md`).

---

## Stav fází

| Fáze | Popis | Stav |
|---|---|---|
| 0 | Audit `launcher.sh` řádek po řádku | **hotovo** |
| 1 | Hard gates (Python 3.8 exportéry, unified-report image kandidát) | čeká (dlouhé) |
| 2 | Checkout runtime (`runtime_root`, `runtime_command`, env kontrakt, research, signály, capability preflight) | **hotovo** (kromě dlouhých gate testů) |
| 3 | S3 staging (sdílená funkce, entrypoint kontroly, uploader image) | **hotovo** |
| 4 | Odstranění wrapperu (launchery, `Dockerfile`, `pipeline_image.py`, `PBS_FRONTEND_DIRECT`) | **hotovo** |
| 5 | Provenance a úklid (manifest, dokumentace, akceptační grep) | **hotovo** (architektura doc je mimo scope, viz níže) |

---

## Fáze 0 — audit `launcher.sh` (332 řádků)

Povinný krok z plánu. Každá proměnná / side effect zařazená do jedné ze
dvou kategorií. `resources/container/launcher.sh`, čísla řádků k HEAD
v době auditu.

### A) Mechanika kontejneru — zaniká se souborem

| Řádky | Co | Poznámka |
|---|---|---|
| 5 | `SCRIPT_DIR` | adresář packaged resources; nahrazuje `runtime_root()` |
| 7 | `WRAPPER_IMAGE` | ruší se |
| 31-40 | `image_available` / `require_image` | přebírá `doctor.py` (image components) |
| 42-59 | `resolve_podman_socket`, `setup_socket`, `INNER_CONTAINER_RUNTIME`, `CONTAINER_EXTRA_ARGS`, `DOCKER_HOST` | bare wrapper má socket přímo |
| 61-84 | `wrapper_pull_args`, `WRAPPER_PULL_POLICY` | týká se jen wrapper image |
| 112-117 | `docker run … -m client.research` | nahrazuje přímý `-m client.research` |
| 128-144 | parsing `--driver/--namespace/--kubeconfig/--pbs-bitcoin-datadir` | launcher je *konzumoval* a pak re-appendoval jako `WRAPPER_EXTRA_ARGS`; nově jde passthrough beze změny → celá logika odpadá |
| 176-206 | `docker cp` extrakce PBS runtime z image, `.pbs-wrapper-runtime`, `.wrapper-exporters` | odpadá, checkout je zdroj |
| 235 | `require_image "${WRAPPER_IMAGE}"` | ruší se |
| 261, 272-275, 282 | bind mounty kubeconfigu / copy-to-host / PBS datadiru do wrapper kontejneru | bez kontejneru netřeba |
| 265 | `--add-host host.docker.internal:host-gateway` | viz otevřená otázka §5 plánu |
| 286-299 | `cleanup()`, `handle_interrupt`, `trap INT TERM` | přebírá signal handler ve `wrapper.py` |
| 300 (část) | `POST_WRAPPER_SHELL` | zaniká bez náhrady (jediný efekt byl `exec /bin/bash` uvnitř kontejneru, ř. 323) |
| 309-323 | celý `docker run`, `WRAPPER_SCRIPT`, `EXPORTERS_FROM_IMAGE` | ruší se |
| 331-332 | normalizace exit kódu | **duplicitní** — `cli.py:220` totéž už dělá nezávisle |

### B) Default nebo side effect — MUSÍ převzít env kontrakt / CLI

| Řádky | Co | Kam to jde | Stav |
|---|---|---|---|
| 10 | `NOTEBOOKS_DIR=${EMULATION_LOGS_DIR}/.notebooks` | `commands.runtime_environment()` | ✅ |
| 300 | `BLOCKSCI_LAUNCH_JUPYTER` (efektivně `0`) | `runtime_environment()`, `os.environ.get(...) or "0"` | ✅ |
| 320 | `PYTHONDONTWRITEBYTECODE=1` | `runtime_environment()` | ✅ |
| 311 | `SCENARIOS_DIR` | `runtime_environment()` = `<checkout>/scenarios` | ✅ |
| 91-92, 238, 240 | `mkdir -p` pro logs/notebooks | `cli.prepare_runtime_directories()` | ✅ |
| 114, 117 | research: `--runs-root`, `--runtime` | `commands.research_command()`; **pozor: musí být PŘED subpříkazem** | ✅ |
| 209-218 | absolutizace `--blocksci-script` | netřeba — `wrapper.py:2999` sám dělá `.expanduser().resolve()` a bare wrapper dědí cwd uživatele | ✅ |
| 219-223 | dostupnost kubeconfigu + `kubectl` | `Capability.KUBECTL`; kontrola souboru zůstala ve `validate_arguments` | ✅ |
| 224-234 | PBS bitcoin datadir `regtest/blocks` | `doctor.validate_arguments()` | ✅ |
| 236 | `echo "[doctor] OK: …"` | zaniká — ověřeno `grep -rn` v `tests/`, `run-all.sh`, `runIt.sh`: nikdo na něj negrepuje | ✅ |
| 241, 260, 271, 279 | absolutizace cest | `cli.py` už `runs_root` resolvuje; zbytek řeší wrapper | ✅ |
| 247-251 | `COINJOIN_EMULATOR_DOCKER_PLATFORM` (joinmarket/arm64) | `runtime_environment()` | ✅ |
| 256-263 | `KUBE_CFG` default | **carry netřeba** — wrapper sám fallbackuje na `~/.kube/config` (ř. 951-953, 2312, 2660) | ✅ |
| 269-271 | `KUBERNETES_COPY_TO_HOST_DIR` default + `mkdir` | `runtime_command()` + `prepare_runtime_directories()` | ✅ |

### C) Ověřeno, že převzetí NENÍ potřeba

| Co | Proč |
|---|---|
| `KUBERNETES_STORAGE_UID/GID` (ř. 267) | `wrapper.py:917-918` defaultuje na `os.getuid()/os.getgid()`; bare wrapper běží jako uživatel, takže default je rovnou správný |
| `SCENARIOS_ROOT=/app/scenarios` (ř. 114) pro research | `scenarios.py:9-10` defaultuje na `parents[2]/"scenarios"` = `<checkout>/scenarios`, což je správně |
| `REPRODUCTION_COMMAND` (ř. 301-307) | už ho staví `commands.py` |
| `CONTAINER_RUNTIME` (ř. 6), `COINJOIN_EMULATOR_IMAGE` (ř. 8), `EMULATION_LOGS_DIR` (ř. 9) | `cli.py`/`images.py` je resolvují a předávají |
| normalizace exit kódu (ř. 331-332) | `cli.py:220` `return exit_code if exit_code in {0,2,3,4,5,130} else 5` |

---

## Fronta dlouhých testů (pouští uživatel)

Nic z toho se v session nespouští automaticky.

- [ ] `pytest tests/pipeline` (delší než `tests/unit`)
- [ ] Python 3.8 exportéry proti `blocksci-complete` (3× `docker run`)
- [ ] unified-report image smoke (Docker + Singularity, přes `bash -c`)
- [x] SIGINT/SIGTERM gate — `tests/test-wrapper-signal-cleanup.sh`, **PASS** (0,8 s, bez kontejnerů)
- [ ] `tests/test-kubernetes-k3d.sh` — režim s bare commandem, bez `--add-host`
- [ ] `tests/test-runIt-overactive-local.sh` (po přepsání — 3 důvody rozbití)
- [ ] `tests/test-podman-no-host-docker.sh`
- [ ] `tests/test-local-pbs-analysis.sh`
- [ ] `tests/test-kubernetes-s3-minio.sh` (povinná finální brána)

---

## Deník rozhodnutí (rozpory plán vs. kód)

Sem přibývá každý případ, kdy plán neseděl na kód a zvolilo se nejmenší
řešení.

| # | Rozpor | Zvolené řešení |
|---|---|---|
| 1 | Plán mluví o `Capability`/`required_capabilities` jako o novém modulu, ale `doctor.py` má dnes `check()` + `validate_arguments()` se dvěma různými podmínkami | zavést `required_capabilities()` v `doctor.py` vedle stávajících funkcí, nerozbíjet jejich signatury |


---

## Fáze 2 — co je hotové (session 1)

Kód:

- `commands.py`: `launcher_command` → **`runtime_command`** (bare
  `sys.executable pipeline/client/wrapper.py …`), **`research_command`**
  (`-m client.research`, globální flagy před subpříkazem),
  `runtime_environment()` jako jediné místo env kontraktu, `prepend_path()`.
- `cli.py`: `direct_wrapper_root()` → **`runtime_root()`** (jedna větev, tvrdá
  chyba místo `None`), zrušen `as_file(launcher.sh)`, zrušena tvrdá
  `PBS_FRONTEND_DIRECT` validace S3 full-runu, přidán
  `prepare_runtime_directories()`, `RESEARCH_ACTIONS` routing, `usage()` bez
  `--pipeline-image`.
- `doctor.py`: **`Capability` + `required_capabilities(action, arguments)`**,
  `check(..., capabilities=…)`; zrušena `PBS_FRONTEND_DIRECT` podmínka u `qsub`;
  přidána validace PBS bitcoin datadiru.
- `host.py`, `images.py`, `configuration.py`: komponenta **`pipeline` úplně
  odstraněna** (`Images.pipeline`, `--pipeline-image`, `WRAPPER_IMAGE` override,
  YAML klíč `images.pipeline`).
- `wrapper.py`: **`install_termination_handlers()`** + `cleanup_peer_containers()`
  (idempotentní, SIGINT i SIGTERM, uklidí peer kontejnery i lock), volané na
  začátku `main()`.
- `pipeline/analysis.sh`, `pipeline/emulate.sh`: `SCENARIOS_DIR`/`NOTEBOOKS_DIR`
  přepnuté na `${VAR:-…}`, takže env kontrakt z CLI je autoritativní.
- `tests/unit/test_cli.py`, `tests/unit/test_configuration.py`: přizpůsobené.

Ověřeno (rychlé):

- `pytest tests/unit` → **82 passed**
- `ruff check src/coinjoin_pipeline pipeline/client/wrapper.py tests/unit` → clean
- `full-run --dry-run` z **jiného pracovního adresáře** → absolutní cesty,
  `PIPELINE_RUN_ID`, `BLOCKSCI_LAUNCH_JUPYTER=0`, `PYTHONDONTWRITEBYTECODE=1`,
  `SCENARIOS_DIR=<checkout>/scenarios`, `sys.executable` z `.venv`
- `runs list` a `scenarios list` přes `-m client.research` → funkční,
  `scenarios list` vidí `overactive-local.json` (tj. správný scénářový strom)
- signal handlery se registrují (`SIGINT`/`SIGTERM` → `handle_termination`)

### Co zbývá ve fázi 2

- ⬜ **dlouhé gate testy před cutoverem** — viz fronta; `cli.py` už je
  přepnuté, takže gaty se musí spustit teď (SIGINT/SIGTERM, k3d bez
  `--add-host`)

## Další fáze — vstupní body

- **Fáze 3 (S3 staging)**: `wrapper.py:run_full_run_s3` (~ř. 2680) a větev
  `emulate` (~ř. 3100) → společná `stage_kubernetes_s3_run()`;
  `kubernetes.py:267` `cp -R /app/exporters` smazat; `prefix_preflight`
  (~ř. 234-260) výjimka + pozitivní `s5 ls` na dva entrypointy;
  `container/uploader.Dockerfile` + `container/uploader.image`.
  **Pozor:** `kubernetes.py:302` `s5 sync` **nesmí** dostat `--delete`.
- **Fáze 4 (mazání)**: `Dockerfile`, `src/coinjoin_pipeline/resources/container/launcher.sh`,
  `container/launcher.sh`, `container/entrypoint.sh`, `pipeline_image.py`,
  `run-pipeline-image.sh`, `tests/test-run-pipeline-image.sh`, console-script
  `coinjoin-pipeline-image`, publish workflow, `_runtime` mappings v
  `pyproject.toml`, `run-all.sh` (21× `WRAPPER_IMAGE`).
  `tests/test-runIt-overactive-local.sh` má **3** důvody rozbití
  (`WRAPPER_PULL_POLICY`, `cp container/launcher.sh` ř. 326,
  grep `-e PYTHONDONTWRITEBYTECODE=1` ř. 170).
- **Fáze 5**: `manifest.py` git commit/dirty + verze nástrojů, dokumentace,
  akceptační grepy (včetně `POST_WRAPPER_SHELL`).

---

## Fáze 3 — co je hotové (session 1)

- `artifacts.py`: `REQUIRED_EXPORTERS`, **`ensure_local_exporters()`**,
  **`upload_exporters()`** (`s5cmd sync --exclude '*__pycache__*' --exclude '*.pyc'`
  + pozitivní ověření obou entrypointů na S3 po uploadu).
- `wrapper.py`: **`stage_kubernetes_s3_run(args, access)`** —
  `s3_access_preflight` → `kubernetes_s3_auth_preflight` →
  `ensure_empty_run_prefix` → upload exportérů. Volá ji `run_full_run_s3`
  **i** samostatná `emulate` S3 větev (dřív neměla žádný preflight).
  Pořadí: cluster auth **před** zápisem na S3.
- `kubernetes.py`: smazán `cp -R /app/exporters …`; `prefix_preflight`
  ignoruje `.pipeline/exporters/` a navíc **pozitivně ověří** oba entrypointy.
- Nové images: `BTC_VOLUME_HELPER_IMAGE = "alpine:3.20"` (interní konstanta),
  `resolve_uploader_image()`, `resolve_unified_report_pbs_image()` s resolverem
  flag → env → lock soubor; `read_image_lock()` čte z `container/`.
- Nové soubory: `container/uploader.Dockerfile`, `container/uploader.image`,
  `container/unified-report.image`.
- Nové CLI flagy `--uploader-image`/`--unified-report-image` v
  **`add_artifact_arguments`** (ne v `add_pbs_arguments` — potřebuje je i
  `emulate --artifact-backend s3`).

Ověřeno: `pytest` (unit+pipeline) → **367 passed**, `ruff` clean.

### ⚠️ Otevřené po fázi 3

- ⬜ **Lock soubory mají zatím plovoucí tag, ne `@sha256:`.**
  `container/uploader.image` = `ghcr.io/ondrejman/coinjoin-pipeline-uploader:latest`,
  `container/unified-report.image` = `python:3.12-slim-bookworm`.
  Digesty jde doplnit až po prvním build+push uploaderu, resp. po
  `docker pull && docker inspect` python image — vyžaduje přístup k registry,
  který v session není. **Proto zatím NEPŘIDÁVAT CI regex na `@sha256:`**,
  jinak spadne. Postup je v plánu, sekce 3.
- ⬜ `s5cmd sync --exclude` nebylo možné ověřit — `s5cmd` na tomhle stroji není.
  Primární obrana proti bytecode je `PYTHONDONTWRITEBYTECODE=1` (fáze 2),
  `--exclude` je druhá vrstva.

---

## Fáze 4 + 5 — co je hotové (session 1)

### Smazáno

`Dockerfile`, `pipeline/client/Dockerfile` (druhý wrapper image, nikdo ho
nepoužíval), `pipeline/client/compose.yaml`, `pipeline/client/develop.sh`,
`container/launcher.sh`, `container/entrypoint.sh`,
`src/coinjoin_pipeline/resources/container/launcher.sh`,
`src/coinjoin_pipeline/pipeline_image.py`, `run-pipeline-image.sh`,
`tests/test-run-pipeline-image.sh`, `tests/unit/test_publish_workflow.py`,
`.github/workflows/publish-pipeline-image.yaml`, `src/coinjoin_pipeline/_runtime/`,
console-script `coinjoin-pipeline-image`, publish job v `tests.yaml`,
`_runtime*` package mappings + `resources.container` package-data v
`pyproject.toml`.

`run-all.sh`: **38 → 0** výskytů `WRAPPER_IMAGE` (build krok, `LOCAL_/UPSTREAM_`
proměnné, propagace do testů). `tests.yaml`: odstraněny `docker build` kroky
i `WRAPPER_IMAGE`/`WRAPPER_PULL_POLICY` env.

### Provenance (sekce 4 plánu)

`images.wrapper`/`image_digests.wrapper` → **`images.uploader`** +
**`images.unified_report`** napříč `manifest.py`, `report_builder.py`,
`markdown_report.py`, `integration_diagnostics.py`, `exporters/cli.py`,
`blocksci/analysis.py`, `compose.yaml`. Nové flagy
`--uploader-image`/`--unified-report-image`; digest se odvozuje textově přes
**`common.digest_from_reference()`** (bez Docker daemonu — kvůli MetaCentru).
`IMAGE_PROVENANCE_ENV` už `WRAPPER_IMAGE` nemapuje.

Ověřeno: `pytest` **362 passed**, `unittest discover -s tests` **41 OK**,
`ruff` clean, `full-run --dry-run` renderuje bare command.

### Akceptační grep — stav

`WRAPPER_PULL_POLICY`, `PBS_FRONTEND_WRAPPER_ROOT`, `POST_WRAPPER_SHELL`,
`/app/exporters`, `launcher_command`, `EXPORTERS_FROM_IMAGE` → **0** mimo
docs/MIGRATION. Zbytek jen v shell testech + README (viz níže).

---

## ZBÝVÁ (další session)

1. **Shell testy — největší zbývající blok.** Asertují na vyrenderovaný
   `docker run` wrapperu, který už neexistuje:
   - `tests/test-runIt-overactive-local.sh` (430 ř., 38 grep asertů) — **3
     důvody rozbití**: `WRAPPER_PULL_POLICY` (ř. 38-98),
     `grep -e PYTHONDONTWRITEBYTECODE=1` (ř. 170 — teď je to env proměnná, ne
     `-e` argument), `cp container/launcher.sh` (ř. 326). Doporučení: přepsat,
     ne opravovat po částech.
   - `tests/test-podman-no-host-docker.sh` (232 ř., 19 asertů) — `WRAPPER_IMAGE`
     override (ř. 50, 167, 181); socket forwarding už neexistuje.
   - `tests/test-runIt-doctor.sh` (ř. 45).
   - `tests/test-kubernetes-*.sh`, `tests/test-*-pbs-analysis.sh` — smazat
     `export PBS_FRONTEND_DIRECT=1`.
2. **README.md** — zmiňuje `coinjoin-pipeline-image` (ř. 82); přepsat na
   editable install + bare běh. `MIGRATION.md` se záměrně nechává.
3. **`docs/coinjoin-pipeline-architecture.md` §2/§9** — dvouvrstvá architektura
   bez kontejnerového obalu.
4. **Lock soubory na `@sha256:`** (viz otevřené po fázi 3) + teprve pak CI regex.
5. **`.github/workflows/publish-uploader-image.yaml`** — ruční workflow pro
   uploader image (build → push → digest → commit).
6. **Fáze 1 hard gates + všechny dlouhé testy** — pouští uživatel na konci.

---

## Session 2 — shell testy, run-all.sh, README

### Přepsané dry testy (rychlé, ověřené)

| Test | Doba | Co teď ověřuje |
|---|---|---|
| `tests/test-runIt-overactive-local.sh` | ~1,2 s | celý env kontrakt bare commandu, kořenové `scenarios/`, `BLOCKSCI_LAUNCH_JUPYTER=0`, `PYTHONDONTWRITEBYTECODE=1`, PBS datadir validaci, copy-to-host default, absenci `PBS_FRONTEND_DIRECT` |
| `tests/test-podman-no-host-docker.sh` | ~0,4 s | podman cesta nikdy nesáhne na host docker; žádný socket mount ani `DOCKER_HOST` |
| `tests/test-runIt-doctor.sh` | ~1,2 s | doctor gating; wrapper image se už nepreflightuje |

**Zásadní zjištění:** starý `test-runIt-overactive-local.sh` wrapper vůbec
nespouštěl — fake `docker` ho spolkl. Teď bare wrapper reálně běží, takže
stage dry-runy můžou skončit nenulově z důvodů nesouvisejících s renderem;
`render()` proto status zachytává místo `set -e`. Kubeconfig gate naopak
musí zůstat **bez** `--dry-run`, protože je v `validate_arguments` pod
`not dry_run`.

### Upravené dlouhé testy (needitovány naslepo, ale NESPUŠTĚNY)

- `test-kubernetes-k3d.sh`, `test-kubernetes-pbs-analysis.sh`,
  `test-parallel-pbs-analysis.sh`, `test-local-pbs-analysis.sh` — pryč
  `WRAPPER_IMAGE`, jeho pull a `PBS_FRONTEND_DIRECT`.
- `test-runIt-{overactive-local,joinmarket,parallel}-local-docker.sh` — pryč
  build wrapper image z už neexistujícího `Dockerfile` (16/20/16 → 0 odkazů).
- **`test-kubernetes-s3-minio.sh`** — největší změna: místo wrapper image
  se staví **uploader image** z `container/uploader.Dockerfile`, importuje
  se do k3d, `s5cmd` se extrahuje z něj a běh dostane `--uploader-image`.
  Volitelný `UPLOADER_IMAGE=` přeskočí build.
- `run-all.sh` — `POST_WRAPPER_SHELL=0` řádky pryč.

### README

Instalace popsaná jako editable checkout, `--pipeline-image` pryč ze
seznamu override, `container/` popsán jako uploader build + lock soubory,
**14× `PBS_FRONTEND_DIRECT=1` odstraněno** ze všech MetaCentrum příkladů.

### Akceptační grep — finální stav

Všechny vzory čisté mimo `MIGRATION.md`/`docs/`. Jediné zbývající výskyty
`WRAPPER_IMAGE`/`POST_WRAPPER_SHELL`/`EXPORTERS_FROM_IMAGE` jsou
**negativní aserty** v `tests/test-runIt-overactive-local.sh`, které
ověřují, že se ty řetězce nikde neobjeví — to je žádoucí.

### Stav ověření

`pytest` 362 · `unittest discover -s tests` 41 · `ruff` clean · 3 dry shell
testy PASS.

## ZBÝVÁ

1. `docs/coinjoin-pipeline-architecture.md` §2/§9 přepsat.
2. Lock soubory na `@sha256:` + teprve pak CI regex (potřebuje registry).
3. `.github/workflows/publish-uploader-image.yaml`.
4. **Dlouhé testy — pouští uživatel na konci** (viz fronta výše).

---

## Session 3 — metadata, publish workflow, scope

### `command_metadata.json` byl stale — contract test to chytil

Nové wrapper flagy se musí promítnout do metadat, jinak
`tests/test-command-builder-contract.sh` selže:

```
ERROR: command metadata snapshot is stale
  - added option: {emulate,full-run,pbs-from-s3} --uploader-image
  - added option: {emulate,full-run,pbs-from-s3} --unified-report-image
```

Regenerováno `python3 scripts/generate-command-metadata.py --wrapper-root ./pipeline`
(+72 řádků). Test teď PASS. **Pravidlo pro budoucí flagy:** každý nový
passthrough flag vyžaduje regeneraci snapshotu ve stejném commitu.

### `.github/workflows/publish-uploader-image.yaml`

Nový workflow, **jen `workflow_dispatch`** (ne při každém pushi do main jako
zrušený `publish-pipeline-image.yaml`) — uploader je immutable nástrojový
artefakt, mění se s `kubectl`/`s5cmd`, ne s aplikační verzí. Buildí
`container/uploader.Dockerfile` pro amd64+arm64 a **vypíše plnou
`@sha256:` referenci do job summary**; aktualizace
`container/uploader.image` je záměrně ruční navazující commit.

### ⚠️ Mimo scope: `docs/coinjoin-pipeline-architecture.md`

Plán (sekce 8) chce přepsat §2/§9 tohohle dokumentu. Soubor ale **není
v `coinjoin-pipeline`** — leží v meta-repu na
`/home/administrator/diplomka/docs/coinjoin-pipeline-architecture.md`.
Tvrdé omezení zadání zní „pracuj pouze v `coinjoin-pipeline`", takže se
**neupravoval**. Až se k němu někdo dostane, potřebuje: dvouvrstvá
host/wrapper architektura zůstává, ale bez kontejnerového obalu kolem
wrapperu a bez `PBS_FRONTEND_DIRECT` jako volitelného módu.

### Stav ověření (rychlé)

`pytest` **362** · `unittest discover -s tests` **41** · `ruff` clean ·
4 dry shell testy PASS (`overactive-local`, `podman-no-host-docker`,
`runIt-doctor`, `command-builder-contract`).

## ZBÝVÁ

1. **Lock soubory na `@sha256:`** — `container/uploader.image` a
   `container/unified-report.image` mají zatím plovoucí tag. Postup:
   spustit `publish-uploader-image.yaml`, vzít digest ze summary, commitnout;
   pro python image `docker pull python:3.12-slim-bookworm && docker inspect`.
   **Teprve pak** přidat CI regex na `@sha256:[0-9a-f]{64}` — dřív by spadl.
2. **Dlouhé testy** — pouští uživatel na konci (fronta výše).
3. `docs/coinjoin-pipeline-architecture.md` — mimo scope tohohle zadání.

---

## Session 4 — signálový hard gate doplněn

**Mezera, kterou jsem sám způsobil:** přepsáním `test-runIt-overactive-local.sh`
na dry test zmizel signálový test (ověřoval launcherův `trap`) a náhrada
chyběla — přitom plán ho označuje za hard gate.

Nový **`tests/test-wrapper-signal-cleanup.sh`**:

- staví příkaz **z `cli.py:runtime_root()` + `commands.runtime_command()`**,
  tedy z téže funkce jako produkční cesta (požadavek plánu: netestovat přes
  `cjp` se stubnutým launcherem);
- pokrývá **SIGINT i SIGTERM** — SIGTERM neprochází `atexit`, takže dřív
  zůstával viset i lock soubor;
- ověřuje exit **130** a zastavení všech **šesti** peer kontejnerů;
- běží **0,8 s** bez jediného reálného kontejneru.

Dva detaily, které bylo nutné trefit, jinak test buď nespustí wrapper, nebo
se zasekne:

1. `rendered()` vrací `VAR=v … argv`; `exec VAR=v cmd` je neplatné —
   musí být `exec env VAR=v cmd`, což zároveň drží wrapper jako
   signálovaný proces (ne potomka bashe).
2. Fake `docker stop` musí **skutečně ukončit** blokující `compose` stub.
   Reálný `docker stop` kontejnery zastaví, čímž se compose odblokuje; bez
   toho wrapper čeká na potomka, který nikdy neskončí, a cleanup nejde
   pozorovat.

**Výsledek: signal handler z fáze 2 prokazatelně funguje.** Byla to jediná
neověřená kritická položka fáze 2.

### Rychlá sada (celkem ~12 s)

`pytest` 362 · `unittest` 41 · `ruff` clean · **6** shell testů PASS
(`overactive-local`, `podman-no-host-docker`, `runIt-doctor`,
`command-builder-contract`, `wrapper-signal-cleanup`, `run-all-local-build`).

`test-run-all-local-build.sh` (0,1 s) potvrzuje, že z `run-all.sh` zmizel
build wrapper image, zatímco ostatní buildy zůstaly.

### Závěrečný sweep

Žádné odkazy na smazané soubory (`Dockerfile`, `run-pipeline-image.sh`,
`pipeline_image`, `container/launcher.sh`, `container/entrypoint.sh`,
`develop.sh`, `_runtime`) — zbylé shody jsou cizí Dockerfily blocksci a
substring `runtime`. Všechny moduly `coinjoin_pipeline.*` jdou importovat.

### Ověřeno před dlouhými testy

Uploader image z GHCR **nikde nechybí**: jediný test s `--artifact-backend s3`
je `test-kubernetes-s3-minio.sh` a ten si uploader staví lokálně z
`container/uploader.Dockerfile` a předává `--uploader-image`. Publikovat
uploader před dlouhými testy tedy **není potřeba**.
