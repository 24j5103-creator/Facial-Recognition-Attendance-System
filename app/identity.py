"""Employee identity and independent, scoped role grants."""
import getpass
import json
from .db import now, password_hash
ROLES = {'employee':'一般従業員','store_manager':'店舗責任者','department_manager':'部署責任者','area_manager':'エリア責任者','hq':'本社担当者','admin':'本社管理責任者'}
INITIAL_PASSWORD = '123456'
SCHEMA = '''
CREATE TABLE IF NOT EXISTS store_codes(store_id INTEGER PRIMARY KEY,code TEXT UNIQUE NOT NULL);
CREATE TABLE IF NOT EXISTS employee_sequences(store_id INTEGER PRIMARY KEY,last_number INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS employee_profiles(employee_id TEXT PRIMARY KEY,kana TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS organizations(store_id INTEGER PRIMARY KEY REFERENCES stores(id),kind TEXT NOT NULL CHECK(kind IN ('store','department')),password TEXT,version INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS areas(id INTEGER PRIMARY KEY,name TEXT UNIQUE NOT NULL);
CREATE TABLE IF NOT EXISTS area_members(area_id INTEGER REFERENCES areas(id),store_id INTEGER REFERENCES stores(id),PRIMARY KEY(area_id,store_id));
CREATE TABLE IF NOT EXISTS role_grants(user_id INTEGER REFERENCES users(id),role TEXT NOT NULL,scope_id INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(user_id,role,scope_id));
CREATE TABLE IF NOT EXISTS session_scopes(session_id TEXT REFERENCES sessions(id) ON DELETE CASCADE,store_id INTEGER REFERENCES stores(id),version TEXT NOT NULL,PRIMARY KEY(session_id,store_id));
CREATE TABLE IF NOT EXISTS session_portals(session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,portal TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS request_secrets(request_id TEXT PRIMARY KEY,password TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS photo_confirmations(id TEXT PRIMARY KEY,user_id INTEGER NOT NULL,digest TEXT NOT NULL,created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS leave_entries(id INTEGER PRIMARY KEY,request_id TEXT UNIQUE NOT NULL,employee_id TEXT NOT NULL,store_id INTEGER NOT NULL,day TEXT NOT NULL,days REAL NOT NULL,seconds INTEGER NOT NULL,basis TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS metric_baselines(shift_id TEXT PRIMARY KEY,employee_id TEXT,store_id INTEGER,month TEXT,metrics TEXT);
CREATE TABLE IF NOT EXISTS metric_postings(id INTEGER PRIMARY KEY,request_id TEXT,employee_id TEXT,store_id INTEGER,month TEXT,metrics TEXT);
CREATE TABLE IF NOT EXISTS alerts(id INTEGER PRIMARY KEY,employee_id TEXT NOT NULL,day TEXT NOT NULL,message TEXT NOT NULL,created TEXT NOT NULL,UNIQUE(employee_id,day,message));
'''
def store_code(db, store):
    row = db.execute('SELECT code FROM store_codes WHERE store_id=?',(store,)).fetchone()
    if row: return row[0]
    org = db.execute('SELECT kind FROM organizations WHERE store_id=?',(store,)).fetchone()
    if not org: raise ValueError('所属がありません。')
    low, high = (1,100) if org[0] == 'department' else (101,99999)
    last = db.execute('SELECT MAX(CAST(code AS INTEGER)) FROM store_codes WHERE CAST(code AS INTEGER) BETWEEN ? AND ?',(low,high)).fetchone()[0]
    code = max(low,(last or low-1)+1)
    if code > high: raise ValueError('所属コードの上限です。')
    db.execute('INSERT INTO store_codes VALUES (?,?)',(store,f'{code:05}'))
    return f'{code:05}'
def allocate_employee(db, store):
    code = store_code(db,store)
    row = db.execute('SELECT last_number FROM employee_sequences WHERE store_id=?',(store,)).fetchone()
    number = (row[0] if row else 0)+1
    if number > 9999: raise ValueError('所属内の従業員番号が上限です。')
    db.execute('INSERT INTO employee_sequences VALUES (?,?) ON CONFLICT(store_id) DO UPDATE SET last_number=excluded.last_number',(store,number))
    return code+f'{number:04}'
def ensure_account(db, employee, name):
    if not db.execute('SELECT 1 FROM users WHERE username=?',(employee,)).fetchone():
        db.execute("INSERT INTO users(username,name,password,role) VALUES (?,?,?,'employee')",(employee,name,password_hash(INITIAL_PASSWORD)))
    db.execute('UPDATE users SET name=? WHERE username=?',(name,employee))
def enrich(db, user):
    user = dict(user)
    grants = [dict(r) for r in db.execute('SELECT role,scope_id FROM role_grants WHERE user_id=? ORDER BY role,scope_id',(user['id'],))]
    roles = {g['role'] for g in grants}
    allowed = set()
    if roles & {'hq','admin'}: allowed = {r[0] for r in db.execute('SELECT id FROM stores WHERE active=1')}
    else:
        for g in grants:
            if g['role'] == 'area_manager': allowed.update(r[0] for r in db.execute('SELECT store_id FROM area_members WHERE area_id=?',(g['scope_id'],)))
            else: allowed.add(g['scope_id'])
    primary = next((r for r in ('admin','hq','area_manager','department_manager','store_manager') if r in roles),'employee')
    user.update(role=primary,roles=sorted(roles),grants=grants,stores=json.dumps(sorted(allowed)))
    return user

def headquarters(actor): return bool(set(actor.get('roles',[actor['role']])) & {'hq','admin'})
def check_scope(actor, store):
    if not headquarters(actor) and int(store) not in json.loads(actor['stores']): raise PermissionError('担当範囲ではありません。')

def change_role(database, actor, employee, role, stores, *, console=False, revoke=False):
    if role not in ROLES or role == 'employee': raise ValueError('付与・解除するロールを選択してください。')
    scopes = sorted(set(int(x) for x in stores)) if role.endswith('_manager') else [0]
    if not scopes: raise ValueError('担当範囲を選択してください。')
    with database.connect(write=True) as db:
        person = db.execute('SELECT * FROM employees WHERE id=? AND active=1',(employee,)).fetchone()
        if not person: raise ValueError('利用中の従業員を選択してください。')
        roles = set(actor.get('roles',[actor.get('role')]))
        if not console and (role == 'admin' or ('admin' not in roles if role == 'hq' else not roles & {'hq','admin'})):
            raise PermissionError('この権限変更は許可されていません。admin変更はサーバーコマンド専用です。')
        if not revoke and role in ('hq','admin'):
            from .requests import employee_store
            home = employee_store(db,employee,now()[:10])
            if db.execute('SELECT kind FROM organizations WHERE store_id=?',(home,)).fetchone()[0] != 'department': raise ValueError('hq/adminは部署所属の従業員へ付与してください。')
        for scope in scopes:
            if role == 'area_manager': found = db.execute('SELECT 1 FROM areas WHERE id=?',(scope,)).fetchone()
            elif role.endswith('_manager'): found = db.execute('SELECT 1 FROM organizations o JOIN stores s ON s.id=o.store_id WHERE s.id=? AND s.active=1 AND o.kind=?',(scope,'store' if role == 'store_manager' else 'department')).fetchone()
            else: found = True
            if not found: raise ValueError('担当範囲の種類がロールと一致しません。')
        ensure_account(db,employee,person['name'])
        user = enrich(db,db.execute('SELECT * FROM users WHERE username=?',(employee,)).fetchone())
        if revoke and role == 'admin' and 'admin' in user['roles']:
            count = db.execute("SELECT count(DISTINCT u.id) FROM users u JOIN role_grants g ON g.user_id=u.id JOIN employees e ON e.id=u.username WHERE g.role='admin' AND u.active=1 AND e.active=1").fetchone()[0]
            if count <= 1: raise ValueError('最後の有効なadminは降格できません。')
        old = [g for g in user['grants'] if g['role'] == role]
        db.execute('DELETE FROM role_grants WHERE user_id=? AND role=?',(user['id'],role))
        if not revoke:
            db.executemany('INSERT INTO role_grants VALUES (?,?,?)',[(user['id'],role,x) for x in scopes])
            if not old: db.execute('UPDATE users SET password=?,must_change=1 WHERE id=?',(password_hash(INITIAL_PASSWORD),user['id']))
        updated = enrich(db,db.execute('SELECT * FROM users WHERE id=?',(user['id'],)).fetchone())
        db.execute('UPDATE users SET role=?,stores=? WHERE id=?',(updated['role'],updated['stores'],user['id']))
        db.execute('DELETE FROM sessions WHERE user_id=?',(user['id'],))
        who = f'server-console:{getpass.getuser()}' if console else actor['id']
        database.audit(db,who,'権限変更',employee,user['grants'],updated['grants'])
