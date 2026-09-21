# Sentinel — Project Notes

## Dev Environment
- Postgres 16 runs in Docker; tests and scripts need it up (real DB, not mocked).
  Start Docker Desktop, then: `docker compose -f infra/docker-compose.yml up -d db`
  Check: `docker exec sentinel-db pg_isready -U sentinel -d sentinel`
- Without the DB, pytest shows psycopg2 connection errors (e.g. 9 failed / 15 errors), not a code bug.
- Backend tests: `cd backend && .venv/bin/python -m pytest` (expects 57 passing after Phase 4).
- Scripts have their own venv: `scripts/.venv`.
- Migrations live in `infra/migrations/` (001-006), applied in order.
- Docs: specs in `docs/superpowers/specs/`, plans/progress logs in `docs/superpowers/plans/`.
- Test DSN default: `postgresql://sentinel:sentinel_dev_only@localhost:5432/sentinel` (override with `SENTINEL_DB_DSN`). Tests use the ONE shared Docker Postgres, not per-worktree DBs.
- Apply a migration: `docker exec -i sentinel-db psql -U sentinel -d sentinel -v ON_ERROR_STOP=1 < infra/migrations/<file>.sql`
- Fresh worktree has no venv (gitignored). Recreate locally, never globally:
  `python3.12 -m venv backend/.venv && backend/.venv/bin/pip install -r backend/requirements.txt`
