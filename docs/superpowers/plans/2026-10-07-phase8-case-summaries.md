# Phase 8 Case Summaries Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add opt-in Claude explanations to cases already created by rules, without changing detection or priority.

**Architecture:** Persist the case before any network request. Pass a minimized evidence object to a bounded Messages API client; persist valid output in nullable case columns, then publish the committed case. Expose the fields through the existing REST and WebSocket projections.

**Tech Stack:** Python 3.12, FastAPI, psycopg2, httpx, Postgres 16, Redis 7, pytest.

**Spec:** `docs/superpowers/specs/2026-10-07-phase8-case-summaries-design.md`

## Global Constraints

- Claude never decides case creation, total score, priority, status, or feedback.
- The feature is off unless `--claude-summaries` is supplied; only then require `ANTHROPIC_API_KEY`.
- Use existing project-local venv and the shared Docker Postgres/Redis; reserve ids 290,000,000–290,009,999 for tests.
- Failed or malformed output leaves summary fields null and still publishes the case.

## Review Focus

- Prompt-injection text in rule details must remain data, not instructions; test the constructed request.
- An HTTP error, timeout, or truncated output must leave the case and event intact; test those paths.
- Replaying an existing case must make no request and preserve its original summary; test it.
- A transaction that fires no rule must make no request; test it.
- Summary text and metadata must be committed before they appear in `case.created`; test with a second DB connection.

---

### Task 1: Schema and projections

**Files:** `infra/migrations/008_case_summaries.sql`, `backend/app/api/cases.py`, `backend/app/realtime/events.py`, `backend/tests/schema/test_migration_008.py`, `backend/tests/realtime/test_events.py`

**Interfaces:** `flagged_cases.ai_summary`, `ai_summary_model`, `ai_summary_generated_at` are nullable; `case_summary(row)` returns text/model keys.

- [ ] Write failing schema and projection tests for nullable fields and JSON summaries.
- [ ] Run focused tests and confirm the expected failures.
- [ ] Add migration and projection fields; apply migration with the documented Docker command.
- [ ] Run focused tests and commit.

### Task 2: Claude client

**Files:** `backend/app/summaries/claude.py`, `backend/tests/summaries/test_claude.py`

**Interfaces:** `ClaudeSummarizer(api_key: str, client: httpx.Client | None = None)` with `model` and `summarize(row: dict, transaction: Transaction) -> str`.

- [ ] Write failing tests for minimized request payload, required API headers, valid text, bad status, empty/truncated/malformed/oversize output, and injected rule details.
- [ ] Run focused tests and confirm failure.
- [ ] Implement the bounded request and response validation with the pinned model and eight-second timeout.
- [ ] Run focused tests and commit.

### Task 3: Pipeline and CLI

**Files:** `backend/app/pipeline/pipeline.py`, `backend/app/pipeline/run_pipeline.py`, `backend/tests/pipeline/test_case_summaries.py`, `backend/tests/pipeline/test_run_pipeline_summary_cli.py`, `backend/tests/pipeline/reserved_ranges.py`

**Interfaces:** `make_pipeline_callback(..., case_summarizer=None)` invokes `summarize(row, transaction)` only for a new committed case. CLI `--claude-summaries` builds the summarizer with `ANTHROPIC_API_KEY`.

- [ ] Write failing real Postgres/Redis tests for success, failure, disabled, duplicate, and unflagged cases, plus CLI key validation.
- [ ] Run focused tests and confirm failure.
- [ ] Add the optional post-commit call, guarded DB update, rollback on failure, and CLI wiring.
- [ ] Run focused tests and commit.

### Task 4: Documentation and final verification

**Files:** `README.md`, `docs/ROADMAP.md`, project `AGENTS.md` if environment details change.

- [ ] Document migration 008, CLI opt-in, data sent, latency/cost boundary, and required setup.
- [ ] Run the complete backend suite in the feature worktree.
- [ ] Integrate into main and run the complete backend suite and build from main.
- [ ] Boot the API and pipeline with real Postgres/Redis and verify the REST/event summary path. If a live Anthropic key is available, verify one real request; otherwise report the external check as pending.
