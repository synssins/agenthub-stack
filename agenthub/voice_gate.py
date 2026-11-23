"""Voice by name, no wake word: decide whether a transcribed sentence was spoken TO an agent.

  1. Stop words ("stop", "cancel", ...) while a voice conversation is open stop that agent.
  2. No agent named and no open conversation: ignored, the model is not asked, unless answering
     without the name is on (Settings > Voice): then the model decides whether the sentence was
     said to the channel's assistant at all, from its wording alone.
  3. Otherwise the voice check model (Settings > Voice, a model from Settings > LLMs) decides
     "to" vs "about" for a named agent, or whether a nameless sentence is a follow-up.
  4. Model missing, failing or slow: ignored (fail closed).

A conversation stays open for WINDOW seconds after the owner's last accepted sentence, the
agent's last reply, or the end of its spoken playback. Conversations are per source (the
browser, or each always-listening device), so one room's Dot doesn't continue another's.
Before anything else, a sentence that repeats what was just spoken to that source is the
device hearing its own speaker (echo), and the same sentence from a second device within
DEDUPE seconds was heard twice (duplicate): both are dropped. Kept in memory only.
Rejected speech is never stored or logged.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections import deque

WINDOW = 30.0
ECHO_SECONDS = 60.0   # how long text spoken to a source can come back through its mic
DEDUPE = 4.0          # two devices hearing one sentence
ECHO_TAIL = 3.0       # after playback ends, its echo can still arrive this long
STOP_RE = re.compile(r"^(stop|wait|cancel|never ?mind|hold on|shut up)[.!]*$", re.I)
PROMPT = (
    "You gate a voice assistant room. The microphone hears everything said in a room. Decide whether "
    "the CURRENT utterance is spoken TO one of the candidate agents (a request, question, command or "
    "answer meant for them), or not: talking ABOUT them to someone else, quoting, other people, TV, or "
    "unrelated talk. If a conversation with an agent is open (it spoke recently), a follow-up without its "
    'name can still be TO it. Reply with JSON only: {"to": "<handle>"} or {"to": null}.')
# Added to the prompt when nobody was named and no conversation is open (answering without the name).
UNNAMED = (
    "No name was said and no conversation is open. Say it is TO the candidate only if it is plainly meant "
    "for a voice assistant: a command for the house or a device (lights, music, timers, locks, the "
    "thermostat), or a question asked of the assistant. Not TO it: people talking to each other (a name, "
    "\"honey\", \"you guys\", plans between people), talking about the assistant, TV, radio, reading aloud, "
    "singing, thinking aloud, or anything unclear. When in doubt, null.")

_lock = threading.Lock()
_conv: dict[tuple[str, str], dict] = {}   # (channel, source) -> {"agent", "until", "since", "turns"}
_spoken: dict[tuple[str, str], deque] = {}  # (channel, source) -> (ts, words) spoken to it
_recent: dict[str, deque] = {}              # channel -> (ts, normalised text, source) just heard


def reset():
    with _lock:
        _conv.clear()
        _spoken.clear()
        _recent.clear()


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", (text or "").lower())


def open_for(channel: str, now: float | None = None, source: str = "browser") -> str | None:
    now = time.time() if now is None else now
    with _lock:
        c = _conv.get((channel, source))
        if c and c["until"] > now:
            return c["agent"]
        _conv.pop((channel, source), None)
        return None


def saw_reply(channel: str, agent: str, text: str, ts: float):
    """An agent's reply in a channel: extends its open voice conversations (and gives follow-ups context)."""
    with _lock:
        for (ch, _), c in _conv.items():
            if ch == channel and c["agent"] == agent and ts >= c["since"]:
                c["turns"].append(("assistant", text[-300:]))
                c["until"] = max(c["until"], ts + WINDOW)


def played(channel: str, now: float | None = None, source: str = "browser"):
    """A reply finished playing on that source: the follow-up window starts now, and what it
    played can't come back as echo after a short tail (the owner may repeat its words)."""
    now = time.time() if now is None else now
    with _lock:
        for e in _spoken.get((channel, source), ()):
            e[2] = min(e[2], now + ECHO_TAIL)
        c = _conv.get((channel, source))
        if c and c["until"] > now - WINDOW:
            c["until"] = max(c["until"], now + WINDOW)


def spoke(channel: str, source: str, text: str, now: float | None = None):
    """Text about to be played on that source: its mic may hear it back (echo)."""
    now = time.time() if now is None else now
    with _lock:
        q = _spoken.setdefault((channel, source), deque(maxlen=20))
        q.append([now, set(_words(text)), now + ECHO_SECONDS])  # [spoken at, words, echo possible until]


def _is_echo(channel: str, source: str, text: str, now: float) -> bool:
    words = _words(text)
    if len(words) < 4:
        return False
    with _lock:
        for ts, spoken, until in _spoken.get((channel, source), ()):
            if ts <= now <= until and sum(w in spoken for w in words) / len(words) >= 0.6:
                return True
    return False


def _same_sentence(a: set, b: set) -> bool:
    """Two mics rarely transcribe a sentence identically: most words in common is the same sentence."""
    return a == b or (bool(a) and bool(b) and len(a & b) / max(len(a), len(b)) >= 0.8)


def _is_duplicate(channel: str, source: str, text: str, now: float) -> bool:
    words = set(_words(text))
    with _lock:
        q = _recent.setdefault(channel, deque(maxlen=50))
        while q and now - q[0][0] > DEDUPE:
            q.popleft()
        dup = any(src != source and _same_sentence(w, words) for _, w, src in q)
        q.append((now, words, source))
    return dup and bool(words)


def _accept(channel: str, agent: str, text: str, now: float, source: str):
    with _lock:
        c = _conv.get((channel, source))
        if not c or c["agent"] != agent:
            c = _conv[(channel, source)] = {"agent": agent, "since": now, "turns": deque(maxlen=6)}
        c["turns"].append(("user", text[-300:]))
        c["until"] = now + WINDOW


def accept(channel: str, agent: str, text: str, now: float | None = None, source: str = "browser"):
    """Record text as said to agent without deciding, for a turn the device opened itself (its wake word
    or a command word): the follow-up window and the turns for the next decision start from it."""
    _accept(channel, agent, text, time.time() if now is None else now, source)


def _turns(channel: str, source: str) -> list[tuple[str, str]]:
    with _lock:
        c = _conv.get((channel, source))
        return list(c["turns"]) if c else []


def _parse(answer: str | None, candidates: list[str]) -> str | None | bool:
    """-> handle, None (no one), or False (unparseable)."""
    if not answer:
        return False
    answer = re.sub(r"<think>.*?(</think>|$)", "", answer, flags=re.S)
    m = re.search(r"\{.*\}", answer, re.S)
    try:
        to = json.loads(m.group(0)).get("to") if m else False
    except (ValueError, AttributeError):
        return False
    if to is None:
        return None
    if not isinstance(to, str):
        return False
    to = to.lstrip("@").lower()
    return to if to in candidates else None


def decide(text: str, channel: str, here: dict[str, str], ask, named, now: float | None = None,
           source: str = "browser", unnamed: str | None = None) -> dict:
    """text: transcript. here: {handle: label} of agents in the channel. ask(prompt) -> model
    answer or None on failure. named(text, here) -> handles named in the text. unnamed: the agent a
    sentence with no name and no open conversation may still be for (answering without the name), or None.
    -> {"accepted", "to", "reason", "open"}."""
    now = time.time() if now is None else now
    text = text.strip()
    current = open_for(channel, now, source)
    if current and current not in here:
        current = None

    def out(accepted, to, reason):
        return {"accepted": accepted, "to": to, "reason": reason, "open": open_for(channel, now, source)}
    if _is_echo(channel, source, text, now):
        return out(False, None, "echo")
    if not here:
        return out(False, None, "no_agents")
    if current and STOP_RE.match(text):
        return out(False, current, "stopped")
    names = [n for n in named(text, here) if n in here]
    alone = not names and not current  # nobody named and no conversation open
    if alone and unnamed not in here:
        return out(False, None, "no_name")
    # Only a sentence this source could post claims it: another device that would ignore it
    # (no name, no conversation) must not make the right one drop it as a duplicate.
    if _is_duplicate(channel, source, text, now):
        return out(False, None, "duplicate")
    candidates = names or [current or unnamed]
    convo = "\n".join(f"{'owner' if r == 'user' else current}: {t}" for r, t in _turns(channel, source)) if current else ""
    who = "\n".join(f"@{n}" + (f" (name: {here[n]})" if here[n] and here[n] != n else "") for n in candidates)
    prompt = (f"Candidate agents:\n{who}\n\nOpen conversation: "
              + (f"with @{current}\n{convo or '(no turns yet)'}" if current else "none")
              + (f"\n\n{UNNAMED}" if alone else "")
              + f"\n\nCURRENT utterance: {text}")
    to = _parse(ask(prompt), candidates)
    if to is False:
        return out(False, None, "decision_unavailable")
    if to is None:
        return out(False, None, "about" if names else "not_for_agent")
    _accept(channel, to, text, now, source)
    return out(True, to, "named" if to in names else ("unnamed" if alone else "followup"))
