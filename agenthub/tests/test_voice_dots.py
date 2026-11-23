import asyncio
import base64
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import voice_devices
import voice_dots


class FakeDot:
    """An ESPHome API device as aioesphomeapi's client sees it: a key, a few entities, their commands."""

    def __init__(self, psk=None, provisionable=False, entities=("agenthub_url", "agenthub_key", "brain",
                                                              "agenthub_listen", "replies_play_on", "agenthub_status")):
        self.psk, self.provisionable, self.values = psk, provisionable, {}
        self.entities = [types.SimpleNamespace(object_id=o, key=i + 1) for i, o in enumerate(entities)]
        self.by_key = {e.key: e.object_id for e in self.entities}
        self.listeners = []

    def client(self, host, port, noise_psk):
        dot = self

        class Client:
            async def connect(self, login=False):
                from aioesphomeapi import ZERO_NOISE_PSK
                ok = noise_psk == dot.psk or (dot.provisionable and noise_psk in (ZERO_NOISE_PSK, None))
                if not ok:
                    raise ConnectionError("handshake failed")

            async def device_info(self):
                return types.SimpleNamespace(mac_address="02:00:00:AA:BB:01", friendly_name="Echo Office", name="echo-office")

            async def noise_encryption_set_key(self, raw):
                dot.psk, dot.provisionable = base64.b64encode(raw).decode(), False
                return True

            async def list_entities_services(self):
                return dot.entities, []

            def subscribe_states(self, on_state):
                dot.listeners.append(on_state)

            def _set(self, key, value):
                dot.values[dot.by_key[key]] = value
                for f in dot.listeners:
                    f(types.SimpleNamespace(key=key, state=value))

            def text_command(self, key, state):
                self._set(key, state)

            def select_command(self, key, state):
                self._set(key, state)

            def switch_command(self, key, state):
                self._set(key, state)

            async def disconnect(self, force=False):
                dot.listeners.clear()

        return Client()


class VoiceDots(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        voice_dots.data_dir = voice_devices.data_dir = Path(d.name)
        real_sleep = asyncio.sleep
        sleeps = patch.object(voice_dots.asyncio, "sleep", lambda s: real_sleep(0))
        sleeps.start()
        self.addCleanup(sleeps.stop)

    def use(self, dot):
        p = patch.object(voice_dots, "_client", dot.client)
        p.start()
        self.addCleanup(p.stop)

    def test_adopt_sets_a_key_of_its_own_and_keeps_it(self):
        dot = FakeDot(provisionable=True)
        self.use(dot)
        r = asyncio.run(voice_dots.adopt("192.168.1.60"))
        self.assertEqual((r["mac"], r["state"]), ("020000aabb01", "adopted"))
        self.assertEqual(voice_dots._load()["020000aabb01"]["psk"], dot.psk)
        self.assertEqual(oct((voice_dots.data_dir / voice_dots.FILE).stat().st_mode & 0o777), "0o600")

    def test_adopt_refuses_a_dot_that_is_not_waiting(self):
        self.use(FakeDot(psk=base64.b64encode(b"x" * 32).decode()))
        with self.assertRaises(ValueError):
            asyncio.run(voice_dots.adopt("192.168.1.60"))

    def test_share_needs_the_right_key(self):
        key = base64.b64encode(b"k" * 32).decode()
        self.use(FakeDot(psk=key))
        with self.assertRaises(ValueError):
            asyncio.run(voice_dots.share("192.168.1.60", 6053, base64.b64encode(b"z" * 32).decode()))
        with self.assertRaises(ValueError):
            asyncio.run(voice_dots.share("192.168.1.60", 6053, "not a key"))
        self.assertEqual(asyncio.run(voice_dots.share("192.168.1.60", 6053, key))["state"], "adopted")

    def test_configure_makes_a_device_and_writes_the_dot(self):
        key = base64.b64encode(b"k" * 32).decode()
        dot = FakeDot(psk=key)
        self.use(dot)
        asyncio.run(voice_dots.share("192.168.1.60", 6053, key))
        r = asyncio.run(voice_dots.configure("020000aabb01", "http://192.168.1.50:8787", "office", True))
        self.assertTrue(r["ok"], r)
        self.assertEqual((r["device"], r["channel"]), ("echo-office", "office"))
        self.assertEqual(dot.values["agenthub_url"], "http://192.168.1.50:8787")
        self.assertEqual((dot.values["brain"], dot.values["agenthub_listen"]), ("AgentHub", True))
        self.assertTrue(voice_devices.verify(dot.values["agenthub_key"]) if hasattr(voice_devices, "verify")
                        else dot.values["agenthub_key"].startswith("vd-"))
        self.assertEqual([d["name"] for d in voice_devices.list_public()], ["echo-office"])
        # set up again: a fresh key, still one device
        asyncio.run(voice_dots.configure("020000aabb01", "http://192.168.1.50:8787", "office", False))
        self.assertEqual(len(voice_devices.list_public()), 1)

    def test_configure_refuses_old_firmware_and_unknown_dots(self):
        key = base64.b64encode(b"k" * 32).decode()
        self.use(FakeDot(psk=key, entities=("brain",)))
        with self.assertRaises(ValueError):
            asyncio.run(voice_dots.configure("020000aabb01", "http://192.168.1.50:8787", "office", True))
        asyncio.run(voice_dots.share("192.168.1.60", 6053, key))
        with self.assertRaises(ValueError):
            asyncio.run(voice_dots.configure("020000aabb01", "http://192.168.1.50:8787", "office", True))
        with self.assertRaises(ValueError):
            asyncio.run(voice_dots.configure("020000aabb01", "not an address", "office", True))

    def test_state_from_mdns(self):
        self.assertEqual(voice_dots.state_of({"api_provisioning": "zero-psk"}, "aa"), "ready")
        self.assertEqual(voice_dots.state_of({"api_encryption": "Noise"}, "aa"), "owned")
        self.assertEqual(voice_dots.state_of({}, "aa"), "open")

    def test_hub_address(self):
        self.assertEqual(voice_dots.hub_address("agenthub.local:8787", 8787), "http://agenthub.local:8787")
        self.assertEqual(voice_dots.hub_address("192.168.1.50", 8787), "http://192.168.1.50:8787")
        self.assertTrue(voice_dots.hub_address("127.0.0.1:8787", 8787).startswith("http://"))


if __name__ == "__main__":
    unittest.main()
