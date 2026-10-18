"""Experimental, supervised steering using presets recovered from MOVAhome.

The requested duration is a client deadline, not a guaranteed movement duration:
cloud requests can block or arrive late. No firmware deadman has been verified.
Only call this module for an authorized action with the robot in sight. It does
not enable camera access or bypass the camera PIN.
"""

import json
import secrets
import time


SUPPORTED_MODEL = "mova.vacuum.r9540h"
READINESS_PROPS = {
    "state": (2, 1),
    "error": (2, 2),
    "battery_percent": (3, 1),
    "charging": (3, 2),
    "activity": (4, 1),
    "task_status": (4, 7),
    "wash_station": (4, 25),
    "child_lock": (4, 27),
    "dust_status": (15, 5),
    "water_draining": (27, 5),
    "lds_state": (28, 4),
}
_PRESETS = {"forward": (200, 0), "left": (0, 45), "right": (0, -45)}


class SteeringError(RuntimeError):
    """A sanitized outcome; acknowledgement does not prove physical stopping."""

    def __init__(self, message, *, start_acknowledged=None, stop_acknowledged=None):
        super().__init__(message)
        self.start_acknowledged = start_acknowledged
        self.stop_acknowledged = stop_acknowledged


class SteeringController:
    def __init__(
        self,
        robot,
        *,
        clock=time.monotonic,
        sleep=time.sleep,
        timestamp=time.time,
        random_int=secrets.randbelow,
        cancel_event=None,
    ):
        self.robot = robot
        self.clock = clock
        self.sleep = sleep
        self.timestamp = timestamp
        self.random_int = random_int
        self.cancel_event = cancel_event

    def _check_readiness(self):
        try:
            state = self.robot.read(READINESS_PROPS)
        except Exception:
            raise SteeringError(
                "Could not verify steering readiness; no movement sent."
            ) from None
        if not isinstance(state, dict) or any(
            type(state.get(name)) is not int for name in READINESS_PROPS
        ):
            raise SteeringError(
                "Steering readiness is incomplete or invalid; no movement sent."
            )
        # Permit an idle robot or another pulse in its existing remote session.
        # Paused tasks, charging, and every unknown state remain excluded.
        if (state["state"], state["activity"]) not in {(2, 0), (2, 17), (23, 13)}:
            raise SteeringError(
                "Robot is not idle or in remote control; no movement sent."
            )
        if state["error"] != 0 or state["task_status"] != 0:
            raise SteeringError(
                "Robot has an error or an existing task; no movement sent."
            )
        if state["charging"] != 2:
            raise SteeringError(
                "Robot is docked or charging state is unknown; no movement sent."
            )
        if not 15 <= state["battery_percent"] <= 100:
            raise SteeringError(
                "Battery level is too low or invalid; no movement sent."
            )
        if any(
            state[name] != 0
            for name in ("wash_station", "dust_status", "water_draining")
        ):
            raise SteeringError(
                "Dock activity is present or unknown; no movement sent."
            )
        if state["child_lock"] != 0:
            raise SteeringError(
                "Child lock is enabled or unknown; no movement sent."
            )
        # MOVAhome permits lowered LDS (0) while idle or remotely controlled;
        # its lowered-LDS restriction applies to an existing cleaning task.
        # The checks above already exclude such tasks. Unknown LDS values do
        # not establish readiness, even when all other readings are safe.
        if state["lds_state"] not in (0, 1):
            raise SteeringError(
                "Navigation sensor state is unknown; no movement sent."
            )

    def _send(self, preset, *, audio):
        cloud = self.robot.cloud
        payload = json.dumps(
            {
                "spdv": preset[0],
                "spdw": preset[1],
                "audio": "true" if audio else "false",
                "random": self.random_int(1000),
                "timestamp": int(self.timestamp() * 1000),
            },
            separators=(",", ":"),
        )
        did = str(cloud._did)
        # Zero retries is essential: a timed-out movement may have executed.
        result = cloud.send(
            "set_properties",
            [{"did": did, "siid": 4, "piid": 15, "value": payload}],
            0,
        )
        if not isinstance(result, list) or len(result) != 1:
            return False
        item = result[0]
        if not isinstance(item, dict) or type(item.get("code")) is not int:
            return False
        if item["code"] != 0:
            return False
        # Some responses omit property identifiers; reject contradictory ones.
        return (
            ("siid" not in item or type(item["siid"]) is int and item["siid"] == 4)
            and ("piid" not in item or type(item["piid"]) is int and item["piid"] == 15)
            and ("did" not in item or str(item["did"]) == did)
        )

    def move(self, direction, duration_ms=250):
        """Request one short pulse, always followed by one explicit stop attempt.

        Stop bypasses readiness checks so it remains available during an error.
        Model and input validation still apply. Never automatically retry a
        movement after any failure, even when the stop was acknowledged.
        """
        if type(direction) is not str or direction not in (*_PRESETS, "stop"):
            raise SteeringError("Direction must be forward, left, right, or stop.")
        if type(duration_ms) is not int or not 100 <= duration_ms <= 750:
            raise SteeringError("Steering duration must be an integer from 100 to 750 ms.")
        if getattr(self.robot.cloud, "_model", None) != SUPPORTED_MODEL:
            raise SteeringError("Steering supports only model mova.vacuum.r9540h.")

        if direction == "stop":
            try:
                acknowledged = self._send((0, 0), audio=False)
            except BaseException:
                acknowledged = False
            if not acknowledged:
                raise SteeringError(
                    "Stop acknowledgement unknown. Robot may still be moving; "
                    "stop it in MOVAhome immediately.",
                    stop_acknowledged=False,
                ) from None
            return self._result(direction, 0, None)

        self._check_readiness()
        # Readiness uses cloud IO. A Stop accepted while that read was pending
        # must prevent a new movement after the response finally arrives.
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise SteeringError("Steering cancelled; no movement sent.")
        deadline = self.clock() + duration_ms / 1000
        start_acknowledged = False
        stop_acknowledged = False
        interrupted = False
        try:
            start_acknowledged = self._send(_PRESETS[direction], audio=True)
            if start_acknowledged:
                remaining = deadline - self.clock()
                if self.cancel_event is not None:
                    interrupted = self.cancel_event.is_set()
                    if remaining > 0 and not interrupted:
                        interrupted = self.cancel_event.wait(remaining)
                elif remaining > 0:
                    self.sleep(remaining)
        except BaseException:
            interrupted = True
        finally:
            # Attempt zero even if the first response is missing, rejected, or
            # interrupted. The robot may have received a request we could not
            # confirm. Do not wait out the pulse after an unknown response.
            try:
                stop_acknowledged = self._send((0, 0), audio=False)
            except BaseException:
                stop_acknowledged = False

        if not stop_acknowledged:
            start = (
                "Movement acknowledged, but stop acknowledgement unknown. "
                if start_acknowledged
                else "Movement acknowledgement unknown; stop acknowledgement also unknown. "
            )
            raise SteeringError(
                start + "Robot may still be moving; stop it in MOVAhome immediately. "
                "Do not retry movement.",
                start_acknowledged=start_acknowledged,
                stop_acknowledged=False,
            ) from None
        if not start_acknowledged or interrupted:
            start = (
                "Movement interrupted. "
                if start_acknowledged
                else "Movement acknowledgement unknown. "
            )
            raise SteeringError(
                start + "Stop acknowledged; verify the robot in MOVAhome before any retry.",
                start_acknowledged=start_acknowledged,
                stop_acknowledged=True,
            ) from None
        return self._result(direction, duration_ms, True)

    @staticmethod
    def _result(direction, duration_ms, start_acknowledged):
        return {
            "direction": direction,
            "requested_duration_ms": duration_ms,
            "start_acknowledged": start_acknowledged,
            "stop_acknowledged": True,
            "experimental": True,
            "firmware_deadman_verified": False,
        }
