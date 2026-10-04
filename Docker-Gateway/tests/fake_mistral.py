"""A local stand-in for the Mistral endpoints the gateway uses."""

import base64
import json
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeMistral:
    def __init__(self):
        self.requests = []          # (path, parsed body)
        self.connections = 0
        self.transcript = "Hoeveel mensen wonen er in Den Haag?"
        self.answer = ["In Den Haag wonen ", "ongeveer 550.000 mensen. ", "Dat is **veel**", "!"]
        self.status = {}            # path -> forced HTTP status
        self.refuse = {}            # path -> number of requests to answer with 429
        self.retry_after = None     # Retry-After header value for those answers
        self.speech_gate = None     # threading.Event: block speech until set
        self.chat_delay = 0         # seconds between the parts of the answer
        self.voices = [
            {"id": "voice-en", "name": "Paul", "type": "preset", "languages": ["en_us"]},
            {"id": "voice-nl", "name": "Sanne", "type": "preset", "languages": ["nl_nl"]},
        ]
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def setup(self):
                super().setup()
                fake.connections += 1

            def _json(self, status, payload):
                data = json.dumps(payload).encode()
                self.send_response(status)
                if status == 429 and fake.retry_after is not None:
                    self.send_header("Retry-After", fake.retry_after)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _sse(self, events, delay=0):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                try:
                    for name, payload in events:
                        text = ""
                        if name:
                            text += f"event: {name}\n"
                        text += f"data: {payload if isinstance(payload, str) else json.dumps(payload)}\n\n"
                        chunk = text.encode()
                        self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                        self.wfile.flush()
                        time.sleep(delay)
                    self.wfile.write(b"0\r\n\r\n")
                except OSError:
                    self.close_connection = True  # the client cancelled

            def do_GET(self):
                path = self.path.split("?")[0]
                fake.requests.append((self.path, None))
                if path in fake.status:
                    return self._json(fake.status[path], {"message": "secret detail"})
                self._json(200, {"items": fake.voices, "total": len(fake.voices)})

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                if self.headers.get("Authorization") != "Bearer test-key":
                    return self._json(401, {"message": "secret detail"})
                if self.path in fake.status:
                    fake.requests.append((self.path, None))
                    return self._json(fake.status[self.path], {"message": "secret detail"})
                if fake.refuse.get(self.path):
                    fake.refuse[self.path] -= 1
                    fake.requests.append((self.path, None))
                    return self._json(429, {"message": "Requests rate limit exceeded"})
                if self.path == "/v1/audio/transcriptions":
                    boundary = self.headers["Content-Type"].split("boundary=")[1].encode()
                    fields = {}
                    for part in body.split(b"--" + boundary)[1:-1]:
                        head, _, value = part.partition(b"\r\n\r\n")
                        name = head.split(b'name="')[1].split(b'"')[0].decode()
                        fields[name] = value[:-2]
                    fake.requests.append((self.path, fields))
                    self._json(200, {"model": "m", "text": f" {fake.transcript} ", "language": "nl"})
                elif self.path == "/v1/chat/completions":
                    fake.requests.append((self.path, json.loads(body)))
                    events = [(None, {"choices": [{"delta": {"role": "assistant", "content": ""}}]})]
                    events += [(None, {"choices": [{"delta": {"content": part}}]}) for part in fake.answer]
                    events += [(None, {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {}}), (None, "[DONE]")]
                    self._sse(events, fake.chat_delay)
                elif self.path == "/v1/audio/speech":
                    request = json.loads(body)
                    fake.requests.append((self.path, request))
                    if fake.speech_gate is not None:
                        fake.speech_gate.wait(5)
                    # 0.1 s of a constant level per word, as float32 LE at 24 kHz,
                    # cut at byte offsets that are not multiples of four.
                    samples = struct.pack("<f", 0.5) * (2400 * len(request["input"].split()))
                    samples += struct.pack("<3f", 2.0, -2.0, float("nan"))
                    events = []
                    for start in range(0, len(samples), 4099):
                        piece = samples[start : start + 4099]
                        events.append(("speech.audio.delta", {"type": "speech.audio.delta", "audio_data": base64.b64encode(piece).decode()}))
                    events.append(("speech.audio.done", {"type": "speech.audio.done", "usage": {}}))
                    self._sse(events)
                else:
                    self._json(404, {})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def paths(self):
        return [path for path, _ in self.requests]

    def bodies(self, path):
        return [body for item, body in self.requests if item == path]

    def close(self):
        if self.speech_gate is not None:
            self.speech_gate.set()
        self.server.shutdown()
        self.server.server_close()
