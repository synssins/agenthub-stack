"""Prompt caching (stable prompt start, stepped history window, cache markers) and per-call metrics."""

import json
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import admin_api
import agent_runner
from registry import RuntimeRegistry
from store import MessageStore


def _setup(test, **spec):
    tmp = tempfile.TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    store = MessageStore(str(Path(tmp.name) / "log.jsonl"))
    hub = types.SimpleNamespace(registry=RuntimeRegistry(data_dir=tmp.name), store=store,
                                rules=types.SimpleNamespace(active_list=lambda: {"rules": []}))
    mgr = agent_runner.AgentManager(hub, tmp.name)
    mgr._apply = lambda name: None
    mgr.upsert({"name": "qwen", "kind": "api", "base_url": "http://x/v1", **spec})
    # another agent shares the channel (alone, every message counts as addressed to you)
    hub.registry.get_active_names = lambda: ["qwen", "other"]
    mgr.in_channel = lambda name, channel: True
    return tmp, store, mgr


class StablePromptTest(unittest.TestCase):
    def test_system_prompt_same_whether_named_or_not(self):
        _, store, mgr = _setup(self)
        r = agent_runner.Runner(mgr, "qwen")
        store.add("alice", "@qwen hello")
        named = r._system("general", dynamic=False)
        store.add("alice", "hello everyone")
        self.assertEqual(r._system("general", dynamic=False), named)
        self.assertIn("did not name you", r._turn_note("general"))
        self.assertIn("did not name you", r._system("general"))  # CLI agents still get it inline

    def test_turn_note_goes_after_history_and_first_line_is_fixed(self):
        _, store, mgr = _setup(self)
        store.add("qwen", "my earlier reply")
        store.add("alice", "hello everyone")
        seen = {}

        def fake(spec, system, msgs, tools=None, tool_choice=None, **kw):
            seen["system"], seen["msgs"] = system, msgs
            return {"content": "ok"}
        with mock.patch.object(agent_runner, "call_api_message", fake):
            agent_runner.Runner(mgr, "qwen")._api_turn("general")
        msgs = seen["msgs"]
        self.assertEqual(msgs[0]["content"], "(earlier conversation omitted)")
        self.assertTrue(msgs[-1]["content"].startswith("(Room note"))
        self.assertIn("did not name you", msgs[-1]["content"])
        self.assertNotIn("did not name you", seen["system"])


class WindowTest(unittest.TestCase):
    def test_window_start_holds_until_2n_then_jumps(self):
        _, store, mgr = _setup(self, context_messages=5)
        r = agent_runner.Runner(mgr, "qwen")
        for i in range(8):
            store.add("alice", f"m{i}")
        first = r._window("general", 5)
        self.assertEqual([m["text"] for m in first], ["m3", "m4", "m5", "m6", "m7"])
        starts = []
        for i in range(8, 14):
            store.add("alice", f"m{i}")
            starts.append(r._window("general", 5)[0]["text"])
        # holds at m3 while the window grows to 10, then jumps to the newest 5
        self.assertEqual(starts, ["m3", "m3", "m3", "m3", "m3", "m9"])
        self.assertEqual(len(r._window("general", 5)), 5)


class CacheMarkerTest(unittest.TestCase):
    def _payload(self, spec):
        sent = {}

        def post(url, payload, headers, timeout=120):
            sent["p"] = payload
            return {"choices": [{"message": {"content": "hi"}}], "usage": {}}
        with mock.patch.object(agent_runner, "_post_json", post):
            agent_runner.call_api_message(spec, "SYS", [{"role": "user", "content": "a"},
                                                        {"role": "user", "content": "b"}])
        return sent["p"]

    def test_explicit_markers_for_claude_and_gemini_on_openrouter(self):
        for model in ("anthropic/claude-x", "google/gemini-y"):
            p = self._payload({"model": model, "base_url": "https://openrouter.ai/api/v1"})
            self.assertEqual(p["messages"][0]["content"][0]["cache_control"], {"type": "ephemeral"})
            self.assertEqual(p["messages"][-1]["content"][0]["cache_control"], {"type": "ephemeral"})
            self.assertEqual(p["messages"][1]["content"], "a")  # only the last one is marked
            self.assertEqual(p["usage"], {"include": True})

    def test_no_markers_for_auto_caching_models(self):
        p = self._payload({"model": "openai/gpt-x", "base_url": "http://local/v1"})
        self.assertEqual(p["messages"][0]["content"], "SYS")
        self.assertNotIn("usage", p)


class MetricsTest(unittest.TestCase):
    def test_call_recorded_and_summarised(self):
        tmp, _, mgr = _setup(self)
        reply = {"choices": [{"message": {"content": "hi"}}], "usage": {
            "prompt_tokens": 1000, "completion_tokens": 50, "cost": 0.0021,
            "prompt_tokens_details": {"cached_tokens": 800}}}
        with mock.patch.object(agent_runner, "_post_json", lambda *a, **k: reply):
            agent_runner._call_ctx.agent, agent_runner._call_ctx.channel = "qwen", "general"
            agent_runner.call_api_message({"model": "m", "base_url": "http://x"}, "S", [{"role": "user", "content": "x"}])
            agent_runner.call_api_message({"model": "m", "base_url": "http://x"}, "S", [{"role": "user", "content": "x"}])
        path = Path(tmp.name) / "metrics.jsonl"
        row = json.loads(path.read_text().splitlines()[0])
        self.assertEqual((row["agent"], row["channel"], row["prompt_tokens"], row["cached_tokens"]),
                         ("qwen", "general", 1000, 800))
        summary = admin_api.metrics_summary(path, time.time() - 60)
        g = summary["rows"][0]
        self.assertEqual((g["calls"], g["prompt_tokens"], g["cache_hit_pct"], g["cost"]), (2, 2000, 80.0, 0.0042))
        self.assertEqual(admin_api.metrics_summary(path, time.time() + 60)["rows"], [])


if __name__ == "__main__":
    unittest.main()


class OwnerToolsTest(unittest.TestCase):
    """Tools are the owner's: a Discord stranger's turn gets chat tools only."""

    def _tools(self, store, mgr, sender, text):
        store.add(sender, text)
        seen = {}

        def fake(spec, system, msgs, tools=None, tool_choice=None, **kw):
            seen["tools"] = {t["function"]["name"] for t in tools or []}
            return {"content": "ok"}
        with mock.patch.object(agent_runner, "call_api_message", fake):
            agent_runner.Runner(mgr, "qwen")._api_turn("general")
        return seen["tools"]

    def test_stranger_gets_chat_tools_owner_keeps_them(self):
        _, store, mgr = _setup(self, permissions="read")
        self.assertIn("read_file", self._tools(store, mgr, "Morgan (Discord, owner)", "@qwen read it"))
        self.assertIn("read_file", self._tools(store, mgr, "Morgan", "@qwen read it"))   # AgentHub
        for who in ("Bob (Discord)", "Morgan (Discord, not the owner)", "Aither (Discord bot)",
                    "Bob (Discord voice)"):
            self.assertNotIn("read_file", self._tools(store, mgr, who, "@qwen read it"), who)

    def test_stranger_naming_agent_after_owner_still_gates(self):
        _, store, mgr = _setup(self, permissions="read")
        store.add("Bob (Discord)", "@qwen open the garage")
        self.assertNotIn("read_file", self._tools(store, mgr, "Morgan (Discord, owner)", "nice weather"))
