# Phase 9 — Reviewer UI

## Outcome

Give an analyst a usable browser workspace for the existing Sentinel backend: a live case queue, evidence and feedback detail, claim/release/decision actions, rule configuration and history. The UI does not add detection logic, authentication, a new database schema, or deployment infrastructure. Its data source is the existing REST/WebSocket API. The analyst name remains the backend's unauthenticated free-text identifier and the UI states that plainly.

## Interface

Build a responsive React client in `frontend/`. A dark navigation rail and light review canvas make status, scores, and actions easy to scan. The queue has status filters, a transaction/user search, counts, priority ordering, and a selected-case detail panel. Case detail shows fired rule evidence, total and adjusted priority scores, optional anomaly annotation, optional AI-generated explanation, claim owner, feedback history, and an action area. Null optional annotations receive an honest empty state; the anomaly score is not described as a probability and the summary is not described as a verdict. Synthetic geo context is labeled as synthetic.

An analyst enters a name before mutating a case or rule. The UI stores that name locally for convenience but makes no identity or authorization claim. Claim, release, and feedback use the existing endpoints. Conflicts and validation errors remain visible and trigger a fresh detail/queue fetch. The UI never optimistically asserts a decision before the API confirms it.

The Rules view shows current enabled state, weight, typed parameter fields, a rule precision/count summary, and recent history. Saving sends only changed fields with `changed_by`. The interface explains that weight changes ranking, while enabled state and params can change flagging. It does not silently save rule changes.

## Data flow and resilience

In local development, Vite proxies `/cases`, `/rules`, `/stats`, and `/ws` to FastAPI on port 8000. The browser uses same-origin REST and WebSocket URLs. On startup the client opens the WebSocket first, then pages `GET /cases?since_id=0` to reconcile all case summaries. It buffers `case.created` and `case.updated` pushes during the scan and applies them after it finishes. `resync`, reconnect, and a periodic timer repeat the full scan; Redis pushes are hints and REST remains authoritative. Case detail is fetched separately and refreshed for a selected-case event or action. Network errors retain visible data with a retry path and connection state; all timers and sockets are cleaned up on unmount.

This full scan follows the Phase 5 client protocol. It can become expensive if case volume grows substantially; server-side pagination/partial reconciliation is a later scale decision. The queue displays the first 50 filtered cases with a Show more control, sorted by `priority_score DESC, id DESC` like the backend. Rule changes refresh the rule data after the API confirms them.

## Environment and validation

Bootstrap `frontend/` as a project-local npm/Vite/React app with a lockfile and a frontend `AGENTS.md` documenting exact commands. No global installs. Node's built-in test runner covers case ordering/filtering and reconciliation behavior; a production Vite build verifies the bundle. Start the real FastAPI and Vite processes against Docker Postgres/Redis, inspect the UI in a browser with reserved-range test cases, exercise read and write paths, and clean up only those rows. Run the complete backend suite plus frontend tests and build from merged main. Deployment, real authentication, and a live Anthropic call stay outside Phase 9.
