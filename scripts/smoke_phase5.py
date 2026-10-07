#!/usr/bin/env python
"""Phase 5 real (non-mocked) smoke test. Run with the BACKEND venv:

    backend/.venv/bin/python scripts/smoke_phase5.py

Boots a real uvicorn API process and a real pipeline CLI process against the
shared Postgres and Redis, then proves the Phase 5 definition of done end to
end (WS push across processes, live rules hot reload, claim/feedback, priority
demotion). Always cleans up after itself and verifies the DB is back to its
original state. Exit code 0 only if every step passed. ~50s.
"""
import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import psycopg2
import psycopg2.extras
import websockets

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
PY = sys.executable
DSN = os.environ.get("SENTINEL_DB_DSN", "postgresql://sentinel:sentinel_dev_only@localhost:5432/sentinel")

# Reserved (see backend/tests/pipeline/reserved_ranges.py): SMOKE_TXN_BASE.
TXN_BASE = 270_000_000
TXN_MAX = TXN_BASE + 9_999
USER_A, USER_B, USER_M = -5701, -5702, -5703  # A: group A, B: groups B+C, M: marker
T0 = datetime(2025, 6, 15, 12, 0, 0, tzinfo=timezone.utc)  # 2025-06 -> default partition
LAT, LON = 40.7128, -74.0060  # identical for all rows: geo can never fire
AMOUNT = 10.0  # users have no rollup row -> amount_baseline cannot fire

# (id offset, user, seconds after T0). Replayed at speed 1 so these ARE real seconds.
SEED = (
    [(0 + i, USER_A, i) for i in range(4)]                 # group A: 4 txns (< default threshold 5)
    + [(4 + i, USER_M, 4 + i) for i in range(5)]           # marker: 5 txns, fires at default -> progress signal
    + [(10 + i, USER_B, 20 + i) for i in range(4)]         # group B: 4th txn fires once threshold=4
    + [(14, USER_B, 40)]                                   # group C: 5th txn, after feedback -> demoted
)

results: list[tuple[bool, str, str]] = []


class StepFailed(Exception):
    pass


def step(name: str, ok: bool, detail: str = "") -> None:
    results.append((ok, name, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""), flush=True)
    if not ok:
        raise StepFailed(name)


def db():
    return psycopg2.connect(DSN)


def q(sql, params=None, one=False):
    c = db()
    try:
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        c.rollback()
    finally:
        c.close()
    return (rows[0] if rows else None) if one else rows


def snapshot_state() -> dict:
    return {
        "flagged_cases": q("SELECT count(*) AS n FROM flagged_cases", one=True)["n"],
        "case_feedback": q("SELECT count(*) AS n FROM case_feedback", one=True)["n"],
        "max_history_id": q("SELECT max(id) AS m FROM rules_config_history", one=True)["m"],
        "history_rows": q("SELECT count(*) AS n FROM rules_config_history", one=True)["n"],
        "rules_config": [dict(r) for r in q("SELECT * FROM rules_config ORDER BY rule_name")],
        "smoke_transactions": q("SELECT count(*) AS n FROM transactions WHERE id BETWEEN %s AND %s",
                                (TXN_BASE, TXN_MAX), one=True)["n"],
    }


def purge_own_rows() -> None:
    c = db()
    try:
        with c.cursor() as cur:
            cur.execute("DELETE FROM case_feedback WHERE case_id IN (SELECT id FROM flagged_cases "
                        "WHERE transaction_id BETWEEN %s AND %s)", (TXN_BASE, TXN_MAX))
            cur.execute("DELETE FROM flagged_cases WHERE transaction_id BETWEEN %s AND %s", (TXN_BASE, TXN_MAX))
            cur.execute("DELETE FROM transactions WHERE id BETWEEN %s AND %s", (TXN_BASE, TXN_MAX))
        c.commit()
    finally:
        c.close()


def restore_rules(orig: dict) -> None:
    c = db()
    try:
        with c.cursor() as cur:
            for r in orig["rules_config"]:
                cur.execute("UPDATE rules_config SET weight=%s, enabled=%s, params=%s, updated_at=%s "
                            "WHERE rule_name=%s",
                            (r["weight"], r["enabled"], json.dumps(r["params"]), r["updated_at"], r["rule_name"]))
            cur.execute("DELETE FROM rules_config_history WHERE id > %s", (orig["max_history_id"],))
        c.commit()
    finally:
        c.close()


def seed() -> None:
    c = db()
    try:
        with c.cursor() as cur:
            for off, user, secs in SEED:
                cur.execute(
                    """INSERT INTO transactions (id, user_id, card_id, ts, amount, lat, lon,
                        v1,v2,v3,v4,v5,v6,v7,v8,v9,v10,v11,v12,v13,v14,v15,v16,v17,v18,v19,v20,
                        v21,v22,v23,v24,v25,v26,v27,v28, class, split)
                       VALUES (%s,%s,1,%s,%s,%s,%s, 0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,
                        0,0,0,0,0,0,0,0, 0,'test')""",
                    (TXN_BASE + off, user, T0 + timedelta(seconds=secs), AMOUNT, LAT, LON),
                )
        c.commit()
    finally:
        c.close()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def stop_proc(p) -> None:
    if p is None or p.poll() is not None:
        return
    p.send_signal(signal.SIGINT)
    try:
        p.wait(10)
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait(5)


async def wait_for(fn, timeout: float, interval: float = 0.25):
    """Poll fn() (sync) until truthy; returns the value or None on timeout."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = fn()
        if v:
            return v
        await asyncio.sleep(interval)
    return None


def case_for_txn(off: int):
    return q("SELECT * FROM flagged_cases WHERE transaction_id = %s", (TXN_BASE + off,), one=True)


async def run(orig: dict, procs: dict) -> None:
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    since0 = q("SELECT coalesce(max(id),0) AS m FROM flagged_cases", one=True)["m"]

    # 1. API + WebSocket
    procs["api"] = subprocess.Popen(
        [PY, "-m", "uvicorn", "app.main:app", "--port", str(port), "--log-level", "warning"],
        cwd=BACKEND, env={**os.environ, "SENTINEL_DB_DSN": DSN})
    async with httpx.AsyncClient(base_url=base, timeout=10) as http:
        async def up():
            try:
                return (await http.get("/health")).status_code == 200
            except httpx.HTTPError:
                return False
        end = time.monotonic() + 30
        while time.monotonic() < end and not await up():
            await asyncio.sleep(0.25)
        step("1a API up", await up(), f"port {port}")

        events: list[dict] = []
        async with websockets.connect(f"ws://127.0.0.1:{port}/ws/cases") as ws:
            async def reader():
                try:
                    async for raw in ws:
                        events.append(json.loads(raw))
                except websockets.ConnectionClosed:
                    pass
            reader_task = asyncio.create_task(reader())
            try:
                hello = await wait_for(lambda: any(e["type"] == "hello" for e in events), 5)
                step("1b WebSocket hello", bool(hello), f"events={events}")
                await asyncio.sleep(1.0)  # let the API's Redis subscription settle

                def ws_find(etype, pred=lambda c: True):
                    return next((e for e in events if e["type"] == etype and pred(e["case"])), None)

                async def ws_wait(etype, pred, timeout=15):
                    return await wait_for(lambda: ws_find(etype, pred), timeout, 0.1)

                # 2. seed
                seed()
                n = q("SELECT count(*) AS n FROM transactions WHERE id BETWEEN %s AND %s",
                      (TXN_BASE, TXN_MAX), one=True)["n"]
                step("2 seeded synthetic transactions", n == len(SEED),
                     f"{n} rows, ids {TXN_BASE}.., users {USER_A},{USER_B},{USER_M}, ts 2025-06-15")

                # 3. pipeline
                procs["pipeline"] = subprocess.Popen(
                    [PY, "-m", "app.pipeline.run_pipeline", "--dsn", DSN, "--start-id", str(TXN_BASE - 1),
                     "--end-id", str(TXN_MAX), "--speed-multiplier", "1", "--rules-poll-seconds", "30"],
                    cwd=BACKEND)
                orig_version = orig["max_history_id"]

                # 4. Group A: no case at default threshold. Marker case = pipeline has passed group A.
                marker = await wait_for(lambda: case_for_txn(8), 40)
                step("3/4a pipeline running; marker case (user M, default threshold) created", bool(marker),
                     f"marker case id={marker and marker['id']} "
                     f"rules_config_version={marker and marker['rules_config_version']}")
                a_cases = q("SELECT id FROM flagged_cases WHERE user_id = %s", (USER_A,))
                step("4a group A (4 txns, threshold 5): NO case in DB", not a_cases, f"cases for user A: {a_cases}")
                await ws_wait("case.created", lambda c: c["user_id"] == USER_M, 5)
                a_events = [e for e in events if e["type"] == "case.created" and e["case"]["user_id"] == USER_A]
                step("4b group A: no case.created over WS for user A", not a_events,
                     f"marker event seen: {ws_find('case.created', lambda c: c['user_id'] == USER_M) is not None}")

                # 5. PATCH threshold while pipeline is running
                r = await http.patch("/rules/velocity", json={"changed_by": "smoke", "params": {"threshold_count": 4}})
                body = r.json()
                new_version = body.get("version")
                step("5 PATCH /rules/velocity threshold_count=4",
                     r.status_code == 200 and body.get("affects_flagging") is True
                     and isinstance(new_version, int) and new_version > orig_version,
                     f"status={r.status_code} affects_flagging={body.get('affects_flagging')} "
                     f"version {orig_version} -> {new_version}")

                # 6. Group B (pickup is proven by the case's rules_config_version)
                b_case = await wait_for(lambda: case_for_txn(13), 40)
                step("6a group B: 4th txn flagged (case exists)", bool(b_case), f"case id={b_case and b_case['id']}")
                step("6b case rules_config_version == PATCH version (hot reload picked up in running pipeline)",
                     b_case["rules_config_version"] == new_version,
                     f"case version={b_case['rules_config_version']} patch version={new_version}")
                ev = await ws_wait("case.created", lambda c: c["id"] == b_case["id"], 10)
                step("6c WS case.created received for group B case (Redis, pipeline proc -> API proc)", bool(ev),
                     f"event={ev and {k: ev['case'][k] for k in ('id', 'user_id', 'priority_score', 'fired_rules')}}")
                rr = await http.get("/cases", params={"since_id": since0})
                ids = [c["id"] for c in rr.json()["items"]]
                step("6d GET /cases?since_id returns the case (repair cursor)",
                     rr.status_code == 200 and b_case["id"] in ids, f"since_id={since0} -> ids {ids}")
                cid = b_case["id"]

                # 7. claim / conflict / feedback
                r = await http.post(f"/cases/{cid}/claim", json={"analyst": "smoke"})
                ev = await ws_wait("case.updated", lambda c: c["id"] == cid and c["status"] == "in_review", 10)
                step("7a claim by smoke -> 200, in_review, WS case.updated",
                     r.status_code == 200 and r.json()["status"] == "in_review" and bool(ev),
                     f"status={r.status_code} case status={r.json().get('status')} ws_event={bool(ev)}")
                r2 = await http.post(f"/cases/{cid}/claim", json={"analyst": "other"})
                step("7b second analyst claim -> 409", r2.status_code == 409, f"status={r2.status_code} {r2.json()}")
                r = await http.post(f"/cases/{cid}/feedback",
                                    json={"analyst": "smoke", "decision": "false_positive", "note": "smoke"})
                ev = await ws_wait("case.updated", lambda c: c["id"] == cid and c["status"] == "false_positive", 10)
                step("7c feedback false_positive by claimant -> 200, WS case.updated, status false_positive",
                     r.status_code == 200 and r.json()["status"] == "false_positive" and bool(ev),
                     f"status={r.status_code} case status={r.json().get('status')} ws_event={bool(ev)}")
                repair_cursor, repaired = 0, None
                while True:
                    repair = await http.get("/cases", params={"since_id": repair_cursor, "limit": 500})
                    repair.raise_for_status()
                    repaired = next((item for item in repair.json()["items"] if item["id"] == cid), None)
                    if repaired is not None or repair.json()["next_since_id"] is None:
                        break
                    repair_cursor = repair.json()["next_since_id"]
                step("7d full REST reconciliation recovers an updated existing case",
                     repaired is not None and repaired["status"] == "false_positive",
                     f"case id={cid} repaired status={repaired and repaired['status']}")

                # 8. Group C
                c_case = await wait_for(lambda: case_for_txn(14), 40)
                step("8a group C: user B fires velocity again (new case)", bool(c_case),
                     f"case id={c_case and c_case['id']}")
                d = (await http.get(f"/cases/{c_case['id']}")).json()
                adj = d.get("priority_adjustment")
                cited = adj and adj["per_rule"] and adj["per_rule"][0]["prior_false_positive_case_ids"]
                step("8b GET /cases/{id} detail: priority_score < total_score, priority_adjustment cites first case",
                     d["priority_score"] < d["total_score"] and cited == [cid],
                     f"total_score={d['total_score']:.4f} priority_score={d['priority_score']:.4f} "
                     f"cited={cited} factor={adj and adj['per_rule'][0]['factor']} "
                     f"ml_anomaly_score={d.get('ml_anomaly_score')}")
                ev = await ws_wait("case.created", lambda c: c["id"] == c_case["id"], 10)
                step("8c WS case.created for group C carries the lower priority_score",
                     bool(ev) and ev["case"]["priority_score"] < ev["case"]["total_score"],
                     f"event priority_score={ev and ev['case']['priority_score']:.4f} "
                     f"total_score={ev and ev['case']['total_score']:.4f}")
            finally:
                reader_task.cancel()


def main() -> int:
    orig = snapshot_state()
    print("BEFORE:", json.dumps({k: v for k, v in orig.items() if k != "rules_config"}, default=str))
    procs: dict = {}
    failed = False
    try:
        purge_own_rows()  # leftovers from a killed earlier run
        asyncio.run(run(orig, procs))
    except StepFailed as e:
        failed = True
        print(f"aborting after failed step: {e}")
    except BaseException as e:  # noqa: BLE001 - incl. KeyboardInterrupt
        failed = True
        print(f"[FAIL] unexpected error: {type(e).__name__}: {e}")
    finally:
        print("--- cleanup ---")
        for name in ("pipeline", "api"):
            stop_proc(procs.get(name))
        purge_own_rows()
        restore_rules(orig)
        after = snapshot_state()
        clean = after == orig
        print("AFTER: ", json.dumps({k: v for k, v in after.items() if k != "rules_config"}, default=str))
        print(f"[{'PASS' if clean else 'FAIL'}] cleanup: DB state (counts, rules_config rows incl. updated_at, "
              f"max history id) equals original")
        if not clean:
            failed = True
    ok = not failed and all(r[0] for r in results)
    print(f"\nSMOKE {'PASSED' if ok else 'FAILED'}: {sum(r[0] for r in results)}/{len(results)} steps passed")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
