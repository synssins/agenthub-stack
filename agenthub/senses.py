"""Senses: what the room's agents know of the world outside the chat, and how they are heard in it.

One service inside AgentHub, with parts that are switched on one by one (Settings > Senses). Each
part names the agents that hear what it senses (the emotion engine makes them feel it) and the one
agent that may say it aloud (it needs Initiative on). Nothing is said unless a row in the settings
says so: a watched thing, a camera trigger, a reflex, or an event a publisher marked to be said.

  ha         Home Assistant: who is home, and the watched things. A row is one entity: whether its
             opening is said aloud, and whether it is brought up again while it stays open. Live over
             Home Assistant's WebSocket. Nothing is changed there, except by a reflex.
  cameras    UniFi Protect, with an API key. A trigger row is one explicit thing on one camera (the
             doorbell rang, a package, a person or a vehicle that stays, an alarm heard): once per
             visit, and one spoken camera event per gap whatever the camera. A row is always said,
             or handed to the agent with what the camera shows, to say only if it is worth it (the
             row can say when it is). Also one sentence per camera every few minutes for the agents'
             prompt. A frame is held in memory for one look; it is saved only when an agent is asked
             for a picture and attaches it to its reply.
  speakers   rooms and their Home Assistant speakers: what an agent says unprompted is spoken in the
             room with the latest motion, in the agent's own voice.
  display    a face over MQTT: <prefix>/state {"face","speaking"} (retained), <prefix>/gesture,
             <prefix>/mute; reads <prefix>/knob (press: acknowledge, long_press: mute).
  events     MQTT topics other services publish to: {"source", "description", "action"?: "speak",
             "serious"?: true, "key"?}.
  reflexes   a fixed question asked the moment something happens ("Front door open. Shall I turn the
             lights on?"), the yes or no heard by the speaker, and the one action written for yes.
             No model takes part, so it is immediate.

An answer after an agent spoke (a message to it, or a press of the knob) ends the reminders about
what is open, until it has closed and opened again. No answer within a few minutes is told to the
agent's feelings as being ignored.

Settings: data/senses.json (mode 600; the Home Assistant token and the camera key never go back to
the browser). Kept across restarts: data/senses_state.json (muted).
"""

from __future__ import annotations

import base64
import collections
import json
import logging
import os
import re
import secrets
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)
data_dir = Path("./data")     # set by start()
FILE, STATE, PATTERNS = "senses.json", "senses_state.json", "senses_patterns.json"
LEARN_DAYS = 7      # what happens is called unusual only after this many days of watching

# camera trigger -> (kind of Protect event, its detect type, what the agents are told, serious)
WHATS = {
    "ring": ("ring", "", "Someone rang the doorbell at the {cam}", False),
    "package": ("smart", "package", "A package was left at the {cam}", False),
    "person": ("smart", "person", "A person is at the {cam}", False),
    "vehicle": ("smart", "vehicle", "A vehicle is at the {cam}", False),
    "animal": ("smart", "animal", "An animal is at the {cam}", False),
    "smoke": ("audio", "alrmSmoke", "The {cam} camera hears a smoke alarm", True),
    "co": ("audio", "alrmCmonx", "The {cam} camera hears a carbon monoxide alarm", True),
    "glass": ("audio", "alrmGlassBreak", "The {cam} camera heard glass breaking", True),
    "burglar": ("audio", "alrmBurglar", "The {cam} camera hears a burglar alarm", True),
    "siren": ("audio", "alrmSiren", "The {cam} camera hears a siren", False),
}
STAYS = {"person": "A person has been at the {cam} for {n} seconds", "vehicle": "A vehicle has been stopped at the {cam} for {n} seconds",
         "animal": "An animal has been at the {cam} for {n} seconds"}
SERIOUS = {"smoke", "gas", "carbon_monoxide", "moisture", "safety"}     # Home Assistant device classes
OPENS = {"door", "window", "garage_door", "opening"}
NO_REFLEX = {"lock", "cover", "alarm_control_panel", "valve"}             # these take the passphrase: never a reflex
YES = ["yes", "yeah", "yep", "yes please", "sure", "please", "please do", "go ahead", "do it", "ok", "okay"]
NO = ["no", "nope", "no thanks", "no thank you", "don't", "leave it", "leave them off", "not now"]
DEFAULTS = {
    "enabled": False,
    "called": "",          # what the people in the house are called when an agent is told to tell them ("" = the room's user)
    "ha": {"enabled": False, "url": "", "token": "", "agents": [], "speaker": "", "notify": "", "watch": [],
           "unusual": False},     # something at an hour it has never happened at is handed to the speaker to weigh
    "cameras": {"enabled": False, "url": "", "api_key": "", "agents": [], "speaker": "", "describe_min": 5,
                "vision": {"server": "", "model": ""}, "gap_min": 10, "triggers": []},
    "speakers": {"enabled": False, "public_url": "", "rooms": [], "default_room": ""},
    "display": {"enabled": False, "prefix": "", "agent": ""},
    "events": {"enabled": False, "topics": [], "agents": [], "speaker": ""},
    "reflexes": [],
}
SECRETS = (("ha", "token"), ("cameras", "api_key"))


def _int(v, lo: int, hi: int, default: int) -> int:
    try:
        return max(lo, min(hi, int(float(v))))
    except (TypeError, ValueError):
        return default


def _s(v, n: int = 200) -> str:
    return str(v if v is not None else "").strip()[:n]


def _names(v) -> list[str]:
    return list(dict.fromkeys(_s(x, 64).lstrip("@").lower() for x in (v if isinstance(v, list) else []) if _s(x)))


def clean(raw) -> dict:
    """The settings as they are stored: every field present and in range. Unknown rows are dropped."""
    raw = raw if isinstance(raw, dict) else {}
    part = lambda k: raw.get(k) if isinstance(raw.get(k), dict) else {}
    rows = lambda v: [r for r in (v if isinstance(v, list) else []) if isinstance(r, dict)]
    ha, cam, spk, dis, ev = part("ha"), part("cameras"), part("speakers"), part("display"), part("events")
    vis = cam.get("vision") if isinstance(cam.get("vision"), dict) else {}
    out = {
        "enabled": raw.get("enabled") is True,
        "called": _s(raw.get("called"), 60),
        "ha": {"enabled": ha.get("enabled") is True, "url": _s(ha.get("url")).rstrip("/"), "token": _s(ha.get("token"), 600),
               "agents": _names(ha.get("agents")), "speaker": _s(ha.get("speaker"), 64).lstrip("@").lower(),
               "notify": re.sub(r"[^a-z0-9_]", "", _s(ha.get("notify"), 80).lower()), "unusual": ha.get("unusual") is True,
               "watch": [{"entity": _s(r.get("entity"), 120).lower(), "say": r.get("say") is True,
                          "remind_min": _int(r.get("remind_min"), 0, 1440, 0), "reminders": _int(r.get("reminders"), 0, 9, 0)}
                         for r in rows(ha.get("watch")) if re.fullmatch(r"[a-z_]+\.[a-z0-9_]+", _s(r.get("entity"), 120).lower())]},
        "cameras": {"enabled": cam.get("enabled") is True, "url": _s(cam.get("url")).rstrip("/"), "api_key": _s(cam.get("api_key"), 600),
                    "agents": _names(cam.get("agents")), "speaker": _s(cam.get("speaker"), 64).lstrip("@").lower(),
                    "describe_min": _int(cam.get("describe_min"), 0, 1440, 5),
                    "vision": {"server": _s(vis.get("server"), 80), "model": _s(vis.get("model"), 120)},
                    "gap_min": _int(cam.get("gap_min"), 0, 1440, 10),
                    # say: "" only felt, "always" said, "judge" the agent looks and says it only if it is worth it
                    "triggers": [{"camera": _s(r.get("camera"), 80), "what": r.get("what"), "stay_s": _int(r.get("stay_s"), 0, 600, 0),
                                  "say": "always" if r.get("say") is True else r.get("say") if r.get("say") in ("always", "judge") else "",
                                  "when": _s(r.get("when"), 300), "again_min": _int(r.get("again_min"), 1, 1440, 10)}
                                 for r in rows(cam.get("triggers")) if r.get("what") in WHATS]},
        "speakers": {"enabled": spk.get("enabled") is True, "public_url": _s(spk.get("public_url")).rstrip("/"),
                     "rooms": [{"name": _s(r.get("name"), 40), "speaker": _s(r.get("speaker"), 120).lower(), "motion": _s(r.get("motion"), 60).lower()}
                               for r in rows(spk.get("rooms")) if _s(r.get("name")) and _s(r.get("speaker")).startswith("assist_satellite.")],
                     "default_room": _s(spk.get("default_room"), 40)},
        "display": {"enabled": dis.get("enabled") is True, "prefix": re.sub(r"[^A-Za-z0-9_/-]", "", _s(dis.get("prefix"), 60)).strip("/"),
                    "agent": _s(dis.get("agent"), 64).lstrip("@").lower()},
        "events": {"enabled": ev.get("enabled") is True, "agents": _names(ev.get("agents")),
                   "speaker": _s(ev.get("speaker"), 64).lstrip("@").lower(),
                   "topics": [t for t in dict.fromkeys(_s(x, 120) for x in (ev.get("topics") if isinstance(ev.get("topics"), list) else []))
                              if t and not re.search(r"[#+\s]", t)]},
        "reflexes": [{"name": _s(r.get("name"), 60), "entity": _s(r.get("entity"), 120).lower(), "state": _s(r.get("state"), 40) or "on",
                      "question": _s(r.get("question"), 300), "service": _s(r.get("service"), 80).lower(),
                      "target": _s(r.get("target"), 120).lower(), "again_min": _int(r.get("again_min"), 1, 1440, 10),
                      "enabled": r.get("enabled") is True}
                     for r in rows(raw.get("reflexes")) if _s(r.get("name")) and _s(r.get("question"))],
    }
    return out


def problems(conf: dict) -> str | None:
    """What stops these settings from being saved, in words for the form."""
    for part, label in (("ha", "Home Assistant"), ("cameras", "The cameras")):
        if conf[part]["url"] and not re.match(r"https?://[^/\s]+", conf[part]["url"]):
            return f"{label}: the address starts with http:// or https://"
    if conf["speakers"]["public_url"] and not re.match(r"https?://[^/\s]+", conf["speakers"]["public_url"]):
        return "Speakers: AgentHub's address starts with http:// or https://"
    for r in conf["reflexes"]:
        if not r["enabled"]:
            continue
        if not re.fullmatch(r"[a-z_]+\.[a-z0-9_]+", r["entity"]):
            return f"Reflex \"{r['name']}\": pick what sets it off"
        if not re.fullmatch(r"[a-z_]+\.[a-z0-9_]+", r["service"]) or not re.fullmatch(r"[a-z_]+\.[a-z0-9_]+", r["target"]):
            return f"Reflex \"{r['name']}\": pick the action for yes and what it acts on"
        if r["service"].split(".")[0] in NO_REFLEX or r["target"].split(".")[0] in NO_REFLEX:
            return (f"Reflex \"{r['name']}\": doors, locks, covers and alarms are never a reflex. They are opened by an "
                    "agent, after the passphrase")
    return None


def load() -> dict:
    try:
        return clean(json.loads((data_dir / FILE).read_text("utf-8")))
    except (OSError, ValueError):
        return clean({})


def save(change: dict) -> tuple[dict, str | None]:
    """Merge `change` (whole parts, by key) into the stored settings. A blank token or key keeps the
    stored one. -> (settings, error)."""
    old = load()
    new = {**old, **{k: v for k, v in (change or {}).items() if k in DEFAULTS}}
    conf = clean(new)
    for part, key in SECRETS:
        if not conf[part][key]:
            conf[part][key] = old[part][key]
    err = problems(conf)
    if err:
        return old, err
    tmp = data_dir / "senses.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)   # holds a token and a key
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(conf, f, indent=2)
    os.replace(tmp, data_dir / FILE)
    return conf, None


def public(conf: dict) -> dict:
    """The settings for the browser: secrets become has_token / has_key."""
    out = json.loads(json.dumps(conf))
    out["ha"]["has_token"] = bool(out["ha"].pop("token"))
    out["cameras"]["has_key"] = bool(out["cameras"].pop("api_key"))
    return out


def _http(url: str, body=None, headers: dict | None = None, timeout: float = 20, raw: bool = False, ctx=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        out = r.read()
    return out if raw else json.loads(out or b"null")


def _lan_tls():
    """A console on the LAN signs its own certificate: it is used unchecked, for that address only."""
    ctx = ssl.create_default_context()
    ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
    return ctx


def words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9']+", (text or "").lower()))


class Senses:
    def __init__(self, hub):
        self.hub = hub                         # app: .agent_manager, .store, .room_settings
        self.conf = load()
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.status: dict[str, str] = {}       # part -> "connected", or what is wrong
        self.heard: dict[str, float] = {}      # part -> when its live connection last told something
        self.activity: collections.deque = collections.deque(maxlen=120)   # newest first, for the settings page
        self.journal: collections.deque = collections.deque(maxlen=12)     # (when, what) for the agents' prompt
        self.names: dict[str, str] = {}        # Home Assistant entity -> its name
        self.kinds: dict[str, str] = {}        # entity -> device class, or its domain
        self.states: dict[str, str] = {}       # entity -> state, for what is followed
        self.motion: set[str] = set()          # the motion sensors of the rooms
        self.motion_at: dict[str, float] = {}
        self.open: dict[str, dict] = {}        # watched thing that is open now -> {"since", "next", "left", "acked"}
        self.cams: dict[str, str] = {}         # Protect camera id -> name (connected ones)
        self.scenes: dict[str, tuple[float, str]] = {}   # camera -> (when, one sentence)
        self.live: dict[tuple, dict] = {}      # (Protect event id, trigger row) -> {"fire_at", "cam", "what", "row"}
        self.fired: dict[tuple, float] = {}    # (what, camera) -> when a trigger last fired
        self.cam_said = 0.0                    # when a camera event was last said, or handed over to be weighed
        self.cam_waiting: dict | None = None   # the latest camera event held back by that; said after all if the agent lets the first pass
        self.asked: dict[str, float] = {}      # reflex -> when it last asked
        self.muted = bool(self._state().get("muted"))
        self.pending: dict | None = None       # what an agent last said aloud, until answered or ignored
        self.ignored = 0
        self.told: dict[str, int] = {}         # key -> times it was said aloud
        self.told_at: dict[str, float] = {}
        self.last_reply: dict[str, tuple[float, set]] = {}   # agent -> (when, its words): its speech heard back is no answer
        self.audio: dict[str, tuple[bytes, str, float]] = {}  # one-time name -> (audio, media type, until)
        self.speak_until = 0.0
        self.shown: tuple | None = None
        self.shown_at = 0.0
        self.mqtt = None
        self.loose: list[dict] = []            # what an agent said aloud and never got an answer to: it may return to it once
        try:   # what happens when, learned over time: "entity=state" -> {"first": when, "h": {hour of the week: times}}
            self.patterns: dict = json.loads((data_dir / PATTERNS).read_text("utf-8"))
        except (OSError, ValueError):
            self.patterns = {}
        self.learned_at = 0.0

    # ---------------------------------------------------------------- what is usual

    @staticmethod
    def _slot(ts: float) -> int:
        from datetime import datetime
        import emotion_engine
        t = datetime.fromtimestamp(ts, emotion_engine.now_local().tzinfo)
        return t.weekday() * 24 + t.hour

    def learn(self, ent: str, state: str, ts: float | None = None):
        ts = time.time() if ts is None else ts
        p = self.patterns.setdefault(f"{ent}={state}", {"first": ts, "h": {}})
        p["first"] = min(p["first"], ts)
        p["h"][str(self._slot(ts))] = p["h"].get(str(self._slot(ts)), 0) + 1
        self.learned_at = time.time()

    def unusual(self, ent: str, state: str, ts: float | None = None) -> bool:
        """Has this never happened at this hour of the week, nor the hour before or after, in at least
        LEARN_DAYS of watching? Asked before the event is learned."""
        ts = time.time() if ts is None else ts
        first = min((p["first"] for k, p in self.patterns.items() if k.startswith(ent + "=")), default=ts)
        if ts - first < LEARN_DAYS * 86400:
            return False
        h, slot = self.patterns.get(f"{ent}={state}", {}).get("h", {}), self._slot(ts)
        return not any(h.get(str((slot + d) % 168)) for d in (-1, 0, 1))

    def learn_history(self):
        """Once: the last weeks of the followed things from Home Assistant's own history, so what is
        usual is known from the first day."""
        if self.patterns:
            return
        from datetime import datetime, timedelta, timezone
        ids = [e for e in self.followed() if e not in self.motion]
        if not ids:
            return
        start = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        end = datetime.now(timezone.utc).isoformat()   # without an end Home Assistant gives one day only
        rows = self.ha("/api/history/period/" + urllib.parse.quote(start) + "?minimal_response&no_attributes&end_time="
                       + urllib.parse.quote(end) + "&filter_entity_id=" + ",".join(ids), timeout=90)
        for series in rows or []:
            ent, was = (series[0] or {}).get("entity_id"), None
            for x in series:
                st = x.get("state")
                try:
                    ts = datetime.fromisoformat(x["last_changed"]).timestamp()
                except (KeyError, ValueError):
                    continue
                if ent and st not in ("unavailable", "unknown", None) and st != was:
                    self.learn(ent, st, ts)
                    was = st
        self.save_patterns()

    def save_patterns(self):
        try:
            tmp = data_dir / "senses_patterns.tmp"
            tmp.write_text(json.dumps(self.patterns), "utf-8")
            tmp.replace(data_dir / PATTERNS)
            self.learned_at = 0.0
        except OSError:
            log.warning("senses: what is usual could not be saved")

    # ---------------------------------------------------------------- small helpers

    def _state(self) -> dict:
        try:
            return json.loads((data_dir / STATE).read_text("utf-8"))
        except (OSError, ValueError):
            return {}

    def note(self, part: str, text: str, did: str):
        """One line of the activity list on the settings page."""
        self.activity.appendleft({"at": time.time(), "part": part, "text": text[:300], "did": did})
        log.info("senses %s: %s -> %s", part, text[:120], did)

    @property
    def called(self) -> str:
        return self.conf["called"] or str((getattr(self.hub, "room_settings", None) or {}).get("username") or "the people here")

    def spec(self, agent: str) -> dict:
        return (self.hub.agent_manager.specs.get(agent) or {}) if agent else {}

    def people_home(self) -> bool:
        people = [v for e, v in self.states.items() if e.startswith("person.")]
        return not people or "home" in people

    def quiet(self, agent: str) -> bool:
        """The agent's own quiet hours (Agents > (agent) > Initiative)."""
        import initiative
        return initiative.quiet(initiative.clean(self.spec(agent).get("initiative")))

    def speakers_of(self) -> set[str]:
        c = self.conf
        return {c[p]["speaker"] for p in ("ha", "cameras", "events") if c[p]["enabled"] and c[p]["speaker"]}

    def targets(self) -> set[str]:
        c = self.conf
        return {a for p in ("ha", "cameras", "events") if c[p]["enabled"] for a in c[p]["agents"]}

    # ---------------------------------------------------------------- Home Assistant

    def _ha_auth(self) -> str:
        """"Bearer <token>": the one in the settings, else the MCP server's at the same address."""
        c = self.conf["ha"]
        if c["token"]:
            return "Bearer " + c["token"]
        try:
            servers = json.loads((data_dir / "mcp_servers.json").read_text("utf-8"))
        except (OSError, ValueError):
            servers = []
        for s in servers if isinstance(servers, list) else []:
            if c["url"] and str(s.get("url") or "").startswith(c["url"]):
                auth = next((v for k, v in (s.get("headers") or {}).items() if k.lower() == "authorization"), "")
                if auth:
                    return auth
        raise RuntimeError("no Home Assistant token: enter one, or add its MCP server at the same address")

    def ha(self, path: str, body=None, timeout: float = 20):
        return _http(self.conf["ha"]["url"] + path, body, {"Authorization": self._ha_auth()}, timeout)

    def followed(self) -> list[str]:
        """The entities whose changes are asked for: people, watched things, room motion, reflex triggers."""
        c = self.conf
        ids = [e for e in self.kinds if e.startswith("person.")] + [r["entity"] for r in c["ha"]["watch"]]
        ids += sorted(self.motion) + [r["entity"] for r in c["reflexes"] if r["enabled"]]
        return list(dict.fromkeys(ids))

    def ha_baseline(self):
        """Every state once: names, kinds, who is home, the rooms' motion sensors, what is open now."""
        rooms = [r["motion"] for r in self.conf["speakers"]["rooms"] if r["motion"]]
        watch = {r["entity"] for r in self.conf["ha"]["watch"]} | {r["entity"] for r in self.conf["reflexes"]}
        for s in self.ha("/api/states", timeout=60):
            ent, st = s.get("entity_id", ""), s.get("state")
            attrs = s.get("attributes") or {}
            cls = attrs.get("device_class") or ent.split(".")[0]
            if ent.startswith("person.") or ent in watch:
                self.names[ent], self.kinds[ent] = attrs.get("friendly_name") or ent, cls
                if st not in ("unavailable", "unknown", None):
                    self.states[ent] = st
            elif cls in ("motion", "occupancy") and any(w in ent for w in rooms):
                self.motion.add(ent)
                try:
                    from datetime import datetime
                    if st == "on":
                        self.motion_at[ent] = datetime.fromisoformat(s["last_changed"]).timestamp()
                except (KeyError, ValueError):
                    pass

        with self.lock:      # what closed while the connection was down is no longer open
            for ent in [e for e in self.open if not self.active(e, self.states.get(e, ""))]:
                self.open.pop(ent, None)

    def open_now(self) -> list[str]:
        """The names of the watched things that are open or unlocked right now."""
        return [self.names.get(r["entity"], r["entity"]) for r in self.conf["ha"]["watch"]
                if self.active(r["entity"], self.states.get(r["entity"], ""))]

    def ha_loop(self, stop: threading.Event):
        """Follow Home Assistant live. Reconnects by itself; a failure never reaches the room."""
        from websockets.sync.client import connect
        wait = 2
        while not stop.is_set():
            try:
                self.ha_baseline()
                try:
                    self.learn_history()
                except Exception as exc:
                    log.warning("senses: history not read: %s", str(exc)[:160])
                url = re.sub(r"^http", "ws", self.conf["ha"]["url"]) + "/api/websocket"
                with connect(url, open_timeout=15, max_size=None) as ws:
                    json.loads(ws.recv(timeout=15))                              # auth_required
                    ws.send(json.dumps({"type": "auth", "access_token": self._ha_auth().split(" ", 1)[-1]}))
                    if json.loads(ws.recv(timeout=15)).get("type") != "auth_ok":
                        raise RuntimeError("Home Assistant refused the token")
                    if self.followed():
                        ws.send(json.dumps({"id": 1, "type": "subscribe_trigger",
                                            "trigger": {"platform": "state", "entity_id": self.followed()}}))
                    self.status["ha"], wait = "connected", 2
                    while not stop.is_set():
                        try:
                            m = json.loads(ws.recv(timeout=5))
                        except TimeoutError:
                            continue
                        tr = ((m.get("event") or {}).get("variables") or {}).get("trigger") or {}
                        to = tr.get("to_state") or {}
                        if tr.get("entity_id") and to:
                            self.heard["ha"] = time.time()
                            self.on_state(tr["entity_id"], to.get("state"), to.get("attributes") or {})
            except Exception as exc:
                self.status["ha"] = f"not connected: {str(exc)[:160] or type(exc).__name__}"
                log.warning("senses: Home Assistant: %s", str(exc)[:200])
            if stop.wait(wait):
                return
            wait = min(60, wait * 2)

    def active(self, ent: str, state: str) -> bool:
        return state in ("on", "open", "unlocked", "opening")

    def describe(self, ent: str, active: bool, still: bool = False) -> str:
        name, kind = self.names.get(ent, ent), self.kinds.get(ent, "")
        if kind in OPENS or ent.startswith("cover."):
            verb = ("is still open" if still else "opened") if active else "closed"
        elif ent.startswith("lock."):
            verb = ("is still unlocked" if still else "was unlocked") if active else "was locked"
        elif kind == "moisture":
            return f"Water leak: {name}" + (" (still wet)" if still else "") if active else f"{name}: dry again"
        elif kind in SERIOUS:
            return f"Alarm: {name}" + (" (still on)" if still else "") if active else f"{name}: clear again"
        else:
            verb = ("is still on" if still else "turned on") if active else "turned off"
        return f"{name} {verb}"

    def on_state(self, ent: str, new, attrs: dict | None = None):
        """One change in Home Assistant."""
        if new in ("unavailable", "unknown", None):
            return
        now = time.time()
        with self.lock:
            was, self.states[ent] = self.states.get(ent), new
            if attrs and attrs.get("friendly_name"):
                self.names[ent] = attrs["friendly_name"]
            if ent in self.motion:
                if new == "on":
                    self.motion_at[ent] = now
                self.states.pop(ent, None)
                return
            if was is None or was == new:
                return      # the first sight is the starting point; an attribute changing is no event
        odd = self.unusual(ent, new)      # asked before this one is learned
        self.learn(ent, new)
        note = " (unusual: in the time it has been watched this has never happened at this hour)" if odd else ""
        weigh = odd and self.conf["ha"]["unusual"]
        if ent.startswith("person."):
            name = self.names.get(ent, ent)
            self.tell("ha", (f"{name} came home" if new == "home" else f"{name} left home" if was == "home" else f"{name} is now at {new}") + note,
                      say=weigh, key=ent, judge="" if weigh else None)
            return
        asked = False
        for r in self.conf["reflexes"]:
            if r["enabled"] and r["entity"] == ent and r["state"] == new:
                asked = self.reflex(r) or asked
        row = next((r for r in self.conf["ha"]["watch"] if r["entity"] == ent), None)
        if not row:
            return
        if self.active(ent, new):
            with self.lock:
                self.open[ent] = {"since": now, "next": now + row["remind_min"] * 60 if row["remind_min"] and row["reminders"] else 0,
                                  "left": row["reminders"], "acked": False}
                self.told.pop(ent, None)       # opened again: a new matter
                self.told_at.pop(ent, None)
            # a reflex that asks about it has said it already; something unusual on a quiet row is weighed by the agent
            quiet_odd = weigh and not row["say"]
            self.tell("ha", self.describe(ent, True) + note, say=(row["say"] or quiet_odd) and not asked, key=ent,
                      serious=self.kinds.get(ent) in SERIOUS, judge="" if quiet_odd else None)
        else:
            with self.lock:
                self.open.pop(ent, None)
            self.tell("ha", self.describe(ent, False), key=ent)

    def remind(self, now: float):
        """What was left open is brought up again: as often as its row says, and never once answered."""
        rows = {r["entity"]: r for r in self.conf["ha"]["watch"]}
        for ent, o in list(self.open.items()):
            row = rows.get(ent)
            if not row or o["acked"] or not o["next"] or now < o["next"] or o["left"] <= 0:
                continue
            o["left"] -= 1
            o["next"] = now + row["remind_min"] * 60 if o["left"] > 0 else 0
            said = self.told.get(ent, 0)
            self.tell("ha", self.describe(ent, True, still=True) + (
                f". It was said aloud {said} time(s) and is still not dealt with" if said else ""),
                say=row["say"], key=ent, serious=self.kinds.get(ent) in SERIOUS)

    def phone(self, text: str) -> bool:
        service = self.conf["ha"]["notify"]
        if not (service and self.conf["ha"]["enabled"]):
            return False
        try:
            self.ha(f"/api/services/notify/{service}", {"title": "AgentHub", "message": text})
            return True
        except Exception as exc:
            log.warning("senses: phone notification failed: %s", str(exc)[:200])
            return False

    # ---------------------------------------------------------------- telling the agents

    def tell(self, part: str, text: str, say: bool = False, key: str = "", serious: bool = False, judge: str | None = None):
        """Something was sensed: the part's agents feel it, and with `say` its speaker gets the chance
        to say it aloud. Without `say` it is felt quietly: never spoken of unprompted. judge (a text,
        maybe empty): the speaker weighs it and says it only if it is worth it; the text says when."""
        import emotion_engine
        p = self.conf[part]
        for a in p["agents"]:
            emotion_engine.event(a, "", text, news=say and judge is None)
        self.journal.append((time.time(), text))
        did = "felt" if p["agents"] else "noted"
        if say:
            did = self.say(p["speaker"], text, key, serious, judge)
        self.note(part, text, did)

    def say(self, agent: str, text: str, key: str, serious: bool = False, judge: str | None = None) -> str:
        """Hand the agent something worth saying, or (judge) something to weigh. -> what became of
        it, for the activity list."""
        import initiative
        if not agent:
            return "felt; not said: no agent speaks for this part"
        if self.muted:
            why = "muted"
        elif not self.people_home():
            why = "nobody is home"
        elif self.quiet(agent):
            why = "quiet hours"
        else:
            said = self.told.get(key, 0)
            again = f" You have said this aloud {said} time(s) already and got no answer; let it show." if said else ""
            if judge is not None:
                rule = f" It is when: {judge.rstrip('. ')}." if judge else ""
                why = initiative.consider(agent, f"{text}. Weigh whether this is worth telling {self.called} aloud now.{rule} "
                                                 "People who live here coming and going, and anything routine, are not", key,
                                          urgent=True, choose=True)
                if not why:
                    return f"given to @{agent} to weigh"
            else:
                why = initiative.consider(agent, f"{text}.{again} Worth telling {self.called} aloud now", key, urgent=True)
                if not why:
                    return f"given to @{agent} to say"
        if serious and self.phone(text):
            return f"not said ({why}); sent to the phone"
        return f"felt; not said: {why}"

    def answered(self, by: str):
        """Somebody answered an agent or pressed the knob. What it said a moment ago was heard: no more
        reminders about it until it has closed and opened again."""
        import emotion_engine
        now = time.time()
        with self.lock:
            heard = [k for k, at in self.told_at.items() if now - at < 600]
            for k in heard:
                if k in self.open:
                    self.open[k]["acked"] = True
            had, agent = bool(self.pending or self.ignored or heard), (self.pending or {}).get("agent") or by
            self.pending, self.ignored = None, 0
            self.told_at = {k: at for k, at in self.told_at.items() if k not in heard}
        if had and agent:
            emotion_engine.event(agent, "answered", f"{self.called} answered me after I spoke")

    def unanswered(self, now: float):
        """An agent said something aloud and nobody answered or acknowledged it in three minutes."""
        import emotion_engine
        with self.lock:
            p = self.pending
            if not p or now - p["at"] < 180:
                return
            self.pending, self.ignored = None, self.ignored + 1
            n, times = self.told.get(p["key"], 1), self.ignored
        if not str(p["key"]).startswith(("self:", "build")):
            self.loose = [x for x in self.loose if x["key"] != p["key"]][-2:] + [{"agent": p["agent"], "key": p["key"], "about": p["about"], "at": now}]
        nth = f" (the {n}{'th' if n > 3 else ('', 'st', 'nd', 'rd')[n]} time I said it)" if n > 1 else ""
        emotion_engine.event(p["agent"], "ignored", f'I told {self.called} aloud: "{p["about"][:120]}" and got no answer{nth}', times)

    # ---------------------------------------------------------------- speech

    def room(self) -> dict | None:
        """The room to speak in: the one with the latest motion (within half an hour), else the default."""
        rooms = self.conf["speakers"]["rooms"]
        best, when = next((r for r in rooms if r["name"] == self.conf["speakers"]["default_room"]), rooms[0] if rooms else None), 0.0
        for r in rooms:
            for ent, at in self.motion_at.items():
                if r["motion"] and r["motion"] in ent and at > when and time.time() - at < 1800:
                    best, when = r, at
        return best

    def audio_url(self, agent: str, text: str) -> tuple[str, float]:
        """The text in the agent's voice, held for a few minutes under a one-time address. -> (url, seconds)."""
        import voice
        s = self.spec(agent)
        audio, media = voice.speak(text, s.get("voice_provider"), s.get("voice"),
                                   __import__("emotion_engine").voice_speed(agent, s.get("voice_speed")))
        name = secrets.token_urlsafe(18) + (".wav" if "wav" in media else ".mp3")
        self.audio[name] = (audio, media, time.time() + 300)
        secs = 0.0
        if "wav" in media:
            try:   # from the bytes: a streamed wav's header does not know its own length
                import io
                import wave
                with wave.open(io.BytesIO(audio)) as w:
                    secs = (len(audio) - 44) / (w.getframerate() * w.getsampwidth() * w.getnchannels())
            except Exception:
                secs = 0.0
        if not 0.3 <= secs <= 120:
            secs = max(1.5, len(text) * 0.065)
        return f"{self.conf['speakers']['public_url']}/senses/a/{name}", secs

    def announce(self, agent: str, text: str, key: str, about: str):
        """Say an agent's unprompted words on the speaker of the room where someone is."""
        try:
            sp = self.conf["speakers"]
            room = self.room()
            why = ("the speakers are off" if not (sp["enabled"] and sp["public_url"] and room) else "muted" if self.muted
                   else "quiet hours" if self.quiet(agent) else "nobody is home" if not self.people_home() else "")
            if why:
                self.note("speakers", text, f"not spoken: {why}")
                return
            url, secs = self.audio_url(agent, text)
            self.ha("/api/services/assist_satellite/announce",
                    {"entity_id": room["speaker"], "media_id": url, "preannounce": False}, timeout=60)
            with self.lock:
                self.speak_until = time.time() + min(60.0, secs)
                self.told[key] = self.told.get(key, 0) + 1
                self.told_at[key] = time.time() + secs
                self.pending = {"agent": agent, "key": key, "about": about, "at": time.time() + secs}
            self.note("speakers", text, f"spoken in {room['name']} ({secs:.0f} s)")
        except Exception as exc:
            self.note("speakers", text, f"not spoken: {str(exc)[:160]}")

    def test_room(self, name: str, play: bool = True) -> dict:
        """A test line in the speaking agent's voice, on one room's speaker (the settings page's Test)."""
        room = next((r for r in self.conf["speakers"]["rooms"] if r["name"] == name), None)
        agent = next(iter(sorted(self.speakers_of())), "") or self.conf["display"]["agent"]
        if not room or not self.conf["speakers"]["public_url"]:
            raise RuntimeError("save the room and AgentHub's address first")
        if not agent:
            raise RuntimeError("no agent speaks yet: pick one on the Home Assistant or Cameras page")
        url, secs = self.audio_url(agent, f"This is a test of the {room['name']} speaker.")
        if play:
            self.ha("/api/services/assist_satellite/announce",
                    {"entity_id": room["speaker"], "media_id": url, "preannounce": False}, timeout=60)
            self.note("speakers", f"Test line in {room['name']}", f"spoken as @{agent}")
        return {"url": url, "seconds": round(secs, 1), "agent": agent}

    def reflex(self, r: dict) -> bool:
        """A reflex's trigger happened. -> whether its question is being asked (in its own thread)."""
        agent, sp, now = self.conf["ha"]["speaker"], self.conf["speakers"], time.time()
        room = self.room()
        why = ("the speakers are off" if not (sp["enabled"] and sp["public_url"] and room) else "no agent speaks for Home Assistant" if not agent
               else "muted" if self.muted else "quiet hours" if self.quiet(agent) else "nobody is home" if not self.people_home()
               else "asked not long ago" if now - self.asked.get(r["name"], 0) < r["again_min"] * 60 else "")
        if why:
            self.note("reflex", r["question"], f"not asked: {why}")
            return False
        self.asked[r["name"]] = now
        threading.Thread(target=self.ask, args=(r, agent, room), daemon=True, name="senses-reflex").start()
        return True

    def ask(self, r: dict, agent: str, room: dict):
        """Ask the question aloud, hear yes or no, do the one thing written for yes, and tell the agent."""
        import emotion_engine
        try:
            body = {"entity_id": room["speaker"], "preannounce": False,
                    "answers": [{"id": "yes", "sentences": YES}, {"id": "no", "sentences": NO}]}
            try:
                body["question_media_id"], secs = self.audio_url(agent, r["question"])
            except Exception as exc:      # its own voice failed: Home Assistant's voice asks
                log.warning("senses: reflex voice: %s", str(exc)[:160])
                body["question"], secs = r["question"], 3.0
            with self.lock:
                self.speak_until = time.time() + secs
            out = self.ha("/api/services/assist_satellite/ask_question?return_response", body, timeout=90)
            got = ((out or {}).get("service_response") or {}).get("id")
            if got == "yes":
                self.ha(f"/api/services/{r['service'].replace('.', '/')}", {"entity_id": r["target"]})
                did = f"yes: {r['service']} on {r['target']}"
                told = f'I asked "{r["question"]}", {self.called} said yes, and it is done'
            elif got == "no":
                did, told = "no", f'I asked "{r["question"]}" and {self.called} said no'
            else:
                did, told = "no answer", f'I asked aloud "{r["question"]}" and got no answer'
            self.journal.append((time.time(), told))
            for a in self.conf["ha"]["agents"]:
                emotion_engine.event(a, "", told, news=False)
            if not got:
                emotion_engine.event(agent, "ignored", told)
            self.note("reflex", r["question"], did)
        except Exception as exc:
            self.note("reflex", r["question"], f"failed: {str(exc)[:160]}")

    # ---------------------------------------------------------------- cameras (UniFi Protect)

    def protect(self, path: str, raw: bool = False, timeout: float = 30):
        c = self.conf["cameras"]
        return _http(c["url"] + "/proxy/protect/integration/v1" + path, None, {"X-API-KEY": c["api_key"]},
                     timeout, raw, _lan_tls())

    def protect_loop(self, stop: threading.Event):
        from websockets.sync.client import connect
        wait = 2
        while not stop.is_set():
            try:
                self.cams = {c["id"]: c["name"] for c in self.protect("/cameras") if c.get("state") == "CONNECTED"}
                url = re.sub(r"^http", "ws", self.conf["cameras"]["url"]) + "/proxy/protect/integration/v1/subscribe/events"
                with connect(url, ssl=_lan_tls() if url.startswith("wss") else None, open_timeout=15, max_size=None,
                             additional_headers={"X-API-KEY": self.conf["cameras"]["api_key"]}) as ws:
                    self.status["cameras"], wait = "connected", 2
                    while not stop.is_set():
                        try:
                            m = json.loads(ws.recv(timeout=5))
                        except TimeoutError:
                            continue
                        self.heard["cameras"] = time.time()
                        self.on_protect(m)
            except Exception as exc:
                self.status["cameras"] = f"not connected: {str(exc)[:160] or type(exc).__name__}"
                log.warning("senses: cameras: %s", str(exc)[:200])
            if stop.wait(wait):
                return
            wait = min(60, wait * 2)

    def on_protect(self, m: dict):
        """One message of the camera event stream. A trigger row fires at once, or when the event has
        lasted as long as the row says; an event that ends sooner never fires."""
        item = m.get("item") or {}
        if item.get("modelKey") != "event" or not item.get("id"):
            return
        cam, typ, kinds = self.cams.get(item.get("device"), ""), item.get("type"), item.get("smartDetectTypes") or []
        if not cam:
            return
        if typ == "ring":
            whats = ["ring"]
        elif typ == "smartAudioDetect":
            whats = [w for w, (k, t, _, _) in WHATS.items() if k == "audio" and t in kinds]
        elif str(typ).startswith("smartDetect"):
            whats = [w for w, (k, t, _, _) in WHATS.items() if k == "smart" and t in kinds]
        else:
            return
        now, ended = time.time(), item.get("end") is not None
        for i, row in enumerate(self.conf["cameras"]["triggers"]):
            if row["what"] not in whats or (row["camera"] and row["camera"].lower() != cam.lower()):
                continue
            k = (item["id"], i)
            with self.lock:
                known = k in self.live
                if ended and (known or row["stay_s"]):
                    self.live.pop(k, None)          # it left: a pass-by never fires a row that asks it to stay
                    continue
                if known:
                    continue
                started = item.get("start") / 1000 if isinstance(item.get("start"), (int, float)) else now
                self.live[k] = {"fire_at": max(now, started + row["stay_s"]) if row["stay_s"] else now,
                                "cam": cam, "row": i, "fired": False}
            if not row["stay_s"]:
                self.fire(k)
                if ended:                           # a ring is over the moment it is told
                    with self.lock:
                        self.live.pop(k, None)

    def fire(self, k: tuple):
        with self.lock:
            ev = self.live.get(k)
            rows = self.conf["cameras"]["triggers"]
            if not ev or ev["fired"] or ev["row"] >= len(rows):
                return
            ev["fired"] = True
            row, cam, now = rows[ev["row"]], ev["cam"], time.time()
            if now - self.fired.get((row["what"], cam), 0) < row["again_min"] * 60:
                return                               # the same visit: once is enough
            self.fired[(row["what"], cam)] = now
        threading.Thread(target=self.camera_event, args=(row, cam), daemon=True, name="senses-camera").start()

    def camera_event(self, row: dict, cam: str):
        what = row["what"]
        text = (STAYS[what].format(cam=cam, n=row["stay_s"]) if row["stay_s"] and what in STAYS else WHATS[what][2].format(cam=cam))
        gap = self.conf["cameras"]["gap_min"] * 60
        with self.lock:
            say = bool(row["say"]) and time.time() - self.cam_said >= gap
            if say:
                self.cam_said = time.time()
        if say and WHATS[what][0] != "audio":      # a look at what is there, so the agent can say who or what
            try:
                text += ". The camera shows: " + self.look(cam, closely=True)
            except Exception as exc:
                log.debug("senses: no look at %s: %s", cam, exc)
        if row["say"] and not say:
            import emotion_engine
            for a in self.conf["cameras"]["agents"]:
                emotion_engine.event(a, "", text, news=False)
            self.journal.append((time.time(), text))
            self.cam_waiting = {"row": row, "cam": cam, "at": time.time()}
            self.note("cameras", text, f"felt; not said: another camera event was said or is being weighed (one per {self.conf['cameras']['gap_min']} minutes)")
            return
        self.tell("cameras", text, say=say, key=f"camera:{cam}:{what}", serious=WHATS[what][3],
                  judge=row["when"] if row["say"] == "judge" else None)

    def vision(self) -> dict | None:
        import llm_servers
        v = self.conf["cameras"]["vision"]
        try:
            s = llm_servers.get(v["server"]) if v["server"] else None
        except llm_servers.Unreadable:
            s = None
        model = v["model"] or (s or {}).get("default_model")
        if not s or not model:
            return None
        return {"name": "senses (cameras)", "kind": "api", "base_url": s["base_url"], "api_format": s.get("api_format", "openai"),
                "api_key": s.get("api_key", ""), "model": model, "temperature": 0.2, "timeout": 60}

    def frame(self, cam: str) -> tuple[str, bytes]:
        """One fresh frame from a camera. -> (the camera's name, the JPEG), held in memory."""
        cid = next((i for i, n in self.cams.items() if n.lower() == cam.strip().lower()), None)
        if not cid:
            raise RuntimeError("no connected camera by that name. Cameras: " + (", ".join(self.cams.values()) or "none"))
        return self.cams[cid], self.protect(f"/cameras/{cid}/snapshot", raw=True)

    def look(self, cam: str, closely: bool = False, shot: bytes | None = None) -> str:
        """What a camera shows right now, in a sentence. closely: who and what kind, for something that
        set a trigger off. The frame is held in memory for the look."""
        import agent_runner
        spec = self.vision()
        if not spec:
            raise RuntimeError("no model is picked to describe what the cameras see (Settings > Senses > Cameras)")
        name, shot = self.frame(cam) if shot is None else (cam, shot)
        ask = ("Describe the people and vehicles in view in one or two plain sentences: what kind of person (an adult, a "
               "child, someone in a uniform or work clothes, someone carrying a parcel), what each is doing and where "
               "(at the door, walking up, leaving, passing on the street), and each vehicle's type and colour and whether "
               "it is parked, stopped or moving. When a uniform or a vehicle clearly belongs to a delivery or postal "
               "service (UPS, FedEx, USPS mail, Amazon, DHL), name the service. Say so if nobody is there. " if closely else
               "Describe what is in view in one plain sentence: people, vehicles, animals, parcels, and anything out of place. ")
        msgs = [{"role": "user", "content": [
            {"type": "text", "text": ask + "Do not guess who a person is, and do not read out any other writing or signs."},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(shot).decode()}}]}]
        try:
            out = agent_runner.call_api_message(spec, "You watch a home security camera.", msgs, effort="none")
        except urllib.error.HTTPError as exc:
            if exc.code not in (400, 422):
                raise
            out = agent_runner.call_api_message(spec, "You watch a home security camera.", msgs)
        text = re.sub(r"<think>.*?</think>", "", (out or {}).get("content") or "", flags=re.S)
        text = re.sub(r"\s+", " ", text).strip()[:300].rstrip(". ")
        if not text:
            raise RuntimeError("the model gave no description")
        if not closely:
            self.scenes[name] = (time.time(), text)
        return text

    def describe_loop(self, stop: threading.Event):
        """One sentence per connected camera every few minutes, for the agents' prompt."""
        while not stop.wait(20):
            every = self.conf["cameras"]["describe_min"] * 60
            if not every or not self.vision():
                continue
            for cam in list(self.cams.values()):
                if stop.is_set():
                    return
                if time.time() - self.scenes.get(cam, (0, ""))[0] >= every:
                    try:
                        self.look(cam)
                    except Exception as exc:
                        log.debug("senses: %s not described: %s", cam, str(exc)[:160])
                        self.scenes[cam] = (time.time(), self.scenes.get(cam, (0, ""))[1])   # try again next round

    # ---------------------------------------------------------------- MQTT: events in, and the desk display

    def mqtt_loop(self, stop: threading.Event):
        import mqtt_out
        import paho.mqtt.client as mqtt
        broker = mqtt_out.load()
        if not broker["host"]:
            self.status["mqtt"] = "no broker: enter one in Settings > Providers > MQTT"
            return
        dis, ev = self.conf["display"], self.conf["events"]
        topics = (ev["topics"] if ev["enabled"] else []) + ([dis["prefix"] + "/knob"] if dis["enabled"] and dis["prefix"] else [])
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="agenthub-senses")
        if broker["username"]:
            client.username_pw_set(broker["username"], broker["password"])

        def on_connect(c, u, flags, rc, props):
            self.status["mqtt"] = "connected" if not getattr(rc, "is_failure", False) else f"not connected: {rc}"
            for t in topics:
                c.subscribe(t)
            self.shown = None     # the display hears the state again
            if dis["enabled"] and dis["prefix"]:
                c.publish(dis["prefix"] + "/mute", "ON" if self.muted else "OFF", retain=True)

        def on_message(c, u, msg):
            try:
                self.on_mqtt(msg.topic, msg.payload.decode("utf-8", "replace"))
            except Exception:
                log.exception("senses: MQTT message")
        client.on_connect, client.on_message = on_connect, on_message
        client.on_disconnect = lambda *a: self.status.__setitem__("mqtt", "not connected: the broker dropped the connection")
        client.reconnect_delay_set(2, 60)
        self.status["mqtt"] = "connecting"
        client.connect_async(broker["host"], int(broker["port"]), 30)
        client.loop_start()
        self.mqtt = client
        stop.wait()
        self.mqtt = None
        client.loop_stop()
        try:
            client.disconnect()
        except Exception:
            pass

    def on_mqtt(self, topic: str, payload: str):
        dis, ev = self.conf["display"], self.conf["events"]
        if dis["enabled"] and topic == dis["prefix"] + "/knob":
            try:
                kind = json.loads(payload).get("type")
            except (ValueError, AttributeError):
                return
            if kind == "press":
                with self.lock:
                    for o in self.open.values():
                        o["acked"] = True
                self.answered(dis["agent"])
                self.gesture("happy", 1200)
                self.note("display", "The knob was pressed", "acknowledged: no more reminders about what is open")
            elif kind == "long_press":
                self.set_muted(not self.muted)
            return
        if not (ev["enabled"] and topic in ev["topics"]):
            return
        try:
            d = json.loads(payload)
            desc = _s(d["description"], 400)
        except (ValueError, KeyError, TypeError):
            return
        if desc:
            self.tell("events", desc, say=d.get("action") == "speak", key="event:" + _s(d.get("key") or desc, 80).lower(),
                      serious=d.get("serious") is True)

    def set_muted(self, on: bool):
        self.muted = bool(on)
        try:
            (data_dir / STATE).write_text(json.dumps({"muted": self.muted}), "utf-8")
        except OSError:
            log.warning("senses: the mute could not be saved")
        dis = self.conf["display"]
        if self.mqtt and dis["enabled"] and dis["prefix"]:
            self.mqtt.publish(dis["prefix"] + "/mute", "ON" if self.muted else "OFF", retain=True)
        self.note("senses", "Unprompted speech", "muted" if self.muted else "no longer muted")

    def gesture(self, face: str, hold_ms: int = 1500):
        dis = self.conf["display"]
        if self.mqtt and dis["enabled"] and dis["prefix"] and face:
            self.mqtt.publish(dis["prefix"] + "/gesture", json.dumps({"face": face, "hold_ms": hold_ms}))

    def pick_face(self, agent: str, text: str) -> str:
        """A reply without a tag: the emotion model picks one face for it. "" when it cannot."""
        try:
            import agent_runner
            import emotion_engine
            model = emotion_engine.ENGINE._model(self.spec(agent)) if emotion_engine.ENGINE else None
            if not model:
                return ""
            out = agent_runner.call_api_message(
                {**model, "name": f"{agent} (face)", "timeout": 8},
                "Pick the one facial expression that fits how this line is said. Answer with one word from: " + " ".join(FACES) + ".",
                [{"role": "user", "content": text[:600]}], effort="none")
            word = ((out or {}).get("content") or "").lower()
            face = next((f for f in FACES if f in word), "")
            return "" if face == "neutral" else face
        except Exception as exc:
            log.debug("senses: no face picked: %s", exc)
            return ""

    def show(self, now: float):
        """The display's resting face and mouth: on a change, and every 20 s (it forgets after 60)."""
        dis = self.conf["display"]
        if not (self.mqtt and dis["enabled"] and dis["prefix"] and dis["agent"]):
            return
        import emotion_engine
        import mqtt_out
        mood = emotion_engine.status(dis["agent"]) or {}
        seen = (mood.get("face") or "neutral", bool(dis["agent"] in mqtt_out._speaking or now < self.speak_until))
        if seen != self.shown or now - self.shown_at >= 20:
            self.shown, self.shown_at = seen, now
            self.mqtt.publish(dis["prefix"] + "/state", json.dumps({"face": seen[0], "speaking": seen[1]}), retain=True)

    # ---------------------------------------------------------------- the room

    def on_room(self, m: dict):
        """A new message in the room: an agent's unprompted words are spoken, its replies move the
        display, and the owner's words to it are an answer."""
        if m.get("type", "chat") != "chat" or not m.get("text"):
            return
        sender, text, meta = str(m.get("sender", "")), m["text"], m.get("metadata") or {}
        dis = self.conf["display"]
        if sender in self.speakers_of() | {dis["agent"] if dis["enabled"] else ""} - {""}:
            import voice
            said = voice.speakable(text)
            self.last_reply[sender] = (time.time(), words(said))
            ini = meta.get("initiative")
            if isinstance(ini, dict) and sender in self.speakers_of():
                threading.Thread(target=self.announce, daemon=True, name="senses-say", args=(
                    sender, text, str(ini.get("key") or f"self:{m.get('id')}"), str(ini.get("why") or said)[:200])).start()
            if dis["enabled"] and sender == dis["agent"]:
                hold = max(1500, min(4000, len(said) * 30))
                face = reply_face(text)
                if face:
                    self.gesture("" if face == "neutral" else face, hold)
                elif said:      # no tag: the emotion model picks the face, off this thread
                    threading.Thread(target=lambda: self.gesture(self.pick_face(sender, said), hold), daemon=True,
                                     name="senses-face").start()
            return
        owner = str((getattr(self.hub, "room_settings", None) or {}).get("username") or "")
        if not owner or sender.lower() != owner.lower():
            return
        for agent in self.speakers_of():
            home = (self.spec(agent).get("initiative") or {}).get("channel") or "general"
            if not (f"@{agent}" in text.lower() or str(meta.get("to", "")).lower() == agent or m.get("channel") == home):
                continue
            at, said = self.last_reply.get(agent, (0.0, set()))
            w = words(re.sub(r"@[\w-]+", "", text))
            if meta.get("voice") and len(w) >= 4 and time.time() - at < 90 and len(w & said) / len(w) >= 0.6:
                continue        # the agent's own speech heard back by a microphone is no answer
            self.answered(agent)

    # ---------------------------------------------------------------- what the agents are given

    def now_lines(self, agent: str) -> str:
        """Lines for the agent's prompt this turn: the house as it is, and how to use the display."""
        c, lines = self.conf, []
        if not c["enabled"]:
            return ""
        if c["display"]["enabled"] and agent == c["display"]["agent"]:
            lines.append("A face for the desk display without a sound: put [face:<id>] in a reply (never shown or spoken), "
                         "id one of " + " ".join(FACES) + ".")
        mine = [x for x in self.loose if x["agent"] == agent and time.time() - x["at"] < 12 * 3600 and x["key"] not in self.told_at]
        if mine:      # once: after this turn the matter is dropped, answered or not
            self.loose = [x for x in self.loose if x not in mine]
            still = [x for x in mine if x["key"] not in self.open or not self.open[x["key"]]["acked"]]
            if still:
                lines.append("Earlier you said this aloud and got no answer: " + "; ".join(x["about"][:120] for x in still)
                             + ". If it still matters you may come back to it once, briefly, when it fits. Then let it go.")
        if agent in self.targets():
            from datetime import datetime
            import emotion_engine
            tz = emotion_engine.now_local().tzinfo
            clock = lambda ts: datetime.fromtimestamp(ts, tz).strftime("%-I:%M %p")
            if c["ha"]["enabled"] and agent in c["ha"]["agents"] and self.states:
                home = [self.names.get(e, e) for e, v in self.states.items() if e.startswith("person.") and v == "home"]
                lines.append(f"[house] As of {clock(time.time())}. Home: {', '.join(home) or 'nobody'}. "
                             f"Open or unlocked: {', '.join(self.open_now()) or 'nothing'}.")
            if c["cameras"]["enabled"] and agent in c["cameras"]["agents"] and self.cams:
                lines.append("When someone asks what a camera shows, or asks to see it, call the camera_look tool first and answer "
                             "from its result; with picture true it gives you the [attach: ...] line that shows the picture. Never "
                             "describe a camera's view without that call, and never write an attach path yourself. "
                             "Cameras: " + ", ".join(self.cams.values()) + ".")
            recent = [f"{clock(ts)} {text[:90]}" for ts, text in self.journal if time.time() - ts < 6 * 3600][-4:]
            if recent:
                lines.append("Latest: " + "; ".join(recent) + ".")
        return "\n".join(lines)

    def tool_specs(self, agent: str) -> list[dict]:
        c = self.conf
        if not (c["enabled"] and c["cameras"]["enabled"] and agent in c["cameras"]["agents"] and self.cams):
            return []
        return [{"type": "function", "function": {
            "name": "camera_look", "description": "A fresh look through one of the house cameras: one sentence on what is in "
            "view right now. picture true: the frame is also saved, to attach to your reply when someone asks to see "
            "it. Cameras: " + ", ".join(self.cams.values()) + ".",
            "parameters": {"type": "object", "properties": {"camera": {"type": "string"}, "picture": {"type": "boolean"}},
                           "required": ["camera"]}}}]

    def camera_look(self, agent: str, camera: str, picture: bool = False) -> str:
        if not self.tool_specs(agent):
            return "error: the cameras are not available to you"
        try:
            name, shot = self.frame(camera)
            try:
                seen = self.look(name, shot=shot)
            except Exception as exc:
                if not picture:
                    raise
                seen = f"(no description: {str(exc)[:120]})"
            if not picture:
                return f"{name}, now: {seen}."
            folder = data_dir / "senses_frames"         # kept a few minutes: the reply copies it into the chat
            folder.mkdir(mode=0o700, exist_ok=True)
            path = folder / f"{re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')}-{secrets.token_hex(4)}.jpg"
            path.write_bytes(shot)
            path.chmod(0o600)
            self.note("cameras", f"A picture from the {name} camera", f"saved for @{agent} to attach")
            return f"{name}, now: {seen}. To show the picture, put [attach: {path}] in your reply."
        except Exception as exc:
            return f"error: {str(exc)[:240]}"

    def picture_for(self, agent: str, path: str) -> str | None:
        """An agent attached a camera picture by a path it made up instead of asking for one: when the
        file's name carries a camera's name, a fresh frame is saved and its real path returned."""
        if not self.tool_specs(agent):
            return None
        name = re.sub(r"[^a-z0-9]+", "", Path(path).name.lower())
        cam = next((c for c in sorted(self.cams.values(), key=len, reverse=True) if re.sub(r"[^a-z0-9]+", "", c.lower()) in name), None)
        if not cam:
            return None
        out = self.camera_look(agent, cam, picture=True)
        m = re.search(r"\[attach: ([^\]]+)\]", out)
        return m.group(1) if m else None

    def silent(self, agent: str, nudge: dict):
        """The agent was handed something and chose not to say it. A camera event it let pass does
        not hold back the next one: only what was said counts as the visit's announcement."""
        if agent in self.speakers_of() and nudge.get("why"):
            self.note("speakers", str(nudge["why"])[:200], f"@{agent} chose to stay silent")
            if str(nudge.get("key") or "").startswith("camera:"):
                held, self.cam_waiting, self.cam_said = self.cam_waiting, None, 0.0
                if held and time.time() - held["at"] < 180:     # e.g. the bell that rang while a passer-by was weighed
                    threading.Thread(target=self.camera_event, args=(held["row"], held["cam"]), daemon=True,
                                     name="senses-camera").start()

    # ---------------------------------------------------------------- running

    def tick(self):
        now = time.time()
        with self.lock:
            due = [k for k, ev in self.live.items() if not ev["fired"] and now >= ev["fire_at"]]
            for k in [k for k, ev in self.live.items() if now - ev["fire_at"] > 900]:
                self.live.pop(k, None)             # an event whose end was never told
            for name in [n for n, (_, _, until) in self.audio.items() if until < now]:
                self.audio.pop(name, None)
        if int(now) % 60 == 0 and self.conf["ha"]["enabled"] and not self.people_home():
            import emotion_engine                  # an empty house is not neglect: nobody is there to answer
            for a in self.conf["ha"]["agents"]:
                emotion_engine.rest(a)
        if int(now) % 60 == 0:                     # pictures saved for a reply are not kept
            for f in (data_dir / "senses_frames").glob("*.jpg"):
                try:
                    if now - f.stat().st_mtime > 600:
                        f.unlink()
                except OSError:
                    pass
        for k in due:
            self.fire(k)
        self.remind(now)
        self.unanswered(now)
        if self.learned_at and now - self.learned_at > 60:
            self.save_patterns()
        self.show(now)

    def run(self, stop: threading.Event):
        while not stop.wait(1.0):
            try:
                self.tick()
            except Exception:
                log.exception("senses")

    def apply(self, conf: dict | None = None):
        """Start, or start again after the settings changed: one thread per part that is on."""
        self.stop.set()
        self.stop = stop = threading.Event()
        self.conf = conf or load()
        self.status = {}
        c = self.conf
        if not c["enabled"]:
            return
        jobs = [("run", self.run)]
        if c["ha"]["enabled"] and c["ha"]["url"]:
            jobs.append(("ha", self.ha_loop))
        if c["cameras"]["enabled"] and c["cameras"]["url"] and c["cameras"]["api_key"]:
            jobs += [("cameras", self.protect_loop), ("describe", self.describe_loop)]
        if (c["display"]["enabled"] and c["display"]["prefix"]) or (c["events"]["enabled"] and c["events"]["topics"]):
            jobs.append(("mqtt", self.mqtt_loop))
        for name, fn in jobs:
            threading.Thread(target=fn, args=(stop,), daemon=True, name=f"senses-{name}").start()

    def state(self) -> dict:
        """For the settings page."""
        first = min((p["first"] for p in self.patterns.values()), default=None)
        return {"status": dict(self.status), "heard": {k: int(time.time() - v) for k, v in self.heard.items()},
                "learned": {"days": round((time.time() - first) / 86400, 1) if first else 0, "things": len({k.split("=")[0] for k in self.patterns}),
                            "ready": bool(first and time.time() - first >= LEARN_DAYS * 86400)},
                "muted": self.muted, "people_home": self.people_home(),
                "open": self.open_now(), "cameras": list(self.cams.values()),
                "activity": list(self.activity)[:60]}

    def options(self) -> dict:
        """What the settings page offers to pick from, read live. Each list is empty when its service
        cannot be reached; "errors" says why."""
        out = {"entities": [], "speakers": [], "notify": [], "actions": [], "cameras": [], "errors": {}}
        c = self.conf
        if c["ha"]["url"]:
            try:
                for s in self.ha("/api/states", timeout=60):
                    ent, attrs = s.get("entity_id", ""), s.get("attributes") or {}
                    dom, cls, name = ent.split(".")[0], attrs.get("device_class") or "", attrs.get("friendly_name") or ent
                    if dom == "assist_satellite":
                        out["speakers"].append({"id": ent, "name": name, "ok": s.get("state") != "unavailable"})
                    elif dom in ("binary_sensor", "lock", "cover") and (dom != "binary_sensor" or cls in OPENS | SERIOUS | {"lock", "motion", "occupancy"}):
                        out["entities"].append({"id": ent, "name": name, "kind": cls or dom})
                    elif dom in ("light", "switch", "fan", "scene", "script", "input_boolean", "media_player"):
                        out["actions"].append({"id": ent, "name": name})
                out["notify"] = sorted(x for d in self.ha("/api/services", timeout=30) if d.get("domain") == "notify"
                                       for x in d.get("services", {}))
            except Exception as exc:
                out["errors"]["ha"] = str(exc)[:200] or type(exc).__name__
        if c["cameras"]["url"] and c["cameras"]["api_key"]:
            try:
                for cam in self.protect("/cameras"):
                    ff = cam.get("featureFlags") or {}
                    out["cameras"].append({"name": cam.get("name"), "ok": cam.get("state") == "CONNECTED",
                                           "smart": ff.get("smartDetectTypes") or [], "audio": ff.get("smartDetectAudioTypes") or [],
                                           "doorbell": "doorbell" in str(cam.get("type") or "").lower()})
            except Exception as exc:
                out["errors"]["cameras"] = str(exc)[:200] or type(exc).__name__
        return out


FACES = ("neutral", "happy", "sad", "angry", "surprised", "fearful", "disgusted", "flirty", "shifty", "mischievous", "slayed")
# an agent's reply tags (the speech engine's own) -> the face shown for a moment
TAG_FACES = {"happy": "happy", "chuckle": "happy", "laugh": "slayed", "sarcastic": "mischievous", "whisper": "shifty",
             "whispering": "shifty", "shush": "shifty", "gasp": "surprised", "groan": "disgusted", "crying": "sad",
             "sigh": "sad", "sniff": "sad", "angry": "angry", "fear": "fearful"}
_TAG = re.compile(r"\[(face:)?([A-Za-z_ -]{2,24})\]")


def reply_face(text: str) -> str:
    """The face a reply asks for: [face:<id>] first, else its first emotion tag. "" when it has neither."""
    text = re.sub(r"```.*?```|`[^`]*`", " ", text or "", flags=re.S)
    found = [(bool(f), t.strip().lower()) for f, t in _TAG.findall(text)]
    return next((t for is_face, t in found if is_face and t in FACES), "") or \
        next((TAG_FACES[t] for is_face, t in found if not is_face and t in TAG_FACES), "")


SENSES: Senses | None = None


def start(hub) -> Senses:
    """Called once when the room starts (run.py), after the emotion engine and initiative."""
    global SENSES, data_dir
    data_dir = Path(hub.agent_manager.data_dir)
    SENSES = Senses(hub)
    hub.store.on_message(lambda m: SENSES.on_room(m) if SENSES.conf["enabled"] else None)
    SENSES.apply()
    return SENSES


def now_lines(agent: str) -> str:
    return SENSES.now_lines(agent) if SENSES else ""


def tool_specs(agent: str) -> list[dict]:
    return SENSES.tool_specs(agent) if SENSES else []


def camera_look(agent: str, camera: str, picture: bool = False) -> str:
    return SENSES.camera_look(agent, camera, picture) if SENSES else "error: the cameras are not available"


def picture_for(agent: str, path: str) -> str | None:
    return SENSES.picture_for(agent, path) if SENSES and SENSES.conf["enabled"] else None


def silent(agent: str, nudge: dict):
    if SENSES and SENSES.conf["enabled"]:
        SENSES.silent(agent, nudge)


def audio(name: str) -> tuple[bytes, str] | None:
    item = SENSES.audio.get(name) if SENSES else None
    return (item[0], item[1]) if item and item[2] > time.time() else None
