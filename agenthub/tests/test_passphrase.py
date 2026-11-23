import tempfile
import unittest
from pathlib import Path

import passphrase


class Passphrase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        passphrase.data_dir = Path(self.dir.name)
        passphrase._held.clear()

    def test_said_in_a_sentence_and_spoken_digits(self):
        self.assertFalse(passphrase.configured())
        passphrase.set_passphrase("Blue Bird 4 2")
        self.assertTrue(passphrase.configured())
        self.assertTrue(passphrase.said("It's blue bird, forty... no: blue bird four two."))
        self.assertTrue(passphrase.said("bluebird".replace("bluebird", "blue bird") + " 42"))
        self.assertFalse(passphrase.said("blue bird 43"))
        self.assertFalse(passphrase.said("bird blue 42"))
        self.assertTrue(passphrase.alone("ok, blue bird four two"))                     # little more than the passphrase
        self.assertFalse(passphrase.alone("I told him the code was blue bird four two and he laughed at me"))
        self.assertFalse(passphrase.alone("nothing here"))
        stored = (Path(self.dir.name) / passphrase.FILE).read_text()
        self.assertNotIn("blue", stored)  # only a salted hash is kept
        passphrase.set_passphrase("")
        self.assertFalse(passphrase.configured())

    def test_what_opens_the_house(self):
        p = passphrase.protected
        self.assertTrue(p("intent__HassTurnOn", {"name": "Front Garage Door"}))
        self.assertTrue(p("intent__HassTurnOn", {"name": "x", "domain": ["cover"]}))
        self.assertTrue(p("intent__HassTurnOff", {"name": "Back", "device_class": "lock"}))
        self.assertTrue(p("intent__HassSetPosition", {"name": "Living room blinds", "position": 50}))
        self.assertTrue(p("homeassistant__call_action", {"action": "lock.unlock", "target": {"entity_id": "lock.front"}}))
        self.assertTrue(p("homeassistant__call_action", {"action": "homeassistant.turn_on", "target": {"entity_id": ["cover.garage_door"]}}))
        self.assertFalse(p("intent__HassTurnOn", {"name": "Front door light", "domain": "light"}))
        self.assertFalse(p("intent__HassTurnOn", {"name": "Office lights"}))
        self.assertFalse(p("light__HassLightSet", {"name": "Garage lights", "brightness": 100}))
        self.assertFalse(p("homeassistant__call_action", {"action": "light.turn_on", "target": {"entity_id": "light.garage"}}))
        # a generic action aimed at a whole place reaches its doors and locks too; aimed at a light it does not
        self.assertTrue(p("homeassistant__call_action", {"action": "homeassistant.turn_on", "target": {"area_id": "garage"}}))
        self.assertTrue(p("homeassistant__call_action", {"action": "homeassistant.toggle", "target": {"device_id": ["abc"]}}))
        self.assertTrue(p("homeassistant__call_action", {"action": "homeassistant.turn_off", "data": {"entity_id": "cover.garage_door_1"}}))
        self.assertTrue(p("homeassistant__call_action", {"action": "Cover.Open_Cover", "target": {"area_id": "garage"}}))
        self.assertFalse(p("homeassistant__call_action", {"action": "homeassistant.turn_on", "target": {"entity_id": "light.garage"}}))
        self.assertFalse(p("homeassistant__call_action", {"action": "light.turn_on", "target": {"area_id": "garage"}}))

    def test_hold_and_take(self):
        passphrase.hold("home", "hazel", {"name": "ha"}, "intent__HassTurnOn", {"name": "Garage Door"}, now=100)
        self.assertTrue(passphrase.waiting("home", now=120))
        self.assertIsNone(passphrase.take("home", now=200))  # too late
        passphrase.hold("home", "hazel", {"name": "ha"}, "intent__HassTurnOn", {"name": "Garage Door"}, now=100)
        h = passphrase.take("home", now=110)
        self.assertEqual(h["tool"], "intent__HassTurnOn")
        self.assertIsNone(passphrase.take("home", now=111))  # once


if __name__ == "__main__":
    unittest.main()
