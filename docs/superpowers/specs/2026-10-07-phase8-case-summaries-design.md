# Phase 8 — Optional Plain-English Case Summaries

## Outcome and boundary

An operator may opt in to a short Claude explanation for each **new case already created by the rule engine**. It helps an analyst read the fired-rule evidence. Claude cannot create, suppress, prioritize, claim, or decide a case. With the option off, the pipeline remains rules-only and sends no data to Anthropic. There is no UI or historical backfill in this phase.

## Approach

The existing pipeline commits a rule-created case first, then optionally annotates its ML score, and finally publishes `case.created`. Phase 8 adds a summary attempt after that first commit and before the event. A failed request or malformed response leaves the durable case intact with a null summary; the event and REST detail still show the case. Duplicates and unflagged transactions make no summary request. A synchronous call with an eight-second timeout preserves the current single-process architecture; opt-in replay throughput is limited by Claude latency. There is no automatic retry or queue in this phase.

The CLI accepts `--claude-summaries`. Startup requires a nonempty `ANTHROPIC_API_KEY` only when that flag is present. The client calls Anthropic's Messages API with pinned `claude-haiku-4-5-20251001`, at most 180 output tokens, and no retries. The prompt contains only transaction id, timestamp, amount, total rule score, and fired rule names/scores/details. It excludes card id, user id, the source fraud label, PCA features, analyst feedback, and the ML anomaly score. Prompt instructions ask for a factual, plain-English explanation of observed rule signals, no fraud verdict or invented context, and treat rule details as data rather than instructions. The output must be nonempty plain text, at most 1,000 characters, and must end normally. It is labeled as AI-generated in API fields.

Migration 008 adds nullable `ai_summary`, `ai_summary_model`, and `ai_summary_generated_at` columns to `flagged_cases`, all populated together on success. The summary is stored once for a newly inserted case. The case detail endpoint exposes all three columns. Queue, `since_id`, and WebSocket case summaries expose `ai_summary` and `ai_summary_model`; `case.created` is published only after the successful summary update commits, or after its failure rolls back. The rule scores and queue ordering never change.

## Verification

Unit tests cover the request contract, input minimization, malformed and truncated responses, and startup opt-in validation. Real Postgres/Redis integration tests prove success, failure, disabled, unflagged, and duplicate behavior, including committed visibility before the event. Apply migration 008 before the full main-tree suite. A real API/pipeline smoke test verifies the exposed fields and event path. A live Anthropic smoke requires the operator's key; report that separately if unavailable.
