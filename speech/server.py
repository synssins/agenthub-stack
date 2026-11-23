"""Chatterbox Turbo streaming server: OpenAI /v1/audio/speech + Wyoming TTS (Piper-style).

One model, one GPU, one request at a time. Voices are saved presets (<name>.pt) in
VOICE_DIR; a <name>.wav in REF_DIR is converted to a preset on first use.
"""

import asyncio
import logging
import os
import re
import struct
import threading
from contextlib import asynccontextmanager
from pathlib import Path

import librosa
import soundfile as sf
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.event import Event
from wyoming.info import Attribution, Describe, Info, TtsProgram, TtsVoice
from wyoming.server import AsyncEventHandler, AsyncServer
from wyoming.tts import (
    Synthesize,
    SynthesizeChunk,
    SynthesizeStart,
    SynthesizeStop,
    SynthesizeStopped,
)

from chatterbox.streaming import audio_to_pcm_s16le
from chatterbox.tts_turbo import ChatterboxTurboTTS, Conditionals

import tags
from tags import SENTENCE_END

log = logging.getLogger("turbo-stream")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

VOICE_DIR = Path(os.environ.get("VOICE_DIR", "/voices"))
REF_DIR = Path(os.environ.get("REF_DIR", "/refs"))
DEFAULT_VOICE = os.environ.get("DEFAULT_VOICE", "Jessica")
WYOMING_URI = os.environ.get("WYOMING_URI", "tcp://0.0.0.0:10220")
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
SEGMENT_CHARS = 300  # stream() caps one pass at 40 s of audio and truncates long text
MAX_INPUT_CHARS = 4096  # same limit as the OpenAI API
RATE = 24000
VOICE_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")  # also blocks path traversal

model: ChatterboxTurboTTS | None = None
gpu_lock = threading.Lock()  # the model holds one voice and one KV cache at a time
voice_cache: dict[str, Conditionals] = {}


class Settings(BaseModel):
    """Editable from the web page; saved to VOICE_DIR/settings.json."""

    default_voice: str = Field(DEFAULT_VOICE, pattern=VOICE_NAME.pattern)
    first_chunk_tokens: int = Field(10, ge=4, le=50)  # 10 measured best on a T4
    chunk_tokens: int = Field(24, ge=8, le=50)
    temperature: float = Field(0.8, ge=0.05, le=2.0)
    top_p: float = Field(0.95, ge=0.05, le=1.0)
    top_k: int = Field(1000, ge=1, le=5000)
    repetition_penalty: float = Field(1.2, ge=1.0, le=2.0)
    translate_tags: bool = True  # map free-form tags to the ones that work, drop the rest (tags.py)
    mood_temperature: float = Field(1.1, ge=0.05, le=2.0)  # used after a sad or surprised tag


SETTINGS_FILE = VOICE_DIR / "settings.json"
try:
    settings = Settings.model_validate_json(SETTINGS_FILE.read_text())
except (OSError, ValueError):
    settings = Settings()


def list_voices() -> list[str]:
    names = {p.stem for d in (VOICE_DIR, REF_DIR) for ext in ("pt", "wav") for p in d.glob(f"*.{ext}")}
    return sorted(n for n in names if VOICE_NAME.match(n))


def ref_wav(name: str) -> Path:
    """Uploaded reference if there is one, else the shared library copy."""
    uploaded = VOICE_DIR / f"{name}.wav"
    return uploaded if uploaded.exists() else REF_DIR / f"{name}.wav"


def resolve_voice(name: str | None) -> str:
    """Case-insensitive match; an empty or unknown-format name raises KeyError."""
    name = name or settings.default_voice
    if not VOICE_NAME.match(name):
        raise KeyError(name)
    for v in list_voices():
        if v.lower() == name.lower():
            return v
    raise KeyError(name)


def load_voice(name: str) -> Conditionals:
    """Call with gpu_lock held. Builds and saves the preset the first time."""
    if name not in voice_cache:
        preset = VOICE_DIR / f"{name}.pt"
        if not preset.exists():
            log.info("building preset %s from reference audio", name)
            model.prepare_conditionals(str(ref_wav(name)))
            model.conds.save(preset)
        voice_cache[name] = Conditionals.load(preset).to(model.device)
    return voice_cache[name]


def synth_blocking(text: str, voice: str, cancel: threading.Event):
    """Yield PCM16 bytes. Runs in a worker thread; holds the GPU for the whole reply."""
    with gpu_lock:
        model.conds = load_voice(voice)
        s = settings
        for segment, temperature in tags.plan(text, s.temperature, s.mood_temperature, SEGMENT_CHARS, s.translate_tags):
            stream = model.stream(
                segment,
                chunk_tokens=s.chunk_tokens,
                first_chunk_tokens=min(s.first_chunk_tokens, s.chunk_tokens),
                temperature=temperature,
                top_p=s.top_p,
                top_k=s.top_k,
                repetition_penalty=s.repetition_penalty,
            )
            try:
                for chunk in stream:
                    if cancel.is_set():
                        return
                    yield audio_to_pcm_s16le(chunk.audio)
            finally:
                stream.close()


async def synth(text: str, voice: str):
    """Async bridge over synth_blocking; cancels generation if the consumer goes away."""
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    cancel = threading.Event()

    def worker():
        try:
            for pcm in synth_blocking(text, voice, cancel):
                loop.call_soon_threadsafe(queue.put_nowait, pcm)
            loop.call_soon_threadsafe(queue.put_nowait, None)
        except Exception as exc:  # surfaced to the consumer below
            log.exception("synthesis failed")
            loop.call_soon_threadsafe(queue.put_nowait, exc)

    threading.Thread(target=worker, daemon=True).start()
    try:
        while (item := await queue.get()) is not None:
            if isinstance(item, Exception):
                raise item
            yield item
    finally:
        cancel.set()


# ---------------------------------------------------------------- OpenAI API


class SpeechRequest(BaseModel):
    input: str = Field(min_length=1, max_length=MAX_INPUT_CHARS)
    voice: str | None = None  # None = the default voice from settings
    model: str = "chatterbox-turbo"
    response_format: str = "wav"


def wav_header() -> bytes:
    # Streamed WAV: total length unknown, so sizes are set to the maximum.
    return b"RIFF" + struct.pack("<I", 0xFFFFFFFF) + b"WAVEfmt " + struct.pack(
        "<IHHIIHH", 16, 1, 1, RATE, RATE * 2, 2, 16
    ) + b"data" + struct.pack("<I", 0xFFFFFFFF)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global model
    VOICE_DIR.mkdir(parents=True, exist_ok=True)
    model = ChatterboxTurboTTS.from_pretrained(device="cuda")
    try:  # warm up so the first real request is fast
        async for _ in synth("Warm up run, just to get everything loaded.", resolve_voice(None)):
            pass
    except KeyError:
        log.warning("default voice %s not found; skipping warm-up", settings.default_voice)
    server = AsyncServer.from_uri(WYOMING_URI)
    task = asyncio.create_task(server.run(WyomingHandler))
    log.info("ready: voices=%s wyoming=%s", list_voices(), WYOMING_URI)
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/v1/models")
def models():
    return {"object": "list", "data": [{"id": "chatterbox-turbo", "object": "model"}]}


@app.get("/v1/audio/voices")
def voices():
    return {"voices": list_voices()}


@app.post("/v1/audio/speech")
async def speech(req: SpeechRequest, request: Request):
    if req.response_format not in ("pcm", "wav"):
        raise HTTPException(400, "response_format must be 'pcm' or 'wav'")
    try:
        voice = resolve_voice(req.voice)
    except KeyError:
        raise HTTPException(400, f"unknown voice; available: {list_voices()}")

    async def body():
        if req.response_format == "wav":
            yield wav_header()
        async for pcm in synth(req.input, voice):
            if await request.is_disconnected():
                return
            yield pcm

    media = "audio/wav" if req.response_format == "wav" else "audio/pcm"
    return StreamingResponse(body(), media_type=media, headers={"X-Sample-Rate": str(RATE)})


# ---------------------------------------------------------------- Web page API
# No login: anyone who can reach the port can add or delete voices. Keep it on the LAN.


def voice_name(name: str) -> str:
    if not VOICE_NAME.match(name):
        raise HTTPException(400, "voice name: letters, digits, _ and - only, up to 64")
    return name


async def read_body(request: Request) -> bytes:
    data = b""
    async for part in request.stream():
        data += part
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "file too large (25 MB max)")
    return data


def build_voice(name: str, data: bytes) -> float:
    """Decode an uploaded clip, store it as the voice's reference WAV, build the preset."""
    tmp = VOICE_DIR / f".upload-{name}"
    tmp.write_bytes(data)
    try:
        audio, rate = librosa.load(tmp, sr=None, mono=True)
    except Exception:
        raise ValueError("could not read that file as audio")
    finally:
        tmp.unlink(missing_ok=True)
    seconds = len(audio) / rate
    if not 6 <= seconds <= 60:
        raise ValueError(f"clip is {seconds:.1f} s; use 6 to 60 s of clean speech")
    sf.write(VOICE_DIR / f"{name}.wav", audio, rate)
    with gpu_lock:
        voice_cache.pop(name, None)
        (VOICE_DIR / f"{name}.pt").unlink(missing_ok=True)
        load_voice(name)
    return seconds


def import_preset(name: str, data: bytes) -> None:
    tmp = VOICE_DIR / f".import-{name}"
    tmp.write_bytes(data)
    try:
        Conditionals.load(tmp)  # weights_only load: rejects anything that is not plain tensors
    except Exception:
        tmp.unlink(missing_ok=True)
        raise ValueError("not a valid Chatterbox Turbo voice preset")
    with gpu_lock:
        voice_cache.pop(name, None)
        tmp.replace(VOICE_DIR / f"{name}.pt")


@app.get("/")
def page():
    return FileResponse(Path(__file__).with_name("ui.html"), media_type="text/html")


@app.get("/api/voices")
def api_voices():
    return [
        {
            "name": v,
            "preset": (VOICE_DIR / f"{v}.pt").exists(),
            "library": (REF_DIR / f"{v}.wav").exists(),  # library voices come back after delete
        }
        for v in list_voices()
    ]


@app.put("/api/voices/{name}")
async def api_upload_voice(name: str, request: Request):
    data = await read_body(request)
    try:
        seconds = await asyncio.to_thread(build_voice, voice_name(name), data)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"name": name, "seconds": round(seconds, 1)}


@app.put("/api/voices/{name}/preset")
async def api_import_preset(name: str, request: Request):
    data = await read_body(request)
    try:
        await asyncio.to_thread(import_preset, voice_name(name), data)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"name": name}


@app.get("/api/voices/{name}/preset")
async def api_export_preset(name: str):
    try:
        voice = resolve_voice(name)
    except KeyError:
        raise HTTPException(404, "unknown voice")

    def ensure():
        with gpu_lock:
            load_voice(voice)

    await asyncio.to_thread(ensure)
    return FileResponse(VOICE_DIR / f"{voice}.pt", filename=f"{voice}.pt")


@app.delete("/api/voices/{name}")
def api_delete_voice(name: str):
    voice_name(name)
    if name == settings.default_voice and not (REF_DIR / f"{name}.wav").exists():
        raise HTTPException(400, "that is the default voice; pick another default first")
    with gpu_lock:
        voice_cache.pop(name, None)
        for ext in ("pt", "wav"):
            (VOICE_DIR / f"{name}.{ext}").unlink(missing_ok=True)
    return {"deleted": name}


@app.get("/api/tags")
def api_tags():
    """Tags worth using: the audited working set, or every tokenizer tag when translation is off."""
    if settings.translate_tags:
        return tags.WORKING
    return sorted(t for t in model.tokenizer.get_added_vocab() if t.startswith("[") and t.endswith("]"))


@app.get("/api/tags/dropped")
def api_dropped_tags():
    """Tags the translator removed because nothing matches them, most frequent first (since start)."""
    return [{"tag": t, "count": n} for t, n in tags.dropped.most_common(50)]


@app.get("/api/settings")
def api_get_settings():
    return settings


@app.put("/api/settings")
def api_put_settings(new: Settings):
    global settings
    if new.first_chunk_tokens > new.chunk_tokens:
        raise HTTPException(400, "first_chunk_tokens must not exceed chunk_tokens")
    try:
        new.default_voice = resolve_voice(new.default_voice)
    except KeyError:
        raise HTTPException(400, "default_voice is not an existing voice")
    settings = new
    SETTINGS_FILE.write_text(new.model_dump_json(indent=2))
    return settings


# ---------------------------------------------------------------- Wyoming

ATTRIBUTION = Attribution(name="Resemble AI", url="https://github.com/resemble-ai/chatterbox")


def wyoming_info() -> Info:
    return Info(
        tts=[
            TtsProgram(
                name="chatterbox-turbo",
                description="Chatterbox Turbo, streamed, cloned voices",
                attribution=ATTRIBUTION,
                installed=True,
                version="0.1",
                supports_synthesize_streaming=True,
                voices=[
                    TtsVoice(
                        name=v,
                        description=v,
                        attribution=ATTRIBUTION,
                        installed=True,
                        version=None,
                        languages=["en"],
                    )
                    for v in list_voices()
                ],
            )
        ]
    )


# shortcut: no Zeroconf advertisement, add the service in Home Assistant by host and port;
# add wyoming's HomeAssistantZeroconf here if auto-discovery is wanted.
class WyomingHandler(AsyncEventHandler):
    """Mirrors wyoming-piper: one audio-start/stop per sentence in streaming mode."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.streaming = False
        self.voice = None
        self.buffer = ""

    async def speak(self, text: str) -> None:
        text = text.strip()[:MAX_INPUT_CHARS]
        if not text:
            return
        try:
            voice = resolve_voice(self.voice)
        except KeyError:
            voice = resolve_voice(None)
        await self.write_event(AudioStart(rate=RATE, width=2, channels=1).event())
        async for pcm in synth(text, voice):
            await self.write_event(AudioChunk(rate=RATE, width=2, channels=1, audio=pcm).event())
        await self.write_event(AudioStop().event())

    async def handle_event(self, event: Event) -> bool:
        if Describe.is_type(event.type):
            await self.write_event(wyoming_info().event())
        elif Synthesize.is_type(event.type):
            if not self.streaming:  # HA also sends the full text inside a streaming session
                request = Synthesize.from_event(event)
                self.voice = request.voice.name if request.voice else None
                await self.speak(request.text)
        elif SynthesizeStart.is_type(event.type):
            start = SynthesizeStart.from_event(event)
            self.streaming, self.buffer = True, ""
            self.voice = start.voice.name if start.voice else None
        elif SynthesizeChunk.is_type(event.type):
            self.buffer += SynthesizeChunk.from_event(event).text
            *sentences, self.buffer = SENTENCE_END.split(self.buffer)
            for sentence in sentences:
                await self.speak(sentence)
        elif SynthesizeStop.is_type(event.type):
            await self.speak(self.buffer)
            self.streaming, self.buffer = False, ""
            await self.write_event(SynthesizeStopped().event())
        return True
