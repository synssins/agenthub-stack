"""Always-listening voice devices (e.g. Echo Dots on TECHO5): each has a name, a key and the
channel its speech goes to. Settings > Voice > Devices creates them; the key is shown once.

  data/voice_devices.json   [{name, channel, hash, created, last_seen}]   (mode 600)

Keys are stored only as SHA-256 hashes, like the /v1 keys.
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
FILE = "voice_devices.json"
NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
CHANNEL_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,19}")  # the room's own rule for channel names (app.py)
_lock = threading.RLock()


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def _load() -> list[dict]:
    try:
        return json.loads((data_dir / FILE).read_text("utf-8"))
    except FileNotFoundError:
        return []


def _save(items: list[dict]):
    p = data_dir / FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(items, f, indent=2)
    os.replace(tmp, p)


def _public(d: dict) -> dict:
    return {k: d.get(k) for k in ("name", "channel", "created", "last_seen")}


def create(name: str, channel: str) -> tuple[dict, str]:
    """-> (public entry, plaintext key: shown once). ValueError on a bad or taken name."""
    name, channel = str(name or "").strip().lower(), str(channel or "").strip().lower() or "home"
    if not NAME_RE.fullmatch(name):
        raise ValueError("name: 1-32 chars, lowercase letters, digits, - or _")
    if not CHANNEL_RE.fullmatch(channel):
        raise ValueError("channel: 1-20 chars, lowercase letters, digits or -")
    key = "vd-" + secrets.token_urlsafe(32)
    with _lock:
        items = _load()
        if any(d["name"] == name for d in items):
            raise ValueError(f"a device named {name} already exists")
        entry = {"name": name, "channel": channel, "hash": _hash(key), "created": time.time(), "last_seen": None}
        items.append(entry)
        _save(items)
    return _public(entry), key


def list_public() -> list[dict]:
    return [_public(d) for d in _load()]


def delete(name: str) -> bool:
    with _lock:
        items = _load()
        kept = [d for d in items if d["name"] != name]
        if len(kept) == len(items):
            return False
        _save(kept)
        return True


def set_channel(name: str, channel: str) -> bool:
    channel = str(channel or "").strip().lower()
    if not CHANNEL_RE.fullmatch(channel):
        return False
    with _lock:
        items = _load()
        d = next((x for x in items if x["name"] == name), None)
        if not d:
            return False
        d["channel"] = channel
        _save(items)
        return True


def authenticate(authorization: str) -> dict | None:
    """The device for an "Authorization: Bearer <key>" header, or None. Updates last_seen."""
    if not authorization.lower().startswith("bearer "):
        return None
    h = _hash(authorization[7:].strip())
    with _lock:
        items = _load()
        for d in items:
            if hmac.compare_digest(d["hash"], h):
                now = time.time()
                if not d.get("last_seen") or now - d["last_seen"] > 30:  # don't rewrite the file every utterance
                    d["last_seen"] = now
                    _save(items)
                return _public(d)
    return None
