"""/v1 OpenAI-compatible surface: auth, models, rounds (stream + non-stream)."""

import json
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI
from fastapi.testclient import TestClient

import v1_api
from router import Router
from store import MessageStore

AGENTS = ["claude", "qwen"]


class FakeRegistry:
    def get_all(self):
        return {n: {"label": n} for n in AGENTS}

    def get_active_names(self):
        return list(AGENTS)


class V1Test(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        store = MessageStore(str(Path(self.tmp.name) / "log.jsonl"))
        hub = types.SimpleNamespace(store=store, registry=FakeRegistry(),
                                    router=Router(AGENTS, default_mention="none",
                                                  online_checker=lambda: set(AGENTS)))
        self.store = store
        # When the room gets a human message, the mentioned agents "reply".
        def fake_agents(msg):
            if msg["sender"] in AGENTS:
                return
            targets = hub.router.parse_mentions(msg["text"])
            def reply():
                for i, name in enumerate(sorted(targets)):
                    time.sleep(0.2)
                    store.add(name, f"{name} here (reply {i})", channel=msg["channel"])
            threading.Thread(target=reply, daemon=True).start()
        store.on_message(fake_agents)
        v1_api.setup(hub, {"v1": {"keys": {"opencode": "sk-hub-test"}, "settle_seconds": 2}})
        app = FastAPI()
        app.include_router(v1_api.router)
        self.client = TestClient(app, base_url="http://127.0.0.1")
        self.auth = {"Authorization": "Bearer sk-hub-test"}

    def tearDown(self):
        self.tmp.cleanup()

    def test_auth_required(self):
        self.assertEqual(self.client.get("/v1/models").status_code, 401)
        r = self.client.get("/v1/models", headers={"Authorization": "Bearer wrong"})
        self.assertEqual(r.status_code, 401)
        self.assertIn("message", r.json()["error"])

    def test_models_lists_room_and_agents(self):
        ids = [m["id"] for m in self.client.get("/v1/models", headers=self.auth).json()["data"]]
        self.assertEqual(ids, ["agenthub-room", "claude", "qwen"])

    def test_room_round_non_stream(self):
        body = {"model": "agenthub-room",
                "messages": [{"role": "system", "content": "ignored"},
                             {"role": "user", "content": "Hey guys! Sound off!"}]}
        r = self.client.post("/v1/chat/completions", headers=self.auth, json=body)
        self.assertEqual(r.status_code, 200)
        content = r.json()["choices"][0]["message"]["content"]
        self.assertIn("claude: claude here", content)
        self.assertIn("qwen: qwen here", content)
        posted = [m for m in self.store.get_recent(10) if m["sender"] == "opencode"][0]
        self.assertEqual(posted["text"], "@all Hey guys! Sound off!")

    def test_agent_model_targets_one_agent_streaming(self):
        body = {"model": "Claude", "stream": True,
                "messages": [{"role": "user", "content": [{"type": "text", "text": "say something"}]}]}
        with self.client.stream("POST", "/v1/chat/completions", headers=self.auth, json=body) as r:
            lines = [l for l in r.iter_lines() if l.startswith("data: ")]
        self.assertEqual(lines[-1], "data: [DONE]")
        chunks = [json.loads(l[6:]) for l in lines[:-1]]
        text = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks)
        self.assertEqual(text, "claude: claude here (reply 0)")
        self.assertEqual(chunks[-1]["choices"][0]["finish_reason"], "stop")

    def test_unknown_model_and_missing_user_message(self):
        r = self.client.post("/v1/chat/completions", headers=self.auth,
                             json={"model": "nobody", "messages": [{"role": "user", "content": "x"}]})
        self.assertEqual(r.status_code, 404)
        r = self.client.post("/v1/chat/completions", headers=self.auth,
                             json={"model": "agenthub-room", "messages": [{"role": "system", "content": "x"}]})
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()
