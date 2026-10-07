"""Replay harness: reads existing transactions from Postgres and redelivers
them via callback on a timer that approximates their original arrival
pacing, scaled by a speed multiplier. Simulates a live feed off a static
dataset without a message broker (deliberately out of scope for this phase
-- see Phase 4 plan).
"""
import threading
import time
from datetime import datetime
from typing import Callable, Optional

from app.detection.models import Transaction

# Batch size for keyset pagination -- large enough to amortize round-trips,
# small enough to never hold more than one batch of the ~3.1M-row dataset in
# memory at once.
DEFAULT_BATCH_SIZE = 2000

# How often the stop Event is polled during a long sleep, so stop() halts
# within this granularity instead of waiting out the full remaining sleep.
_STOP_POLL_INTERVAL_SECONDS = 0.1

_SELECT_BATCH = """
    SELECT id, user_id, ts, amount, lat, lon
    FROM transactions
    WHERE id > %(last_id)s
    ORDER BY id ASC
    LIMIT %(batch_size)s;
"""

_SELECT_BATCH_BOUNDED = """
    SELECT id, user_id, ts, amount, lat, lon
    FROM transactions
    WHERE id > %(last_id)s AND id <= %(end_id)s
    ORDER BY id ASC
    LIMIT %(batch_size)s;
"""


class ReplayHarness:
    """Replays rows from `transactions` in id order via keyset pagination on
    the primary key (id, ts) -- confirmed empirically that id order is
    identical to ts order across all 3.1M rows (zero inversions), so no new
    ts index is needed and OFFSET's linear-scan-per-page cost is avoided
    entirely. Never loads more than one batch into memory.

    Pacing is anchored to absolute wall-clock time, not consecutive relative
    gaps: each transaction's target delivery time is computed from a single
    fixed start point, so time spent inside on_transaction() (real DB
    round-trips in Task 3's loader -> score_transaction() -> persist chain)
    delays only that transaction's delivery and never compounds into
    permanent drift across the run. If the harness falls behind schedule, it
    catches up on subsequent rows rather than sleeping negative time.

    No cap on inter-transaction sleep: this dataset's max real gap is 32s
    (avg 0.6s, p99 5s across all 3.1M rows) -- not large enough to warrant
    special-casing. Deliberate decision, not an oversight.

    No loop-on-exhaustion: when [start_id, end_id] is fully replayed, the
    harness stops. Restarting is a manual action -- not building for an
    "always keep running" requirement that isn't one of this phase's exit
    criteria.
    """

    def __init__(
        self,
        conn_factory: Callable[[], object],
        speed_multiplier: float = 1.0,
        batch_size: int = DEFAULT_BATCH_SIZE,
        start_id: int = 0,
        end_id: Optional[int] = None,
    ):
        if speed_multiplier <= 0:
            raise ValueError("speed_multiplier must be > 0")

        # conn_factory (not a caller-owned conn, unlike Task 1's loader):
        # this harness owns a long-running background thread that can outlive
        # a single connection across a long dev session, so it needs to be
        # able to open its own connection rather than share one handed in at
        # construction time.
        self._conn_factory = conn_factory
        self._speed_multiplier = speed_multiplier
        self._batch_size = batch_size
        self._start_id = start_id
        self._end_id = end_id
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self, on_transaction: Callable[[Transaction], None]) -> None:
        if self._thread is not None:
            raise RuntimeError("ReplayHarness already started")
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, args=(on_transaction,), daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()

    def join(self, timeout: Optional[float] = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def _fetch_batch(self, conn, last_id: int) -> list[Transaction]:
        if self._end_id is not None:
            query = _SELECT_BATCH_BOUNDED
            params = {"last_id": last_id, "end_id": self._end_id, "batch_size": self._batch_size}
        else:
            query = _SELECT_BATCH
            params = {"last_id": last_id, "batch_size": self._batch_size}

        with conn.cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall()

        return [
            Transaction(id=row[0], user_id=row[1], ts=row[2], amount=float(row[3]), lat=row[4], lon=row[5])
            for row in rows
        ]

    def _sleep_until(self, target_monotonic: float) -> bool:
        """Sleep in short increments until wall-clock `target_monotonic`,
        checking the stop Event between increments. Returns False (and stops
        sleeping immediately) if stop() is called mid-sleep.
        """
        while True:
            remaining = target_monotonic - time.monotonic()
            if remaining <= 0:
                return True
            if self._stop_event.wait(min(remaining, _STOP_POLL_INTERVAL_SECONDS)):
                return False

    def _run(self, on_transaction: Callable[[Transaction], None]) -> None:
        conn = self._conn_factory()
        try:
            last_id = self._start_id
            wall_start: Optional[float] = None
            first_ts: Optional[datetime] = None

            while not self._stop_event.is_set():
                batch = self._fetch_batch(conn, last_id)
                if not batch:
                    return

                for txn in batch:
                    if self._stop_event.is_set():
                        return

                    if wall_start is None:
                        # First transaction of the run anchors the schedule
                        # and fires immediately.
                        wall_start = time.monotonic()
                        first_ts = txn.ts
                    else:
                        target = wall_start + (txn.ts - first_ts).total_seconds() / self._speed_multiplier
                        if not self._sleep_until(target):
                            return

                    on_transaction(txn)
                    last_id = txn.id
        finally:
            conn.close()
