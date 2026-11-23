"""Voice recognition: matching, guided enrolling by reading the passage, and doing nothing when it is off."""
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import speakers as S

try:
    import numpy as np
except ImportError:
    np = None


@unittest.skipIf(np is None, "numpy is not installed")
class Speakers(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        S.data_dir, S._enroll = Path(tmp.name), None
        self.model = Path(tmp.name) / "m.onnx"
        self.model.write_bytes(b"x")
        self.voices = {"a": np.array([1, 0, 0], dtype=np.float32), "b": np.array([0, 1, 0], dtype=np.float32)}
        real, S.embed = S.embed, lambda wav, model: self.voices.get(wav.decode())
        self.addCleanup(setattr, S, "embed", real)

    def read(self, name, voice, device="echo-office"):
        """Enroll by reading the whole passage in two sentences."""
        self.assertIsNone(S.enroll_start(name, device))
        w = S.PASSAGE.split()
        S.enroll_heard(voice, 6.0, " ".join(w[:20]))
        S.enroll_heard(voice, 9.0, " ".join(w[20:]))

    def test_off_enroll_match_and_forget(self):
        self.assertEqual(S.heard(b"a", 3.0), "")                         # off: nothing
        self.assertEqual(S.enroll_start("Alex", "echo-office"), "set the model file first")
        self.assertEqual(S.settings({"enabled": True, "model": "/nope.onnx"}), "no such model file on the server")
        self.assertIsNone(S.settings({"enabled": True, "model": str(self.model)}))
        self.assertEqual(S.heard(b"a", 3.0, "echo-office"), "")           # nobody enrolled yet
        self.assertEqual(S.enroll_start(" ", "echo-office"), "give the person a name")
        self.read(" Alex ", b"a")
        self.read("Sam", b"b")
        self.assertEqual((S.heard(b"a", 2.0), S.heard(b"b", 2.0)), ("Alex", "Sam"))
        self.voices["c"] = np.array([0.7, 0.7, 0.14], dtype=np.float32) / np.linalg.norm([0.7, 0.7, 0.14])
        self.assertEqual(S.heard(b"c", 2.0), "")                          # between two people: nobody in particular
        self.voices["d"] = np.array([0, 0, 1], dtype=np.float32)
        self.assertEqual(S.heard(b"d", 2.0), "")                          # a stranger
        self.assertEqual(S.heard(b"zz", 2.0), "")                         # the model failed: nobody
        pub = S.public()
        self.assertEqual([(p["name"], p["samples"]) for p in pub["people"]], [("Alex", 2), ("Sam", 2)])
        self.assertNotIn("emb", str(pub["people"]))
        self.assertEqual(oct((S.data_dir / S.FILE).stat().st_mode & 0o777), "0o600")
        S.forget("Sam")
        self.assertEqual(S.heard(b"b", 2.0), "")

    def test_reading_is_followed_word_by_word(self):
        self.assertIsNone(S.settings({"enabled": True, "model": str(self.model)}))
        self.assertIsNone(S.enroll_start("Alex", "echo-office"))
        self.assertTrue(S.enrolling("echo-office"))
        self.assertFalse(S.enrolling("echo-living-room"))                 # other devices still reach the agents
        S.enroll_heard(b"a", 3.0, "The weather was fine, um")             # a filler after the words: the next word waits
        st = S.enroll_state()
        self.assertEqual((st["pos"], st["stuck"], st["samples"]), (4, True, 1))
        S.enroll_heard(b"a", 1.0, "uh")                                   # still waiting, and too short to learn from
        self.assertEqual((S.enroll_state()["pos"], S.enroll_state()["stuck"], S.enroll_state()["samples"]), (4, True, 1))
        S.enroll_heard(b"a", 3.0, "was fine this morning so I took")      # read again from two words back
        self.assertEqual((S.enroll_state()["pos"], S.enroll_state()["stuck"]), (9, False))
        S.enroll_heard(b"b", 4.0, "and now the news from the capital")    # a voice in the background: not learned
        st = S.enroll_state()
        self.assertEqual((st["pos"], st["stuck"], st["samples"]), (9, True, 2))
        self.assertTrue(S._same("neighbour", "neighbor"))
        S.enroll_skip()                                                   # the word is passed by hand
        st = S.enroll_state()
        self.assertEqual((st["pos"], st["stuck"], st["skipped"]), (10, False, [9]))
        S.enroll_heard(b"a", 9.0, "a neighbour " + " ".join(S.PASSAGE.split()[18:]))
        st = S.enroll_state()
        self.assertEqual((st["pos"], st["stuck"]), (10, True))            # "slow walk around ..." was never read
        S.enroll_heard(b"a", 9.0, " ".join(S.PASSAGE.split()[10:]))
        st = S.enroll_state()
        self.assertTrue(st["done"])
        self.assertFalse(S.enrolling("echo-office"))
        self.assertEqual(S.heard(b"a", 2.0), "Alex")
        S._enroll["at"] = 0
        S.enroll_cancel()
        self.assertIsNone(S.public()["enroll"])

    def test_an_enrollment_nobody_reads_to_ends(self):
        self.assertIsNone(S.settings({"enabled": True, "model": str(self.model)}))
        self.assertIsNone(S.enroll_start("Alex", "echo-office"))
        S._enroll["at"] = time.time() - S.ENROLL_IDLE - 1
        self.assertFalse(S.enrolling("echo-office"))
        self.assertIsNone(S.enroll_state())


if __name__ == "__main__":
    unittest.main()
