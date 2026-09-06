# Sentinel

Real-time fraud detection and reviewer platform — a deterministic rules-based scoring engine, a PostgreSQL query-optimization benchmark, and a WebSocket-driven case-review queue, with zero AI in the actual detection path.

<!-- TODO: add a screenshot once the case-review UI exists (Phase 4+) -->

## Status: Phase 1 in progress

This project is under active development, not a finished product. Phase 1 (dataset ingestion, naive schema, "before" Postgres benchmark) is in progress; later phases build scoring, real-time delivery, and the review UI on top of it. Nothing here is runnable yet — the sections below describe the design as specified, not a working demo.

**No AI layer, still works.** Every detection decision — the scoring rules, the query optimization, the event pipeline, and the case queue — is deterministic, auditable engineering with zero AI involvement. Claude's only planned role, added in a later phase, is turning an already-flagged case into a plain-English summary after the fact; it never makes or influences a detection decision.

## Planned Stack & Reasoning

| Layer | Choice | Why |
|---|---|---|
| Backend | FastAPI (Python) | Minimal ceremony for a `/health`-only Phase 1 deliverable; dependency management via `requirements.txt` rather than Poetry since there's no lockfile-worthy dependency graph yet |
| Database | PostgreSQL 16 | The project's actual subject is Postgres query optimization (index design, cold-cache behavior) — the schema is deliberately under-indexed in Phase 1 so the benchmark has a real "before" to measure |
| Real-time | WebSockets | The case-review queue needs push delivery to reviewers, not polling |
| Frontend | React (later phase) | Deferred until the backend and data model are proven — a UI has nothing to render until scoring exists |
| Infra | Docker Compose locally, AWS + CI/CD later | Local Postgres control is required for the cold-cache benchmark methodology (below); cloud deployment isn't relevant until later phases |

## Design Decisions Made So Far

Even though Phase 1 has no running code yet, several real engineering decisions are locked in the spec:

- **Leakage-safe train/val/test split.** The dataset (ULB Credit Card Fraud, ~284.8K real rows) gets a stratified 70/15/15 split assigned once per *original* row, before an 11x synthetic-identity replication step that brings the table to ~3.1M rows. Every replica inherits its source row's split assignment unchanged, so the same underlying transaction can never appear in both train and test wearing a different synthetic identity later.
- **`SELECT *` scope is locked across the before/after benchmark.** The optimization benchmark query (`SELECT * FROM transactions WHERE user_id = ? ORDER BY timestamp DESC LIMIT N`) intentionally keeps its column scope fixed between the "before" and "after" measurements, so adding an index is the only variable that changes — narrowing columns later would conflate "added an index" with "reduced payload size" and invalidate the comparison.
- **Cold-cache methodology is a script, not a guess.** Rather than inferring cache state from elapsed idle time, `scripts/benchmark.py` explicitly restarts the Postgres container (`docker compose restart db`), runs one recorded cold query immediately after it's accepting connections, then runs the remaining warm queries back-to-back — making the methodology reproducible from the script itself, not from operator timing.
- **Geo and identity fields are synthetic by necessity, and documented as such.** The source dataset has no `user_id` or location data, so both are seeded deterministically from the row index rather than derived from any real signal — including the "impossible geo" fraud tell, which is intentionally biased toward `Class=1` rows so it's detectable in a later phase without hand-building detection now.

## Setup

There's nothing to run yet — Phase 1 hasn't landed the ingestion script or schema migration. Once it does, this section will cover: starting Postgres via `docker compose up`, applying `infra/migrations/001_init.sql`, running `scripts/ingest.py`, and running `scripts/benchmark.py`.
