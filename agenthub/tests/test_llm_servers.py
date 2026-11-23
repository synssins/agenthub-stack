import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import llm_servers


class LlmServers(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        llm_servers.data_dir = Path(tmp.name)
        patcher = mock.patch.object(llm_servers, "config_entries", lambda: [])
        patcher.start()
        self.addCleanup(patcher.stop)

    def specs(self):
        return {
            "a": {"name": "a", "kind": "api", "base_url": "http://127.0.0.1:8090/v1", "api_format": "openai",
                  "api_key": "k1", "model": "m-a"},
            "b": {"name": "b", "kind": "api", "base_url": "http://127.0.0.1:8090/v1", "api_format": "openai",
                  "api_key": "k1", "model": "m-b"},
            "c": {"name": "c", "kind": "api", "base_url": "http://127.0.0.1:8090/v1", "api_format": "openai",
                  "api_key": "k2", "model": "m-c"},
            "cc": {"name": "cc", "kind": "claude", "model": "opus"},
        }

    def test_migrate_groups_by_endpoint_and_key_and_pins_models(self):
        specs = self.specs()
        self.assertTrue(llm_servers.migrate(specs))
        servers = llm_servers.load()
        self.assertEqual(len(servers), 2)
        self.assertEqual(specs["a"]["llm"], specs["b"]["llm"])
        self.assertNotEqual(specs["a"]["llm"], specs["c"]["llm"])
        self.assertEqual([specs[n]["llm_model"] for n in "abc"], ["m-a", "m-b", "m-c"])
        for n in "abc":
            for k in ("api_key", "base_url", "api_format"):
                self.assertNotIn(k, specs[n])
        self.assertEqual(specs["cc"], {"name": "cc", "kind": "claude", "model": "opus"})
        self.assertEqual(sorted(s["api_key"] for s in servers), ["k1", "k2"])

    def test_migrate_is_idempotent(self):
        specs = self.specs()
        llm_servers.migrate(specs)
        self.assertFalse(llm_servers.migrate(specs))
        self.assertEqual(len(llm_servers.load()), 2)

    def test_migrate_names_after_preset_or_host(self):
        specs = {"o": {"name": "o", "kind": "api", "base_url": "https://openrouter.ai/api/v1", "model": "x"},
                 "h": {"name": "h", "kind": "api", "base_url": "http://10.0.0.5:9000/v1", "model": "y"}}
        llm_servers.migrate(specs)
        self.assertEqual(sorted(s["name"] for s in llm_servers.load()), ["10.0.0.5:9000", "OpenRouter"])

    def test_resolve_default_follows_server(self):
        llm_servers.upsert({"id": "srv-1", "name": "S", "base_url": "http://h/v1", "api_format": "openai",
                            "api_key": "k", "default_model": "one"})
        spec = {"kind": "api", "llm": "srv-1", "llm_model": ""}
        self.assertEqual(llm_servers.resolve(spec)["model"], "one")
        llm_servers.upsert({"id": "srv-1", "default_model": "two"})
        r = llm_servers.resolve(spec)
        self.assertEqual((r["model"], r["base_url"], r["api_key"], r["api_format"]), ("two", "http://h/v1", "k", "openai"))

    def test_resolve_pinned_model(self):
        llm_servers.upsert({"id": "srv-1", "name": "S", "base_url": "http://h/v1", "default_model": "one"})
        self.assertEqual(llm_servers.resolve({"llm": "srv-1", "llm_model": "pinned"})["model"], "pinned")

    def test_resolve_default_missing_gives_error(self):
        llm_servers.upsert({"id": "srv-1", "name": "Strata", "base_url": "http://h/v1"})
        self.assertEqual(llm_servers.resolve({"llm": "srv-1", "llm_model": ""}),
                         {"llm_error": "Strata has no default model; set one in Settings > LLMs"})

    def test_resolve_missing_server_gives_needs_server(self):
        self.assertEqual(llm_servers.resolve({"llm": "srv-gone", "llm_model": "x"}), {"llm_error": "needs an LLM server"})
        self.assertEqual(llm_servers.resolve({"llm_model": "x"}), {"llm_error": "needs an LLM server"})

    def test_single_model_becomes_auto_default_and_owner_default_wins(self):
        llm_servers.upsert({"id": "srv-1", "name": "S", "base_url": "http://h/v1"})
        llm_servers.note_models("srv-1", ["only"])
        self.assertEqual(llm_servers.get("srv-1")["default_model"], "only")
        llm_servers.note_models("srv-1", ["swapped"])  # still auto: follows the server
        self.assertEqual(llm_servers.get("srv-1")["default_model"], "swapped")
        llm_servers.upsert({"id": "srv-1", "default_model": "mine"})
        llm_servers.note_models("srv-1", ["other"])
        self.assertEqual(llm_servers.get("srv-1")["default_model"], "mine")
        llm_servers.note_models("srv-1", ["a", "b"])
        self.assertEqual(llm_servers.get("srv-1")["default_model"], "mine")

    def test_public_never_has_key(self):
        llm_servers.upsert({"id": "srv-1", "name": "S", "base_url": "http://h/v1", "api_key": "secret"})
        p = llm_servers.public(llm_servers.get("srv-1"))
        self.assertNotIn("api_key", p)
        self.assertTrue(p["has_api_key"])
        self.assertNotIn("secret", repr(p))

    def test_upsert_blank_key_keeps_stored(self):
        llm_servers.upsert({"id": "srv-1", "name": "S", "base_url": "http://h/v1", "api_key": "secret"})
        llm_servers.upsert({"id": "srv-1", "name": "S2", "base_url": "http://h/v1", "api_key": ""})
        self.assertEqual(llm_servers.get("srv-1")["api_key"], "secret")
        self.assertEqual(llm_servers.get("srv-1")["name"], "S2")

    def test_upsert_new_gets_id_and_validates(self):
        self.assertIsNone(llm_servers.upsert({"name": "N", "base_url": "http://h/v1"}))
        self.assertRegex(llm_servers.load()[0]["id"], r"^srv-[0-9a-f]{8}$")
        self.assertIn("base_url", llm_servers.upsert({"name": "N", "base_url": "ftp://x"}) or "")

    def test_remove_blocked_while_linked(self):
        llm_servers.upsert({"id": "srv-1", "name": "S", "base_url": "http://h/v1"})
        self.assertIn("unlink", llm_servers.remove("srv-1", ["a"]))
        self.assertIsNone(llm_servers.remove("srv-1", []))
        self.assertIsNone(llm_servers.get("srv-1"))

    def test_config_entries_are_readonly(self):
        with mock.patch.object(llm_servers, "config_entries",
                               lambda: [{"id": "cfg-x", "name": "x", "base_url": "http://c/v1", "readonly": True}]):
            self.assertEqual(llm_servers.get("cfg-x")["base_url"], "http://c/v1")
            self.assertIsNotNone(llm_servers.upsert({"id": "cfg-x", "name": "y", "base_url": "http://c/v1"}))
            self.assertIsNotNone(llm_servers.remove("cfg-x", []))

    def test_file_mode_600(self):
        llm_servers.upsert({"name": "S", "base_url": "http://h/v1", "api_key": "k"})
        mode = stat.S_IMODE(os.stat(llm_servers.data_dir / "llm_servers.json").st_mode)
        self.assertEqual(mode, 0o600)

    def test_migrate_gives_duplicate_names_a_number(self):
        specs = {"a": {"name": "a", "kind": "api", "base_url": "https://openrouter.ai/api/v1", "api_key": "k1", "model": "x"},
                 "b": {"name": "b", "kind": "api", "base_url": "https://openrouter.ai/api/v1", "api_key": "k2", "model": "y"}}
        llm_servers.migrate(specs)
        self.assertEqual(sorted(s["name"] for s in llm_servers.load()), ["OpenRouter", "OpenRouter 2"])

    def test_new_host_with_blank_key_drops_the_old_key(self):
        llm_servers.upsert({"id": "srv-1", "name": "S", "base_url": "https://a.example/v1", "api_key": "secret"})
        llm_servers.upsert({"id": "srv-1", "base_url": "https://a.example/api/v1", "api_key": ""})  # same host: kept
        self.assertEqual(llm_servers.get("srv-1").get("api_key"), "secret")
        llm_servers.upsert({"id": "srv-1", "base_url": "https://b.example/v1", "api_key": ""})
        self.assertNotIn("api_key", llm_servers.get("srv-1"))


if __name__ == "__main__":
    unittest.main()
