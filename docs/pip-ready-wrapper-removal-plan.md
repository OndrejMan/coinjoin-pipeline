# Odstranění wrapper image — checkout jako zdroj pravdy

## Kontext

`coinjoin-pipeline` dnes defaultně spouští wrapper
(`pipeline/client/wrapper.py`) přes `docker/podman run` postavený z
`Dockerfile` (`ENTRYPOINT ["python3", "/app/wrapper.py"]`), který do sebe
balí celý zdrojový strom wrapperu a exportéry. Jediná výjimka je
`PBS_FRONTEND_DIRECT=1` (MetaCentrum frontend nemá Docker), kde
`cli.py:direct_wrapper_root()` už dnes umí najít wrapper přímo v
checkoutu (`Path(__file__).resolve().parents[2] / "pipeline" /
"client/wrapper.py"`) — ale i v tomhle režimu dnes hostitelské CLI pořád
renderuje volání přes `resources/container/launcher.sh` (jen mu navíc
předá `PBS_FRONTEND_WRAPPER_ROOT`, aby launcher interně přeskočil
extrakci z image a spustil `python3` napřímo).

**Cíl beze změny:** wrapper image se přestává buildovat a publikovat,
`PBS_FRONTEND_DIRECT` mizí jako flag — bare spuštění se stává jediným,
nepodmíněným režimem.

**Zásadní revize oproti dřívějším verzím tohohle plánu:** předchozí
iterace se snažily zároveň udělat z `coinjoin-pipeline` samostatně
distribuovatelný wheel (instalovatelný na jiném stroji bez checkoutu) —
to postupně vyžadovalo: přesun `pipeline/` pod `src/coinjoin_pipeline/`,
konverzi všech importů na relativní, tři nové runtime images
(`report-runtime`, `blocksci-runtime`, `uploader`), řešení Python
3.8-vs-3.10 nekompatibility s `blocksci-complete` (včetně zavržené verze,
která navrhovala přestavět BlockSci bindings — to by navíc porušilo
"Scope repozitářů" níže), rozdělení na dvě Python distribuce, a vlastní
release/provenance mašinérii. Tenhle rozsah je zbytečný, pokud
distribuovatelnost mimo checkout **není** skutečný cíl.

**Rozhodnutí: checkout je záměrně a natrvalo zdrojem pravdy.** Instalace
přes `pip`/`pipx` zůstává, ale jako **editable install**
(`pip install -e .` / `pipx install --editable .`) svázaná s konkrétním
checkoutem — přesně to, jak se `coinjoin-pipeline` dnes reálně nasazuje i
na MetaCentru (`git clone`/`git pull` do `/storage/...`, viz existující
runbook). Není to kompromis kvůli lenosti — je to nejjednodušší a
nejméně rizikové řešení pro nástroj s jedním uživatelem, který stejně
vždycky pracuje z checkoutu. Cena: nejde vytvořit samostatný wheel
instalovatelný jinam bez zdrojového repozitáře. To se netýká hlavního
požadovaného use-case a je to výslovně přijatelný trade-off.

## Scope repozitářů — tvrdé omezení

Implementace se smí pohybovat výhradně v repozitáři `coinjoin-pipeline`.
Žádný jiný nested repo z meta-repa (`blocksci`, `coinjoin-emulator`,
`coinjoin-analysis`, `coinjoin-mappings`, `OverleafThesis`, `thesis`,
`materials`) se v rámci téhle práce neprochází, neupravuje ani se do něj
necommituje. S revidovaným přístupem výše je tohle teď triviálně
splněné — nic v plánu se `blocksci` repa ani nedotýká.

## Co se NEMĚNÍ (explicitně, aby nedošlo k regresi rozsahu)

- **Žádný přesun `pipeline/` pod `src/coinjoin_pipeline/`.** Zůstává
  přesně tam, kde je. Relativní odvozování cest uvnitř `emulate.sh`/
  `analysis.sh` (`HOST_CLIENT_DIR` podle polohy skriptu,
  `SCENARIOS_DIR`/`EXPORTERS_DIR`/`NOTEBOOKS_DIR` odvozené odtud) dál
  funguje beze změny — dnešní "fallback pro ruční spuštění z checkoutu"
  se stává jediným a jedině používaným režimem, ne něčím, co je potřeba
  opravovat.
- **Žádná konverze importů.** `from client.X import`/`from exporters.X
  import` + ruční `sys.path.insert`/`.append` zůstávají — fungují, dokud
  se wrapper spouští jako skript z checkoutu (`python3
  pipeline/client/wrapper.py`), což je přesně to, co se bude dít pořád.
- **Žádné velké nové images.** `blocksci-complete`, `coinjoin-emulator`,
  `coinjoin-analysis`, `coinjoin-mappings-*` beze změny. Žádný
  `report-runtime`/`blocksci-runtime` obsahující wheel. **Výjimka, ověřená
  a popsaná v sekci 3 níže:** `WRAPPER_IMAGE` má ve `wrapper.py` tři
  odvozené konzumenty mimo spouštění wrapperu samotného (K8s S3 uploader,
  PBS unified-report job, `populate_btc_data_volume` helper) — ty se bez
  náhrady rozbijou, pokud image přestane existovat. Náhrada je jeden malý
  nový image (`uploader`, bez wheelu, bez exportérů) + dva pinned
  veřejné images (žádný vlastní build) pro PBS report krok a pro
  `populate_btc_data_volume`.
- **Transport exportérů zůstává bind-mount/S3, ale K8s→S3 upload krok se
  přesouvá** — viz sekce 3. Lokální peer-kontejnery (`pipeline/compose.yaml`)
  a `.pipeline/exporters` na S3 pro PBS BlockSci joby beze změny.
- **Žádný vlastní PEP 517 build backend, žádný release marker, žádný
  wheel-verze-vs-image-tag resolver.** Neřeší se, protože nevzniká žádný
  nový image, jehož verzi by bylo potřeba párovat s `coinjoin-pipeline`
  balíčkem. **To neznamená, že nový `uploader` image nemá žádný
  lifecycle** — má vlastní, nezávislý na aplikační verzi: je to
  spravovaný immutable nástrojový artefakt, jehož efektivní identita je
  plný digest uložený v `container/uploader.image` (viz sekce 3).

## Co se skutečně mění

### 1. Hostitelské CLI vždy spouští wrapper přímo z checkoutu, bez shell launcheru

Dnešní tok: `cli.py` → renderuje volání `resources/container/launcher.sh`
→ ten (pokud `PBS_FRONTEND_DIRECT=1` a `PBS_FRONTEND_WRAPPER_ROOT`
nastavené) interně spustí `python3 "${DIRECT_WRAPPER_SCRIPT}"`, jinak
udělá `docker run ... "${WRAPPER_IMAGE}"`.

Nový tok: `cli.py` volá `direct_wrapper_root()`, přejmenovanou na
**`runtime_root()`** — už to není PBS výjimka, ale standardní, jediná
cesta — **vždy**, bez podmínky na env var, a přímo (v Pythonu, ne přes
shell skript) sestaví a spustí subprocess s explicitně nastaveným
prostředím, ne se spoléháním na `wrapper.py`'s vlastní `sys.path` hack:
```python
RuntimeCommand(
    executable=sys.executable,
    arguments=(str(runtime_root / "client" / "wrapper.py"), *passthrough),
    environment={
        **environment,
        "PYTHONPATH": prepend_path(runtime_root, environment.get("PYTHONPATH")),
        "HOST_CLIENT_DIR": str(runtime_root / "client"),
        "EXPORTERS_DIR": str(runtime_root / "exporters"),
        "EMULATION_LOGS_DIR": str(runs_root),
        # runtime_root == <checkout>/pipeline, scénáře jsou o úroveň výš
        "SCENARIOS_DIR": str(runtime_root.parent / "scenarios"),
        "NOTEBOOKS_DIR": str(runs_root / ".notebooks"),
        # dnešní efektivní default z launcher.sh:300 — bez něj compose
        # spadne na `true` a spustí interaktivní prostředí
        "BLOCKSCI_LAUNCH_JUPYTER": environment.get("BLOCKSCI_LAUNCH_JUPYTER", "0"),
    },
)
```
Explicitní `PYTHONPATH` (místo spoléhání jen na wrapper.py's vlastní
`sys.path.insert(0, ...)` při `__package__ in (None, "")`) dělá kontrakt
mezi hostitelským CLI a wrapperem výslovný, ne implicitní — nezávisí na
tom, že introspekce `__file__`/`__package__` uvnitř wrapper.py zafunguje
správně ve všech způsobech invokace. Wrapper.py's vlastní hack zůstává
jako neškodná redundance (žádná konverze importů, viz "Co se NEMĚNÍ"),
ale hostitelské CLI už na něm není závislé.

**`sys.executable`, ne natvrdo `"python3"`.** Instalace je editable
(`pip install -e .`/`pipx install --editable .`) — hostitelské CLI běží v
konkrétním Python prostředí (virtuální env, pipx-izolovaný env, nebo
systémový interpret). Natvrdo `python3` by mohlo spustit wrapper pod
úplně jiným interpretem/prostředím, než ve kterém běží `cjp` samotné, a
wrapperu by chyběly závislosti dostupné jen v tom správném prostředí.
`sys.executable` garantuje shodu. Zbytek subprocess `environment`
(`PIPELINE_RUN_ID`, `REPRODUCTION_COMMAND`, `CONTAINER_RUNTIME`, a
existující `*_IMAGE` proměnné pro emulator/blocksci/coinjoin-analysis/
mappings peer kontejnery — ty zůstávají, mění se jen to, že
`WRAPPER_IMAGE` mezi nimi už není). Subprocess model (ne in-process
import) se zachovává kvůli normalizovaným exit codes, `PIPELINE_RUN_ID`,
host manifestu, dry-run a SIGINT/SIGTERM handlingu.

**`SCENARIOS_DIR`/`NOTEBOOKS_DIR` — `analysis.sh`/`emulate.sh` dnes hodnotu
z prostředí zahazují, a bez opravy se tiše změní mountovaný scénářový
strom.** Tohle je konkrétní regrese, ne kosmetika. `pipeline/analysis.sh:8-9`
a `pipeline/emulate.sh:15` dělají **nepodmíněný** `export
SCENARIOS_DIR="${HOST_CLIENT_DIR}/scenarios"` (resp. `NOTEBOOKS_DIR=
"${HOST_CLIENT_DIR}/notebooks"`) — bez `${VAR:-...}` idiomu, který o dva
řádky níž používají `EMULATION_LOGS_DIR` i `EXPORTERS_DIR`
(`analysis.sh:11-12`). Cokoli hostitelské CLI v těchhle dvou proměnných
pošle, se přepíše dřív, než to uvidí `compose.yaml` (`:94` mountuje
`${SCENARIOS_DIR}` do emulátoru, `:119` `${NOTEBOOKS_DIR}` do BlockSci,
`:122` `${SCENARIOS_DIR}` do BlockSci).

Dnes to nevadí, protože `launcher.sh:311` posílá `HOST_CLIENT_DIR=${SCRIPT_DIR}`
= adresář *packaged resources*, jehož `scenarios/` je ten pravý
(`resources/scenarios/` = `defaultJoinMarket.json`, `overactive-k8s-small.json`,
`overactive-local.json`, shodné s kořenovým `scenarios/`). Po přechodu na
bare wrapper ale `HOST_CLIENT_DIR` = `<checkout>/pipeline/client`, jehož
`scenarios/` je **jiný, zastaralý pár** (`defaultCoinJoin.json`,
`defaultJoinMarket.json`) — chybí v něm `overactive-local.json`, což je
default `SCENARIO_PATH` v `emulate.sh:8`, i `overactive-k8s-small.json`.
Odvození z `HOST_CLIENT_DIR` by tedy default lokálního běhu rozbilo.
Stejně tak `<checkout>/pipeline/client/notebooks` je prázdný, zatímco
kořenový `notebooks/` obsahuje `Analysis.ipynb`.

Oprava (obojí ve stejném kroku, jinak nemá smysl):
1. `analysis.sh`/`emulate.sh` přepnout obě proměnné na `${VAR:-<dnešní
   odvození>}`, tedy stejný idiom jako sousední `EMULATION_LOGS_DIR`/
   `EXPORTERS_DIR`. Tím se env kontrakt z hostitelského CLI stává
   skutečně autoritativním — což je celý smysl explicitního prostředí výše.
2. CLI posílá `SCENARIOS_DIR` = `<checkout>/scenarios` (kořenový adresář,
   ne `pipeline/client/scenarios`) a `NOTEBOOKS_DIR` = `<runs_root>/.notebooks`
   (záměr `launcher.sh:10`, drží notebooky u evidence dat a nezapisuje do
   checkoutu).

Zastaralý `pipeline/client/scenarios/` a prázdný `pipeline/client/notebooks/`
se v rámci tohohle plánu **nemažou** (mimo rozsah), ale nesmí se na ně
nikdo spolehnout — akceptační test níže ověřuje, že mountovaný scénářový
strom obsahuje `overactive-local.json`.

**`BLOCKSCI_LAUNCH_JUPYTER` — bez převzetí defaultu se každý běh na konci
zasekne v interaktivním prostředí.** Druhý případ téhož vzorce jako výše,
a s horším dopadem. `launcher.sh:300` počítá default, který **nikde jinde
v repozitáři neexistuje**:
```bash
POST_WRAPPER_SHELL="${POST_WRAPPER_SHELL:-0}"
BLOCKSCI_LAUNCH_JUPYTER="${BLOCKSCI_LAUNCH_JUPYTER:-${POST_WRAPPER_SHELL}}"
```
a posílá ho do prostředí wrapperu (`launcher.sh:320`). Konzument je
`compose.yaml:147` (`${BLOCKSCI_LAUNCH_JUPYTER:-true}`) a větev na
`compose.yaml:203`:
```yaml
if [ "${BLOCKSCI_LAUNCH_JUPYTER:-true}" = "0" ] || [ ... = "false" ]; then
  echo 'Parsing complete. Skipping interactive BlockSci environment.'
else
  cd /mnt/blocksci && ./build.sh    # interaktivní notebook prostředí
fi
```
Dnes tedy neinteraktivní `full-run` dostane `0` a BlockSci po
deterministických exportech skončí. Po smazání `launcher.sh` proměnná
nebude nastavená vůbec, compose spadne na svůj vlastní default `true`, a
**každý běh na konci spustí interaktivní BlockSci/Jupyter prostředí, které
neskončí** — tedy zaseknutá analýza lokálně i v CI. Komentář přímo nad tou
větví to říká výslovně: „Noninteractive runIt.sh workflows stop here after
deterministic exports."

Řešení: `BLOCKSCI_LAUNCH_JUPYTER` patří do explicitního prostředí v
`RuntimeCommand` výše, s výchozí hodnotou `0` (dnešní efektivní chování).
`POST_WRAPPER_SHELL` naproti tomu **zaniká bez náhrady** — jeho jediný
efekt byl `exec /bin/bash` uvnitř wrapperova kontejneru
(`launcher.sh:323`), což bez toho kontejneru nedává smysl; kdo chce shell,
spustí si ho sám. Vazba „`POST_WRAPPER_SHELL=1` implikuje Jupyter" tím
mizí a `BLOCKSCI_LAUNCH_JUPYTER` se stává samostatným, přímo nastavitelným
přepínačem.

`runtime_root()` se zjednoduší na jedinou větev — kontrolu checkoutu
(`Path(__file__).resolve().parents[2] / "pipeline"`). Druhá větev
(`coinjoin_pipeline._runtime.client` wheel-bundled fallback) se odstraní
— obsluhovala scénář "nainstalovaný wheel bez checkoutu", který už není
podporovaný (viz sekce 2, odstranění `_runtime` package mappings);
ponechání by jen skrývalo chybu (tichý pokus o neexistující fallback)
místo jasné chybové hlášky, když checkout chybí.

**Konsolidace duplicitní launcher logiky.** Dnes existují tři překrývající
se mechanismy: `resources/container/launcher.sh` (332 řádků — argument
parsing, docker socket setup, `research`/`wrapper` routing, PBS direct
větev s `docker cp` extrakcí), malý `container/launcher.sh` (9 řádků,
passthrough), a `cli.py:direct_wrapper_root()` (Python-side detekce).
Po týhle změně zůstává **jen** `runtime_root()` + přímý subprocess
exec v Pythonu — oba shell launchery se smažou celé. Tím zároveň mizí
celá "jak se wrapper uvnitř vlastního kontejneru dostane k hostitelskému
docker/podman socketu" komplikace (`setup_socket()`,
`resolve_podman_socket()`, `DOCKER_HOST` forwarding,
`INNER_CONTAINER_RUNTIME` rozlišení) — bare wrapper na hostu má k
socketu přímý přístup, žádné mountování/forwarding není potřeba.

**Research subcommand.** `runs`/`external`/`scenarios` podpříkazy dnes
launcher.sh routuje na `run_research()` (`--entrypoint python3
"${WRAPPER_IMAGE}" -m client.research`, `launcher.sh:86/116`). Musí se
převést na stejný přímý model přes `sys.executable` — ale **jako
`-m client.research`, ne jako cesta k souboru
`<runtime_root>/client/research.py`**. Modulová forma je doslovný
ekvivalent dneška: zachová `__package__ == "client"`, takže absolutní
`from client.X import` v `research.py` funguje bez ohledu na `sys.path[0]`.
Cesta k souboru by fungovala jen díky vlastnímu
`sys.path.insert` hacku v `research.py:16-17` — tedy přesně přes tu
implicitní introspekci, na které podle odstavce výše hostitelské CLI
záměrně nemá záviset.
```python
RuntimeCommand(
    executable=sys.executable,
    arguments=("-m", "client.research", *passthrough),
    environment={
        **environment,
        "PYTHONPATH": prepend_path(runtime_root, environment.get("PYTHONPATH")),
    },
)
```

**Modul se hledá přes `PYTHONPATH`, ne přes změnu pracovního adresáře.**
Dřívější verze tohohle odstavce psala „`-m client.research` s
`cwd=runtime_root`" — to je neproveditelné a zároveň nežádoucí.
Neproveditelné, protože `RuntimeCommand` (`commands.py:327-330`) je frozen
dataclass s právě třemi poli (`executable`, `arguments`, `environment`) a
`process.py:25` volá `subprocess.Popen(argv, env=env)` **bez** `cwd`;
pracovní adresář se dnes nikam nepředává. Nežádoucí, protože `cjp` se
běžně spouští z libovolného adresáře a uživatelem zadané relativní cesty
(`--scenario ./scenarios/x.json`, `--run-dir`, `--blocksci-script`) musí
dál vycházet z adresáře, odkud běží `cjp` — přepnutí `cwd` do checkoutu
by je tiše rozvázalo. `PYTHONPATH=<checkout>/pipeline` (stejná hodnota
jako u wrapperu výše) stačí, aby Python `client.research` našel, a
pracovní adresář nechává být.

Rozšiřovat `RuntimeCommand`/`process.run()` o `working_directory` se
v rámci tohohle plánu **nezavádí** — nemá to praktický přínos a je to
zásah do vrstvy, kterou plán jinak nechává beze změny.

Přesný seznam call sites (kde dnes `cli.py`/`commands.py`
tenhle routing dělá) se ověří `rg` auditem při implementaci, ne jen
odhadem.

### 2. Odstranění wrapper image infrastruktury

Smazat: `Dockerfile` (wrapper image), `src/coinjoin_pipeline/resources/container/launcher.sh`,
`container/launcher.sh`, `container/entrypoint.sh`,
`src/coinjoin_pipeline/pipeline_image.py`, `run-pipeline-image.sh`,
`tests/test-run-pipeline-image.sh`, `coinjoin-pipeline-image`
console-script entry, `.github/workflows/publish-pipeline-image.yaml`
(nebo jeho odpovídající job). `WRAPPER_IMAGE`, `WRAPPER_PULL_POLICY`,
`PBS_FRONTEND_WRAPPER_ROOT` env proměnné mizí z kódu úplně. Spolu s nimi
mizí i dvě proměnné, které dnes existují jen kvůli obalovému kontejneru a
nikdo je nečte: `EXPORTERS_FROM_IMAGE` (`launcher.sh:312`, ověřeno `rg` —
žádný konzument) a `WRAPPER_SCRIPT` (`launcher.sh:320`, čte ho jen inline
`bash -c` v témže `docker run`). `WRAPPER_PULL_POLICY` má navíc konzumenty
v testech (`tests/test-runIt-overactive-local.sh:39/71/91/95/98`,
`tests/test-kubernetes-k3d.sh:266`) — smazat i tam, ne jen v
`launcher.sh:65/79-82`.

**`tests/test-runIt-overactive-local.sh` se rozbije ještě druhým způsobem.**
Kromě `WRAPPER_PULL_POLICY` dělá na řádku 326
```bash
cp "${PROJECT_DIR}/container/launcher.sh" "${ISOLATED_PROJECT}/container/launcher.sh"
```
— tedy kopíruje soubor, který tenhle plán maže, do izolovaného projektu.
Ten `cp` selže, ne jen zbytečně vykoná práci. Upravit ve stejném kroku
jako smazání `container/launcher.sh`.

**`commands.py:launcher_command` se odstraňuje explicitně, ne jen
nahrazuje mlčky.** `cli.py` dnes importuje `launcher_command` z
`.commands` a volá ho k sestavení `docker/podman run ... launcher.sh`
invokace (viz sekce 1 — nahrazuje ho přímé sestavení `RuntimeCommand`
kolem `sys.executable`). Po přechodu na přímý subprocess exec by
`launcher_command` a jeho volání z `cli.py` zůstaly jako mrtvý kód, kdyby
se explicitně neodstranily — včetně jeho jednotkových testů
(`tests/unit/...`, ať se jmenují jakkoli; přesný seznam se ověří `rg
launcher_command` při implementaci). Odstranit funkci, její importy a
testy ve stejném kroku, kdy `cli.py` přejde na `RuntimeCommand` přímo.

`host.py`: odstranit `"--pipeline-image": "pipeline"` z
`HOST_VALUE_OPTIONS` a `PBS_FRONTEND_DIRECT`-větev v
`required_image_components` (teď bezpředmětná — "pipeline" komponenta
neexistuje vůbec).

`images.py`/`configuration.py`: odstranit `"pipeline"` z `IMAGE_NAMES`/
`Images`/YAML (`--pipeline-image` flag pryč).

`builder.py:715` — interaktivní TUI má `WRAPPER_IMAGE` v seznamu env
proměnných, ze kterých nabízí defaulty pro `image`-typované flagy
(`"WRAPPER_IMAGE", "BLOCKSCI_IMAGE", "COINJOIN_ANALYSIS_IMAGE",
"COINJOIN_EMULATOR_IMAGE"`) — odstranit `"WRAPPER_IMAGE"` z tohohle
seznamu.

`run-all.sh` — **21 výskytů `WRAPPER_IMAGE`** (ne jen jeden), potřebuje
skutečnou úpravu, ne jen zmínku: `LOCAL_WRAPPER_IMAGE`/
`UPSTREAM_WRAPPER_IMAGE` proměnné, `docker build -t "${WRAPPER_IMAGE}"
"${SCRIPT_DIR}"` build krok (~řádek 339), a `WRAPPER_IMAGE="${WRAPPER_IMAGE}"`
předávané do desítky testovacích invokací (~řádky 409-525). Celý tenhle
build+propagační mechanismus se maže; kde testy dnes potřebují
`WRAPPER_IMAGE`, potřebují ho nahradit odpovídajícím `--uploader-image`/
`--unified-report-image` override podle kontextu testu.

`pyproject.toml` — ověřeno: `packages`/`package-dir`/`package-data`
(řádky 35-47) dnes obsahují `coinjoin_pipeline._runtime`,
`._runtime.client` (→ `pipeline/client`), `._runtime.exporters`
(→ `pipeline/exporters`) — částečný, neúplný pokus o standalone-wheel
packaging (chybí `exporters.blocksci_export`, kořenové skripty). Tyhle mappings
se **odstraní úplně**, ne jen doplní — jinak by obyčejné `pip install .`
mohlo vyprodukovat wheel, který ČÁSTEČNĚ obsahuje runtime (bez
`exporters.blocksci_export`, bez kořenových skriptů) a tiše maskuje chybějící
checkout, místo aby jasně selhal. Zůstávají jen skutečné balíčky pod
`src/coinjoin_pipeline`. Editable instalace (`pip install -e .`) funguje
dál beze změny, protože `cli.py.__file__` pořád odkazuje do checkoutu.

**Akceptační test pro tohle konkrétně:** `python -m build` → čistá venv
→ `pip install dist/*.whl` (ne editable) → `cjp version` uspěje (host CLI
samo je plnohodnotný balíček), ale `cjp full-run --dry-run` **musí**
skončit jasnou chybou typu "coinjoin-pipeline requires an editable
installation from a source checkout" — ne náhodným `FileNotFoundError`
při pokusu najít `pipeline/client/wrapper.py`, který v needitable wheelu
nikdy nebude.

Starý publikovaný GHCR image (`ghcr.io/ondrejman/coinjoin-pipeline`) se
nemaže z registru, jen se přestane aktualizovat.

**SIGINT cleanup pro peer kontejnery — hard gate na smazání launcheru,
ne jen otevřené riziko.** `resources/container/launcher.sh`'s `cleanup()`
dnes na SIGINT/SIGTERM zastaví jmenované peer kontejnery
(`blocksci_analyzer`, `coinjoin_analysis`, `emulator_manager`,
`btc_data_wiper`, `dind_image_prefetch`, `isolated_docker_daemon` —
všechny definované v `pipeline/compose.yaml`, spouštěné přímo z
`wrapper.py` přes `docker/podman compose`).

**Není to "nebyl nalezen odpovídající handler" — `wrapper.py` neobsahuje
modul `signal` vůbec.** `rg 'signal\.|SIGINT|SIGTERM' pipeline/client/wrapper.py`
nevrací nic; jediný úklidový mechanismus je `atexit` (import na řádku 4,
`atexit.register(handle.close)` na řádku 347), a ten se týká lock souboru,
ne peer kontejnerů. Napsání handleru tedy **není podmíněná varianta pro
případ, že test selže** — je to jistá práce v kroku 2 pořadí implementace.
Test níž slouží k ověření, že napsaný handler funguje, ne k rozhodnutí,
jestli je potřeba.

**Druhá, méně nápadná polovina téhle regrese: `atexit` neběží na SIGTERM.**
Na SIGINT se `atexit` handlery spustí (výchozí chování vyhodí
`KeyboardInterrupt` a interpret se korektně ukončí), na SIGTERM ale
proces končí bez unwindu, takže `handle.close` na řádku 347 se neprovede.
Dnes to nevadí, protože vnější `launcher.sh` má na obou signálech `trap`
a peer kontejnery uklidí sám; po jeho odstranění zmizí i tahle záchranná
síť pro lock soubor. Nový handler ve `wrapper.py` proto musí pokrývat
`SIGINT` **i** `SIGTERM` a být idempotentní (uklidí kontejnery i lock,
ať přijde jakýkoli z nich, a snese dvojí doručení).

**Gate patří na přepnutí `cli.py` na `runtime_root()`, ne na smazání
souborů launcheru — dřívější verze plánu tohle spletla.** Jakmile
`cli.py` začne volat `runtime_root()` + přímý `sys.executable` exec
**bezpodmínečně** (sekce 1 — to je celý smysl kroku "Checkout runtime" v
pořadí implementace níže), `resources/container/launcher.sh` a jeho
`cleanup()` trap přestanou být volané **od tohohle okamžiku**, ne až
když se soubor launcheru fyzicky smaže. Riziko osiřelých peer kontejnerů
po Ctrl-C tedy vzniká v momentě přepnutí invokace, ne v momentě smazání
souboru — pozdější fyzické smazání `launcher.sh` je jen úklid mrtvého
kódu beze změny běhového chování. **SIGINT integrační test proto musí
projít předtím, než `cli.py` přepne invokaci na `runtime_root()`
(krok "Checkout runtime"), ne teprve před krokem "Odstranění
wrapperu".** Protože `wrapper.py` dnes žádný signal handler nemá (viz
výše), pořadí uvnitř kroku 2 je dané: nejdřív do `wrapper.py` přibude
top-level `SIGINT`/`SIGTERM` handler s idempotentním cleanupem, pak se
spustí test, a teprve po jeho úspěchu se `cli.py` přepne na
`runtime_root()` — ne obráceně, a rozhodně ne až před fyzickým smazáním
souborů, které v tu chvíli už dávno neběží. Fyzické smazání
`launcher.sh`/`Dockerfile` v pozdějším kroku pak žádnou další ochranu
nepotřebuje — jen potvrzuje, že na ně už nic needukazuje (viz akceptační
grep).

**Test se ale nesmí spouštět přes `cjp` — před cutoverem by testoval starý
launcher, ne nový handler.** Tohle je past, do které dřívější verze týhle
sekce spadla: gate má sedět *před* přepnutím `cli.py`, jenže dokud k
přepnutí nedošlo, `cjp --local-build full-run ...` pořád renderuje volání
`launcher.sh`. Test by tedy prošel díky launcherovu `cleanup()` trapu a o
novém handleru ve `wrapper.py` by neřekl vůbec nic — přesně opačně, než k
čemu je.

**Řešení: `cli.py` dostane sestavení příkazu jako samostatnou funkci a test
volá tutéž funkci.** Místo aby test skládal prostředí ručně (a tím zavedl
druhou, rozcházející se definici kontraktu), vznikne v `cli.py`
```python
def runtime_command(runtime_root: Path, passthrough: list[str], ...) -> RuntimeCommand:
    ...
```
která se použije **jak** v budoucí produkční cestě `cli.py`, **tak** v
integračním testu před cutoverem. Test tím ověřuje přesně ten příkaz,
který se za chvíli stane výchozím, a nemůže se od něj odchýlit.

Test (integrační, spouští se **před** přepnutím `cli.py` na
`runtime_root()`, ne před fyzickým smazáním launcheru):
```bash
# pozor: NE `cjp ...` — to by před cutoverem šlo pořád přes launcher.sh.
# Příkaz se vyrenderuje z cli.py:runtime_command(), tedy z téže funkce,
# kterou bude po cutoveru používat produkční cesta.
python3 -c 'from coinjoin_pipeline.cli import runtime_command, runtime_root; \
  print(runtime_command(runtime_root(), ["full-run", "--driver", "docker", ...]).rendered())' \
  > /tmp/bare-command.sh
bash /tmp/bare-command.sh &
pid=$!
sleep 20
kill -INT "$pid"
wait "$pid" || test "$?" -eq 130
docker ps --format '{{.Names}}' \
  | grep -E 'blocksci_analyzer|coinjoin_analysis|emulator_manager|btc_data_wiper|dind_image_prefetch|isolated_docker_daemon' \
  && exit 1
exit 0
```
(`RuntimeCommand.rendered()` už existuje — `commands.py:336-339` skládá
`env … argv` řetězec, takže tenhle mezikrok nevyžaduje nic nového.
Druhý průchod testu s `kill -TERM` místo `kill -INT`, viz Testy.)

**Totéž platí pro `test-kubernetes-k3d.sh`.** Ten je v kroku 2 druhým
pre-cutover gate (shared-storage driver bez launcherového `--add-host`,
sekce 5) a naráží na stejný problém: dokud `cli.py` není přepnuté,
`cjp` v něm pořád spouští launcher, který `--add-host` přidá — tedy přesně
tu podmínku, kterou má test vyloučit. Musí proto dostat režim (env
přepínač nebo argument), ve kterém místo `cjp` spustí připravený bare
runtime command ze stejné funkce.

### 3. `WRAPPER_IMAGE` má tři konzumenty mimo spouštění wrapperu — ověřeno v kódu

`wrapper.py` používá `WRAPPER_IMAGE` (fallback
`ghcr.io/ondrejman/coinjoin-pipeline:latest`) na třech dalších místech,
která s odpálením wrapperu nemají nic společného. Bez náhrady by po
smazání image tiše začala odkazovat na zmrazený, dál needržovaný
`:latest` tag — nebo by přestala fungovat úplně, jakmile ten tag
zanikne. Platí tedy přesněji: **odstraňuje se wrapper image, vzniká
jeden malý náhradní image (ne žádný) plus dvě pinned veřejné reference**
(`python:3.12-slim-bookworm@sha256:...` pro PBS `unified-report` krok,
`alpine:<verze>@sha256:...` pro `populate_btc_data_volume`) —
**ne "žádné nové images" bez výhrady.**

**a) `populate_btc_data_volume` (`wrapper.py:1043`)** — pomocný kontejner,
co udělá `cp` mezi lokální cestou a pojmenovaným Docker volume. Potřebuje
jen `sh`/`cp`. Náhrada: připnutý obecný `alpine`/`busybox` image
(`alpine:<verze>@sha256:...`) jako **interní konstanta**
(`BTC_VOLUME_HELPER_IMAGE` v `wrapper.py`), ne vlastní `--*-image` CLI
flag — je to čistě interní implementační detail bez potřeby uživatelského
override, žádný build, žádná publikace.

**b) PBS `unified-report` job (`wrapper.py:resolve_unified_report_pbs_image`)**
— "lightweight pipeline image for JSON-only report assembly", potřebuje
jen `python3`+`bash -c` k spuštění bind-mountovaného
`unified_report.py` (exportéry se dál stahují ze S3 a bind-mountují
přesně jako dnes — mění se jen `$IMAGE`, ne mechanismus). **Sjednocené
jméno napříč celým plánem** (dřívější verze míchaly "report-runtime
image"/"unified-report image"/`images.unified_report` — nekonzistentně,
protože žádný vlastní "report-runtime" image nevzniká): `--unified-report-image`/
`COINJOIN_UNIFIED_REPORT_IMAGE`, resolvováno stejným lock-file vzorem
jako uploader (`container/unified-report.image`, stejné umístění jako
`container/uploader.image` — plain soubor v checkoutu, ne packaged
resource přes `importlib.resources`, protože standalone-wheel
distribuce není podporovaná a všechno se čte přímo z checkoutu; jeden
řádek, **neutrální OCI reference bez schématu**,
`python:3.12-slim-bookworm@sha256:...` — ne `docker://python:...`).
Lock soubor musí být použitelný přímo jako argument `docker run
"$UNIFIED_REPORT_IMAGE"` — `docker run` odmítne `docker://` prefix jako
neplatný název image. Singularity cesta (PBS) prefix doplní sama
při invokaci — `unified_report_s3_template.sh:59` volá konkrétně
`singularity exec "docker://$UNIFIED_REPORT_IMAGE" ...`. Tím
funguje **stejný** lock soubor beze změny pro Docker smoke test i pro
PBS/Singularity spuštění — schéma je věcí volajícího runtime, ne
uloženého identifikátoru. **Kandidát je přijatý jen tehdy, pokud to
skutečně projde smoke testem (viz Testy)** — Docker i Singularity varianta
musí spustit `unified_report.py --help` bez instalace dodatečných
balíčků. Pokud smoke test odhalí chybějící OS/Python závislost, buď se z
JSON-only report cesty odstraní, nebo se zavede jiný minimální image
(teprve pak, ne předem) — `blocksci-complete` se tím nijak nemění.

**c) Kubernetes S3 uploader (`kubernetes.py`, `uploader_image` parametr,
`wrapper.py:1701/2258`)** — tohle je to skutečné zjištění: uploader
sidecar dnes dělá `cp -R /app/exporters "/artifacts/$RUN_ID/.pipeline/exporters"`
(`kubernetes.py` řádek ~267) — `/app/exporters` existuje jen proto, že ho
tam nakopíroval starý wrapper `Dockerfile`. Bare wrapper běžící na
frontendu/hostu uploaderu nijak nezpřístupní svůj checkout — uploader
běží v Kubernetes podu, na jiném stroji, bez sdíleného filesystému ani
volume mountu na checkout. Aspoň jedno z těchhle tří tvrzení se tedy musí
změnit: (1) wrapper image se odstraní, (2) žádný nový image nevznikne,
(3) K8s→S3 transport exportérů zůstane úplně beze změny.

Zvažovaná varianta "zabalit `/app/exporters/` do nového malého uploader
image při buildu" — funkční, ale **zavržená**: znovu zavádí přesně ten
staleness problém, kterému se celý "checkout je zdroj pravdy" přístup
snaží vyhnout. Kdyby se exportéry pekly do image při buildu, jakákoli
změna `pipeline/exporters/` (i commitnutá) by se do K8s→S3→PBS toku
propsala až po ručním rebuildu a republishnutí uploader image — tichý,
snadno zapomenutelný krok, bez jakékoli vazby na "dirty checkout" flag
(commitnutá změna není dirty, ale pořád by nebyla v tom, co se skutečně
uploadne).

**Zvolené řešení: exportéry uploaduje na S3 přímo wrapper (frontend-side),
ne in-cluster uploader — ale s přesně určeným pořadím.** S3 režim už dnes
vyžaduje `s5cmd` a S3 credentials na frontendu
(`artifacts.py:s3_access_preflight`) — wrapper tedy má, přesně na tom
stroji, kde běží bare z checkoutu, jak přístup k živému
`pipeline/exporters/`, tak nástroj k jeho nahrání. PBS strana (která z
tohohle přesného S3 umístění dnes stahuje) se nemění vůbec — jen se
mění, **kdo** tam ty soubory dá.

**Kritické omezení pořadí, ověřené v kódu.** `prefix_preflight`
(`kubernetes.py:234-260`, kontejner `"name": "prefix-preflight"` v pod
specu) dnes běží jako součást Kubernetes podu a dělá `s5 ls
"$ARTIFACT_URI/$RUN_ID/*"` — pokud najde **cokoli** pod tímhle prefixem,
job hned selže s "run prefix already contains artifacts; choose a fresh
--run-id". Kdyby wrapper nahrál exportéry na S3 dřív, než je pod
vytvořený, tenhle kontejner by run prefix už neviděl jako prázdný a
odmítl by ho jen kvůli přítomnosti exportérů samotných. **Řešení je
záměrně minimální — jedna cílená výjimka, ale jen v tom preflightu, který
běží *po* uploadu, ne v tom, který mu předchází.**

**Výjimka pro `.pipeline/exporters/**` patří výhradně do in-pod
`prefix_preflight`, ne do frontendového `ensure_empty_run_prefix`.**
Tohle je důležité rozlišení, které dřívější verze plánu smíchala
dohromady. Frontendový `ensure_empty_run_prefix` běží **před** uploadem —
pokud by i on ignoroval `.pipeline/exporters/**`, vznikne mezera:
nedokončený/napůl selhaný upload z předchozího pokusu zanechá staré
soubory pod stejným run-id; frontend je při dalším pokusu ignoruje
(protože "exportéry se přece ignorují"); nový `s5cmd sync` přepíše jen
soubory, které se změnily nebo přibyly, ale nesmaže soubory, které mezitím
z `pipeline/exporters/` zmizely — výsledkem je na S3 směs starých a
nových exportérů, tichy a nedeterministicky. Frontendová kontrola proto
**nemá žádnou výjimku**: vyžaduje, aby byl run prefix před uploadem
**úplně prázdný**, přesně jako dnes. Teprve `prefix_preflight` uvnitř
Kubernetes podu (který běží *po* tom, co frontend už legitimně nahrál
exportéry) ignoruje `.pipeline/exporters/**` — protože v tu chvíli už ví,
že tenhle konkrétní obsah tam patří.

**Ignorovat ale nestačí — in-pod preflight musí zároveň pozitivně ověřit,
že exportéry tam opravdu jsou.** Samotná výjimka je jen negativní pravidlo
("tohle neber jako cizí obsah"); prázdný nebo neúplný exporter prefix by
jí prošel stejně dobře jako kompletní. Preflight proto kromě ignorování
zkontroluje oba vstupní body přímo:
```bash
s5 ls "$ARTIFACT_URI/$RUN_ID/.pipeline/exporters/unified_report.py" >/dev/null
s5 ls "$ARTIFACT_URI/$RUN_ID/.pipeline/exporters/blocksci_export/analysis.py" >/dev/null
```
Jsou to tytéž dva soubory, které kontroluje frontend před uploadem (bod 4
níž) — jednou na zdroji, podruhé na cíli.

**Co ta dvojice kontrol garantuje a co ne — přesně, ať se to nepřecení.**
Garantuje: exporter prefix není prázdný a obsahuje oba očekávané vstupní
body. **Negarantuje: že se celý exporter strom nahrál kompletně.** Oba
entrypointy mohou existovat, a přesto může chybět třeba `common.py`,
`manifest.py`, `report_builder.py` nebo `blocksci/detector.py` — takový
upload projde a selže až při importu na výpočetním uzlu.

Není to důvod vracet marker, hash gate ani resumovatelný protokol.
Formulace zní tedy takhle: **dvojice kontrol zachytí prázdný nebo zjevně
neúplný upload ještě před zahájením emulace; úplná integrita všech souborů
se samostatně transakčně neověřuje a je to vědomé omezení jednoduchého,
neresumovatelného staging modelu.** Kdyby se to někdy ukázalo jako reálný
problém, nejlevnější přírůstek je porovnat po `s5cmd sync` počet lokálních
filtrovaných souborů s počtem objektů pod prefixem — pro tenhle plán se
ale nezavádí.

**Zvolen jednoduchý, ne resumovatelný postup — vědomý trade-off oproti
dřívější verzi tohohle plánu.** Předchozí iterace zavedla plný
resumovatelný stavový automat (JSON `exporters.ready` marker, hash
porovnávaný přes `EXPORTERS_SHA256` env var uvnitř podu, deduplikaci a
opětovné připojování k existujícím Jobům s hash-anotacemi) — technicky
správně, ale je to samostatný distribuovaný staging protokol, ne
přiměřená odpověď na "kam dát pár souborů před spuštěním Jobu" pro
nástroj s jedním uživatelem. Zjednodušeno na:

1. `s3_access_preflight` (S3 credentials/`s5cmd` na frontendu) —
   beze změny.
2. **`kubernetes_s3_auth_preflight` se přesouvá před staging, ne až
   po něm.** Dřívější pořadí (upload → teprve pak K8s auth preflight)
   má díru: kdyby K8s auth preflight selhal (chybějící RBAC, špatný
   kubeconfig), exportéry by už ležely na S3 pod daným run-id a run-id
   by vyžadovalo cleanup, i když se Kubernetes strana vůbec nezapojila.
   Ověřit dostupnost/oprávnění clusteru je levné a nemá vedlejší efekty
   na S3 — patří tedy před cokoli, co na S3 zapisuje.
3. Frontend ověří přes existující `artifacts.py:ensure_empty_run_prefix`,
   že run prefix je **úplně prázdný** — beze změny oproti dnešku, žádná
   výjimka pro exportéry na týhle straně (viz vysvětlení výše). Žádný
   marker, žádné rozlišování stavů — pořád je to binární "prázdno, nebo
   ne".
4. Frontend nejdřív ověří, že vůbec má co nahrát — **kontrola vstupních
   bodů, ne jen existence adresáře**:
   ```python
   required_exporters = (
       exporters_dir / "unified_report.py",
       exporters_dir / "blocksci" / "analysis.py",
   )
   for path in required_exporters:
       if not path.is_file():
           raise RuntimeError(f"Required exporter is missing: {path}")
   ```
   Tyhle dva soubory jsou skutečné vstupní body reportovací a BlockSci
   analytické cesty (`unified_report_s3_template.sh:54` a
   `blocksci_analyze_s3_template.sh:70` je bind-mountují a spouštějí).
   Bez téhle kontroly by `EXPORTERS_DIR` bez vstupních bodů (v krajním
   případě prázdný) prošel staging krokem tiše — `s5cmd sync` prázdného
   adresáře uspěje — a selhalo by to až na výpočetním uzlu, hodiny po
   zařazení jobu do fronty. Kontrola cílí právě na tenhle případ, ne na
   úplnost celého stromu (viz vymezení výše).
5. Frontend nahraje aktuální exportéry přímo na S3 — **z absolutní
   cesty odvozené od `EXPORTERS_DIR`, ne z relativní `pipeline/exporters/`**.
   `cjp`/wrapper se dnes může spustit z libovolného pracovního adresáře
   (`--runs-root` je taky absolutizovaná v `cli.py`), takže relativní
   cesta by při spuštění z jiného CWD mířila jinam nebo by vůbec
   neexistovala:
   ```python
   exporters_dir = Path(os.environ["EXPORTERS_DIR"]).resolve()
   ```
   (`EXPORTERS_DIR` už wrapper dostává jako absolutní cestu z hostitelského
   CLI — viz sekce 1 — `.resolve()` je jen obranná normalizace, ne nový
   zdroj pravdy.)
   ```bash
   s5cmd sync --exclude '*__pycache__*' --exclude '*.pyc' \
     "$EXPORTERS_DIR/" "$ARTIFACT_URI/$RUN_ID/.pipeline/exporters/"
   ```
   — žádný dočasný snapshot, žádný samostatný hash-gating krok. Nástroj
   běží interaktivně z jednoho checkoutu; riziko, že se exportéry změní
   uprostřed jednoho `s5cmd sync` běhu na milisekundy, je akceptované
   jako zanedbatelné pro tenhle use-case.

   **Exclude filtry nejsou volitelná hygiena — bez nich se rozbije
   BlockSci cesta.** Dnešní `cp -R /app/exporters` kopíruje z čisté
   vrstvy image, kde žádný bytecode není (`Dockerfile` navíc jede s
   `PYTHONDONTWRITEBYTECODE=1`). Živý checkout ho ale má: v tomhle
   pracovním stromu právě teď leží `pipeline/exporters/__pycache__/`
   s `*.cpython-314.pyc` soubory z hostitelského Pythonu 3.14. Ty by se
   nahrály na S3, stáhly zpátky přes `pbs.py:1021`, a bind-mountly do
   `blocksci-complete` (Python **3.8**, `blocksci_s3_template.sh:55`) i
   do pinnutého `python:3.12` report image — tedy do dvou interpretů,
   pro které je cp314 bytecode nečitelný. Je to přesně ten vzorec
   "hostitelský Python 3.14 maskuje/rozbíjí to, co běží v kontejneru",
   na který už v tomhle workspace došlo dřív. Filtry proto patří do
   plánu, ne do improvizace při implementaci.
6. **Teprve potom** wrapper vytvoří Kubernetes Job.
7. Pokud upload selže, frontend skončí jasnou chybou — žádný retry,
   žádné resumování. Uživatel zvolí nový `--run-id`, nebo prefix ručně
   vyčistí (existující `clean-s3`/`download-report` host akce) a spustí
   znovu od začátku. To je záměrně stejná úroveň zotavení, jakou dnešní
   `ensure_empty_run_prefix` už nabízí — plán nepřidává nic nad rámec
   toho, co dnešní S3 mód dělá pro cokoli jiného než exportéry.

**Žádná deduplikace ani opětovné připojování k existujícímu Jobu.**
Pokud se stejný `--run-id` použije podruhé po částečném selhání, dřívější
Job (pokud existuje) koliduje se standardním Kubernetes chováním
(`kubectl apply`/create ohlásí konflikt) — řeší se stejně jako dnes,
novým `--run-id`. Žádná annotace s hashem, žádný stav-podle-tabulky.

**Hash exportérů se zaznamená do provenance, ale neřídí spuštění.**
Frontend může spočítat jednoduchý hash nahrávaného stromu (např.
`sha256` nad seřazeným výpisem `<hash souboru> <cesta>` řádků) a uložit
ho do `research_manifest.json`/unified reportu vedle `git_commit`/
`git_dirty` — čistě informační záznam pro zpětnou dohledatelnost "co se
skutečně nahrálo", ne branding, na kterém by záviselo, jestli Job vznikne.
Žádný JSON marker na S3, žádné `EXPORTERS_SHA256` env, žádné porovnání
uvnitř podu.

**Stejná staging funkce pro `full-run` i samostatné `emulate` — ověřeno v
kódu, dnes tomu tak není.** `run_full_run_s3` (`wrapper.py:2583`) dělá
`require_qsub()` → `s3_access_preflight` → `ensure_empty_run_prefix`
→ `kubernetes_s3_auth_preflight` → `run_kubernetes_s3_emulation(args)`
(řádky 2619-2624). Ale samostatná akce `emulate --driver kubernetes
--artifact-backend s3` (`wrapper.py:3038-3045`) volá
`run_kubernetes_s3_emulation(args)` **přímo**, bez jediného z těchhle
kroků před tím. Kdyby nový upload krok (body 4-5 výše) přibyl jen do
`run_full_run_s3`, samostatné `emulate` by exportéry na S3 vůbec
nenahrálo — přesně ten rozpor, co je dnes u `ensure_empty_run_prefix`
(`full-run` ho dělá, samostatné `emulate` ne). Řešení: nová sdílená
funkce (např. `stage_kubernetes_s3_run(args, access)`), která zabalí
`s3_access_preflight` → `kubernetes_s3_auth_preflight` →
`ensure_empty_run_prefix` (beze změny, bez výjimky — prefix musí být
úplně prázdný před uploadem) → kontrola vstupních bodů exportérů →
upload exportérů, a volá ji **jak**
`run_full_run_s3`, **tak** `emulate`'s S3 větev, obě před voláním
`run_kubernetes_s3_emulation`. **Pořadí prvních dvou kroků se oproti
dnešnímu `run_full_run_s3` mění** — `kubernetes_s3_auth_preflight`
běží před `ensure_empty_run_prefix`/uploadem, ne po nich (viz
zdůvodnění výše: neúspěšný K8s auth preflight nesmí nechat exportéry
už nahrané na S3 pod run-id, které pak vyžaduje cleanup, i když se
Kubernetes strana vůbec nezapojila). Tohle je oprava existující nekonzistence
mezi oběma akcemi, ne nová komplikace zavedená tímhle plánem.

**Pozor: pro samostatné `emulate --artifact-backend s3` to ale není jen
oprava, je to změna chování — musí se tak i komunikovat.** Dnes tahle
akce běží bez `ensure_empty_run_prefix` (`wrapper.py:3038-3045` volá
`run_kubernetes_s3_emulation` přímo), takže **uspěje i proti neprázdnému
run prefixu**. Po zavedení sdílené staging funkce začne v tom případě
selhat. To je záměrné a správné (jinak by upload exportérů zapisoval do
cizího prefixu), ale je to fail-closed regrese pro kohokoli, kdo dnes
`emulate` pouští opakovaně nad stejným `--run-id`. Před implementací
projít MetaCentrum runbook a `tests/test-kubernetes-s3-minio.sh`, jestli
na tomhle chování nějaký dokumentovaný postup nestojí; pokud ano, upravit
runbook ve stejném commitu, ne až po prvním selhaném běhu.

In-cluster uploader `cp -R /app/exporters ...` řádek (`kubernetes.py:267`)
se maže úplně — uploader pak nahrává **jen emulační data**, nikdy
exportéry, a nový uploader image obsahuje jen `bash`+`kubectl`+`s5cmd`
(multi-stage, pinned, checksum-ověřené binárky, žádné exportéry, žádný
wheel, žádný `jq` — bez JSON markeru k parsování ho nic uvnitř podu
nepotřebuje).

`populate_btc_data_volume` (bod a) se přepojí na interní
`BTC_VOLUME_HELPER_IMAGE` konstantu (pinned `alpine`/`busybox`), ne na
uploader image ani na vlastní CLI flag — nesouvisející odpovědnost bez
potřeby uživatelského override.

**Lifecycle nového `uploader` image — musí být konkrétní, ne jen
pojmenovaný.** `--version`-koordinovaný resolver (jako u
`emulator`/`blocksci`/`coinjoin_analysis`/`mappings`) by uploader svazoval
s verzemi, se kterými nemá nic společného — nemění se s
`coinjoin-pipeline` verzí ani s Git SHA checkoutu, jen s tím, kdy se
změní pinned `kubectl`/`s5cmd`/shell logika. Místo toho:
`container/uploader.Dockerfile` (build) + committnutý
`container/uploader.image` (jeden řádek, plná immutable reference
`ghcr.io/ondrejman/coinjoin-pipeline-uploader@sha256:...`). Resolver:
explicitní `--uploader-image` → `COINJOIN_UPLOADER_IMAGE` env → obsah
`container/uploader.image`.

**Do které vrstvy oba nové flagy patří — musí být určeno, jsou to dvě
různé cesty kódu.** `coinjoin-pipeline` má dva oddělené systémy pro
image flagy a plán zatím neřekl, do kterého `--uploader-image`/
`--unified-report-image` spadají:

- **hostitelské options** — `HOST_VALUE_OPTIONS` (`host.py`), `Images`,
  `images.py`, `configuration.py`, `usage()` v `cli.py:80`; sem patří
  rušený `--pipeline-image` a celá `--version`-koordinovaná sada;
- **wrapper passthrough** — `pipeline/client/cli_options.py` +
  `command_metadata.json`; sem patří například `--blocksci-image`.

**Rozhodnutí: oba nové flagy jsou wrapper passthrough, ne hostitelské
options.** Plyne to přímo z lifecycle rozhodnutí o pár odstavců výš —
hostitelská vrstva `Images` je právě ta `--version`-koordinovaná sada, a
uploader ani pinnutý Python image se s `--version` **záměrně nekoordinují**
(mají vlastní lock soubory a nezávislý lifecycle). Zařadit je do `Images`
by ten závěr přímo popřelo. Konzumenti jsou navíc oba uvnitř wrapperu
(`kubernetes.py` pod spec, `wrapper.py` PBS report krok), ne v hostitelském
CLI, které tyhle images nikdy samo nespouští.

Praktický důsledek, na který se nesmí zapomenout: passthrough flag musí
být v `command_metadata.json`, protože `tests/test-command-builder-contract.sh`
vynucuje paritu metadat a parseru — bez toho ten test spadne. Do
`HOST_VALUE_OPTIONS` se naopak **nepřidávají**.

Ještě jedna vazba, ať se nezdvojí: `host.py:123 add_effective_image_arguments`
existuje proto, aby se do wrapperu nevrátily plovoucí `latest` tagy tím, že
se flag vynechá. U těchhle dvou images plní tutéž roli lock soubor
(`container/*.image` + CI regex na `@sha256:`), takže se do
`add_effective_image_arguments` **nedoplňují** — jinak by tentýž problém
řešily dva mechanismy najednou a nebylo by jasné, který vyhrává.

**Konkrétní proces aktualizace lock souboru — dřívější verze plánu
popsala jen mechanismus, ne workflow.**
1. Ruční/na-vyžádání workflow (`.github/workflows/publish-uploader-image.yaml`
   — samostatný, nahrazující dnešní automatické publikování při každém
   pushi do main z `publish-pipeline-image.yaml`, protože pro immutable
   lock na technický nástroj nedává smysl publikovat při každém commitu)
   sestaví a pushne `uploader` image.
2. Workflow vypíše plnou `@sha256:...` referenci jako artifact/output.
3. Navazující commit/PR aktualizuje `container/uploader.image` touhle
   hodnotou (ruční krok, ne automatický — udržuje auditovatelnost, kdy a
   proč se referencovaný uploader změnil).
4. CI ověří formát souboru, ne jen že existuje:
   ```bash
   grep -Eq '^ghcr\.io/ondrejman/coinjoin-pipeline-uploader@sha256:[0-9a-f]{64}$' \
     container/uploader.image
   ```
   (floating tag bez `@sha256:` = CI selže).

**Pro `container/unified-report.image` platí jen část tohohle procesu —
ne "stejný proces".** Rozdíl je zásadní: `uploader` je náš vlastní build
(kroky 1-3 výše — build, push, aktualizace lock souboru vlastním
digestem), zatímco `python:3.12-slim-bookworm` je cizí, upstream image —
nic se nebuilduje ani nepushuje. Proces je jen:
1. Vybrat aktuální upstream tag (`python:3.12-slim-bookworm`), zjistit
   jeho `@sha256:...` digest (`docker pull`+`docker inspect`, nebo
   `skopeo inspect`, na kterémkoli stroji s přístupem k Docker Hub).
2. Commit/PR aktualizuje `container/unified-report.image` touhle
   hodnotou (ruční krok, stejně jako u uploaderu — auditovatelnost kdy a
   proč se pinned verze změnila).
3. CI ověří formát souboru stejně jako u uploaderu (regex na
   `@sha256:[0-9a-f]{64}`, viz výše), jen s jiným image jménem/bez
   vlastního GHCR registru.
Kandidát je navíc přijatý **jen po úspěšném smoke testu** (viz sekce b
výše a Testy) — vybraný digest se do lock souboru zapíše až po ověření,
ne předem.

Lokální `run-all.sh`/`test-kubernetes-s3-minio.sh` musí umět přepsat
referenci na lokálně sestavený tag (`--uploader-image` override), aby
test nezávisel na tom, co je zrovna v `container/uploader.image`.

### 4. Provenance — `WRAPPER_IMAGE*` zpětná kompatibilita, plus nová pole pro uploader/report image

Ověřeno v kódu: `wrapper.py:330` (`IMAGE_PROVENANCE_ENV`) pořád mapuje
`WRAPPER_IMAGE` → `(WRAPPER_IMAGE_ID, WRAPPER_IMAGE_DIGEST)`;
`wrapper.py:1182-1184` (`export_command`) pořád předává `--wrapper-image`/
`--wrapper-image-id`/`--wrapper-image-digest` do
`pipeline/exporters/cli.py`/`blocksci/analysis.py`.

**Dřívější verze tohohle plánu tady nechávala volbu ("buď se přestat
plnit, nebo se explicitně zjednodušit") — ta volba je ve skutečnosti
vynucená a padá na jednu možnost.** Varianta "přestat plnit, env prázdné
→ `null`" je neproveditelná, protože by identifikátor `WRAPPER_IMAGE`
nechala v kódu, a **akceptační grep níže (`git grep -n 'WRAPPER_IMAGE'`)
očekává nula výskytů mimo `MIGRATION.md`**. Platí tedy jen druhá varianta:
flagy i env mapping se odstraní, a to na všech čtyřech místech, kde ten
řetězec po smazání launcheru ještě zbývá:

- `pipeline/client/wrapper.py:330` — položka v `IMAGE_PROVENANCE_ENV`.
- `pipeline/client/wrapper.py:1182-1184` — tři `--wrapper-image*` argumenty
  v `export_command`.
- `pipeline/exporters/cli.py:132/148` a `pipeline/exporters/blocksci_export/analysis.py:181`
  — `parser.add_argument("--wrapper-image", default=os.environ.get("WRAPPER_IMAGE"))`
  a jeho `--wrapper-image-digest` protějšek.
- `pipeline/compose.yaml:141-143` — `WRAPPER_IMAGE`/`_ID`/`_DIGEST`
  předávané do prostředí BlockSci kontejneru.

Manifest/report parser (`pipeline/exporters/manifest.py`,
`images.wrapper`/`image_digests.wrapper` pole) zůstává schopný **číst**
staré hodnoty ze starých reportů (zpětná kompatibilita), ale nové běhy
zapíšou `null`.

**To samo o sobě by ale ztratilo informaci, která dřív existovala** —
který image uploadoval výstupy a ve kterém image se sestavil report. Nová
pole v unified reportu (ne jen v host manifestu):
```json
{
  "images": {
    "uploader": "ghcr.io/ondrejman/coinjoin-pipeline-uploader@sha256:...",
    "unified_report": "docker.io/library/python:3.12-slim-bookworm@sha256:..."
  }
}
```
s odpovídajícími položkami v `image_digests`, kde jsou dostupné.

**Transport těch hodnot musí být určený, ne ponechaný na implementaci.**
Odstraněním `--wrapper-image*` z `export_command` zmizí jediná cesta,
kterou se dnes image provenance dostává z wrapperu do exportérů — nová
pole by jinak neměla jak vzniknout. Kontrakt je proto stejného tvaru jako
ten rušený, jen s jinými jmény: `wrapper.py:export_command` předává
exportérům čtyři argumenty
```
--uploader-image / --uploader-image-digest
--unified-report-image / --unified-report-image-digest
```
a `pipeline/exporters/cli.py` (resp. `blocksci/analysis.py`) je přijímá
přesně tam, kde dnes stojí `--wrapper-image*` (`cli.py:132/148`,
`analysis.py:181`). CLI argumenty, ne env proměnné — je to konzistentní
s dnešní provenance architekturou a hodnota je viditelná v
`REPRODUCTION_COMMAND`.

Digest se u pinnutých referencí získá **bez Docker daemonu**, prostým
rozborem samotné reference:
```python
def digest_from_reference(image: str | None) -> str | None:
    if image and "@sha256:" in image:
        return f"sha256:{image.rsplit('@sha256:', 1)[1]}"
    return None
```
To je podstatné hlavně na MetaCentrum frontendu, kde Docker není a nikdy
nebude — `docker inspect`-based odvození digestu (jak ho dnes dělá
`IMAGE_PROVENANCE_ENV` pro lokální images) by tam selhalo. Protože oba
lock soubory (`container/uploader.image`, `container/unified-report.image`)
smí obsahovat **jen** plnou `@sha256:` referenci a CI to vynucuje regexem
(viz sekce 3), je tahle cesta vždy dostupná; `None` zbývá jen pro případ
`--uploader-image` overridu na plovoucí tag v lokálních testech.

`populate_btc_data_volume`'s alpine/busybox helper do unified reportu
nepatří (je to čistě host-side operační detail), ale může jít do host
manifestu (`research_manifest.json`) vedle `coinjoin_pipeline_git_commit`/
`_git_dirty`.

### 5. Nevyřešená otázka: `host.docker.internal` pro shared-storage Kubernetes driver

**Na rozdíl od ostatních zjištění v tomhle plánu tohle nejde uzavřít
čistě čtením `coinjoin-pipeline` — patří sem jako otevřená otázka k
ověření, ne jako potvrzený bug.** `wrapper.py:320` defaultuje
`DEFAULT_K8S_CONTROL_IP = "host.docker.internal"`, předávané do
`kubernetes_emulator_command()` (`wrapper.py:981-992`) jako `--control-ip`
argument pro **`coinjoin-emulator`'s vlastní `manager.py --driver
kubernetes`** — oddělený, mimo scope repozitář (viz "Scope repozitářů").
Co přesně `control_ip` uvnitř `coinjoin-emulator` dělá (manager reaching
cluster, nebo cluster reaching back to manager), odsud nejde bezpečně
uzavřít.

Co jde ověřit odsud: `wrapper.py`/`pipeline/compose.yaml` **nikde** samy
nenastavují `--add-host`/`extra_hosts` — jediné místo, kde se dnes
`host.docker.internal` mapuje, je `resources/container/launcher.sh`'s
`--add-host host.docker.internal:host-gateway`, aplikované jen na
**wrapperův vlastní** kontejner. Docker kontejnery nedědí `/etc/hosts`
záznamy jeden od druhého, a na nativním Linuxu (ne Docker Desktop) se
`host.docker.internal` bez explicitního mappingu nerozresolvuje. Otevřená
otázka: záviselo tohle chování dnes na tom, že `host.docker.internal` je
mapovaný **uvnitř wrapperova vlastního kontejneru**, nebo je to
irelevantní (k3d si tohle řeší na úrovni clusteru nezávisle)? Pokud první,
odstranění wrapper image bez náhrady může rozbít shared-storage
Kubernetes driver konkrétně pro tenhle default.

**Musí se ověřit před přepnutím `cli.py` na bare wrapper jako jediné
chování — ne až před fyzickým smazáním launcheru — a výhradně
empiricky, ne čtením `coinjoin-emulator`.** Dřívější verze týhle sekce
připouštěla i "přečtení
`coinjoin-emulator`'s zpracování `--control-ip`" jako variantu ověření —
to je v přímém rozporu se "Scope repozitářů" výše ("žádný jiný nested
repo... se v rámci téhle práce neprochází"), i kdyby šlo jen o čtení bez
úpravy. Aby nevznikl interpretační spor, platí jen jedno pravidlo: **žádný
jiný repozitář se nečte ani neupravuje.** Ověření běží výhradně
empiricky — reálný `test-kubernetes-k3d.sh` (shared-storage driver, ne
S3) s bare wrapperem a BEZ `--add-host` musí projít. Pokud selže, oprava
se řeší výhradně uvnitř `coinjoin-pipeline`, např. explicitním
`--kubernetes-control-ip` flagem/resolvnutím skutečné gateway IP na
hostu místo spoléhání na `host.docker.internal` magic hostname, který
dával smysl jen v kontextu "wrapper běží v kontejneru".

### 6. Preflighty — kapacitní model, ne "zůstávají beze změny"

**Tohle je vážnější, než dřívější verze plánu tvrdila.** Ověřeno v kódu:
`doctor.py:check()` (dnešní `docker/podman` preflight) je **nepodmíněný**
— zavolá `shutil.which(runtime)` + `<runtime> info` bez ohledu na to, jestli
akce/driver Docker vůbec potřebuje. Místo, které tenhle
nepodmíněný check dnes obchází, je `cli.py:183`:
```python
direct_pbs = os.environ.get("PBS_FRONTEND_DIRECT") == "1" and not required_images
preflight = [] if direct_pbs else doctor_check(...)
```
Zároveň `doctor.py:validate_arguments()`'s `qsub` kontrola je dnes
podmíněná přesně opačně — funguje **jen** když
`os.environ.get("PBS_FRONTEND_DIRECT") == "1"`:
```python
if uses_pbs and os.environ.get("PBS_FRONTEND_DIRECT") == "1" and not dry_run:
    if shutil.which("qsub") is None:
        errors.append(...)
```
A do třetice — **tohle dřívější verze plánu vůbec nezmínila, přitom je to
nejviditelnější z celé trojice** — `cli.py:155-163` není preflight, ale
tvrdá validační chyba, která `full-run --artifact-backend s3` bez
nastavené proměnné **odmítne spustit vůbec**:
```python
if (
    action == "full-run"
    and option_value(passthrough, "--artifact-backend") == "s3"
    and os.environ.get("PBS_FRONTEND_DIRECT") != "1"
):
    errors.append(
        "full-run --artifact-backend s3 runs qsub and s5cmd directly on the "
        "frontend; set PBS_FRONTEND_DIRECT=1"
    )
```

Prosté smazání `PBS_FRONTEND_DIRECT` bez přepsání týhle logiky rozbije
preflighty **třemi směry najednou**: `direct_pbs` přestane existovat, takže
nepodmíněný `docker`/`podman` check začne běžet i pro čisté
`full-run --artifact-backend s3` na MetaCentru (kde Docker není a nikdy
nebyl potřeba); `qsub` podmínka natrvalo zůstane `False` (env
var už nikdy nebude `"1"`), takže chybějící `qsub` přestane preflight
kontrolovat úplně, tichá regrese opačným směrem; a validace výše se buď
smaže spolu s proměnnou (správně — S3 režim je po tomhle plánu jediné a
nepodmíněné chování), nebo se z ní stane chyba, kterou nejde uspokojit.

**Úplný soupis míst — ověřeno `rg`, ne odhadem.** Dřívější verze plánu
jmenovala jen `cli.py:183` a `doctor.py:32`. Skutečná množina:

| Soubor | Řádek | Co s tím |
|---|---|---|
| `src/coinjoin_pipeline/cli.py` | 158 | tvrdá validace výše — smazat celý blok |
| `src/coinjoin_pipeline/cli.py` | 178-181 | nastavení `PBS_FRONTEND_WRAPPER_ROOT` — smazat |
| `src/coinjoin_pipeline/cli.py` | 183 | `direct_pbs` — nahradit `required_capabilities` |
| `src/coinjoin_pipeline/host.py` | 93 | větev v `required_image_components` |
| `src/coinjoin_pipeline/doctor.py` | 32 | `qsub` podmínka — navázat na `Capability.QSUB` |
| `src/coinjoin_pipeline/resources/container/launcher.sh` | 165, 173 | mizí s celým souborem |

A testy, které se bez úpravy rozbijí — v seznamu smazávaných/upravovaných
artefaktů v sekci 2 dosud chyběly, přestože `launcher_command` testy tam
jmenované jsou:

- `tests/unit/test_cli.py:257, 268, 310, 335` — `mock.patch.dict` s
  `{"PBS_FRONTEND_DIRECT": "1"}`.
- `tests/unit/test_cli.py:545-551` — test, který nastaví proměnnou na
  `"0"` a **asertuje řetězec `PBS_FRONTEND_DIRECT=1` ve stderr**. Po
  smazání validace tenhle test nemá co ověřovat; nahradit ho testem, že
  `full-run --artifact-backend s3` projde validací bez jakékoli env
  proměnné.
- `tests/test-local-pbs-analysis.sh:126`, `tests/test-kubernetes-s3-minio.sh:261`,
  `tests/test-kubernetes-pbs-analysis.sh:267`, `tests/test-parallel-pbs-analysis.sh:199`
  — každý dělá `export PBS_FRONTEND_DIRECT=1`; řádek se smaže (to je
  zároveň to, co Testy níž myslí "ověřit bez `PBS_FRONTEND_DIRECT` env
  vůbec").

**Řešení: kapacitní model, ne další ad hoc podmínka.**
```python
class Capability(Enum):
    BASH = auto()
    CONTAINER_RUNTIME = auto()
    COMPOSE = auto()
    KUBECTL = auto()
    KUBERNETES_RBAC = auto()
    QSUB = auto()
    S5CMD_FRONTEND = auto()

def required_capabilities(action: str, arguments: list[str]) -> set[Capability]: ...
```
Příklady (přesné mapování se dolaďuje při implementaci, tohle je princip):

| Akce | Capabilities |
|---|---|
| lokální Docker/Podman `full-run` | `BASH`, `CONTAINER_RUNTIME`, `COMPOSE` |
| Kubernetes shared-storage emulace | `CONTAINER_RUNTIME`, `KUBECTL` |
| Kubernetes S3 emulace | `KUBECTL`, `KUBERNETES_RBAC`, `S5CMD_FRONTEND` |
| `full-run --artifact-backend s3` na MetaCentru | `KUBECTL`, `KUBERNETES_RBAC`, `QSUB`, `S5CMD_FRONTEND` — **bez** `CONTAINER_RUNTIME` |
| PBS shared-storage analýza | `BASH`, `QSUB` |
| `pbs-from-s3` | `QSUB`, `S5CMD_FRONTEND` |

**Proč Kubernetes shared-storage emulace potřebuje `CONTAINER_RUNTIME` —
oprava chybného zdůvodnění z dřívější verze.** Není to proto, že by
"wrapper běžel lokálně" (to platí pro každou akci, wrapper vždycky běží
lokálně po tomhle plánu). Je to proto, že **`coinjoin-emulator`'s vlastní
`manager.py`** (spouštěný z bare wrapperu jako dřív) i pro shared-storage
Kubernetes driver dál spouští jednotlivé round/workload procesy jako
lokální Docker/Podman kontejnery — jen s daty na sdíleném volume
dostupném i K8s podům, ne přes S3. `CONTAINER_RUNTIME` je tedy potřeba
kvůli **coinjoin-emulator**, ne kvůli wrapperu samotnému; S3 driver
naproti tomu emulační práci deleguje celou dovnitř clusteru (K8s Job), a
proto `CONTAINER_RUNTIME` nepotřebuje.

**Dostupnost workload images (`required_image_components`, dnešní funkce v
`host.py`) je záměrně oddělená funkce od `required_capabilities`** — jedna
odpovídá na "je nástroj dostupný", druhá na "je tenhle konkrétní image
dostupný pro lokální Docker/Podman driver". Nesměšovat do jednoho modelu.
`workload images` preflight (emulator/blocksci/coinjoin-analysis/mappings),
`kubectl`/`kubeconfig` reachability, a `qsub` dostupnost jako koncepty
zůstávají — mění se **jak a kdy** se vyhodnocují (podle capabilities dané
akce, ne podle env var), ne jestli existují.

### 7. Provenance — commit a dirty stav checkoutu, plus verze hostitelských nástrojů

Protože checkout je teď garantovaná, dokumentovaná prerekvizita (ne něco,
čemu se runtime kód snaží vyhnout), `git rev-parse HEAD` a `git status
--porcelain` je v pořádku volat přímo za běhu CLI (na rozdíl od
dřívějších verzí plánu, které se tomu vyhýbaly kvůli cílené
distribuovatelnosti mimo checkout). `research_manifest.json` (host
manifest, `src/coinjoin_pipeline/manifest.py`) dostane nová pole:
```json
{
  "coinjoin_pipeline_git_commit": "<full SHA nebo null, pokud .git chybí>",
  "coinjoin_pipeline_git_dirty": true,
  "host_python_version": "3.12.3",
  "host_s5cmd_version": "v2.3.0-...",
  "host_kubectl_version": "v1.31.2",
  "host_container_runtime_version": "Docker version 27.3.1, ..."
}
```
Špinavý checkout běh nezastavuje — jen se to jasně zaznamená do
provenance, aby bylo při zpětné analýze vidět, že výsledek nemusí
odpovídat žádnému konkrétnímu commitu.

**Proč přibývají i verze nástrojů: tenhle plán ruší pinning, který dneska
zajišťoval wrapper image, a bez záznamu by ta ztráta byla neviditelná.**
Dnešní `Dockerfile` staví na `docker:29-cli` a pinuje `S5CMD_VERSION=2.3.0`
(stažený a ověřený při buildu) plus Alpine `kubectl`; každý běh tedy sahal
na známé verze bez ohledu na to, jak vypadal hostitel. Po přechodu na bare
wrapper se použije to, co je zrovna na frontendu v `PATH` — a kapacitní
model v sekci 6 kontroluje jen **přítomnost** (`S5CMD_FRONTEND`, `KUBECTL`),
ne verzi. Pro nástroj, jehož výstup je evidence do diplomové práce, je
minimum zaznamenat skutečně použité verze do manifestu; hodnoty se získají
tam, kde se stejně už dělá capability preflight (`s5cmd version`,
`kubectl version --client`, `<runtime> --version`), takže to nestojí žádné
volání navíc. Pokud nástroj chybí, pole je `null` — zapisuje se stav, běh
se kvůli tomu nezastavuje (to řeší capability preflight).

Případná tvrdší varianta (minimální požadovaná verze `s5cmd`) se **v tomhle
plánu nezavádí** — jen záznam. Zavést spodní hranici má smysl teprve
tehdy, až se ukáže, že nějaká starší verze `s5cmd sync`/`--exclude` chová
jinak; do té doby by to byla jen další podmínka bez zjištěné příčiny.

### 8. Dokumentace

README/MIGRATION.md musí jasně říct: instalace je
`pip install -e .`/`pipx install --editable .` z checkoutu; `cjp`/
`coinjoin-pipeline` odkazuje na soubory v tomhle checkoutu; není k
dispozici samostatný přenositelný wheel. `docs/coinjoin-pipeline-architecture.md`
§2/§9 přepsat — dvouvrstvá host/wrapper architektura zůstává koncepčně
stejná, jen bez kontejnerového obalu kolem wrapperu a bez
`PBS_FRONTEND_DIRECT` jako volitelného módu.

## Akceptační grep

```
git grep -n 'WRAPPER_IMAGE' -- ':!MIGRATION.md' ':!coinjoin-runs' ':!emulation_logs'
git grep -n 'WRAPPER_PULL_POLICY' -- ':!MIGRATION.md'
git grep -n 'PBS_FRONTEND_DIRECT' -- ':!MIGRATION.md'
git grep -n 'PBS_FRONTEND_WRAPPER_ROOT' -- ':!MIGRATION.md'
git grep -n '/app/wrapper.py' -- ':!MIGRATION.md'
git grep -n '/app/exporters' -- ':!MIGRATION.md'
git grep -n 'coinjoin-pipeline-image' -- ':!MIGRATION.md'
git grep -n 'launcher_command' -- ':!MIGRATION.md'
git grep -n 'EXPORTERS_FROM_IMAGE' -- ':!MIGRATION.md'
```
Očekávaný výsledek: nic mimo `MIGRATION.md`/historickou provenance ve
starých run adresářích.

## Testy

- **Python 3.8 kompatibilita `pipeline/exporters/` proti skutečnému
  `blocksci-complete` — trvalý CI gate, ne jednorázová kontrola.**
  Exportéry se v tomhle plánu vůbec nemění a `blocksci-complete` taky ne
  (Python 3.8.20 beze změny, viz "Scope repozitářů"), takže tohle nic
  neopravuje — ale bez automatizovaného gate by budoucí drobná úprava
  `pipeline/exporters/` mohla nepozorovaně zavést 3.9+/3.10+ syntaxi a
  potichu rozbít bind-mount cestu do BlockSci. Spustit přímo proti
  publikovanému `blocksci-complete`, ne proti jakémukoli novému image:
  ```bash
  docker run --rm -v "$PWD/pipeline:/runtime:ro" -e PYTHONPATH=/runtime \
    "$BLOCKSCI_IMAGE" python3 -m compileall -q /runtime/exporters
  docker run --rm -v "$PWD/pipeline:/runtime:ro" -e PYTHONPATH=/runtime \
    "$BLOCKSCI_IMAGE" python3 /runtime/exporters/blocksci_export/analysis.py --help
  docker run --rm -v "$PWD/pipeline:/runtime:ro" -e PYTHONPATH=/runtime \
    "$BLOCKSCI_IMAGE" python3 /runtime/exporters/unified_report.py --help
  ```
- **Reálný lokální `full-run --driver docker`** (ne dry-run) — hlavní
  gate. Ověří, že přímý exec z checkoutu, peer-kontejnery
  (emulator/coinjoin-analysis/blocksci přes `compose.yaml`), a exportér
  bind-mount fungují beze změny chování oproti dnešku.
- **Scénářový a notebookový mount ukazuje na správný strom** (regrese
  popsaná v sekci 1). Unit test na to, jaké `SCENARIOS_DIR`/`NOTEBOOKS_DIR`
  CLI sestaví, a shell assert, že adresář namountovaný do emulátoru
  obsahuje `overactive-local.json` i `overactive-k8s-small.json` — tedy
  že to **není** `pipeline/client/scenarios/` (zastaralý pár bez nich).
  Doplňkově test, že `analysis.sh`/`emulate.sh` hodnotu z prostředí
  respektují: spustit je s předem nastaveným `SCENARIOS_DIR`/`NOTEBOOKS_DIR`
  a ověřit, že ji nepřepíšou (dnes přepíšou — `analysis.sh:8-9`,
  `emulate.sh:15`).
- **Lokální `full-run` skončí sám a nespustí Jupyter.** `full-run --driver
  docker` musí doběhnout bez zásahu a `blocksci_analyzer` po
  deterministických exportech skončit — ne zůstat běžet v interaktivním
  prostředí. Nejlevnější assert: v logu stage se objeví `Skipping
  interactive BlockSci environment`, a `docker ps` po doběhnutí neobsahuje
  `blocksci_analyzer`. Bez převzetí `BLOCKSCI_LAUNCH_JUPYTER` (sekce 1)
  tenhle test **zatuhne**, což je přesně ta regrese, kterou má chytit;
  proto ho spouštět s timeoutem, ne bez něj.
- **`tests/test-command-builder-contract.sh` prochází s novými flagy.**
  `--uploader-image`/`--unified-report-image` jsou v `command_metadata.json`
  (wrapper passthrough vrstva) a **nejsou** v `HOST_VALUE_OPTIONS` —
  parita metadat a parseru je přesně to, co tenhle test hlídá.
- **Pracovní adresář se nemění.** `cjp full-run --dry-run --scenario
  ./relativni/cesta.json` a `cjp runs list` spuštěné z jiného adresáře než
  checkout musí relativní cestu vyhodnotit vůči adresáři uživatele, ne
  vůči checkoutu — ani wrapper, ani `-m client.research` nedostávají `cwd`
  override (sekce 1). Regresní test proti pokušení „přidat `cwd=runtime_root`",
  které by relativní cesty tiše rozvázalo.
- **Nahrané exportéry neobsahují bytecode.** Po staging kroku
  `s5cmd ls "$ARTIFACT_URI/$RUN_ID/.pipeline/exporters/*"` nesmí vrátit
  žádný `.pyc` ani cestu s `__pycache__`. Test má smysl spustit
  **poté**, co se v checkoutu bytecode záměrně vyrobí (`python3 -m
  compileall pipeline/exporters`), jinak projde i bez `--exclude` filtrů.
- **`--driver podman` bez host Docker socketu** — ověří, že zmizelo
  socket-mount/forwarding zjednodušení nerozbilo podman cestu
  (`test-podman-no-host-docker.sh`).
- **Ctrl-C uprostřed `full-run` (SIGINT na wrapper.py proces) nesmí
  nechat běžet žádný pojmenovaný peer kontejner** (`blocksci_analyzer`,
  `coinjoin_analysis`, `emulator_manager`, `btc_data_wiper`,
  `dind_image_prefetch`, `isolated_docker_daemon`) — ověřuje, že
  odstranění launcher.sh's outer `cleanup()` safety netu neponechává
  osiřelé kontejnery. `docker ps`/`podman ps` po přerušení musí být
  prázdné od těchhle jmen. **Stejný test se musí spustit i s `kill -TERM`,
  ne jen `kill -INT`** — na SIGTERM neběží `atexit`, takže bez nového
  handleru zůstane viset i lock soubor (`wrapper.py:347`), ne jen
  kontejnery; varianta jen se SIGINT by tuhle půlku regrese neodhalila.
- **Kubernetes driver** (`test-kubernetes-k3d.sh`) a **PBS/S3**
  (`test-local-pbs-analysis.sh`, `test-kubernetes-s3-minio.sh`) — ověřit
  bez `PBS_FRONTEND_DIRECT` env vůbec (je to teď jediné chování, ne
  volitelné).
- Unit testy: `runtime_root()` má jen jednu větev a vrací jasnou
  chybu (ne `None` tiše), když checkout není nalezen; `host.py`/
  `images.py` už neznají `"pipeline"` komponentu; `research_manifest.json`
  obsahuje `coinjoin_pipeline_git_commit`/`_git_dirty` i
  `host_python_version`/`host_s5cmd_version`/`host_kubectl_version`/
  `host_container_runtime_version` (a `null` tam, kde nástroj chybí, bez
  pádu); `images.wrapper`/
  `image_digests.wrapper` čtou staré reporty správně a u nových běhů
  jsou `null`.
- **`required_capabilities(action, arguments)` pro každý řádek kapacitní
  tabulky v sekci 6** (ne v sekci 5 — tam je otevřená otázka kolem
  `host.docker.internal`) — konkrétně: `full-run --artifact-backend s3` **nevrací**
  `CONTAINER_RUNTIME` (dřívější regrese, kterou by prosté smazání
  `PBS_FRONTEND_DIRECT` způsobilo); `qsub` preflight běží pro každou akci
  s `--*Pbs`/`pbs-from-s3`, ne jen když byl nastavený zrušený env var.
- **S3 upload pořadí:** `kubernetes_s3_auth_preflight` proběhne
  **před** `ensure_empty_run_prefix`/uploadem, ne po nich (regresní test:
  simulovaný neúspěšný K8s auth preflight nesmí zanechat žádné exportéry
  na S3); upload doběhne úspěšně **předtím**, než se Kubernetes Job
  vůbec vytvoří; `full-run --artifact-backend s3` a samostatné `emulate
  --artifact-backend s3` procházejí **stejnou** sdílenou staging funkcí
  (dnes tomu tak není — ověřit, že se to nevrátilo); frontendový
  `ensure_empty_run_prefix` **nemá žádnou výjimku** a správně selže,
  pokud run prefix obsahuje cokoli — včetně starých
  `.pipeline/exporters/**` z předchozího nedokončeného pokusu (regresní
  test na přesně tenhle scénář: napůl nahrané staré exportéry pod
  stejným run-id musí staging odmítnout, ne přeskočit); teprve in-pod
  `prefix_preflight` (běžící *po* uploadu) ignoruje
  `.pipeline/exporters/**`, ale selže na jakémkoli jiném obsahu; upload
  používá absolutní cestu odvozenou z `EXPORTERS_DIR`, ne relativní
  `pipeline/exporters/` (regresní test: spuštění `cjp` z jiného pracovního
  adresáře než checkout musí exportéry nahrát správně); selhání uploadu
  Job vůbec nespustí a skončí jasnou chybou (bez
  retry/resume logiky).
- **Kontrola vstupních bodů exportérů na obou stranách.** Frontend:
  `EXPORTERS_DIR` bez `unified_report.py` (resp. bez `blocksci/analysis.py`)
  musí skončit chybou `Required exporter is missing: ...` **před** jakýmkoli
  zápisem na S3 — regresní test ověří, že run prefix po takovém selhání
  zůstal prázdný. In-pod: `prefix_preflight` musí selhat, když v
  `.pipeline/exporters/` některý z těch dvou souborů chybí, i když je
  prefix jinak "čistý" (tj. samotná výjimka pro `.pipeline/exporters/**`
  nesmí stačit k projití).
- **Nová image provenance dorazí až do unified reportu.** Po
  `full-run --artifact-backend s3` obsahuje report
  `images.uploader`/`images.unified_report` a odpovídající
  `image_digests` položky; unit test na `digest_from_reference` pokrývá
  pinnutou referenci (`...@sha256:<64 hex>` → `sha256:<64 hex>`), plovoucí
  tag (→ `None`) i `None` na vstupu. Test musí běžet **bez dostupného
  Docker daemonu** (odvození je čistě textové) — to je ta vlastnost, na
  které stojí MetaCentrum frontend.
- **Image smoke testy pro nové/náhradní reference:** `docker run
  <uploader-image> kubectl version --client`, `... s5cmd version`;
  `docker run <pinned-alpine> sh -c 'echo ok'`.
- **`container/unified-report.image` lock soubor neobsahuje `docker://`
  prefix** — unit test na formát (`^[^:/]+.*@sha256:[0-9a-f]{64}$` bez
  `docker://` substringu) plus funkční test, že `docker run
  "$(cat container/unified-report.image)"` funguje beze změny (Docker
  odmítne `docker://`-prefixovaný název image jako neplatný).
- **Smoke test pro pinned Python image musí spustit skutečný importní
  strom, ne jen dokázat, že `python3` existuje.** `python3 -c
  'print("ok")'` neověří nic o `unified_report.py`'s závislostech
  (`report_builder.py`, `markdown_report.py`, `comparison.py`,
  `heuristics.py`, `manifest.py`, `common.py`, ...). **A musí jít přes
  `bash -c`, ne volat `python3` přímo** — sekce 3(b) tvrdí, že image
  potřebuje `python3` **i** `bash -c`, a produkční šablona
  (`unified_report_s3_template.sh:59-63`) skutečně volá
  `... "$IMAGE" bash -c 'cd "…" && {command}'`. Přímé volání `python3` by
  přítomnost `bash` v image vůbec neověřilo a testovalo by jinou strukturu
  invokace, než jaká na PBS poběží. Správně:
  ```
  docker run --rm -v "$PWD/pipeline/exporters:/mnt/exporters:ro" -w /mnt \
    <pinned-python-image> \
    bash -c 'python3 /mnt/exporters/unified_report.py --help'
  ```
  a pro PBS navíc přes stejný mechanismus, co skutečně používá Singularity —
  **doslova `singularity exec`, ne `apptainer exec`**, protože
  `pipeline/client/unified_report_s3_template.sh:59` volá `singularity exec`;
  smoke test, který by testoval jiný binárník než produkční šablona,
  neověřuje produkční cestu:
  ```
  singularity exec --bind "$PWD/pipeline/exporters:/mnt/exporters:ro" \
    "docker://<pinned-python-image>" \
    bash -c 'python3 /mnt/exporters/unified_report.py --help'
  ```
  Nejpřesnější je strukturu příkazu zkopírovat rovnou z
  `unified_report_s3_template.sh` (včetně `cd` do pracovního adresáře uvnitř
  `bash -c`), ať se smoke test a produkční šablona nemůžou rozejít.
- **K8s S3 test musí ověřit přesné pořadí, ne jen že exportéry
  dorazí:** upload exportérů proběhne a doběhne **předtím**, než se
  Kubernetes Job vůbec vytvoří; `prefix_preflight` uvnitř podu ignoruje
  `.pipeline/exporters/**`, ale selže na jakémkoli jiném existujícím
  obsahu prefixu; uploader pod nikdy nesahá na exportéry, jen na
  emulační data; selhání uploadu Job vůbec nespustí. PBS strana dál
  stahuje ze stejné S3 cesty beze změny.
- `tests/test-run-pipeline-image.sh` se maže (testoval odstraněnou
  funkcionalitu).
- Povinná finální brána: `./tests/test-kubernetes-s3-minio.sh`.

## Pořadí implementace

Bezpečné pořadí drží `main` funkční po každém kroku — pozdější kroky
záměrně nezačínají, dokud dřívější hard gates neprojdou:

**Nultý úkol kroku 2: řádkový audit `launcher.sh`.** Opakovaně se ukazuje,
že `resources/container/launcher.sh` (332 řádků) nedělá jen mechaniku
kontejneru — **počítá defaulty a normalizuje argumenty**, a každá revize
tohohle plánu našla další takový případ (nejdřív `NOTEBOOKS_DIR`/
`SCENARIOS_DIR`, pak `BLOCKSCI_LAUNCH_JUPYTER`). Hledat je ad hoc je
neohraničená úloha; projít soubor řádek po řádku je ohraničená. Každou
proměnnou, kterou launcher nastavuje nebo odvozuje, proto zařaď do jedné
ze dvou kategorií a výsledek zapiš:

- **mechanika kontejneru** — zaniká se souborem (socket setup,
  `--add-host`, `docker cp` extrakce, `WRAPPER_PULL_ARGS`,
  `INNER_CONTAINER_RUNTIME`, `POST_WRAPPER_SHELL`, `EXPORTERS_FROM_IMAGE`,
  `WRAPPER_SCRIPT`, …);
- **default nebo normalizace, kterou musí převzít env kontrakt v sekci 1**
  (`NOTEBOOKS_DIR`, `SCENARIOS_DIR`, `BLOCKSCI_LAUNCH_JUPYTER`, …).

Je to levné teď a drahé po smazání souboru — jakmile `launcher.sh`
zmizí, chybějící default se projeví až chybou za běhu, bez místa, kde ho
dohledat.

1. **Hard gates** (baseline, nic se zatím neodstraňuje): Python 3.8
   exporter test proti skutečnému `blocksci-complete`, smoke test pinned
   `--unified-report-image` kandidáta (Docker i Singularity).
2. **Checkout runtime**: **nejdřív úplný audit `launcher.sh`** (viz
   níže — bez něj se do dalších kroků protáhne další nezachycený default),
   pak `pyproject.toml` `_runtime` mappings pryč,
   `runtime_root()` implementace + přejmenování, explicitní
   `PYTHONPATH`/env-var invokace, `SCENARIOS_DIR`/`NOTEBOOKS_DIR` a
   `BLOCKSCI_LAUNCH_JUPYTER` kontrakt
   (`${VAR:-}` v `analysis.sh`/`emulate.sh` + explicitní hodnoty z CLI,
   sekce 1), research routing přes `-m client.research`, kapacitní
   preflight model včetně smazání tvrdé `PBS_FRONTEND_DIRECT` validace v
   `cli.py:155-163` a úpravy dotčených testů (sekce 6).
   **Uvnitř tohohle kroku platí pevné pořadí:** nejdřív `SIGINT`/`SIGTERM`
   handler s idempotentním cleanupem ve `wrapper.py` (dnes tam žádný
   `signal` není — je to jistá práce, ne kontingence), pak SIGINT/SIGTERM
   test a `test-kubernetes-k3d.sh` (shared-storage, bez launcherového
   `--add-host`), a **teprve po jejich úspěchu** se `cli.py` přepne na bare
   wrapper jako jediné, bezpodmínečné chování — ne až před fyzickým
   smazáním launcheru později. Pokud `test-kubernetes-k3d.sh` selže,
   oprava (`--kubernetes-control-ip`, sekce 5) se doplní tady, před
   přepnutím.
3. **S3 staging**: sdílená staging funkce pro `full-run`/`emulate`
   (upload exportérů přímo na S3 před vytvořením Jobu, s `--exclude`
   filtry na `__pycache__`/`*.pyc`, žádný stavový
   automat/marker/resume; frontendový `ensure_empty_run_prefix` beze
   změny, bez výjimky — a s vědomím, že pro samostatné `emulate` je to
   nová fail-closed podmínka, viz sekce 3), in-pod `prefix_preflight` s výjimkou pro
   `.pipeline/exporters/**` (jediné místo, kde tahle výjimka existuje),
   nový `uploader` image + lock soubor.
4. **Odstranění wrapperu**: launcher(y), `Dockerfile`, socket forwarding,
   `pipeline_image.py`, `commands.py:launcher_command` a jeho testy,
   `PBS_FRONTEND_DIRECT` — bezpečné, protože riziko (SIGINT,
   `host.docker.internal`) už bylo ošetřeno v kroku 2 před přepnutím;
   tohle jen maže kód, na který už nic needukazuje.
5. **Provenance a úklid**: schema, dokumentace, publish workflow,
   zbývající integrační testy, akceptační grep.

## Akceptační kritéria

Wrapper image se nebuilduje ani nepublikuje. `PBS_FRONTEND_DIRECT`
neexistuje jako flag — bare spuštění z checkoutu (přes `sys.executable`,
ne natvrdo `python3`) je jediné chování pro všechny drivery (Docker,
Podman, Kubernetes, PBS/S3). Instalace je editable (`pip install -e .`/
`pipx install --editable .`) a je zdokumentovaná jako svázaná s
checkoutem — žádné tvrzení o samostatně distribuovatelném wheelu.
Wrapper i `runs`/`external`/`scenarios` (`-m client.research`) se
spouštějí s explicitním `PYTHONPATH`, ale **bez jakékoli změny pracovního
adresáře** — `RuntimeCommand`/`process.run()` `cwd` nepředávají a
nerozšiřují se o něj, takže uživatelem zadané relativní cesty dál vychází
z adresáře, odkud běží `cjp`.
`pipeline/` zůstává na svém současném místě, beze změny importního
stylu; `pyproject.toml`'s neúplné `_runtime`/`_runtime.client`/
`_runtime.exporters` package mappings jsou odstraněné, ne doplněné —
needitable `pip install .` dá jasnou chybu při pokusu o `full-run`, ne
částečně funkční wheel. Exportéry samy se nemění, ale mají trvalý CI
gate ověřující kompatibilitu se skutečným `blocksci-complete`'s Pythonem
3.8.20. **Nevznikají žádné Python/BlockSci runtime images** — jen jeden
malý `uploader` image (bash+kubectl+s5cmd, žádné exportéry, žádný
wheel; vlastní Dockerfile + committnutý `container/uploader.image` lock
soubor s plnou immutable referencí, aktualizovaný explicitním
build→push→digest→commit procesem, ne automaticky; `--uploader-image`/
`COINJOIN_UPLOADER_IMAGE` override) a dvě pinned veřejné reference
(`--unified-report-image`/`COINJOIN_UNIFIED_REPORT_IMAGE` s
`container/unified-report.image` lock souborem — **neutrální OCI
reference bez `docker://` prefixu**, funkční přímo jako `docker run`
argument, s prefixem doplněným až `singularity exec` voláním na PBS straně — pro
PBS report krok; interní `BTC_VOLUME_HELPER_IMAGE` konstanta, bez CLI
flagu, pro `populate_btc_data_volume`), žádný vlastní build ani publish
workflow pro tyhle dvě — jejich pinned digesty se přesto ověřují
formátovým i funkčním smoke testem v CI (viz Testy), ne ponechávají bez
kontroly. Exportéry se na S3 nahrávají přímo z wrapperu (frontend-side,
přímo z `pipeline/exporters/`, bez dočasného snapshotu, ale **s
`--exclude` filtry na `__pycache__`/`*.pyc`**, aby se hostitelský bytecode
nedostal do `blocksci-complete`'s Pythonu 3.8 ani do pinnutého
`python:3.12`) přes **jednu
sdílenou staging funkci použitou jak `full-run --artifact-backend s3`,
tak samostatným `emulate --artifact-backend s3`** — vědomě
zjednodušeno na jediný krok "ověř **úplně prázdný** prefix (frontendový
`ensure_empty_run_prefix`, beze změny, bez výjimky) → ověř vstupní body →
nahraj → vytvoř
Job", bez resumovatelného stavového automatu, bez JSON markeru, bez
deduplikace/opětovného připojování k existujícímu Jobu. Místo markeru s
hashem stojí dvojice kontrol vstupních bodů: frontend odmítne
`EXPORTERS_DIR` bez `unified_report.py`/`blocksci/analysis.py` ještě před
zápisem na S3, a in-pod `prefix_preflight` tytéž dva objekty pozitivně
ověří na cíli — samotná výjimka pro `.pipeline/exporters/**` k projití
nestačí. Zachytí to prázdný nebo zjevně neúplný upload před zahájením
emulace; **úplná integrita celého exporter stromu se transakčně
neověřuje** a je to vědomé omezení neresumovatelného staging modelu, ne
opomenutí. Výjimka pro
`.pipeline/exporters/**` existuje **jen** v in-pod `prefix_preflight`,
který běží *po* uploadu — nikdy ve frontendové kontrole, která běží
*před* ním (jinak by napůl selhaný upload z předchozího pokusu zanechal
staré exportéry, které by další pokus tiše ignoroval místo aby je
odmítl). Selhání kdekoli v tomhle kroku končí jasnou chybou a vyžaduje
nový `--run-id` nebo ruční cleanup, ne
automatický retry. Hash nahrávaného stromu se může zaznamenat do
provenance jako informační záznam, ale neřídí, jestli Job vznikne.
Přepsaný `prefix_preflight` uvnitř podu ignoruje `.pipeline/exporters/**`,
ale selže na jakémkoli jiném existujícím obsahu prefixu přesně jako dnes.
K8s uploader se stará jen o emulační data, nikdy o exportéry. Preflighty
jsou řízené
`required_capabilities(action, arguments)` odvozenými z akce, ne z
`PBS_FRONTEND_DIRECT` env var — `full-run --artifact-backend s3` na
MetaCentru nevyžaduje `CONTAINER_RUNTIME` capability a neselže na
chybějícím Dockeru; `qsub` dostupnost se ověřuje pro každou akci, co ho
skutečně potřebuje, ne jen když byl nastavený zrušený env var. `cli.py`
se nepřepne na bezpodmínečné `runtime_root()`+přímý exec (a shell
launcher se fyzicky nesmaže), dokud SIGINT integrační test nepotvrdí, že
žádný pojmenovaný peer kontejner nepřežije Ctrl-C uprostřed lokálního
běhu — gate sedí na přepnutí chování, ne až na pozdějším smazání
souborů. Oba pre-cutover gaty (SIGINT/SIGTERM i `test-kubernetes-k3d.sh`)
spouštějí **bare runtime command vyrenderovaný z `cli.py:runtime_command()`**,
ne `cjp` — přes `cjp` by před cutoverem pořád běžel launcher a testy by
ověřovaly jeho `cleanup()`/`--add-host`, ne nové chování.
`blocksci-complete` a `blocksci` repo jsou zcela nedotčené. Staré
`images.wrapper`/`image_digests.wrapper` provenance zůstávají čitelné,
nové běhy je mají `null`; unified report nově obsahuje
`images.uploader`/`images.unified_report` (a odpovídající digesty, kde
dostupné) — předávané exportérům jako `--uploader-image`/
`--uploader-image-digest`/`--unified-report-image`/`--unified-report-image-digest`
přesně tam, kde dnes stojí zrušené `--wrapper-image*`, s digestem
odvozeným **textově z pinnuté reference** (`digest_from_reference`), tedy
bez Docker daemonu a použitelně i na MetaCentrum frontendu. Pinned Python image pro PBS report krok je přijatý jen po
úspěšném `unified_report.py --help` smoke testu (Docker i Singularity), ne
předpokládaný. Akceptační grep nenajde nic mimo `MIGRATION.md`/historickou
provenance — což znamená, že `--wrapper-image*` flagy a `WRAPPER_IMAGE`
env mizí i z `pipeline/exporters/cli.py`, `pipeline/exporters/blocksci_export/analysis.py`
a `pipeline/compose.yaml`, ne jen z `wrapper.py` (varianta "nechat flagy a
plnit je `null`" je tím vyloučená). `research_manifest.json` nově obsahuje git commit a dirty
stav checkoutu **plus verze skutečně použitých hostitelských nástrojů**
(`host_python_version`, `host_s5cmd_version`, `host_kubectl_version`,
`host_container_runtime_version`) — náhrada za pinning, který dosud
zajišťoval wrapper image (`S5CMD_VERSION=2.3.0`, Alpine `kubectl`) a který
bare běh ruší. `SCENARIOS_DIR`/`NOTEBOOKS_DIR` posílá hostitelské CLI
explicitně a `analysis.sh`/`emulate.sh` je respektují (`${VAR:-}`), takže
mountovaný scénářový strom zůstává ten kořenový (`overactive-local.json`
přítomný), ne zastaralý `pipeline/client/scenarios/`.
`BLOCKSCI_LAUNCH_JUPYTER` posílá hostitelské CLI s výchozí hodnotou `0`,
takže neinteraktivní běh po deterministických exportech skončí a nespustí
interaktivní BlockSci prostředí (`POST_WRAPPER_SHELL` zaniká bez náhrady).
`--uploader-image`/`--unified-report-image` jsou wrapper passthrough flagy
v `command_metadata.json`, ne hostitelské options v `HOST_VALUE_OPTIONS`, a
nedoplňují se do `add_effective_image_arguments` — plovoucí tagy u nich
hlídá lock soubor. Před smazáním `launcher.sh` proběhl řádkový audit, který
každou jím nastavovanou proměnnou zařadil buď do „mechanika kontejneru,
zaniká", nebo do „default, který přebírá env kontrakt". Reálný lokální `full-run --driver docker`, Podman bez
socket forwarding, a finální Kubernetes S3/MinIO test (s ověřeným
pořadím upload→Job a bez prefix-preflight regrese na `.pipeline/exporters/**`)
projdou.
