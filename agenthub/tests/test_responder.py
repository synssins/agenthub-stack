import asyncio
import sys
import types
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import responder


class Named(unittest.TestCase):
    def test_whole_word_handle_or_label_without_at(self):
        who = {"quill-qwen": "Quill", "fable": "Fable", "x": "x"}
        self.assertEqual(responder.named("What do you think, quill?", who), ["quill-qwen"])
        self.assertEqual(responder.named("FABLE and Quill-Qwen, go", who), ["quill-qwen", "fable"])
        self.assertEqual(responder.named("an unfabled tale, x marks it", who), [])  # substrings and 1-letter names


class Pick(unittest.TestCase):
    def test_answer_is_matched_to_a_candidate(self):
        seen = {}

        def call(spec, system, msgs, effort=None):
            seen.update(spec=spec, prompt=msgs[0]["content"], effort=effort)
            return {"content": "<think>the db one</think> @dba-2 should take this."}
        got = responder.pick({"name": "r", "timeout": 600}, {"dba": "old", "dba-2": "databases", "ui": "frontend"},
                             ["alice: hi"], "why is the query slow?", call=call)
        self.assertEqual(got, "dba-2")
        self.assertIn("@dba-2: databases", seen["prompt"])
        self.assertIn("why is the query slow?", seen["prompt"])
        self.assertEqual((seen["effort"], seen["spec"]["timeout"]), ("none", responder.PICK_TIMEOUT))

    def test_provider_that_rejects_effort_is_asked_again_and_failures_give_none(self):
        calls = []

        def picky(spec, system, msgs, **kw):
            calls.append(kw)
            if kw:
                raise urllib.error.HTTPError("u", 400, "bad", {}, None)
            return {"content": "ui"}
        self.assertEqual(responder.pick({}, {"ui": "", "dba": ""}, [], "x", call=picky), "ui")
        self.assertEqual(calls, [{"effort": "none"}, {}])
        self.assertIsNone(responder.pick({}, {"ui": ""}, [], "x", call=lambda *a, **k: {"content": "nobody"}))
        self.assertIsNone(responder.pick({}, {"ui": ""}, [], "x", call=mock.Mock(side_effect=TimeoutError())))


class Untagged(unittest.TestCase):
    """app._untagged_targets: the room setting decides who gets an untagged human message."""

    def setUp(self):
        import app
        self.app = app
        specs = {"a": {"name": "a", "kind": "api", "label": "Ada"}, "b": {"name": "b", "kind": "claude"}}
        members = {"a": ["general", "team"], "b": ["general", "team"], "c": ["general"]}
        patches = [
            mock.patch.object(app, "registry", types.SimpleNamespace(get_active_names=lambda: ["a", "b", "c"])),
            mock.patch.object(app, "agent_manager", types.SimpleNamespace(
                specs=specs, in_channel=lambda n, ch: ch in members[n])),
            mock.patch.object(app, "store", types.SimpleNamespace(get_recent=lambda n, channel: [])),
            mock.patch.dict(app.room_settings, {"untagged": "channel", "name_mentions": False, "picker_agent": ""}),
            mock.patch("mcp_bridge.get_role", lambda n: ""),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def run_it(self, text, channel):
        return asyncio.run(self.app._untagged_targets(text, channel))

    def test_default_keeps_team_channels_and_a_quiet_general(self):
        self.assertEqual(self.run_it("hello", "team"), ["a", "b"])
        self.assertEqual(self.run_it("hello", "general"), [])

    def test_names_random_and_model_pick(self):
        rs = self.app.room_settings
        rs["name_mentions"] = True
        self.assertEqual(self.run_it("ada, your turn", "general"), ["a"])
        rs["untagged"] = "random"
        self.assertIn(self.run_it("hello", "general")[0], ["a", "b", "c"])
        rs["untagged"] = "pick"
        with mock.patch.object(responder, "pick", lambda spec, about, recent, text: "b") as _:
            self.assertEqual(self.run_it("hello", "team"), ["b"])
        with mock.patch.object(responder, "pick", lambda *a: None):   # model failed: someone still answers
            self.assertIn(self.run_it("hello", "team")[0], ["a", "b"])


class Echo(unittest.TestCase):
    """/echo: agents post the line as written; nobody is woken and it stays out of their context."""

    def setUp(self):
        import tempfile
        import app
        from store import MessageStore
        self.app = app
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = MessageStore(str(Path(tmp.name) / "log.jsonl"))
        members = {"a": ["general", "team"], "b": ["general", "team"], "c": ["general"]}
        names = lambda: ["a", "b", "c"]
        for p in (mock.patch.object(app, "registry", types.SimpleNamespace(get_active_names=names, get_all_names=names)),
                  mock.patch.object(app, "agent_manager", types.SimpleNamespace(in_channel=lambda n, ch: ch in members[n])),
                  mock.patch.object(app, "store", self.store)):
            p.start()
            self.addCleanup(p.stop)

    def test_named_agents_or_the_whole_team_channel_echo(self):
        self.assertIsNone(self.app._echo(" @B @nobody Hello, world.", "general"))
        self.assertIsNone(self.app._echo(" Testing one two.", "team"))
        got = [(m["sender"], m["text"], m["channel"], m["metadata"]) for m in self.store.get_recent(9)]
        self.assertEqual(got, [("b", "Hello, world.", "general", {"echo": True}),
                               ("a", "Testing one two.", "team", {"echo": True}),
                               ("b", "Testing one two.", "team", {"echo": True})])
        self.assertIn("Usage", self.app._echo(" no names in general", "general"))
        self.assertIn("Usage", self.app._echo(" @a", "general"))

    def test_echo_lines_wake_nobody(self):
        woken = []
        with mock.patch.object(self.app, "broadcast", mock.AsyncMock()), \
                mock.patch.object(self.app, "router", types.SimpleNamespace(get_targets=lambda *a: woken.append(a) or [])):
            asyncio.run(self.app._handle_new_message(
                {"id": 1, "sender": "a", "text": "@b say hi", "channel": "team", "metadata": {"echo": True}}))
        self.assertEqual(woken, [])


if __name__ == "__main__":
    unittest.main()
