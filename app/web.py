import base64
from contextlib import asynccontextmanager
from datetime import datetime
import hashlib
import json
from pathlib import Path
import secrets
import sqlite3
import threading
import time
import uuid
import asyncio
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .db import Database, ROOT, JST, now, parse, password_ok, password_hash, asdict
from .attendance import state, issue, work_day, duration, punch, monthly_report
from .recognition import Recognition
from .requests import submit, review, employee_store, KINDS
from .identity import change_role, store_code
from .plans import change_plan
from .maintenance import Maintenance, export_zip


def create_app(database=None, recognizer=None, scheduled=True):
    database = database or Database()
    recognizer = recognizer or Recognition(database)
    maintenance = Maintenance(database)
    mutation_lock = threading.RLock()
    maintenance.guard = mutation_lock
    attempts = {}
    api_gate = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(app):
        worker = None
        if scheduled:
            worker = threading.Thread(target=maintenance.scheduler, daemon=True)
            worker.start()
        yield
        maintenance.stop.set()
        if worker:
            worker.join(timeout=3)

    app = FastAPI(title="顔認証勤怠管理", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.database = database
    app.state.recognizer = recognizer

    @app.middleware("http")
    async def guard(request, call_next):
        if request.method not in ("GET", "HEAD"):
            if database.setting("maintenance") and request.url.path not in ("/api/login", "/api/logout", "/api/restore"):
                return JSONResponse({"error": "復元・保守中のため更新を停止しています。管理責任者が状態を確認してください。"}, 503)
            origin = request.headers.get("origin")
            if origin and urlsplit(origin).netloc != request.headers.get("host"):
                return JSONResponse({"error": "同じサイトから操作してください。"}, 403)
            try:
                length = int(request.headers.get("content-length", "0"))
            except ValueError:
                return JSONResponse({"error": "リクエストが不正です。"}, 400)
            if length > 9_000_000:
                return JSONResponse({"error": "送信データが大きすぎます。"}, 413)
            # Bound chunked requests, too.
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 9_000_000:
                    return JSONResponse({"error": "送信データが大きすぎます。"}, 413)
            request._body = bytes(body)
        if request.url.path.startswith("/api/"):
            async with api_gate:
                response = await call_next(request)
        else:
            response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'"
        return response

    @app.exception_handler(ValueError)
    async def bad(request, exc):
        return JSONResponse({"error": str(exc)}, 400)

    @app.exception_handler(PermissionError)
    async def denied(request, exc):
        return JSONResponse({"error": str(exc)}, 403)

    @app.exception_handler(sqlite3.IntegrityError)
    async def conflict(request, exc):
        return JSONResponse({"error": "同じ対象の申請・勤務・番号が重複しているか、参照先がありません。"}, 409)

    @app.exception_handler(KeyError)
    async def missing(request, exc):
        return JSONResponse({"error": "必要な入力が不足しています。"}, 400)

    @app.exception_handler(Exception)
    async def internal(request, exc):
        return JSONResponse({"error": "処理に失敗しました。モデル・保存先・サーバーの状態を確認してください。"}, 500)

    def cookie_name(request):
        return "attendance_hq_session" if request.headers.get("x-portal") == "hq" else "attendance_session"

    def session(request, roles=None):
        with database.connect() as db:
            row = db.execute("SELECT * FROM sessions WHERE id=?", (request.cookies.get(cookie_name(request), ""),)).fetchone()
            if not row:
                raise PermissionError("ログインしてください。")
            s = dict(row)
            user = asdict(db.execute("SELECT * FROM users WHERE id=? AND active=1", (s["user_id"],)).fetchone()) if s["user_id"] else None
            if user and len(user["username"]) == 9 and user["username"].isdigit():
                employee = db.execute("SELECT active FROM employees WHERE id=?", (user["username"],)).fetchone()
                if not employee or not employee["active"]:
                    raise PermissionError("従業員の利用が停止されています。")
            if s["user_id"] and not user:
                raise PermissionError("アカウントが停止されています。")
            if s["store_id"]:
                store = db.execute("SELECT active FROM stores WHERE id=?", (s["store_id"],)).fetchone()
                if not store or not store[0] or s["version"] != database.setting("store_password_version"):
                    raise PermissionError("店舗へログインし直してください。")
            if user:
                timeout = 300 if user["role"] == "manager" else user["timeout"]
                if timeout and time.time() - s["last_seen"] > timeout:
                    raise PermissionError("無操作時間を超えました。ログインし直してください。")
                if user["must_change"] and request.url.path not in ("/api/me", "/api/password", "/api/logout"):
                    raise PermissionError("先に仮パスワードを変更してください。")
            role = user["role"] if user else "terminal"
            if roles and role not in roles:
                raise PermissionError("この操作の権限がありません。")
            s.update(user=user, role=role)
            return s

    def manage(request, admin=False):
        return session(request, ("admin",) if admin else ("manager", "hq", "admin"))

    def scope(s, store):
        if s["role"] == "manager" and int(store) not in json.loads(s["user"]["stores"]):
            raise PermissionError("担当店舗ではありません。")

    def user_public(user):
        if not user:
            return None
        return {k: v for k, v in user.items() if k != "password"}

    def login_response(user, store, previous=None, request=None):
        token = secrets.token_urlsafe(32)
        with database.connect(write=True) as db:
            if previous:
                db.execute("DELETE FROM sessions WHERE id=?", (previous,))
            db.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?)", (token, user, store, database.setting("store_password_version"), time.time(), time.time()))
        response = JSONResponse({"ok": True})
        response.set_cookie(cookie_name(request) if request else "attendance_session", token, httponly=True, samesite="strict", secure=request.url.scheme == "https" if request else False, max_age=365 * 86400 if not user else None)
        return response

    @app.get("/api/public")
    def public():
        with database.connect() as db:
            return {"stores": [dict(r) for r in db.execute("SELECT id,name FROM stores WHERE active=1")], "initialized": bool(db.execute("SELECT 1 FROM users LIMIT 1").fetchone()), "maintenance": database.setting("maintenance"), "server_time": now()}

    @app.post("/api/login")
    def login(request: Request, body: dict):
        address = request.client.host if request.client else "local"
        records = attempts.setdefault(address, [])
        records[:] = [t for t in records if time.time() - t < 60]
        if len(records) >= 10:
            raise ValueError("ログイン試行が多いため1分待ってください。")
        records.append(time.time())
        with database.connect() as db:
            if body.get("mode") == "store":
                store = db.execute("SELECT * FROM stores WHERE id=? AND active=1", (int(body["store_id"]),)).fetchone()
                if not store or body.get("store_name") != store["name"] or not password_ok(body.get("password", ""), database.setting("store_password", "")):
                    raise ValueError("店舗名またはパスワードが一致しません。")
                result = login_response(None, store["id"], request.cookies.get(cookie_name(request)), request)
            else:
                user = db.execute("SELECT * FROM users WHERE username=? AND active=1", (body.get("username", ""),)).fetchone()
                employee = db.execute("SELECT active FROM employees WHERE id=?", (user["username"],)).fetchone() if user else None
                if user and len(user["username"]) == 9 and (not employee or not employee[0]):
                    raise PermissionError("従業員の利用が停止されています。")
                if not user or not password_ok(body.get("password", ""), user["password"]):
                    raise ValueError("IDまたはパスワードが一致しません。")
                store = None
                if body.get("mode") == "manager":
                    previous = session(request)
                    store = previous["store_id"]
                    if user["role"] not in ("manager", "hq", "admin") or not store or (user["role"] == "manager" and store not in json.loads(user["stores"])):
                        raise PermissionError("この店舗の管理権限がありません。")
                elif user["role"] not in ("hq", "admin"):
                    raise PermissionError("本社権限が必要です。")
                result = login_response(user["id"], store, request.cookies.get(cookie_name(request)), request)
        records.clear()
        return result

    @app.get("/api/me")
    def me(request: Request):
        s = session(request)
        return {"role": s["role"], "user": user_public(s["user"]), "store_id": s["store_id"], "maintenance": database.setting("maintenance")}

    @app.post("/api/activity")
    def activity(request: Request):
        s = session(request)
        with database.connect(write=True) as db:
            db.execute("UPDATE sessions SET last_seen=? WHERE id=?", (time.time(), s["id"]))
        return {"ok": True}

    @app.post("/api/logout")
    def logout(request: Request):
        try:
            s = session(request)
        except PermissionError:
            with database.connect() as db:
                row = db.execute("SELECT * FROM sessions WHERE id=?", (request.cookies.get(cookie_name(request), ""),)).fetchone()
                s = dict(row) if row else None
                if s and (s["version"] != database.setting("store_password_version") or not db.execute("SELECT 1 FROM stores WHERE id=? AND active=1", (s["store_id"],)).fetchone()):
                    s = None
        if s and s["store_id"] and s["user_id"]:
            return login_response(None, s["store_id"], s["id"], request)
        with database.connect(write=True) as db:
            db.execute("DELETE FROM sessions WHERE id=?", (request.cookies.get(cookie_name(request), ""),))
        response = JSONResponse({"ok": True})
        response.delete_cookie(cookie_name(request))
        return response

    @app.post("/api/password")
    def password(request: Request, body: dict):
        s = manage(request)
        if not password_ok(body.get("old", ""), s["user"]["password"]):
            raise ValueError("現在のパスワードが一致しません。")
        if body.get("new") == "123456":
            raise ValueError("初期パスワード123456以外を設定してください。")
        with database.connect(write=True) as db:
            db.execute("UPDATE users SET password=?,must_change=0 WHERE id=?", (password_hash(body["new"]), s["user_id"]))
            db.execute("DELETE FROM sessions WHERE user_id=? AND id<>?", (s["user_id"], s["id"]))
            database.audit(db, s["user_id"], "パスワード変更", s["user_id"])
        return {"ok": True}

    @app.post("/api/recognize")
    def recognize(request: Request, body: dict):
        s = session(request, ("terminal",))
        if database.setting("maintenance"):
            raise ValueError("保守中です。しばらく待ってください。")
        result = recognizer.check(body["image"])
        if result["authenticated"]:
            with database.connect(write=True) as db:
                shift = asdict(db.execute("SELECT * FROM shifts WHERE employee_id=? AND end IS NULL AND cancelled=0", (result["employee_id"],)).fetchone())
                result["state"] = state(shift)
                result["ticket"] = secrets.token_urlsafe(24)
                # Keep still-valid tickets when a periodic response races a click.
                # The submitted fresh frame is independently checked at punch time.
                db.execute("DELETE FROM tickets WHERE created<?", (time.time() - 5,))
                db.execute("INSERT INTO tickets VALUES (?,?,?,?)", (result["ticket"], s["id"], result["employee_id"], time.time()))
        return result

    @app.post("/api/punch")
    def stamp(request: Request, body: dict):
        s = session(request, ("terminal",))
        rid = str(uuid.UUID(body["request_id"]))
        fingerprint = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        with mutation_lock:
            with database.connect() as db:
                saved = db.execute("SELECT * FROM receipts WHERE id=?", (rid,)).fetchone()
                if saved:
                    if saved["session_id"] != s["id"] or saved["fingerprint"] != fingerprint:
                        raise ValueError("要求IDが別の操作に使用されています。")
                    return json.loads(saved["result"])
                ticket = db.execute("SELECT * FROM tickets WHERE id=? AND session_id=?", (body.get("ticket"), s["id"])).fetchone()
                if not ticket or time.time() - ticket["created"] > 5:
                    raise ValueError("認証結果が期限切れです。再認証してください。")
            received = now()
            if abs((parse(received) - parse(body["timestamp"])).total_seconds()) >= 120:
                raise ValueError("端末の時刻・通信状態を確認し、もう一度打刻してください。")
            if database.setting("maintenance"):
                raise ValueError("保守中です。")
            result = recognizer.check(body["image"])
            if not result["authenticated"] or result["employee_id"] != ticket["employee_id"]:
                raise ValueError("同じ本人を再認証できないため、打刻しませんでした。")
            with database.connect(write=True) as db:
                sid = punch(db, result["employee_id"], s["store_id"], body["action"], body["timestamp"])
                messages = []
                if body["action"] in ("出勤", "休憩終了"):
                    day = work_day(dict(db.execute("SELECT * FROM shifts WHERE id=?", (sid,)).fetchone()))
                    messages = available_notices(db, result["employee_id"], s["store_id"], body["timestamp"], day)
                reply = {"ok": True, "request_id": rid, "store_id": s["store_id"], "name": result["name"], "employee_id": result["employee_id"], "action": body["action"], "timestamp": body["timestamp"], "server_received": received, "shift_id": sid, "notices": messages}
                db.execute("INSERT INTO receipts VALUES (?,?,?,?,?)", (rid, s["id"], fingerprint, json.dumps(reply, ensure_ascii=False), now()))
                db.execute("DELETE FROM tickets WHERE session_id=?", (s["id"],))
                database.audit(db, result["employee_id"], "打刻", sid, after={"action": body["action"], "timestamp": body["timestamp"], "server_received": received})
            return reply

    @app.get("/api/result/{rid}")
    def receipt(rid: str, request: Request):
        s = session(request, ("terminal",))
        with database.connect() as db:
            row = db.execute("SELECT result FROM receipts WHERE id=?", (str(uuid.UUID(rid)),)).fetchone()
        if row:
            result = json.loads(row[0])
            if result.get("store_id") != s["store_id"]:
                raise PermissionError("別店舗の打刻結果は取得できません。")
            return result
        # API requests are serialized: a prior upload has finished before this lookup.
        return {"pending": False, "not_saved": True, "message": "現在のDBにこの打刻の記録はありません。復元・通信断の影響がある場合は店舗責任者へ報告してください。"}

    def available_notices(db, employee, store, at, day):
        current = parse(at)
        rows = []
        for row in db.execute("SELECT n.* FROM notices n JOIN recipients r ON n.id=r.notice_id WHERE r.employee_id=? AND r.ack IS NULL AND n.withdrawn=0 ORDER BY n.start,n.created,n.id", (employee,)):
            if parse(row["start"]) <= current <= parse(row["end"]) and store in json.loads(row["store_ids"]):
                home = employee_store(db, employee, day)
                if home == store or db.execute("SELECT 1 FROM helpers WHERE employee_id=? AND store_id=? AND day=?", (employee, store, day)).fetchone():
                    rows.append(dict(row))
        return rows

    @app.post("/api/ack")
    def ack(request: Request, body: dict):
        s = session(request, ("terminal",))
        with database.connect(write=True) as db:
            receipt = db.execute("SELECT result FROM receipts WHERE id=?", (body["request_id"],)).fetchone()
            if not receipt:
                raise PermissionError("対象の打刻結果がありません。")
            result = json.loads(receipt[0])
            if result.get("store_id") != s["store_id"]:
                raise PermissionError("別店舗の打刻結果です。")
            if not any(n["id"] == int(body["notice_id"]) for n in result.get("notices", [])):
                raise PermissionError("確認できる配信対象ではありません。")
            db.execute("UPDATE recipients SET ack=COALESCE(ack,?) WHERE notice_id=? AND employee_id=?", (now(), body["notice_id"], result["employee_id"]))
        return {"ok": True}

    @app.get("/api/data")
    def data(request: Request):
        s = manage(request)
        with database.connect() as db:
            stores = [dict(r) for r in db.execute("SELECT s.*,c.code FROM stores s LEFT JOIN store_codes c ON c.store_id=s.id")]
            allowed = json.loads(s["user"]["stores"]) if s["role"] == "manager" else [r["id"] for r in stores]
            employees = []
            for row in db.execute("SELECT * FROM employees"):
                person = dict(row)
                profile = db.execute("SELECT kana FROM employee_profiles WHERE employee_id=?", (person["id"],)).fetchone()
                person["kana"] = profile[0] if profile else ""
                person["current_store_id"] = employee_store(db, person["id"], now()[:10])
                if s["role"] == "manager" and person["current_store_id"] not in allowed and not any(db.execute("SELECT 1 FROM helpers WHERE employee_id=? AND store_id=? AND day>=?", (person["id"], st, now()[:10])).fetchone() for st in allowed):
                    continue
                employees.append(person)
            shifts = []
            for row in db.execute("SELECT s.*,e.name FROM shifts s JOIN employees e ON s.employee_id=e.id ORDER BY start DESC"):
                if row["store_id"] in allowed:
                    item = dict(row)
                    gross, actual = duration(item)
                    item.update(gross_seconds=gross, actual_seconds=actual, state="取り消し済み" if item["cancelled"] else ("退勤済み" if item["end"] else state(item)), issue=issue(item))
                    shifts.append(item)
            reqs = [dict(r) for r in db.execute("SELECT r.*,u.name author_name FROM requests r JOIN users u ON r.author=u.id ORDER BY r.updated DESC") if r["store_id"] in allowed]
            notices = []
            for row in db.execute("SELECT * FROM notices ORDER BY created DESC"):
                item = dict(row)
                if not set(json.loads(item["store_ids"])) & set(allowed):
                    continue
                recs = [dict(r) for r in db.execute("SELECT r.*,e.name,e.store_id FROM recipients r JOIN employees e ON r.employee_id=e.id WHERE notice_id=?", (item["id"],))]
                if s["role"] == "manager":
                    recs = [r for r in recs if employee_store(db, r["employee_id"], now()[:10]) in allowed or any(db.execute("SELECT 1 FROM helpers WHERE employee_id=? AND store_id=? AND day BETWEEN ? AND ?", (r["employee_id"], st, item["start"][:10], item["end"][:10])).fetchone() for st in allowed)]
                item.update(recipients=recs, total=len(recs), confirmed=sum(bool(r["ack"]) for r in recs))
                notices.append(item)
            return {"stores": [r for r in stores if r["id"] in allowed], "employees": employees, "shifts": shifts, "requests": reqs, "notices": notices,
                    "backups": [dict(r) for r in db.execute("SELECT * FROM backups ORDER BY created DESC LIMIT 100")] if s["role"] != "manager" else [],
                    "monthly": [dict(r) for r in db.execute("SELECT * FROM monthly") if r["store_id"] in allowed], "annual": [dict(r) for r in db.execute("SELECT * FROM annual")] if s["role"] != "manager" else [],
                    "helpers": [dict(r) for r in db.execute("SELECT * FROM helpers") if r["store_id"] in allowed],
                    "transfers": [dict(r) for r in db.execute("SELECT * FROM transfers") if r["store_id"] in allowed], "kinds": KINDS,
                    "users": [user_public(dict(r)) for r in db.execute("SELECT * FROM users WHERE role<>'employee'")] if s["role"] in ("hq", "admin") else [],
                    "settings": {"backup_root": database.setting("backup_root"), "face_ratio": database.setting("face_ratio")} if s["role"] == "admin" else {}}

    @app.get("/api/request/{rid}")
    def request_detail(rid: str, request: Request):
        s = manage(request)
        with database.connect(write=True) as db:
            r = asdict(db.execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone())
            if not r:
                raise ValueError("申請が見つかりません。")
            scope(s, r["store_id"])
            if s["role"] == "manager":
                db.execute("UPDATE requests SET seen=1 WHERE id=?", (rid,))
            r["history"] = [dict(x) for x in db.execute("SELECT * FROM request_history WHERE request_id=? ORDER BY id", (rid,))]
            r["author_name"] = db.execute("SELECT name FROM users WHERE id=?", (r["author"],)).fetchone()[0]
            r["photos"] = {p["kind"]: base64.b64encode(p["image"]).decode() for p in db.execute("SELECT kind,image FROM request_photos WHERE request_id=?", (rid,))}
            r["shift"] = asdict(db.execute("SELECT * FROM shifts WHERE id=?", (r["shift_id"],)).fetchone()) if r["shift_id"] else None
            return r

    @app.post("/api/request")
    def request_submit(request: Request, body: dict):
        s = manage(request)
        images = body.pop("images", None)
        photos = recognizer.register(images) if images else None
        with mutation_lock:
            if database.setting("maintenance"):
                raise ValueError("保守中です。")
            direct = s["role"] in ("hq", "admin") and body["kind"] in ("register", "profile", "correction")
            rid = submit(database, s["user"], body, photos, direct=direct)
            return {"id": rid, "direct": direct}

    @app.post("/api/request/{rid}/review")
    def request_review(rid: str, request: Request, body: dict):
        s = session(request, ("hq", "admin"))
        with mutation_lock:
            review(database, s["user"], rid, body["decision"], body.get("reason", ""))
        return {"ok": True}

    @app.post("/api/request/{rid}/cancel")
    def request_cancel(rid: str, request: Request):
        s = manage(request)
        with database.connect(write=True) as db:
            r = db.execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()
            if not r or r["author"] != s["user_id"] or r["status"] not in ("申請中", "差し戻し"):
                raise PermissionError("この申請は取り消せません。")
            db.execute("UPDATE requests SET status='取り消し',updated=? WHERE id=?", (now(), rid))
            db.execute("DELETE FROM request_photos WHERE request_id=?", (rid,))
            database.audit(db, s["user_id"], "申請取り消し", rid)
        return {"ok": True}

    @app.post("/api/notice")
    def notice_save(request: Request, body: dict):
        s = manage(request)
        stores = [int(x) for x in body["store_ids"]]
        if not stores:
            raise ValueError("配信店舗を選択してください。")
        for store in stores:
            scope(s, store)
        start, end = parse(body["start"]), parse(body["end"])
        if end <= start or not body.get("title", "").strip() or not body.get("body", "").strip():
            raise ValueError("件名・本文・掲載期間を確認してください。")
        with database.connect(write=True) as db:
            recipients = set(body["recipients"])
            if not recipients:
                raise ValueError("配信先を選択してください。")
            for employee in recipients:
                person = db.execute("SELECT * FROM employees WHERE id=? AND active=1", (employee,)).fetchone()
                if not person:
                    raise ValueError("配信先に利用停止中の人が含まれています。")
                home = employee_store(db, employee, start.date().isoformat())
                if home not in stores and not any(db.execute("SELECT 1 FROM helpers WHERE employee_id=? AND store_id=? AND day BETWEEN ? AND ?", (employee, st, start.date().isoformat(), end.date().isoformat())).fetchone() for st in stores):
                    raise ValueError("配信店舗に所属・応援予定のない人が含まれています。")
            nid = body.get("id")
            before = None
            if nid:
                before = asdict(db.execute("SELECT * FROM notices WHERE id=?", (nid,)).fetchone())
                if not before:
                    raise ValueError("連絡がありません。")
                for st in json.loads(before["store_ids"]):
                    scope(s, st)
                old_recipients = {r[0] for r in db.execute("SELECT employee_id FROM recipients WHERE notice_id=?", (nid,))}
                if not old_recipients <= recipients:
                    raise ValueError("配信後の宛先は追加のみ可能です。既存対象は削除できません。")
                db.execute("UPDATE notices SET title=?,body=?,store_ids=?,start=?,end=?,updated=? WHERE id=?", (body["title"], body["body"], json.dumps(stores), start.isoformat(), end.isoformat(), now(), nid))
            else:
                nid = db.execute("INSERT INTO notices(title,body,author,store_ids,start,end,created,updated) VALUES (?,?,?,?,?,?,?,?)", (body["title"], body["body"], s["user_id"], json.dumps(stores), start.isoformat(), end.isoformat(), now(), now())).lastrowid
            for employee in recipients:
                db.execute("INSERT OR IGNORE INTO recipients(notice_id,employee_id) VALUES (?,?)", (nid, employee))
            database.audit(db, s["user_id"], "業務連絡更新", nid, before, {k: v for k, v in body.items() if k != "images"})
        return {"id": nid}

    @app.post("/api/notice/{nid}/withdraw")
    def notice_withdraw(nid: int, request: Request):
        s = manage(request)
        with database.connect(write=True) as db:
            row = db.execute("SELECT * FROM notices WHERE id=?", (nid,)).fetchone()
            if not row:
                raise ValueError("連絡がありません。")
            for st in json.loads(row["store_ids"]):
                scope(s, st)
            db.execute("UPDATE notices SET withdrawn=1,updated=? WHERE id=?", (now(), nid))
            database.audit(db, s["user_id"], "業務連絡取り下げ", nid)
        return {"ok": True}

    @app.get("/api/monthly")
    def month_report(request: Request, month: str, store: int | None = None):
        session(request, ("hq", "admin"))
        datetime.strptime(month, "%Y-%m")
        with database.connect() as db:
            return monthly_report(db, month, store)

    @app.post("/api/monthly")
    def month_change(request: Request, body: dict):
        s = session(request, ("hq", "admin"))
        month = body["month"]
        datetime.strptime(month, "%Y-%m")
        store = int(body["store_id"])
        closed = bool(body["closed"])
        with mutation_lock, database.connect(write=True) as db:
            if closed:
                rows = [dict(r) for r in db.execute("SELECT * FROM shifts WHERE store_id=? AND substr(start,1,7)=? AND cancelled=0", (store, month))]
                if any(not r["end"] or issue(r) for r in rows):
                    raise ValueError("要確認・未退勤の勤務が残っています。")
                pending = db.execute("SELECT * FROM requests WHERE store_id=? AND kind IN ('correction','cancel') AND status IN ('申請中','差し戻し')", (store,)).fetchall()
                if any((json.loads(r["payload"]).get("start", "")[:7] == month) or (r["shift_id"] and db.execute("SELECT 1 FROM shifts WHERE id=? AND substr(start,1,7)=?", (r["shift_id"], month)).fetchone()) for r in pending):
                    raise ValueError("承認待ちの修正申請が残っています。")
            db.execute("INSERT INTO monthly VALUES (?,?,?,?,?) ON CONFLICT(store_id,month) DO UPDATE SET closed=excluded.closed,actor=excluded.actor,updated=excluded.updated", (store, month, int(closed), s["user_id"], now()))
            if not closed:
                db.execute("UPDATE annual SET completed=0 WHERE year=?", (int(month[:4]),))
            database.audit(db, s["user_id"], "月次確定" if closed else "月次解除", f"{store}/{month}")
        return {"ok": True}

    @app.post("/api/annual")
    def annual(request: Request, body: dict):
        s = session(request, ("hq", "admin"))
        year = int(body["year"])
        with database.connect(write=True) as db:
            stores = [r[0] for r in db.execute("SELECT id FROM stores")]
            for store in stores:
                for month in range(1, 13):
                    row = db.execute("SELECT closed FROM monthly WHERE store_id=? AND month=?", (store, f"{year}-{month:02}")).fetchone()
                    if not row or not row[0]:
                        raise ValueError("全店舗・全対象月の確定が必要です。")
            if any(not r["end"] or issue(r) for r in db.execute("SELECT * FROM shifts WHERE substr(start,1,4)=? AND cancelled=0", (str(year),))):
                raise ValueError("要確認・未退勤が残っています。")
            if db.execute("SELECT 1 FROM requests r LEFT JOIN shifts s ON r.shift_id=s.id WHERE r.status IN ('申請中','差し戻し') AND (substr(s.start,1,4)=? OR substr(json_extract(r.payload,'$.start'),1,4)=?) LIMIT 1", (str(year), str(year))).fetchone():
                raise ValueError("承認待ちが残っています。")
            db.execute("INSERT INTO annual VALUES (?,1,?,?) ON CONFLICT(year) DO UPDATE SET completed=1,actor=excluded.actor,updated=excluded.updated", (year, s["user_id"], now()))
            database.audit(db, s["user_id"], "年度完了", year)
        return {"ok": True}

    @app.get("/api/export")
    def export(request: Request, month: str, store: int | None = None):
        s = session(request, ("hq", "admin"))
        datetime.strptime(month, "%Y-%m")
        with mutation_lock:
            data = export_zip(database, month, store)
            maintenance.snapshot(month)
            with database.connect(write=True) as db:
                database.audit(db, s["user_id"], "CSV出力", month, after={"store_id": store})
        return Response(data, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="attendance_{month}.zip"'})

    @app.post("/api/settings")
    def settings(request: Request, body: dict):
        s = session(request, ("hq", "admin"))
        with database.connect(write=True) as db:
            if "timeout" in body:
                value = int(body["timeout"])
                if value < 0 or value > 86400:
                    raise ValueError("自動ログアウト時間を確認してください。")
                db.execute("UPDATE users SET timeout=? WHERE id=?", (value, s["user_id"]))
            for key in ("backup_root", "face_ratio", "store_password"):
                if key not in body:
                    continue
                if s["role"] != "admin":
                    raise PermissionError("管理責任者の権限が必要です。")
                value = body[key]
                if key == "store_password":
                    value = password_hash(value)
                    database.set(db, "store_password_version", database.setting("store_password_version") + 1)
                    db.execute("DELETE FROM sessions WHERE store_id IS NOT NULL")
                if key == "face_ratio":
                    value = float(value)
                    if not 0 < value <= 1:
                        raise ValueError("顔面積比は0より大きく1以下です。")
                if key == "backup_root":
                    path = Path(value).resolve()
                    if path == path.anchor or path == database.path.parent:
                        raise ValueError("専用バックアップフォルダを指定してください。")
                    path.mkdir(parents=True, exist_ok=True)
                    value = str(path)
                database.set(db, key, value)
                database.audit(db, s["user_id"], "設定変更", key)
        return {"ok": True}

    @app.post("/api/store")
    def store_change(request: Request, body: dict):
        s = manage(request, True)
        with database.connect(write=True) as db:
            if body.get("id"):
                store = int(body["id"])
                if not body["active"] and db.execute("SELECT 1 FROM shifts WHERE store_id=? AND end IS NULL AND cancelled=0", (store,)).fetchone():
                    raise ValueError("勤務中の人がいるため停止できません。")
                db.execute("UPDATE stores SET active=? WHERE id=?", (int(body["active"]), store))
            else:
                if not body["name"].strip():
                    raise ValueError("店舗名が必要です。")
                cursor = db.execute("INSERT INTO stores(name) VALUES (?)", (body["name"].strip(),))
                store_code(db, cursor.lastrowid)
            database.audit(db, s["user_id"], "店舗設定", body.get("id", body.get("name")))
        return {"ok": True}

    @app.post("/api/user")
    def user_change(request: Request, body: dict):
        s = session(request, ("hq", "admin"))
        change_role(database, s["user"], str(body["employee_id"]), body["role"], body.get("stores", []))
        return {"ok": True}

    @app.post("/api/plan/{kind}/{identifier}")
    def plan_change(kind: str, identifier: int, request: Request, body: dict):
        s = session(request, ("hq", "admin"))
        with mutation_lock:
            change_plan(database, s["user"], kind, identifier, body)
        return {"ok": True}

    @app.post("/api/backup")
    def backup(request: Request, body: dict):
        session(request, ("hq", "admin"))
        with mutation_lock:
            if body.get("kind") == "sqlite":
                maintenance.snapshot(body.get("month", now()[:7]))
            else:
                maintenance.daily()
        return {"ok": True}

    @app.post("/api/restore")
    def restore(request: Request, body: dict):
        s = manage(request, True)
        if body.get("confirm") != "復元する":
            raise ValueError("確認欄へ「復元する」と入力してください。")
        with mutation_lock:
            maintenance.restore(body["backup_id"], s["user_id"])
        return {"ok": True, "relogin": True}

    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

    @app.get("/hq")
    @app.get("/store")
    @app.get("/")
    def index():
        return FileResponse(ROOT / "static" / "index.html")

    return app
