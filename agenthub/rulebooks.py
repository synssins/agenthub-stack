"""Rulebooks: named, long-form guides for kinds of work (story writing, code &
processes, docs, ...). Attach one to channels (everyone there follows it) and/or
agents (they follow it everywhere). Attached rulebooks go into the agent's prompt
in full; the rest stay out of the prompt until an agent opens one with the rulebook
tool. Access is open to every agent unless fine-grained access is on (see visible()).

Stored in data/rulebooks.json; each is mirrored to <root>/rulebooks/<slug>.md,
outside the protected data folder, so CLI agents can read it with their file tools.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path

_lock = threading.Lock()
data_dir = Path("./data")
md_dir = Path("./rulebooks")

STARTERS = [
    {"name": "Writing (general)", "description": "Plain, human prose for anything people read.",
     "text": """- Say the point first. One idea per sentence; cut words that carry nothing.
- Plain words over impressive ones (use, not leverage; help, not empower).
- No AI tells: em-dashes, "delve", "seamless", "robust", "in summary", rule-of-three padding,
  stock openers and closers. Run `slop-chop check` on anything long before handing it over.
- Concrete over abstract: numbers, names, examples.
- Active voice; the reader is "you".
- Don't hedge what you know; say plainly what you don't."""},
    {"name": "Story writing", "description": "Fiction: voice, scene, dialogue.",
     "text": """- Show through action, dialogue and detail; tell only to move time along.
- Each scene wants something and ends changed. Cut scenes that don't.
- Specific sensory detail over adjectives. One strong detail beats three weak ones.
- Dialogue: people interrupt, dodge, and talk past each other. No speeches that explain the plot.
- Keep point of view steady within a scene.
- Vary sentence length on purpose: short for impact, longer for flow.
- Avoid stock phrasing ("a chill ran down her spine", "little did he know") and AI tells."""},
    {"name": "Code & processes", "description": "Building software and workflows: standard first, bespoke last.",
     "text": """- Bespoke is bad unless absolutely necessary. Before building anything, check in order:
  is it needed at all; does the codebase already have it; does the standard library or
  platform do it; does an installed dependency do it. Build custom only when all four fail.
- Smallest change that fully solves the problem. No speculative options, layers or abstractions.
- Follow the conventions already in the codebase over your preferences.
- Every non-trivial change ships with a test or a check that proves it works.
- Processes: reuse an existing tool or workflow before inventing one. Document the why, not the what.
- Say what you skipped and what you did not verify."""},
]


def setup(cfg: dict, root: Path):
    global data_dir, md_dir
    data_dir = Path(cfg.get("server", {}).get("data_dir", "./data"))
    md_dir = Path(root) / "rulebooks"
    if not (data_dir / "rulebooks.json").exists():
        save([dict(s, channels=[], agents=[]) for s in STARTERS])


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "rulebook"


def load() -> list[dict]:
    try:
        return json.loads((data_dir / "rulebooks.json").read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return []


def save(books: list[dict]):
    with _lock:
        data_dir.mkdir(parents=True, exist_ok=True)
        tmp = data_dir / "rulebooks.tmp"
        tmp.write_text(json.dumps(books, indent=2), "utf-8")
        os.replace(tmp, data_dir / "rulebooks.json")
        md_dir.mkdir(parents=True, exist_ok=True)
        keep = set()
        for b in books:
            p = md_dir / f"{slug(b['name'])}.md"
            p.write_text(f"# {b['name']}\n\n{b.get('description', '')}\n\n{b.get('text', '')}\n", "utf-8")
            keep.add(p.name)
        for p in md_dir.glob("*.md"):
            if p.name not in keep:
                p.unlink()


def path_of(b: dict) -> str:
    return str((md_dir / f"{slug(b['name'])}.md").resolve())


def find(name: str) -> dict | None:
    want = slug(name)
    return next((b for b in load() if slug(b["name"]) == want or b["name"].lower() == name.lower()), None)


def fine_grained() -> bool:
    """Advanced setting. Off (default): every agent can open every rulebook. On: an agent can
    open only rulebooks that name it as a reader, attach to it, or attach to its channel."""
    try:
        return bool(json.loads((data_dir / "rulebook_access.json").read_text("utf-8")).get("fine_grained"))
    except (OSError, ValueError, AttributeError):
        return False


def set_fine_grained(on: bool):
    with _lock:
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "rulebook_access.json").write_text(json.dumps({"fine_grained": bool(on)}), "utf-8")


def visible(agent: str, channel: str = "") -> list[dict]:
    books = load()
    if not fine_grained():
        return books
    return [b for b in books if agent in b.get("readers", []) or agent in b.get("agents", [])
            or (channel and channel in b.get("channels", []))]


def lookup(agent: str, query: str, channel: str = "") -> str:
    """The rulebook tool: one match returns its text; several ask the agent to check with the user."""
    books = visible(agent, channel)
    if not books:
        return "No rulebooks are available to you."
    listing = lambda bs: "\n".join(f"- {b['name']}: {b.get('description', '')}" for b in bs)
    q = (query or "").strip().lower()
    if not q:
        return "Rulebooks:\n" + listing(books)
    hits = ([b for b in books if b["name"].lower() == q or slug(b["name"]) == slug(q)]
            or [b for b in books if all(w in f"{b['name']} {b.get('description', '')}".lower() for w in q.split())])
    if len(hits) == 1:
        return f"Rulebook \"{hits[0]['name']}\" (follow it):\n{hits[0].get('text', '')}"
    if hits:
        return "Several rulebooks match. Ask the user which one they mean:\n" + listing(hits)
    return f"No rulebook matches \"{query}\". Available:\n" + listing(books)


def prompt_for(agent: str, channel: str) -> str:
    """Prompt section: attached rulebooks in full; the rest only as a one-line pointer to the
    rulebook tool, so they cost no context until someone asks for one."""
    books = visible(agent, channel)
    on = [b for b in books if agent in b.get("agents", []) or channel in b.get("channels", [])]
    parts = [f"Rulebook \"{b['name']}\" (follow it here):\n{b.get('text', '')}" for b in on]
    if len(on) < len(books):
        parts.append("More rulebooks (guides for kinds of work) are available on request. Only when the user "
                     "asks you to use or follow one, call the rulebook tool (chat_rulebook on the chat MCP "
                     "server) with its name or a keyword; an empty query lists them. If several match, "
                     "ask the user which one they mean."
                     + ("" if fine_grained() else f" Without that tool, they are .md files in {md_dir.resolve()}."))
    return "\n\n".join(parts)


def rename_agent(old: str, new: str | None):
    books = load()
    if not any(old in b.get("agents", []) for b in books):
        return
    for b in books:
        a = [n for n in b.get("agents", []) if n != old]
        if new and old in b.get("agents", []):
            a.append(new)
        b["agents"] = a
    save(books)


def review_message(book: dict, targets: list[str], body: str) -> str:
    who = " ".join(f"@{t}" for t in targets)
    return (f"{who} Please review the following against the rulebook \"{book['name']}\" "
            f"({path_of(book)}). List concrete issues: quote the passage, name the rule it breaks, "
            f"and suggest a fix. Don't rewrite the whole thing unless asked.\n\n{body}").strip()
