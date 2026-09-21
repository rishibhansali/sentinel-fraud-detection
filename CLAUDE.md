# Sentinel — Project Notes

## Dev Environment
- Postgres 16 runs in Docker; tests and scripts need it up (real DB, not mocked).
  Start Docker Desktop, then: `docker compose -f infra/docker-compose.yml up -d db`
  Check: `docker exec sentinel-db pg_isready -U sentinel -d sentinel`
- Without the DB, pytest shows psycopg2 connection errors (e.g. 9 failed / 15 errors), not a code bug.
- Backend tests: `cd backend && .venv/bin/python -m pytest` (run the suite; all must pass). Tests need BOTH Docker containers up: Postgres (`sentinel-db`) AND Redis (`sentinel-redis`).
- Scripts have their own venv: `scripts/.venv`.
- Migrations live in `infra/migrations/` (001-007), all must be applied in order (007 = case queue/feedback tables, needed by everything in Phase 5).
- Docs: specs in `docs/superpowers/specs/`, plans/progress logs in `docs/superpowers/plans/`.
- Test DSN default: `postgresql://sentinel:sentinel_dev_only@localhost:5432/sentinel` (override with `SENTINEL_DB_DSN`). Tests use the ONE shared Docker Postgres, not per-worktree DBs.
- Apply a migration: `docker exec -i sentinel-db psql -U sentinel -d sentinel -v ON_ERROR_STOP=1 < infra/migrations/<file>.sql`
- Redis 7 runs in Docker (Phase 5 real-time layer); `backend/tests/realtime` needs it up (real Redis, not mocked).
  Start: `docker compose -f infra/docker-compose.yml up -d redis`; check: `docker exec sentinel-redis redis-cli ping` (expect PONG).
  `REDIS_URL` env var, default `redis://localhost:6379/0`. Tests share the ONE Docker Redis and use unique channel names.
- Fresh worktree has no venv (gitignored). Recreate locally, never globally:
  `python3.12 -m venv backend/.venv && backend/.venv/bin/pip install -r backend/requirements.txt`
- Run the API: `cd backend && .venv/bin/python -m uvicorn app.main:app --port 8000` (needs Postgres and Redis).
- Run the pipeline (replay -> detection -> cases, hot-reloads rules): `cd backend && .venv/bin/python -m app.pipeline.run_pipeline --start-id N --end-id M --speed-multiplier X` (Ctrl+C to stop).
- Phase 5 end-to-end smoke test (real API + pipeline processes, real Postgres/Redis, ~50s, cleans up after itself):
  `backend/.venv/bin/python scripts/smoke_phase5.py` (exit 0 = all steps passed).

## Shared-state hazards
- All tests, the smoke script and dev runs share the ONE Postgres and ONE Redis. Each test/script owns a reserved id range (`backend/tests/pipeline/reserved_ranges.py`); pick a new non-overlapping range and clean up only your own rows.
- Tests that mutate `rules_config` / `rules_config_history` restore them in teardown (exact original rows incl. `updated_at`; delete only history rows with id above the saved max). Never leave a changed rule behind.
- `flagged_cases.rules_config_version` has an FK to `rules_config_history`: delete cases (and their `case_feedback`) BEFORE deleting history rows.
