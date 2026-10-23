import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const source=readFileSync(new URL('../mova-robot/scripts/viewer_assets/viewer.js',import.meta.url),'utf8');
const TOKEN='a'.repeat(43);
const flush=async()=>{for(let i=0;i<20;i++)await Promise.resolve();};
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return{promise,resolve,reject};};
const response=(body={},status=200)=>({ok:status>=200&&status<300,status,json:async()=>body,blob:async()=>({image:true})});
class Target{
  constructor(){this.handlers=new Map();}
  addEventListener(name,fn){if(!this.handlers.has(name))this.handlers.set(name,[]);this.handlers.get(name).push(fn);}
  dispatch(name,fields={}){
    const event={button:0,isPrimary:true,pointerId:1,repeat:false,preventDefault(){this.prevented=true;},...fields};
    for(const fn of this.handlers.get(name)||[])fn(event);
    this['on'+name]?.(event);return event;
  }
}
class Element extends Target{
  constructor(id,direction){super();this.id=id;this.dataset=direction?{direction}:{};this.disabled=false;this.hidden=false;
    this.checked=false;this.value='';this.textContent='';this.attributes={};this.captures=new Set();this.classes=new Set();
    this.classList={toggle:(name,on)=>on?this.classes.add(name):this.classes.delete(name)};}
  setAttribute(name,value){this.attributes[name]=value;}
  focus(){this.focused=true;}
  scrollIntoView(){this.scrolled=true;}
  removeAttribute(name){delete this[name];delete this.attributes[name];}
  setPointerCapture(id){this.captures.add(id);}
  hasPointerCapture(id){return this.captures.has(id);}
  releasePointerCapture(id){this.captures.delete(id);this.dispatch('lostpointercapture',{pointerId:id});}
}
function browser({hash='#'+TOKEN,storage=new Map(),origin='http://127.0.0.1:54321',phase='live'}={}){
  let now=10000,nextTimer=1,random=0,nextBlob=0;
  const timers=new Map(),calls=[],pending=new Map(),routes=new Map();
  const elements=Object.fromEntries(['picture','empty','livebadge','status','dot','countdown','open','close','pin','supervised','steerstatus','steerhint','steertiming','reconnect'].map(id=>[id,new Element(id)]));
  elements.picture.hidden=true;
  const buttons=['left','forward','right','stop'].map(direction=>new Element(direction,direction));
  const document=new Target();document.hidden=false;document.getElementById=id=>elements[id];
  document.querySelectorAll=()=>buttons;document.querySelector=()=>buttons[3];
  const window=new Target();
  const model={phase,message:'Fixture camera',has_frame:phase==='live',frame_age_ms:0,steering_allowed:true,steering_active:false,remaining_seconds:30};
  const context=vm.createContext({document,window,location:{hash,origin,pathname:'/'},history:{replaceState(){}},
    sessionStorage:{getItem:key=>storage.get(key)||null,setItem:(key,value)=>storage.set(key,value),removeItem:key=>storage.delete(key)},
    performance:{now:()=>now},crypto:{getRandomValues:array=>{array.fill(++random);return array;}},Uint8Array,
    URL:{createObjectURL:()=>`blob:fixture-${++nextBlob}`,revokeObjectURL(){}},AbortController,
    Image:class{decode(){return imageDecode();}},
    setInterval:(fn,delay)=>{const id=nextTimer++;timers.set(id,{fn,at:now+delay,delay});return id;},
    clearInterval:id=>timers.delete(id),
    setTimeout:(fn,delay)=>{const id=nextTimer++;timers.set(id,{fn,at:now+delay,delay:0});return id;},
    clearTimeout:id=>timers.delete(id),
    fetch:async(path,options={})=>{
      const body=options.body?JSON.parse(options.body):undefined;calls.push({path,body,options,time:now});
      if(routes.has(path))return routes.get(path)(body,options);
      if(path==='/status')return response({...model});
      if(path==='/frame')return response();
      if(path==='/steer/hold'){const d=deferred();pending.set(body.hold_id,d);return d.promise;}
      if(path==='/steer/release'){
        pending.get(body.hold_id)?.resolve(response({stop_acknowledged:true}));
        return response({stop_acknowledged:true});
      }
      if(path==='/steer')return response({stop_acknowledged:true});
      return response({ok:true});
    },console});
  let imageDecode=()=>Promise.resolve();
  vm.runInContext(source,context);
  async function advance(ms){
    const end=now+ms;
    while(true){
      const due=[...timers.entries()].filter(([,t])=>t.at<=end).sort((a,b)=>a[1].at-b[1].at||a[0]-b[0])[0];
      if(!due)break;
      const[id,t]=due;now=t.at;if(t.delay)t.at+=t.delay;else timers.delete(id);
      t.fn();await flush();
    }
    now=end;await flush();
  }
  async function ready(){await flush();elements.supervised.checked=true;elements.supervised.dispatch('change');await vm.runInContext('frame()',context);await flush();}
  return{context,storage,elements,buttons,document,window,model,routes,calls,pending,advance,ready,
    setDecode:fn=>{imageDecode=fn;}, count:path=>calls.filter(c=>c.path===path),
    run:code=>vm.runInContext(code,context)};
}

test('reload restores per-tab origin-scoped access and never saves the PIN',async()=>{
  const storage=new Map(),first=browser({storage,phase:'closed'});await flush();
  first.elements.pin.value='1234';first.routes.set('/start',()=>response({error:'Wrong PIN'},409));
  first.elements.open.dispatch('click');await flush();
  assert.equal(first.elements.pin.value,'');assert.equal(storage.size,1);
  assert.equal(storage.get('mova.viewer.access:http://127.0.0.1:54321'),TOKEN);
  const second=browser({hash:'',storage,phase:'closed'});await flush();
  assert.equal(second.count('/status')[0].options.headers['X-Viewer-Token'],TOKEN);
  assert.equal(second.elements.open.disabled,false);
  const different=browser({hash:'',storage,origin:'http://127.0.0.1:54322'});await flush();
  assert.equal(different.count('/status').length,0);assert.equal(different.elements.open.disabled,true);
  assert.match(different.elements.status.textContent,/private viewer link/i);
});

test('403 clears stored access, including a frame response, and explains expiry',async()=>{
  const b=browser();await b.ready();b.routes.set('/frame',()=>response({},403));
  await b.run('frame()');await flush();
  assert.equal(b.storage.size,0);assert.equal(b.elements.open.disabled,true);assert.equal(b.buttons[3].disabled,true);
  assert.match(b.elements.status.textContent,/expired.*fresh private link/i);
  await b.advance(2000);assert.equal(b.count('/status').length,1);
});

test('a rejected start re-enables Open and PIN with a closed-camera prerequisite',async()=>{
  const b=browser({phase:'closed'});await flush();b.routes.set('/start',()=>response({error:'PIN was rejected'},409));
  b.elements.pin.value='9999';b.elements.open.dispatch('click');await flush();
  assert.equal(b.elements.open.disabled,false);assert.equal(b.elements.pin.disabled,false);
  assert.match(b.elements.status.textContent,/PIN was rejected/);assert.match(b.elements.steerhint.textContent,/Open live view/);
});

test('prerequisites distinguish closed, starting, disabled, unsupervised and stale display',async()=>{
  const b=browser({phase:'closed'});await flush();assert.match(b.elements.steerhint.textContent,/camera is closed.*Reopen live view/);
  b.model.phase='starting';await b.run('poll()');assert.match(b.elements.steerhint.textContent,/Wait for the live picture/);
  b.model.phase='live';b.model.has_frame=true;b.model.steering_allowed=false;await b.run('poll()');
  assert.match(b.elements.steerhint.textContent,/Movement is unavailable.*restart the viewer with steering enabled/);
  b.model.steering_allowed=true;await b.run('poll()');assert.match(b.elements.steerhint.textContent,/Tick the box/);
  b.elements.supervised.checked=true;b.elements.supervised.dispatch('change');
  assert.match(b.elements.steerhint.textContent,/Waiting for a fresh picture/);assert.equal(b.buttons[0].disabled,true);
  await b.run('frame()');assert.equal(b.buttons[0].disabled,false);
});

test('click alone does not move; pointer hold sends one command and heartbeats until release',async()=>{
  const b=browser();await b.ready();b.buttons[0].dispatch('click');assert.equal(b.count('/steer/hold').length,0);
  b.buttons[0].dispatch('pointerdown',{pointerId:7});await flush();
  const start=b.count('/steer/hold')[0];assert.match(start.body.hold_id,/^[0-9a-f]{32}$/);
  assert.equal(start.body.direction,'left');assert.equal(start.body.supervised,true);
  assert.equal(b.buttons[0].disabled,false);assert.equal(b.buttons[1].disabled,true);
  await b.advance(250);assert.equal(b.count('/steer/heartbeat').length,1);
  b.buttons[0].dispatch('pointerup',{pointerId:7});await flush();
  assert.equal(b.count('/steer/release').length,1);assert.equal(b.count('/steer/release')[0].body.hold_id,start.body.hold_id);
  const heartbeats=b.count('/steer/heartbeat').length;await b.advance(750);
  assert.equal(b.count('/steer/heartbeat').length,heartbeats);assert.equal(b.elements.supervised.checked,true);
  assert.equal(b.buttons[0].disabled,false);assert.match(b.elements.steerstatus.textContent,/Stop acknowledged/);
});

test('release before start completion is tombstoned and cancellation stays reusable',async()=>{
  const b=browser();await b.ready();const start=deferred(),release=deferred();
  b.routes.set('/steer/hold',()=>start.promise);b.routes.set('/steer/release',()=>release.promise);
  b.buttons[0].dispatch('pointerdown');b.buttons[0].dispatch('pointerup');await flush();
  assert.equal(b.count('/steer/release').length,1);assert.equal(b.buttons[0].disabled,true);
  release.resolve(response({released:true,stop_required:false,stop_acknowledged:null}));await flush();
  assert.equal(b.buttons[0].disabled,true,'must wait for the old blocking hold response too');
  start.resolve(response({error:'Hold cancelled before start'},409));await flush();
  assert.equal(b.elements.supervised.checked,true);assert.equal(b.buttons[0].disabled,false);
  assert.match(b.elements.steerstatus.textContent,/cancelled before it started/);
  await b.advance(500);assert.equal(b.count('/steer/heartbeat').length,0);
});

test('release timing measures delayed stop confirmation without claiming physical stop',async()=>{
  const b=browser();await b.ready();const released=deferred();
  b.routes.set('/steer/release',()=>released.promise);
  b.buttons[0].dispatch('pointerdown');await b.advance(250);
  const id=b.count('/steer/hold')[0].body.hold_id;
  b.buttons[0].dispatch('pointerup');await b.advance(180);
  released.resolve(response({stop_acknowledged:true}));
  b.pending.get(id).resolve(response({stop_acknowledged:true}));await flush();
  assert.equal(b.elements.steertiming.attributes['data-stop-reply-ms'],'180');
  assert.match(b.elements.steertiming.textContent,/180 ms.*communication, not physical stopping/);
  b.buttons[2].dispatch('pointerdown',{pointerId:2});await flush();
  assert.equal(b.elements.steertiming.textContent,'');
  assert.equal(b.elements.steertiming.attributes['data-stop-reply-ms'],undefined);
});

test('cancelled-before-start does not produce a stop acknowledgement timing',async()=>{
  const b=browser();await b.ready();const stopped={stop_required:false,stop_acknowledged:null};
  b.routes.set('/steer/release',body=>{
    b.pending.get(body.hold_id).resolve(response(stopped));return response(stopped);
  });
  b.buttons[0].dispatch('pointerdown');b.buttons[0].dispatch('pointerup');await flush();
  assert.equal(b.elements.steertiming.textContent,'');
  assert.equal(b.elements.steertiming.attributes['data-stop-reply-ms'],undefined);
});

for(const event of ['pointercancel','lostpointercapture','blur'])test(`${event} releases once without waiting for the hold reply`,async()=>{
  const b=browser();await b.ready();b.buttons[0].dispatch('pointerdown');await flush();
  if(event==='blur')b.window.dispatch('blur');else b.buttons[0].dispatch(event);
  b.window.dispatch('pointerup');await flush();
  assert.equal(b.count('/steer/release').length,1);assert.equal(b.count('/steer/hold').length,1);
});

for(const key of [' ','Enter'])test(`keyboard ${JSON.stringify(key)} ignores auto-repeat and releases from global keyup`,async()=>{
  const b=browser();await b.ready();b.buttons[0].dispatch('keydown',{key});
  b.buttons[0].dispatch('keydown',{key,repeat:true});b.buttons[0].dispatch('click');await flush();
  assert.equal(b.count('/steer/hold').length,1);b.window.dispatch('keyup',{key});await flush();
  assert.equal(b.count('/steer/release').length,1);
});

test('global Stop releases the exact pending hold before its separate stop request',async()=>{
  const b=browser();await b.ready();b.buttons[0].dispatch('pointerdown');b.buttons[3].dispatch('click');await flush();
  const motionCalls=b.calls.filter(c=>c.path.startsWith('/steer'));
  assert.deepEqual(motionCalls.map(c=>c.path),['/steer/hold','/steer/release','/steer']);
  assert.equal(motionCalls[0].body.hold_id,motionCalls[1].body.hold_id);
  assert.equal(motionCalls[2].body.direction,'stop');assert.match(b.elements.steerstatus.textContent,/Stop acknowledged/);
});

test('late heartbeat failure cannot revive or overwrite a later gesture',async()=>{
  const b=browser();await b.ready();const oldHeartbeat=deferred();b.routes.set('/steer/heartbeat',()=>oldHeartbeat.promise);
  b.buttons[0].dispatch('pointerdown');await b.advance(250);b.buttons[0].dispatch('pointerup');await flush();
  b.buttons[2].dispatch('pointerdown',{pointerId:2});await flush();const message=b.elements.steerstatus.textContent;
  oldHeartbeat.resolve(response({error:'Old hold finished'},409));await flush();
  assert.equal(b.elements.supervised.checked,true);assert.equal(b.elements.steerstatus.textContent,message);
  assert.equal(b.count('/steer/hold').length,2);assert.equal(b.count('/steer/release').length,1);
  b.buttons[2].dispatch('pointerup',{pointerId:2});await flush();
});

test('a rejected heartbeat releases the hold and cannot continue renewing it',async()=>{
  const b=browser();await b.ready();b.routes.set('/steer/heartbeat',()=>response({accepted:false}));
  b.buttons[0].dispatch('pointerdown');await b.advance(250);
  assert.equal(b.count('/steer/release').length,1);
  assert.equal(b.count('/steer/release')[0].body.hold_id,b.count('/steer/hold')[0].body.hold_id);
  assert.equal(b.elements.supervised.checked,false);assert.equal(b.buttons[0].disabled,true);
  assert.match(b.elements.steerstatus.textContent,/Movement permission ended/);
  await b.advance(1000);
  assert.equal(b.count('/steer/heartbeat').length,1);assert.equal(b.count('/steer/hold').length,1);
});

test('loss of an actually displayed fresh image releases even while status is fresh',async()=>{
  const b=browser();await b.ready();b.buttons[0].dispatch('pointerdown');await flush();
  const hung=deferred();b.routes.set('/frame',()=>hung.promise);await b.advance(2100);
  assert.equal(b.model.frame_age_ms,0);assert.equal(b.count('/steer/release').length,1);
  assert.equal(b.buttons[0].disabled,true);assert.match(b.elements.steerhint.textContent,/fresh picture/);
});

test('a failed image decode cannot enable movement',async()=>{
  const b=browser();await flush();b.elements.supervised.checked=true;b.setDecode(()=>Promise.reject(Error('invalid fixture')));
  await b.run('frame()');await flush();assert.equal(b.buttons[0].disabled,true);assert.equal(b.elements.picture.hidden,true);
});

test('checkbox off, hidden tab and pagehide release and cease heartbeat',async()=>{
  for(const reason of ['checkbox','hidden','pagehide']){
    const b=browser();await b.ready();b.buttons[0].dispatch('pointerdown');await flush();
    if(reason==='checkbox'){b.elements.supervised.checked=false;b.elements.supervised.dispatch('change');}
    else if(reason==='hidden'){b.document.hidden=true;b.document.dispatch('visibilitychange');}
    else b.window.dispatch('pagehide');
    await flush();assert.equal(b.count('/steer/release').length,1,reason);
    await b.advance(500);assert.equal(b.count('/steer/heartbeat').length,0,reason);
  }
});

test('five-second frontend cap releases without another press or movement retry',async()=>{
  const b=browser();await b.ready();b.buttons[0].dispatch('pointerdown');await b.advance(5000);
  assert.equal(b.count('/steer/hold').length,1);assert.equal(b.count('/steer/release').length,1);
  const heartbeats=b.count('/steer/heartbeat').length;await b.advance(1000);
  assert.equal(b.count('/steer/heartbeat').length,heartbeats);assert.equal(b.elements.supervised.checked,true);
});

test('hidden tabs keep only the viewer heartbeat, release movement, and require a new visible image',async()=>{
  const b=browser();await b.ready();b.buttons[0].dispatch('pointerdown');await flush();
  const frames=b.count('/frame').length,statusPolls=b.count('/status').length;
  b.document.hidden=true;b.document.dispatch('visibilitychange');await flush();
  assert.equal(b.count('/steer/release').length,1);assert.equal(b.count('/stop').length,0);
  assert.equal(b.elements.picture.hidden,true);assert.equal(b.buttons[0].disabled,true);
  await b.advance(4500);
  assert.ok(b.count('/status').length>statusPolls+2);
  assert.equal(b.count('/frame').length,frames);assert.equal(b.count('/steer/heartbeat').length,0);
  const decode=deferred();b.setDecode(()=>decode.promise);
  b.document.hidden=false;b.document.dispatch('visibilitychange');await flush();
  assert.equal(b.elements.picture.hidden,true);assert.equal(b.buttons[0].disabled,true);
  decode.resolve();await flush();
  assert.equal(b.elements.picture.hidden,false);assert.equal(b.buttons[0].disabled,false);
  assert.equal(b.elements.supervised.checked,true);assert.equal(b.count('/steer/hold').length,1);
});

test('a frame requested before hiding cannot restore the picture while hidden',async()=>{
  const b=browser();await b.ready();const old=deferred();b.routes.set('/frame',()=>old.promise);
  const request=b.run('frame()');await flush();b.document.hidden=true;b.document.dispatch('visibilitychange');
  old.resolve(response());await request;await flush();
  assert.equal(b.elements.picture.hidden,true);assert.equal(b.buttons[0].disabled,true);
  assert.equal(b.count('/stop').length,0);
});

test('manual close and actual page exit still close the camera',async()=>{
  for(const event of ['manual','pagehide']){
    const b=browser();await b.ready();b.buttons[0].dispatch('pointerdown');await flush();
    if(event==='manual')b.elements.close.dispatch('click');else b.window.dispatch('pagehide');
    await flush();
    assert.equal(b.count('/steer/release').length,1,event);assert.equal(b.count('/stop').length,1,event);
    assert.equal(b.elements.picture.hidden,true,event);assert.equal(b.elements.supervised.checked,false,event);
  }
});

test('an expired camera has a prominent reopen action that only focuses the PIN controls',async()=>{
  const b=browser({phase:'closed'});await flush();b.model.close_reason='timeout';await b.run('poll()');
  assert.match(b.elements.steerhint.textContent,/five-minute camera session ended/);
  assert.equal(b.elements.reconnect.hidden,false);assert.equal(b.elements.reconnect.textContent,'Reopen live view');
  b.elements.reconnect.dispatch('click');
  assert.equal(b.elements.pin.focused,true);assert.equal(b.elements.open.scrolled,true);
  assert.equal(b.count('/start').length,0);assert.equal(b.storage.size,1);
  b.model.phase='ready';b.model.close_reason=null;await b.run('poll()');
  assert.match(b.elements.steerhint.textContent,/Open live view to enable/);
  assert.equal(b.elements.reconnect.textContent,'Open live view');
  b.model.phase='live';b.model.has_frame=true;await b.run('poll()');assert.equal(b.elements.reconnect.hidden,true);
});

test('a hung frame fetch times out and a later frame can restore controls',async()=>{
  const b=browser();await b.ready();const hung=deferred();b.routes.set('/frame',()=>hung.promise);
  const request=b.run('frame()');await flush();const options=b.count('/frame').at(-1).options;
  await b.advance(2000);await request;
  assert.equal(options.signal.aborted,true);assert.equal(b.elements.picture.hidden,true);
  assert.equal(b.buttons[0].disabled,true);
  b.routes.delete('/frame');await b.advance(250);
  assert.equal(b.elements.picture.hidden,false);assert.equal(b.buttons[0].disabled,false);
  assert.equal(b.count('/steer/hold').length,0);
});

test('pointer capture failure explains the failed press without sending movement',async()=>{
  const b=browser();await b.ready();b.buttons[0].setPointerCapture=()=>{throw Error('not captured');};
  b.buttons[0].dispatch('pointerdown');await flush();
  assert.match(b.elements.steerstatus.textContent,/press could not be held.*Space or Enter/);
  assert.equal(b.count('/steer/hold').length,0);assert.equal(b.buttons[0].disabled,false);
  await b.advance(500);assert.equal(b.count('/steer/heartbeat').length,0);
});
