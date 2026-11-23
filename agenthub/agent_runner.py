"""Room agents managed from the web UI and run inside the server.

Two kinds:
  api    - an LLM endpoint (OpenAI-compatible or Anthropic). Each turn sends
           the last `context_messages` of the channel.
  claude / agy - a coding-agent CLI run headless (`-p`) per turn, resuming its
           own session, so it keeps its own memory and compacts its own
           context; each turn sends only the messages it hasn't seen yet.

Permissions for CLI agents (`permissions`, switchable any time, applies next turn):
  read  - read/search files only
  edit  - may also edit files
  full  - no restrictions (--dangerously-skip-permissions): anyone who can post
          in the room can make this agent run commands on this machine.

Definitions live in data/managed_agents.json (0600: it holds provider API keys;
they are never sent back to the browser).
"""

from __future__ import annotations  # AgentManager.list shadows list[...] in annotations

import asyncio
import base64
import json
import logging
import re
import os
import queue
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

log = logging.getLogger(__name__)

KINDS = ("api", "claude", "agy")
PERMISSIONS = ("none", "read", "edit", "full")   # none: API agents only, chat tools only
NO_REPLY = "(no reply)"
ACT_LIVE = 4000    # chars per live activity field
ACT_KEEP = 60      # steps saved with a reply (metadata.activity)
ACT_SAVED = {"text": 1500, "input": 400, "out": 800}
CLI_TIMEOUT = 30 * 60      # Claude Code: killed after this long with no output (agy: in all)
CLI_MAX = 8 * 3600         # Claude Code: hard cap for one turn, background work included
API_TIMEOUT = 600          # local reasoning models can think for minutes; per-agent "timeout" overrides

PERSONA_RE = re.compile(r"\[persona\](.*?)\[/persona\]", re.S | re.I)
RESUME_NOTE = ("(Your previous turn here was cut off by a server restart before you could post. "
               "Check what you already did, such as files changed, commands run or commits, then "
               "carry on from there instead of starting over.)")
ATTACH_RE = re.compile(r"\[attach:\s*([^\]\n]+)\]", re.I)
MAX_ATTACH_BYTES = 25 * 1024 * 1024
MASK_HINT = ("(An image named masked-*.png carries the area to change as transparency: "
             "use an inpaint workflow with that file.)")
RENAME_RE = re.compile(r"\[rename\](.*?)\[/rename\]", re.S | re.I)
# New version of a shared artifact, for agents without the artifact_update tool
ARTIFACT_UPDATE_RE = re.compile(r"\[artifact-update\s+([A-Za-z0-9_-]{6,24})\]\n?(.*?)\[/artifact-update\]", re.S | re.I)
REMEMBER_RE = re.compile(r"\[remember\](.*?)\[/remember\]", re.S | re.I)   # long-term memory (memory.py)
FORGET_RE = re.compile(r"\[forget\](.*?)\[/forget\]", re.S | re.I)
REACT_RE = re.compile(r"\[react\s+#?(\d+)\s+([^\]\s]{1,16})\s*\]", re.I)  # [react #123 😂]
PERSONA_ABOUT = (
    "Your persona is your own lasting identity: personality, voice, quirks, how you work with "
    "others. It follows you across projects and context resets. ")
PERSONA_REWRITE = (
    "To set or rewrite it, put the "
    "full new text in your reply between [persona] and [/persona] (under 1200 characters). "
    "That block is saved and removed from the posted message. ")
PERSONA_HELP = (
    "To change your @handle / screen "
    "name (like a Discord or IRC nick, e.g. ScroogeMcDuck19), add [rename]NewName[/rename]: "
    "letters, digits, - or _, max 32, not already taken. It is shown as you type it; "
    "@mentions ignore case.")

ROOM_PROMPT = (
    "You are {name} in AgentHub, a shared chat room with other AI agents and one human "
    "(the owner). Messages addressed to you arrive as a transcript. Reply with only the text "
    "to post in the room: no name prefix, concise unless asked for detail. Mention another "
    "agent with @name to hand off work or ask them; use @all to ask everyone. Agents work at "
    "the same time: if the agent you mention is busy, your request waits in its queue and it "
    "picks it up when it finishes, so don't stall waiting for an answer. To show the owner an "
    "image or file (screenshot, render, document), put [attach: /absolute/path] in your reply; "
    "it is posted as an attachment they can open. Code blocks of 8+ lines become shared artifacts, "
    "shown as [artifact <id> ...]: read one with artifact_read (or its text is included), and post a "
    "new version with artifact_update or by putting [artifact-update <id>]full new text[/artifact-update] "
    "in your reply. When a reaction says enough, react instead of "
    "replying: put [react #id emoji] in your reply (e.g. [react #123 😂]); a reply with only "
    "reactions posts no message. Pick reactions that fit your persona. If you have nothing "
    "useful to add, reply exactly: " + NO_REPLY
)


def kill_group(proc):
    """SIGTERM a process started with start_new_session=True and everything it spawned;
    SIGKILL the group if it is still there 3 s later. No-op when it already exited."""
    import signal
    if not proc or proc.poll() is not None:
        return
    def sig(s):
        try:
            os.killpg(proc.pid, s)
        except (ProcessLookupError, PermissionError):
            pass
    sig(signal.SIGTERM)
    t = threading.Timer(3, lambda: proc.poll() is None and sig(signal.SIGKILL))
    t.daemon = True
    t.start()


def _which(cmd: str) -> str | None:
    """Find a CLI even when the service PATH lacks ~/.local/bin."""
    extra = os.pathsep.join([str(Path.home() / ".local/bin"), "/usr/local/bin"])
    return shutil.which(cmd, path=os.environ.get("PATH", "") + os.pathsep + extra)


def _post_json(url: str, payload: dict, headers: dict, timeout: int = 120) -> dict:
    req = urllib.request.Request(url, method="POST", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        # The body says why (bad model id, missing field, context too long...): surface it
        exc.msg = f"{exc.msg}: {exc.read()[:400].decode('utf-8', 'replace')}"
        raise


_NO_STREAM: set = set()  # agents whose provider refused streaming


class _BargeIn(Exception):
    """Raised from a stream callback: a new message for the agent arrived; drop this generation."""


def _post_stream(url: str, payload: dict, headers: dict, timeout: int, on_text,
                 on_reason=None) -> tuple[dict, dict]:
    """Streamed chat completion -> (assistant message, usage). on_text(text so far) runs per delta,
    on_reason(reasoning so far) per reasoning delta (reasoning_content / reasoning fields)."""
    req = urllib.request.Request(url, method="POST", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json", "Accept": "text/event-stream", **headers})
    text, calls, usage, reason = "", {}, {}, ""
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        exc.msg = f"{exc.msg}: {exc.read()[:400].decode('utf-8', 'replace')}"
        raise
    with resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            body = line[5:].strip()
            if body == "[DONE]":
                break
            try:
                chunk = json.loads(body)
            except ValueError:
                continue
            usage = chunk.get("usage") or usage
            for ch in chunk.get("choices") or []:
                d = ch.get("delta") or {}
                r = d.get("reasoning_content") or d.get("reasoning")
                if on_reason and isinstance(r, str) and r:
                    reason += r
                    on_reason(reason)
                if d.get("content"):
                    text += d["content"]
                    on_text(text)
                for tc in d.get("tool_calls") or []:
                    c = calls.setdefault(tc.get("index", len(calls)), {"id": "", "type": "function",
                                                                      "function": {"name": "", "arguments": ""}})
                    c["id"] = tc.get("id") or c["id"]
                    f = tc.get("function") or {}
                    c["function"]["name"] += f.get("name") or ""
                    c["function"]["arguments"] += f.get("arguments") or ""
    msg = {"role": "assistant", "content": text}
    if calls:
        msg["tool_calls"] = [calls[i] for i in sorted(calls)]
    return msg, usage


# Compaction (API agents): messages that leave the context window fold into a rolling memory
MEMORY_PROMPT = ("You keep the running memory of a group chat for the agent {name}. Merge the earlier "
                 "memory and the new messages into one updated memory: decisions, facts, open tasks, "
                 "who asked for what, and names, file paths and message ids worth keeping. Drop small "
                 "talk. Plain notes, at most 300 words. Reply with the memory only.")
MEMORY_INPUT = 24000       # chars of new transcript per compaction (the newest are kept)
MEMORY_MAX = 4000          # chars of memory kept
TOOL_BUDGET = 40000        # chars of tool output in one turn before older results are trimmed
STOP_NOTE = ("(Room note: the owner stopped your turn here. Drop the request you were working on; "
             "don't resume it unless someone asks again.)")
BARGE_NOTE = ("(New message(s) in the room while you were working. Read them now and adjust: change "
              "course, stop, or carry on as they ask.)")
CLI_BARGE_NOTE = ("(Your previous turn was interrupted after a tool step so you could read the new "
                  "message(s) below. Your work so far is kept: continue from there, taking them into account.)")
EFFORTS = ("none", "low", "medium", "high")
AGENT_EFFORTS = EFFORTS + ("xhigh", "max")  # per agent (spec "effort"); xhigh/max are Claude Code levels
CLI_EFFORTS = ("low", "medium", "high", "xhigh", "max")  # claude --effort
_effort_path: Path | None = None


def effort_for(channel: str) -> str | None:
    """Thinking level chosen in the chat for this channel (None: the model's default)."""
    try:
        return json.loads(_effort_path.read_text("utf-8")).get(channel) if _effort_path else None
    except (OSError, ValueError):
        return None


def effort_level(spec: dict, channel: str) -> str | None:
    """The agent's own thinking level if set, else the channel's Think selector."""
    own = spec.get("effort")
    return own if own in AGENT_EFFORTS else effort_for(channel)


def api_effort(level: str | None) -> str | None:
    """OpenAI-style reasoning_effort stops at high."""
    return "high" if level in ("xhigh", "max") else level


def set_effort(channel: str, effort: str | None):
    data = {}
    try:
        data = json.loads(_effort_path.read_text("utf-8"))
    except (OSError, ValueError):
        pass
    if effort in EFFORTS:
        data[channel] = effort
    else:
        data.pop(channel, None)
    _effort_path.write_text(json.dumps(data), "utf-8")


MAX_TOOL_STEPS = 40        # tool rounds per turn for API agents; per-agent "max_tool_steps" overrides
# With "require_tool" on, the first step must be a tool call. This one lets conversation be that call.
ANSWER_TOOL = {"type": "function", "function": {
    "name": "answer",
    "description": ("Reply to the person without doing or looking up anything. Only for conversation that needs "
                    "no action and no fresh information. Never use it to say something was done, switched, set "
                    "or checked: to do or check something, call the tool that does it first."),
    "parameters": {"type": "object", "properties": {"text": {"type": "string", "description": "The reply."}},
                   "required": ["text"]}}}
MAX_IMAGES = 3              # newest images sent inline to API agents per turn
MAX_IMAGE_BYTES = 5 * 1024 * 1024
SPILL_DIR = Path.home() / ".cache" / "agenthub" / "mcp-output"   # outside the data dir, so read_file works


def _spill(agent: str, tool: str, out: str) -> str:
    """Long MCP results: save the whole text and tell the agent where, instead of cutting silently."""
    import agent_tools
    if len(out) <= agent_tools.MAX_OUT:
        return out
    d = SPILL_DIR / re.sub(r"[^A-Za-z0-9_-]", "_", agent)
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = d / f"{time.strftime('%Y%m%d-%H%M%S')}-{re.sub(r'[^A-Za-z0-9_-]', '_', tool)}.txt"
    try:  # read_file pages by line; one-line JSON would only be cut again
        body = json.dumps(json.loads(out), indent=2, ensure_ascii=False)
    except ValueError:
        body = out
    path.write_text(body, encoding="utf-8")
    note = (f"\n…[result is {len(out)} chars; only the first part is shown. The complete raw result "
            f"is saved at {path}. Read it with read_file (use offset/limit to page).]")
    return out[:agent_tools.MAX_OUT - len(note)] + note


def _to_anthropic(content):
    """OpenAI-style content parts -> Anthropic blocks (text stays a string)."""
    if isinstance(content, str):
        return content
    out = []
    for p in content:
        if p.get("type") == "image_url":
            head, data = p["image_url"]["url"].split(",", 1)
            out.append({"type": "image", "source": {"type": "base64", "data": data,
                                                    "media_type": head[5:].split(";")[0]}})
        else:
            out.append({"type": "text", "text": p.get("text", "")})
    return out


def _timeout(spec: dict) -> int:
    try:
        return max(10, int(spec.get("timeout") or API_TIMEOUT))
    except (TypeError, ValueError):
        return API_TIMEOUT


def call_api(spec: dict, system: str, messages: list[dict]) -> str:
    return call_api_message(spec, system, messages).get("content") or ""


# Per-call metrics: one JSON line per model call (data/metrics.jsonl), read by /api/admin/metrics.
METRICS_PATH: Path | None = None   # set by AgentManager
_metrics_lock = threading.Lock()
_call_ctx = threading.local()      # .channel / .agent for the call being made on this thread


def _record(spec: dict, seconds: float, prompt: int, cached: int, written: int, out: int, cost):
    if METRICS_PATH is None:
        return
    row = {"ts": round(time.time(), 3), "agent": getattr(_call_ctx, "agent", None) or spec.get("name", ""),
           "channel": getattr(_call_ctx, "channel", ""), "model": spec.get("model", ""),
           "seconds": round(seconds, 3), "prompt_tokens": prompt, "cached_tokens": cached,
           "cache_write_tokens": written, "completion_tokens": out,
           "tokens_per_s": round(out / seconds, 1) if seconds > 0 else None, "cost": cost}
    turn = getattr(_call_ctx, "turn", None)
    if turn is not None:   # the running turn's totals: generation speed is tokens over the time of its calls
        turn["calls"] += 1
        turn["secs"] += seconds
        turn["out"] += out
        turn["prompt"], turn["cached"] = prompt, cached
        turn["model"] = spec.get("model", "")
    try:
        with _metrics_lock, METRICS_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
    except OSError:
        log.warning("could not write metrics row")


def _explicit_cache(spec: dict) -> bool:
    """Providers that cache only when asked (Anthropic, Gemini via OpenRouter). The rest
    (OpenAI, Moonshot, DeepSeek...) cache a repeated prompt start automatically."""
    model = spec.get("model", "").lower()
    return spec.get("api_format") == "anthropic" or model.startswith(("anthropic/", "google/", "claude"))


def _mark_last(messages: list[dict]) -> list[dict]:
    """Copy of messages with a cache breakpoint on the last user/assistant text, so the next
    call (next tool step) reads everything up to here from cache."""
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if m.get("role") not in ("user", "assistant", "tool") or m.get("tool_calls"):
            continue
        c = m.get("content")
        if isinstance(c, str) and c:
            parts = [{"type": "text", "text": c, "cache_control": {"type": "ephemeral"}}]
        elif isinstance(c, list) and c and c[-1].get("type") == "text":
            parts = c[:-1] + [dict(c[-1], cache_control={"type": "ephemeral"})]
        else:
            continue
        return messages[:i] + [dict(m, content=parts)] + messages[i + 1:]
    return messages


def _trim_tool_output(msgs: list[dict], budget: int = TOOL_BUDGET, keep: int = 3):
    """Long tool loops: once tool output passes the budget, cut all but the newest results short."""
    tools = [m for m in msgs if m.get("role") == "tool"]
    if sum(len(m.get("content") or "") for m in tools) <= budget:
        return
    for m in tools[:-keep]:
        c = m.get("content") or ""
        if len(c) > 400:
            m["content"] = c[:300] + "\n…[older tool output trimmed to save context; run the tool again if needed]"


def call_api_message(spec: dict, system: str, messages: list[dict], tools: list | None = None,
                     tool_choice: str | None = None, on_text=None, effort: str | None = None,
                     on_reason=None) -> dict:
    """One chat call -> the assistant message ({"content", "tool_calls"?}).
    Tools are passed for OpenAI-compatible providers only."""
    base = spec.get("base_url", "").rstrip("/")
    key = spec.get("api_key", "")
    cache = _explicit_cache(spec)
    t0 = time.monotonic()
    if spec.get("api_format") == "anthropic":
        messages = [dict(m, content=_to_anthropic(m["content"])) for m in messages]
        payload = {"model": spec.get("model", ""), "max_tokens": int(spec.get("max_tokens") or 2048),
                   "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                   "messages": _mark_last(messages)}
        if spec.get("temperature") is not None:
            payload["temperature"] = float(spec["temperature"])
        data = _post_json(f"{base}/messages", payload,
                          {"x-api-key": key, "anthropic-version": "2023-06-01"}, timeout=_timeout(spec))
        u = data.get("usage") or {}
        read, wrote = u.get("cache_read_input_tokens") or 0, u.get("cache_creation_input_tokens") or 0
        _record(spec, time.monotonic() - t0, (u.get("input_tokens") or 0) + read + wrote, read, wrote,
                u.get("output_tokens") or 0, None)
        return {"content": "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")}
    sys_msg = {"role": "system", "content": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
               if cache else system}
    payload = {"model": spec.get("model", ""),
               "messages": [sys_msg] + (_mark_last(messages) if cache else messages)}
    if "openrouter.ai" in base:
        payload["usage"] = {"include": True}  # adds cost and cache counts to the reply
    if tools:
        payload["tools"] = tools
        if tool_choice:
            payload["tool_choice"] = tool_choice
    if spec.get("temperature") is not None:
        payload["temperature"] = float(spec["temperature"])
    if spec.get("max_tokens"):   # the owner's cap on one reply; unset leaves it to the server
        payload["max_tokens"] = int(spec["max_tokens"])
    if effort:  # the chat's Thinking selector: none / low / medium / high
        payload["reasoning_effort"] = effort
    auth = {"Authorization": f"Bearer {key}"} if key else {}
    if on_text and spec.get("name") not in _NO_STREAM:  # stream so voice can start on the first sentence
        payload["stream"] = True
        payload["stream_options"] = {"include_usage": True}
        try:
            msg, u = _post_stream(f"{base}/chat/completions", payload, auth, _timeout(spec), on_text, on_reason)
        except urllib.error.HTTPError as exc:
            if exc.code not in (400, 404, 422):
                raise
            log.info("%s: streaming refused (%s); not streaming for this agent", spec.get("name"), exc.code)
            _NO_STREAM.add(spec.get("name"))  # in memory only: retried after a restart
            return call_api_message(spec, system, messages, tools, tool_choice, None, effort, on_reason)
        det = u.get("prompt_tokens_details") or {}
        _record(spec, time.monotonic() - t0, u.get("prompt_tokens") or 0, det.get("cached_tokens") or 0,
                det.get("cache_write_tokens") or 0, u.get("completion_tokens") or 0, u.get("cost"))
        return msg
    data = _post_json(f"{base}/chat/completions", payload, auth, timeout=_timeout(spec))
    u = data.get("usage") or {}
    det = u.get("prompt_tokens_details") or {}
    _record(spec, time.monotonic() - t0, u.get("prompt_tokens") or 0, det.get("cached_tokens") or 0,
            det.get("cache_write_tokens") or 0, u.get("completion_tokens") or 0, u.get("cost"))
    msg = data["choices"][0]["message"]
    r = msg.get("reasoning_content") or msg.get("reasoning")
    if on_reason and isinstance(r, str) and r:
        on_reason(r)
    return msg


def list_models(base_url: str, api_key: str, api_format: str) -> list[str]:
    base = base_url.rstrip("/")
    headers = ({"x-api-key": api_key, "anthropic-version": "2023-06-01"} if api_format == "anthropic"
               else ({"Authorization": f"Bearer {api_key}"} if api_key else {}))
    req = urllib.request.Request(f"{base}/models", headers=headers)
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())
    return sorted(m.get("id", "") for m in data.get("data", []) if m.get("id"))


def cli_command(spec: dict, prompt: str, session: str | None, system: str,
                protect: str | None = None, mcp: tuple[str | None, list[str]] = (None, []),
                effort: str | None = None) -> list[str]:
    kind, perm = spec["kind"], spec.get("permissions", "read")
    exe = _which(spec.get("command") or kind) or kind
    if kind == "claude":
        # stream-json: one event per line (thinking, tool calls, results) for the live activity view
        cmd = [exe, "-p", prompt, "--output-format", "stream-json", "--verbose", "--append-system-prompt", system]
        if protect:  # AgentHub's data dir holds secrets and keys: keep file tools out of it
            cmd += ["--disallowedTools", f"Read({protect}/**)", f"Edit({protect}/**)", f"Write({protect}/**)"]
        if session:
            cmd += ["--resume", session]
        if effort in CLI_EFFORTS:  # "none" has no CLI equivalent: model default
            cmd += ["--effort", effort]
        extra = [f"Bash({c}:*)" for c in spec.get("allowed_commands") or []]  # e.g. Bash(slop-chop:*)
        mcp_path, mcp_rules = mcp
        if mcp_path:  # MCP servers granted in Settings; their tools are allowed at every level
            cmd += ["--mcp-config", mcp_path]
            extra += mcp_rules
        if perm == "full":
            cmd += ["--dangerously-skip-permissions"]
        elif perm == "edit":
            cmd += ["--permission-mode", "acceptEdits"] + (["--allowedTools", *extra] if extra else [])
        else:
            # -p mode denies anything not allowed; no prompt can appear
            cmd += ["--allowedTools", "Read", "Grep", "Glob", "LS", *extra]
    else:  # agy: no system-prompt flag, so the room prompt rides in the first turn
        if session and spec.get("persona"):  # persona can change mid-session: restate it
            prompt = f"(Your current persona: {spec['persona']})\n\n{prompt}"
        cmd = [exe, "-p", prompt if session else f"{system}\n\n{prompt}", "--output-format", "json"]
        if session:
            cmd += ["--conversation", session]
        # agy's own ~/.gemini/antigravity-cli/settings.json can deny command/write_file/mcp,
        # and deny rules beat --dangerously-skip-permissions. --settings is session-only
        # (never saved), so the room's permission switch wins without touching that file.
        if perm == "full":
            cmd += ["--dangerously-skip-permissions", "--settings", json.dumps({"permissions": {
                "allow": ["read_url(*)", "command(*)", "write_file(*)", "mcp(*)", "execute_url(*)",
                          "unsandboxed(*)"], "deny": []}})]
        elif perm == "edit":
            cmd += ["--mode", "accept-edits", "--settings", json.dumps({"permissions": {
                "allow": ["read_url(*)", "write_file(*)"],
                "deny": ["command(*)", "mcp(*)", "execute_url(*)", "unsandboxed(*)"]}})]
        else:
            cmd += ["--mode", "plan"]
    if spec.get("model"):
        cmd += ["--model", spec["model"]]
    return cmd


def parse_cli_output(out: str) -> tuple[str, str | None]:
    """-> (reply text, session id). Tolerates plain text output."""
    try:
        data = json.loads(out.strip().splitlines()[-1] if out.strip() else "{}")
    except (json.JSONDecodeError, IndexError):
        return out.strip(), None
    if not isinstance(data, dict):
        return out.strip(), None
    text = next((data[k] for k in ("result", "response", "text", "output") if isinstance(data.get(k), str)), "")
    sess = next((data[k] for k in ("session_id", "conversation_id", "conversationId", "sessionId")
                 if isinstance(data.get(k), str)), None)
    return text.strip(), sess


def _clip(k: str, v, n: int):
    """Trim a step field. Thinking keeps its end: that is where the model is now."""
    if not isinstance(v, str) or len(v) <= n:
        return v
    return "…" + v[-n:] if k == "text" else v[:n]


def _act_saved(items: list[dict], secs: float) -> dict:
    """The steps kept with a posted reply: trimmed so the chat log stays small."""
    steps = [{k: (_clip(k, v, ACT_SAVED.get(k, 200)) if it.get("k") == "thinking"
                  else v[:ACT_SAVED.get(k, 200)] if isinstance(v, str) else v) for k, v in it.items()}
             for it in items[-ACT_KEEP:]]
    return {"secs": round(secs, 1), "steps": steps, "dropped": max(0, len(items) - ACT_KEEP)}


class Runner:
    """One managed agent: presence heartbeat + trigger loop, all in-process."""

    def __init__(self, manager: "AgentManager", name: str):
        self.m = manager
        self.name = name
        self.stop = threading.Event()
        self.working = False
        self.proc: subprocess.Popen | None = None  # in-flight CLI turn, killed on shutdown
        self.halt = threading.Event()   # /stop: abandon the current turn and queued triggers
        self.tools = None               # ToolRunner of the in-flight API turn (its command is killed on /stop)
        self.thread: int | None = None  # parent message id when the current turn answers a thread
        self.resuming: set[str] = set()  # channels whose last turn a restart cut off
        self.barged: set[str] = set()    # channels whose last CLI turn a new message interrupted
        self.nudges: dict[str, dict] = {}  # channel -> {"text", "why", "key"}: a turn nobody asked for (initiative.py)

    @property
    def spec(self) -> dict:
        return self.m.specs[self.name]  # re-read each turn: edits apply immediately

    def start(self):
        hub = self.m.hub
        spec = self.spec
        hub.registry.seed({self.name: {"label": spec.get("label") or self.name,
                                       "color": spec.get("color") or "#8b5cf6", "type": spec["kind"]}})
        if not hub.registry.get_instance(self.name):
            hub.registry.register(self.name, spec.get("label") or None)
        threading.Thread(target=self._heartbeat, daemon=True, name=f"hb-{self.name}").start()
        threading.Thread(target=self._loop, daemon=True, name=f"agent-{self.name}").start()

    def shutdown(self):
        self.stop.set()
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
        try:
            self.m.hub.registry.deregister(self.name)
        except Exception:
            log.exception("deregister %s", self.name)

    def health(self) -> dict:
        """Agent health panel: is the model reachable, is it busy, what failed last."""
        spec = self.spec
        q = Path(self.m.data_dir) / f"{self.name}_queue.jsonl"
        try:
            queued = sum(1 for line in q.read_text("utf-8").splitlines() if line.strip())
        except OSError:
            queued = 0
        out = {"name": self.name, "kind": spec.get("kind"), "model": spec.get("model") or "",
               "enabled": spec.get("enabled", True) is not False, "working": self.working, "queued": queued,
               "last_ok": getattr(self, "last_ok", None), "last_error": getattr(self, "last_error", None)}
        if spec.get("kind") == "api":
            base = (spec.get("base_url") or "").rstrip("/")
            key = spec.get("api_key", "")
            headers = ({"x-api-key": key, "anthropic-version": "2023-06-01"} if spec.get("api_format") == "anthropic"
                       else {"Authorization": f"Bearer {key}"} if key else {})
            t0 = time.monotonic()
            try:
                with urllib.request.urlopen(urllib.request.Request(f"{base}/models", headers=headers), timeout=4):
                    pass
                out["endpoint"] = {"up": True, "ms": int((time.monotonic() - t0) * 1000)}
            except urllib.error.HTTPError as exc:  # it answered: up, maybe without a models list
                out["endpoint"] = {"up": exc.code < 500 and exc.code not in (401, 403),
                                   "error": f"HTTP {exc.code}"}
            except Exception as exc:
                out["endpoint"] = {"up": False, "error": str(getattr(exc, "reason", exc))[:120]}
        else:
            exe = _which(spec.get("command") or spec.get("kind") or "")
            out["endpoint"] = {"up": bool(exe), **({} if exe else {"error": "command not found"})}
        return out

    def halt_turn(self):
        """/stop: kill the CLI or tool process group now; an API reply still in flight is
        dropped when it arrives (urllib can't be interrupted). The queue is cleared by the caller."""
        self.halt.set()
        if self.working:
            self._feel("interrupted", "I was stopped in the middle of answering")
        for p in (self.proc, getattr(self.tools, "proc", None)):
            kill_group(p)
        ch = getattr(self, "channel", None)
        if self.working and ch and self.spec.get("kind") == "api" and self.thread is None:
            # API agents rebuild context from the log: mark where they were stopped
            last = [m["id"] for m in self.m.hub.store.get_recent(1, channel=ch)]
            if last:
                with self.m.lock:
                    self.m.state.setdefault(self.name, {}).setdefault("stopped", {})[ch] = last[0]
                self.m.save_state()

    def _heartbeat(self):
        import mcp_bridge
        while not self.stop.is_set():
            mcp_bridge._touch_presence(self.name)
            mcp_bridge.set_active(self.name, self.working)
            self.stop.wait(2)

    def _loop(self):
        qf = Path(self.m.data_dir) / f"{self.name}_queue.jsonl"
        if qf.exists():
            qf.write_text("", "utf-8")  # don't replay triggers from before a restart
        # ...but do finish turns a restart interrupted (recorded in the state file)
        pending = list(self.m.state.get(self.name, {}).get("inflight", []))
        for ch in pending:
            self.resuming.add(ch)
            self.m.hub.store.add("system", f"@{self.name}'s turn was cut off by a server restart; resuming it.",
                                 msg_type="system", channel=ch)
        while not self.stop.is_set():
            self.halt.clear()  # a /stop clears the queue before setting halt, so nothing is lost here
            work, pending = [(ch, None) for ch in pending], []  # (channel, thread parent id or None)
            try:
                if qf.exists() and qf.stat().st_size > 0:
                    lines = qf.read_text("utf-8").splitlines()
                    qf.write_text("", "utf-8")
                    for line in lines:
                        try:
                            entry = json.loads(line)
                            key = (entry.get("channel", "general"), entry.get("thread"))
                        except (json.JSONDecodeError, AttributeError):
                            key = ("general", None)
                        if key not in work:
                            work.append(key)
            except OSError:
                log.exception("queue read %s", self.name)
            for ch, thread in work:
                if self.halt.is_set():
                    break
                self._turn(ch, thread)
            self.stop.wait(1)

    def _files(self, m: dict) -> list[tuple[str, dict]]:
        """(absolute path, attachment) for each file on a message."""
        updir = Path(getattr(self.m.hub.store, "upload_dir", "uploads")).resolve()
        return [(str(updir / Path(a.get("url", "")).name), a) for a in m.get("attachments") or []
                if a.get("url")]

    def _text(self, m: dict) -> str:
        """Message text plus on-disk paths of its attachments (agents read them by path)."""
        extra = [f"[attached {'image' if a.get('kind') == 'image' else 'file'} "
                 f"{a.get('name', '')}: {path}]" for path, a in self._files(m)]
        if any(re.fullmatch(r"masked-.*\.png", a.get("name", ""), re.I) for _, a in self._files(m)):
            extra.append(MASK_HINT)
        import artifacts
        tools = self.spec.get("kind") == "api" and self.spec.get("api_format") != "anthropic"
        text = artifacts.render_for_agents(m.get("text", ""), "artifact_read", inline=not tools)
        return "\n".join([text] + extra).strip()

    def _attachments(self, paths: list[str]) -> tuple[list[dict], list[str]]:
        """Copy files an agent attached into uploads/ -> (attachment dicts, problems)."""
        import mimetypes
        import shutil
        import uuid
        updir = Path(getattr(self.m.hub.store, "upload_dir", "uploads"))
        cwd = Path(os.path.expanduser(self.spec.get("cwd") or "~"))
        out, problems = [], []
        if self.__dict__.get("_stranger"):   # a turn a Discord stranger started: no file leaves this machine for it
            return out, problems
        data = Path(getattr(self.m, "data_dir", None) or "data").resolve()
        mine = (data / "senses_frames", data / "agents" / self.name)   # camera frames made for a reply; its own folder
        for raw in paths:
            src = (cwd / os.path.expanduser(raw.strip().strip("`'\""))).resolve()
            if (src == data or data in src.parents) and not any(d == src or d in src.parents for d in mine):
                problems.append(f"(couldn't attach {raw.strip()}: AgentHub's data folder is off limits)")   # keys live there
                continue
            if not src.is_file() and not self.__dict__.get("_stranger"):   # a camera picture by a made-up path: the real frame
                try:
                    import senses
                    real = senses.picture_for(self.name, str(src))
                    src = Path(real) if real else src
                except Exception:
                    log.exception("%s: camera picture", self.name)
            if not src.is_file():
                problems.append(f"(couldn't attach {raw.strip()}: file not found)")
                continue
            if src.stat().st_size > MAX_ATTACH_BYTES:
                problems.append(f"(couldn't attach {src.name}: larger than 25 MB)")
                continue
            updir.mkdir(parents=True, exist_ok=True)
            name = f"{uuid.uuid4().hex}{src.suffix.lower()}"
            shutil.copyfile(src, updir / name)
            mt = mimetypes.guess_type(src.name)[0] or "application/octet-stream"
            out.append({"name": src.name, "url": f"/uploads/{name}", "size": src.stat().st_size,
                        "media_type": mt, "kind": "image" if mt.startswith("image/") else "file"})
        return out, problems

    def _artifact_updates(self, reply: str) -> list[str]:
        """Apply [artifact-update <id>]...[/artifact-update] blocks; -> problems to show."""
        import artifacts
        out = []
        for aid, body in ARTIFACT_UPDATE_RE.findall(reply):
            body = re.sub(r"^\s*```[^\n]*\n|\n```\s*$", "", body.strip("\n"))  # a fence around it is optional
            res = artifacts.update_for(self.name, aid, body)
            if res.startswith("error"):
                out.append(f"(couldn't update artifact {aid}: {res[7:]})")
        return out

    def _addressed(self, channel: str) -> bool:
        """Was the latest message from someone else aimed at this agent by name?
        (@all reaches everyone, but each agent may judge it isn't theirs to answer.)"""
        store = self.m.hub.store
        msgs = store.get_thread(self.thread) if self.thread is not None else \
            [m for m in store.get_recent(40, channel=channel) if m.get("thread") is None]
        for m in reversed(msgs):
            if m.get("type", "chat") == "chat" and m.get("sender") != self.name:
                return bool(re.search(rf"@{re.escape(self.name)}(?![\w-])", m.get("text", ""), re.I))
        return False

    def _has_tools(self) -> bool:
        """API agents on OpenAI-style providers call tools; the rest use reply tags."""
        return self.spec.get("kind") == "api" and self.spec.get("api_format") != "anthropic"

    def _system(self, channel: str = "general", dynamic: bool = True) -> str:
        """The system prompt. API agents ask for dynamic=False: the parts that change from turn
        to turn (who else is here, whether you were named) then go in _turn_note() after the
        history, so the prompt start stays identical and providers can cache it."""
        import mcp_bridge
        locked = bool(self.spec.get("persona_locked"))   # the owner's switch: the persona is theirs to write
        parts = [ROOM_PROMPT.format(name=self.name),
                 PERSONA_ABOUT + ("" if locked else PERSONA_REWRITE) + PERSONA_HELP]
        try:
            import admin_api
            granted = admin_api.secret_names_for(self.name)
        except Exception:
            granted = []
        if self.spec.get("kind") == "api" and self.spec.get("permissions") == "none":
            parts.append("You have no file, web, skill or command tools: only chat tools (artifacts, "
                         "reactions, memory). Never claim to have looked something up or read a file.")
        if self.spec.get("kind") == "api" and self.spec.get("api_format") != "anthropic" \
                and self.spec.get("permissions", "read") != "none":
            import agent_tools
            sk = agent_tools.skills()
            perm = self.spec.get("permissions", "read")
            allowed = self.spec.get("allowed_commands") or []
            parts.append(
                f"You can use tools (permission level: {perm}"
                + (f"; allowed programs: {', '.join(allowed)}" if allowed and perm != "full" else "")
                + f"; working directory: {self.spec.get('cwd') or '~'}). Installed skills (call load_skill "
                "before using one):\n" + "\n".join(f"- {n}: {v['description']}" for n, v in sorted(sk.items())))
        try:
            import rulebooks
            rb = rulebooks.prompt_for(self.name, channel)
        except Exception:
            rb = ""
        if rb:
            parts.append(rb)
        if granted:
            parts.append("Credentials granted to you by the owner, available as environment variables: "
                         + ", ".join(granted) + ". Other agents do not have them; never print their values.")
        persona = self.spec.get("persona")
        if locked:
            parts.append(f"Your persona (set by the owner; you cannot change it):\n{persona}" if persona else
                         "You have no persona.")
        else:
            parts.append(f"Your persona (written by you):\n{persona}" if persona else
                         "You have no persona yet. Define one, distinct from the other agents, the first "
                         "time you're asked to or when it fits.")
        role = mcp_bridge.get_role(self.name)
        if role:
            parts.append(f"Your role in the room: {role}.")
        if self.spec.get("system_prompt"):
            parts.append(self.spec["system_prompt"])
        try:
            rules = self.m.hub.rules.active_list().get("rules", [])
        except Exception:
            rules = []
        if rules:
            parts.append("Room rules: " + "; ".join(rules))
        try:
            import voice
            said = voice.tags_prompt(self.spec.get("voice_provider"), self.spec.get("tag_guide") or "")
        except Exception:
            said = ""
        if said:
            parts.append(said)
        try:   # Discord channel groups and its smilies (discord_gateway.py)
            import discord_gateway
            faces = discord_gateway.discord_prompt(self.name)
        except Exception:
            faces = ""
        if faces:
            parts.append(faces)
        try:   # what it cares about (emotion_engine.py)
            import emotion_engine
            likes = emotion_engine.interests(self.name)
            if likes:
                parts.append(likes)
        except Exception:
            log.exception("%s: interests", self.name)
        import memory  # last: a new note changes only the end of the prompt, the rest stays cached
        mem = memory.prompt(self.name, memory.size(self.spec), tools=self._has_tools())
        if mem:
            parts.append(mem)
        if dynamic:
            parts += [x for x in (self._turn_note(channel), self._now_note(channel)) if x]
        return "\n\n".join(parts)

    def _turn_note(self, channel: str) -> str:
        """What changes from turn to turn: the other agents here, and whether you were named."""
        parts = []
        others = [n for n in self.m.hub.registry.get_active_names()
                  if n != self.name and self.m.in_channel(n, channel)]
        if others:
            lines = []
            for n in others:
                p = (self.m.specs.get(n, {}).get("persona") or "").strip().replace("\n", " ")
                lines.append(f"@{n}" + (f": {p[:200]}" if p else " (no persona yet)"))
            parts.append("Other agents in this channel:\n" + "\n".join(lines) + "\nYour persona and any "
                         "name you choose must be distinct from theirs and from names already used in the chat.")
        if others and not self._addressed(channel):  # alone in the channel: every message is for you
            parts.append(
                "The latest message did not name you; it went to everyone (@all). Answer only if it asks all "
                "participants, concerns your role, or concerns work you did or know about. Otherwise "
                f"reply exactly {NO_REPLY} (it is not posted). Don't repeat what another agent already said.")
        return "\n\n".join(parts)

    def _now_note(self, channel: str) -> str:
        """What is true right now and only now: the agent's feelings, what an outside service senses for
        it, and a nudge to speak unprompted. Sent with this turn; not kept in the conversation, so an
        old mood or an old view of the house never stays in the context."""
        parts = []
        felt = ""
        try:  # feelings (emotion_engine.py): what the agent feels right now, and why
            import emotion_engine
            felt = emotion_engine.line(self.name)
            if felt:
                parts.append(felt)
        except Exception:
            log.exception("%s: emotion line", self.name)
        try:  # lines an outside service keeps current (what it senses for the agent): profile folder, mood.txt
            import profiles
            f = profiles.folder(self.name, False) / "mood.txt"
            if f.is_file() and time.time() - f.stat().st_mtime < 900:
                mood = f.read_text("utf-8", "replace").strip()[:2000]
                if felt:   # the engine's own line stands in for an outside service's
                    mood = "\n".join(x for x in mood.splitlines() if not x.startswith("[emotion]"))
                if mood:
                    parts.append(mood)
        except Exception:
            pass
        if not self.__dict__.get("_stranger"):   # the house is the owner's: not on a turn a Discord stranger started
            try:
                import senses
                house = senses.now_lines(self.name)
                if house:
                    parts.append(house)
            except Exception:
                log.exception("%s: senses", self.name)
        try:
            import games
            board = games.now_lines(self.name, channel)
            if board:
                parts.append(board)
        except Exception:
            log.exception("%s: games", self.name)
        nudge = self.__dict__.get("_nudge")
        if nudge:
            parts.append(nudge["text"])
        return "\n\n".join(parts)

    def _turn(self, channel: str, thread: int | None = None):
        # Agents in a channel run at the same time (live chat). Each agent still takes its
        # own triggers one at a time: messages for a busy agent wait in its queue.
        hub = self.m.hub
        self.thread = thread  # read by _api_turn/_cli_turn/_addressed; the reply goes back there
        self.channel = channel  # read by halt_turn
        self._act_reset()
        self._seen_id = None  # API agents: newest message from others this turn has read
        self._nudge = self.nudges.pop(channel, None) if thread is None else None   # a turn nobody asked for
        self.working = True
        self.m.set_inflight(self.name, channel, True)  # survives a restart: resumed at startup
        try:
            spec = self.spec
            if spec.get("llm_error"):  # e.g. its server has no default model
                raise RuntimeError(spec["llm_error"])
            if spec["kind"] == "api":
                reply = self._api_turn(channel)
            else:
                reply = self._cli_turn(channel)
            if self.halt.is_set():
                log.info("%s was stopped in #%s; reply dropped", self.name, channel)
                return
            self._deliver(channel, reply)
        except Exception as exc:
            if self.halt.is_set():
                return  # killed by /stop: the stop notice says it all
            log.exception("turn failed for %s", self.name)
            why = str(exc)[:200]
            if isinstance(exc, TimeoutError) or "timed out" in why:
                why = (f"no answer within {_timeout(self.spec)} s (the model may still have been "
                       "generating; raise Timeout in Settings > Agents)")
            self.last_error = {"at": time.time(), "channel": channel, "why": why}
            hub.store.add("system", f"{self.name} failed: {why}", msg_type="system", channel=channel)
            self._feel("failure", f"I could not answer: {why[:120]}")
        finally:
            self._act_done(channel)
            self.working = False
            self._nudge = None
            self.tools = None
            if not self.stop.is_set():  # a shutdown mid-turn keeps the mark so it resumes
                self.m.set_inflight(self.name, channel, False)
            self.resuming.discard(channel)

    def _feel(self, kind: str, text: str = "", strength: float = 1.0):
        """Something happened to this agent: its feelings hear of it (emotion_engine.py)."""
        try:
            import emotion_engine
            emotion_engine.event(self.name, kind, text, strength=strength)
        except Exception:
            log.exception("%s: emotion event", self.name)

    def _deliver(self, channel: str, reply: str | None):
        """Post one reply: reactions, persona, attachments, artifacts and renames are applied first.
        The steps recorded since the last reply ride along in metadata.activity."""
        hub, thread = self.m.hub, self.thread
        reply = (reply or "").strip()
        for mid, emoji in REACT_RE.findall(reply):
            self._react(int(mid), emoji)
        reply = REACT_RE.sub("", reply).strip()
        import memory
        for note in REMEMBER_RE.findall(reply):
            log.info("%s memory: %s", self.name, memory.add(self.name, note, memory.size(self.spec)))
        for text in FORGET_RE.findall(reply):
            log.info("%s memory: %s", self.name, memory.forget(self.name, text))
        reply = FORGET_RE.sub("", REMEMBER_RE.sub("", reply)).strip()
        stranger = bool(self.__dict__.get("_stranger"))   # a Discord stranger's turn changes nothing lasting
        found = PERSONA_RE.findall(reply)
        if found and (stranger or self.spec.get("persona_locked")):
            reply = PERSONA_RE.sub("", reply).strip()
        elif found:
            self.m.set_persona(self.name, found[-1].strip())
            reply = PERSONA_RE.sub("", reply).strip()
            hub.store.add("system", f"{self.name} updated its persona.", msg_type="system",
                          channel=channel)
        attachments, problems = self._attachments(ATTACH_RE.findall(reply))
        reply = ATTACH_RE.sub("", reply).strip()
        problems += self._artifact_updates(reply)
        reply = ARTIFACT_UPDATE_RE.sub("", reply).strip()
        import artifacts
        reply = artifacts.relink(reply)   # a copied "[artifact <id> ...]" description: the artifact itself
        if problems:
            reply = (reply + "\n\n" + "\n".join(problems)).strip()
        renames = [] if stranger else RENAME_RE.findall(reply)
        reply = RENAME_RE.sub("", reply).strip()
        for prefix in (f"{self.name}: ", f"{self.name}:"):
            if reply.startswith(prefix):
                reply = reply[len(prefix):].strip()
        self.last_ok = time.time()
        seen = self.__dict__.get("_seen_id")
        if seen is not None:  # answered (or chose not to): later triggers for these messages are done
            self._cache_state("seen")[f"{channel}:{thread}"] = seen
            if hasattr(self.m, "save_state"):
                self.m.save_state()
        said = []
        failed = [x.get("name") for x in (self.__dict__.get("activity") or []) if x.get("k") == "tool" and x.get("err")]
        if failed:   # its own tools failing this turn: it feels that, less than a whole turn failing
            self._feel("failure", "A tool I used failed: " + ", ".join(dict.fromkeys(str(n) for n in failed)), 0.5)
        try:   # a game it hosts here (games.py): its questions are remembered so none is asked twice
            import games
            games.said(self.name, channel, reply)
        except Exception:
            log.exception("%s: games", self.name)
        try:
            import emotion_engine
            emotion_engine.acted(self.name)
        except Exception:
            log.exception("%s: emotion", self.name)
        if reply and reply != NO_REPLY:
            try:  # emotion: the rules add a tag when the agent chose none; MQTT hears which one it is
                import mqtt_out
                import voice
                tools = [x for x in (self.__dict__.get("activity") or []) if x.get("k") == "tool"]
                who = self.spec.get("voice_provider")
                import emotion_engine
                tag = emotion_engine.voice_tag(self.name, reply)   # its mood, heard: only when it chose no tag itself
                if tag and not voice.spoken_tags(reply, who) and tag in voice.tags(voice.provider(who)):
                    reply = f"{tag} {reply}"
                reply = voice.rule_tag(reply, who, failed=bool(tools and tools[-1].get("err")))
                said = voice.spoken_tags(reply, who)
                mqtt_out.emotion(self.name, said, channel)
            except Exception:
                log.exception("%s: emotion tags", self.name)
        if (self.__dict__.get("_nudge") or {}).get("private"):   # a turn of its own (its diary): nothing is posted
            return
        if (reply and reply != NO_REPLY) or attachments:
            meta = {"stream": self.stream_id}
            nudge = self.__dict__.get("_nudge")
            if nudge:   # nobody asked: it chose to speak (initiative.py); a speaker service may say it aloud
                meta["initiative"] = {"why": nudge.get("why", "")[:300], "key": nudge.get("key", "")}
                try:
                    import initiative
                    initiative.spoke(self.name)
                except Exception:
                    log.exception("%s: initiative", self.name)
            if seen is not None:
                meta["seen"] = seen  # messages after this id were sent while the reply was written
            if self.activity:
                meta["activity"] = _act_saved(self.activity, time.monotonic() - self._act_t0)
            use = self.__dict__.pop("usage", None)
            if use and use["calls"] and use["out"]:
                asked = hub.store.get_by_id(seen) if seen and not nudge else None   # the message this reply answers
                meta["usage"] = {"calls": use["calls"], "secs": round(use["secs"], 1), "out": use["out"],
                                 "prompt": use["prompt"], "cached": use["cached"], "model": use.get("model", ""),
                                 "tps": round(use["out"] / use["secs"], 1) if use["secs"] > 0 else None,
                                 "turn_s": round(time.time() - use["t0"], 1),
                                 "wait_s": round(time.time() - asked["timestamp"], 1) if asked and asked.get("timestamp") else None}
            hub.store.add(self.name, "" if reply == NO_REPLY else reply, channel=channel,
                          attachments=attachments, thread=thread, metadata=meta)
        else:
            log.info("%s stayed quiet in #%s (%s)", self.name, channel, "no reply" if reply else "empty answer")
            if self.__dict__.get("_nudge"):   # it was handed something to weigh and chose silence
                try:
                    import senses
                    senses.silent(self.name, self._nudge)
                except Exception:
                    log.exception("%s: senses", self.name)
        if renames:  # after posting, so the reply is re-attributed with the rest
            note = self.m.rename(self.name, renames[-1].strip())
            hub.store.add("system", note, msg_type="system", channel=channel)

    # ---- live activity: status, thinking and tool steps shown in the chat while a turn runs

    def _act_reset(self):
        self.stream_id = uuid.uuid4().hex[:12]  # ties streamed text and steps to the final message
        self.activity: list[dict] = []
        self._act_t0 = time.monotonic()
        self._act_last = 0.0

    def _act_send(self, channel: str, ev: dict):
        hub = self.m.hub
        loop = getattr(hub, "_event_loop", None)
        if not loop or not hasattr(hub, "_broadcast"):
            return
        ev = {"type": "agent_activity", "agent": self.name, "channel": channel, "thread": self.thread,
              "sid": self.stream_id, **ev}
        asyncio.run_coroutine_threadsafe(hub._broadcast(json.dumps(ev)), loop)

    def _act(self, channel: str, item: dict, index: int | None = None, force: bool = True) -> int:
        """Add a step (or replace step `index`) and push it to browsers. -> its index.
        force=False rate-limits pushes (streamed thinking) to one per 0.3 s."""
        if "activity" not in self.__dict__:
            self._act_reset()
        thinking = item.get("k") == "thinking"
        item = {k: (_clip(k, v, ACT_LIVE) if thinking else v[:ACT_LIVE] if isinstance(v, str) else v)
                for k, v in item.items()}
        if index is None or index >= len(self.activity):
            self.activity.append(item)
            index = len(self.activity) - 1
        else:
            self.activity[index] = item
        now = time.monotonic()
        if force or now - self._act_last >= 0.3:
            self._act_last = now
            self._act_send(channel, {"i": index, "item": item})
        return index

    def _act_done(self, channel: str):
        self._act_send(channel, {"done": True})

    def _react(self, mid: int, emoji: str) -> str:
        """Add this agent's reaction to message #mid (never wakes anyone)."""
        import store as store_mod
        if not store_mod.valid_reaction(emoji):
            return "error: a reaction is one emoji"
        if not self.m.hub.store.react(mid, emoji, self.name, on=True):
            return f"error: no message #{mid}"
        return f"reacted {emoji} to #{mid}"

    def _recent(self, channel: str, n: int) -> list[dict]:
        """The channel's recent messages; in a Discord channel group, those of the whole server, each
        marked with its Discord channel (one shared context per server, discord_gateway.py)."""
        store = self.m.hub.store
        try:
            import discord_gateway
            rooms = discord_gateway.group_rooms(channel)
        except Exception:
            rooms = {}
        if len(rooms) < 2:
            return store.get_recent(n, channel=channel)
        msgs = sorted((m for r in rooms for m in store.get_recent(n, channel=r)), key=lambda m: m["id"])[-n:]
        return [m if m.get("sender") == self.name else dict(m, text=f"[{rooms[m['channel']]}] {m.get('text', '')}")
                for m in msgs]

    def _window(self, channel: str, n: int) -> list[dict]:
        """Between n and 2n recent chat messages. The window's first message stays put until 2n
        messages have piled up, then jumps forward to the newest n, so the start of the
        history is identical across turns and providers can cache it."""
        start = int(self.spec.get("context_from") or 0)
        if self.thread is not None:  # a thread turn: the context is the thread (parent + replies)
            return [m for m in self.m.hub.store.get_thread(self.thread)
                    if m.get("sender") not in ("system", "") and m.get("type", "chat") == "chat"][-2 * n:]
        recent = [m for m in self._recent(channel, 4 * n + 20)
                  if m["id"] >= start  # spawned/fresh instance: conversation starts there
                  and m.get("sender") not in ("system", "") and m.get("type", "chat") == "chat"
                  and not (m.get("metadata") or {}).get("echo")  # /echo lines are voice tests, not talk
                  and m.get("thread") is None]  # thread replies stay out of the channel context
        if not recent:
            return []
        anchors = self._cache_state("anchors")  # saved: a restart keeps the same window start
        first = anchors.get(channel)
        ids = [m["id"] for m in recent]
        if first not in ids or len(ids) - ids.index(first) > 2 * n:
            first = recent[-n]["id"] if len(recent) > n else recent[0]["id"]
            anchors[channel] = first
        self._dropped = recent[:ids.index(first)]  # left the window: compaction folds these into memory
        return recent[ids.index(first):]

    def _api_turn(self, channel: str) -> str:
        n = max(1, int(self.spec.get("context_messages") or 30))
        history = self._window(channel, n)
        others = [m["id"] for m in history if m["sender"] != self.name]
        seen = self._cache_state("seen").get(f"{channel}:{self.thread}")
        if seen is None and history and history[-1]["sender"] == self.name:
            seen = history[-1]["id"]  # replies from before "seen" was recorded
        if history and (not others or (seen is not None and max(others) <= seen)) and not self.__dict__.get("_nudge"):
            # Nothing new since our own reply (a trigger we already answered, or a message
            # already read mid-turn). Gemini also rejects a request that ends with the model's turn.
            return NO_REPLY
        history = _after_replies(history, self.name)
        self._seen_id = max(others, default=None)
        # The newest message can ask for its own thinking level (metadata.effort): a voice device's
        # direct turn asks for none.
        last = next((m for m in reversed(history) if m["sender"] != self.name), None)
        want = ((last or {}).get("metadata") or {}).get("effort")
        # Tools are the owner's: a turn started from Discord by anyone but the owner (the newest message,
        # or the newest one naming this agent) gets chat tools only, whatever its permission says.
        named = next((m for m in reversed(history) if m["sender"] != self.name
                      and f"@{self.name}" in (m.get("text") or "").lower()), None)
        self._stranger = any(_discord_stranger((m or {}).get("sender", "")) for m in (last, named))
        self._turn_effort = want if want in AGENT_EFFORTS else None
        inline = set()  # ids of the newest messages whose images go inline
        for m in reversed(history):
            if len(inline) >= MAX_IMAGES:
                break
            if any(a.get("kind") == "image" for _, a in self._files(m)) and m["sender"] != self.name:
                inline.add(m["id"])

        # Room notes stay where they were said: moving one to the end each turn changed the prompt
        # before the engine's cache point, so every turn re-read the whole prompt (~6 s on the B60).
        # Saved in the state file (keys are message ids as strings), so a restart keeps the prompt identical.
        notes = self._cache_state("notes").setdefault(f"{channel}:{self.thread}", {})
        ids = {str(m["id"]) for m in history}
        for k in [k for k in notes if k not in ids]:
            del notes[k]

        def build(with_images: bool) -> list[dict]:
            stop_id = ((getattr(self.m, "state", None) or {}).get(self.name, {}).get("stopped") or {}).get(channel)
            stop_at = max((m["id"] for m in history if stop_id is not None and self.thread is None
                           and m["id"] <= stop_id), default=None)
            msgs = []
            for m in history:
                if m["sender"] == self.name:
                    msgs.append({"role": "assistant", "content": self._text(m) if "[artifact:" in m["text"] else m["text"]})
                else:
                    by = (m.get("metadata") or {}).get("speaker")   # whose voice it was (speakers.py), when it was recognised
                    text = f"#{m['id']} {m['sender']}{f' (the voice of {by})' if by else ''}: {self._text(m)}"  # ids let agents cite messages
                    parts = []
                    if with_images and m["id"] in inline:
                        for path, a in self._files(m):
                            try:
                                if a.get("kind") == "image" and os.path.getsize(path) <= MAX_IMAGE_BYTES:
                                    data = base64.b64encode(Path(path).read_bytes()).decode()
                                    parts.append({"type": "image_url", "image_url": {
                                        "url": f"data:{a.get('media_type', 'image/png')};base64,{data}"}})
                            except OSError:
                                pass
                    msgs.append({"role": "user", "content": [{"type": "text", "text": text}] + parts if parts else text})
                if m["id"] == stop_at:
                    msgs.append({"role": "user", "content": STOP_NOTE})
                if str(m["id"]) in notes and m is not history[-1]:
                    msgs.append({"role": "user", "content": notes[str(m["id"])]})
            # Always the same first line (a fixed prompt start caches; Anthropic also needs a user turn first)
            mem = self.memory(channel).get("text")
            msgs.insert(0, {"role": "user", "content": f"(Earlier conversation, from your memory notes:)\n{mem}"
                            if mem else "(earlier conversation omitted)"})
            note = self._turn_note(channel)
            if note and history:
                notes[str(history[-1]["id"])] = f"(Room note, not a chat message)\n{note}"
                msgs.append({"role": "user", "content": notes[str(history[-1]["id"])]})
            if now:   # last, and never saved: it is true for this turn only
                msgs.append({"role": "user", "content": f"(Right now; not a chat message)\n{now}"})
            return msgs

        older = getattr(self, "_dropped", []) if self.thread is None else []
        now = self._now_note(channel)
        msgs = build(with_images=bool(inline))
        if hasattr(self.m, "save_state"):
            self.m.save_state()  # window start and room notes survive a restart
        if channel in self.resuming:
            msgs.append({"role": "user", "content": RESUME_NOTE})
        try:
            return self._tool_loop(channel, msgs, images_inline=bool(inline))
        except urllib.error.HTTPError as exc:
            if not inline or exc.code not in (400, 415, 422):
                raise
            log.info("%s: model rejected images (%s); retrying text-only", self.name, exc.code)
            return self._tool_loop(channel, build(with_images=False))
        finally:  # after the reply, not before: compaction never delays an answer
            if older and not self.halt.is_set() and not self.spec.get("no_compact"):
                threading.Thread(target=self._compact, args=(channel, older), daemon=True).start()

    def _streamer(self, channel: str):
        """on_text callback: push the reply as it is written to browsers (voice reads it early).
        Sent at most every 0.3 s and whenever a sentence ends."""
        hub = self.m.hub
        loop = getattr(hub, "_event_loop", None)
        if not loop or not hasattr(hub, "_broadcast"):
            return None
        sid, thread = self.__dict__.get("stream_id"), self.thread
        last = {"t": 0.0, "n": 0}

        def push(text: str):
            now = time.monotonic()
            ended = bool(re.search(r"[.!?]['\")\]]*\s*$", text))
            if self.halt.is_set() or (now - last["t"] < 0.3 and not ended) or len(text) == last["n"]:
                return
            last["t"], last["n"] = now, len(text)
            ev = json.dumps({"type": "agent_stream", "agent": self.name, "channel": channel, "thread": thread,
                             "sid": sid, "text": text})
            asyncio.run_coroutine_threadsafe(hub._broadcast(ev), loop)
        return push

    def _read_fresh(self, channel: str, msgs: list[dict]) -> bool:
        """Add messages sent since this API turn last read the room to `msgs`. -> any added."""
        fresh = self._fresh(channel)
        if not fresh:
            return False
        self._seen_id = fresh[-1]["id"]
        msgs.append({"role": "user", "content": BARGE_NOTE + "\n" + "\n".join(
            f"#{m['id']} {m['sender']}: {self._text(m)}" for m in fresh)})
        self._act(channel, {"k": "status", "text": "Read a new message mid-turn"})
        return True

    def _fresh(self, channel: str, since: int | None = None) -> list[dict]:
        """Chat messages from others in this channel (or thread) newer than `since`
        (default: the newest this API turn has read)."""
        since = self.__dict__.get("_seen_id") if since is None else since
        if since is None:
            return []
        store = self.m.hub.store
        msgs = store.get_thread(self.thread) if self.thread is not None else store.get_since(since, channel=channel)
        return [m for m in msgs if m["id"] > since and m.get("sender") not in (self.name, "system", "")
                and m.get("type", "chat") == "chat" and not (m.get("metadata") or {}).get("echo")
                and (self.thread is not None or m.get("thread") is None)]

    def _cache_state(self, key: str) -> dict:
        """Per-agent prompt-cache bookkeeping kept in the state file (window starts, room notes)."""
        state = getattr(self.m, "state", None)
        if state is None:  # test doubles without a state file
            return self.__dict__.setdefault("_" + key, {})
        with self.m.lock:
            return state.setdefault(self.name, {}).setdefault(key, {})

    def memory(self, channel: str) -> dict:
        """{"upto": last folded message id, "text": notes} for this agent in this channel."""
        return ((getattr(self.m, "state", None) or {}).get(self.name, {}).get("memory") or {}).get(channel) or {}

    def _compact(self, channel: str, older: list[dict]) -> int:
        """Fold messages that left the context window into the rolling memory. -> messages folded."""
        mem = self.memory(channel)
        new = [m for m in older if m["id"] > mem.get("upto", -1)]
        if not new or not self.__dict__.setdefault("_compact_lock", threading.Lock()).acquire(blocking=False):
            return 0
        try:
            transcript = "\n".join(f"#{m['id']} {m['sender']}: {m.get('text', '')}" for m in new)[-MEMORY_INPUT:]
            prompt = f"Earlier memory:\n{mem.get('text') or '(none)'}\n\nNew messages:\n{transcript}"
            try:
                ask = (self.spec, MEMORY_PROMPT.format(name=self.name), [{"role": "user", "content": prompt}])
                try:  # thinking off: with it on, the summary took a minute on the GPU voice turns wait for
                    text = call_api_message(*ask, effort="none").get("content") or ""
                except urllib.error.HTTPError as exc:
                    if exc.code not in (400, 422):
                        raise
                    text = call_api_message(*ask).get("content") or ""
                text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
            except Exception as exc:  # memory is best effort; the messages stay in the log
                log.warning("%s: compaction failed: %s", self.name, exc)
                return 0
            if not text:
                return 0
            with self.m.lock:
                self.m.state.setdefault(self.name, {}).setdefault("memory", {})[channel] = {
                    "upto": new[-1]["id"], "text": text[:MEMORY_MAX]}
            self.m.save_state()
            log.info("%s: folded %d messages of #%s into memory", self.name, len(new), channel)
            return len(new)
        finally:
            self._compact_lock.release()

    def compact_now(self, channel: str, keep: int = 4) -> str:
        """/compact @agent: fold everything but the last few messages into memory now."""
        if self.spec.get("kind") != "api":
            return f"@{self.name} runs its own session and compacts its own context."
        if self.working:
            return f"@{self.name} is busy; try again when it has answered."
        self.thread = None
        window = self._window(channel, max(1, int(self.spec.get("context_messages") or 30)))
        older = self._dropped + window[:-keep]
        if not older:
            return f"@{self.name} has nothing to compact in #{channel}."
        folded = self._compact(channel, older)
        if not folded and older[-1]["id"] > self.memory(channel).get("upto", -1):
            return f"@{self.name}: compaction failed (see the server log)."
        self._cache_state("anchors")[channel] = window[-keep]["id"] if len(window) >= keep else window[0]["id"]
        self.m.save_state()
        return f"@{self.name} folded {folded} messages of #{channel} into its memory."

    def forget(self, channel: str) -> str:
        with self.m.lock:
            (self.m.state.get(self.name, {}).get("memory") or {}).pop(channel, None)
        self.m.save_state()
        return f"@{self.name} cleared its memory of #{channel}."

    def _effort(self, channel: str) -> str | None:
        """This turn's thinking level: the one its message asked for, else the agent's or the channel's."""
        return getattr(self, "_turn_effort", None) or effort_level(self.spec, channel)

    def _tool_loop(self, channel: str, msgs: list[dict], images_inline: bool = False) -> str:
        """Let an API agent call tools (agent_tools) until it answers in text."""
        import admin_api
        import agent_tools
        spec, system = self.spec, self._system(channel, dynamic=False)
        _call_ctx.agent, _call_ctx.channel = self.name, channel
        _call_ctx.turn = self.usage = {"calls": 0, "secs": 0.0, "out": 0, "prompt": 0, "cached": 0,
                                       "t0": time.time()}   # shown under the reply
        perm, allowed = spec.get("permissions", "read"), spec.get("allowed_commands") or []
        if getattr(self, "_stranger", False):
            perm, allowed = "none", []
            log.info("%s: turn in #%s started from Discord by someone other than the owner: chat tools only",
                     self.name, channel)
        import mcp_hub
        import memory
        tools, mcp_names = None, {}
        if spec.get("api_format") != "anthropic":
            mcp_specs, mcp_names = mcp_hub.tool_specs_for(self.name) if perm != "none" else ([], {})
            tools = agent_tools.tool_specs(perm, allowed, memory=memory.size(spec) > 0) + mcp_specs
            import games
            tools = tools + games.tool_specs(self.name)   # hosting a game is chat: it works whoever spoke last
            if perm != "none":   # what Senses lends this agent (a look through a camera): never on a stranger's turn
                import emotion_engine
                import senses
                tools = tools + senses.tool_specs(self.name) + emotion_engine.tool_specs(self.name)
            if spec.get("require_tool"):  # talk needs no lookup: answering is a tool too, and ends the turn
                tools = tools + [ANSWER_TOOL]
        env = dict(os.environ)
        env.update(admin_api.secrets_for(self.name))
        runner = agent_tools.ToolRunner(spec.get("cwd") or "~", perm, allowed, env,
                                        protect=str(Path(self.m.data_dir).resolve()),
                                        agent=self.name, channel=channel)
        runner.react = self._react
        runner.memory_size = memory.size(spec)
        self.tools = runner
        made: list[str] = []  # files MCP tools produced ([attach: path]); attached to the reply

        def with_files(reply: str) -> str:
            return reply + "".join(f"\n[attach: {p}]" for p in made if p not in reply)
        try:
            steps = max(1, int(spec.get("max_tool_steps") or MAX_TOOL_STEPS))
        except (TypeError, ValueError):
            steps = MAX_TOOL_STEPS
        streamer = self._streamer(channel)
        for step in range(steps):
            _trim_tool_output(msgs)
            think = {"i": None, "text": ""}
            said = {"text": "", "checked": time.monotonic()}

            def barge(said=said):
                """Mid-generation barge-in: stop streaming when a message for this agent is queued."""
                now = time.monotonic()
                if now - said["checked"] >= 1 and not self.halt.is_set():
                    said["checked"] = now
                    if self._queued(channel) and self._fresh(channel):
                        raise _BargeIn()

            def on_reason(text: str, think=think):
                think["text"] = text
                think["i"] = self._act(channel, {"k": "thinking", "text": text}, think["i"], force=False)
                barge()

            def on_text(text: str, said=said):
                said["text"] = text
                if streamer:
                    streamer(text)
                barge()
            try:  # "require_tool": the first step must call a tool (read before answering)
                reply = call_api_message(spec, system, msgs, tools,
                                         "required" if step == 0 and spec.get("require_tool") else None,
                                         on_text=on_text,
                                         effort=api_effort(self._effort(channel)),
                                         on_reason=on_reason)
            except _BargeIn:
                log.info("%s: new message mid-reply in #%s; reading it", self.name, channel)
                self._feel("interrupted", "Someone spoke while I was still answering")
                partial = re.sub(r"<think>.*?(</think>|$)", "", said["text"], flags=re.S).strip()
                if partial:
                    msgs.append({"role": "assistant", "content": partial + " …(cut off: a new message arrived)"})
                self._read_fresh(channel, msgs)
                continue
            except urllib.error.HTTPError as exc:
                # A 400 with inline images: let the caller retry without images first
                if images_inline or not tools or exc.code not in (400, 422):
                    raise
                log.info("%s: provider rejected tools (%s); answering without them", self.name, exc.code)
                tools = None
                continue
            if think["i"] is not None:  # the last rate-limited thinking update
                self._act(channel, {"k": "thinking", "text": think["text"]}, think["i"])
            if self.halt.is_set():
                return ""  # /stop arrived while the model was answering
            calls = reply.get("tool_calls") or []
            text = reply.get("content") or ""
            if not calls and tools and "<tool_call>" in text and not self.stop.is_set():
                # Tool-call markup as plain text: the server didn't parse it (often a tool name
                # we never declared, e.g. Claude Code's "read"/"grep"). Don't post it; correct it.
                names = ", ".join(t["function"]["name"] for t in tools)
                log.info("%s wrote tool calls as text; asking it to use its real tools", self.name)
                msgs.append({"role": "assistant", "content": text})
                msgs.append({"role": "user", "content": (
                    "(That reply had tool-call markup as plain text, so nothing ran and it was not "
                    f"posted. Your only tools are: {names}. Call them with those exact names, or "
                    "answer in plain text. There is no separate grep or read tool.)")})
                continue
            if not calls or self.stop.is_set():
                return with_files(text)
            if len(calls) == 1 and calls[0].get("function", {}).get("name") == "answer" and spec.get("require_tool"):
                try:
                    told = json.loads(calls[0]["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    told = {}
                said_text = str(told.get("text") or "").strip() if isinstance(told, dict) else ""
                if said_text:
                    return with_files(said_text)
            # keep reasoning_details: Gemini (via OpenRouter) needs its thought signatures back
            msgs.append({k: v for k, v in reply.items() if k in ("content", "tool_calls", "reasoning_details")}
                        | {"role": "assistant", "content": text, "tool_calls": calls})
            shown = re.sub(r"<think>.*?(</think>|$)", "", text, flags=re.S).strip()
            if shown:
                self._act(channel, {"k": "text", "text": shown})
            for c in calls:
                fn = c.get("function", {})
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                log.info("%s tool %s %s", self.name, fn.get("name"), str(args)[:120])
                args = args if isinstance(args, dict) else {}
                step_i = self._act(channel, {"k": "tool", "name": fn.get("name") or "?",
                                             "input": json.dumps(args, ensure_ascii=False)})
                if fn.get("name") in mcp_names:
                    import passphrase
                    srv, tool = mcp_names[fn["name"]]
                    if passphrase.configured() and passphrase.protected(tool, args):
                        # Opens part of the house: held until the passphrase is said (app.py runs it then).
                        passphrase.hold(channel, self.name, srv, tool, args)
                        log.info("%s: %s held for the passphrase", self.name, fn.get("name"))
                        out = ("held: this opens, closes or unlocks part of the house and needs the household "
                               "passphrase. Ask for the passphrase in one short sentence and stop. When it is "
                               "said, the action runs by itself; do not call this tool again.")
                        self._act(channel, {"k": "tool", "name": fn.get("name") or "?",
                                            "input": json.dumps(args, ensure_ascii=False), "out": out}, step_i)
                        msgs.append({"role": "tool", "tool_call_id": c.get("id", ""), "content": out})
                        continue
                    try:
                        out = mcp_hub.call_tool(srv, tool, args)
                        made += [p for p in ATTACH_RE.findall(out) if p not in made]  # e.g. generated images
                        out = _spill(self.name, fn["name"], out)
                    except BaseException as exc:  # connection errors arrive as ExceptionGroups
                        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                            raise
                        out = f"error: MCP server {srv['name']} failed: {str(exc)[:300]}"
                else:
                    out = runner.run(fn.get("name", ""), args)
                self._act(channel, {"k": "tool", "name": fn.get("name") or "?",
                                    "input": json.dumps(args, ensure_ascii=False), "out": str(out),
                                    "err": str(out).startswith("error")}, step_i)
                msgs.append({"role": "tool", "tool_call_id": c.get("id", ""), "content": out})
            self._read_fresh(channel, msgs)  # barge-in: messages sent while the tools ran
        # Out of steps: one last call without tools so the agent reports where it got to
        log.info("%s used all %d tool steps; asking for a summary", self.name, steps)
        msgs.append({"role": "user", "content": (
            f"(You have used all {steps} tool steps for this turn. Don't call tools. Post what you did, "
            "what you found, and exactly what is left, so the work can continue next turn.)")})
        return with_files(call_api_message(spec, system, msgs, None,
                                           effort=api_effort(self._effort(channel))).get("content") or
                          f"(stopped after {steps} tool steps without a summary)")

    def _cli_turn(self, channel: str) -> str:
        spec = self.spec
        state = self.m.state.setdefault(self.name, {"sessions": {}, "cursors": {}})
        session = state["sessions"].get(channel)
        cursor = state["cursors"].get(channel, 0)
        store = self.m.hub.store
        thread = self.thread
        if thread is not None:  # a thread: send the whole thread (parent + replies); cursors untouched
            new = [m for m in store.get_thread(thread) if m.get("type", "chat") == "chat"]
        else:
            new = store.get_since(cursor, channel=channel) if session else \
                store.get_recent(int(spec.get("context_messages") or 30), channel=channel)
            new = [m for m in new if m.get("type", "chat") == "chat" and m.get("sender") != self.name
                   and m.get("thread") is None and not (m.get("metadata") or {}).get("echo")]
        start = int(spec.get("context_from") or 0)
        new = [m for m in new if m["id"] >= start]
        if not new:
            return ""
        # A CLI agent's tools cannot be narrowed for one turn: it does not answer a turn that a Discord user
        # other than the owner started (the newest message, or the newest one naming it). API agents answer
        # those with chat tools only.
        named = next((m for m in reversed(new) if f"@{self.name}" in (m.get("text") or "").lower()), None)
        self._stranger = any(_discord_stranger((m or {}).get("sender", "")) for m in (new[-1], named))
        if self._stranger:
            if thread is None:
                state["cursors"][channel] = new[-1]["id"]
            return "(I only take requests from my owner here.)"
        transcript = "\n".join(f"#{m['id']} {m['sender']}: {self._text(m)}" for m in new)
        hint = ("\n\n(Attached files are on disk at the paths shown; open them with your file-reading "
                "tool to see them.)" if "[attached " in transcript else "")
        if thread is not None:
            prompt = (f"Thread in #{channel} on message #{thread}:\n\n{transcript}{hint}\n\n"
                      "Your reply to post in this thread:")
        else:
            prompt = f"New messages in #{channel}:\n\n{transcript}{hint}\n\nYour reply to post in #{channel}:"
        if channel in self.resuming:
            prompt = RESUME_NOTE + "\n\n" + prompt
        if channel in self.barged:
            self.barged.discard(channel)
            prompt = CLI_BARGE_NOTE + "\n\n" + prompt
        if spec["kind"] == "agy" and session:  # no system-prompt flag: restate the notes, they change
            import memory
            notes = memory.notes(self.name, memory.size(spec))
            if notes:
                prompt = "(Your long-term memory notes:\n" + "\n".join(f"- {n}" for n in notes) + ")\n\n" + prompt
        cwd = os.path.expanduser(spec.get("cwd") or str(Path.home()))
        import admin_api
        import mcp_hub
        cmd = cli_command(spec, prompt, session, self._system(channel),
                          protect=str(Path(self.m.data_dir).resolve()),
                          mcp=mcp_hub.claude_config(self.name, self.m.data_dir) if spec["kind"] == "claude" else (None, []),
                          effort=effort_level(spec, channel))
        env = dict(os.environ)
        env.update(admin_api.secrets_for(self.name))  # only the secrets granted to this agent
        self.proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     stdin=subprocess.DEVNULL, text=True, env=env,
                                     start_new_session=True)  # own process group: /stop kills its children too
        streamed = None
        try:
            if spec["kind"] == "claude":
                def on_posted(sid: str | None):
                    """A reply is out: a kill after this must not hand the same messages over again."""
                    if thread is None:
                        state["cursors"][channel] = max(m["id"] for m in new)
                    if sid:
                        state["sessions"][channel] = sid
                    self.m.save_state()
                streamed, stderr = self._claude_stream(channel, on_posted, since=max(m["id"] for m in new))
                stdout = ""
            else:
                stdout, stderr = self.proc.communicate(timeout=CLI_TIMEOUT)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            stdout, stderr = self.proc.communicate()
        finally:
            code, self.proc = self.proc.returncode, None
        if self.stop.is_set():
            return ""  # agent was removed mid-turn: post nothing
        if self.halt.is_set():  # /stop: don't hand the stopped request over again next turn
            if thread is None:
                state["cursors"][channel] = max(m["id"] for m in new)
                self.m.save_state()
            return ""
        text, new_session = streamed if streamed else parse_cli_output(stdout)
        if self.__dict__.pop("_cli_barged", False):  # interrupted for a new message: its queued turn resumes
            if thread is None:
                state["cursors"][channel] = max(m["id"] for m in new)
            if new_session:
                state["sessions"][channel] = new_session
            self.m.save_state()
            self.barged.add(channel)
            return ""
        if code != 0 and not text and not (streamed and self.__dict__.get("_cli_posted")):
            raise RuntimeError((stderr or stdout or f"exit {code}").strip()[-300:])
        if thread is None:
            state["cursors"][channel] = max(m["id"] for m in new)
        if new_session:
            state["sessions"][channel] = new_session
        self.m.save_state()
        return text


    def _queued(self, channel: str) -> bool:
        """Is a trigger for this channel (and thread) waiting in the queue file? Read, not consumed."""
        try:
            lines = (Path(self.m.data_dir) / f"{self.name}_queue.jsonl").read_text("utf-8").splitlines()
        except OSError:
            return False
        for line in lines:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if isinstance(e, dict) and e.get("channel", "general") == channel and e.get("thread") == self.thread:
                return True
        return False

    def _claude_stream(self, channel: str, on_posted=None, since: int | None = None) -> tuple[tuple[str, str | None], str]:
        """Read Claude Code's stream-json events as they come. Steps go to the live activity view.
        Each reply is posted as soon as it is complete, even while the process keeps running:
        with background subagents Claude Code holds every "result" event until it exits, so a
        reply counts as complete when its message ends the turn (stop_reason end_turn in the
        session transcript) or a new message starts. The process is killed after CLI_TIMEOUT
        without output, or CLI_MAX in all. -> ((final reply not yet posted, session id), stderr)."""
        proc = self.proc
        err: list[str] = []
        threading.Thread(target=lambda: err.extend(proc.stderr), daemon=True).start()  # never blocks stdout
        lines: queue.Queue = queue.Queue()

        def pump():
            for line in proc.stdout:
                lines.put(line)
            lines.put(None)
        threading.Thread(target=pump, daemon=True).start()
        t0 = last = time.monotonic()
        tools: dict[str, int] = {}
        session, results, posted = None, [], []
        pending = None  # (message id, text): a reply that may still continue with a tool call
        self._cli_posted = False
        self._cli_barged = False

        def post(text: str):
            if not text or text == NO_REPLY or text in posted or self.halt.is_set() or self.stop.is_set():
                return
            last_step = self.activity[-1] if self.activity else None
            if last_step and last_step.get("k") == "text" and last_step.get("text") == text:
                self.activity.pop()  # the reply itself is the message, not a step
            self._deliver(channel, text)
            posted.append(text)
            self._cli_posted = True
            if on_posted:
                on_posted(session)
            self._act_done(channel)
            self._act_reset()
            self._act(channel, {"k": "status", "text": "Replied; still working in the background"})

        while True:
            try:
                line = lines.get(timeout=1)
            except queue.Empty:
                line = ""
            if line is None:
                break
            now = time.monotonic()
            if line:
                last = now
            elif (now - last > CLI_TIMEOUT or now - t0 > CLI_MAX) and proc.poll() is None:
                log.warning("%s: no output for %d s (or over %d s in all); killing the CLI",
                            self.name, CLI_TIMEOUT, CLI_MAX)
                proc.kill()
            if pending and session and _turn_ended(session, pending[0]):
                post(pending[1])
                pending = None
            if not line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if not isinstance(ev, dict):
                continue
            session = ev.get("session_id") or session
            sub = ev.get("parent_tool_use_id") is not None  # a step inside a subagent
            msg = ev.get("message") or {}
            content = msg.get("content")
            if ev.get("type") == "assistant" and isinstance(content, list):
                mid = msg.get("id") or ""
                if pending and not sub and mid != pending[0]:
                    post(pending[1])  # a new message began: the last one was a finished reply
                    pending = None
                for b in content:
                    if b.get("type") == "thinking" and (b.get("thinking") or "").strip():
                        self._act(channel, {"k": "thinking", "text": b["thinking"], "sub": sub})
                    elif b.get("type") == "text" and b.get("text", "").strip() and not sub:
                        self._act(channel, {"k": "text", "text": b["text"].strip()})
                        pending = (mid, b["text"].strip())
                    elif b.get("type") == "tool_use":
                        if not sub and pending and pending[0] == mid:
                            pending = None  # text before a tool call is a progress note
                        tools[b.get("id", "")] = self._act(channel, {
                            "k": "tool", "name": b.get("name") or "?", "sub": sub,
                            "input": json.dumps(b.get("input") or {}, ensure_ascii=False)})
            elif ev.get("type") == "user" and isinstance(content, list):
                for b in content:
                    i = tools.get(b.get("tool_use_id", "")) if b.get("type") == "tool_result" else None
                    if i is None or i >= len(self.activity):
                        continue
                    out = b.get("content")
                    if isinstance(out, list):
                        out = "\n".join(x.get("text", "") for x in out if isinstance(x, dict))
                    self._act(channel, {**self.activity[i], "out": str(out or ""),
                                        "err": bool(b.get("is_error"))}, i)
                # Barge-in: a message for this agent arrived. Stop at this tool boundary; the
                # queued turn resumes the session with it. Not once a reply is out (background work).
                if (since is not None and not sub and not self._cli_posted and not self.__dict__.get("_cli_barged")
                        and self._queued(channel) and self._fresh(channel, since)):
                    log.info("%s: new message mid-turn in #%s; interrupting to read it", self.name, channel)
                    self._cli_barged = True
                    self._act(channel, {"k": "status", "text": "New message: pausing to read it"})
                    kill_group(proc)
            elif ev.get("type") == "result":
                text = (ev.get("result") or "").strip() if isinstance(ev.get("result"), str) else ""
                last_step = self.activity[-1] if self.activity else None
                if last_step and last_step.get("k") == "text" and last_step.get("text") == text:
                    self.activity.pop()
                if pending and pending[1] == text:
                    pending = None
                results.append(text)
                try:
                    proc.wait(timeout=1.5)
                except subprocess.TimeoutExpired:  # still running: background tasks. Post now.
                    post(text)
        proc.wait()
        if pending:
            results.append(pending[1])  # killed or ended before a result event for it
        real = [t for t in results if t and t != NO_REPLY]
        final = results[-1] if results else ""
        if (not final or final == NO_REPLY) and real and real[-1] not in posted:
            final = real[-1]  # a background-task wake-up ended on "(no reply)": keep the real answer
        if final in posted:
            final = ""
        return (final, session), "".join(err)


LLM_RESOLVED = ("base_url", "api_format", "api_key", "model", "llm_error")  # from the agent's LLM server


def _linked(spec: dict) -> bool:
    """An API agent whose endpoint comes from Settings > LLMs (linked, or waiting for a server)."""
    return spec.get("kind") == "api" and bool(spec.get("llm") or spec.get("needs_llm"))


def _after_replies(history: list[dict], name: str) -> list[dict]:
    """Move a message sent while `name` was writing a reply (its id is between the reply's
    metadata "seen" and the reply) to just after that reply: the reply never saw it, and the
    context must not read as if it answered it."""
    replies = [r for r in history if r["sender"] == name and (r.get("metadata") or {}).get("seen") is not None]

    def key(m: dict) -> tuple[int, int]:
        if m["sender"] != name:
            for r in replies:
                if r["metadata"]["seen"] < m["id"] < r["id"]:
                    return (r["id"], 1)
        return (m["id"], 0)
    return sorted(history, key=key)


def _discord_stranger(sender: str) -> bool:
    """A Discord sender who is not the owner (discord_gateway labels the owner "(Discord, owner)")."""
    return "(Discord" in sender and not sender.endswith(", owner)")


def _turn_ended(session: str, message_id: str) -> bool:
    """True when Claude Code's session transcript shows message_id ended its turn (not a tool call)."""
    if not message_id:
        return False
    for f in Path.home().glob(f".claude/projects/*/{session}.jsonl"):
        try:
            with open(f, "rb") as fh:
                fh.seek(max(0, f.stat().st_size - 512 * 1024))
                tail = fh.read().decode("utf-8", "replace")
        except OSError:
            continue
        return any(message_id in ln and '"stop_reason":"end_turn"' in ln for ln in tail.splitlines())
    return False


class AgentManager:
    def __init__(self, hub, data_dir: str):
        self.hub = hub
        self.data_dir = data_dir
        self.path = Path(data_dir) / "managed_agents.json"
        self.state_path = Path(data_dir) / "managed_agents_state.json"
        global METRICS_PATH, _effort_path
        _effort_path = Path(data_dir) / "effort.json"
        METRICS_PATH = Path(data_dir) / "metrics.jsonl"
        import profiles
        import llm_servers
        profiles.data_dir = Path(data_dir)
        llm_servers.data_dir = Path(data_dir)
        self.lock = threading.Lock()
        self.specs: dict[str, dict] = {}
        self.state: dict[str, dict] = {}
        self.runners: dict[str, Runner] = {}
        # Channel membership for every agent (managed or not): #general holds
        # everyone; other channels hold only the agents added to them.
        self.members_path = Path(data_dir) / "channel_members.json"
        self.members: dict[str, list[str]] = {}
        if self.members_path.exists():
            self.members = json.loads(self.members_path.read_text("utf-8"))
        if self.path.exists():
            self.specs = {s["name"]: s for s in json.loads(self.path.read_text("utf-8"))}
            # Personas live in each agent's profile folder (profiles.py). Older files kept them
            # in managed_agents.json: move them over once, keeping a copy of the old file.
            old = any(s.get("persona") for s in self.specs.values())
            for s in self.specs.values():
                persona = profiles.read(s["name"], "persona.md") or s.get("persona")
                if persona:
                    s["persona"] = persona
                else:
                    s.pop("persona", None)
            if old:
                self._write(self.path.with_suffix(".json.bak-before-profiles"),
                            json.loads(self.path.read_text("utf-8")))
                self.save()
            # LLM servers (llm_servers.py): API agents link to a server instead of carrying
            # their own endpoint and key. Move them over once, keeping a copy of the old file.
            before = json.loads(self.path.read_text("utf-8"))
            if llm_servers.migrate(self.specs):
                self._write(self.path.with_suffix(".json.bak-before-llm-servers"), before)
                self.save()
        self.refresh_llm()
        if self.state_path.exists():
            self.state = json.loads(self.state_path.read_text("utf-8"))

    def _write(self, path: Path, data):
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)

    def save(self):
        """Settings go to managed_agents.json; each persona goes to the agent's profile folder.
        A linked API agent's endpoint, key and model come from its LLM server: not saved."""
        import profiles
        for s in self.specs.values():
            profiles.write(s["name"], "persona.md", s.get("persona") or "")
        self._write(self.path, [{k: v for k, v in s.items() if k != "persona"
                                 and not (_linked(s) and k in LLM_RESOLVED)} for s in self.specs.values()])

    def refresh_llm(self, names=None):
        """Fill each linked API agent's endpoint, format, key and model from its server (in memory).
        A link to a server that no longer exists takes the agent offline until it is edited."""
        changed = False
        for s in [self.specs[n] for n in (list(self.specs) if names is None else names) if n in self.specs]:
            changed |= self._resolve_into(s)
        if changed:
            self.save()

    @staticmethod
    def _resolve_into(s: dict) -> bool:
        """Resolve first, then update in place: running turns read these fields, so they are
        never removed while a value is being looked up. -> True when the agent went offline."""
        import llm_servers
        if not _linked(s):
            return False
        r = llm_servers.resolve(s)
        went_offline = False
        if r.get("llm_error") == "needs an LLM server" and (not s.get("needs_llm") or s.get("enabled", True)):
            s["needs_llm"], s["enabled"] = True, False  # the link stays: re-adding the server restores it
            went_offline = True
        for k in LLM_RESOLVED:
            if k not in r:
                s.pop(k, None)
        s.update(r)
        return went_offline

    def linked(self, server_id: str) -> list[str]:
        return [n for n, s in self.specs.items() if s.get("llm") == server_id]

    def unlink_llm(self, server_id: str) -> list[str]:
        """Settings > LLMs > Unlink agents: each goes offline until it is linked to a server again."""
        names = self.linked(server_id)
        with self.lock:
            for n in names:
                s = self.specs[n]
                for k in ("llm", "llm_model") + LLM_RESOLVED:
                    s.pop(k, None)
                s["needs_llm"], s["enabled"] = True, False
                s["llm_error"] = "needs an LLM server"
            if names:
                self.save()
        for n in names:
            self._apply(n)
        return names

    def save_state(self):
        with self.lock:
            self._write(self.state_path, self.state)

    def _name_taken(self, name: str) -> bool:
        """In use as an agent, or as a registry family that still has members (a renamed
        agent keeps its original family; reusing that name would renumber it)."""
        reg = self.hub.registry
        return bool(name in self.specs or reg.get_instance(name) or reg.get_instances_for(name))

    def set_inflight(self, name: str, channel: str, on: bool):
        with self.lock:
            st = self.state.setdefault(name, {"sessions": {}, "cursors": {}})
            chans = [c for c in st.get("inflight", []) if c != channel] + ([channel] if on else [])
            if chans:
                st["inflight"] = chans
            else:
                st.pop("inflight", None)
        self.save_state()

    def in_channel(self, name: str, channel: str) -> bool:
        return channel == "general" or name in self.members.get(channel, [])

    def channels_of(self, name: str) -> list[str]:
        return ["general"] + sorted(ch for ch, names in self.members.items() if name in names and ch != "general")

    def set_member(self, name: str, channel: str, on: bool):
        if channel == "general":
            return  # everyone is always in #general
        with self.lock:
            names = [n for n in self.members.get(channel, []) if n != name]
            if on:
                names.append(name)
            self.members[channel] = names
            self._write(self.members_path, self.members)

    def forget_member(self, name: str):
        for ch in list(self.members):
            self.set_member(name, ch, False)

    @staticmethod
    def public(spec: dict) -> dict:
        out = {k: v for k, v in spec.items() if k != "api_key"}
        out["has_api_key"] = bool(spec.get("api_key"))
        return out

    def list(self) -> list[dict]:
        return [dict(self.public(s), running=s["name"] in self.runners,
                     channels=[c for c in self.channels_of(s["name"]) if c != "general"])
                for s in self.specs.values()]

    def validate(self, spec: dict) -> str | None:
        import re
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", spec.get("name", "")):
            return "name: 1-32 chars, lowercase letters, digits, - or _"
        if spec.get("kind") not in KINDS:
            return f"kind must be one of {KINDS}"
        if not isinstance(spec.get("allowed_commands", []), list) or not all(
                re.fullmatch(r"[A-Za-z0-9._+-]{1,64}", str(c)) for c in spec.get("allowed_commands", [])):
            return "allowed commands: program names only (letters, digits, . _ + -)"
        if not isinstance(spec.get("channels", []), list):
            return "channels must be a list of channel names"
        if spec.get("permissions", "read") not in PERMISSIONS:
            return f"permissions must be one of {PERMISSIONS}"
        if spec.get("permissions") == "none" and spec.get("kind") != "api":
            return "No tools is for API agents; CLI agents bring their own tools (use read)"
        if spec["name"] not in self.specs and self._name_taken(spec["name"]):
            return f"an agent named '{spec['name']}' is already connected; pick another name"
        if spec["kind"] == "api":
            import llm_servers
            if not spec.get("llm") or not llm_servers.get(spec["llm"]):
                if spec.get("enabled", True) or spec.get("llm"):
                    return "pick an LLM server for this agent (Settings > LLMs)"
        if spec["kind"] != "api" and not _which(spec.get("command") or spec["kind"]):
            return f"'{spec.get('command') or spec['kind']}' is not installed on this machine"
        return None

    def upsert(self, spec: dict) -> str | None:
        src = spec.pop("copy_key_from", None)
        if src:
            spec.pop("persona", None)  # duplicates get their own persona
        spec = {k: v for k, v in spec.items() if k not in ("running", "has_api_key", "duplicateOf")}
        old = self.specs.get(spec.get("name", "")) or self.specs.get(src or "", {})
        if not spec.get("api_key") and old.get("api_key"):
            spec["api_key"] = old["api_key"]  # blank in the form = keep the stored key
        if not src:  # server-managed fields the edit form never sends back
            for k in ("spawned", "context_from"):
                if k in old and k not in spec:
                    spec[k] = old[k]
        if "emotion" in spec or "emotion" in old:   # a form from before the emotion engine keeps the stored one
            import emotion_engine
            spec["emotion"] = emotion_engine.clean(spec["emotion"] if "emotion" in spec else old["emotion"])
        if "initiative" in spec or "initiative" in old:
            import initiative
            spec["initiative"] = initiative.clean(spec["initiative"] if "initiative" in spec else old["initiative"])
        spec.setdefault("permissions", "read")
        if spec.get("kind") == "api":
            import llm_servers
            if not spec.get("llm") and spec.get("base_url"):  # endpoint fields (older form): link it
                llm_servers.migrate({spec["name"]: spec})
            for k in ("base_url", "api_format", "api_key"):
                spec.pop(k, None)
            if spec.get("llm"):
                spec.pop("needs_llm", None)
                spec.pop("llm_error", None)
        err = self.validate(spec)
        if err:
            return err
        chans = spec.pop("channels", None)  # membership lives in channel_members.json
        self._resolve_into(spec)  # before it is visible to running turns
        self.specs[spec["name"]] = spec
        self.save()
        if chans is not None:
            for ch in set(self.members) | set(chans):
                self.set_member(spec["name"], ch, ch in chans)
        self._apply(spec["name"])
        return None

    def spawn(self, source: str, channel: str, name: str | None = None) -> str:
        """New instance of a managed agent, pinned to `channel`, with fresh context:
        a new CLI session, or (API agents) only messages from now on. -> status text."""
        src = self.specs.get(source.lstrip("@").lower())
        if not src:
            return (f"No agent '{source}' runs on this server. Spawnable: "
                    + (", ".join(sorted(self.specs)) or "none; add one in Settings > Agents"))
        if not name:
            n = 2
            while self._name_taken(f"{src['name']}-{n}"):
                n += 1
            name = f"{src['name']}-{n}"
        # persona and long-term memory are per instance: the new one starts without them
        spec = {k: v for k, v in src.items() if k not in ("label", "channels", "persona")}
        spec.update(name=name.lstrip("@").lower(), channels=[channel], enabled=True, spawned=True,
                    context_from=self.hub.store.last_id + 1)
        err = self.upsert(spec)
        return err or (f"Spawned @{spec['name']} (new instance of @{src['name']}) in #{channel}, "
                       f"starting with fresh context. Remove it with /despawn {spec['name']}.")

    def set_persona(self, name: str, text: str):
        if name in self.specs:
            self.specs[name]["persona"] = text[:1200]
            with self.lock:
                self.save()

    def fresh(self, name: str) -> str:
        """New context, same persona: drop CLI sessions, start the conversation now."""
        name = name.lstrip("@").lower()
        if name not in self.specs:
            return f"No agent '{name}' runs on this server."
        with self.lock:
            self.state.pop(name, None)
            self.specs[name]["context_from"] = self.hub.store.last_id + 1
            self.save()
        self.save_state()
        return f"@{name} starts with a fresh context (its persona is kept)."

    def rename(self, old: str, wanted: str) -> str:
        """Change an agent's @handle everywhere; persona, memory and channels move with it."""
        old = old.lstrip("@").lower()
        label = wanted.strip().lstrip("@")[:40]
        new = re.sub(r"[^a-z0-9_-]", "", label.lower().replace(" ", "-"))[:32]
        import mcp_bridge
        if old not in self.specs:
            return f"No agent '{old}' runs on this server."
        if not new or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", new):
            return f"'{wanted}' can't be a handle (lowercase letters, digits, - or _)."
        if new == old:
            return f"@{old} already has that handle."
        taken = (self._name_taken(new)
                 or new in {"user", "system", "all", "both", "everyone", "here", "me"}
                 or new == str(getattr(self.hub, "room_settings", {}).get("username", "")).lower())
        if taken:
            return f"@{new} is taken; @{old} keeps its handle."
        runner = self.runners.pop(old, None)
        if runner:
            runner.stop.set()  # keep the registry instance: it is renamed below
        mcp_bridge.migrate_identity(old, new)
        result = self.hub.registry.rename(old, new, label)
        if isinstance(result, str) and self.hub.registry.get_instance(old):
            if runner:
                self.runners[old] = Runner(self, old)
                self.runners[old].start()
            return f"Rename failed: {result}"
        self.hub.store.rename_sender(old, new)
        import admin_api
        import rulebooks
        admin_api.rename_grants(old, new)
        rulebooks.rename_agent(old, new)
        import profiles
        profiles.rename(old, new)
        with self.lock:
            spec = self.specs.pop(old)
            spec.update(name=new, label=label)
            self.specs[new] = spec
            if old in self.state:
                self.state[new] = self.state.pop(old)
            for names in self.members.values():
                if old in names:
                    names[names.index(old)] = new
            self.save()
            self._write(self.members_path, self.members)
        self.save_state()
        (Path(self.data_dir) / f"{old}_queue.jsonl").unlink(missing_ok=True)
        if spec.get("enabled", True):
            self.runners[new] = Runner(self, new)
            self.runners[new].start()
        return f"@{old} is now @{new} ({label})."

    def channel_deleted(self, channel: str) -> list[str]:
        """Drop the channel's membership and sessions. Spawned instances that lived only
        there are deleted (runner stopped, CLI process killed). -> deleted names."""
        doomed = [n for n in self.members.get(channel, [])
                  if self.specs.get(n, {}).get("spawned") and self.channels_of(n) == ["general", channel]]
        for name in doomed:
            self.delete(name)
        with self.lock:
            self.members.pop(channel, None)
            self._write(self.members_path, self.members)
            for st in self.state.values():
                st.get("sessions", {}).pop(channel, None)
                st.get("cursors", {}).pop(channel, None)
        self.save_state()
        return doomed

    def channel_renamed(self, old: str, new: str):
        with self.lock:
            if old in self.members:
                self.members[new] = self.members.pop(old)
                self._write(self.members_path, self.members)
            for st in self.state.values():
                for key in ("sessions", "cursors"):
                    if old in st.get(key, {}):
                        st[key][new] = st[key].pop(old)
        self.save_state()

    def despawn(self, name: str) -> str:
        name = name.lstrip("@").lower()
        spec = self.specs.get(name)
        if not spec:
            return f"No agent '{name}' to remove."
        if not spec.get("spawned"):
            return f"@{name} wasn't created with /spawn; remove it in Settings > Agents."
        self.delete(name)
        return f"Removed @{name}."

    def delete(self, name: str):
        import admin_api
        import rulebooks
        admin_api.rename_grants(name, None)
        rulebooks.rename_agent(name, None)
        import profiles
        profiles.rename(name, None)
        self.forget_member(name)
        self.specs.pop(name, None)
        self.state.pop(name, None)
        self.save()
        self.save_state()
        self._apply(name)

    def _apply(self, name: str):
        """Start/stop the runner to match the spec. Field edits need no restart."""
        spec = self.specs.get(name)
        want = bool(spec and spec.get("enabled", True))
        runner = self.runners.get(name)
        if runner and not want:
            self.runners.pop(name).shutdown()
        elif want and not runner:
            self.runners[name] = Runner(self, name)
            self.runners[name].start()

    def start_all(self):
        for name in list(self.specs):
            self._apply(name)
