"""Stable employee numbers, linked roles and an explicit, backed-up v2 migration."""
import getpass
import json
from pathlib import Path
import re
import sqlite3
from contextlib import closing
import uuid
from .db import now, password_hash, password_ok

ROLES = {"employee": "一般従業員", "manager": "店舗責任者", "hq": "本社担当者", "admin": "本社管理責任者"}
INITIAL_PASSWORD = "123456"
SCHEMA = """
CREATE TABLE IF NOT EXISTS store_codes(store_id INTEGER PRIMARY KEY,code TEXT UNIQUE NOT NULL);
CREATE TABLE IF NOT EXISTS employee_sequences(store_id INTEGER PRIMARY KEY,last_number INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS employee_profiles(employee_id TEXT PRIMARY KEY,kana TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS employee_id_migration(old_id TEXT PRIMARY KEY,new_id TEXT UNIQUE NOT NULL);
"""


def store_code(db, store):
    if not db.execute("SELECT 1 FROM stores WHERE id=?", (store,)).fetchone():
        raise ValueError("店舗がありません。")
    row = db.execute("SELECT code FROM store_codes WHERE store_id=?", (store,)).fetchone()
    if row:
        return row[0]
    code = db.execute("SELECT COALESCE(MAX(CAST(code AS INTEGER)),0)+1 FROM store_codes").fetchone()[0]
    if code > 99999:
        raise ValueError("店舗コードの上限です。管理責任者へ連絡してください。")
    db.execute("INSERT INTO store_codes VALUES (?,?)", (store, f"{code:05}"))
    return f"{code:05}"


def allocate_employee(db, store):
    code = store_code(db, store)
    row = db.execute("SELECT last_number FROM employee_sequences WHERE store_id=?", (store,)).fetchone()
    number = (row[0] if row else 0) + 1
    if number > 9999:
        raise ValueError("この店舗の従業員番号が9999件に達しました。管理責任者へ連絡してください。")
    db.execute("INSERT INTO employee_sequences VALUES (?,?) ON CONFLICT(store_id) DO UPDATE SET last_number=excluded.last_number", (store, number))
    return code + f"{number:04}"


def ensure_account(db, employee, name):
    db.execute("INSERT OR IGNORE INTO users(username,name,password,role,stores,must_change) VALUES (?,?,?,'employee','[]',0)", (employee, name, password_hash(INITIAL_PASSWORD)))
    db.execute("UPDATE users SET name=? WHERE username=?", (name, employee))


def change_role(database, actor, employee, role, stores, *, console=False):
    if role not in ROLES:
        raise ValueError("権限が不正です。")
    stores = sorted(set(int(s) for s in stores))
    with database.connect(write=True) as db:
        person = db.execute("SELECT * FROM employees WHERE id=?", (employee,)).fetchone()
        if not person or not person["active"]:
            raise ValueError("利用中の従業員を選択してください。")
        current = db.execute("SELECT * FROM users WHERE username=?", (employee,)).fetchone()
        old_role = current["role"] if current else "employee"
        # Both source and destination roles constrain an operation.
        if not console:
            if old_role == "admin" or role == "admin":
                raise PermissionError("adminの昇格・降格はサーバーコマンドのみです。")
            required = ("admin",) if "hq" in (old_role, role) else ("hq", "admin")
            if actor["role"] not in required:
                raise PermissionError("この権限変更は許可されていません。")
        if old_role == "admin" and role != "admin":
            count = db.execute("SELECT count(*) FROM users u JOIN employees e ON e.id=u.username WHERE u.role='admin' AND u.active=1 AND e.active=1").fetchone()[0]
            if count <= 1:
                raise ValueError("最後の有効なadminは降格できません。")
        if role == "manager":
            if not stores or any(not db.execute("SELECT 1 FROM stores WHERE id=? AND active=1", (s,)).fetchone() for s in stores):
                raise ValueError("利用可能な担当店舗を1つ以上選択してください。")
        else:
            stores = []
        before = {"role": old_role, "stores": json.loads(current["stores"]) if current else []}
        ensure_account(db, employee, person["name"])
        user = db.execute("SELECT * FROM users WHERE username=?", (employee,)).fetchone()
        must_change = int(role != "employee" and (user["must_change"] or password_ok(INITIAL_PASSWORD, user["password"])))
        db.execute("UPDATE users SET role=?,stores=?,must_change=? WHERE id=?", (role, json.dumps(stores), must_change, user["id"]))
        db.execute("DELETE FROM sessions WHERE user_id=?", (user["id"],))
        who = f"server-console:{getpass.getuser()}" if console else actor["id"]
        database.audit(db, who, "権限変更", employee, before, {"role": role, "stores": stores})


def migrate(database, account_map=None):
    """Call offline through manage.py. Keep user PKs and historical references."""
    if database.setting("identity_version") == 2:
        return {"already_migrated": True}
    backup = database.path.with_name("attendance.before-identity-" + now().replace(":", "").replace("+", "_") + "-" + uuid.uuid4().hex[:8] + ".db")
    with database.connect() as source, closing(sqlite3.connect(backup)) as target:
        source.backup(target)
    overrides = account_map or {}
    with database.connect(write=True) as db:
        db.execute("PRAGMA defer_foreign_keys=ON")
        for store in db.execute("SELECT id FROM stores ORDER BY id").fetchall():
            store_code(db, store[0])
        employees = [dict(r) for r in db.execute("SELECT rowid,* FROM employees ORDER BY rowid")]
        mapping = {r["id"]: allocate_employee(db, r["store_id"]) for r in employees}
        # Reserve numbers for registrations still awaiting approval as well.
        for r in db.execute("SELECT employee_id,store_id FROM requests WHERE kind='register' ORDER BY created,id").fetchall():
            if r["employee_id"] not in mapping:
                mapping[r["employee_id"]] = allocate_employee(db, r["store_id"])
        aliases = {"admin": ("No1", "01", "1", "000001"), "hq": ("02", "2", "000002"), "manager": ("03", "3", "000003")}
        bindings = {}
        for u in db.execute("SELECT * FROM users").fetchall():
            old = overrides.get(u["username"])
            if not old:
                old = u["username"] if u["username"] in mapping else next((x for x in aliases.get(u["username"], ()) if x in mapping), None)
            if old not in {e["id"] for e in employees}:
                raise ValueError(f"管理アカウント {u['username']} の移行先がありません。--account-map で従業員番号を指定してください。元DBは変更していません。")
            if old in bindings.values():
                raise ValueError("同じ従業員へ複数の管理アカウントは統合できません。移行先を確認してください。")
            bindings[u["id"]] = old
        def rewrite(value):
            if isinstance(value, dict):
                return {k: mapping.get(v, v) if k == "employee_id" and isinstance(v, str) else rewrite(v) for k, v in value.items()}
            if isinstance(value, list):
                return [rewrite(v) for v in value]
            return value
        # Temporary IDs avoid collisions with IDs already in the destination format.
        tables = ("photos", "transfers", "helpers", "shifts", "requests", "postings", "recipients", "employee_profiles")
        for old, new in mapping.items():
            temp = "__migration__" + new
            db.execute("UPDATE employees SET id=? WHERE id=?", (temp, old))
            for table in tables:
                db.execute(f"UPDATE {table} SET employee_id=? WHERE employee_id=?", (temp, old))
        for old, new in mapping.items():
            temp = "__migration__" + new
            db.execute("UPDATE employees SET id=? WHERE id=?", (new, temp))
            for table in tables:
                db.execute(f"UPDATE {table} SET employee_id=? WHERE employee_id=?", (new, temp))
            db.execute("INSERT INTO employee_id_migration VALUES (?,?)", (old, new))
        for table, fields in (("requests", ("payload",)), ("request_history", ("payload",)), ("receipts", ("result",))):
            for row in db.execute(f"SELECT rowid AS migration_rowid,* FROM {table}").fetchall():
                for field in fields:
                    db.execute(f"UPDATE {table} SET {field}=? WHERE rowid=?", (json.dumps(rewrite(json.loads(row[field])), ensure_ascii=False), row["migration_rowid"]))
        for uid, old in bindings.items():
            person = next(e for e in employees if e["id"] == old)
            db.execute("UPDATE users SET username=?,name=? WHERE id=?", ("__migration_user__" + mapping[old], person["name"], uid))
        for uid, old in bindings.items():
            new = mapping[old]
            db.execute("UPDATE users SET username=?,must_change=1 WHERE id=?", (new, uid))
            # Initial responsibilities are the employee's current home store.
            from .requests import employee_store
            db.execute("UPDATE users SET stores=? WHERE id=? AND role='manager'", (json.dumps([employee_store(db, new, now()[:10])]), uid))
        for person in employees:
            ensure_account(db, mapping[person["id"]], person["name"])
        for role, choices in aliases.items():
            selected = next((old for old in choices if old in mapping and old in {e['id'] for e in employees}), None)
            if selected:
                new = mapping[selected]
                from .requests import employee_store
                stores = [employee_store(db, new, now()[:10])] if role == 'manager' else []
                db.execute("UPDATE users SET role=?,stores=?,must_change=1 WHERE username=?", (role, json.dumps(stores), new))
        if employees and not db.execute("SELECT 1 FROM users u JOIN employees e ON e.id=u.username WHERE u.role='admin' AND u.active=1 AND e.active=1").fetchone():
            raise ValueError("利用可能なadminが残らないため移行を中止しました。管理責任者の移行先と利用状態を確認してください。")
        db.execute("DELETE FROM sessions")
        db.execute("DELETE FROM tickets")
        database.set(db, "identity_version", 2)
        database.audit(db, f"server-console:{getpass.getuser()}", "従業員番号・権限統合", "identity-v2", after=mapping)
        if db.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("移行後の参照確認に失敗しました。元DBは変更していません。")
    return {"backup": str(backup), "mapping": mapping}
