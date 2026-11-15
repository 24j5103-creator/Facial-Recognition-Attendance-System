from contextlib import contextmanager
from datetime import datetime, timedelta
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid
import zipfile

from fastapi.testclient import TestClient
from app.db import Database, JST, now, password_hash
from app.web import create_app
from app.requests import submit, review
from app.attendance import monthly_report, duration
from app.maintenance import Maintenance


class FakeRecognition:
    def __init__(self):
        self.employee = "D001"
        self.authenticated = True
    def check(self, image):
        return {"authenticated": self.authenticated, "employee_id": self.employee, "name": "テスト従業員", "box": [0, 0, 100, 100], "width": 200, "height": 200, "message": "テスト専用"}
    def register(self, images):
        if set(images) != {"normal", "mask"}:
            raise ValueError("2枚必要です。")
        return {k: (b"test-image", b"test-vector") for k in images}


class SystemTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = Database(self.root / "attendance.db")
        self.pw = "Test-Password-12345"
        with self.db.connect(write=True) as db:
            for name in ("デモ店舗A", "デモ店舗B", "デモ店舗C"):
                db.execute("INSERT INTO stores(name) VALUES (?)", (name,))
            for username, role, stores in (("admin", "admin", "[]"), ("hq", "hq", "[]"), ("manager", "manager", "[1]")):
                db.execute("INSERT INTO users(username,name,password,role,stores) VALUES (?,?,?,?,?)", (username, username, password_hash(self.pw), role, stores))
            db.execute("INSERT INTO employees VALUES ('D001','テスト従業員',1,1)")
            db.execute("INSERT INTO employees VALUES ('D002','他店舗従業員',2,1)")
            self.db.set(db, "store_password", password_hash(self.pw))
        self.fake = FakeRecognition()
        self.app = create_app(self.db, self.fake, scheduled=False)
        self.client = TestClient(self.app)
        self.hq = TestClient(self.app)
        self.manager = TestClient(self.app)
        self.admin = TestClient(self.app)
        self.login_store(self.client)
        self.hq.post("/api/login", json={"mode": "hq", "username": "hq", "password": self.pw})
        self.admin.post("/api/login", json={"mode": "hq", "username": "admin", "password": self.pw})
        self.login_store(self.manager)
        self.manager.post("/api/login", json={"mode": "manager", "username": "manager", "password": self.pw})
        with self.db.connect() as db:
            self.author = dict(db.execute("SELECT * FROM users WHERE username='manager'").fetchone())
            self.reviewer = dict(db.execute("SELECT * FROM users WHERE username='hq'").fetchone())

    def tearDown(self):
        self.client.close(); self.hq.close(); self.manager.close(); self.admin.close()
        self.tmp.cleanup()

    def login_store(self, client, store=1):
        name = {1: "デモ店舗A", 2: "デモ店舗B"}[store]
        result = client.post("/api/login", json={"mode": "store", "store_id": store, "store_name": name, "password": self.pw})
        self.assertEqual(result.status_code, 200, result.text)

    def stamp_body(self, action="出勤", timestamp=None):
        result = self.client.post("/api/recognize", json={"image": "test"}).json()
        return {"request_id": str(uuid.uuid4()), "ticket": result["ticket"], "image": "test", "timestamp": timestamp or now(), "action": action}

    def add_shift(self, start="2026-08-01T09:00:00+09:00", end="2026-08-01T17:00:00+09:00", store=1, employee="D001"):
        sid = str(uuid.uuid4())
        with self.db.connect(write=True) as db:
            db.execute("INSERT INTO shifts(id,employee_id,store_id,start,end) VALUES (?,?,?,?,?)", (sid, employee, store, start, end))
        return sid

    def correction(self, sid, payload, kind="correction", employee="D001"):
        return submit(self.db, self.author, {"kind": kind, "store_id": 1, "employee_id": employee, "shift_id": sid, "reason": "テスト修正", "payload": payload})

    def test_same_day_reentry_and_break_state(self):
        for action in ("出勤", "休憩開始", "休憩終了", "退勤", "出勤", "退勤"):
            result = self.client.post("/api/punch", json=self.stamp_body(action))
            self.assertEqual(result.status_code, 200, result.text)
        with self.db.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM shifts").fetchone()[0], 2)

    def test_no_exit_during_break(self):
        self.client.post("/api/punch", json=self.stamp_body())
        self.client.post("/api/punch", json=self.stamp_body("休憩開始"))
        self.assertEqual(self.client.post("/api/punch", json=self.stamp_body("退勤")).status_code, 400)

    def test_duplicate_receipt_is_atomic(self):
        body = self.stamp_body()
        first = self.client.post("/api/punch", json=body)
        second = self.client.post("/api/punch", json=body)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json(), second.json())
        self.assertEqual(self.client.get("/api/result/" + body["request_id"]).json(), first.json())
        with self.db.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM shifts").fetchone()[0], 1)

    def test_changed_identity_rejected(self):
        body = self.stamp_body()
        self.fake.employee = "D002"
        self.assertEqual(self.client.post("/api/punch", json=body).status_code, 400)

    def test_ticket_still_valid_after_four_seconds(self):
        body = self.stamp_body()
        with self.db.connect(write=True) as db:
            db.execute("UPDATE tickets SET created=?", (time.time() - 4,))
        result = self.client.post("/api/punch", json=body)
        self.assertEqual(result.status_code, 200, result.text)

    def test_ticket_expiry(self):
        body = self.stamp_body()
        with self.db.connect(write=True) as db:
            db.execute("UPDATE tickets SET created=?", (time.time() - 6,))
        self.assertEqual(self.client.post("/api/punch", json=body).status_code, 400)

    def test_clock_120_seconds_rejected(self):
        body = self.stamp_body(timestamp=(datetime.now(JST)-timedelta(seconds=121)).isoformat())
        self.assertEqual(self.client.post("/api/punch", json=body).status_code, 400)

    def test_roles_and_scopes(self):
        self.assertEqual(self.manager.get("/api/export?month=2026-08").status_code, 403)
        sid = self.add_shift(store=2, employee="D002")
        rows = self.manager.get("/api/data").json()
        self.assertNotIn(sid, [r["id"] for r in rows["shifts"]])
        self.assertNotIn("D002", [r["id"] for r in rows["employees"]])
        self.assertEqual(self.hq.post("/api/settings", json={"store_password": "Another-Password-123"}).status_code, 403)

    def test_self_approval_and_pending_duplicate(self):
        sid = self.add_shift()
        rid = self.correction(sid, {"end": "2026-08-01T16:00:00+09:00"})
        with self.assertRaises(sqlite3.IntegrityError):
            self.correction(sid, {}, "cancel")
        author_as_hq = {**self.author, "role": "hq"}
        with self.assertRaises(PermissionError):
            review(self.db, author_as_hq, rid, "承認")

    def test_partial_overwrite_uses_latest_other_fields(self):
        sid = self.add_shift()
        rid = self.correction(sid, {"end": "2026-08-01T18:00:00+09:00"})
        with self.db.connect(write=True) as db:
            db.execute("UPDATE shifts SET start='2026-08-01T10:00:00+09:00' WHERE id=?", (sid,))
        review(self.db, self.reviewer, rid, "承認")
        with self.db.connect() as db:
            shift = dict(db.execute("SELECT * FROM shifts WHERE id=?", (sid,)).fetchone())
        self.assertEqual(shift["start"], "2026-08-01T10:00:00+09:00")
        self.assertEqual(shift["end"], "2026-08-01T18:00:00+09:00")

    def test_old_month_difference_not_double_counted(self):
        sid = self.add_shift()
        rid = self.correction(sid, {"end": "2026-08-01T16:00:00+09:00"})
        with patch("app.requests.now", return_value="2026-09-05T12:00:00+09:00"):
            review(self.db, self.reviewer, rid, "承認")
        with self.db.connect() as db:
            august = monthly_report(db, "2026-08")["rows"][0]
            sept = monthly_report(db, "2026-09")["rows"][0]
        self.assertEqual(august["adjusted_seconds"], 8*3600)
        self.assertEqual(sept["carry_out"], 3600)
        self.assertEqual(sept["runs"], 0)

    def test_cross_store_carry_allocates_oldest_shift_first(self):
        sid = self.add_shift()
        rid = self.correction(sid, {"end": "2026-08-01T12:00:00+09:00"})
        with patch("app.requests.now", return_value="2026-09-05T12:00:00+09:00"):
            review(self.db, self.reviewer, rid, "承認")
        self.add_shift("2026-09-01T09:00:00+09:00", "2026-09-01T11:00:00+09:00", 1)
        self.add_shift("2026-09-02T09:00:00+09:00", "2026-09-02T11:00:00+09:00", 2)
        with self.db.connect() as db:
            report = monthly_report(db, "2026-09")
        self.assertEqual(sum(r["adjusted_seconds"] for r in report["rows"]), 0)
        self.assertEqual(sum(r["carry_out"] for r in report["rows"]), 3600)
        self.assertEqual([r["store_id"] for r in report["allocations"]], [1, 2])

    def test_special_new_day_and_same_day_extra_run(self):
        for hour in (9, 15):
            rid = self.correction(None, {"start": f"2026-08-01T{hour:02}:00:00+09:00", "end": f"2026-08-01T{hour+2:02}:00:00+09:00"})
            with patch("app.requests.now", return_value="2026-09-05T12:00:00+09:00"):
                review(self.db, self.reviewer, rid, "承認")
        with self.db.connect() as db:
            row = monthly_report(db, "2026-09")["rows"][0]
        self.assertEqual(row["days"], 1)
        self.assertEqual(row["runs"], 2)
        self.assertEqual(row["special_days"], 1)

    def test_closed_approval_month_blocks_review(self):
        sid = self.add_shift()
        rid = self.correction(sid, {"end": "2026-08-01T16:00:00+09:00"})
        with self.db.connect(write=True) as db:
            db.execute("INSERT INTO monthly VALUES (1,?,1,1,?)", (now()[:7], now()))
        with self.assertRaises(ValueError):
            review(self.db, self.reviewer, rid, "承認")

    def test_photo_two_images_approval_and_stop(self):
        body = {"kind": "photo", "store_id": 1, "employee_id": "D001", "reason": "写真更新", "payload": {}, "images": {"normal": "a", "mask": "b"}}
        r = self.manager.post("/api/request", json=body)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.hq.post(f"/api/request/{r.json()['id']}/review", json={"decision": "承認"}).status_code, 200)
        with self.db.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM photos").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT count(*) FROM request_photos").fetchone()[0], 0)
        rid = submit(self.db, self.author, {"kind": "stop", "employee_id": "D001", "store_id": 1, "reason": "退職", "payload": {}})
        review(self.db, self.reviewer, rid, "承認")
        with self.db.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM photos").fetchone()[0], 0)

    def test_notice_edit_keeps_read_state_and_recipients(self):
        body = {"title": "業務連絡", "body": "本文", "store_ids": [1], "start": "2026-01-01T00:00:00+09:00", "end": "2027-12-31T23:59:59+09:00", "recipients": ["D001"]}
        result = self.manager.post("/api/notice", json=body)
        self.assertEqual(result.status_code, 200, result.text)
        nid = result.json()["id"]
        with self.db.connect(write=True) as db:
            db.execute("UPDATE recipients SET ack=? WHERE notice_id=?", (now(), nid))
        result = self.manager.post("/api/notice", json={**body, "id": nid, "body": "本文修正"})
        self.assertEqual(result.status_code, 200)
        data = self.manager.get("/api/data").json()["notices"][0]
        self.assertEqual(data["confirmed"], 1)
        self.assertIsNotNone(data["recipients"][0]["ack"])

    def test_common_password_invalidates_store_sessions_only(self):
        self.assertEqual(self.admin.post("/api/settings", json={"store_password": "ReplacementPassword123"}).status_code, 200)
        self.assertEqual(self.client.get("/api/me").status_code, 403)
        self.assertEqual(self.hq.get("/api/me").status_code, 200)

    def test_expired_manager_returns_terminal(self):
        with self.db.connect(write=True) as db:
            db.execute("UPDATE sessions SET last_seen=? WHERE user_id=?", (time.time()-301, self.author["id"]))
        self.assertEqual(self.manager.get("/api/me").status_code, 403)
        self.manager.post("/api/logout", json={})
        self.assertEqual(self.manager.get("/api/me").json()["role"], "terminal")

    def test_export_four_csv_and_snapshot(self):
        self.add_shift()
        r = self.hq.get("/api/export?month=2026-08")
        self.assertEqual(r.status_code, 200, r.text[:200] if r.status_code!=200 else '')
        with zipfile.ZipFile(io.BytesIO(r.content)) as archive:
            self.assertEqual(len(archive.namelist()), 4)
            self.assertTrue(archive.read("月次集計.csv").startswith(b"\xef\xbb\xbf"))
        self.assertTrue((self.root / "backups/sqlite/2026/attendance_2026-08.db").exists())

    def test_csv_expiry_independent_of_sqlite(self):
        maintenance = Maintenance(self.db)
        path = maintenance.root / "csv/old.zip"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"test")
        with self.db.connect(write=True) as db:
            db.execute("INSERT INTO backups VALUES ('old','csv',?,'2026-01',?,1,'')", (str(path), "2026-01-01T03:00:00+09:00"))
        maintenance.cleanup(datetime(2026, 1, 31, 3, tzinfo=JST))
        self.assertFalse(path.exists())

    def test_sqlite_year_delete_requires_complete_and_newer_valid(self):
        maintenance = Maintenance(self.db)
        old = maintenance.snapshot("2026-12")
        maintenance.cleanup(datetime(2027, 12, 31, 3, tzinfo=JST))
        self.assertTrue(old.exists())
        with self.db.connect(write=True) as db:
            db.execute("INSERT INTO annual VALUES (2026,1,1,?)", (now(),))
        maintenance.cleanup(datetime(2027, 12, 31, 3, tzinfo=JST))
        self.assertTrue(old.exists())
        maintenance.snapshot("2027-11")
        maintenance.cleanup(datetime(2027, 12, 31, 3, tzinfo=JST))
        self.assertFalse(old.exists())

    def test_restore_keeps_pre_restore_and_invalidates_sessions(self):
        maintenance = Maintenance(self.db)
        maintenance.snapshot("2026-11")
        with self.db.connect(write=True) as db:
            backup = db.execute("SELECT id FROM backups WHERE kind='sqlite' LIMIT 1").fetchone()[0]
            db.execute("UPDATE employees SET name='後の変更' WHERE id='D001'")
        maintenance.restore(backup, 1)
        with self.db.connect() as db:
            self.assertEqual(db.execute("SELECT name FROM employees WHERE id='D001'").fetchone()[0], "テスト従業員")
            self.assertEqual(db.execute("SELECT count(*) FROM sessions").fetchone()[0], 0)
        self.assertTrue(list((maintenance.root/'pre_restore').glob('*.db')))

    def test_origin_check(self):
        self.assertEqual(self.client.post("/api/logout", json={}, headers={"Origin": "https://other.invalid"}).status_code, 403)

    def test_combined_employee_total_deduplicates_same_day_across_stores(self):
        self.add_shift("2026-08-01T09:00:00+09:00", "2026-08-01T12:00:00+09:00", 1)
        self.add_shift("2026-08-01T15:00:00+09:00", "2026-08-01T18:00:00+09:00", 2)
        with self.db.connect() as db:
            row = monthly_report(db, "2026-08")["employee_totals"][0]
        self.assertEqual(row["days"], 1)
        self.assertEqual(row["runs"], 2)
        self.assertEqual(row["actual_seconds"], 6*3600)

    def test_result_recoverable_after_same_store_relogin(self):
        body = self.stamp_body()
        first = self.client.post("/api/punch", json=body).json()
        self.client.post("/api/logout", json={})
        self.login_store(self.client)
        self.assertEqual(self.client.get("/api/result/"+body["request_id"]).json(), first)
        self.assertTrue(self.client.get("/api/result/"+str(uuid.uuid4())).json()["not_saved"])

    def test_help_notice_on_next_day_uses_shift_start_day(self):
        self.add_shift("2026-11-13T22:00:00+09:00", None, 2)
        with self.db.connect(write=True) as db:
            db.execute("INSERT INTO helpers(employee_id,store_id,day,request_id) VALUES ('D001',2,'2026-11-13','test')")
            nid = db.execute("INSERT INTO notices(title,body,author,store_ids,start,end,created,updated) VALUES ('応援連絡','本文',1,'[2]','2026-11-13T00:00:00+09:00','2026-11-15T00:00:00+09:00',?,?)", (now(), now())).lastrowid
            db.execute("INSERT INTO recipients(notice_id,employee_id) VALUES (?,'D001')", (nid,))
            db.execute("UPDATE shifts SET breaks=? WHERE end IS NULL", (json.dumps([{"start": "2026-11-14T00:10:00+09:00", "end": None}]),))
        self.login_store(self.client, 2)
        body = self.stamp_body("休憩終了", "2026-11-14T00:20:00+09:00")
        with patch("app.web.now", return_value="2026-11-14T00:20:01+09:00"):
            result = self.client.post("/api/punch", json=body)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(len(result.json()["notices"]), 1)

    def test_maintenance_blocks_updates(self):
        with self.db.connect(write=True) as db:
            self.db.set(db, "maintenance", True)
        self.assertEqual(self.admin.post("/api/store", json={"name": "禁止"}).status_code, 503)

    def test_same_month_snapshot_replaces_old_generation(self):
        m = Maintenance(self.db)
        m.snapshot("2026-11")
        m.snapshot("2026-11")
        with self.db.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM backups WHERE kind='sqlite' AND valid=1").fetchone()[0], 1)
        self.assertEqual(len(list((m.root/'sqlite/2026').glob('*.db'))), 1)

    def test_periodic_recognition_does_not_invalidate_click_ticket(self):
        body = self.stamp_body()
        self.client.post("/api/recognize", json={"image": "next-frame"})
        result = self.client.post("/api/punch", json=body)
        self.assertEqual(result.status_code, 200, result.text)


if __name__ == "__main__":
    unittest.main()
