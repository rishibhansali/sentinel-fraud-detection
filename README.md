# Sentinel

Real-time fraud detection and reviewer platform — a deterministic rules-based scoring engine, a PostgreSQL query-optimization benchmark, a WebSocket-driven case-review queue, and an offline anomaly baseline. The detection path remains rules-only.

## Status

The backend through Phase 5 is implemented: transaction replay and rules-based detection create cases in Postgres; analysts can claim, release, and decide cases through the REST API; Redis fans out case events to WebSocket clients; and rule changes hot-reload into the running pipeline. Phase 6 adds an offline Isolation Forest evaluation on original source rows. It does not score live cases. There is no reviewer UI yet. The [Phase 5 spec](docs/superpowers/specs/2026-09-21-phase5-realtime-backend-design.md), [roadmap](docs/ROADMAP.md), and [Phase 6 design](docs/superpowers/specs/2026-10-07-phase6-offline-anomaly-evaluation-design.md) describe the boundaries.

**Detection uses no AI.** Every flag comes from an auditable rule. A later phase may add plain-English summaries after a case is flagged; those summaries will not determine whether a transaction is flagged.

## Stack

| Layer | Choice | Why |
|---|---|---|
| Backend | FastAPI (Python) | REST APIs, case workflow, and deterministic scoring |
| Database | PostgreSQL 16 | Transaction history, case queue, rule history, and query benchmarks |
| Real-time | Redis 7 + WebSockets | Push case changes across API instances; REST provides missed-event repair |
| Frontend | React (planned) | Reviewer UI is a later phase |
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

On a fresh database, apply `infra/migrations/001` through `007` once, in filename order:

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

Each run creates a new ignored directory under `artifacts/ml/` with `model.joblib`, `manifest.json`, `metrics.json`, and `report.md`. The manifest records the CSV hash, exact feature order, original-row split counts, fixed model settings, validation threshold, dependency versions, and code revision. The report shows validation and test metrics plus evaluation limits. Only locally trusted model artifacts should be loaded; future Phase 7 case annotation must check manifest compatibility before loading one. The rule engine remains the only case-creation gate.
