import csv
from datetime import datetime, timedelta
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import threading
import uuid
import zipfile

from .db import now, parse, JST
from .attendance import monthly_report


def hms(seconds):
    value = int(seconds)
    sign = "-" if value < 0 else ""
    value = abs(value)
    return f"{sign}{value // 3600}時間{value % 3600 // 60}分{value % 60}秒"


def csv_bytes(rows, columns):
    out = io.StringIO(newline="")
    writer = csv.writer(out)
    writer.writerow(columns)
    for row in rows:
        values = []
        for key in columns:
            value = row.get(key, "")
            if isinstance(value, (list, dict)):
                value = json.dumps(value, ensure_ascii=False)
            if isinstance(value, str) and value.startswith(("=", "+", "@", "\t", "\r")):
                value = "'" + value
            values.append(value)
        writer.writerow(values)
    return out.getvalue().encode("utf-8-sig")


def export_zip(database, month, store=None):
    with database.connect() as db:
        report = monthly_report(db, month, store)
        from .payroll import summarize, METRICS
        summary=summarize(db,month,store)
        for r in summary:
            for key in METRICS[2:]: r[key]=hms(r[key])
        # Include origin shifts used by adjustments, not just current calendar month.
        shifts = []
        for row in db.execute("SELECT s.*,e.name FROM shifts s JOIN employees e ON s.employee_id=e.id ORDER BY s.start"):
            r = dict(row)
            belongs = r["start"][:7] == month or db.execute("SELECT 1 FROM postings p JOIN requests q ON p.request_id=q.id WHERE q.shift_id=? AND p.month=?", (r["id"], month)).fetchone()
            if belongs and (store is None or r["store_id"] == store):
                receipt = db.execute("SELECT result,created FROM receipts WHERE json_extract(result,'$.shift_id')=? ORDER BY created", (r["id"],)).fetchall()
                r["打刻操作履歴"] = [dict(x) for x in receipt]
                shifts.append(r)
        history = []
        for row in db.execute("SELECT h.*,r.store_id,r.employee_id,r.shift_id FROM request_history h JOIN requests r ON h.request_id=r.id ORDER BY h.id"):
            r = dict(row)
            if (r["created"][:7] == month or any(s["id"] == r["shift_id"] for s in shifts)) and (store is None or r["store_id"] == store):
                history.append(r)
        detail = report["ledger"] + [{**r, "type": "調整割当"} for r in report["allocations"]]
        for emp, debts in report["carry"].items():
            for debt in debts:
                if store is None or debt["store_id"] == store:
                    detail.append({**debt, "employee_id": emp, "month": month, "type": "翌月繰越残"})
    groups = [("月次集計.csv",summary,['employee_id','name','store_id','month']+METRICS+['確定状態']),
              ("打刻明細.csv", shifts, ["id", "employee_id", "name", "store_id", "start", "end", "breaks", "cancelled", "version", "打刻操作履歴"]),
              ("修正履歴.csv", history, ["id", "request_id", "revision", "store_id", "employee_id", "shift_id", "actor", "event", "payload", "created"]),
              ("繰越明細.csv", detail, ["type", "employee_id", "store_id", "origin_store_id", "month", "work_month", "work_day", "request_id", "source", "approved", "seconds", "gross", "days", "runs", "special", "applied_seconds"])]
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, rows, cols in groups:
            archive.writestr(name, csv_bytes(rows, cols))
    return data.getvalue()


class Maintenance:
    def __init__(self, database):
        self.db = database
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.guard = threading.RLock()

    @property
    def root(self):
        return Path(self.db.setting("backup_root")).resolve()

    def record(self, kind, path, month, valid, detail=""):
        with self.db.connect(write=True) as db:
            if valid:
                db.execute("UPDATE backups SET valid=0,detail='同対象月の最新版へ置換済み' WHERE path=? AND valid=1", (str(path),))
            db.execute("INSERT INTO backups VALUES (?,?,?,?,?,?,?)", (str(uuid.uuid4()), kind, str(path), month, now(), int(valid), detail))

    def snapshot(self, month, folder=None, label=None):
        if not self.lock.acquire(blocking=False):
            raise ValueError("保守処理中です。")
        try:
            folder = Path(folder) if folder else self.root / "sqlite" / month[:4]
            folder.mkdir(parents=True, exist_ok=True)
            dest = folder / (label or f"attendance_{month}.db")
            temp = dest.with_suffix(".tmp")
            try:
                source = sqlite3.connect(self.db.path)
                target = sqlite3.connect(temp)
                try:
                    source.backup(target)
                    if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise ValueError("バックアップ検証に失敗しました。")
                finally:
                    source.close()
                    target.close()
                os.replace(temp, dest)
                self.record("sqlite", dest, month, True)
                return dest
            except Exception:
                temp.unlink(missing_ok=True)
                self.record("sqlite", dest, month, False, "SQLite保存・整合性検証に失敗しました。")
                raise
        finally:
            self.lock.release()

    def manual(self, kind, month, actor):
        from .payroll import valid_month
        valid_month(month)
        if kind not in ('sqlite','csv'): raise ValueError('バックアップ種別が不正です。')
        try:
            if kind=='sqlite':
                path=self.snapshot(month)
            else:
                folder=self.root/'csv';folder.mkdir(parents=True,exist_ok=True)
                path=folder/f'attendance_{month}_manual.zip'
                temp=path.with_suffix('.tmp')
                try:
                    temp.write_bytes(export_zip(self.db,month))
                    with zipfile.ZipFile(temp) as archive:
                        if archive.testzip() or len(archive.namelist())!=4: raise ValueError('CSV検証に失敗しました。')
                    os.replace(temp,path)
                    self.record('csv',path,month,True)
                finally: temp.unlink(missing_ok=True)
            with self.db.connect(write=True) as db:
                self.db.audit(db,actor,'手動バックアップ成功',str(path),after={'kind':kind,'month':month})
        except Exception:
            with self.db.connect(write=True) as db:
                self.db.audit(db,actor,'手動バックアップ失敗',month,after={'kind':kind})
            raise
        return path

    def daily(self, at=None):
        at = at or datetime.now(JST)
        folder = self.root / "csv"
        folder.mkdir(parents=True, exist_ok=True)
        months = {at.strftime("%Y-%m")}
        if at.day == 1:
            months.add((at - timedelta(days=1)).strftime("%Y-%m"))
        try:
            for month in sorted(months):
                path = folder / f"attendance_{month}_{at.strftime('%Y%m%d_%H%M%S')}.zip"
                temp = path.with_suffix(".tmp")
                temp.write_bytes(export_zip(self.db, month))
                os.replace(temp, path)
                self.record("csv", path, month, True)
            with self.db.connect(write=True) as db:
                self.db.set(db, "last_daily", at.date().isoformat())
            self.cleanup(at)
        except Exception:
            self.record("csv", folder, at.strftime("%Y-%m"), False, "日次CSV保存に失敗しました。")
            raise

    def cleanup(self, at=None):
        at = at or datetime.now(JST)
        with self.db.connect() as db:
            rows = [dict(r) for r in db.execute("SELECT * FROM backups WHERE valid=1 ORDER BY created DESC")]
            annual = {r["year"] for r in db.execute("SELECT * FROM annual WHERE completed=1")}
        seen = set()
        for r in rows:
            path = Path(r["path"]).resolve()
            if path in seen or not path.is_relative_to(self.root) or not path.is_file():
                continue
            seen.add(path)
            delete = r["kind"] == "csv" and (at - parse(r["created"])).total_seconds() >= 30 * 86400
            if r["kind"] == "sqlite" and r["month"]:
                year = int(r["month"][:4])
                due = at.date() >= datetime(year + 1, 12, 31).date()
                newer = [x for x in rows if x["kind"] == "sqlite" and x["month"] and int(x["month"][:4]) > year and Path(x["path"]).is_file()]
                valid_newer = False
                for n in newer:
                    connection = None
                    try:
                        connection = sqlite3.connect(f"file:{Path(n['path']).as_posix()}?mode=ro", uri=True)
                        valid_newer = connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
                    except sqlite3.Error:
                        pass
                    finally:
                        if connection:
                            connection.close()
                    if valid_newer:
                        break
                delete = due and year in annual and valid_newer
            if delete:
                path.unlink()
                with self.db.connect(write=True) as db:
                    db.execute("UPDATE backups SET valid=0,detail='保存期限経過のため削除済み' WHERE path=?",(str(path),))
                    self.db.audit(db, "system", "バックアップ期限削除", r["id"], {"path": str(path)})

    def restore(self, backup_id, actor):
        with self.db.connect() as db:
            record = db.execute("SELECT * FROM backups WHERE id=? AND kind='sqlite' AND valid=1", (backup_id,)).fetchone()
            known_backups = [dict(r) for r in db.execute("SELECT * FROM backups")]
        original_root = str(self.root)
        if not record:
            raise ValueError("復元可能なバックアップではありません。")
        path = Path(record["path"]).resolve()
        if not path.is_relative_to(self.root) or not path.is_file():
            raise ValueError("バックアップが見つかりません。")
        src = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            if src.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("バックアップの整合性を確認できません。")
            version = src.execute("SELECT value FROM settings WHERE key='identity_version'").fetchone()
            if not version or json.loads(version[0]) != 3:
                raise ValueError("この更新版に対応しないバックアップです。旧版の環境で復元してください。")
            with self.db.connect(write=True) as db:
                self.db.set(db, "maintenance", True)
            self.snapshot(datetime.now(JST).strftime("%Y-%m"), self.root / "pre_restore", f"before_{datetime.now(JST).strftime('%Y%m%d_%H%M%S')}.db")
            target = sqlite3.connect(self.db.path)
            try:
                src.backup(target)
            finally:
                target.close()
            with self.db.connect(write=True) as db:
                db.execute("DELETE FROM sessions")
                db.execute("DELETE FROM tickets")
                self.db.set(db, "maintenance", False)
                self.db.set(db, "backup_root", original_root)
                for record in known_backups:
                    if Path(record["path"]).is_file():
                        db.execute("INSERT INTO backups VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET path=excluded.path,valid=excluded.valid,detail=excluded.detail", tuple(record[k] for k in ("id", "kind", "path", "month", "created", "valid", "detail")))
                self.db.audit(db, actor, "DB全体復元", backup_id, after={"path": str(path), "at": now()})
            # Keep a recovery log outside the restored snapshot as well.
            with (self.root / "restore_history.jsonl").open("a", encoding="utf-8") as log:
                log.write(json.dumps({"actor": actor, "backup": backup_id, "at": now()}, ensure_ascii=False) + "\n")
        finally:
            src.close()

    def scheduler(self):
        while not self.stop.is_set():
            at = datetime.now(JST)
            last = self.db.setting("last_daily", "")
            expected = (at if at.hour >= 3 else at - timedelta(days=1)).date().isoformat()
            if last < expected:
                try:
                    with self.guard:
                        self.daily(at)
                except Exception:
                    pass  # Failure is persisted and visible on the headquarters home.
            self.stop.wait(60)
