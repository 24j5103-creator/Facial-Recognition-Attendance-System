import hashlib
import hmac
import json
import os
import re
from pathlib import Path
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta

JST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parents[1]


def now():
    return datetime.now(JST).isoformat(timespec="seconds")


def parse(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}(?::[0-9]{2}(?:\.[0-9]{1,6})?)?(?:Z|[+-][0-9]{2}:[0-9]{2})", value):
        raise ValueError("日時は西暦4桁と月日時分・タイムゾーンを指定してください。")
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError("日時にはタイムゾーンを含めてください。")
    return dt.astimezone(JST)


def password_hash(value):
    if len(value) < 6:
        raise ValueError("パスワードは6文字以上にしてください。")
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", value.encode(), bytes.fromhex(salt), 310000).hex()
    return salt + ":" + digest


def password_ok(value, stored):
    try:
        salt, expected = stored.split(":")
        actual = hashlib.pbkdf2_hmac("sha256", value.encode(), bytes.fromhex(salt), 310000).hex()
        return hmac.compare_digest(actual, expected)
    except (ValueError, AttributeError):
        return False


SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS stores(id INTEGER PRIMARY KEY,name TEXT UNIQUE NOT NULL,active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE NOT NULL,name TEXT NOT NULL,password TEXT NOT NULL,role TEXT NOT NULL,stores TEXT NOT NULL DEFAULT '[]',active INTEGER NOT NULL DEFAULT 1,timeout INTEGER NOT NULL DEFAULT 1800,must_change INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY,user_id INTEGER,store_id INTEGER,version INTEGER NOT NULL,last_seen REAL NOT NULL,created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS employees(id TEXT PRIMARY KEY,name TEXT NOT NULL,store_id INTEGER NOT NULL,active INTEGER NOT NULL DEFAULT 1,FOREIGN KEY(store_id) REFERENCES stores(id));
CREATE TABLE IF NOT EXISTS photos(employee_id TEXT NOT NULL,kind TEXT NOT NULL,image BLOB NOT NULL,vector BLOB NOT NULL,PRIMARY KEY(employee_id,kind),FOREIGN KEY(employee_id) REFERENCES employees(id));
CREATE TABLE IF NOT EXISTS transfers(id INTEGER PRIMARY KEY,employee_id TEXT NOT NULL,store_id INTEGER NOT NULL,effective TEXT NOT NULL,request_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS helpers(id INTEGER PRIMARY KEY,employee_id TEXT NOT NULL,store_id INTEGER NOT NULL,day TEXT NOT NULL,request_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS shifts(id TEXT PRIMARY KEY,employee_id TEXT NOT NULL,store_id INTEGER NOT NULL,start TEXT NOT NULL,end TEXT,breaks TEXT NOT NULL DEFAULT '[]',cancelled INTEGER NOT NULL DEFAULT 0,version INTEGER NOT NULL DEFAULT 1,FOREIGN KEY(employee_id) REFERENCES employees(id));
CREATE UNIQUE INDEX IF NOT EXISTS one_open_shift ON shifts(employee_id) WHERE end IS NULL AND cancelled=0;
CREATE TABLE IF NOT EXISTS requests(id TEXT PRIMARY KEY,kind TEXT NOT NULL,store_id INTEGER NOT NULL,employee_id TEXT,shift_id TEXT,author INTEGER NOT NULL,payload TEXT NOT NULL,reason TEXT NOT NULL,status TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 1,created TEXT NOT NULL,updated TEXT NOT NULL,reviewer INTEGER,reviewed TEXT,review_reason TEXT,seen INTEGER NOT NULL DEFAULT 0);
CREATE UNIQUE INDEX IF NOT EXISTS one_pending_correction ON requests(shift_id) WHERE status IN ('申請中','差し戻し') AND kind IN ('correction','cancel') AND shift_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS request_photos(request_id TEXT NOT NULL,kind TEXT NOT NULL,image BLOB NOT NULL,vector BLOB NOT NULL,PRIMARY KEY(request_id,kind));
CREATE TABLE IF NOT EXISTS request_history(id INTEGER PRIMARY KEY,request_id TEXT NOT NULL,revision INTEGER NOT NULL,actor INTEGER NOT NULL,event TEXT NOT NULL,payload TEXT NOT NULL,created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY,actor TEXT NOT NULL,event TEXT NOT NULL,target TEXT NOT NULL,before_data TEXT,after_data TEXT,created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS postings(id INTEGER PRIMARY KEY,request_id TEXT NOT NULL,employee_id TEXT NOT NULL,store_id INTEGER NOT NULL,work_month TEXT NOT NULL,work_day TEXT NOT NULL,approved TEXT NOT NULL,month TEXT NOT NULL,seconds INTEGER NOT NULL,gross INTEGER NOT NULL,days INTEGER NOT NULL,runs INTEGER NOT NULL,special INTEGER NOT NULL DEFAULT 0,work_start TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS monthly(store_id INTEGER NOT NULL,month TEXT NOT NULL,closed INTEGER NOT NULL DEFAULT 0,actor INTEGER,updated TEXT,PRIMARY KEY(store_id,month));
CREATE TABLE IF NOT EXISTS annual(year INTEGER PRIMARY KEY,completed INTEGER NOT NULL DEFAULT 0,actor INTEGER,updated TEXT);
CREATE TABLE IF NOT EXISTS notices(id INTEGER PRIMARY KEY,title TEXT NOT NULL,body TEXT NOT NULL,author INTEGER NOT NULL,store_ids TEXT NOT NULL,start TEXT NOT NULL,end TEXT NOT NULL,withdrawn INTEGER NOT NULL DEFAULT 0,created TEXT NOT NULL,updated TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS recipients(notice_id INTEGER NOT NULL,employee_id TEXT NOT NULL,ack TEXT,PRIMARY KEY(notice_id,employee_id),FOREIGN KEY(notice_id) REFERENCES notices(id));
CREATE TABLE IF NOT EXISTS receipts(id TEXT PRIMARY KEY,session_id TEXT NOT NULL,fingerprint TEXT NOT NULL,result TEXT NOT NULL,created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tickets(id TEXT PRIMARY KEY,session_id TEXT NOT NULL,employee_id TEXT NOT NULL,created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS backups(id TEXT PRIMARY KEY,kind TEXT NOT NULL,path TEXT NOT NULL,month TEXT,created TEXT NOT NULL,valid INTEGER NOT NULL DEFAULT 0,detail TEXT NOT NULL DEFAULT '');
"""


class Database:
    def __init__(self, path=None):
        self.path = Path(path or os.environ.get("ATTENDANCE_DB", ROOT / "data" / "attendance.db"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(SCHEMA)
            from .identity import SCHEMA as IDENTITY_SCHEMA
            db.executescript(IDENTITY_SCHEMA)
            defaults = {"store_password_version": 1, "face_threshold": 0.6, "liveness_threshold": 0.8, "face_ratio": 0.8, "backup_root": str(self.path.parent / "backups"), "last_daily": "", "maintenance": False}
            for key, value in defaults.items():
                db.execute("INSERT OR IGNORE INTO settings VALUES (?,?)", (key, json.dumps(value)))

    @contextmanager
    def connect(self, write=False):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA secure_delete=ON")
        db.execute("PRAGMA busy_timeout=30000")
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def setting(self, key, default=None):
        with self.connect() as db:
            row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def set(self, db, key, value):
        db.execute("INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value, ensure_ascii=False)))

    def audit(self, db, actor, event, target, before=None, after=None):
        db.execute("INSERT INTO audit(actor,event,target,before_data,after_data,created) VALUES (?,?,?,?,?,?)", (str(actor), event, str(target), json.dumps(before, ensure_ascii=False) if before is not None else None, json.dumps(after, ensure_ascii=False) if after is not None else None, now()))


def asdict(row):
    return dict(row) if row is not None else None
