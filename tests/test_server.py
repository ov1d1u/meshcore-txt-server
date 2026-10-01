import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from teletext.content import ContentStore
from teletext.protocol import base36, checksum, parse_response
from teletext.server import (
    PortInUseError,
    TeletextServer,
    configure_companion,
    exclusive_port,
    wait_for_disconnect,
)


class FakeEventType:
    OK = "ok"
    ERROR = "error"
    MSG_SENT = "sent"
    NO_MORE_MSGS = "empty"
    CONTACT_MSG_RECV = "contact"
    DISCONNECTED = "disconnected"


class FakeCommands:
    def __init__(self):
        self.frames = []
        self.server = None
        self.ack = True
        self.inbox = []
        self.get_msg_calls = 0

    async def get_msg(self):
        self.get_msg_calls += 1
        if not self.inbox:
            return SimpleNamespace(type=FakeEventType.NO_MORE_MSGS)
        payload = self.inbox.pop(0)
        await self.server._on_message(SimpleNamespace(payload=payload))
        return SimpleNamespace(type=FakeEventType.CONTACT_MSG_RECV, payload=payload)

    async def send_msg(self, sender, frame):
        self.frames.append((sender, frame))
        code = len(self.frames).to_bytes(4, "little")
        if self.ack:
            await self.server._on_ack(SimpleNamespace(payload={"code": code.hex()}))
        return SimpleNamespace(
            type=FakeEventType.MSG_SENT,
            payload={"expected_ack": code, "suggested_timeout": 1000},
        )


class FakeMesh:
    def __init__(self):
        self.commands = FakeCommands()


class ServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "pages").mkdir()
        (self.root / "index.md").write_text("[101](page:101)", encoding="utf-8")
        (self.root / "pages" / "101.md").write_text(
            "# Héllo 🌍\n" * 30 + "".join(f"Entry {i:03d}: {i * i:08d}\n" for i in range(80)),
            encoding="utf-8",
        )
        (self.root / "pages" / "999.md").write_text("hidden", encoding="utf-8")
        self.mesh = FakeMesh()
        self.server = TeletextServer(self.mesh, ContentStore(self.root))
        self.mesh.commands.server = self.server
        self.module = patch.dict("sys.modules", {"meshcore": SimpleNamespace(EventType=FakeEventType)})
        self.module.start()

    async def asyncTearDown(self):
        if self.server._active is not None:
            self.server._active.cancel()
            await asyncio.gather(self.server._active, return_exceptions=True)
        self.module.stop()
        self.temporary.cleanup()

    async def request(self, frame):
        await self.server._on_message(SimpleNamespace(payload={
            "pubkey_prefix": "aabbccddeeff", "text": frame,
        }))
        if self.server._active is not None:
            await self.server._active
        return [parse_response(text) for _, text in self.mesh.commands.frames]

    async def test_index_and_unlisted_page(self):
        index = await self.request("T1IABCD")
        self.assertEqual("".join(x.text for x in index[:-1]), "[101](page:101)")
        self.assertEqual(index[-1].count, len(index) - 1)
        self.assertEqual(index[0].count, len(index) - 1)
        self.mesh.commands.frames.clear()
        (self.root / "pages" / "100.md").write_text("wrong page", encoding="utf-8")
        page_100 = await self.request("T1P1234100")
        self.assertEqual("".join(x.text for x in page_100[:-1]), "[101](page:101)")
        self.mesh.commands.frames.clear()
        page = await self.request("T1P1234999")
        self.assertEqual("".join(x.text for x in page[:-1]), "hidden")

    async def test_multiple_chunks_complete_with_checksum(self):
        frames = await self.request("T1P1234101")
        self.assertGreater(len(frames), 2)
        self.assertTrue(any(text.startswith("T1Z") for _, text in self.mesh.commands.frames))
        self.assertTrue(any(text.startswith("T1D") for _, text in self.mesh.commands.frames))
        body = "".join(frame.text for frame in frames[:-1]).encode("utf-8")
        self.assertEqual(frames[-1].crc32, checksum(body))
        self.assertEqual(frames[0].count, len(frames) - 1)
        self.assertTrue(all(len(text.encode()) <= 100 for _, text in self.mesh.commands.frames))

    async def test_resends_specific_chunk_from_original_snapshot(self):
        original = await self.request("T1P1234101")
        original_frame = self.mesh.commands.frames[1][1]
        plain_index = next(i for i, (_, frame) in enumerate(self.mesh.commands.frames)
                           if frame.startswith("T1D"))
        plain_frame = self.mesh.commands.frames[plain_index][1]
        (self.root / "pages" / "101.md").write_text("edited", encoding="utf-8")
        self.mesh.commands.frames.clear()
        resent = await self.request("T1R1234.1")
        self.assertEqual(resent[-1], original[1])
        self.assertEqual(self.mesh.commands.frames[-1][1], original_frame)
        self.mesh.commands.frames.clear()
        await self.request(f"T1R1234.{base36(plain_index)}")
        self.assertEqual(self.mesh.commands.frames[-1][1], plain_frame)
        self.mesh.commands.frames.clear()
        first_again = await self.request("T1R1234.0")
        self.assertEqual(first_again[-1], original[0])
        self.assertEqual(first_again[-1].count, original[-1].count)
        self.mesh.commands.frames.clear()
        await self.request("T1P5678101")
        self.mesh.commands.frames.clear()
        await self.request("T1R1234.1")
        await asyncio.sleep(0)
        bad = [parse_response(text) for _, text in self.mesh.commands.frames]
        self.assertEqual(bad[0].code, "BAD")

    async def test_missing_page_and_old_cancel(self):
        response = await self.request("T1P1234500")
        self.assertEqual(response[0].code, "NF")
        self.mesh.commands.frames.clear()
        self.mesh.commands.ack = False
        await self.server._on_message(SimpleNamespace(payload={
            "pubkey_prefix": "aabbccddeeff", "text": "T1P1234101",
        }))
        await asyncio.sleep(0)
        active = self.server._active
        await self.server._on_message(SimpleNamespace(payload={
            "pubkey_prefix": "aabbccddeeff", "text": "T1C9999",
        }))
        self.assertIs(self.server._active, active)
        self.assertFalse(active.cancelled())

    async def test_drains_all_startup_messages(self):
        self.mesh.commands.inbox = [
            {"pubkey_prefix": "aabbccddeeff", "text": "other service"},
            {"pubkey_prefix": "aabbccddeeff", "text": "hello"},
        ]
        await self.server._drain_messages()
        self.assertEqual(self.mesh.commands.get_msg_calls, 3)
        self.assertEqual(self.mesh.commands.inbox, [])

    async def test_configures_name_before_flood_advert(self):
        calls = []

        class Commands:
            async def set_name(self, name):
                calls.append(("name", name))
                return SimpleNamespace(type=FakeEventType.OK)

            async def send_advert(self, flood=False):
                calls.append(("advert", flood))
                return SimpleNamespace(type=FakeEventType.OK)

        await configure_companion(SimpleNamespace(commands=Commands()), "Teletext-txt")
        self.assertEqual(calls, [("name", "Teletext-txt"), ("advert", True)])

    async def test_does_not_advertise_if_name_is_rejected(self):
        calls = []

        class Commands:
            async def set_name(self, name):
                calls.append("name")
                return SimpleNamespace(type=FakeEventType.ERROR)

            async def send_advert(self, flood=False):
                calls.append("advert")
                return SimpleNamespace(type=FakeEventType.OK)

        with self.assertRaises(RuntimeError):
            await configure_companion(SimpleNamespace(commands=Commands()), "Teletext-txt")
        self.assertEqual(calls, ["name"])

    async def test_idle_connection_waits_for_real_disconnect(self):
        class DisconnectMesh:
            def __init__(self):
                self.callback = None
                self.unsubscribed = False

            def subscribe(self, event_type, callback):
                self.assert_event_type = event_type
                self.callback = callback
                return callback

            def unsubscribe(self, subscription):
                self.unsubscribed = True
                self.callback = None

        mesh = DisconnectMesh()
        waiting = asyncio.create_task(wait_for_disconnect(mesh))
        await asyncio.sleep(0.02)
        self.assertEqual(mesh.assert_event_type, FakeEventType.DISCONNECTED)
        self.assertFalse(waiting.done())
        self.assertFalse(mesh.unsubscribed)
        event = SimpleNamespace(payload={"reason": "serial_disconnect"})
        mesh.callback(event)
        self.assertIs(await waiting, event)
        self.assertTrue(mesh.unsubscribed)


class SerialPortLockTests(unittest.TestCase):
    def test_second_server_cannot_claim_same_port(self):
        with tempfile.TemporaryDirectory() as directory:
            port = str(Path(directory) / "companion")
            with exclusive_port(port):
                with self.assertRaises(PortInUseError):
                    with exclusive_port(port):
                        pass
            with exclusive_port(port):
                pass


if __name__ == "__main__":
    unittest.main()
