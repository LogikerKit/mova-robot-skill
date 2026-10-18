'use strict';
const token=location.hash.slice(1);
history.replaceState(null,'',location.pathname);
const $=id=>document.getElementById(id);
let state=null,busy=false,stopBusy=false,frameBusy=false,frameUrl=null,stopped=false,pollBusy=false;
let stateAt=0,frameGeneration=0,frameRequest=null,stateGeneration=0,steeringGeneration=0;
const headers={'X-Viewer-Token':token,'Content-Type':'application/json'};
async function call(path,data){
  const r=await fetch(path,{method:data===undefined?'GET':'POST',headers,body:data===undefined?undefined:JSON.stringify(data),cache:'no-store'});
  const result=await r.json();if(!r.ok)throw Error(result.error||'Request failed.');return result;
}
function clearPicture(){
  frameGeneration++;
  if(frameRequest)frameRequest.abort();
  $('picture').hidden=true;$('picture').removeAttribute('src');$('empty').hidden=false;$('livebadge').hidden=true;
  if(frameUrl){URL.revokeObjectURL(frameUrl);frameUrl=null;}
}
function freshPicture(){
  return !document.hidden&&state?.phase==='live'&&state.has_frame&&typeof state.frame_age_ms==='number'
    &&state.frame_age_ms+performance.now()-stateAt<2000;
}
function refreshButtons(){
  const live=freshPicture()&&state?.steering_allowed&&$('supervised').checked&&!busy&&!stopBusy;
  document.querySelectorAll('[data-direction]').forEach(b=>{b.disabled=b.dataset.direction==='stop'?stopBusy||stopped||!token:!live;});
  const running=['starting','live','stopping'].includes(state?.phase);
  $('open').disabled=running||stopped||!token;$('pin').disabled=running||stopped||!token;
}
$('open').onclick=async()=>{
  const pin=$('pin').value;$('pin').value='';$('open').disabled=true;
  stateGeneration++;state={phase:'starting',has_frame:false};clearPicture();refreshButtons();
  try{await call('/start',{pin});await poll();}catch(e){$('status').textContent=e.message;refreshButtons();}
};
$('close').onclick=async()=>{
  stateGeneration++;clearPicture();$('supervised').checked=false;$('empty').textContent='Closing the camera…';
  if(state)state={...state,phase:'stopping',has_frame:false};refreshButtons();
  try{await call('/stop',{});await poll();}catch(e){$('status').textContent=e.message;}
};
$('supervised').onchange=refreshButtons;
document.querySelectorAll('[data-direction]').forEach(b=>b.onclick=async()=>{
  const stopping=b.dataset.direction==='stop';
  if(stopped||!token||stopBusy||(!stopping&&busy))return;
  if(stopping)stopBusy=true;else busy=true;
  const generation=++steeringGeneration;
  refreshButtons();$('steerstatus').textContent=stopping?'Sending stop…':'Sending movement and stop…';
  try{const r=await call('/steer',{direction:b.dataset.direction,supervised:$('supervised').checked});
    if(generation===steeringGeneration)$('steerstatus').textContent=r.stop_acknowledged?'Stop acknowledged. Confirm the robot has stopped.':'Stop not confirmed—stop the robot in MOVAhome.';
  }catch(e){if(generation===steeringGeneration)$('steerstatus').textContent=e.message;$('supervised').checked=false;}
  finally{if(stopping)stopBusy=false;else busy=false;refreshButtons();}
});
async function poll(){
  if(!token||stopped||document.hidden||pollBusy)return;
  pollBusy=true;
  const generation=stateGeneration;
  try{const next=await call('/status');if(document.hidden||generation!==stateGeneration)return;
    state=next;stateAt=performance.now();$('status').textContent=state.message;$('dot').className=state.phase;
    $('countdown').textContent=state.remaining_seconds?`${state.remaining_seconds}s`:'';
    $('livebadge').hidden=!freshPicture();
    if(!freshPicture()){clearPicture();$('empty').textContent=state.phase==='starting'?'Connecting to your robot…':
      state.phase==='stopping'?'Closing the camera…':state.phase==='live'?'Waiting for a fresh picture…':'Your camera is off.';}
    refreshButtons();
  }catch(e){if(document.hidden||generation!==stateGeneration)return;
    $('status').textContent='Viewer connection lost. Check MOVAhome to confirm the camera is closed.';
    state=null;clearPicture();$('empty').textContent='Viewer disconnected.';$('countdown').textContent='';$('dot').className='error';
    $('supervised').checked=false;refreshButtons();}
  finally{pollBusy=false;}
}
async function frame(){
  if(frameBusy||!freshPicture()||stopped)return;frameBusy=true;
  const generation=frameGeneration;const request=new AbortController();frameRequest=request;
  try{const r=await fetch('/frame',{headers,cache:'no-store',signal:request.signal});if(r.status!==200)return;
    const blob=await r.blob();if(generation!==frameGeneration||!freshPicture()||stopped)return;
    const next=URL.createObjectURL(blob);const old=frameUrl;frameUrl=next;$('picture').src=next;$('picture').hidden=false;
    $('empty').hidden=true;$('livebadge').hidden=false;if(old)URL.revokeObjectURL(old);
  }catch{}finally{if(frameRequest===request)frameRequest=null;frameBusy=false;}
}
function closeOnLeave(){
  stateGeneration++;clearPicture();$('supervised').checked=false;
  if(state)state={...state,phase:'stopping',has_frame:false};refreshButtons();
  if(!token)return;fetch('/stop',{method:'POST',headers,body:'{}',keepalive:true}).catch(()=>{});
}
window.addEventListener('pagehide',closeOnLeave);
document.addEventListener('visibilitychange',()=>{if(document.hidden)closeOnLeave();else poll();});
if(!token){stopped=true;$('status').textContent='Open the private viewer link printed when it was started.';refreshButtons();}
else{poll();setInterval(poll,1500);setInterval(frame,250);}
