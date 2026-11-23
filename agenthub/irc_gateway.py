"""IRC gateway — lets any IRC client join the room.

Room channels appear as #channels and each registered agent as a nick in the
member list. Presence: an online agent is joined; a working agent is marked
away ("working", via IRCv3 away-notify) and sends a +typing tag; an offline
agent quits. Lines the IRC user sends post to the room as the room's human
username, so routing, @mentions and the loop guard behave exactly as in the
web UI.

Security: plaintext IRC with a shared password (PASS). Meant for a trusted
LAN only; put a TLS tunnel in front of it for anything wider.

Config (config.toml / config.local.toml):
    [irc]
    enabled = true
    host = "0.0.0.0"
    port = 6667
    password = "..."      # required unless host is loopback
    backlog = 50          # messages replayed per channel on join
"""

import asyncio
import hmac
import logging
import re
import time

log = logging.getLogger(__name__)

SERVER = "agenthub"
CAPS = {"away-notify", "message-tags", "server-time", "multi-prefix"}
_NICK_BAD = re.compile(r"[^A-Za-z0-9_\-\[\]\\`^{}|]")
_MAX_LINE = 400  # bytes of text per PRIVMSG, leaves room for prefix + tags


def irc_nick(name: str) -> str:
    """Room sender name -> valid IRC nick."""
    nick = _NICK_BAD.sub("_", name or "unknown")
    return nick if not nick[0].isdigit() and nick[0] != "-" else "_" + nick


def split_text(text: str, limit: int = _MAX_LINE) -> list[str]:
    """Split message text into IRC-sized lines (UTF-8 safe, keeps blank-line structure out)."""
    out = []
    for line in text.replace("\r", "").split("\n"):
        if not line.strip():
            continue
        data = line.encode("utf-8")
        while len(data) > limit:
            cut = limit
            while cut > 0 and (data[cut] & 0xC0) == 0x80:  # don't cut inside a UTF-8 char
                cut -= 1
            out.append(data[:cut].decode("utf-8"))
            data = data[cut:]
        out.append(data.decode("utf-8"))
    return out


def parse_line(line: str) -> tuple[str, list[str]]:
    """Parse a client line -> (COMMAND, params). Client tags and prefix are ignored."""
    if line.startswith("@"):
        line = line.split(" ", 1)[1] if " " in line else ""
    if line.startswith(":"):
        line = line.split(" ", 1)[1] if " " in line else ""
    if " :" in line:
        head, trailing = line.split(" :", 1)
        params = head.split() + [trailing]
    else:
        params = line.split()
    if not params:
        return "", []
    return params[0].upper(), params[1:]


class Client:
    def __init__(self, gw: "Gateway", reader, writer):
        self.gw = gw
        self.reader = reader
        self.writer = writer
        self.nick = "*"
        self.user_ok = False
        self.pass_ok = False
        self.registered = False
        self.caps: set[str] = set()
        self.cap_negotiating = False
        self.joined: set[str] = set()  # room channel names (no '#')
        self.parted: set[str] = set()  # channels the user left; no auto-rejoin
        self.own_ids: set[int] = set()  # messages this client sent (IRC clients echo locally)
        self.closed = False

    # --- output ---------------------------------------------------------
    def send(self, line: str, ts: float | None = None):
        if self.closed:
            return
        tags = []
        if "server-time" in self.caps and ts is not None:
            tags.append("time=" + time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts))
                        + ".%03dZ" % int((ts % 1) * 1000))
        if tags:
            line = "@" + ";".join(tags) + " " + line
        try:
            self.writer.write((line + "\r\n").encode("utf-8", "replace"))
        except Exception:
            self.closed = True

    def numeric(self, code: str, *params: str):
        *mid, last = params if params else ("",)
        self.send(" ".join([f":{SERVER}", code, self.nick, *mid]) + f" :{last}")

    # --- input ----------------------------------------------------------
    async def run(self):
        try:
            while not self.closed:
                raw = await asyncio.wait_for(self.reader.readline(), timeout=300)
                if not raw:
                    break
                cmd, params = parse_line(raw.decode("utf-8", "replace").rstrip("\r\n"))
                if cmd:
                    self.handle(cmd, params)
                    await self.writer.drain()
        except (asyncio.TimeoutError, ConnectionError, OSError):
            pass
        finally:
            self.closed = True
            self.gw.clients.discard(self)
            try:
                self.writer.close()
            except Exception:
                pass

    def handle(self, cmd: str, p: list[str]):
        if cmd == "PING":
            self.send(f":{SERVER} PONG {SERVER} :{p[0] if p else ''}")
        elif cmd == "PONG":
            pass
        elif cmd == "CAP":
            self.handle_cap(p)
        elif cmd == "PASS":
            self.pass_ok = bool(p) and self.gw.check_password(p[0])
        elif cmd == "NICK" and p:
            if not self.registered:
                self.nick = irc_nick(p[0])
            self.try_register()
        elif cmd == "USER":
            self.user_ok = True
            self.try_register()
        elif cmd == "QUIT":
            self.send(f"ERROR :Closing link")
            self.closed = True
        elif not self.registered:
            self.numeric("451", "You have not registered")
        elif cmd == "JOIN" and p:
            for ch in p[0].split(","):
                self.parted.discard(ch.lstrip("#"))
                self.gw.join(self, ch.lstrip("#"))
        elif cmd == "PART" and p:
            for ch in p[0].split(","):
                name = ch.lstrip("#")
                self.parted.add(name)
                if name in self.joined:
                    self.joined.discard(name)
                    self.send(f":{self.nick}!user@{SERVER} PART #{name}")
        elif cmd == "NAMES" and p:
            self.gw.send_names(self, p[0].lstrip("#"))
        elif cmd == "WHO" and p:
            self.gw.send_who(self, p[0])
        elif cmd == "PRIVMSG" and len(p) >= 2:
            self.gw.from_irc(self, p[0], p[1])
        elif cmd == "TOPIC" and p:
            self.gw.send_topic(self, p[0].lstrip("#"))
        elif cmd == "MODE" and p:
            if p[0].startswith("#"):
                self.numeric("324", p[0], "+nt")
            else:
                self.send(f":{self.nick} MODE {self.nick} :+i")
        elif cmd in ("LIST",):
            for ch in self.gw.channels():
                self.numeric("322", f"#{ch}", str(len(self.gw.members(ch))), "")
            self.numeric("323", "End of /LIST")
        elif cmd == "CONTINUE":
            self.gw.continue_routing()
        elif cmd in ("TAGMSG", "AWAY", "USERHOST", "ISON", "WHOIS"):
            pass  # accepted, nothing to do
        else:
            self.numeric("421", cmd, "Unknown command")

    def handle_cap(self, p: list[str]):
        sub = p[0].upper() if p else ""
        if sub == "LS":
            self.cap_negotiating = True
            self.send(f":{SERVER} CAP * LS :" + " ".join(sorted(CAPS)))
        elif sub == "REQ" and len(p) > 1:
            want = set(p[1].split())
            if want <= CAPS:
                self.caps |= want
                self.send(f":{SERVER} CAP {self.nick} ACK :{p[1]}")
            else:
                self.send(f":{SERVER} CAP {self.nick} NAK :{p[1]}")
        elif sub == "END":
            self.cap_negotiating = False
            self.try_register()

    def try_register(self):
        if self.registered or self.cap_negotiating or self.nick == "*" or not self.user_ok:
            return
        if not self.pass_ok and self.gw.password_required:
            self.numeric("464", "Password incorrect")
            self.send("ERROR :Password incorrect")
            self.closed = True
            return
        self.registered = True
        self.numeric("001", f"Welcome to the AgentHub room, {self.nick}")
        self.numeric("002", f"Your host is {SERVER}")
        self.numeric("004", SERVER, "agenthub", "o", "nt")
        self.numeric("005", "CHANTYPES=#", "PREFIX=(ov)@+", "NETWORK=AgentHub",
                      "are supported by this server")
        self.numeric("422", "MOTD File is missing")
        # Everything the human types posts as the room username: rename to match.
        human = irc_nick(self.gw.username())
        if human != self.nick:
            self.send(f":{self.nick}!user@{SERVER} NICK {human}")
            self.nick = human
        for ch in self.gw.channels():
            self.gw.join(self, ch)


class Gateway:
    def __init__(self, hub, password: str, password_required: bool, backlog: int):
        self.hub = hub  # the app module (store, registry, agents, router, room_settings)
        self.password = password
        self.password_required = password_required
        self.backlog = backlog
        self.clients: set[Client] = set()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.status: dict[str, dict] = {}  # agent name -> {"available", "busy"}

    # --- room accessors -------------------------------------------------
    def check_password(self, given: str) -> bool:
        return bool(self.password) and hmac.compare_digest(given.encode(), self.password.encode())

    def username(self) -> str:
        return self.hub.room_settings.get("username", "user")

    def channels(self) -> list[str]:
        return list(self.hub.room_settings.get("channels", ["general"]))

    def online_agents(self, ch: str | None = None) -> list[str]:
        return [n for n, s in self.status.items() if s.get("available")
                and (ch is None or not s.get("channels") or ch in s["channels"])]

    def members(self, ch: str) -> list[str]:
        return [irc_nick(self.username())] + [irc_nick(n) for n in self.online_agents(ch)]

    def continue_routing(self, channel: str | None = None):
        """Resume the loop guard in one channel, or in every channel when none is given."""
        for ch in ([channel] if channel else self.channels()):
            self.hub.router.continue_routing(ch)
            self.hub.store.add("system", "Resuming agent conversation...", msg_type="system", channel=ch)

    # --- per-client replies ----------------------------------------------
    def join(self, c: Client, ch: str):
        if ch not in self.channels():
            c.numeric("403", f"#{ch}", "No such channel (create it in the web UI)")
            return
        if ch in c.joined:
            return
        c.joined.add(ch)
        c.send(f":{c.nick}!user@{SERVER} JOIN #{ch}")
        self.send_topic(c, ch)
        self.send_names(c, ch)
        for name in self.online_agents(ch):
            if self.status[name].get("busy") and "away-notify" in c.caps:
                c.send(f":{irc_nick(name)}!agent@{SERVER} AWAY :working")
        for msg in self.hub.store.get_recent(self.backlog, channel=ch):
            self.deliver(c, msg)

    def send_topic(self, c: Client, ch: str):
        title = self.hub.room_settings.get("title") or "AgentHub"
        c.numeric("332", f"#{ch}", f"{title} — agents: {', '.join(self.online_agents(ch)) or 'none online'}")

    def send_names(self, c: Client, ch: str):
        names = [("@" if i == 0 else "") + n for i, n in enumerate(self.members(ch))]
        c.numeric("353", "=", f"#{ch}", " ".join(names))
        c.numeric("366", f"#{ch}", "End of /NAMES list")

    def send_who(self, c: Client, target: str):
        ch = target if target.startswith("#") else "*"
        nicks = self.members(target.lstrip("#")) if target.startswith("#") else [irc_nick(target)]
        online = {irc_nick(n): s for n, s in self.status.items()}
        for nick in nicks:
            away = "G" if online.get(nick, {}).get("busy") else "H"
            c.numeric("352", ch, "agent", SERVER, SERVER, nick, away, f"0 {nick}")
        c.numeric("315", target, "End of /WHO list")

    def deliver(self, c: Client, msg: dict):
        ch = msg.get("channel", "general")
        if msg.get("id") in c.own_ids:
            c.own_ids.discard(msg["id"])
            return
        if ch not in c.joined:
            if ch in c.parted or ch not in self.channels():
                return
            self.join(c, ch)  # join() replays backlog, which includes msg
            return
        kind = msg.get("type", "chat")
        import artifacts
        text = artifacts.render_plain(msg.get("text", ""))
        for a in msg.get("attachments") or []:
            text += f"\n[attachment] {a.get('url') or a.get('name', '')}"
        ts = msg.get("timestamp")
        sender = msg.get("sender", "")
        if kind == "chat" and sender and sender != "system":
            nick = irc_nick(sender)
            for line in split_text(text):
                c.send(f":{nick}!agent@{SERVER} PRIVMSG #{ch} :{line}", ts)
        else:
            for line in split_text(text):
                c.send(f":{SERVER} NOTICE #{ch} :{line}", ts)

    # --- room -> IRC -----------------------------------------------------
    def on_store_message(self, msg: dict):
        """Store callback; may run on any thread."""
        if self.loop:
            self.loop.call_soon_threadsafe(self._fanout, msg)

    def _fanout(self, msg: dict):
        for c in list(self.clients):
            if c.registered:
                self.deliver(c, msg)

    def update_status(self, new: dict[str, dict]):
        """Diff agent presence and tell IRC clients (JOIN/QUIT/AWAY/typing)."""
        old = self.status
        self.status = {n: {"available": bool(s.get("available")), "busy": bool(s.get("busy")),
                           "channels": s.get("channels")} for n, s in new.items()}
        for name in set(old) | set(self.status):
            was, now = old.get(name, {}), self.status.get(name, {})
            nick = irc_nick(name)
            src = f":{nick}!agent@{SERVER}"
            for c in list(self.clients):
                if not c.registered:
                    continue
                if now.get("available") and not was.get("available"):
                    for ch in c.joined:
                        if not now.get("channels") or ch in now["channels"]:
                            c.send(f"{src} JOIN #{ch}")
                elif was.get("available") and not now.get("available"):
                    c.send(f"{src} QUIT :offline")
                    continue
                if not now.get("available") or now.get("busy") == was.get("busy"):
                    continue
                if "away-notify" in c.caps:
                    c.send(f"{src} AWAY :working" if now["busy"] else f"{src} AWAY")
                if "message-tags" in c.caps:
                    state = "active" if now["busy"] else "done"
                    for ch in c.joined:
                        c.send(f"@+typing={state} {src} TAGMSG #{ch}")

    # --- IRC -> room -----------------------------------------------------
    def from_irc(self, c: Client, target: str, text: str):
        text = text.strip()
        if not text:
            return
        if text.startswith("\x01"):  # CTCP (ACTION etc.): keep the words, drop the framing
            text = text.strip("\x01").removeprefix("ACTION ").strip()
        if text.lower() in ("!continue", "/continue"):
            self.continue_routing(target.lstrip("#") if target.startswith("#") else None)
            return
        words = text.split()
        if words[0].lower() in ("!stop", "/stop"):  # /stop = all working agents, /stop @name = one
            ch = target[1:] if target.startswith("#") else "general"
            if not self.hub.stop_agents(words[1:] or None, self.username(), ch):
                self.hub.store.add("system", "Nothing to stop.", msg_type="system", channel=ch)
            return
        if target.startswith("#"):
            ch = target[1:]
            if ch not in self.channels():
                c.numeric("403", target, "No such channel")
                return
        else:
            # A private message to an agent nick becomes an @mention in #general.
            ch = "general"
            agent = next((n for n in self.status if irc_nick(n).lower() == target.lower()), target)
            text = f"@{agent} {text}"
        msg = self.hub.store.add(self.username(), text, channel=ch)
        if not target.startswith("#"):
            return  # rewritten text: let the client see the @mention form
        c.own_ids.add(msg["id"])

    async def poll_status(self):
        last_ping = time.monotonic()
        while True:
            try:
                self.update_status(self.hub.agents.get_status())
            except Exception:
                log.exception("IRC status poll failed")
            if time.monotonic() - last_ping > 60:  # keepalive; clients time out idle links
                last_ping = time.monotonic()
                for c in list(self.clients):
                    if c.registered:
                        c.send(f"PING :{SERVER}")
            await asyncio.sleep(1)

    async def _on_connect(self, reader, writer):
        peer = writer.get_extra_info("peername") or ("",)
        if not self.hub.client_allowed(peer[0]):
            writer.close()
            return
        c = Client(self, reader, writer)
        self.clients.add(c)
        await c.run()


async def start(hub, cfg: dict):
    """Start the gateway on the running event loop if [irc] enabled = true."""
    irc = cfg.get("irc", {})
    if not irc.get("enabled"):
        return None
    host = irc.get("host", "127.0.0.1")
    port = int(irc.get("port", 6667))
    password = str(irc.get("password", ""))
    loopback = host in ("127.0.0.1", "localhost", "::1")
    if not password and not loopback:
        log.error("IRC gateway not started: [irc] password is required when host is %s", host)
        return None
    gw = Gateway(hub, password, password_required=bool(password),
                 backlog=int(irc.get("backlog", 50)))
    gw.loop = asyncio.get_running_loop()
    hub.store.on_message(gw.on_store_message)
    server = await asyncio.start_server(gw._on_connect, host, port)
    asyncio.ensure_future(gw.poll_status())
    log.info("IRC gateway on %s:%d", host, port)
    return gw, server
