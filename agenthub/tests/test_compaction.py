import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent_runner
from registry import RuntimeRegistry
from store import MessageStore


class Compaction(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = MessageStore(str(Path(tmp.name) / "log.jsonl"))
        hub = types.SimpleNamespace(registry=RuntimeRegistry(data_dir=tmp.name), store=self.store,
                                    rules=types.SimpleNamespace(active_list=lambda: {"rules": []}))
        self.mgr = agent_runner.AgentManager(hub, tmp.name)
        self.mgr._apply = lambda name: None
        self.mgr.upsert({"name": "q", "kind": "api", "base_url": "http://x/v1", "context_messages": 3,
                         "cwd": tmp.name})
        self.calls = []

    def fake(self, spec, system, msgs, tools=None, tool_choice=None, **kw):
        self.calls.append((system, msgs))
        if system.startswith("You keep the running memory"):
            return {"content": "<think>hmm</think>MEMORY: alice wants a red bike"}
        return {"content": "ok"}

    def test_dropped_messages_fold_into_memory_and_return_next_turn(self):
        for i in range(10):
            self.store.add("alice", f"msg {i}")
        r = agent_runner.Runner(self.mgr, "q")
        with mock.patch.object(agent_runner, "call_api_message", self.fake), \
                mock.patch.object(agent_runner.threading, "Thread",
                                  lambda target, args, daemon: types.SimpleNamespace(start=lambda: target(*args))):
            r._api_turn("general")
            mem = r.memory("general")
            self.assertEqual(mem["text"], "MEMORY: alice wants a red bike")
            folded = self.calls[-1][1][0]["content"]
            self.assertIn("msg 0", folded)
            self.assertNotIn("msg 9", folded)          # still in the window, not folded
            r._api_turn("general")
        first = self.calls[-1][1][0]["content"]
        self.assertIn("MEMORY: alice wants a red bike", first)   # memory leads the next prompt
        self.assertEqual(len(self.calls), 3)                     # nothing new to fold the second time

    def test_compact_now_and_forget(self):
        for i in range(5):
            self.store.add("alice", f"msg {i}")
        r = agent_runner.Runner(self.mgr, "q")
        with mock.patch.object(agent_runner, "call_api_message", self.fake):
            self.assertIn("folded", r.compact_now("general", keep=2))
        self.assertIn("MEMORY", r.memory("general")["text"])
        self.assertEqual([m["text"] for m in r._window("general", 3)], ["msg 3", "msg 4"])
        r.forget("general")
        self.assertEqual(r.memory("general"), {})

    def test_stopped_request_is_marked_in_the_next_context(self):
        self.store.add("alice", "draw a cat")
        r = agent_runner.Runner(self.mgr, "q")
        r.working, r.channel, r.thread = True, "general", None
        r.halt_turn()
        r.working = False
        r.halt.clear()
        self.store.add("alice", "what time is it")
        with mock.patch.object(agent_runner, "call_api_message", self.fake):
            r._api_turn("general")
        msgs = [m["content"] for m in self.calls[-1][1]]
        stop = next(i for i, c in enumerate(msgs) if c == agent_runner.STOP_NOTE)
        self.assertIn("draw a cat", msgs[stop - 1])
        self.assertIn("what time is it", msgs[stop + 1])

    def test_health_reports_unreachable_endpoint_queue_and_last_error(self):
        r = agent_runner.Runner(self.mgr, "q")
        self.mgr.specs["q"]["base_url"] = "http://127.0.0.1:9/v1"   # nothing listens on port 9
        (Path(self.mgr.data_dir) / "q_queue.jsonl").write_text('{"c": "general"}\n', "utf-8")
        r.last_error = {"at": 1, "channel": "general", "why": "boom"}
        h = r.health()
        self.assertFalse(h["endpoint"]["up"])
        self.assertEqual(h["queued"], 1)
        self.assertEqual(h["last_error"]["why"], "boom")

    def test_next_turn_prompt_extends_the_last_one(self):
        """Regression: the room note moved to the end each turn, so the engine re-read the
        whole prompt every time. The earlier prompt must be an exact prefix of the next."""
        self.mgr.specs["q"]["context_messages"] = 30
        self.store.add("alice", "hello")
        r = agent_runner.Runner(self.mgr, "q")
        with mock.patch.object(agent_runner, "call_api_message", self.fake), \
                mock.patch.object(agent_runner.Runner, "_turn_note", lambda self, ch: "nobody named you"):
            r._api_turn("general")
            first = list(self.calls[-1][1])
            self.store.add("q", "ok")
            self.store.add("alice", "and now?")
            r._api_turn("general")
            second = self.calls[-1][1]
        self.assertEqual(second[:len(first)], first)
        self.assertGreater(len(second), len(first))

    def test_alone_in_a_channel_untagged_messages_are_for_you(self):
        """Regression: with thinking off, the only agent in a channel obeyed 'not named, stay quiet'."""
        r = agent_runner.Runner(self.mgr, "q")
        self.store.add("alice", "good morning")
        self.mgr.hub.registry.get_active_names = lambda: ["q"]
        self.assertNotIn("did not name you", r._turn_note("general"))
        self.mgr.hub.registry.get_active_names = lambda: ["q", "other"]
        self.mgr.in_channel = lambda n, ch: True
        self.assertIn("did not name you", r._turn_note("general"))

    def test_prompt_stays_identical_after_a_restart(self):
        """Room notes and the window start are saved, so a new Runner builds the same prompt."""
        self.mgr.specs["q"]["context_messages"] = 30
        self.store.add("alice", "hello")
        with mock.patch.object(agent_runner, "call_api_message", self.fake), \
                mock.patch.object(agent_runner.Runner, "_turn_note", lambda self, ch: "note"):
            agent_runner.Runner(self.mgr, "q")._api_turn("general")
            first = list(self.calls[-1][1])
            self.store.add("q", "ok")
            self.store.add("alice", "again")
            reloaded = agent_runner.AgentManager(self.mgr.hub, self.mgr.data_dir)  # as after a restart
            reloaded._apply = lambda name: None
            agent_runner.Runner(reloaded, "q")._api_turn("general")
        self.assertEqual(self.calls[-1][1][:len(first)], first)

    def test_old_tool_output_is_trimmed_past_the_budget(self):
        msgs = [{"role": "tool", "content": "x" * 1000} for _ in range(5)]
        agent_runner._trim_tool_output(msgs, budget=3000, keep=2)
        self.assertTrue(all("trimmed" in m["content"] for m in msgs[:3]))
        self.assertEqual(msgs[-1]["content"], "x" * 1000)


if __name__ == "__main__":
    unittest.main()
