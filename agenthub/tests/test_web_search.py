import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent_tools

WEB = {"results": [{"title": "KiCad - PCB", "url": "https://www.kicad.org/", "content": "Open source PCB design."},
                   {"title": "KiCad docs", "url": "https://docs.kicad.org/", "content": ""}]}
IMG = {"results": [{"title": "A cat", "url": "https://p.example/cat", "img_src": "https://i.example/cat.jpg",
                    "thumbnail_src": "https://t.example/cat.jpg"},
                   {"title": "no image", "url": "https://p.example/x", "img_src": ""}]}


class WebSearch(unittest.TestCase):
    def setUp(self):
        self.seen = []
        p = mock.patch.object(agent_tools, "_searxng_url", lambda: "http://sx:8082")
        p.start()
        self.addCleanup(p.stop)

    def fake(self, data):
        def urlopen(req, timeout=None):
            self.seen.append(req.full_url if hasattr(req, "full_url") else req)
            return io.BytesIO(json.dumps(data).encode())
        return urlopen

    def runner(self):
        return agent_tools.ToolRunner("~", "read", [], {}, "/nonexistent")

    def test_tool_offered_only_when_configured(self):
        self.assertIn("web_search", [t["function"]["name"] for t in agent_tools.tool_specs("read", [])])
        with mock.patch.object(agent_tools, "_searxng_url", lambda: ""):
            self.assertNotIn("web_search", [t["function"]["name"] for t in agent_tools.tool_specs("read", [])])

    def test_web_results_come_back_as_markdown_links(self):
        with mock.patch("urllib.request.urlopen", self.fake(WEB)):
            out = self.runner().run("web_search", {"query": "kicad"})
        self.assertIn("1. [KiCad - PCB](https://www.kicad.org/) — Open source PCB design.", out)
        self.assertIn("2. [KiCad docs](https://docs.kicad.org/)", out)
        self.assertIn("format=json", self.seen[0])
        self.assertIn("q=kicad", self.seen[0])

    def test_image_results_come_back_as_markdown_images(self):
        with mock.patch("urllib.request.urlopen", self.fake(IMG)):
            out = self.runner().run("web_search", {"query": "cat", "kind": "images"})
        self.assertIn("![A cat](https://i.example/cat.jpg)", out)
        self.assertIn("https://p.example/cat", out)
        self.assertNotIn("no image", out)
        self.assertIn("categories=images", self.seen[0])

    def test_count_is_capped_and_errors_are_reported(self):
        with mock.patch("urllib.request.urlopen", self.fake({"results": WEB["results"] * 20})):
            out = self.runner().run("web_search", {"query": "kicad", "count": 99})
        self.assertIn("15. ", out)
        self.assertNotIn("16. ", out)
        with mock.patch("urllib.request.urlopen", mock.Mock(side_effect=OSError("refused"))):
            self.assertIn("error", self.runner().run("web_search", {"query": "x"}))


if __name__ == "__main__":
    unittest.main()
