import json
import sqlite3
import tempfile
import os
import subprocess
import sys
import io
from contextlib import redirect_stdout
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest.mock import patch
from datetime import datetime, timedelta
from fastapi.testclient import TestClient
from app.db import Database, password_hash, password_ok, now, JST
from app.identity import allocate_employee, change_role, migrate, ensure_account, store_code
from app.requests import submit, review
from app.plans import change_plan
from tests import test_system as legacy


class UpdateTests(unittest.TestCase):
    setUp=legacy.SystemTests.setUp
    tearDown=legacy.SystemTests.tearDown
    login_store=legacy.SystemTests.login_store
    add_shift=legacy.SystemTests.add_shift

    def test_direct_registration_and_employee_account(self):
        body={"kind":"register","store_id":1,"reason":"本社登録","payload":{"name":"新しい人","kana":"アタラシイヒト"},"images":{"normal":"a","mask":"b"}}
        r=self.hq.post('/api/request',json=body)
        self.assertEqual(r.status_code,200,r.text)
        with self.db.connect() as db:
            person=db.execute("SELECT * FROM employees WHERE name='新しい人'").fetchone()
            self.assertEqual(person['id'],'000010001')
            user=db.execute("SELECT * FROM users WHERE username=?",(person['id'],)).fetchone()
            self.assertTrue(password_ok('123456',user['password']))
            self.assertEqual(user['role'],'employee')
            self.assertEqual(db.execute('SELECT status FROM requests WHERE id=?',(r.json()['id'],)).fetchone()[0],'承認済み')

    def test_manager_registration_pending_until_review(self):
        body={"kind":"register","store_id":1,"reason":"登録","payload":{"name":"店舗申請"},"images":{"normal":"a","mask":"b"}}
        r=self.manager.post('/api/request',json=body)
        self.assertEqual(r.status_code,200,r.text)
        with self.db.connect() as db:
            self.assertIsNone(db.execute("SELECT * FROM employees WHERE name='店舗申請'").fetchone())
        self.assertEqual(self.hq.post('/api/request/'+r.json()['id']+'/review',json={"decision":"承認"}).status_code,200)

    def test_failed_direct_registration_rolls_back_counter_and_request(self):
        with self.assertRaises(ValueError):
            submit(self.db,self.reviewer,{"kind":"register","store_id":1,"reason":"登録","payload":{"name":"写真なし"}},direct=True)
        with self.db.connect(write=True) as db:
            self.assertEqual(allocate_employee(db,1),'000010001')
            self.assertEqual(db.execute("SELECT count(*) FROM requests").fetchone()[0],0)

    def test_profile_scope_and_direct_edit(self):
        body={"kind":"profile","store_id":1,"employee_id":"D001","reason":"氏名変更","payload":{"name":"変更氏名","kana":"ヨミ"}}
        r=self.manager.post('/api/request',json=body)
        self.assertEqual(r.status_code,200,r.text)
        with self.db.connect() as db:
            self.assertNotEqual(db.execute("SELECT name FROM employees WHERE id='D001'").fetchone()[0],'変更氏名')
        r=self.hq.post('/api/request',json=body)
        self.assertEqual(r.status_code,200,r.text)
        with self.db.connect() as db:
            self.assertEqual(db.execute("SELECT name FROM employees WHERE id='D001'").fetchone()[0],'変更氏名')
            self.assertEqual(db.execute("SELECT kana FROM employee_profiles WHERE employee_id='D001'").fetchone()[0],'ヨミ')
        body.update(employee_id='D002',store_id=2)
        self.assertEqual(self.manager.post('/api/request',json=body).status_code,403)

    def test_direct_correction_uses_existing_ledger(self):
        sid=self.add_shift()
        body={"kind":"correction","store_id":1,"employee_id":"D001","shift_id":sid,"reason":"本社訂正","payload":{"end":"2026-08-01T16:00:00+09:00"}}
        r=self.hq.post('/api/request',json=body)
        self.assertEqual(r.status_code,200,r.text)
        with self.db.connect() as db:
            self.assertEqual(db.execute("SELECT end FROM shifts WHERE id=?",(sid,)).fetchone()[0],'2026-08-01T16:00:00+09:00')
            self.assertGreater(db.execute("SELECT count(*) FROM postings").fetchone()[0],0)

    def test_role_hierarchy_and_session_invalidation(self):
        with self.db.connect(write=True) as db:
            ensure_account(db,'D001','テスト従業員')
            ensure_account(db,'D002','他店舗従業員')
        r=self.hq.post('/api/user',json={"employee_id":"D001","role":"manager","stores":[1,2]})
        self.assertEqual(r.status_code,200,r.text)
        self.assertEqual(self.hq.post('/api/user',json={"employee_id":"D002","role":"hq"}).status_code,403)
        self.assertEqual(self.admin.post('/api/user',json={"employee_id":"D002","role":"admin"}).status_code,403)
        self.assertEqual(self.admin.post('/api/user',json={"employee_id":"D002","role":"hq"}).status_code,200)
        user=self.client.post('/api/login',json={"mode":"manager","username":"D001","password":"123456"})
        self.assertEqual(user.status_code,200,user.text)
        self.assertEqual(self.client.get('/api/data').status_code,403)
        self.assertEqual(self.client.post('/api/password',json={"old":"123456","new":"123456"}).status_code,400)
        self.assertEqual(self.client.post('/api/password',json={"old":"123456","new":"abcdef"}).status_code,200)
        self.assertEqual(self.client.get('/api/data').status_code,200)
        self.assertEqual(self.hq.post('/api/user',json={"employee_id":"D001","role":"employee"}).status_code,200)
        self.assertEqual(self.client.get('/api/me').status_code,403)

    def test_number_limits_and_independent_store_counters(self):
        with self.db.connect(write=True) as db:
            self.assertEqual(allocate_employee(db,1),'000010001')
            self.assertEqual(allocate_employee(db,2),'000020001')
            db.execute('UPDATE employee_sequences SET last_number=9999 WHERE store_id=1')
            with self.assertRaises(ValueError):allocate_employee(db,1)
        self.assertEqual(len(password_hash('123456').split(':')),2)
        with self.assertRaises(ValueError):password_hash('12345')

    def test_portal_cookie_separation(self):
        with TestClient(self.app) as browser:
            self.login_store(browser)
            login=browser.post('/api/login',headers={'X-Portal':'hq'},json={'mode':'hq','username':'hq','password':self.pw})
            self.assertEqual(login.status_code,200,login.text)
            self.assertEqual(browser.get('/api/me').json()['role'],'terminal')
            self.assertEqual(browser.get('/api/me',headers={'X-Portal':'hq'}).json()['role'],'hq')
            self.assertEqual(browser.get('/store').status_code,200)
            self.assertEqual(browser.get('/hq').status_code,200)

    def test_future_plan_changes_and_punch_guard(self):
        day=(datetime.now(JST)+timedelta(days=2)).date().isoformat()
        rid=submit(self.db,self.author,{'kind':'help','store_id':1,'employee_id':'D001','reason':'応援','payload':{'day':day,'target_store':2}})
        review(self.db,self.reviewer,rid,'承認')
        with self.db.connect() as db:pid=db.execute('SELECT id FROM helpers').fetchone()[0]
        change_plan(self.db,self.reviewer,'help',pid,{'day':day,'target_store':3})
        self.add_shift(start=day+'T09:00:00+09:00',end=day+'T17:00:00+09:00',store=3)
        with self.assertRaises(ValueError):change_plan(self.db,self.reviewer,'help',pid,{'cancel':True})


class MigrationTests(unittest.TestCase):
    def test_cli_init_reset_and_last_admin_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(__file__).resolve().parents[1]
            env={**os.environ,'ATTENDANCE_DB':str(Path(tmp)/'attendance.db'),'PYTHONIOENCODING':'utf-8'}
            def command(*args,text=''):
                import manage
                output=io.StringIO()
                with patch.dict(os.environ,env), patch.object(sys,'argv',['manage.py',*args]), patch('getpass.getpass',return_value='abcdef'), patch('builtins.input',side_effect=text.splitlines()), redirect_stdout(output):
                    try:
                        manage.main()
                        status=0
                    except ValueError as error:
                        print(error)
                        status=1
                return SimpleNamespace(returncode=status,stdout=output.getvalue(),stderr='')
            result=command('init',text='検証店舗\n検証管理者\n')
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            database=Database(env['ATTENDANCE_DB'])
            with database.connect() as db:
                self.assertEqual(db.execute("SELECT username FROM users WHERE role='admin'").fetchone()[0],'000010001')
                self.assertTrue(password_ok('123456',db.execute("SELECT password FROM users WHERE role='admin'").fetchone()[0]))
            result=command('reset-password','--username','000010001')
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            result=command('set-role','--username','000010001','--role','employee',text='wrong\n')
            self.assertEqual(result.returncode,0)
            result=command('set-role','--username','000010001','--role','employee',text='000010001\n')
            self.assertEqual(result.returncode,1)
            self.assertIn('最後の有効なadmin',result.stdout)

    def test_migration_preserves_references_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            database=Database(Path(tmp)/'attendance.db')
            with database.connect(write=True) as db:
                db.execute("INSERT INTO stores VALUES (1,'第一店舗',1)")
                for employee,name in [('No1','管理者'),('02','本社'),('03','責任者')]:
                    db.execute('INSERT INTO employees VALUES (?,?,1,1)',(employee,name))
                for username,role in [('admin','admin'),('hq','hq'),('manager','manager')]:
                    db.execute('INSERT INTO users(username,name,password,role) VALUES (?,?,?,?)',(username,username,password_hash('abcdef'),role))
                db.execute("INSERT INTO photos VALUES ('No1','normal',X'0102',X'0304')")
                db.execute("INSERT INTO shifts(id,employee_id,store_id,start,end) VALUES ('shift','No1',1,'2026-08-01T09:00:00+09:00','2026-08-01T17:00:00+09:00')")
                db.execute("INSERT INTO receipts VALUES ('receipt','old','fingerprint',?,?)",(json.dumps({'employee_id':'No1'}),now()))
            result=migrate(database)
            self.assertEqual(result['mapping'],{'No1':'000010001','02':'000010002','03':'000010003'})
            self.assertTrue(Path(result['backup']).exists())
            with database.connect() as db:
                self.assertEqual(db.execute('SELECT employee_id FROM photos').fetchone()[0],'000010001')
                self.assertEqual(db.execute('SELECT employee_id FROM shifts').fetchone()[0],'000010001')
                self.assertEqual(json.loads(db.execute('SELECT result FROM receipts').fetchone()[0])['employee_id'],'000010001')
                self.assertEqual(db.execute("SELECT username FROM users WHERE role='admin'").fetchone()[0],'000010001')
                self.assertIsNone(db.execute('PRAGMA foreign_key_check').fetchone())
                self.assertTrue(password_ok('abcdef',db.execute("SELECT password FROM users WHERE role='admin'").fetchone()[0]))
            self.assertTrue(migrate(database)['already_migrated'])
            with self.assertRaises(ValueError):change_role(database,None,'000010001','employee',[],console=True)

    def test_unresolved_migration_does_not_modify_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            database=Database(Path(tmp)/'attendance.db')
            with database.connect(write=True) as db:
                db.execute("INSERT INTO stores VALUES (1,'第一店舗',1)")
                db.execute("INSERT INTO employees VALUES ('someone','名前',1,1)")
                db.execute("INSERT INTO users(username,name,password,role) VALUES ('admin','管理者',?,'admin')",(password_hash('abcdef'),))
            with self.assertRaises(ValueError):migrate(database)
            with database.connect() as db:self.assertEqual(db.execute('SELECT id FROM employees').fetchone()[0],'someone')


if __name__=='__main__':unittest.main()
