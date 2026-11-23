"""One profile folder per agent: data/agents/<name>/ holds what belongs to the agent itself
and outlives its sessions.

  persona.md   the agent's own identity (it writes this itself; editable on its page)
  memory.txt   its long-term notes (memory.py)
  emotion.json its feelings right now, when the emotion engine is on for it (emotion_engine.py)

The folder moves with a rename and is removed when the agent is deleted. It is inside
AgentHub's data folder, so agents' file tools cannot reach it: they change it through
their persona and memory tools only.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

data_dir = Path("./data")   # set by AgentManager


def folder(name: str, make: bool = False) -> Path:
    d = data_dir / "agents" / re.sub(r"[^a-z0-9_-]", "_", name.lower())
    if make:
        d.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        d.mkdir(exist_ok=True, mode=0o700)
    return d


def write(name: str, fname: str, text: str):
    """Replace one profile file; empty text removes it."""
    if not text.strip():
        (folder(name) / fname).unlink(missing_ok=True)
        return
    p = folder(name, make=True) / fname
    tmp = p.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text.rstrip("\n") + "\n")
    os.replace(tmp, p)


def read(name: str, fname: str) -> str | None:
    try:
        return (folder(name) / fname).read_text("utf-8").strip()
    except OSError:
        return None


def rename(old: str, new: str | None):
    """Move a profile to the agent's new name (None: the agent was deleted)."""
    try:   # Discord bots linked to the agent follow the new name
        import discord_gateway
        discord_gateway.rename_agent(old, new)
    except Exception:
        pass
    src = folder(old)
    if not src.is_dir():
        return
    if new and not folder(new).exists():
        os.replace(src, folder(new))
    else:
        shutil.rmtree(src, ignore_errors=True)
