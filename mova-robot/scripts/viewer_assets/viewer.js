'use strict';
const $=id=>document.getElementById(id);
const storageKey=`mova.viewer.access:${location.origin}`;
const validToken=value=>typeof value==='string'&&/^[A-Za-z0-9_-]{32,128}$/.test(value);
let token='',accessExpired=false;
try{token=sessionStorage.getItem(storageKey)||'';}catch{}
if(location.hash.length>1){
  token=location.hash.slice(1);
  try{if(validToken(token))sessionStorage.setItem(storageKey,token);else sessionStorage.removeItem(storageKey);}catch{}
}
if(!validToken(token))token='';
history.replaceState(null,'',location.pathname);
let state=null,stateAt=0,stateGeneration=0,pollBusy=false,opening=false,stopBusy=false;
let frameBusy=false,frameUrl=null,frameRequest=null,frameGeneration=0,displayedUntil=0;
let activeHold=null,steeringGeneration=0;
const MAX_HOLD_MS=5000;
const headers=()=>({'X-Viewer-Token':token,'Content-Type':'application/json'});
function accessMessage(){
  return accessExpired?'This viewer link has expired. Open the fresh private link from the running viewer.':
    'Open the private viewer link from the running viewer to connect. Reloading that link in this tab will keep access.';
}
function expireAccess(){
  if(accessExpired)return;
  accessExpired=true;token='';state=null;stateGeneration++;
  try{sessionStorage.removeItem(storageKey);}catch{}
  releaseHold('Viewer access expired. Confirm the robot has stopped in MOVAhome.',false);
  $('supervised').checked=false;clearPicture();
  $('status').textContent=accessMessage();$('empty').textContent='A fresh private link is needed.';
  refreshButtons();
}
async function call(path,data,options={}){
  if(!token)throw Error(accessMessage());
  const response=await fetch(path,{method:data===undefined?'GET':'POST',headers:headers(),
    body:data===undefined?undefined:JSON.stringify(data),cache:'no-store',...options});
  if(response.status===403){expireAccess();throw Error(accessMessage());}
  const result=await response.json();
  if(!response.ok)throw Error(result.error||'Request failed.');
  return result;
}
function clearPicture(){
  frameGeneration++;displayedUntil=0;
  if(frameRequest)frameRequest.abort();
  $('picture').hidden=true;$('picture').removeAttribute('src');$('empty').hidden=false;$('livebadge').hidden=true;
  if(frameUrl){URL.revokeObjectURL(frameUrl);frameUrl=null;}
}
function serverPictureFresh(){
  return !!token&&!document.hidden&&state?.phase==='live'&&state.has_frame&&
    typeof state.frame_age_ms==='number'&&state.frame_age_ms+performance.now()-stateAt<2000;
}
function freshPicture(){
  return serverPictureFresh()&&!$('picture').hidden&&performance.now()<displayedUntil;
}
function prerequisite(){
  if(!token)return accessMessage();
  if(document.hidden)return 'Return to this tab before moving the robot.';
  if(!state)return 'Connecting to the viewer…';
  if(state.phase==='starting'||opening)return 'Wait for the live picture to appear.';
  if(state.phase==='stopping')return 'Wait for the camera to close, then open live view again.';
  if(state.phase!=='live'){
    if(state.close_reason==='timeout')return 'This five-minute camera session ended. Reopen live view to continue.';
    if(state.close_reason==='disconnected')return 'The camera closed after losing its connection. Reopen live view to continue.';
    if(state.phase==='closed'||state.phase==='error')return 'The camera is closed. Reopen live view to continue.';
    return 'Open live view to enable movement.';
  }
  if(!state.steering_allowed)return 'Movement is unavailable in this viewer. Check MOVAhome, then restart the viewer with steering enabled.';
  if(!$('supervised').checked)return 'Tick the box to confirm you can see the robot in person and keep its path clear.';
  if(!freshPicture())return 'Waiting for a fresh picture. Movement stays off until it appears.';
  if(state.steering_active&&!activeHold)return 'Another movement is finishing. Wait for it to stop.';
  return '';
}
function refreshButtons(){
  const reason=prerequisite();
  document.querySelectorAll('[data-direction]').forEach(button=>{
    const ownActive=activeHold?.held&&activeHold.button===button;
    button.disabled=button.dataset.direction==='stop'?stopBusy||!token:
      ownActive?false:!!reason||!!activeHold||stopBusy;
    button.classList.toggle('is-held',!!ownActive);
    button.setAttribute('aria-pressed',ownActive?'true':'false');
  });
  const running=['starting','live','stopping'].includes(state?.phase);
  $('open').disabled=opening||running||!token;$('pin').disabled=opening||running||!token;
  $('close').disabled=!token||(!running&&!opening);
  $('reconnect').hidden=!token||!state||running||opening;
  $('reconnect').textContent=state?.phase==='ready'?'Open live view':'Reopen live view';
  $('steerhint').textContent=reason||(activeHold?.held?'Keep holding to move. Release to stop.':
    activeHold||stopBusy?'Waiting for the stop confirmation…':'Press and hold a direction. Release to stop.');
}
function holdId(){return Array.from(crypto.getRandomValues(new Uint8Array(16)),value=>value.toString(16).padStart(2,'0')).join('');}
function stopInput(hold){
  if(!hold.held)return;
  hold.held=false;clearInterval(hold.heartbeatTimer);clearTimeout(hold.limitTimer);
  if(hold.pointerId!==undefined){
    try{if(hold.button.hasPointerCapture(hold.pointerId))hold.button.releasePointerCapture(hold.pointerId);}catch{}
  }
  refreshButtons();
}
function releaseHold(message='Releasing — waiting for stop confirmation…',send=true){
  const hold=activeHold;
  if(!hold||!hold.held)return hold?.releasePromise;
  hold.releasedAt=performance.now();
  stopInput(hold);
  if(hold.generation===steeringGeneration)$('steerstatus').textContent=message;
  if(send&&token){
    hold.releasePromise=call('/steer/release',{hold_id:hold.id},{keepalive:true})
      .then(result=>{hold.releaseResult=result;recordStopReply(hold,result);return result;})
      .catch(error=>{hold.releaseError=error;return null;});
  }
  return hold.releasePromise;
}
function safeRelease(result){return result?.stop_acknowledged===true||result?.stop_required===false;}
function recordStopReply(hold,result){
  if(result.stop_acknowledged===true&&Number.isFinite(hold.releasedAt)&&hold.ackElapsedMs===undefined){
    hold.ackElapsedMs=Math.max(0,performance.now()-hold.releasedAt);
  }
}
async function heartbeat(hold){
  if(activeHold!==hold||!hold.held||hold.heartbeatBusy)return;
  if(prerequisite()){releaseHold('Live view is no longer ready. Waiting for stop confirmation…');return;}
  hold.heartbeatBusy=true;
  try{
    const result=await call('/steer/heartbeat',{hold_id:hold.id});
    if(result.accepted===false)throw Error('Movement permission ended.');
  }
  catch(error){
    if(activeHold===hold&&hold.held){
      hold.heartbeatError=error;
      releaseHold('Connection lost. Release sent; confirm the robot stops in MOVAhome.');
      $('supervised').checked=false;refreshButtons();
    }
  }finally{hold.heartbeatBusy=false;}
}
async function runHold(hold){
  let message='',fault=false;
  try{
    const result=await call('/steer/hold',{direction:hold.direction,supervised:true,hold_id:hold.id});
    recordStopReply(hold,result);
    stopInput(hold);
    if(result.stop_acknowledged===true)message='Stop acknowledged. Confirm the robot has stopped.';
    else if(result.stop_required===false)message='Movement cancelled before it started.';
    else throw Error('Stop was not confirmed. Stop the robot in MOVAhome.');
    if(hold.heartbeatError){fault=true;message=hold.heartbeatError.message+' Confirm the robot has stopped.';}
  }catch(error){
    const releasedBeforeError=!hold.held;
    if(hold.held)releaseHold('Movement interrupted. Waiting for stop confirmation…');
    if(hold.releasePromise)await hold.releasePromise;
    if(releasedBeforeError&&safeRelease(hold.releaseResult)&&!hold.heartbeatError){
      message=hold.releaseResult.stop_required===false?'Movement cancelled before it started.':
        'Stop acknowledged. Confirm the robot has stopped.';
    }else{fault=true;message=error.message+' Check the robot in MOVAhome before trying again.';}
  }finally{
    stopInput(hold);
    if(hold.releasePromise)await hold.releasePromise;
    if(activeHold===hold)activeHold=null;
    if(hold.generation===steeringGeneration){
      if(fault)$('supervised').checked=false;
      $('steerstatus').textContent=message;
      if(!fault&&Number.isFinite(hold.ackElapsedMs)){
        const elapsed=Math.round(hold.ackElapsedMs);
        $('steertiming').textContent=`Stop reply: ${elapsed} ms after control release. This measures communication, not physical stopping.`;
        $('steertiming').setAttribute('data-stop-reply-ms',String(hold.ackElapsedMs));
      }
    }
    refreshButtons();
  }
}
function startHold(button,input){
  if(prerequisite()||activeHold||stopBusy||button.disabled)return;
  const hold={id:holdId(),direction:button.dataset.direction,button,held:true,
    generation:++steeringGeneration,...input};
  activeHold=hold;
  $('steertiming').textContent='';$('steertiming').removeAttribute('data-stop-reply-ms');
  if(hold.pointerId!==undefined){
    try{button.setPointerCapture(hold.pointerId);}catch{
      activeHold=null;
      $('steerstatus').textContent='This press could not be held. Release and try again, or hold Space or Enter on a direction.';
      refreshButtons();return;
    }
  }
  hold.heartbeatTimer=setInterval(()=>heartbeat(hold),250);
  hold.limitTimer=setTimeout(()=>releaseHold('Five-second limit reached. Waiting for stop confirmation…'),MAX_HOLD_MS);
  $('steerstatus').textContent='Hold the button to move. Release to stop.';refreshButtons();
  runHold(hold);
}
function releasePointer(event){
  if(activeHold?.pointerId===event.pointerId)releaseHold();
}
function releaseKey(event){
  if(activeHold?.key===event.key){event.preventDefault();releaseHold();}
}
document.querySelectorAll('[data-direction]').forEach(button=>{
  if(button.dataset.direction==='stop')return;
  button.addEventListener('pointerdown',event=>{
    if(event.button!==0||event.isPrimary===false)return;
    event.preventDefault();startHold(button,{pointerId:event.pointerId});
  });
  for(const name of ['pointerup','pointercancel','lostpointercapture'])button.addEventListener(name,releasePointer);
  button.addEventListener('keydown',event=>{
    if(event.key!==' '&&event.key!=='Enter')return;
    event.preventDefault();if(!event.repeat)startHold(button,{key:event.key});
  });
  button.addEventListener('keyup',releaseKey);
  button.addEventListener('blur',()=>{if(activeHold?.button===button)releaseHold();});
  button.addEventListener('click',event=>event.preventDefault());
  button.addEventListener('contextmenu',event=>event.preventDefault());
});
window.addEventListener('pointerup',releasePointer);window.addEventListener('pointercancel',releasePointer);
window.addEventListener('keyup',releaseKey);window.addEventListener('blur',()=>releaseHold());
$('supervised').onchange=()=>{if(!$('supervised').checked)releaseHold();refreshButtons();};
$('reconnect').onclick=()=>{
  if($('reconnect').hidden)return;
  $('open').scrollIntoView({behavior:'smooth',block:'center'});$('pin').focus();
};
document.querySelector('[data-direction="stop"]').onclick=async()=>{
  if(stopBusy||!token)return;
  releaseHold();stopBusy=true;const generation=++steeringGeneration;
  $('steertiming').textContent='';$('steertiming').removeAttribute('data-stop-reply-ms');
  $('steerstatus').textContent='Sending stop…';refreshButtons();
  try{
    const result=await call('/steer',{direction:'stop',supervised:$('supervised').checked});
    if(generation===steeringGeneration)$('steerstatus').textContent=result.stop_acknowledged===true?
      'Stop acknowledged. Confirm the robot has stopped.':'Stop not confirmed. Stop the robot in MOVAhome.';
  }catch(error){if(generation===steeringGeneration)$('steerstatus').textContent=error.message;$('supervised').checked=false;}
  finally{stopBusy=false;refreshButtons();}
};
$('open').onclick=async()=>{
  if(!token||opening)return;
  $('steertiming').textContent='';$('steertiming').removeAttribute('data-stop-reply-ms');
  const pin=$('pin').value;$('pin').value='';opening=true;
  const generation=++stateGeneration;const allowed=state?.steering_allowed;
  state={phase:'starting',has_frame:false};clearPicture();$('status').textContent='Opening live view…';refreshButtons();
  try{await call('/start',{pin});if(generation===stateGeneration)await poll();}
  catch(error){
    if(generation===stateGeneration&&token){state={phase:'ready',has_frame:false,steering_allowed:allowed};$('status').textContent=error.message;}
  }finally{opening=false;refreshButtons();}
};
$('close').onclick=async()=>{
  releaseHold();stateGeneration++;clearPicture();$('supervised').checked=false;
  $('empty').textContent='Closing the camera…';if(state)state={...state,phase:'stopping',has_frame:false};refreshButtons();
  try{await call('/stop',{});await poll();}catch(error){$('status').textContent=error.message;}
};
async function poll(){
  if(!token||pollBusy)return;
  pollBusy=true;const generation=stateGeneration;
  try{
    const next=await call('/status');if(!token||generation!==stateGeneration)return;
    state=next;stateAt=performance.now();$('status').textContent=state.message;$('dot').className=state.phase;
    $('countdown').textContent=state.remaining_seconds?`${state.remaining_seconds}s`:'';
    if(!serverPictureFresh()){
      clearPicture();$('empty').textContent=state.phase==='starting'?'Connecting to your robot…':
        state.phase==='stopping'?'Closing the camera…':state.phase==='live'?'Waiting for a fresh picture…':'Your camera is off.';
    }
    guardHold();refreshButtons();
  }catch(error){
    if(!token||generation!==stateGeneration)return;
    state=null;releaseHold('Viewer connection lost. Waiting for stop confirmation…');clearPicture();
    $('status').textContent='Viewer connection lost. Check MOVAhome to confirm the camera is closed.';
    $('empty').textContent='Viewer disconnected.';$('countdown').textContent='';$('dot').className='error';
    $('supervised').checked=false;refreshButtons();
  }finally{pollBusy=false;}
}
async function frame(){
  if(frameBusy||!serverPictureFresh())return;
  frameBusy=true;const generation=frameGeneration;const request=new AbortController();frameRequest=request;
  const now=performance.now();const expires=now+2000-(state.frame_age_ms+now-stateAt);
  let next=null,timer=null;
  try{
    const timeout=new Promise((_,reject)=>{
      timer=setTimeout(()=>{request.abort();reject(Error('Picture timed out'));},2000);
    });
    const response=await Promise.race([fetch('/frame',{headers:headers(),cache:'no-store',signal:request.signal}),timeout]);
    if(response.status===403){expireAccess();return;}
    if(response.status!==200)throw Error('No picture');
    const blob=await Promise.race([response.blob(),timeout]);
    if(generation!==frameGeneration||!serverPictureFresh()||performance.now()>=expires)return;
    next=URL.createObjectURL(blob);const decoded=new Image();decoded.src=next;
    await Promise.race([decoded.decode(),timeout]);
    if(generation!==frameGeneration||!serverPictureFresh()||performance.now()>=expires)return;
    const old=frameUrl;frameUrl=next;next=null;$('picture').src=frameUrl;$('picture').hidden=false;
    displayedUntil=expires;$('empty').hidden=true;$('livebadge').hidden=false;
    if(old)URL.revokeObjectURL(old);refreshButtons();
  }catch{
    if(generation===frameGeneration){clearPicture();guardHold();refreshButtons();}
  }finally{
    if(timer!==null)clearTimeout(timer);if(next)URL.revokeObjectURL(next);
    if(frameRequest===request)frameRequest=null;frameBusy=false;
  }
}
function guardHold(){
  if(activeHold?.held&&prerequisite())releaseHold('Live view is no longer ready. Waiting for stop confirmation…');
}
function closeOnLeave(){
  releaseHold();stateGeneration++;clearPicture();$('supervised').checked=false;
  if(state)state={...state,phase:'stopping',has_frame:false};refreshButtons();
  if(token)call('/stop',{}, {keepalive:true}).catch(()=>{});
}
window.addEventListener('pagehide',closeOnLeave);
document.addEventListener('visibilitychange',()=>{
  stateGeneration++;
  if(document.hidden){
    releaseHold('Movement released while you switch away. Waiting for stop confirmation…');
    clearPicture();refreshButtons();poll();
  }else{
    // A status heartbeat keeps an existing camera session alive while hidden;
    // movement still needs a newly loaded picture when the person returns.
    clearPicture();refreshButtons();poll().then(frame);
  }
});
refreshButtons();
if(!token)$('status').textContent=accessMessage();
else poll();
setInterval(poll,1500);setInterval(frame,100);
setInterval(()=>{
  guardHold();
  if(!freshPicture())$('livebadge').hidden=true;
  refreshButtons();
},100);
