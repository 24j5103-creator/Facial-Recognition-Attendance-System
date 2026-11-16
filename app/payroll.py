"""Exclusive time bins and paid-leave quotations; salary calculation is external."""
from datetime import datetime, date, timedelta
import json
import re
from jpholiday import JPHoliday
from .db import parse, JST, now
from .attendance import duration, monthly_report
HOLIDAYS = JPHoliday()
BINS = [p+k for p in ('通常日','休日') for k in ('基本','深夜','残業','深夜残業')]
METRICS = ['出勤日数','有給取得日数','総実働時間']+BINS+['有給計上時間','過去月調整時間']

def valid_day(value):
    if not isinstance(value,str) or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}',value): raise ValueError('日付は西暦4桁・月2桁・日2桁で指定してください。')
    return date.fromisoformat(value)
def valid_month(value):
    if not isinstance(value,str) or not re.fullmatch(r'[0-9]{4}-[0-9]{2}',value): raise ValueError('対象月は西暦4桁・月2桁です。')
    return date.fromisoformat(value+'-01')
def holiday(day): return day.weekday() >= 5 or HOLIDAYS.is_holiday(day)

def shift_bins(shift):
    result = dict.fromkeys(BINS,0)
    if not shift or shift['cancelled'] or not shift['end']: return result
    gross, actual = duration(shift)
    if gross > 86400: return result
    start,end = parse(shift['start']),parse(shift['end'])
    pauses = json.loads(shift['breaks']) if isinstance(shift['breaks'],str) else shift['breaks']
    intervals=[]; cursor=start
    for pause in pauses:
        a,b=parse(pause['start']),parse(pause['end'])
        if cursor<a: intervals.append((cursor,a))
        cursor=b
    if cursor<end: intervals.append((cursor,end))
    worked=0
    for a,b in intervals:
        while a<b:
            nextday=datetime.combine(a.date()+timedelta(days=1),datetime.min.time(),JST)
            cuts=[b,nextday]
            for hour in (5,22):
                cut=a.replace(hour=hour,minute=0,second=0,microsecond=0)
                if cut>a: cuts.append(cut)
            if worked<28800: cuts.append(a+timedelta(seconds=28800-worked))
            stop=min(cuts); seconds=int((stop-a).total_seconds())
            night=a.hour>=22 or a.hour<5
            key=('休日' if holiday(a.date()) else '通常日')+('深夜残業' if night and worked>=28800 else '残業' if worked>=28800 else '深夜' if night else '基本')
            result[key]+=seconds; worked+=seconds; a=stop
    assert sum(result.values())==actual
    return result

def previous_months(day):
    n=day.year*12+day.month-1
    return [f'{(n-i)//12:04}-{(n-i)%12+1:02}' for i in (3,2,1)]

def leave_quote(db, employee, day, portion):
    from .requests import employee_store
    from .identity import enrich
    day=valid_day(day)
    person=db.execute('SELECT * FROM employees WHERE id=? AND active=1',(employee,)).fetchone()
    if not person: raise ValueError('利用中の従業員を選択してください。')
    home=employee_store(db,employee,day.isoformat())
    org=db.execute('SELECT kind FROM organizations WHERE store_id=?',(home,)).fetchone()[0]
    user=db.execute('SELECT * FROM users WHERE username=?',(employee,)).fetchone()
    privileged=org=='department' or bool(user and enrich(db,user)['roles'])
    if portion not in ('full','half') or (not privileged and portion!='full'): raise ValueError('店舗一般従業員は全休のみ選択できます。')
    if privileged: return {'days':.5 if portion=='half' else 1,'seconds':14400 if portion=='half' else 28800,'basis':{'type':'固定時間'}}
    months=previous_months(day); seconds=0; dates=set()
    for month in months:
        # Every affiliation with relevant history must have a closed month.
        shifts=[dict(r) for r in db.execute('SELECT * FROM shifts WHERE employee_id=? AND substr(start,1,7)=? AND cancelled=0',(employee,month))]
        orgs={r['store_id'] for r in shifts} or {employee_store(db,employee,month+'-01')}
        for orgid in orgs:
            closed=db.execute('SELECT closed FROM monthly WHERE store_id=? AND month=?',(orgid,month)).fetchone()
            if not closed or not closed[0]: raise ValueError('平均算定には直前3か月の月次確定が必要です。')
        totals=summarize(db,month)
        seconds+=sum(r['総実働時間'] for r in totals if r['employee_id']==employee)
        report=monthly_report(db,month)
        days=sum(r['days'] for r in report['employee_totals'] if r['employee_id']==employee)
        dates.update((month,i) for i in range(max(0,days)))
    if not dates: raise LookupError('勤務実績がないため自動算定できません。管理者へ通知しました。手作業の算定はシステム外です。')
    return {'days':1,'seconds':min(28800,round(seconds/len(dates))),'basis':{'type':'直前確定3か月平均','months':months,'days':len(dates),'seconds':seconds}}

def summarize(db, month, store=None):
    valid_month(month)
    report=monthly_report(db,month,store)
    rows={r['employee_id']:{'employee_id':r['employee_id'],'name':r['name'],'store_id':store or '', 'month':month,**dict.fromkeys(METRICS,0),'確定状態':'確定済み' if r['closed'] else '未確定'} for r in report['employee_totals']}
    def get(emp):
        if emp not in rows:
            e=db.execute('SELECT name FROM employees WHERE id=?',(emp,)).fetchone()
            rows[emp]={'employee_id':emp,'name':e[0],'store_id':store or '', 'month':month,**dict.fromkeys(METRICS,0),'確定状態':'未確定'}
        return rows[emp]
    for s in db.execute('SELECT * FROM shifts WHERE substr(start,1,7)=?',(month,)):
        if store is not None and s['store_id']!=store: continue
        base=db.execute('SELECT 1 FROM metric_baselines WHERE shift_id=?',(s['id'],)).fetchone()
        if base: continue
        metrics=shift_bins(s); row=get(s['employee_id'])
        for k,v in metrics.items(): row[k]+=v
    for table in ('metric_baselines','metric_postings'):
        for p in db.execute(f'SELECT * FROM {table} WHERE month=?',(month,)):
            if store is None or p['store_id']==store:
                row=get(p['employee_id'])
                for k,v in json.loads(p['metrics']).items(): row[k]+=v
    for entry in db.execute('SELECT * FROM leave_entries WHERE substr(day,1,7)=?',(month,)):
        if store is None or entry['store_id']==store:
            row=get(entry['employee_id']);row['有給取得日数']+=entry['days'];row['有給計上時間']+=entry['seconds']
    for r in report['employee_totals']:
        row=get(r['employee_id']);row['出勤日数']=r['days'];row['過去月調整時間']=r['special_seconds']
    for row in rows.values():
        row['総実働時間']=sum(row[k] for k in BINS)
        scopes={r[0] for r in db.execute('SELECT store_id FROM leave_entries WHERE employee_id=? AND substr(day,1,7)=?',(row['employee_id'],month)) if store is None or r[0]==store}
        scopes.update(r['store_id'] for r in report['rows'] if r['employee_id']==row['employee_id'])
        if scopes: row['確定状態']='確定済み' if all((db.execute('SELECT closed FROM monthly WHERE store_id=? AND month=?',(o,month)).fetchone() or [0])[0] for o in scopes) else '未確定'
    return list(rows.values())
