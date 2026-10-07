# Phase 6 Offline Anomaly Evaluation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce a reproducible, offline Isolation Forest evaluation on the 284,807 original ULB rows without changing rule-based case creation.

**Architecture:** A standard-library feature extractor in `backend/app/ml/` defines the future inference contract. A separate `scripts/` dataset loader reads the original CSV, validates it, and reproduces the pre-replication split. An offline evaluator trains only on train rows; a CLI writes a model, manifest, and held-out report under an ignored directory.

**Tech Stack:** Python 3.12, existing pandas 2.2.3 and NumPy 2.3.5 in `scripts/.venv`, scikit-learn 1.9.1 (new, scripts venv only), pytest, joblib (transitive scikit-learn dependency, pinned explicitly if imported directly).

**Spec:** `docs/superpowers/specs/2026-10-07-phase6-offline-anomaly-evaluation-design.md`

## Global Constraints

- Input: original `data/raw/creditcard.csv`, exactly 284,807 rows, columns `Time`, `V1`–`V28`, `Amount`, `Class`; never run `ingest.py` or mutate Postgres/Redis.
- Reproduce `assign_splits(labels, np.random.default_rng(42))` once on original rows; train/val/test are 70/15/15 and source row indices never overlap.
- Model features: `v1`–`v28`, then `log_amount = log1p(Amount)`; exclude `Time`, `Class`, IDs, synthetic fields, feedback, rules, and full-lifetime rollups.
- Isolation Forest: `n_estimators=200`, `max_samples=256`, `contamination="auto"`, `random_state=42`, `n_jobs=1`; fit all train rows without labels.
- Score: `-decision_function`, higher means more anomalous. Validation threshold: 99.5th percentile with `np.quantile(..., method="linear")`; alerts use `>=`.
- Report AP, ROC-AUC, prevalence, precision, recall, alert count/rate, TP/FP/TN/FN on validation and untouched test. No metric pass floor or live model use.
- Dependencies stay in project-local venvs. Generated CSV/model/report artifacts remain ignored; committed code and docs contain no source data.

## File map

| File | Responsibility |
|---|---|
| `backend/app/ml/features.py` | Dependency-free shared feature names, extraction, and scalar validation |
| `backend/tests/ml/test_features.py` | Feature contract and invalid-input tests |
| `scripts/ml_dataset.py` | Original CSV schema/size/finite validation, hash, matrix, split indices |
| `scripts/tests/test_ml_dataset.py` | Source validation and split/label-leak tests |
| `scripts/ml_evaluation.py` | Fixed model fitting, score orientation, threshold, metrics |
| `scripts/tests/test_ml_evaluation.py` | Train-only and validation-only controls, known metrics |
| `scripts/train_anomaly.py` | CLI, versioned artifact directory, manifest and report serialization |
| `scripts/tests/test_train_anomaly.py` | Real small-CSV end-to-end artifact and missing-file checks |
| `scripts/requirements.txt`, `.gitignore`, `README.md` | Local dependency, generated-output exclusion, user command |

## Review Focus

1. Malformed CSV with valid-looking headers but `NaN`/infinite V values must fail before fit (Task 2 test).
2. `Class` changes must never change feature values, though it may change split membership (Task 2 test).
3. Altering test rows must not change fitted model or validation threshold (Task 3 test).
4. A validation score tie at the cutoff must report the actual alert rate rather than claim exactly 0.5% (Task 3 test).
5. Existing artifact directory or missing CSV must fail without overwriting output or touching the DB (Task 4 test).

---

### Task 1: Shared feature contract

**Files:** Create `backend/app/ml/__init__.py`, `backend/app/ml/features.py`, `backend/tests/ml/__init__.py`, `backend/tests/ml/test_features.py`.

**Interfaces:** Produce `FEATURE_NAMES: tuple[str, ...]` and `extract_features(v_values: Sequence[object], amount: object) -> tuple[float, ...]`. `v_values` must contain exactly 28 values; `amount` must be finite and nonnegative. No NumPy or pandas import in this module.

- [ ] **Step 1: Write failing tests** in `backend/tests/ml/test_features.py`:

```python
from decimal import Decimal
import math
import pytest
from app.ml.features import FEATURE_NAMES, extract_features

def test_feature_order_and_log_amount():
    assert FEATURE_NAMES == tuple(f"v{i}" for i in range(1, 29)) + ("log_amount",)
    assert extract_features(range(1, 29), Decimal("99")) == tuple(float(i) for i in range(1, 29)) + (math.log1p(99),)

@pytest.mark.parametrize("values,amount", [([0]*27, 1), ([0]*29, 1), ([float("nan")]+[0]*27, 1), ([0]*28, -1), ([0]*28, float("inf"))])
def test_invalid_inputs_fail(values, amount):
    with pytest.raises(ValueError):
        extract_features(values, amount)
```

- [ ] **Step 2: Verify red.** Run `cd backend && .venv/bin/python -m pytest tests/ml/test_features.py -q`; expected import failure for missing `app.ml.features`.
- [ ] **Step 3: Implement** a tuple-producing extractor using `float`, `math.isfinite`, and `math.log1p`; raise `ValueError` for wrong length, conversion failure, nonfinite values, and negative amount. Keep the feature order exactly as tested.
- [ ] **Step 4: Verify green.** Run the focused command, then `cd backend && .venv/bin/python -m pytest`; both must pass.
- [ ] **Step 5: Commit.** `git add backend/app/ml backend/tests/ml && git commit -m 'Add shared anomaly feature contract'`.

### Task 2: Source CSV validation and original-row splits

**Files:** Create `scripts/ml_dataset.py`, `scripts/tests/test_ml_dataset.py`.

**Interfaces:** Consume Task 1's `FEATURE_NAMES` and `extract_features`. Produce frozen `SourceData(features: np.ndarray, labels: np.ndarray, splits: np.ndarray, source_row_indices: np.ndarray, source_sha256: str)` and `load_source_csv(path: Path, expected_rows: int = 284_807) -> SourceData`. The injectable row count is for small tests; the CLI never overrides 284,807.

- [ ] **Step 1: Write failing tests** with a temporary CSV containing the exact 31 source columns. Generate 400 rows with both labels and distinct V values. Assert the returned matrix has 29 columns, its values do not change when only `Class` is changed, source indices form `range(400)`, and split arrays match `assign_splits(labels, np.random.default_rng(42))`. Assert missing file, wrong row count, extra/missing column, negative amount, nonbinary class, and a `NaN`/infinite V value raise a clear error. Compare `source_sha256` to a direct hash of the file.
- [ ] **Step 2: Verify red.** Run `scripts/.venv/bin/python -m pytest scripts/tests/test_ml_dataset.py -q`; expected import failure for missing `ml_dataset`.
- [ ] **Step 3: Implement** `load_source_csv` with `pd.read_csv`, exact header comparison, numeric/finite checks, `extract_features` for every row, and SHA-256 streaming reads. Import `assign_splits` and `SEED` from existing scripts; use a fresh `np.random.default_rng(SEED)` exactly as `ingest.py` does. Build `source_row_indices=np.arange(n)` and assert the three masks partition it. Report missing input with the documented `data/README.md` acquisition path; do not call `ingest.main()`.
- [ ] **Step 4: Verify green.** Run the focused scripts test, existing `scripts/tests/test_augment.py`, then the backend suite via `cd backend && .venv/bin/python -m pytest`.
- [ ] **Step 5: Commit.** `git add scripts/ml_dataset.py scripts/tests/test_ml_dataset.py && git commit -m 'Validate original anomaly dataset and splits'`.

### Task 3: Fit and evaluate without holdout tuning

**Files:** Create `scripts/ml_evaluation.py`, `scripts/tests/test_ml_evaluation.py`; modify `scripts/requirements.txt`.

**Interfaces:** Consume `SourceData`. Produce `Evaluation(model: IsolationForest, threshold: float, metrics: dict[str, dict], validation_scores: np.ndarray, test_scores: np.ndarray)` and `evaluate(data: SourceData) -> Evaluation`. `split_metrics(labels, scores, threshold)` reports `sample_count`, `positive_count`, `prevalence`, `average_precision`, `roc_auc`, `precision`, `recall`, `alert_count`, `alert_rate`, `tp`, `fp`, `tn`, `fn`.

- [ ] **Step 1: Pin/install the local dependency** by adding `scikit-learn==1.9.1` and `joblib==1.5.2` to `scripts/requirements.txt`, then run `scripts/.venv/bin/pip install -r scripts/requirements.txt`. Do not install globally.
- [ ] **Step 2: Write failing tests** that call `split_metrics` on known labels/scores for confusion counts; assert `threshold_for_validation([0, 0, 0, 1]) == 0.985` (linear 99.5th percentile) and use `[0, 0, 1, 1]` to check that tied top scores yield an actual alert rate of `0.5`; assert `-model.decision_function(X)` orientation. Build two `SourceData` fixtures with identical train/validation features but different test features, and assert identical model scores on a fixed probe plus identical threshold. Flip validation labels without changing scores and assert threshold unchanged. Ensure a split lacking both classes raises a clear metric error.
- [ ] **Step 3: Verify red.** Run `scripts/.venv/bin/python -m pytest scripts/tests/test_ml_evaluation.py -q`; expected missing `ml_evaluation` import or function failure.
- [ ] **Step 4: Implement** the fixed `IsolationForest` settings from Global Constraints; call `.fit(features[train_mask])` with no labels; score validation and test only after fit. Set `threshold=float(np.quantile(validation_scores, .995, method="linear"))`. Compute AP with `average_precision_score` and ROC-AUC with `roc_auc_score`, then confusion counts for `scores >= threshold`; calculate precision/recall with zero-denominator guards. Check each evaluation split has both classes before metrics.
- [ ] **Step 5: Verify green.** Run focused scripts tests and full scripts suite, then full backend suite.
- [ ] **Step 6: Commit.** `git add scripts/ml_evaluation.py scripts/tests/test_ml_evaluation.py scripts/requirements.txt && git commit -m 'Train and evaluate fixed anomaly baseline'`.

### Task 4: Offline CLI, artifacts, and documentation

**Files:** Create `scripts/train_anomaly.py`, `scripts/tests/test_train_anomaly.py`; modify `.gitignore`, `README.md`.

**Interfaces:** `train_and_write(source: Path, output_root: Path, expected_rows: int = 284_807, run_id: str | None = None) -> Path` calls Tasks 2–3 and returns a newly created run directory. `run_id` is an internal test seam to prove collisions fail; the CLI parses `--source` and `--output-root` only, with no way to change the production row count or run ID. Output contains `model.joblib`, `manifest.json`, `metrics.json`, `report.md`.

- [ ] **Step 1: Write failing tests** using a 400-row valid CSV and `tmp_path` output root. Call `train_and_write(..., expected_rows=400)`, then assert the four files exist, model reloads, manifest feature order/model settings/source hash/threshold/dependency versions/code revision are exact, and report shows holdout limitations. Assert two runs create different directories; an existing run path is never overwritten. Assert a missing source raises before creating output. Test CLI `--help` and missing-source exit using `subprocess.run` with the current `scripts/.venv` interpreter.
- [ ] **Step 2: Verify red.** Run `scripts/.venv/bin/python -m pytest scripts/tests/test_train_anomaly.py -q`; expected missing `train_anomaly` import.
- [ ] **Step 3: Implement** `train_and_write`: validate/fit/evaluate before `output_root.mkdir`; create a unique directory named with UTC timestamp, source hash prefix, and git revision; fail if it exists. Use `joblib.dump`, JSON writes with sorted keys, SHA-256 of `model.joblib`, and a Markdown report including source/split lineage, metric table, threshold, and limits from the spec. Parse CLI arguments with `argparse`; catch `FileNotFoundError`/`ValueError` and print a concise error. Add `artifacts/ml/` to `.gitignore`. Update README with the local setup, source CSV acquisition link, safe run command, outputs, and no DB mutation.
- [ ] **Step 4: Verify green.** Run focused and full scripts tests, full backend suite, `backend/.venv/bin/python -m compileall -q backend/app scripts`, and `git diff --check`.
- [ ] **Step 5: Commit.** `git add scripts/train_anomaly.py scripts/tests/test_train_anomaly.py .gitignore README.md && git commit -m 'Add offline anomaly evaluation CLI and report'`.

### Task 5: Real-data reproducibility and integration gate

**Files:** Modify only as needed to correct defects proven by this task's checks; update the plan ledger with actual results, not fabricated numbers.

**Interfaces:** Consumes the one-command CLI; produces two ignored real-data run directories and a validation record in the PR description.

- [ ] **Step 1: Confirm the original CSV.** `wc -l data/raw/creditcard.csv` must be 284808 including the header; the CLI must independently check 284807 data rows and record the SHA-256. If absent, download **only** the CSV using the documented source; never run `scripts/ingest.py` or its `main()`.
- [ ] **Step 2: Run twice.** `scripts/.venv/bin/python scripts/train_anomaly.py --source data/raw/creditcard.csv --output-root artifacts/ml` two times. Compare `metrics.json`, manifest split counts, threshold, feature order, and source hash. Any difference outside explicit numerical tolerance is a bug; do not adjust the test set to improve the numbers.
- [ ] **Step 3: Prove external state unchanged.** Read Postgres counts for `transactions`, `flagged_cases`, `case_feedback` and `rules_config` before and after the two runs, plus Redis `PING`; require equality. This is read-only observation, not a mocked check.
- [ ] **Step 4: Run full checks in the primary checkout after integration:** `cd backend && .venv/bin/python -m pytest`, `scripts/.venv/bin/python -m pytest scripts/tests`, and `backend/.venv/bin/python -m compileall -q backend/app scripts`; then review `git diff --check`. Record actual counts and paths in the PR.
