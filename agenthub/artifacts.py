"""Shared code artifacts.

A fenced code block of MIN_LINES or more lines in a chat message is stored here
with version history, and the stored message keeps a short [artifact:<id>] token
(transcripts sent to agents stay short; the web UI renders the token as a card).

Layout: <data_dir>/artifacts/<id>/meta.json + v1.txt, v2.txt, ...
Access: the owner (browser session) and the participants of the artifact's
channel. Agents read/update through agent_tools (API agents), mcp_bridge
(chat_artifact_read/update) or HTTP GET/POST /api/artifacts/<id> (Bearer token).
"""

import json
import os
import re
import secrets
import threading
import time
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()
hub = None       # app module (store, agent_manager, room_settings, _resolve_authenticated_agent)
store = None     # ArtifactStore

MIN_LINES = 8
MAX_BYTES = 512 * 1024
ID_RE = re.compile(r"[A-Za-z0-9_-]{6,24}")
TOKEN_RE = re.compile(r"\[artifact:([A-Za-z0-9_-]{6,24})\]")
# A fence at line start, its info string, the body, and a closing fence on its own line.
FENCE_RE = re.compile(r"^```([^\n`]*)\n(.*?)\n```[ \t]*$", re.S | re.M)
SKIP_LANGS = {"session"}  # ```session drafts are parsed from the message text by app.py

EXTENSIONS = {
    "powershell": "ps1", "ps1": "ps1", "pwsh": "ps1", "python": "py", "py": "py",
    "bash": "sh", "sh": "sh", "shell": "sh", "zsh": "sh", "javascript": "js", "js": "js",
    "typescript": "ts", "ts": "ts", "json": "json", "yaml": "yaml", "yml": "yaml",
    "html": "html", "css": "css", "sql": "sql", "go": "go", "rust": "rs", "rs": "rs",
    "c": "c", "cpp": "cpp", "c++": "cpp", "csharp": "cs", "cs": "cs", "java": "java",
    "xml": "xml", "toml": "toml", "ini": "ini", "markdown": "md", "md": "md",
    "batch": "bat", "bat": "bat", "cmd": "bat",
}
COMMENT_RE = re.compile(r"^\s*(?:#!?|//|--|;+|<#|/\*+|REM\s|::)\s*(.+?)\s*(?:#>|\*/)?\s*$", re.I)


def filename_for(title: str, lang: str) -> str:
    """Download name: the title if it already looks like a filename, else slug + extension."""
    lang = (lang or "").lower()
    if lang == "dockerfile":
        return "Dockerfile"
    ext = EXTENSIONS.get(lang, "txt")
    if re.fullmatch(r"[\w.-]+\.\w{1,10}", title or ""):
        return title
    slug = re.sub(r"[^A-Za-z0-9]+", "-", title or "").strip("-")[:60] or "artifact"
    return f"{slug}.{ext}"


def _title(lang: str, body: str) -> str:
    """First comment line of the code, else '<lang> snippet'."""
    for line in body.splitlines()[:5]:
        if line.lower().startswith(("#!", "#requires")):
            continue
        m = COMMENT_RE.match(line)
        if m:
            if re.search(r"[A-Za-z]", m.group(1)):
                return m.group(1).strip(" .#")[:70]
            continue  # a ruler like "# =====": look at the next line
        if line.strip():
            break
    return f"{lang} snippet" if lang else "Code snippet"


class ArtifactStore:
    def __init__(self, root):
        self.root = Path(root)
        self._lock = threading.Lock()

    def _dir(self, aid: str) -> Path | None:
        return self.root / aid if ID_RE.fullmatch(aid or "") else None

    def get(self, aid: str) -> dict | None:
        d = self._dir(aid)
        try:
            return json.loads((d / "meta.json").read_text("utf-8")) if d else None
        except (OSError, ValueError):
            return None

    def content(self, aid: str, version: int | None = None) -> str | None:
        meta = self.get(aid)
        if not meta:
            return None
        n = int(version or len(meta["versions"]))
        p = self._dir(aid) / f"v{n}.txt"
        return p.read_text("utf-8") if 1 <= n <= len(meta["versions"]) and p.exists() else None

    def _write_meta(self, meta: dict):
        p = self._dir(meta["id"]) / "meta.json"
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(meta, indent=2, ensure_ascii=False), "utf-8")
        os.replace(tmp, p)

    def create(self, content: str, lang: str, title: str, channel: str, author: str,
               filename: str = "") -> dict:
        with self._lock:
            while True:
                aid = secrets.token_urlsafe(6)  # 8 URL-safe chars
                if not (self.root / aid).exists():
                    break
            (self.root / aid).mkdir(parents=True)
            (self.root / aid / "v1.txt").write_text(content, "utf-8")
            now = time.time()
            meta = {"id": aid, "title": title, "language": lang,
                    "filename": filename or filename_for(title, lang), "channel": channel,
                    "created_by": author, "created": now,
                    "versions": [{"n": 1, "author": author, "time": now, "lines": len(content.splitlines())}]}
            self._write_meta(meta)
        return meta

    def update(self, aid: str, content: str, author: str, note: str = "") -> dict | None:
        with self._lock:
            meta = self.get(aid)
            if not meta:
                return None
            n = len(meta["versions"]) + 1
            (self._dir(aid) / f"v{n}.txt").write_text(content, "utf-8")
            v = {"n": n, "author": author, "time": time.time(), "lines": len(content.splitlines())}
            if note:
                v["note"] = note[:200]
            meta["versions"].append(v)
            self._write_meta(meta)
        return meta

    def extract(self, text: str, channel: str = "general", sender: str = "") -> str:
        """Store each fenced block of MIN_LINES+ lines; return the text with tokens in their place."""
        if "```" not in (text or "") or sender == "system":
            return text

        def repl(m):
            info, body = m.group(1).strip(), m.group(2)
            lang, _, fname = info.partition(":")
            lang = (lang.split() or [""])[0].lower()
            fname = fname.strip()
            if lang in SKIP_LANGS or len(body.splitlines()) < MIN_LINES or len(body.encode()) > MAX_BYTES:
                return m.group(0)
            meta = self.create(body, lang, fname or _title(lang, body), channel, sender,
                               filename=fname if re.fullmatch(r"[\w.-]+", fname) else "")
            return f"[artifact:{meta['id']}]"
        return FENCE_RE.sub(repl, text)


def setup(app_module, cfg: dict):
    """Create the store and hook it into MessageStore.add (every message path goes through it)."""
    global hub, store
    hub = app_module
    store = ArtifactStore(Path(cfg.get("server", {}).get("data_dir", "./data")) / "artifacts")
    hub.store.transform = store.extract


def summary(meta: dict) -> dict:
    v = meta["versions"][-1]
    return {**meta, "version": v["n"], "lines": v["lines"]}


def render_for_agents(text: str, how: str = "artifact_read", inline: bool = False) -> str:
    """[artifact:<id>] -> a one-line description an agent can act on. inline=True (agents
    without artifact tools) also gives the current text as a fenced block."""
    if not store or "[artifact:" not in (text or ""):
        return text

    def sub(m):
        meta = store.get(m.group(1))
        if not meta:
            return m.group(0)
        v = meta["versions"][-1]
        head = (f'[artifact {meta["id"]} "{meta["title"]}" ({meta["language"] or "text"}, '
                f'{v["lines"]} lines, v{v["n"]})')
        if not inline:
            return f"{head} - read it with {how}]"
        return f"{head}, current text:]\n```{meta['language']}\n{store.content(meta['id'])}\n```"
    return TOKEN_RE.sub(sub, text)


# The description render_for_agents gives an agent; an agent that copies it into a reply means the artifact
LABEL_RE = re.compile(r'\[artifact ([A-Za-z0-9_-]{6,24}) "[^"\n]*" \([^)\n]*\)(?: - read it with \w+|, current text:)?\]')


def relink(text: str) -> str:
    """A copied artifact description in an agent's reply -> the [artifact:<id>] token it stands for."""
    if not store or "[artifact " not in (text or ""):
        return text
    return LABEL_RE.sub(lambda m: f"[artifact:{m.group(1)}]" if store.get(m.group(1)) else m.group(0), text)


def render_plain(text: str) -> str:
    """[artifact:<id>] -> a readable label for plain-text clients (IRC)."""
    if not store or "[artifact:" not in (text or ""):
        return text

    def sub(m):
        meta = store.get(m.group(1))
        if not meta:
            return m.group(0)
        v = meta["versions"][-1]
        return (f'[artifact "{meta["title"]}" ({meta["language"] or "text"}, {v["lines"]} lines, '
                f'v{v["n"]}) - open it in the web chat]')
    return TOKEN_RE.sub(sub, text)


def can_access(name: str, meta: dict) -> bool:
    """Participants of the artifact's channel only (the owner is checked by the caller)."""
    am = getattr(hub, "agent_manager", None)
    return am is None or am.in_channel(name, meta.get("channel", "general"))


def read_for(name: str, aid: str, version: int | None = None) -> str:
    """Tool result text for an agent (agent_tools / mcp_bridge)."""
    meta = store.get(aid) if store else None
    if not meta or not can_access(name, meta):
        return f"error: no artifact {aid} in your channels"
    text = store.content(aid, version)
    if text is None:
        return f"error: {aid} has versions 1-{len(meta['versions'])}"
    n = int(version or len(meta["versions"]))
    return (f'artifact {aid} "{meta["title"]}" ({meta["language"] or "text"}), version {n} of '
            f'{len(meta["versions"])}, channel #{meta["channel"]}:\n{text}')


def update(name: str, aid: str, content: str, note: str = "") -> tuple[dict | None, str]:
    """New version by `name` (access already checked) + a line in the channel. -> (meta, error)."""
    if not isinstance(content, str) or not content.strip():
        return None, "content is required"
    if len(content.encode()) > MAX_BYTES:
        return None, f"content larger than {MAX_BYTES // 1024} KB"
    meta = store.update(aid, content.strip("\n"), name, str(note or ""))
    if not meta:
        return None, f"no artifact {aid}"
    n = meta["versions"][-1]["n"]
    line = f"@{name} updated {meta['title']} to v{n}" + (f": {note.strip()[:200]}" if note and note.strip() else "")
    # The metadata rides the normal message broadcast; open panels refresh from it.
    hub.store.add("system", line, msg_type="system", channel=meta["channel"],
                  metadata={"artifact": summary(meta)})
    return meta, ""


def update_for(name: str, aid: str, content: str, note: str = "") -> str:
    meta = store.get(aid) if store else None
    if not meta or not can_access(name, meta):
        return f"error: no artifact {aid} in your channels"
    meta, err = update(name, aid, content, note)
    return f"error: {err}" if err else f"{aid} is now v{len(meta['versions'])} ({meta['versions'][-1]['lines']} lines)"


# ------------------------------------------------------------------ HTTP

def _caller(request: Request) -> tuple[str, bool]:
    """(name, is_owner). The security middleware already accepted a Bearer agent token
    or the browser session token; a resolvable Bearer token means an agent."""
    inst = hub._resolve_authenticated_agent(request)
    if inst:
        return inst["name"], False
    return hub.room_settings.get("username", "user"), True


def _version(raw) -> int | None:
    try:
        return int(raw) if raw not in (None, "") else None
    except (TypeError, ValueError):
        return -1


@router.get("/api/artifacts/{aid}")
async def artifact_get(aid: str, request: Request, version: str = ""):
    name, owner = _caller(request)
    meta = store.get(aid)
    if not meta or not (owner or can_access(name, meta)):
        return JSONResponse({"error": "not found"}, status_code=404)
    n = _version(version)
    content = store.content(aid, n)
    if content is None:
        return JSONResponse({"error": "no such version"}, status_code=404)
    return {**summary(meta), "version": n or len(meta["versions"]), "content": content}


@router.post("/api/artifacts/{aid}")
async def artifact_post(aid: str, request: Request):
    name, owner = _caller(request)
    meta = store.get(aid)
    if not meta or not (owner or can_access(name, meta)):
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        return JSONResponse({"error": "JSON body {content, note?} required"}, status_code=400)
    meta, err = update(name, aid, body.get("content"), str(body.get("note") or ""))
    if err:
        return JSONResponse({"error": err}, status_code=400)
    return summary(meta)
