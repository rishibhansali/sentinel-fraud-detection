# Sentinel

Real-time fraud detection and reviewer platform — a deterministic rules-based scoring engine, a PostgreSQL query-optimization benchmark, a WebSocket-driven case-review queue, and an optional anomaly annotation for rule-created cases. The detection path remains rules-only.

## Status

Phases 1–9 are implemented: transaction replay and rules-based detection create cases in Postgres; analysts can claim, release, and decide cases through the REST API; Redis fans out case events to WebSocket clients; and rule changes hot-reload into the running pipeline. Phase 6 adds offline Isolation Forest evaluation on original source rows. Phase 7 can annotate newly rule-created cases with a compatible local model when explicitly enabled. Phase 8 can add an optional Claude explanation after a rule-created case is committed. Phase 9 adds a browser reviewer interface for the existing case, feedback, and rule APIs. The [roadmap](docs/ROADMAP.md) and [Phase 9 design](docs/superpowers/specs/2026-10-07-phase9-reviewer-ui-design.md) describe the boundaries.

**Detection uses no AI.** Every flag comes from an auditable rule. Optional ML annotations and AI summaries add context only after a case is flagged; neither determines whether a transaction is flagged.

## Stack

| Layer | Choice | Why |
|---|---|---|
| Backend | FastAPI (Python) | REST APIs, case workflow, and deterministic scoring |
| Database | PostgreSQL 16 | Transaction history, case queue, rule history, and query benchmarks |
| Real-time | Redis 7 + WebSockets | Push case changes across API instances; REST provides missed-event repair |
| Frontend | React + Vite | Local reviewer UI for cases, feedback, and rule controls |
| Infra | Docker Compose locally | Runs Postgres and Redis for development and tests |

## Design Decisions Made So Far

Several engineering decisions shape the implementation:

- **Leakage-safe train/val/test split.** The dataset (ULB Credit Card Fraud, ~284.8K real rows) gets a stratified 70/15/15 split assigned once per *original* row, before an 11x synthetic-identity replication step that brings the table to ~3.1M rows. Every replica inherits its source row's split assignment unchanged, so the same underlying transaction can never appear in both train and test wearing a different synthetic identity later.
- **`SELECT *` scope is locked across the before/after benchmark.** The optimization benchmark query (`SELECT * FROM transactions WHERE user_id = ? ORDER BY timestamp DESC LIMIT N`) intentionally keeps its column scope fixed between the "before" and "after" measurements, so adding an index is the only variable that changes — narrowing columns later would conflate "added an index" with "reduced payload size" and invalidate the comparison.
- **Cold-cache methodology is a script, not a guess.** Rather than inferring cache state from elapsed idle time, `scripts/benchmark.py` explicitly restarts the Postgres container (`docker compose restart db`), runs one recorded cold query immediately after it's accepting connections, then runs the remaining warm queries back-to-back — making the methodology reproducible from the script itself, not from operator timing.
- **Geo and identity fields are synthetic by necessity, and documented as such.** The source dataset has no `user_id` or location data, so both are generated with a fixed seed rather than derived from a real identity signal. The synthetic "impossible geo" pattern is intentionally biased toward `Class=1` rows; see [data notes](data/README.md).

## Setup

Use Python 3.12 and project-local virtual environments. Start Docker Desktop, then run from the repository root:

```sh
docker compose -f infra/docker-compose.yml up -d db redis
docker exec sentinel-db pg_isready -U sentinel -d sentinel
docker exec sentinel-redis redis-cli ping
python3.12 -m venv backend/.venv
backend/.venv/bin/pip install -r backend/requirements.txt
```

On a fresh database, apply `infra/migrations/001` through `008` once, in filename order:

```sh
for migration in infra/migrations/*.sql; do
  docker exec -i sentinel-db psql -U sentinel -d sentinel -v ON_ERROR_STOP=1 < "$migration"
done
```

For normal replay on a fresh database, load the [source dataset](data/README.md). The ingest script downloads it if absent and replaces the contents of `transactions`, so do not run it against a database whose transactions you need to retain:

```sh
python3.12 -m venv scripts/.venv
scripts/.venv/bin/pip install -r scripts/requirements.txt
scripts/.venv/bin/python scripts/ingest.py
```

The test suite and Phase 5 smoke script use reserved transaction IDs and clean up their own data.

```sh
(cd backend && .venv/bin/python -m pytest)
backend/.venv/bin/python scripts/smoke_phase5.py
```

To run the service and replay pipeline, start these in separate terminals from `backend/`:

```sh
.venv/bin/python -m uvicorn app.main:app --port 8000
.venv/bin/python -m app.pipeline.run_pipeline --start-id 1 --end-id 1000 --speed-multiplier 10
```

The API uses `SENTINEL_DB_DSN` (default `postgresql://sentinel:sentinel_dev_only@localhost:5432/sentinel`) and `REDIS_URL` (default `redis://localhost:6379/0`).

## Reviewer UI

With Postgres, Redis, and the API running as above, install frontend dependencies locally and start Vite in another terminal:

```sh
cd frontend
npm ci
npm run dev
```

Open the local URL printed by Vite (normally `http://127.0.0.1:5173`). Its development proxy forwards `/cases`, `/rules`, `/stats`, and `/ws` to the API on port 8000. Enter an analyst name before claiming, releasing, deciding, or correcting a case; the name is stored in this browser and is an attribution label, not authentication. The queue offers status filters, search, priority order, and case detail; the detail shows rule evidence and optional ML/AI context when present. The Rules view edits current rule weights and thresholds and shows stats and change history. WebSocket events update the queue, while REST scans repair missed events after reconnects or periodically. An API connection is required; there is no deployed frontend or login yet.

Run frontend checks with `npm test` and `npm run build` from `frontend/`.

## Offline anomaly evaluation

The Phase 6 command reads the original 284,807-row CSV directly; it does not use the 11x database replicas or connect to Postgres or Redis. Set up the scripts environment shown above, then place `creditcard.csv` at `data/raw/creditcard.csv` as described in the [data notes](data/README.md). To download only that file from the documented mirror without running database ingestion:

```sh
mkdir -p data/raw
curl -fL https://storage.googleapis.com/download.tensorflow.org/data/creditcard.csv -o data/raw/creditcard.csv
```

Run the baseline from the repository root:

```sh
scripts/.venv/bin/python scripts/train_anomaly.py
```

Each run creates a new ignored directory under `artifacts/ml/` with `model.joblib`, `manifest.json`, `metrics.json`, and `report.md`. The manifest records the CSV hash, exact feature order, original-row split counts, fixed model settings, validation threshold, dependency versions, and code revision. The report shows validation and test metrics plus evaluation limits.

## Optional case annotation

Install the pinned backend dependencies from the Setup section and start Postgres and Redis. Choose a **specific, locally trusted** Phase 6 run directory; serialized model files must not come from an untrusted source. For the local run produced during Phase 6:

```sh
(cd backend && .venv/bin/python -m app.pipeline.run_pipeline \
  --start-id 1 --end-id 1000 --speed-multiplier 10 \
  --ml-artifact-dir ../artifacts/ml/20261007T155206536532Z-76274b691b16-725016f1d528)
```

Replace the artifact path with your own Phase 6 run directory if different. Without `--ml-artifact-dir`, the pipeline remains rules-only and leaves `ml_anomaly_score` null. With it, a compatible model loads once at startup; an invalid artifact stops startup before replay. Newly rule-created cases receive `-decision_function` scores after case creation and before their `case.created` event. A higher score means more anomalous; it is not a fraud probability or a flagging threshold. Runtime scoring failures leave the case intact with a null score and are logged. Existing cases are not rescored, and there is no historical backfill. The rule engine remains the only case-creation gate.

## Optional case explanations

Apply migration 008 and set `ANTHROPIC_API_KEY` in the pipeline process environment. Then enable summaries explicitly:

```sh
(cd backend && .venv/bin/python -m app.pipeline.run_pipeline \
  --start-id 1 --end-id 1000 --speed-multiplier 10 --claude-summaries)
```

The pipeline sends only a newly flagged transaction's id, timestamp, amount, total rule score, and fired-rule evidence to Anthropic's Messages API using pinned `claude-haiku-4-5-20251001`. It does not send user/card ids, the source fraud label, PCA features, analyst feedback, or the optional anomaly score. A valid explanation is stored as `ai_summary` with `ai_summary_model` and `ai_summary_generated_at`; the case detail, list, and live event expose it. The explanation is AI-generated context, not a fraud verdict. The external call has an eight-second timeout and no retry, so enabling it can slow replay and incur API charges. Any request/output error keeps the case and rule scores intact with null summary fields. Existing cases are not summarized again or backfilled. Without the flag, no Anthropic key or request is needed. The ML and summary options may be used together; neither changes which transactions are flagged.
