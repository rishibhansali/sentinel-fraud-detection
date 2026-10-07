# Phase 6 — Offline Anomaly Features and Evaluation: Design Spec

## Purpose

Establish an honest, reproducible anomaly-model baseline before connecting any
model to the live case workflow. Phase 6 produces an offline feature contract,
a trained Isolation Forest artifact, and a report on held-out source rows. It
does not change which transactions become cases or how analysts work them.

The audience is a developer deciding whether the model merits Phase 7
integration. Success means they can rerun one local command against the same
source CSV, inspect the exact features and split lineage, and reproduce the
reported metrics without touching the running pipeline or database.

## Scope decision

Phase 6 owns feature extraction, offline training, and evaluation. Phase 7 may
load the selected artifact to annotate **already flagged** cases through the
existing nullable `flagged_cases.ml_anomaly_score`; the deterministic rule
engine remains the only flagging gate. Phase 8 owns optional plain-English
summaries after a flag. Phase 9 owns the reviewer UI. The [roadmap](../../ROADMAP.md)
supersedes older phase numbers in historical specs and plans.

Three approaches were considered:

1. Train on all 3,132,877 database rows. This repeats each source row eleven
   times and would overstate the effective sample size.
2. Feed `user_transaction_features` or `user_transaction_rollup` into the
   model. Both aggregate a user's full lifetime, including future rows; their
   `fraud_count` also directly uses the label. Neither is safe as an offline
   model feature. They remain useful for the existing rules/benchmark paths.
3. **Chosen:** train and evaluate on the 284,807 original CSV rows, one row per
   source event, using only fields available on a transaction before a
   decision. This keeps evaluation independent of synthetic identities and
   avoids duplicate weighting.

## Input and lineage

- Input is `data/raw/creditcard.csv` with the original row order and columns
  `Time`, `V1`–`V28`, `Amount`, and `Class`. The file is absent from this
  checkout today. The Phase 6 command must fail with clear acquisition
  guidance when it is missing; it must never call `ingest.py` or modify the
  database. The existing [data notes](../../../data/README.md) identify the
  source and download location.
- Require exactly 284,807 source rows, finite numeric model inputs,
  nonnegative `Amount`, and binary `Class`. Reject a malformed
  file rather than silently dropping or imputing rows. Record its SHA-256 in
  the output manifest.
- Reuse `scripts/augment.py::assign_splits` with its own NumPy generator
  seeded with `scripts/ingest.py::SEED` (`42`), once on the original labels.
  The resulting stratified 70/15/15 assignment is the same one copied to all
  eleven database replicas. Give each original row a stable zero-based
  `source_row_index`; assert that indices are disjoint across train,
  validation, and test. Never split the expanded database rows again.
- Training and evaluation read the CSV only. Synthetic `user_id`, `card_id`,
  `lat`, `lon`, shifted `ts`, case status, feedback, and rule results are not
  inputs. The synthetic geo signal was generated with a class-dependent bias,
  so including it would make the model evaluation misleading.

## Feature contract

Feature order is `v1` through `v28`, then `log_amount = log1p(Amount)`, stored
in the manifest. The `V` fields pass through unchanged. `Time`, `Class`,
`split`, source index, and database IDs are metadata only. Feature extraction
is a pure function shared by offline training and a future Phase 7 inference
path; the same schema and transformation must be used on a database
transaction, where `v1`–`v28` and `amount` already exist.

No per-user history is included in this baseline. A future history feature
must be computed from rows strictly earlier than the scored transaction by
`(ts, id)`, without labels or held-out future data. The existing full-lifetime
rollup cannot satisfy that contract and must not be reused for this model.

## Training and evaluation

- Use an Isolation Forest with `n_estimators=200`, `max_samples=256`,
  `contamination="auto"`, `random_state=42`, and `n_jobs=1`. Fit on **all
  training rows without consulting their labels**. Labels are used to
  establish the stratified split and to evaluate held-out predictions, not
  to select normal-only training rows or tune the model.
- Define `anomaly_score = -decision_function(features)`, so larger values
  always mean more anomalous. Do not present it as a probability or blend it
  with `total_score` in Phase 6.
- Fix the alert budget at 0.5% of validation rows before looking at metrics.
  Choose the numeric threshold from the validation score distribution alone
  at its 99.5th percentile using NumPy's linear quantile method. Flag scores
  greater than or equal to the threshold. Freeze that threshold and all
  model settings before scoring test rows. Report the actual test alert rate,
  which may differ from 0.5% if the score distribution shifts or ties occur.
- Report validation and test sample counts, class prevalence, average
  precision (AP), ROC-AUC, precision, recall, alert count/rate, and the
  confusion counts at the fixed threshold. Show prevalence as the no-skill
  AP reference. No minimum metric is required to pass Phase 6: a weak
  result is a valid finding and does not authorize live integration.
- Compute test metrics once for the finalized model. Do not select features,
  hyperparameters, or thresholds using test labels. Seeing the test result
  does not reset that holdout for a later candidate: later tuning uses train
  and validation only, and an independent untouched holdout is needed for a
  new final comparison.

These controls prevent replica overlap and model-selection leakage in this
project. They do not make the random split a prospective time-based test. The
source `V1`–`V28` fields are already PCA-derived, and their original fitting
provenance is unavailable here. Report those limits with the metrics; do not
describe the held-out result as a production fraud-detection guarantee.

## Outputs and boundaries

Place the dependency-free, pure feature extractor in `backend/app/ml/features.py`
so Phase 7 can reuse it. Put the offline training/evaluation CLI in
`scripts/train_anomaly.py`, run through the project-local `scripts/.venv`.
That CLI writes a
versioned directory under `artifacts/ml/` containing a model artifact, a JSON
manifest, and a JSON/Markdown evaluation report. The manifest records the
source hash, source-row count, split counts, exact feature order and transform,
model settings, score orientation, threshold, dependency versions, seed, and
code revision. Generated artifacts and the source CSV stay out of Git; the
specification, tests, and reproducible command belong in Git. Add any ML
dependency only to the project-local requirements used by the CLI.

No migration, API change, Redis event, automatic case rescore, or background
training job is part of Phase 6. In particular `ml_anomaly_score` remains
`NULL` and rules continue to determine case creation. Model serialization is
for locally trusted artifacts; Phase 7 must verify manifest compatibility
before loading one, and fetch `v1`–`v28` from the transaction row because the
current pipeline `Transaction` object does not carry them.

## Verification and acceptance

- Unit tests prove feature order, `log1p` behavior, rejected invalid inputs,
  stable split assignment, and disjoint source-row indices. A label sentinel
  test proves `Class` never enters the model feature matrix.
- A small fixture test proves fit sees train rows only, validation chooses the
  threshold without labels, and test data is never used to fit or tune. The
  score sign and metric/confusion-count calculations have known examples.
- An end-to-end run on the real original CSV produces a manifest and report;
  a second run with the same CSV and pinned dependencies reproduces split
  counts, scores, threshold, and metrics within stated numerical tolerance.
  The command leaves Postgres, Redis, rules, and cases unchanged.
- Before Phase 6 implementation is called done, run the full backend suite
  and build checks from the main tree as required by `AGENTS.md`, plus the
  real offline CSV evaluation. The source CSV must be acquired first if it
  is still absent.
