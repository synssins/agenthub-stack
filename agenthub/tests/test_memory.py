import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent_runner
import agent_tools
import memory
import profiles
from registry import RuntimeRegistry
from store import MessageStore


class Memory(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.store = MessageStore(str(self.dir / "log.jsonl"))
        self.hub = types.SimpleNamespace(registry=RuntimeRegistry(data_dir=tmp.name), store=self.store,
                                         rules=types.SimpleNamespace(active_list=lambda: {"rules": []}))
        self.mgr = agent_runner.AgentManager(self.hub, tmp.name)
        self.mgr._apply = lambda name: None
        self.mgr.upsert({"name": "q", "kind": "api", "base_url": "http://x/v1", "model": "m", "cwd": tmp.name})

    def test_notes_are_capped_oldest_first_and_deduplicated(self):
        self.assertEqual(memory.add("q", "  likes\n blue ", 40), "saved to your memory")
        self.assertEqual(memory.add("q", "Likes blue", 40), "already in your memory")
        memory.add("q", "b" * 20, 40)
        self.assertIn("1 oldest notes dropped", memory.add("q", "c" * 15, 40))
        self.assertEqual(memory.notes("q"), ["b" * 20, "c" * 15])
        self.assertTrue(memory.add("q", "x", 0).startswith("error"))
        self.assertIn("removed 1 note", memory.forget("q", "BBB"))
        self.assertEqual((self.dir / "agents" / "q" / "memory.txt").read_text("utf-8"), "c" * 15 + "\n")

    def test_a_hand_edited_file_over_the_cap_is_cut_before_the_model_sees_it(self):
        profiles.write("q", "memory.txt", "old " * 30 + "\nnew fact")
        self.assertEqual(memory.notes("q", 20), ["new fact"])

    def test_tools_and_prompt_for_api_agents(self):
        names = [t["function"]["name"] for t in agent_tools.tool_specs("read", [], memory=True)]
        self.assertIn("remember", names)
        self.assertNotIn("remember", [t["function"]["name"] for t in agent_tools.tool_specs("read", [])])
        tr = agent_tools.ToolRunner(str(self.dir), "read", [], {}, protect=str(self.dir), agent="q")
        tr.memory_size = 4096
        self.assertEqual(tr.run("remember", {"note": "owner prefers tabs"}), "saved to your memory")
        system = agent_runner.Runner(self.mgr, "q")._system("general", dynamic=False)
        self.assertIn("- owner prefers tabs", system)
        self.assertIn("remember tool", system)
        self.assertIn("removed 1", tr.run("forget_memory", {"text": "tabs"}))

    def test_memory_off_leaves_the_prompt_and_tools_alone(self):
        self.mgr.specs["q"]["memory_size"] = 0
        self.assertNotIn("long-term memory", agent_runner.Runner(self.mgr, "q")._system("general", dynamic=False))

    def test_reply_tags_save_and_are_not_posted(self):
        self.mgr.upsert({"name": "c", "kind": "api", "api_format": "anthropic", "base_url": "http://x/v1", "model": "m"})
        r = agent_runner.Runner(self.mgr, "c")
        self.assertIn("[remember]", r._system("general", dynamic=False))
        r.thread = None
        r._act_reset()
        r._deliver("general", "Noted. [remember]Deploys happen on Fridays[/remember][forget]nothing[/forget]")
        self.assertEqual(memory.notes("c"), ["Deploys happen on Fridays"])
        self.assertEqual(self.store.get_recent(1)[0]["text"], "Noted.")

    def test_profile_follows_rename_and_goes_with_delete(self):
        memory.add("q", "a fact", 4096)
        self.mgr.set_persona("q", "I am Q.")
        self.assertEqual((self.dir / "agents" / "q" / "persona.md").read_text("utf-8"), "I am Q.\n")
        with mock.patch("mcp_bridge.migrate_identity", lambda *a: None), \
                mock.patch("admin_api.rename_grants", lambda *a: None), \
                mock.patch("rulebooks.rename_agent", lambda *a: None):
            self.hub.registry.rename = lambda old, new, label: None
            self.hub.registry.get_instance = lambda n: None
            self.store.rename_sender = lambda old, new: 0
            self.mgr.rename("q", "Quill")
            self.assertEqual(memory.notes("quill"), ["a fact"])
            self.assertEqual(profiles.read("quill", "persona.md"), "I am Q.")
            self.assertFalse((self.dir / "agents" / "q").exists())
            self.mgr.delete("quill")
        self.assertFalse((self.dir / "agents" / "quill").exists())

    def test_personas_move_from_the_settings_file_to_profile_folders(self):
        path = self.dir / "managed_agents.json"
        path.write_text(json.dumps([{"name": "old", "kind": "api", "base_url": "http://x/v1", "model": "m", "persona": "Old soul."}]), "utf-8")
        mgr = agent_runner.AgentManager(self.hub, str(self.dir))
        self.assertEqual(mgr.specs["old"]["persona"], "Old soul.")
        self.assertEqual(profiles.read("old", "persona.md"), "Old soul.")
        self.assertNotIn("persona", json.loads(path.read_text("utf-8"))[0])
        self.assertIn("Old soul.", (self.dir / "managed_agents.json.bak-before-profiles").read_text("utf-8"))
        again = agent_runner.AgentManager(self.hub, str(self.dir))   # as after a restart
        self.assertEqual(again.specs["old"]["persona"], "Old soul.")


if __name__ == "__main__":
    unittest.main()
