import io
import json
import os
import socket
import sys
import tempfile
import threading
import unittest
import wave
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import voice


def fake_wyoming(reply: str):
    """One-shot Wyoming server: reads events until audio-stop, answers with a transcript."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    got = {"audio": b"", "types": []}

    def run():
        conn, _ = srv.accept()
        f = conn.makefile("rb")
        while True:
            ev = json.loads(f.readline())
            got["types"].append(ev["type"])
            if ev.get("payload_length"):
                got["audio"] += f.read(ev["payload_length"])
            if ev["type"] == "audio-stop":
                break
        data = json.dumps({"text": reply}).encode()
        conn.sendall(json.dumps({"type": "transcript", "data_length": len(data)}).encode() + b"\n" + data)
        conn.close()
        srv.close()
    threading.Thread(target=run, daemon=True).start()
    return srv.getsockname()[1], got


class Voice(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        voice.data_dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_emotion_tags_kept_only_when_the_engine_has_them(self):
        t = "Well [Laugh] that worked. [sighs] Next [1] item [not a tag at all, far too long to be one]."
        self.assertEqual(voice.speakable(t, ["[laugh]"]), "Well [laugh] that worked. Next [1] item [not a tag at all, far too long to be one].")
        self.assertEqual(voice.speakable(t, []), "Well that worked. Next [1] item [not a tag at all, far too long to be one].")
        self.assertIn("[sighs]", voice.speakable(t))  # no engine named: left alone
        self.assertEqual(voice.parse_tags("[Laugh], sigh\n[x]; bad!,[laugh]"), ["[laugh]", "[sigh]"])
        self.assertEqual(voice.tags({"tags": []}), [])
        voice.save({**voice.load(), "providers": [{"name": "A", "base_url": "http://127.0.0.1:9/v1", "tags": ["[laugh]"]},
                                                  {"name": "B", "base_url": "http://127.0.0.1:9/v1", "tags": ["[sigh]", "[laugh]"]}]})
        self.assertEqual(voice.all_tags(), ["[laugh]", "[sigh]"])
        self.assertEqual(voice.tags({"base_url": "http://127.0.0.1:9/v1"}), [])  # nothing listening: none

    def test_speakable_drops_code_links_and_markup(self):
        t = voice.speakable("Done **now**:\n```py\nx = 1\n```\nSee https://a.b/c and @bob [docs](http://d) [artifact:abc123]")
        self.assertEqual(t, "Done now : (code block) See and bob (artifact)")  # links are not read aloud

    def test_transcribe_streams_pcm_to_wyoming(self):
        port, got = fake_wyoming("hello there")
        voice.save({**voice.load(), "stt": {"wyoming": f"127.0.0.1:{port}", "language": "en"}})
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
            w.writeframes(b"\x01\x00" * 40000)   # 2.5 s
        self.assertEqual(voice.transcribe(buf.getvalue()), "hello there")
        self.assertEqual(got["types"][:2], ["transcribe", "audio-start"])
        self.assertEqual(got["types"][-1], "audio-stop")
        self.assertEqual(len(got["audio"]), 80000)

    def test_whisper_hallucinations_are_dropped_but_real_speech_kept(self):
        for junk in ("Thanks for watching!", "Thank you.", "you", "[BLANK_AUDIO]", "Please subscribe."):
            self.assertEqual(voice.clean_transcript(junk), "", junk)
        self.assertEqual(voice.clean_transcript("Thank you, now draw a cat."), "Thank you, now draw a cat.")

    def test_streaming_provider_passes_pcm_through_in_chunks(self):
        from http.server import BaseHTTPRequestHandler, HTTPServer
        seen = {}

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                seen.update(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(200)
                self.send_header("X-Sample-Rate", "22050")
                self.end_headers()
                for _ in range(3):
                    self.wfile.write(b"\x01\x00" * 100)
                    self.wfile.flush()

            def log_message(self, *a):
                pass
        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.handle_request, daemon=True).start()
        voice.save({**voice.load(), "providers": [{"name": "T", "base_url": f"http://127.0.0.1:{srv.server_port}/v1", "stream": True, "tags": []}],
                    "default_provider": "T"})
        self.assertTrue(voice.streams(None))
        rate, chunks = voice.speak_stream("Hello there.", None, "Jessica")
        self.assertEqual(rate, 22050)
        self.assertEqual(len(b"".join(chunks)), 600)
        self.assertEqual((seen["stream"], seen["response_format"], seen["voice"]), (True, "pcm", "Jessica"))
        srv.server_close()

    def serve(self, routes: dict, hits: list):
        """A provider stub: {path: (content type, body bytes)}; hits collects (path, json body)."""
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class H(BaseHTTPRequestHandler):
            def reply(self, body=None):
                hits.append((self.path, body))
                ctype, data = routes[self.path]
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self.reply()

            def do_POST(self):
                self.reply(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))

            def log_message(self, *a):
                pass
        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        return f"http://127.0.0.1:{srv.server_port}"

    OPENAPI = {"paths": {"/v1/audio/speech": {"post": {"requestBody": {"content": {"application/json": {
        "schema": {"$ref": "#/components/schemas/SpeechRequest"}}}}}}},
        "components": {"schemas": {"SpeechRequest": {"properties": {
            "input": {"type": "string"}, "voice": {"type": "string"}, "speed": {"type": "number"},
            "exaggeration": {"anyOf": [{"type": "number", "minimum": 0.25, "maximum": 2.0}, {"type": "null"}],
                             "title": "Exaggeration", "default": 0.5},
            "lang_code": {"type": "string", "enum": ["a", "b"]},
            "normalization_options": {"type": "object"}}}}}}

    def test_parameters_are_read_from_the_service_and_checked(self):
        hits = []
        base = self.serve({"/openapi.json": ("application/json", json.dumps(self.OPENAPI).encode()),
                           "/caps": ("application/json", json.dumps({"parameters": [
                               {"name": "text", "type": "string"},
                               {"name": "num_steps", "type": "integer", "min": 4, "max": 128, "default": 16,
                                "advanced": True, "description": "Steps"}]}).encode())}, hits)
        specs = voice.capabilities({"base_url": base + "/v1"})
        self.assertEqual([x["name"] for x in specs], ["exaggeration", "lang_code"])  # core and object fields left out
        self.assertEqual((specs[0]["type"], specs[0]["min"], specs[0]["max"], specs[0]["default"]), ("number", 0.25, 2.0, 0.5))
        self.assertEqual(voice.check_parameters(specs, {"exaggeration": "0.7", "lang_code": "", "gone": 1}),
                         ({"exaggeration": 0.7}, None))
        self.assertIn("between 0.25 and 2.0", voice.check_parameters(specs, {"exaggeration": 3})[1])
        self.assertIn("one of: a, b", voice.check_parameters(specs, {"lang_code": "z"})[1])
        steps = voice.capabilities({"base_url": base + "/v1", "capabilities_url": base + "/caps"})
        self.assertEqual([(x["name"], x["advanced"]) for x in steps], [("num_steps", True)])
        self.assertIn("integer", voice.check_parameters(steps, {"num_steps": 4.5})[1])

    def test_parameters_are_sent_and_rendered_audio_is_saved_for_replay(self):
        hits = []
        base = self.serve({"/v1/audio/speech": ("audio/wav", b"RIFFaudio")}, hits)
        voice.save({**voice.load(), "default_provider": "P", "providers": [
            {"name": "P", "base_url": base + "/v1", "format": "wav", "tags": [], "parameters": {"exaggeration": 0.7, "voice": "x"}}]})
        self.assertEqual(voice.speak("Hello there.", None, "Nora"), (b"RIFFaudio", "audio/wav"))
        self.assertEqual(hits[0][1], {"exaggeration": 0.7, "input": "Hello there.", "voice": "Nora", "response_format": "wav"})
        self.assertEqual(voice.speak("Hello there.", None, "Nora"), (b"RIFFaudio", "audio/wav"))
        self.assertEqual(len(hits), 1)                      # the replay came from disk
        voice.speak("Hello there.", None, "Other")           # another voice is another recording
        self.assertEqual(len(hits), 2)
        voice.save({**voice.load(), "cache_mb": 0})
        voice.speak("Not kept.", None, "Nora")
        voice.speak("Not kept.", None, "Nora")
        self.assertEqual(len(hits), 4)

    def test_streamed_audio_is_saved_once_complete_and_old_audio_is_pruned(self):
        hits = []
        base = self.serve({"/v1/audio/speech": ("audio/pcm", b"\x01\x00" * 300)}, hits)
        voice.save({**voice.load(), "default_provider": "S", "providers": [
            {"name": "S", "base_url": base + "/v1", "stream": True, "tags": [], "sample_rate": 16000}]})
        rate, chunks = voice.speak_stream("One.", None, "v")
        next(chunks)
        chunks.close()                                       # the listener stopped early: nothing saved
        self.assertEqual(len(b"".join(voice.speak_stream("One.", None, "v")[1])), 600)
        rate, again = voice.speak_stream("One.", None, "v")
        self.assertEqual((rate, len(b"".join(again)), len(hits)), (16000, 600, 2))
        cache = voice.data_dir / "voice_cache"
        with mock.patch.object(voice, "load", lambda: {"cache_mb": 1}):
            voice._cache_put(cache / "big1", {}, b"x" * 700_000)
            os.utime(cache / "big1", (1, 1))
            voice._cache_put(cache / "big2", {}, b"x" * 700_000)
        self.assertEqual(sorted(f.name for f in cache.iterdir() if f.name.startswith("big")), ["big2"])

    def test_provider_falls_back_to_default(self):
        self.assertEqual(voice.provider("nope")["name"], "Kokoro")
        self.assertEqual(voice.provider("Chatterbox")["name"], "Chatterbox")


if __name__ == "__main__":
    unittest.main()
