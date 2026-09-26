# Kontrola návrhu a opravy — 25. 9. 2026

Základ návrhu je pro výzkumnou pipeline rozumný: hostitelské CLI sestaví
požadavek, wrapper řídí běh, adaptéry provádějí práci přes Compose/Kubernetes/PBS
a exportéry skládají výsledky. Kód ale ještě není rovnoměrně čistý ani důsledně
typovaný. Nejslabší jsou opakované zpracování argumentů a chybové větve souběhu.
Kontrola našla konkrétní chyby v obou těchto oblastech; opravy zůstávají bez commitu.

**Stav 26. 9. 2026:** opravy z tabulky jsou commitnuté (`2406df1`…`2a9cd5c`),
kromě dry-runu — ten v kódu chyběl a byl dopsán dodatečně. Technický dluh
1–4 je zpracovaný, viz [Dokončení](#dokončení-26-9-2026).

## Rozsah a metoda

Výchozí pracovní strom `coinjoin-pipeline` byl čistý. Kontrola zahrnovala
hostitelské CLI, konfiguraci a image, předávání prostředí a manifestů,
deklaraci a vykonávání etap, sdílené PBS adaptéry, S3 submission/rollback,
základ katalogu běhů a strukturu reportových exportérů. Vychází z aktuálního
zdrojového kódu a cílených testů, nikoli z pravdivosti commit messages.

Nejde o kontrolu každého řádku všech shellových šablon, Kubernetes manifestů
a exportérů. Neběžela emulace, parsování, image build, skutečný PBS job ani
celá testovací sada. Starší přehled architektury v nadřazeném `docs/` stále
popisuje odstraněnou wrapper image; aktuální CLI spouští wrapper přímo z checkoutu.

## Opravené chyby

| Oblast | Před opravou | Po opravě a důkaz |
| --- | --- | --- |
| Opakované přepínače | Host četl první `--run-id`, `--engine`, `--driver` apod., zatímco argparse ve wrapperu poslední. Host tak mohl zapsat manifest pod jiným run ID nebo provést jiný preflight. | `commands.option_value` čte poslední výskyt včetně alternativních názvů; odstraněn druhý parser artifact backendu z `host.py`. Testy porovnávají hosta se skutečným wrapper parserem. |
| Lokální image | `--local-build --emulator-image emulator:custom` použil `coinjoin-emulator:local`; ignoroval také image z prostředí a nevalidoval jejich přepsání. | Explicitní image > prostředí > lokální výchozí image. Test zachytává prostředí skutečné hostitelské orchestrace s mockovaným spuštěním wrapperu. |
| PBS/S3 dry-run | Host při vykreslování dry-runu přepsal existující `research_manifest.json` a označil tuto neprovedenou operaci jako dokončenou. | Dry-run může spustit wrapper pro vykreslení příkazů, ale nezapisuje hostitelský manifest ani nepřipravuje runtime adresáře. Test zachová původní manifest přesně. |
| Pokračování po chybě paralelní etapy | Po selhání BlockSci a úspěšném návratu baseline během rušení executor ještě odeslal mappings. | Selhání čekání vyprázdní dosud neodeslané etapy. Regresní test reprodukoval původní zbytečné odeslání. |
| Chyba při rušení | Výjimka z jednoho `cancel()` překryla původní selhání a zabránila pokusům zrušit další sourozence. | Sdílený helper provede všechny dostupné pokusy, zachová původní chybu a připojí chyby rušení. Nepotvrzené rušení (`False`) není vydáváno za úspěch. |
| Přerušení paralelního běhu | `KeyboardInterrupt`/`SystemExit` opustil plánování a thread pool čekal na workery bez zrušení již odeslaných etap. | Před čekáním při ukončení poolu se zavolají dostupné cancellation callbacky. Test používá přerušení a blokovaný waiter, bez scheduleru. |
| Deklarovaný Python 3.10 | Manifest a logování importovaly `datetime.UTC`, dostupné až od Pythonu 3.11; CLI přitom deklaruje `>=3.10`. | Používají `timezone.utc`. Statická kontrola pro Python 3.10 prošla; skutečný interpreter 3.10 zde nebyl spuštěn. |

Regresní testy chyb argumentů, lokálních image, dry-runu a souběhu nejprve
selhaly na původním kódu a následně prošly s opravou. Kontrola shody s wrapper
parserem je doplňující test výsledného chování.

Dosavadní politika při **odmítnutí submission**, kdy nezávislá větev může
dokončit svou práci, zůstává zachována a má vlastní test. Zastavení nových etap
se týká selhání již spuštěné práce. S3 orchestrace nadále ruší jen závislé joby;
její samostatná politika nebyla sjednocována se shared-storage executorem.

## Čistota a typování

Provedené úpravy jsou malé a souvisejí s ověřenými problémy:

- `HostOptions` nyní rozlišuje povinné a volitelné klíče a jejich typy. Z CLI
  zmizel `type: ignore[arg-type]` při předávání verze image.
- `_error_and_exit` má návratový typ `NoReturn`. Tím správně popisuje, že
  následující kód už nemůže běžet s chybějícím run adresářem; odstranilo to osm
  navazujících diagnostik mypy bez přidávání castů.
- `_blocksci_resources` vrací existující `PBSResources`, čímž neztrácí typy
  jednotlivých schedulerových parametrů v obecném slovníku.
- Cancellation helper sdílí obsluhu selhání a přerušení;
  `StageSubmission.cancel` nyní vrací `bool | None` místo obecného `object`.

Silné stránky návrhu:

- `StagePlan`/`StageGraph` oddělují popis závislostí od backendových operací.
  `StageRunner` představuje malou, konkrétní hranici mezi nimi.
- `S3Target`, `Images` a konfigurační dataclasses dávají pojmenovanou strukturu
  datům, která by jinak putovala jako množství paralelních argumentů.
- S3 tracker zaznamenává získané job ID do rollback evidence před jeho zápisem
  na disk. Chyba lokálního zápisu tak neztrácí už odeslaný job z rollback seznamu.
- Samostatné testy kontraktů kontrolují CLI a orchestrace bez emulace. Právě
  takové testy zde umožnily prokázat chyby rychle a bez infrastruktury.

Zbývající technický dluh, který tato změna nepřepisuje:

1. **Duplicita validace.** `commands.py`, `builder.py`, konfigurační model a
   wrapperové validátory částečně opakují stejná pravidla. Přepis jedné větve
   nemusí změnit ostatní. Další refaktoring by měl přesouvat konkrétní čistá
   pravidla do společného modulu a kontrolovat shodu všech vstupních cest.
2. **Příliš obecné callbacky a Namespace.** Například
   `SharedStorageOperations` a `S3StageSubmissionOperations` stále používají
   `Callable[..., ...]`. Mypy tak nekontroluje názvy ani typy jejich argumentů.
   Další smysluplný krok jsou konkrétní callable protokoly pro I/O hranice,
   nikoli další obecná vrstva factory funkcí.
3. **Slabé typy reportu.** `pipeline/exporters/common.py` definuje
   `JsonObject = dict`. Návratová anotace `JsonObject` tak neověřuje schéma
   reportu. Typované normalizované transakce a provenance by pomohly více než
   mechanicky anotovat každý volný JSON slovník.
4. **Velká kompatibilní fasáda.** `wrapper.py` má přibližně 1 800 řádků a
   mnohé operace jen přeposílá kvůli existujícím importům a mockům. Oddělení
   čistých pomocných funkcí už proběhlo; další dělení má cenu při skutečném
   zmenšení odpovědností a testování přes backendové adaptéry.
5. **Provozní omezení nejsou typové chyby.** Dostupnost správných image,
   scheduleru, sdílených cest a vzdálených markerů musí potvrdit integrační
   běh. Ani úspěšné mypy, ani test s mockovaným qdel to neprokazuje.

## Dokončení 26. 9. 2026

Druhý průchod ověřil tabulku oprav proti kódu, dodělal chybějící opravu
a zpracoval technický dluh. Změny zatím nejsou commitnuté.

| Položka | Stav | Co se změnilo |
| --- | --- | --- |
| Oprava PBS/S3 dry-runu | **chyběla, dopsána** | Tabulka ji uváděla jako hotovou, ale v commitech ani v kódu nebyla: host při dry-runu dál vytvářel runtime adresáře a přepisoval `research_manifest.json`. Nově `cli.py` wrapper pro vykreslení spustí, ale adresáře ani manifest nesahá. Test `test_pbs_dry_run_leaves_the_run_directory_untouched` na původním kódu selže (manifest přepsán) a s opravou projde; ručně ověřeno skutečným `analyze --blocksciPbs --dry-run`. |
| 1. Duplicita validace | hotovo | Pravidla mezi volbami žijí jednou v `src/coinjoin_pipeline/option_rules.py` (`cross_option_errors` nad `OptionView`). Host, YAML konfigurace, wrapper (`_validate_request`) i builder volají tentýž kód. Z builderu zmizelo asi 150 řádků kopií. Z wrapperu zmizely kopie, které se na stejném argv nemohly uplatnit. `artifact_validation.py` kontroluje jen efektivní hodnoty: výchozí hodnoty z prostředí (`PBS_BITCOIN_DATADIR`), rozsah portu notebooku a normalizaci URI, run ID a přihlašovacího souboru. Pravidla pro mappings, dosud jen ve wrapperu, se přesunula do sdíleného modulu, takže je host odmítne dřív. |
| — shoda vstupních cest | hotovo | `tests/pipeline/test_option_rules_parity.py`: 19 případů, na kterých host, builder i wrapper musí souhlasit. Na původním kódu selže 9 z nich, protože cesty se skutečně rozcházely. Builder odmítal platné `--blocksci-bitcoin-blocks-uri` a přijímal S3 `emulate` bez `--s3-credentials-file`/`--s3-profile`, reusable BlockSci na shared storage i `--blocksci-task external` bez baseline. Host pouštěl `mappings` bez `--mappingsPbs` a `--mappingsPbs` s JoinMarketem. |
| 2. Obecné callbacky | hotovo | `pipeline/client/operation_types.py` obsahuje protokoly se signaturami produkčních funkcí. Ve wrapperových balíčcích operací nezůstal žádný `Callable[..., …]`. `S3StageRunner.common` je `TypedDict`, takže mypy kontroluje i rozbalované `**` argumenty. Na záměrně přejmenovaném keywordu mypy selže. |
| 3. Slabé typy reportu | hotovo | `pipeline/exporters/report_types.py`: `IORecord`, `TransactionRecord`, `TransactionMetrics`, `RecordSummary`, `RunManifest` a jeho části, `BlockSciAnalysisArtifact`. Normalizace obou analyzátorů, porovnání, heuristiky, scénářové kontroly i manifest pracují s nimi. `JsonObject` zůstává `dict` jen pro volný JSON. BlockSci modul popisují protokoly místo `object` (`typing.Any` je v `pipeline/` zakázán). |
| 4. Fasáda `wrapper.py` | vědomě ponecháno | Odstraněna jediná nikde nepoužívaná funkce (`s3_access_from_target`). Ostatní přeposílací funkce jsou patch pointy testů. Další dělení by podle bodu 4 nezmenšilo odpovědnosti. |
| 5. Provozní omezení | mimo kód | Vyžaduje integrační běh (viz níže). |

Vedlejší nález při běhu pod Pythonem 3.10: `exists_or_unreadable` ve wrapperu
volala `Path.is_file()`, které do Pythonu 3.13 včetně za adresářem 0700
vlastněným rootem vyhodí `PermissionError`. `False` vrací až 3.14 (ověřeno
na 3.11–3.14). Funkce psaná právě pro root-owned `parsed/` by tak na
frontendu se starším Pythonem spadla. Nyní používá `os.path`.

Otevřené pozorování mimo rozsah: sám wrapper při PBS dry-runu zapisuje do run
adresáře (snapshot `.pipeline/exporters`, `logs/`, `.research.lock`).
Snapshot exportérů se při dalších pokusech nepřepisuje, takže dry-run může
zafixovat verzi exportérů pro pozdější skutečné odeslání.

### Ověření dokončení

- `pytest tests/unit tests/pipeline tests/test_command_builder.py`: **623 PASS**
  na hostitelském Pythonu 3.14, **623 PASS** i na skutečném Pythonu 3.10.
- `tests/test-command-builder-contract.sh`, `tests/test-wrapper-signal-cleanup.sh`:
  **PASS**.
- `ruff check src pipeline tests`: **PASS**.
- `mypy` se skutečným procházením importů (`MYPYPATH=pipeline:src
  mypy src/coinjoin_pipeline pipeline/client pipeline/exporters
  --explicit-package-bases`): **PASS**, 71 souborů. Varianta
  `--follow-imports=skip` z původní kontroly bere importy `client.*` jako
  `Any`, a proto dřívějších 17 chyb v exportérech neukázala.
- Exportéry se importují a normalizují pod Pythonem 3.8 (BlockSci image).
  Unified report reálného JoinMarket běhu (`2026-08-28_20-23_default-joinmarket`,
  kopie) vygenerovaný původním a novým kódem na Pythonu 3.8 i 3.14 je
  shodný. Liší se jen hash a commit stromu exportérů v provenance; Markdown
  je bajtově shodný.
- Neběželo: image build, emulace, skutečný PBS ani
  `tests/test-parallel-pbs-analysis.sh` (vyžaduje infrastrukturu).

## Ověření

- 115 cílených testů hosta, konfigurace, logování, CLI kontraktů a grafu/executoru:
  **PASS**, 0,60 s samotných testů.
- 9 testů wrapperu vybraných přes `-k 'parallel or dry_run'`:
  **PASS**, 1,20 s samotných testů.
- Ruff nad `src`, `pipeline/client`, `pipeline/exporters` a upravenými testy:
  **PASS**.
- Mypy nad 50 zdrojovými soubory `src/coinjoin_pipeline` a `pipeline/client`,
  s `--follow-imports=skip`: **PASS**, také s `--python-version 3.10`.
  Importy mimo explicitně zadané soubory jsou přeskočené; nejde o strict
  typování celého projektu ani o kontrolu dynamických JSON schémat.
- Pokus o širší testy `test_wrapper.py` + `test_cli_contract.py` ukončil
  pětisekundový limit; tato kombinace proto není vykazována jako dokončená.
  Stejný limit zastavil původní mypy s procházením všech importů; výše je
  uvedena přesná varianta kontroly, která skutečně dokončila.

Změny cancellation jsou ověřeny krátkými řízenými testy, nikoli živým PBS.
Pokud `qdel` selže nebo lokální etapa nemá cancellation callback, executor
nadále musí počkat na její vlastní návrat/timeout. Oprava negarantuje okamžité
ukončení neodpovídajícího backendu.

Pro následné uživatelské ověření všech unit/wrapper testů lze z kořene repozitáře
spustit `rtk proxy .venv/bin/python -m pytest tests/unit tests/pipeline`.
Pro skutečný paralelní PBS tok je navazující test
`rtk proxy bash tests/test-parallel-pbs-analysis.sh`; vyžaduje připravené image
a infrastrukturu a v této kontrole nebyl spuštěn.
