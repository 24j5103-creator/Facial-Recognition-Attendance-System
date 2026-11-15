"""Durations and month attribution are separate from the original punches."""
import calendar
import json
import uuid
from datetime import datetime

from .db import JST, now, parse, asdict


def duration(shift, at=None):
    if shift["cancelled"]:
        return 0, 0
    start = parse(shift["start"])
    end = parse(shift["end"]) if shift["end"] else (at or datetime.now(JST))
    if end < start:
        raise ValueError("退勤日時が出勤日時より前です。")
    gross = int((end - start).total_seconds())
    breaks = json.loads(shift["breaks"]) if isinstance(shift["breaks"], str) else shift["breaks"]
    previous = start
    total = 0
    for item in breaks:
        a = parse(item["start"])
        b = parse(item["end"]) if item.get("end") else end
        if a < previous or b < a or a < start or b > end:
            raise ValueError("休憩日時の順序・勤務範囲が不正です。")
        total += int((b - a).total_seconds())
        previous = b
    return gross, gross - total


def state(shift):
    if shift is None or shift["end"] or shift["cancelled"]:
        return "未勤務"
    pauses = json.loads(shift["breaks"])
    return "休憩中" if pauses and not pauses[-1].get("end") else "勤務中"


def work_day(shift):
    return parse(shift["start"]).date().isoformat()


def issue(shift):
    if shift["cancelled"]:
        return False
    gross, _ = duration(shift)
    return gross > 24 * 3600


def ensure_open(db, store, month):
    row = db.execute("SELECT closed FROM monthly WHERE store_id=? AND month=?", (store, month)).fetchone()
    if row and row[0]:
        raise ValueError("対象の店舗・月は確定済みです。本社で確定を解除してください。")


def normal_rows(db):
    """Exclude corrected shifts from raw aggregation; postings retain their attribution."""
    changed = {r[0] for r in db.execute("SELECT DISTINCT shift_id FROM requests WHERE status='承認済み' AND shift_id IS NOT NULL AND kind IN ('correction','cancel')")}
    for row in db.execute("SELECT * FROM shifts ORDER BY start,id"):
        shift = dict(row)
        if shift["id"] in changed or shift["cancelled"] or not shift["end"] or issue(shift):
            continue
        gross, actual = duration(shift)
        yield {"shift_id": shift["id"], "employee_id": shift["employee_id"], "store_id": shift["store_id"], "work_day": work_day(shift), "work_month": work_day(shift)[:7], "month": work_day(shift)[:7], "seconds": actual, "gross": gross, "runs": 1, "days": 1, "special": 0, "start": shift["start"], "source": "勤務"}


def ledger_rows(db):
    rows = list(normal_rows(db))
    for row in db.execute("SELECT p.*,r.shift_id FROM postings p JOIN requests r ON p.request_id=r.id ORDER BY p.approved,p.id"):
        item = dict(row)
        item.update(start=item["work_start"] or item["work_day"] + "T00:00:00+09:00", source="勤務" if item["special"] == -1 else ("過去月修正" if item["special"] else "修正"))
        rows.append(item)
    for record in db.execute("SELECT * FROM shifts WHERE cancelled=0"):
        if not record["end"] or issue(record):
            rows.append({"employee_id": record["employee_id"], "store_id": record["store_id"], "work_day": work_day(record), "work_month": work_day(record)[:7], "month": work_day(record)[:7], "seconds": 0, "gross": 0, "runs": 0, "days": 0, "special": 0, "start": record["start"], "source": "未確定"})
    return rows


def monthly_report(db, month, store=None):
    """Recompute carry chronologically. Closed months use the same immutable inputs."""
    employees = {r["id"]: dict(r) for r in db.execute("SELECT * FROM employees")}
    rows = ledger_rows(db)
    months = sorted({r["month"] for r in rows if r["month"] <= month} | {month})
    carry = {}  # employee -> FIFO list of {seconds, work_month, approved, store_id}
    output, trace = {}, []
    for current in months:
        buckets = {}
        for r in rows:
            if r["month"] != current:
                continue
            key = (r["employee_id"], r["store_id"])
            b = buckets.setdefault(key, {"employee_id": key[0], "store_id": key[1], "month": current, "gross_seconds": 0, "actual_seconds": 0, "adjustment_seconds": 0, "adjusted_seconds": 0, "days_set": set(), "special_days_set": set(), "days_delta": 0, "runs": 0, "special_runs": 0, "special_seconds": 0, "carry_in": 0, "carry_out": 0})
            b["gross_seconds"] += r["gross"]
            b["runs"] += r["runs"]
            if r["source"] == "勤務":
                b["actual_seconds"] += r["seconds"]
                b["days_set"].add(r["work_day"])
            else:
                b["adjustment_seconds"] += r["seconds"]
                b["days_delta"] += r["days"]
                if r["special"]:
                    b["special_runs"] += r["runs"]
                    b["special_seconds"] += r["seconds"]
                    if r["days"] > 0:
                        b["special_days_set"].add(r["work_day"])
        for employee in employees:
            old = carry.setdefault(employee, [])
            incoming = sum(x["seconds"] for x in old)
            negatives = [r for r in rows if r["employee_id"] == employee and r["month"] == current and r["seconds"] < 0]
            for r in negatives:
                old.append({"seconds": -r["seconds"], "work_month": r["work_month"], "approved": r.get("approved", ""), "store_id": r["store_id"], "request_id": r.get("request_id", "")})
            old.sort(key=lambda x: (x["work_month"], x["approved"]))
            # Apply to shifts in actual chronological order, across all stores.
            credits = sorted([r for r in rows if r["employee_id"] == employee and r["month"] == current and r["seconds"] > 0], key=lambda r: (r["start"], r.get("id", 0)))
            for r in credits:
                b = buckets[(employee, r["store_id"])]
                remaining = r["seconds"]
                for debt in old:
                    applied = min(remaining, debt["seconds"])
                    if applied:
                        trace.append({"employee_id": employee, "month": current, "store_id": r["store_id"], "origin_store_id": debt["store_id"], "work_month": debt["work_month"], "request_id": debt["request_id"], "applied_seconds": applied})
                        debt["seconds"] -= applied
                        remaining -= applied
                    if not remaining:
                        break
                b["adjusted_seconds"] += remaining
            old[:] = [x for x in old if x["seconds"]]
            own = sorted([b for (e, _), b in buckets.items() if e == employee], key=lambda b: b["store_id"])
            if not own and (incoming or old):
                s = employees[employee]["store_id"]
                own = [buckets.setdefault((employee, s), {"employee_id": employee, "store_id": s, "month": current, "gross_seconds": 0, "actual_seconds": 0, "adjustment_seconds": 0, "adjusted_seconds": 0, "days_set": set(), "special_days_set": set(), "days_delta": 0, "runs": 0, "special_runs": 0, "special_seconds": 0, "carry_in": 0, "carry_out": 0})]
            if own:
                own[0]["carry_in"] = incoming
                own[0]["carry_out"] = sum(x["seconds"] for x in old)
        if current == month:
            output = buckets
    final = []
    for (employee, sid), b in output.items():
        if store is not None and sid != store:
            continue
        b["days"] = len(b.pop("days_set")) + b.pop("days_delta")
        b["special_days"] = len(b.pop("special_days_set"))
        b["name"] = employees[employee]["name"]
        b["home_store_id"] = employees[employee]["store_id"]
        b["closed"] = bool((db.execute("SELECT closed FROM monthly WHERE store_id=? AND month=?", (sid, month)).fetchone() or [0])[0])
        relevant = [dict(r) for r in db.execute("SELECT * FROM shifts WHERE employee_id=? AND store_id=? AND substr(start,1,7)=? AND cancelled=0", (employee, sid, month))]
        b["issues"] = sum(issue(r) or not r["end"] for r in relevant)
        b["pending"] = db.execute("SELECT count(*) FROM requests WHERE employee_id=? AND store_id=? AND status IN ('申請中','差し戻し')", (employee, sid)).fetchone()[0]
        final.append(b)
    totals = {}
    sum_keys = ("gross_seconds", "actual_seconds", "adjustment_seconds", "adjusted_seconds", "days", "runs", "special_runs", "special_seconds", "carry_in", "carry_out", "issues", "pending")
    for item in final:
        employee = item["employee_id"]
        if employee not in totals:
            totals[employee] = {**item, "store_id": store if store is not None else 0, **{k: 0 for k in sum_keys}, "special_days": 0, "closed": True}
        for key in sum_keys:
            totals[employee][key] += item[key]
        totals[employee]["closed"] = totals[employee]["closed"] and item["closed"]
    selected = [r for r in rows if r["month"] == month and (store is None or r["store_id"] == store)]
    for employee, total in totals.items():
        own = [r for r in selected if r["employee_id"] == employee]
        total["days"] = len({r["work_day"] for r in own if r["source"] == "勤務"}) + sum(r["days"] for r in own if r["source"] != "勤務")
        total["special_days"] = len({r["work_day"] for r in own if r["source"] != "勤務" and r["special"] > 0 and r["days"] > 0})
    return {"rows": final, "employee_totals": list(totals.values()), "allocations": [r for r in trace if r["month"] == month and (store is None or r["store_id"] == store)], "carry": {k: v for k, v in carry.items() if v}, "ledger": selected}


def punch(db, employee, store, action, timestamp):
    person = db.execute("SELECT * FROM employees WHERE id=? AND active=1", (employee,)).fetchone()
    if not person:
        raise ValueError("利用可能な従業員ではありません。")
    shift = asdict(db.execute("SELECT * FROM shifts WHERE employee_id=? AND end IS NULL AND cancelled=0", (employee,)).fetchone())
    t = parse(timestamp)
    if action == "出勤":
        if shift:
            raise ValueError("すでに勤務中です。")
        home = person["store_id"]
        transfer = db.execute("SELECT store_id FROM transfers WHERE employee_id=? AND effective<=? ORDER BY effective DESC,id DESC LIMIT 1", (employee, t.date().isoformat())).fetchone()
        if transfer:
            home = transfer[0]
        allowed = home == store or db.execute("SELECT 1 FROM helpers WHERE employee_id=? AND store_id=? AND day=?", (employee, store, t.date().isoformat())).fetchone()
        if not allowed:
            raise ValueError("この店舗・出勤日の勤務許可がありません。")
        ensure_open(db, store, t.strftime("%Y-%m"))
        sid = str(uuid.uuid4())
        db.execute("INSERT INTO shifts(id,employee_id,store_id,start) VALUES (?,?,?,?)", (sid, employee, store, t.isoformat(timespec="seconds")))
        return sid
    if not shift or shift["store_id"] != store:
        raise ValueError("この店舗で勤務中の記録がありません。")
    ensure_open(db, store, work_day(shift)[:7])
    breaks = json.loads(shift["breaks"])
    s = state(shift)
    if action == "休憩開始" and s == "勤務中":
        breaks.append({"start": t.isoformat(timespec="seconds"), "end": None})
    elif action == "休憩終了" and s == "休憩中":
        breaks[-1]["end"] = t.isoformat(timespec="seconds")
    elif action == "退勤" and s == "勤務中":
        shift["end"] = t.isoformat(timespec="seconds")
    else:
        raise ValueError("現在の勤務状態ではその操作はできません。")
    shift["breaks"] = json.dumps(breaks)
    duration(shift, t)
    db.execute("UPDATE shifts SET end=?,breaks=?,version=version+1 WHERE id=?", (shift["end"], shift["breaks"], shift["id"]))
    return shift["id"]
