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

## Šest jobů

Synchronizace odešle šest jobů řetězených `afterok`: `mainnet-restore` →
`mainnet-producer` → `mainnet-blocksci` → `mainnet-checkpoint` → `mainnet-pack` →
`mainnet-clear`. Všechny kromě `mainnet-pack` si samy vezmou a pustí zámek datasetu. Když jeden spadne, PBS zruší ty, které na
něm závisí. Synchronizace je hotová už závěrečným `done.json` checkpointu; selhání
`mainnet-pack` nebo `mainnet-clear` ji nezmění a v Labu se ukáže jen u daného jobu.

`mainnet-restore` (8 CPU / 8 GB) vymaže stav minulého běhu a podívá se, zda
na multijobu existují pracovní data (`bitcoin/`, `dumplings/`, `archive-state/`,
`config.json`, `files.txt`, `manifest.json` nebo `parsed/` indexu; zámky, `sync/`
a logy se nepočítají).

- **Něco existuje:** skončí hned s `restore.json: skipped-not-empty`.
- **Něco existuje, ale index má `.writing`:** BlockSci job spadl uprostřed zápisu, takže
  pokračovat nelze. Pokud manifesty záloh v S3 projdou stejnými kontrolami jako níže,
  smaže celý pracovní dataset a obnoví ho ze S3; práce nedokončeného běhu se ztratí.
  Bez použitelných záloh skončí chybou a dataset nechá být.
- **Nic neexistuje a v S3 nejsou žádné zálohy:** jde o první synchronizaci.
  Zapíše `skipped-no-backup` a producer staví od nuly. Neprázdné `blocks_uri`
  bez záloh je chyba, protože nový IBD by rozložil bloky do jiných `blk*.dat`.
- **Nic neexistuje a v S3 jsou zálohy:** ověří, že všechny tři manifesty
  (Core checkpoint, index a Dumplings) patří k sobě:
  - Core odkazuje na tenhle index,
  - `run_id` jsou stejná,
  - výška Core = `exported_max_block` = `last_processed_height`,
  - kurzor parseru `resume_file_*` se shoduje.

  Potom stáhne všechny tři do `.restore-<run-id>/` s kontrolou SHA-256
  a velikostí a teprve potom je přesune na místo. Ověří `files.txt` a kurzor parseru
  proti obnoveným blokům a zapíše `restored`. Částečný dataset se se zálohami
  nikdy nemíchá.

`mainnet-producer` nejdřív zkontroluje úplnost datasetu: Core chainstate a bloky,
`archive-state/`, výšku Scanneru a `config.json` indexu.

- **Všechno chybí:** projde jen tehdy, když `restore.json` tohoto běhu říká
  `skipped-no-backup`.
- **Chybí část:** skončí dřív, než spustí Core, a vypíše, co chybí. Řešením je
  smazat celý `mainnet.root` i `blocksci.index_dir`; další synchronizace je obnoví.
- **Rozběhnutá první synchronizace** (bez indexu, ale s `blk00000.dat`) pokračuje.

Dál producer synchronizuje Core s `prune=1`, `txindex=0`, `blocksxor=0`.
Po dosynchronizování vypne síť, čistě restartuje Core bez sítě a zachytí
stabilní výšku H a hash. Spustí jeden průchod
Dumplings, ověří shodu výšky a checkpointu včetně délek souborů a prázdného
`Tx0CandidateMisses.txt`. Archivuje a ověří dokončené blk soubory; dva poslední
archiver standardně ponechává jako otevřený konec. Zálohy Dumplings se nedotkne:
pád produceru nebo BlockSci jobu tak nechá všechny tři zálohy v S3 z jednoho běhu.
Čistě vypne Core a publikuje `ready.json`.
Poslední soubory bloků zůstávají pro parser na sdíleném disku a ve výsledném
checkpointu Core. Samotné `ready.json` neznamená dokončenou synchronizaci.
Před skenem uloží i `Scanner/ChainCheckpoint.json` s hashem zmrazeného cíle.
Další průchod ověří původní větev i při obnovení částečného skenu. Reorganizace
nebo import starého Scanner bez této vazby vyžadují explicitní ověření/obnovu;
detekce se automaticky nepřenášejí mezi větvemi.

`mainnet-blocksci` (256 GB) ověří přesnou generaci a pod výhradním zámkem
aktualizuje index přímo nad stejnými blk soubory. Ověří počet bloků H+1 a hash H,
zapíše `manifest.json` a `files.txt` a smaže `.writing`: index je hotový, i když
ještě nemá zálohu. Core v tomto jobu neběží.

`mainnet-checkpoint` (16 CPU kvůli `zstd -T`, 8 GB) drží zámek indexu jen pro čtení,
takže notebooky mohou index dál číst a ruční indexový job jej změnit nemůže.

1. Ověří, že index dokončil BlockSci job tohoto běhu: `run_id` a `exported_max_block`
   v manifestu, `files.txt`, žádný `.writing` a kurzor parseru proti Core.
2. Zazálohuje index a ověří existenci a velikost odeslaného objektu. Archiv znovu
   nestahuje.
3. Přírůstkově zrcadlí Scanner včetně Tx0Candidates do `<dumplings_uri>/Scanner/`
   (`s5cmd sync --delete`, přenesou se jen změněné soubory), porovná velikosti objektů
   s lokálními soubory a nakonec zapíše `<dumplings_uri>/manifest.json` s výškou
   a velikostmi. Nová kopie nejdřív smaže manifest, takže baseline nikdy nečte napůl
   přepsaný Scanner.
4. Spustí Core offline s `dbcache` nejvýše 1024 MB, zkontroluje H/hash, provede
   `--prune-only` a Core čistě vypne.
5. Až potom zálohuje jeho výsledný stav. Během kopírování drží POSIX zámek
   datadiru a odmítne přítomnou RPC cookie.

Teprve dokončený upload checkpointu umožní publikovat závěrečné
`<state_uri>/done.json`.

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

`mainnet-pack` (4 CPU, 4 GB, 24 h) zkomprimuje archiv bloků. Zámek datasetu si nebere,
takže může běžet současně s další synchronizací. Vypíše S3 a až potom disk a bere jen
surové `blkNNNNN.dat`, které už Core na disku nemá: pruning totiž u souborů na disku
ověřuje surový objekt. Odmítne běh bez `done.json` tohoto běhu i prázdný adresář bloků.
Každý soubor zpracuje samostatně: ověří ho proti SHA-256 ze sidecaru, zabalí `zstd -19`,
nahraje `blkNNNNN.dat.zst`, přečte ho zpět, rozbalený obsah znovu porovná se sidecarem
a teprve potom smaže surový objekt. Sidecar se nemění, takže `block_archive_index_sha256`
platí dál. Opakování je bezpečné: soubor ve stavu raw + `.zst` se zpracuje znovu a čtení
bere raw. Půl hodiny před koncem walltime (nejvýš čtvrtina walltime) job přestane brát
další soubory; zbytek zůstane surový do dalšího běhu. Souhrn je v `pack.json`
(`packed`, `raw_bytes`, `packed_bytes`, `remaining`, `failed`, `seconds`). Stahování
bloků pro parse a update čte obě podoby a `.zst` před `verify` rozbalí. Job běžící
současně se stahováním může parse shodit, protože surový objekt zmizí mezi výpisem
a stažením; opakovaný parse už uvidí `.zst`.

`mainnet-clear` (1 CPU, 2 GB, 2 h) vyprázdní multijob: Core, Dumplings, stav archivu,
index i vše ostatní v kořeni datasetu, například staré logy v `runs/`. Běží po
`mainnet-pack`, protože ten porovnává S3 s bloky na disku. Bere zámek
datasetu i zapisovací zámek indexu a odmítne dataset, jehož `done.json` patří jinému
běhu. Všechno nejdřív přesune stranou do `.clear-<run-id>/`, aby pád nezanechal částečný
dataset; zbytky odstraní další restore job. Zůstává jen adresář `sync/`. Když `mainnet-pack`
spadne, `mainnet-clear` se nespustí a data zůstanou do další synchronizace. Další běh
je obnoví z S3. Vypíná se pomocí `mainnet.clear_after_sync: false`.

Protože je multijob mezi synchronizacemi prázdný, joby čtoucí index (`script`, `external`,
`notebook`) ho nečtou z `blocksci.index_dir`. S `blocksci.index_in_scratch: true` si
stáhnou zálohu z `blocksci.index_backup_uri` do vlastního `$SCRATCHDIR`, kde ji nikdo
jiný nečte, takže nepotřebují zámek. S `blocksci.dumplings_uri` přidají Scanner,
který kontejner vidí v `/mnt/dumplings/Scanner`. Scratch musí stačit na rozbalený index
(135 GB ve výšce 500 027), Lab proto žádá 200 GB. Ruční joby `index-update`
a `index-backup` dál pracují s `index_dir` a na prázdném multijobu potřebují obnovu.

Notebook může místo toho nechat data na multijobu: s `blocksci.restore_index: true`
doplní neúplný `index_dir` ze zálohy pod zapisovacím zámkem a čte ho pod sdíleným.
Lab k tomu používá vlastní složku `mainnet-notebook/{index,dumplings/Scanner}`, kterou
synchronizace ani `mainnet-clear` nevidí; kopie v `index_dir` synchronizace by restore
považoval za neprázdný dataset a producer by ji odmítl. Kopie zůstává a další notebook ji
použije beze stahování, i když mezitím vznikla novější záloha. Maže se jen ručně v Labu.

Třetí možnost je číst přímo synchronizační složku, bez stahování: `blocksci.index_dir`
synchronizace a `blocksci.dumplings_dir` na její `dumplings/Scanner`. Data tam jsou jen
po synchronizaci s `mainnet.clear_after_sync: false` (v Labu vypnuté „Po synchronizaci
vyprázdnit multijob“); jinak job skončí chybou. Notebook drží index pod sdíleným zámkem,
takže další BlockSci job nebo `mainnet-clear` po dobu notebooku skončí chybou. Scanner
zámek nemá: běžící `mainnet-producer` ho notebooku mění pod rukama.

Po každém jobu synchronizace, i neúspěšném, worker změří `mainnet.root`, `blocksci.index_dir`
a `mainnet.notebook_root` a zapíše je do `<state_uri>/multijob-storage.json`. Notebook
s ponechanou kopií zapíše její velikost do `blocksci.storage_report_uri`. Lab z těchto
hlášení bere pro každou složku to nejnovější a smazání v Labu sám zaznamená jako nulu, takže
běžný přehled nepotřebuje inspekční job. Selhání hlášení job neshodí.

## Pracovní data a zálohy

```
mainnet.root/
  bitcoin/{blocks,chainstate}
  dumplings/Scanner/
  archive-state/
  sync/{restore,producer,blocksci,checkpoint}.json, sync/{ready.json,done.json}
  current-ready.json
  .sync-active/owner.json
blocksci.index_dir/
  config.json
  parsed/
  manifest.json
  files.txt
  .lock/
```

Všechny joby musí mít stejné umístění multijob. Jeho adresář je oddělený od
`$SCRATCHDIR`, ale multijob storage je dočasný a může zmizet. Ze S3 stahuje jen
`mainnet-restore`, a to jen do úplně prázdného datasetu. Počáteční parse potřebuje kompletní historii
bloků. Prunovaný Core bez indexu nelze rovnou použít pro prvotní parse.
Existující index lze navázat jen se stejným zdrojem bloků a image; cizí index
se automaticky nepřekonfiguruje.

Každý prefix drží jedinou zálohu: archiv s pevným jménem
(`<backup-uri>/checkpoint.tar.zst`, `<index-backup-uri>/blocksci_index.tar.zst`)
a `manifest.json`. Nová záloha nejdřív smaže manifest a starý archiv, takže S3
nikdy nedrží dvě kopie a do konce uploadu žádná platná záloha neexistuje.
Archiv se streamuje do S3 se SHA-256 spočítaným během odesílání; po uploadu se
ověří jen velikost objektu a nakonec se zapíše manifest
(`upload_validation: size-only`, `uploaded_at`). Obsah se znovu nečte, SHA-256
se kontroluje až při obnově. Selhaný upload nechá zálohu chybět do dalšího
běhu; pracovní kopie na multijobu zůstává.
Záloha Core obsahuje `bitcoin/blocks`, `bitcoin/chainstate` a stav archiveru;
Scanner v ní není, obnoví se zvlášť z jeho přírůstkové kopie
(`mainnet_runtime … restore-dumplings <dumplings_uri>/manifest.json <cíl>/dumplings/Scanner`). Neobsahuje peněženky, přihlašovací údaje ani konfiguraci Core.
Konfiguraci pro pokračování vždy nastavuje job. Core se starými XOR bloky nebo
více chainstates vyžaduje explicitní migraci.

Manifest každé zálohy uvádí `member_bytes`, zdánlivou velikost každého zálohovaného
kořene. Závěrečné `done.json` z nich skládá `storage`: obsazení multijobu
(`multijob.core`, `multijob.index`, po kořenech) a velikost nově odeslaných archivů
(`s3.core_checkpoint`, `s3.index_backup`). Měří se při zastaveném Core a drženém
zápisovém zámku indexu, takže samostatný měřicí job není pro tato čísla potřeba.
Celkové obsazení S3 včetně archivu bloků zjistí až listování prefixů.

Stav synchronizace má pevné místo, nikoli složku podle run ID: lokálně
`mainnet.root/sync/`, v S3 `mainnet.state_uri` (`ready.json`, `done.json`,
`{restore,producer,blocksci,checkpoint}.json`, `blocksci-index-manifest.json`,
`.pbs/mainnet-{restore,producer,blocksci,checkpoint}.{done,failed}`). Na rozdíl od
markerů ostatních stage, které obsahují jen `done`/`failed`, nesou mainnet markery JSON:
`.done` stejný obsah jako výsledek jobu (restore akci a zálohy, producer `ready.json`,
blocksci manifest indexu, checkpoint `done.json`), `.failed` objekt s `run_id`. Restore job
nového běhu je pod zámkem datasetu nejdřív smaže, takže prefix vždy popisuje poslední běh; `run_id`
v každém JSON říká, ke kterému patří. Logy každého běhu zůstávají v
`<state_uri>/logs/<run-id>/`. Konfigurace synchronizace proto nemá `artifacts.uri`;
sekce `artifacts` nese jen endpoint, soubor s klíči a profil. Ruční indexové joby (`parse`, `update`, `backup`) mají
podobně pevný prefix `blocksci.index_state_uri` (`bitcoin-mainnet/index/`), viz
`docs/pbs-stage-contract.md`.

Průběh v `<state_uri>/{restore,producer,blocksci,checkpoint}.json` obsahuje fázi,
výšku, procento nebo null a čas aktualizace. Pro Core jde o RPC odhad; procento
parseru platí pro jeho právě hlášenou fázi. Streamovaná záloha a její ověřování
mají neurčité procento. Značka produceru ani `blocksci.json: indexed/done`
nenahrazuje závěrečné `done.json`; `restore.json` navíc nese `action`. Lab doplňuje stav PBS i při pádu před publikováním průběhu.

`Live log` v Labu (CLI `watch --pbs-only --run-id ID --run-dir RUN_DIR`)
čte také skutečný stdout/stderr Core, Dumplings, archivátoru a BlockSci.
Nově odeslané joby zapisují tyto malé soubory přímo do
`<runs_root>/<run-id>/logs/mainnet-{restore,producer,blocksci,checkpoint}/*.log` na úložišti
sdíleném s frontendem; `runs_root` musí být z PBS uzlů přístupný pod stejnou
cestou. Pracovní blockchain a index zůstávají v multijob adresářích.
Watcher sleduje i soubory vzniklé až během běhu, označuje výstup komponentou
a při připojení přehraje posledních 200 řádků každého zdroje (`--tail`).
Nečeká na doručení PBS stdout ani na upload do S3. Při ukončení fáze se její
logy stále archivují do S3 pod `<state_uri>/logs/<run-id>/`. Dříve odeslané PBS skripty obsahují
původní worker a toto průběžné ukládání se na ně zpětně nevztahuje.

## Selhání a souběh

Každý job drží výhradní zámek datasetu po dobu své práce. Při předání ve frontě
zůstává `current-ready.json`; BlockSci i checkpoint job ověří, že patří přesně
jejich generaci. Producer další generace tuto značku zneplatní ještě před změnou
Core, takže starý čekající job nesmí zpracovat novější nebo částečně změněná data.
Mezi joby se zámek nedrží: zrušení dosud nezačatého jobu nezanechá zámek datasetu.
Index používá stejný writer/readers zámek jako existující notebooky. Restore
a BlockSci job berou zámek pro zápis, checkpoint job pro čtení. Pokud notebook drží
index při startu restore nebo BlockSci jobu, job ihned skončí; nezabírá RAM
čekáním. Notebook ukonči a spusť novou synchronizaci.

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

Neúplný index má `.writing` a další průchod jej odmítne. Vzniká jen při pádu
samotného BlockSci updatu; pak smaž celý dataset a nech restore job obnovit
poslední platné zálohy. Selhání checkpoint jobu index nepoškodí: index i Core
zůstávají na multijobu použitelné, v S3 chybí jen záloha, kterou smazal začátek
uploadu, a další synchronizace ji vyrobí znovu. Do té doby ale prázdný multijob
obnovit nejde, protože zálohy v S3 k sobě nesedí.

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

## Ověření záloh bez mazání

`scripts/render-mainnet-restore-check.py [config.yaml]` vypíše samostatný PBS job,
který spustí skutečný `mainnet-restore` do vlastního `$SCRATCHDIR` (2 CPU, 4 GB RAM,
200 GB scratch). Ověří párování tří manifestů, SHA-256 archivů, velikosti Scanneru,
inventář indexu a kurzor parseru. Multijob, zálohy ani `state_uri` nemění:
stav jde do `<state_uri>-restore-check/<run-id>/` a rozbalená data job na konci smaže.
Neověřuje, že obnovená data otevře Core nebo BlockSci. Výsledek je
`restore.json: restored` a nulový návratový kód.

`restore.json` má v `parts` pro Core, Dumplings a index trvání, CPU čas, stažené
a rozbalené bajty, počet souborů a rychlosti; stejné řádky jsou v logu jobu. Stahování,
rozbalování a kontroly běží současně, proto má každá část jediný čas. Job navíc nejdřív
oba archivy přečte a zahodí (`raw_download_*`), takže porovnání ukáže, zda brzdí S3,
nebo rozbalování. Archivy se tak stahují dvakrát. Ostrá obnova `parts` zapisuje taky,
ale bez tohoto průchodu.

## Explicitní obnova checkpointu

Běžnou obnovu prázdného datasetu dělá `mainnet-restore`. Tato kapitola popisuje
ruční obnovu jednotlivé zálohy, například pro kontrolu.

Helper `coinjoin_pipeline.execution.mainnet_runtime` umí obnovit ověřený
manifest do **neexistujícího** adresáře. Použije staging vedle cíle; neúspěšná
obnova nezpřístupní částečný adresář. Odmítá odkazy a cesty mimo deklarované
kořeny. Připrav malý JSON bez samotných klíčů (cesty uprav pro prostředí):

```json
{
  "root": "/storage/local/mainnet",
  "index_dir": "/storage/local/index",
  "run_id": "explicit-recovery",
  "state_uri": "s3://local/sync",
  "endpoint": "http://127.0.0.1:9000",
  "credentials": "/storage/local/credentials",
  "profile": "local"
}
```

```bash
python -m coinjoin_pipeline.execution.mainnet_runtime recovery.json restore \
  s3://local/core/manifest.json /storage/local/restored-core
```

Stejný helper obnoví index, pokud dostane manifest jeho zálohy a nový cílový
adresář. Původní kopie se tím nepřepisuje. Zálohu indexu umí obnovit i
`index_store.py` (update nad neúplnou pracovní kopií).
Index i Core zapisuje stejný běh, index dřív. Pokud běh selže až při záloze Core,
manifest Core chybí a záloha indexu je o generaci novější než pracovní kopie Core;
takovou dvojici nelze slepě spojit a je potřeba nejdřív doběhnout synchronizaci.
