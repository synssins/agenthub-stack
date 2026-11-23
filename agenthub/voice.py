"""Voice: text-to-speech through OpenAI-style /audio/speech providers (Kokoro, Chatterbox,
external APIs) and speech-to-text through a Wyoming server (wyoming-whisper).

Providers live in data/voice.json (owner-editable in Settings > Voice). Each agent picks a
provider, voice and speed on its own page (spec fields voice_provider, voice, voice_speed).
Cloning uploads a short sample to a provider that keeps a voice library (Chatterbox POST /voices).

Engine parameters: a provider says which extra settings it takes (capabilities()), the owner
sets them in Settings > Voice, and they are sent with every request (provider "parameters").

Saved audio: every rendered sentence is kept in data/voice_cache, keyed by provider, voice,
settings and text, so replaying a message plays the same audio again at once.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import wave
from pathlib import Path

data_dir = Path("./data")
_lock = threading.Lock()

DEFAULTS = {
    "providers": [
        {"name": "Kokoro", "base_url": "http://127.0.0.1:8880/v1", "model": "kokoro", "format": "mp3",
         "voices_url": "http://127.0.0.1:8880/v1/audio/voices", "default_voice": "af_bella"},
        {"name": "Chatterbox", "base_url": "http://127.0.0.1:4123", "model": "", "format": "wav",
         "voices_url": "http://127.0.0.1:4123/voices", "clone_url": "http://127.0.0.1:4123/voices",
         "default_voice": "Nora"},
    ],
    "default_provider": "Kokoro",
    "stt": {"wyoming": "127.0.0.1:10300", "language": "en"},
    "cache_mb": 512,   # saved audio kept on disk; 0 = keep none
}
TIMEOUT = 120  # Chatterbox on CPU takes ~9 s per sentence


def setup(cfg: dict):
    global data_dir
    data_dir = Path(cfg.get("server", {}).get("data_dir", "./data"))


def load() -> dict:
    try:
        return {**DEFAULTS, **json.loads((data_dir / "voice.json").read_text("utf-8"))}
    except (OSError, ValueError):
        return json.loads(json.dumps(DEFAULTS))


def save(conf: dict):
    with _lock:
        data_dir.mkdir(parents=True, exist_ok=True)
        tmp = data_dir / "voice.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # may hold API keys
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(conf, f, indent=2)
        os.replace(tmp, data_dir / "voice.json")


def provider(name: str | None) -> dict:
    conf = load()
    ps = conf["providers"]
    return (next((p for p in ps if p["name"] == name), None)
            or next((p for p in ps if p["name"] == conf.get("default_provider")), None) or ps[0])


def _get(url: str, key: str = "", timeout: int = 8):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"} if key else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def voices() -> dict:
    """{provider name: {voices, models, clone}} (an unreachable provider lists none, with an error)."""
    out = {}
    for p in load()["providers"]:
        try:
            raw = _get(p["voices_url"], p.get("api_key", "")) if p.get("voices_url") else {"voices": p.get("voices", [])}
            items = raw.get("voices", raw) if isinstance(raw, dict) else raw
            names = [v if isinstance(v, str) else v.get("name") or v.get("id") for v in items]
            out[p["name"]] = {"voices": sorted(n for n in names if n), "clone": bool(p.get("clone_url")), "models": []}
            try:  # OpenAI-style model list, for the Model dropdown; many speech services have none
                listed = _get(p["base_url"].rstrip("/") + "/models", p.get("api_key", ""), 4)
                out[p["name"]]["models"] = sorted(str(m["id"]) for m in listed.get("data", []) if m.get("id"))
            except Exception:
                pass
        except Exception as exc:
            out[p["name"]] = {"voices": [], "clone": bool(p.get("clone_url")), "error": str(exc)[:160]}
    return out


CODE_RE = re.compile(r"```.*?```", re.S)


TAG_RE = re.compile(r"\[[A-Za-z][A-Za-z ]{1,23}\]")   # [laugh], [whispering]: an emotion tag for the speech engine
_tags: dict[str, tuple[float, list[str]]] = {}          # base_url -> (asked at, tags)


def speakable(text: str, keep=None) -> str:
    """Chat text -> what to read aloud: no code, artifacts, attachments, links or markdown.
    keep: the emotion tags the engine performs; every other [tag] is removed so it is never
    read out as a word. None leaves tags alone."""
    t = CODE_RE.sub(" (code block) ", text or "")
    t = re.sub(r"\[artifact:[^\]]+\]", " (artifact) ", t)
    t = re.sub(r"\[attach(?:ed)?[^\]]*\]", " ", t)
    t = re.sub(r"\[face:[a-z_ -]{1,24}\]", " ", t, flags=re.I)   # a face for a display, not a word
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"!?\[[^\]]*\]\([^)]*\)", " ", t)              # links and images are for the screen only
    t = re.sub(r"https?://\S+", " ", t)
    t = re.sub(r"(^|\s)@([\w-]+)", r"\1\2", t)                 # @name -> name
    if keep is not None:
        ok = {k.lower() for k in keep}
        t = TAG_RE.sub(lambda m: m.group(0).lower() if m.group(0).lower() in ok else " ", t)
    t = re.sub(r"[*_#>|~]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def parse_tags(value) -> list[str]:
    """"[laugh], sigh" or a list -> ["[laugh]", "[sigh]"] (lower case, valid tags only, max 40)."""
    items = value if isinstance(value, list) else re.split(r"[,\n]+", str(value or ""))
    out = []
    for x in items:
        tag = "[" + str(x).strip().strip("[]").strip().lower() + "]"
        if TAG_RE.fullmatch(tag) and tag not in out:
            out.append(tag)
    return out[:40]


def tags(p: dict) -> list[str]:
    """The emotion tags a provider's engine performs inline in the text. The provider's own
    "tags" list wins (an empty list = none); without one the service is asked at /api/tags
    (a JSON list, as turbo-stream publishes) and the answer is kept for ten minutes."""
    if isinstance(p.get("tags"), list):
        return parse_tags(p["tags"])
    url = p.get("base_url", "")
    hit = _tags.get(url)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    try:
        u = urllib.parse.urlsplit(url)
        got = _get(f"{u.scheme}://{u.netloc}/api/tags", p.get("api_key", ""), timeout=3)
        out = parse_tags(got) if isinstance(got, list) else []
    except Exception:
        out = []   # no such list (or the service is down): no tags
    _tags[url] = (time.time(), out)
    return out


def all_tags() -> list[str]:
    """Every emotion tag any provider performs: the chat page hides these in message text."""
    out: list[str] = []
    for p in load()["providers"]:
        out += [t for t in tags(p) if t not in out]
    return out


# When a tag fits: the default guidance in an agent's prompt (its own "tag_guide" replaces it)
GUIDE = {"[chuckle]": "a light joke or teasing", "[laugh]": "something really funny",
         "[happy]": "good news or a task that went well", "[sigh]": "bad news or an action that failed",
         "[gasp]": "a surprising result", "[whispering]": "a secret, or when asked to be quiet",
         "[sarcastic]": "a dry remark", "[groan]": "a bad pun or a tedious chore"}
# Tags AgentHub adds by itself: after a failed action, and in quiet hours (voice.json "tag_rules")
RULES = {"failed": "[sigh]", "quiet": "", "quiet_from": 22, "quiet_to": 7}


def tags_prompt(provider_name: str | None, guide: str = "") -> str:
    """The lines of an agent's system prompt about emotion tags ("" when its voice has none).
    guide: the agent's own guidance on when to use which tag; blank = the default table."""
    have = tags(provider(provider_name))
    if not have:
        return ""
    known = [t for t in have if t in GUIDE]
    hints = (guide or "").strip()[:800] or (
        "; ".join(f"{t} for {GUIDE[t]}" for t in known)
        + ("; the others only when the moment clearly calls for them" if len(known) < len(have) else ""))
    return ("Your replies can be read aloud. The voice performs these tags as sounds or moods: "
            + " ".join(have) + ". Where it fits the moment, put one inline in your text, exactly as "
            "written; most sentences need none. Use no other tags in square brackets."
            + (f" When to use them: {hints}." if hints else ""))


def rules() -> dict:
    return {**RULES, **(load().get("tag_rules") or {})}


def spoken_tags(text: str, provider_name: str | None) -> list[str]:
    """The tags in a reply that its voice performs, in order."""
    have = set(tags(provider(provider_name)))
    return [t for t in (m.lower() for m in TAG_RE.findall(re.sub(r"`[^`]*`", " ", CODE_RE.sub(" ", text or "")))) if t in have]


def rule_tag(text: str, provider_name: str | None, failed: bool, hour: int | None = None) -> str:
    """A reply with the tag the rules call for put in front: after a failed action, or in quiet
    hours. Unchanged when the agent chose a tag itself or the voice does not perform that tag."""
    have = tags(provider(provider_name))
    if not have or not text or spoken_tags(text, provider_name):
        return text
    r = rules()
    hour = time.localtime().tm_hour if hour is None else hour
    a, b = int(r.get("quiet_from") or 0), int(r.get("quiet_to") or 0)
    quiet = (a <= hour < b) if a <= b else (hour >= a or hour < b)
    pick = (r.get("failed") if failed else "") or (r.get("quiet") if quiet else "")
    return f"{pick} {text}" if pick in have else text


# ---------------------------------------------------------------- engine parameters

CORE = {"input", "text", "voice", "model", "response_format", "stream", "speed", "audio_base64"}  # set by AgentHub
TYPES = ("integer", "number", "boolean", "string")


def capabilities(p: dict) -> list[dict]:
    """The extra request settings a provider advertises, as [{name, type, min, max, step, enum,
    default, description, advanced}]. Read from the provider's "capabilities_url" (a document with
    a "parameters" list, as tts-serve publishes) or, without one, from the service's own
    /openapi.json (the request body of POST …/audio/speech). Raises when neither can be read."""
    key = p.get("api_key", "")
    if p.get("capabilities_url"):
        items = [(x.get("name"), x) for x in _get(p["capabilities_url"], key).get("parameters") or []
                 if isinstance(x, dict)]
    else:
        u = urllib.parse.urlsplit(p["base_url"])
        doc = _get(f"{u.scheme}://{u.netloc}/openapi.json", key)
        post = next((v["post"] for k, v in doc.get("paths", {}).items()
                     if k.endswith("/audio/speech") and "post" in v), None)
        if not post:
            raise ValueError("the service's openapi.json has no /audio/speech request")
        schema = post.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema", {})
        if "$ref" in schema:
            schema = doc.get("components", {}).get("schemas", {}).get(schema["$ref"].rsplit("/", 1)[-1], {})
        items = list((schema.get("properties") or {}).items())
    out = []
    for name, raw in items:
        for alt in raw.get("anyOf") or []:  # "number or null": the limits sit on the typed branch
            if isinstance(alt, dict) and alt.get("type") in TYPES:
                raw = {**alt, **raw, "type": alt["type"]}
        if not name or name in CORE or raw.get("type") not in TYPES:
            continue
        out.append({"name": name, "type": raw["type"], "min": raw.get("min", raw.get("minimum")),
                    "max": raw.get("max", raw.get("maximum")), "step": raw.get("step"),
                    "enum": raw.get("enum") if isinstance(raw.get("enum"), list) else None,
                    "default": raw.get("default"), "advanced": bool(raw.get("advanced")),
                    "description": str(raw.get("description") or raw.get("title") or "")[:300]})
    return out


def check_parameters(specs: list[dict], values: dict) -> tuple[dict, str | None]:
    """Values for a provider's engine parameters -> (the values to store, or an error message).
    Blank values are left out (the engine's default applies), and so is anything not advertised."""
    by, clean = {x["name"]: x for x in specs}, {}
    for name, v in (values or {}).items():
        spec = by.get(name)
        if spec is None or v is None or v == "":
            continue
        t = spec["type"]
        try:
            if t == "boolean":
                if str(v).lower() not in ("true", "false"):
                    raise ValueError
                v = str(v).lower() == "true"
            elif t == "integer":
                if float(v) != int(float(v)):
                    raise ValueError
                v = int(float(v))
            elif t == "number":
                v = float(v)
            else:
                v = str(v)
        except (TypeError, ValueError):
            return {}, f"{name} must be {'an' if t == 'integer' else 'a'} {t}"
        lo, hi = spec.get("min"), spec.get("max")
        if t in ("integer", "number") and ((lo is not None and v < lo) or (hi is not None and v > hi)):
            return {}, f"{name} must be between {lo if lo is not None else '-∞'} and {hi if hi is not None else '∞'}"
        if spec.get("enum") and v not in spec["enum"]:
            return {}, f"{name} must be one of: {', '.join(map(str, spec['enum']))}"
        clean[name] = v
    return clean, None


# ---------------------------------------------------------------- saved audio

def _cache_file(p: dict, body: dict) -> Path:
    key = json.dumps([p["base_url"], body], sort_keys=True, ensure_ascii=False)
    return data_dir / "voice_cache" / hashlib.sha256(key.encode("utf-8")).hexdigest()


def _cache_get(path: Path) -> tuple[dict, bytes] | None:
    """-> (meta, audio) saved earlier for this exact request, or None."""
    try:
        head, _, audio = path.read_bytes().partition(b"\n")
        os.utime(path)  # recently played audio is pruned last
        return json.loads(head), audio
    except (OSError, ValueError):
        return None


def _cache_put(path: Path, meta: dict, audio: bytes):
    limit = int(load().get("cache_mb") or 0) * 1024 * 1024
    if not audio or len(audio) > limit:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = path.with_suffix(f".{uuid.uuid4().hex[:8]}.tmp")
        tmp.write_bytes(json.dumps(meta).encode() + b"\n" + audio)
        os.replace(tmp, path)
        files = sorted(((f.stat().st_mtime, f.stat().st_size, f) for f in path.parent.iterdir()), key=lambda x: x[0])
        total = sum(size for _, size, _ in files)
        for _, size, f in files:  # oldest first, until the folder fits
            if total <= limit:
                break
            f.unlink(missing_ok=True)
            total -= size
    except OSError:
        pass  # saving is best effort; the audio was still played


def _request(text: str, provider_name: str | None, voice: str | None, speed: float | None,
             **extra) -> tuple[dict, dict, urllib.request.Request]:
    """-> (provider, request body, the /audio/speech request)."""
    p = provider(provider_name)
    text = speakable(text, tags(p))[:3000]
    if not text:
        raise ValueError("nothing to read aloud")
    body = {**(p.get("parameters") or {}), "input": text,
            "voice": voice or p.get("default_voice") or "alloy", **extra}
    if p.get("model"):
        body["model"] = p["model"]
    if speed:
        body["speed"] = max(0.5, min(2.0, float(speed)))
    headers = {"Content-Type": "application/json"}
    if p.get("api_key"):
        headers["Authorization"] = f"Bearer {p['api_key']}"
    return p, body, urllib.request.Request(p["base_url"].rstrip("/") + "/audio/speech",
                                           data=json.dumps(body).encode(), headers=headers)


def speak(text: str, provider_name: str | None = None, voice: str | None = None,
          speed: float | None = None) -> tuple[bytes, str]:
    """-> (audio bytes, media type)."""
    p, body, req = _request(text, provider_name, voice, speed,
                            response_format=provider(provider_name).get("format") or "mp3")
    path = _cache_file(p, body)
    hit = _cache_get(path)
    if hit:
        return hit[1], hit[0].get("media") or "audio/mpeg"
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        audio, media = r.read(), r.headers.get("Content-Type") or "audio/mpeg"
    _cache_put(path, {"media": media}, audio)
    return audio, media


def speak_stream(text: str, provider_name: str | None = None, voice: str | None = None,
                 speed: float | None = None):
    """For providers with "stream": true -> (sample rate, iterator of raw PCM16 mono bytes).
    Bytes are passed on as the provider produces them, so playback starts on the first chunk."""
    p, body, req = _request(text, provider_name, voice, speed, response_format="pcm", stream=True)
    path = _cache_file(p, body)
    hit = _cache_get(path)
    if hit:
        return int(hit[0].get("rate") or 24000), (hit[1][i:i + 8192] for i in range(0, len(hit[1]), 8192))
    resp = urllib.request.urlopen(req, timeout=TIMEOUT)
    rate = int(resp.headers.get("X-Sample-Rate") or p.get("sample_rate") or 24000)

    def chunks():
        got = []
        with resp:
            while True:
                b = resp.read1(8192)
                if not b:
                    break
                got.append(b)
                yield b
        _cache_put(path, {"rate": rate}, b"".join(got))  # only a sentence that was rendered to the end
    return rate, chunks()


def streams(provider_name: str | None) -> bool:
    return bool(provider(provider_name).get("stream"))


def clone(provider_name: str, name: str, filename: str, sample: bytes) -> str:
    """Upload a voice sample to a provider's voice library -> the new voice name."""
    p = provider(provider_name)
    if not p.get("clone_url"):
        raise ValueError(f"{p['name']} has no voice library to clone into")
    name = re.sub(r"[^\w -]", "", name).strip()[:40]
    if not name:
        raise ValueError("a voice name is required")
    boundary = uuid.uuid4().hex
    parts = []
    for field, value in (("voice_name", name), ("language", load()["stt"].get("language", "en"))):
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"\r\n\r\n{value}\r\n'.encode())
    safe = re.sub(r"[^\w.-]", "_", filename or "sample.wav")
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="voice_file"; filename="{safe}"\r\n'
                 f'Content-Type: application/octet-stream\r\n\r\n'.encode() + sample + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(p["clone_url"], data=b"".join(parts),
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=60):
            pass
    except urllib.error.HTTPError as exc:
        raise ValueError(f"{p['name']} refused the sample: {exc.read()[:200].decode('utf-8', 'replace')}")
    return name


# ---------------------------------------------------------------- speech-to-text (Wyoming)

# Whisper invents these on silence or noise (trained on video subtitles); drop them when they are
# the whole transcript.
HALLUCINATIONS = re.compile(
    r"^(thanks? (you )?(so much )?for watching|thank you( very much)?|thanks|bye|you|"
    r"please subscribe|(like and )?subscribe( to (my|the) channel)?|subtitles by.*|"
    r"see you (next time|in the next video)|\.+|\[.*\]|\(.*\))[.!]*$", re.I)


def clean_transcript(text: str) -> str:
    t = (text or "").strip()
    return "" if HALLUCINATIONS.match(t) else t


def _send(sock, etype: str, data: dict | None = None, payload: bytes = b""):
    head = {"type": etype, "data": data or {}}
    if payload:
        head["payload_length"] = len(payload)
    sock.sendall(json.dumps(head).encode() + b"\n" + payload)


def _read_event(f) -> dict:
    line = f.readline()
    if not line:
        raise ConnectionError("speech-to-text server closed the connection")
    ev = json.loads(line)
    if ev.get("data_length"):
        ev["data"] = {**(ev.get("data") or {}), **json.loads(f.read(ev["data_length"]))}
    if ev.get("payload_length"):
        f.read(ev["payload_length"])
    return ev


def transcribe(wav_bytes: bytes) -> str:
    """WAV (any rate; 16-bit) -> text, through the Wyoming server in voice.json stt.wyoming."""
    with wave.open(io.BytesIO(wav_bytes)) as w:
        rate, width, channels = w.getframerate(), w.getsampwidth(), w.getnchannels()
        pcm = w.readframes(w.getnframes())
    stt = load()["stt"]
    host, port = stt["wyoming"].rsplit(":", 1)
    fmt = {"rate": rate, "width": width, "channels": channels}
    with socket.create_connection((host, int(port)), timeout=60) as sock:
        _send(sock, "transcribe", {"language": stt.get("language") or "en"})
        _send(sock, "audio-start", fmt)
        step = rate * width * channels  # one second per chunk
        for i in range(0, len(pcm), step):
            _send(sock, "audio-chunk", fmt, pcm[i:i + step])
        _send(sock, "audio-stop")
        f = sock.makefile("rb")
        while True:
            ev = _read_event(f)
            if ev["type"] == "transcript":
                return clean_transcript((ev.get("data") or {}).get("text", ""))
            if ev["type"] == "error":
                raise RuntimeError((ev.get("data") or {}).get("text", "speech-to-text failed"))
