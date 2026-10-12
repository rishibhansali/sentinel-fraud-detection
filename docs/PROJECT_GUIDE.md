# Sentinel: complete project guide

This is the owner's reference for revising Sentinel before an interview. It describes **the implemented local portfolio project**, its original purpose, architecture, data, behavior, operating commands, measured evidence, tradeoffs, and every tracked file. Start with the [public README](../README.md) for the quick demo and return here for the details.

## The project in one minute

Sentinel demonstrates a small but complete fraud-review loop. It takes the public ULB card-transaction dataset, adds **synthetic** identities, cards, timestamps, and geographic coordinates so customer-history rules can be exercised, and stores 11 copies (3,132,877 rows) in PostgreSQL. A replay process reads those transactions in timestamp order. For each one it loads recent user history and a baseline, evaluates three transparent rules, and creates one case if any enabled rule fires. The FastAPI server exposes case and rule APIs. Redis Pub/Sub feeds WebSocket messages to the React reviewer interface; periodic REST reconciliation repairs gaps. The reviewer claims a case, examines the rule evidence, records a decision, and can edit the rules that future replay uses. Separate offline ML evaluation, optional anomaly annotation, and optional Claude text provide context **after** deterministic flagging.

The point was to practice several concrete engineering problems together: reproducible dataset preparation, leakage prevention, an honest SQL before/after benchmark, explainable detection, transaction-safe case state, live event delivery with recovery, and a usable local interface. It is not a production payment system or an externally deployed service. The planned local phases 1–9 and CI are implemented; production operations and login are separate possible future projects.

## Architecture and event flow

```text
                         original ULB CSV (Time, V1–V28, Amount, Class)
                                      |
                  deterministic 70/15/15 split, synthetic user/card/geo
                                      |
                            11× copy -> transactions
                                      |
               replay cursor -> recent rows + user average -> three rules
                                      |
                        any enabled rule fired? -> flagged_cases
                                      |                 |
                   optional ML score / Claude text   PostgreSQL rule version
                                      |
                     Redis Pub/Sub -> API subscriber -> WebSocket
                                      |                       |
                  React case queue/detail <- REST reconciliation
                                      |
              claim/release/feedback -> DB commit -> case.updated event
                                      |
            rule edit -> versioned DB write -> Redis hint -> pipeline reload
```

The backend API and replay pipeline are **separate processes**. PostgreSQL is the source of truth for transactions, rules, cases, decisions, and audit history. Redis is an ephemeral notification channel; losing a Redis event cannot be treated as losing a committed case. The frontend restores state by querying REST after reconnect and periodically. The pipeline opens its own database connection and commits per transaction; it does not run inside the API process.

### Data and schema

The ULB CSV contains 284,807 original rows, including `Time`, `Amount`, anonymized PCA columns `V1`–`V28`, and binary `Class`. It does **not** contain user identity, card identity, or geography. Ingest assigns 5,000 synthetic users, two possible synthetic cards per user, and seeded locations. A seeded great-circle generator puts roughly 35% of source fraud rows and 3% of legitimate rows far from the assigned home location; that engineered signal is for exercising the geo rule and cannot validate real-world geo accuracy. Eleven replicas have distinct IDs and timestamps but retain the original transaction values and original-row split label. The 70/15/15 split is assigned once to original rows before replication so copies of one original row cannot cross train/validation/test. See [data provenance](../data/README.md).

Migration `001` creates the intentionally naive `transactions` table. `002` adds `(user_id, ts DESC)` for recent-user lookups. `003` rebuilds it as monthly partitions and changes the key to `(id, ts)`, with a default partition. `004` adds the live `user_transaction_features` view and cached `user_transaction_rollup` materialized view. The pipeline's baseline loader reads the **materialized** rollup for fast average-amount lookup; it needs manual refresh and can include future replay rows. `005` seeds three rule rows. `006` creates `flagged_cases` with unique `transaction_id` (replay is idempotent at case insertion). `007` adds append-only rule history, feedback, case status/claim/priority, and nullable ML score; its rule-history ID is the configuration version. `008` adds optional AI summary fields. Apply all migrations in order on a fresh DB only.

The partitions explicitly cover December 2024 through March 2025 plus `transactions_default`. The bulk synthetic dataset is in January 2025, while today's streaming inserts reach the default partition. There is no scheduler for future partitions or for rollup refresh. These are intentional local-demo limits, not invisible automation.

### Rule engine and replay

The three rules are:

| Rule | Evidence and trigger | Edge handling |
|---|---|---|
| Velocity | Count transactions within the configured minute window, including the current transaction; fire at or above `threshold_count`. | An enabled rule's score scales with count/threshold. Recent-history loader has a row cap, so the API bounds the threshold accordingly. |
| Amount versus baseline | Compare amount with the user's historical **average**; fire when absolute relative deviation reaches the configured multiplier. | It uses multiples of the mean, not a z-score; no history or a zero mean yields no fire. |
| Geo impossibility | Compare the current location to the most recent prior user transaction with haversine distance and implied speed. | Ignore distances below a minimum; far-apart transactions at identical timestamps fire automatically. |

Each rule returns `fired`, a numeric sub-score, and a JSON evidence object. The total score is the **weight-normalized average of enabled sub-scores**, with zero enabled weight handled as zero. `any(rule.fired)` is the sole flagging condition. Changing a weight changes total/priority ordering but not flagging; enabled state or thresholds can change flagging. Rule edits validate parameter types/ranges, disallow turning off the last enabled rule, record before/after and analyst attribution in history, then publish a Redis hint after commit. The long-running pipeline reloads an immutable rule snapshot on the hint, with polling fallback (default 30 seconds). A case stores the rule results and rule configuration version that existed when it was created; later edits do not rewrite its evidence.

Replay accepts `--start-id`, `--end-id`, `--speed-multiplier`, and `--row-cap`; its default speed follows transaction timestamp gaps. A case is inserted only once for a transaction. The pipeline commits the case before publishing `case.created`. A separate **priority score** ranks the queue: past false positives for the user's fired rules can demote priority, with a floor of 0.5×; a prior confirmed fraud for that user vetoes demotion. This feedback changes ordering only, never the rule flag or total score. The queue orders by `(priority_score DESC, id DESC)` and uses a keyset cursor.

### Case review, API, and real-time delivery

`flagged_cases` holds the current materialized status (`open`, `in_review`, `confirmed_fraud`, `false_positive`), claim owner/time, score, evidence, optional annotations, and provenance. `case_feedback` is append-only; the latest decision by `(decided_at, id)` determines the projected decided status. Claim/release/feedback use database transactions and conflict checks so two reviewers cannot silently overwrite one another. A correction appends another feedback row instead of erasing history. The UI's analyst name is an attribution string in local storage, **not authentication**.

| Endpoint | Purpose |
|---|---|
| `GET /health` | API process health. |
| `GET /cases` | Pending queue by default; `status`, `limit`, keyset `cursor`; alternate `since_id` scan for all statuses. |
| `GET /cases/{id}` | Full case evidence and decision history. |
| `POST /cases/{id}/claim`, `/release`, `/feedback` | Review actions; 404 for missing case, 409 for conflicting state. |
| `GET /stats/rules` | Decided-case confirmed-fraud/false-positive counts and observed precision per fired rule. |
| `GET /rules`, `GET /rules/history`, `PATCH /rules/{name}` | Current version, audit history, validated edits. |
| `WS /ws/cases` | Case create/update events, heartbeat, and resync hint. |

Redis carries event hints across API/pipeline processes. One subscriber per API instance fans messages out through bounded per-client WebSocket queues; slow clients can be dropped to protect the server. REST remains authoritative. The browser buffers events during a full scan, rescans after reconnect or resync hints, and periodically performs a `since_id=0` reconciliation so updates to *existing* case IDs are repaired too. A nonzero `since_id` only finds newly created cases. That cursor assumes a single case writer: with concurrent writers, ID allocation and commit order may differ, so a future multi-writer design needs an outbox or overlap strategy.

The React interface has a priority-ordered queue with status/search controls, a detail pane with fired-rule evidence and optional annotation, claim/release/decision controls, and a rule view with editable configuration, observed rule stats, and change history. Vite's local proxy joins browser `/cases`, `/rules`, `/stats`, and `/ws` requests to FastAPI on port 8000.

### Optional ML and explanations

Offline `scripts/train_anomaly.py` reads the **original** CSV, not the 11× database table. It uses untouched V1–V28 plus `log1p(Amount)` (29 features), fits a fixed `IsolationForest` on the stratified training rows without labels, chooses the 99.5th percentile of **validation scores** as the alert threshold, and reports validation/test AP, ROC-AUC, precision, recall, alert rate, and confusion counts. Scores are `-decision_function`: higher means more anomalous, **not a probability**. The labeled test split is held out from fit and threshold selection. Each run writes `model.joblib`, `manifest.json`, `metrics.json`, and `report.md` in a new ignored directory. The manifest records source/model hashes, feature order/transform, dependency versions, split counts, threshold, and code revision. The recorded test AP is 0.291836 and ROC-AUC is 0.980504; those are descriptive results from a random offline holdout, not a prospective guarantee. PCA preprocessing provenance is not available.

The optional runtime `--ml-artifact-dir` loads a **trusted** local joblib artifact once, checks its hash and contract, then adds `ml_anomaly_score` to newly rule-created cases. It never gates case creation and does not backfill existing cases. A bad artifact fails startup; a per-case scoring failure leaves the case and rule evidence intact. Never load a joblib file obtained from an untrusted party.

The optional `--claude-summaries` path requires `ANTHROPIC_API_KEY` and calls Anthropic Messages with only transaction ID/time/amount, total rule score, and fired-rule evidence. It omits user/card IDs, source label, PCA columns, analyst feedback, and ML score. The pinned Claude Haiku model returns one or two evidence-grounded sentences after a case is committed. Input/output size bounds, an eight-second deadline, and error handling keep failed calls from removing a case; enabling the option can still delay replay and incur charges. No flag or key means no external call. This is explanation text, not a fraud verdict or ground truth.

## Benchmark: what was actually improved

The locked query is `SELECT * FROM transactions WHERE user_id = %s ORDER BY ts DESC LIMIT %s`. On 3,132,877 rows, Phase 1's warm median was **68.68 ms** (20 runs), versus **2.02 ms** after Phase 2 (20 runs), a 34.0× improvement. The respective recorded cold-first-query values were 76.26 ms and 4.52 ms. The composite `(user_id, ts DESC)` index lets PostgreSQL fetch one user's latest rows in index order and stop at `LIMIT`. The query has no time predicate and the data is concentrated in January 2025, so partition pruning contributes nothing here; the rollup is not referenced by the query. Do not claim the combined index/partition/rollup architecture caused the 34× speedup without this qualification.

The script restarts PostgreSQL before one recorded cold query, then runs warm queries. Restart clears PostgreSQL's own cache, **not necessarily the host OS page cache**. The streaming check sustained 50 transactions/second while measuring historical reads in a **different** partition; under-load warm median was 1.40 ms. It is a cross-partition regression check, not same-partition contention or a throughput-ceiling study. See [BENCHMARK.md](../BENCHMARK.md) for the full methodology and p95 figures.

## Local operation and verification

Prerequisites: Docker Desktop, Python 3.12, Node.js 22.12+ (or 20.19+), and enough disk space for 3.1 million rows. Keep Python dependencies in `backend/.venv` and `scripts/.venv`, and npm dependencies in `frontend/node_modules`. Never install them globally. From the repo root:

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

For a **new database**, apply `infra/migrations/001` through `008` in order:

```sh
for migration in infra/migrations/*.sql; do
  docker exec -i sentinel-db psql -U sentinel -d sentinel -v ON_ERROR_STOP=1 < "$migration"
done
```

For a **new/replaceable transaction table**, run the following. Ingest downloads `data/raw/creditcard.csv` if absent and **truncates `transactions`**, so do not run it to refresh an existing demo you wish to preserve. Refresh the materialized user baseline afterward: migration 004 creates it before any fresh-database transactions have been loaded, and the amount rule reads this cached view.

```sh
scripts/.venv/bin/python scripts/ingest.py
docker exec sentinel-db psql -U sentinel -d sentinel -c 'REFRESH MATERIALIZED VIEW CONCURRENTLY user_transaction_rollup;'
```

If you want only offline ML, acquire the CSV as described in [data/README.md](../data/README.md) and run `scripts/.venv/bin/python scripts/train_anomaly.py`; no DB is needed for training. The raw CSV and generated model artifacts are intentionally ignored.

Start API, replay, and UI in separate terminals:

```sh
cd backend && .venv/bin/python -m uvicorn app.main:app --port 8000
```

```sh
cd backend && .venv/bin/python -m app.pipeline.run_pipeline --start-id 0 --end-id 1000 --speed-multiplier 1000
```

```sh
cd frontend && npm run dev
```

Use `http://127.0.0.1:5173` for the browser UI, `http://127.0.0.1:8000/docs` for Swagger API exploration, and `http://127.0.0.1:8000/health` for a quick API check. The API and tests read `SENTINEL_DB_DSN`, defaulting to `postgresql://sentinel:sentinel_dev_only@localhost:5432/sentinel`. Replay uses that URL as its default `--dsn` value but does not read the environment variable, so pass `--dsn YOUR_URL` for a custom database. `REDIS_URL` defaults to `redis://localhost:6379/0`, and frontend `SENTINEL_API_ORIGIN` can override Vite's proxy target. All three processes and both Docker services must be running for the live demo. Previously flagged transactions do not create duplicate cases; choose an unprocessed ID range if the queue seems unchanged. `Ctrl+C` stops replay. A health response alone does not prove DB/Redis connectivity; inspect `/cases` or run the smoke test.

Run the full checks with Docker services and migrations ready:

```sh
(cd backend && .venv/bin/python -m pytest)
backend/.venv/bin/python scripts/smoke_phase5.py
scripts/.venv/bin/python -m pytest scripts/tests
(cd frontend && npm test && npm run build)
```

Backend integration tests and the ~50-second smoke exercise **real** Postgres and Redis. They reserve ID ranges, remove only their own data, and restore changed rules. If backend tests show connection errors, check Docker before diagnosing application code. GitHub Actions runs backend tests/smoke with real service containers, offline script tests without a DB, and frontend tests/build on pull requests and `main` pushes. On a local machine without Docker Desktop running, DB-backed checks cannot run even though offline/frontend checks can.

## Limits and next steps

For interviews, state the limits plainly. The geography and identities are synthetic and partially correlated with the original fraud label. The offline model uses a random split rather than prospective time-based validation; its AP/ROC numbers do not predict deployment quality. The benchmark demonstrates one specific lookup and a cross-partition load check; it does not establish a general throughput ceiling. The amount baseline is a mean multiplier, not a statistical z-score. The replay's amount baseline reads a precomputed full-lifetime materialized rollup, so it can both become stale and look ahead to future replay transactions; there is no refresh scheduler. Future partitions are not created automatically. Replay commits per transaction and has no robust PostgreSQL reconnect/restart checkpoint. Redis/WebSocket repair is built for the current single case writer; the `since_id` cursor needs redesign for concurrent writers. WebSocket queue bounds count messages rather than bytes. Feedback demotion has no time decay. There is no login, authorization, tenant isolation, production secret management, or deployment configuration; analyst names are only labels. Claude behavior is tested with transport doubles, but a live paid-key call is optional and not required for the local project.

Reasonable next steps *if* this became more than a local portfolio project: real identity/location data and prospective evaluation; automatically managed partitions and rollup refresh; an outbox/checkpointed multi-worker replay; operational metrics and throughput testing; authenticated reviewer actions; proper secret management and deployment. None is required to demonstrate the completed local scope.

## Interview revision prompts

- **Why rules first?** They make the case-creation decision deterministic, inspectable, and testable. ML and LLM outputs are contextual annotations so their failures cannot silently alter the flagging gate.
- **Where did 34× come from?** The composite `(user_id, ts DESC)` index supports the exact predicate, order, and limit. Partitioning and rollup are separate design choices and were not responsible for this query's measured gain.
- **How did you avoid leakage?** Assign stratified splits once to original CSV rows before 11× replication; every copy inherits the same split. Fit Isolation Forest on original train rows; select threshold from validation scores; evaluate test only afterward.
- **How do cases stay consistent?** One unique case per transaction, a stored rules version/evidence snapshot, transactional claim and decision operations, append-only feedback history, and events published after commit.
- **Why Redis plus REST?** Pub/Sub gives low-latency hints but is not durable. PostgreSQL remains authoritative; the UI resynchronizes through REST after gaps and on a timer.
- **What would fail under greater scale?** Single-writer `since_id` ordering, per-transaction commits, missing pipeline DB reconnect, unbounded default partition growth, stale rollup maintenance, and limited backpressure are explicit redesign points.

## File-by-file map

Paths below are relative to the repository root. Empty `__init__.py` files designate Python packages. Placeholder `.gitkeep` files keep otherwise empty directories in Git. The inventory covers the tracked repository; generated data, virtual environments, caches, model artifacts, and frontend build output are intentionally absent.

### Root, data, and documentation

| File | Role |
|---|---|
| `.github/workflows/ci.yml` | Pull-request/main CI: real Postgres/Redis backend suite and smoke, independent offline scripts suite, frontend tests/build. |
| `.gitignore` | Keeps local venvs, downloaded CSV, generated artifacts/build output, and worktrees out of commits. |
| `README.md` | Public GitHub overview, quick local run, architecture and results. |
| `BENCHMARK.md` | Exact benchmark query, cache procedure, latency results, explanation, and load-test caveats. |
| `CLAUDE.md` | Repository development guidance and historical project context. |
| `LICENSE` | Repository license. |
| `data/.gitkeep` | Retains the data directory without committing the raw dataset. |
| `data/README.md` | Dataset origin, synthetic augmentation and split provenance. |
| `docs/ROADMAP.md` | Phase allocation, completed local scope, and future boundaries. |
| `docs/PROJECT_GUIDE.md` | This owner/interview reference and file map. |

The design specs capture intended behavior and boundaries; the plan logs record implementation steps, test evidence, and decisions. Older documents preserve their historical phase numbering and may discuss a feature before later phases completed it. This guide and the current code describe the final local state.

| File | Role |
|---|---|
| `docs/superpowers/specs/2026-07-11-phase1-baseline-design.md` | Initial naive schema, data preparation and baseline benchmark design. |
| `docs/superpowers/specs/2026-09-12-phase2-optimization-design.md` | Index, partition, rollup and comparable after-benchmark design. |
| `docs/superpowers/specs/2026-09-12-phase3-detection-engine-design.md` | Deterministic rule and scoring contract. |
| `docs/superpowers/specs/2026-09-21-phase5-realtime-backend-design.md` | Queue, feedback, rules API and Redis/WebSocket backend design. |
| `docs/superpowers/specs/2026-10-07-ci-design.md` | CI checks, service setup and no-secret policy. |
| `docs/superpowers/specs/2026-10-07-phase6-offline-anomaly-evaluation-design.md` | Leakage-safe offline Isolation Forest evaluation design. |
| `docs/superpowers/specs/2026-10-07-phase7-case-annotation-design.md` | Optional runtime ML score contract and safety checks. |
| `docs/superpowers/specs/2026-10-07-phase8-case-summaries-design.md` | Optional bounded Claude explanation contract. |
| `docs/superpowers/specs/2026-10-07-phase9-reviewer-ui-design.md` | Browser queue/detail/rules/realtime interaction design. |
| `docs/superpowers/plans/2026-07-11-phase1-baseline.md` | Baseline implementation checklist and measurement record. |
| `docs/superpowers/plans/2026-09-12-phase2-optimization.md` | Index/partition/rollup implementation and benchmark evidence. |
| `docs/superpowers/plans/2026-09-12-phase3-detection-engine.md` | Rule engine build and verification record. |
| `docs/superpowers/plans/2026-09-13-phase4-realtime-pipeline.md` | Replay, load generation, persistence and streaming benchmark record. |
| `docs/superpowers/plans/2026-09-21-phase5-realtime-backend.md` | Case review API, real-time delivery and smoke-test record. |
| `docs/superpowers/plans/2026-10-07-ci-checks.md` | CI rollout and validation record. |
| `docs/superpowers/plans/2026-10-07-phase6-offline-anomaly-evaluation.md` | ML implementation/evaluation record, including held-out metrics. |
| `docs/superpowers/plans/2026-10-07-phase7-case-annotation.md` | Model artifact validation and case annotation implementation record. |
| `docs/superpowers/plans/2026-10-07-phase8-case-summaries.md` | Claude summary implementation and checks. |
| `docs/superpowers/plans/2026-10-07-phase9-reviewer-ui.md` | Frontend implementation and verification record. |

### Infrastructure and migrations

| File | Role |
|---|---|
| `infra/.gitkeep` | Retains infrastructure directory. |
| `infra/docker-compose.yml` | Local PostgreSQL 16 and Redis 7 services, ports, health checks, Postgres volume. |
| `infra/migrations/001_init.sql` | Naive original transactions table for the before benchmark. |
| `infra/migrations/002_add_user_id_ts_index.sql` | Concurrent composite user/time index for latest-user lookup. |
| `infra/migrations/003_partition_transactions_by_month.sql` | Transactional table rebuild into monthly/default partitions; recreates index and data. |
| `infra/migrations/004_create_user_transaction_rollup.sql` | Per-user live feature view and materialized rollup plus unique refresh index. |
| `infra/migrations/005_create_rules_config.sql` | Rule configuration table and initial velocity/amount/geo settings. |
| `infra/migrations/006_create_flagged_cases.sql` | Initial unique-per-transaction case persistence and rule evidence. |
| `infra/migrations/007_case_queue_and_feedback.sql` | Versioned rule history, append-only feedback, case queue/claim/priority/ML columns and indexes. |
| `infra/migrations/008_case_summaries.sql` | Optional summary/model/timestamp fields on cases. |

### Backend application

| File | Role |
|---|---|
| `backend/.gitkeep` | Retains backend directory. |
| `backend/requirements.txt` | Pinned Python runtime/test dependencies for local backend venv and CI. |
| `backend/conftest.py` | Shared pytest setup/fixtures. |
| `backend/app/__init__.py` | Application package marker. |
| `backend/app/main.py` | FastAPI factory, routers, health endpoint, Redis subscriber and WebSocket lifecycle. |
| `backend/app/api/__init__.py` | API package marker. |
| `backend/app/api/db.py` | DSN/pool connection dependency and cleanup for API requests. |
| `backend/app/api/schemas.py` | Validated analyst and feedback request bodies. |
| `backend/app/api/cases.py` | Case queue/detail/actions, keyset and since-ID scans, rule outcome stats, post-commit events. |
| `backend/app/api/rules.py` | Versioned rule reads/edits, parameter validation, last-enabled guard, audit, reload hint. |
| `backend/app/api/ws.py` | `/ws/cases` protocol, heartbeats, and client registration. |
| `backend/app/cases/__init__.py` | Case package marker. |
| `backend/app/cases/repository.py` | Transactional claim/release/feedback and detail queries; append-only decision projection. |
| `backend/app/detection/__init__.py` | Detection package marker. |
| `backend/app/detection/models.py` | Typed transactions, baselines, rule configurations, evidence, and score results. |
| `backend/app/detection/config.py` | Load persisted rule configuration into typed objects. |
| `backend/app/detection/velocity.py` | Time-window count rule. |
| `backend/app/detection/amount_baseline.py` | Relative amount-versus-mean rule. |
| `backend/app/detection/geo.py` | Haversine distance math. |
| `backend/app/detection/geo_impossibility.py` | Recent-location implied-speed rule. |
| `backend/app/detection/scoring.py` | Evaluate the three rules and weight-normalize enabled sub-scores. |
| `backend/app/ml/__init__.py` | ML package marker. |
| `backend/app/ml/features.py` | Shared 29-feature extraction/validation contract for training and runtime. |
| `backend/app/ml/scorer.py` | Validate trusted artifact compatibility/hash and produce optional anomaly scores. |
| `backend/app/pipeline/__init__.py` | Replay package marker. |
| `backend/app/pipeline/loader.py` | Recent-user history and baseline SQL loading, order/cap safeguards. |
| `backend/app/pipeline/replay.py` | Transaction cursor with timestamp-based replay pacing. |
| `backend/app/pipeline/pipeline.py` | Score, create case, annotate it, commit and publish case event. |
| `backend/app/pipeline/priority.py` | Feedback-aware false-positive demotion and confirmed-fraud veto. |
| `backend/app/pipeline/rules_provider.py` | Immutable rule snapshots, Redis hot reload, polling fallback. |
| `backend/app/pipeline/run_pipeline.py` | CLI wiring for DB, replay bounds/speed, rules, optional ML and Claude. |
| `backend/app/pipeline/load_generator.py` | Synthetic streaming load used in benchmark/testing. |
| `backend/app/realtime/__init__.py` | Real-time package marker. |
| `backend/app/realtime/config.py` | Redis URL and channel configuration. |
| `backend/app/realtime/events.py` | Stable case event/summary payload construction. |
| `backend/app/realtime/manager.py` | Bounded WebSocket client queues and fanout/resync control. |
| `backend/app/realtime/publisher.py` | Best-effort Redis event publishing after DB commits. |
| `backend/app/realtime/subscriber.py` | Redis subscription/reconnect and broadcast callback. |
| `backend/app/summaries/__init__.py` | Summary package marker. |
| `backend/app/summaries/claude.py` | Bounded Anthropic Messages request and evidence-grounded response validation. |

### Backend tests

The database-backed tests use reserved ID ranges and cleanup so the local shared Postgres/Redis services remain usable. Test names below describe the responsibility of each file.

| File | Verifies |
|---|---|
| `backend/tests/test_health.py` | Health route behavior. |
| `backend/tests/api/__init__.py` | API test package marker. |
| `backend/tests/api/conftest.py` | API test fixtures. |
| `backend/tests/api/test_case_actions.py` | Claim, release, decision, correction and conflict API behavior. |
| `backend/tests/api/test_publish.py` | Post-commit case event publishing. |
| `backend/tests/api/test_queue.py` | Queue status filters, priority order, pagination and detail. |
| `backend/tests/api/test_since_id.py` | Creation-cursor scans and reconciliation shape. |
| `backend/tests/api/test_stats.py` | Rule outcome counts and precision. |
| `backend/tests/cases/__init__.py` | Case test package marker. |
| `backend/tests/cases/conftest.py` | Case repository fixtures. |
| `backend/tests/cases/test_repository.py` | Transactional case actions and feedback projection. |
| `backend/tests/detection/__init__.py` | Detection test package marker. |
| `backend/tests/detection/test_amount_baseline.py` | Amount rule thresholds/no-baseline cases. |
| `backend/tests/detection/test_config.py` | Persisted rule config loading. |
| `backend/tests/detection/test_geo.py` | Haversine distance computation. |
| `backend/tests/detection/test_geo_impossibility.py` | Geo speed/distance/zero-time edge cases. |
| `backend/tests/detection/test_scoring.py` | Weighted score and enabled-rule behavior. |
| `backend/tests/detection/test_scoring_end_to_end.py` | Combined rule behavior on representative transactions. |
| `backend/tests/detection/test_velocity.py` | Window count, including current transaction. |
| `backend/tests/integration/__init__.py` | Integration test package marker. |
| `backend/tests/integration/test_case_detail_priority.py` | Priority/evidence carried through to case detail. |
| `backend/tests/ml/__init__.py` | ML test package marker. |
| `backend/tests/ml/test_features.py` | Feature order, transform and invalid values. |
| `backend/tests/ml/test_scorer.py` | Artifact checks and runtime anomaly orientation. |
| `backend/tests/pipeline/__init__.py` | Pipeline test package marker. |
| `backend/tests/pipeline/reserved_ranges.py` | Distinct transaction-ID ranges for shared-DB tests. |
| `backend/tests/pipeline/test_baseline.py` | User baseline query behavior. |
| `backend/tests/pipeline/test_case_summaries.py` | Optional summaries in pipeline after case creation. |
| `backend/tests/pipeline/test_load_generator.py` | Synthetic stream load generation. |
| `backend/tests/pipeline/test_loader.py` | Recent-history ordering, cap, and extraction. |
| `backend/tests/pipeline/test_ml_annotation.py` | Optional ML annotation without changing flagging. |
| `backend/tests/pipeline/test_pipeline.py` | Rule-to-case persistence and replay idempotence. |
| `backend/tests/pipeline/test_pipeline_realtime.py` | Pipeline event path. |
| `backend/tests/pipeline/test_priority.py` | Feedback-driven case ranking. |
| `backend/tests/pipeline/test_priority_exit.py` | Priority edge/exit behavior. |
| `backend/tests/pipeline/test_replay.py` | Replay cursor and pacing. |
| `backend/tests/pipeline/test_run_pipeline.py` | Base pipeline CLI validation/wiring. |
| `backend/tests/pipeline/test_run_pipeline_ml_cli.py` | ML CLI path validation/wiring. |
| `backend/tests/pipeline/test_run_pipeline_summary_cli.py` | Claude CLI flag/key validation/wiring. |
| `backend/tests/realtime/__init__.py` | Real-time test package marker. |
| `backend/tests/realtime/test_events.py` | Event payload schema. |
| `backend/tests/realtime/test_redis_pubsub.py` | Real Redis publish/subscribe/reconnect. |
| `backend/tests/rules/__init__.py` | Rules test package marker. |
| `backend/tests/rules/conftest.py` | Rule config snapshot/restore fixtures. |
| `backend/tests/rules/test_hot_reload.py` | Versioned rule updates reaching live pipeline. |
| `backend/tests/rules/test_rules_api.py` | Rule reads/history/edit validation and versioning. |
| `backend/tests/schema/__init__.py` | Schema test package marker. |
| `backend/tests/schema/test_migration_007.py` | Queue/feedback/version schema and constraints. |
| `backend/tests/schema/test_migration_008.py` | Summary schema fields. |
| `backend/tests/summaries/test_claude.py` | Bounded request payload and mocked Anthropic response/error handling. |
| `backend/tests/ws/__init__.py` | WebSocket test package marker. |
| `backend/tests/ws/conftest.py` | WebSocket fixtures. |
| `backend/tests/ws/test_manager.py` | Client fanout, queue bounds, resync. |
| `backend/tests/ws/test_ws.py` | WebSocket route protocol/heartbeats. |

### Frontend

| File | Role |
|---|---|
| `frontend/AGENTS.md` | Frontend-local environment and verification notes. |
| `frontend/index.html` | Vite HTML entry point. |
| `frontend/package.json` | React/Vite scripts and pinned direct dependencies. |
| `frontend/package-lock.json` | Reproducible npm dependency resolution for `npm ci`. |
| `frontend/vite.config.js` | React plugin, local API/WebSocket proxy and test/build configuration. |
| `frontend/src/main.jsx` | Mount React app into DOM. |
| `frontend/src/App.jsx` | Application shell, views, analyst identity, loading and selected-case state. |
| `frontend/src/api.js` | Fetch clients for cases, feedback, rules and stats; API error normalization. |
| `frontend/src/caseFeed.js` | Pure case-feed merge/sort/reconciliation helpers. |
| `frontend/src/useCaseFeed.js` | Browser WebSocket connection, event buffering, reconnect and periodic REST repair. |
| `frontend/src/ruleForm.js` | Rule form parsing/validation and patch preparation. |
| `frontend/src/format.js` | Presentation formatters for dates, amounts and scores. |
| `frontend/src/styles.css` | Responsive visual styling for queue, detail and rules screens. |
| `frontend/src/components/CaseQueue.jsx` | Filter/search/list of prioritized cases. |
| `frontend/src/components/CaseDetail.jsx` | Evidence, history, optional annotations for selected case. |
| `frontend/src/components/AnalystActions.jsx` | Claim/release/decision interactions and errors. |
| `frontend/src/components/RulesView.jsx` | Rule cards, editable settings, stats and audit trail. |
| `frontend/src/api.test.js` | REST client URL/body/error behavior. |
| `frontend/src/caseFeed.test.js` | Feed merge/order and reconciliation helpers. |
| `frontend/src/ruleForm.test.js` | Rule form validation/patch construction. |
| `frontend/src/smoke.test.js` | Built UI source/entry smoke checks. |

### Offline and operational scripts

| File | Role |
|---|---|
| `scripts/.gitkeep` | Retains scripts directory. |
| `scripts/requirements.txt` | Pinned isolated data/ML/benchmark test environment. |
| `scripts/augment.py` | Seeded synthetic identities, cards, locations and stratified original-row split. |
| `scripts/ingest.py` | Download/validate CSV, construct 11 replicas, truncate/COPY into transactions. |
| `scripts/benchmark.py` | Before/after per-user SQL latency, restart-first cold and warm samples. |
| `scripts/benchmark_streaming.py` | Query latency while synthetic pipeline writes run elsewhere in partitions. |
| `scripts/ml_dataset.py` | Validate original CSV and produce shared features/labels/splits/hash. |
| `scripts/ml_evaluation.py` | Fixed Isolation Forest fit, validation threshold and held-out metrics. |
| `scripts/train_anomaly.py` | Offline CLI and immutable model/manifest/metrics/report run output. |
| `scripts/smoke_phase5.py` | Real local API+pipeline+Redis+Postgres workflow, isolated data and cleanup. |
| `scripts/tests/test_augment.py` | Synthetic geography/identity/split reproducibility. |
| `scripts/tests/test_ml_dataset.py` | Source schema, features, splits, hashes and invalid input. |
| `scripts/tests/test_ml_evaluation.py` | Threshold, score orientation, metric math and split isolation. |
| `scripts/tests/test_train_anomaly.py` | CLI and artifact/report creation/validation. |
