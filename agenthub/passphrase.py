"""A passphrase for actions that open the house: covers (garage doors, gates, blinds), locks and alarms.

An agent's Home Assistant tool call that would change one is held instead of run, and the agent is told
to ask for the passphrase. Said or typed by the owner within HOLD seconds on the same channel, the held
call runs and the words are never posted or kept (app.py checks speech, and what the owner types in the
web chat, before posting it). Off until a passphrase is set in Settings > Voice.

  data/passphrase.json   {"salt", "hash", "words"}   (mode 600; words is the count, not the words)

Spoken, it is a check against a false wake and a passing guest, not a secret from anyone in the room.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path

data_dir = Path("./data")   # set by admin_api.setup
FILE = "passphrase.json"
HOLD = 30  # seconds a held action waits for the passphrase

# What opens the house, by Home Assistant domain and device class.
DOMAINS = ("cover", "lock", "alarm_control_panel")
CLASSES = ("garage", "garage_door", "door", "gate", "lock", "window")
# Names that mean the same when the agent names a device without its domain.
WORDS = ("garage", "door", "gate", "lock", "blind", "shade", "shutter", "curtain", "awning", "alarm")
# Domains that are never the house's doors, whatever their name ("front door light").
SAFE = ("light", "switch", "fan", "media_player", "climate", "scene", "script", "vacuum", "sensor")
INTENTS = ("HassTurnOn", "HassTurnOff", "HassSetPosition", "HassStopMoving", "HassToggle")
# homeassistant.turn_on and its kin act on whatever they are aimed at; aimed at a place, that includes its doors
GENERIC = ("turn_on", "turn_off", "toggle")
PLACES = ('"area_id"', '"device_id"', '"label_id"', '"floor_id"')
DIGITS = {"zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
          "six": "6", "seven": "7", "eight": "8", "nine": "9"}

_lock = threading.RLock()
_held: dict[str, dict] = {}  # channel -> {"agent", "server", "tool", "args", "until"}


def _tokens(text: str) -> list[str]:
    """Words as speech to text writes them, made comparable: lowercase, no punctuation, number words and
    digit groups as one run of digits ("one two 3 4" and "1234" alike)."""
    out = []
    for w in re.findall(r"[a-z0-9]+", str(text).lower()):
        w = DIGITS.get(w, w)
        if w.isdigit() and out and out[-1].isdigit():
            out[-1] += w
        else:
            out.append(w)
    return out


def _digest(salt: str, words: list[str]) -> str:
    return hashlib.sha256((salt + " ".join(words)).encode()).hexdigest()


def _load() -> dict:
    try:
        return json.loads((data_dir / FILE).read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def configured() -> bool:
    return bool(_load().get("hash"))


def set_passphrase(text: str):
    """Set it, or clear it with an empty one. ValueError when it has no words."""
    p = data_dir / FILE
    words = _tokens(text)
    with _lock:
        if not str(text).strip():
            p.unlink(missing_ok=True)
            return
        if not words:
            raise ValueError("a passphrase needs at least one word or digit")
        salt = secrets.token_hex(16)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"salt": salt, "hash": _digest(salt, words), "words": len(words)}, f)
        os.replace(tmp, p)


def said(text: str) -> bool:
    """Whether text contains the passphrase, as a run of its words anywhere in it."""
    d = _load()
    if not d.get("hash"):
        return False
    words, n = _tokens(text), int(d.get("words") or 0)
    return any(hmac.compare_digest(_digest(d["salt"], words[i:i + n]), d["hash"])
               for i in range(len(words) - n + 1)) if n else False


def alone(text: str) -> bool:
    """Whether text is the passphrase with at most a few words around it."""
    return said(text) and len(_tokens(text)) <= int(_load().get("words") or 0) + 4


def protected(tool: str, args: dict) -> bool:
    """Whether a Home Assistant MCP tool call would open, close or unlock part of the house."""
    if not isinstance(args, dict):
        return False
    if tool.endswith("call_action"):
        domain, _, verb = str(args.get("action") or "").lower().partition(".")
        target = json.dumps([args.get("target") or {}, args.get("data") or {}]).lower()
        if domain in DOMAINS or any(f'"{d}.' in target for d in DOMAINS):
            return True
        return domain == "homeassistant" and verb in GENERIC and any(k in target for k in PLACES)
    if not any(tool.endswith(i) for i in INTENTS):
        return False
    domains = args.get("domain") or []
    domains = [domains] if isinstance(domains, str) else list(domains)
    classes = args.get("device_class") or []
    classes = [classes] if isinstance(classes, str) else list(classes)
    if any(d in DOMAINS for d in domains) or any(c in CLASSES for c in classes):
        return True
    if domains and all(d in SAFE for d in domains):
        return False
    named = _tokens(" ".join(str(args.get(k) or "") for k in ("name", "area")))
    return any(w.rstrip("s") in WORDS for w in named)


def hold(channel: str, agent: str, server: dict, tool: str, args: dict, now: float | None = None):
    with _lock:
        _held[channel] = {"agent": agent, "server": server, "tool": tool, "args": args,
                          "until": (time.time() if now is None else now) + HOLD}


def take(channel: str, now: float | None = None) -> dict | None:
    """The action held on channel, still waiting, taken off the hold; None when there is none."""
    now = time.time() if now is None else now
    with _lock:
        h = _held.pop(channel, None)
    return h if h and h["until"] >= now else None


def waiting(channel: str, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    with _lock:
        h = _held.get(channel)
        return bool(h and h["until"] >= now)
