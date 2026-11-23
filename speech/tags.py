"""Tag translator: turn free-form bracket tags from an LLM into what Chatterbox Turbo can really do.

Based on the listening audit of 2026-11-12 (F:\\Projects\\TTS\\tag-audit\\verdict.md): only five tags
do something useful, emotion comes from wording, and a higher temperature helps sad and surprised lines.
"""

import re
from collections import Counter

SENTENCE_END = re.compile(r"(?<=[.!?…])[\"')\]]*\s+")
WORKING = ["[angry]", "[chuckle]", "[gasp]", "[laugh]", "[shush]"]

# A tag is a short bracketed phrase of words. "[1]" or a long bracketed aside is left alone.
TAG = re.compile(r"\[([A-Za-z][A-Za-z ,'-]{0,39})\]")
# First match wins, so the soft-laugh rule sits above the plain laugh rule.
SOUNDS = [
    (re.compile(r"chuckl|giggl|snicker|titter|snort|(soft|quiet|light|gentl|nervous)\w* laugh|laugh\w* (soft|quiet|light|gentl|nervous)"), "[chuckle]"),
    (re.compile(r"laugh|cackl|guffaw"), "[laugh]"),
    (re.compile(r"gasp|sharp (breath|inhale)|inhales? sharply"), "[gasp]"),
    (re.compile(r"shush|\bshh+|hush"), "[shush]"),
    (re.compile(r"angr|furious|irritat|annoy|\bmad\b|rage|yell|shout|snap"), "[angry]"),
]
PAUSE = re.compile(r"pause|\bbeat\b|silence")
MOOD_HOT = re.compile(r"sad|cry|sob|tear|weep|grie[fv]|sniffl|whimper|surpris|shock|astonish|amaz|stunned")
MOOD_BASE = re.compile(r"happy|cheer|calm|neutral|normal|joy|excit|warm|playful")

dropped: Counter = Counter()  # tags with no equivalent, for choosing what to train next


def translate(sentence: str, hot: bool) -> tuple[str, bool]:
    """Rewrite one sentence. Returns (text, hot): hot means "use the mood temperature from here on"."""

    def swap(match: re.Match) -> str:
        nonlocal hot
        raw = match.group(0).lower()
        if raw in WORKING:
            return raw
        words = match.group(1).lower()
        for pattern, tag in SOUNDS:
            if pattern.search(words):
                return tag
        if PAUSE.search(words):
            return "…"
        if MOOD_HOT.search(words):
            hot = True
        elif MOOD_BASE.search(words):
            hot = False
        else:
            if len(dropped) < 500 or raw in dropped:  # bound memory against endless novel tags
                dropped[raw] += 1
        return ""

    text = TAG.sub(swap, sentence)
    text = re.sub(r"\s+([,.!?;:])", r"\1", re.sub(r"\s{2,}", " ", text)).strip()
    return text, hot


def plan(text: str, base_temp: float, mood_temp: float, max_chars: int, enabled: bool = True) -> list[tuple[str, float]]:
    """Split text into (segment, temperature) pieces of whole sentences, at most max_chars each."""
    pieces: list[tuple[str, float]] = []
    hot = False
    for sentence in SENTENCE_END.split(text.strip()):
        if enabled:
            sentence, hot = translate(sentence, hot)
        if not re.search(r"\w", sentence):  # nothing speakable left (a lone tag, or just "…")
            continue
        temp = mood_temp if hot else base_temp
        if pieces and pieces[-1][1] == temp and len(pieces[-1][0]) + len(sentence) + 1 <= max_chars:
            pieces[-1] = (f"{pieces[-1][0]} {sentence}", temp)
        else:
            pieces.append((sentence, temp))
    return pieces
