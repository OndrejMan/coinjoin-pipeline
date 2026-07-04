# Migration provenance

Snapshot date: 2026-07-04 (Europe/Prague).

This fresh repository snapshot combines only the controller/orchestration
layers. It does not merge Git histories or copy generated thesis evidence.

| Source | Origin | Snapshot commit | State before copy |
| --- | --- | --- | --- |
| `bitcoinAnalysis` | `git@github.com:OndrejMan/bitcoinAnalysis.git` | `155ffd81df54f66cb0cb59977e9ccb829a0e4d3c` | clean |
| `blocksciEmulatorAnalysis` | `git@github.com:OndrejMan/blocksciEmulatorAnalysis.git` | `027ac7e883e3e562bfd0f92ea0923997d2eba2cd` | clean |

The original repositories and `bitcoinAnalysis/emulation_logs` remain unchanged
and authoritative for historical evidence. No archive/deprecation change is
made until Docker, Podman, Kubernetes/k3d, PBS, report, and JoinMarket cutover
gates pass.

Path mapping:

- `bitcoinAnalysis` launcher, scenarios, builder, scripts, and tests → root.
- `blocksciEmulatorAnalysis/client` → `pipeline/client`.
- `blocksciEmulatorAnalysis/exporters` → `pipeline/exporters`.
- Wrapper compose and lifecycle scripts → `pipeline/`.

`coinjoin-emulator`, `coinjoin-analysis`, and `blocksci` remain independent
image dependencies. Until cutover acceptance, the original runner remains the
rollback path for thesis evidence.
