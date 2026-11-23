"""OpenAI-compatible surface: the room as models.

GET  /v1/models            -> the room model plus one model per registered agent
POST /v1/chat/completions  -> posts the latest user line into the room and
                              returns the agents' replies (stream or not)

Model ids: `agenthub-room` (configurable) talks to every online agent (@all);
an agent's name talks to that agent only (@name). The room keeps its own
history, so only the newest user message of each request is posted; clients
that resend the whole conversation (OpenCode) work unchanged.

Auth: `Authorization: Bearer <key>`, keys from [v1.keys] in config
(name = key). Messages post under the key's name, as a human sender, so
routing and the loop guard treat them like the web UI.

Not supported yet: tool calls. `tools` in a request are ignored and agents
answer in text (CLI agents still use their own tools).

Config:
    [v1]
    model_id = "agenthub-room"
    channel = "general"
    settle_seconds = 8        # end the round this long after the last reply
    round_cap_seconds = 300   # hard cap (CLI agents can take minutes)
    [v1.keys]
    opencode = "sk-hub-..."
"""

import asyncio
import hmac
import json
import time
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

router = APIRouter()
hub = None   # app module, set by setup()
cfg: dict = {}


def setup(app_module, config: dict):
    global hub, cfg
    hub = app_module
    cfg = config.get("v1", {})


def _err(status: int, message: str, kind: str = "invalid_request_error"):
    return JSONResponse({"error": {"message": message, "type": kind}}, status_code=status)


def _key_name(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return None
    given = auth[7:].strip().encode()
    for name, key in cfg.get("keys", {}).items():
        if key and hmac.compare_digest(given, str(key).encode()):
            return name
    import admin_api  # keys created in the settings UI
    return admin_api.v1_key_name(given.decode())


def _room_id() -> str:
    return cfg.get("model_id", "agenthub-room")


def _agents() -> dict[str, dict]:
    return hub.registry.get_all() if hub.registry else {}


def _last_user_text(messages) -> str:
    for m in reversed(messages or []):
        if isinstance(m, dict) and m.get("role") == "user":
            c = m.get("content")
            if isinstance(c, list):  # multimodal parts: keep the text
                c = "\n".join(p.get("text", "") for p in c if isinstance(p, dict) and p.get("type") == "text")
            return (c or "").strip()
    return ""


@router.get("/v1/models")
async def list_models(request: Request):
    if not _key_name(request):
        return _err(401, "invalid API key", "authentication_error")
    now = int(time.time())
    ids = [_room_id()] + sorted(_agents())
    return {"object": "list",
            "data": [{"id": i, "object": "model", "created": now, "owned_by": "agenthub"} for i in ids]}


async def _round(sender: str, text: str, target: str | None):
    """Post `text` and yield agent reply messages until the round settles."""
    channel = cfg.get("channel", "general")
    settle = float(cfg.get("settle_seconds", 8))
    cap = float(cfg.get("round_cap_seconds", 300))
    posted_text = f"@{target} {text}" if target else (text if "@" in text else f"@all {text}")
    expected = set(hub.router.parse_mentions(posted_text))
    online = set(hub.registry.get_active_names()) if hub.registry else set()
    expected &= online
    msg = hub.store.add(sender, posted_text, channel=channel)
    if not expected:
        return
    cursor, start, last_reply, replied = msg["id"], time.monotonic(), None, set()
    while True:
        await asyncio.sleep(0.5)
        for m in hub.store.get_since(cursor, channel=channel):
            cursor = max(cursor, m["id"])
            who = m.get("sender", "").lower()
            if m.get("type", "chat") != "chat" or who not in {n.lower() for n in _agents()}:
                continue
            if target and who != target.lower():
                continue
            replied.add(who)
            last_reply = time.monotonic()
            yield m
        now = time.monotonic()
        if now - start > cap:
            return
        if last_reply is not None:
            if expected <= replied and now - last_reply > (2 if target else 1):
                return  # everyone answered; short grace for follow-ups
            if now - last_reply > settle:
                return


def _format(m: dict) -> str:
    import artifacts
    return f"{m['sender']}: {artifacts.render_for_agents(m['text'], 'GET /api/artifacts/<id>')}"


@router.post("/v1/chat/completions")
async def chat_completions(request: Request):
    sender = _key_name(request)
    if not sender:
        return _err(401, "invalid API key", "authentication_error")
    try:
        body = await request.json()
    except Exception:
        return _err(400, "body must be JSON")
    text = _last_user_text(body.get("messages"))
    if not text:
        return _err(400, "no user message in messages")
    model = str(body.get("model", ""))
    if model == _room_id():
        target = None
    else:
        target = next((n for n in _agents() if n.lower() == model.lower()), None)
        if not target:
            return _err(404, f"model '{model}' not found; use {_room_id()} or an agent name")
    cid = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())

    if not body.get("stream"):
        parts = [_format(m) async for m in _round(sender, text, target)]
        content = "\n\n".join(parts) or "(the room was silent: no agent replied)"
        return {"id": cid, "object": "chat.completion", "created": created, "model": model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}

    def chunk(delta: dict, finish=None) -> str:
        return "data: " + json.dumps({"id": cid, "object": "chat.completion.chunk", "created": created,
                                      "model": model, "choices": [{"index": 0, "delta": delta,
                                                                   "finish_reason": finish}]}) + "\n\n"

    async def events():
        yield chunk({"role": "assistant", "content": ""})
        first, any_reply = True, False
        async for m in _round(sender, text, target):
            if await request.is_disconnected():
                return
            piece = ("" if first else "\n\n") + _format(m)
            first, any_reply = False, True
            for i in range(0, len(piece), 160):  # small pieces so it reads as live
                yield chunk({"content": piece[i:i + 160]})
                await asyncio.sleep(0.03)
        if not any_reply:
            yield chunk({"content": "(the room was silent: no agent replied)"})
        yield chunk({}, "stop")
        yield "data: [DONE]\n\n"

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache"})
