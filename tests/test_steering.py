"""Offline tests for the opt-in steering primitive; never uses a real session."""

import asyncio
import json
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mova-robot/scripts"))
from steering import READINESS_PROPS, SteeringController, SteeringError  # noqa: E402


READY = {
    "state": 2, "error": 0, "battery_percent": 60, "charging": 2,
    "activity": 0, "task_status": 0, "wash_station": 0, "child_lock": 0,
    "dust_status": 0, "water_draining": 0, "lds_state": 1,
}


class FakeClock:
    def __init__(self):
        self.now = 10.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class SteeringTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.cloud = Mock()
        self.cloud._model = "mova.vacuum.r9540h"
        self.cloud._did = "123"
        self.cloud.send.return_value = [{"did": "123", "siid": 4, "piid": 15, "code": 0}]
        self.robot = Mock(cloud=self.cloud)
        self.robot.read.return_value = dict(READY)
        self.random_int = Mock(return_value=42)
        self.controller = SteeringController(
            self.robot, clock=self.clock.monotonic, sleep=self.clock.sleep,
            timestamp=lambda: 1_700_000_000.125, random_int=self.random_int,
        )

    def packets(self):
        packets = []
        for call in self.cloud.send.call_args_list:
            self.assertEqual(call.args[0], "set_properties")
            self.assertEqual(call.args[2], 0, "Motion commands must never enable automatic retries")
            self.assertEqual(len(call.args[1]), 1)
            prop = call.args[1][0]
            self.assertEqual((prop["did"], prop["siid"], prop["piid"]), ("123", 4, 15))
            self.assertIsInstance(prop["value"], str)
            packets.append(json.loads(prop["value"]))
        return packets

    def assert_no_commands(self):
        self.cloud.send.assert_not_called()

    def test_invalid_direction_or_duration_is_rejected_before_read(self):
        cases = [(direction, 250) for direction in ("backward", "turnAround", "", None, True, 1)]
        cases += [("forward", duration) for duration in (True, False, "250", 250.0, None, -1, 0, 99, 1251)]
        cases += [("stop", 99), ("stop", True)]
        for direction, duration in cases:
            with self.subTest(direction=direction, duration=duration), self.assertRaises(SteeringError):
                self.controller.move(direction, duration)
        self.robot.read.assert_not_called()
        self.assert_no_commands()

    def test_unknown_model_is_rejected_even_for_stop(self):
        self.cloud._model = "mova.vacuum.unverified"
        for direction in ("forward", "stop"):
            with self.subTest(direction=direction), self.assertRaises(SteeringError):
                self.controller.move(direction)
        self.robot.read.assert_not_called()
        self.assert_no_commands()

    def test_readiness_requires_known_safe_state(self):
        unsafe = {
            "state": [1, 3, 4, 6, 7, 999], "activity": [1, 13, 999],
            "error": [1, -1], "battery_percent": [14, 101], "charging": [0, 1, 3],
            "task_status": [1], "wash_station": [1], "child_lock": [1],
            "dust_status": [1], "water_draining": [1], "lds_state": [-1, 2, 999],
        }
        for field, values in unsafe.items():
            for value in values:
                self.robot.read.return_value = {**READY, field: value}
                with self.subTest(field=field, value=value), self.assertRaises(SteeringError):
                    self.controller.move("forward")
        self.assert_no_commands()

    def test_readiness_rejects_missing_or_coerced_values(self):
        for field in READY:
            missing = dict(READY)
            del missing[field]
            for reading in (missing, *({**READY, field: value} for value in (None, False, "0", 0.0))):
                self.robot.read.return_value = reading
                with self.subTest(field=field, reading=reading), self.assertRaises(SteeringError):
                    self.controller.move("left")
        self.assert_no_commands()

    def test_readiness_read_failure_never_sends_motion(self):
        self.robot.read.side_effect = RuntimeError("FAKE-PRIVATE-TOKEN")
        with self.assertRaises(SteeringError) as raised:
            self.controller.move("forward")
        self.assertNotIn("FAKE-PRIVATE-TOKEN", str(raised.exception))
        self.assert_no_commands()

    def test_malformed_readiness_response_is_rejected(self):
        for reading in (None, [], "FAKE-PRIVATE-TOKEN", True):
            self.robot.read.return_value = reading
            with self.subTest(reading=reading), self.assertRaises(SteeringError) as raised:
                self.controller.move("forward")
            self.assertNotIn("FAKE-PRIVATE-TOKEN", str(raised.exception))
        self.assert_no_commands()

    def test_allowed_idle_combinations_and_battery_boundary(self):
        for state, activity in ((2, 0), (2, 17), (23, 13)):
            self.robot.read.return_value = {**READY, "state": state, "activity": activity, "battery_percent": 15}
            with self.subTest(state=state, activity=activity):
                result = self.controller.move("forward", 100)
                self.assertTrue(result["start_acknowledged"])
                self.assertTrue(result["stop_acknowledged"])
        self.assertEqual(self.cloud.send.call_count, 6)
        self.robot.read.assert_called_with(READINESS_PROPS)

    def test_stop_bypasses_readiness_and_sends_only_zero_velocity(self):
        self.robot.read.side_effect = AssertionError("Stop must not wait for a status read")
        result = self.controller.move("stop")
        self.robot.read.assert_not_called()
        self.assertEqual(len(self.packets()), 1)
        self.assertEqual(self.packets()[0]["spdv"], 0)
        self.assertEqual(self.packets()[0]["spdw"], 0)
        self.assertEqual(self.packets()[0]["audio"], "false")
        self.assertEqual(self.clock.sleeps, [])
        self.assertEqual(result["requested_duration_ms"], 0)
        self.assertIsNone(result["start_acknowledged"])
        self.assertTrue(result["stop_acknowledged"])

    def test_lowered_sensor_allows_idle_or_remote_without_existing_task(self):
        # Recovered MOVAhome rule permits lowered LDS when no cleaning task is
        # active, including the observed standby state 2 / activity 17.
        for state, activity in ((2, 0), (2, 17), (23, 13)):
            self.cloud.send.reset_mock()
            self.robot.read.return_value = {
                **READY, "state": state, "activity": activity, "lds_state": 0,
            }
            with self.subTest(state=state, activity=activity):
                result = self.controller.move("forward", 100)
                self.assertTrue(result["start_acknowledged"])
                self.assertTrue(result["stop_acknowledged"])
                self.assertEqual(
                    [(p["spdv"], p["spdw"]) for p in self.packets()],
                    [(200, 0), (0, 0)],
                )

    def test_lowered_sensor_does_not_permit_cleaning_or_existing_task(self):
        cases = (
            {"state": 2, "activity": 17, "task_status": 1},
            {"state": 23, "activity": 13, "task_status": 1},
            {"state": 1, "activity": 2, "task_status": 1},
            {"state": 2, "activity": 2, "task_status": 0},
        )
        for status in cases:
            self.robot.read.return_value = {**READY, **status, "lds_state": 0}
            with self.subTest(status=status), self.assertRaises(SteeringError):
                self.controller.move("forward", 100)
        self.assert_no_commands()

    def test_cancel_during_readiness_prevents_any_nonzero_command(self):
        cancel = threading.Event()
        self.controller.cancel_event = cancel
        def readiness(_):
            cancel.set()
            return dict(READY)
        self.robot.read.side_effect = readiness
        with self.assertRaisesRegex(SteeringError, "cancelled; no movement sent") as raised:
            self.controller.move("forward")
        self.assertIsNone(raised.exception.start_acknowledged)
        self.assertIsNone(raised.exception.stop_acknowledged)
        self.assert_no_commands()

    def test_cancel_interrupts_pulse_wait_but_still_stops_exactly_once(self):
        cancel = Mock()
        cancel.is_set.return_value = False
        cancel.wait.return_value = True
        self.controller.cancel_event = cancel
        with self.assertRaises(SteeringError) as raised:
            self.controller.move("left", 750)
        cancel.wait.assert_called_once_with(0.75)
        self.assertEqual(self.clock.sleeps, [])
        self.assertEqual([(p["spdv"], p["spdw"]) for p in self.packets()], [(0, 45), (0, 0)])
        self.assertTrue(raised.exception.start_acknowledged)
        self.assertTrue(raised.exception.stop_acknowledged)

    def test_explicit_stop_ignores_already_set_cancellation(self):
        cancel = threading.Event()
        cancel.set()
        self.controller.cancel_event = cancel
        result = self.controller.move("stop")
        self.assertTrue(result["stop_acknowledged"])
        self.robot.read.assert_not_called()
        self.assertEqual([(p["spdv"], p["spdw"]) for p in self.packets()], [(0, 0)])

    def test_announcement_occurs_only_on_first_movement_even_after_explicit_stop(self):
        self.controller.move("stop")
        self.controller.move("forward")
        self.controller.move("stop")
        self.controller.move("left")
        self.assertEqual([p["audio"] for p in self.packets()],
                         ["false", "true", "false", "false", "false", "false"])

    def test_failed_readiness_does_not_consume_first_announcement(self):
        self.robot.read.return_value = {**READY, "child_lock": 1}
        with self.assertRaises(SteeringError):
            self.controller.move("left")
        self.assert_no_commands()
        self.robot.read.return_value = dict(READY)
        self.controller.move("left")
        self.assertEqual([p["audio"] for p in self.packets()], ["true", "false"])

    def test_unknown_start_ack_does_not_repeat_announcement_on_next_authorized_pulse(self):
        self.cloud.send.side_effect = [None, [{"code": 0}], [{"code": 0}], [{"code": 0}]]
        with self.assertRaises(SteeringError):
            self.controller.move("forward")
        self.controller.move("left")
        self.assertEqual([p["audio"] for p in self.packets()], ["true", "false", "false", "false"])

    def test_normal_move_is_one_start_then_stop_with_precise_acknowledgements(self):
        result = self.controller.move("forward", 250)
        packets = self.packets()
        self.assertEqual(len(packets), 2)
        self.assertEqual((packets[0]["spdv"], packets[0]["spdw"], packets[0]["audio"]), (200, 0, "true"))
        self.assertEqual((packets[1]["spdv"], packets[1]["spdw"], packets[1]["audio"]), (0, 0, "false"))
        self.assertEqual(self.clock.sleeps, [0.25])
        self.assertEqual(result["direction"], "forward")
        self.assertEqual(result["requested_duration_ms"], 250)
        self.assertTrue(result["start_acknowledged"])
        self.assertTrue(result["stop_acknowledged"])
        self.assertTrue(result["experimental"])
        self.assertFalse(result["firmware_deadman_verified"])
        self.assertEqual(set(packets[0]), {"spdv", "spdw", "audio", "random", "timestamp"})
        self.assertEqual(packets[0]["random"], 42)
        self.assertEqual(packets[0]["timestamp"], 1_700_000_000_125)
        self.random_int.assert_called_with(1000)

    def test_turn_directions_have_correct_sign_and_no_forward_velocity(self):
        for direction, angular in (("left", 45), ("right", -45)):
            self.cloud.send.reset_mock()
            with self.subTest(direction=direction):
                self.controller.move(direction, 750)
                packets = self.packets()
                self.assertEqual(len(packets), 2)
                self.assertEqual((packets[0]["spdv"], packets[0]["spdw"]), (0, angular))
                self.assertEqual((packets[1]["spdv"], packets[1]["spdw"]), (0, 0))
        self.assertEqual(self.clock.sleeps, [0.75, 0.75])

    def test_round_trip_time_counts_towards_requested_duration(self):
        def reply(*args):
            if self.cloud.send.call_count == 1:
                self.clock.now += 0.2
            return [{"code": 0}]
        self.cloud.send.side_effect = reply
        self.controller.move("forward", 250)
        self.assertEqual(len(self.clock.sleeps), 1)
        self.assertAlmostEqual(self.clock.sleeps[0], 0.05)
        self.assertEqual(self.cloud.send.call_count, 2)

    def test_1250ms_hold_repeats_once_at_one_second_and_stops_at_fixed_deadline(self):
        times = []
        def acknowledge(*args):
            times.append(self.clock.now)
            return [{"code": 0}]
        self.cloud.send.side_effect = acknowledge
        result = self.controller.move("left", 1250)
        self.assertEqual(times, [10.0, 11.0, 11.25])
        self.assertEqual(self.clock.sleeps, [1.0, 0.25])
        self.assertEqual([(p["spdv"], p["spdw"], p["audio"]) for p in self.packets()],
                         [(0, 45, "true"), (0, 45, "false"), (0, 0, "false")])
        self.assertEqual(result["requested_duration_ms"], 1250)
        self.assertTrue(result["stop_acknowledged"])

    def test_one_second_boundary_has_no_repeat(self):
        self.controller.move("left", 1000)
        self.assertEqual(self.clock.sleeps, [1.0])
        self.assertEqual([(p["spdv"], p["spdw"]) for p in self.packets()], [(0, 45), (0, 0)])

    def test_slow_first_ack_waits_for_ack_but_does_not_extend_deadline(self):
        times = []
        def acknowledge(*args):
            times.append(self.clock.now)
            if len(times) == 1:
                self.clock.now += 1.1
            return [{"code": 0}]
        self.cloud.send.side_effect = acknowledge
        self.controller.move("left", 1250)
        self.assertEqual(times, [10.0, 11.1, 11.25])
        self.assertEqual(len(self.clock.sleeps), 1)
        self.assertAlmostEqual(self.clock.sleeps[0], 0.15)

    def test_first_ack_after_hold_deadline_does_not_repeat(self):
        def acknowledge(*args):
            if self.cloud.send.call_count == 1:
                self.clock.now += 1.3
            return [{"code": 0}]
        self.cloud.send.side_effect = acknowledge
        self.controller.move("left", 1250)
        self.assertEqual(self.clock.sleeps, [])
        self.assertEqual(len(self.packets()), 2)
        self.assertEqual(self.packets()[-1]["spdw"], 0)

    def test_slow_repeat_ack_stops_immediately_without_extending_hold(self):
        times = []
        def acknowledge(*args):
            times.append(self.clock.now)
            if len(times) == 2:
                self.clock.now += 0.4
            return [{"code": 0}]
        self.cloud.send.side_effect = acknowledge
        self.controller.move("left", 1250)
        self.assertEqual(times, [10.0, 11.0, 11.4])
        self.assertEqual(self.clock.sleeps, [1.0])
        self.assertEqual(len(self.packets()), 3)

    def test_cancel_before_repeat_sends_no_additional_movement(self):
        cancel = Mock()
        cancel.is_set.return_value = False
        def cancel_wait(seconds):
            self.clock.now += seconds
            return True
        cancel.wait.side_effect = cancel_wait
        self.controller.cancel_event = cancel
        with self.assertRaises(SteeringError) as raised:
            self.controller.move("left", 1250)
        cancel.wait.assert_called_once_with(1.0)
        self.assertEqual([(p["spdv"], p["spdw"]) for p in self.packets()], [(0, 45), (0, 0)])
        self.assertIsNone(raised.exception.repeat_acknowledged)
        self.assertTrue(raised.exception.stop_acknowledged)

    def test_lost_or_failed_repeat_ack_stops_once_and_reports_uncertainty(self):
        for response in (None, [{"code": 1}], TimeoutError("FAKE-PRIVATE"), KeyboardInterrupt()):
            with self.subTest(response=type(response).__name__):
                self.cloud.send.reset_mock()
                self.clock.now = 10.0
                self.clock.sleeps.clear()
                self.cloud.send.side_effect = [[{"code": 0}], response, [{"code": 0}]]
                with self.assertRaisesRegex(SteeringError, "Repeated movement acknowledgement unknown") as raised:
                    self.controller.move("left", 1250)
                self.assertTrue(raised.exception.start_acknowledged)
                self.assertFalse(raised.exception.repeat_acknowledged)
                self.assertTrue(raised.exception.stop_acknowledged)
                self.assertEqual(self.clock.sleeps, [1.0])
                self.assertEqual([(p["spdv"], p["spdw"]) for p in self.packets()],
                                 [(0, 45), (0, 45), (0, 0)])
                self.assertNotIn("FAKE-PRIVATE", str(raised.exception))

    def test_long_hold_never_repeats_an_unknown_first_ack(self):
        self.cloud.send.side_effect = [None, [{"code": 0}]]
        with self.assertRaises(SteeringError) as raised:
            self.controller.move("left", 1250)
        self.assertFalse(raised.exception.start_acknowledged)
        self.assertIsNone(raised.exception.repeat_acknowledged)
        self.assertEqual(self.clock.sleeps, [])
        self.assertEqual(len(self.packets()), 2)

    def test_late_start_acknowledgement_stops_without_extra_sleep(self):
        def reply(*args):
            if self.cloud.send.call_count == 1:
                self.clock.now += 0.8
            return [{"code": 0}]
        self.cloud.send.side_effect = reply
        self.controller.move("forward", 250)
        self.assertEqual(self.clock.sleeps, [])
        self.assertEqual(self.cloud.send.call_count, 2)

    def test_unconfirmed_start_still_stops_once_and_does_not_sleep(self):
        invalid_acks = [
            None, {}, [], [{}], [{"code": True}], [{"code": 0.0}], [{"code": "0"}],
            [{"code": 1}], [{"code": 0}, {"code": 0}],
            [{"code": 0, "did": "456"}], [{"code": 0, "siid": 5}],
            [{"code": 0, "piid": 16}], [{"code": 0, "siid": "4"}],
        ]
        for ack in invalid_acks:
            self.cloud.send.reset_mock()
            self.cloud.send.side_effect = [ack, [{"code": 0}]]
            with self.subTest(ack=ack), self.assertRaises(SteeringError) as raised:
                self.controller.move("forward")
            self.assertFalse(raised.exception.start_acknowledged)
            self.assertTrue(raised.exception.stop_acknowledged)
            self.assertEqual(len(self.packets()), 2)
            self.assertEqual(self.packets()[-1]["spdv"], 0)
        self.assertEqual(self.clock.sleeps, [])

    def test_start_transport_failure_is_redacted_and_still_attempts_stop(self):
        self.cloud.send.side_effect = [RuntimeError("FAKE-PRIVATE-TOKEN"), [{"code": 0}]]
        with self.assertRaises(SteeringError) as raised:
            self.controller.move("left")
        self.assertFalse(raised.exception.start_acknowledged)
        self.assertTrue(raised.exception.stop_acknowledged)
        self.assertNotIn("FAKE-PRIVATE-TOKEN", str(raised.exception))
        self.assertEqual(len(self.packets()), 2)

    def test_failed_stop_retains_successful_start_and_is_not_retried(self):
        self.cloud.send.side_effect = [[{"code": 0}], RuntimeError("FAKE-PRIVATE-TOKEN")]
        with self.assertRaises(SteeringError) as raised:
            self.controller.move("right")
        self.assertTrue(raised.exception.start_acknowledged)
        self.assertFalse(raised.exception.stop_acknowledged)
        self.assertNotIn("FAKE-PRIVATE-TOKEN", str(raised.exception))
        self.assertEqual(len(self.packets()), 2)

    def test_interruption_during_start_still_attempts_stop(self):
        for interruption in (KeyboardInterrupt("FAKE-PRIVATE-TOKEN"), asyncio.CancelledError("FAKE-PRIVATE-TOKEN")):
            self.cloud.send.reset_mock()
            self.cloud.send.side_effect = [interruption, [{"code": 0}]]
            with self.subTest(interruption=type(interruption)), self.assertRaises(SteeringError) as raised:
                self.controller.move("forward")
            self.assertFalse(raised.exception.start_acknowledged)
            self.assertTrue(raised.exception.stop_acknowledged)
            self.assertNotIn("FAKE-PRIVATE-TOKEN", str(raised.exception))
            self.assertEqual(len(self.packets()), 2)
        self.assertEqual(self.clock.sleeps, [])

    def test_interruption_during_sleep_stops_and_preserves_start_acknowledgement(self):
        for interruption in (RuntimeError("FAKE-PRIVATE-TOKEN"), KeyboardInterrupt("FAKE-PRIVATE-TOKEN"), asyncio.CancelledError("FAKE-PRIVATE-TOKEN")):
            self.cloud.send.reset_mock()
            self.controller.sleep = Mock(side_effect=interruption)
            with self.subTest(interruption=type(interruption)), self.assertRaises(SteeringError) as raised:
                self.controller.move("forward")
            self.assertTrue(raised.exception.start_acknowledged)
            self.assertTrue(raised.exception.stop_acknowledged)
            self.assertNotIn("FAKE-PRIVATE-TOKEN", str(raised.exception))
            self.assertEqual(len(self.packets()), 2)

    def test_explicit_stop_failure_never_sends_movement_or_retries(self):
        self.cloud.send.return_value = [{"code": 1}]
        with self.assertRaises(SteeringError) as raised:
            self.controller.move("stop")
        self.assertIsNone(raised.exception.start_acknowledged)
        self.assertFalse(raised.exception.stop_acknowledged)
        self.robot.read.assert_not_called()
        self.assertEqual(len(self.packets()), 1)
        self.assertEqual((self.packets()[0]["spdv"], self.packets()[0]["spdw"]), (0, 0))

    def test_both_unknown_outcomes_are_reported_without_retry(self):
        self.cloud.send.side_effect = [TimeoutError("private start"), TimeoutError("private stop")]
        with self.assertRaises(SteeringError) as raised:
            self.controller.move("forward")
        self.assertFalse(raised.exception.start_acknowledged)
        self.assertFalse(raised.exception.stop_acknowledged)
        self.assertEqual(len(self.packets()), 2)
        self.assertEqual(self.clock.sleeps, [])


if __name__ == "__main__":
    unittest.main()
