"""Barge-in: a message sent while an agent works is read mid-turn, or answered next turn."""
import json
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


class BargeIn(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.store = MessageStore(str(Path(tmp.name) / "log.jsonl"))
        hub = types.SimpleNamespace(registry=RuntimeRegistry(data_dir=tmp.name), store=self.store,
                                    rules=types.SimpleNamespace(active_list=lambda: {"rules": []}))
        self.mgr = agent_runner.AgentManager(hub, tmp.name)
        self.mgr._apply = lambda name: None
        self.mgr.upsert({"name": "q", "kind": "api", "base_url": "http://x/v1", "model": "m", "cwd": tmp.name})
        self.calls = []

    def texts(self, msgs):
        return [m["content"] if isinstance(m["content"], str) else json.dumps(m["content"]) for m in msgs]

    def test_message_sent_while_replying_is_answered_next_turn(self):
        def fake(spec, system, msgs, tools=None, tool_choice=None, **kw):
            self.calls.append(msgs)
            if len(self.calls) == 1:
                self.store.add("alice", "actually make it blue")  # typed while the model works
            return {"content": f"reply {len(self.calls)}"}
        self.store.add("alice", "draw a red bike")
        r = agent_runner.Runner(self.mgr, "q")
        with mock.patch.object(agent_runner, "call_api_message", fake):
            r._turn("general")   # posts "reply 1" after the blue message
            r._turn("general")   # the queued trigger for the blue message
        self.assertEqual(len(self.calls), 2)
        last = self.texts(self.calls[-1])
        # the reply that never saw it comes first; the late message is last, so it reads as unanswered
        self.assertLess(next(i for i, t in enumerate(last) if t == "reply 1"),
                        next(i for i, t in enumerate(last) if "make it blue" in t))
        with mock.patch.object(agent_runner, "call_api_message", fake):
            r._turn("general")   # nothing new now
        self.assertEqual(len(self.calls), 2)

    def test_message_sent_during_a_tool_step_is_read_at_the_next_step(self):
        def fake(spec, system, msgs, tools=None, tool_choice=None, **kw):
            self.calls.append(list(msgs))
            if len(self.calls) == 1:
                self.store.add("alice", "stop, use blue")
                return {"content": "", "tool_calls": [{"id": "t1", "function": {"name": "nope", "arguments": "{}"}}]}
            return {"content": "ok, blue"}
        self.store.add("alice", "draw a red bike")
        r = agent_runner.Runner(self.mgr, "q")
        with mock.patch.object(agent_runner, "call_api_message", fake):
            r._turn("general")
            self.assertTrue(any(agent_runner.BARGE_NOTE in t and "stop, use blue" in t
                                for t in self.texts(self.calls[1])))
            r._turn("general")   # the queued trigger: already read mid-turn
        self.assertEqual(len(self.calls), 2)
        self.assertEqual([m["text"] for m in self.store.get_recent(5) if m["sender"] == "q"], ["ok, blue"])

    def test_long_reply_without_tools_is_cut_off_for_a_queued_message(self):
        def fake(spec, system, msgs, tools=None, tool_choice=None, on_text=None, **kw):
            self.calls.append(list(msgs))
            if len(self.calls) == 1:
                on_text("Once upon a time")
                self.store.add("alice", "go ahead and stop")
                (Path(self.tmp) / "q_queue.jsonl").write_text(json.dumps({"channel": "general"}) + "\n")
                for i in range(5):
                    on_text("Once upon a time" + " more" * i)
                self.fail("the stream should have been dropped")
            return {"content": "Stopped."}
        self.store.add("alice", "tell me a long story")
        r = agent_runner.Runner(self.mgr, "q")
        clock = iter(range(0, 10000, 2))  # each reading is 2 s later: the 1 s check always runs
        with mock.patch.object(agent_runner, "call_api_message", fake), \
                mock.patch.object(agent_runner.time, "monotonic", lambda: next(clock)):
            r._turn("general")
        second = self.texts(self.calls[1])
        self.assertTrue(any("Once upon a time" in t and "cut off" in t for t in second))
        self.assertIn("go ahead and stop", second[-1])
        self.assertEqual([m["text"] for m in self.store.get_recent(5) if m["sender"] == "q"], ["Stopped."])

    def test_failed_turn_does_not_mark_the_message_answered(self):
        def boom(*a, **kw):
            raise RuntimeError("down")
        self.store.add("alice", "hello")
        r = agent_runner.Runner(self.mgr, "q")
        with mock.patch.object(agent_runner, "call_api_message", boom):
            r._turn("general")
        with mock.patch.object(agent_runner, "call_api_message", lambda *a, **kw: {"content": "hi"}):
            r._turn("general")
        self.assertIn("hi", [m["text"] for m in self.store.get_recent(5)])

    def test_claude_turn_is_interrupted_at_a_tool_boundary_for_a_queued_message(self):
        r = agent_runner.Runner(self.mgr, "q")
        r.thread = None
        r._act_reset()
        first = self.store.add("alice", "build it")
        events = [
            {"type": "assistant", "session_id": "s1", "message": {"id": "a1", "content": [
                {"type": "tool_use", "id": "u1", "name": "Bash", "input": {"command": "make"}}]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "u1", "content": "done"}]}},
            {"type": "assistant", "message": {"id": "a2", "content": [
                {"type": "tool_use", "id": "u2", "name": "Bash", "input": {"command": "make test"}}]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "u2", "content": "ok"}]}},
        ]
        def lines():
            for i, e in enumerate(events):
                if i == 2:  # the owner writes after the first tool step; the trigger is queued
                    self.store.add("alice", "wait, use the debug build")
                    (Path(self.tmp) / "q_queue.jsonl").write_text(json.dumps({"channel": "general"}) + "\n")
                yield json.dumps(e) + "\n"
        proc = types.SimpleNamespace(stdout=lines(), stderr=iter(()), poll=lambda: None, wait=lambda timeout=None: 0)
        r.proc = proc
        killed = []
        with mock.patch.object(agent_runner, "kill_group", killed.append):
            (final, session), _ = r._claude_stream("general", since=first["id"])
        self.assertEqual(killed, [proc])          # once, at the first boundary after the message
        self.assertTrue(r._cli_barged)
        self.assertEqual(session, "s1")

    def test_claude_turn_is_not_interrupted_by_a_message_for_someone_else(self):
        r = agent_runner.Runner(self.mgr, "q")
        r.thread = None
        r._act_reset()
        first = self.store.add("alice", "build it")
        self.store.add("alice", "@other hi")  # in the room, but no trigger queued for q
        events = [
            {"type": "assistant", "session_id": "s1", "message": {"id": "a1", "content": [
                {"type": "tool_use", "id": "u1", "name": "Bash", "input": {}}]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "u1", "content": "x"}]}},
        ]
        proc = types.SimpleNamespace(stdout=iter(json.dumps(e) + "\n" for e in events), stderr=iter(()),
                                     poll=lambda: None, wait=lambda timeout=None: 0)
        r.proc = proc
        with mock.patch.object(agent_runner, "kill_group", lambda p: self.fail("killed")):
            r._claude_stream("general", since=first["id"])
        self.assertFalse(r._cli_barged)


if __name__ == "__main__":
    unittest.main()
