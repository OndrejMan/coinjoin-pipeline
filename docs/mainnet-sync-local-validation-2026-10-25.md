# Lokální ověření synchronizace — 2026-10-25

**Výsledek: PASS na vm207.** Jde o celý lokální řetězec s reálnými komponentami
nad regtestem, nikoliv o full-chain mainnetový experiment. MetaCentrum, jeho
PBS, Kubernetes ani CESNET S3 nebyly použity. Změny nebyly commitnuty ani pushnuty.

## Provedené kontroly

| Kontrola | Výsledek |
|---|---|
| Sestavení archiveru, Dumplings a BlockSci včetně Python bindings | PASS |
| Dumplings testy (.NET 8 SDK, net6 testy s `DOTNET_ROLL_FORWARD=Major`) | 27 PASS |
| Krátké testy pipeline včetně zámků / archiver / Lab / launcher / service | 66 / 80 / 23 / 10 / 6 PASS |
| Parser: prázdný koncový blk soubor, opakovaný update a chybějící skutečný kurzor | 1 PASS |
| Reálné OpenPBS → Apptainer: Core + Dumplings + archiver | PASS |
| Reálné OpenPBS → Apptainer: BlockSci + backup + prune-only + checkpoint Core | PASS |
| Inkrementální pokračování po pruningu | PASS, výška 1200 → 1400 |
| Obnova Core a jeho skutečné spuštění | PASS |
| Obnova indexu a jeho skutečné načtení v BlockSci | PASS |
| JupyterLab přes lokální bridge/proxy, skutečný Python kernel | PASS |
| Graf, widget a uložené výstupy notebooku | PASS |
| Původní persistent-index integrační test: parse/update/restore/pinned analysis/backup | PASS |
| Ruff, mypy a pylint nových modulů; TypeScript, ESLint a formátování dotčeného Lab kódu | PASS |

Lokální stack použil Bitcoin Core 29.1, OpenPBS 23.06.06, Apptainer 1.5.1
a MinIO. Core neměl txindex ani automatický pruning. Regtest použil `-fastprune`
a rezervu 288 bloků, aby šlo skutečné mazání souborů ověřit na malém datasetu.
Výchozí mainnetová rezerva v konfiguraci zůstává 2016 bloků.

Požadavky PBS v tomto regtestu byly 2 CPU / 4 GiB pro producenta a 4 CPU / 8 GiB
pro indexaci. Nejde o měření ani doporučení prostředků pro mainnet. Notebook
požadoval 1 CPU / 2 GiB.

## Konkrétní výsledek druhé generace

- Core i BlockSci končí na výšce **1400**; index obsahuje **1401 bloků včetně genesis**.
- Oba po obnově vracejí hash
  `3be507b108606b8246fa9da754308bed3105febcaa8d78aa455e639cd96fd5ec`.
- Pruner požádal o výšku **1015**, obnovený Core hlásí `pruneheight: 1016`.
- Soubor `blk00000.dat` je lokálně odstraněný a jeho objekt v MinIO zůstává.
- Přesný kurzor parseru je v `blk00005.dat`; jeho prefix do délky **33798** bajtů
  zůstává zachovaný. Následná indexace použila existující index po předchozím pruningu.
- Checkpoint Core odkazuje na generační zálohu právě odpovídajícího indexu.
- Obnovený Core hlásí `automatic_pruning: false` a `getindexinfo` vrací `{}`.

Upload velkých archivů byl podle zvolené politiky kontrolován podle existence,
klíče a velikosti, nikoliv čtením celého objektu zpět. SHA-256 se počítá během
odesílání. V tomto testu byly **oba archivy skutečně obnoveny a jejich streamy
ověřeny pomocí SHA-256**. Není tím tvrzeno, že každý běžný upload provádí read-back.

## Opravy z reálného běhu

- PBS nespouštěl shell s `/usr/local/bin`; job nyní tuto cestu přidává před
  kontrolou `s5cmd` a před spuštěním procesů.
- Živý scanner Dumplings záměrně neukládá právě zpracovaný tip. Nový explicitní
  `--include-tip` uloží i zmrazený cíl PBS průchodu. Běžný živý režim se tím nemění.
- Lokální notebookový launcher zjišťuje GID Docker socketu uvnitř PBS kontejneru,
  protože hostitelský sandbox jej může zobrazit jako nepřiřazený GID. PBS job
  žádá odpovídající primární skupinu. Hostitelská oprávnění socketu se nemění.
- Názvy lokálních notebookových kontejnerů zahrnují run ID a náhodnou část;
  samotné `$$` není v různých PID namespaces unikátní.

## Artefakty

Hlavní běh má dvě generace `local-sync-20261025T154453Z-242318-{1,2}`.

- [Pracovní data, index, MinIO data a PBS historie](/storage/github-runner/mainnet-sync-20261025T154453Z-242318.ihGIwP)
- [Výsledek obnoveného indexu](/storage/github-runner/mainnet-sync-20261025T154453Z-242318.ihGIwP/recovered-index-info.json)
- [Výsledek obnoveného Core](/storage/github-runner/mainnet-sync-20261025T154453Z-242318.ihGIwP/recovered-core-info.json)
- [Manifest výsledného checkpointu](/storage/github-runner/mainnet-sync-20261025T154453Z-242318.ihGIwP/core-latest.json)
- [Logy sestavení, testů a předchozích neúspěšných pokusů](/home/administrator/diplomka/coinjoin-pipeline/emulation_logs/_test-results/mainnet-sync-local-20261025)
- [Uložený notebook s provedenými buňkami](/home/administrator/diplomka/coinjoin-pipeline/coinjoin-runs/notebook-local-20261025-170032-3.MsZqSu/notebooks/BlockSci.ipynb)
- [Graf z notebooku — syntetický regtest](/home/administrator/diplomka/coinjoin-pipeline/coinjoin-runs/notebook-local-20261025-170032-3.MsZqSu/notebooks/local-regtest-index.png)
- [Původní persistent-index test včetně ponechaných dat](/tmp/tmp.ALis9oWVsI)

Testy zachovaly data i logy. Core a integrační PBS/MinIO kontejnery byly po běhu
ukončeny; samostatná lokální notebooková relace byla ponechána pro prohlédnutí
s limitem jedné hodiny od svého startu. Zdroj indexu má v jejím kernelu read-only
mount; zápis notebooku a obrázku jde do samostatného adresáře.

## Co výsledek nedokládá

Nový dvoujobový test obsahuje coinbase-only regtestové bloky; nedokládá mainnetovou úplnost
Tx0 kandidátů ani kvalitu CoinJoin heuristik. Neověřuje kapacitu multijob,
full-chain paměť, propustnost CESNET S3 ani skutečné přihlášení a odeslání z Labu
na MetaCentrum. API/UI odeslání je lokálně pokryté mock testy. Nasazení vyžaduje
obrazy obsahující tyto změny; vzdálené `latest` tagy nebyly tímto během aktualizovány.
