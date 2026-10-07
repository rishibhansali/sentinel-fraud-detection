"""Case REST endpoints (spec 6, 5.3, 5.1 publish-after-commit)."""
import base64
import json

from fastapi import APIRouter, Depends, HTTPException
from psycopg2.extras import RealDictCursor

from app.api.db import get_conn
from app.api.schemas import AnalystBody, FeedbackBody
from app.cases.repository import (
    CaseConflict,
    CaseNotFound,
    claim_case,
    get_case_detail,
    release_case,
    submit_feedback,
)
from app.realtime.events import case_summary
from app.realtime.publisher import publish_case_event

router = APIRouter()

STATUSES = ("open", "in_review", "confirmed_fraud", "false_positive")
# INFERENCE (not stated in the spec): "list pending cases" is read as
# open + in_review, i.e. everything not yet decided. Override with ?status=.
DEFAULT_STATUSES = ("open", "in_review")

QUEUE_DEFAULT_LIMIT, QUEUE_MAX_LIMIT = 50, 200
SINCE_DEFAULT_LIMIT, SINCE_MAX_LIMIT = 200, 500

_LIST_COLS = (
    "id, transaction_id, transaction_ts, user_id, total_score, priority_score, "
    "status, claimed_by, claimed_at, rule_results, ml_anomaly_score, ai_summary, ai_summary_model, "
    "rules_config_version, flagged_at"
)


def list_item(row: dict) -> dict:
    item = case_summary(row)
    for k in ("claimed_at", "transaction_ts", "rules_config_version"):
        item[k] = row.get(k)
    return item


def _encode_cursor(score: float, case_id: int) -> str:
    return base64.urlsafe_b64encode(json.dumps([score, case_id]).encode()).decode()


def _decode_cursor(cursor: str) -> tuple[float, int]:
    try:
        score, case_id = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        return float(score), int(case_id)
    except Exception:  # noqa: BLE001
        raise HTTPException(422, "invalid cursor")


def query_queue(conn, statuses, limit, cursor):
    """Keyset page over (priority_score DESC, id DESC); uses the
    (status, priority_score DESC, id DESC) index. Row comparison on the pair
    is exact under ties in priority_score."""
    sql = f"SELECT {_LIST_COLS} FROM flagged_cases WHERE status = ANY(%s)"
    params: list = [list(statuses)]
    if cursor is not None:
        sql += " AND (priority_score, id) < (%s, %s)"
        params += list(cursor)
    sql += " ORDER BY priority_score DESC, id DESC LIMIT %s"
    params.append(limit + 1)  # one extra row detects a further page
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    return rows[:limit], len(rows) > limit


def query_since_id(conn, since_id, limit):
    """Creation cursor: id > since_id, id ASC, any status.

    To repair missed updates to existing ids, scan from since_id=0 and
    replace cached summaries by id. A nonzero cursor alone cannot find them.

    LIMITATION (spec 5.3): this cursor assumes a SINGLE writer, so that commit
    order equals id (BIGSERIAL sequence) order. With multiple concurrent
    writers (e.g. LoadGenerator alongside the pipeline against the same table)
    a lower id can commit AFTER a higher id, so `id > N` can permanently skip
    a case. If multiple concurrent writers are introduced this cursor must be
    revisited (overlap window, or move to an outbox).
    """
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            f"SELECT {_LIST_COLS} FROM flagged_cases WHERE id > %s ORDER BY id ASC LIMIT %s",
            (since_id, limit),
        )
        return cur.fetchall()


@router.get("/cases")
def list_cases(
    status: str | None = None,
    limit: int | None = None,
    cursor: str | None = None,
    since_id: int | None = None,
    conn=Depends(get_conn),
):
    """Queue (`status`, `limit`, `cursor`) or id cursor (`since_id`).

    `since_id=N` returns cases with id > N, id ASC, any status (default limit
    200, max 500) and `next_since_id`. Start at 0 for full reconciliation
    after a WebSocket gap; a nonzero cursor only retrieves newly created ids.
    Combining it with `status` or `cursor` is a 422.

    LIMITATION (spec 5.3): the since_id cursor assumes a SINGLE writer so that
    commit order equals id (BIGSERIAL sequence) order. With multiple
    concurrent writers (e.g. LoadGenerator alongside the pipeline against the
    same table) a lower id can commit AFTER a higher id, so `id > N` can
    permanently skip a case. If multiple concurrent writers are introduced the
    cursor must be revisited (overlap window, or move to an outbox).
    """
    if since_id is not None:
        if status is not None or cursor is not None:
            raise HTTPException(422, "since_id cannot be combined with status or cursor")
        lim = SINCE_DEFAULT_LIMIT if limit is None else limit
        if not 1 <= lim <= SINCE_MAX_LIMIT:
            raise HTTPException(422, f"limit must be between 1 and {SINCE_MAX_LIMIT}")
        rows = query_since_id(conn, since_id, lim)
        return {
            "items": [list_item(r) for r in rows],
            "next_since_id": rows[-1]["id"] if len(rows) == lim else None,
        }

    lim = QUEUE_DEFAULT_LIMIT if limit is None else limit
    if not 1 <= lim <= QUEUE_MAX_LIMIT:
        raise HTTPException(422, f"limit must be between 1 and {QUEUE_MAX_LIMIT}")
    if status is None:
        statuses = DEFAULT_STATUSES
    else:
        statuses = tuple(s.strip() for s in status.split(","))
        bad = [s for s in statuses if s not in STATUSES]
        if bad:
            raise HTTPException(422, f"invalid status {bad}; expected from {list(STATUSES)}")
    decoded = _decode_cursor(cursor) if cursor is not None else None
    rows, has_more = query_queue(conn, statuses, lim, decoded)
    next_cursor = _encode_cursor(rows[-1]["priority_score"], rows[-1]["id"]) if has_more else None
    return {"items": [list_item(r) for r in rows], "next_cursor": next_cursor}


@router.get("/cases/{case_id}")
def case_detail(case_id: int, conn=Depends(get_conn)):
    row = get_case_detail(conn, case_id)
    if row is None:
        raise HTTPException(404, f"case {case_id} not found")
    return row


def _mutate(fn, conn, case_id, *args):
    try:
        row = fn(conn, case_id, *args)  # commits internally
    except CaseNotFound:
        raise HTTPException(404, f"case {case_id} not found")
    except CaseConflict as exc:
        raise HTTPException(
            409,
            detail={
                "message": str(exc),
                "reason": exc.reason,
                "current_status": exc.current_status,
                "claimed_by": exc.claimed_by,
            },
        )
    # Only after the commit, only on success. Never raises.
    publish_case_event("case.updated", case_summary(row))
    return row


@router.post("/cases/{case_id}/claim")
def claim(case_id: int, body: AnalystBody, conn=Depends(get_conn)):
    return _mutate(claim_case, conn, case_id, body.analyst)


@router.post("/cases/{case_id}/release")
def release(case_id: int, body: AnalystBody, conn=Depends(get_conn)):
    return _mutate(release_case, conn, case_id, body.analyst)


@router.post("/cases/{case_id}/feedback")
def feedback(case_id: int, body: FeedbackBody, conn=Depends(get_conn)):
    return _mutate(submit_feedback, conn, case_id, body.analyst, body.decision, body.note)


@router.get("/stats/rules")
def rule_stats(conn=Depends(get_conn)):
    """Per rule: decided cases (CURRENT status confirmed_fraud/false_positive)
    in which the rule fired. Includes every rule in rules_config (zero counts
    if none) plus any other rule name seen in rule_results. precision is null
    when there are no decided cases for the rule."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """SELECT r->>'rule_name' AS rule_name,
                      count(*) FILTER (WHERE fc.status = 'confirmed_fraud') AS confirmed_fraud,
                      count(*) FILTER (WHERE fc.status = 'false_positive') AS false_positive
                 FROM flagged_cases fc,
                      jsonb_array_elements(
                        CASE WHEN jsonb_typeof(fc.rule_results) = 'array'
                             THEN fc.rule_results ELSE '[]'::jsonb END) r
                WHERE fc.status IN ('confirmed_fraud', 'false_positive')
                  AND r->>'fired' = 'true'
                GROUP BY 1"""
        )
        counts = {r["rule_name"]: r for r in cur.fetchall()}
        cur.execute("SELECT rule_name FROM rules_config")
        names = {r["rule_name"] for r in cur.fetchall()} | set(counts)
    out = []
    for name in sorted(names):
        c = counts.get(name)
        cf = c["confirmed_fraud"] if c else 0
        fp = c["false_positive"] if c else 0
        out.append({
            "rule_name": name,
            "confirmed_fraud": cf,
            "false_positive": fp,
            "precision": cf / (cf + fp) if cf + fp else None,
        })
    return out
