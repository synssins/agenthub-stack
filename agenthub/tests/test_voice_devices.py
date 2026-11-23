import json
import os
import stat
import sys
import tempfile
import types
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import admin_api
import voice_devices
from registry import RuntimeRegistry
from store import MessageStore


class VoiceDevices(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        voice_devices.data_dir = self.dir

    def test_key_shown_once_and_stored_hashed(self):
        dev, key = voice_devices.create("office-dot", "home")
        self.assertTrue(key.startswith("vd-"))
        raw = (self.dir / "voice_devices.json").read_text()
        self.assertNotIn(key, raw)
        self.assertEqual(stat.S_IMODE(os.stat(self.dir / "voice_devices.json").st_mode), 0o600)
        self.assertEqual(dev["name"], "office-dot")
        self.assertNotIn("hash", json.dumps(voice_devices.list_public()))

    def test_authenticate_valid_invalid_deleted(self):
        _, key = voice_devices.create("office-dot", "home")
        self.assertEqual(voice_devices.authenticate(f"Bearer {key}")["name"], "office-dot")
        self.assertIsNotNone(voice_devices.list_public()[0]["last_seen"])
        self.assertIsNone(voice_devices.authenticate("Bearer vd-wrong"))
        self.assertIsNone(voice_devices.authenticate(""))
        self.assertTrue(voice_devices.delete("office-dot"))
        self.assertIsNone(voice_devices.authenticate(f"Bearer {key}"))

    def test_bad_or_duplicate_name_rejected(self):
        voice_devices.create("office-dot", "home")
        with self.assertRaises(ValueError):
            voice_devices.create("office-dot", "home")
        with self.assertRaises(ValueError):
            voice_devices.create("Bad Name!", "home")

    def test_channel_names_follow_room_rules(self):
        with self.assertRaises(ValueError):
            voice_devices.create("dot", "office_dot")          # room channels: letters, digits, -
        with self.assertRaises(ValueError):
            voice_devices.create("dot", "a" * 21)              # room channels: at most 20
        voice_devices.create("dot", "office")
        self.assertFalse(voice_devices.set_channel("dot", "Bad_Name"))
        self.assertTrue(voice_devices.set_channel("dot", "Kitchen"))   # lowercased
        self.assertEqual(voice_devices.list_public()[0]["channel"], "kitchen")

    def test_set_channel(self):
        voice_devices.create("office-dot", "home")
        self.assertTrue(voice_devices.set_channel("office-dot", "office"))
        self.assertEqual(voice_devices.list_public()[0]["channel"], "office")
        self.assertFalse(voice_devices.set_channel("nope", "office"))


class VoiceDeviceAdmin(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = tmp.name
        hub = types.SimpleNamespace(registry=RuntimeRegistry(data_dir=d), store=MessageStore(str(Path(d) / "log.jsonl")),
                                    room_settings={"channels": ["general"]}, _save_settings=lambda: None)
        self.hub = hub
        admin_api.setup(hub, {"server": {"data_dir": d}})
        app = FastAPI()
        app.include_router(admin_api.router)
        self.c = TestClient(app, base_url="http://127.0.0.1")

    def test_admin_routes_never_return_hash_or_key_after_create(self):
        r = self.c.post("/api/admin/voice-devices", json={"name": "office-dot", "channel": "home"})
        self.assertEqual(r.status_code, 200)
        key = r.json()["key"]
        listed = self.c.get("/api/admin/voice-devices").text
        self.assertNotIn(key, listed)
        self.assertNotIn("hash", listed)
        self.assertEqual(self.c.post("/api/admin/voice-devices", json={"name": "office-dot", "channel": "home"}).status_code, 400)
        self.assertEqual(self.c.patch("/api/admin/voice-devices/office-dot", json={"channel": "office"}).status_code, 200)
        self.assertEqual(self.hub.room_settings["channels"], ["general", "home", "office"])  # channels created
        self.assertEqual(self.c.delete("/api/admin/voice-devices/office-dot").status_code, 200)
        self.assertEqual(self.c.get("/api/admin/voice-devices").json(), [])


class DeviceMiddleware(unittest.TestCase):
    def test_device_route_passes_middleware_with_key_only(self):
        import app as appmod
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        voice_devices.data_dir = Path(tmp.name)
        _, key = voice_devices.create("office-dot", "home")
        saved_app = appmod.app
        appmod.app = FastAPI()
        try:
            appmod._install_security_middleware("tok", {"server": {"port": 8787}})

            @appmod.app.get("/api/device/ping")
            async def ping(request: appmod.Request):
                dev = voice_devices.authenticate(request.headers.get("authorization", ""))
                return appmod.JSONResponse({"device": dev["name"]} if dev else {"error": "no"},
                                           status_code=200 if dev else 401)

            @appmod.app.get("/api/other")
            async def other():
                return {"ok": True}
            c = TestClient(appmod.app, base_url="http://127.0.0.1")
            self.assertEqual(c.get("/api/device/ping", headers={"Authorization": f"Bearer {key}"}).json(),
                             {"device": "office-dot"})
            self.assertEqual(c.get("/api/device/ping").status_code, 401)
            self.assertEqual(c.get("/api/other", headers={"Authorization": f"Bearer {key}"}).status_code, 403)
        finally:
            appmod.app = saved_app


if __name__ == "__main__":
    unittest.main()
