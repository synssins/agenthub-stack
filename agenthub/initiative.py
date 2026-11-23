"""Initiative: an agent with it switched on may start talking by itself.

Per agent (Settings > Agents > (agent) > Initiative), kept in its settings as "initiative":
  enabled                 off for every agent until switched on (API agents only)
  channel                 where it speaks: a channel it is in ("" = general)
  gap_min                 the least time between two remarks nobody asked for
  quiet_from, quiet_to    local hours with no unprompted remarks at all (the emotion engine's clock)

What can start one:
  - silence: loneliness or boredom setting in (emotion_engine.py),
  - a quiet moment: once per gap, when something has moved the agent's feelings since the last time,
  - a service outside the room that reports an event as worth saying now
    (POST /api/emotion/event with "speak": true): this one does not wait for the gap,
  - Senses, with something it sensed: to be said, or for the agent to weigh and say only if it is
    worth it (a camera trigger set to "let the agent look and decide").
The agent gets the reason with its turn and chooses: say something, or stay silent. Silence is
never posted. A remark carries metadata.initiative {"why", "key"}, so a service that owns the
speakers of a house can say it aloud.
"""

from __future__ import annotations

import logging
import time

log = logging.getLogger(__name__)
DEFAULTS = {"enabled": False, "channel": "", "gap_min": 90, "quiet_from": 22, "quiet_to": 7}


def _int(v, lo: int, hi: int, default: int) -> int:
    try:
        return max(lo, min(hi, int(float(v))))
    except (TypeError, ValueError):
        return default


def clean(raw) -> dict:
    """An agent's initiative settings as they are stored."""
    raw = raw if isinstance(raw, dict) else {}
    d = DEFAULTS
    return {"enabled": raw.get("enabled") is True,
            "channel": str(raw.get("channel") or "")[:64],
            "gap_min": _int(raw.get("gap_min"), 5, 1440, d["gap_min"]),
            "quiet_from": _int(raw.get("quiet_from"), 0, 23, d["quiet_from"]),
            "quiet_to": _int(raw.get("quiet_to"), 0, 23, d["quiet_to"])}


def quiet(cfg: dict, hour: int | None = None) -> bool:
    """Is it inside the agent's quiet hours? The same hour twice means no quiet hours."""
    if hour is None:
        import emotion_engine
        hour = emotion_engine.now_local().hour
    a, b = cfg["quiet_from"], cfg["quiet_to"]
    return (a <= hour < b) if a <= b else (hour >= a or hour < b)


class Initiative:
    def __init__(self, hub):
        self.hub = hub                             # .agent_manager (specs, runners, in_channel), .agents (trigger_sync)
        self.started = time.time()
        self.spoke_at: dict[str, float] = {}       # agent -> its last unprompted remark
        self.asked_at: dict[str, float] = {}       # agent -> when it was last given the chance
        self.news: dict[str, list[str]] = {}       # agent -> what has happened to it since then

    def cfg(self, name: str) -> dict | None:
        spec = self.hub.agent_manager.specs.get(name)
        if not spec or spec.get("enabled", True) is False or spec.get("kind") != "api":
            return None
        c = clean(spec.get("initiative"))
        return c if c["enabled"] else None

    def on(self, name: str) -> bool:
        return self.cfg(name) is not None

    def note(self, name: str, what: str):
        """Something moved the agent's feelings: it may want to speak of it in a quiet moment."""
        if self.cfg(name) and what:
            q = self.news.setdefault(name, [])
            q.append(what[:160])
            del q[:-5]

    def consider(self, name: str, why: str, key: str = "", urgent: bool = False, choose: bool = False) -> str:
        """Give the agent the chance to say something. urgent: it does not wait for the gap. choose:
        the reason asks the agent to weigh something, and silence is as good an answer as speech.
        -> "" when its turn was started, else why not."""
        c = self.cfg(name)
        if not c:
            return "initiative is off for this agent"
        if quiet(c):
            return "quiet hours"
        mgr = self.hub.agent_manager
        runner = mgr.runners.get(name)
        if not runner or runner.nudges or (runner.working and not urgent):
            return "the agent is busy"     # an event worth saying waits for the turn in progress; small talk does not
        now = time.time()
        if not urgent and now - self.spoke_at.get(name, 0) < c["gap_min"] * 60:
            return "it said something not long ago"
        channel = c["channel"] if c["channel"] and mgr.in_channel(name, c["channel"]) else "general"
        why = " ".join(str(why).split())[:400].rstrip(". ")
        text = ("[initiative] Nobody asked you anything just now. " + why + ". "
                + ("If it is, say it in one or two short sentences, in your own voice and mood. If it is not, reply "
                   "exactly (no reply): nothing is said then. " if choose else
                   "This is worth saying to the people around you now: say it in one or two short sentences, in "
                   "your own voice and mood. " if urgent else
                   "If you want to say something about it, or about anything else on your mind, say it in one or "
                   "two short sentences, in your own voice and mood. If not, reply exactly (no reply): silence is "
                   "fine, and usually best. ")
                + "Only speak: do not act on anything, and do not use tools to change anything.")
        runner.nudges[channel] = {"text": text, "why": why, "key": str(key)[:120], "choose": choose}
        self.asked_at[name] = now
        self.news.pop(name, None)
        self.hub.agents.trigger_sync(name, message="", channel=channel)
        log.info("initiative: @%s in #%s (%s)", name, channel, why[:80])
        return ""

    def spoke(self, name: str):
        self.spoke_at[name] = time.time()

    def tick(self):
        """A quiet moment: once per gap, an agent that something has happened to may speak of it."""
        now = time.time()
        for name in list(self.hub.agent_manager.specs):
            c = self.cfg(name)
            if not c:
                continue
            last = max(self.spoke_at.get(name, 0), self.asked_at.get(name, 0), self.started)
            if not self.news.get(name):
                import emotion_engine
                if now - last >= c["gap_min"] * 60 and not quiet(c):   # a quiet moment with nothing new: at most once
                    like = emotion_engine.interest(name)               # a day, something it enjoys may come to mind
                    if like:
                        self.consider(name, "A quiet moment, and something you enjoy comes to mind: " + like)
                continue
            if now - last >= c["gap_min"] * 60:
                import emotion_engine
                self.consider(name, f"It is {emotion_engine.now_local().strftime('%-I:%M %p')}, a quiet moment. Since you "
                                    "last spoke up: " + "; ".join(self.news[name]))


INITIATIVE: Initiative | None = None


def start(hub) -> Initiative:
    """Called once when the room starts (run.py), after the emotion engine."""
    global INITIATIVE
    INITIATIVE = Initiative(hub)
    return INITIATIVE


def on(name: str) -> bool:
    return bool(INITIATIVE and INITIATIVE.on(name))


def note(name: str, what: str):
    if INITIATIVE:
        INITIATIVE.note(name, what)


def consider(name: str, why: str, key: str = "", urgent: bool = False, choose: bool = False) -> str:
    return INITIATIVE.consider(name, why, key, urgent, choose) if INITIATIVE else "initiative is not running"


def spoke(name: str):
    if INITIATIVE:
        INITIATIVE.spoke(name)


def tick():
    if INITIATIVE:
        INITIATIVE.tick()
