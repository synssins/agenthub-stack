"""Stop agents, reactions, threads, and remote-agent wake-ups (control features)."""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import types
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI
from fastapi.testclient import TestClient

import admin_api
import agent_runner
import app
import mcp_bridge
from agents import AgentTrigger
from registry import RuntimeRegistry
from router import Router
from store import MessageStore


class AppHarness(unittest.TestCase):
    """app.py globals pointed at a temp store/registry/queue dir."""

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.d = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.store = MessageStore(str(self.d / "log.jsonl"))
        self.registry = RuntimeRegistry(data_dir=str(self.d))
        self.members = {"blah": ["magpiestrongbox", "claudegod"]}
        self.tokens = {}
        manager = types.SimpleNamespace(
            runners={}, specs={}, in_channel=lambda n, ch: ch == "general" or n in self.members.get(ch, []),
            channels_of=lambda n: ["general"] + [c for c, ns in self.members.items() if n in ns])
        self.manager = manager
        for name, value in {"store": self.store, "registry": self.registry,
                            "agents": AgentTrigger(self.registry, str(self.d)),
                            "router": Router([], default_mention="none"), "config": {},
                            "agent_manager": manager, "session_engine": None, "_event_loop": None,
                            "ws_clients": set(), "room_settings": {"username": "alice", "channels": ["general", "blah"]},
                            }.items():
            self.stack.enter_context(patch.object(app, name, value))
        self.stack.enter_context(patch.object(mcp_bridge, "store", self.store))
        self.stack.enter_context(patch.object(mcp_bridge, "registry", self.registry))

    def join(self, *names):
        for n in names:
            self.registry.seed({n: {"label": n, "color": "#888"}})
            self.tokens[n] = self.registry.register(n)["token"]
        app.router.update_agents(self.registry.get_active_names())

    def queue(self, name) -> list[dict]:
        q = self.d / f"{name}_queue.jsonl"
        return [json.loads(l) for l in q.read_text().splitlines()] if q.exists() else []

    def post(self, sender, text, channel="general", thread=None):
        msg = self.store.add(sender, text, channel=channel, thread=thread)
        asyncio.run(app._handle_new_message(msg))
        return msg


class RemoteWakeTest(AppHarness):
    """Bug: claudegod (remote, member of #blah) was woken by '@maskedmagpie ...' in #blah."""

    def test_mention_of_unknown_or_renamed_handle_wakes_nobody_else(self):
        self.join("magpiestrongbox", "claudegod")
        # 'maskedmagpie' had just been renamed: no longer a handle the router knows. The
        # team-channel rule used to read that as an untagged message and wake every member.
        self.post("alice", "@maskedmagpie just testing", channel="blah")
        self.assertEqual(self.queue("claudegod"), [])
        self.assertEqual(self.queue("magpiestrongbox"), [])

    def test_only_the_named_agent_is_woken(self):
        self.join("magpiestrongbox", "claudegod")
        self.post("alice", "@magpiestrongbox just testing", channel="blah")
        self.assertEqual(self.queue("claudegod"), [])
        self.assertEqual(len(self.queue("magpiestrongbox")), 1)

    def test_untagged_human_message_still_reaches_the_team_and_all_reaches_everyone(self):
        self.join("magpiestrongbox", "claudegod")
        self.post("alice", "anyone around?", channel="blah")
        self.assertEqual((len(self.queue("claudegod")), len(self.queue("magpiestrongbox"))), (1, 1))
        with patch.object(mcp_bridge, "is_online", lambda n: True), \
                patch.object(mcp_bridge, "_get_online", lambda: ["claudegod", "magpiestrongbox"]):
            app.router._online_checker = lambda: {"claudegod", "magpiestrongbox"}
            self.post("alice", "@all status", channel="blah")
        self.assertEqual((len(self.queue("claudegod")), len(self.queue("magpiestrongbox"))), (2, 2))

    def test_trigger_payload_says_which_message_named_you(self):
        self.join("claudegod")
        self.store.add("alice", "@magpie hi", channel="blah")
        self.store.add("magpie", "passing it to @claudegod", channel="blah")
        (self.d / "claudegod_queue.jsonl").write_text(json.dumps({"channel": "blah"}) + "\n")
        hub = types.SimpleNamespace(registry=self.registry, store=self.store,
                                    room_settings={"username": "alice"},
                                    _resolve_authenticated_agent=lambda req: self.registry.resolve_token(
                                        req.headers.get("authorization", "")[7:]))
        admin_api._remote_cursors.clear()
        admin_api.setup(hub, {"server": {"data_dir": str(self.d)}})
        api = FastAPI()
        api.include_router(admin_api.router)
        token = self.tokens["claudegod"]
        r = TestClient(api, base_url="http://127.0.0.1").get("/api/triggers?wait=0", headers={"Authorization": f"Bearer {token}"})
        msgs = r.json()["triggers"][0]["messages"]
        self.assertEqual([m["mentions_you"] for m in msgs], [False, True])


class StopTest(AppHarness):
    def test_stop_kills_cli_process_group_and_clears_queues(self):
        self.join("cli", "remote")
        r = agent_runner.Runner(types.SimpleNamespace(), "cli")
        # a CLI that started a child: both must die
        r.proc = subprocess.Popen(["bash", "-c", "sleep 30 & sleep 30; wait"], start_new_session=True)
        r.working = True
        self.manager.runners["cli"] = r
        for n in ("cli", "remote"):
            (self.d / f"{n}_queue.jsonl").write_text(json.dumps({"channel": "general"}) + "\n")
        res = TestClient(app.app, base_url="http://127.0.0.1").post("/api/agents/stop", json={"channel": "general"})
        self.assertEqual(res.json()["stopped"], ["cli", "remote"])
        self.assertIsNotNone(r.proc.wait(timeout=5))
        deadline = time.time() + 5
        while time.time() < deadline:  # the backgrounded child is in the same group
            try:
                os.killpg(r.proc.pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            self.fail("child of the CLI process survived /stop")
        self.assertTrue(r.halt.is_set())
        self.assertEqual((self.queue("cli"), self.queue("remote")), ([], []))
        notice = self.store.get_recent(1)[0]
        self.assertEqual((notice["type"], notice["text"]), ("system", "@cli, @remote stopped by alice."))

    def test_stop_one_by_name_and_nothing_to_stop(self):
        self.join("a", "b")
        (self.d / "b_queue.jsonl").write_text(json.dumps({"channel": "general"}) + "\n")
        self.assertEqual(app.stop_agents(["@a"], "alice"), ["a"])
        self.assertEqual(len(self.queue("b")), 1)          # only the named agent
        self.assertEqual(app.stop_agents(["@nobody"], "alice"), [])

    def test_api_reply_arriving_after_stop_is_dropped_and_agent_works_again(self):
        store = self.store
        mgr = types.SimpleNamespace(hub=types.SimpleNamespace(store=store), specs={"qwen": {"kind": "api"}},
                                    set_inflight=lambda *a: None)
        r = agent_runner.Runner(mgr, "qwen")
        r._api_turn = lambda ch: (r.halt.set(), "late reply")[1]
        r._turn("general")
        self.assertEqual([m for m in store.get_recent(10) if m["sender"] == "qwen"], [])
        r.halt.clear()  # the trigger loop clears it before the next queue read
        r._api_turn = lambda ch: "back"
        r._turn("general")
        self.assertEqual(store.get_recent(1)[0]["text"], "back")


class ReactionTest(AppHarness):
    def test_toggle_persist_and_no_trigger(self):
        self.join("qwen")
        msg = self.store.add("alice", "@qwen ship it?")
        before = len(self.store.get_recent(100))
        c = TestClient(app.app, base_url="http://127.0.0.1")
        r = c.post(f"/api/messages/{msg['id']}/react", json={"emoji": "😂"})
        self.assertEqual(r.json()["reactions"], {"😂": ["alice"]})
        token = self.tokens["qwen"]
        r = c.post(f"/api/messages/{msg['id']}/react", json={"emoji": "😂"},
                   headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(r.json()["reactions"], {"😂": ["alice", "qwen"]})
        c.post(f"/api/messages/{msg['id']}/react", json={"emoji": "🙄"})
        r = c.post(f"/api/messages/{msg['id']}/react", json={"emoji": "😂"})  # own reaction: toggles off
        self.assertEqual(r.json()["reactions"], {"😂": ["qwen"], "🙄": ["alice"]})
        self.assertEqual(c.post(f"/api/messages/{msg['id']}/react", json={"emoji": "<b>"}).status_code, 400)
        self.assertEqual(c.post("/api/messages/999/react", json={"emoji": "👍"}).status_code, 404)
        # persisted with the message store
        again = MessageStore(str(self.d / "log.jsonl")).get_by_id(msg["id"])
        self.assertEqual(again["reactions"], {"😂": ["qwen"], "🙄": ["alice"]})
        # never a new message, never a trigger, never a loop-guard hop
        self.assertEqual(len(self.store.get_recent(100)), before)
        self.assertEqual(self.queue("qwen"), [])
        self.assertEqual(app.router._get_ch("general")["hop_count"], 0)

    def test_mcp_chat_react(self):
        self.join("claude")
        msg = self.store.add("alice", "hello")
        from mcp.server.fastmcp import Context
        ctx = Context(request_context=types.SimpleNamespace(request=types.SimpleNamespace(
            headers={"authorization": f"Bearer {self.tokens['claude']}"})))
        self.assertIn("Reacted", mcp_bridge.chat_react("claude", msg["id"], "🔥", ctx=ctx))
        self.assertIn("Removed", mcp_bridge.chat_react("claude", msg["id"], "🔥", ctx=ctx))
        self.assertNotIn("reactions", self.store.get_by_id(msg["id"]))


class ReactDirectiveTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = MessageStore(str(Path(tmp.name) / "log.jsonl"))
        self.mgr = types.SimpleNamespace(hub=types.SimpleNamespace(store=self.store),
                                         specs={"qwen": {"kind": "api"}}, set_inflight=lambda *a: None)
        self.target = self.store.add("alice", "my code is perfect")

    def turn(self, reply):
        r = agent_runner.Runner(self.mgr, "qwen")
        r._api_turn = lambda ch: reply
        r._turn("general")
        return [m["text"] for m in self.store.get_recent(10) if m["sender"] == "qwen"]

    def test_reaction_only_reply_posts_nothing(self):
        self.assertEqual(self.turn(f"[react #{self.target['id']} 🙄]"), [])
        self.assertEqual(self.store.get_by_id(self.target["id"])["reactions"], {"🙄": ["qwen"]})

    def test_directive_is_stripped_from_text_and_bad_ones_ignored(self):
        posted = self.turn(f"Bold claim. [react {self.target['id']} 🤡] [react #999 😂] [react #0 lol]")
        self.assertEqual(posted, ["Bold claim."])  # directives are always stripped; invalid ones do nothing
        self.assertEqual(self.store.get_by_id(self.target["id"])["reactions"], {"🤡": ["qwen"]})


class ThreadTest(AppHarness):
    def test_thread_reply_wakes_agent_with_thread_context_and_reply_lands_in_thread(self):
        self.join("qwen")
        self.store.add("alice", "main chatter that is not in the thread")
        parent = self.store.add("alice", "Release notes draft")
        self.post("alice", "@qwen check the wording", thread=parent["id"])
        self.assertEqual(self.queue("qwen")[0]["thread"], parent["id"])

        mgr = types.SimpleNamespace(hub=types.SimpleNamespace(store=self.store, registry=self.registry),
                                    specs={"qwen": {"kind": "api"}}, set_inflight=lambda *a: None,
                                    in_channel=lambda n, ch: True, data_dir=str(self.d))
        r = agent_runner.Runner(mgr, "qwen")
        seen = []

        def fake(spec, system, msgs, tools=None, tool_choice=None, **kw):
            seen.extend(m["content"] for m in msgs if isinstance(m["content"], str))
            return {"content": "Looks fine."}
        with patch.object(agent_runner, "call_api_message", fake), \
                patch.object(mcp_bridge, "get_role", lambda n: ""):
            r._system = lambda *a, **k: "SYS"
            r._turn("general", parent["id"])
        text = "\n".join(seen)
        self.assertIn("Release notes draft", text)
        self.assertIn("check the wording", text)
        self.assertNotIn("main chatter", text)
        reply = self.store.get_recent(1)[0]
        self.assertEqual((reply["sender"], reply["thread"]), ("qwen", parent["id"]))
        self.assertEqual([m["id"] for m in self.store.get_thread(parent["id"])][0], parent["id"])
        # the channel context of a later main-channel turn leaves thread replies out
        r.thread = None
        self.assertNotIn(reply["id"], [m["id"] for m in r._window("general", 30)])

    def test_remote_agent_send_with_thread(self):
        self.join("remote")
        parent = self.store.add("alice", "parent", channel="blah")
        token = self.tokens["remote"]
        c = TestClient(app.app, base_url="http://127.0.0.1")
        r = c.post("/api/send", json={"text": "in thread", "channel": "blah", "thread": parent["id"]},
                   headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(r.json()["thread"], parent["id"])
        bad = c.post("/api/send", json={"text": "x", "channel": "general", "thread": parent["id"]},
                     headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(bad.status_code, 400)  # parent lives in another channel


if __name__ == "__main__":
    unittest.main()


class IrcStopTest(unittest.TestCase):
    def test_irc_stop_uses_the_shared_stop_path(self):
        import irc_gateway
        calls = []
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        hub = types.SimpleNamespace(room_settings={"username": "Alice", "channels": ["general", "dev"]},
                                    store=MessageStore(str(Path(tmp.name) / "log.jsonl")),
                                    stop_agents=lambda names, by, ch: calls.append((names, by, ch)) or [])
        gw = irc_gateway.Gateway(hub, "", False, 0)
        gw.from_irc(None, "#dev", "/stop @qwen")
        gw.from_irc(None, "#dev", "!stop")
        self.assertEqual(calls, [(["@qwen"], "Alice", "dev"), (None, "Alice", "dev")])
        self.assertEqual(hub.store.get_recent(1)[0]["text"], "Nothing to stop.")
        self.assertEqual([m for m in hub.store.get_recent(10) if m["type"] == "chat"], [])  # not posted as chat
