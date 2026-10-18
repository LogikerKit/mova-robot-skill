"""Offline bridge tests. Fake sessions and clients never contact a real robot."""

import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server.fastmcp.exceptions import ToolError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from bridge.server import LocalClient, create_server  # noqa: E402


READ_TOOLS = {"get_capabilities", "list_robots", "get_status", "list_rooms"}
CONTROL_TOOLS = {"pause_robot", "stop_robot", "mop_room"}


class ToolContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = Mock()
        self.client.run = AsyncMock(return_value={"ok": True})

    async def test_read_only_default_and_explicit_control_inventory(self):
        for enabled in (False, True):
            with self.subTest(allow_control=enabled):
                server = create_server(allow_control=enabled, client=self.client)
                tools = {tool.name: tool for tool in await server.list_tools()}
                self.assertEqual(set(tools), READ_TOOLS | (CONTROL_TOOLS if enabled else set()))
                for name in READ_TOOLS:
                    self.assertTrue(tools[name].annotations.readOnlyHint)
                    self.assertFalse(tools[name].annotations.destructiveHint)
                for name in CONTROL_TOOLS if enabled else ():
                    self.assertFalse(tools[name].annotations.readOnlyHint)
                    self.assertTrue(tools[name].annotations.destructiveHint)
                    self.assertFalse(tools[name].annotations.idempotentHint)
                result = await server.call_tool("get_capabilities", {})
                capabilities = json.loads(result[0].text)
                self.assertEqual(capabilities["control_enabled"], enabled)
                self.assertEqual(set(capabilities["actions"]), CONTROL_TOOLS if enabled else set())
                self.assertFalse(capabilities["spill_activation"])
        self.client.run.assert_not_awaited()

    async def test_mutations_unavailable_without_opt_in(self):
        server = create_server(client=self.client)
        for name in CONTROL_TOOLS:
            with self.subTest(tool=name), self.assertRaises(ToolError):
                await server.call_tool(name, {"device_id": "123", "execute": True, "room_id": 5})
        self.client.run.assert_not_awaited()

    async def test_execute_gate_precedes_runner_for_every_mutation(self):
        server = create_server(allow_control=True, client=self.client)
        for name in CONTROL_TOOLS:
            args = {"device_id": "123", "execute": False}
            if name == "mop_room":
                args["room_id"] = 5
            with self.subTest(tool=name), self.assertRaisesRegex(ToolError, "execute=true"):
                await server.call_tool(name, args)
        self.client.run.assert_not_awaited()

    async def test_input_types_and_ranges_are_validated_before_runner(self):
        server = create_server(allow_control=True, client=self.client)
        cases = [
            ("get_status", {"device_id": value})
            for value in (123, True, "", "123; echo injected", "--execute", "1" * 33, "１２３")
        ]
        cases.extend(
            ("mop_room", {"device_id": "123", "room_id": value, "execute": True})
            for value in (True, "5", 5.0, 0, 256)
        )
        cases.extend(
            ("pause_robot", {"device_id": "123", "execute": value})
            for value in ("true", 1, None)
        )
        cases.append(("pause_robot", {"device_id": "123"}))
        cases.append(("mop_room", {"device_id": "123", "room_id": 5, "execute": True, "replace_task": "false"}))
        for name, args in cases:
            with self.subTest(tool=name, arguments=args), self.assertRaises(ToolError):
                await server.call_tool(name, args)
        self.client.run.assert_not_awaited()

    async def test_valid_mopping_forwards_explicit_replace_choice(self):
        server = create_server(allow_control=True, client=self.client)
        await server.call_tool("mop_room", {"device_id": "123", "room_id": 5, "execute": True})
        self.client.run.assert_awaited_once_with("mop", device_id="123", room_id=5, replace_task=False)
        self.client.run.reset_mock()
        await server.call_tool("mop_room", {"device_id": "123", "room_id": 5, "execute": True, "replace_task": True})
        self.client.run.assert_awaited_once_with("mop", device_id="123", room_id=5, replace_task=True)


class LocalClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mova-bridge-offline-")
        self.addCleanup(self.directory.cleanup)
        self.config = Path(self.directory.name)
        self.session = self.config / "session.json"
        self.session.write_text('{"region":"eu","refresh_token":"FAKE-OFFLINE-TOKEN"}')
        self.session.chmod(0o600)
        self.client = LocalClient(config_dir=self.config, timeout=0.1)
        self.process = Mock()
        self.process.returncode = 0
        self.process.communicate = AsyncMock(return_value=(b'{"ok":true}', b""))
        self.process.wait = AsyncMock(return_value=0)
        self.spawn = AsyncMock(return_value=self.process)
        patcher = patch("bridge.server.asyncio.create_subprocess_exec", self.spawn)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_fixed_argument_vector_and_private_config_are_used(self):
        result = await self.client.run("mop", device_id="123", room_id=5, replace_task=True)
        self.assertEqual(result, {"ok": True})
        self.spawn.assert_awaited_once()
        call = self.spawn.call_args
        self.assertEqual(call.args, (
            sys.executable, str(ROOT / "mova-robot/scripts/mova.py"), "mop",
            "--device=123", "--execute", "--room", "5", "--replace-task",
        ))
        self.assertNotIn("shell", call.kwargs)
        self.assertEqual(call.kwargs["stdin"], asyncio.subprocess.DEVNULL)
        self.assertEqual(call.kwargs["env"]["MOVA_CONFIG_DIR"], str(self.config))
        self.assertNotIn("FAKE-OFFLINE-TOKEN", repr(call))

    async def test_reads_do_not_include_execute_flag(self):
        for operation in ("devices", "status", "rooms"):
            with self.subTest(operation=operation):
                await self.client.run(operation, device_id="123")
                self.assertNotIn("--execute", self.spawn.call_args.args)
                self.assertNotIn("--replace-task", self.spawn.call_args.args)

    async def test_injected_arguments_and_unknown_operations_never_spawn(self):
        cases = [
            {"operation": "login"},
            {"operation": "status", "device_id": "123; echo injected"},
            {"operation": "status", "device_id": "--execute"},
            {"operation": "status", "device_id": 123},
            {"operation": "mop", "device_id": "123", "room_id": True},
            {"operation": "mop", "device_id": "123", "room_id": "5"},
            {"operation": "mop", "device_id": "123", "room_id": 256},
        ]
        for kwargs in cases:
            with self.subTest(arguments=kwargs), self.assertRaises(ToolError):
                await self.client.run(**kwargs)
        self.spawn.assert_not_awaited()

    async def test_untrusted_stderr_is_redacted_even_with_safe_error_prefix(self):
        self.process.returncode = 1
        for stderr in (
            b"Traceback: refresh_token=FAKE-OFFLINE-TOKEN /private/session.json",
            b"No property response\nrefresh_token=FAKE-OFFLINE-TOKEN",
            b"\xffFAKE-OFFLINE-TOKEN",
        ):
            self.process.communicate.return_value = (b"", stderr)
            with self.subTest(stderr=stderr), self.assertRaises(ToolError) as raised:
                await self.client.run("status", device_id="123")
            message = str(raised.exception)
            self.assertIn("Private details omitted", message)
            self.assertNotIn("FAKE-OFFLINE-TOKEN", message)
            self.assertNotIn("/private/session.json", message)

    async def test_only_exact_safe_error_is_forwarded(self):
        self.process.returncode = 1
        self.process.communicate.return_value = (b"", b"Room is not in current map\n")
        with self.assertRaises(ToolError) as raised:
            await self.client.run("mop", device_id="123", room_id=5)
        self.assertEqual(str(raised.exception), "Room is not in current map")

    async def test_timeout_kills_and_reaps_child_without_retry(self):
        self.process.returncode = None
        self.process.communicate.side_effect = asyncio.TimeoutError
        with self.assertRaisesRegex(ToolError, "outcome may be unknown"):
            await self.client.run("mop", device_id="123", room_id=5)
        self.process.kill.assert_called_once_with()
        self.process.wait.assert_awaited_once_with()
        self.spawn.assert_awaited_once()
        self.process.communicate.assert_awaited_once_with()

    async def test_cancellation_kills_and_reaps_child_without_retry(self):
        self.process.returncode = None
        self.process.communicate.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await self.client.run("pause", device_id="123")
        self.process.kill.assert_called_once_with()
        self.process.wait.assert_awaited_once_with()
        self.spawn.assert_awaited_once()

    async def test_invalid_response_does_not_expose_raw_output(self):
        for stdout in (b"FAKE-OFFLINE-TOKEN", b'"FAKE-OFFLINE-TOKEN"', b"null", b"true", b"123", b"\xff"):
            self.process.communicate.return_value = (stdout, b"")
            with self.subTest(stdout=stdout), self.assertRaises(ToolError) as raised:
                await self.client.run("status", device_id="123")
            self.assertNotIn("FAKE-OFFLINE-TOKEN", str(raised.exception))

    async def test_startup_failure_is_redacted(self):
        self.spawn.side_effect = OSError("FAKE-OFFLINE-TOKEN /private/session.json")
        with self.assertRaises(ToolError) as raised:
            await self.client.run("status", device_id="123")
        self.assertEqual(str(raised.exception), "Could not start the local MOVA client.")

    async def test_group_or_world_access_rejected_before_spawn(self):
        for mode in (0o640, 0o604, 0o666):
            self.session.chmod(mode)
            with self.subTest(mode=oct(mode)), self.assertRaisesRegex(ToolError, "owner-only"):
                await self.client.run("status", device_id="123")
        self.spawn.assert_not_awaited()

    async def test_missing_session_rejected_without_private_path(self):
        self.session.unlink()
        with self.assertRaises(ToolError) as raised:
            await self.client.run("status", device_id="123")
        self.assertNotIn(str(self.config), str(raised.exception))
        self.spawn.assert_not_awaited()

    async def test_symlink_directory_or_other_owner_rejected(self):
        target = self.config / "fake-token.json"
        self.session.rename(target)
        self.session.symlink_to(target)
        with self.assertRaisesRegex(ToolError, "regular, owner-only"):
            await self.client.run("status", device_id="123")
        self.session.unlink()
        self.session.mkdir(mode=0o700)
        with self.assertRaisesRegex(ToolError, "regular, owner-only"):
            await self.client.run("status", device_id="123")
        self.session.rmdir()
        target.rename(self.session)
        with patch("bridge.server.os.getuid", return_value=self.session.stat().st_uid + 1):
            with self.assertRaisesRegex(ToolError, "regular, owner-only"):
                await self.client.run("status", device_id="123")
        self.spawn.assert_not_awaited()


class StdioSmokeTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_mcp_handshake_inventory_and_capabilities_without_login(self):
        # An empty, explicitly selected directory prevents any fallback to the
        # operator's real login, even if a future regression reads a session here.
        with tempfile.TemporaryDirectory(prefix="mova-mcp-empty-") as empty:
            for enabled in (False, True):
                args = [str(ROOT / "bridge/server.py")]
                if enabled:
                    args.append("--allow-control")
                parameters = StdioServerParameters(
                    command=sys.executable, args=args, cwd=ROOT,
                    env={**os.environ, "MOVA_CONFIG_DIR": empty},
                )
                with self.subTest(allow_control=enabled):
                    with anyio.fail_after(15):
                        async with stdio_client(parameters) as (reader, writer):
                            async with ClientSession(reader, writer) as session:
                                initialized = await session.initialize()
                                self.assertEqual(initialized.serverInfo.name, "MOVA Robot")
                                self.assertIsNotNone(initialized.capabilities.tools)
                                inventory = await session.list_tools()
                                self.assertEqual(
                                    {tool.name for tool in inventory.tools},
                                    READ_TOOLS | (CONTROL_TOOLS if enabled else set()),
                                )
                                result = await session.call_tool("get_capabilities", {})
                                self.assertFalse(result.isError)
                                capabilities = json.loads(result.content[0].text)
                                self.assertEqual(capabilities["control_enabled"], enabled)
                                self.assertFalse(capabilities["spill_activation"])
            self.assertEqual(list(Path(empty).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
