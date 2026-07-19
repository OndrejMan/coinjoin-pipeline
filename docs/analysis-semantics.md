# Analysis semantics and provenance

This document defines which artifacts produce labels and metrics in the
pipeline. It is the contract to use when interpreting a unified report.

## Emulator labels and detector metrics

Emulator-mode labels are independent of `coinjoin-analysis` detector output:

- JoinMarket positives come from `joinmarket_round_events.json` records that
  match an exported transaction id or destination output.
- Wasabi 2 positives come from successful-broadcast records in the exported
  coordinator `Logs.txt` (or the legacy combined backend log).
- Every new emulator run includes `data/coinjoin_label_manifest.json`, which
  declares capture completeness plus the size and SHA-256 digest of each exact
  producer source and the producer-computed positive transaction count. Only
  sources verified against a complete, engine-matching schema-1.0 manifest are
  used, and the consumer's parsed positive count must match.
- When a verified producer label source is present, exported non-coinbase
  transactions absent from its positive records are negative labels. A
  verified empty source is therefore valid evidence with zero positives.
- When the manifest or a declared source is missing, incomplete, malformed,
  modified, or truncated, every `is_coinjoin` value is `null`,
  `evaluation_scope` is `emulator_labels_unavailable`, and no confusion matrix
  or precision/recall values are emitted.
- For older Wasabi manifests without a producer positive count, a manifest is
  also rejected when its logs contain no parseable broadcast record while
  exported blocks contain a transaction with at least five inputs. New
  manifests can authoritatively declare zero positives without this
  transaction-shape fallback.
- Producer-positive txids must all occur in the exported block set. Any
  unmatched positive makes the complete label set unavailable instead of
  silently removing that transaction from the confusion matrix.

`coinjoin_tx_info.json` remains the baseline-analyzer input to the agreement
comparison. Its wallet/address ownership data may enrich emulator transaction
records, but its `coinjoins` keys never determine `is_coinjoin`. Every report
records `label_provenance`, including source paths and
`baseline_used_for_labels: false`.

When independent labels are available, schema-1.7 reports evaluate both
BlockSci and `coinjoin-analysis` against the same set of exported non-coinbase
transactions. The two confusion matrices are stored under
`detector_evaluations.blocksci` and
`detector_evaluations.coinjoin_analysis`. The legacy
`detection_confusion_matrix` field remains an alias of the BlockSci matrix for
schema-1.x consumers. `coinjoin-analysis` metrics use its normalized effective
output after applying any `false_cjtxs.json*` sidecars recorded in
`baseline_filter`; they do not describe the unfiltered raw detector output.

Analyzer detections whose txids are absent from the exported emulator
transaction universe are reported as `out_of_scope_detected_txids`. They are
not silently discarded or classified as false positives because the producer
label set contains no observation for them. Unknown emulator labels are counted
in `unknown` and excluded from TP/FP/TN/FN rate denominators.

The analyzer-agreement fields have a different meaning from the ground-truth
matrices: `matched_by_both`, `missed_by_blocksci`, and `blocksci_only` compare
the two detector outputs and do not by themselves establish a true positive,
false negative, or false positive.

The producer logs/events and manifest are run evidence and must be preserved.
Historical runs without the manifest intentionally produce unavailable metrics
when re-exported; regenerate them with a rebuilt emulator image rather than
inferring completeness from file presence.

External mode has no emulator labels and retains
`evaluation_scope = "baseline_agreement_only"`.

## Wasabi threshold precedence

The Wasabi 2 heuristic chooses an internal minimum input count when its
optional `inputCount` argument is absent. For pre-height-850237 transactions,
that internal minimum is 50 in production mode and 20 with test values; newer
transactions use 20.

The wrapper and exporter now leave the option unset by default. Thus
`--test-values` affects the internal threshold as designed. An explicit
`--min-input-count N` still overrides the internal height/test-mode threshold,
and the run manifest records that override as `N`; no override is recorded as
`null`. Overrides must be positive integers; zero, negative, and non-numeric
values are command-line errors.

Small regtest Wasabi rounds generally need an explicit `--test-values`. When
production thresholds are used on pre-850237 emulator blocks and BlockSci
detects zero transactions, the JSON and Markdown reports carry a
`wasabi_production_threshold_zero_detections` warning.

## BlockSci bulk detector APIs

- `filter_coinjoin_txes_raw` returns every transaction-level heuristic match
  in the requested range. The report exporter requires this API for non-
  JoinMarket detector counts.
- `filter_coinjoin_txes` is a separate linked-transaction API. It returns both
  endpoints of connections between matched transactions and excludes isolated
  matches. It is useful for linked-chain analysis, not detector metrics.
- `filter_joinmarket_txes` directly scans the range with the selected
  JoinMarket subset detector and returns `(detected, skipped)`. `skipped`
  records searches that reached the configured depth limit.

The exporter fails with a rebuild instruction when the installed BlockSci
module lacks the raw binding; it never silently substitutes the linked subset.

## PBS template inputs

Before rendering a PBS script, the pipeline validates shared-storage paths,
positive CPU counts, memory/scratch size grammar, walltime components, job and
stage tokens, and container-image characters. Invalid values raise `PBSError`
and are not interpolated into PBS directives or shell assignments.

Stage command bodies are generated internally by the wrapper and remain the
only deliberate shell fragments in the templates.

## Run catalog report statuses

`runs list` (`pipeline/client/run_catalog.py::report_status`) classifies each
run's `coinjoinPipeline_data/unified_report.json` into one of:

- `missing` — no report file exists; the export stage has not run.
- `invalid` — the report file exists but cannot be parsed as JSON.
- `stale` — the report exists but an upstream artifact (emulator data,
  baseline, BlockSci config, or mappings) is newer, so it describes a previous
  analyzer run; re-export to refresh it.
- `baseline_agreement_only` — external mode; the report compares BlockSci with
  `coinjoin-analysis` only and intentionally has no ground-truth metrics.
- `emulator_labels_unavailable` — emulator mode, but independent producer
  labels could not be verified (missing/incomplete/hash-mismatched
  `coinjoin_label_manifest.json`, unparseable sources, or unmatched producer
  positives). `is_coinjoin` is `null` everywhere and no confusion matrix or
  precision/recall is emitted; the reason is in
  `emulator_data.label_provenance.unavailable_reason`.
- `diagnostics_missing` — the report predates or omits
  `integration_diagnostics`.
- `diagnostics_not_ok` — integration diagnostics ran and found a problem, or
  reported a status other than the explicit `ok` (the check fails closed).
- `complete` — emulator ground truth was available and diagnostics passed.

Runs made with an emulator image that predates the producer-label manifest
always classify as `emulator_labels_unavailable` after re-export; regenerate
them with a rebuilt emulator image if ground-truth metrics are needed.
