import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import voice


class SpeakableLinks(unittest.TestCase):
    def test_links_and_images_are_not_read_aloud(self):
        t = ("Official page: [Home Assistant Voice PE](https://www.home-assistant.io/voice-pe/). "
             "See https://example.com too.\n![The device](https://x/y.jpg)")
        out = voice.speakable(t)
        self.assertNotIn("Home Assistant Voice PE", out)
        self.assertNotIn("link", out)
        self.assertNotIn("The device", out)
        self.assertNotIn("http", out)
        self.assertIn("Official page", out)


if __name__ == "__main__":
    unittest.main()
