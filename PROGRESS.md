# Clean history rebuild of coinjoin-pipeline — working notes

Target: `refs/tags/target` = 9aa44e9 ("test(e2e): run the parallel variants as wrappers of the serial tests"),
tree e16b7ebbe70c928e26a459922fa42bc853e1591d. Remote `local` push URL is disabled.

## Method (reverse peeling)

1. Branch `peel` starts at `target`. Each peel commit REMOVES one logical feature
   (files, hunks in hub files, tests, docs) and its message is the FORWARD commit
   message of the feature it removes (subject + one-sentence body).
2. Peel from the last forward commit to the first until the tree is empty.
3. Forward history is built with `git commit-tree` from the peel trees in reverse:
   forward commit k has tree T(m-k) and message of peel commit m-k+1. The last
   forward commit has the target tree by construction. Script: `.peel/build-forward.sh`.
4. Gate per peel step: `.peel/check.sh` (compile + ruff F/E9 on remaining files).
   pytest at block boundaries only; full pytest + lint at the end.

Message format: `type(scope): subject`, blank line, one sentence why. No trailers.
Types: feat, test, docs, build, ci, chore. Never fix/refactor/style.

## Forward blocks (story order)

1 base, 2 run dirs, 3 local run, 4 report, 5 YAML/options, 6 images, 7 PBS local,
8 Kubernetes, 9 K8s→S3→PBS, 10 run control, 11 research, 12 mainnet, 13 tests/CI, 14 docs.
(Refined in PLAN.md.)

## Status

- [x] clone + target
- [ ] reading/plan

## Log

- 2026-11-12: peeled (forward tail, 46 commits): docs (16), runtime image + CI (7), run-all, e2e tests (14),
  contract test modules (7). pytest after that: 792 passed, 1 skipped.
- Lesson: unit tests use tests/support/persistent-index/{s5cmd,singularity} fakes -> they stay until
  the block 6 test that first needs them (fixed with filter-branch; backup branch peel-backup-1).
- conftest.py imports registry -> lands with registry.py (block 6).
- tests/pipeline/config_support.py -> block 1 test commit.
- Gate: .peel/check.sh (py_compile, ruff F/E9, import every module, pytest --collect-only, bash -n).
- Always recompute line numbers after each deletion (one wrong sed range had to be undone).
- Next: block 9 mainnet (M1..M8 + test), see PLAN.md notes below.

## Block 9 plan (forward)
M1 feat(mainnet): validate the configuration of a mainnet synchronization
M2 feat(mainnet): synchronize Core and Dumplings in a PBS job run by a shipped worker (producer)
M3 feat(mainnet): update the BlockSci index from the frozen Core datadir (blocksci)
M4 feat(mainnet): back up the index, Dumplings and a pruned Core checkpoint (checkpoint)
M5 feat(mainnet): restore an empty working dataset from the S3 backups (restore)
M6 feat(mainnet): pack the archived block files with zstd (pack)
M7 feat(mainnet): clear the working dataset after the synchronization (clear)
M8 feat(mainnet): render a job that proves the backups restore (restore check)
M9 feat(examples): add the mainnet-sync example
M10 test(mainnet): cover the synchronization jobs and their worker
- Block 9 mainnet peeled (12 commits: config, sync jobs (producer+blocksci together), layout, watch,
  checkpoint, restore, pack, clear, restore check, example, test). pytest 716 passed.
- Next: block 8 research (liquidity, statistics, research adapters, coinjoin tools, external, runs).
- Block 8 research peeled (11 commits: mix events, postmix validation, liquidity report, liquidity example,
  coinjoin tools, research adapters, statistics, external-analyze, scenarios utility, runs utility). pytest 593.
- Delayed funds stay part of the base scenario format (not split).
- Next: block 7 run control (run_layout, resume, stop, status, watch).
- Block 7 run control peeled (6 commits: run layout JSON, resume, stop, status+run_records, watch). pytest 569.
- peel.py blank-line rule now keeps the tighter side; .peel/layout_check.py target finds new anomalies.
- Next: block 6 Kubernetes -> S3 -> PBS (design commit order first).
- Shared-storage PBS peeled (7 commits) after S3 in forward order; S3 end tests peeled (4 commits). pytest 303.
- replace.py: anchors may end mid-line now; empty new side deletes whole lines.
- S3 block, BlockSci part peeled (forward order): split parse/detect over an S3 index -> cached workflow ->
  combined job publishes its index -> --blocksci-task parse -> script task -> notebook (JupyterLab,
  connection via S3, seed file) -> read another run's index -> external Bitcoin datadir -> external
  BlockSci index -> update task -> block archive -> chain verification -> persistent index (lock
  module, wiring, backup, ...). pytest 225 at HEAD.
- Gate now also: ruff format of files format-clean at target (.peel/unformatted-at-target.txt),
  layout_check, stale repo paths not already stale at target (.peel/stale-at-target.txt).
- History-wide fixers (run with `git rebase target --exec ...`, idempotent): fmt_amend.sh,
  s5dead.sh, prune_dead.sh, fix_helpers_comment.sh, fix_inputs.py, fiximports_head.sh,
  docfix_external.py, fix_misc.py. Conflicts: take `--theirs` for the file, exec reapplies the fix.
- .peel/verify_history.sh REF runs the gate on every commit target..REF in a unique worktree
  (PYTHONPATH=src:pipeline so the editable install does not shadow the worktree).
- Residue checks after each peel: vulture (uvx vulture src pipeline/exporters scripts --min-confidence 60)
  compared with target by name; dead helpers go in the peel that removed their last user.
- Next (reverse forward order): S3 mappings, S3 Job overrides, registry, S3 full-run, S3 emulate,
  Kubernetes S3 Job, Kerberos, pbs-from-s3, s3_submission, s3_markers, s3_staging, report job,
  combined BlockSci job, coinjoin-analysis job, stage runtime, storage, PBS basics.
- Peeled next (forward order): S3 mappings job -> report mappings section; Job overrides; registry
  pinning; marker waiting -> full-run -> failed-Job diagnostics. Forward order for the rest of the
  S3 block: S3 storage basics -> Kubernetes S3 emulation Job (uploader, staged exporters) -> PBS
  basics -> stage runtime -> analysis/BlockSci/report jobs -> pbs-from-s3 graph (+rollback, Kerberos).
- .peel/rebase_loop.sh continues a fixer rebase (takes the picked commit's side, git rm on delete).
- Verified: .peel/verify_history.sh passed on every commit (snapshot before mappings peel).
- PBS part of the S3 block peeled (forward): PBS settings -> runtime+BlockSci job (+exporter staging,
  in-cluster exporter check, s5cmd>=2.1 gate) -> pbs-from-s3 graph -> rollback -> job IDs/overlap ->
  per-run locks -> exporters on demand -> coinjoin-analysis job -> unified-report job (+qsub deps) ->
  Kerberos? no: marker waiting -> full-run -> Kerberos -> diagnostics -> registry -> ...
- Gate now runs mypy too (caught a dangling config attribute). Extra fixers: fix_submit.py,
  fix_worker.py, fix_stage_docs.py, fix_docs2.py, fix_docs3.py, fix_s5cmd.py.
- Known compromise: "Not part of the Kubernetes → S3 → PBS path" headers of the local modules exist
  before that path in forward order.
- Next: Kubernetes S3 emulation (s3_emulation, s3_staging preflight, kubernetes.py S3 Job, k8s_s3_*.sh,
  container/uploader.*), then S3 storage basics, then block 4 (Kubernetes local), 3 (reports), 2, 1.
- S3 block fully peeled (130 peel commits). Forward start of the S3 block: Kubernetes S3 Job with
  uploader (S3 options, artifact backend) -> Job ownership/rollback -> frontend preflights (S5Client,
  credentials options) -> PBS settings -> runtime+BlockSci job -> pbs-from-s3 -> ...
- Verified 2026-11-12: verify_history (gate incl. mypy) passed on every commit of the snapshot before
  the Kubernetes S3 peels; orphan comment heuristic clean on all commits.
- Next: block 4 (Kubernetes local launch), 3 (report exporters), 2 (local Docker), 1 (base).
- Block 4 (local Kubernetes driver) peeled as one commit (driver option, kubernetes section, launcher).
- Next: block 3 report exporters (split by feature), then block 2 local Docker, then block 1 base.

## 2026-11-12 (cont.)
- Block 3 peels done: subset-matching scan (+ JoinMarket detector options), integration diagnostics, heuristic explanations,
  script metadata, manifest comparison, exporter tree hash, image provenance, base run manifest.
- History fixers this round: fix_uploader.py (uploader image option/provenance belongs to the S3 Job commit),
  fix_pbs_comments.py (PBS/S3 rationale comments in pre-PBS trees), fix_mode.py (artifact "mode" fixtures),
  fix_report_inputs.py (report input hashes belong to the `runs` commit), fix_diffed.py.
- Lesson: always rebase onto the CURRENT hash of a base commit (find it by message); a stale hash replays half the stack.
- Next: scenario checks, emulator ground truth / detector evaluations, base report; then block 2 (local Docker), block 1.
- TODO before forward build: sweep fixer collapsing double blank lines between import groups left by import peels (only where target's file has none), e.g. option_rules.py after the verify-chain peel.
- peel.py key: removing a dict entry whose value is a parenthesized multi-line string leaves the closing paren (seen on HELP images.registry); check diffs after 'key' on such entries.

## 2026-11-12 — finished
- All blocks peeled; the last peel empties the tree. Forward history built with .peel/build-forward.sh:
  branch `clean-history`, 179 commits (122 feat, 31 test, 16 docs, 7 ci, 3 build), tree identical to target.
- Tip checks (worktree of clean-history = target tree): pytest 1220 passed / 13 skipped; ruff, mypy clean; pylint 10.00.
- Every commit message: type(scope): subject + one-sentence body, no trailers, no fix/refactor types.
- Orphan-comment heuristic clean on every commit; gate verification over all commits: see below.
- Late fixers this round: fix_report_inputs, fix_env_prefixes, fix_gitignore, fix_k8s_scenario, fix_run_all_mentions,
  fix_dry_run_k8s, fix_manifest_mode, fix_lint_ci, fix_pylint_bool, fix_exit_codes, fix_config_support,
  fix_import_blanks (sweep), fix_stored_run, fix_sidecar_note.
- Findings in Czech: REVIEW-FINDINGS.md (raw notes in .peel/notes.md).
- Nothing pushed; the original repository is untouched.
- Gate verification over all 179 peel trees (= forward trees): all commits pass.
