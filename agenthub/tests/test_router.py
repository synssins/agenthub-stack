import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from router import Router


class RouterMentionTests(unittest.TestCase):
    def test_zero_max_hops_turns_loop_guard_off(self):
        router = Router(["a", "b"], default_mention="none", max_hops=0)
        for _ in range(200):
            self.assertEqual(router.get_targets("a", "@b go"), ["b"])
        self.assertFalse(router.is_paused())
        router.max_hops = 2
        router.get_targets("a", "@b go")
        self.assertEqual(router.get_targets("a", "@b go"), [])  # 200+ hops already counted
        self.assertTrue(router.is_paused())

    def test_hyphenated_agent_name_is_parsed_as_full_mention(self):
        router = Router(["telegram-bridge"], default_mention="none")

        self.assertEqual(
            set(router.parse_mentions("please ask @telegram-bridge to check")),
            {"telegram-bridge"},
        )

    def test_shorter_agent_name_does_not_match_prefix_of_hyphenated_unknown(self):
        router = Router(["telegram"], default_mention="none")

        self.assertEqual(router.parse_mentions("@telegram-bridge check"), [])
        self.assertEqual(router.get_targets("ben", "@telegram-bridge check"), [])

    def test_longest_hyphenated_name_wins_when_prefix_agent_also_exists(self):
        router = Router(["telegram", "telegram-bridge"], default_mention="none")

        self.assertEqual(
            set(router.parse_mentions("@telegram-bridge check")),
            {"telegram-bridge"},
        )

    def test_unknown_exact_handle_still_does_not_route(self):
        router = Router(["telegram-bridge"], default_mention="none")

        self.assertEqual(router.parse_mentions("@telegram-bot check"), [])
        self.assertEqual(router.get_targets("ben", "@telegram-bot check"), [])


if __name__ == "__main__":
    unittest.main()


class DefaultAllOnlineOnly(unittest.TestCase):
    def test_plain_human_message_reaches_online_agents_only(self):
        from router import Router
        r = Router(["claude", "codex", "qwen3"], default_mention="all",
                   online_checker=lambda: {"claude", "qwen3"})
        self.assertEqual(sorted(r.get_targets("alice", "Hey guys. Sound off.")), ["claude", "qwen3"])
        self.assertEqual(r.get_targets("alice", "@codex hi"), ["codex"])
        self.assertEqual(r.get_targets("claude", "plain agent broadcast"), [])  # agents still need @mentions
