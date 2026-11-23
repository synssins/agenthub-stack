"""Discord gateway: settings, text conversion, both message directions (no Discord connection)."""

import asyncio
import base64
import os
import sys
import tempfile
import time
import types
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import discord_gateway as dg
from store import MessageStore

BOT_ID = 111111111111111111
GUILD = "222222222222222222"
TOKEN = base64.b64encode(str(BOT_ID).encode()).decode().rstrip("=") + ".abc.def"


class FakeUser:
    def __init__(self, uid, name, bot=False):
        self.id, self.display_name, self.bot = uid, name, bot


class FakePerms:
    view_channel = send_messages = connect = speak = True


class FakeGuild:
    def __init__(self, gid=GUILD, name="Sonorium Server"):
        self.id, self.name, self.me = int(gid), name, object()
        self.text_channels, self.voice_channels, self.system_channel = [], [], None

    def get_member(self, i):
        return None

    def get_role(self, i):
        return None

    def get_channel(self, i):
        return None


class FakeChannel:
    def __init__(self, cid=500, name="general", guild=None):
        self.id, self.name, self.guild = cid, name, guild or FakeGuild()
        self.sent = []
        self.guild.text_channels.append(self)

    def permissions_for(self, who):
        return FakePerms()

    async def send(self, text=None, reference=None, suppress_embeds=False, files=None, **kw):
        if files:
            self.sent.append((text, reference, [f.filename for f in files]))
        else:
            self.sent.append((text, reference))

    async def typing(self):
        pass


class FakeMessage:
    def __init__(self, author, content, mentions, channel, guild_id=None):
        self.author, self.content, self.mentions, self.channel = author, content, mentions, channel
        self.webhook_id = None
        self.role_mentions = []
        self.attachments = []
        self.guild = channel.guild if guild_id is None else FakeGuild(guild_id)

    def to_reference(self, fail_if_not_exists=True):
        return ("ref", self.author.id)


class FakeManager:
    def __init__(self):
        self.members = {}

    def set_member(self, name, channel, on):
        self.members.setdefault(channel, set()).add(name)


def make_bot(tmp, channels=()):
    dg.data_dir = Path(tmp)
    hub = types.SimpleNamespace(store=MessageStore(str(Path(tmp) / "log.jsonl")),
                                room_settings={"username": "owner", "channels": ["general"]},
                                config={"images": {"upload_dir": str(Path(tmp) / "uploads")}},
                                agent_manager=FakeManager())
    bot = dg.Bot(hub, {"id": "b", "token": TOKEN, "agent": "hazel", "guilds": [GUILD], "voice": False})
    by_id = {c.id: c for c in channels}
    bot.client = types.SimpleNamespace(user=FakeUser(BOT_ID, "Hazel", bot=True), get_channel=by_id.get,
                                       get_guild=lambda g: next((c.guild for c in channels if c.guild.id == g), None))
    bot.state = "online"
    return hub, bot


class Settings(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        dg.data_dir = Path(self.tmp)

    def test_save_keeps_token_and_is_private(self):
        dg.save_bot({"id": "Hazel", "token": TOKEN, "agent": "hazel", "guilds": GUILD})
        dg.save_bot({"id": "hazel", "token": "", "agent": "hazel", "guilds": f"{GUILD}, 333333333333333333"})
        [b] = dg.load()
        self.assertEqual((b["token"], b["guilds"]), (TOKEN, [GUILD, "333333333333333333"]))
        self.assertEqual(os.stat(Path(self.tmp) / dg.FILE).st_mode & 0o777, 0o600)

    def test_rejects_bad_ids(self):
        with self.assertRaises(ValueError):
            dg.save_bot({"id": "x", "agent": "a", "guilds": "my-server"})
        with self.assertRaises(ValueError):
            dg.save_bot({"id": "Bad Name", "agent": "a"})

    def test_invite(self):
        self.assertEqual(dg.app_id(TOKEN), str(BOT_ID))
        url = dg.invite_url({"token": TOKEN, "voice": True}, GUILD)
        self.assertIn(f"client_id={BOT_ID}", url)
        self.assertIn(f"permissions={dg.PERMS_TEXT | dg.PERMS_VOICE}", url)
        self.assertIn(f"guild_id={GUILD}&disable_guild_select=true", url)
        self.assertEqual(dg.invite_url({"token": "junk"}), "")


class Text(unittest.TestCase):
    def test_to_room(self):
        names = {("user", 5): "Bob"}
        t = dg.to_room(f"<@{BOT_ID}> hi <@!5> <:wave:123>", BOT_ID, "hazel", lambda k, i: names.get((k, i)))
        self.assertEqual(t, "@hazel hi @Bob :wave:")

    def test_to_discord(self):
        people = {"alice": 1, "big al": 2}
        self.assertEqual(dg.to_discord("[happy] Hi @Alice and @Big Al, @alicex", people, ["happy"]),
                         "Hi <@1> and <@2>, @alicex")
        # voice.all_tags() gives tags with their brackets
        self.assertEqual(dg.to_discord("Done. [happy] Off until set. [whispering] [unknown]", {}, ["[happy]", "[whispering]"]),
                         "Done. Off until set. [unknown]")

    def test_artifacts_inline_or_attached(self):
        import artifacts
        old = artifacts.store
        bodies = {"aaaaaa": ("Get-ADUser", "powershell", "Get users"), "bbbbbb": ("x" * 3000, "powershell", "Big script")}
        artifacts.store = types.SimpleNamespace(
            get=lambda a: {"title": bodies[a][2], "language": bodies[a][1]} if a in bodies else None,
            content=lambda a, v=None: bodies[a][0] if a in bodies else None)
        try:
            text, files = dg.expand_artifacts("Here:\n[artifact:aaaaaa]\nRun it.")
            self.assertEqual((text, files), ("Here:\n\n```powershell\nGet-ADUser\n```\n\nRun it.", []))
            text, files = dg.expand_artifacts("Big: [artifact:bbbbbb] and [artifact:zzzzzz]")
            self.assertEqual(text, "Big: (attached: Big-script.ps1) and [artifact:zzzzzz]")
            self.assertEqual([(len(b), n) for b, n in files], [(3000, "Big-script.ps1")])
        finally:
            artifacts.store = old

    def test_split(self):
        parts = dg.split("a" * 1500 + "\n" + "b" * 1500)
        self.assertEqual([len(p) for p in parts], [1500, 1500])
        self.assertTrue(all(len(p) <= 2000 for p in dg.split("x" * 5000)))


class Flow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_groups_mirror_and_reply_in_origin_channel(self):
        g = FakeGuild()
        gen, chat = FakeChannel(500, "general", g), FakeChannel(501, "chat", g)
        hub, bot = make_bot(self.tmp, [gen, chat])
        alice, bob = FakeUser(1, "Alice"), FakeUser(2, "Bob")
        me = bot.client.user

        async def go():
            await bot._from_discord(FakeMessage(alice, "not joined yet", [], gen))          # ignored
            await bot._from_discord(FakeMessage(alice, f"<@{BOT_ID}> hello", [me], gen))     # tag joins #general
            await bot._from_discord(FakeMessage(bob, "just chatting", [], gen))              # context only
            await bot._from_discord(FakeMessage(bob, f"<@{BOT_ID}> hi from chat", [me], chat))
            await bot._from_discord(FakeMessage(alice, f"<@{BOT_ID}> x", [me], chat, "999999999999999999"))  # other server
            room_gen, room_chat = bot.where_room(500), bot.where_room(501)
            self.assertEqual((room_gen, room_chat), ("sonorium-general", "sonorium-chat"))
            texts = [(m["channel"], m["sender"], m["text"], bool((m.get("metadata") or {}).get("discord_quiet")))
                     for m in hub.store.get_recent(10)]
            self.assertEqual(texts, [("sonorium-general", "Alice (Discord)", "@hazel hello", False),
                                     ("sonorium-general", "Bob (Discord)", "just chatting", True),
                                     ("sonorium-chat", "Bob (Discord)", "@hazel hi from chat", False)])
            self.assertEqual(hub.agent_manager.members, {"sonorium-general": {"hazel"}, "sonorium-chat": {"hazel"}})
            self.assertEqual(dg.group_rooms("sonorium-chat"), {"sonorium-general": "#general", "sonorium-chat": "#chat"})
            # replies go to the channel they were posted in, tagging the one who asked there
            await bot.to_discord({"channel": "sonorium-chat", "sender": "hazel", "text": "[#chat] Hi Bob"}, [])
            await bot.to_discord({"channel": "sonorium-general", "sender": "hazel", "text": "Hi Alice"}, [])
            await bot.to_discord({"channel": "sonorium-general", "sender": "owner", "text": "Owner here"}, [])
            await bot.to_discord({"channel": "sonorium-general", "sender": "system", "text": "x"}, [])
            await bot.to_discord({"channel": "general", "sender": "hazel", "text": "x"}, [])

        asyncio.run(go())
        self.assertEqual(chat.sent, [("<@2> Hi Bob", ("ref", 2))])
        self.assertEqual(gen.sent, [("<@1> Hi Alice", ("ref", 1)), ("Owner here", None)])

    def test_commands_join_leave_show(self):
        g = FakeGuild()
        gen, chat = FakeChannel(500, "general", g), FakeChannel(501, "chat", g)
        hub, bot = make_bot(self.tmp, [gen, chat])

        async def go():
            await bot._check_guild(g)   # a new server: its group starts with a channel
            room = bot.where_room(500)
            self.assertIn("#chat: can join", await bot.command(room, "/join"))
            self.assertIn("Joined #chat", await bot.command(room, "/join chat"))
            self.assertIn("#chat: joined", await bot.command(room, "/show channels"))
            self.assertIn("No channel", await bot.command(room, "/join nope"))
            self.assertIn("Voice is off", await bot.command(room, "/voice lounge"))
            self.assertIn("Left #chat", await bot.command(room, "/leave chat"))
            await bot._from_discord(FakeMessage(FakeUser(1, "Al"), "after leaving", [], chat))
            self.assertEqual(hub.store.get_recent(10, channel="sonorium-chat"), [])
            self.assertTrue(next(c for gr in dg.groups() for c in gr["channels"] if c["name"] == "sonorium-chat")["left"])

        asyncio.run(go())

    def test_expired_tag_gets_a_plain_message(self):
        ch = FakeChannel()
        hub, bot = make_bot(self.tmp, [ch])
        me = bot.client.user

        async def go():
            await bot._from_discord(FakeMessage(FakeUser(1, "Alice"), f"<@{BOT_ID}> q", [me], ch))
            await bot.to_discord({"channel": "sonorium-general", "sender": "hazel", "text": "@alice yes"}, [])
            await bot._from_discord(FakeMessage(FakeUser(2, "Bob"), f"<@{BOT_ID}> q", [me], ch))
            q = bot.pending[ch.id]
            q[0] = (time.monotonic() - dg.PENDING_S - 1, q[0][1])
            await bot.to_discord({"channel": "sonorium-general", "sender": "hazel", "text": "late"}, [])

        asyncio.run(go())
        self.assertEqual(ch.sent, [("<@1> yes", ("ref", 1)), ("late", None)])

    def test_room_names(self):
        self.assertEqual(dg.room_name("Sonorium: Ambient Soundscapes", "general", ["sonorium-general"]), "sonorium-general-2")
        self.assertRegex(dg.room_name("!!!", "a very long channel name indeed", []), r"^[a-z0-9][a-z0-9-]{0,19}$")


class Relink(unittest.TestCase):
    def test_copied_artifact_description_becomes_the_artifact(self):
        import artifacts
        old = artifacts.store
        artifacts.store = types.SimpleNamespace(get=lambda a: {"id": a} if a == "DuGgaQzC" else None)
        try:
            text = ('Again: [happy]\n\n[artifact DuGgaQzC "==== CONFIG - edit these, then run ====" (powershell, 50 lines, v1)'
                    ' - read it with artifact_read]\n\n[artifact Missing1 "x" (text, 1 lines, v1) - read it with artifact_read]')
            self.assertEqual(artifacts.relink(text), 'Again: [happy]\n\n[artifact:DuGgaQzC]\n\n'
                             '[artifact Missing1 "x" (text, 1 lines, v1) - read it with artifact_read]')
        finally:
            artifacts.store = old


class NoTools(unittest.TestCase):
    def test_permission_none_is_chat_only(self):
        import agent_tools
        names = {s["function"]["name"] for s in agent_tools.tool_specs("none", ["ls"], memory=True)}
        self.assertEqual(names, {"artifact_read", "artifact_update", "react", "remember", "forget_memory"})
        self.assertIn("read_file", {s["function"]["name"] for s in agent_tools.tool_specs("read", [])})
        r = agent_tools.ToolRunner("/tmp", "none", [], {}, protect="/nonexistent")
        self.assertIn("no tool read_file", r.run("read_file", {"path": "/etc/hostname"}))
        self.assertIn("no tool run_command", r.run("run_command", {"command": "id"}))


class SharedContext(unittest.TestCase):
    def test_agent_sees_the_whole_server_marked_by_channel(self):
        import agent_runner
        tmp = tempfile.mkdtemp()
        dg.data_dir = Path(tmp)
        store = MessageStore(str(Path(tmp) / "log.jsonl"))
        dg.save_links({"b": {GUILD: {"title": "S", "channels": {
            "1": {"room": "s-general", "label": "#general"}, "2": {"room": "s-chat", "label": "#chat"}}}}})
        store.add("Al (Discord)", "hi", channel="s-general")
        store.add("Bo (Discord)", "yo", channel="s-chat")
        store.add("hazel", "hello", channel="s-chat")
        store.add("owner", "elsewhere", channel="general")
        me = types.SimpleNamespace(name="hazel", m=types.SimpleNamespace(hub=types.SimpleNamespace(store=store)))
        got = [(m["channel"], m["text"]) for m in agent_runner.Runner._recent(me, "s-chat", 10)]
        self.assertEqual(got, [("s-general", "[#general] hi"), ("s-chat", "[#chat] yo"), ("s-chat", "hello")])
        self.assertEqual([m["text"] for m in agent_runner.Runner._recent(me, "general", 10)], ["elsewhere"])


class RoleTag(unittest.TestCase):
    def test_bot_role_counts_as_tag(self):
        ch = FakeChannel()
        hub, bot = make_bot(tempfile.mkdtemp(), [ch])
        role = types.SimpleNamespace(id=55, name="Hazel", tags=types.SimpleNamespace(bot_id=BOT_ID))
        other = types.SimpleNamespace(id=56, name="Mods", tags=None)
        m = FakeMessage(FakeUser(1, "Alice"), "<@&55> hi", [], ch)
        m.role_mentions = [role]
        n = FakeMessage(FakeUser(1, "Alice"), "<@&56> hi", [], ch)
        n.role_mentions = [other]
        asyncio.run(bot._from_discord(m))
        asyncio.run(bot._from_discord(n))
        self.assertEqual([(x["text"], bool(x.get("metadata"))) for x in hub.store.get_recent(10)],
                         [("@hazel hi", False), ("@role hi", True)])


class Files(unittest.TestCase):
    def test_attachments_go_with_the_reply(self):
        tmp = tempfile.mkdtemp()
        ch = FakeChannel()
        hub, bot = make_bot(tmp, [ch])
        up = Path(tmp) / "uploads"
        up.mkdir()
        (up / "a1.gif").write_bytes(b"GIF")
        (up / "big.png").write_bytes(b"xxxxxx")
        me = bot.client.user
        dg.MAX_FILE, old = 3, dg.MAX_FILE
        try:
            async def go():
                await bot._from_discord(FakeMessage(FakeUser(1, "Alice"), f"<@{BOT_ID}> a gif?", [me], ch))
                await bot.to_discord({"channel": "sonorium-general", "sender": "hazel", "text": "", "attachments": [
                    {"url": "/uploads/a1.gif", "name": "dance.gif"}, {"url": "/uploads/../x"},
                    {"url": "/uploads/missing.png"}]}, [])
                await bot.to_discord({"channel": "sonorium-general", "sender": "owner", "text": "", "attachments": [
                    {"url": "/uploads/big.png", "name": "big.png"}]}, [])
            asyncio.run(go())
        finally:
            dg.MAX_FILE = old
        self.assertEqual(ch.sent, [("<@1>", ("ref", 1), ["dance.gif"]),
                                   ("(big.png: over Discord's 10 MB limit)", None)])


class Smilies(unittest.TestCase):
    def setUp(self):
        import profiles
        self.tmp = tempfile.mkdtemp()
        dg.data_dir = profiles.data_dir = Path(self.tmp)

    def zipped(self, files):
        import io, zipfile
        b = io.BytesIO()
        with zipfile.ZipFile(b, "w") as z:
            for name, data in files.items():
                z.writestr(name, data)
        return b.getvalue()

    def test_add_list_remove(self):
        added = dg.add_smilies("hazel", "x.zip", self.zipped({
            "hazel_happy.gif": b"GIF89a1", "set/hazel_sad.png": b"\x89PNG\r\n\x1a\n2",
            "readme.txt": b"hi", "__MACOSX/hazel_angry.gif": b"GIF89a"}))
        self.assertEqual(sorted(added), ["happy", "sad"])
        dg.add_smilies("hazel", "Hazel_Happy.gif", b"GIF87a3")   # replaces
        self.assertEqual({e: p.read_bytes() for e, p in dg.smilies("hazel").items()},
                         {"happy": b"GIF87a3", "sad": b"\x89PNG\r\n\x1a\n2"})
        with self.assertRaises(ValueError):
            dg.add_smilies("hazel", "x.gif", b"not a picture")
        dg.remove_smiley("hazel", "sad")
        self.assertEqual(list(dg.smilies("hazel")), ["happy"])

    def test_prompt_needs_a_group_channel(self):
        self.assertEqual(dg.discord_prompt("hazel"), "")
        dg.save_bot({"id": "b", "agent": "hazel", "guilds": GUILD})
        self.assertEqual(dg.discord_prompt("hazel"), "")
        dg.save_links({"b": {GUILD: {"title": "S", "channels": {"5": {"room": "s-general", "label": "#general"}}}}})
        p = dg.discord_prompt("hazel")
        self.assertIn("#s-general", p)
        self.assertNotIn("[face:", p)
        dg.add_smilies("hazel", "m_wink.gif", b"GIF89a")
        p = dg.discord_prompt("hazel")
        self.assertIn("wink", p)
        self.assertIn("[face:<emotion>]", p)

    def test_rename_follows(self):
        dg.save_bot({"id": "b", "agent": "hazel", "guilds": GUILD})
        dg.rename_agent("hazel", "juniper")
        self.assertEqual(dg.load()[0]["agent"], "juniper")

    def test_face_marker_sends_the_picture(self):
        ch = FakeChannel()
        hub, bot = make_bot(self.tmp, [ch])
        dg.add_smilies("hazel", "m_laughing.gif", b"GIF89a")
        dg.add_smilies("hazel", "m_sad.gif", b"GIF89a")
        me = bot.client.user

        async def go():
            await bot._from_discord(FakeMessage(FakeUser(1, "Alice"), f"<@{BOT_ID}> joke?", [me], ch))
            room = "sonorium-general"
            await bot.to_discord({"channel": room, "sender": "hazel", "text": "Ha! [face:laughing]"}, [])
            await bot.to_discord({"channel": room, "sender": "hazel", "text": "Plain. [face:nonexistent]"}, [])
            await bot.to_discord({"channel": room, "sender": "owner", "text": "me [face:laughing]"}, [])
            await bot.to_discord({"channel": room, "sender": "hazel",
                                  "text": "All: [face:sad] [face:laughing] [face:sad]"}, [])

        asyncio.run(go())
        self.assertEqual(ch.sent, [("<@1> Ha!", ("ref", 1), ["laughing.gif"]), ("Plain.", None), ("me", None),
                                   ("All:", None, ["sad.gif", "laughing.gif"])])


class Owner(unittest.TestCase):
    def test_owner_is_known_by_id_not_name(self):
        hub, bot = make_bot(tempfile.mkdtemp())
        bot.owner_ids, bot.owner_names = {7}, {"morgan", "morganx"}
        self.assertEqual(bot.label(FakeUser(7, "Anything")), "Anything (Discord, owner)")
        self.assertEqual(bot.label(FakeUser(8, "Morgan")), "Morgan (Discord, not the owner)")
        self.assertEqual(bot.label(FakeUser(9, "OWNER")), "OWNER (Discord, not the owner)")   # the hub username
        self.assertEqual(bot.label(FakeUser(10, "Alice")), "Alice (Discord)")
        self.assertEqual(bot.label(FakeUser(7, "Anything"), "Discord voice"), "Anything (Discord voice, owner)")
        self.assertEqual(bot.label(FakeUser(11, "Aither", bot=True)), "Aither (Discord bot)")
        self.assertEqual(bot.label(FakeUser(12, "Bob (Discord, owner)")), "Bob Discord, owner (Discord)")


class Pictures(unittest.TestCase):
    def _gif(self):
        import subprocess
        out = Path(tempfile.mkdtemp()) / "t.gif"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=2",
                        str(out)], check=True)
        return out.read_bytes()

    def test_gif_becomes_three_frames_png_still_stays(self):
        data, name = dg.still(self._gif(), "dance.gif")
        self.assertTrue(data.startswith(b"\x89PNG"))
        self.assertEqual(name, "dance (3 frames).png")
        import struct
        w, h = struct.unpack(">II", data[16:24])
        self.assertEqual((w, h), (480, 120))   # three 160 px frames side by side
        self.assertEqual(dg.still(b"x", "a.png"), (b"x", "a.png"))
        self.assertIsNone(dg.still(b"not a picture", "a.gif"))

    def test_discord_picture_reaches_the_room_as_image(self):
        ch = FakeChannel()
        hub, bot = make_bot(tempfile.mkdtemp(), [ch])
        gif = self._gif()

        async def read():
            return gif
        m = FakeMessage(FakeUser(1, "Alice"), f"<@{BOT_ID}> look", [bot.client.user], ch)
        m.attachments = [types.SimpleNamespace(filename="dance.gif", url="https://cdn/x.gif", size=len(gif),
                                               content_type="image/gif", read=read)]
        asyncio.run(bot._from_discord(m))
        msg = hub.store.get_recent(5, channel="sonorium-general")[-1]
        self.assertEqual([(a["name"], a["kind"]) for a in msg["attachments"]], [("dance (3 frames).png", "image")])
        self.assertTrue((Path(hub.config["images"]["upload_dir"]) / msg["attachments"][0]["url"][9:]).is_file())


class Hold(unittest.TestCase):
    def test_holding_line_only_when_slow(self):
        ch = FakeChannel()
        hub, bot = make_bot(tempfile.mkdtemp(), [ch])
        me = bot.client.user

        async def go(wait):
            with mock.patch.object(dg, "HOLD_TEXT_S", 0.05):
                await bot._from_discord(FakeMessage(FakeUser(1, "Alice"), f"<@{BOT_ID}> hard one", [me], ch))
                if not wait:
                    await bot.to_discord({"channel": "sonorium-general", "sender": "hazel", "text": "Quick."}, [])
                await asyncio.sleep(0.15)
        asyncio.run(go(wait=False))
        self.assertEqual(ch.sent, [("<@1> Quick.", ("ref", 1))])
        ch.sent.clear()
        asyncio.run(go(wait=True))
        self.assertEqual(ch.sent, [("Let me think about that.", ("ref", 1))])


class Voice(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_wav_and_level(self):
        import wave, io
        import discord_voice as dv
        from array import array
        pcm = array("h", [1000, 1000] * 48000).tobytes()   # 1 s of 48 kHz stereo
        with wave.open(io.BytesIO(dv.to_wav16k(pcm))) as w:
            self.assertEqual((w.getframerate(), w.getnchannels(), w.getnframes()), (16000, 1, 16000))
        self.assertAlmostEqual(dv.rms(pcm), 1000, delta=1)

    def test_opus_round_trip_without_dave(self):
        import discord.opus
        import discord_voice as dv
        if not discord.opus.is_loaded():
            discord.opus._load_default()
        frame = discord.opus.Encoder().encode(b"\x00\x10" * 960 * 2, 960)
        vc = types.SimpleNamespace(_connection=types.SimpleNamespace(dave_session=None, dave_protocol_version=0))
        self.assertEqual(len(dv.decode(vc, 1, frame, {})), 960 * 2 * 2)
        self.assertEqual(dv.decode(vc, 1, b"junk", {}), b"")

    def test_hearing_ends_utterance_after_silence(self):
        import discord_voice as dv
        got = []

        async def go():
            async def heard(member, pcm):
                got.append((member.id, len(pcm)))
            h = dv.Hearing(asyncio.get_running_loop(), heard)
            from array import array
            loud = array("h", [3000, -3000] * 960).tobytes()   # 20 ms
            for _ in range(50):                                  # 1 s
                h.add(FakeUser(7, "Al"), loud)
            h.add(FakeUser(8, "Bo"), loud)                       # 20 ms: too short, dropped
            await asyncio.sleep(dv.SILENCE_S + 0.4)
            h.close()

        asyncio.run(go())
        self.assertEqual(got, [(7, 50 * 3840)])

    def test_voice_commands_and_spoken_reply(self):
        ch = FakeChannel()
        vc = FakeChannel(600, "lounge", ch.guild)
        hub, bot = make_bot(self.tmp, [ch, vc])
        me = bot.client.user
        replies, joined, said = [], [], []

        class Msg(FakeMessage):
            async def reply(self, text, mention_author=False):
                replies.append(text)

        async def fake_join(channel):
            joined.append(channel)

        async def fake_say(gid, text):
            said.append((gid, text))

        bot.join, bot._say = fake_join, fake_say
        alice = FakeUser(1, "Alice")

        async def go():
            await bot._from_discord(Msg(alice, f"<@{BOT_ID}> join", [me], ch))          # voice off
            bot.conf["voice"] = True
            await bot._from_discord(Msg(alice, f"<@{BOT_ID}> join", [me], ch))          # not in voice
            alice.voice = types.SimpleNamespace(channel=vc)
            await bot._from_discord(Msg(alice, f"<@{BOT_ID}> Join!", [me], ch))
            self.assertEqual(bot.where_room(600), "sonorium-lounge")
            bot.pending[600].append((time.monotonic(), dg.VoiceTurn(5, 1)))
            await bot.to_discord({"channel": "sonorium-lounge", "sender": "hazel", "text": "spoken"}, [])

        asyncio.run(go())
        self.assertEqual(replies, ["Voice is switched off for me.", "Join a voice channel first, then tag me with join."])
        self.assertEqual(joined, [vc])
        self.assertEqual(said, [(5, "spoken")])
        self.assertEqual(hub.store.get_recent(10, channel="sonorium-general"), [])   # commands are not posted
        self.assertEqual(ch.sent, [])
        self.assertEqual(vc.sent, [])


if __name__ == "__main__":
    unittest.main()
