import asyncio
import json
import unittest
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
        self.assertEqual(body["server_time"]["timezone_offset"], 120)
        self.assertGreater(body["server_time"]["timestamp"], 0)
        self.assertNotIn("mqtt", body)

    async def test_health(self):
        head, body = await self.request(
            b"GET /healthz HTTP/1.1\r\nHost: localhost\r\n\r\n"
        )
        self.assertIn(b"200 OK", head)
        self.assertEqual(body, {"status": "ok"})


if __name__ == "__main__":
    unittest.main()
