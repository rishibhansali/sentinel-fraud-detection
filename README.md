# Sentinel

Sentinel is a **local fraud-detection and case-review portfolio project**. It replays card transactions through three explainable rules, persists flagged cases in PostgreSQL, and gives an analyst a live React queue for investigation and feedback. It also measures a real database optimization and evaluates an optional offline anomaly model. The project is designed to demonstrate the engineering decisions behind a fraud workflow, not to process real payments.

**The flagging decision is deterministic.** A transaction becomes a case when any enabled rule fires. Rule weights change its score and queue position; they do not create or suppress a flag. An optional Isolation Forest score and optional Claude summary annotate an already-created case and never decide the outcome.

The planned local scope (Phases 1–9) is implemented. Deployment and authentication are outside that scope. For a complete technical walkthrough, design rationale, interview notes, and a file-by-file map, read the [Project Guide](docs/PROJECT_GUIDE.md). The [roadmap](docs/ROADMAP.md) records the scope and the [benchmark report](BENCHMARK.md) gives the measurement details.

## What you can demonstrate

1. Replay transactions at a chosen speed, then watch rule-created cases appear in the browser through Redis Pub/Sub and WebSockets.
2. Inspect the fired-rule evidence, claim or release a case, record a fraud/false-positive decision, and see the append-only feedback history.
3. Change a rule's thresholds or weight in the UI. The running pipeline hot-reloads the versioned configuration; past cases retain their original rule evidence.
4. Run the offline anomaly evaluation on original source rows, then optionally attach its score to new rule-created cases. Optionally enable a bounded Claude explanation with your own API key.
5. Reproduce the database benchmark. The same per-user `SELECT * ... ORDER BY ts DESC LIMIT N` query went from a **68.68 ms** warm median before the composite index to **2.02 ms** after it on 3,132,877 local rows (about **34×** faster). Partitioning and the rollup do not explain that specific gain.

```text
ULB CSV -> deterministic augmentation -> PostgreSQL transactions
                                          |
                              replay + recent history
                                          |
                          velocity / amount / geo rules
                                          |
                            flagged_cases + rule evidence
                                          |
                     Redis event -> WebSocket -> React reviewer UI
                                          |
                           claim / feedback / rule edits

Optional, after flagging: Isolation Forest score; Claude summary.
```

| Layer | Technology | Role |
|---|---|---|
| Backend | Python 3.12, FastAPI, psycopg2 | Rules, replay, REST API, case actions, WebSocket endpoint |
| Database | PostgreSQL 16 | Transactions, indexes/partitions, cases, feedback, rule history |
| Live updates | Redis 7 Pub/Sub + WebSockets | Broadcast hints; REST reconciliation repairs missed events |
| Frontend | React 19, Vite 8 | Local case queue, detail, analyst actions, rule controls |
| Offline analysis | pandas, NumPy, scikit-learn, joblib | Original-row split, Isolation Forest, reproducible report |
| Checks | pytest, Node test runner, GitHub Actions | Database-backed backend tests, smoke, offline tests, UI tests/build |

## Run it locally

Use Docker Desktop, Python 3.12, and Node.js 22.12+ (or 20.19+). All dependencies stay in project-local environments. Run commands from the repository root unless a command changes directory.

```sh
docker compose -f infra/docker-compose.yml up -d db redis
docker exec sentinel-db pg_isready -U sentinel -d sentinel
docker exec sentinel-redis redis-cli ping

python3.12 -m venv backend/.venv
backend/.venv/bin/python -m pip install -r backend/requirements.txt
python3.12 -m venv scripts/.venv
scripts/.venv/bin/python -m pip install -r scripts/requirements.txt
(cd frontend && npm ci)
```

**Fresh database only:** apply the eight migrations in order. An existing configured database does not need this step again.

```sh
for migration in infra/migrations/*.sql; do
  docker exec -i sentinel-db psql -U sentinel -d sentinel -v ON_ERROR_STOP=1 < "$migration"
done
```

**Fresh dataset only:** `scripts/ingest.py` downloads the documented [ULB CSV](data/README.md) if needed, assigns reproducible synthetic identities and locations, and loads 11 replicas. It **truncates and replaces `transactions`**; skip this step if you want to keep existing rows. The raw CSV and generated ML artifacts are ignored by Git.

```sh
scripts/.venv/bin/python scripts/ingest.py
docker exec sentinel-db psql -U sentinel -d sentinel -c 'REFRESH MATERIALIZED VIEW CONCURRENTLY user_transaction_rollup;'
```

Refresh the rollup after ingestion because the amount rule reads that cached user baseline. Migration 004 creates the view, but on a fresh database it is still empty until transactions are loaded and refreshed.

Start three terminals:

```sh
# Terminal 1: API (from repository root)
cd backend && .venv/bin/python -m uvicorn app.main:app --port 8000
```

```sh
# Terminal 2: replay an existing range; Ctrl+C to stop
cd backend && .venv/bin/python -m app.pipeline.run_pipeline --start-id 0 --end-id 1000 --speed-multiplier 1000
```

```sh
# Terminal 3: reviewer UI
cd frontend && npm run dev
```

Open **[http://127.0.0.1:5173](http://127.0.0.1:5173)** for the reviewer UI, **[http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)** for interactive API documentation, and **[http://127.0.0.1:8000/health](http://127.0.0.1:8000/health)** for the health endpoint. Vite proxies `/cases`, `/rules`, `/stats`, and `/ws` to the API. Enter an analyst name in the UI to attribute actions; it is not a login. If you have replayed a range before, cases for those transactions are not duplicated. Choose an unprocessed range to see new events. The UI needs the API, PostgreSQL, and Redis running.

The API and tests use `SENTINEL_DB_DSN` (default `postgresql://sentinel:sentinel_dev_only@localhost:5432/sentinel`); replay uses the same URL by default but needs an explicit `--dsn` if yours differs. `REDIS_URL` defaults to `redis://localhost:6379/0`. These credentials are for local development only. See the [guide](docs/PROJECT_GUIDE.md#local-operation-and-verification) for troubleshooting notes.

## Optional analysis and annotations

To evaluate the model on the **284,807 original rows**, run `scripts/.venv/bin/python scripts/train_anomaly.py` after acquiring `data/raw/creditcard.csv`. Each run writes an ignored `artifacts/ml/<run-id>/` directory with `model.joblib`, `manifest.json`, `metrics.json`, and `report.md`. The split is stratified 70/15/15 **before** the database's 11× replication, avoiding replica leakage. The model uses V1–V28 and `log1p(Amount)`; it is an offline baseline, not a fraud probability or case gate.

To annotate *new* cases, add `--ml-artifact-dir ../artifacts/ml/<run-id>` to the backend pipeline command. Use only a local artifact you trust because joblib deserialization can execute code. The pipeline checks the manifest, model hash, feature contract, and dependency versions before replay.

To add an optional case explanation, set `ANTHROPIC_API_KEY` for the pipeline process and add `--claude-summaries`. This sends limited fired-rule evidence for newly flagged transactions to Anthropic, may incur charges, and can delay replay by up to the eight-second request deadline. With the flag absent, no key or external API call is needed. Neither optional feature changes flagging, and neither backfills earlier cases.

## Verify the project

With PostgreSQL and Redis running and migrations applied:

```sh
(cd backend && .venv/bin/python -m pytest)
backend/.venv/bin/python scripts/smoke_phase5.py
scripts/.venv/bin/python -m pytest scripts/tests
(cd frontend && npm test && npm run build)
```

The smoke script boots a real API and pipeline against local Postgres/Redis, exercises the live case flow, and cleans up its reserved rows. [GitHub Actions](.github/workflows/ci.yml) runs the backend suite and smoke with real service containers, the offline data/ML tests, and the frontend tests/build on pull requests and pushes to `main`.

## What the results mean

The benchmark's ~34× warm-median improvement is for one indexed lookup on one local dataset. A Docker restart clears PostgreSQL's buffer cache but not necessarily the host OS page cache; the report does not claim a fully cold disk. The 50 transactions/second streaming check writes to a different partition from the measured user's historical data, so it does not establish same-partition contention or a throughput ceiling.

The model's recorded held-out test AP was **0.291836** and ROC-AUC **0.980504** at a validation-set threshold, but this was a random offline holdout of an anonymized public dataset, not prospective performance. Synthetic user and geography fields make the rules demonstrable, not a measure of real-world fraud quality. The [guide](docs/PROJECT_GUIDE.md#limits-and-next-steps) lists the other intentional local-demo limits and credible next steps.
