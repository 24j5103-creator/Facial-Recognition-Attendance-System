'use strict';

const roleLabel=r=>({employee:'一般従業員',manager:'店舗責任者',store_manager:'店舗責任者',department_manager:'部署責任者',area_manager:'エリア責任者',hq:'本社担当者',admin:'本社管理責任者'}[r]||r);
function isDirectKind(kind){return high()&&['register','profile','correction'].includes(kind)}
sections.employees[1].unshift(['people','従業員一覧']);
sections.people[1].push(['employee/register','従業員を登録'],['plans','異動・応援予定']);
sections.settings[1]=sections.settings[1].map(([r,t])=>[r,r==='users'?'権限保持者一覧':t]);
sections.approval[1].push(['approval/profile','従業員情報修正']);

const originalRender=render;
render=async function(path='home'){
    if(!me||me.role==='terminal')return;
    if(['people','users','plans'].includes(path)){
        stopCamera();route=path;D=await api('data');header();
        $('#navigation').classList.remove('open');
        if(path==='people'){
            $('#main').innerHTML=page('従業員一覧','従業員番号・氏名から検索し、詳細画面で情報を編集できます。',btn('従業員を登録','enroll-open','primary'))+
                '<section class="panel"><label for="employee-search">従業員を検索</label><input id="employee-search" type="search" placeholder="従業員番号・氏名" autocomplete="off"></section><div id="employee-results"></div>';
            paintEmployees('');
        }else if(path==='users'){
            if(!['hq','admin'].includes(me.role))throw new Error('本社権限が必要です。');
            $('#main').innerHTML=page('権限保持者一覧','従業員IDに付与された権限を表示します。adminの昇格・降格はサーバーコマンドで行います。',btn('従業員に権限を付与','user-add','primary'))+
                table(['従業員番号','氏名','権限','担当店舗','操作'],D.users.map(u=>`<tr><td>${esc(u.username)}</td><td>${esc(u.name)}</td><td>${esc(roleLabel(u.role))}</td><td>${esc(JSON.parse(u.stores).map(storeName).join('・'))||'全店舗'}</td><td>${u.role==='admin'?'サーバーコマンドで変更':me.role==='hq'&&u.role==='hq'?'管理責任者が変更':btn('権限・担当店舗','user-edit','',`data-id="${u.id}"`)}</td></tr>`));
        }else{
            $('#main').innerHTML=page('異動・応援予定','打刻がない未来の予定を変更できます。応援取り消しは当日も打刻前であれば可能です。')+plansTable();
        }
        return;
    }
    await originalRender(path);
    if(path==='home'&&!['hq','admin'].includes(me.role)){
        $('#main .grid').insertAdjacentHTML('beforebegin','<section class="panel"><h2>勤務中・休憩中の従業員</h2><div id="working-now"></div></section>');
        paintWorking();
    }
};

function paintEmployees(query){
    query=query.trim().toLocaleLowerCase();
    $('#employee-results').innerHTML=table(['従業員番号','氏名','所属店舗','権限','状態','詳細'],D.employees.filter(e=>(e.id+' '+e.name).toLocaleLowerCase().includes(query)).map(e=>`<tr><td>${esc(e.id)}</td><td>${esc(e.name)}</td><td>${esc(storeName(e.current_store_id))}</td><td>${esc((e.roles?.length?e.roles:['employee']).map(roleLabel).join('・'))}</td><td>${tag(e.active?'利用中':'停止中')}</td><td>${btn('詳細・編集','person-detail','',`data-id="${esc(e.id)}"`)}</td></tr>`));
}
function paintWorking(){
    if(!$('#working-now'))return;
    $('#working-now').innerHTML=table(['氏名','状態'],D.shifts.filter(s=>!s.end&&!s.cancelled).map(s=>`<tr><td>${btn(s.name,'working-detail','link-button',`data-id="${esc(s.id)}"`)}</td><td>${tag(s.state)}</td></tr>`));
}
function personDetail(id){
    const e=D.employees.find(e=>e.id===id);if(!e)return;
    modal('従業員情報',`<div class="grid"><section><label>従業員番号</label><p>${esc(e.id)}</p><label>氏名</label><p>${esc(e.name)}</p><label>読み仮名</label><p>${esc(e.kana)||'―'}</p></section><section><label>所属店舗</label><p>${esc(storeName(e.current_store_id))}</p><label>状態</label><p>${tag(e.active?'利用中':'停止中')}</p></section></div><div class="actions">${btn('編集','person-edit','primary',`data-id="${esc(e.id)}"`)}${btn('写真更新申請','person-photo','',`data-id="${esc(e.id)}"`)}${btn('店舗異動申請','person-transfer','',`data-id="${esc(e.id)}"`)}</div>`);
}
function workingDetail(id){
    const s=D.shifts.find(s=>s.id===id);if(!s)return;
    modal('今回の勤務',`<h2>${esc(s.name)} ${tag(s.state)}</h2><p>${esc(storeName(s.store_id))}</p><label>出勤時刻</label><p>${local(s.start)}</p><div id="live-durations"></div>`);
    function paint(){
        if(!dialog.open||!$('#live-durations'))return false;
        const end=s.end?new Date(s.end).getTime():Date.now(),gross=Math.max(0,Math.floor((end-new Date(s.start))/1000));
        const breaks=JSON.parse(s.breaks),rest=breaks.reduce((n,b)=>n+Math.max(0,Math.floor(((b.end?new Date(b.end).getTime():end)-new Date(b.start))/1000)),0);
        const last=breaks.at(-1),running=last&&!last.end?Math.max(0,Math.floor((end-new Date(last.start))/1000)):0;
        $('#live-durations').innerHTML=`<label>実働時間</label><p>${fmt(gross-rest)}</p><label>累計休憩時間（今回の勤務内）</label><p>${fmt(rest)}</p><label>総勤務時間</label><p>${fmt(gross)}</p>${last&&!last.end?`<label>今回の休憩経過時間</label><p>${fmt(running)}</p>`:''}`;
        return true;
    }
    paint();const timer=setInterval(()=>{if(!paint())clearInterval(timer)},1000);
}

userForm=function(u=null){
    if(u?.role==='admin')throw new Error('adminの変更はサーバーコマンドで行ってください。');
    const candidates=D.employees.filter(e=>e.active&&(u?e.id===u.username:!D.users.some(x=>x.username===e.id)));
    modal('権限・担当店舗の変更',`<form id="user-form"><label>従業員</label><select name="employee_id" required>${candidates.map(e=>`<option value="${esc(e.id)}" ${u?.username===e.id?'selected':''}>${esc(e.id)} ${esc(e.name)}</option>`).join('')}</select><label>権限</label><select name="role"><option value="employee">一般従業員（管理権限なし）</option><option value="manager">店舗責任者</option>${me.role==='admin'?'<option value="hq">本社担当者</option>':''}</select><label>担当店舗（店舗責任者は1店舗以上・複数選択可）</label><div class="checklist">${D.stores.filter(s=>s.active).map(s=>`<label><input type="checkbox" name="store" value="${s.id}" ${u&&JSON.parse(u.stores).includes(s.id)?'checked':''}>${esc(s.code||'')} ${esc(s.name)}</label>`).join('')}</div><p class="muted">初期パスワードは123456です。初回・リセット後は本人による変更が必要です。変更後、対象者は再ログインしてください。</p><button type="submit" class="primary">権限変更を確認</button></form>`);
    $('#user-form').elements.role.value=u?.role||'manager';
    if(u){const select=$('#user-form').elements.employee_id;select.onpointerdown=e=>e.preventDefault();select.onkeydown=e=>e.preventDefault();}
};

function plansTable(){
    const today=new Date().toLocaleDateString('sv-SE',{timeZone:'Asia/Tokyo'});
    return table(['種類','従業員番号','店舗','日付','操作'],[...D.helpers.map(p=>({...p,kind:'help',date:p.day})),...D.transfers.map(p=>({...p,kind:'transfer',date:p.effective}))].map(p=>`<tr><td>${p.kind==='help'?'応援勤務':'店舗異動'}</td><td>${esc(p.employee_id)}</td><td>${esc(storeName(p.store_id))}</td><td>${esc(p.date)}</td><td>${p.date>today?btn('予定を編集','plan-edit','',`data-kind="${p.kind}" data-id="${p.id}"`):''}${p.kind==='help'&&p.date>=today?btn('応援を取り消す','plan-cancel','danger',`data-kind="help" data-id="${p.id}"`):''}</td></tr>`));
}
document.addEventListener('input',e=>{if(e.target.id==='employee-search')paintEmployees(e.target.value)});
document.addEventListener('click',async e=>{
    const b=e.target.closest('[data-action]');if(!b)return;
    const action=b.dataset.action;
    if(!['enroll-open','person-detail','person-edit','person-photo','person-transfer','working-detail','plan-edit','plan-cancel'].includes(action))return;
    e.stopImmediatePropagation();
    try{
        if(action==='enroll-open'){navTo('employee/register');return}
        if(action==='person-detail'){personDetail(b.dataset.id);return}
        if(action.startsWith('person-')){
            const p=D.employees.find(x=>x.id===b.dataset.id);
            requestForm(action==='person-edit'?'profile':action==='person-photo'?'photo':'transfer',null,null,p);return;
        }
        if(action==='working-detail'){D=await api('data');workingDetail(b.dataset.id);return}
        if(action==='plan-cancel'){
            if(await ask('打刻前の応援予定を取り消しますか？')){await api(`plan/help/${b.dataset.id}`,{cancel:true});await render('plans')}
            return;
        }
        const p=(b.dataset.kind==='help'?D.helpers:D.transfers).find(x=>x.id==b.dataset.id);
        modal('予定の変更',`<form id="plan-form" data-kind="${b.dataset.kind}" data-id="${p.id}"><label>従業員番号</label><p>${esc(p.employee_id)}</p><label>変更先店舗</label><select name="target_store">${destinationOptions(b.dataset.kind)}</select><label>日付</label><input name="day" type="date" value="${esc(p.day||p.effective)}" required><button type="submit" class="primary">内容を確認して保存</button></form>`);
        $('#plan-form').elements.target_store.value=p.store_id;
    }catch(error){fail(error)}
},true);
document.addEventListener('submit',async e=>{
    const f=e.target;
    if(!['plan-form','user-form'].includes(f.id))return;
    e.preventDefault();e.stopImmediatePropagation();
    if(!await ask('変更内容を確定しますか？'))return;
    const button=f.querySelector('[type=submit]');button.disabled=true;
    try{
        const values=Object.fromEntries(new FormData(f));
        if(f.id==='plan-form')await api(`plan/${f.dataset.kind}/${f.dataset.id}`,{day:values.day,target_store:Number(values.target_store)});
        else await api('user',{employee_id:values.employee_id,role:values.role,stores:[...f.querySelectorAll('[name=store]:checked')].map(x=>Number(x.value))});
        dialog.close();await render(f.id==='plan-form'?'plans':'users');toast('保存しました。');
    }catch(error){fail(error)}finally{button.disabled=false}
},true);
setInterval(async()=>{
    if(route!=='home'||high()||dialog.open)return;
    try{D=await api('data');paintWorking()}catch{}
},15000);
boot().catch(e=>{$('#main').textContent='サーバーへ接続できません。起動状態を確認してください。';fail(e)});
