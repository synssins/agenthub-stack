import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import admin_api
import agent_runner
import llm_servers
from registry import RuntimeRegistry
from store import MessageStore


class LlmAdmin(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = Path(tmp.name)
        cfg = [{"id": "cfg-x", "name": "x", "base_url": "http://c/v1", "api_key": "cfgkey", "readonly": True}]
        p = mock.patch.object(llm_servers, "config_entries", lambda: cfg)
        p.start()
        self.addCleanup(p.stop)
        (d / "managed_agents.json").write_text(json.dumps([
            {"name": "q", "kind": "api", "base_url": "http://h/v1", "api_key": "secret", "model": "m", "enabled": True}]))
        registry = RuntimeRegistry(data_dir=tmp.name)
        hub = types.SimpleNamespace(registry=registry, store=MessageStore(str(d / "log.jsonl")),
                                    rules=types.SimpleNamespace(active_list=lambda: {"rules": []}),
                                    room_settings={"channels": ["general"]})
        hub.agent_manager = agent_runner.AgentManager(hub, tmp.name)
        hub.agent_manager._apply = lambda name: None
        admin_api.setup(hub, {"server": {"data_dir": tmp.name}})
        app = FastAPI()
        app.include_router(admin_api.router)
        self.c = TestClient(app, base_url="http://127.0.0.1")
        self.sid = hub.agent_manager.specs["q"]["llm"]
        self.mgr = hub.agent_manager

    def test_list_hides_keys(self):
        r = self.c.get("/api/admin/llms")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("secret", r.text)
        self.assertNotIn("cfgkey", r.text)
        rows = {s["id"]: s for s in r.json()}
        self.assertTrue(rows[self.sid]["has_api_key"])
        self.assertEqual(rows[self.sid]["agents"], ["q"])
        self.assertTrue(rows["cfg-x"]["readonly"])

    def test_save_then_test_lists_models(self):
        r = self.c.post("/api/admin/llms", json={"name": "Local", "base_url": "http://l/v1", "api_format": "openai"})
        self.assertEqual(r.status_code, 200)
        new = r.json()["id"]
        with mock.patch.object(agent_runner, "list_models", lambda url, key, fmt: ["only-model"]):
            t = self.c.post(f"/api/admin/llms/{new}/test")
        self.assertEqual(t.json(), {"models": ["only-model"], "default_model": "only-model"})
        with mock.patch.object(agent_runner, "list_models", mock.Mock(side_effect=OSError("refused"))):
            self.assertEqual(self.c.post(f"/api/admin/llms/{new}/test").status_code, 502)

    def test_test_uses_stored_key(self):
        seen = {}
        def lm(url, key, fmt):
            seen["key"] = key
            return ["a", "b"]
        with mock.patch.object(agent_runner, "list_models", lm):
            self.c.post(f"/api/admin/llms/{self.sid}/test")
        self.assertEqual(seen["key"], "secret")

    def test_default_change_reaches_agents(self):
        self.mgr.upsert(dict(self.mgr.specs["q"], llm_model=""))
        self.c.post("/api/admin/llms", json={"id": self.sid, "name": "H", "base_url": "http://h/v1",
                                             "default_model": "d2"})
        self.assertEqual(self.mgr.specs["q"]["model"], "d2")

    def test_delete_blocked_until_unlinked(self):
        r = self.c.delete(f"/api/admin/llms/{self.sid}")
        self.assertEqual(r.status_code, 409)
        self.assertIn("1 agents linked", r.json()["error"])

    def test_unlink_then_delete(self):
        self.assertEqual(self.c.post(f"/api/admin/llms/{self.sid}/unlink").json(), {"unlinked": ["q"]})
        self.assertFalse(self.mgr.specs["q"]["enabled"])
        self.assertEqual(self.c.delete(f"/api/admin/llms/{self.sid}").status_code, 200)
        self.assertIsNone(llm_servers.get(self.sid))

    def test_cfg_entry_readonly(self):
        self.assertEqual(self.c.post("/api/admin/llms", json={"id": "cfg-x", "name": "y",
                                                               "base_url": "http://c/v1"}).status_code, 400)
        self.assertEqual(self.c.delete("/api/admin/llms/cfg-x").status_code, 400)


if __name__ == "__main__":
    unittest.main()
