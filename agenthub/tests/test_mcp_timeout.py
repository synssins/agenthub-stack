import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mcp_hub


class McpTimeout(unittest.TestCase):
    def test_hard_stop_follows_the_server_tool_timeout(self):
        """Regression: a 135 s cap cut off 5-minute image runs although the server allowed 1200 s."""
        seen = []

        async def fake_wait_for(coro, timeout):
            seen.append(timeout)
            coro.close()

        async def work():
            return None

        with mock.patch.object(mcp_hub.asyncio, "wait_for", fake_wait_for):
            mcp_hub._run(work(), {"timeout": 1200})
            mcp_hub._run(work(), {})
        self.assertEqual(seen, [1215, mcp_hub.CALL_TIMEOUT + 15])


if __name__ == "__main__":
    unittest.main()
