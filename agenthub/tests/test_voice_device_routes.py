import io
import sys
import tempfile
import threading
import time
import types
import unittest
import wave
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
import voice
import voice_devices
import voice_gate
from store import MessageStore


def wav(seconds=1.0, rate=16000):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds))
    return buf.getvalue()


class DeviceRoutes(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        voice_devices.data_dir = root
        voice_gate.reset()
        self.store = MessageStore(str(root / "log.jsonl"))
        self.dev, self.key = voice_devices.create("office-dot", "home")
        self.auth = {"Authorization": f"Bearer {self.key}"}
        registry = types.SimpleNamespace(get_active_names=lambda: ["hazel"], get_instance=lambda n: {"label": "Hazel"})
        api = FastAPI()
        for path, fn, methods in (("/api/device/listen", app.device_listen, ["POST"]),
                                  ("/api/device/reply", app.device_reply, ["GET"]),
                                  ("/api/device/audio/{message_id}", app.device_audio, ["GET"]),
                                  ("/api/device/played", app.device_played, ["POST"]),
                                  ("/voice-devices.txt", app.voice_devices_txt, ["GET"])):
            api.add_api_route(path, fn, methods=methods)
        self.transcripts = []
        self.answer = '{"to": "hazel"}'
        for name, value in {"app": api, "store": self.store, "registry": registry, "agent_manager": None,
                            "room_settings": {"username": "alice"}}.items():
            self.stack.enter_context(patch.object(app, name, value))
        self.stack.enter_context(patch.object(app, "_voice_check_spec", lambda: {"name": "voice-check"}))
        self.asked = []
        self.stack.enter_context(patch.object(app, "_voice_ask", lambda spec, prompt: (self.asked.append(prompt), self.answer)[1]))
        self.stack.enter_context(patch.object(voice, "transcribe", lambda b: self.transcripts.pop(0)))
        self.stack.enter_context(patch.object(voice, "streams", lambda p: False))
        self.stack.enter_context(patch.object(voice, "speak", lambda text, *a: (b"RIFF....WAVEfake:" + text.encode(), "audio/wav")))
        app._install_security_middleware("session", {"server": {"port": 8787}})
        self.c = self.stack.enter_context(TestClient(api, base_url="http://127.0.0.1"))

    def listen(self, said, **kw):
        self.transcripts.append(said)
        return self.c.post("/api/device/listen", content=wav(), headers={**self.auth, "Content-Type": "audio/wav"}, **kw)

    def test_listen_requires_device_key(self):
        self.transcripts.append("Hazel, lights off.")
        r = self.c.post("/api/device/listen", content=wav(), headers={"Content-Type": "audio/wav"})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(self.transcripts, ["Hazel, lights off."])  # STT never ran
        for method, path in (("get", "/api/device/reply?after=1"), ("get", "/api/device/audio/1"),
                             ("post", "/api/device/played")):
            self.assertEqual(getattr(self.c, method)(path).status_code, 401, path)

    def test_listen_wav_named_is_posted_with_device_metadata(self):
        r = self.listen("Hazel, turn off the office lights.").json()
        self.assertEqual((r["accepted"], r["to"], r["reason"]), (True, "hazel", "named"))
        m = self.store.get_recent(1, channel="home")[-1]
        self.assertEqual(m["text"], "@hazel Hazel, turn off the office lights.")
        self.assertEqual(m["sender"], "alice")
        heard = m["metadata"].pop("heard")   # timings kept with a spoken message
        self.assertEqual(m["metadata"], {"voice": True, "device": "office-dot", "to": "hazel"})
        self.assertEqual((heard["audio_s"], heard["reason"], sorted(heard)), (1.0, "named", ["audio_s", "check_s", "reason", "stt_s"]))

    def test_listen_direct_skips_the_name_and_thinking(self):
        self.transcripts.append("lights on")
        r = self.c.post("/api/device/listen?direct=1", content=wav(),
                        headers={**self.auth, "Content-Type": "audio/wav"}).json()
        self.assertEqual((r["accepted"], r["to"], r["reason"]), (True, "hazel", "direct"))
        self.assertEqual(self.asked, [])  # no voice check
        m = self.store.get_recent(1, channel="home")[-1]
        self.assertEqual(m["text"], "@hazel lights on")
        self.assertEqual(m["metadata"]["effort"], "none")
        self.assertEqual(voice_gate.open_for("home", source="office-dot"), "hazel")

    def test_direct_goes_to_the_agent_with_a_voice(self):
        reg = types.SimpleNamespace(get_active_names=lambda: ["claudecode", "hazel"], get_instance=lambda n: {"label": n})
        mgr = types.SimpleNamespace(specs={"hazel": {"voice_provider": "Chatterbox"}, "claudecode": {}},
                                    in_channel=lambda n, ch: True)
        self.stack.enter_context(patch.object(app, "registry", reg))
        self.stack.enter_context(patch.object(app, "agent_manager", mgr))
        r = self.c.post("/api/device/listen?direct=1", json={"text": "what time is it"}, headers=self.auth).json()
        self.assertEqual((r["reason"], r["to"]), ("direct", "hazel"))

    def test_passphrase_runs_the_held_action_and_is_never_posted(self):
        import mcp_hub
        import passphrase
        passphrase.data_dir = voice_devices.data_dir
        passphrase._held.clear()
        passphrase.set_passphrase("blue bird")
        ran = []
        self.stack.enter_context(patch.object(mcp_hub, "call_tool", lambda srv, tool, args: (ran.append((tool, args)), "done")[1]))
        passphrase.hold("home", "hazel", {"name": "home_assistant"}, "intent__HassTurnOn", {"name": "Garage Door"})
        self.transcripts.append("Blue bird.")
        r = self.c.post("/api/device/listen?direct=1", content=wav(), headers={**self.auth, "Content-Type": "audio/wav"}).json()
        self.assertEqual((r["accepted"], r["reason"]), (True, "passphrase"))
        self.assertEqual(ran, [("intent__HassTurnOn", {"name": "Garage Door"})])
        texts = " ".join(m["text"] for m in self.store.get_recent(10, channel="home"))
        self.assertNotIn("blue", texts.lower())
        self.assertIn("Passphrase accepted", texts)
        self.assertFalse(passphrase.waiting("home"))

    def test_listen_text_body(self):
        r = self.c.post("/api/device/listen", json={"text": "Hazel, what time is it?"}, headers=self.auth).json()
        self.assertTrue(r["accepted"])

    def test_listen_rejected_leaves_no_message(self):
        self.answer = '{"to": null}'
        r = self.listen("Hazel is my assistant, she runs the lights.").json()
        self.assertEqual((r["accepted"], r["reason"]), (False, "about"))
        self.assertNotIn("text", r)
        self.assertEqual(self.store.get_recent(5, channel="home"), [])

    def test_listen_empty_or_noise_transcript_is_dropped(self):
        for said in ("[BLANK_AUDIO]", "(noise)", "", "  "):
            r = self.listen(said).json()
            self.assertEqual(r["reason"], "empty", said)
        self.assertEqual(self.asked, [])

    def test_listen_rejects_oversize_or_non_wav(self):
        big = self.c.post("/api/device/listen", content=wav(seconds=31), headers={**self.auth, "Content-Type": "audio/wav"})
        self.assertIn(big.status_code, (400, 413))
        junk = self.c.post("/api/device/listen", content=b"not a wav", headers={**self.auth, "Content-Type": "audio/wav"})
        self.assertEqual(junk.status_code, 400)

    def test_reply_long_poll_returns_agent_reply_with_speakable_text(self):
        mid = self.listen("Hazel, find the KiCad site.").json()["message_id"]
        def later():
            time.sleep(0.3)
            self.store.add("hazel", "Here: [KiCad](https://www.kicad.org/) is the site.", channel="home")
        threading.Thread(target=later).start()
        r = self.c.get(f"/api/device/reply?after={mid}&wait=5", headers=self.auth).json()
        self.assertIn("kicad.org", r["text"])
        self.assertNotIn("http", r["speak"])
        self.assertEqual(r["audio_url"], f"/api/device/audio/{r['message_id']}")
        audio = self.c.get(r["audio_url"], headers=self.auth)
        self.assertEqual(audio.status_code, 200)
        self.assertTrue(audio.content.startswith(b"RIFF"))
        self.assertNotIn(b"http", audio.content)  # speakable text was synthesised
        # the reply is now echo-protected on this device
        self.assertEqual(self.listen("Here is the site").json()["reason"], "echo")

    def test_reply_times_out_with_null_text(self):
        mid = self.listen("Hazel, are you there?").json()["message_id"]
        t0 = time.time()
        r = self.c.get(f"/api/device/reply?after={mid}&wait=1", headers=self.auth).json()
        self.assertIsNone(r["text"])
        self.assertLess(time.time() - t0, 3)

    def test_reply_and_audio_only_for_this_devices_messages(self):
        other = self.store.add("alice", "@hazel hi", channel="elsewhere")
        self.assertEqual(self.c.get(f"/api/device/reply?after={other['id']}&wait=1", headers=self.auth).status_code, 404)
        foreign = self.store.add("hazel", "secret", channel="elsewhere")
        self.assertEqual(self.c.get(f"/api/device/audio/{foreign['id']}", headers=self.auth).status_code, 404)

    def test_played_extends_device_window(self):
        self.listen("Hazel, read me the news.")
        real = time.time
        with patch.object(voice_gate.time, "time", lambda: real() + 25):
            self.c.post("/api/device/played", headers=self.auth)
        self.assertEqual(voice_gate.open_for("home", now=real() + 50, source="office-dot"), "hazel")

    def test_protocol_doc_lists_routes(self):
        t = self.c.get("/voice-devices.txt").text
        for route in ("/api/device/listen", "/api/device/reply", "/api/device/audio/", "/api/device/played", "Bearer"):
            self.assertIn(route, t)


if __name__ == "__main__":
    unittest.main()
