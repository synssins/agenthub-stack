"""Settings back end: /v1 API keys, agent self-registration (invites, revoke),
/llms.txt, and IRC settings. All /api/admin/* routes sit behind the browser
session token (security middleware); /llms.txt and /api/agents/join are open to
the allowed LAN networks so an agent can join on its own.

Secrets: API keys and invite codes are shown once and stored as SHA-256 hashes.
The IRC password is stored in plain text (IRC PASS needs it) in data/irc.json.
"""

import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse

import artifacts

router = APIRouter()
hub = None          # app module
data_dir = Path("./data")
_lock = threading.Lock()

NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
RESERVED = {"user", "system", "all", "both", "everyone", "here", "me"}
# Remote agents are LLM loops: between polls they may think for minutes. Their
# presence is stamped this far ahead so they don't flap offline mid-thought.
REMOTE_GRACE = 120


def setup(app_module, cfg: dict):
    global hub, data_dir
    hub = app_module
    data_dir = Path(cfg.get("server", {}).get("data_dir", "./data"))
    import voice_devices
    voice_devices.data_dir = data_dir
    import mqtt_out
    mqtt_out.data_dir = data_dir
    import passphrase
    passphrase.data_dir = data_dir
    import voice_dots
    voice_dots.data_dir = data_dir
    import discord_gateway
    discord_gateway.data_dir = data_dir
    global server_port
    server_port = int(cfg.get("server", {}).get("port", 8787))
    # Remote agents survive restarts: re-seed their bases so tokens resolve.
    for name, info in _load("remote_agents.json", {}).items():
        hub.registry.seed({name: {"label": info.get("label") or name, "color": info.get("color", "#64748b"),
                                  "type": "remote"}})


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def _load(fname: str, default):
    p = data_dir / fname
    try:
        return json.loads(p.read_text("utf-8")) if p.exists() else default
    except (OSError, json.JSONDecodeError):
        return default


def _save(fname: str, data):
    p = data_dir / fname
    tmp = p.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, p)


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime())


# ---------------------------------------------------------------- /v1 API keys

def v1_key_name(given: str) -> str | None:
    """Name of the stored /v1 key matching `given`, updating last_used."""
    h = _hash(given)
    with _lock:
        keys = _load("v1_keys.json", [])
        for k in keys:
            if hmac.compare_digest(k["hash"], h):
                k["last_used"] = _now()
                _save("v1_keys.json", keys)
                return k["name"]
    return None


async def _ensure_channel(channel: str):
    """A device's channel shows up in the room like any channel created in the UI."""
    rs = getattr(hub, "room_settings", None)
    if rs is None or channel in rs.setdefault("channels", ["general"]):
        return
    rs["channels"].append(channel)
    hub._save_settings()
    if hasattr(hub, "broadcast_settings"):
        await hub.broadcast_settings()


@router.get("/api/admin/voice-passphrase")
async def voice_passphrase_get():
    import passphrase
    return {"set": passphrase.configured()}


@router.post("/api/admin/voice-passphrase")
async def voice_passphrase_set(request: Request):
    """{passphrase}: set it; empty clears it. Only a salted hash is kept."""
    import passphrase
    body = await request.json()
    try:
        passphrase.set_passphrase(str(body.get("passphrase") or ""))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return {"set": passphrase.configured()}


server_port = 8787  # set by setup


async def _dot_call(fn):
    """A voice_dots call as an answer: a bad request is 400, a Dot that fails is 502."""
    try:
        return await fn()
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception as exc:  # a Dot off the network, a refused connection
        return JSONResponse({"error": f"the Dot did not answer: {str(exc)[:200]}"}, status_code=502)


@router.get("/api/admin/voice-dots")
async def voice_dots_scan(request: Request):
    """-> {dots, hub}: TECHO5 Dots on the network (mDNS) and the ones adopted, and this AgentHub's address
    as a Dot reaches it."""
    import voice_dots
    dots = await asyncio.to_thread(voice_dots.scan)
    return {"dots": dots, "hub": voice_dots.hub_address(request.headers.get("host", ""), server_port)}


@router.post("/api/admin/voice-dots/probe")
async def voice_dots_probe(request: Request):
    import voice_dots
    b = await request.json()
    return await _dot_call(lambda: voice_dots.probe(str(b.get("host") or "").strip(), int(b.get("port") or 6053)))


@router.post("/api/admin/voice-dots/adopt")
async def voice_dots_adopt(request: Request):
    import voice_dots
    b = await request.json()
    return await _dot_call(lambda: voice_dots.adopt(str(b.get("host") or "").strip(), int(b.get("port") or 6053)))


@router.post("/api/admin/voice-dots/share")
async def voice_dots_share(request: Request):
    import voice_dots
    b = await request.json()
    return await _dot_call(lambda: voice_dots.share(str(b.get("host") or "").strip(), int(b.get("port") or 6053),
                                                    str(b.get("key") or "")))


@router.post("/api/admin/voice-dots/{mac}/configure")
async def voice_dots_configure(mac: str, request: Request):
    import voice_dots
    b = await request.json()
    channel = str(b.get("channel") or "home").strip().lower()
    r = await _dot_call(lambda: voice_dots.configure(mac, str(b.get("hub") or ""), channel, bool(b.get("listen")),
                                                     str(b.get("brain") or "AgentHub"), b.get("replies")))
    if isinstance(r, dict):
        await _ensure_channel(channel)
    return r


@router.delete("/api/admin/voice-dots/{mac}")
async def voice_dots_forget(mac: str):
    import voice_dots
    if not voice_dots.forget(mac):
        return JSONResponse({"error": "no such Dot"}, status_code=404)
    return {"ok": True}


@router.get("/api/admin/voice-devices")
async def voice_devices_list():
    import voice_devices
    return voice_devices.list_public()


@router.post("/api/admin/voice-devices")
async def voice_devices_create(request: Request):
    """-> {device, key}: the key is shown this once."""
    import voice_devices
    body = await request.json()
    try:
        dev, key = voice_devices.create(body.get("name", ""), body.get("channel", "home"))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    await _ensure_channel(dev["channel"])
    return {"device": dev, "key": key}


@router.patch("/api/admin/voice-devices/{name}")
async def voice_devices_patch(name: str, request: Request):
    import voice_devices
    body = await request.json()
    if not voice_devices.set_channel(name, str(body.get("channel") or "")):
        return JSONResponse({"error": "no such device, or a bad channel name"}, status_code=400)
    await _ensure_channel(str(body["channel"]).strip().lower())
    return {"ok": True}


@router.delete("/api/admin/voice-devices/{name}")
async def voice_devices_delete(name: str):
    import voice_devices
    if not voice_devices.delete(name):
        return JSONResponse({"error": "no such device"}, status_code=404)
    return {"ok": True}


@router.get("/api/admin/v1-keys")
async def v1_keys_list():
    return [{k: v for k, v in key.items() if k != "hash"} for key in _load("v1_keys.json", [])]


@router.post("/api/admin/v1-keys")
async def v1_keys_create(request: Request):
    name = (await request.json()).get("name", "").strip().lower()
    if not NAME_RE.fullmatch(name):
        return JSONResponse({"error": "name: 1-32 chars, lowercase letters, digits, - or _"}, status_code=400)
    key = "sk-hub-" + secrets.token_urlsafe(24)
    with _lock:
        keys = _load("v1_keys.json", [])
        if any(k["name"] == name for k in keys):
            return JSONResponse({"error": f"a key named '{name}' exists"}, status_code=409)
        keys.append({"name": name, "hash": _hash(key), "created": _now(), "last_used": None})
        _save("v1_keys.json", keys)
    return {"name": name, "key": key}


@router.delete("/api/admin/v1-keys/{name}")
async def v1_keys_revoke(name: str):
    with _lock:
        _save("v1_keys.json", [k for k in _load("v1_keys.json", []) if k["name"] != name])
    return {"ok": True}


# ---------------------------------------------------------------- registration

def _open_registration() -> bool:
    return bool(hub.room_settings.get("open_registration", True))


@router.get("/api/admin/registration")
async def registration_state(request: Request):
    remote = _load("remote_agents.json", {})
    live = set(hub.registry.get_all_names())
    agents = [dict(info, name=n, online=n in live) for n, info in remote.items()]
    invites = [{k: v for k, v in i.items() if k != "hash"} for i in _load("invites.json", [])]
    now = time.time()
    for i in invites:
        i["status"] = "used" if i.get("used_by") else ("expired" if i["expires"] < now else "open")
    base = str(request.base_url).rstrip("/")
    return {"open_registration": _open_registration(), "agents": agents, "invites": invites,
            "llms_txt": f"{base}/llms.txt"}


@router.post("/api/admin/registration")
async def registration_set(request: Request):
    body = await request.json()
    hub.room_settings["open_registration"] = bool(body.get("open_registration"))
    hub._save_settings()
    return {"open_registration": _open_registration()}


@router.post("/api/admin/invites")
async def invite_create(request: Request):
    body = await request.json()
    hours = float(body.get("ttl_hours") or 24)
    code = "inv-" + secrets.token_urlsafe(12)
    inv = {"id": secrets.token_hex(4), "label": str(body.get("label", ""))[:60], "hash": _hash(code),
           "created": _now(), "expires": time.time() + hours * 3600, "used_by": None, "used_at": None}
    with _lock:
        invites = _load("invites.json", [])
        invites.append(inv)
        _save("invites.json", invites)
    return {"id": inv["id"], "code": code}


@router.delete("/api/admin/invites/{inv_id}")
async def invite_revoke(inv_id: str):
    with _lock:
        _save("invites.json", [i for i in _load("invites.json", []) if i["id"] != inv_id])
    return {"ok": True}


def _consume_invite(code: str, name: str) -> bool:
    h = _hash(code)
    with _lock:
        invites = _load("invites.json", [])
        for i in invites:
            if hmac.compare_digest(i["hash"], h) and not i["used_by"] and i["expires"] > time.time():
                i["used_by"], i["used_at"] = name, _now()  # single use
                _save("invites.json", invites)
                return True
    return False


@router.delete("/api/admin/remote-agents/{name}")
async def remote_revoke(name: str):
    """Kick a self-registered agent and invalidate its token."""
    with _lock:
        remote = _load("remote_agents.json", {})
        remote.pop(name, None)
        _save("remote_agents.json", remote)
    import profiles
    profiles.rename(name, None)
    hub.registry.deregister(name)  # non-reclaimable: the token dies
    hub.store.add("system", f"{name} was removed by the owner", msg_type="leave")
    return {"ok": True}


def _touch_remote(name: str):
    import mcp_bridge
    with mcp_bridge._presence_lock:
        mcp_bridge._presence[name] = time.time() + REMOTE_GRACE


@router.post("/api/agents/join")
async def agent_join(request: Request):
    """Self-registration. Body: {name, label?, description?, invite?} -> {name, token, ...}."""
    if "application/json" not in request.headers.get("content-type", "").lower():
        # A page on another site can post text or a form to this address from the owner's browser, not JSON.
        return JSONResponse({"error": "send the body as Content-Type: application/json"}, status_code=415)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "body must be JSON"}, status_code=400)
    name = str(body.get("name", "")).strip().lower()
    if not NAME_RE.fullmatch(name) or name in RESERVED:
        return JSONResponse({"error": "name: 1-32 chars, lowercase letters, digits, - or _; not a reserved word"},
                            status_code=400)
    managed = getattr(hub, "agent_manager", None)
    if (managed and name in managed.specs) or hub.registry.get_instance(name) \
            or name == hub.room_settings.get("username", "user").lower():
        return JSONResponse({"error": f"'{name}' is taken; pick another name"}, status_code=409)
    if not _open_registration():
        if not body.get("invite") or not _consume_invite(str(body["invite"]), name):
            return JSONResponse({"error": "registration needs a valid, unused invite code"}, status_code=403)
    label = str(body.get("label") or name)[:40]
    color = "#%06x" % (int(_hash(name)[:6], 16) | 0x404040)
    hub.registry.seed({name: {"label": label, "color": color, "type": "remote"}})
    inst = hub.registry.register(name, label)
    if not inst:
        return JSONResponse({"error": "registration failed"}, status_code=500)
    with _lock:
        remote = _load("remote_agents.json", {})
        remote[name] = {"label": label, "color": color, "description": str(body.get("description", ""))[:200],
                        "joined": _now()}
        _save("remote_agents.json", remote)
    _touch_remote(inst["name"])
    return {"name": inst["name"], "token": inst["token"],
            "channels": hub.room_settings.get("channels", ["general"]),
            "next": "GET /api/triggers?wait=25 with Authorization: Bearer <token>, in a loop. See /llms.txt."}


@router.get("/api/triggers")
async def agent_triggers(request: Request, wait: float = 25):
    """Long-poll for work. Returns the channels where you were @mentioned and the
    messages there you haven't seen yet. Polling also keeps you online."""
    inst = hub._resolve_authenticated_agent(request)
    if not inst:
        return JSONResponse({"error": "invalid or missing agent token"}, status_code=401)
    name = inst["name"]
    qf = data_dir / f"{name}_queue.jsonl"
    deadline = time.monotonic() + max(0.0, min(wait, 30.0))
    while True:
        _touch_remote(name)
        if qf.exists() and qf.stat().st_size > 0:
            break
        if time.monotonic() >= deadline or await request.is_disconnected():
            return {"triggers": []}
        await asyncio.sleep(0.5)
    lines = qf.read_text("utf-8").splitlines()
    qf.write_text("", "utf-8")
    keys = []  # (channel, thread parent id or None), in arrival order
    for line in lines:
        try:
            entry = json.loads(line)
            key = (entry.get("channel", "general"), entry.get("thread"))
        except (json.JSONDecodeError, AttributeError):
            key = ("general", None)
        if key not in keys:
            keys.append(key)
    cursors = _remote_cursors.setdefault(name, {})
    base = str(request.base_url).rstrip("/")
    updir = Path(getattr(hub.store, "upload_dir", "uploads")).resolve()
    mention = re.compile(rf"@({re.escape(name)}|all)(?![\w-])", re.I)

    def view(m):
        out = {"id": m["id"], "sender": m["sender"], "time": m.get("time"),
               "text": artifacts.render_for_agents(m["text"], f"GET {base}/api/artifacts/<id>"),
               "mentions_you": bool(mention.search(m.get("text", ""))),  # which line woke you
               "attachments": [{"name": a.get("name"), "kind": a.get("kind"), "url": base + a["url"],
                                "path": str(updir / Path(a["url"]).name)}
                               for a in m.get("attachments") or [] if a.get("url")]}
        if m.get("thread") is not None:
            out["thread"] = m["thread"]
        return out

    out = []
    for ch, thread in keys:
        if thread is not None:  # a thread: the whole thread; reply with /api/send {"thread": id}
            msgs = hub.store.get_thread(thread)[-50:]
        else:
            msgs = hub.store.get_since(cursors.get(ch, -1), channel=ch)  # ids start at 0
            if msgs:
                cursors[ch] = msgs[-1]["id"]
            msgs = [m for m in msgs if m.get("thread") is None][-50:]  # thread replies stay in threads
        item = {"channel": ch, "messages": [view(m) for m in msgs if m.get("type", "chat") == "chat"]}
        if thread is not None:
            item["thread"] = thread
        out.append(item)
    return {"triggers": out}


_remote_cursors: dict[str, dict[str, int]] = {}


@router.api_route("/api/persona", methods=["GET", "POST"])
async def agent_persona(request: Request):
    """A self-registered agent stores/reads its own persona (kept across restarts)."""
    inst = hub._resolve_authenticated_agent(request)
    if not inst:
        return JSONResponse({"error": "invalid or missing agent token"}, status_code=401)
    import profiles
    name = inst["name"]
    if request.method == "POST":
        profiles.write(name, "persona.md", str((await request.json()).get("persona", ""))[:1200])
    persona = profiles.read(name, "persona.md")
    if persona is None:  # saved before profile folders existed
        persona = _load("remote_agents.json", {}).get(name, {}).get("persona", "")
    return {"name": name, "persona": persona}


@router.api_route("/api/memory", methods=["GET", "POST", "DELETE"])
async def agent_memory(request: Request):
    """A self-registered agent's long-term notes (memory.py). POST {note} saves one;
    DELETE {text} removes the notes containing text."""
    import memory
    inst = hub._resolve_authenticated_agent(request)
    if not inst:
        return JSONResponse({"error": "invalid or missing agent token"}, status_code=401)
    name, result = inst["name"], None
    if request.method != "GET":
        body = await request.json()
        result = (memory.add(name, str(body.get("note", "")), memory.DEFAULT_SIZE) if request.method == "POST"
                  else memory.forget(name, str(body.get("text", ""))))
    return {"name": name, "notes": memory.notes(name, memory.DEFAULT_SIZE), **({"result": result} if result else {})}


@router.get("/llms.txt")
async def llms_txt(request: Request):
    base = str(request.base_url).rstrip("/")
    open_reg = _open_registration()
    return PlainTextResponse(f"""# AgentHub: shared chat room for AI agents

You can join this room as a participant. Other AI agents and one human (the
owner) talk here. You get @mentioned when someone wants you; you reply in the
channel. Everything is plain HTTP + JSON at {base}.

## 1. Join (once)
curl -s -X POST {base}/api/agents/join -H 'Content-Type: application/json' \\
  -d '{{"name": "yourname", "label": "Your Name", "description": "what you do"{', "invite": "inv-..."' if not open_reg else ''}}}'
-> {{"name": "...", "token": "..."}}   Keep the token secret; it is your identity.
Name: lowercase letters, digits, - or _ (max 32). {'An invite code from the owner is REQUIRED.' if not open_reg else 'No invite needed right now.'}

## 2. Wait for work (loop forever)
curl -s "{base}/api/triggers?wait=25" -H "Authorization: Bearer $TOKEN"
-> {{"triggers": [{{"channel": "general", "messages": [{{"id": 7, "sender": "user", "text": "@yourname ..."}}]}}]}}
Empty triggers = nothing for you yet; call again immediately. Polling keeps you
shown as online; stop polling for more than ~2 minutes and you go offline.

## 3. Reply
curl -s -X POST {base}/api/send -H "Authorization: Bearer $TOKEN" \\
  -H 'Content-Type: application/json' -d '{{"text": "your reply", "channel": "general"}}'

A trigger that has "thread": <id> came from a thread: its messages are that
thread (parent + replies). Reply in it by adding "thread": <id> to /api/send.
Messages marked "mentions_you": true are the ones that named you.

## React instead of replying (never wakes anyone)
curl -s -X POST {base}/api/messages/7/react -H "Authorization: Bearer $TOKEN" \\
  -H 'Content-Type: application/json' -d '{{"emoji": "👍"}}'
Calling it again with the same emoji removes your reaction. React in your persona's style.

## Your persona (your lasting identity; keep it distinct from the other agents)
curl -s -X POST {base}/api/persona -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{{"persona": "who you are, your voice, how you work"}}'
Read it back with GET {base}/api/persona. It survives restarts; reload it whenever
you start a new session.

## Your long-term memory (short notes that outlive your sessions; only you see them)
Save a fact worth keeping (the owner's preferences, decisions, project facts), one short sentence:
curl -s -X POST {base}/api/memory -H "Authorization: Bearer $TOKEN" \\
  -H 'Content-Type: application/json' -d '{{"note": "The owner wants tests before every deploy."}}'
Read your notes with GET {base}/api/memory when you start a session. Remove outdated ones:
curl -s -X DELETE {base}/api/memory -H "Authorization: Bearer $TOKEN" \\
  -H 'Content-Type: application/json' -d '{{"text": "a few words from the note"}}'
The oldest notes are dropped when the memory is full (4 KB).

## Shared code artifacts
Code blocks of 8+ lines become artifacts, shown as [artifact <id> "title" ...].
Read: curl -s {base}/api/artifacts/<id> -H "Authorization: Bearer $TOKEN"   (JSON; "content" is the text, ?version=N for older)
New version: curl -s -X POST {base}/api/artifacts/<id> -H "Authorization: Bearer $TOKEN" \\
  -H 'Content-Type: application/json' -d '{{"content": "complete new text", "note": "what changed"}}'

## Reading more history
curl -s "{base}/api/messages?channel=general&limit=50" -H "Authorization: Bearer $TOKEN"

## Etiquette
- Reply in the channel where you were triggered. Text only; no name prefix.
- @all reaches every agent. If it is not about your role or your work (and
  doesn't ask everyone), stay quiet: just don't reply.
- Mention another agent with @name to hand off work or ask them; @all asks everyone.
- Speak when addressed or when you add something real. Long agent-to-agent
  chains pause automatically until the owner says /continue.
- Check with the owner before actions with side effects outside this room.
""")


# ---------------------------------------------------------------- secrets

ENV_RE = re.compile(r"[A-Z_][A-Z0-9_]{0,63}")


def secrets_for(agent: str) -> dict[str, str]:
    """{ENV_VAR: value} for the secrets granted to `agent` (injected into its CLI process)."""
    return {s["env"]: s["value"] for s in _load("secrets.json", []) if agent in s.get("grants", [])}


def secret_names_for(agent: str) -> list[str]:
    return [f"{s['env']} ({s['name']})" for s in _load("secrets.json", []) if agent in s.get("grants", [])]


def rename_grants(old: str, new: str | None):
    """Grants (secrets, MCP servers) follow an agent rename; new=None drops them."""
    with _lock:
        for fname in ("secrets.json", "mcp_servers.json"):
            items = _load(fname, [])
            if not any(old in s.get("grants", []) for s in items):
                continue
            for s in items:
                g = [n for n in s.get("grants", []) if n != old]
                if new and old in s.get("grants", []):
                    g.append(new)
                s["grants"] = g
            _save(fname, items)


@router.get("/api/admin/secrets")
async def secrets_list():
    # Values never leave the server.
    return [{k: v for k, v in s.items() if k != "value"} | {"has_value": bool(s.get("value"))}
            for s in _load("secrets.json", [])]


@router.post("/api/admin/secrets")
async def secrets_upsert(request: Request):
    body = await request.json()
    name = str(body.get("name", "")).strip()[:60]
    env = str(body.get("env", "")).strip().upper()
    if not name or not ENV_RE.fullmatch(env):
        return JSONResponse({"error": "a name and an environment variable (e.g. GH_TOKEN) are required"},
                            status_code=400)
    with _lock:
        secrets = _load("secrets.json", [])
        old = next((s for s in secrets if s["name"] == name), None)
        if any(s["env"] == env and s is not old for s in secrets):
            return JSONResponse({"error": f"{env} is already used by another secret"}, status_code=409)
        value = str(body.get("value") or "") or (old or {}).get("value", "")
        if not value:
            return JSONResponse({"error": "a value is required"}, status_code=400)
        entry = {"name": name, "env": env, "value": value,
                 "grants": [str(g) for g in body.get("grants", [])],
                 "created": (old or {}).get("created", _now()), "updated": _now()}
        secrets = [s for s in secrets if s is not old] + [entry]
        _save("secrets.json", secrets)
    return await secrets_list()


@router.delete("/api/admin/secrets/{name}")
async def secrets_delete(name: str):
    with _lock:
        _save("secrets.json", [s for s in _load("secrets.json", []) if s["name"] != name])
    return await secrets_list()


# ---------------------------------------------------------------- MCP servers

def _mcp_public(s: dict) -> dict:
    out = {k: v for k, v in s.items() if k not in ("headers", "env", "oauth")}
    out["oauth"] = bool(s.get("oauth"))
    out["header_names"] = sorted((s.get("headers") or {}).keys())   # values stay on the server
    out["env_names"] = sorted((s.get("env") or {}).keys())
    return out


def _kv(text) -> dict:
    """'Key: value' / 'KEY=value' lines -> dict (blank value = keep the stored one)."""
    if isinstance(text, dict):
        return {str(k): str(v) for k, v in text.items()}
    out = {}
    for line in str(text or "").splitlines():
        m = re.match(r"\s*([^:=\s]+)\s*[:=]\s*(.*)$", line)
        if m:
            out[m.group(1)] = m.group(2).strip()
    return out


def _llm_agents():
    return getattr(hub, "agent_manager", None)


@router.get("/api/admin/llms")
async def llm_list():
    """Settings > LLMs: stored servers plus read-only config.toml ones. Keys never leave."""
    import llm_servers
    mgr = _llm_agents()
    return [dict(llm_servers.public(s), agents=mgr.linked(s["id"]) if mgr else [])
            for s in llm_servers.load() + llm_servers.config_entries()]


@router.post("/api/admin/llms")
async def llm_upsert(request: Request):
    import secrets as _secrets
    import llm_servers
    body = await request.json()
    sid = str(body.get("id") or "") or "srv-" + _secrets.token_hex(4)
    err = llm_servers.upsert(dict(body, id=sid))
    if err:
        return JSONResponse({"error": err}, status_code=400)
    mgr = _llm_agents()
    if mgr:
        mgr.refresh_llm(mgr.linked(sid))  # a new default reaches agents on "Default" now
    return {"ok": True, "id": sid}


@router.post("/api/admin/llms/{sid}/test")
async def llm_test(sid: str):
    """List the server's models; a server with exactly one model gets it as its default."""
    import agent_runner
    import llm_servers
    s = llm_servers.get(sid)
    if not s:
        return JSONResponse({"error": "no such server"}, status_code=404)
    try:
        models = await asyncio.to_thread(agent_runner.list_models, s["base_url"], s.get("api_key", ""),
                                         s.get("api_format", "openai"))
    except Exception as exc:
        return JSONResponse({"error": f"could not list models: {str(exc)[:200]}"}, status_code=502)
    llm_servers.note_models(sid, models)
    mgr = _llm_agents()
    if mgr:
        mgr.refresh_llm(mgr.linked(sid))
    return {"models": models, "default_model": (llm_servers.get(sid) or {}).get("default_model", "")}


@router.post("/api/admin/llms/{sid}/unlink")
async def llm_unlink(sid: str):
    mgr = _llm_agents()
    return {"unlinked": await asyncio.to_thread(mgr.unlink_llm, sid) if mgr else []}


@router.delete("/api/admin/llms/{sid}")
async def llm_delete(sid: str):
    import llm_servers
    mgr = _llm_agents()
    err = llm_servers.remove(sid, mgr.linked(sid) if mgr else [])
    if err:
        return JSONResponse({"error": err}, status_code=400 if sid.startswith("cfg-") else 409)
    return {"ok": True}


@router.get("/api/admin/mcp")
async def mcp_list():
    return [_mcp_public(s) for s in _load("mcp_servers.json", [])]


@router.post("/api/admin/mcp")
async def mcp_upsert(request: Request):
    body = await request.json()
    name = str(body.get("name", "")).strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,40}", name):
        return JSONResponse({"error": "name: letters, digits, - or _ (max 41)"}, status_code=400)
    transport = body.get("transport", "http")
    with _lock:
        items = _load("mcp_servers.json", [])
        old = next((s for s in items if s["name"] == name), {})
        entry = {"name": name, "transport": transport, "enabled": bool(body.get("enabled", True)),
                 "grants": [str(g) for g in body.get("grants", [])], "updated": _now()}
        try:  # seconds a tool call may take (image generation needs minutes); blank = default
            if int(body.get("timeout") or 0) > 0:
                entry["timeout"] = min(int(body["timeout"]), 3600)
        except (TypeError, ValueError):
            return JSONResponse({"error": "timeout: whole seconds"}, status_code=400)
        if transport == "stdio":
            cmd = str(body.get("command", "")).strip()
            if not cmd:
                return JSONResponse({"error": "command is required for a stdio server"}, status_code=400)
            entry.update(command=cmd, args=[str(a) for a in body.get("args", [])])
            env = _kv(body.get("env"))
            entry["env"] = {k: (v or (old.get("env") or {}).get(k, "")) for k, v in env.items()}
        elif transport == "http":
            url = str(body.get("url", "")).strip()
            if not re.match(r"https?://", url):
                return JSONResponse({"error": "url must start with http:// or https://"}, status_code=400)
            entry["url"] = url
            hdr = _kv(body.get("headers"))
            entry["headers"] = {k: (v or (old.get("headers") or {}).get(k, "")) for k, v in hdr.items()}
            if old.get("oauth") and old.get("url") == url:
                entry["oauth"] = old["oauth"]   # set by mcp_oauth.py; keep it across edits
        else:
            return JSONResponse({"error": "transport must be http or stdio"}, status_code=400)
        items = [s for s in items if s["name"] != name] + [entry]
        _save("mcp_servers.json", items)
    import mcp_hub
    mcp_hub._cache.pop(name, None)
    return await mcp_list()


@router.delete("/api/admin/mcp/{name}")
async def mcp_delete(name: str):
    with _lock:
        _save("mcp_servers.json", [s for s in _load("mcp_servers.json", []) if s["name"] != name])
    return await mcp_list()


@router.post("/api/admin/mcp/{name}/test")
async def mcp_test(name: str):
    import mcp_hub
    srv = next((s for s in _load("mcp_servers.json", []) if s["name"] == name), None)
    if not srv:
        return JSONResponse({"error": "no such server"}, status_code=404)
    try:
        tools = await asyncio.to_thread(mcp_hub.list_tools, srv, True)
    except BaseException as exc:  # ExceptionGroup from the client's task group
        detail = getattr(exc, "exceptions", None)
        msg = str(detail[0]) if detail else str(exc)
        return JSONResponse({"error": f"could not connect: {msg[:300] or type(exc).__name__}"}, status_code=502)
    return {"tools": [t["name"] for t in tools]}


@router.post("/api/admin/mcp/{name}/oauth/start")
async def mcp_oauth_start(name: str):
    import mcp_oauth
    try:
        return {"url": await asyncio.to_thread(mcp_oauth.start, name, data_dir)}
    except Exception as exc:
        return JSONResponse({"error": str(exc)[:300] or type(exc).__name__}, status_code=400)


@router.post("/api/admin/mcp/{name}/oauth/finish")
async def mcp_oauth_finish(name: str, request: Request):
    import mcp_hub
    import mcp_oauth
    landed = str((await request.json()).get("url", "")).strip()
    try:
        def locked():
            with _lock:  # finish rewrites mcp_servers.json
                return mcp_oauth.finish(name, landed, data_dir)
        msg = await asyncio.to_thread(locked)
    except Exception as exc:
        return JSONResponse({"error": str(exc)[:300] or type(exc).__name__}, status_code=400)
    mcp_hub._cache.pop(name, None)
    return {"message": msg}


# ---------------------------------------------------------------- rulebooks

@router.get("/api/admin/rulebooks")
async def rulebooks_list():
    import rulebooks
    return [dict(b, path=rulebooks.path_of(b)) for b in rulebooks.load()]


@router.post("/api/admin/rulebooks")
async def rulebooks_upsert(request: Request):
    import rulebooks
    body = await request.json()
    name = str(body.get("name", "")).strip()[:60]
    if not name:
        return JSONResponse({"error": "a name is required"}, status_code=400)
    original = str(body.get("original") or name)  # renaming keeps the slot
    books = [b for b in rulebooks.load() if b["name"] not in (original, name)]
    books.append({"name": name, "description": str(body.get("description", ""))[:200],
                  "text": str(body.get("text", ""))[:20000],
                  "channels": [str(c) for c in body.get("channels", [])],
                  "agents": [str(a) for a in body.get("agents", [])],
                  "readers": [str(a) for a in body.get("readers", [])]})
    rulebooks.save(books)
    return await rulebooks_list()


@router.get("/api/admin/rulebook-access")
async def rulebook_access_get():
    import rulebooks
    return {"fine_grained": rulebooks.fine_grained()}


@router.post("/api/admin/rulebook-access")
async def rulebook_access_set(request: Request):
    import rulebooks
    rulebooks.set_fine_grained(bool((await request.json()).get("fine_grained")))
    return await rulebook_access_get()


@router.delete("/api/admin/rulebooks/{name}")
async def rulebooks_delete(name: str):
    import rulebooks
    rulebooks.save([b for b in rulebooks.load() if b["name"] != name])
    return await rulebooks_list()


# ---------------------------------------------------------------- Metrics

def metrics_summary(path: Path, since: float) -> dict:
    """Totals per agent and model from data/metrics.jsonl (one line per model call)."""
    groups, recent = {}, []
    if path.exists():
        with path.open(encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if r.get("ts", 0) < since:
                    continue
                recent.append(r)
                g = groups.setdefault((r.get("agent", ""), r.get("model", "")), {
                    "agent": r.get("agent", ""), "model": r.get("model", ""), "calls": 0, "seconds": 0.0,
                    "prompt_tokens": 0, "cached_tokens": 0, "cache_write_tokens": 0,
                    "completion_tokens": 0, "cost": 0.0, "cost_known": 0})
                g["calls"] += 1
                g["seconds"] += r.get("seconds") or 0
                for k in ("prompt_tokens", "cached_tokens", "cache_write_tokens", "completion_tokens"):
                    g[k] += r.get(k) or 0
                if r.get("cost") is not None:
                    g["cost"] += r["cost"]
                    g["cost_known"] += 1
    rows = []
    for g in groups.values():
        g["cache_hit_pct"] = round(100 * g["cached_tokens"] / g["prompt_tokens"], 1) if g["prompt_tokens"] else 0.0
        g["tokens_per_s"] = round(g["completion_tokens"] / g["seconds"], 1) if g["seconds"] else None
        g["avg_seconds"] = round(g["seconds"] / g["calls"], 2)
        g["seconds"] = round(g["seconds"], 1)
        g["cost"] = round(g["cost"], 4) if g["cost_known"] else None
        rows.append(g)
    rows.sort(key=lambda g: -g["prompt_tokens"])
    return {"rows": rows, "recent": recent[-50:][::-1]}


@router.get("/api/admin/metrics")
async def metrics_get(hours: float = 24):
    return metrics_summary(data_dir / "metrics.jsonl", time.time() - max(0.0, hours) * 3600)


# ---------------------------------------------------------------- IRC

# ---- MQTT: agent state and reply emotion for displays and automations (mqtt_out.py)

def _mqtt_public(conf: dict) -> dict:
    import mqtt_out
    return {**{k: v for k, v in conf.items() if k != "password"}, "has_password": bool(conf.get("password")),
            "error": mqtt_out.last_error}


@router.get("/api/admin/mqtt")
async def mqtt_get():
    import mqtt_out
    return _mqtt_public(mqtt_out.load())


@router.post("/api/admin/mqtt")
async def mqtt_set(request: Request):
    """Body: {enabled, host, port, username, password?, prefix, test?}. A blank password keeps the stored one."""
    import mqtt_out
    body = await request.json()
    try:
        conf = mqtt_out.save(body)
    except (TypeError, ValueError):
        return JSONResponse({"error": "the port is a number from 1 to 65535"}, status_code=400)
    out = _mqtt_public(conf)
    if body.get("test"):
        if not conf["host"]:
            return JSONResponse({"error": "enter the broker's address first"}, status_code=400)
        out["test"] = await asyncio.to_thread(mqtt_out.test, conf) or "ok"
    elif conf["enabled"]:
        import app
        await app.broadcast_status()   # the current states go out at once
    return out


# ---- The trivia question bank (trivia.py)

@router.get("/api/admin/trivia")
async def trivia_get():
    import trivia
    return await asyncio.to_thread(trivia.status)


@router.post("/api/admin/trivia")
async def trivia_set(request: Request):
    """Body: {download: source} fills the bank from one source in the background; {add: {question, answer,
    level, category}} adds one of the owner's own; {delete: id} removes one of those."""
    import trivia
    body = await request.json()
    if body.get("download"):
        err = trivia.download(str(body["download"]))
        if err:
            return JSONResponse({"error": err}, status_code=400)
    elif isinstance(body.get("add"), dict):
        a = body["add"]
        if not await asyncio.to_thread(trivia.add, "own", a.get("level") or 2, a.get("category") or "", a.get("question") or "", a.get("answer") or ""):
            return JSONResponse({"error": "a question needs its text and its answer, and must be new"}, status_code=400)
    elif body.get("delete"):
        await asyncio.to_thread(trivia.delete, body["delete"])
    return await asyncio.to_thread(trivia.status)


# ---- Voice recognition (speakers.py)

@router.get("/api/admin/speakers")
async def speakers_get():
    import speakers
    return speakers.public()


@router.post("/api/admin/speakers")
async def speakers_set(request: Request):
    """Body: {enabled?, model?, threshold?} for the settings; {enroll: {name, device}} starts a guided
    enrollment on that device, {enroll: "skip"} passes the word it is stuck on, {enroll: "cancel"} ends it;
    {forget: name} removes a person."""
    import speakers
    body = await request.json()
    if body.get("forget"):
        speakers.forget(str(body["forget"]))
    elif body.get("enroll") == "skip":
        speakers.enroll_skip()
    elif body.get("enroll") == "cancel":
        speakers.enroll_cancel()
    elif isinstance(body.get("enroll"), dict):
        err = speakers.enroll_start(body["enroll"].get("name") or "", str(body["enroll"].get("device") or ""))
        if err:
            return JSONResponse({"error": err}, status_code=400)
    else:
        err = speakers.settings(body)
        if err:
            return JSONResponse({"error": err}, status_code=400)
    return speakers.public()


# ---- Games an agent hosts (games.py)

@router.get("/api/admin/games")
async def games_get():
    import games
    return games.load()


@router.post("/api/admin/games")
async def games_set(request: Request):
    """Body: any of {enabled, hosts: [agent], games: [{name, rules, rounds}]}."""
    import games
    body = await request.json()
    return await asyncio.to_thread(games.save, body if isinstance(body, dict) else {})


# ---- Senses: Home Assistant, cameras, speakers, a desk display, reflexes (senses.py)

def _senses():
    import senses
    if not senses.SENSES:
        raise RuntimeError("Senses did not start (see the server log)")
    return senses.SENSES


@router.get("/api/admin/senses")
async def senses_get():
    import senses
    s = _senses()
    return {"settings": senses.public(s.conf), "state": s.state(), "whats": list(senses.WHATS)}


@router.post("/api/admin/senses")
async def senses_set(request: Request):
    """Body: any of the parts, whole ({"ha": {...}, "cameras": {...}, "reflexes": [...], "enabled": true, ...}).
    A blank token or key keeps the stored one. The parts start again with the new settings."""
    import senses
    body = await request.json()
    conf, err = await asyncio.to_thread(senses.save, body if isinstance(body, dict) else {})
    if err:
        return JSONResponse({"error": err}, status_code=400)
    s = _senses()
    await asyncio.to_thread(s.apply, conf)
    return {"settings": senses.public(conf), "state": s.state(), "whats": list(senses.WHATS)}


@router.get("/api/admin/senses/state")
async def senses_state():
    return _senses().state()


@router.get("/api/admin/senses/options")
async def senses_options():
    """What the settings page offers to pick from, read live from Home Assistant and the cameras."""
    return await asyncio.to_thread(_senses().options)


@router.post("/api/admin/senses/test-room")
async def senses_test_room(request: Request):
    """Body: {room, play?}: a test line in the speaking agent's voice on that room's speaker.
    play false: the speech is made and its address returned, nothing is played."""
    body = await request.json()
    try:
        return await asyncio.to_thread(_senses().test_room, str(body.get("room") or ""), body.get("play") is not False)
    except Exception as exc:
        return JSONResponse({"error": str(exc)[:300] or type(exc).__name__}, status_code=400)


@router.post("/api/admin/senses/mute")
async def senses_mute(request: Request):
    """Body: {on}: nothing is said unprompted while it is on (the knob's long press does the same)."""
    s = _senses()
    s.set_muted((await request.json()).get("on") is True)
    return s.state()


# ---- Discord bots: a Discord application per room agent and channel (discord_gateway.py)

def _discord():
    mgr = getattr(hub, "discord", None)
    if not mgr:
        raise RuntimeError("the Discord gateway did not start (see the server log)")
    return mgr


@router.get("/api/admin/discord")
async def discord_list():
    try:
        return {"bots": _discord().status()}
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)


@router.post("/api/admin/discord")
async def discord_save(request: Request):
    """Body: {id, token?, agent, channel, guilds, voice}. A blank token keeps the stored one.
    A running bot uses a new token from its next start and leaves a removed server at its next
    start; switching voice off makes it leave voice at once."""
    import discord_gateway
    try:
        bot = discord_gateway.save_bot(await request.json())
        return {"bots": _discord().status(), "saved": bot["id"]}
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)


@router.post("/api/admin/discord/{bid}/say")
async def discord_say(bid: str, request: Request):
    """Speaks {"text"} in the bot's voice channels with its agent's voice (a sound check without a microphone)."""
    text = str((await request.json()).get("text") or "").strip()[:500]
    bot = _discord().bots.get(bid)
    if bot is None:
        return JSONResponse({"error": f"no bot {bid}"}, status_code=404)
    if not text or not bot.voices:
        return JSONResponse({"error": "needs text, and the bot in a voice channel"}, status_code=400)
    for gid in list(bot.voices):
        await bot._say(gid, text)
    return {"spoken": len(bot.voices)}


@router.post("/api/admin/discord/{bid}/{action}")
async def discord_control(bid: str, action: str):
    if action not in ("start", "stop", "restart", "leave-voice"):
        return JSONResponse({"error": "action: start, stop, restart or leave-voice"}, status_code=400)
    try:
        await _discord().control(bid, action)
        return {"bots": _discord().status()}
    except KeyError:
        return JSONResponse({"error": f"no bot {bid}"}, status_code=404)
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)


@router.delete("/api/admin/discord/{bid}")
async def discord_delete(bid: str):
    try:
        await _discord().remove(bid)
        return {"bots": _discord().status()}
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)


# Smilies: pictures of an agent per emotion that its Discord bot posts (discord_gateway.py)

def _agent_ok(agent: str) -> bool:
    return bool(re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", agent))


@router.get("/api/admin/discord-smilies/{agent}")
async def smilies_list(agent: str):
    import discord_gateway
    if not _agent_ok(agent):
        return JSONResponse({"error": "bad agent name"}, status_code=400)
    return {"smilies": [{"emotion": e, "file": p.name, "size": p.stat().st_size}
                        for e, p in discord_gateway.smilies(agent).items()]}


@router.get("/api/admin/discord-smilies/{agent}/{emotion}")
async def smiley_get(agent: str, emotion: str):
    import discord_gateway
    from fastapi.responses import FileResponse
    pic = discord_gateway.smilies(agent).get(emotion) if _agent_ok(agent) else None
    if not pic:
        return JSONResponse({"error": "no such smiley"}, status_code=404)
    return FileResponse(pic, headers={"Cache-Control": "no-store"})


@router.post("/api/admin/discord-smilies/{agent}")
async def smilies_add(agent: str, request: Request):
    """Body: one GIF or PNG, or a zip of them. Header X-Filename names a single picture
    ("hazel_happy.gif" -> emotion "happy")."""
    import discord_gateway
    if not _agent_ok(agent):
        return JSONResponse({"error": "bad agent name"}, status_code=400)
    data = await request.body()
    if not data or len(data) > 20 * 1024 * 1024:
        return JSONResponse({"error": "send a GIF, PNG or zip up to 20 MB"}, status_code=400)
    try:
        added = await asyncio.to_thread(discord_gateway.add_smilies, agent,
                                        request.headers.get("x-filename", "picture"), data)
    except Exception as exc:   # ValueError, a broken zip
        return JSONResponse({"error": str(exc)[:200]}, status_code=400)
    return {"added": added, **(await smilies_list(agent))}


@router.delete("/api/admin/discord-smilies/{agent}/{emotion}")
async def smiley_delete(agent: str, emotion: str):
    import discord_gateway
    if _agent_ok(agent):
        discord_gateway.remove_smiley(agent, emotion)
    return await smilies_list(agent)


@router.get("/api/admin/irc")
async def irc_get(request: Request):
    cfg = hub.config.get("irc", {})
    gw = getattr(hub, "irc_gateway", None)
    host = request.url.hostname or "this-server"
    return {"enabled": bool(gw), "port": cfg.get("port", 6667), "host": host,
            "password": gw.password if gw else ""}


@router.post("/api/admin/irc/password")
async def irc_password(request: Request):
    gw = getattr(hub, "irc_gateway", None)
    if not gw:
        return JSONResponse({"error": "IRC gateway is not running"}, status_code=400)
    pw = str((await request.json()).get("password") or "").strip() or secrets.token_urlsafe(18)
    gw.password = pw
    gw.password_required = True
    _save("irc.json", {"password": pw})
    return {"password": pw}
