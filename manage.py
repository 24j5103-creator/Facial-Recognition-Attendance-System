"""Offline setup and recovery commands. Existing databases are never reset implicitly."""
import argparse
from datetime import datetime,timedelta
import getpass
import json
import uuid
from app.db import Database,JST,now,password_hash
from app.identity import ROLES,allocate_employee,ensure_account,change_role,enrich,store_code
from app.instance_lock import instance_lock

def initialize(database,demo=False,store_password='example-store-pass',office_password='example-office-pass',admin_name='初期管理者'):
    if store_password==office_password: raise ValueError('店舗・本社部署の共通パスワードは別にしてください。')
    store_hash=password_hash(store_password);office_hash=password_hash(office_password)
    with database.connect(write=True) as db:
        if db.execute('SELECT 1 FROM stores').fetchone() or db.execute('SELECT 1 FROM users').fetchone(): raise ValueError('初期設定済みです。元データを削除する処理は実行しません。')
        organizations=[('人事部','department'),('総務部','department'),('南大沢店','store'),('八王子店','store'),('立川店','store')] if demo else [('本社','department'),('第一店舗','store')]
        orgids=[]
        for name,kind in organizations:
            oid=db.execute('INSERT INTO stores(name) VALUES (?)',(name,)).lastrowid
            db.execute('INSERT INTO organizations(store_id,kind) VALUES (?,?)',(oid,kind));store_code(db,oid);orgids.append(oid)
        aid=db.execute('INSERT INTO areas(name) VALUES (?)',('多摩エリア',)).lastrowid
        storeids=[o for o,(_,k) in zip(orgids,organizations) if k=='store']
        db.executemany('INSERT INTO area_members VALUES (?,?)',[(aid,o) for o in storeids[:2]])
        people=[(0,'本社 管理', [('admin',0)]),(0,'人事 担当',[('hq',0)]),(0,'部署 責任者',[('department_manager',orgids[0])]),(0,'部署 一般',[]),(2,'店舗 責任者',[('store_manager',orgids[2])]),(2,'店舗 一般',[]),(1,'エリア 責任者',[('area_manager',aid)]),(1,'兼任 担当',[('department_manager',orgids[1]),('store_manager',orgids[3])]),(3,'八王子 一般',[]),(4,'立川 一般',[])] if demo else [(0,admin_name,[('admin',0)])]
        accounts=[];today=datetime.now(JST).replace(hour=9,minute=0,second=0,microsecond=0)
        for idx,name,grants in people:
            oid=orgids[idx];eid=allocate_employee(db,oid);name=name+('（架空）' if demo else '')
            db.execute('INSERT INTO employees VALUES (?,?,?,1)',(eid,name,oid));ensure_account(db,eid,name)
            db.execute('INSERT INTO employee_profiles VALUES (?,?)',(eid,''))
            uid=db.execute('SELECT id FROM users WHERE username=?',(eid,)).fetchone()[0]
            db.executemany('INSERT INTO role_grants VALUES (?,?,?)',[(uid,r,scope) for r,scope in grants])
            actor=enrich(db,db.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone())
            db.execute('UPDATE users SET role=?,stores=?,must_change=?,timeout=? WHERE id=?',(actor['role'],actor['stores'],int(bool(grants)),1800 if actor['role'] in ('hq','admin') else 300,uid))
            accounts.append({'id':eid,'name':name,'roles':[r for r,_ in grants]})
            if demo:
                # Three closed months of artificial actual work for PTO average.
                from app.payroll import previous_months
                for m in previous_months(today.date()):
                    start=datetime.fromisoformat(m+'-10T09:00:00+09:00');end=start+timedelta(hours=8)
                    pauses=[{'start':(start+timedelta(hours=3)).isoformat(),'end':(start+timedelta(hours=4)).isoformat()}]
                    db.execute('INSERT INTO shifts(id,employee_id,store_id,start,end,breaks) VALUES (?,?,?,?,?,?)',(str(uuid.uuid4()),eid,oid,start.isoformat(),end.isoformat(),json.dumps(pauses)))
                start=today-timedelta(days=1);end=start+timedelta(hours=8)
                db.execute('INSERT INTO shifts(id,employee_id,store_id,start,end) VALUES (?,?,?,?,?)',(str(uuid.uuid4()),eid,oid,start.isoformat(),end.isoformat()))
        if demo:
            from app.payroll import previous_months
            for m in previous_months(today.date()):
                for oid in orgids: db.execute('INSERT INTO monthly VALUES (?,?,1,NULL,?)',(oid,m,now()))
        for k,v in {'store_password':store_hash,'department_password':office_hash,'store_password_version':1,'department_password_version':1,'identity_version':3,'schema_version':3}.items(): database.set(db,k,v)
        database.audit(db,'server-console:'+getpass.getuser(),'初期設定','demo' if demo else 'production')
    return accounts

def main():
    parser=argparse.ArgumentParser(description='顔認証勤怠管理システムの設定・復旧')
    parser.add_argument('command',choices=['init','serve','reset-password','set-role','check-models'])
    parser.add_argument('--demo',action='store_true');parser.add_argument('--host',default='127.0.0.1');parser.add_argument('--port',type=int,default=8765)
    parser.add_argument('--username');parser.add_argument('--role',choices=[r for r in ROLES if r!='employee']);parser.add_argument('--scopes','--stores',dest='scopes',default='');parser.add_argument('--revoke',action='store_true')
    args=parser.parse_args();database=Database()
    if args.command=='check-models':
        from app.recognition import Recognition
        Recognition(database).load();print('CPUモデル読み込み成功。本人認証・ライブネス精度は実機確認が必要です。');return
    with instance_lock(database.path.with_suffix('.lock')):
        if args.command=='init':
            a=getpass.getpass('店舗共通パスワード（6文字以上）: ');b=getpass.getpass('部署共通パスワード（店舗とは別、6文字以上）: ')
            print(json.dumps(initialize(database,args.demo,a,b),ensure_ascii=False,indent=2));print('個人初期パスワード123456。権限保持者は初回変更必須。顔写真は未登録です。')
        elif args.command=='reset-password':
            with database.connect(write=True) as db:
                u=db.execute('SELECT * FROM users WHERE username=?',(args.username,)).fetchone()
                if not u: raise ValueError('従業員IDがありません。')
                actor=enrich(db,u);db.execute('UPDATE users SET password=?,must_change=? WHERE id=?',(password_hash('123456'),int(bool(actor['roles'])),u['id']))
                db.execute('DELETE FROM sessions WHERE user_id=?',(u['id'],));database.audit(db,'server-console:'+getpass.getuser(),'パスワードリセット',args.username)
            print('123456にリセットしました。権限保持者は次回ログインで変更してください。')
        elif args.command=='set-role':
            if not args.username or not args.role: raise ValueError('--username と --role が必要です。')
            with database.connect() as db:
                u=db.execute('SELECT * FROM users WHERE username=?',(args.username,)).fetchone()
                if not u: raise ValueError('従業員IDがありません。')
                print('対象:',args.username,u['name'],'変更前:',enrich(db,u)['grants'],'変更:',args.role,'解除' if args.revoke else '付与/担当変更',args.scopes)
            if input('実行する対象の従業員IDを再入力: ').strip()!=args.username: print('中止しました。');return
            change_role(database,{},args.username,args.role,[int(x) for x in args.scopes.split(',') if x],console=True,revoke=args.revoke)
            print('変更を記録し、対象者のログインを無効化しました。新しい権限の付与時は123456へリセットします。')
        else:
            if database.setting('schema_version')!=3: raise ValueError('初期設定が必要です。python manage.py init --demo を実行してください。旧データは退避してから切り替えてください。')
            import uvicorn
            from app.web import create_app
            for path,label in [('store','店舗'),('office','本社部署'),('admin','管理者')]: print(f'{label}: http://{args.host}:{args.port}/{path}')
            print('停止: Ctrl+C')
            uvicorn.run(create_app(database),host=args.host,port=args.port,workers=1,access_log=False,proxy_headers=True,forwarded_allow_ips='127.0.0.1')
if __name__=='__main__':
    try: main()
    except (ValueError,RuntimeError,PermissionError) as error: print(error);raise SystemExit(1)
