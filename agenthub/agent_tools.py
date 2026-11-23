"""Tools and skills for API agents (LLMs reached over HTTP, e.g. Qwen on Strata).

CLI agents (Claude Code, agy) bring their own tools. API agents get this small
set through OpenAI-style function calling, run by the server on their behalf:

  load_skill   read an installed skill's instructions (index is in the prompt)
  read_file    read a text file            (every permission level)
  list_dir     list a directory            (every permission level)
  react        add an emoji reaction to a chat message (every level; wakes nobody)
  write_file   create/overwrite a file     (edit, full)
  run_command  run a command               (full: any shell command;
                                            otherwise only `allowed_commands`)
  artifact_read / artifact_update   shared code artifacts of the agent's channels
  rulebook     open a rulebook by name or keyword (every level)
  remember / forget_memory   the agent's own long-term notes (memory.py), when memory is on

Skills are discovered from ~/.agents/skills and ~/.claude/skills, so anything
installed there shows up for every agent without config.

Security: tools run as the server's user. File tools refuse AgentHub's own data
folder (secrets, keys). With `full`, an agent can run anything this user can.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

SKILL_DIRS = [Path.home() / ".agents/skills", Path.home() / ".claude/skills"]
MAX_OUT = 12000
CMD_TIMEOUT = 600  # test suites and builds can take minutes


def skills() -> dict[str, dict]:
    """{name: {"description", "path"}} from SKILL.md front matter."""
    found = {}
    for d in SKILL_DIRS:
        for f in sorted(d.glob("*/SKILL.md")) if d.is_dir() else []:
            name, desc = f.parent.name, ""
            try:
                text = f.read_text("utf-8", errors="replace")
            except OSError:
                continue
            if text.startswith("---"):
                head = text.split("---", 2)[1]
                lines = head.splitlines()
                for i, line in enumerate(lines):
                    if line.startswith("name:"):
                        name = line[5:].strip() or name
                    if line.startswith("description:"):
                        val = line[12:].strip()
                        if val in (">", ">-", "|", "|-", ""):  # folded block: gather indented lines
                            val = " ".join(l.strip() for l in lines[i + 1:] if l.startswith((" ", "\t")))
                        desc = val
            found.setdefault(name, {"description": desc[:300], "path": str(f)})
    return found


_searxng_cache: list = []


def _searxng_url() -> str:
    """[search] searxng = "http://host:port" in config.toml / config.local.toml; "" = no web_search."""
    if not _searxng_cache:
        try:
            import config_loader
            url = str(((config_loader.load_config() or {}).get("search") or {}).get("searxng") or "")
        except Exception:
            url = ""
        _searxng_cache.append(url.rstrip("/"))
    return _searxng_cache[0]


CHAT_TOOLS = {"artifact_read", "artifact_update", "react", "remember", "forget_memory", "game"}   # permission "none"


def tool_specs(perm: str, allowed: list[str], memory: bool = False) -> list[dict]:
    def fn(name, desc, props, required):
        return {"type": "function", "function": {"name": name, "description": desc, "parameters": {
            "type": "object", "properties": props, "required": required}}}
    s = {"type": "string"}
    specs = [
        fn("load_skill", "Read the full instructions of an installed skill before using it.",
           {"name": s}, ["name"]),
        fn("read_file", "Read a text file. Relative paths start in your working directory.",
           {"path": s, "offset": {"type": "integer"}, "limit": {"type": "integer"}}, ["path"]),
        fn("list_dir", "List the entries of a directory.", {"path": s}, ["path"]),
        fn("artifact_read", "Read a shared code artifact from the chat (the [artifact <id> ...] items). "
           "Latest version unless you give one.", {"id": s, "version": {"type": "integer"}}, ["id"]),
        fn("artifact_update", "Post a new version of a shared artifact: the complete new text. "
           "The channel sees the update.", {"id": s, "content": s, "note": s}, ["id", "content"]),
        fn("rulebook", "Open a rulebook (a guide for a kind of work) by name or keyword; an empty query "
           "lists them. Use it only when the user asks for a rulebook.", {"query": s}, []),
        fn("react", "Add an emoji reaction to a chat message (#id from the transcript). Use it instead "
           "of replying when a reaction says enough. Reactions never wake anyone.",
           {"message_id": {"type": "integer"}, "emoji": s}, ["message_id", "emoji"]),
    ]
    if _searxng_url():
        specs.append(fn("web_search", "Search the web (the owner's SearXNG). kind: web (default), images or news. "
                        "Results come back as Markdown: show the owner the useful ones as links "
                        "[title](url), and images inline as ![caption](image url).",
                        {"query": s, "kind": {"type": "string", "enum": ["web", "images", "news"]},
                         "count": {"type": "integer"}}, ["query"]))
    if memory:
        specs.append(fn("remember", "Save one short note to your long-term memory (a preference, decision or "
                        "fact worth keeping across conversations).", {"note": s}, ["note"]))
        specs.append(fn("forget_memory", "Remove the notes in your long-term memory that contain this text.",
                        {"text": s}, ["text"]))
    if perm in ("edit", "full"):
        specs.append(fn("write_file", "Create or overwrite a text file.", {"path": s, "content": s},
                        ["path", "content"]))
    if perm == "full" or allowed:
        which = "any shell command" if perm == "full" else "only these programs: " + ", ".join(allowed)
        specs.append(fn("run_command", f"Run a command ({which}). To feed text to a program, put it in stdin "
                        "(instead of echo ... | program). "
                        "Returns exit code and output.", {"command": s, "stdin": s}, ["command"]))
    if perm == "none":   # chat only: no files, skills, rulebooks, web or commands, whatever else is set
        specs = [x for x in specs if x["function"]["name"] in CHAT_TOOLS]
    return specs


class ToolRunner:
    def __init__(self, cwd: str, perm: str, allowed: list[str], env: dict, protect: str,
                 agent: str = "", channel: str = "general"):
        self.agent, self.channel = agent, channel  # who calls artifact_* (channel access check)
        self.cwd = Path(os.path.expanduser(cwd or "~"))
        self.perm, self.allowed, self.env = perm, allowed, env
        self.protect = Path(protect).resolve()
        self.react = None   # callable(message_id, emoji) -> str, set by the agent runner
        self.memory_size = 0  # bytes of long-term memory (memory.py); set by the agent runner
        self.proc = None    # running run_command process (killed on /stop)

    def _path(self, p: str) -> Path:
        path = (self.cwd / os.path.expanduser(p)).resolve()
        if path == self.protect or self.protect in path.parents:
            raise PermissionError("AgentHub's data folder is off limits")
        return path

    def run(self, name: str, args: dict) -> str:
        if self.perm == "none" and name not in CHAT_TOOLS:
            return f"error: no tool {name} (this agent has no tools beyond chat)"
        try:
            out = getattr(self, "t_" + name)(**args)
        except AttributeError:
            out = f"error: unknown or unavailable tool {name}"
        except TypeError as exc:
            out = f"error: bad arguments for {name}: {exc}"
        except Exception as exc:  # report to the model; it can try something else
            out = f"error: {exc}"
        return out if len(out) <= MAX_OUT else out[:MAX_OUT] + f"\n…[truncated, {len(out)} chars]"

    def t_web_search(self, query: str, kind: str = "web", count: int = 8) -> str:
        import json
        import urllib.parse
        import urllib.request
        base = _searxng_url()
        if not base:
            return "error: web search is not configured"
        kind = kind if kind in ("web", "images", "news") else "web"
        count = max(1, min(int(count or 8), 15))
        params = {"q": query, "format": "json"}
        if kind != "web":
            params["categories"] = kind
        req = urllib.request.Request(f"{base}/search?{urllib.parse.urlencode(params)}",
                                     headers={"Accept": "application/json", "User-Agent": "AgentHub"})
        with urllib.request.urlopen(req, timeout=20) as r:
            results = json.load(r).get("results") or []
        md = lambda t: str(t or "").replace("[", "(").replace("]", ")").replace("\n", " ").strip()
        lines = []
        for x in results:
            if len(lines) >= count:
                break
            if kind == "images":
                img = x.get("img_src") or x.get("thumbnail_src")
                if img:
                    lines.append(f"{len(lines) + 1}. ![{md(x.get('title')) or 'image'}]({img}) (from {x.get('url', '')})")
            elif x.get("url"):
                snippet = md(x.get("content"))[:240]
                lines.append(f"{len(lines) + 1}. [{md(x.get('title')) or x['url']}]({x['url']})"
                             + (f" — {snippet}" if snippet else ""))
        return "\n".join(lines) or f"no {kind} results for {query!r}"

    def t_diary_write(self, entry: str) -> str:
        import emotion_engine
        return emotion_engine.diary_write(self.agent, entry)

    def t_diary_read(self, day: str = "", about: str = "") -> str:
        import emotion_engine
        return emotion_engine.diary_read(self.agent, day, about)

    def t_game(self, action: str, game: str = "", players=None, player: str = "", points: int = 1, level: int = 0,
               category: str = "") -> str:
        import games
        return games.play(self.agent, self.channel, action, game, players, player, points, level, category)

    def t_camera_look(self, camera: str, picture: bool = False) -> str:
        import senses
        return senses.camera_look(self.agent, camera, bool(picture))

    def t_react(self, message_id: int, emoji: str) -> str:
        return self.react(int(message_id), str(emoji)) if self.react else "error: reactions are not available"

    def t_rulebook(self, query: str = "") -> str:
        import rulebooks
        return rulebooks.lookup(self.agent, query, self.channel)

    def t_remember(self, note: str) -> str:
        import memory
        return memory.add(self.agent, note, self.memory_size)

    def t_forget_memory(self, text: str) -> str:
        import memory
        return memory.forget(self.agent, text)

    def t_load_skill(self, name: str) -> str:
        sk = skills().get(name)
        if not sk:
            return "error: no such skill. Installed: " + ", ".join(sorted(skills()))
        return Path(sk["path"]).read_text("utf-8", errors="replace")

    def t_read_file(self, path: str, offset: int = 0, limit: int = 400) -> str:
        lines = self._path(path).read_text("utf-8", errors="replace").splitlines()
        offset, end, size = max(0, int(offset)), min(len(lines), max(0, int(offset)) + int(limit)), 0
        stop = offset
        # numbered like cat -n (L1 = first line), so agents cite real line numbers
        numbered = [f"{i + 1:>5}\t{lines[i]}" for i in range(offset, end)]
        while stop < end and size + len(numbered[stop - offset]) + 1 <= MAX_OUT - 300:  # whole lines only
            size += len(numbered[stop - offset]) + 1
            stop += 1
        stop = max(stop, min(offset + 1, end))  # always return at least one line
        out = "\n".join(numbered[:stop - offset])
        if stop < len(lines):  # say exactly where to continue, so nobody guesses the rest
            out += (f"\n…[shown L{offset + 1}-L{stop} of {len(lines)}; the file continues. "
                    f"Call read_file with offset={stop} to read from L{stop + 1}.]")
        return out

    def t_list_dir(self, path: str = ".") -> str:
        p = self._path(path)
        return "\n".join(e.name + ("/" if e.is_dir() else "") for e in sorted(p.iterdir()))

    def t_artifact_read(self, id: str, version: int | None = None) -> str:
        # shortcut: run() cuts output at MAX_OUT chars, so very large artifacts arrive truncated; add paging if that bites
        import artifacts
        return artifacts.read_for(self.agent, id, version)

    def t_artifact_update(self, id: str, content: str, note: str = "") -> str:
        import artifacts
        return artifacts.update_for(self.agent, id, content, note)

    def t_write_file(self, path: str, content: str) -> str:
        if self.perm not in ("edit", "full"):
            return "error: your permissions do not allow writing files"
        p = self._path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, "utf-8")
        return f"wrote {len(content)} chars to {p}"

    def t_run_command(self, command: str, stdin: str = "") -> str:
        if self.perm == "full":
            argv = ["bash", "-lc", command]
        else:
            argv = shlex.split(command)
            if not argv or Path(argv[0]).name not in self.allowed:
                return "error: only these programs are allowed: " + (", ".join(self.allowed) or "none")
            if any(t in command for t in ("|", ";", "&&", "||", "`", "$(", ">", "<")):
                return ("error: pipes and redirects are not allowed at your permission level. Put the text in "
                        "the stdin argument instead, e.g. {\"command\": \"slop-chop fix\", \"stdin\": \"text to clean\"} "
                        "rather than echo \"text\" | slop-chop fix")
        # own process group so /stop (agent_runner.kill_group) also kills what the command started
        self.proc = proc = subprocess.Popen(argv, cwd=self.cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=subprocess.PIPE, text=True, env=self.env,
                                            start_new_session=True)
        try:
            out, err = proc.communicate(stdin or None, timeout=CMD_TIMEOUT)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise
        finally:
            self.proc = None
        return f"exit {proc.returncode}\n{out}" + (f"\n[stderr]\n{err}" if err else "")
