import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent_runner

CHUNKS = [
    {"choices": [{"delta": {"content": "Hello there. "}}]},
    {"choices": [{"delta": {"content": "Second bit."}}]},
    {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "list_", "arguments": "{\"pa"}}]}}]},
    {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"name": "dir", "arguments": "th\": \".\"}"}}]}}]},
    {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5}},
]


class SSE(BaseHTTPRequestHandler):
    seen = {}

    def do_POST(self):
        SSE.seen = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for c in CHUNKS:
            self.wfile.write(f"data: {json.dumps(c)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")

    def log_message(self, *a):
        pass


class Streaming(unittest.TestCase):
    def test_stream_collects_text_tool_calls_usage_and_sends_effort(self):
        srv = HTTPServer(("127.0.0.1", 0), SSE)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        seen = []
        spec = {"name": "t", "base_url": f"http://127.0.0.1:{srv.server_port}/v1", "model": "m"}
        msg = agent_runner.call_api_message(spec, "sys", [{"role": "user", "content": "hi"}],
                                            on_text=seen.append, effort="none")
        self.assertEqual(msg["content"], "Hello there. Second bit.")
        self.assertEqual(seen, ["Hello there. ", "Hello there. Second bit."])
        self.assertEqual(msg["tool_calls"][0]["function"], {"name": "list_dir", "arguments": "{\"path\": \".\"}"})
        self.assertTrue(SSE.seen["stream"])
        self.assertEqual(SSE.seen["reasoning_effort"], "none")


if __name__ == "__main__":
    unittest.main()
