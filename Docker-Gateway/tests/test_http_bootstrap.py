import asyncio
import json
import unittest
from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo
from xiaozhi_gateway.config import Config
from xiaozhi_gateway.http_bootstrap import BootstrapServer


CONFIG = Config(
    bind_host="127.0.0.1",
    bootstrap_port=0,
    websocket_port=0,
    public_ws_url="wss://xandria.synology.me:8443/xiaozhi/ws",
    device_token="test-token",
    openai_api_key="test-key",
    openai_model="gpt-realtime-2.1",
    transcription_model="gpt-4o-mini-transcribe",
    voice="marin",
    instructions="test",
    allowed_device_id=None,
    max_turn_seconds=60,
    max_session_seconds=3300,
    timezone_offset_minutes=120,
    log_level="INFO",
    public_compute_url="https://xandria.synology.me:8444",
)


class BootstrapTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        handler = BootstrapServer(CONFIG)
        self.server = await asyncio.start_server(handler.handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        self.server.close()
        await self.server.wait_closed()

    async def request(self, request: bytes):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        writer.write(request)
        await writer.drain()
        response = await reader.read()
        writer.close()
        await writer.wait_closed()
        head, body = response.split(b"\r\n\r\n", 1)
        return head, json.loads(body)

    async def test_ota_bootstrap(self):
        head, body = await self.request(
            b"POST /xiaozhi/ota/ HTTP/1.1\r\nHost: localhost\r\nContent-Length: 2\r\n\r\n{}"
        )
        self.assertIn(b"200 OK", head)
        self.assertEqual(body["websocket"]["version"], 1)
        self.assertEqual(body["websocket"]["token"], "test-token")
        # Europe/Amsterdam is UTC+2 in summer and UTC+1 in winter.
        expected = int(datetime.now(ZoneInfo("Europe/Amsterdam")).utcoffset().total_seconds() // 60)
        self.assertEqual(body["server_time"]["timezone_offset"], expected)
        self.assertGreater(body["server_time"]["timestamp"], 0)
        self.assertNotIn("mqtt", body)

    async def test_ota_tells_a_cloud_device_to_skip_the_compute_check(self):
        _, body = await self.request(
            b"POST /xiaozhi/ota/ HTTP/1.1\r\nHost: localhost\r\nContent-Length: 2\r\n\r\n{}"
        )
        self.assertEqual(
            body["compute"],
            {
                "required": False,
                "status_url": "https://xandria.synology.me:8444/compute/status",
                "wake_url": "https://xandria.synology.me:8444/compute/wake",
            },
        )

    async def test_compute_endpoints_require_the_device_token(self):
        for request in (
            b"GET /compute/status HTTP/1.1\r\nHost: x\r\n\r\n",
            b"POST /compute/wake HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\n\r\n",
            b"GET /compute/status HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer wrong\r\n\r\n",
        ):
            with self.subTest(request=request):
                head, body = await self.request(request)
                self.assertIn(b"401", head)
                self.assertEqual(body, {"error": "unauthorized"})

    async def test_cloud_backend_is_ready_with_a_valid_token(self):
        for request in (
            b"GET /compute/status HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer test-token\r\n\r\n",
            b"POST /compute/wake HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer test-token\r\nContent-Length: 0\r\n\r\n",
        ):
            with self.subTest(request=request):
                head, body = await self.request(request)
                self.assertIn(b"200 OK", head)
                self.assertEqual(body, {"state": "ready"})

    async def test_wrong_method_is_rejected(self):
        head, _ = await self.request(
            b"GET /compute/wake HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer test-token\r\n\r\n"
        )
        self.assertIn(b"405", head)

    async def test_health(self):
        head, body = await self.request(
            b"GET /healthz HTTP/1.1\r\nHost: localhost\r\n\r\n"
        )
        self.assertIn(b"200 OK", head)
        self.assertEqual(body, {"status": "ok"})


class FakeComputeNode:
    def __init__(self):
        self.woken = 0

    async def status(self):
        return {"state": "off", "ollama": False, "stt": False, "tts": False}

    def wake(self):
        self.woken += 1
        return {"state": "waking"}


class LocalComputeTests(unittest.IsolatedAsyncioTestCase):
    async def start(self, **changes):
        self.node = FakeComputeNode()
        config = replace(CONFIG, backend="local", **changes)
        server = BootstrapServer(config, self.node)
        self.server = await asyncio.start_server(server.handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        self.server.close()
        await self.server.wait_closed()

    request = BootstrapTests.request

    async def test_ota_tells_a_local_device_to_check_compute(self):
        await self.start()
        _, body = await self.request(
            b"POST /xiaozhi/ota/ HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\n\r\n"
        )
        self.assertTrue(body["compute"]["required"])

    async def test_wake_and_status_reach_the_compute_node_only_when_authorized(self):
        await self.start()
        head, _ = await self.request(
            b"POST /compute/wake HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\n\r\n"
        )
        self.assertIn(b"401", head)
        self.assertEqual(self.node.woken, 0)
        head, body = await self.request(
            b"POST /compute/wake HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer test-token\r\nContent-Length: 0\r\n\r\n"
        )
        self.assertIn(b"202", head)
        self.assertEqual(body, {"state": "waking"})
        self.assertEqual(self.node.woken, 1)
        head, body = await self.request(
            b"GET /compute/status HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer test-token\r\n\r\n"
        )
        self.assertEqual(body["state"], "off")

    async def test_allowed_device_id_is_enforced(self):
        await self.start(allowed_device_id="aa:bb:cc:dd:ee:ff")
        token = b"Authorization: Bearer test-token\r\n"
        head, _ = await self.request(
            b"POST /compute/wake HTTP/1.1\r\nHost: x\r\n" + token + b"Device-Id: 11:22:33:44:55:66\r\nContent-Length: 0\r\n\r\n"
        )
        self.assertIn(b"401", head)
        head, _ = await self.request(
            b"POST /compute/wake HTTP/1.1\r\nHost: x\r\n" + token + b"Device-Id: AA:BB:CC:DD:EE:FF\r\nContent-Length: 0\r\n\r\n"
        )
        self.assertIn(b"202", head)
        self.assertEqual(self.node.woken, 1)


if __name__ == "__main__":
    unittest.main()
