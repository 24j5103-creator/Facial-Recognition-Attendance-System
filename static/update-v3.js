'use strict';
// In-page dialogs retain the underlying form and work in all supported browsers.
async function question(message, text=false, html=''){
 const d=document.createElement('dialog');d.className='question';
 d.innerHTML=`<form><h2>${esc(message)}</h2>${html}${text?'<label>入力してください</label><input name="answer" required autocomplete="off">':''}<div class="actions"><button type="button" data-no>戻る</button><button type="submit" class="primary">${text?'確定':'確認して進む'}</button></div></form>`;
 document.body.append(d);d.showModal();
 return new Promise(resolve=>{const done=value=>{d.close();d.remove();resolve(value)};d.querySelector('[data-no]').onclick=()=>done(text?null:false);d.oncancel=e=>{e.preventDefault();e.stopImmediatePropagation();done(text?null:false)};d.querySelector('form').onsubmit=e=>{e.preventDefault();e.stopImmediatePropagation();done(text?d.querySelector('[name=answer]').value:true)}})
}
const ask=message=>question(message),askText=message=>question(message,true);
function destinationOptions(kind){return (D.all_stores||D.stores).filter(o=>o.active&&(kind!=='help'||o.kind==='store')).map(s=>`<option value="${s.id}">${esc(s.name)}</option>`).join('')}
function roleText(role){return D?.roles?.[role]||({store_manager:'店舗責任者',department_manager:'部署責任者',area_manager:'エリア責任者',hq:'本社担当者',admin:'本社管理責任者'}[role]||role)}
function high(){return ['hq','admin'].includes(me?.role)}
function scopeLabel(g){return g.role==='area_manager'?(D.areas.find(a=>a.id===g.scope_id)?.name||g.scope_id):g.scope_id?storeName(g.scope_id):'全社'}
sections.employees[1].push(['leave','有給申請']);sections.approval[1].push(['approval/leave','有給休暇'],['approval/password','共用パスワード変更']);
sections.people[1].push(['areas','エリア管理']);sections.settings[1].push(['history','操作履歴']);
sections.settings[1]=sections.settings[1].map(([p,t])=>[p,p==='store-password'?'端末パスワード変更申請':t]);
const baseLogin=login;
login=function(mode=HQ_PORTAL?'hq':'terminal'){
 route='login';if(mode==='manager'||HQ_PORTAL){baseLogin(mode==='manager'?'manager':'hq');return}
 const orgs=P.stores.filter(s=>s.kind===(PORTAL==='office'?'department':'store'));
 $('#main').innerHTML=`<section class="panel narrow"><h1>${PORTAL==='office'?'本社部署':'店舗'}端末へログイン</h1><p>所属を選択し、名称をもう一度入力してください。</p><form id="terminal-login"><label>所属${PORTAL==='office'?'（同じパスワードなら複数選択可）':''}</label><div class="checklist">${orgs.map(o=>`<label><input type="${PORTAL==='office'?'checkbox':'radio'}" name="org" value="${o.id}">${esc(o.name)}</label>`).join('')}</div><p id="chosen-orgs" class="notice">所属を選択してください。</p><label>選択した名称（複数は「、」で区切る）</label><input name="store_name" required autocomplete="off"><label>端末パスワード</label><input type="password" name="password" required minlength="6"><button class="primary" type="submit">ログイン</button></form></section>`;
 $('#terminal-login').onchange=()=>{$('#chosen-orgs').textContent=[...document.querySelectorAll('[name=org]:checked')].map(x=>orgs.find(o=>o.id==x.value).name).join('、')};
};
const oldHeader=header;
header=function(){oldHeader();if(me?.user){const span=$('#header .header-actions span');if(span)span.textContent=me.user.name+'　'+me.roles.map(roleText).join('・')}else if(!me){$('#header .header-actions').innerHTML='<a href="/store">店舗</a>　<a href="/office">本社部署</a>　<a href="/admin">管理者</a>'}else if(me.role==='terminal'){$('#header .brand').textContent=(me.scopes||[me.store_id]).map(storeName).join('・')}};
const previousRender=render;
render=async function(path='home'){
 if(!me||me.role==='terminal')return;
 const special=['users','areas','history','leave','stores','store-password','backups'];
 if(!special.includes(path)){await previousRender(path);if(path==='home'&&D.alerts.length)$('#main').insertAdjacentHTML('beforeend',`<section class="panel"><h2>有給算定の要確認</h2>${table(['従業員','対象日','内容'],D.alerts.map(a=>`<tr><td>${esc(a.employee_id)}</td><td>${esc(a.day)}</td><td>${esc(a.message)}</td></tr>`))}</section>`);return}
 stopCamera();route=path;D=await api('data');header();$('#navigation').classList.remove('open');$('.mega')?.remove();const m=$('#main');
 if(path==='users'){
  m.innerHTML=page('権限保持者一覧','所属と権限は別に管理します。adminの変更はサーバーコマンド限定です。',btn('権限を付与・変更','user-add','primary'))+table(['従業員番号','氏名','付与された権限・担当範囲','操作'],D.users.map(u=>`<tr><td>${esc(u.username)}</td><td>${esc(u.name)}</td><td>${u.grants.map(g=>`${esc(roleText(g.role))}：${esc(scopeLabel(g))}`).join('<br>')}</td><td>${btn('権限を編集','user-edit','',`data-id="${u.id}"`)}</td></tr>`));return
 }
 if(path==='stores'){
  m.innerHTML=page('店舗・部署一覧','所属コードは再利用しません。',high()?btn('所属を追加','org-add','primary'):'')+table(['コード','名称','種類','状態','操作'],D.stores.map(o=>`<tr><td>${esc(o.code)}</td><td>${esc(o.name)}</td><td>${o.kind==='store'?'店舗':'部署'}</td><td>${tag(o.active?'利用中':'停止中')}</td><td>${high()?btn(o.active?'停止':'再開','store-active','',`data-id="${o.id}" data-active="${o.active?0:1}"`):''}</td></tr>`));return
 }
 if(path==='areas'){
  m.innerHTML=page('エリア管理','エリア内の店舗から担当範囲が自動的に導出されます。',high()?btn('エリアを追加','area-add','primary'):'')+table(['エリア','構成店舗','操作'],D.areas.map(a=>`<tr><td>${esc(a.name)}</td><td>${esc(a.stores.map(storeName).join('・'))}</td><td>${high()?btn('編集','area-edit','',`data-id="${a.id}"`):''}</td></tr>`));return
 }
 if(path==='leave'){
  m.innerHTML=page('有給休暇','部署所属・権限保持者は全休8時間／半休4時間。店舗一般は直前の確定3か月平均です。')+`<form class="panel" id="leave-form"><label>所属</label><select name="store_id">${storeOptions()}</select><label>対象従業員</label><select name="employee_id">${D.employees.filter(e=>e.active).map(e=>`<option value="${e.id}">${esc(e.name)} ${e.id}</option>`).join('')}</select><label>取得日</label><input type="date" name="day" required><label>取得区分</label><select name="portion"><option value="full">全休</option><option value="half">半休（部署所属・権限保持者のみ）</option></select><label>理由</label><textarea name="reason" required></textarea><button type="submit" class="primary">${high()?'直接確定':'申請'}</button></form>`+table(['従業員','取得日','日数','計上時間'],D.leaves.map(x=>`<tr><td>${esc(x.employee_id)}</td><td>${esc(x.day)}</td><td>${x.days}</td><td>${fmt(x.seconds)}</td></tr>`));return
 }
 if(path==='store-password'){
  m.innerHTML=page('端末パスワード変更申請','別のhq/adminの承認後に反映します。個別設定は共通設定より優先します。')+`<form id="shared-v3" class="panel narrow"><label>対象</label><select name="target"><option value="store">店舗共通</option><option value="department">本社部署共通</option><option value="individual">個別の店舗・部署</option></select><label>個別変更する所属</label><select name="store_id">${storeOptions()}</select><label><input name="use_common" type="checkbox">個別パスワードを解除し共通に戻す</label><label>新しいパスワード（6文字以上）</label><input name="password" type="password" minlength="6"><label>理由</label><textarea name="reason" required></textarea><button type="submit" class="primary">変更を申請</button></form>`;return
 }
 if(path==='backups'){
  m.innerHTML=page('バックアップ・復元','同じ対象月の手動バックアップを検証してから上書きします。日次CSVは別に30日保持します。')+`<form id="backup-v3" class="panel filters"><input name="month" type="month" value="${currentMonth()}" required><select name="kind"><option value="sqlite">SQLite</option><option value="csv">CSV（4種類のZIP）</option></select><button class="primary" type="submit">対象を確認して上書き</button></form>`+table(['種類','対象月','作成日時','結果','詳細','操作'],D.backups.map(b=>`<tr><td>${esc(b.kind)}</td><td>${esc(b.month)}</td><td>${local(b.created)}</td><td>${tag(b.valid?'成功':'失敗/置換済み')}</td><td>${esc(b.detail)}</td><td>${me.role==='admin'&&b.kind==='sqlite'&&b.valid?btn('復元','restore','danger',`data-id="${b.id}"`):''}</td></tr>`));return
 }
 if(path==='history'){
  m.innerHTML=page('操作履歴','操作者・対象従業員・所属・年月・操作から検索できます。')+`<form id="history-filter" class="panel filters"><input name="start" type="date" aria-label="開始日"><input name="end" type="date" aria-label="終了日"><input name="actor" placeholder="操作者 氏名・番号"><input name="employee" placeholder="対象者 氏名・番号"><select name="store"><option value="">全所属</option>${storeOptions()}</select><input name="month" type="month" aria-label="対象月"><select name="event"><option value="">全操作</option>${['申請','再申請','取り消し','承認','差し戻し','直接確定','月次確定','月次解除','権限変更','バックアップ'].map(t=>`<option>${t}</option>`).join('')}</select><button class="primary" type="submit">検索</button>${btn('検索結果をCSV出力','history-export')}</form><div id="history-results"></div>`;await historyLoad();return
 }
};
async function historyLoad(download=false){let params=new URLSearchParams(new FormData($('#history-filter')));for(let [k,v] of [...params])if(!v)params.delete(k);if(download){params.set('download','true');return downloadFile('history?'+params,'operations.csv')}let r=await api('history?'+params);window.historyRows=r.rows;$('#history-results').innerHTML=table(['日時','操作者','操作','対象従業員','所属・年月','詳細'],r.rows.map(x=>`<tr><td>${local(x.created)}</td><td>${esc(x.actor_name)}<small>${esc(x.actor_employee_id||'')}</small></td><td>${esc(x.event)}</td><td>${esc(x.employee_name)} ${esc(x.employee_id||'')}</td><td>${esc(storeName(x.store_id)||'―')} ${esc(x.month)}</td><td>${btn('変更前後','history-detail','',`data-id="${x.id}"`)}</td></tr>`))}
async function downloadFile(path,name){const r=await fetch('/api/'+path,{headers:PORTAL_HEADERS});if(!r.ok)throw new Error((await r.json()).error);const url=URL.createObjectURL(await r.blob()),a=document.createElement('a');a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)}
userForm=function(u=null){
 const roles=['store_manager','department_manager','area_manager',...(me.roles.includes('admin')?['hq']:[])];
 modal('従業員の権限',`<form id="roles-v3"><label>従業員</label><select name="employee_id">${D.employees.filter(e=>e.active&&(!u||e.id===u.username)).map(e=>`<option value="${e.id}">${esc(e.name)} ${e.id}</option>`).join('')}</select><label>変更するロール（他のロールは維持）</label><select name="role">${roles.map(r=>`<option value="${r}">${roleText(r)}</option>`).join('')}</select><div id="grant-scopes"></div><label><input name="revoke" type="checkbox">このロールを解除</label><p>新規付与・再付与時は123456に戻り、初回変更が必要です。担当範囲だけの変更ではパスワードを維持します。</p><button type="submit" class="primary">変更を確認</button></form>`);
 function scopes(){const r=$('#roles-v3 [name=role]').value;const items=r==='area_manager'?D.areas:D.stores.filter(o=>o.active&&o.kind===(r==='store_manager'?'store':'department'));$('#grant-scopes').innerHTML=r==='hq'?'<p>全社を担当します。</p>':`<label>担当範囲</label><div class="checklist">${items.map(o=>`<label><input name="scope" type="checkbox" value="${o.id}" ${u?.grants.some(g=>g.role===r&&g.scope_id===o.id)?'checked':''}>${esc(o.name)}</label>`).join('')}</div>`}
 $('#roles-v3 [name=role]').onchange=scopes;scopes()
};
async function confirmPhotos(body){
 const person=D.employees.find(e=>e.id===body.employee_id),name=body.payload.name||person?.name||'';
 if(!await question(`${name}さんの顔写真登録で間違いありませんか？`,false,`<p>従業員番号：${esc(body.employee_id||'新規採番')}<br>所属：${esc(storeName(body.store_id))}</p><div class="photos">${Object.entries(body.images).map(([k,v])=>`<div><p>${k==='mask'?'正面・マスクあり':'正面・マスクなし'}</p><img src="${v}" alt="登録写真"></div>`).join('')}</div><p>この従業員の写真として申請・登録します。</p>`))return null;
 const r=await api('photo-confirm',{...body,confirmed_name:name});return r.token
}
// Fixed-width numeric date controls: four-digit years even on paste.
function upgradeDates(root=document){
 root.querySelectorAll('input[type=datetime-local],input[type=date],input[type=month]').forEach(input=>{
  if(input.dataset.segmented)return;const kind=input.type,original=input.value;input.dataset.segmented='1';input.type='hidden';input.required=false;
  const box=document.createElement('div');box.className='date-parts';const parts=kind==='datetime-local'?['年','月','日','時','分','秒']:kind==='date'?['年','月','日']:['年','月'];
  const values=original.match(/\d+/g)||[];
  parts.forEach((p,i)=>{const field=document.createElement('input');field.type='text';field.inputMode='numeric';field.maxLength=i?2:4;field.placeholder=i?p:'西暦';field.setAttribute('aria-label',(input.name||input.getAttribute('aria-label')||'日時')+' '+p);field.value=values[i]||(original&&i===5?'00':'');field.required=true;field.pattern=i?'[0-9]{2}':'[0-9]{4}';field.oninput=()=>{field.value=field.value.replace(/\D/g,'').slice(0,field.maxLength);sync()};box.append(field,document.createTextNode(p))});
  function sync(){const v=[...box.querySelectorAll('input')].map(x=>x.value);input.value=v[0]+'-'+v[1]+(v.length>2?'-'+v[2]:'')+(v.length>3?'T'+v[3]+':'+v[4]+':'+v[5]:'');input.dispatchEvent(new Event('input',{bubbles:true}));input.dispatchEvent(new Event('change',{bubbles:true}))}
  if(!input.required&&(input.closest('#history-filter')||input.closest('.filters:not(form)')))box.querySelectorAll('input').forEach(x=>x.required=false);
  input.after(box);if(original)sync()
 })
}
const observer=new MutationObserver(()=>upgradeDates());observer.observe(document.body,{childList:true,subtree:true});upgradeDates();
// Dedicated form handlers run before legacy handlers.
document.addEventListener('submit',async e=>{
 const f=e.target;if(!['terminal-login','roles-v3','leave-form','shared-v3','backup-v3','history-filter','org-v3','area-v3'].includes(f.id))return;
 e.preventDefault();e.stopImmediatePropagation();const v=Object.fromEntries(new FormData(f)),button=f.querySelector('[type=submit],button:not([type])');if(button)button.disabled=true;
 try{
  if(f.id==='terminal-login'){await api('login',{...v,mode:'terminal',store_ids:[...f.querySelectorAll('[name=org]:checked')].map(x=>Number(x.value))});await boot();return}
  if(f.id==='history-filter'){await historyLoad();return}
  if(f.id==='backup-v3'){const confirmation=await askText(`${v.month} の ${v.kind} バックアップを上書きします。実行する場合「上書きする」と入力`);if(confirmation===null)return;await api('backup',{...v,confirm:confirmation});await render('backups');return}
  if(!await ask('入力した内容で処理しますか？'))return;
  if(f.id==='roles-v3'){await api('user',{employee_id:v.employee_id,role:v.role,revoke:!!v.revoke,stores:[...f.querySelectorAll('[name=scope]:checked')].map(x=>Number(x.value))});dialog.close();if(v.employee_id===me.user.username)await boot();else await render('users')}
  if(f.id==='leave-form'){await api('request',{id:f.dataset.id||undefined,kind:'leave',store_id:Number(v.store_id),employee_id:v.employee_id,reason:v.reason,payload:{day:v.day,portion:v.portion}});navTo('requests')}
  if(f.id==='shared-v3'){await api('request',{id:f.dataset.id||undefined,kind:'password',store_id:Number(v.store_id),reason:v.reason,payload:{target:v.target,use_common:!!v.use_common},new_password:v.password});f.reset();navTo('requests')}
  if(f.id==='org-v3'){await api('store',v);dialog.close();await render('stores')}
  if(f.id==='area-v3'){await api('area',{id:Number(f.dataset.id)||undefined,name:v.name,stores:[...f.querySelectorAll('[name=store]:checked')].map(x=>Number(x.value))});dialog.close();await render('areas')}
  toast('保存しました。')
 }catch(error){fail(error)}finally{if(button)button.disabled=false}
},true);
document.addEventListener('click',async e=>{
 const b=e.target.closest('[data-action]');if(!b||!['org-add','area-add','area-edit','history-export','history-detail','shift-restore'].includes(b.dataset.action))return;e.stopImmediatePropagation();
 try{
  const a=b.dataset.action;
  if(a==='org-add')modal('店舗・部署の追加','<form id="org-v3"><label>種類</label><select name="kind"><option value="store">店舗</option><option value="department">部署</option></select><label>名称</label><input name="name" required><button type="submit" class="primary">追加</button></form>');
  if(a==='area-add'||a==='area-edit'){const area=D.areas.find(x=>x.id==b.dataset.id);modal('エリア設定',`<form id="area-v3" data-id="${area?.id||''}"><label>エリア名</label><input name="name" value="${esc(area?.name||'')}" required><div class="checklist">${D.all_stores.filter(o=>o.kind==='store'&&o.active).map(o=>`<label><input type="checkbox" name="store" value="${o.id}" ${area?.stores.includes(o.id)?'checked':''}>${esc(o.name)}</label>`).join('')}</div><button type="submit" class="primary">保存</button></form>`)}
  if(a==='history-export')await historyLoad(true);
  if(a==='history-detail'){const x=window.historyRows.find(x=>x.id==b.dataset.id);modal('操作の詳細',`<p>${esc(x.actor_name)} ${esc(x.event)}</p><h3>変更前</h3><pre>${esc(JSON.stringify(x.before,null,2))}</pre><h3>変更後</h3><pre>${esc(JSON.stringify(x.after,null,2))}</pre>`)}
  if(a==='shift-restore'){const s=D.shifts.find(x=>x.id===b.dataset.id);const reason=await askText('取り消し解除の理由');if(!reason)return;await api('request',{kind:'restore',store_id:s.store_id,employee_id:s.employee_id,shift_id:s.id,payload:{},reason});dialog.close();await render('attendance')}
 }catch(error){fail(error)}
},true);
const originalLoadMonth=loadMonth;
loadMonth=async function(){await originalLoadMonth();if(route==='carry')return;const r=window.monthReport,metrics=['出勤日数','有給取得日数','総実働時間','通常日基本','通常日深夜','通常日残業','通常日深夜残業','休日基本','休日深夜','休日残業','休日深夜残業','有給計上時間','過去月調整時間'];$('#month-result').innerHTML=table(['従業員',...metrics],r.metrics.map(x=>`<tr><td>${esc(x.name)}<small>${x.employee_id}</small></td>${metrics.map((k,i)=>`<td>${i<2?x[k]+'日':fmt(x[k])}</td>`).join('')}</tr>`))};

isDirectKind=function(kind){return (high()||me?.roles?.includes("area_manager"))&&["register","profile","correction","cancel","restore"].includes(kind)};

const standardRequestForm=requestForm;
requestForm=async function(kind,shift=null,existing=null,person=null){
 if(existing&&['leave','password'].includes(kind)){
  await render(kind==='leave'?'leave':'store-password');const f=$(kind==='leave'?'#leave-form':'#shared-v3'),p=JSON.parse(existing.payload);f.dataset.id=existing.id;f.elements.store_id.value=existing.store_id;f.elements.reason.value=existing.reason;
  if(kind==='password'){f.elements.target.value=p.target;f.elements.use_common.checked=p.use_common;f.elements.target.setAttribute('aria-readonly','true');f.elements.store_id.setAttribute('aria-readonly','true')}
  else{f.elements.employee_id.value=existing.employee_id;f.elements.portion.value=p.portion;const input=f.querySelector('[name=day]');input.value=p.day;const parts=input.nextElementSibling.querySelectorAll('input');p.day.split('-').forEach((v,i)=>parts[i].value=v)}
  dialog.close();return
 }
 return standardRequestForm(kind,shift,existing,person)
};
