"""CLI routing tests with temporary fake sessions and a mocked robot."""

import io
import json
from pathlib import Path
import signal
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mova-robot/scripts"))
import mova  # noqa: E402
import steering  # noqa: E402


class SteeringCliTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mova-cli-offline-")
        self.addCleanup(self.directory.cleanup)
        self.config = Path(self.directory.name) / "private"
        patcher = patch.object(mova, "CONFIG", self.config)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_opt_in_execution_direction_and_bounds_checked_before_connection(self):
        cases = [
            ["steer"],
            ["steer", "--execute", "--direction", "forward"],
            ["steer", "--experimental-steering", "--direction", "forward"],
            ["steer", "--execute", "--experimental-steering"],
            ["steer", "--execute", "--experimental-steering", "--direction", "backward"],
            ["steer", "--execute", "--direction", "stop"],
        ]
        prefix = ["steer", "--execute", "--experimental-steering", "--direction", "forward"]
        cases += [prefix + ["--duration-ms", value] for value in ("99", "751", "250.0", "true")]
        for args in cases:
            with self.subTest(arguments=args), patch.object(sys, "argv", ["mova.py", *args]), \
                    patch.object(sys, "stderr", io.StringIO()), \
                    patch.object(mova, "DreameVacuumDreameHomeCloudProtocol") as cloud:
                with self.assertRaises(SystemExit) as raised:
                    mova.main()
                self.assertEqual(raised.exception.code, 2)
                cloud.assert_not_called()
                self.assertFalse(self.config.exists())

    def fake_cloud(self, records=None):
        self.config.mkdir(mode=0o700)
        mova.save_session({"region": "eu", "refresh_token": "FAKE-PRIVATE-INITIAL"})
        cloud = Mock()
        cloud._session = Mock()
        cloud._logged_in = True
        cloud._secondary_key = "FAKE-PRIVATE-REFRESHED"
        cloud._strings = [""] * 40
        cloud._strings[8] = "uid"
        cloud._strings[9] = "host"
        cloud._strings[35] = "model"
        cloud.login.return_value = True
        cloud.get_devices.return_value = {"page": {"records": records or [
            {"did": "123", "model": "mova.vacuum.r9540h", "uid": "fake-owner", "host": "fake-host"}
        ]}}
        return cloud

    def invoke(self, cloud, move, extra_args=()):
        args = ["mova.py", "steer", "--execute", "--experimental-steering", "--direction", "left", *extra_args]
        self.output = io.StringIO()
        self.robot = Mock()
        self.previous_signal = object()
        with patch.object(sys, "argv", args), patch.object(sys, "stdout", self.output), \
                patch.object(mova, "DreameVacuumDreameHomeCloudProtocol", return_value=cloud), \
                patch.object(mova, "Robot", return_value=self.robot) as robot_factory, \
                patch.object(steering, "SteeringController") as controller_factory, \
                patch.object(mova.signal, "signal", return_value=self.previous_signal) as signals:
            controller_factory.return_value.move = move
            self.controller_factory = controller_factory
            self.robot_factory = robot_factory
            self.signals = signals
            mova.main()

    def test_authorized_steering_routes_only_to_controller(self):
        cloud = self.fake_cloud()
        expected = {"direction": "left", "requested_duration_ms": 500, "start_acknowledged": True,
                    "stop_acknowledged": True, "experimental": True, "firmware_deadman_verified": False}
        move = Mock(return_value=expected)
        self.invoke(cloud, move, ["--device", "123", "--duration-ms", "500"])
        move.assert_called_once_with("left", 500)
        self.controller_factory.assert_called_once_with(self.robot)
        self.robot.action.assert_not_called()
        self.robot.mop.assert_not_called()
        self.robot.status.assert_not_called()
        self.assertEqual(json.loads(self.output.getvalue()), expected)
        self.assertNotIn("FAKE-PRIVATE", self.output.getvalue())
        self.assertEqual(cloud._did, "123")
        self.assertEqual(cloud._model, "mova.vacuum.r9540h")
        cloud._session.close.assert_called_once_with()
        self.assertEqual(self.signals.call_count, 2)
        self.assertEqual(self.signals.call_args.args, (signal.SIGTERM, self.previous_signal))

    def test_ambiguous_or_unowned_device_never_constructs_controller(self):
        records = [{"did": str(number), "model": "mova.vacuum.r9540h", "uid": "fake-owner"} for number in (123, 456)]
        cloud = self.fake_cloud(records)
        for extra in ([], ["--device", "789"]):
            with self.subTest(arguments=extra), self.assertRaisesRegex(RuntimeError, "Select one robot"):
                self.invoke(cloud, Mock(), extra)
            self.controller_factory.assert_not_called()
            self.robot_factory.assert_not_called()

    def test_steering_failure_restores_signal_and_saves_rotated_token(self):
        cloud = self.fake_cloud()
        failure = steering.SteeringError("Stop acknowledgement unknown", start_acknowledged=True, stop_acknowledged=False)
        def fail(*args):
            cloud._secondary_key = "FAKE-PRIVATE-ROTATED-DURING-STEERING"
            raise failure
        with self.assertRaises(steering.SteeringError) as raised:
            self.invoke(cloud, Mock(side_effect=fail))
        self.assertIs(raised.exception, failure)
        self.assertEqual(self.output.getvalue(), "")
        self.robot.action.assert_not_called()
        cloud._session.close.assert_called_once_with()
        self.assertEqual(self.signals.call_args.args, (signal.SIGTERM, self.previous_signal))
        session = json.loads((self.config / "session.json").read_text())
        self.assertEqual(session["refresh_token"], "FAKE-PRIVATE-ROTATED-DURING-STEERING")
        self.assertEqual((self.config / "session.json").stat().st_mode & 0o777, 0o600)

    def test_sigterm_handler_turns_signal_into_cleanup_path(self):
        cloud = self.fake_cloud()
        def interrupted(*args):
            handler = self.signals.call_args_list[0].args[1]
            handler(signal.SIGTERM, None)
        with self.assertRaises(KeyboardInterrupt):
            self.invoke(cloud, Mock(side_effect=interrupted))
        self.assertEqual(self.signals.call_args_list[1].args, (signal.SIGTERM, signal.SIG_IGN))
        self.assertEqual(self.signals.call_args_list[-1].args, (signal.SIGTERM, self.previous_signal))
        self.assertEqual(self.output.getvalue(), "")
        cloud._session.close.assert_called_once_with()
        self.robot.action.assert_not_called()


if __name__ == "__main__":
    unittest.main()
