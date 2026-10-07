# Phase 4 — Real-Time Pipeline: Progress Log

## Built
- Task 1: `get_recent_transactions()` loader (backend/app/pipeline/loader.py) with
  a runtime-enforced descending-timestamp guard (`UnsortedRecentTransactionsError`),
  keyset pagination on the existing `(user_id, ts DESC)` index, row_cap=100
  (20x velocity's threshold_count=5).
- Task 2: `ReplayHarness` (backend/app/pipeline/replay.py) — dataset-paced replay
  via keyset pagination on `id` (confirmed empirically identical to `ts` order,
  zero inversions across all 3.1M rows), absolute-wall-clock-anchor scheduling
  so callback latency never compounds into permanent drift.
- Task 3: `get_user_baseline()` (loader.py) reading `user_transaction_rollup`
  (not the live view — measured 301ms vs 0.04ms per lookup); `flagged_cases`
  table (migration 006); `pipeline.py` wiring (loader -> baseline -> score ->
  persist-if-any-rule-fired); `run_pipeline.py` CLI driver.
- Task 4: `LoadGenerator` (backend/app/pipeline/load_generator.py) — fixed-rate
  synthetic transaction generator, `insert_only`/`full_pipeline` modes,
  consecutive-miss (threshold=5) fell-behind detection; `reserved_ranges.py`
  registry for test/synthetic id ranges.
- Task 5: `scripts/benchmark_streaming.py` — Phase 2's locked benchmark query
  re-run under 50 tx/sec concurrent `full_pipeline` write load; new
  `BENCHMARK.md` section with results and honest scope-of-claim statement.
- Task 6: `test_run_pipeline_cli_flags_correct_transaction` (subprocess-based
  test proving `run_pipeline.py` itself works end-to-end, not just its pieces
  assembled by hand in test code) and this progress log.

## Key decisions & why
- `get_recent_transactions` takes a caller-owned `conn`; `ReplayHarness` and
  `LoadGenerator` take a `conn_factory` instead — divergence justified by
  call frequency and thread-lifetime, not inconsistency (see Task 1/2 reports).
- Flag predicate is `any(rule fired)`, never a `total_score` cutoff — every
  flagged row must be explainable by a specific rule (auditability principle).
- `user_transaction_rollup` (materialized) over `user_transaction_features`
  (live view) for baseline lookups — an 8000x latency difference, not a
  style preference.
- `ReplayHarness` and `LoadGenerator` are sibling classes, not a shared base —
  same paced-thread shape, different data source and pacing anchor; forcing
  a shared abstraction now would be speculative.
- Regression benchmark (Task 5) compares under-load numbers against Phase 2's
  *fixed documented* median, not a same-run no-load re-measurement — the
  latter has its own warm-up-timing confound (see BENCHMARK.md's Task 5
  section for the full explanation).
- **Task 5's regression check proves cross-partition write/read interaction
  only, not same-partition write contention — this is a boundary on what
  this phase's exit criteria actually proved, not an implementation detail.**
  LoadGenerator's synthetic rows (today's timestamp) land in
  `transactions_default`; the benchmarked user and the entire bulk-loaded
  dataset live in `transactions_2025_01` — a different physical partition.
  Task 5 answers "do writes elsewhere in the table affect reads of an
  already-settled partition?" It does **not** answer "do writes to the same
  partition being read cause contention?" — that scenario has never been
  tested in this phase. Do not read Task 5's "no regression" conclusion as a
  general "optimizations hold under streaming write load" claim; it's scoped
  to the cross-partition case specifically.

## Deviations from planned scope
- `reserved_ranges.py` (Task 4): not in the original phase plan; added because
  four tasks had each independently picked id/user_id ranges with no shared
  registry, and one real near-miss surfaced. Scoped narrowly (plain constants,
  no allocation/collision logic) per explicit instruction.
- Regression check's timed window is duration-based (15s), not Phase 1/2's
  fixed count of 20 runs — at ~2-5ms/query, 20 runs finish in ~100ms, far too
  short to overlap with meaningful sustained load.

## Deferred to a later phase
- **Rollup look-ahead** (Task 3, `get_user_baseline` docstring): baselines
  reflect a user's *entire* lifetime history, not a point-in-time snapshot —
  a transaction early in a replay is scored against a baseline that already
  contains everything that "happens" later. Not point-in-time; not fixed here.
- **Rollup staleness** (same location): nothing refreshes
  `user_transaction_rollup` automatically during a pipeline run; new
  transactions are invisible to baseline lookups until a manual `REFRESH`.
- **No automated future-partition creation** (Task 5, BENCHMARK.md): partitions
  only cover Dec 2024-Mar 2025 plus `transactions_default`. Every real write
  from any future deployment lands in the default partition indefinitely —
  no pg_partman, no cron job. A real deployment needs this solved before the
  default partition's isolation benefit degrades into exactly the
  ever-growing-table problem partitioning was meant to avoid.
- **Rerun the streaming benchmark at Task 4's found throughput ceiling**
  (Task 5): only one load level (50 tx/sec, proven sustainable) was tested;
  never pushed to find where degradation actually begins. Directly connected
  to the commit-batching item below — that's the run that would actually
  answer it.
- **LoadGenerator ramp-up** (Task 4): fixed-rate only, by explicit decision;
  a ramp-up option was scoped out as not asked for by the phase doc.
- **`ON CONFLICT (transaction_id) DO NOTHING`'s silent-first-write-wins
  behavior** (Task 3, `persist_flagged_case`): if a transaction is ever
  processed more than once (e.g. a retry after a connection drop mid-run),
  only the *first* score/rule_results is kept — a second, possibly different
  result (from a reloaded `rules_config`, say) is silently discarded, not
  merged or flagged as a conflict. Never came up as a problem in any test
  because nothing in this phase's tests processes the same transaction
  twice — surfacing it here for the first time as a known, real behavior of
  the code as written, not something caught and deferred mid-task.
- **Connection reconnect-on-failure was never exercised.** None of
  `ReplayHarness`, `LoadGenerator`, or the pipeline wiring retries or
  reconnects if their long-lived connection drops mid-run (network blip, DB
  restart) — the background thread would raise and die silently (`daemon=True`
  threads don't propagate exceptions to the caller). Same as the item above:
  a real, present gap in the code as written, named now rather than assumed
  away.
- **Commit-per-transaction batching — partially answered, not fully closed.**
  Task 3 flagged this as a candidate for revisiting if Task 5's throughput
  numbers showed COMMIT round-trips as the bottleneck. Task 5's numbers
  (50 tx/sec sustained cleanly, full_pipeline p50 ~4ms, never fell behind)
  show commit-per-transaction is **not** a bottleneck *at the one rate
  tested*. Whether it becomes one at higher throughput is genuinely still
  open — directly tied to the "rerun at throughput ceiling" item above,
  since that's the run that would actually answer it.

## Repo state
- Branch: `phase4-realtime-pipeline`, off `main` (PR #3 merged before this
  phase started).
- 57/57 backend tests passing (`cd backend && .venv/bin/python -m pytest`).
- New tables: `flagged_cases` (migration 006). No changes to Phase 1-3 schema.
- `BENCHMARK.md`: Phase 1/2 sections untouched; new Task 5 section appended.
- No PR opened yet for this phase — left as an open item for you to act on.
