# Ruční synchronizace mainnetu

Celý lokální řetězec Core/Dumplings/archiver → OpenPBS/Apptainer → BlockSci
a obnova obou záloh byly ověřeny na vm207; viz
[protokol lokálního běhu](mainnet-sync-local-validation-2026-10-25.md).
Není tím doložen full-chain mainnetový experiment ani nasazení na MetaCentru.
Žádný cron se nezavádí. Scenario Lab nabízí **Spustit synchronizaci**, samostatné
rozpočty CPU/RAM a průběh aktuální fáze. **Notebook nad indexem** otevře existující
formulář notebooku; ten index neparsuje a používá jej pouze pro čtení.

## Volitelná cílová výška

V Labu je **Cílová výška synchronizace (volitelné)** předvyplněná na `400000`.
To je doporučený minimální cíl běhu se skutečným zpracováním bloků v Dumplings,
který na mainnetu začíná výškou 336861. Stejný cíl používá příklad
`examples/mainnet-sync.yaml`: `mainnet.target_height: 400000`
(CLI `--mainnet-target-height 400000`). Core stále synchronizuje historii od genesis;
400000 je cílová výška, nikoliv počátek stahování.
Menší cíle zůstávají dostupné pro testy infrastruktury a prázdného checkpointu.
Prázdné pole nebo vynechané nastavení znamená současný tip řetězce.
S limitem se nečeká na dokončení IBD ani na dohnání všech hlaviček: po dosažení
prahu se vypne síť, Core se čistě restartuje bez sítě pro Dumplings a pokračuje celý řetězec
včetně indexace a checkpointu. Procento Core v tomto režimu vyjadřuje výšku vůči
zadanému cíli; nejde o odhad zbývajícího času.

Limit je práh pro vypnutí sítě, nikoliv přesný řez daty. Kontrola probíhá nejvýše
po jedné sekundě plus čas RPC a publikování průběhu; již stažené a rozpracované
bloky mohou výslednou výšku posunout. Samotné vypnutí sítě nečeká na dokončení
jejich validace: konečná výška se proto čte až po čistém ukončení Core a jeho
opětovném spuštění s `networkactive=0`. Všechny další kroky používají skutečnou
zmrazenou výšku a hash; `ready.json` navíc uchovává `requested_target_height`.
Existující vyšší řetězec se nezkracuje. Pro menší test použij oddělený Core dataset,
index a zálohovací prefixy. Dumplings pro cíl před svým výchozím počátkem 336861
publikuje prázdný checkpoint na skutečné výšce a příště bezpečně naváže.
U krátkého mainnetového testu pod výškou 100000 se pruning přeskočí, protože jej
Core ještě nepovoluje; indexace a záloha pokračují se zachováním všech bloků.
Tato možnost vyžaduje také nově sestavené Dumplings s `--frozen-height`.

## Dva joby

`mainnet-producer` synchronizuje Core s `prune=1`, `txindex=0`, `blocksxor=0`.
Po dosynchronizování vypne síť, čistě restartuje Core bez sítě a zachytí
stabilní výšku H a hash. Spustí jeden průchod
Dumplings, ověří shodu výšky a checkpointu včetně délek souborů a prázdného
`Tx0CandidateMisses.txt`. Archivuje a ověří dokončené blk soubory; dva poslední
archiver standardně ponechává jako otevřený konec. Zálohuje a čtením zpět ověří
Scanner včetně Tx0Candidates. Čistě vypne Core a publikuje generační `ready.json`.
Poslední soubory bloků zůstávají pro parser na sdíleném disku a ve výsledném
checkpointu Core. Samotné `ready.json` neznamená dokončenou synchronizaci.
Před skenem uloží i `Scanner/ChainCheckpoint.json` s hashem zmrazeného cíle.
Další průchod ověří původní větev i při obnovení částečného skenu. Reorganizace
nebo import starého Scanner bez této vazby vyžadují explicitní ověření/obnovu;
detekce se automaticky nepřenášejí mezi větvemi.

`mainnet-blocksci` má PBS závislost `afterok` na prvním jobu. Ověří přesnou
generaci a pod výhradním zámkem aktualizuje index přímo nad stejnými blk soubory.
Ověří počet bloků H+1 a hash H, následně zazálohuje index a ověří existenci
a velikost odeslaného objektu. Archiv znovu nestahuje. Spustí Core offline, zkontroluje H/hash, provede `--prune-only`,
čistě Core vypne a až potom zálohuje jeho výsledný stav. Během kopírování drží
POSIX zámek datadiru a odmítne přítomnou RPC cookie. Teprve dokončený upload checkpointu
umožní publikovat závěrečné `synchronization/done.json`.

Pruning neprovádí uploady. Ověří existující archivované kopie. Limit výšky je
minimum pokroku obou konzumentů minus rezerva (výchozí 2016 bloků). Navíc chrání
soubor přesného kurzoru parseru a všechny novější blk/rev páry. Chybějící,
prázdný nebo nečitelný chráněný soubor pruning zablokuje.

BlockSci ukládá `parsed/parser/blockList.dat.resume.json` ze skutečného
`newestBlock.nFile/nDataPos/size`. Tento kurzor se může lišit od souboru posledního
kanonického bloku. Checkpoint je použitelný teprve po úspěšném ukončení parseru
a ověření výsledného indexu. Starší image bez tohoto souboru pruning neumožní.
Manifest indexu navíc obsahuje SHA-256 prefixu tohoto souboru až po kurzor.
Další update ověří stejné bajty; nová data lze připojit za kurzor, ale jiný
layout Core se nesmí zaměnit za stejný zdroj jen proto, že má stejnou cestu.

## Pracovní data a zálohy

```
mainnet.root/
  bitcoin/{blocks,chainstate}
  dumplings/Scanner/
  archive-state/
  runs/<run-id>/{ready.json,done.json,producer.json,blocksci.json,*.log}
  current-ready.json
  .sync-active/owner.json
blocksci.index_dir/
  config.json
  parsed/
  manifest.json
  files.txt
  .lock/
```

Oba joby musí mít stejné umístění multijob. Jeho adresář je oddělený od
`$SCRATCHDIR`; úklid PBS jej nesmí odstranit. Normální průchod nic nestahuje
ze S3. Obnova je explicitní. Počáteční parse potřebuje kompletní historii
bloků. Prunovaný Core bez indexu nelze rovnou použít pro prvotní parse.
Existující index lze navázat jen se stejným zdrojem bloků a image; cizí index
se automaticky nepřekonfiguruje.

Každá záloha dostane `<backup-uri>/generations/<run-id>/checkpoint.tar.zst`
a manifest se SHA-256 spočítaným během odesílání. Po úspěšném uploadu a kontrole
existence a velikosti objektu se přepne `<backup-uri>/latest.json`. Manifest
uvádí `upload_validation: size-only` a `uploaded_at`; obsah vzdálené zálohy se
po uploadu znovu nečte. Kontrola SHA-256 celého archivu probíhá při obnově.
Předchozí generace se nemažou. Retenci bude možné zavést odděleně; současný
kód staré platné zálohy neodstraňuje ani při selhání nového uploadu.
Záloha Core obsahuje `bitcoin/blocks`, `bitcoin/chainstate`, `dumplings/Scanner`
a stav archiveru. Neobsahuje peněženky, přihlašovací údaje ani konfiguraci Core.
Konfiguraci pro pokračování vždy nastavuje job. Core se starými XOR bloky nebo
více chainstates vyžaduje explicitní migraci.

Manifest každé zálohy uvádí `member_bytes`, zdánlivou velikost každého zálohovaného
kořene. Závěrečné `done.json` z nich skládá `storage`: obsazení multijobu
(`multijob.core`, `multijob.index`, po kořenech) a velikost nově odeslaných archivů
(`s3.core_checkpoint`, `s3.index_backup`). Měří se při zastaveném Core a drženém
zápisovém zámku indexu, takže samostatný měřicí job není pro tato čísla potřeba.
Celkové obsazení S3 včetně starších generací a archivu bloků zjistí až listování
prefixů.

Průběh v `runs/<run-id>/synchronization/{producer,blocksci}.json` obsahuje fázi,
výšku, procento nebo null a čas aktualizace. Pro Core jde o RPC odhad; procento
parseru platí pro jeho právě hlášenou fázi. Streamovaná záloha a její ověřování
mají neurčité procento. Značka prvního jobu ani `blocksci.json: done` nenahrazuje
závěrečné `done.json`. Lab doplňuje stav PBS i při pádu před publikováním průběhu.

`Live log` v Labu (CLI `watch --pbs-only --run-id ID --run-dir RUN_DIR`)
čte také skutečný stdout/stderr Core, Dumplings, archivátoru a BlockSci.
Nově odeslané joby zapisují tyto malé soubory přímo do
`<runs_root>/<run-id>/logs/mainnet-{producer,blocksci}/*.log` na úložišti
sdíleném s frontendem; `runs_root` musí být z PBS uzlů přístupný pod stejnou
cestou. Pracovní blockchain a index zůstávají v multijob adresářích.
Watcher sleduje i soubory vzniklé až během běhu, označuje výstup komponentou
a při připojení přehraje posledních 200 řádků každého zdroje (`--tail`).
Nečeká na doručení PBS stdout ani na upload do S3. Při ukončení fáze se její
logy stále archivují do S3 pod `logs/`. Dříve odeslané PBS skripty obsahují
původní worker a toto průběžné ukládání se na ně zpětně nevztahuje.

## Selhání a souběh

Každý job drží výhradní zámek datasetu po dobu své práce. Při předání ve frontě
zůstává `current-ready.json`; druhý job ověří, že patří přesně jeho generaci.
První job další generace tuto značku zneplatní ještě před změnou Core, takže
starý čekající job nesmí zpracovat novější nebo částečně změněná data. Mezi joby
se zámek nedrží: zrušení dosud nezačatého druhého jobu nezanechá zámek datasetu.
Index používá stejný writer/readers zámek jako existující notebooky.
Pokud notebook drží index při startu druhého jobu, job ihned skončí; nezabírá
velkou RAM čekáním. Notebook ukonči a spusť novou synchronizaci.

Po chybě se dataset odemkne pouze pokud worker potvrdil ukončení svých procesů,
nebo se ještě vůbec nespustil. Nenulový návratový kód již ukončeného Core
nebrání odemčení po chybě; stále ale zakazuje publikovat checkpoint. Timeout
nebo chyba zastavení, která nepotvrdí konec procesu, zámek ponechá.
Po SIGKILL/OOM může `.sync-active` zůstat. Nejdřív
ověř konec obou PBS jobů a Core; potom lze jeho adresář přejmenovat pro audit.
Neodstraňuj jej automaticky podle stáří: některý job může stále běžet.

Na MetaCentru je `/scratch/multijob-brno` symlink na BeeGFS. Worker proto binduje
pracovní adresáře pod původní i skutečnou cestou ze `Path.resolve()`, aby symlinky
v kontejneru dosáhly na stejná data jako na hostiteli. Cesty v příkazech a uložených
manifestech zůstávají původní. Pokud Core hlásí neexistující datadir, přestože
adresář na hostiteli existuje, zkontroluj oba bindy v aktuálním workeru.
Upload persistentních logů se zkusí i při chybě úklidu; taková chyba ale nepotvrdí
ukončení procesů a nevytvoří značku `quiescent`.

Neúplný index má `.writing` a další průchod jej odmítne. Obnov jej do nového
adresáře z poslední platné zálohy, ověř jej a až potom ručně nahraď pracovní kopii.
Při selhání závěrečné zálohy Core zůstávají pracovní data i dokončený index na
multijob, platný starší checkpoint zůstává v S3 a celý běh není úspěšný.

## Lokální ověřování bez služeb MetaCentra

Krátké testy používají malé soubory, mocky PBS/Core/parseru a adresářovou náhradu
S3. Ověřují také skutečný malý tar/zstd stream, SHA-256 a obnovu. Nic neposílají
na síť, nespouštějí Core, parser ani kontejnery:

```bash
cd /home/administrator/diplomka/coinjoin-pipeline
.venv/bin/python -m pytest -q tests/unit/test_mainnet_sync.py \
  tests/pipeline/test_index_store.py \
  -k 'not readers_share and not reader_waits and not killed and not readers_finish'

cd /home/administrator/diplomka/bitcoin-block-archive
.venv/bin/python -m pytest -q tests/test_parser_pruning.py

cd /home/administrator/diplomka/scenario-lab
VITE_CONFIG_NATIVE_IGNORE_WARNING=true npx vitest run \
  src/domain/mainnet-sync.test.ts server/mainnet-sync.test.ts \
  src/components/mainnet/MainnetPage.test.tsx
```

Lokální integrační běh potřebuje obrazy obsahující tyto změny. Sestavení nic
nepushuje:

```bash
cd /home/administrator/diplomka
docker build -t bitcoin-block-archive:sync-local bitcoin-block-archive
docker build -t dumplings:sync-local Dumplings
docker build --target complete --build-arg NTHREADS=4 \
  -t blocksci-complete:sync-local blocksci
# Parser regression including an empty tail file and a missing resume file:
docker run --rm --entrypoint bash blocksci-complete:sync-local -lc \
  'cd /mnt/blocksci/test/blockscipy && uv run --project /blocksci --locked pytest -q --btc test_parser_resume.py'
```

Pro skutečný lokální OpenPBS/Apptainer průchod je potřeba zvláštní konfigurace
s lokálním S3 (např. MinIO), lokálními credentials, sdílenými cestami pod
`/storage/`, menším testovacím rozpočtem a odpovídajícími image URI. Dockerové
tagy samy nejsou přístupné z Apptaineru v PBS kontejneru: obrazy musí být
exportované či převedené a dostupné uzlu. Nepouštěj cloudový příklad jako
lokální test. Reprodukovatelný integrační test připraví izolované MinIO a PBS,
převede místní obrazy, vygeneruje regtest a provede dva průchody i obnovu:

```bash
cd /home/administrator/diplomka/coinjoin-pipeline
bash tests/test-mainnet-sync-local.sh
```

Vyžaduje i existující lokální obrazy PBS a MinIO uvedené na začátku skriptu.
Používá `mainnet.network: bitcoin_regtest`, `scan_start_height: 1` a vypnutou
P2P síť. Výchozí produkční režim je stále `bitcoin`; změna počátku skenu je
povolena pouze pro regtest. Vygenerovaná data, MinIO úložiště i logy zůstávají
pod `/storage/github-runner/mainnet-sync-*`. Test nepoužívá MetaCentrum ani
vzdálené S3. Potvrzené výsledky a jejich omezení jsou v odkazovaném protokolu.

### Omezený mainnet E2E v CI

`tests/test-mainnet-sync-mainnet.sh` používá stejný testovací řetězec jako
regtestová varianta, ale začíná s prázdným datovým adresářem a skutečný Bitcoin
Core stahuje mainnet přes veřejnou P2P síť. Výchozí první cíl je **400000**;
Dumplings tak skutečně zpracovává bloky od své počáteční výšky 336861.
Druhý průchod navazuje na skutečnou zmrazenou výšku a požaduje dalších 1000
bloků. Nejde o přesný řez na výšce 400000: platí výše popsaný přesah o již
rozpracované bloky. Samostatná kontrola odmítne výšku Core nad 450000 a překročení
256 GiB součtu pracovních dat, indexu, MinIO a obnovených kopií. Rezerva výšky
umožňuje přesah Core i druhou generaci nad 400000. Image tar/SIF
soubory do tohoto datového limitu nepatří. Na každou generaci se čeká nejvýše
34200 sekund (9,5 hodiny); CI omezuje celý test včetně přípravy obrazů pro
Apptainer na 1200 minut a celý job včetně buildů na 1260 minut. Producer má
rozpočet 8 GiB RAM / 6 hodin, BlockSci 16 GiB RAM / 3 hodiny. Původních
90 minut producentovi nestačilo: při cíli 400000 trvala samotná synchronizace
Core přibližně 80 minut a PBS poté ukončil rozpracovaný sken Dumplings.
Limit generace pokrývá oba navazující PBS joby a půlhodinovou rezervu;
limit testu pokrývá dvě generace a hodinu na přípravu a ověření obnovy.

Průběh Dumplings vykazuje zmrazenou výšku Core v `height` a checkpoint skeneru
samostatně v `scanner_height`. Chybějící checkpoint proto nemůže vydávat výchozí
výšku skeneru 336860 za dosaženou výšku blockchainu.

Test vyžaduje úspěch obou produkčních PBS jobů v obou generacích. Ověřuje
inkrementální postup, archiv dokončených blokových souborů v lokálním MinIO, obnovu obou záloh se SHA-256,
skutečné spuštění obnoveného Core a načtení obnoveného indexu v BlockSci.
Po obnově musejí oba vracet stejnou výšku, hash tipu a mainnetový genesis;
checkpoint Core musí odkazovat na odpovídající zálohu indexu. Výchozí cíl 400000
zahrnuje i skutečný scan Dumplings. Test nepředepisuje počet nalezených CoinJoinů
a bez ground truth neměří kvalitu detekce. Skutečné mazání starých blk souborů zůstává
povinnou kontrolou regtestové varianty, kde ho malý dataset spolehlivě vyvolá.
Archiver ponechává dva nejnovější blokové soubory. Pokud se krátká historie vejde
do nich, test nevyžaduje samostatný archiv `blk00000.dat`; ověří jeho přítomnost
v obnoveném checkpointu Core a zaznamená `block_archive_verified: false`.
Jakmile vznikne třetí soubor nebo existuje archivní marker, vzdálenou kopii
`blk00000.dat` nadále vyžaduje. Regtest ji vyžaduje vždy.

Job **`mainnet-sync-bounded`** ve workflow `Tests` běží automaticky při pushi
a pull requestu podle společného CI gate. Checkoutuje a sestaví Core/archiver,
Dumplings a BlockSci z jejich repozitářů, používá lokální OpenPBS/Apptainer a
MinIO a nevyžaduje MetaCentrum ani CESNET credentials. Artefakt
`mainnet-sync-bounded-results` uchovává 14 dní konfigurace, `result.json`,
návratový stav, PBS historii a konce logů, i při selhání. Velká data a zálohy
zůstávají v pracovním adresáři pod `/storage/github-runner/`; cestu uvádí
`working-directory.txt`. Test ani CI nemažou zachované datové důkazy.

Se sestavenými obrazy z předchozí sekce, dostupným PBS a MinIO image:

```bash
cd /home/administrator/diplomka/coinjoin-pipeline
timeout --signal=TERM --kill-after=120s 1200m bash tests/test-mainnet-sync-mainnet.sh
# Volitelně menší první cíl (kladné celé číslo nejvýše 400000):
MAINNET_TEST_TARGET_HEIGHT=10000 timeout --signal=TERM --kill-after=120s 1200m \
  bash tests/test-mainnet-sync-mainnet.sh
```

Obrazy lze vybrat přes `MAINNET_CORE_LOCAL_IMAGE`,
`MAINNET_DUMPLINGS_LOCAL_IMAGE`, `MAINNET_BLOCKSCI_LOCAL_IMAGE` a `PBS_IMAGE`.
Limity mění `MAINNET_TEST_MAX_DATA_MB` a `MAINNET_TEST_GENERATION_TIMEOUT`.
Krátké testy `tests/unit/test_mainnet_sync_e2e.py` ověřují konfigurace, hlídače,
záporné případy obnovy a automatické CI zapojení bez stahování a bez kontejnerů.
Přidání testu samo o sobě nedokládá úspěšný mainnetový průchod; ten musí doložit
výstup nového CI jobu.

Pro kontrolu konfigurace a vygenerovaných skriptů bez přístupu ke službám:

```bash
cd /home/administrator/diplomka/coinjoin-pipeline
./runIt.sh run examples/mainnet-sync.yaml --dry-run > /tmp/mainnet-sync-plan.txt
```

## Explicitní obnova checkpointu

Helper `coinjoin_pipeline.execution.mainnet_runtime` umí obnovit ověřený
manifest do **neexistujícího** adresáře. Použije staging vedle cíle; neúspěšná
obnova nezpřístupní částečný adresář. Odmítá odkazy a cesty mimo deklarované
kořeny. Připrav malý JSON bez samotných klíčů (cesty uprav pro prostředí):

```json
{
  "root": "/storage/local/mainnet",
  "index_dir": "/storage/local/index",
  "run_id": "explicit-recovery",
  "artifact_uri": "s3://local/runs",
  "endpoint": "http://127.0.0.1:9000",
  "credentials": "/storage/local/credentials",
  "profile": "local"
}
```

```bash
python -m coinjoin_pipeline.execution.mainnet_runtime recovery.json restore \
  s3://local/core/generations/GENERATION/manifest.json /storage/local/restored-core
```

Stejný helper obnoví index, pokud dostane jeho generační manifest a nový cílový
adresář. Původní kopie se tím nepřepisuje. Staré pevně pojmenované zálohy indexu
zůstávají čitelné původním `index_store.sh`; jejich formát není formátem této
nové explicitní obnovy.
Při společné obnově vyber index přes `index_backup` v manifestu checkpointu Core.
Samostatný `blocksci-index/latest.json` může být novější, pokud poslední běh
selhal až při závěrečné záloze Core. Takové dvě různé generace nelze slepě spojit.
