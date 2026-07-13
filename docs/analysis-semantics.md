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
  producer source. Only sources verified against a complete, engine-matching
  schema-1.0 manifest are used.
- When a verified producer label source is present, exported non-coinbase
  transactions absent from its positive records are negative labels. A
  verified empty source is therefore valid evidence with zero positives.
- When the manifest or a declared source is missing, incomplete, malformed,
  modified, or truncated, every `is_coinjoin` value is `null`,
  `evaluation_scope` is `emulator_labels_unavailable`, and no confusion matrix
  or precision/recall values are emitted.
- A complete Wasabi manifest is also rejected when its logs contain no
  parseable broadcast record while exported blocks contain a transaction with
  at least five inputs. This guards legacy log-format drift from becoming a
  confident all-negative ground truth.
- Producer-positive txids must all occur in the exported block set. Any
  unmatched positive makes the complete label set unavailable instead of
  silently removing that transaction from the confusion matrix.

`coinjoin_tx_info.json` remains the baseline-analyzer input to the agreement
comparison. Its wallet/address ownership data may enrich emulator transaction
records, but its `coinjoins` keys never determine `is_coinjoin`. Every report
records `label_provenance`, including source paths and
`baseline_used_for_labels: false`.

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
