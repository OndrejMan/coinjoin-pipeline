# Self-review implementace odstranění wrapper image

Kritické review vlastního diffu (session 1-4), **bez oprav**. Doplněk k
`wrapper-removal-implementation-log.md`, který popisuje co je hotové;
tenhle soubor popisuje, co je na tom podezřelé.

Stav při psaní: `pytest` 362 · `unittest` 41 · `ruff` clean · 6 rychlých
shell testů PASS · žádný dlouhý test nespuštěn.

---

## H1 — `prefix_preflight` závisí na neověřeném formátu `s5cmd ls`

**Kde:** `pipeline/client/kubernetes.py`, in-pod `prefix_preflight`.

Přepsal jsem kontrolu z „jakýkoli objekt → selhat" na filtrování výpisu:

```bash
listing="$(s5 ls "$ARTIFACT_URI/$RUN_ID/*" 2>&1)"
unexpected="$(printf '%s\n' "$listing" | grep -v '\.pipeline/exporters/' || true)"
```

Původní kód se na obsah výpisu **nedíval vůbec** — stačil mu exit status.
Můj ho parsuje, čímž vzniká závislost na dvou nezjištěných věcech:

1. Jestli `s5cmd ls` s wildcardem vypisuje klíče včetně cesty
   (`.pipeline/exporters/unified_report.py`), nebo jen poslední segment.
2. Jestli `*` vůbec matchuje přes `/`. Pokud ne, `.pipeline/` se objeví
   jako **`DIR` řádek**, ten neprojde `grep -v '\.pipeline/exporters/'`,
   `unexpected` bude neprázdné a **preflight selže po každém úspěšném
   stagingu** — tedy každý S3 běh.

**Proč jsem to neověřil:** `s5cmd` na tomhle stroji není.

**Kde se to projeví:** `tests/test-kubernetes-s3-minio.sh`, hned v prvním
S3 běhu. Snadno rozpoznatelné podle hlášky „run prefix … already contains
artifacts" u čerstvého run-id.

**Levnější varianta, kdyby to selhalo:** filtrovat na `\.pipeline/`
místo `\.pipeline/exporters/` (nic jiného se tam nestageuje), nebo se
vrátit k exit-status sémantice a spolehnout se jen na pozitivní kontrolu
entrypointů níž v tomtéž skriptu.

---

## H2 — Regrese v pokrytí image preflightu pro `full-run --analysisPbs`

**Kde:** `src/coinjoin_pipeline/host.py:required_image_components`.

Odstranil jsem podmínku na `PBS_FRONTEND_DIRECT` a nechal větev
nepodmíněnou:

```python
if any(flag in arguments for flag in ("--analysisPbs", "--blocksciPbs", "--mappingsPbs")):
    return set()
```

Dřív tahle větev platila **jen** když bylo `PBS_FRONTEND_DIRECT=1`, tedy
na MetaCentru. Na běžném stroji `full-run --analysisPbs` (shared-storage)
spadl do plné sady a preflightoval emulátor/blocksci/analysis.

Naměřeno:

| akce | images | capabilities |
|---|---|---|
| `full-run --engine wasabi` | blocksci, coinjoin_analysis, emulator | CONTAINER_RUNTIME |
| `full-run --engine wasabi --analysisPbs` | **prázdné** | CONTAINER_RUNTIME, QSUB |

Tedy: kontroluje se daemon, ale **ne images** — přestože u
shared-storage `full-run` emulace pořád běží lokálně přes docker.
Chybějící emulátor image se projeví až pádem uprostřed běhu místo
v preflightu.

Kapacitní model (`doctor.py`) tuhle situaci řeší správně
(`delegated = uses_s3 or (uses_pbs and action not in {"full-run","emulate"})`),
ale `required_image_components` stejnou úvahu nedostal.

**Oprava by byla malá:** stejné rozlišení jako v `required_capabilities` —
pro `full-run`/`emulate` na shared-storage nevracet prázdnou množinu jen
kvůli PBS flagům.

---

## M1 — Nová image provenance je zatím vždy `null`

`digest_from_reference()` čte digest z reference; oba lock soubory ale
obsahují plovoucí tag (`…-uploader:latest`, `python:3.12-slim-bookworm`),
takže vrací `None`. Důsledek: `image_digests.uploader` i
`image_digests.unified_report` budou v **každém** reportu `null`, dokud se
digesty nedoplní.

Není to chyba kódu — je to důsledek toho, že uploader ještě není
publikovaný. Ale znamená to, že **provenance feature je fakticky neaktivní**
a nikdo si toho podle reportu nemusí všimnout, protože `null` vypadá jako
legitimní „nedostupné".

---

## M2 — Nesouvisející přeuspořádání importů v `unified_report.py`

Spustil jsem `ruff check --fix` na celý `pipeline/`, což přerovnalo importy
v souboru, kterého se tahle práce jinak netýká:

```diff
-from exporters.blocksci import detector as _blocksci_export
 from exporters import cli as _cli
 from exporters import integration_diagnostics as _integration_diagnostics
+from exporters.blocksci import detector as _blocksci_export
```

`unified_report.py` má nahoře `try/except ImportError`, `sys.path.append`
a několik `import *`. Přehazovat pořadí importů zrovna tam není bez rizika
a **nemá to s odstraněním wrapperu nic společného** — je to scope creep,
který jsem způsobil plošným `--fix`.

Riziko je malé (mezi přesunutým importem a zbytkem není zjevná závislost),
ale patří to k revertnutí, ne k obhajobě.

---

## M3 — Odchylka od plánu: `images.wrapper` zmizelo, místo aby bylo `null`

Plán (sekce 4) říká, že parser **zůstane schopný číst** staré hodnoty ze
starých reportů a nové běhy zapíšou `null`. Já jsem místo toho
`images.wrapper`/`image_digests.wrapper` odstranil z
`MANIFEST_COMPARE_FIELDS` úplně a nahradil je
`uploader`/`unified_report`.

Praktický dopad: starý report tu hodnotu pořád má v JSONu, ale
`compare_run_manifests` ji už nikde nezobrazí ani neporovná. Pro srovnání
starého a nového běhu to znamená tichou ztrátu jednoho pole.

Je to obhajitelné (mrtvé pole navíc), ale je to **vědomá odchylka od
plánu, kterou jsem nikde nezaznamenal** — až teď.

---

## L1 — Klíčové defaulty kryté jen shell testem

`BLOCKSCI_LAUNCH_JUPYTER=0` a `PYTHONDONTWRITEBYTECODE=1` jsou dvě
nejdražší chyby, jaké tahle změna mohla způsobit (zaseknutý běh v Jupyteru,
bytecode v exportérech na S3). Ověřuje je ale **jen**
`tests/test-runIt-overactive-local.sh` grepem na vyrenderovaný příkaz.

Unit test nad `commands.runtime_environment()` by byl odolnější a levnější
(běží v `pytest`, nezávisí na `runIt.sh`). Chybí.

---

## L2 — `full-run --artifact-backend s3` bez PBS flagů nedostane `QSUB`

`required_capabilities` přidá `QSUB` jen když je přítomný některý
`--*Pbs` flag nebo akce je `pbs-from-s3`. `run_full_run_s3` ale volá
`require_qsub()` **nepodmíněně**. Kombinace `--artifact-backend s3` bez
PBS flagů tedy projde preflightem a spadne až ve wrapperu.

Okrajové (S3 full-run bez PBS etap nedává moc smysl), ale je to
nekonzistence mezi tím, co preflight slibuje, a co wrapper vyžaduje.

---

## L3 — Signálový cleanup nesahá na Kubernetes Job

`cleanup_peer_containers()` zastaví šest compose kontejnerů. Při přerušení
běhu s `--driver kubernetes` zůstane Job v clusteru.

**Není to regrese** — launcherův `cleanup()` dělal přesně totéž (jen
compose kontejnery). Zaznamenávám jen proto, aby se to nepletlo s
„signálový handler je hotový", což platí jen pro lokální driver.

---

## Co review NEnašlo (ověřeno, v pořádku)

- Všechny moduly `coinjoin_pipeline.*` jdou importovat; žádné odkazy na
  smazané soubory (shody na `Dockerfile`/`_runtime` jsou cizí blocksci
  Dockerfily a substring `runtime`).
- `resolve_uploader_image(args)` je uvnitř `run_kubernetes_s3_emulation(args)`
  — `args` je v scope.
- Všechny čtyři `tests/pipeline/*.sh` procházejí, tedy `${VAR:-}` úprava
  `emulate.sh`/`analysis.sh` nerozbila jejich vlastní kontrakty.
- Exit kódy: `cli.py:220` normalizuje nezávisle na smazaném launcheru,
  `process.run` vrací 130 při přerušení — složení se nezměnilo.
- `credentials` se v přepsaném `prefix_preflight` mažou na všech cestách
  ven (ověřeno čtením všech větví).
- Pořadí `--runs-root`/`--runtime` v `research_command` odpovídá launcheru,
  takže uživatelovy vlastní hodnoty pořád vyhrávají (argparse last-wins).

---

## Doporučené pořadí, kdyby se to řešilo

1. **H1** — blokuje S3 cestu, projeví se hned v `test-kubernetes-s3-minio.sh`.
2. **H2** — tichá ztráta preflightu, projeví se až pádem uprostřed běhu.
3. **M2** — jednořádkový revert, odstraní nesouvisející změnu z diffu.
4. **M1/M3** — až se budou pinovat digesty, resp. při aktualizaci plánu.
5. **L1** — přidat unit test, nezávisle na zbytku.

---

## Stav oprav (2026-07-26)

| bod | stav | co se změnilo |
|---|---|---|
| H1 | opraveno | `kubernetes.py`: filtr `grep -v '\.pipeline'` místo `'\.pipeline/exporters/'` |
| H2 | opraveno | `host.py`: PBS flagy odečítají jen svoji komponentu, ne celou sadu |
| M1 | opraveno | `manifest.py`: fallback na `docker_image_digest` pro uploader/report |
| M2 | neplatné | mezitím proběhl rename `exporters/blocksci` → `blocksci_export`, importy jsou tedy součástí věcné změny |
| M3 | zamítnuto | zpětná kompatibilita se starými reporty se neřeší; `wrapper` pole zůstávají odstraněná |
| L1 | opraveno | `tests/unit/test_cli.py`: dva unit testy nad `runtime_environment()` |
| L2 | opraveno | `doctor.py`: `full-run --artifact-backend s3` vyžaduje `QSUB` i bez PBS flagů |
| L3 | ponecháno | není regrese, `cleanup()` launcheru dělal totéž |

**H1** — filtr teď snese obě podoby výpisu `s5cmd ls`: rekurzivní výpis
plných klíčů i řádek `DIR .pipeline/`, kdyby `*` nematchoval přes `/`.
Ověřeno bez `s5cmd` na stroji: `tests/pipeline/test_s3_backend.py` renderuje
skutečný init-container skript, přesměruje `/credentials` do tmp adresáře a
spustí ho proti stub `s5cmd`. Čtyři případy — plné klíče, `DIR` řádek,
znovupoužitý prefix (`.k8s/upload.done` → exit 1), chybějící exportéry
(exit 1). Starý filtr by na `DIR` variantě selhal (ověřeno samostatně).

**H2** — místo plošného `return set()` se od požadované sady odečítá jen to,
co daný flag skutečně deleguje (`PBS_DELEGATED_COMPONENTS`). Takže
shared-storage `full-run --analysisPbs` teď preflightuje `{emulator,
blocksci}`, `--analysisPbs --blocksciPbs` → `{emulator}`, čistě PBS etapa
(`coinjoin-analysis --analysisPbs`) pořád `set()`. S3 `full-run` zůstává
prázdný, protože emulace běží v clusteru.

**M1** — provenance už není nutně `null`: `digest_from_reference` zůstává
prvním zdrojem (frontend bez Dockeru), přibyl fallback na lokální
`docker image inspect`. Pinnutí digestů v lock souborech je pořád to
správné řešení, ale feature už není fakticky mrtvá.

**M3** — nebude se opravovat. Staré reporty ani zpětná kompatibilita
`compare_run_manifests` nejsou cíl; `images.wrapper` a
`image_digests.wrapper` zůstávají z `MANIFEST_COMPARE_FIELDS` odstraněné.
Odchylka od sekce 4 plánu je tím vědomá a schválená, ne nedopatření.

Testy po opravách: `pytest` 373 · `unittest` 229 · `ruff` clean ·
`mypy` beze změny (11 pre-existing chyb v `detector.py`/`cli.py`/
`emulator_data.py`) · `test-runIt-overactive-local.sh` a
`test-runIt-doctor.sh` PASS. Dlouhé S3/k3d testy nespuštěny.
