"""Rules admin API (spec 6, 7.1): read config/history, PATCH a rule.

Weight semantics: `total_score` is the weight-normalized average of the
enabled rules' sub-scores, so `weight` affects ONLY `total_score` (and thus
queue order via `priority_score`), NEVER whether a transaction is flagged.
Flagging is "any enabled rule fired" and depends only on `enabled` and
`params`.
"""
import json
import math
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from psycopg2.extras import RealDictCursor

from app.api.db import get_conn
from app.pipeline.loader import DEFAULT_ROW_CAP
from app.realtime.publisher import publish_rules_updated

router = APIRouter()

WEIGHT_NOTE = (
    "weight affects only total_score (and queue order via priority_score), never "
    "whether a transaction is flagged; flagging depends only on `enabled` and `params`."
)

HISTORY_DEFAULT_LIMIT, HISTORY_MAX_LIMIT = 50, 200


def _is_num(v: Any) -> bool:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return False
    try:
        return math.isfinite(v)
    except OverflowError:
        return False  # an integer too large for the database's float column


def _int_check(lo=None, hi=None):
    def check(v):
        # ints must be real ints: reject bool and non-integral (or any) floats
        if isinstance(v, bool) or not isinstance(v, int):
            return "must be an integer"
        if lo is not None and v < lo:
            return f"must be >= {lo}"
        if hi is not None and v > hi:
            return f"must be <= {hi}"
        return None
    return check


def _num_check(lo, strict):
    def check(v):
        if not _is_num(v):
            return "must be a finite number"
        if strict and not v > lo:
            return f"must be > {lo}"
        if not strict and not v >= lo:
            return f"must be >= {lo}"
        return None
    return check


# Per-rule param schema: key -> validator returning an error string or None.
# Every key is required in the merged result.
PARAM_SPECS: dict[str, dict[str, Any]] = {
    "velocity": {
        "window_minutes": _int_check(lo=1),
        # Upper bound = DEFAULT_ROW_CAP: the loader supplies at most that many
        # recent transactions, so a larger threshold could never fire.
        "threshold_count": _int_check(lo=2, hi=DEFAULT_ROW_CAP),
    },
    "amount_baseline": {"deviation_multiplier": _num_check(1.0, strict=True)},
    "geo_impossibility": {
        "min_distance_km": _num_check(0, strict=False),
        "max_speed_kmh": _num_check(0, strict=True),
    },
}


def _state(row: dict) -> dict:
    return {"weight": row["weight"], "enabled": row["enabled"], "params": row["params"]}


def _rule_out(row: dict) -> dict:
    return {"rule_name": row["rule_name"], **_state(row), "updated_at": row["updated_at"]}


@router.get("/rules")
def get_rules(conn=Depends(get_conn)):
    # One statement => one snapshot, so rules and version are consistent.
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT r.rule_name, r.weight, r.enabled, r.params, r.updated_at, "
            "(SELECT max(id) FROM rules_config_history) AS version "
            "FROM rules_config r ORDER BY r.rule_name"
        )
        rows = cur.fetchall()
    version = rows[0]["version"] if rows else None
    return {"version": version, "rules": [_rule_out(r) for r in rows], "note": WEIGHT_NOTE}


@router.get("/rules/history")
def get_history(
    rule_name: str | None = None,
    limit: int = Query(HISTORY_DEFAULT_LIMIT, ge=1, le=HISTORY_MAX_LIMIT),
    conn=Depends(get_conn),
):
    sql = "SELECT id, rule_name, before, after, changed_by, changed_at FROM rules_config_history"
    params: list = []
    if rule_name is not None:
        sql += " WHERE rule_name = %s"
        params.append(rule_name)
    sql += " ORDER BY id DESC LIMIT %s"
    params.append(limit)
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, params)
        return {"history": cur.fetchall()}


def _bad(msg: str) -> HTTPException:
    return HTTPException(422, msg)


def _validate_body(body: Any) -> tuple[str, dict]:
    if not isinstance(body, dict):
        raise _bad("body must be a JSON object")
    unknown = set(body) - {"changed_by", "weight", "enabled", "params"}
    if unknown:
        raise _bad(f"unknown fields: {sorted(unknown)}")
    changed_by = body.get("changed_by")
    if not isinstance(changed_by, str) or not changed_by.strip():
        raise _bad("changed_by is required and must be a non-empty string")
    changes = {k: body[k] for k in ("weight", "enabled", "params") if k in body}
    if not changes:
        raise _bad("at least one of weight, enabled, params is required")
    if "weight" in changes:
        w = changes["weight"]
        if not _is_num(w) or w < 0:
            raise _bad("weight must be a finite number >= 0")
    if "enabled" in changes and not isinstance(changes["enabled"], bool):
        raise _bad("enabled must be a boolean")
    if "params" in changes and (not isinstance(changes["params"], dict) or not changes["params"]):
        raise _bad("params must be a non-empty object")
    return changed_by.strip(), changes


def _validate_params(rule_name: str, merged: dict, patch: dict) -> None:
    spec = PARAM_SPECS.get(rule_name)
    if spec is None:
        raise _bad(f"no params schema for rule {rule_name!r}")
    unknown = set(patch) - set(spec)
    if unknown:
        raise _bad(f"unknown params for {rule_name}: {sorted(unknown)}")
    for key, check in spec.items():
        if key not in merged:
            raise _bad(f"params.{key} is required for {rule_name}")
        err = check(merged[key])
        if err:
            raise _bad(f"params.{key} {err}")


@router.patch("/rules/{rule_name}")
def patch_rule(rule_name: str, body: Any = Body(...), conn=Depends(get_conn)):
    """Partially update one rule (`weight`, `enabled`, `params`; `params` is a
    shallow merge). `changed_by` is required.

    WEIGHT SEMANTICS: `total_score` is the weight-normalized average of the
    enabled rules' sub-scores, so `weight` affects ONLY `total_score` (and
    thus queue order via `priority_score`), NEVER whether a transaction is
    flagged. Flagging depends only on `enabled` and `params`. The response's
    `affects_flagging` is true iff `enabled` or `params` actually changed
    value.

    INFERENCE (not in the spec): a PATCH whose every supplied value already
    equals the current value is a no-op. Nothing is written (no history row,
    no version bump, no publish); the response has `changed: false`,
    `affects_flagging: false` and the current version.
    """
    changed_by, changes = _validate_body(body)

    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Lock ALL rows (not just the target): the last-enabled-rule guard
            # reads every row, so concurrent disables must serialize.
            cur.execute(
                "SELECT rule_name, weight, enabled, params, updated_at FROM rules_config "
                "ORDER BY rule_name FOR UPDATE"
            )
            rows = {r["rule_name"]: r for r in cur.fetchall()}
            row = rows.get(rule_name)
            if row is None:
                raise HTTPException(404, f"unknown rule {rule_name!r}")

            before = _state(row)
            after = json.loads(json.dumps(before))  # deep copy
            if "weight" in changes:
                after["weight"] = float(changes["weight"])
            if "enabled" in changes:
                after["enabled"] = changes["enabled"]
            if "params" in changes:
                after["params"] = {**before["params"], **changes["params"]}
                _validate_params(rule_name, after["params"], changes["params"])
            if not any(
                (after["enabled"] if n == rule_name else r["enabled"]) for n, r in rows.items()
            ):
                raise _bad("cannot leave no rule enabled")

            if after == before:
                cur.execute("SELECT max(id) AS v FROM rules_config_history")
                version = cur.fetchone()["v"]
                conn.rollback()
                return {"rule": _rule_out(row), "version": version, "changed": False,
                        "affects_flagging": False, "weight_note": WEIGHT_NOTE}

            cur.execute(
                "UPDATE rules_config SET weight = %s, enabled = %s, params = %s, "
                "updated_at = clock_timestamp() WHERE rule_name = %s "
                "RETURNING rule_name, weight, enabled, params, updated_at",
                (after["weight"], after["enabled"], json.dumps(after["params"]), rule_name),
            )
            new_row = cur.fetchone()
            cur.execute(
                "INSERT INTO rules_config_history (rule_name, before, after, changed_by) "
                "VALUES (%s, %s, %s, %s) RETURNING id",
                (rule_name, json.dumps(before), json.dumps(_state(new_row)), changed_by),
            )
            version = cur.fetchone()["id"]
        conn.commit()
    except BaseException:
        conn.rollback()
        raise

    # Only after a successful commit; publish never raises.
    publish_rules_updated(version)
    return {
        "rule": _rule_out(new_row),
        "version": version,
        "changed": True,
        "affects_flagging": before["enabled"] != after["enabled"] or before["params"] != after["params"],
        "weight_note": WEIGHT_NOTE,
    }
