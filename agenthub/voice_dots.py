"""TECHO5 Dots (and other EchoLocal-family devices) found on the network, adopted, and set up as voice
devices from Settings > Voice > Devices, without Home Assistant.

A Dot speaks the ESPHome native API (port 6053) and advertises it over mDNS (_esphomelib._tcp) with TXT
platform=echolocal. Its key state is in the TXT too:

  api_provisioning=zero-psk   waiting to be adopted: the adoption window a press on the Dot opens
  api_encryption=...          it has a key already (usually Home Assistant's)

Adopting sets a key of AgentHub's own over the zero key, as Home Assistant does, and takes the Dot from
whatever had it. Sharing uses the key the Dot already has (pasted from Home Assistant), so Home Assistant
keeps it too. Configuring makes a voice device (voice_devices.py) for it and writes the AgentHub address,
that device's key and the chosen settings to the Dot's own entities, as Home Assistant would.

  data/voice_dots.json   {mac: {name, host, port, psk, device, channel}}   (mode 600; psk is the API key)
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import secrets
import socket
import threading
import time
from pathlib import Path

data_dir = Path("./data")   # set by admin_api.setup
FILE = "voice_dots.json"
SERVICE = "_esphomelib._tcp.local."
PLATFORM = "echolocal"
CLIENT = "AgentHub"
_lock = threading.RLock()

# The Dot's own entities (TECHO5 feature/agenthub and feature/voice), by object id.
URL, KEY, BRAIN, LISTEN, REPLIES, STATUS = ("agenthub_url", "agenthub_key", "brain", "agenthub_listen",
                                            "replies_play_on", "agenthub_status")
BRAINS = ("AgentHub", "Home Assistant", "Chat model")


def _load() -> dict:
    try:
        return json.loads((data_dir / FILE).read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def _save(dots: dict):
    p = data_dir / FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(dots, f, indent=2)
    os.replace(tmp, p)


def _public(mac: str, d: dict) -> dict:
    return {"mac": mac, **{k: d.get(k) for k in ("name", "host", "port", "device", "channel")}, "state": "adopted"}


def adopted() -> list[dict]:
    return [_public(m, d) for m, d in sorted(_load().items())]


def state_of(txt: dict, mac: str) -> str:
    """adopted (this AgentHub has its key), ready (waiting to be adopted), owned (has another's key) or
    open (no key at all)."""
    if mac and mac in _load():
        return "adopted"
    if txt.get("api_provisioning") == "zero-psk":
        return "ready"
    if txt.get("api_encryption"):
        return "owned"
    return "open"


def scan(seconds: float = 4.0) -> list[dict]:
    """The EchoLocal-family devices advertising on the network, by mDNS. Blocking: run it in a thread."""
    from zeroconf import ServiceBrowser, ServiceListener, Zeroconf
    found: dict[str, dict] = {}

    class Listener(ServiceListener):
        def add_service(self, zc, kind, name):
            info = zc.get_service_info(kind, name, 2000)
            if not info:
                return
            txt = {k.decode(): (v or b"").decode() for k, v in info.properties.items()}
            if txt.get("platform") != PLATFORM:
                return
            v4 = [a for a in info.parsed_addresses() if ":" not in a]
            mac = txt.get("mac", "")
            found[mac or name] = {"name": txt.get("friendly_name") or name.split(".")[0], "host": (v4 or [""])[0],
                                  "port": info.port, "mac": mac, "board": txt.get("board", ""),
                                  "state": state_of(txt, mac)}

        def update_service(self, zc, kind, name):
            self.add_service(zc, kind, name)

        def remove_service(self, zc, kind, name):
            pass

    zc = Zeroconf()
    try:
        ServiceBrowser(zc, SERVICE, Listener())
        time.sleep(seconds)
    finally:
        zc.close()
    known = _load()
    for mac, d in known.items():  # adopted Dots that did not answer just now
        found.setdefault(mac, {**_public(mac, d), "board": "", "state": "adopted", "seen": False})
    return sorted(found.values(), key=lambda d: d["name"].lower())


def _client(host: str, port: int, psk: str | None):
    from aioesphomeapi import APIClient
    return APIClient(host, int(port or 6053), "", noise_psk=psk or None, client_info=CLIENT)


async def _connected(host: str, port: int, psk: str | None):
    cli = _client(host, port, psk)
    await asyncio.wait_for(cli.connect(login=True), 15)
    return cli


async def probe(host: str, port: int = 6053) -> dict:
    """What a Dot typed in by address says about itself: its name and MAC, or that it needs a key."""
    from aioesphomeapi import ZERO_NOISE_PSK
    for psk in (ZERO_NOISE_PSK, None):
        try:
            cli = await _connected(host, port, psk)
        except Exception:
            continue
        try:
            info = await cli.device_info()
        finally:
            await cli.disconnect()
        mac = (info.mac_address or "").replace(":", "").lower()
        return {"name": info.friendly_name or info.name, "host": host, "port": port, "mac": mac,
                "state": "adopted" if mac in _load() else ("ready" if psk else "open")}
    return {"name": host, "host": host, "port": port, "mac": "", "state": "owned"}


async def adopt(host: str, port: int = 6053) -> dict:
    """Set a key of AgentHub's own on a Dot waiting to be adopted, and keep it."""
    from aioesphomeapi import ZERO_NOISE_PSK
    raw = secrets.token_bytes(32)
    psk = base64.b64encode(raw).decode()
    try:
        cli = await _connected(host, port, ZERO_NOISE_PSK)
    except Exception as exc:
        raise ValueError(f"the Dot is not waiting to be adopted (open its adoption window first): {exc}") from exc
    try:
        info = await cli.device_info()
        if not await cli.noise_encryption_set_key(raw):
            raise ValueError("the Dot refused the new key")
    finally:
        await cli.disconnect()
    await asyncio.sleep(1)
    return await _keep(host, port, psk, info)


async def share(host: str, port: int, psk: str) -> dict:
    """Use the key a Dot already has (from Home Assistant), so both can reach it."""
    psk = str(psk or "").strip()
    try:
        if len(base64.b64decode(psk, validate=True)) != 32:
            raise ValueError
    except Exception as exc:
        raise ValueError("the key is the Dot's 44-character API encryption key, as Home Assistant shows it") from exc
    try:
        cli = await _connected(host, port, psk)
    except Exception as exc:
        raise ValueError(f"the Dot did not accept that key: {exc}") from exc
    try:
        info = await cli.device_info()
    finally:
        await cli.disconnect()
    return await _keep(host, port, psk, info)


async def _keep(host: str, port: int, psk: str, info) -> dict:
    """Keep a Dot once its key is proven by a connection with it."""
    cli = await _connected(host, port, psk)
    await cli.disconnect()
    mac = (info.mac_address or "").replace(":", "").lower()
    with _lock:
        dots = _load()
        old = dots.get(mac, {})
        dots[mac] = {**old, "name": info.friendly_name or info.name, "host": host, "port": int(port), "psk": psk}
        _save(dots)
    return _public(mac, dots[mac])


def forget(mac: str) -> bool:
    with _lock:
        dots = _load()
        if dots.pop(mac, None) is None:
            return False
        _save(dots)
        return True


def device_name(dot_name: str) -> str:
    """A voice device name (voice_devices.NAME_RE) from the Dot's name: "Echo Office" -> "echo-office"."""
    return (re.sub(r"[^a-z0-9]+", "-", dot_name.lower()).strip("-") or "dot")[:32]


async def configure(mac: str, hub_url: str, channel: str, listen: bool, brain: str = "AgentHub",
                    replies: str | None = None) -> dict:
    """Make the Dot a voice device of this AgentHub: a device key and channel here, and on the Dot the
    AgentHub address, that key, who answers and whether it listens without a wake word."""
    import voice_devices
    if brain not in BRAINS:
        raise ValueError(f"answered by: one of {', '.join(BRAINS)}")
    if not re.match(r"https?://[^/\s]+$", hub_url or ""):
        raise ValueError("the AgentHub address is like http://192.168.1.50:8787")
    with _lock:
        dot = _load().get(mac)
    if not dot:
        raise ValueError("adopt the Dot, or give it its key, first")
    name = device_name(dot["name"])
    voice_devices.delete(name)  # a key is shown once: configuring again makes a fresh one
    _, key = voice_devices.create(name, channel)
    cli = await _connected(dot["host"], dot["port"], dot["psk"])
    try:
        entities, _ = await cli.list_entities_services()
        by_id = {e.object_id: e for e in entities}
        missing = [o for o in (URL, KEY, BRAIN, LISTEN) if o not in by_id]
        if missing:
            raise ValueError(f"this Dot's firmware has no AgentHub settings ({', '.join(missing)}): update it first")
        states: dict[str, object] = {}
        cli.subscribe_states(lambda s: states.__setitem__(s.key, getattr(s, "state", None)))
        cli.text_command(by_id[URL].key, hub_url)
        cli.text_command(by_id[KEY].key, key)
        if replies is not None and REPLIES in by_id:
            cli.text_command(by_id[REPLIES].key, replies)
        await asyncio.sleep(1)  # the address and key are saved before AgentHub can be chosen
        cli.select_command(by_id[BRAIN].key, brain)
        cli.switch_command(by_id[LISTEN].key, bool(listen))
        await asyncio.sleep(2)
        got = {o: states.get(by_id[o].key) for o in (URL, BRAIN, LISTEN, STATUS) if o in by_id}
    finally:
        await cli.disconnect()
    with _lock:
        dots = _load()
        dots[mac] = {**dots.get(mac, dot), "device": name, "channel": channel}
        _save(dots)
    ok = got.get(URL) == hub_url and got.get(BRAIN) == brain and got.get(LISTEN) == bool(listen)
    return {"ok": ok, "device": name, "channel": channel, "dot": got}


def hub_address(request_host: str, port: int) -> str:
    """This AgentHub's address as a Dot on the LAN reaches it: the host the admin page was opened by,
    unless that was loopback, else this machine's LAN address."""
    host = (request_host or "").split(":")[0]
    if host and host not in ("localhost", "127.0.0.1", "::1"):
        return f"http://{request_host}" if ":" in request_host else f"http://{host}:{port}"
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))  # no packet is sent: this only picks the outgoing interface
        return f"http://{s.getsockname()[0]}:{port}"
    finally:
        s.close()
