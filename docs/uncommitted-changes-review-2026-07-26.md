# Review necommitnutých změn (2026-07-26)

Průchod celým `git diff HEAD` v `coinjoin-pipeline` (69 souborů, +2326/−1793):
odstranění wrapper image, přechod na bare wrapper z checkoutu, nový uploader
image, přejmenování `exporters/blocksci` → `blocksci_export`. Navazuje na
`wrapper-removal-self-review.md` (body H1–L3, vyřešeno) — tenhle dokument
hledá **nové** problémy, které tam nejsou.

Pozn.: během review běžela paralelně druhá session, která do stromu doplnila
rename `blocksci_export` a `assert_real_blocksci`. Ten je v diffu zahrnutý.

Stav testů při psaní: `pytest` 373 · `unittest` 229 · `ruff` clean ·
`mypy` 11 pre-existing chyb · rychlé shell testy (`overactive-local`,
`doctor`, `podman-no-host-docker`, `wrapper-signal-cleanup`,
`command-builder-contract`, 4× `tests/pipeline/*.sh`) PASS.

---

## H1 — Read-only akce `runs`/`scenarios` teď na frontendu vyžadují Docker

**Kde:** `src/coinjoin_pipeline/doctor.py:required_capabilities`.

`required_capabilities` přidá `CONTAINER_RUNTIME` vždy, když není
`delegated` (`uses_s3` nebo PBS mimo full-run/emulate). Pro
`runs list`, `runs inspect`, `scenarios list` je `delegated = False`, takže
preflight spustí `shutil.which("docker")` + `docker info`.

Ověřeno:

```python
required_capabilities("runs list", ["runs", "list"])   # {CONTAINER_RUNTIME}
check("docker", ..., capabilities=caps)                # ['docker command not found']
```

Přitom `research_command` spustí jen `python -m client.research`, který
žádný kontejner nepoužívá. Dřív to fungovalo, protože `PBS_FRONTEND_DIRECT=1`
+ prázdná sada images přeskočila **celý** `doctor_check`; ten přepínač byl
odstraněn a kapacitní model náhradu nedostal.

**Dopad:** na MetaCentrum frontendu (bez Dockeru) nejde vypsat ani
prohlédnout běhy — přesně na stroji, kde má S3 workflow žít.
`download-report` je náhodou v pořádku (routuje se před preflightem),
`runs`/`scenarios` ne.

**Oprava:** v `required_capabilities` nepřidávat `CONTAINER_RUNTIME` pro akce
začínající `runs `/`scenarios ` (stejná podmínka, jakou už má
`required_image_components`).

---

## H2 — `populate_btc_data_volume` teď tahá `alpine:3.20` z Docker Hubu

**Kde:** `pipeline/client/wrapper.py`, `BTC_VOLUME_HELPER_IMAGE`.

Původně:

```python
helper_image = os.environ.get("WRAPPER_IMAGE", "ghcr.io/ondrejman/coinjoin-pipeline:latest")
```

Nově `BTC_VOLUME_HELPER_IMAGE = "alpine:3.20"`. Tři důsledky:

1. **Nová síťová závislost.** Wrapper image byl na hostu už stažený (běžel
   z něj); `alpine:3.20` se musí poprvé stáhnout, a to z Docker Hubu
   (anonymní rate limit), ne z GHCR jako všechno ostatní.
2. **Plovoucí tag** — v přímém rozporu s docstringem
   `add_effective_image_arguments` („Prevent wrapper defaults from silently
   reintroducing mutable latest tags“) a s tím, proč vznikly
   `container/*.image` lock soubory.
3. **Není v preflightu.** `required_image_components` o tomhle image neví,
   takže nedostupnost se projeví až pádem uprostřed běhu.

Navíc komentář nad `populate_btc_data_volume` pořád tvrdí opak toho, co se
děje: „Reuse the wrapper image for the copy helper instead of pulling an
unpinned `alpine`“.

**Oprava:** buď třetí lock soubor (`container/btc-helper.image`) se stejným
resolve řetězcem jako uploader, nebo použít už stažený emulátor image;
minimálně opravit komentář.

---

## M1 — PBS unified-report image je `python:3.12-slim-bookworm` bez lokální náhrady

**Kde:** `container/unified-report.image`, `resolve_unified_report_pbs_image`,
`tests/test-kubernetes-s3-minio.sh`.

Report job dělá `singularity exec docker://python:3.12-slim-bookworm`. Ostatní
PBS images mají v S3 e2e testu offline cestu
(`PBS_BLOCKSCI_LOCAL_IMAGE`, `PBS_COINJOIN_ANALYSIS_LOCAL_IMAGE` →
`export_pbs_docker_archive`), report **nemá** — a to i když CLI flag
`--unified-report-image` existuje. Test čeká na `.pbs/unified-report.done`,
takže stage z Docker Hubu musí projít uvnitř PBS kontejneru.

Ověřeno jako neškodné z jiné strany: exportéry používají **jen stdlib**
(+ volitelně `blocksci`), takže holý `python:3.12-slim` k sestavení reportu
opravdu stačí. Problém je dostupnost image, ne jeho obsah.

**Doporučení:** doplnit `PBS_UNIFIED_REPORT_LOCAL_IMAGE` do testu stejným
mechanismem jako u ostatních dvou a lock soubor pinnout digestem (viz M1 ve
`wrapper-removal-self-review.md` — dokud jsou tagy plovoucí, provenance je
`null` a `digest_from_reference` nemá co číst).

---

## M2 — `test-podman-no-host-docker.sh` už netestuje to, kvůli čemu existuje

**Kde:** `tests/test-podman-no-host-docker.sh` (−179 řádků).

Obě sekce teď běží s `--dry-run` a **`|| true`**, výstup se jen grepuje:

```bash
) >"${RENDERED}" 2>&1 || true
```

Následky:

- `PODMAN_LOG` se už nekontroluje vůbec. Jediné, co dnes podman skutečně
  zavolá, je `podman info` z preflightu; žádné tvrzení o tom není.
- „Docker se nepoužil“ je v dry-runu splněné triviálně, protože se nespustí
  vůbec nic.
- `|| true` schová i nenulový exit: test projde, i kdyby `runIt.sh` spadl na
  preflightu, protože „Generated runtime command“ se tiskne **před** ním.

Není to regrese chování produktu, je to regrese důkazu — a tenhle test je
citovaný jako deployability evidence.

**Oprava:** zahodit `|| true` (nebo aspoň asertovat očekávaný exit kód) a
přidat zpět tvrzení nad `PODMAN_LOG` (`^info` a nepřítomnost `docker`).

---

## ~~M3~~ — `read_image_lock` na S3 full-run cestě (neplatné)

Tvrdil jsem, že `run_full_run_s3` nechytá `RuntimeError` z `read_image_lock`.
**Není to pravda:** volání je ve `wrapper.py:3333` obalené
`except (PBSError, RuntimeError)`, a protože `ArtifactTransportError` i
`PBSError` z `RuntimeError` dědí, chytí se tam i selhání stagingu. Uživatel
dostane `[ERROR] …` a exit 2, ne traceback. Bez opravy.

---

## M4 — Provenance nových images je zapojená jen napůl

1. `pipeline/exporters/cli.py:main` předává do `build_report`
   `uploader_image=` / `unified_report_image=`, ale **ne**
   `uploader_image_digest=` / `unified_report_image_digest=` — na rozdíl od
   staršího `wrapper_image_digest=`. Funguje to jen díky fallbacku uvnitř
   `build_run_manifest`; dvě volací místa v jednom souboru přitom počítají
   digest různě (`digest_from_reference(...)` vs. nic).
2. `integration_diagnostics.IMAGE_COMPONENTS` z původní čtveřice `wrapper`
   vypustil a `uploader`/`unified_report` **nepřidal**. Image diagnostika
   tedy pokrývá 3 z 5 sledovaných images. Možná záměr (na frontendu bez
   Dockeru je nelze inspectovat), ale nikde to není řečeno.

---

## L1 — Rozbitá nápověda `run-all.sh`

Hromadné odstranění `WRAPPER_IMAGE` proběhlo bez kontroly textu:

```
  BLOCKSCI_IMAGE,            Selected image refs for either mode.
  LOCAL_BLOCKSCI_IMAGE, LOCAL_BLOCKSCI_IMAGE,
  UPSTREAM_BLOCKSCI_IMAGE, UPSTREAM_BLOCKSCI_IMAGE,
```

Duplicitní názvy a rozbité odsazení v `--help`; navíc po vymazaném build
kroku zůstala prázdná díra ve výpisu build sekce.

---

## L2 — Bare wrapper dědí celé prostředí hostitele

`process.run` dělá `env = os.environ.copy(); env.update(environment)`.
Wrapper v kontejneru dostával jen to, co mu launcher explicitně předal
(`-e`); teď vidí **všechno**, co má uživatel ve shellu — `BLOCKSCI_*`,
`COINJOIN_*`, `KUBERNETES_CONTROL_IP`, `PBS_*`, `AWS_*`.

Frontend s5cmd je proti tomuhle ošetřený (`scrubbed_s3_environment()`,
ověřeno), takže o únik kredencí nejde. Jde o reprodukovatelnost:
`generated_runtime_command` v manifestu obsahuje jen kurátorovanou sadu, ale
běh mohly ovlivnit i zděděné proměnné, o kterých manifest mlčí.

Souvisí: `runtime_environment` naopak `EXPORTERS_DIR`/`SCENARIOS_DIR`/
`NOTEBOOKS_DIR` přepisuje bez ohledu na to, co si uživatel exportoval
(u `BLOCKSCI_LAUNCH_JUPYTER` se uživatelská hodnota respektuje — nekonzistence
v rámci jedné funkce).

---

## L3 — `upload_exporters` spoléhá na `--exclude` bez kontroly verze s5cmd

```python
run_s5cmd(access, "sync", "--exclude", "*__pycache__*", "--exclude", "*.pyc", ...)
```

`s3_access_preflight` ověřuje jen přítomnost binárky (`shutil.which`), ne
verzi. Uploader image má s5cmd 2.3.0 přišpendlený, frontend má, co má.
Starší s5cmd bez podpory `--exclude` shodí staging až v okamžiku uploadu.

---

## L4 — Zastaralá dokumentace mimo diff

- `docs/runner-docker-image-store.md` pořád radí
  `docker pull ghcr.io/ondrejman/coinjoin-pipeline:latest` — ten image se už
  nepublikuje (workflow smazaný).
- `thesis/metacentrum_fullchain_runbook.md:184` pořád používá
  `PBS_FRONTEND_DIRECT=1` (neškodné, ale matoucí — proměnná nikde nic nedělá).

---

## L5 — Prefix bez exportérů u `pbs-from-s3 --blocksci-task update` (pre-existing)

`run_pbs_from_s3` v update režimu volá `ensure_empty_run_prefix` nad **novým**
run-id, ale exportéry do něj nikdo nestageuje (`stage_kubernetes_s3_run` běží
jen ve full-run/emulate S3). Job `blocksci-analyze` přitom stahuje
`$ARTIFACT_URI/$RUN_ID/.pipeline/exporters/*`.

**Není to regrese** — dřív je tam taky dostal jen uploader container při
emulaci. Ale posunem stagingu na frontend to začíná být opravitelné jedním
voláním `upload_exporters` a stojí za ověření při prvním mainnet běhu.

---

## Co je naopak ověřeno v pořádku

- Rename `exporters/blocksci` → `blocksci_export` je konzistentní ve všech
  konzumentech: `pbs.py` (2×), `kubernetes.py` preflight, `artifacts.py:REQUIRED_EXPORTERS`,
  testy. Žádný zbylý odkaz na starou cestu.
- Exportéry importují **jen stdlib** (+ volitelně `blocksci`), takže
  `python:3.12-slim-bookworm` pro report job stačí — vypadlá vrstva závislostí
  z wrapper image nechybí.
- Všechny `.py` v `pipeline/` a `src/` se parsují pod syntaxí 3.10
  (`ast.parse(..., feature_version=(3,10))`), takže `requires-python = ">=3.10"`
  a spouštění pod `sys.executable` si neodporují.
- Kubernetes API reachability check ze smazaného `launcher.sh:222` nezmizel —
  ekvivalent běží dál ve `wrapper.py` přes `kubernetes_auth_preflight`
  (`kubernetes.py:67`) a `kubernetes_s3_auth_preflight`.
- Frontend s5cmd volání scrubují `AWS_*`/`AWS_PROFILE`/`AWS_REGION` stejně
  jako in-pod varianta.
- `command_metadata.json` odpovídá živým parserům včetně nových
  `--uploader-image` / `--unified-report-image`
  (`tests/test-command-builder-contract.sh` PASS).
- Uploader image nese s5cmd v `/usr/local/bin`, odkud si ho S3 e2e test
  kopíruje (`docker create` + `docker cp`) — cesta sedí.
- Pořadí ve `stage_kubernetes_s3_run` je správné: `ensure_empty_run_prefix`
  **před** `upload_exporters`, takže staging si sám nezpůsobí „prefix already
  contains artifacts“.

---

## Opraveno ještě během tohoto review

`required_image_components`: po opravě H2 z předchozího review vracela pro
stage akce s PBS flagem neúplnou, ale neprázdnou množinu
(`analyze --blocksciPbs` → `{coinjoin_analysis}`,
`mappings --mappingsPbs` → `{emulator, coinjoin_analysis, blocksci}`), přestože
takový běh lokálně nespustí nic. Prakticky se to neprojevilo (`check()`
image loop přeskočí, když se nechce runtime), ale model si odporoval s
`doctor.delegated`. Nově: stage akce s PBS flagem → `set()`, full-run/emulate
→ odečte se jen delegovaná komponenta. Pokryto testem
`test_pbs_flags_only_drop_the_stage_they_delegate`.

---

## Doporučené pořadí

1. **H1** — blokuje běžné použití na frontendu, oprava je jedna podmínka.
2. **M2** — vrátit důkazní hodnotu podman testu (`|| true` + `PODMAN_LOG`).
3. **H2** — rozhodnout, čím seedovat btc-data volume, a opravit komentář.
4. **M1** — offline override pro report image v S3 testu (nutné před e2e).
5. **M3/M4/L1** — drobnosti, jedno sezení.
6. **L2–L5** — poznámky k doplnění při dalším průchodu / prvním mainnet běhu.

---

## Stav oprav (2026-07-26, tentýž den)

| bod | stav | co se změnilo |
|---|---|---|
| H1 | opraveno | `doctor.py`: `runs `/`scenarios ` akce jsou `delegated`, nechtějí runtime |
| H2 | opraveno | `wrapper.py`: helper pro btc-data volume je emulátor image, ne `alpine:3.20` |
| M1 | opraveno | `PBS_UNIFIED_REPORT_LOCAL_IMAGE` v S3 e2e testu + oprava schématu (viz N1) |
| M2 | opraveno | podman test: `run_pipeline()` kontroluje exit kód, vrácené assertce nad `PODMAN_LOG` |
| M3 | neplatné | `run_full_run_s3` už `RuntimeError` chytá (`wrapper.py:3333`) |
| M4 | opraveno | `cli.py` předává oba digesty explicitně; `IMAGE_COMPONENTS` má odůvodnění |
| L1 | opraveno | `run-all.sh` nápověda + prázdná díra po build kroku; totéž v `tests.yaml` |
| L2 | opraveno | `commands.py`: zděděné pipeline proměnné se renderují (bez secretů) |
| L3 | opraveno | `artifacts.py`: `require_s5cmd_version()` (minimum 2.1 kvůli `sync --exclude`) |
| L4 | opraveno | `runner-docker-image-store.md`, `metacentrum_fullchain_runbook.md` |
| L5 | opraveno | `ensure_staged_exporters()` doplní exportéry do prefixu, který je nemá |

**H1** — `delegated` nově zahrnuje `action.startswith(("runs ", "scenarios "))`.
Test `test_research_actions_do_not_need_a_container_runtime`.

**H2** — `populate_btc_data_volume` bere
`COINJOIN_EMULATOR_IMAGE` (fallback `DEFAULT_EMULATOR_IMAGE`). Ten je v téhle
větvi vždy lokálně přítomný (běží z něj manager Kubernetes emulace), je krytý
preflightem i `--version`/`--emulator-image`, takže nepřibývá žádný nepinnutý
pull. `BTC_VOLUME_HELPER_IMAGE` zmizel i s nepravdivým komentářem.

**L2** — `runtime_environment` teď do renderované sady přidá zděděné proměnné s
prefixy `BLOCKSCI_`, `COINJOIN_`, `KUBERNETES_`, `MAPPINGS_`, `SAKE_`, `PBS_`.
Jména obsahující `SECRET`/`TOKEN`/`PASSWORD`/`ACCESS_KEY`/`CREDENTIAL` se
vynechávají, aby se do stdoutu a manifestu nedostala hodnota kredencí; spočítané
hodnoty (images, cesty) zděděné přebijí. Chování běhu se nemění — `process.run`
stejně mergoval do `os.environ` —, mění se jen to, co je vidět a zaznamenané.
Testy `test_environment_variables_that_steer_a_run_are_rendered` a
`test_computed_images_beat_inherited_values`; podman test navíc tvrdí, že
`KUBERNETES_CONTROL_IP` v renderovaném příkazu opravdu je.

**L5** — `run_pbs_from_s3` volá `ensure_staged_exporters()`, která uploaduje
exportéry **jen když v prefixu nejsou** (`unified_report.py` jako sonda). Prefix,
který už exportéry má z dřívější etapy, si je nechá — nová verze z checkoutu by
jinak tiše přepsala to, s čím běžely předchozí joby. Runbook
(`metacentrum_fullchain_runbook.md`) přesně tenhle `--blocksci-task update` do
čerstvého run-id popisuje, takže šlo o reálnou díru, ne teoretickou.

### N1 — nález při opravě M1: `docker-archive:` reference se komolila

`resolve_unified_report_pbs_image` rozhodovala podle `"://" in image`. Offline
předání lokálního image do Apptaineru má ale tvar
`docker-archive:/cesta/report.tar` — bez `//`, takže by dostalo prefix
`docker://docker-archive:/cesta/report.tar` a job by spadl. Nově se testuje
seznam skutečných schémat (`with_singularity_scheme`), takže `python:3.12-slim`
prefix dostane a `docker-archive:`/`oras://`/… ne. Parametrizovaný test v
`test_s3_backend.py`.

Testy po opravách: `pytest` 384 · `unittest` 229 · `ruff` clean · `mypy`
beze změny (11 pre-existing) · `overactive-local`, `doctor`,
`podman-no-host-docker`, `wrapper-signal-cleanup`, `command-builder-contract`
a všechny 4 `tests/pipeline/*.sh` PASS. Dlouhé k3d/S3/PBS testy nespuštěny.

---

## Druhý průchod (2026-07-26) — nálezy v dosud nepročtených částech

První průchod nepokryl `command_metadata.json`, ostatní shell testy, k8s Job
rendering a provenance řetězec end-to-end. Tady je, co v nich je.

### N2 (H) — `images.uploader` a `images.unified_report` nebyly plněné nikdy

Obě pole nahradila `images.wrapper`, ale hodnotu do nich nikdo nedodával:
`COINJOIN_UPLOADER_IMAGE` a `COINJOIN_UNIFIED_REPORT_IMAGE` se v celém stromu
jen **čtou** (compose.yaml passthrough, `export_command`, exportéry,
`resolve_*`), nikde nenastavují. `resolve_uploader_image()` vyřeší referenci pro
Kubernetes Job a zahodí ji. Důsledek: v každém reportu `images.uploader` i
`images.unified_report` = `null`, a to i když uživatel předá `--uploader-image`.
Náhrada za `images.wrapper` tedy nefungovala vůbec — to je víc než M1 z prvního
průchodu, který řešil jen chybějící digesty.

**Opraveno:** S3 report job je jediný kanál, kterým se ta informace k reportu
dostane, takže `blocksci_export_pbs_command()` umí `--uploader-image` a
`--unified-report-image` (validované přes `require_safe_image`) a
`run_pbs_from_s3` je předává. Lokální běh je nechává `null` právem — žádný
uploader tam neběží. Kvůli tomu vznikla i `unified_report_image_reference()`,
aby provenance dostala neutrální referenci a Singularity zvlášť tu se schématem.

### N3 (M) — Frontend nepozná pod, který se vůbec nerozběhl

`kubernetes_job_probe` četla jen `status.conditions` Jobu. Pod v
`Init:ImagePullBackOff` ale Job drží „active“ donekonečna, takže
`wait_for_s3_marker` čekalo až do `--emulation-timeout` (v e2e testu 85 minut)
bez jediné hlášky. In-pod watchdog tuhle situaci hlídá **jen pro controller** —
pro image, ve kterém běží on sám, ji z principu hlídat nemůže.

**Opraveno:** probe se dívá i na `initContainerStatuses`/`containerStatuses`
a na `ErrImagePull`, `ImagePullBackOff`, `InvalidImageName`,
`CreateContainerConfigError` reaguje `PROBE_TERMINAL` + hláškou, která
container jmenuje. Testy pro init i běžný container.

### N4 (M) — Uploader image se publikuje ručně a lock míří na `:latest`

`container/uploader.image` = `ghcr.io/ondrejman/coinjoin-pipeline-uploader:latest`,
publikuje ho jen `workflow_dispatch` workflow, který ještě nikdy neběžel
(anonymní `docker manifest inspect` vrací `denied`, takže existenci nelze
potvrdit ani vyvrátit). Job navíc nemá `imagePullPolicy`, a pro `:latest`
Kubernetes defaultuje na `Always`. První S3 běh tedy skončí na
ImagePullBackOff, dokud image nevznikne.

**Řešeno částečně:** N3 z toho dělá rychlé selhání místo hodinového čekání a
README teď explicitně říká „publikovat uploader **před** prvním S3 během a
digest dopsat do lock souboru“. Samotné publikování je na uživateli.

### N5 (L) — `tests/support/pbs-runtime/` je mrtvý kód, který už driftuje

Adresář obsahuje kopii `client/pbs.py` a tří templates, ale **nic ho
nereferencuje** (žádný `.sh`, `.py`, `.yaml`). Kopie zná ještě starou signaturu
`blocksci_export_pbs_command` a cestu `exporters/blocksci/analysis.py`, takže
každý, kdo do ní příště sáhne, bude ladit fikci. Doporučení: smazat (nechal
jsem být — je mimo tenhle diff).

### N6 (L) — Zbytek po přesunutém stagingu exportérů

Uploader skript pořád zakládal `/artifacts/$RUN_ID/.pipeline`, ačkoli
`cp -R /app/exporters` do něj zmizel; prázdný adresář se do S3 stejně
nesyncuje. **Opraveno** (odstraněno z `mkdir -p`).

### N7 (L) — Publikační workflow ztratil test

`tests/unit/test_publish_workflow.py` byl smazán spolu s
`publish-pipeline-image.yaml`, ale nový uploader workflow žádný nedostal.
**Opraveno:** test je zpět a hlídá to, co je u ručně publikovaného tool image
podstatné — jen `workflow_dispatch`, build z `container/uploader.Dockerfile`,
obě platformy, vypsaný digest pro lock soubor, a že v `tests.yaml` po starém
publish kroku ani po `WRAPPER_IMAGE` nezbyla stopa.

### Souběžná session

Během tohoto průchodu do stromu přišly z druhé session dvě věci, které se s mými
opravami potkaly ve stejných souborech a jsou **lepší** než to, co jsem měl:

- `DOCKERLESS_RESEARCH_ACTIONS` (whitelist) místo mého
  `action.startswith(("runs ", "scenarios "))` u H1. Podstatný rozdíl:
  `runs validate` v seznamu **není**, protože spouští BlockSci image
  (`research.py:validate_existing_run`) — moje varianta by mu preflight vypnula.
  `required_image_components` mu zároveň dává `{"blocksci"}`.
- `staged_exporters_state()` místo mé jednoduché sondy na `unified_report.py`:
  rozlišuje complete/partial/missing a **odmítne** prefix stagovaný před
  přejmenováním `blocksci_export` místo aby do něj domíchal novou verzi.
  Moje testy jsem přepsal na tohle API a doplnil případ „partial“.

Testy po druhém průchodu: `pytest` 392 · `unittest` 229 · `ruff` clean ·
shell testy (`podman-no-host-docker`, `overactive-local`, `doctor`,
`wrapper-signal-cleanup`) PASS.

---

## Třetí průchod — ověření celku (2026-07-26)

Tenhle průchod nečetl diff, ale **spouštěl** ho: dry-run každé S3 cesty proti
dočasnému runs-rootu. Obojí níž našel běh, ne čtení.

### N8 (H) — `emulate --artifact-backend s3` spadl na AttributeError

`stage_kubernetes_s3_run()`, kterou nově volá i standalone emulate větev, čte
`args.s3_credentials_file` a `args.s3_profile`. Emulate parser je ale
**neměl** — `add_artifact_arguments(emulate_parser, kubernetes_secret=True)`,
`pbs_credentials` zůstalo `False`, protože do téhle chvíle frontend v emulate
větvi na S3 nesahal (kredence má pod ze Secretu).

Dry-run to neodhalí (`if not args.dry_run`), contract test taky ne (kontroluje
paritu parser ↔ metadata, ne existenci atributu), takže by to spadlo až v
ostrém běhu, po vytvoření namespace, tracebackem.

**Opraveno:** emulate dostal `pbs_credentials=True`, oba flagy jsou v S3 režimu
povinné (`validate_artifact_arguments`), snapshoty `command_metadata.json`
přegenerované, regresní test kontroluje atributy i povinnost. Jeden starší test
musel dostat kredence do argv, jinak ho zastavila nová kontrola dřív, než došel
ke své vlastní.

### N9 (M) — Runs root z éry wrapper image je pro bare wrapper nezapisovatelný

První ostrý pokus o běh proti výchozímu `./coinjoin-runs` skončil:

```
PermissionError: [Errno 13] Permission denied: '.../coinjoin-runs/.pipeline.lock'
```

`.pipeline.lock`, `.notebooks`, `.pending` i staré run adresáře vlastní **root**
— wrapper je vytvořil, když běžel jako root ve svém kontejneru. Bare wrapper
běží pod uživatelem, takže je nedokáže otevřít. Netýká se to čerstvého
runs-rootu, ale **každého, kdo pipeline používal před tímhle refaktorem** — a
projeví se to hned první akcí, tracebackem z `acquire_lock`.

**Opraveno dvakrát:** `acquire_lock` `PermissionError` převádí na hlášku s
konkrétním `chown` příkazem, a `doctor.check` na to upozorní **v preflightu**
(`inherited_root_ownership_errors`), tedy dřív než se cokoli spustí. Ověřeno
proti skutečnému `coinjoin-runs` na tomhle stroji.

### Co dry-runy potvrdily jako v pořádku

- `full-run --artifact-backend s3 --dry-run` (exit 0): vyrenderuje Job,
  tři PBS skripty a čeká na správné markery; uploader image je v Jobu 2×
  (init + uploader), report job jede na `docker://python:3.12-slim-bookworm`
  a report command nese `--uploader-image` i `--unified-report-image` (N2).
- `pbs-from-s3 --dry-run` (exit 0): tři PBS skripty, provenance flagy taky.
- `emulate --artifact-backend s3 --dry-run` (exit 0) po opravě N8.
- `runs list` bez Dockeru (H1 z prvního průchodu) a všech 9 rychlých shell testů.

Celkový stav: `pytest` 397 · `unittest` 229 + 46 · `ruff` clean · `mypy` 11
pre-existing · 9 rychlých shell testů PASS · všechny změněné `.sh` projdou
`bash -n`, oba workflow YAML a oba metadata JSON se parsují · všech 21
změněných Python modulů jde naimportovat. Dlouhé k3d/S3/PBS testy pořád
nespuštěné.
