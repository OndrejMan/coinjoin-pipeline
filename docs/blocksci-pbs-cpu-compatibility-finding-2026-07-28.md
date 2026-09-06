# BlockSci PBS CPU-compatibility finding — 2026-07-28

## Summary

An S3 PBS submission using the image reference
`ghcr.io/ondrejman/blocksci-complete:latest` succeeded on an AMD EPYC 9474F
node with AVX-512, but `blocksci_parser` terminated with `SIGILL` (PBS exit
code 132) on an AMD EPYC 7543 node without AVX-512.

This is a CPU-instruction compatibility problem, not an S3 transfer, RAM, or
scratch-capacity failure.

## Evidence

Both jobs were S3-backed Wasabi regtest BlockSci stages.  Their PBS metadata
and logs were inspected with `qstat -xf` and the corresponding `.o<job-id>`
files.

| PBS job | Result | Host / CPU | Requested resources |
| --- | --- | --- | --- |
| `22480279.pbs-m1.metacentrum.cz` | success, exit 0 | `eluo1-5`, AMD EPYC 9474F; exposes `avx512f` and other AVX-512 flags | 8 CPUs, 64 GB RAM, 100 GB local scratch, 24 h |
| `22481150.pbs-m1.metacentrum.cz` | failure, exit 132 | `turin20`, AMD EPYC 7543; exposes AVX/AVX2 but no AVX-512 flags | 2 CPUs, 16 GB RAM, 8 GB local scratch, 48 h |

The failed job completed S3 downloads and OCI-to-SIF conversion.  Its first
BlockSci command then failed:

```text
Illegal instruction blocksci_parser ... generate-config bitcoin_regtest ...
```

The success on `eluo1-5` proves that the pipeline input layout and normal
BlockSci workflow are valid.  The failure on `turin20` isolates the difference
to the allocation/image combination.  The image is referenced by mutable
`:latest`, so the logs do not prove that the two submissions pulled identical
OCI digests.

## Interpretation

`SIGILL` means the process attempted an unsupported CPU instruction.  The
AVX-512 feature difference makes an AVX-512 instruction in the image the
strongest explanation.  This is an inference from the observed CPU flags; the
job log does not identify the individual instruction.

The BlockSci source tree includes dependencies whose default build settings use
`-march=native` (notably the bundled RocksDB and range-v3 settings).  A
host-optimised build can therefore produce an image that works on the build
host or a compatible compute node but fails on an older/different CPU family.

The reduced resources were not themselves the direct cause.  They coincided
with a different allocation: the successful job requested 24 h, while the
failed job requested 48 h and ran on `turin20`.  PBS routing by requested
resources makes that a plausible scheduling explanation, but it is not proven
from these two job records alone.

## Operational guidance

1. Do not diagnose this as an S3, data-layout, RAM, or scratch failure when
   the log reports `Illegal instruction` from `blocksci_parser`.
2. For the immediate Wasabi regtest retry, use the previously verified
   allocation: 8 CPUs, 64 GB RAM, 100 GB local scratch, and 24 h walltime.
   This is a scheduling workaround, not a portability guarantee.
3. Keep the successful and failed PBS logs, job IDs, and S3 `.pbs` markers as
   evidence.  Do not delete them.
4. For a durable fix, publish and pin a portable BlockSci image that disables
   host-specific `-march=native` optimisation (including the relevant bundled
   dependencies).  Validate it on both AVX-512 and AVX2-only PBS nodes before
   making it the default.
5. Pin the tested image by immutable digest or tag.  `:latest` is pulled at
   job start and can change between otherwise comparable submissions.

## Useful verification commands

```bash
qstat -xf <job-id> | grep -E 'exec_host|Resource_List'
ssh <execution-host> 'lscpu | grep -E "Vendor ID|Model name|Flags"'
grep -n -C 5 -Ei 'illegal instruction|sigill|blocksci_parser' \
  blocksci_analysis_s3.o<job-id>
```
