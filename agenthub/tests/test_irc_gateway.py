"""IRC gateway: registration, member list, presence, both message directions."""

import asyncio
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import irc_gateway
from store import MessageStore


class FakeAgents:
    def __init__(self):
        self.status = {"claude": {"available": True, "busy": False}}

    def get_status(self):
        return self.status


def make_hub(tmp):
    hub = types.SimpleNamespace(client_allowed=lambda ip: True)
    hub.store = MessageStore(str(Path(tmp) / "log.jsonl"))
    hub.room_settings = {"username": "Alice", "channels": ["general", "dev"], "title": "Room"}
    hub.agents = FakeAgents()
    hub.router = types.SimpleNamespace(continue_routing=lambda: None)
    return hub


class IrcGatewayTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.hub = make_hub(self.tmp.name)
        self.hub.store.add("claude", "earlier message", channel="general")
        cfg = {"irc": {"enabled": True, "host": "127.0.0.1", "port": 0, "password": "pw"}}
        self.gw, self.server = await irc_gateway.start(self.hub, cfg)
        self.port = self.server.sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        self.server.close()
        self.tmp.cleanup()

    async def connect(self, password="pw", caps=True):
        r, w = await asyncio.open_connection("127.0.0.1", self.port)
        if caps:
            w.write(b"CAP LS 302\r\n")
        w.write(f"PASS {password}\r\nNICK me\r\nUSER me 0 * :me\r\n".encode())
        if caps:
            w.write(b"CAP REQ :away-notify message-tags server-time\r\nCAP END\r\n")
        await w.drain()
        return r, w

    async def read_until(self, r, needle, timeout=3.0):
        lines = []
        async def go():
            while True:
                line = (await r.readline()).decode()
                if not line:
                    return
                lines.append(line.rstrip())
                if needle in line:
                    return
        await asyncio.wait_for(go(), timeout)
        return lines

    async def read_all(self, r, needles, timeout=3.0):
        """Read until every needle has appeared (order-independent)."""
        lines, left = [], set(needles)
        async def go():
            while left:
                line = (await r.readline()).decode()
                if not line:
                    return
                lines.append(line.rstrip())
                left.difference_update({n for n in left if n in line})
        await asyncio.wait_for(go(), timeout)
        return lines

    async def test_wrong_password_rejected(self):
        r, w = await self.connect(password="nope", caps=False)
        lines = await self.read_until(r, "ERROR")
        self.assertTrue(any(" 464 " in l for l in lines))
        w.close()

    async def test_join_names_backlog_and_both_directions(self):
        r, w = await self.connect()
        lines = await self.read_until(r, "earlier message")
        backlog_line = lines[-1]
        lines += await self.read_until(r, "#dev :End of /NAMES")
        joined = "\n".join(lines)
        self.assertIn("NICK Alice", joined)               # renamed to the room username
        self.assertIn("JOIN #general", joined)
        self.assertIn("JOIN #dev", joined)
        names = next(l for l in lines if " 353 " in l and "#general" in l)
        self.assertIn("@Alice", names)
        self.assertIn("claude", names)
        self.assertIn("@time=", backlog_line)                # server-time on backlog
        self.assertIn(":claude!agent@agenthub PRIVMSG #general :earlier message", backlog_line)

        # IRC -> room, posted as the human username, not echoed back
        w.write(b"PRIVMSG #dev :@claude hello there\r\n")
        await w.drain()
        await asyncio.sleep(0.2)
        last = self.hub.store.get_recent(1, channel="dev")[0]
        self.assertEqual((last["sender"], last["text"]), ("Alice", "@claude hello there"))

        # private message to an agent nick -> @mention in #general
        w.write(b"PRIVMSG claude :private words\r\n")
        await w.drain()
        await asyncio.sleep(0.2)
        last = self.hub.store.get_recent(1, channel="general")[0]
        self.assertEqual(last["text"], "@claude private words")

        # room -> IRC, multi-line split into separate PRIVMSGs
        self.hub.store.add("claude", "line one\nline two", channel="dev")
        lines = await self.read_until(r, "line two")
        self.assertFalse(any("hello there" in l for l in lines))  # own line not echoed
        self.assertTrue(any("PRIVMSG #dev :line one" in l for l in lines))
        w.close()

    async def test_presence_join_away_typing_quit(self):
        r, w = await self.connect()
        await self.read_until(r, "#dev :End of /NAMES")
        self.hub.agents.status = {"claude": {"available": True, "busy": True},
                                  "qwen": {"available": True, "busy": False}}
        lines = await self.read_all(r, ["qwen!agent@agenthub JOIN #dev",
                                        "qwen!agent@agenthub JOIN #general",
                                        "typing=active", "AWAY :working"])
        joined = "\n".join(lines)
        self.assertIn(":claude!agent@agenthub AWAY :working", joined)
        self.hub.agents.status = {"claude": {"available": True, "busy": False}}
        lines = await self.read_until(r, "QUIT :offline")
        self.assertTrue(any("qwen!agent@agenthub QUIT" in l for l in lines))
        w.close()


class ChannelMembersTest(unittest.TestCase):
    def test_members_per_channel(self):
        gw = irc_gateway.Gateway(types.SimpleNamespace(room_settings={"username": "Alice"}), "", False, 0)
        gw.update_status({"claude": {"available": True, "channels": ["general"]},
                          "irc-bot": {"available": True, "channels": ["general", "dev"]}})
        self.assertEqual(gw.members("general"), ["Alice", "claude", "irc-bot"])
        self.assertEqual(gw.members("dev"), ["Alice", "irc-bot"])


class HelpersTest(unittest.TestCase):
    def test_split_text_utf8_safe(self):
        parts = irc_gateway.split_text("é" * 300, limit=401)
        self.assertEqual("".join(parts), "é" * 300)
        self.assertTrue(all(len(p.encode()) <= 401 for p in parts))

    def test_nick_and_parse(self):
        self.assertEqual(irc_gateway.irc_nick("claude 2"), "claude_2")
        self.assertEqual(irc_gateway.irc_nick("2bot"), "_2bot")
        self.assertEqual(irc_gateway.parse_line("@a=b :x PRIVMSG #c :hi there"),
                         ("PRIVMSG", ["#c", "hi there"]))


if __name__ == "__main__":
    unittest.main()


class AllowlistTest(unittest.TestCase):
    def test_client_allowed(self):
        import ipaddress
        import app
        saved = list(app._allowed_networks)
        try:
            app._allowed_networks[:] = [ipaddress.ip_network("192.168.1.0/24")]
            for ip, ok in [("127.0.0.1", True), ("::1", True), ("192.168.1.40", True),
                           ("::ffff:192.168.1.40", True), ("10.0.0.5", False), ("192.168.2.1", False)]:
                self.assertEqual(app.client_allowed(ip), ok, ip)
        finally:
            app._allowed_networks[:] = saved
