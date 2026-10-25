# Ruční synchronizace mainnetu

Celý lokální řetězec Core/Dumplings/archiver → OpenPBS/Apptainer → BlockSci
a obnova obou záloh byly ověřeny na vm207; viz
[protokol lokálního běhu](mainnet-sync-local-validation-2026-10-25.md).
Není tím doložen full-chain mainnetový experiment ani nasazení na MetaCentru.
Žádný cron se nezavádí. Scenario Lab nabízí **Spustit synchronizaci**, samostatné
rozpočty CPU/RAM a průběh aktuální fáze. **Notebook nad indexem** otevře existující
formulář notebooku; ten index neparsuje a používá jej pouze pro čtení.

## Dva joby

`mainnet-producer` synchronizuje Core s `prune=1`, `txindex=0`, `blocksxor=0`.
Po dosynchronizování vypne síť a zachytí výšku H a hash. Spustí jeden průchod
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

Průběh v `runs/<run-id>/synchronization/{producer,blocksci}.json` obsahuje fázi,
výšku, procento nebo null a čas aktualizace. Pro Core jde o RPC odhad; procento
parseru platí pro jeho právě hlášenou fázi. Streamovaná záloha a její ověřování
mají neurčité procento. Značka prvního jobu ani `blocksci.json: done` nenahrazuje
závěrečné `done.json`. Lab doplňuje stav PBS i při pádu před publikováním průběhu.

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
nebo se ještě vůbec nespustil. Po SIGKILL/OOM může `.sync-active` zůstat. Nejdřív
ověř konec obou PBS jobů a Core; potom lze jeho adresář přejmenovat pro audit.
Neodstraňuj jej automaticky podle stáří: některý job může stále běžet.

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
