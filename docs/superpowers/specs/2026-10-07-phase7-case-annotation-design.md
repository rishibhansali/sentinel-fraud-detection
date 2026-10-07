# Phase 7 — Optional Anomaly Annotation of Rule-Created Cases

## Purpose and boundary

Phase 6 produced a reproducible offline Isolation Forest artifact. Phase 7 lets an operator explicitly use one locally trusted artifact to annotate **new cases already created by deterministic rules**. The rules remain the only flagging gate and still determine `total_score` and `priority_score`. `ml_anomaly_score` is a separate, signed anomaly measure (`-decision_function`), not a probability, verdict, threshold gate, or queue-ranking input.

No UI, summary generation, background worker, retraining, historical backfill, rescore, database migration, or automatic model selection belongs here. The pipeline behaves exactly as before unless the operator supplies `--ml-artifact-dir`.

## Design choice

Scoring before `INSERT flagged_cases` would put inference in the case-creation transaction, so a model error could affect detection and duplicates could trigger needless inference. A separate asynchronous worker would add queue and repair machinery before a demonstrated need. Instead, the pipeline commits the rule-created case first, optionally fetches the transaction's original model fields and updates `ml_anomaly_score` in a second transaction, then reads the case and publishes `case.created`. If annotation fails, it rolls back only that second transaction, logs the error, and publishes the intact case with `ml_anomaly_score=NULL`. A crash between the first commit and annotation may also leave NULL; the existing REST repair path still sees the case. There is no automatic retry in this phase.

Only a newly inserted case is annotated. An unflagged transaction never invokes the scorer; an existing case reached by replay remains first-write-wins and is not overwritten or republished. The score is visible through the existing case REST and WebSocket summary fields after the annotation commit. The `case.created` event is not emitted until the annotation attempt finishes, so a successful event contains the persisted value. Case creation and priority must be identical with annotation disabled, successful, or failed.

## Artifact loading and compatibility

`run_pipeline.py --ml-artifact-dir PATH` loads one artifact once at startup. The path must be an explicit local run directory containing the Phase 6 `manifest.json` and `model.joblib`; a model is never downloaded or selected by "latest". Serialized model files are executable deserialization inputs: the operator must trust this local directory. SHA-256 verification provides corruption detection, not proof that an attacker-supplied artifact is safe.

Before `joblib.load`, validate the manifest's exact feature names/order (`v1`–`v28`, `log_amount`), transform, model type/settings, score formula/orientation, finite threshold, SHA-256 format and equality with model bytes, and compatible Python major.minor plus NumPy, SciPy, scikit-learn, and joblib versions. After load, verify the object is an `IsolationForest`, has 29 fitted features, and its relevant parameters equal the manifest. An absent, malformed, incompatible, or corrupt artifact fails startup with a clear error and no replay. Record its model hash in startup logs. The threshold is validated for provenance but never used to decide case creation.

Phase 7 adds the same pinned numeric/model packages to `backend/requirements.txt` as Phase 6's scripts environment. The feature extractor remains dependency-free. Import the model-loading module only when the CLI flag is used, so ordinary pipeline runs retain the previous no-model path.

## Inference data flow

The current pipeline `Transaction` object lacks `v1`–`v28`; extending that frozen detection type would spread model-only fields through replay, load generation, and existing rule tests. Instead, after the case insert commits, fetch `v1`–`v28` and `amount` by the case's `transaction_id` from the existing `transactions` row. Feed them to `app.ml.features.extract_features`, use the loaded model's `decision_function` on one 29-column row, negate the result, require a finite float, and update only `flagged_cases.ml_anomaly_score` for that new case ID. Do not read `Class`, synthetic location/identity, feedback, or full-lifetime rollups. Keep the `Transaction` and rule engine unchanged.

The optional scorer has one public operation, `score_values(v_values, amount) -> float`, and the pipeline owns the database fetch/update/transaction. This keeps artifact validation and numeric inference separate from case persistence. Runtime annotation errors are logged with case and transaction IDs, without exposing model paths or data values in case API responses.

## Verification and acceptance

- Unit tests reject missing, tampered, malformed, version-incompatible, feature-incompatible, and unfitted model artifacts before replay; a compatible Phase 6-shaped fixture loads and scores with the shared extractor and correct sign.
- Real Postgres/Redis integration tests prove no scoring for unflagged or duplicate transactions, successful score persistence and `case.created` visibility, and fail-open behavior after a scorer error while rule score, priority, case count, and publication stay correct. Tests use a new reserved ID range and clean their own rows.
- A real CLI smoke run with the Phase 6 artifact, a new reserved test transaction pair, and real Postgres/Redis proves the opt-in flag loads and annotates a rule-created case. It cleans up its rows. Run the full backend suite and build checks from `main` after integration.
- Document exact backend setup, artifact path selection, CLI command, null-score failure behavior, and lack of backfill. Phase 6's offline test metrics do not authorize model-gated flagging.
