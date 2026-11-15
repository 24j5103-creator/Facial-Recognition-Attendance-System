const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8');
const punch = source.slice(source.indexOf('async function doPunch(action)'), source.indexOf('async function recover()'));

async function scenario({fresh=false, age=0, authenticated=true, rejectPunch=false}={}) {
    const calls=[];
    const context={Date, Error, AUTH_VALID_MS:5000, crypto:{randomUUID:()=> 'request-id'},
        face:fresh?{ticket:'recent-ticket'}:null, faceAt:Date.now()-age, punching:false,
        route:'clock', stream:{}, recognitionTask:Promise.resolve(), unresolved:null,
        updateFace(){}, $(){return {}}, capture:()=> 'new-frame',
        localStorage:{setItem(){},removeItem(){}}, toast(){}, recover(){throw Error('unexpected recovery')},
        complete:async()=>calls.push('complete'),
        api:async(name,body)=>{
            calls.push({name,body});
            if(name==='recognize') {
                // Simulate inference taking longer than the preview's two-second limit.
                await new Promise(resolve=>setTimeout(resolve,10));
                return {authenticated,ticket:'click-ticket',message:'認証失敗'};
            }
            if(rejectPunch)throw Object.assign(new Error('別人'),{status:400});
            return {ok:true};
        }};
    vm.createContext(context); vm.runInContext(punch,context);
    await Promise.all([context.doPunch('出勤'),context.doPunch('出勤')]);
    return {calls,context};
}
(async()=>{
    let currentTime=1000, scheduled=[], previousVisible=false;
    const preview={Date:{now:()=>currentTime}, AUTH_VALID_MS:5000, RECOGNIZE_INTERVAL_MS:4000,
        route:'clock',stream:{},punching:false,recognizing:false,recognitionTask:null,
        face:null,faceAt:0, cameraTimer:null, capture:()=> 'frame',updateFace(){},$(){return {}},
        setTimeout(fn,delay){scheduled.push(delay);return 1},
        api:async()=>{previousVisible=!!preview.face;currentTime+=2000;return {authenticated:true,name:'Test'}}};
    vm.createContext(preview);
    vm.runInContext(source.slice(source.indexOf('async function cameraLoop()'),source.indexOf('function updateFace()')),preview);
    await preview.cameraLoop();
    assert.equal(preview.faceAt,3000); // Full five seconds remain after a two-second inference.
    assert.equal(scheduled[0],2000); // Next capture starts at t=5000, four seconds after t=1000.
    currentTime=5000;await preview.cameraLoop();
    assert.equal(previousVisible,true);assert.equal(preview.faceAt,7000);
    preview.punching=true;await preview.cameraLoop();
    assert.equal(scheduled.at(-1),250); // Polling survives a click during inference.
    const stale=await scenario();
    assert.deepEqual(stale.calls.map(x=>x.name||x),['recognize','punch','complete']);
    assert.equal(stale.calls[1].body.ticket,'click-ticket');
    assert.equal(stale.calls[1].body.image,'new-frame');
    assert.deepEqual((await scenario({fresh:true})).calls.map(x=>x.name||x),['punch','complete']);
    assert.deepEqual((await scenario({fresh:true,age:4000})).calls.map(x=>x.name||x),['punch','complete']);
    assert.deepEqual((await scenario({fresh:true,age:6000})).calls.map(x=>x.name||x),['recognize','punch','complete']);
    const failed=await scenario({authenticated:false});
    assert.equal(failed.calls.length,1); assert.equal(failed.context.punching,false);
    const changed=await scenario({rejectPunch:true});
    assert.equal(changed.context.punching,false); assert.equal(changed.context.unresolved,null);
    assert.ok(!changed.calls.includes('complete'));
    assert.ok(source.includes("${punching?'disabled':''}"));
    assert.ok(source.includes('updateClock();updateFace();'));
    console.log('Passed: five-second window, four-second cadence including inference, continuous preview, polling recovery, authentication and double-click checks.');
})().catch(e=>{console.error(e);process.exitCode=1});
