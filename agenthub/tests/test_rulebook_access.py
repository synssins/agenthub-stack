import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import rulebooks


class RulebookAccess(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        rulebooks.data_dir = Path(self.tmp.name) / "data"
        rulebooks.md_dir = Path(self.tmp.name) / "rulebooks"
        rulebooks.save([
            {"name": "Story writing", "description": "Fiction", "text": "SHOW DONT TELL", "channels": [], "agents": []},
            {"name": "Story review", "description": "Critique fiction", "text": "REVIEW", "channels": [], "agents": [],
             "readers": ["ann"]},
            {"name": "Code", "description": "Software", "text": "SMALL DIFFS", "channels": ["dev"], "agents": []},
        ])

    def tearDown(self):
        self.tmp.cleanup()

    def test_open_mode_keeps_unattached_books_out_of_the_prompt(self):
        p = rulebooks.prompt_for("bob", "general")
        self.assertNotIn("SHOW DONT TELL", p)
        self.assertIn("rulebook tool", p)
        self.assertIn("SMALL DIFFS", rulebooks.prompt_for("bob", "dev"))  # attached to the channel

    def test_lookup_returns_one_match_or_asks_which(self):
        self.assertIn("SHOW DONT TELL", rulebooks.lookup("bob", "story writing"))
        self.assertIn("SMALL DIFFS", rulebooks.lookup("bob", "software"))
        many = rulebooks.lookup("bob", "story")
        self.assertIn("Ask the user", many)
        self.assertNotIn("SHOW DONT TELL", many)
        self.assertIn("Code", rulebooks.lookup("bob", ""))

    def test_fine_grained_limits_to_readers_and_attachments(self):
        rulebooks.set_fine_grained(True)
        self.assertEqual([b["name"] for b in rulebooks.visible("ann", "general")], ["Story review"])
        self.assertEqual([b["name"] for b in rulebooks.visible("bob", "dev")], ["Code"])
        self.assertEqual(rulebooks.lookup("bob", "story", "general"), "No rulebooks are available to you.")
        rulebooks.set_fine_grained(False)
        self.assertEqual(len(rulebooks.visible("bob")), 3)


if __name__ == "__main__":
    unittest.main()
