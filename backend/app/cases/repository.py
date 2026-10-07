"""Case repository: claim / release / feedback / status projection.

Caller-owned `conn` (same convention as pipeline.loader). Each public function
runs as ONE database transaction; the repository owns commit/rollback. It does
NOT publish to Redis -- the API layer does that after commit.
"""
from contextlib import contextmanager

from psycopg2.extras import RealDictCursor

VALID_DECISIONS = ("confirmed_fraud", "false_positive")


class CaseNotFound(Exception):
    def __init__(self, case_id):
        self.case_id = case_id
        super().__init__(f"case {case_id} not found")


class CaseConflict(Exception):
    def __init__(self, case_id, current_status, claimed_by, reason=None):
        self.case_id = case_id
        self.current_status = current_status
        self.claimed_by = claimed_by
        self.reason = reason
        super().__init__(
            f"case {case_id} conflict: status={current_status!r} "
            f"claimed_by={claimed_by!r}" + (f" ({reason})" if reason else "")
        )


@contextmanager
def _tx(conn):
    """One transaction: commit on success, rollback on any error. Forces
    autocommit off for the duration so FOR UPDATE locks span the statements."""
    saved = conn.autocommit
    if saved:
        conn.autocommit = False
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            yield cur
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        if saved:
            conn.autocommit = True


# ---------------------------------------------------------------------------
# Resolution + projection rule: the ONLY place status is computed for the
# decision source.
#
# flagged_cases.status is a denormalized projection of the case_feedback log
# (kept for indexed queue filtering). The LOG WINS on any disagreement: this
# statement always recomputes from the log and never writes an incoming value.
#
# Current decision = the feedback row with greatest (decided_at, id); id breaks
# ties. Order is stated explicitly, never left to query order.
#
# decided_at defaults to clock_timestamp(), and submit_feedback holds the
# FOR UPDATE row lock while inserting. now() would be transaction START time,
# which can invert relative to lock-acquisition order (a transaction that began
# earlier but acquired the lock later would stamp an earlier time than the one
# it must supersede). clock_timestamp() is taken after the lock is held, so
# (decided_at, id) order matches serialization order.
# ---------------------------------------------------------------------------
_RECOMPUTE_STATUS_SQL = """
    UPDATE flagged_cases fc
       SET status = COALESCE(
             (SELECT cf.decision
                FROM case_feedback cf
               WHERE cf.case_id = fc.id
               ORDER BY cf.decided_at DESC, cf.id DESC
               LIMIT 1),
             CASE WHEN fc.claimed_by IS NOT NULL THEN 'in_review' ELSE 'open' END)
     WHERE fc.id = %s
 RETURNING fc.status
"""


def recompute_status(cur, case_id):
    """Recompute and store flagged_cases.status from the log. Takes a cursor
    (caller controls the transaction). Returns the new status, or None if the
    case does not exist."""
    cur.execute(_RECOMPUTE_STATUS_SQL, (case_id,))
    row = cur.fetchone()
    if row is None:
        return None
    return row[0] if not isinstance(row, dict) else row["status"]


def _select_case(cur, case_id, for_update=False):
    cur.execute(
        "SELECT * FROM flagged_cases WHERE id = %s" + (" FOR UPDATE" if for_update else ""),
        (case_id,),
    )
    return cur.fetchone()


def get_case(conn, case_id):
    with _tx(conn) as cur:
        row = _select_case(cur, case_id)
    return dict(row) if row else None


def get_case_detail(conn, case_id):
    """Read the case and feedback from one PostgreSQL statement snapshot."""
    with _tx(conn) as cur:
        cur.execute(
            """SELECT fc.*,
                      (SELECT COALESCE(json_agg(f ORDER BY f.decided_at, f.id), '[]'::json)
                         FROM (SELECT id, decision, analyst, note, decided_at
                                 FROM case_feedback WHERE case_id = fc.id) f) AS feedback
                 FROM flagged_cases fc WHERE fc.id = %s""",
            (case_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    detail = dict(row)
    detail["current_decision"] = detail["feedback"][-1] if detail["feedback"] else None
    return detail


def _raise_for_failed_update(cur, case_id, reason):
    # Follow-up read is for the error only; the UPDATE already decided.
    row = _select_case(cur, case_id)
    if row is None:
        raise CaseNotFound(case_id)
    raise CaseConflict(case_id, row["status"], row["claimed_by"], reason)


def claim_case(conn, case_id, analyst):
    with _tx(conn) as cur:
        cur.execute(
            """UPDATE flagged_cases
                  SET status='in_review', claimed_by=%s, claimed_at=clock_timestamp()
                WHERE id=%s AND status='open'
            RETURNING *""",
            (analyst, case_id),
        )
        row = cur.fetchone()
        if row is None:
            _raise_for_failed_update(cur, case_id, "case is not open")
    return dict(row)


def release_case(conn, case_id, analyst):
    with _tx(conn) as cur:
        cur.execute(
            """UPDATE flagged_cases
                  SET status='open', claimed_by=NULL, claimed_at=NULL
                WHERE id=%s AND status='in_review' AND claimed_by=%s
            RETURNING *""",
            (case_id, analyst),
        )
        row = cur.fetchone()
        if row is None:
            _raise_for_failed_update(cur, case_id, "not in_review or not claimed by caller")
    return dict(row)


def submit_feedback(conn, case_id, analyst, decision, note=None):
    if decision not in VALID_DECISIONS:
        raise ValueError(f"invalid decision {decision!r}; expected one of {VALID_DECISIONS}")
    with _tx(conn) as cur:
        case = _select_case(cur, case_id, for_update=True)
        if case is None:
            raise CaseNotFound(case_id)
        if case["status"] == "in_review" and case["claimed_by"] != analyst:
            raise CaseConflict(
                case_id, case["status"], case["claimed_by"],
                "only the claimant may decide an in_review case",
            )
        cur.execute(
            "INSERT INTO case_feedback (case_id, decision, analyst, note) "
            "VALUES (%s, %s, %s, %s)",
            (case_id, decision, analyst, note),
        )
        recompute_status(cur, case_id)
        row = _select_case(cur, case_id)
    return dict(row)
