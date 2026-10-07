# Sentinel frontend development

## Dev Environment

- Use Node.js 20.19+ or 22.12+ and the project-local npm environment. From `frontend/`, run `npm ci`; never install packages globally.
- Start the existing backend from `backend/` with `.venv/bin/python -m uvicorn app.main:app --port 8000`. Postgres and Redis must already be running as described in the root project notes.
- From `frontend/`, run `npm run dev` to start Vite on `http://127.0.0.1:5173`. It proxies `/cases`, `/rules`, `/stats`, and `/ws` to `http://127.0.0.1:8000` by default. `SENTINEL_API_ORIGIN` overrides that backend origin for development.
- Run all frontend tests with `cd frontend && npm test` and the production build with `cd frontend && npm run build`. Both must pass before reporting completion.
- `node_modules/` and `dist/` are ignored. Commit `package-lock.json` and use `npm ci` on a fresh worktree.
