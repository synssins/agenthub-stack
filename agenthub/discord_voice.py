"""Discord voice for discord_gateway bots: hear the people in a voice channel, speak the agent's replies.

Each person's speech is cut into utterances (Discord stops sending packets when someone goes quiet,
so an utterance ends after SILENCE_S without audio), turned into 16 kHz mono WAV and transcribed by
AgentHub's own speech to text (voice.transcribe). Replies are spoken with the agent's own voice
(voice.speak) through ffmpeg.

Discord voice is end-to-end encrypted (DAVE). discord-ext-voice-recv cannot decrypt it, so the sink
asks for raw Opus, decrypts it with the call's DAVE session and decodes it here. Frames the session
lets through unencrypted (silence at the end of speech, a session that is not ready yet) are decoded
as they are.
"""

from __future__ import annotations

import asyncio
import io
import logging
import threading
import time
import wave
from array import array

log = logging.getLogger(__name__)
SILENCE_S = 0.8     # quiet this long ends an utterance
MIN_S = 0.5         # shorter sounds (a cough, a click) are dropped
MAX_S = 30.0        # longer speech is cut here
MIN_RMS = 250       # quieter utterances (background noise) are dropped
BYTES_PER_S = 48000 * 2 * 2   # Discord PCM: 48 kHz, stereo, 16-bit


def to_wav16k(pcm: bytes) -> bytes:
    """48 kHz stereo 16-bit PCM -> 16 kHz mono WAV (both channels and three frames averaged)."""
    a = array("h")
    a.frombytes(pcm[: len(pcm) // 12 * 12])
    mono = array("h", (sum(a[i:i + 6]) // 6 for i in range(0, len(a), 6)))
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(mono.tobytes())
    return out.getvalue()


def rms(pcm: bytes) -> float:
    a = array("h")
    a.frombytes(pcm[: len(pcm) // 2 * 2])
    return (sum(x * x for x in a) / len(a)) ** 0.5 if a else 0.0


def decode(vc, user_id: int, opus: bytes, decoders: dict) -> bytes:
    """Opus from one speaker -> 48 kHz stereo PCM, DAVE-decrypted first when the call uses it.
    Returns b"" for a frame that cannot be used; never raises (a raising sink stops every speaker)."""
    conn = getattr(vc, "_connection", None)
    dave = getattr(conn, "dave_session", None)
    if dave is not None and getattr(conn, "dave_protocol_version", 0):
        try:
            import davey
            opus = dave.decrypt(user_id, davey.MediaType.audio, bytes(opus))
        except Exception as exc:
            try:
                plain = "Unencrypted" in str(exc) or not dave.ready or dave.can_passthrough(user_id)
            except Exception:
                plain = False
            if not plain:
                return b""
    try:
        from discord.opus import Decoder
        dec = decoders.get(user_id) or decoders.setdefault(user_id, Decoder())
        return dec.decode(bytes(opus), fec=False)
    except Exception:
        return b""


class Hearing:
    """Collects each speaker's audio in one voice connection and hands over finished utterances:
    on_utterance(member, pcm) is run as a coroutine on loop."""

    def __init__(self, loop: asyncio.AbstractEventLoop, on_utterance):
        self.loop = loop
        self.on_utterance = on_utterance
        self.buf: dict[int, bytearray] = {}
        self.last: dict[int, float] = {}
        self.members: dict[int, object] = {}
        self.lock = threading.Lock()
        self.closed = threading.Event()
        threading.Thread(target=self._closer, daemon=True, name="discord-voice-closer").start()

    def add(self, member, pcm: bytes):
        with self.lock:
            b = self.buf.setdefault(member.id, bytearray())
            b += pcm
            self.last[member.id] = time.monotonic()
            self.members[member.id] = member
            full = len(b) >= MAX_S * BYTES_PER_S
        if full:
            self._flush(member.id)

    def _flush(self, uid: int):
        with self.lock:
            pcm = bytes(self.buf.pop(uid, b""))
            self.last.pop(uid, None)
            member = self.members.get(uid)
        if member is not None and len(pcm) >= MIN_S * BYTES_PER_S and rms(pcm) >= MIN_RMS:
            asyncio.run_coroutine_threadsafe(self.on_utterance(member, pcm), self.loop)

    def _closer(self):
        while not self.closed.wait(0.1):
            now = time.monotonic()
            with self.lock:
                quiet = [u for u, t in self.last.items() if now - t >= SILENCE_S]
            for uid in quiet:
                self._flush(uid)

    def close(self):
        self.closed.set()

    def sink(self):
        from discord.ext import voice_recv
        hearing = self

        class Sink(voice_recv.AudioSink):
            def __init__(self):
                super().__init__()
                self.decoders: dict = {}

            def wants_opus(self) -> bool:
                return True   # DAVE: decrypted and decoded here, see the module notes

            def write(self, user, data):
                if user is None or getattr(user, "bot", False) or not data.opus:
                    return
                pcm = decode(self.voice_client, user.id, data.opus, self.decoders)
                if pcm:
                    hearing.add(user, pcm)

            def cleanup(self):
                hearing.close()

        return Sink()


async def play(vc, audio: bytes, rate: int | None = None):
    """Plays one reply and returns when it ends. rate: audio is raw 16-bit mono PCM at that rate
    (a streaming speech provider); otherwise any format ffmpeg reads."""
    import discord
    loop = asyncio.get_running_loop()
    done = asyncio.Event()
    while vc.is_playing():
        await asyncio.sleep(0.2)
    before = f"-f s16le -ar {int(rate)} -ac 1" if rate else None
    vc.play(discord.FFmpegPCMAudio(io.BytesIO(audio), pipe=True, before_options=before),
            after=lambda err: (err and log.error("discord voice: playback failed: %s", err),
                               loop.call_soon_threadsafe(done.set)))
    await done.wait()
