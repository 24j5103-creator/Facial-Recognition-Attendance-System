import argparse
from datetime import datetime, timedelta
import getpass
import json
from pathlib import Path
import secrets
import uuid

from app.db import Database, JST, ROOT, now, password_hash


def seed_demo(database):
    """Create only artificial business records. Never create biometric vectors."""
    with database.connect(write=True) as db:
        if db.execute("SELECT 1 FROM stores LIMIT 1").fetchone():
            raise ValueError("店舗データが存在します。デモデータは空のDBにだけ作成できます。")
        for name in ("デモ店舗A", "デモ店舗B", "デモ店舗C"):
            db.execute("INSERT INTO stores(name) VALUES (?)", (name,))
        names = [f"デモ従業員{number:02}" for number in range(1, 10)]
        day = datetime.now(JST).replace(hour=9, minute=0, second=0, microsecond=0)
        for i, name in enumerate(names, 1):
            store = (i - 1) // 3 + 1
            employee = f"D{i:03}"
            db.execute("INSERT INTO employees VALUES (?,?,?,1)", (employee, name + "（架空）", store))
            for offset in (1, 2, 3):
                start = day - timedelta(days=offset)
                end = start + timedelta(hours=8)
                pauses = [{"start": (start + timedelta(hours=3)).isoformat(), "end": (start + timedelta(hours=4)).isoformat()}]
                db.execute("INSERT INTO shifts(id,employee_id,store_id,start,end,breaks) VALUES (?,?,?,?,?,?)", (str(uuid.uuid4()), employee, store, start.isoformat(), end.isoformat(), json.dumps(pauses)))
        # One open >24h record and one pending correction, both intentionally artificial.
        start = day - timedelta(days=2)
        sid = str(uuid.uuid4())
        db.execute("INSERT INTO shifts(id,employee_id,store_id,start) VALUES (?,?,?,?)", (sid, "D001", 1, start.isoformat()))
        database.audit(db, "system", "架空デモデータ作成", "demo")


def main():
    from app.identity import migrate, change_role, allocate_employee, ensure_account, ROLES
    from app.instance_lock import instance_lock
    parser = argparse.ArgumentParser(description="顔認証勤怠管理システムの初期設定・復旧")
    parser.add_argument("command", choices=["init", "serve", "reset-password", "set-role", "migrate", "check-models"])
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--username", help="対象の9桁の従業員ID")
    parser.add_argument("--role", choices=list(ROLES))
    parser.add_argument("--stores", default="", help="担当店舗の内部ID。複数はカンマ区切り")
    parser.add_argument("--account-map", help="旧ログインIDと旧従業員番号の対応JSON")
    args = parser.parse_args()
    database = Database()
    if args.command in ("init", "migrate", "reset-password", "set-role"):
        with instance_lock(database.path.with_suffix(".lock")):
            if args.command == "migrate":
                bindings = json.loads(Path(args.account_map).read_text(encoding="utf-8-sig")) if args.account_map else None
                result = migrate(database, bindings)
                print(json.dumps(result, ensure_ascii=False, indent=2))
                print("既存パスワードは維持します。新規作成分は123456です。権限保持者は移行後の初回変更が必要です。")
            elif args.command == "init":
                with database.connect() as db:
                    if db.execute("SELECT 1 FROM users LIMIT 1").fetchone() or db.execute("SELECT 1 FROM employees LIMIT 1").fetchone():
                        raise ValueError("初期設定済みです。旧データの移行は migrate を実行してください。")
                common = password_hash(getpass.getpass("店舗共通パスワード（6文字以上）: "))
                if args.demo:
                    seed_demo(database)
                    with database.connect(write=True) as db:
                        for username, role, stores in (("admin","admin","[]"),("hq","hq","[]"),("manager","manager","[1]")):
                            db.execute("INSERT INTO users(username,name,password,role,stores,must_change) VALUES (?,?,?,?,?,1)", (username,username,password_hash("123456"),role,stores))
                        database.set(db,"store_password",common)
                    result = migrate(database,{"admin":"D001","hq":"D002","manager":"D003"})
                    print(json.dumps(result,ensure_ascii=False,indent=2))
                else:
                    store_name = input("第一店舗名: ").strip()
                    name = input("最初の管理責任者の氏名: ").strip()
                    if not store_name or not name:
                        raise ValueError("店舗名・氏名は必須です。")
                    with database.connect(write=True) as db:
                        store = db.execute("INSERT INTO stores(name) VALUES (?)",(store_name,)).lastrowid
                        employee = allocate_employee(db,store)
                        db.execute("INSERT INTO employees VALUES (?,?,?,1)",(employee,name,store))
                        ensure_account(db,employee,name)
                        db.execute("UPDATE users SET role='admin',must_change=1 WHERE username=?",(employee,))
                        database.set(db,"store_password",common)
                        database.set(db,"identity_version",2)
                    print("管理責任者の従業員ID:",employee)
                print("初期パスワード: 123456。権限保持者は初回に変更してください。顔写真は未登録です。")
                print("起動: python manage.py serve / 店舗: /store / 本社: /hq")
            elif args.command == "reset-password":
                if not args.username:
                    raise ValueError("--username に従業員IDを指定してください。")
                with database.connect(write=True) as db:
                    row = db.execute("SELECT * FROM users WHERE username=?",(args.username,)).fetchone()
                    if not row:
                        raise ValueError("従業員アカウントがありません。旧データは先にmigrateしてください。")
                    db.execute("UPDATE users SET password=?,must_change=? WHERE id=?",(password_hash("123456"),int(row["role"]!="employee"),row["id"]))
                    db.execute("DELETE FROM sessions WHERE user_id=?",(row["id"],))
                    database.audit(db,"server-console:"+getpass.getuser(),"パスワードリセット",args.username)
                print("123456にリセットしました。権限保持者は次回ログインで変更が必要です。")
            else:
                if not args.username or not args.role:
                    raise ValueError("--username と --role を指定してください。")
                with database.connect() as db:
                    person = db.execute("SELECT * FROM employees WHERE id=?",(args.username,)).fetchone()
                    user = db.execute("SELECT role FROM users WHERE username=?",(args.username,)).fetchone()
                    if not person:
                        raise ValueError("従業員がありません。")
                    print("従業員番号:",person["id"],"氏名:",person["name"])
                    print("変更前:",ROLES.get(user[0] if user else "employee"),"→ 変更後:",ROLES[args.role])
                if input("実行する場合、対象の従業員番号を再入力: ").strip()!=args.username:
                    print("中止しました。")
                    return
                change_role(database,None,args.username,args.role,[int(s) for s in args.stores.split(",") if s],console=True)
                print("権限を変更し、対象者のログイン状態を無効化しました。")
    elif args.command == "check-models":
        from app.recognition import Recognition
        Recognition(database).load()
        print("CPUモデルの読み込み成功。本人認証・ライブネス精度は実機で確認してください。")
    else:
        import uvicorn
        from app.web import create_app
        with instance_lock(database.path.with_suffix(".lock")):
            with database.connect() as db:
                has_legacy = db.execute("SELECT 1 FROM employees WHERE length(id)<>9 OR id GLOB '*[^0-9]*' LIMIT 1").fetchone()
                has_unlinked = db.execute("SELECT 1 FROM users u LEFT JOIN employees e ON e.id=u.username WHERE e.id IS NULL LIMIT 1").fetchone()
            if has_legacy or has_unlinked:
                raise ValueError("旧データを検出しました。サーバー停止中に python manage.py migrate を実行してください。")
            print(f"店舗: http://{args.host}:{args.port}/store  本社: http://{args.host}:{args.port}/hq  停止: Ctrl+C")
            uvicorn.run(create_app(database),host=args.host,port=args.port,workers=1,access_log=False,proxy_headers=True,forwarded_allow_ips="127.0.0.1")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError) as error:
        print(error)
        raise SystemExit(1)
