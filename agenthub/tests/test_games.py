"""Games: the host agent brings the questions, the module keeps the players, turn, round and score."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import games as G


class Games(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        G.data_dir = Path(tmp.name)
        import trivia
        trivia.data_dir = Path(tmp.name)

    def test_a_game_from_start_to_winner(self):
        self.assertEqual((G.tool_specs("hazel"), G.now_lines("hazel", "home")), ([], ""))      # off: nothing
        self.assertEqual(G.play("hazel", "home", "start", "Trivia", ["Alex"]), "error: you do not host games")
        G.save({"enabled": True, "hosts": ["@Hazel"], "games": [{"name": "Trivia", "rules": "Ask one question.", "rounds": 2},
                                                               {"name": "", "rules": "x"}, {"name": "NoRules"}]})
        self.assertEqual([g["name"] for g in G.load()["games"]], ["Trivia"])
        self.assertEqual(G.tool_specs("bo"), [])
        self.assertEqual(G.tool_specs("hazel")[0]["function"]["name"], "game")
        self.assertIn("You can host a game here", G.now_lines("hazel", "home"))
        p = lambda *a, **k: G.play("hazel", "home", *a, **k)
        self.assertIn("no such game. Games: Trivia", p("start", "Chess", ["Alex"]))
        self.assertIn("ask who is playing first", p("start", "trivia", []))
        self.assertIn("no game is running", p("score", player="Alex"))
        self.assertEqual(p("start", "trivia", ["Alex", " Sam ", "Alex"]), "Started. Trivia: round 1 of 2. Turn: Alex. Score: Alex 0, Sam 0.")
        self.assertIn("only the game tool changes: Trivia: round 1 of 2. Turn: Alex.", G.now_lines("hazel", "home"))
        self.assertIn("Rules: Ask one question.", G.now_lines("hazel", "home"))
        self.assertIn("You can host a game here", G.now_lines("hazel", "general"))          # the game is this channel's
        self.assertIn("no such player", p("score", player="Bob"))
        self.assertEqual(p("score", player="sam", points=2), "Trivia: round 1 of 2. Turn: Alex. Score: Alex 0, Sam 2.")
        self.assertIn("Turn: Sam", p("next"))
        self.assertIn("round 2 of 2. Turn: Alex", p("next"))
        p("score", player="Alex")
        p("next")
        self.assertEqual(p("next"), "The game is over. Final score: Alex 1, Sam 2. Winner: Sam.")   # the last round ended it
        self.assertIn("no game is running", p("state"))
        p("start", "Trivia", ["Ann", "Ben"])
        self.assertEqual(p("result", player="Ann", points=1), "Trivia: round 1 of 2. Turn: Ben. Score: Ann 1, Ben 0.")
        self.assertEqual(p("result", points=0), "Trivia: round 2 of 2. Turn: Ann. Score: Ann 1, Ben 0.")     # wrong: no point, next turn
        G.said("hazel", "home", "**Ann 1.** Right! Round 2:\n\nWhat is the **capital of Australia**? Take your time.")
        G.said("hazel", "home", "What is the capital of Australia?")
        G.said("bo", "home", "What is two plus two, really?")
        G.said("hazel", "general", "Which planet is the red one, then?")
        lines = G.now_lines("hazel", "home")
        self.assertEqual(lines.count("What is the capital of Australia?"), 1)
        self.assertNotIn("two plus two", lines)
        self.assertIn("never say a score the board does not show", lines)
        self.assertIn("Score: Ann 1, Ben 0.", lines)
        p("end")
        p("start", "Trivia", "Ann, Ben")
        self.assertEqual(p("end"), "The game is over. Final score: Ann 0, Ben 0.")

    def test_built_in_games_until_the_owner_edits_them(self):
        self.assertEqual([g["name"] for g in G.load()["games"]], [g["name"] for g in G.BUILT_IN])
        self.assertFalse(G.load()["enabled"])


if __name__ == "__main__":
    unittest.main()
