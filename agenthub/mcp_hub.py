"""MCP servers for room agents: define once in Settings > MCP servers, grant per agent.

- API agents (Qwen, OpenRouter, ...): AgentHub is the MCP client. Each granted
  server's tools appear in the agent's tool loop as mcp__<server>__<tool>.
- Claude Code agents: a per-agent --mcp-config file (in the protected data dir)
  lists the granted servers; mcp__<server> is added to their allowed tools.
- Antigravity (agy) reads one fixed global MCP file, so it can't get per-agent
  grants; it keeps whatever ~/.gemini/config/mcp_config.json lists.

Header and env values can hold credentials: they are never returned to the browser.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
from datetime import timedelta
from pathlib import Path

CALL_TIMEOUT = 120
LIST_TTL = 300
_cache: dict[str, tuple[float, list[dict]]] = {}
_lock = threading.Lock()


def _admin():
    import admin_api
    return admin_api


def servers() -> list[dict]:
    return _admin()._load("mcp_servers.json", [])


def servers_for(agent: str) -> list[dict]:
    return [s for s in servers() if agent in s.get("grants", []) and s.get("enabled", True)]


def safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", name)


def _headers(server: dict) -> dict:
    """Static headers, plus a fresh OAuth bearer for servers authorised with mcp_oauth.py."""
    hdr = dict(server.get("headers") or {})
    oa = server.get("oauth")
    if not oa:
        return hdr
    with _lock:
        oa = next((s.get("oauth") for s in servers() if s["name"] == server["name"]), None) or oa
        if oa.get("expires_at", 0) < time.time() + 60:
            oa = _refresh(server["name"])
    hdr["Authorization"] = f"Bearer {oa['access_token']}"
    return hdr


def _refresh(name: str) -> dict:
    import urllib.parse
    import urllib.request
    adm = _admin()
    with adm._lock:
        items = adm._load("mcp_servers.json", [])
        srv = next(s for s in items if s["name"] == name)
        oa = srv["oauth"]
        body = urllib.parse.urlencode({"grant_type": "refresh_token", "refresh_token": oa["refresh_token"],
                                       "client_id": oa["client_id"]}).encode()
        with urllib.request.urlopen(urllib.request.Request(oa["token_endpoint"], body), timeout=30) as r:
            tok = json.load(r)
        oa["access_token"] = tok["access_token"]
        oa["refresh_token"] = tok.get("refresh_token", oa["refresh_token"])  # rotated tokens are single-use
        oa["expires_at"] = int(time.time()) + int(tok.get("expires_in", 3600))
        adm._save("mcp_servers.json", items)
        return oa


# ------------------------------------------------------------------ client

async def _with_session(server: dict, fn):
    from mcp import ClientSession
    timeout = timedelta(seconds=int(server.get("timeout") or CALL_TIMEOUT))  # e.g. image generation
    if server.get("transport") == "stdio":
        from mcp.client.stdio import StdioServerParameters, stdio_client
        params = StdioServerParameters(command=server["command"], args=server.get("args", []),
                                       env={**os.environ, **server.get("env", {})})
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w, read_timeout_seconds=timeout) as s:
                await s.initialize()
                return await fn(s)
    from mcp.client.streamable_http import streamablehttp_client
    async with streamablehttp_client(server["url"], headers=_headers(server) or None,
                                     timeout=30, sse_read_timeout=timeout.total_seconds()) as (r, w, _):
        async with ClientSession(r, w, read_timeout_seconds=timeout) as s:
            await s.initialize()
            return await fn(s)


def _run(coro, server: dict | None = None):
    """Runner threads have no event loop; give each call its own. The hard stop follows the
    server's own tool timeout (image generation needs minutes), plus time to start the server."""
    return asyncio.run(asyncio.wait_for(coro, int((server or {}).get("timeout") or CALL_TIMEOUT) + 15))


def list_tools(server: dict, fresh: bool = False) -> list[dict]:
    key = server["name"]
    with _lock:
        hit = _cache.get(key)
    if hit and not fresh and time.time() - hit[0] < LIST_TTL:
        return hit[1]

    async def go(s):
        res = await s.list_tools()
        return [{"name": t.name, "description": t.description or "", "inputSchema": t.inputSchema or {}}
                for t in res.tools]
    tools = _run(_with_session(server, go))
    with _lock:
        _cache[key] = (time.time(), tools)
    return tools


def call_tool(server: dict, tool: str, args: dict) -> str:
    async def go(s):
        res = await s.call_tool(tool, args)
        parts = []
        for c in res.content:
            if getattr(c, "type", "") == "text":
                parts.append(c.text)
            else:
                parts.append(f"[{getattr(c, 'type', 'content')} omitted]")
        if getattr(res, "structuredContent", None) and not parts:
            parts.append(json.dumps(res.structuredContent))
        return ("error: " if res.isError else "") + "\n".join(parts)
    return _run(_with_session(server, go), server)


# ------------------------------------------------------------------ agents

def tool_specs_for(agent: str) -> tuple[list[dict], dict[str, tuple[dict, str]]]:
    """OpenAI function specs for an API agent's granted servers, and a name map
    {function_name: (server, tool)}. Servers that fail to answer are skipped."""
    specs, names = [], {}
    for srv in servers_for(agent):
        try:
            tools = list_tools(srv)
        except Exception:
            continue
        for t in tools:
            fname = f"mcp__{safe(srv['name'])}__{safe(t['name'])}"[:64]
            names[fname] = (srv, t["name"])
            schema = t["inputSchema"] if t["inputSchema"].get("type") == "object" else {"type": "object", "properties": {}}
            specs.append({"type": "function", "function": {
                "name": fname, "description": f"[MCP {srv['name']}] {t['description']}"[:1000],
                "parameters": schema}})
    return specs, names


def claude_config(agent: str, data_dir: str) -> tuple[str | None, list[str]]:
    """Write the agent's --mcp-config file; -> (path or None, allowed-tool rules)."""
    granted = servers_for(agent)
    if not granted:
        return None, []
    cfg = {"mcpServers": {}}
    for s in granted:
        if s.get("transport") == "stdio":
            cfg["mcpServers"][safe(s["name"])] = {"command": s["command"], "args": s.get("args", []),
                                                  "env": s.get("env", {})}
        else:
            cfg["mcpServers"][safe(s["name"])] = {"type": "http", "url": s["url"], "headers": _headers(s)}
    d = Path(data_dir) / "mcp"
    d.mkdir(mode=0o700, exist_ok=True)
    path = d / f"{safe(agent)}.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(cfg, f)
    return str(path), [f"mcp__{safe(s['name'])}" for s in granted]
