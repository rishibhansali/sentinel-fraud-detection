# Phase 7 Optional Case Annotation Implementation Plan

> Implement task by task with TDD and commit each verified boundary. The user authorized proceeding with the next roadmap task; self-review the design and plan against existing requirements before code.

**Goal:** Add opt-in anomaly scores to newly rule-created cases without changing the flagging gate, priority, or default pipeline behavior.

**Spec:** `docs/superpowers/specs/2026-10-07-phase7-case-annotation-design.md`

**Environment:** Follow `AGENTS.md` Dev Environment exactly. Tests need Docker Postgres and Redis. Use project-local backend `.venv`; never install globally. Original source CSV and Phase 6 artifact exist only in the primary checkout's ignored directories.

## File map

| File | Responsibility |
|---|---|
| `backend/app/ml/scorer.py` | Validate explicit Phase 6 artifact, load once, score shared features |
| `backend/tests/ml/test_scorer.py` | Compatible/incompatible artifact and score-sign tests |
| `backend/app/pipeline/pipeline.py` | Annotate only newly inserted rule cases after first commit; fail open and publish |
| `backend/tests/pipeline/test_ml_annotation.py` | Real Postgres/Redis annotation, duplicate/unflagged, fail-open tests |
| `backend/app/pipeline/run_pipeline.py` | Optional `--ml-artifact-dir` startup wiring |
| `backend/tests/pipeline/test_run_pipeline.py` | CLI missing/invalid artifact startup checks |
| `backend/tests/pipeline/reserved_ranges.py` | Reserve Phase 7 integration/smoke rows |
| `backend/requirements.txt` | Pin local model inference dependencies |
| `README.md`, `docs/ROADMAP.md` | Opt-in command and current phase status |

## Task 1 — Artifact compatibility and pure scoring

- [x] Add pinned `numpy==2.3.5`, `scipy==1.18.1`, `scikit-learn==1.9.1`, `joblib==1.5.2` to `backend/requirements.txt`; install only in `backend/.venv`.
- [x] Write failing tests that create a Phase 6-shaped local model and manifest. Test score `-decision_function` on shared `extract_features`, and reject missing file, bad hash, wrong features/transform/model settings/orientation, invalid threshold, incompatible runtime versions, and unfitted/wrong-width model. Artifact fixtures live in `tmp_path`, not tracked data.
- [x] Implement `load_artifact(path: Path) -> AnomalyScorer` in `backend/app/ml/scorer.py`. Validate manifest JSON and SHA-256 before `joblib.load`, then verify fitted model type/settings/width. Raise `ValueError` with a clear artifact reason. Expose `model_sha256` and `score_values(v_values, amount) -> float`; ensure finite result.
- [x] Run focused backend ML tests and full backend suite, then commit the loader/tests/dependency pin.

## Task 2 — Post-commit annotation with failure isolation

- [x] Reserve `280_000_000..280_009_999` for integration tests and `281_000_000..281_009_999` for the real CLI smoke, with distinct negative user IDs in `reserved_ranges.py`; cleanup deletes case feedback before cases, then transactions, and touches only the owning range.
- [x] Write failing integration tests using real Postgres/Redis. Seed a NYC→London geo-rule pair with distinct `v1`–`v28`/amount; pass an injected scorer spy. Assert only the new flagged case is scored, `ml_anomaly_score` persists, the created event observes that committed value, rule/priority scores remain unchanged, and replaying the same transaction does not rescore or republish. Seed an unflagged transaction and assert no scorer call. Inject scorer failure and assert case persists with NULL, publish still occurs, and log identifies case/transaction IDs.
- [x] Add an optional `anomaly_scorer` callable to `make_pipeline_callback`. After a rule-created case commits, query that transaction's V fields/amount, call the scorer, require finite output, update only `ml_anomaly_score`, and commit. On any annotation exception, roll back only that second transaction, log, then continue to existing case read/publish. Leave the disabled path and rule engine untouched.
- [x] Run focused real-service tests and full backend suite, then commit.

## Task 3 — CLI, documentation, and real artifact smoke

- [x] Add `--ml-artifact-dir PATH` to `run_pipeline.py`; load before rules/harness startup, pass scorer method to the callback, and log model hash. Test `--help` and startup rejection for a nonexistent/incompatible path; omit flag to preserve current behavior.
- [x] Update README with backend local dependency setup, Phase 6 artifact selection, exact opt-in command, fail-open NULL behavior, and no backfill. Update roadmap status for completed Phase 6 and Phase 7.
- [x] Run a real CLI subprocess using the primary checkout's Phase 6 artifact and fresh reserved 281_000_000-range transactions, then query the resulting case for a finite score and unchanged rule gate. Clean case/transactions afterward; capture pre/post counts. Run `backend/.venv/bin/python -m compileall -q backend/app scripts`, `git diff --check`, focused and full suites. Commit docs/CLI/tests.
- [ ] Merge reviewed PR, then run full backend suite, scripts suite, build checks, and a real CLI smoke from **main** per `AGENTS.md`. Record the actual results in this plan/PR, restore the user's stashed notes, and remove the worktree/branch.

## Worktree verification (2026-10-07)

- Baseline: 260 backend tests passed with Docker Postgres and Redis.
- After artifact loading: 272 backend tests passed; after case annotation: 275; after CLI wiring: 277. `compileall`, `pip check`, and `git diff --check` passed.
- Real CLI smoke used the primary checkout's Phase 6 artifact at `artifacts/ml/20261007T155206536532Z-76274b691b16-725016f1d528` and reserved transactions 281,000,001–281,000,002. The geo rule created exactly one case; the stored finite anomaly score was `0.23270480525753767`, and startup logged the artifact hash. Rule and priority scores remained equal. Cleanup restored `(transactions, flagged_cases, case_feedback, rules_config)` counts to `(3132877, 0, 0, 3)`; Redis returned PONG.
- The `main` integration gate remains to be run after PR merge.
