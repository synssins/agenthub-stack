"""Emotion tags: guidance in the prompt, the rules that add a tag, and what goes to MQTT."""
import json
import tempfile
import time
import unittest
from pathlib import Path

import mqtt_out
import voice

P = [{"name": "T", "base_url": "http://127.0.0.1:9/v1", "tags": ["[laugh]", "[sigh]", "[whispering]", "[angry]"]},
     {"name": "Plain", "base_url": "http://127.0.0.1:9/v1", "tags": []}]


class Emotion(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        voice.data_dir = mqtt_out.data_dir = Path(self.tmp.name)
        voice.save({**voice.load(), "providers": P, "default_provider": "Plain"})
        self.sent = []
        self._send, mqtt_out._send = mqtt_out._send, lambda conf, msgs: self.sent.extend(msgs)
        mqtt_out._last.clear()

    def tearDown(self):
        mqtt_out._send = self._send
        self.tmp.cleanup()

    def wait(self, n):
        for _ in range(100):
            if len(self.sent) >= n:
                return
            time.sleep(0.02)

    def test_prompt_carries_guidance_for_the_tags_the_voice_has(self):
        p = voice.tags_prompt("T")
        self.assertIn("[sigh] for bad news or an action that failed", p)
        self.assertIn("the others only when", p)            # [angry] has no line of its own
        self.assertNotIn("[gasp]", p)                        # this voice does not perform it
        self.assertIn("When to use them: only [laugh].", voice.tags_prompt("T", "only [laugh]"))
        self.assertEqual(voice.tags_prompt("Plain"), "")

    def test_rules_add_a_tag_only_when_the_agent_chose_none(self):
        self.assertEqual(voice.rule_tag("That did not work.", "T", failed=True, hour=12), "[sigh] That did not work.")
        self.assertEqual(voice.rule_tag("Ha [laugh] oops.", "T", failed=True, hour=12), "Ha [laugh] oops.")
        self.assertEqual(voice.rule_tag("Done.", "T", failed=False, hour=12), "Done.")
        self.assertEqual(voice.rule_tag("That did not work.", "Plain", failed=True, hour=12), "That did not work.")
        voice.save({**voice.load(), "tag_rules": {"failed": "", "quiet": "[whispering]", "quiet_from": 22, "quiet_to": 7}})
        self.assertEqual(voice.rule_tag("Done.", "T", failed=False, hour=23), "[whispering] Done.")
        self.assertEqual(voice.rule_tag("Done.", "T", failed=False, hour=3), "[whispering] Done.")
        self.assertEqual(voice.rule_tag("Done.", "T", failed=True, hour=12), "Done.")
        self.assertEqual(voice.spoken_tags("a [Sigh] b `[laugh]` [nope] [angry]", "T"), ["[sigh]", "[angry]"])

    def test_mqtt_gets_state_changes_and_every_reply_emotion(self):
        mqtt_out.states({"ada": {"available": True, "busy": False}})
        self.assertEqual(self.sent, [])                       # off: nothing is sent
        mqtt_out.save({"enabled": True, "host": "broker", "prefix": "hub/x", "username": "u", "password": "secret"})
        self.assertEqual(mqtt_out.save({"enabled": True, "host": "broker", "prefix": "hub/x"})["password"], "secret")
        status = {"ada": {"available": True, "busy": True, "busy_channel": "lab"}, "paused": False}
        mqtt_out.states(status)
        mqtt_out.states(status)                               # unchanged: not repeated
        self.wait(4)
        self.assertEqual(self.sent[:3], [("hub/x/ada/speaking", "false"),   # a stale retained "true" is cleared first
                                         ("hub/x/ada/state", "busy"), ("hub/x/ada/emotion", "neutral")])
        self.assertEqual(len(self.sent), 4)
        mqtt_out.emotion("ada", ["[sigh]", "[laugh]"], "lab")
        mqtt_out.emotion("ada", [], "lab")
        self.wait(10)
        self.assertEqual([p for t, p in self.sent if t.endswith("/emotion")], ["neutral", "sigh", "neutral"])
        last = json.loads(self.sent[-1][1])
        self.assertEqual((last["state"], last["emotion"], last["channel"]), ("busy", "neutral", "lab"))
        self.assertEqual(oct((Path(self.tmp.name) / "mqtt.json").stat().st_mode & 0o777), "0o600")

    def test_speaking_ends_by_itself_when_nobody_reports_the_end(self):
        mqtt_out.save({"enabled": True, "host": "broker", "prefix": "hub"})
        mqtt_out.speaking("ada", True, seconds=0)       # floor: 2 s
        mqtt_out.speaking("ada", True, seconds=0)       # still speaking: not said twice
        self.wait(1)
        self.assertEqual(self.sent, [("hub/ada/speaking", "true")])
        for _ in range(150):
            if len(self.sent) > 1:
                break
            time.sleep(0.02)
        self.assertEqual(self.sent, [("hub/ada/speaking", "true"), ("hub/ada/speaking", "false")])
        mqtt_out.speaking(None, False)
        self.assertEqual(len(self.sent), 2)


if __name__ == "__main__":
    unittest.main()
