"""A device's microphone as a live stream: where speech starts and ends, and the route that takes it."""
import json
import math
import random
import sys
import tempfile
import types
import unittest
from array import array
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
import speakers
import voice
import voice_devices
import voice_gate
import voice_stream as V
from store import MessageStore

rng = random.Random(1)


def sound(seconds: float, level: int) -> bytes:
    """A 300 Hz tone of that peak over a little room noise."""
    n = int(V.RATE * seconds)
    return array("h", (int(level * math.sin(i * 2 * math.pi * 300 / V.RATE)) + rng.randint(-40, 40) for i in range(n))).tobytes()


class Segments(unittest.TestCase):
    def kinds(self, s, pcm, step=640):   # 20 ms at a time, as a device sends
        out = []
        for i in range(0, len(pcm), step):
            out += s.feed(pcm[i:i + step])
        return out

    def test_speech_is_cut_at_its_pause(self):
        s = V.Segmenter()
        ev = self.kinds(s, sound(1.0, 0) + sound(1.2, 6000) + sound(0.25, 0) + sound(0.8, 6000) + sound(1.2, 0))
        self.assertEqual([e[0] for e in ev], ["start", "soon", "soon", "end"])   # the short pause did not end it
        self.assertNotEqual(ev[1][1], ev[3][1])     # what was transcribed in the first pause is stale
        self.assertEqual(ev[2][1], ev[3][1])        # ... and in the last one it is the whole speech
        seconds = len(ev[3][1]) / 2 / V.RATE
        self.assertTrue(2.4 <= seconds <= 3.1, seconds)   # pre-roll + speech + tail, not the pause after
        self.assertFalse(s.active)

    def test_a_noise_is_not_speech(self):
        s = V.Segmenter()
        ev = self.kinds(s, sound(1.0, 0) + sound(0.15, 9000) + sound(2.5, 0))
        self.assertEqual([e[0] for e in ev], ["start", "drop"])

    def test_the_devices_own_sound_drops_what_was_collected(self):
        s = V.Segmenter()
        self.kinds(s, sound(1.0, 0) + sound(0.6, 6000))
        self.assertTrue(s.active)
        s.reset()
        self.assertEqual(self.kinds(s, sound(1.0, 0)), [])

    def test_a_steady_loud_room_is_not_speech(self):
        s = V.Segmenter()
        self.assertEqual([e[0] for e in self.kinds(s, sound(0.3, 0) + sound(0.5, 2000) + sound(6, 300))][:1], ["start"])
        self.assertEqual(self.kinds(s, sound(4.0, 300)), [])   # the room's level is the floor now


class Route(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        voice_devices.data_dir = speakers.data_dir = root
        speakers._enroll = None
        voice_gate.reset()
        self.store = MessageStore(str(root / "log.jsonl"))
        _, self.key = voice_devices.create("office-dot", "home")
        registry = types.SimpleNamespace(get_active_names=lambda: ["hazel"], get_instance=lambda n: {"label": "Hazel"})
        api = FastAPI()
        api.add_api_websocket_route("/api/device/stream", app.device_stream)
        for name, value in {"app": api, "store": self.store, "registry": registry, "agent_manager": None,
                            "room_settings": {"username": "alice"}}.items():
            self.stack.enter_context(patch.object(app, name, value))
        self.stack.enter_context(patch.object(app, "client_allowed", lambda ip: True))
        self.stack.enter_context(patch.object(app, "_host_known", lambda h: True))
        self.stack.enter_context(patch.object(app, "_voice_check_spec", lambda: {"name": "voice-check"}))
        self.stack.enter_context(patch.object(app, "_voice_ask", lambda spec, prompt: '{"to": "hazel"}'))
        self.heard = []
        self.stack.enter_context(patch.object(voice, "transcribe", lambda b: (self.heard.append(len(b)), "Hazel, what time is it?")[1]))
        self.client = TestClient(api)

    def test_speech_in_the_stream_is_answered(self):
        with self.client.websocket_connect("/api/device/stream", headers={"Authorization": f"Bearer {self.key}"}) as ws:
            pcm = sound(1.0, 0) + sound(1.5, 6000) + sound(1.0, 0)
            for i in range(0, len(pcm), 640):
                ws.send_bytes(pcm[i:i + 640])
            r = json.loads(ws.receive_text())
        self.assertEqual((r["accepted"], r["to"], r["reason"]), (True, "hazel", "named"))
        self.assertEqual(len(self.heard), 1)          # transcribed once: in the pause, not again at its end
        self.assertEqual(self.store.get_recent(1)[0]["text"], "@hazel Hazel, what time is it?")

    def test_an_unknown_key_is_closed(self):
        from starlette.websockets import WebSocketDisconnect
        with self.client.websocket_connect("/api/device/stream", headers={"Authorization": "Bearer nope"}) as ws:
            with self.assertRaises(WebSocketDisconnect) as c:
                ws.receive_text()
        self.assertEqual(c.exception.code, 4401)


if __name__ == "__main__":
    unittest.main()
