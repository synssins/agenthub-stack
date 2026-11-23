"""Long-term memory per agent: short notes an agent saves about the owner, projects and
decisions. They follow the agent across channels, restarts and context resets.

One text file in the agent's profile folder (profiles.py: data/agents/<name>/memory.txt),
one note per line, oldest first.
The file is capped in bytes (spec "memory_size", 0 = off); when a new note does not fit,
the oldest notes are dropped. The owner can read and edit the notes on the agent's page.
"""

from __future__ import annotations

import re
import threading

import profiles

DEFAULT_SIZE = 4096
MAX_SIZE = 16384
MAX_NOTE = 500      # chars per note
_lock = threading.Lock()


def size(spec: dict) -> int:
    """The agent's cap in bytes (0 = memory off)."""
    try:
        v = spec.get("memory_size")
        return max(0, min(MAX_SIZE, DEFAULT_SIZE if v is None or v == "" else int(v)))
    except (TypeError, ValueError):
        return DEFAULT_SIZE


def _fit(lines: list[str], cap: int) -> list[str]:
    """Drop the oldest notes until the rest fits in cap bytes."""
    lines = [l for l in lines if l]
    while lines and len("\n".join(lines).encode("utf-8")) > cap:
        lines.pop(0)
    return lines


def notes(name: str, cap: int = MAX_SIZE) -> list[str]:
    try:
        lines = [l.strip() for l in (profiles.read(name, "memory.txt") or "").splitlines()]
    except UnicodeDecodeError:
        return []
    return _fit(lines, cap)  # a file edited by hand never reaches the model over the cap


def _save(name: str, lines: list[str]):
    profiles.write(name, "memory.txt", "\n".join(lines))


def write(name: str, text: str, cap: int = MAX_SIZE) -> list[str]:
    """Replace all notes (the owner's edit). -> the notes kept."""
    with _lock:
        lines = _fit(list(dict.fromkeys(re.sub(r"\s+", " ", l).strip()[:MAX_NOTE]
                                        for l in (text or "").splitlines())), cap)
        _save(name, lines)
    return lines


def add(name: str, text: str, cap: int) -> str:
    note = re.sub(r"\s+", " ", text or "").strip()[:MAX_NOTE]
    if cap <= 0:
        return "error: memory is turned off for you"
    if not note:
        return "error: nothing to remember"
    with _lock:
        lines = notes(name, cap)
        if note.lower() in (l.lower() for l in lines):
            return "already in your memory"
        kept = _fit(lines + [note], cap)
        _save(name, kept)
    dropped = len(lines) + 1 - len(kept)
    return "saved to your memory" + (f" ({dropped} oldest notes dropped to make room)" if dropped else "")


def forget(name: str, text: str) -> str:
    """Remove every note that contains text (case-insensitive)."""
    needle = re.sub(r"\s+", " ", text or "").strip().lower()
    if not needle:
        return "error: say which note to forget (a few words from it)"
    with _lock:
        lines = notes(name)
        kept = [l for l in lines if needle not in l.lower()]
        _save(name, kept)
    n = len(lines) - len(kept)
    return f"removed {n} note{'s' if n != 1 else ''} from your memory" if n else "no note contains that text"


def prompt(name: str, cap: int, tools: bool) -> str:
    """The memory section of an agent's system prompt ("" when memory is off)."""
    if cap <= 0:
        return ""
    how = ("call the remember tool; drop an outdated note with forget_memory (a few words from it)" if tools else
           "put [remember]the fact[/remember] in your reply; drop an outdated note with "
           "[forget]a few words from it[/forget]. Both are removed from the posted message")
    lines = notes(name, cap)
    return ("Your long-term memory: notes you saved earlier. They follow you across channels, restarts and "
            "context resets, and only you see them. When you learn something worth keeping (the owner's "
            "preferences, decisions, facts about a project or a person), save it as one short sentence: "
            f"{how}. Don't save small talk or what the chat history already shows.\n"
            + ("\n".join(f"- {l}" for l in lines) if lines else "(no notes yet)"))
