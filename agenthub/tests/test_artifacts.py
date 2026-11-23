"""Shared code artifacts: extraction from messages, versions, channel access, HTTP auth."""
import tempfile
import types
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import agent_runner
import agent_tools
import app
import artifacts
import mcp_bridge
from registry import RuntimeRegistry
from store import MessageStore


def block(n, info="powershell"):
    return f"```{info}\n" + "\n".join(f"Write-Host {i}" for i in range(n)) + "\n```"


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.store = MessageStore(str(self.root / "log.jsonl"))
        self.registry = RuntimeRegistry(data_dir=str(self.root))
        self.registry.seed({"bob": {"label": "Bob"}, "eve": {"label": "Eve"}})
        mgr = agent_runner.AgentManager(types.SimpleNamespace(registry=self.registry), str(self.root))
        mgr.set_member("bob", "ops", True)
        cfg = {"server": {"port": 8300, "data_dir": str(self.root)}}
        api = FastAPI()
        api.include_router(artifacts.router)
        for name, value in {"app": api, "config": cfg, "registry": self.registry, "store": self.store,
                            "agent_manager": mgr, "session_token": "tok",
                            "room_settings": {"username": "alice", "channels": ["general", "ops"]}}.items():
            self.stack.enter_context(patch.object(app, name, value))
        app._install_security_middleware("tok", cfg)
        artifacts.setup(app, cfg)
        self.client = self.stack.enter_context(TestClient(api, base_url="http://127.0.0.1"))
        self.bob = self.registry.register("bob")
        self.eve = self.registry.register("eve")

    def test_threshold_token_and_title(self):
        short = self.store.add("alice", "look\n" + block(7), channel="ops")
        self.assertIn("```powershell", short["text"])  # under 8 lines stays inline
        msg = self.store.add("alice", "here:\n" + block(8, "powershell:Reset-Users.ps1") + "\nthanks", channel="ops")
        aid = artifacts.TOKEN_RE.search(msg["text"]).group(1)
        self.assertEqual(msg["text"], f"here:\n[artifact:{aid}]\nthanks")
        meta = artifacts.store.get(aid)
        self.assertEqual((meta["title"], meta["language"], meta["filename"], meta["channel"], meta["created_by"]),
                         ("Reset-Users.ps1", "powershell", "Reset-Users.ps1", "ops", "alice"))
        self.assertEqual(meta["versions"][0]["lines"], 8)
        self.assertTrue(artifacts.store.content(aid).startswith("Write-Host 0\n"))
        sess = self.store.add("bob", block(9, "session"))
        self.assertIn("```session", sess["text"])  # session drafts are parsed by app.py

    def test_derived_title_and_filename(self):
        body = "# Clean up stale profiles\n" + "\n".join(["x = 1"] * 9)
        aid = artifacts.TOKEN_RE.search(self.store.add("alice", f"```python\n{body}\n```")["text"]).group(1)
        meta = artifacts.store.get(aid)
        self.assertEqual(meta["title"], "Clean up stale profiles")
        self.assertEqual(meta["filename"], "Clean-up-stale-profiles.py")
        self.assertEqual(artifacts.filename_for("x", "dockerfile"), "Dockerfile")
        self.assertEqual(artifacts.filename_for("x", "cmd"), "x.bat")
        self.assertEqual(artifacts.filename_for("x", "weird"), "x.txt")

    def test_versions_access_and_agent_rendering(self):
        msg = self.store.add("bob", block(10), channel="ops")
        aid = artifacts.TOKEN_RE.search(msg["text"]).group(1)
        self.assertIn(f'[artifact {aid} "powershell snippet" (powershell, 10 lines, v1) - read it with x]',
                      artifacts.render_for_agents(msg["text"], "x"))
        self.assertIn("Write-Host 9\n```", artifacts.render_for_agents(msg["text"], inline=True))
        # eve is not in #ops: no read, no update
        self.assertTrue(artifacts.read_for("eve", aid).startswith("error"))
        self.assertTrue(artifacts.update_for("eve", aid, "x").startswith("error"))
        self.assertIn("v2", artifacts.update_for("bob", aid, "new\ntext", "fix"))
        meta = artifacts.store.get(aid)
        self.assertEqual([v["n"] for v in meta["versions"]], [1, 2])
        self.assertEqual(artifacts.store.content(aid, 1).count("\n"), 9)
        self.assertEqual(artifacts.store.content(aid), "new\ntext")
        note = self.store.get_recent(1, channel="ops")[0]
        self.assertEqual(note["text"], "@bob updated powershell snippet to v2: fix")
        self.assertEqual(note["metadata"]["artifact"]["version"], 2)
        self.assertIn("version 1 of 2", artifacts.read_for("bob", aid, 1))

    def test_api_and_mcp_tools(self):
        msg = self.store.add("alice", block(8))
        aid = artifacts.TOKEN_RE.search(msg["text"]).group(1)
        self.assertIn("read it with chat_artifact_read", mcp_bridge._serialize_messages([msg]))
        runner = agent_tools.ToolRunner(str(self.root), "read", [], {}, str(self.root / "x"), agent="eve")
        self.assertIn("Write-Host 7", runner.run("artifact_read", {"id": aid}))  # #general: everyone
        self.assertIn("v2", runner.run("artifact_update", {"id": aid, "content": "a"}))
        self.assertIn("artifact_read", [t["function"]["name"] for t in agent_tools.tool_specs("read", [])])
        self.assertIn("version 2", mcp_bridge.chat_artifact_read("alice", aid))
        self.assertIn("v3", mcp_bridge.chat_artifact_update("alice", aid, "b"))

    def test_reply_block_updates_for_toolless_agents(self):
        aid = artifacts.TOKEN_RE.search(self.store.add("alice", block(8))["text"]).group(1)
        r = agent_runner.Runner(types.SimpleNamespace(hub=types.SimpleNamespace(store=self.store),
                                                      specs={"bob": {}}), "bob")
        reply = f"done\n[artifact-update {aid}]\n```powershell\nfixed\n```\n[/artifact-update]"
        self.assertEqual(r._artifact_updates(reply), [])
        self.assertEqual(artifacts.store.content(aid), "fixed")
        self.assertEqual(agent_runner.ARTIFACT_UPDATE_RE.sub("", reply).strip(), "done")

    def test_a_foreign_host_name_or_a_missing_session_cannot_reach_the_api(self):
        url, ok = "/api/artifacts/nope", {"X-Session-Token": "tok"}
        self.assertEqual(self.client.get(url, headers=ok).status_code, 404)                       # reached the route
        for host in ("evil.example:8300", "evil.example"):                                        # DNS rebinding
            self.assertEqual(self.client.get(url, headers={**ok, "Host": host}).status_code, 403)
        for host in ("localhost:8300", "192.168.1.50:8300", "[::1]:8300"):
            self.assertEqual(self.client.get(url, headers={**ok, "Host": host}).status_code, 404)
        self.assertEqual(self.client.post("/api/roles/bob", json={"role": "x"}).status_code, 403)   # setting a role needs the session
        self.assertNotEqual(self.client.get("/api/roles").status_code, 403)                       # reading stays open

    def test_http_auth(self):
        aid = artifacts.TOKEN_RE.search(self.store.add("alice", block(8), channel="ops")["text"]).group(1)
        url = f"/api/artifacts/{aid}"
        self.assertEqual(self.client.get(url).status_code, 403)                     # no credentials
        bad = {"Authorization": "Bearer nope"}
        self.assertEqual(self.client.get(url, headers=bad).status_code, 403)
        owner = self.client.get(url, headers={"X-Session-Token": "tok"})
        self.assertEqual(owner.json()["content"].count("\n"), 7)
        bob = {"Authorization": f"Bearer {self.bob['token']}"}
        eve = {"Authorization": f"Bearer {self.eve['token']}"}
        self.assertEqual(self.client.get(url, headers=bob).status_code, 200)
        self.assertEqual(self.client.get(url, headers=eve).status_code, 404)        # not in #ops
        self.assertEqual(self.client.post(url, headers=eve, json={"content": "x"}).status_code, 404)
        r = self.client.post(url, headers=bob, json={"content": "y", "note": "n"})
        self.assertEqual((r.status_code, r.json()["version"]), (200, 2))
        self.assertEqual(self.client.post(url, headers=bob, json={"content": ""}).status_code, 400)
        self.assertEqual(self.client.get(url + "?version=1", headers=bob).json()["version"], 1)
        self.assertEqual(self.client.get(url + "?version=9", headers=bob).status_code, 404)
        self.assertEqual(self.client.get("/api/artifacts/..%2F..", headers=bob).status_code, 404)
        r = self.client.post(url, headers={"X-Session-Token": "tok"}, json={"content": "z"})
        self.assertEqual(artifacts.store.get(aid)["versions"][-1]["author"], "alice")


if __name__ == "__main__":
    unittest.main()
