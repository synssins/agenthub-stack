"""The trivia bank: levels, no repeats until a level runs out, flagged questions, and the game reading from it."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import games as G
import trivia as T


class Trivia(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        T.data_dir = G.data_dir = Path(tmp.name)

    def test_levels_no_repeats_and_flags(self):
        self.assertIsNone(T.draw(2))
        for i in range(3):
            self.assertTrue(T.add("own", 2, "TV", f"Question {i}?", f"A{i}"))
        self.assertFalse(T.add("own", 2, "TV", "question 0?", "again"))        # the same question twice: once
        self.assertFalse(T.add("own", 2, "TV", "No answer?", ""))
        T.add("own", 9, "Music", "Hard one?", "Yes")                          # levels stay within 1 to 4
        self.assertEqual(T.status()["levels"], {"1": 0, "2": 3, "3": 0, "4": 1})
        seen = [T.draw(2)["question"] for _ in range(3)]
        self.assertEqual(sorted(seen), ["Question 0?", "Question 1?", "Question 2?"])   # each once before any repeat
        again = T.draw(2)
        self.assertEqual(again["question"], seen[0])                           # all asked: the oldest comes round
        T.flag(again["id"])
        self.assertNotIn(seen[0], [T.draw(2)["question"] for _ in range(4)])
        self.assertEqual(T.status()["flagged"], 1)
        self.assertIsNone(T.draw(2, "Music"))
        self.assertEqual(T.draw(4, "mus")["answer"], "Yes")
        own = T.status()["own"]
        T.delete(own[0]["id"])
        self.assertEqual(len(T.status()["own"]), len(own) - 1)
        self.assertEqual(T.download("nope"), "no such source")

    def test_which_jeopardy_clues_are_level_four(self):
        j = T.jeopardy_level4
        self.assertTrue(j({"round": "3", "clue_value": "0", "air_date": "1990-01-01"}))
        self.assertTrue(j({"round": "2", "clue_value": "800", "air_date": "1990-01-01"}))
        self.assertFalse(j({"round": "2", "clue_value": "800", "air_date": "2010-01-01"}))   # after the values doubled
        self.assertTrue(j({"round": "2", "clue_value": "2000", "air_date": "2010-01-01"}))
        self.assertFalse(j({"round": "1", "clue_value": "1000", "air_date": "2010-01-01"}))
        self.assertFalse(j({"round": "x"}))

    def test_the_game_reads_from_the_bank(self):
        G.save({"enabled": True, "hosts": ["hazel"]})
        T.add("own", 3, "TV", "Who shot J.R.?", "Kristin Shepard")
        T.add("own", 3, "TV", "What bar is Cheers set in?", "Cheers")
        p = lambda *a, **k: G.play("hazel", "home", *a, **k)
        out = p("start", "Trivia", ["Alex"], level=3)
        self.assertIn("Level 3 of 4. Read this question out, word for word: [TV]", out)
        self.assertIn("The answer, which you keep to yourself", out)
        first = G._state()["home"]["q"]["question"]
        self.assertIn(first, G.now_lines("hazel", "home"))
        self.assertNotIn("Already asked", G.now_lines("hazel", "home"))
        out = p("result", player="Alex", points=1)
        self.assertIn("Score: Alex 1.", out)
        second = G._state()["home"]["q"]["question"]
        self.assertNotEqual(first, second)
        self.assertIn(second, out)
        self.assertIn("set aside for good", p("bad"))
        self.assertEqual(T.status()["flagged"], 1)
        p("end")
        out = p("start", "Trivia", ["Alex"], level=1)                          # nothing at this level: her own questions
        self.assertNotIn("Read this question", out)
        self.assertIn("Started. Trivia: round 1", out)


if __name__ == "__main__":
    unittest.main()
