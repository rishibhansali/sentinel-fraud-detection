# Sentinel

Real-time fraud detection and reviewer platform — a rules-based scoring
engine, a PostgreSQL optimization benchmark, and a WebSocket-driven case
review queue, built to demonstrate Postgres performance work, real-time
systems architecture, and infrastructure ownership.

**No AI layer, still works.** Every detection decision — the scoring
rules, the Postgres query optimization, the real-time event pipeline, and
the case queue — is deterministic, auditable engineering with zero AI
involvement. Claude's only role anywhere in this system (added in a later
phase) is turning an already-flagged case into a plain-English summary
after the fact — it never makes or influences a detection decision.

## Planned tech stack

- **Backend:** FastAPI (Python)
- **Database:** PostgreSQL 16
- **Real-time:** WebSockets
- **Frontend:** React (later phase)
- **Infra:** Docker Compose locally, AWS + CI/CD later

## Status

Status: Phase 1 in progress — dataset ingestion, naive schema, and the
"before" Postgres benchmark.
