"""Offline gesture lease, ordering, and HTTP routing tests; no robot/session IO."""
from contextlib import contextmanager
from email.message import Message
import io
import json
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'mova-robot/scripts'))
import camera
import mova
import viewer
from steering import SteeringController, SteeringError

READY = {'state':2,'error':0,'battery_percent':60,'charging':2,'activity':0,
         'task_status':0,'wash_station':0,'child_lock':0,'dust_status':0,
         'water_draining':0,'lds_state':1}
HOLD_ID = 'a'*32
OTHER_ID = 'b'*32


class FakeAccess:
    def __init__(self,cloud):
        self.cloud=cloud
        self.enter=None
        self.calls=0

    @contextmanager
    def transaction(self):
        self.calls+=1
        if self.enter:self.enter()
        yield self.cloud


class HoldTests(unittest.TestCase):
    def setUp(self):
        self.now=10.0
        self.cloud=Mock(_did='123',_model='mova.vacuum.r9540h')
        self.access=FakeAccess(self.cloud)
        self.manager=camera.CameraSession(self.access,True)
        self.manager.phase='live'
        self.manager.frame_at=self.now
        self.manager.last_client=self.now
        self.manager.deadline=70.0
        self.robot=Mock(cloud=self.cloud)
        self.robot.read.return_value=dict(READY)
        self.dispatches=[]
        self.waits=[]
        self.heartbeats=True
        self.fresh_frames=True
        self.cancel_at=None
        self.fail_send=None
        self.cloud.send.side_effect=self.send
        for patcher in (
            patch.object(camera.time,'monotonic',side_effect=lambda:self.now),
            patch.object(mova,'Robot',return_value=self.robot),
            patch.object(camera,'SteeringController',side_effect=self.controller),
            patch.object(self.manager.motion_cancel,'wait',side_effect=self.wait),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def controller(self,robot,**kwargs):
        return SteeringController(robot,clock=lambda:self.now,**kwargs)

    def send(self,method,params,retries):
        self.assertEqual((method,retries),('set_properties',0))
        self.dispatches.append((self.now,json.loads(params[0]['value'])))
        return None if len(self.dispatches)==self.fail_send else [{'code':0}]

    def wait(self,duration):
        self.assertLessEqual(duration,0.100000001)
        self.waits.append(duration)
        self.now=round(self.now+duration,9)
        if self.fresh_frames:self.manager.frame_at=self.now
        if self.heartbeats:self.manager.hold_heartbeat(HOLD_ID)
        if self.cancel_at is not None and self.now>=self.cancel_at:
            self.manager.motion_cancel.set()
        return self.manager.motion_cancel.is_set()

    def run_hold(self):
        return self.manager.hold('left',True,HOLD_ID)

    def speeds(self):
        return [payload['spdw'] for _,payload in self.dispatches]

    def test_heartbeat_hold_caps_at_five_seconds_and_serializes_native_repeats(self):
        result=self.run_hold()
        self.assertTrue(result['stop_acknowledged'])
        self.assertEqual(self.speeds(),[45,45,45,45,45,0])
        for actual,expected in zip([at for at,_ in self.dispatches],[10,11,12,13,14,15]):
            self.assertAlmostEqual(actual,expected,places=6)
        self.assertEqual([p['audio'] for _,p in self.dispatches],['true']+['false']*5)
        self.assertFalse(self.manager.steering_disabled)
        self.assertFalse(self.manager.status()['steering_active'])
        self.assertFalse(self.manager.hold_heartbeat(HOLD_ID)['accepted'])

    def test_missing_heartbeat_stops_at_lease_expiry_without_repeat(self):
        self.heartbeats=False
        result=self.run_hold()
        self.assertTrue(result['stop_acknowledged'])
        self.assertEqual(self.speeds(),[45,0])
        self.assertAlmostEqual(self.dispatches[-1][0],11.0)
        self.assertFalse(self.manager.steering_disabled)
        self.assertFalse(self.manager.hold_heartbeat(HOLD_ID)['accepted'])

    def test_normal_release_cancellation_is_successful_and_allows_next_gesture(self):
        self.cancel_at=10.4
        result=self.run_hold()
        self.assertTrue(result['stop_acknowledged'])
        self.assertEqual(self.speeds(),[45,0])
        self.assertFalse(self.manager.steering_disabled)
        self.assertEqual(self.manager.hold_release(HOLD_ID),result)
        self.cancel_at=self.now+0.2
        second=self.manager.hold('right',True,OTHER_ID)
        self.assertTrue(second['stop_acknowledged'])
        self.assertEqual(self.speeds(),[45,0,-45,0])
        self.assertEqual(self.dispatches[-2][1]['audio'],'false')

    def test_stale_frame_cancels_hold_even_with_heartbeats(self):
        self.fresh_frames=False
        result=self.run_hold()
        self.assertTrue(result['stop_acknowledged'])
        self.assertEqual(self.speeds(),[45,45,0])
        self.assertLessEqual(self.dispatches[-1][0],12.1)

    def test_lease_expiring_during_readiness_sends_only_zero(self):
        def read(props):
            self.now+=1.1
            return dict(READY)
        self.robot.read.side_effect=read
        result=self.run_hold()
        self.assertIsNone(result['start_acknowledged'])
        self.assertTrue(result['stop_acknowledged'])
        self.assertEqual(self.speeds(),[0])

    def test_lease_expiring_while_waiting_for_account_lock_never_starts(self):
        self.access.enter=lambda:setattr(self,'now',self.now+1.1)
        result=self.run_hold()
        self.assertIsNone(result['start_acknowledged'])
        self.robot.read.assert_not_called()
        self.assertEqual(self.speeds(),[0])

    def test_unknown_repeat_ack_stops_once_and_disables_future_motion(self):
        self.fail_send=2
        with self.assertRaises(SteeringError) as error:self.run_hold()
        self.assertTrue(error.exception.start_acknowledged)
        self.assertFalse(error.exception.repeat_acknowledged)
        self.assertTrue(error.exception.stop_acknowledged)
        self.assertEqual(self.speeds(),[45,45,0])
        self.assertTrue(self.manager.steering_disabled)
        with self.assertRaises(camera.CameraError):self.manager.hold('right',True,OTHER_ID)
        self.assertEqual(self.speeds(),[45,45,0])

    def test_unknown_first_ack_never_repeats(self):
        self.fail_send=1
        with self.assertRaises(SteeringError):self.run_hold()
        self.assertEqual(self.speeds(),[45,0])
        self.assertEqual(self.waits,[])

    def test_release_before_start_and_late_heartbeat_cannot_create_motion(self):
        released=self.manager.hold_release(HOLD_ID)
        self.assertFalse(released['stop_required'])
        self.assertIsNone(released['stop_acknowledged'])
        self.assertFalse(self.manager.hold_heartbeat(HOLD_ID)['accepted'])
        self.assertEqual(self.run_hold(),released)
        self.assertEqual(self.access.calls,0)
        self.assertEqual(self.dispatches,[])

    def test_reused_completed_id_does_not_start_again(self):
        self.cancel_at=10.2
        first=self.run_hold()
        self.assertEqual(self.run_hold(),first)
        self.assertEqual(self.speeds(),[45,0])

    def test_tombstone_survives_camera_reopen_and_storage_never_evicts(self):
        self.manager.hold_release(HOLD_ID)
        with patch.object(camera.threading,'Thread'):
            self.manager.start('1234')
        self.manager.phase='live'
        self.manager.frame_at=self.now
        self.assertFalse(self.run_hold()['stop_required'])
        for number in range(255):self.manager.hold_release(f'{number:032x}')
        with self.assertRaises(camera.CameraError):self.manager.hold_release(OTHER_ID)
        self.assertFalse(self.run_hold()['stop_required'])
        self.assertEqual(self.dispatches,[])

    def test_concurrent_legacy_pulse_prevents_hold_and_tombstones_rejected_id(self):
        self.manager.steer_lock.acquire()
        try:
            with self.assertRaises(camera.CameraError):self.run_hold()
        finally:self.manager.steer_lock.release()
        with self.assertRaises(camera.CameraError):self.run_hold()
        self.assertEqual(self.access.calls,0)

    def test_invalid_id_or_supervision_never_reaches_cloud(self):
        for hold_id in (None,'','A'*32,'a'*31,'a'*33,123):
            with self.subTest(hold_id=hold_id),self.assertRaises(camera.CameraError):
                self.manager.hold('left',True,hold_id)
        for supervised in (False,1,'true'):
            with self.subTest(supervised=supervised),self.assertRaises(camera.CameraError):
                self.manager.hold('left',supervised,HOLD_ID)
        self.assertEqual(self.access.calls,0)

    def test_release_waits_for_actual_stop_result_not_just_cancellation(self):
        entered=threading.Event()
        allow_stop=threading.Event()
        release_finished=threading.Event()
        results=[]
        errors=[]
        def hold(direction,limit):
            entered.set()
            if not allow_stop.wait(1):raise AssertionError('Missing test stop release')
            return {'start_acknowledged':True,'stop_acknowledged':True}
        def do_hold():
            try:results.append(self.run_hold())
            except BaseException as error:errors.append(error)
        def do_release():
            try:results.append(self.manager.hold_release(HOLD_ID))
            except BaseException as error:errors.append(error)
            finally:release_finished.set()
        controller=Mock()
        controller.hold.side_effect=hold
        with patch.object(camera,'SteeringController',return_value=controller):
            holding=threading.Thread(target=do_hold)
            releasing=threading.Thread(target=do_release)
            holding.start()
            self.assertTrue(entered.wait(1))
            releasing.start()
            self.assertFalse(release_finished.wait(0.05))
            self.assertTrue(self.manager.motion_cancel.is_set())
            allow_stop.set()
            holding.join(1)
            releasing.join(1)
        self.assertFalse(holding.is_alive() or releasing.is_alive())
        self.assertEqual(errors,[])
        self.assertEqual(len(results),2)
        self.assertTrue(all(r['stop_acknowledged'] for r in results))

    def test_pending_global_stop_blocks_new_hold_admission(self):
        entered=threading.Event()
        finish=threading.Event()
        errors=[]
        gate=Mock()
        def acquire(*args,**kwargs):
            if kwargs.get('timeout')==15:
                entered.set()
                finish.wait(1)
                return False
            return True
        gate.acquire.side_effect=acquire
        self.manager.steer_lock=gate
        def stop():
            try:self.manager.steer('stop')
            except camera.CameraError as error:errors.append(error)
        thread=threading.Thread(target=stop)
        thread.start()
        self.assertTrue(entered.wait(1))
        try:
            with self.assertRaises(camera.CameraError):self.run_hold()
            self.assertEqual(self.access.calls,0)
        finally:
            finish.set()
            thread.join(1)
        self.assertEqual(self.manager._pending_stops,0)
        self.assertEqual(len(errors),1)


class HoldHttpTests(unittest.TestCase):
    def setUp(self):
        self.cloud=Mock()
        self.manager=camera.CameraSession(FakeAccess(self.cloud),True)
        self.handler_type=viewer.make_handler(self.manager,'fake-viewer-token')

    def request(self,path,body):
        # Exercise the real handler/auth/body parsing and manager validation
        # using in-memory HTTP streams rather than a listening network socket.
        handler=object.__new__(self.handler_type)
        raw=json.dumps(body).encode()
        handler.server=Mock(server_port=12345)
        handler.command='POST'
        handler.path=path
        handler.headers=Message()
        for key,value in {'Host':'127.0.0.1:12345','Origin':'http://127.0.0.1:12345',
                          'Content-Type':'application/json','Content-Length':str(len(raw)),
                          'X-Viewer-Token':'fake-viewer-token'}.items():handler.headers[key]=value
        handler.rfile=io.BytesIO(raw)
        handler.wfile=io.BytesIO()
        handler.send_response=Mock()
        handler.send_header=Mock()
        handler.end_headers=Mock()
        handler.do_POST()
        return handler.send_response.call_args.args[0],json.loads(handler.wfile.getvalue())

    def test_hold_routes_reject_missing_and_invalid_identifiers(self):
        for route in ('hold','heartbeat','release'):
            for invalid in (None,'bad','A'*32):
                code,_=self.request('/steer/'+route,{'direction':'left','supervised':True,'hold_id':invalid})
                self.assertEqual(code,409)
        self.cloud.send.assert_not_called()

    def test_release_before_hold_http_ordering_never_admits_a_late_start(self):
        code,released=self.request('/steer/release',{'hold_id':HOLD_ID})
        self.assertEqual(code,200)
        self.assertFalse(released['stop_required'])
        code,heartbeat=self.request('/steer/heartbeat',{'hold_id':HOLD_ID})
        self.assertEqual((code,heartbeat['accepted']),(200,False))
        code,result=self.request('/steer/hold',{'direction':'left','supervised':True,'hold_id':HOLD_ID})
        self.assertEqual((code,result),(200,released))
        self.assertEqual(self.manager.access.calls,0)


if __name__=='__main__':unittest.main()
