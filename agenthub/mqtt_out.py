"""Publishes each agent's state and the emotion of its replies to an MQTT broker, for displays
and automations outside the room (a face on a desk, Home Assistant).

  <prefix>/<agent>/state     idle | busy | offline                     (retained)
  <prefix>/<agent>/emotion   the tag of its last reply without brackets, or "neutral" (retained)
  <prefix>/<agent>/status    {"state", "emotion", "channel", "ts"} as JSON             (retained)
  <prefix>/<agent>/speaking  true | false: its speech is being fetched or played, where AgentHub can tell
  <prefix>/<agent>/mood      agents with the emotion engine on (emotion_engine.py): {"feeling", "word", "face",
                             "pleasure", "arousal", "dominance", "mood", "initiative", "ts"} as JSON (retained);
                             "initiative": it may speak unprompted (initiative.py)

Settings > Providers > MQTT writes data/mqtt.json (mode 600; the password is never sent back).
Publishing never blocks or fails a turn: messages go through one background thread.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)
data_dir = Path("./data")   # set by admin_api.setup
FILE = "mqtt.json"
DEFAULTS = {"enabled": False, "host": "", "port": 1883, "username": "", "password": "", "prefix": "agenthub"}
_q: queue.Queue = queue.Queue(maxsize=500)
_worker: threading.Thread | None = None
_last: dict[str, dict] = {}     # agent -> {"state", "emotion", "channel"}
_lock = threading.Lock()
last_error = ""                 # shown on the settings page


def load() -> dict:
    try:
        return {**DEFAULTS, **json.loads((data_dir / FILE).read_text("utf-8"))}
    except (OSError, ValueError):
        return dict(DEFAULTS)


def save(new: dict) -> dict:
    """Store the settings; a blank password keeps the stored one."""
    old = load()
    conf = {
        "enabled": bool(new.get("enabled")),
        "host": str(new.get("host") or "").strip()[:200],
        "port": max(1, min(65535, int(new.get("port") or 1883))),
        "username": str(new.get("username") or "").strip()[:100],
        "password": str(new.get("password") or "") or old["password"],
        "prefix": re.sub(r"[^A-Za-z0-9_/-]", "", str(new.get("prefix") or "agenthub")).strip("/")[:60] or "agenthub",
    }
    tmp = data_dir / "mqtt.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # holds the broker password
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(conf, f, indent=2)
    os.replace(tmp, data_dir / FILE)
    with _lock:
        _last.clear()   # everything is published again to the new broker
    return conf


def _send(conf: dict, msgs: list[tuple[str, str]]):
    import paho.mqtt.publish as publish
    # paho 2.x logs "Client is not connected" after every clean one-shot publish; real failures raise (see _run)
    logging.getLogger("paho.mqtt.client").setLevel(logging.CRITICAL)
    auth = {"username": conf["username"], "password": conf["password"]} if conf["username"] else None
    publish.multiple([{"topic": t, "payload": p, "qos": 0, "retain": True} for t, p in msgs],
                     hostname=conf["host"], port=conf["port"], auth=auth, client_id="agenthub", keepalive=10)


def _run():
    global last_error
    while True:
        msgs = [_q.get()]
        while not _q.empty() and len(msgs) < 50:
            msgs.append(_q.get_nowait())
        conf = load()
        if not (conf["enabled"] and conf["host"]):
            continue
        try:
            _send(conf, msgs)
            last_error = ""
        except Exception as exc:
            if str(exc) != last_error:
                log.warning("MQTT publish to %s:%s failed: %s", conf["host"], conf["port"], exc)
            last_error = str(exc)[:200]


def _publish(agent: str, conf: dict):
    global _worker
    cur = _last[agent]
    base = f"{conf['prefix']}/{re.sub(r'[^A-Za-z0-9_-]', '_', agent)}"
    for pair in ((f"{base}/state", cur["state"]), (f"{base}/emotion", cur["emotion"]),
                 (f"{base}/status", json.dumps({**cur, "ts": int(time.time())}))):
        try:
            _q.put_nowait(pair)
        except queue.Full:
            return
    if _worker is None or not _worker.is_alive():
        _worker = threading.Thread(target=_run, daemon=True, name="mqtt-out")
        _worker.start()


def _update(agent: str, **changes):
    conf = load()
    if not (conf["enabled"] and conf["host"]):
        return
    with _lock:
        if agent not in _last and agent not in _speaking:   # first word about this agent since the start:
            try:                                             # a retained "speaking: true" from before must not stand
                _q.put_nowait((f"{conf['prefix']}/{re.sub(r'[^A-Za-z0-9_-]', '_', agent)}/speaking", "false"))
            except queue.Full:
                pass
        cur = _last.setdefault(agent, {"state": "offline", "emotion": "neutral", "channel": ""})
        new = {**cur, **changes}
        if new == cur and "emotion" not in changes:   # an emotion is published for every reply
            return
        _last[agent] = new
        _publish(agent, conf)


def states(status: dict):
    """The room's status map (agents.get_status()): publishes the agents whose state changed."""
    for name, info in status.items():
        if isinstance(info, dict):
            state = "offline" if not info.get("available") else "busy" if info.get("busy") else "idle"
            _update(name, state=state, channel=info.get("busy_channel") or "")


def emotion(agent: str, tags: list[str], channel: str = ""):
    """The emotion of a reply just posted: its first tag, or neutral."""
    _update(agent, emotion=tags[0].strip("[]") if tags else "neutral", channel=channel)


def mood(agent: str, state: dict | None):
    """The agent's feelings changed (emotion_engine.py). None: the engine was switched off for it,
    and the retained message is cleared."""
    global _worker
    conf = load()
    if not (conf["enabled"] and conf["host"]):
        return
    payload = json.dumps({**state, "ts": int(time.time())}) if state else ""
    try:
        _q.put_nowait((f"{conf['prefix']}/{re.sub(r'[^A-Za-z0-9_-]', '_', agent)}/mood", payload))
    except queue.Full:
        return
    with _lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_run, daemon=True, name="mqtt-out")
            _worker.start()


_speaking: dict[str, threading.Timer] = {}   # agent -> the timer that ends its speech if nothing else does


def speaking(agent: str | None, on: bool = True, seconds: float = 30.0):
    """Speech of that agent starts (a device or browser fetches its audio) or, with on=False,
    ends; agent None ends it for every agent that was speaking. Nothing always reports the end
    (a read-aloud in the browser does not), so a start ends by itself after `seconds`."""
    global _worker
    conf = load()
    if not (conf["enabled"] and conf["host"]):
        return
    with _lock:
        for name in ([agent] if agent else list(_speaking)):
            was = _speaking.pop(name, None)
            if was:
                was.cancel()
            if on:
                _speaking[name] = threading.Timer(max(2.0, min(120.0, seconds)), speaking, (name, False))
                _speaking[name].daemon = True
                _speaking[name].start()
            if on == bool(was):
                continue      # still speaking (only the timer moved), or it was not speaking
            try:
                _q.put_nowait((f"{conf['prefix']}/{re.sub(r'[^A-Za-z0-9_-]', '_', name)}/speaking", "true" if on else "false"))
            except queue.Full:
                return
            if _worker is None or not _worker.is_alive():
                _worker = threading.Thread(target=_run, daemon=True, name="mqtt-out")
                _worker.start()


def test(conf: dict) -> str:
    """Publish one message now. -> "" or the error."""
    try:
        _send(conf, [(f"{conf['prefix']}/hub", "online")])
        return ""
    except Exception as exc:
        return str(exc)[:200] or type(exc).__name__
