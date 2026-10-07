# Phase 9 Reviewer UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver a browser reviewer workspace on the existing Sentinel REST/WebSocket contract.

**Architecture:** A Vite React client uses same-origin proxied API requests. A dedicated case-feed module owns WebSocket-first full reconciliation, event buffering, retry, and periodic repair; views consume its case map. Detail/action and rule modules remain separate from transport and presentation.

**Tech Stack:** Node 23 locally, npm project-local `node_modules`, Vite, React, JavaScript/JSX, CSS, Node built-in test runner, FastAPI/Postgres/Redis backend.

**Spec:** `docs/superpowers/specs/2026-10-07-phase9-reviewer-ui-design.md`

## Global Constraints

- Detection and priority remain backend-owned. UI never infers a fraud verdict from anomaly or AI summary fields.
- No auth claim: `analyst` and `changed_by` are free-text and unauthenticated.
- Use existing REST/WebSocket routes; no backend schema or API mutation for UI convenience.
- Frontend dependencies stay in `frontend/node_modules`; lockfile is committed, dist is ignored.
- All main-tree backend tests and frontend test/build commands must pass; run a real process smoke with Docker services.

## Review Focus

- A WebSocket event arriving during full reconciliation must not be lost.
- Reconnect and `resync` must repair changes to existing ids, not only new ids.
- A 409 case action must show the conflict and refresh stale state.
- Empty and null annotations must not render as numeric zero or a verdict.
- Rule parameter edits must send typed values and only changed fields.

---

### Task 1: Frontend bootstrap

**Files:** `frontend/package.json`, `frontend/package-lock.json`, `frontend/index.html`, `frontend/vite.config.js`, `frontend/AGENTS.md`, `frontend/src/main.jsx`, `frontend/src/bootstrap.css`.

- [ ] Establish pinned React/Vite dependencies in project-local `node_modules`; no global install.
- [ ] Add a minimal visible React shell and a trivial Node test.
- [ ] Run documented `npm test` and `npm run build`; commit bootstrap separately from features.

### Task 2: API and live case feed

**Files:** `frontend/src/api.js`, `frontend/src/caseFeed.js`, `frontend/src/useCaseFeed.js`, `frontend/src/caseFeed.test.js`, `frontend/src/api.test.js`.

- [ ] Write failing tests for API errors, full paging, priority order, filtering, and event buffering.
- [ ] Run tests to observe failure, then implement same-origin REST helpers and WebSocket-first reconciliation.
- [ ] Test retry/resync and cleanup behavior; run test/build and commit.

### Task 3: Review queue and case actions

**Files:** `frontend/src/App.jsx`, `frontend/src/components/CaseQueue.jsx`, `frontend/src/components/CaseDetail.jsx`, `frontend/src/components/AnalystActions.jsx`, `frontend/src/styles.css`.

- [ ] Build queue, filters, search, status/priority display, and detail evidence from the feed.
- [ ] Wire claim/release/feedback to API; refresh on success and conflict.
- [ ] Add responsive styling, keyboard/focus states, and honest labels for ML/AI fields.
- [ ] Run test/build and commit.

### Task 4: Rule controls and documentation

**Files:** `frontend/src/components/RulesView.jsx`, `frontend/src/ruleForm.js`, `frontend/src/ruleForm.test.js`, `README.md`, `docs/ROADMAP.md`.

- [ ] Write failing tests for typed changed-field rule patches and no-op state.
- [ ] Build rule stats, editable config, recent history, and save/error states.
- [ ] Document frontend setup and run commands; update roadmap.
- [ ] Run frontend tests/build and backend suite, then commit.

### Task 5: Integrated verification

- [ ] Run the API and Vite dev server with real Postgres/Redis and reserved-range cases; visually inspect desktop/mobile and exercise a case action and rule read path.
- [ ] Get an independent read-only review and fix important findings.
- [ ] Merge with a PR, then run full frontend tests/build and backend suite from main.
- [ ] Remove feature worktree/branches and restore user-local untracked files.
