"""Self-check against a running server: python test_server.py (inside the container)."""
import asyncio, json, time, urllib.error, urllib.request
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.client import AsyncTcpClient
from wyoming.info import Describe, Info
from wyoming.tts import Synthesize, SynthesizeChunk, SynthesizeStart, SynthesizeStop, SynthesizeStopped, SynthesizeVoice

BASE = "http://127.0.0.1:4125"


def post(body):
    req = urllib.request.Request(BASE + "/v1/audio/speech", json.dumps(body).encode(), {"Content-Type": "application/json"})
    return urllib.request.urlopen(req, timeout=120)


def openai_checks():
    for text in ("Sure, I can help with that.", "[laugh] Oh no, you really did that? [sigh] Fine, I'll fix it."):
        t = time.time(); r = post({"input": text, "voice": "jessica", "response_format": "pcm"})
        first = r.read(2); ttfb = time.time() - t; n = 2 + len(r.read()); tot = time.time() - t
        assert first and n > 24000, "no audio"
        assert r.headers["X-Sample-Rate"] == "24000"
        print(f"openai pcm: first audio {ttfb*1000:.0f}ms total {tot*1000:.0f}ms audio {n/48000:.2f}s")
    assert post({"input": "Hi.", "response_format": "wav"}).read()[:4] == b"RIFF"  # read it all, or the GPU stays busy into the next check
    for bad in ({"input": "Hi.", "voice": "nobody"}, {"input": "Hi.", "voice": "../etc/passwd"}, {"input": "Hi.", "response_format": "mp3"}, {"input": ""}):
        try:
            post(bad); raise AssertionError(f"accepted {bad}")
        except urllib.error.HTTPError as e:
            assert e.code in (400, 422), e.code
    print("openai: bad requests rejected")


async def collect(client, until):
    starts = stops = audio = 0; first = None; t = time.time()
    while True:
        ev = await asyncio.wait_for(client.read_event(), 120)
        assert ev is not None, "server closed connection"
        if AudioStart.is_type(ev.type): starts += 1
        elif AudioChunk.is_type(ev.type):
            first = first or time.time() - t; audio += len(AudioChunk.from_event(ev).audio)
        elif AudioStop.is_type(ev.type): stops += 1
        if until(ev): return starts, stops, audio, first


async def wyoming_checks():
    async with AsyncTcpClient("127.0.0.1", 10220) as c:
        await c.write_event(Describe().event())
        info = Info.from_event(await c.read_event()); tts = info.tts[0]
        names = [v.name for v in tts.voices]
        assert tts.supports_synthesize_streaming and "Jessica" in names, names
        print("wyoming describe:", names)
        await c.write_event(Synthesize(text="Sure, I can help with that.", voice=SynthesizeVoice(name="Jessica")).event())
        s, e, a, f = await collect(c, lambda ev: AudioStop.is_type(ev.type))
        assert (s, e) == (1, 1) and a > 24000
        print(f"wyoming synthesize: first audio {f*1000:.0f}ms audio {a/48000:.2f}s")
        await c.write_event(SynthesizeStart(voice=SynthesizeVoice(name="Jessica")).event())
        for part in ("Hello there. ", "How are ", "you today? I am", " fine"):
            await c.write_event(SynthesizeChunk(text=part).event())
        await c.write_event(Synthesize(text="Hello there. How are you today? I am fine").event())  # HA's duplicate, must be ignored
        await c.write_event(SynthesizeStop().event())
        s, e, a, f = await collect(c, lambda ev: SynthesizeStopped.is_type(ev.type))
        assert (s, e) == (3, 3), (s, e)
        print(f"wyoming streaming: 3 sentences, first audio {f*1000:.0f}ms audio {a/48000:.2f}s")


openai_checks()
asyncio.run(wyoming_checks())
print("ALL OK")
