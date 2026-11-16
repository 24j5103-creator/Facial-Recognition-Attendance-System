import csv,io,json,tempfile,unittest,uuid,zipfile
from pathlib import Path
from datetime import timedelta
from unittest.mock import patch
from fastapi.testclient import TestClient
from app.db import Database,now,parse,password_ok,password_hash
from app.identity import enrich,change_role
from app.payroll import shift_bins,summarize,leave_quote
from app.requests import submit,review
from app.maintenance import Maintenance,export_zip
from app.web import create_app
from manage import initialize
class Fake:
    employee='001010002'
    def check(self,image):return dict(authenticated=True,employee_id=self.employee,name='テスト',box=[1,1,20,20],width=100,height=100)
    def register(self,images):return {k:(b'image',b'vector') for k in images}
class Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base=tempfile.TemporaryDirectory();cls.base_db=Database(Path(cls.base.name)/'base.db');initialize(cls.base_db,True)
    @classmethod
    def tearDownClass(cls): cls.base.cleanup()
    def setUp(self):
        self.t=tempfile.TemporaryDirectory();self.db=Database(Path(self.t.name)/'test.db')
        with self.base_db.connect() as source,self.db.connect() as target: source.backup(target)
        self.c=TestClient(create_app(self.db,Fake(),scheduled=False));self.c.__enter__();self.c.headers['X-Portal']='admin';self.login('000010001')
    def tearDown(self):self.c.__exit__(None,None,None);self.t.cleanup()
    def login(self,eid):
        r=self.c.post('/api/login',json=dict(username=eid,password='123456'));self.assertEqual(r.status_code,200,r.text)
        r=self.c.post('/api/password',json=dict(old='123456',new='Changed123'));self.assertEqual(r.status_code,200,r.text)
    def actor(self,eid):
        with self.db.connect() as db:return enrich(db,db.execute('SELECT * FROM users WHERE username=?',(eid,)).fetchone())
    def request(self,kind='profile',payload=None,**extra):
        return self.c.post('/api/request',json=dict(kind=kind,employee_id='001010002',store_id=3,payload=payload if payload is not None else {'name':'修正氏名'},reason='検証',**extra))
    def terminal(self,ids=[3],name='南大沢店',pw='example-store-pass'):
        r=self.c.post('/api/login',json=dict(mode='terminal',store_ids=ids,store_name=name,password=pw));self.assertEqual(r.status_code,200,r.text)
    def test_portals(self):self.c.headers['X-Portal']='office';self.assertEqual(self.c.get('/api/me').status_code,403)
    def test_forced_change(self):
        self.c.post('/api/login',json=dict(username='000010002',password='123456'));self.assertEqual(self.c.get('/api/data').status_code,403)
    def test_admin_console(self):self.assertEqual(self.c.post('/api/user',json=dict(employee_id='000010002',role='admin')).status_code,403)
    def test_last_admin(self):
        with self.assertRaises(ValueError):change_role(self.db,{},'000010001','admin',[],console=True,revoke=True)
    def test_hq_department(self):
        with self.assertRaises(ValueError):change_role(self.db,self.actor('000010001'),'001010002','hq',[])
    def test_regrant_password(self):
        a=self.actor('000010001');change_role(self.db,a,'001010002','store_manager',[3])
        with self.db.connect(write=True) as db:db.execute('UPDATE users SET password=?,must_change=0 WHERE username=?',(password_hash('Former123'),'001010002'))
        change_role(self.db,a,'001010002','store_manager',[3],revoke=True);change_role(self.db,a,'001010002','store_manager',[3])
        u=self.actor('001010002');self.assertTrue(password_ok('123456',u['password']));self.assertEqual(u['must_change'],1)
    def test_scope_password(self):
        with self.db.connect(write=True) as db:db.execute('UPDATE users SET password=? WHERE username=?',(password_hash('Keep123'),'001010001'))
        change_role(self.db,self.actor('000010001'),'001010001','store_manager',[3,4]);self.assertTrue(password_ok('Keep123',self.actor('001010001')['password']))
    def test_multi_roles_area(self):
        self.assertEqual(len(self.actor('000020002')['roles']),2);self.assertEqual(json.loads(self.actor('000020001')['stores']),[3,4])
    def test_manager_scope(self):
        self.c.headers['X-Portal']='store';self.terminal();self.login('001010001');d=self.c.get('/api/data').json();self.assertNotIn('001030001',[e['id'] for e in d['employees']]);self.assertEqual(len(d['all_stores']),5)
        self.assertEqual(self.c.post('/api/request',json=dict(kind='profile',employee_id='001030001',store_id=5,payload={'name':'NG'},reason='NG')).status_code,403)
    def test_multi_departments(self):self.c.headers['X-Portal']='office';self.terminal([1,2],'人事部、総務部','example-office-pass');self.assertEqual(self.c.get('/api/me').json()['scopes'],[1,2])
    def test_salted_overrides(self):
        with self.db.connect(write=True) as db:
            for i in (1,2):db.execute('UPDATE organizations SET password=? WHERE store_id=?',(password_hash('Same123'),i))
        self.c.headers['X-Portal']='office';self.terminal([1,2],'人事部、総務部','Same123')
    def test_wrong_group_password(self):
        with self.db.connect(write=True) as db:db.execute('UPDATE organizations SET password=? WHERE store_id=2',(password_hash('Other123'),))
        self.c.headers['X-Portal']='office';self.assertEqual(self.c.post('/api/login',json=dict(mode='terminal',store_ids=[1,2],store_name='人事部、総務部',password='example-office-pass')).status_code,400)
    def test_face_scope(self):self.c.headers['X-Portal']='office';self.terminal([1],'人事部','example-office-pass');self.assertFalse(self.c.post('/api/recognize',json={'image':'fake'}).json()['authenticated'])
    def test_punch(self):
        self.c.headers['X-Portal']='store';self.terminal();f=self.c.post('/api/recognize',json={'image':'fake'}).json();body=dict(image='fake',action='出勤',timestamp=now(),ticket=f['ticket'],request_id=str(uuid.uuid4()))
        r=self.c.post('/api/punch',json=body);self.assertEqual(r.status_code,200,r.text);self.assertEqual(self.c.post('/api/punch',json=body).json(),r.json())
    def test_ticket_expiry(self):
        self.c.headers['X-Portal']='store';self.terminal();f=self.c.post('/api/recognize',json={'image':'fake'}).json()
        with self.db.connect(write=True) as db:db.execute('UPDATE tickets SET created=created-6')
        self.assertEqual(self.c.post('/api/punch',json=dict(image='fake',action='出勤',timestamp=now(),ticket=f['ticket'],request_id=str(uuid.uuid4()))).status_code,400)
    def test_profile(self):
        r=self.request();self.assertEqual(r.status_code,200,r.text)
        with self.db.connect() as db:self.assertEqual(db.execute("SELECT name FROM employees WHERE id='001010002'").fetchone()[0],'修正氏名')
    def test_area_direct(self):
        rid=submit(self.db,self.actor('000020001'),dict(kind='profile',employee_id='001010002',store_id=3,payload={'name':'修正'},reason='検証'))
        with self.db.connect() as db:self.assertEqual(db.execute('SELECT status FROM requests WHERE id=?',(rid,)).fetchone()[0],'承認済み')
    def test_helper(self):
        day=(parse(now())+timedelta(days=2)).date().isoformat();rid=submit(self.db,self.actor('001010001'),dict(kind='help',employee_id='001010002',store_id=3,payload={'day':day,'target_store':4},reason='応援'))
        review(self.db,self.actor('000010001'),rid,'承認')
        with self.db.connect() as db:self.assertEqual(db.execute('SELECT store_id FROM helpers WHERE request_id=?',(rid,)).fetchone()[0],4)
    def test_return(self):
        rid=submit(self.db,self.actor('001010001'),dict(kind='profile',employee_id='001010002',store_id=3,payload={'name':'修正'},reason='検証'))
        self.assertEqual(self.c.post('/api/request/'+rid+'/review',json=dict(decision='差し戻し',reason='')).status_code,400)
        self.assertEqual(self.c.post('/api/request/'+rid+'/review',json=dict(decision='差し戻し',reason='理由')).status_code,200)
    def test_photo_self_approval(self):
        rid=submit(self.db,self.actor('000010001'),dict(kind='photo',employee_id='001010002',store_id=3,payload={},reason='更新'),{k:(b'img',b'vec') for k in ('normal','mask')})
        self.assertEqual(self.c.post('/api/request/'+rid+'/review',json=dict(decision='承認')).status_code,403)
    def test_photo_binding(self):
        b=dict(kind='photo',employee_id='001010002',store_id=3,payload={},reason='更新',images={'normal':'a','mask':'b'})
        self.assertEqual(self.c.post('/api/request',json=b).status_code,400)
        r=self.c.post('/api/photo-confirm',json={**b,'confirmed_name':'店舗 一般（架空）'});self.assertEqual(r.status_code,200,r.text)
        b['photo_confirmation']=r.json()['token'];b['images']['mask']='c';self.assertEqual(self.c.post('/api/request',json=b).status_code,400)
    def test_shared_password(self):
        r=self.request('password',{'target':'store'},new_password='NewStore123');self.assertEqual(r.status_code,200,r.text);rid=r.json()['id']
        self.assertEqual(self.c.post('/api/request/'+rid+'/review',json=dict(decision='承認')).status_code,403)
        self.assertNotIn('NewStore123',self.c.get('/api/request/'+rid).text);review(self.db,self.actor('000010002'),rid,'承認');self.assertTrue(password_ok('NewStore123',self.db.setting('store_password')))
        with self.db.connect() as db:self.assertNotIn('NewStore123',str([tuple(x) for x in db.execute('SELECT * FROM audit')]))
    def test_leave_quote(self):
        with self.db.connect() as db:
            self.assertEqual(leave_quote(db,'000010004',now()[:10],'half')['seconds'],14400);self.assertEqual(leave_quote(db,'001010002',now()[:10],'full')['seconds'],25200)
            with self.assertRaises(ValueError):leave_quote(db,'001010002',now()[:10],'half')
    def test_leave_direct(self):
        r=self.request('leave',dict(day=now()[:10],portion='full'));self.assertEqual(r.status_code,200,r.text)
        with self.db.connect() as db:
            r=next(r for r in summarize(db,now()[:7],3) if r['employee_id']=='001010002');self.assertEqual(r['有給計上時間'],25200);self.assertEqual(r['総実働時間'],28800)
    def test_area_leave_pending(self):
        rid=submit(self.db,self.actor('000020001'),dict(kind='leave',employee_id='001010002',store_id=3,payload=dict(day=now()[:10],portion='full'),reason='休暇'))
        with self.db.connect() as db:self.assertEqual(db.execute('SELECT status FROM requests WHERE id=?',(rid,)).fetchone()[0],'申請中')
    def test_no_history_alert(self):
        with self.db.connect(write=True) as db:db.execute("DELETE FROM shifts WHERE employee_id='001010002'")
        self.assertEqual(self.request('leave',dict(day=now()[:10],portion='full')).status_code,400)
        with self.db.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM alerts').fetchone()[0],1)
    def test_date(self):self.assertEqual(self.request('correction',dict(start='202626-11-16T09:00:00+09:00',end='2026-11-16T17:00:00+09:00')).status_code,400)
    def test_cancel_restore(self):
        with self.db.connect() as db:sid=db.execute("SELECT id FROM shifts WHERE employee_id='001010002' ORDER BY start DESC").fetchone()[0]
        for kind in ('cancel','restore'):
            r=self.request(kind,{},shift_id=sid);self.assertEqual(r.status_code,200,r.text)
        with self.db.connect() as db:self.assertEqual(db.execute('SELECT cancelled FROM shifts WHERE id=?',(sid,)).fetchone()[0],0)
    def test_late_correction(self):
        with self.db.connect() as db:
            s=dict(db.execute("SELECT * FROM shifts WHERE employee_id='001010002' ORDER BY start LIMIT 1").fetchone());old=next(r for r in summarize(db,s['start'][:7],3) if r['employee_id']==s['employee_id'])
        r=self.request('correction',{'end':(parse(s['end'])+timedelta(hours=1)).isoformat()},shift_id=s['id']);self.assertEqual(r.status_code,200,r.text)
        with self.db.connect() as db:
            after=next(r for r in summarize(db,s['start'][:7],3) if r['employee_id']==s['employee_id']);self.assertEqual(old['総実働時間'],after['総実働時間'])
            current=next(r for r in summarize(db,now()[:7],3) if r['employee_id']==s['employee_id']);self.assertEqual(current['過去月調整時間'],3600)
    def test_csv(self):
        z=zipfile.ZipFile(io.BytesIO(export_zip(self.db,now()[:7])));read=lambda n:next(csv.reader(io.StringIO(z.read(n).decode('utf-8-sig'))))
        self.assertEqual(len(z.namelist()),4);self.assertEqual(len(read('月次集計.csv')),18);self.assertEqual(len(read('打刻明細.csv')),10);self.assertEqual(len(read('修正履歴.csv')),10);self.assertEqual(len(read('繰越明細.csv')),16)
    def test_backup_fail_preserves(self):
        m=Maintenance(self.db);p=m.manual('csv',now()[:7],'test');old=p.read_bytes()
        with patch('app.maintenance.export_zip',return_value=b'invalid'):
            with self.assertRaises(zipfile.BadZipFile):m.manual('csv',now()[:7],'test')
        self.assertEqual(old,p.read_bytes())
    def test_history(self):
        self.request();rows=self.c.get('/api/history?employee=001010002').json()['rows'];self.assertTrue(rows);self.assertTrue(all(r['employee_id']=='001010002' for r in rows))
        self.c.headers['X-Portal']='store';self.terminal();self.login('001010001');self.assertEqual(self.c.get('/api/history').status_code,403)

    def test_register_photo_confirmed(self):
        b=dict(kind='register',employee_id='',store_id=3,payload={'name':'追加従業員','kana':'ツイカ'},reason='登録',images={'normal':'a','mask':'b'})
        r=self.c.post('/api/photo-confirm',json={**b,'confirmed_name':'追加従業員'});self.assertEqual(r.status_code,200,r.text)
        b['photo_confirmation']=r.json()['token'];r=self.c.post('/api/request',json=b);self.assertEqual(r.status_code,200,r.text)
        with self.db.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM photos WHERE employee_id='001010003'").fetchone()[0],2)
            self.assertEqual(db.execute("SELECT name FROM employees WHERE id='001010003'").fetchone()[0],'追加従業員')
    def test_pending_leave_blocks_close(self):
        submit(self.db,self.actor('001010001'),dict(kind='leave',employee_id='001010002',store_id=3,payload=dict(day=now()[:10],portion='full'),reason='休暇'))
        self.assertEqual(self.c.post('/api/monthly',json=dict(store_id=3,month=now()[:7],closed=True)).status_code,400)
    def test_password_resubmit(self):
        b=dict(kind='password',store_id=3,reason='変更',payload={'target':'store'},new_password='First123')
        r=self.c.post('/api/request',json=b);self.assertEqual(r.status_code,200,r.text);rid=r.json()['id']
        review(self.db,self.actor('000010002'),rid,'差し戻し','再入力')
        b.update(id=rid,new_password='Second123');r=self.c.post('/api/request',json=b);self.assertEqual(r.status_code,200,r.text)
        review(self.db,self.actor('000010002'),rid,'承認');self.assertTrue(password_ok('Second123',self.db.setting('store_password')))
    def test_password_override_survives_common_update(self):
        with self.db.connect(write=True) as db:db.execute('UPDATE organizations SET password=? WHERE store_id=4',(password_hash('Own123'),))
        a=TestClient(create_app(self.db,scheduled=False));a.headers['X-Portal']='store'
        a.post('/api/login',json=dict(mode='terminal',store_ids=[3],store_name='南大沢店',password='example-store-pass'))
        b=TestClient(create_app(self.db,scheduled=False));b.headers['X-Portal']='store'
        b.post('/api/login',json=dict(mode='terminal',store_ids=[4],store_name='八王子店',password='Own123'))
        r=self.request('password',{'target':'store'},new_password='NewStore123');review(self.db,self.actor('000010002'),r.json()['id'],'承認')
        self.assertEqual(a.get('/api/me').status_code,403);self.assertEqual(b.get('/api/me').status_code,200)
    def test_cancel_restore_metrics_not_doubled(self):
        with self.db.connect() as db:sid=db.execute("SELECT id FROM shifts WHERE employee_id='001010002' ORDER BY start DESC").fetchone()[0]
        r=self.request('cancel',{},shift_id=sid);self.assertEqual(r.status_code,200,r.text)
        with self.db.connect() as db:self.assertEqual(next(x for x in summarize(db,now()[:7],3) if x['employee_id']=='001010002')['総実働時間'],0)
        self.request('restore',{},shift_id=sid)
        with self.db.connect() as db:self.assertEqual(next(x for x in summarize(db,now()[:7],3) if x['employee_id']=='001010002')['総実働時間'],28800)
    def test_old_backup_rejected(self):
        import sqlite3
        m=Maintenance(self.db);p=m.snapshot(now()[:7])
        with sqlite3.connect(p) as db:db.execute("UPDATE settings SET value='2' WHERE key='identity_version'")
        with self.db.connect() as db:bid=db.execute("SELECT id FROM backups WHERE valid=1").fetchone()[0]
        with self.assertRaises(ValueError):m.restore(bid,'test')
        self.assertEqual(self.db.setting('identity_version'),3)
    def test_common_password_must_differ(self):self.assertEqual(self.request('password',{'target':'store'},new_password='example-office-pass').status_code,400)
    def test_profile_resubmit(self):
        a=self.actor('001010001');b=dict(kind='profile',employee_id='001010002',store_id=3,payload={'name':'初回'},reason='修正')
        rid=submit(self.db,a,b);review(self.db,self.actor('000010001'),rid,'差し戻し','修正してください');b.update(id=rid,payload={'name':'再入力'})
        submit(self.db,a,b);review(self.db,self.actor('000010001'),rid,'承認')
        with self.db.connect() as db:self.assertEqual(db.execute('SELECT revision FROM requests WHERE id=?',(rid,)).fetchone()[0],2)
class TimeTests(unittest.TestCase):
    def bins(self,a,b,pauses=[]):return shift_bins(dict(start=a,end=b,breaks=json.dumps(pauses),cancelled=0))
    def test_weekday(self):
        r=self.bins('2026-11-16T09:00:00+09:00','2026-11-16T19:00:00+09:00');self.assertEqual(r['通常日基本'],28800);self.assertEqual(r['通常日残業'],7200)
    def test_midnight(self):
        r=self.bins('2026-11-16T18:00:00+09:00','2026-11-17T06:00:00+09:00');self.assertEqual(r['通常日深夜残業'],10800);self.assertEqual(sum(r.values()),43200)
    def test_break(self):
        r=self.bins('2026-11-16T21:00:00+09:00','2026-11-17T07:00:00+09:00',[dict(start='2026-11-16T22:00:00+09:00',end='2026-11-17T00:00:00+09:00')]);self.assertEqual(sum(r.values()),28800);self.assertEqual(r['通常日深夜'],18000);self.assertEqual(r['通常日残業'],0)
    def test_weekend(self):
        r=self.bins('2026-11-20T21:00:00+09:00','2026-11-21T07:00:00+09:00');self.assertEqual(r['通常日深夜'],7200);self.assertEqual(r['休日残業'],7200)
    def test_holiday(self):self.assertEqual(self.bins('2026-05-06T09:00:00+09:00','2026-05-06T17:00:00+09:00')['休日基本'],28800)
    def test_separate_shifts(self):
        a=self.bins('2026-11-16T08:00:00+09:00','2026-11-16T14:00:00+09:00');b=self.bins('2026-11-16T15:00:00+09:00','2026-11-16T21:00:00+09:00');self.assertEqual(a['通常日残業']+b['通常日残業'],0)
if __name__=='__main__':unittest.main()
