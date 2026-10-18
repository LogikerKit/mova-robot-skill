"""Offline camera lifecycle tests: fake cloud, fake processes, temporary config."""
from contextlib import contextmanager
import fcntl
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mova-robot/scripts"))
import camera
import mova
from steering import SteeringController, SteeringError


class FakeAccess:
    def __init__(self, cloud):
        self.cloud = cloud
        self.enter = None
        self.transactions = 0

    @contextmanager
    def transaction(self):
        self.transactions += 1
        if self.enter:
            self.enter()
        yield self.cloud


class CameraTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="mova-camera-offline-")
        self.addCleanup(directory.cleanup)
        self.config = Path(directory.name)
        patcher = patch.object(mova, "CONFIG", self.config)
        patcher.start()
        self.addCleanup(patcher.stop)
        readiness = patch.object(mova.Robot, "read", return_value={"video": {"vendor": "ali", "initStatus": 1}})
        self.read_video = readiness.start()
        self.addCleanup(readiness.stop)
        mova.save_session({"region": "eu", "refresh_token": "FAKE-PRIVATE-REFRESH"})
        app = self.config / "video-app.json"
        app.write_text(json.dumps({"appKey": "fake-app-key", "appSecret": "FAKE-PRIVATE-APP-SECRET"}))
        app.chmod(0o600)
        self.cloud = Mock()
        self.cloud._did = "123"
        self.cloud._uuid = "FAKE-PRIVATE-ACCOUNT-UUID"
        self.cloud._model = "mova.vacuum.r9540h"
        self.cloud._logged_in = True
        self.cloud._key_expire = time.time() + 3600
        self.cloud._secondary_key = "FAKE-PRIVATE-REFRESH"
        self.cloud._session = Mock()
        self.cloud._api_call.side_effect = [
            {"code": 0, "data": {"vendor": "ali", "property": {"iotId": "FAKE-IOT-DEVICE"}}},
            {"code": 0, "data": "FAKE-PRIVATE-AUTH-CODE"},
        ]
        self.access = FakeAccess(self.cloud)
        self.session = camera.CameraSession(self.access, allow_steering=True)
        self.identity = {"regionId": "eu-central-1", "iotToken": "FAKE-PRIVATE-IOT-TOKEN"}

    @staticmethod
    def successful_ack(method, params, retries):
        if params["aiid"] == 4:
            return {"code": 0, "out": [{"value": '{"code":2}'}]}
        return {"code": 0}

    def operations(self):
        return [(call.args[1]["aiid"], call.args[1]["in"][0]["piid"],
                 json.loads(call.args[1]["in"][0]["value"])) for call in self.cloud.send.call_args_list
                if call.args[0] == "action"]

    def test_action_uses_one_zero_retry_command_and_cannot_override_session(self):
        self.cloud.send.return_value = {"code": 0}
        camera.action(self.cloud, "FAKE-CORRELATION", 1, 1, {"operation": "end", "session": "wrong"})
        self.cloud.send.assert_called_once()
        method, request, retries = self.cloud.send.call_args.args
        self.assertEqual((method, retries), ("action", 0))
        self.assertEqual((request["did"], request["siid"], request["aiid"]), ("123", 10001, 1))
        self.assertEqual(json.loads(request["in"][0]["value"]), {"operation": "end", "session": "FAKE-CORRELATION"})

    def test_action_requires_strict_integer_zero_ack_and_does_not_retry(self):
        for response in (None, [], {}, {"code": False}, {"code": 0.0}, {"code": "0"}, {"code": 1}):
            with self.subTest(response=response):
                self.cloud.send.reset_mock()
                self.cloud.send.return_value = response
                with self.assertRaises(camera.CameraError):
                    camera.action(self.cloud, "session", 1, 1, {})
                self.cloud.send.assert_called_once()
                self.assertEqual(self.cloud.send.call_args.args[2], 0)

    def test_pin_hash_is_sent_once_and_only_strict_code_two_accepts(self):
        for code in (0, 1, 3, True, 2.0, "2", None):
            with self.subTest(code=code):
                self.cloud.send.reset_mock()
                self.cloud.send.return_value = {"code": 0, "out": [{"value": json.dumps({"code": code})}]}
                with self.assertRaises(camera.CameraError):
                    camera.verify_pin(self.cloud, "session", "123456")
                self.cloud.send.assert_called_once()
        self.cloud.send.reset_mock()
        self.cloud.send.return_value = {"code": 0, "out": [{"value": {"code": 2}}]}
        camera.verify_pin(self.cloud, "session", "123456")
        method, request, retries = self.cloud.send.call_args.args
        self.assertEqual((method, request["aiid"], request["in"][0]["piid"], retries), ("action", 4, 1102, 0))
        payload = json.loads(request["in"][0]["value"])
        self.assertEqual(payload["oldcode"], hashlib.sha256(b"123456").hexdigest())
        self.assertNotIn("123456", str(request))
        self.assertNotIn("newcode", payload)

    def test_invalid_pin_never_contacts_robot(self):
        for pin in (None, True, 1234, "123", "1" * 17, "123a", "1234\n"):
            with self.subTest(pin=pin), self.assertRaises(camera.CameraError):
                camera.verify_pin(self.cloud, "session", pin)
        self.cloud.send.assert_not_called()

    def test_unrecognized_pin_result_does_not_start_camera(self):
        self.cloud.send.return_value = {"code": 0, "out": [{"value": '{"code":1,"secret":"FAKE-PRIVATE"}'}]}
        with patch.object(self.session, "_authenticate", return_value=self.identity), \
                patch.object(camera.subprocess, "Popen") as process:
            self.session._run("1234")
        self.assertEqual(len(self.operations()), 1)
        self.assertEqual(self.operations()[0][:2], (4, 1102))
        self.assertEqual(self.session.phase, "error")
        self.assertIsNone(self.session.camera_stop_acknowledged)
        self.assertNotIn("FAKE-PRIVATE", self.session.message)
        process.assert_not_called()

    def test_unknown_start_ack_still_sends_exactly_one_end(self):
        def respond(method, request, retries):
            value = json.loads(request["in"][0]["value"])
            if value.get("operation") == "start":
                raise TimeoutError("FAKE-PRIVATE-AUTH-CODE response secret")
            return self.successful_ack(method, request, retries)
        self.cloud.send.side_effect = respond
        with patch.object(self.session, "_authenticate", return_value=self.identity), \
                patch.object(camera.subprocess, "Popen") as process:
            self.session._run("1234")
        operations = self.operations()
        self.assertEqual([op[:2] for op in operations], [(4, 1102), (7, 11), (1, 1), (1, 1)])
        self.assertEqual([op[2].get("operation") for op in operations[2:]], ["start", "end"])
        self.assertEqual(operations[2][2]["session"], operations[3][2]["session"])
        self.assertTrue(self.session.camera_stop_acknowledged)
        self.assertNotIn("FAKE-PRIVATE", self.session.message)
        self.assertIsNone(self.session.frame)
        self.assertTrue(all(call.args[2] == 0 for call in self.cloud.send.call_args_list))
        process.assert_not_called()

    def test_cancel_after_start_still_ends_and_does_not_spawn_media(self):
        def respond(method, request, retries):
            if json.loads(request["in"][0]["value"]).get("operation") == "start":
                self.session.stop()
            return self.successful_ack(method, request, retries)
        self.cloud.send.side_effect = respond
        with patch.object(self.session, "_authenticate", return_value=self.identity), \
                patch.object(camera.subprocess, "Popen") as process:
            self.session._run("1234")
        self.assertEqual([op[2].get("operation") for op in self.operations()[2:]], ["start", "end"])
        self.assertTrue(self.session.camera_stop_acknowledged)
        process.assert_not_called()

    def test_unconfirmed_initialization_is_bounded_and_closes_without_monitor_start(self):
        self.cloud.send.side_effect = self.successful_ack
        self.read_video.return_value = {"video": {"vendor": "ali", "initStatus": True}}
        with patch.object(self.session, "_authenticate", return_value=self.identity), \
                patch.object(self.session.cancel, "wait", return_value=False) as wait, \
                patch.object(camera.subprocess, "Popen") as process:
            self.session._run("1234")
        self.assertEqual(self.read_video.call_count, 6)
        self.assertEqual(wait.call_count, 5)
        self.assertEqual([op[:2] for op in self.operations()], [(4, 1102), (7, 11), (1, 1)])
        self.assertEqual(self.operations()[-1][2]["operation"], "end")
        self.assertTrue(self.session.camera_stop_acknowledged)
        process.assert_not_called()

    def test_cancelled_authentication_never_sends_pin_initialization_or_monitor(self):
        def authenticate(payload):
            self.session.cancel.set()
            return self.identity
        with patch.object(self.session, "_authenticate", side_effect=authenticate), \
                patch.object(camera.subprocess, "Popen") as process:
            self.session._run("1234")
        self.cloud.send.assert_not_called()
        process.assert_not_called()
        self.assertIsNone(self.session.camera_stop_acknowledged)

    def test_stop_arriving_during_start_admission_is_not_erased(self):
        entered_clear = threading.Event()
        resume_start = threading.Event()
        stop_started = threading.Event()
        stop_set_cancel = threading.Event()
        original_clear = self.session.cancel.clear
        original_set = self.session.cancel.set
        errors = []
        def delayed_clear():
            entered_clear.set()
            if not resume_start.wait(1):
                raise AssertionError("Test did not release start")
            original_clear()
        def record_stop():
            stop_set_cancel.set()
            original_set()
        def start():
            try:
                self.session.start("1234")
            except BaseException as error:
                errors.append(error)
        def stop():
            stop_started.set()
            self.session.stop()
        start_thread = threading.Thread(target=start)
        stop_thread = threading.Thread(target=stop)
        worker = Mock()
        worker.is_alive.return_value = True
        with patch.object(self.session.cancel, "clear", side_effect=delayed_clear), \
                patch.object(self.session.cancel, "set", side_effect=record_stop), \
                patch.object(camera.threading, "Thread", return_value=worker):
            start_thread.start()
            self.assertTrue(entered_clear.wait(1))
            stop_thread.start()
            self.assertTrue(stop_started.wait(1))
            # The old race sets cancel here while start holds the lock, then
            # start's delayed clear erases it. Serialized Stop waits instead.
            stop_set_cancel.wait(0.05)
            resume_start.set()
            start_thread.join(timeout=1)
            stop_thread.join(timeout=1)
        self.assertFalse(start_thread.is_alive())
        self.assertFalse(stop_thread.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(self.session.cancel.is_set())

    def test_nonprivate_app_credentials_rejected_before_any_cloud_call(self):
        (self.config / "video-app.json").chmod(0o644)
        with patch.object(self.session, "_authenticate") as authenticate, \
                patch.object(camera.subprocess, "Popen") as process:
            self.session._run("1234")
        self.assertEqual(self.session.phase, "error")
        self.assertEqual(self.access.transactions, 0)
        self.cloud.send.assert_not_called()
        authenticate.assert_not_called()
        process.assert_not_called()

    def test_unknown_close_ack_disables_reopening_without_retry(self):
        def respond(method, request, retries):
            operation = json.loads(request["in"][0]["value"]).get("operation")
            if operation == "start":
                self.session.cancel.set()
            if operation == "end":
                return {"code": 5}
            return self.successful_ack(method, request, retries)
        self.cloud.send.side_effect = respond
        with patch.object(self.session, "_authenticate", return_value=self.identity):
            self.session._run("1234")
        self.assertFalse(self.session.camera_stop_acknowledged)
        self.assertEqual(len(self.operations()), 4)
        with patch.object(camera.threading, "Thread") as thread, self.assertRaises(camera.CameraError):
            self.session.start("1234")
        thread.assert_not_called()

    def test_camera_end_survives_subprocess_cleanup_failure(self):
        self.cloud.send.side_effect = self.successful_ack
        node = Mock()
        node.stdin = io.BytesIO()
        node.stdout = io.BytesIO()
        node.poll.return_value = None
        node.terminate.side_effect = ProcessLookupError("Process disappeared during cleanup")
        with patch.object(self.session, "_authenticate", return_value=self.identity), \
                patch.object(camera.subprocess, "Popen", side_effect=[node, OSError("Fake decoder launch failure")]):
            self.session._run("1234")
        self.assertEqual(self.operations()[-1][2]["operation"], "end")
        self.assertTrue(self.session.camera_stop_acknowledged)

    def test_live_session_and_stream_worker_share_five_minute_limit(self):
        self.cloud.send.side_effect = self.successful_ack
        now = [100.0]
        written = []
        node = Mock()
        node.stdin = Mock()
        node.stdin.write.side_effect = lambda value: written.append(json.loads(value))
        node.stdout = io.BytesIO()
        node.poll.return_value = None
        decoder = Mock()
        decoder.stdin = node.stdout
        decoder.stdout = io.BytesIO()
        decoder.poll.return_value = None
        checkpoints = []
        def wait(_):
            now[0] += 1
            # Model a visible client receiving fresh frames and polling status.
            self.session.frame = b'fake-jpeg'
            self.session.frame_at = now[0]
            self.session.status(touch=True)
            if now[0] in (161, 399):
                checkpoints.append(self.session.status())
            return False
        with patch.object(camera.time, 'monotonic', side_effect=lambda: now[0]), \
                patch.object(self.session, '_authenticate', return_value=self.identity), \
                patch.object(camera.subprocess, 'Popen', side_effect=[node, decoder]), \
                patch.object(camera.threading, 'Thread'), \
                patch.object(self.session.cancel, 'wait', side_effect=wait):
            self.session.last_client = now[0]
            self.session._run('1234')
        self.assertEqual(camera.CAMERA_SESSION_SECONDS, 300)
        self.assertEqual(written[0]['maxDurationMs'], 300000)
        self.assertEqual([state['phase'] for state in checkpoints], ['live', 'live'])
        self.assertEqual([state['remaining_seconds'] for state in checkpoints], [239, 1])
        self.assertTrue(all(state['close_reason'] is None for state in checkpoints))
        self.assertEqual(now[0], 400)
        self.assertEqual(self.session.phase, 'closed')
        self.assertEqual(self.session.status()['close_reason'], 'timeout')
        self.assertTrue(self.session.camera_stop_acknowledged)
        self.assertTrue(self.session.cancel.is_set())
        self.assertTrue(self.session.motion_cancel.is_set())
        self.assertIsNone(self.session.frame)
        self.assertEqual([op[2].get('operation') for op in self.operations()
                          if op[2].get('operation')], ['start', 'end'])
        self.assertTrue(all(call.args[2] == 0 for call in self.cloud.send.call_args_list))

    def test_viewer_disconnect_limit_remains_fifteen_seconds(self):
        with patch.object(camera.time, 'monotonic', return_value=100.0):
            self.session.last_client = 85.0
            self.session._check_cancel()
            self.session.last_client = 84.99
            with self.assertRaisesRegex(camera.CameraError, 'Viewer disconnected'):
                self.session._check_cancel()
        self.assertEqual(self.session.status()['close_reason'], 'disconnected')
        self.assertTrue(self.session.motion_cancel.is_set())

    def test_manual_close_reason_resets_only_for_new_camera_session(self):
        self.session.stop()
        self.session.stop('timeout')
        self.assertEqual(self.session.status()['close_reason'], 'user')
        with patch.object(camera.threading, 'Thread'):
            self.session.start('1234')
        self.assertIsNone(self.session.status()['close_reason'])

    def test_cloud_lock_released_and_rotated_token_saved_on_exception(self):
        access = camera.CloudAccess("123")
        access.cloud = self.cloud
        lock_path = self.config / "session.lock"
        with self.assertRaisesRegex(RuntimeError, "fake failure"):
            with access.transaction() as cloud:
                with lock_path.open("a") as probe:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                cloud._secondary_key = "FAKE-PRIVATE-ROTATED"
                raise RuntimeError("fake failure")
        with lock_path.open("a") as probe:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(probe, fcntl.LOCK_UN)
        saved = json.loads((self.config / "session.json").read_text())
        self.assertEqual(saved["refresh_token"], "FAKE-PRIVATE-ROTATED")
        self.assertEqual((self.config / "session.json").stat().st_mode & 0o777, 0o600)
        acquired = []
        def probe_thread_lock():
            got = access.lock.acquire(timeout=0.5)
            acquired.append(got)
            if got:
                access.lock.release()
        thread = threading.Thread(target=probe_thread_lock)
        thread.start()
        thread.join(timeout=1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(acquired, [True])

    def ready_for_steering(self):
        self.session.phase = "live"
        self.session.frame_at = time.monotonic()

    def test_steering_requires_explicit_supervision_opt_in_and_recent_live_frame(self):
        cases = [
            {"supervised": False}, {"supervised": 1}, {"supervised": "true"},
            {"allow_steering": False}, {"steering_disabled": True},
            {"phase": "starting"}, {"frame_at": time.monotonic() - 3},
        ]
        with patch.object(camera, "SteeringController") as controller:
            for changes in cases:
                self.ready_for_steering()
                self.session.allow_steering = True
                self.session.steering_disabled = False
                supervised = changes.get("supervised", True)
                for key, value in changes.items():
                    if key != "supervised":
                        setattr(self.session, key, value)
                with self.subTest(changes=changes), self.assertRaises(camera.CameraError):
                    self.session.steer("forward", supervised)
            controller.assert_not_called()
            self.assertEqual(self.access.transactions, 0)

    def test_steering_rechecks_cancel_and_frame_after_cloud_lock(self):
        with patch.object(camera, "SteeringController") as controller:
            for expire in (lambda: self.session.cancel.set(),
                           lambda: setattr(self.session, "frame_at", time.monotonic() - 3)):
                self.ready_for_steering()
                self.session.cancel.clear()
                self.access.enter = expire
                with self.assertRaises(camera.CameraError):
                    self.session.steer("left", True)
            controller.assert_not_called()
        self.assertTrue(self.session.steer_lock.acquire(blocking=False))
        self.session.steer_lock.release()

    def test_stop_bypasses_readiness_but_other_movements_are_serialized(self):
        self.session.allow_steering = False
        self.session.steering_disabled = True
        self.session.cancel.set()
        with patch.object(camera, "SteeringController") as controller, patch.object(mova, "Robot") as robot:
            expected = {"stop_acknowledged": True}
            controller.return_value.move.return_value = expected
            self.assertEqual(self.session.steer("stop"), expected)
            controller.assert_called_once_with(robot.return_value, cancel_event=self.session._steering_cancel)
            controller.return_value.move.assert_called_once_with("stop", 250)
            self.session.allow_steering = True
            self.session.steering_disabled = False
            self.session.cancel.clear()
            self.ready_for_steering()
            self.session.steer_lock.acquire()
            try:
                with self.assertRaises(camera.CameraError):
                    self.session.steer("forward", True)
            finally:
                self.session.steer_lock.release()
            self.assertEqual(controller.call_count, 1)

    def test_stop_waits_for_inflight_pulse_and_signals_its_cancellation(self):
        self.ready_for_steering()
        moving = threading.Event()
        operations = []
        errors = []
        def move(direction, duration):
            operations.append(direction)
            if direction != "stop":
                moving.set()
                if not self.session.motion_cancel.wait(1):
                    raise AssertionError("Stop did not cancel the pulse")
            return {"stop_acknowledged": True}
        def forward():
            try:
                self.session.steer("forward", True)
            except BaseException as error:
                errors.append(error)
        with patch.object(camera, "SteeringController") as controller:
            controller.return_value.move.side_effect = move
            thread = threading.Thread(target=forward)
            thread.start()
            self.assertTrue(moving.wait(1))
            result = self.session.steer("stop")
            thread.join(timeout=1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(operations, ["forward", "stop"])
        self.assertTrue(result["stop_acknowledged"])

    def test_camera_stop_during_readiness_cannot_be_followed_by_motion(self):
        self.ready_for_steering()
        def readiness(_):
            self.session.stop()
            return {"state": 2, "error": 0, "battery_percent": 60, "charging": 2,
                    "activity": 0, "task_status": 0, "wash_station": 0, "child_lock": 0,
                    "dust_status": 0, "water_draining": 0, "lds_state": 1}
        self.read_video.side_effect = readiness
        with self.assertRaisesRegex(SteeringError, "cancelled; no movement sent"):
            self.session.steer("forward", True)
        self.cloud.send.assert_not_called()

    def test_automatic_camera_failure_cancels_pending_steering_readiness(self):
        self.ready_for_steering()
        self.cloud._api_call.side_effect = TimeoutError("FAKE-PRIVATE-FAILURE")
        def readiness(_):
            # Model the camera worker taking its automatic failure/finally
            # path while another request awaits its readiness response.
            self.session._run("1234")
            return {"state": 2, "error": 0, "battery_percent": 60, "charging": 2,
                    "activity": 0, "task_status": 0, "wash_station": 0, "child_lock": 0,
                    "dust_status": 0, "water_draining": 0, "lds_state": 1}
        self.read_video.side_effect = readiness
        with self.assertRaisesRegex(SteeringError, "cancelled; no movement sent"):
            self.session.steer("forward", True)
        self.cloud.send.assert_not_called()
        self.assertTrue(self.session.cancel.is_set())
        self.assertTrue(self.session.motion_cancel.is_set())

    def test_announcement_persists_across_pulses_and_resets_for_fresh_camera_session(self):
        self.ready_for_steering()
        self.read_video.return_value = {
            "state": 2, "error": 0, "battery_percent": 60, "charging": 2,
            "activity": 0, "task_status": 0, "wash_station": 0, "child_lock": 0,
            "dust_status": 0, "water_draining": 0, "lds_state": 1,
        }
        self.cloud.send.return_value = [{"code": 0}]
        with patch.object(self.session.motion_cancel, "wait", return_value=False):
            self.session.steer("left", True)
            self.session.steer("right", True)
            with patch.object(camera.threading, "Thread") as thread:
                self.session.start("1234")
                thread.return_value.start.assert_called_once_with()
            self.ready_for_steering()
            self.session.steer("left", True)
        payloads = [json.loads(call.args[1][0]["value"]) for call in self.cloud.send.call_args_list]
        self.assertEqual([p["audio"] for p in payloads],
                         ["true", "false", "false", "false", "false", "false", "true", "false", "false"])

    def test_persistent_controller_refreshes_robot_after_cloud_client_replacement(self):
        self.ready_for_steering()
        second_cloud = Mock()
        first_robot, second_robot = Mock(), Mock()
        with patch.object(camera, "SteeringController") as controller, \
                patch.object(mova, "Robot", side_effect=[first_robot, second_robot]) as robot:
            self.session.steer("left", True)
            self.access.cloud = second_cloud
            self.session.steer("right", True)
        controller.assert_called_once_with(first_robot, cancel_event=self.session._steering_cancel)
        self.assertIs(controller.return_value.robot, second_robot)
        self.assertEqual(robot.call_args_list[0].args, (self.cloud,))
        self.assertEqual(robot.call_args_list[1].args, (second_cloud,))
        self.assertEqual([call.args for call in controller.return_value.move.call_args_list],
                         [("left", 1250), ("right", 1250)])

    def run_video_hold(self, interruption=None):
        now = [10.0]
        self.session.phase = "live"
        self.session.frame_at = now[0]
        self.session.last_client = now[0]
        self.session.deadline = 70.0
        self.read_video.return_value = {
            "state": 2, "error": 0, "battery_percent": 60, "charging": 2,
            "activity": 0, "task_status": 0, "wash_station": 0, "child_lock": 0,
            "dust_status": 0, "water_draining": 0, "lds_state": 1,
        }
        dispatches = []
        def send(method, params, retry_count):
            dispatches.append((now[0], json.loads(params[0]["value"]), retry_count))
            return [{"code": 0}]
        self.cloud.send.side_effect = send
        waits = []
        def wait(duration):
            waits.append(duration)
            now[0] += duration
            if interruption == "stale":
                self.session.frame_at = now[0] - 2.01
            elif interruption == "closed":
                self.session.phase = "closed"
            elif interruption == "deadline":
                self.session.deadline = now[0]
            elif interruption == "cancel":
                self.session.stop()
            else:
                self.session.frame_at = now[0]
            return self.session.motion_cancel.is_set()
        def controller(robot, **kwargs):
            return SteeringController(robot, clock=lambda: now[0], **kwargs)
        with patch.object(camera.time, "monotonic", side_effect=lambda: now[0]), \
                patch.object(self.session.motion_cancel, "wait", side_effect=wait), \
                patch.object(camera, "SteeringController", side_effect=controller):
            if interruption:
                with self.assertRaises(SteeringError) as error:
                    self.session.steer("left", True)
                self.assertTrue(error.exception.stop_acknowledged)
                self.assertIsNone(error.exception.repeat_acknowledged)
                result = None
            else:
                result = self.session.steer("left", True)
        self.assertTrue(all(retries == 0 for _, _, retries in dispatches))
        return result, dispatches, waits

    def test_viewer_uses_1250ms_hold_with_one_repeat_and_fresh_video(self):
        result, dispatches, waits = self.run_video_hold()
        self.assertEqual(result["requested_duration_ms"], 1250)
        self.assertEqual([at for at, _, _ in dispatches], [10.0, 11.0, 11.25])
        self.assertEqual([p["spdw"] for _, p, _ in dispatches], [45, 45, 0])
        self.assertEqual([p["audio"] for _, p, _ in dispatches], ["true", "false", "false"])
        self.assertEqual(waits, [1.0, 0.25])

    def test_stale_video_before_repeat_sends_stop_without_second_movement(self):
        _, dispatches, waits = self.run_video_hold("stale")
        self.assertEqual([p["spdw"] for _, p, _ in dispatches], [45, 0])
        self.assertEqual(waits, [1.0])

    def test_camera_phase_end_before_repeat_sends_only_stop(self):
        _, dispatches, waits = self.run_video_hold("closed")
        self.assertEqual([p["spdw"] for _, p, _ in dispatches], [45, 0])
        self.assertEqual(waits, [1.0])

    def test_camera_deadline_before_repeat_sends_only_stop(self):
        _, dispatches, waits = self.run_video_hold("deadline")
        self.assertEqual([p["spdw"] for _, p, _ in dispatches], [45, 0])
        self.assertEqual(waits, [1.0])

    def test_camera_cancel_before_repeat_sends_only_stop(self):
        _, dispatches, waits = self.run_video_hold("cancel")
        self.assertEqual([p["spdw"] for _, p, _ in dispatches], [45, 0])
        self.assertEqual(waits, [1.0])

    def test_unknown_steering_ack_disables_future_movement_and_releases_lock(self):
        self.ready_for_steering()
        failure = SteeringError("Unconfirmed stop", start_acknowledged=True, stop_acknowledged=False)
        with patch.object(camera, "SteeringController") as controller:
            controller.return_value.move.side_effect = failure
            with self.assertRaises(SteeringError):
                self.session.steer("forward", True)
            self.assertTrue(self.session.steering_disabled)
            with self.assertRaises(camera.CameraError):
                self.session.steer("left", True)
            self.assertEqual(controller.return_value.move.call_count, 1)
        self.assertTrue(self.session.steer_lock.acquire(blocking=False))
        self.session.steer_lock.release()


if __name__ == "__main__":
    unittest.main()
