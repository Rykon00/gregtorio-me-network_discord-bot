import asyncio
import json
import logging
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "presence"))

import websockets  # noqa: E402

from discord_presence import Fatal, Presence, presence_payload  # noqa: E402

HELLO = {"op": 10, "d": {"heartbeat_interval": 60}}  # milliseconds


class FakeGateway:
    """A local stand-in for the Discord Gateway; one scripted behaviour per connection."""

    def __init__(self, *behaviours):
        self.behaviours = list(behaviours)
        self.received = []  # per connection: every message the client sent
        self.close_codes = []
        self.acknowledge = True

    async def __aenter__(self):
        self.server = await websockets.serve(self._handle, "127.0.0.1", 0)
        port = self.server.sockets[0].getsockname()[1]
        self.url = f"ws://127.0.0.1:{port}"
        return self

    async def __aexit__(self, *_):
        self.server.close()
        await self.server.wait_closed()

    async def _handle(self, socket):
        index = len(self.received)
        self.received.append([])
        await socket.send(json.dumps(HELLO))
        try:
            if index < len(self.behaviours):
                await self.behaviours[index](self, socket, index)
            await socket.wait_closed()
        finally:
            self.close_codes.append(socket.close_code)

    async def expect(self, socket, index, op):
        """Read until a message with this op arrives; heartbeats are acknowledged on the way."""
        while True:
            message = json.loads(await socket.recv())
            self.received[index].append(message)
            if message["op"] == 1 and self.acknowledge:
                await socket.send(json.dumps({"op": 11}))
            if message["op"] == op:
                return message

    async def ready(self, socket, sequence=1):
        await socket.send(json.dumps({"op": 0, "t": "READY", "s": sequence, "d": {
            "session_id": "session-1", "resume_gateway_url": self.url, "user": {"username": "test-bot"}}}))


class PresenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)

    def service(self, gateway, **options):
        return Presence("the-token", gateway_url=gateway.url, backoff=(0.01, 0.05), settle=(0, 0), **options)

    async def run_until(self, service, condition, timeout=5):
        task = asyncio.ensure_future(service.run())
        try:
            async with asyncio.timeout(timeout):
                while not condition():
                    if task.done():
                        task.result()
                        self.fail("the service stopped before the condition was met")
                    await asyncio.sleep(0.01)
        finally:
            service.stop()
            await asyncio.wait_for(task, 5)

    async def test_identifies_with_the_status_and_resumes_after_a_reconnect_request(self):
        resumed = []

        async def first(gateway, socket, index):
            identify = await gateway.expect(socket, index, 2)
            self.assertEqual(identify["d"]["token"], "the-token")
            self.assertEqual(identify["d"]["intents"], 0)
            self.assertEqual(identify["d"]["presence"], {
                "since": None, "afk": False, "status": "online",
                "activities": [{"name": "Factorio", "type": 0}]})
            await gateway.ready(socket, sequence=7)
            await gateway.expect(socket, index, 1)          # a heartbeat, acknowledged
            await socket.send(json.dumps({"op": 7}))        # RECONNECT

        async def second(gateway, socket, index):
            resume = await gateway.expect(socket, index, 6)
            resumed.append(resume["d"])
            await socket.send(json.dumps({"op": 0, "t": "RESUMED", "s": 8, "d": {}}))

        with tempfile.TemporaryDirectory() as folder:
            alive = Path(folder) / "alive"
            async with FakeGateway(first, second) as gateway:
                service = self.service(gateway, alive_file=alive)
                await self.run_until(service, lambda: service.sessions == 2)
            self.assertTrue(alive.exists())
        self.assertEqual(resumed, [{"token": "the-token", "session_id": "session-1", "seq": 7}])
        heartbeats = [m for m in gateway.received[0] if m["op"] == 1]
        self.assertEqual(heartbeats[-1]["d"], 7)             # heartbeats carry the last sequence number
        self.assertEqual(gateway.close_codes[0], 4000)       # a resumable close, not 1000

    async def test_invalid_session_identifies_anew(self):
        async def first(gateway, socket, index):
            await gateway.expect(socket, index, 2)
            await gateway.ready(socket)
            await socket.send(json.dumps({"op": 9, "d": False}))

        async def second(gateway, socket, index):
            await gateway.expect(socket, index, 2)
            await gateway.ready(socket)

        async with FakeGateway(first, second) as gateway:
            service = self.service(gateway)
            await self.run_until(service, lambda: service.sessions == 2)
        self.assertEqual([m["op"] for m in gateway.received[1] if m["op"] in (2, 6)], [2])

    async def test_missing_heartbeat_acknowledgements_reconnect(self):
        async def first(gateway, socket, index):
            await gateway.expect(socket, index, 2)
            await gateway.ready(socket)
            gateway.acknowledge = False
            await gateway.expect(socket, index, 1)
            gateway.acknowledge = True

        async def second(gateway, socket, index):
            await gateway.expect(socket, index, 6)
            await socket.send(json.dumps({"op": 0, "t": "RESUMED", "s": 2, "d": {}}))

        async with FakeGateway(first, second) as gateway:
            service = self.service(gateway)
            await self.run_until(service, lambda: service.sessions == 2)
        self.assertEqual(gateway.close_codes[0], 4000)

    async def test_a_timed_out_session_is_not_resumed(self):
        async def first(gateway, socket, index):
            await gateway.expect(socket, index, 2)
            await gateway.ready(socket)
            await socket.close(4009)

        async def second(gateway, socket, index):
            await gateway.expect(socket, index, 2)
            await gateway.ready(socket)

        async with FakeGateway(first, second) as gateway:
            service = self.service(gateway)
            await self.run_until(service, lambda: service.sessions == 2)
        self.assertEqual([m["op"] for m in gateway.received[1] if m["op"] in (2, 6)], [2])

    async def test_a_rejected_token_is_fatal(self):
        async def first(gateway, socket, index):
            await gateway.expect(socket, index, 2)
            await socket.close(4004)

        async with FakeGateway(first) as gateway:
            service = self.service(gateway)
            with self.assertRaises(Fatal) as caught:
                await asyncio.wait_for(service.run(), 5)
        self.assertIn("rejected the bot token", str(caught.exception))
        self.assertEqual(len(gateway.received), 1)           # no second attempt

    async def test_an_unreachable_gateway_is_retried(self):
        async with FakeGateway() as gateway:
            url = gateway.url
        service = Presence("the-token", gateway_url=url, backoff=(0.01, 0.02), settle=(0, 0))
        task = asyncio.ensure_future(service.run())
        await asyncio.sleep(0.2)
        self.assertFalse(task.done())                        # still trying
        service.stop()
        await asyncio.wait_for(task, 5)

    async def test_stop_closes_the_connection_normally(self):
        async def first(gateway, socket, index):
            await gateway.expect(socket, index, 2)
            await gateway.ready(socket)

        async with FakeGateway(first) as gateway:
            service = self.service(gateway)
            await self.run_until(service, lambda: service.sessions == 1)
            await asyncio.sleep(0.05)
        self.assertEqual(gateway.close_codes, [1000])


class PayloadTests(unittest.TestCase):
    def test_activity_kinds(self):
        self.assertEqual(presence_payload("the factory grow", "watching")["activities"],
                         [{"name": "the factory grow", "type": 3}])
        self.assertEqual(presence_payload("Gregtorio & ME Network", "custom")["activities"],
                         [{"name": "Custom Status", "type": 4, "state": "Gregtorio & ME Network"}])
        self.assertEqual(presence_payload("", "playing")["activities"], [])
        self.assertEqual(presence_payload("x", "PLAYING")["status"], "online")

    def test_unknown_kind(self):
        with self.assertRaises(Fatal):
            presence_payload("x", "sleeping")


if __name__ == "__main__":
    unittest.main()
