"""Local camera session orchestration; private data stays in memory/pipes.

Requires MOVAhome's existing camera PIN. No PIN reset/set operation is provided.
Every robot write has zero retries. Camera close is attempted even when its
start acknowledgement is lost. This module never starts a cleaning task.
"""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import select
import subprocess
import threading
import time

import mova
from steering import SteeringController, SteeringError

# This module can also be imported outside viewer.main(). Upstream debug and
# timeout messages include private protocol payloads.
logging.getLogger('protocol').disabled = True

HERE = Path(__file__).resolve().parent
CAMERA_SESSION_SECONDS = 300

_VIDEO_PHASES = frozenset(('authenticating','authenticated','auth_failed',
    'querying_encrypted_stream','stream_query_failed','receiving_encrypted_stream','stream_ended','error'))
_VIDEO_ERROR_CODES = frozenset(
    'VIDEO_MEDIA_'+name for name in ('KEY','DECRYPT','CLOSED','FRAME','CODEC','INPUT','URL',
        'OUTPUT','EMPTY','TIMEOUT','BACKPRESSURE','REJECTED','RELAY')) | frozenset(
    'VIDEO_WORKER_'+name for name in ('INPUT_TOO_LARGE','INVALID_INPUT','INVALID_MODE',
        'PRIVATE_PIPE_REQUIRED','ABORTED','INVALID_DURATION','INVALID_CACHE','STREAM_QUERY_FAILED','FAILED'))


def _safe_video_report(raw):
    """Reject arbitrary stderr; retain only fixed enums and bounded numbers."""
    if len(raw)>1024:
        return None
    try:
        value = json.loads(raw)
    except (ValueError,TypeError,RecursionError):
        return None
    if not isinstance(value,dict) or not isinstance(value.get('phase'),str) or value['phase'] not in _VIDEO_PHASES:
        return None
    result = {'phase':value['phase']}
    if value['phase']=='error':
        code = value.get('code')
        result['code'] = code if isinstance(code,str) and code in _VIDEO_ERROR_CODES else 'VIDEO_WORKER_FAILED'
    for key in ('httpStatus','responseCode','videoFrames','audioFrames'):
        number = value.get(key)
        if type(number) is int and 0<=number<=2_147_483_647:
            result[key] = number
    if value.get('endedBy') in ('aborted','duration','peer','unknown'):
        result['endedBy'] = value['endedBy']
    return result


class CameraError(RuntimeError):
    pass


class CloudAccess:
    """Serialize short transactions, releasing the account lock between them."""
    def __init__(self, device=None):
        self.device = device
        self.cloud = None
        self.lock = threading.RLock()

    @contextmanager
    def transaction(self):
        with self.lock, (mova.CONFIG/'session.lock').open('a') as lock:
            deadline = time.monotonic()+8
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic()>=deadline:
                        raise CameraError('Another MOVA operation is holding the login. Check the robot in MOVAhome.')
                    time.sleep(0.05)
            saved = json.loads((mova.CONFIG/'session.json').read_text())
            region = saved.get('region')
            # The short-region mapping and live authentication are validated
            # only for Europe. Do not guess a camera region for other accounts.
            if region != 'eu':
                raise CameraError('Camera support currently requires a European MOVA account.')
            c = self.cloud
            if c is None or c._secondary_key != saved['refresh_token']:
                if c:
                    c._session.close()
                c = mova.DreameVacuumDreameHomeCloudProtocol(None, None, 'mova', region,
                    auth_key=saved['refresh_token'], client_id=mova.load_client_id())
                self.cloud = c
            try:
                if not c._logged_in or not c._key_expire or c._key_expire <= time.time():
                    if not c.login():
                        raise CameraError('MOVA login expired. Sign in privately again.')
                    mova.save_session({'region':region,'refresh_token':c._secondary_key})
                if c._did is None:
                    records = c.get_devices()['page']['records']
                    selected = [r for r in records if str(r['did']) == self.device] if self.device else records
                    if len(selected) != 1:
                        raise CameraError('Select one robot with --device; use the devices command to list them.')
                    r = selected[0]
                    if r['model'] != 'mova.vacuum.r9540h':
                        raise CameraError('Camera and steering support currently covers the MOVA Z60 only.')
                    c._did = str(r['did'])
                    c._model = r['model']
                    c._host = r.get(c._strings[9])
                    c._uid = r[c._strings[8]]
                yield c
            finally:
                if c._logged_in and c._secondary_key and c._secondary_key != saved['refresh_token']:
                    mova.save_session({'region':region,'refresh_token':c._secondary_key})

    def close(self):
        with self.lock:
            if self.cloud:
                self.cloud._session.close()


def action(cloud, session, aiid, piid, params):
    result = cloud.send('action', {'did':str(cloud._did),'siid':10001,'aiid':aiid,
        'in':[{'piid':piid,'value':json.dumps({**params,'session':session},separators=(',',':'))}]}, 0)
    if not isinstance(result, dict) or type(result.get('code')) is not int or result['code'] != 0:
        raise CameraError('Camera command was not acknowledged. Check MOVAhome before retrying.')
    return result


def verify_pin(cloud, session, pin):
    if not isinstance(pin, str) or not re.fullmatch(r'[0-9]{4,16}', pin):
        raise CameraError('Enter the camera PIN already configured in MOVAhome.')
    result = action(cloud, session, 4, 1102, {'oldcode':hashlib.sha256(pin.encode()).hexdigest()})
    values = result.get('out')
    if not isinstance(values, list) or len(values) != 1:
        raise CameraError('Camera PIN verification response was not recognized; no retry was sent.')
    try:
        value = values[0]['value']
        value = json.loads(value) if isinstance(value, str) else value
        code = value.get('code')
    except (KeyError, TypeError, ValueError, AttributeError):
        code = None
    if type(code) is not int or code != 2:
        message = {0:'The camera PIN is not set. Configure it in MOVAhome first.',
            1:'Camera PIN was rejected. No automatic retry was sent.',
            3:'Camera PIN access is locked. Check MOVAhome.'}.get(code,
            'Camera PIN verification was not confirmed. No automatic retry was sent.')
        raise CameraError(message)


class _VideoSteeringCancellation:
    """Combine explicit Stop with video freshness at each steering checkpoint."""
    def __init__(self, manager):
        self.manager = manager

    def is_set(self):
        manager = self.manager
        with manager.lock:
            now = time.monotonic()
            hold = manager._active_hold
            return (manager.motion_cancel.is_set() or manager.cancel.is_set()
                or manager.phase != 'live' or now-manager.frame_at >= 2
                or bool(manager.deadline and now >= manager.deadline)
                or now-manager.last_client > 15
                or bool(hold and (hold['released'] or now >= hold['lease_until']
                                  or now >= hold['hard_deadline'])))

    def wait(self, timeout):
        if self.is_set():
            return True
        # Preserve the requested cadence. Recheck the image/session after the
        # wait, before SteeringController may issue its one scheduled repeat.
        return self.manager.motion_cancel.wait(timeout) or self.is_set()


class CameraSession:
    def __init__(self, access, allow_steering=False):
        self.access = access
        self.allow_steering = allow_steering
        self.lock = threading.RLock()
        self.steer_lock = threading.Lock()
        self.cancel = threading.Event()
        self.motion_cancel = threading.Event()
        self._steering_cancel = _VideoSteeringCancellation(self)
        self.motion_generation = 0
        self._pending_stops = 0
        self.worker = None
        self.phase = 'ready'
        self.message = 'Enter your MOVAhome camera PIN to open a five-minute live view.'
        self.close_reason = None
        self.frame = None
        self.frame_at = 0.0
        self.frame_seq = 0
        self.frame_epoch = 0
        self.last_client = time.monotonic()
        self.camera_stop_acknowledged = None
        self.steering_disabled = False
        self._steering_controller = None
        self._active_hold = None
        self._last_hold_timing = None
        self._video_reports = []
        self._video_exit_codes = {'receiver':None,'decoder':None}
        # Never evict an ID while this viewer can receive delayed requests.
        # A full table fails closed rather than reviving a released gesture.
        self._hold_records = {}
        self.deadline = 0.0

    def status(self, touch=False):
        with self.lock:
            if touch:
                self.last_client = time.monotonic()
            return {'phase':self.phase,'message':self.message,'has_frame':self.frame is not None,
                'frame_seq':self.frame_seq,'frame_epoch':self.frame_epoch,
                'frame_age_ms':int((time.monotonic()-self.frame_at)*1000) if self.frame_at else None,
                'remaining_seconds':max(0, int(self.deadline-time.monotonic())) if self.deadline else 0,
                'steering_allowed':self.allow_steering and not self.steering_disabled,
                'steering_active':self.steer_lock.locked(),
                'steering_timing':dict(self._last_hold_timing) if self._last_hold_timing else None,
                'video_diagnostics':{'worker_reports':[dict(report) for report in self._video_reports],
                    'exit_codes_before_cleanup':dict(self._video_exit_codes)},
                'close_reason':self.close_reason,
                'camera_stop_acknowledged':self.camera_stop_acknowledged}

    def _state(self, phase, message):
        with self.lock:
            self.phase, self.message = phase, message

    def start(self, pin):
        if not isinstance(pin, str) or not re.fullmatch(r'[0-9]{4,16}', pin):
            raise CameraError('Enter the existing numeric camera PIN.')
        with self.lock:
            if self.worker and self.worker.is_alive():
                raise CameraError('A camera session is already starting or running.')
            if self.camera_stop_acknowledged is False:
                raise CameraError('Camera close was not confirmed. Check MOVAhome and restart this viewer.')
            self.cancel.clear()
            self.frame, self.frame_at = None, 0.0
            self.frame_seq = 0
            self.frame_epoch += 1
            self.camera_stop_acknowledged = None
            self.close_reason = None
            self.last_client = time.monotonic()
            self.deadline = 0.0
            self._steering_controller = None
            self._last_hold_timing = None
            self._video_reports = []
            self._video_exit_codes = {'receiver':None,'decoder':None}
            self._state('starting','Signing in to the video service…')
            self.worker = threading.Thread(target=self._run,args=(pin,),daemon=False)
            self.worker.start()

    def stop(self, reason='user'):
        with self.lock:
            if self.close_reason is None:
                self.close_reason = reason
            self.cancel.set()
            self.motion_cancel.set()
            self.motion_generation += 1
            if self.worker and self.worker.is_alive():
                self._state('stopping','Closing the camera…')

    def _check_cancel(self):
        if self.cancel.is_set():
            raise CameraError('Camera opening cancelled.')
        if time.monotonic()-self.last_client > 15:
            self.stop('disconnected')
            raise CameraError('Viewer disconnected; camera session ended.')

    def _authenticate(self, payload):
        p = subprocess.Popen(['node',str(HERE/'video_worker.mjs'),'auth'],stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
        try:
            p.stdin.write(json.dumps(payload).encode())
            p.stdin.close()
            deadline = time.monotonic()+50
            while p.poll() is None:
                self._check_cancel()
                if time.monotonic() >= deadline:
                    raise CameraError('Video login timed out.')
                self.cancel.wait(0.1)
            output = p.stdout.read(65537)
            if p.returncode != 0 or len(output)>65536:
                raise CameraError('Video-service login failed. Camera was not opened.')
            result = json.loads(output)
            if not result.get('ok') or not isinstance(result.get('identity'),dict):
                raise CameraError('Video-service login failed. Camera was not opened.')
            return result['identity']
        finally:
            if p.poll() is None:
                p.terminate()
                try:p.wait(timeout=3)
                except subprocess.TimeoutExpired:p.kill();p.wait()
            for pipe in (p.stdin,p.stdout):
                if pipe and not pipe.closed:pipe.close()

    def _read_video_reports(self, pipe, done):
        # Drain continuously so stderr cannot stall video output. Nonblocking
        # reads plus a short select timeout allow bounded cleanup even when a
        # child leaves its pipe open. Never retain an arbitrary/raw line.
        pending = b''
        discarding = False
        try:
            descriptor = pipe.fileno()
            os.set_blocking(descriptor,False)
            while not done.is_set():
                if not select.select([descriptor],[],[],0.1)[0]:continue
                try:chunk = os.read(descriptor,1024)
                except BlockingIOError:continue
                if not chunk:break
                parts = chunk.split(b'\n')
                for index,part in enumerate(parts):
                    if not discarding:
                        if len(pending)+len(part)>1024:
                            pending,discarding = b'',True
                        else:
                            pending += part
                    if index<len(parts)-1:
                        if not discarding:self._store_video_report(pending)
                        pending,discarding = b'',False
            if pending and not discarding:self._store_video_report(pending)
        except Exception:
            # Process/pipe teardown must never prevent the camera end command.
            pass

    def _store_video_report(self, raw):
        report = _safe_video_report(raw)
        if report is not None:
            with self.lock:
                self._video_reports.append(report)
                del self._video_reports[:-8]

    def _record_video_exits(self, receiver, decoder):
        exited = False
        for name,process in (('receiver',receiver),('decoder',decoder)):
            if process is None:continue
            try:code = process.poll()
            except Exception:continue
            if type(code) is int and -255<=code<=255:
                with self.lock:self._video_exit_codes[name] = code
                exited = True
        return exited

    def _read_frames(self, pipe):
        buffer = bytearray()
        while not self.cancel.is_set():
            chunk = pipe.read1(65536)
            if not chunk:break
            buffer.extend(chunk)
            if len(buffer)>4_000_000:
                self.stop('error')
                break
            while True:
                begin = buffer.find(b'\xff\xd8')
                end = buffer.find(b'\xff\xd9',max(0,begin+2))
                if begin<0 or end<0:break
                with self.lock:
                    self.frame = bytes(buffer[begin:end+2])
                    self.frame_at = time.monotonic()
                    self.frame_seq += 1
                del buffer[:end+2]

    def _run(self, pin):
        attempted = False
        session = None
        processes = []
        reader = None
        report_reader = None
        report_done = threading.Event()
        node = decoder = None
        failure = None
        try:
            path = mova.CONFIG/'video-app.json'
            if not path.is_file() or path.stat().st_mode & 0o077:
                raise CameraError('Run the APK setup first; video-app.json must have owner-only permissions.')
            credentials = json.loads(path.read_text())
            with self.access.transaction() as c:
                result = c._api_call('dreame-user-iot/iotuserbind/device/info',{'did':str(c._did)},0)
                data = result.get('data',{}) if isinstance(result,dict) and result.get('code')==0 else {}
                prop = data.get('property',{})
                prop = json.loads(prop) if isinstance(prop,str) else prop
                if data.get('vendor')!='ali' or not isinstance(prop.get('iotId'),str) or not prop['iotId']:
                    raise CameraError('An Aliyun camera connection could not be verified for this robot.')
                iot_id = prop['iotId']
                result = c._api_call('dreame-smarthome/aliIot/getAuthCodeV3',{},0)
                auth = result.get('data') if isinstance(result,dict) and result.get('code')==0 else None
                if isinstance(auth,dict):auth=auth.get('authCode')
                if not isinstance(auth,str) or not auth:
                    raise CameraError('MOVA video login was not available.')
                if not c._uuid:
                    raise CameraError('The account identity needed for video is unavailable.')
                session = hashlib.md5(f'{c._uuid}_{int(time.time()*1000)}'.encode()).hexdigest()
            identity = self._authenticate({'authCode':auth,'region':'eu',**credentials})
            auth = None
            self._check_cancel()
            self._state('starting','Checking your existing camera PIN…')
            with self.access.transaction() as c:
                verify_pin(c,session,pin)
            pin = None
            self._check_cancel()
            attempted = True
            with self.access.transaction() as c:
                action(c,session,7,11,{})
            initialized = False
            for attempt in range(6):
                self._check_cancel()
                with self.access.transaction() as c:
                    value = mova.Robot(c).read({'video':(10001,11)})['video']
                value = json.loads(value) if isinstance(value,str) else value
                if isinstance(value,dict) and value.get('vendor')=='ali' and type(value.get('initStatus')) is int and value['initStatus']==1:
                    initialized = True
                    break
                if attempt < 5 and self.cancel.wait(6):break
            if not initialized:
                raise CameraError('Camera service did not become ready. Check MOVAhome before retrying.')
            self._check_cancel()
            self._state('starting','Opening the encrypted video stream…')
            # Mark before sending: a lost reply must still trigger close.
            attempted = True
            with self.access.transaction() as c:
                action(c,session,1,1,{'operType':'monitor','operation':'start',
                    'token':'alify','channelId':iot_id,'area':'4'})
            self._check_cancel()
            self.deadline = time.monotonic()+CAMERA_SESSION_SECONDS
            node = subprocess.Popen(['node',str(HERE/'video_worker.mjs'),'stream'],
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            processes.append(node)
            report_reader = threading.Thread(target=self._read_video_reports,args=(node.stderr,report_done),daemon=True)
            report_reader.start()
            node.stdin.write(json.dumps({'identity':identity,'credentials':credentials,'iotId':iot_id,
                'maxDurationMs':CAMERA_SESSION_SECONDS*1000}).encode())
            node.stdin.close()
            decoder = subprocess.Popen(['ffmpeg','-hide_banner','-loglevel','error','-nostdin',
                '-fflags','nobuffer','-flags','low_delay',
                '-f','flv','-i','pipe:0','-an',
                '-vf','fps=10,scale=640:-2','-c:v','mjpeg','-threads','1','-q:v','5','-f','image2pipe','pipe:1'],
                stdin=node.stdout,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
            processes.append(decoder)
            node.stdout.close()
            reader = threading.Thread(target=self._read_frames,args=(decoder.stdout,),daemon=True)
            reader.start()
            started = time.monotonic()
            keepalive = 0
            while time.monotonic()<self.deadline and not self.cancel.wait(0.1):
                self._check_cancel()
                now = time.monotonic()
                if self._record_video_exits(node,decoder):
                    raise CameraError('Video reception stopped. The camera is being closed.')
                if now-(self.frame_at or started)>25:
                    raise CameraError('No recent video frames arrived. The camera is being closed.')
                if self.frame_at:
                    self._state('live','Live view · closes automatically after five minutes')
                if now-keepalive>=8:
                    with self.access.transaction() as c:
                        action(c,session,1,6,{'operType':'keep_alive',
                            'videoStatus':'opened' if self.frame_at else 'preparing'})
                    keepalive = time.monotonic()
        except CameraError as exc:
            failure = str(exc)
        except Exception:
            failure = 'Camera connection failed; private details omitted. Check MOVAhome before retrying.'
        finally:
            pin = None
            self.stop('error' if failure else 'timeout' if self.deadline and
                time.monotonic()>=self.deadline else 'user')
            self._record_video_exits(node,decoder)
            for p in reversed(processes):
                try:
                    if p.poll() is None:p.terminate()
                except Exception:pass
            for p in reversed(processes):
                try:
                    try:p.wait(timeout=3)
                    except subprocess.TimeoutExpired:p.kill();p.wait(timeout=3)
                except Exception:pass
            if reader:reader.join(timeout=1)
            if report_reader:
                # Allow EOF diagnostics to drain, then end even if EOF is lost.
                report_reader.join(timeout=0.3)
                report_done.set()
                report_reader.join(timeout=0.3)
            for p in reversed(processes):
                for pipe in (p.stdin,p.stdout,p.stderr):
                    try:
                        if pipe and not pipe.closed:pipe.close()
                    except Exception:pass
            if attempted:
                try:
                    with self.access.transaction() as c:
                        action(c,session,1,1,{'operType':'monitor','operation':'end'})
                    self.camera_stop_acknowledged = True
                except Exception:
                    self.camera_stop_acknowledged = False
                    self.close_reason = 'error'
                    failure = 'Camera close was not confirmed. Close live video in MOVAhome now.'
            with self.lock:
                self.frame = None
                self.frame_at = 0.0
                self.deadline = 0.0
                closed_message = ('Five-minute live view ended. Camera close acknowledged. No video was saved.'
                    if self.close_reason == 'timeout' else 'Camera close acknowledged. No video was saved.')
                self._state('error' if failure else 'closed', failure or closed_message)

    def steer(self, direction, supervised=False):
        if direction != 'stop':
            with self.lock:
                generation = self.motion_generation
                if not self.allow_steering or self.steering_disabled or supervised is not True:
                    raise CameraError('Enable supervised steering with the robot in sight first.')
                if self._pending_stops:
                    raise CameraError('A stop command is still finishing.')
                if self.phase!='live' or time.monotonic()-self.frame_at>2:
                    raise CameraError('A recent live picture is required before steering.')
        if direction=='stop':
            with self.lock:
                self.motion_generation += 1
                self.motion_cancel.set()
                self._pending_stops += 1
            acquired = self.steer_lock.acquire(timeout=15)
        else:
            acquired = self.steer_lock.acquire(blocking=False)
        if not acquired:
            if direction=='stop':
                with self.lock:self._pending_stops -= 1
            raise CameraError('Movement is still finishing. Stop the robot in MOVAhome if it is moving.')
        try:
            if direction!='stop':
                with self.lock:
                    if self.cancel.is_set() or self.motion_generation != generation:
                        raise CameraError('Movement was cancelled; no movement sent.')
                    self.motion_cancel.clear()
            with self.access.transaction() as c:
                # Repeat the cancellation/frame check after waiting for cloud IO.
                if direction!='stop' and (self.cancel.is_set() or time.monotonic()-self.frame_at>2):
                    raise CameraError('Live view is no longer current; no movement sent.')
                # Preserve MOVAhome's one-time announcement across pulses in
                # this camera session. Account refresh may replace the cloud
                # client, so refresh the robot reference for every transaction.
                with self.lock:
                    if self._steering_controller is None:
                        self._steering_controller = SteeringController(
                            mova.Robot(c),cancel_event=self._steering_cancel,
                            timing_callback=self._record_steering_timing)
                    else:
                        self._steering_controller.robot = mova.Robot(c)
                    controller = self._steering_controller
                return controller.move(direction,250 if direction=='stop' else 1250)
        except SteeringError:
            self.steering_disabled = True
            raise
        finally:
            self.steer_lock.release()
            if direction=='stop':
                with self.lock:self._pending_stops -= 1

    @staticmethod
    def _validate_hold_id(hold_id):
        if not isinstance(hold_id,str) or not re.fullmatch(r'[0-9a-f]{32}',hold_id):
            raise CameraError('Invalid steering gesture identifier.')

    def _record_steering_timing(self, event, at_ms):
        if event not in ('start_dispatched_ms','start_acknowledged_ms',
                         'stop_dispatched_ms','stop_acknowledged_ms') or type(at_ms) is not int:
            return
        with self.lock:
            if self._active_hold is not None:
                self._active_hold['timing'][event] = at_ms

    def _new_hold_record(self, hold_id):
        if len(self._hold_records) >= 256:
            raise CameraError('Gesture limit reached. Restart this private viewer before steering again.')
        record = {'released':False,'done':threading.Event(),'result':None,'error':None,
                  'timing':{'clock':'monotonic_ms','measurement':'command_transport',
                            'hold_received_ms':None,'release_received_ms':None,
                            'start_dispatched_ms':None,'start_acknowledged_ms':None,
                            'stop_dispatched_ms':None,'stop_acknowledged_ms':None,
                            'completed_ms':None}}
        self._hold_records[hold_id] = record
        return record

    def hold(self, direction, supervised=False, hold_id=None):
        received_ms = int(time.monotonic()*1000)
        self._validate_hold_id(hold_id)
        if direction not in ('forward','left','right') or supervised is not True:
            raise CameraError('Hold a direction only while supervising the robot.')
        with self.lock:
            if hold_id in self._hold_records:
                record = self._hold_records[hold_id]
                if record['released'] and record['done'].is_set() and not record['error']:
                    return record['result']
                raise CameraError('This steering gesture has already been used.')
            record = self._new_hold_record(hold_id)
            record['timing']['hold_received_ms'] = received_ms
            try:
                if (not self.allow_steering or self.steering_disabled or self.cancel.is_set()
                        or self._pending_stops
                        or self.phase!='live' or time.monotonic()-self.frame_at>=2):
                    raise CameraError('A supervised, current live picture is required before steering.')
                if not self.steer_lock.acquire(blocking=False):
                    raise CameraError('Another steering command is still finishing.')
            except CameraError as error:
                record.update(released=True,error=str(error))
                record['done'].set()
                raise
            now = time.monotonic()
            record.update(lease_until=now+1.0,hard_deadline=now+5.0)
            self._active_hold = record
            self._last_hold_timing = record['timing']
            self.motion_cancel.clear()
        result = None
        failure = None
        try:
            with self.access.transaction() as cloud:
                # The lease adapter is checked after account locks and again
                # after readiness IO, before each nonzero command.
                with self.lock:
                    if self._steering_controller is None:
                        self._steering_controller = SteeringController(
                            mova.Robot(cloud),cancel_event=self._steering_cancel,
                            timing_callback=self._record_steering_timing)
                    else:
                        self._steering_controller.robot = mova.Robot(cloud)
                    controller = self._steering_controller
                result = controller.hold(direction,5000)
            return result
        except SteeringError as error:
            if (error.start_acknowledged is False or error.repeat_acknowledged is False
                    or error.stop_acknowledged is False):
                self.steering_disabled = True
            failure = str(error)
            raise
        except Exception:
            failure = 'Steering request failed. Check MOVAhome; private details omitted.'
            raise CameraError(failure) from None
        finally:
            with self.lock:
                record['timing']['completed_ms'] = int(time.monotonic()*1000)
                if result is not None:
                    result['steering_timing'] = dict(record['timing'])
                record.update(released=True,result=result,error=failure)
                self._active_hold = None
                self.steer_lock.release()
                record['done'].set()

    def hold_heartbeat(self, hold_id):
        self._validate_hold_id(hold_id)
        with self.lock:
            record = self._hold_records.get(hold_id)
            now = time.monotonic()
            active = bool(record and record is self._active_hold and not record['released']
                          and now < record['lease_until'] and now < record['hard_deadline']
                          and not self.cancel.is_set() and not self.motion_cancel.is_set())
            if active:
                record['lease_until'] = min(now+1.0,record['hard_deadline'])
            return {'accepted':active,'steering_active':active}

    def hold_release(self, hold_id):
        received_ms = int(time.monotonic()*1000)
        self._validate_hold_id(hold_id)
        with self.lock:
            record = self._hold_records.get(hold_id)
            if record is None:
                record = self._new_hold_record(hold_id)
                record.update(released=True,result={'released':True,'stop_required':False,
                                                     'stop_acknowledged':None})
                record['timing']['completed_ms'] = received_ms
                record['done'].set()
            elif record is self._active_hold:
                record['released'] = True
                self.motion_generation += 1
                self.motion_cancel.set()
            if record['timing']['release_received_ms'] is None:
                record['timing']['release_received_ms'] = received_ms
        if not record['done'].wait(15):
            raise CameraError('Steering is still stopping. Check MOVAhome if the robot is moving.')
        if record['error']:
            raise CameraError(record['error'])
        with self.lock:
            record['result']['steering_timing'] = dict(record['timing'])
            return record['result']

    def close(self):
        self.stop()
        if self.worker:self.worker.join(timeout=65)
        # Wait for any already-authorized pulse and its finally-stop to finish.
        with self.steer_lock:
            self.access.close()
