"""Synthetic load generator: fabricates and inserts new transactions at a
controlled rate, independent of ReplayHarness's dataset-paced timing (avg
0.6s gaps, p99 5s, max 32s -- Task 2's measurement of the real dataset). Its
job is to find the pipeline's real sustained throughput ceiling and to give
Task 5 genuine streaming write load to benchmark Phase 2's optimizations
against -- bulk-loaded numbers alone don't answer that question.

Sibling to ReplayHarness, not a subclass or shared base: both are paced
background-thread loops with start()/stop()/join(), but they differ in where
data comes from (existing rows vs. fabricated) and what pacing is anchored to
(dataset gaps vs. a fixed target rate) -- forcing a shared base now would be
an abstraction built for two data points, not a proven need.

Fixed-rate only -- no ramp-up. The phase doc's Task 4 text asks for target
rate + duration, nothing more; ramp-up is a Task 5/11 talking-point candidate
if fixed-rate signal proves insufficient, not built speculatively now (same
restraint as no connection pooling until concurrency is proven needed, no
rollup refresh loop, no replay looping).
"""
import random
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

from app.detection.models import RuleConfig, Transaction
from app.pipeline.pipeline import make_pipeline_callback

_STOP_POLL_INTERVAL_SECONDS = 0.1

_INSERT_TRANSACTION = """
    INSERT INTO transactions (
        id, user_id, card_id, ts, amount, lat, lon,
        v1, v2, v3, v4, v5, v6, v7, v8, v9, v10,
        v11, v12, v13, v14, v15, v16, v17, v18, v19, v20,
        v21, v22, v23, v24, v25, v26, v27, v28,
        class, split
    ) VALUES (
        %(id)s, %(user_id)s, 1, %(ts)s, %(amount)s, %(lat)s, %(lon)s,
        0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
        0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
        0, 0, 0, 0, 0, 0, 0, 0,
        0, 'test'
    );
"""


@dataclass(frozen=True)
class LoadTestReport:
    mode: str
    target_rate_per_sec: float
    achieved_rate_per_sec: float
    total_transactions: int
    duration_seconds: float
    latency_p50_ms: float
    latency_p95_ms: float
    latency_p99_ms: float
    latency_max_ms: float
    fell_behind_at_transaction_index: Optional[int]
    fell_behind_at_seconds: Optional[float]


def _percentile(sorted_values: list[float], p: float) -> float:
    if not sorted_values:
        return 0.0
    index = min(int(len(sorted_values) * p), len(sorted_values) - 1)
    return sorted_values[index]


class LoadGenerator:
    """Fabricates and inserts synthetic transactions at a controlled target
    rate for a fixed duration (or up to max_transactions, whichever comes
    first), independent of any real dataset's own timing.

    Two modes:
    - "insert_only": INSERT the fabricated row into `transactions`, nothing
      else. Isolates raw Postgres write cost.
    - "full_pipeline": INSERT, then immediately run the row through Task 3's
      make_pipeline_callback (loader -> baseline -> score -> persist).
      Measures realistic end-to-end throughput, reusing Task 3's wiring
      verbatim rather than reimplementing it.

    `ts` on each fabricated row is real wall-clock time at generation
    (datetime.now(timezone.utc)), not a synthetic historical timestamp like
    ReplayHarness's dataset rows -- this is genuinely simulating new arrivals
    in real time, not replaying history.

    Uses a small reserved pool of synthetic user ids (default 50, randomly
    assigned per transaction), not one unique user per transaction, so
    per-user history accumulates as the run progresses and exercises the
    same index-scan-bounded-by-row_cap path a real active user would. Two
    consequences of this choice, both expected, neither a bug:

    1. amount_baseline will effectively never fire for load-generated
       transactions, regardless of pool size: synthetic users are fresh
       reserved ids with no user_transaction_rollup row, and per
       get_user_baseline's documented staleness limitation
       (backend/app/pipeline/loader.py), nothing refreshes that view
       mid-run. Same root cause as that documented limitation, not a new
       one -- see get_user_baseline's docstring for the full explanation.
    2. velocity fires almost immediately at any realistic target rate: a
       50-user pool at, say, 200 tx/sec is ~4 tx/sec per user, which blows
       through threshold_count=5 within window_minutes=10 almost instantly.
       Most transactions past the first handful per user will flag on
       velocity alone -- this is realistic stress on the flagged_cases
       INSERT path, not a scoring bug.

    One connection, not two: unlike Task 3's wiring (which needed a second
    connection specifically because ReplayHarness's callback interface was
    already closed and couldn't be reopened), LoadGenerator owns its whole
    loop itself -- one conn_factory-sourced connection covers insert and (in
    full_pipeline mode) loader/baseline/persist.
    """

    def __init__(
        self,
        conn_factory: Callable[[], object],
        rules_config: dict[str, RuleConfig],
        target_rate_per_sec: float,
        duration_seconds: float,
        max_transactions: Optional[int] = None,
        mode: str = "full_pipeline",
        user_pool_size: int = 50,
        id_start: int = 0,
        user_id_pool_start: int = -10050,
        seed: int = 0,
    ):
        if target_rate_per_sec <= 0:
            raise ValueError("target_rate_per_sec must be > 0")
        if mode not in ("insert_only", "full_pipeline"):
            raise ValueError(f"unknown mode: {mode!r}")

        self._conn_factory = conn_factory
        self._rules_config = rules_config
        self._target_rate_per_sec = target_rate_per_sec
        self._duration_seconds = duration_seconds
        self._max_transactions = max_transactions
        self._mode = mode
        self._user_pool = [user_id_pool_start + i for i in range(user_pool_size)]
        self._next_id = id_start
        self._rng = random.Random(seed)

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._report: Optional[LoadTestReport] = None

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("LoadGenerator already started")
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()

    def join(self, timeout: Optional[float] = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def report(self) -> LoadTestReport:
        if self._report is None:
            raise RuntimeError("LoadGenerator has not completed a run yet")
        return self._report

    def _fabricate_transaction(self) -> Transaction:
        txn_id = self._next_id
        self._next_id += 1
        user_id = self._rng.choice(self._user_pool)
        amount = self._rng.uniform(1.0, 1000.0)
        lat = self._rng.uniform(-90.0, 90.0)
        lon = self._rng.uniform(-180.0, 180.0)
        ts = datetime.now(timezone.utc)
        return Transaction(id=txn_id, user_id=user_id, ts=ts, amount=amount, lat=lat, lon=lon)

    def _insert(self, conn, txn: Transaction) -> None:
        with conn.cursor() as cur:
            cur.execute(_INSERT_TRANSACTION, {
                "id": txn.id, "user_id": txn.user_id, "ts": txn.ts,
                "amount": txn.amount, "lat": txn.lat, "lon": txn.lon,
            })
        conn.commit()

    def _sleep_until(self, target_monotonic: float) -> bool:
        while True:
            remaining = target_monotonic - time.monotonic()
            if remaining <= 0:
                return True
            if self._stop_event.wait(min(remaining, _STOP_POLL_INTERVAL_SECONDS)):
                return False

    def _run(self) -> None:
        conn = self._conn_factory()
        try:
            on_transaction = (
                make_pipeline_callback(conn, self._rules_config)
                if self._mode == "full_pipeline" else None
            )

            latencies_ms: list[float] = []
            fell_behind_at_index: Optional[int] = None
            fell_behind_at_seconds: Optional[float] = None
            interval = 1.0 / self._target_rate_per_sec
            consecutive_misses = 0
            # A single transient blip (a slow first connection, a cold-cache
            # first score_transaction() call, one GC pause) can miss one
            # scheduled slot without indicating real capacity exhaustion --
            # and per the absolute-anchor scheduling design (same technique
            # as ReplayHarness's drift fix), a one-time blip's backlog gets
            # paid down over the next couple of iterations rather than
            # persisting. 5 consecutive misses against a fixed schedule is
            # not something a one-time blip can produce; it takes sustained
            # inability to keep up. Grounded the same way loader.py's
            # row_cap is grounded against threshold_count, not picked as a
            # round number.
            CONSECUTIVE_MISS_THRESHOLD = 5

            wall_start = time.monotonic()
            index = 0
            while not self._stop_event.is_set():
                now = time.monotonic()
                if (now - wall_start) >= self._duration_seconds:
                    break
                if self._max_transactions is not None and index >= self._max_transactions:
                    break

                target = wall_start + index / self._target_rate_per_sec
                now = time.monotonic()
                if (now - target) > interval:
                    consecutive_misses += 1
                    if fell_behind_at_index is None and consecutive_misses >= CONSECUTIVE_MISS_THRESHOLD:
                        fell_behind_at_index = index
                        fell_behind_at_seconds = now - wall_start
                else:
                    consecutive_misses = 0
                if target > now:
                    if not self._sleep_until(target):
                        break

                txn = self._fabricate_transaction()
                op_start = time.monotonic()
                self._insert(conn, txn)
                if on_transaction is not None:
                    on_transaction(txn)
                latencies_ms.append((time.monotonic() - op_start) * 1000.0)

                index += 1

            actual_duration = time.monotonic() - wall_start
            latencies_ms.sort()
            self._report = LoadTestReport(
                mode=self._mode,
                target_rate_per_sec=self._target_rate_per_sec,
                achieved_rate_per_sec=(len(latencies_ms) / actual_duration) if actual_duration > 0 else 0.0,
                total_transactions=len(latencies_ms),
                duration_seconds=actual_duration,
                latency_p50_ms=_percentile(latencies_ms, 0.50),
                latency_p95_ms=_percentile(latencies_ms, 0.95),
                latency_p99_ms=_percentile(latencies_ms, 0.99),
                latency_max_ms=latencies_ms[-1] if latencies_ms else 0.0,
                fell_behind_at_transaction_index=fell_behind_at_index,
                fell_behind_at_seconds=fell_behind_at_seconds,
            )
        finally:
            conn.close()
