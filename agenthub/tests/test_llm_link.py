import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent_runner
import llm_servers
from registry import RuntimeRegistry
from store import MessageStore

OLD = [{"name": "q", "kind": "api", "base_url": "http://127.0.0.1:8090/v1", "api_format": "openai",
        "api_key": "k1", "model": "m-q", "enabled": True},
       {"name": "cc", "kind": "claude", "model": "opus", "enabled": False}]


class LlmLink(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        p = mock.patch.object(llm_servers, "config_entries", lambda: [])
        p.start()
        self.addCleanup(p.stop)
        self.store = MessageStore(str(self.dir / "log.jsonl"))
        self.hub = types.SimpleNamespace(registry=RuntimeRegistry(data_dir=tmp.name), store=self.store,
                                         rules=types.SimpleNamespace(active_list=lambda: {"rules": []}))

    def manager(self, specs=OLD):
        (self.dir / "managed_agents.json").write_text(json.dumps(specs))
        m = agent_runner.AgentManager(self.hub, str(self.dir))
        m._apply = lambda name: None
        return m

    def on_disk(self):
        return {s["name"]: s for s in json.loads((self.dir / "managed_agents.json").read_text())}

    def test_load_migrates_backs_up_and_resolves(self):
        m = self.manager()
        self.assertTrue((self.dir / "managed_agents.json.bak-before-llm-servers").exists())
        q = m.specs["q"]
        self.assertEqual((q["base_url"], q["api_key"], q["model"]), ("http://127.0.0.1:8090/v1", "k1", "m-q"))
        disk = self.on_disk()["q"]
        self.assertTrue(disk["llm"].startswith("srv-"))
        self.assertEqual(disk["llm_model"], "m-q")
        for k in ("api_key", "base_url", "api_format", "model"):
            self.assertNotIn(k, disk)
        self.assertEqual(self.on_disk()["cc"]["model"], "opus")  # CLI agents untouched

    def test_save_strips_resolved_fields(self):
        m = self.manager()
        m.save()
        self.assertNotIn("api_key", (self.dir / "managed_agents.json").read_text())

    def test_default_follows_server_after_refresh(self):
        m = self.manager()
        sid = m.specs["q"]["llm"]
        m.upsert(dict(m.specs["q"], llm_model=""))
        llm_servers.upsert({"id": sid, "default_model": "new-default"})
        m.refresh_llm()
        self.assertEqual(m.specs["q"]["model"], "new-default")

    def test_upsert_api_agent_requires_llm(self):
        m = self.manager()
        err = m.upsert({"name": "z", "kind": "api", "llm": "srv-nope", "llm_model": ""})
        self.assertIn("LLM server", err)

    def test_old_style_form_still_links(self):
        m = self.manager()
        self.assertIsNone(m.upsert({"name": "z", "kind": "api", "base_url": "http://127.0.0.1:8090/v1",
                                    "api_format": "openai", "api_key": "k1", "model": "m-z"}))
        self.assertEqual(m.specs["z"]["llm"], m.specs["q"]["llm"])
        self.assertEqual(m.specs["z"]["model"], "m-z")

    def test_unlink_takes_agents_offline_and_blocks_enabling(self):
        m = self.manager()
        sid = m.specs["q"]["llm"]
        self.assertEqual(m.linked(sid), ["q"])
        self.assertEqual(m.unlink_llm(sid), ["q"])
        q = m.specs["q"]
        self.assertTrue(q["needs_llm"])
        self.assertFalse(q["enabled"])
        self.assertNotIn("llm", q)
        self.assertEqual(m.linked(sid), [])
        self.assertIn("LLM server", m.upsert(dict(q, enabled=True)) or "")
        self.assertIsNone(m.upsert(dict(q, enabled=True, llm=sid, llm_model="m-q")))
        self.assertFalse(m.specs["q"].get("needs_llm"))

    def test_missing_server_entry_takes_agent_offline(self):
        m = self.manager()
        llm_servers.save([])  # hand-edited file lost the server
        m.refresh_llm()
        q = m.specs["q"]
        self.assertTrue(q["needs_llm"])
        self.assertFalse(q["enabled"])

    def test_default_without_default_model_fails_turn_visibly(self):
        m = self.manager()
        m.upsert(dict(m.specs["q"], llm_model=""))  # server has no default model
        self.assertIn("no default model", m.specs["q"]["llm_error"])
        self.store.add("alice", "hi")
        r = agent_runner.Runner(m, "q")
        with mock.patch.object(agent_runner, "call_api_message", lambda *a, **k: self.fail("model called")):
            r._turn("general")
        self.assertTrue(any("no default model" in x["text"] for x in self.store.get_recent(5)))

    def test_spawn_copy_links_same_server(self):
        m = self.manager()
        m.spawn("q", "general", name="q-2")
        self.assertEqual(m.specs["q-2"]["llm"], m.specs["q"]["llm"])
        self.assertNotIn("api_key", self.on_disk()["q-2"])

    def test_corrupt_servers_file_destroys_nothing(self):
        m = self.manager()
        sid = m.specs["q"]["llm"]
        good = (self.dir / "llm_servers.json").read_text()
        (self.dir / "llm_servers.json").write_text(good.rstrip().rstrip("]") + ",]")  # hand edit gone wrong
        m2 = agent_runner.AgentManager(self.hub, str(self.dir))
        m2._apply = lambda name: None
        disk = self.on_disk()["q"]
        self.assertEqual((disk["llm"], disk["llm_model"], disk.get("enabled")), (sid, "m-q", True))
        self.assertIn("llm_servers.json", m2.specs["q"]["llm_error"])
        self.assertIsNotNone(llm_servers.upsert({"name": "N", "base_url": "http://n/v1"}))  # refuses to overwrite
        self.assertIn(",]", (self.dir / "llm_servers.json").read_text())

    def test_missing_server_keeps_the_link_for_when_it_returns(self):
        m = self.manager()
        sid = m.specs["q"]["llm"]
        saved = llm_servers.load()
        llm_servers.save([])
        m.refresh_llm()
        self.assertEqual((m.specs["q"]["llm"], m.specs["q"]["llm_model"]), (sid, "m-q"))
        llm_servers.save(saved)
        m.refresh_llm()
        self.assertEqual(m.specs["q"]["base_url"], "http://127.0.0.1:8090/v1")

    def test_refresh_never_clears_fields_a_running_turn_reads(self):
        m = self.manager()
        real = llm_servers.resolve
        seen = []
        def watch(spec):
            seen.append((spec.get("base_url"), spec.get("model")))
            return real(spec)
        with mock.patch.object(llm_servers, "resolve", watch):
            m.refresh_llm()
            m.upsert(dict(m.specs["q"], llm_model="m-q"))
        self.assertTrue(seen)
        self.assertNotIn((None, None), seen)


if __name__ == "__main__":
    unittest.main()
