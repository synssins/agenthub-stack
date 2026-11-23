"""LLM servers (Settings > LLMs): each server is defined once, with a default model, and API
agents link to one by id ("llm") with "llm_model" = "" for the server's default or a model id.

  data/llm_servers.json   [{id, name, base_url, api_format, api_key, default_model, default_auto}]

Keys stay in that file (mode 600) and never go back to the browser. config.toml API agents
run as wrapper_api.py processes and are not stored here: they are listed read-only as
"cfg-<name>" with their key read from the config at call time.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import urllib.parse
from pathlib import Path

data_dir = Path("./data")   # set by AgentManager
_lock = threading.RLock()   # every read-modify-write of the file
FILE = "llm_servers.json"
FORMATS = ("openai", "anthropic")
# The agent form's old provider presets (static/agents-admin.js): names for migrated servers
PRESETS = {
    "http://127.0.0.1:4000/v1": "Nexus router (local)",
    "https://openrouter.ai/api/v1": "OpenRouter",
    "https://generativelanguage.googleapis.com/v1beta/openai": "Google Gemini",
    "https://api.anthropic.com/v1": "Anthropic",
}


class Unreadable(Exception):
    """llm_servers.json exists but can't be read: nothing may overwrite it or drop links."""


UNREADABLE = "LLM servers file (data/llm_servers.json) is unreadable; fix or restore it"


def load() -> list[dict]:
    p = data_dir / FILE
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text("utf-8"))
    except (OSError, ValueError) as exc:
        raise Unreadable(str(exc)) from exc
    if not isinstance(data, list) or not all(isinstance(s, dict) and s.get("id") and s.get("base_url") for s in data):
        raise Unreadable("expected a list of servers with id and base_url")
    return data


def save(servers: list[dict]):
    p = data_dir / FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(servers, f, indent=2)
    os.replace(tmp, p)


def config_entries() -> list[dict]:
    """Read-only entries for config.toml / config.local.toml API agents."""
    try:
        import config_loader
        agents = (config_loader.load_config() or {}).get("agents") or {}
    except Exception:
        return []
    out = []
    for name, cfg in agents.items():
        if not isinstance(cfg, dict) or cfg.get("type") != "api" or not cfg.get("base_url"):
            continue
        key = cfg.get("api_key") or (os.environ.get(cfg["api_key_env"], "") if cfg.get("api_key_env") else "")
        out.append({"id": f"cfg-{name}", "name": cfg.get("label") or name, "base_url": cfg["base_url"],
                    "api_format": cfg.get("api_format", "openai"), "api_key": key,
                    "default_model": cfg.get("model", ""), "readonly": True})
    return out


def get(server_id: str) -> dict | None:
    """Raises Unreadable when the servers file is damaged."""
    return next((s for s in load() + config_entries() if s.get("id") == server_id), None)


def public(server: dict) -> dict:
    out = {k: v for k, v in server.items() if k != "api_key"}
    out["has_api_key"] = bool(server.get("api_key"))
    out["readonly"] = bool(server.get("readonly"))
    return out


def upsert(entry: dict) -> str | None:
    """Add or edit a server; a blank api_key keeps the stored one (unless the host changed). -> error or None."""
    with _lock:
        return _upsert(entry)


def _upsert(entry: dict) -> str | None:
    sid = entry.get("id") or ""
    if sid.startswith("cfg-"):
        return "servers from config.toml are read-only; edit the config file"
    try:
        servers = load()
    except Unreadable:
        return UNREADABLE
    old = next((s for s in servers if s["id"] == sid), None) if sid else None
    new = dict(old or {"id": sid or "srv-" + secrets.token_hex(4)})
    for k in ("name", "base_url", "api_format", "default_model"):
        if k in entry:
            new[k] = str(entry[k] or "").strip()
    if entry.get("api_key"):
        new["api_key"] = str(entry["api_key"]).strip()
    elif old and _host(old.get("base_url", "")) != _host(new.get("base_url", "")):
        new.pop("api_key", None)  # never send the old host's key to a new host
    if "default_model" in entry:
        new["default_auto"] = False  # the owner's choice: no longer follows a one-model server
    new.setdefault("api_format", "openai")
    if not new.get("base_url", "").startswith(("http://", "https://")):
        return "base_url must start with http:// or https://"
    if new["api_format"] not in FORMATS:
        return f"api_format must be one of {FORMATS}"
    new["name"] = new.get("name") or _host(new["base_url"])
    if old:
        servers[servers.index(old)] = new
    else:
        servers.append(new)
    save(servers)
    return None


def remove(server_id: str, linked: list[str]) -> str | None:
    with _lock:
        return _remove(server_id, linked)


def _remove(server_id: str, linked: list[str]) -> str | None:
    if server_id.startswith("cfg-"):
        return "servers from config.toml are read-only; edit the config file"
    if linked:
        return f"{len(linked)} agents linked; unlink them first"
    try:
        save([s for s in load() if s["id"] != server_id])
    except Unreadable:
        return UNREADABLE
    return None


def note_models(server_id: str, models: list[str]):
    """After listing a server's models: one model and no default of the owner's -> that's the default."""
    with _lock:
        _note_models(server_id, models)


def _note_models(server_id: str, models: list[str]):
    try:
        servers = load()
    except Unreadable:
        return
    s = next((x for x in servers if x["id"] == server_id), None)
    if not s or len(models) != 1 or (s.get("default_model") and not s.get("default_auto")):
        return
    if s.get("default_model") != models[0] or not s.get("default_auto"):
        s["default_model"], s["default_auto"] = models[0], True
        save(servers)


def resolve(spec: dict) -> dict:
    """{base_url, api_format, api_key, model} for a linked agent, or {llm_error}. No network."""
    try:
        s = get(spec.get("llm") or "")
    except Unreadable:
        return {"llm_error": UNREADABLE}
    if not s:
        return {"llm_error": "needs an LLM server"}
    model = spec.get("llm_model") or s.get("default_model") or ""
    if not model:
        return {"llm_error": f"{s.get('name') or s['id']} has no default model; set one in Settings > LLMs"}
    return {"base_url": s["base_url"], "api_format": s.get("api_format", "openai"),
            "api_key": s.get("api_key", ""), "model": model}


def migrate(specs: dict[str, dict]) -> bool:
    """One server per distinct (base_url, api_format, api_key) of API agents without "llm";
    each agent is linked with its current model pinned. -> True when anything changed."""
    with _lock:
        return _migrate(specs)


def _migrate(specs: dict[str, dict]) -> bool:
    todo = [s for s in specs.values() if s.get("kind") == "api" and not s.get("llm") and s.get("base_url")]
    if not todo:
        return False
    try:
        servers = load()
    except Unreadable:
        return False  # keep the agents' own endpoint fields until the file is fixed
    for spec in todo:
        key = (spec["base_url"].rstrip("/"), spec.get("api_format", "openai"), spec.get("api_key", ""))
        srv = next((s for s in servers if (s["base_url"].rstrip("/"), s.get("api_format", "openai"),
                                           s.get("api_key", "")) == key), None)
        if not srv:
            base = PRESETS.get(key[0]) or _host(key[0])
            taken, name, n = {s.get("name") for s in servers}, base, 2
            while name in taken:  # two keys for one provider: "OpenRouter", "OpenRouter 2"
                name, n = f"{base} {n}", n + 1
            srv = {"id": "srv-" + secrets.token_hex(4), "name": name,
                   "base_url": spec["base_url"], "api_format": key[1], "default_model": ""}
            if key[2]:
                srv["api_key"] = key[2]
            servers.append(srv)
        spec["llm"], spec["llm_model"] = srv["id"], spec.get("model", "")
        for k in ("base_url", "api_format", "api_key"):
            spec.pop(k, None)
    save(servers)
    return True


def _host(url: str) -> str:
    return urllib.parse.urlsplit(url).netloc or url
