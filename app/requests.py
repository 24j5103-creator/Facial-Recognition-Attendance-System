import json
import uuid
from .db import now, parse, asdict
from .attendance import duration, work_day, ensure_open
from .identity import headquarters, check_scope
from .payroll import valid_day, shift_bins, leave_quote

KINDS = {"correction": "勤怠修正", "cancel": "打刻取り消し", "register": "従業員登録", "profile": "従業員情報修正", "photo": "写真更新", "stop": "利用停止", "rehire": "利用再開", "restore": "打刻取り消し解除", "leave": "有給休暇", "password": "共用パスワード変更", "transfer": "所属異動", "help": "応援勤務"}


def employee_store(db, employee, day):
    row = db.execute("SELECT store_id FROM transfers WHERE employee_id=? AND effective<=? ORDER BY effective DESC,id DESC LIMIT 1", (employee, day)).fetchone()
    return row[0] if row else db.execute("SELECT store_id FROM employees WHERE id=?", (employee,)).fetchone()[0]


def submit(database, actor, body, photo_data=None, *, direct=False):
    if actor['role'] == 'employee': raise PermissionError('管理権限が必要です。')
    if body['kind'] == 'password': return submit_password(database,actor,body)
    if body['kind'] == 'leave':
        try:
            with database.connect() as connection:
                leave_quote(connection,str(body.get('employee_id','')),body.get('payload',{}).get('day',''),body.get('payload',{}).get('portion','full'))
        except LookupError as exc:
            with database.connect(write=True) as connection:
                connection.execute('INSERT OR IGNORE INTO alerts(employee_id,day,message,created) VALUES (?,?,?,?)',(body['employee_id'],body['payload']['day'],str(exc),now()))
            raise ValueError(str(exc)) from exc
    kind = body["kind"]
    if kind not in KINDS:
        raise ValueError("申請種別が不正です。")
    if not body.get("reason", "").strip():
        raise ValueError("申請理由を入力してください。")
    store = int(body["store_id"])
    if not headquarters(actor) and store not in json.loads(actor["stores"]):
        raise PermissionError("担当店舗ではありません。")
    payload = body.get("payload", {})
    if not isinstance(payload, dict):
        raise ValueError("申請内容が不正です。")
    sid = body.get("shift_id") or None
    employee = str(body.get("employee_id", "")).strip()
    if not employee and kind != "register":
        raise ValueError("従業員番号が必要です。")
    with database.connect(write=True) as db:
        if not db.execute("SELECT 1 FROM stores WHERE id=? AND active=1", (store,)).fetchone():
            raise ValueError("利用可能な店舗を選択してください。")
        if kind == "register" and not body.get("id"):
            from .identity import allocate_employee
            employee = allocate_employee(db, store)
        person = db.execute("SELECT * FROM employees WHERE id=?", (employee,)).fetchone()
        if kind == "register":
            if person:
                raise ValueError("従業員番号は使用済みです。退職者は利用再開申請を使用してください。")
            if not payload.get("name", "").strip():
                raise ValueError("氏名を入力してください。")
        elif not person:
            raise ValueError("従業員番号が見つかりません。")
        if kind in ("help", "transfer"):
            if not headquarters(actor) and employee_store(db, employee, now()[:10]) not in json.loads(actor["stores"]):
                raise PermissionError("所属店舗の店舗責任者が申請してください。")
            if not db.execute("SELECT 1 FROM stores WHERE id=? AND active=1", (int(payload["target_store"]),)).fetchone():
                raise ValueError("異動先・応援先が利用可能ではありません。")
            from datetime import date
            valid_day(payload["day"])
            if kind == "help":
                dest=db.execute("SELECT kind FROM organizations WHERE store_id=?",(int(payload["target_store"]),)).fetchone()
                if dest[0] != "store" or int(payload["target_store"]) == employee_store(db,employee,payload["day"]): raise ValueError("応援先には所属先以外の店舗を選択してください。")
        if sid:
            shift = db.execute("SELECT * FROM shifts WHERE id=?", (sid,)).fetchone()
            if not shift or shift["store_id"] != store or shift["employee_id"] != employee:
                raise ValueError("勤務と従業員・店舗が一致しません。")
        if kind == "correction":
            allowed = {"start", "end", "breaks"}
            if not payload or set(payload) - allowed:
                raise ValueError("修正できる項目は出勤・退勤・休憩です。")
            if not sid and not {"start", "end"} <= set(payload):
                raise ValueError("勤務追加には出勤・退勤日時が必要です。")
        if kind == "cancel" and not sid:
            raise ValueError("取り消す勤務を選択してください。")
        if kind == "profile":
            if set(payload) - {"name", "kana"} or not payload.get("name", "").strip():
                raise ValueError("氏名・読み仮名のみ変更できます。氏名は必須です。")
        if kind in ("profile", "photo", "stop", "rehire") and not headquarters(actor) and employee_store(db, employee, now()[:10]) not in json.loads(actor["stores"]):
            raise PermissionError("所属店舗の店舗責任者が申請してください。")
        if kind == 'leave':
            if store != employee_store(db,employee,payload['day']): raise ValueError('有給の所属と対象日の配属先が一致しません。')
            check_scope(actor,employee_store(db,employee,payload['day']))
            payload = {k:payload[k] for k in ('day','portion')}
        if kind == 'correction':
            for key in ('start','end'):
                if payload.get(key): parse(payload[key])
            if 'breaks' in payload:
                if not isinstance(payload['breaks'],list): raise ValueError('休憩の入力が不正です。')
                for pause in payload['breaks']: parse(pause['start']);parse(pause['end'])
        if kind in ('cancel','restore') and not sid: raise ValueError('勤務を選択してください。')
        direct = can_direct(db,actor,kind,payload,employee,store)
        previous = None
        rid = body.get("id") or str(uuid.uuid4())
        existing = db.execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()
        if existing:
            if existing["author"] != actor["id"] or existing["status"] not in ("申請中", "差し戻し"):
                raise PermissionError("この申請は編集できません。")
            if (existing["kind"], existing["store_id"], existing["employee_id"], existing["shift_id"]) != (kind, store, employee, sid):
                raise ValueError("再申請では種別・対象を変更できません。")
            previous = dict(existing)
            db.execute("UPDATE requests SET payload=?,reason=?,status='申請中',revision=revision+1,updated=?,reviewer=NULL,reviewed=NULL,review_reason=NULL,seen=0 WHERE id=?", (json.dumps(payload, ensure_ascii=False), body["reason"], now(), rid))
        else:
            db.execute("INSERT INTO requests(id,kind,store_id,employee_id,shift_id,author,payload,reason,status,created,updated) VALUES (?,?,?,?,?,?,?,?,?,?,?)", (rid, kind, store, employee, sid, actor["id"], json.dumps(payload, ensure_ascii=False), body["reason"], "申請中", now(), now()))
        if kind in ("register", "photo", "rehire"):
            if photo_data is None and not existing:
                raise ValueError("正面マスクなし・ありの2枚が必要です。")
            if photo_data:
                db.execute("DELETE FROM request_photos WHERE request_id=?", (rid,))
                for label, (image, vector) in photo_data.items():
                    db.execute("INSERT INTO request_photos VALUES (?,?,?,?)", (rid, label, image, vector))
        current = dict(db.execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone())
        db.execute("INSERT INTO request_history(request_id,revision,actor,event,payload,created) VALUES (?,?,?,?,?,?)", (rid, current["revision"], actor["id"], "再申請" if existing else "申請", json.dumps(current, ensure_ascii=False), now()))
        database.audit(db, actor["id"], "申請", rid, previous, current)
        if direct:
            review(database, actor, rid, "承認", "本社による直接確定", _db=db, _direct=True)
    return rid


def approve_correction(database, db, request, payload, approved):
    sid = request["shift_id"]
    before = asdict(db.execute("SELECT * FROM shifts WHERE id=?", (sid,)).fetchone()) if sid else None
    original = before.copy() if before else None
    new = before.copy() if before else {"id": str(uuid.uuid4()), "employee_id": request["employee_id"], "store_id": request["store_id"], "start": payload["start"], "end": None, "breaks": "[]", "cancelled": 0, "version": 0}
    for field in ("start", "end", "breaks"):
        if field in payload:
            value = payload[field]
            new[field] = json.dumps(value) if field == "breaks" else (parse(value).isoformat(timespec="seconds") if value else None)
    if request["kind"] == "restore":
        if not before or not before["cancelled"]: raise ValueError("取り消し済みの勤務ではありません。")
        new["cancelled"] = 0
    if request["kind"] == "cancel":
        new["cancelled"] = 1
    if not new["cancelled"] and not new["end"]:
        raise ValueError("修正申請には退勤日時を指定してください。")
    new_gross, new_seconds = duration(new)
    if not new["cancelled"] and new_gross > 86400:
        raise ValueError("修正後も総勤務時間が24時間を超えています。")
    month = parse(approved).strftime("%Y-%m")
    ensure_open(db, request["store_id"], month)
    # Freeze the originally counted version before modifying the raw punch.
    has_posting = sid and db.execute("SELECT 1 FROM postings p JOIN requests r ON p.request_id=r.id WHERE r.shift_id=?", (sid,)).fetchone()
    before_counted = before and not before["cancelled"] and before["end"] and duration(before)[0] <= 86400
    if before_counted and not has_posting:
        old_gross, old_seconds = duration(before)
        db.execute("INSERT INTO postings(request_id,employee_id,store_id,work_month,work_day,approved,month,seconds,gross,days,runs,special) VALUES (?,?,?,?,?,?,?,?,?,?,?,-1)", (request["id"], before["employee_id"], before["store_id"], work_day(before)[:7], work_day(before), approved, work_day(before)[:7], old_seconds, old_gross, 1, 1))
        db.execute("UPDATE postings SET work_start=? WHERE request_id=? AND special=-1", (before["start"], request["id"]))
    # Current record is the last approved value. Compare against it, not request-time snapshot.
    old_gross, old_seconds = duration(before) if before_counted else (0, 0)
    day = work_day(new)
    old_day = work_day(before) if before_counted else day
    others = [dict(r) for r in db.execute("SELECT * FROM shifts WHERE employee_id=? AND cancelled=0 AND id<>?", (new["employee_id"], new["id"])) ]
    if not new["cancelled"] and any(parse(r["start"]) < parse(new["end"]) and (not r["end"] or parse(new["start"]) < parse(r["end"])) for r in others):
        raise ValueError("同じ従業員の別勤務と時刻が重複しています。")
    old_day_delta = -int(bool(before_counted) and not any(work_day(r) == old_day for r in others))
    new_day_delta = int(not new["cancelled"] and not any(work_day(r) == day for r in others))
    old_runs, new_runs = int(bool(before_counted)), int(not new["cancelled"])
    values = (new["employee_id"], new["store_id"], new["start"], new["end"], new["breaks"], new["cancelled"], new["version"] + 1, new["id"])
    if before:
        db.execute("UPDATE shifts SET employee_id=?,store_id=?,start=?,end=?,breaks=?,cancelled=?,version=? WHERE id=?", values)
    else:
        db.execute("INSERT INTO shifts(employee_id,store_id,start,end,breaks,cancelled,version,id) VALUES (?,?,?,?,?,?,?,?)", values)
        db.execute("UPDATE requests SET shift_id=? WHERE id=?", (new["id"], request["id"]))
    if old_day == day:
        entries = [(day, new_seconds - old_seconds, new_gross - old_gross, old_day_delta + new_day_delta, new_runs - old_runs)]
    else:
        entries = [(old_day, -old_seconds, -old_gross, old_day_delta, -old_runs), (day, new_seconds, new_gross, new_day_delta, new_runs)]
    for d, secs, gross, days, runs in entries:
        db.execute("INSERT INTO postings(request_id,employee_id,store_id,work_month,work_day,approved,month,seconds,gross,days,runs,special) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (request["id"], new["employee_id"], new["store_id"], d[:7], d, approved, month, secs, gross, days, runs, int(d[:7] != month)))
        db.execute("UPDATE postings SET work_start=? WHERE id=last_insert_rowid()", (new["start"] if d == day else before["start"],))
    # Freeze original bins, then record only current-month bin differences. Late
    # changes are represented separately by the existing signed adjustment ledger.
    old_bins=shift_bins(original); new_bins=shift_bins(new)
    if original:
        db.execute('INSERT OR IGNORE INTO metric_baselines VALUES (?,?,?,?,?)',(new['id'],new['employee_id'],new['store_id'],original['start'][:7],json.dumps(old_bins)))
    changes={}
    if original and original['start'][:7]==month:
        for k,v in old_bins.items(): changes[k]=changes.get(k,0)-v
    if new['start'][:7]==month:
        for k,v in new_bins.items(): changes[k]=changes.get(k,0)+v
    db.execute('INSERT INTO metric_postings(request_id,employee_id,store_id,month,metrics) VALUES (?,?,?,?,?)',(request['id'],new['employee_id'],new['store_id'],month,json.dumps(changes)))
    if not original:
        db.execute('INSERT INTO metric_baselines VALUES (?,?,?,?,?)',(new['id'],new['employee_id'],new['store_id'],new['start'][:7],json.dumps(dict.fromkeys(old_bins,0))))
    database.audit(db, request["reviewer"], "勤怠修正反映", new["id"], original, new)


def review(database, actor, rid, decision, reason="", *, _db=None, _direct=False):
    if not _direct and not headquarters(actor):
        raise PermissionError("本社権限が必要です。")
    from contextlib import nullcontext
    with (nullcontext(_db) if _db is not None else database.connect(write=True)) as db:
        r = asdict(db.execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone())
        if not r or r["status"] != "申請中":
            raise ValueError("承認待ちの申請ではありません。")
        if _direct and (not can_direct(db,actor,r["kind"],json.loads(r["payload"]),r["employee_id"],r["store_id"]) or r["author"] != actor["id"]):
            raise PermissionError("直接確定の対象ではありません。")
        if r["author"] == actor["id"] and not _direct:
            raise PermissionError("自分の申請は承認・差し戻しできません。")
        if decision not in ("承認", "差し戻し"):
            raise ValueError("操作が不正です。")
        if decision == "差し戻し" and not reason.strip():
            raise ValueError("差し戻し理由を入力してください。")
        stamp = now()
        payload = json.loads(r["payload"])
        r["reviewer"] = actor["id"]
        if decision == "承認":
            if r["kind"] in ("correction", "cancel", "restore"):
                approve_correction(database, db, r, payload, stamp)
            elif r['kind'] == 'password':
                apply_password(database,db,r,payload)
            elif r['kind'] == 'leave':
                quote=leave_quote(db,r['employee_id'],payload['day'],payload['portion'])
                ensure_open(db,r['store_id'],payload['day'][:7])
                used=db.execute('SELECT COALESCE(sum(days),0) FROM leave_entries WHERE employee_id=? AND day=?',(r['employee_id'],payload['day'])).fetchone()[0]
                if used+quote['days']>1: raise ValueError('同日の有給が1日を超えます。')
                if quote['days']==1 and db.execute('SELECT 1 FROM shifts WHERE employee_id=? AND cancelled=0 AND substr(start,1,10)=?',(r['employee_id'],payload['day'])).fetchone(): raise ValueError('全休と勤務が重複しています。')
                db.execute('INSERT INTO leave_entries(request_id,employee_id,store_id,day,days,seconds,basis) VALUES (?,?,?,?,?,?,?)',(rid,r['employee_id'],r['store_id'],payload['day'],quote['days'],quote['seconds'],json.dumps(quote['basis'],ensure_ascii=False)))
            elif r["kind"] in ("register", "photo", "rehire"):
                pics = db.execute("SELECT * FROM request_photos WHERE request_id=?", (rid,)).fetchall()
                if {p["kind"] for p in pics} != {"normal", "mask"}:
                    raise ValueError("正面マスクなし・ありの2枚が必要です。")
                if r["kind"] == "register":
                    db.execute("INSERT INTO employees VALUES (?,?,?,1)", (r["employee_id"], payload["name"], r["store_id"]))
                    from .identity import ensure_account
                    ensure_account(db, r["employee_id"], payload["name"])
                    db.execute("INSERT INTO employee_profiles VALUES (?,?)", (r["employee_id"], payload.get("kana", "")))
                if r["kind"] == "rehire":
                    db.execute("UPDATE employees SET active=1,store_id=? WHERE id=?", (r["store_id"], r["employee_id"]))
                db.execute("DELETE FROM photos WHERE employee_id=?", (r["employee_id"],))
                for p in pics:
                    db.execute("INSERT INTO photos VALUES (?,?,?,?)", (r["employee_id"], p["kind"], p["image"], p["vector"]))
                db.execute("DELETE FROM request_photos WHERE request_id=?", (rid,))
            elif r["kind"] == "stop":
                if db.execute("SELECT 1 FROM users WHERE username=? AND role='admin' AND active=1", (r["employee_id"],)).fetchone():
                    raise ValueError("adminの従業員停止前に、サーバーコマンドで権限を変更してください。")
                if db.execute("SELECT 1 FROM shifts WHERE employee_id=? AND end IS NULL AND cancelled=0", (r["employee_id"],)).fetchone():
                    raise ValueError("勤務中です。先に退勤・修正してください。")
                db.execute("UPDATE employees SET active=0 WHERE id=?", (r["employee_id"],))
                db.execute("DELETE FROM sessions WHERE user_id IN (SELECT id FROM users WHERE username=?)", (r["employee_id"],))
                db.execute("DELETE FROM photos WHERE employee_id=?", (r["employee_id"],))
                # Do not retain prior images inside other applications.
                db.execute("DELETE FROM request_photos WHERE request_id IN (SELECT id FROM requests WHERE employee_id=?)", (r["employee_id"],))
            elif r["kind"] == "transfer":
                db.execute("INSERT INTO transfers(employee_id,store_id,effective,request_id) VALUES (?,?,?,?)", (r["employee_id"], int(payload["target_store"]), payload["day"], rid))
            elif r["kind"] == "help":
                db.execute("INSERT INTO helpers(employee_id,store_id,day,request_id) VALUES (?,?,?,?)", (r["employee_id"], int(payload["target_store"]), payload["day"], rid))
            elif r["kind"] == "profile":
                old = dict(db.execute("SELECT * FROM employees WHERE id=?", (r["employee_id"],)).fetchone())
                old_profile = db.execute("SELECT kana FROM employee_profiles WHERE employee_id=?", (r["employee_id"],)).fetchone()
                old["kana"] = old_profile[0] if old_profile else ""
                db.execute("UPDATE employees SET name=? WHERE id=?", (payload["name"].strip(), r["employee_id"]))
                db.execute("UPDATE users SET name=? WHERE username=?", (payload["name"].strip(), r["employee_id"]))
                db.execute("INSERT INTO employee_profiles VALUES (?,?) ON CONFLICT(employee_id) DO UPDATE SET kana=excluded.kana", (r["employee_id"], payload.get("kana", "")))
                database.audit(db, actor["id"], "従業員情報修正", r["employee_id"], old, payload)
        status = "承認済み" if decision == "承認" else "差し戻し"
        db.execute("UPDATE requests SET status=?,reviewer=?,reviewed=?,review_reason=?,seen=0,updated=? WHERE id=?", (status, actor["id"], stamp, reason, stamp, rid))
        event = "直接確定" if _direct else decision
        db.execute("INSERT INTO request_history(request_id,revision,actor,event,payload,created) VALUES (?,?,?,?,?,?)", (rid, r["revision"], actor["id"], event, json.dumps({"payload": payload, "reason": reason}, ensure_ascii=False), stamp))
        database.audit(db, actor["id"], event, rid, r, {"status": status, "reason": reason, "payload":payload})


def can_direct(db,actor,kind,payload,employee,store):
    roles=set(actor.get('roles',[actor['role']]))
    high=bool(roles & {'hq','admin'})
    area=any(g['role']=='area_manager' and db.execute('SELECT 1 FROM area_members WHERE area_id=? AND store_id=?',(g['scope_id'],store)).fetchone() for g in actor.get('grants',[]))
    if kind=='leave': return high
    if not (high or area): return False
    if kind in ('register','profile','correction','cancel','restore'): return True
    if kind in ('help','transfer'):
        day=payload.get('day','');valid_day(day)
        if (day<=now()[:10] if kind=='transfer' else day<now()[:10]): return False
        if db.execute('SELECT 1 FROM shifts WHERE employee_id=? AND cancelled=0 AND substr(start,1,10)=?',(employee,day)).fetchone(): return False
        return True
    return False

def password_version(database,db,store):
    org=db.execute('SELECT * FROM organizations WHERE store_id=?',(store,)).fetchone()
    return f"{org['version']}:" + ('override' if org['password'] else str(database.setting(org['kind']+'_password_version',1)))

def submit_password(database,actor,body):
    if not headquarters(actor): raise PermissionError('本社権限が必要です。')
    from .db import password_hash
    payload=body.get('payload',{})
    target=payload.get('target')
    if target not in ('store','department','individual'): raise ValueError('変更対象が不正です。')
    if not body.get('reason','').strip(): raise ValueError('変更理由が必要です。')
    value=body.pop('new_password','')
    from .db import password_ok
    if target in ('store','department') and password_ok(value,database.setting(('department' if target=='store' else 'store')+'_password','')): raise ValueError('店舗と本社部署の共通パスワードは別にしてください。')
    hashed=password_hash(value) if not payload.get('use_common') else ''
    rid=body.get('id') or str(uuid.uuid4())
    with database.connect(write=True) as db:
        orgid=int(body['store_id'])
        org=db.execute('SELECT * FROM organizations WHERE store_id=?',(orgid,)).fetchone()
        if not org: raise ValueError('所属がありません。')
        clean={'target':target,'use_common':bool(payload.get('use_common')),'version':password_version(database,db,orgid) if target=='individual' else database.setting(target+'_password_version',1)}
        if target!='individual': clean['other_version']=database.setting(('department' if target=='store' else 'store')+'_password_version',1)
        if target!='individual' and clean['use_common']: raise ValueError('共通パスワードを解除できません。')
        existing=db.execute('SELECT * FROM requests WHERE id=?',(rid,)).fetchone()
        if existing:
            if existing['author']!=actor['id'] or existing['status'] not in ('申請中','差し戻し') or existing['kind']!='password' or existing['store_id']!=orgid or json.loads(existing['payload'])['target']!=target: raise PermissionError('この申請は編集できません。')
            db.execute("UPDATE requests SET payload=?,reason=?,status='申請中',revision=revision+1,reviewer=NULL,reviewed=NULL,review_reason=NULL,updated=? WHERE id=?",(json.dumps(clean),body['reason'],now(),rid))
        else:
            db.execute("INSERT INTO requests(id,kind,store_id,employee_id,author,payload,reason,status,created,updated) VALUES (?,'password',?,NULL,?,?,?,'申請中',?,?)",(rid,orgid,actor['id'],json.dumps(clean),body['reason'],now(),now()))
        db.execute('INSERT INTO request_secrets VALUES (?,?) ON CONFLICT(request_id) DO UPDATE SET password=excluded.password',(rid,hashed))
        revision=(existing['revision']+1) if existing else 1
        db.execute('INSERT INTO request_history(request_id,revision,actor,event,payload,created) VALUES (?,?,?,?,?,?)',(rid,revision,actor['id'],'再申請' if existing else '申請',json.dumps(clean),now()))
        database.audit(db,actor['id'],'共用パスワード変更申請',rid,after=clean)
    return rid

def apply_password(database,db,r,payload):
    from .db import password_ok
    secret=db.execute('SELECT password FROM request_secrets WHERE request_id=?',(r['id'],)).fetchone()
    if not secret: raise ValueError('パスワード変更の申請を作り直してください。')
    target=payload['target'];org=r['store_id']
    current=password_version(database,db,org) if target=='individual' else database.setting(target+'_password_version',1)
    if current!=payload['version']: raise ValueError('申請後に設定が変更されました。新しく申請してください。')
    if target!='individual' and database.setting(('department' if target=='store' else 'store')+'_password_version',1)!=payload['other_version']: raise ValueError('他方の共通パスワードが変更されました。再申請してください。')
    if target=='individual':
        db.execute('UPDATE organizations SET password=?,version=version+1 WHERE store_id=?',(secret[0] or None,org))
        db.execute('DELETE FROM sessions WHERE id IN (SELECT session_id FROM session_scopes WHERE store_id=?)',(org,))
    else:
        database.set(db,target+'_password',secret[0]);database.set(db,target+'_password_version',current+1)
        db.execute('DELETE FROM sessions WHERE id IN (SELECT ss.session_id FROM session_scopes ss JOIN organizations o ON ss.store_id=o.store_id WHERE o.kind=? AND o.password IS NULL)',(target,))
    db.execute('DELETE FROM request_secrets WHERE request_id=?',(r['id'],))
