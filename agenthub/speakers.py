"""Voice recognition: a voice heard by a device is matched with the people who enrolled theirs.

Off until a model file is set and somebody is enrolled (Settings > Voice > Voice recognition). The
model is a speaker-embedding ONNX file run by sherpa-onnx on the CPU (about 0.05 s per sentence);
without that package or the file this module does nothing. No audio is kept: a person is the mean
of the embeddings of a few things they said.

Enrolling is guided: a person reads PASSAGE to one device. While that runs, what the device hears goes
here and not to an agent; each sentence is lined up with the passage, and the voice is saved when the
last word was read.

  data/speakers.json   {"enabled", "model": path, "threshold", "people": {name: {"emb": [...], "n"}}}
"""

from __future__ import annotations

import difflib
import io
import json
import logging
import os
import re
import threading
import time
import wave
from pathlib import Path

log = logging.getLogger(__name__)
data_dir = Path("./data")   # set by start (run.py)
FILE = "speakers.json"
MARGIN = 0.04               # the best match must beat the next person by this much
_lock = threading.RLock()
_ex = None                  # (model path, extractor)
error = ""
PASSAGE = ("The weather was fine this morning, so I took a slow walk around the block before breakfast. "
           "A neighbor waved from across the street and asked how the garden was coming along. "
           "I told her the tomatoes were doing well, but the peppers still needed more sun. "
           "When I got home, I made a pot of coffee and sat by the window for a while. "
           "Later today I plan to fix the back gate and maybe read a few pages of my book.")
ENROLL_IDLE = 180           # an enrollment nobody speaks to ends after this many seconds
MIN_SECONDS = 2.0           # shorter speech is too little to learn a voice from
_enroll: dict | None = None  # the enrollment in progress: {"name", "device", "words", "pos", "stuck", "skipped", "embs", "heard", "at", "done", "error"}


def load() -> dict:
    try:
        raw = json.loads((data_dir / FILE).read_text("utf-8"))
    except (OSError, ValueError):
        raw = {}
    raw = raw if isinstance(raw, dict) else {}
    try:
        th = max(0.3, min(0.95, float(raw.get("threshold") or 0.65)))
    except (TypeError, ValueError):
        th = 0.65
    people = {str(k)[:40]: v for k, v in (raw.get("people") or {}).items() if isinstance(v, dict) and isinstance(v.get("emb"), list)}
    return {"enabled": raw.get("enabled") is True, "model": str(raw.get("model") or ""), "threshold": th, "people": people}


def save(conf: dict):
    tmp = data_dir / "speakers.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(conf, f)
    os.replace(tmp, data_dir / FILE)


def public() -> dict:
    c = load()
    return {"enabled": c["enabled"], "model": c["model"], "threshold": c["threshold"], "error": error,
            "people": [{"name": k, "samples": int(v.get("n") or 1)} for k, v in c["people"].items()],
            "enroll": enroll_state()}


def embed(wav: bytes, model: str):
    """A 16-bit mono WAV -> its unit-length voice embedding (numpy), or None when it cannot be made."""
    global _ex, error
    try:
        import numpy as np
        import sherpa_onnx
        with _lock:
            if not _ex or _ex[0] != model:
                _ex = (model, sherpa_onnx.SpeakerEmbeddingExtractor(sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=model, num_threads=2)))
            ex = _ex[1]
        with wave.open(io.BytesIO(wav)) as w:
            rate, raw = w.getframerate(), w.readframes(w.getnframes())
        a = np.frombuffer(raw[:len(raw) // 2 * 2], dtype=np.int16).astype(np.float32) / 32768
        s = ex.create_stream()
        s.accept_waveform(rate, a)
        s.input_finished()
        e = np.array(ex.compute(s), dtype=np.float32)
        error = ""
        return e / (np.linalg.norm(e) or 1.0)
    except Exception as exc:
        error = str(exc)[:200] or type(exc).__name__
        log.warning("speaker model: %s", error)
        return None


def heard(wav: bytes, seconds: float, source: str = "") -> str:
    """A voice was heard. -> the enrolled person it is, or "" (nobody enrolled, too unsure, switched off)."""
    c = load()
    if not (c["enabled"] and c["model"]):
        return ""
    e = embed(wav, c["model"])
    if e is None:
        return ""
    return match(e, c)


def match(e, c: dict) -> str:
    import numpy as np
    scores = sorted(((float(e @ np.array(v["emb"], dtype=np.float32)), k) for k, v in c["people"].items()), reverse=True)
    if not scores or scores[0][0] < c["threshold"] or (len(scores) > 1 and scores[0][0] - scores[1][0] < MARGIN):
        return ""
    return scores[0][1]


def _said(text: str) -> list[str]:
    return re.findall(r"[a-z']+", str(text).lower().replace("\u2019", "'"))


def _same(a: str, b: str) -> bool:
    """One word heard is the word expected: equal, or close enough to be its other spelling."""
    return a == b or (len(b) > 3 and difflib.SequenceMatcher(None, a, b).ratio() >= 0.8)


def _align(heard: list[str], words: list[str], pos: int) -> tuple[int, bool]:
    """Line a sentence heard up with the passage from word `pos`. -> (the new position, whether it is stuck).
    Extra words (a filler, a repeat, a wrong word) are passed over when the right word follows them;
    stuck: the sentence ended on words that are not the next ones."""
    while heard and pos < len(words):
        best, at = 0, 0
        for k in range(len(heard)):   # the start that reads furthest: a re-read sentence joins where it left off
            n = 0
            while k + n < len(heard) and pos + n < len(words) and _same(heard[k + n], words[pos + n]):
                n += 1
            if n > best:
                best, at = n, k
        if not best:
            return pos, True
        pos, heard = pos + best, heard[at + best:]
    return pos, bool(heard) and pos < len(words)


def enroll_start(name: str, device: str) -> str | None:
    """Begin a guided enrollment of this person on this device. -> an error, or None."""
    global _enroll
    name = " ".join(str(name).split())[:40]
    if not name:
        return "give the person a name"
    if not device:
        return "pick the device to read to"
    model = load()["model"]
    if not (model and Path(model).is_file()):
        return "set the model file first"
    with _lock:
        _enroll = {"name": name, "device": str(device), "words": _said(PASSAGE), "pos": 0, "stuck": False,
                   "skipped": [], "embs": [], "heard": "", "at": time.time(), "done": False, "error": ""}
    return None


def enrolling(device: str) -> bool:
    """Whether what this device hears now belongs to an enrollment (and not to an agent)."""
    global _enroll
    with _lock:
        if _enroll and not _enroll["done"] and time.time() - _enroll["at"] > ENROLL_IDLE:
            _enroll = None
        return bool(_enroll and not _enroll["done"] and _enroll["device"] == device)


def enroll_heard(wav: bytes, seconds: float, text: str):
    """The enrolling device heard this. The voice counts when most of it was the passage."""
    heard = _said(text)
    e = embed(wav, load()["model"]) if seconds >= MIN_SECONDS and heard else None
    with _lock:
        s = _enroll
        if not s or s["done"]:
            return
        pos, stuck = _align(heard, s["words"], s["pos"])
        if e is not None and (pos - s["pos"]) * 2 >= len(heard):   # not a voice in the background
            s["embs"].append(e)
        s.update(pos=pos, stuck=stuck, heard=str(text)[:300], at=time.time())
        _finish(s)


def enroll_skip():
    """Pass the word the reading is stuck on: the transcription does not always spell what was said."""
    with _lock:
        s = _enroll
        if s and not s["done"]:
            s["skipped"].append(s["pos"])
            s.update(pos=s["pos"] + 1, stuck=False, at=time.time())
            _finish(s)


def enroll_cancel():
    global _enroll
    with _lock:
        _enroll = None


def _finish(s: dict):
    """The last word was read: the person is the mean of what they had and the voices just heard."""
    import numpy as np
    if s["pos"] < len(s["words"]):
        return
    s["done"] = True
    if not s["embs"]:
        s["error"] = error or "No sentence was long and clear enough to learn the voice from. Start again."
        return
    c = load()
    p = c["people"].get(s["name"]) or {"emb": [0.0] * len(s["embs"][0]), "n": 0}
    n = int(p.get("n") or 0)
    mean = np.array(p["emb"], dtype=np.float32) * n + sum(s["embs"])
    c["people"][s["name"]] = {"emb": [round(float(x), 5) for x in mean / (np.linalg.norm(mean) or 1.0)], "n": n + len(s["embs"])}
    save(c)


def enroll_state() -> dict | None:
    """The enrollment in progress, for the page: the passage as it was written and how far the reading is."""
    with _lock:
        s = _enroll
        if not s:
            return None
        return {"name": s["name"], "device": s["device"], "text": PASSAGE, "pos": s["pos"], "stuck": s["stuck"],
                "skipped": list(s["skipped"]), "samples": len(s["embs"]), "heard": s["heard"], "done": s["done"],
                "error": s["error"]}


def forget(name: str):
    with _lock:
        c = load()
        c["people"].pop(name, None)
        save(c)


def settings(new: dict) -> str | None:
    with _lock:
        c = load()
        model = str(new.get("model", c["model"]) or "").strip()
        if model and not Path(model).is_file():
            return "no such model file on the server"
        c.update(enabled=new.get("enabled", c["enabled"]) is True, model=model)
        try:
            c["threshold"] = max(0.3, min(0.95, float(new.get("threshold", c["threshold"]))))
        except (TypeError, ValueError):
            pass
        save(c)
    return None
