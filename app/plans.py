import json
from .db import now, parse
from .requests import employee_store


def change_plan(database, actor, kind, identifier, body):
    if actor["role"] not in ("hq", "admin") or kind not in ("help", "transfer"):
        raise PermissionError("本社権限が必要です。")
    with database.connect(write=True) as db:
        table = "helpers" if kind == "help" else "transfers"
        row = db.execute(f"SELECT * FROM {table} WHERE id=?", (identifier,)).fetchone()
        if not row:
            raise ValueError("予定が見つかりません。")
        old = dict(row)
        previous_day = old["day"] if kind == "help" else old["effective"]
        cancel = body.get("cancel") is True
        if cancel and kind != "help":
            raise ValueError("異動取り消しはこの操作の対象ではありません。")
        from datetime import date
        new_day = previous_day if cancel else date.fromisoformat(body["day"]).isoformat()
        if previous_day < now()[:10] or (not cancel and (previous_day <= now()[:10] or new_day <= now()[:10])):
            raise ValueError("変更できるのは未来の予定だけです。")
        shifts = [dict(r) for r in db.execute("SELECT * FROM shifts WHERE employee_id=? AND cancelled=0", (old["employee_id"],))]
        for day in {previous_day, new_day}:
            if any(parse(s["start"]).date().isoformat() <= day <= (parse(s["end"]).date().isoformat() if s["end"] else now()[:10]) for s in shifts):
                raise ValueError("対象日に打刻があるため直接変更できません。")
        if cancel:
            db.execute(f"DELETE FROM {table} WHERE id=?", (identifier,))
            updated = {"cancelled": True}
        else:
            store = int(body["target_store"])
            if not db.execute("SELECT 1 FROM stores WHERE id=? AND active=1", (store,)).fetchone():
                raise ValueError("利用可能な店舗を選択してください。")
            field = "day" if kind == "help" else "effective"
            if db.execute(f"SELECT 1 FROM {table} WHERE employee_id=? AND {field}=? AND id<>?", (old["employee_id"], new_day, identifier)).fetchone():
                raise ValueError("同じ日の予定が重複しています。")
            db.execute(f"UPDATE {table} SET store_id=?,{field}=? WHERE id=?", (store, new_day, identifier))
            updated = dict(db.execute(f"SELECT * FROM {table} WHERE id=?", (identifier,)).fetchone())
        request = db.execute("SELECT * FROM requests WHERE id=?", (old["request_id"],)).fetchone()
        if request:
            payload = json.loads(request["payload"])
            payload.update({"cancelled": True} if cancel else {"target_store": store, "day": new_day})
            db.execute("UPDATE requests SET payload=?,revision=revision+1,updated=? WHERE id=?", (json.dumps(payload, ensure_ascii=False), now(), request["id"]))
            db.execute("INSERT INTO request_history(request_id,revision,actor,event,payload,created) VALUES (?,?,?,?,?,?)", (request["id"], request["revision"]+1, actor["id"], "予定取り消し" if cancel else "予定直接変更", json.dumps({"before":old,"after":updated}, ensure_ascii=False), now()))
        database.audit(db, actor["id"], "予定取り消し" if cancel else "予定直接変更", f"{kind}/{identifier}", old, updated)
