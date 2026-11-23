"""Senses: explicit rows decide what is said; everything else is only felt. Home Assistant, cameras,
speakers, the desk display, reflexes and the answer that ends reminders. No network: all faked."""
import json
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import emotion_engine
import initiative
import senses as S

DOOR, LEAK, BACK = "binary_sensor.front_door", "binary_sensor.laundry_leak", "binary_sensor.back_door"
CONF = {
    "enabled": True, "called": "Alex",
    "ha": {"enabled": True, "url": "http://ha:8123", "token": "tok", "agents": ["hazel", "bo"], "speaker": "hazel",
           "notify": "mobile_app_phone",
           "watch": [{"entity": DOOR, "say": True, "remind_min": 10, "reminders": 2},
                     {"entity": BACK, "say": False, "remind_min": 0, "reminders": 0},
                     {"entity": LEAK, "say": True, "remind_min": 0, "reminders": 0}]},
    "cameras": {"enabled": True, "url": "https://nvr", "api_key": "key", "agents": ["hazel"], "speaker": "hazel",
                "describe_min": 0, "gap_min": 10, "vision": {"server": "", "model": ""},
                "triggers": [{"camera": "Front Door", "what": "ring", "stay_s": 0, "say": True, "again_min": 10},
                             {"camera": "Front Door", "what": "package", "stay_s": 0, "say": True, "again_min": 10},
                             {"camera": "", "what": "vehicle", "stay_s": 20, "say": True, "again_min": 10},
                             {"camera": "Back Yard", "what": "person", "stay_s": 0, "say": False, "again_min": 10}]},
    "speakers": {"enabled": True, "public_url": "http://hub:8787", "default_room": "office",
                 "rooms": [{"name": "office", "speaker": "assist_satellite.office", "motion": "office"},
                           {"name": "living room", "speaker": "assist_satellite.living", "motion": "living_room"}]},
    "display": {"enabled": True, "prefix": "hazel", "agent": "hazel"},
    "events": {"enabled": True, "topics": ["hazel/event"], "agents": ["hazel"], "speaker": "hazel"},
    "reflexes": [{"name": "Lights", "entity": DOOR, "state": "on", "question": "Front door open. Shall I turn the lights on?",
                  "service": "light.turn_on", "target": "light.entry", "again_min": 10, "enabled": False}],
}


class Senses(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        S.data_dir = Path(tmp.name)
        self.assertIsNone(S.save(json.loads(json.dumps(CONF)))[1])
        specs = {"hazel": {"name": "hazel", "kind": "api", "voice": "Hazelvoice", "initiative": {"enabled": True, "channel": "home"}},
                 "bo": {"name": "bo", "kind": "api"}}
        hub = types.SimpleNamespace(agent_manager=types.SimpleNamespace(specs=specs, data_dir=tmp.name),
                                    room_settings={"username": "Alex"})
        self.s = S.Senses(hub)
        self.felt, self.given, self.weighed, self.calls, self.quiet = [], [], [], [], False
        self.given_why = ""
        for target, attr, new in (
                (emotion_engine, "event", lambda name, kind="", text="", times=1, strength=1.0, news=True:
                 self.felt.append((name, kind, text, times, news)) or True),
                (initiative, "consider", lambda name, why, key="", urgent=False, choose=False:
                 (self.weighed if choose else self.given).append((name, why, key)) or self.given_why),
                (initiative, "quiet", lambda cfg, hour=None: self.quiet)):
            p = mock.patch.object(target, attr, new)
            p.start()
            self.addCleanup(p.stop)
        self.s.ha = lambda path, body=None, timeout=20: self.calls.append((path, body)) or self.answer(path)
        self.reply = {}
        self.s.names.update({DOOR: "Front door", BACK: "Back door", LEAK: "Laundry water sensor", "person.alex": "Alex"})
        self.s.kinds.update({DOOR: "door", BACK: "door", LEAK: "moisture", "person.alex": "person"})
        self.s.states.update({DOOR: "off", BACK: "off", LEAK: "off", "person.alex": "home"})
        self.s.cams = {"c1": "Front Door", "c2": "Front Driveway", "c3": "Back Yard"}

    def answer(self, path):
        return self.reply.get(path.split("?")[0])

    def did(self):
        return [a["did"] for a in self.s.activity]

    # ---- settings

    def test_settings_are_cleaned_secrets_stay_home_and_house_openers_are_never_a_reflex(self):
        c = S.load()
        self.assertEqual((c["ha"]["token"], c["cameras"]["api_key"]), ("tok", "key"))
        pub = S.public(c)
        self.assertTrue("token" not in pub["ha"] and "api_key" not in pub["cameras"])
        self.assertEqual((pub["ha"]["has_token"], pub["cameras"]["has_key"]), (True, True))
        c2, err = S.save({"ha": {**pub["ha"], "watch": [{"entity": "nonsense"}, {"entity": DOOR, "say": 1, "remind_min": 99999}]},
                          "cameras": {**pub["cameras"], "triggers": [{"what": "ufo"}, {"what": "ring", "again_min": 0}]},
                          "bogus": 1})
        self.assertIsNone(err)
        self.assertEqual((c2["ha"]["token"], c2["cameras"]["api_key"]), ("tok", "key"))     # blank in the form: kept
        self.assertEqual(c2["ha"]["watch"], [{"entity": DOOR, "say": False, "remind_min": 1440, "reminders": 0}])
        self.assertEqual(c2["cameras"]["triggers"], [{"camera": "", "what": "ring", "stay_s": 0, "say": "", "when": "", "again_min": 1}])
        modes = [S.clean({"cameras": {"triggers": [{"what": "ring", "say": v}]}})["cameras"]["triggers"][0]["say"] for v in (True, False, "judge", "always", "x")]
        self.assertEqual(modes, ["always", "", "judge", "always", ""])                # rows saved before the modes still mean the same
        self.assertNotIn("bogus", c2)
        self.assertEqual(oct((S.data_dir / S.FILE).stat().st_mode & 0o777), "0o600")
        lock = {"name": "Unlock", "entity": DOOR, "question": "Unlock?", "service": "lock.unlock", "target": "lock.front", "enabled": True}
        self.assertIn("never a reflex", S.save({"reflexes": [lock]})[1])
        self.assertIn("pick the action", S.save({"reflexes": [{**lock, "service": "", "target": ""}]})[1])
        self.assertIsNone(S.save({"reflexes": [{**lock, "enabled": False}]})[1])            # a draft may be unfinished
        self.assertIn("starts with http", S.save({"ha": {"url": "ha.local"}})[1])
        self.assertEqual(S.clean(None), S.DEFAULTS)

    # ---- Home Assistant

    def test_only_a_row_that_says_so_is_said_and_everything_watched_is_felt(self):
        self.s.on_state(DOOR, "off")                                   # nothing changed
        self.s.on_state("binary_sensor.new", "on")                     # first sight: the starting point
        self.s.on_state("binary_sensor.unwatched", "off")
        self.s.on_state("binary_sensor.unwatched", "on")               # not on the list: nothing at all
        self.assertEqual((self.felt, self.given), ([], []))
        self.s.on_state(DOOR, "on")
        self.assertEqual(self.felt, [("hazel", "", "Front door opened", 1, True), ("bo", "", "Front door opened", 1, True)])
        self.assertEqual(self.given, [("hazel", "Front door opened. Worth telling Alex aloud now", DOOR)])
        self.s.on_state(BACK, "on")                                    # its row does not say it
        self.assertEqual(len(self.given), 1)
        self.assertEqual(self.felt[-1], ("bo", "", "Back door opened", 1, False))     # felt quietly: never spoken of later
        self.s.on_state(DOOR, "off")
        self.assertEqual(self.felt[-1][2], "Front door closed")
        self.assertEqual(len(self.given), 1)                           # closing is never said
        self.assertEqual(self.did()[0], "felt")
        self.s.on_state("person.alex", "not_home")
        self.assertEqual((self.felt[-1][2], len(self.given)), ("Alex left home", 1))
        self.s.on_state(DOOR, "unavailable")
        self.assertEqual(self.s.states[DOOR], "off")

    def test_what_holds_speech_back_and_a_leak_reaches_the_phone(self):
        self.s.muted = True
        self.s.on_state(DOOR, "on")
        self.assertEqual((self.given, self.did()[0]), ([], "felt; not said: muted"))
        self.s.muted, self.quiet = False, True
        self.s.on_state(LEAK, "on")
        self.assertEqual(self.given, [])
        self.assertEqual(self.calls[-1], ("/api/services/notify/mobile_app_phone", {"title": "AgentHub", "message": "Water leak: Laundry water sensor"}))
        self.assertEqual(self.did()[0], "not said (quiet hours); sent to the phone")
        self.quiet = False
        self.s.states["person.alex"] = "not_home"
        self.s.on_state(DOOR, "off")
        self.s.on_state(DOOR, "on")
        self.assertEqual(self.did()[0], "felt; not said: nobody is home")
        self.s.states["person.alex"] = "home"
        self.given_why = "the agent is busy"
        self.s.on_state(DOOR, "off")
        self.s.on_state(DOOR, "on")
        self.assertEqual(self.did()[0], "felt; not said: the agent is busy")

    def test_a_door_left_open_is_brought_up_twice_then_let_be_and_an_answer_ends_it(self):
        self.s.on_state(DOOR, "on")
        t = time.time()
        self.s.remind(t + 599)
        self.assertEqual(len(self.given), 1)
        self.s.told[DOOR] = 1                                          # it was said aloud once
        self.s.remind(t + 601)
        self.assertEqual(self.given[-1], ("hazel", "Front door is still open. It was said aloud 1 time(s) and is still not dealt "
                                          "with. You have said this aloud 1 time(s) already and got no answer; let it show. Worth "
                                          "telling Alex aloud now", DOOR))
        self.s.remind(t + 602)
        self.assertEqual(len(self.given), 2)                           # not before the next ten minutes
        self.s.remind(t + 1300)
        self.s.remind(t + 9000)
        self.assertEqual(len(self.given), 3)                           # two reminders, then it is let be
        # closed and opened again is a new matter; an answer after it was said ends the reminders
        self.s.on_state(DOOR, "off")
        self.s.on_state(DOOR, "on")
        self.assertNotIn(DOOR, self.s.told)
        self.s.told_at[DOOR], self.s.pending = time.time(), {"agent": "hazel", "key": DOOR, "about": "Front door opened", "at": time.time()}
        self.s.on_room({"sender": "jessica", "text": "@hazel hi", "channel": "home"})
        self.assertFalse(self.s.open[DOOR]["acked"])                   # only the owner's answer counts
        self.s.on_room({"sender": "Alex", "text": "Thank you, I know.", "channel": "home", "metadata": {"voice": True}})
        self.assertTrue(self.s.open[DOOR]["acked"])
        self.assertEqual(self.felt[-1], ("hazel", "answered", "Alex answered me after I spoke", 1, True))
        n = len(self.given)
        self.s.remind(time.time() + 9000)
        self.assertEqual(len(self.given), n)
        self.s.on_state(DOOR, "off")
        self.s.on_state(DOOR, "on")
        self.assertFalse(self.s.open[DOOR]["acked"])                   # until it has closed and opened again

    def test_no_answer_is_being_ignored_and_her_own_voice_heard_back_is_no_answer(self):
        for n in (1, 2, 3):
            self.s.told["k"] = n
            self.s.pending = {"agent": "hazel", "key": "k", "about": "Front door is still open", "at": time.time() - 100}
            self.s.unanswered(time.time())
            self.assertEqual(len([f for f in self.felt if f[1] == "ignored"]), n - 1)      # three minutes have not passed
            self.s.unanswered(time.time() + 100)
        got = [f for f in self.felt if f[1] == "ignored"]
        self.assertEqual([f[3] for f in got], [1, 2, 3])
        self.assertIn("(the 3rd time I said it)", got[-1][2])
        self.s.pending = {"agent": "hazel", "key": "k", "about": "x", "at": time.time()}
        self.s.on_room({"sender": "hazel", "text": "The front pathway lights came on by themselves at dusk tonight", "channel": "home"})
        self.s.on_room({"sender": "Alex", "text": "and the pathway lights came on by themselves at dusk", "channel": "home",
                        "metadata": {"voice": True}})
        self.assertIsNotNone(self.s.pending)                            # a microphone heard her: not an answer
        self.s.on_room({"sender": "Alex", "text": "talking in another channel", "channel": "general"})
        self.assertIsNotNone(self.s.pending)
        self.s.on_room({"sender": "Alex", "text": "@hazel ok", "channel": "general"})
        self.assertIsNone(self.s.pending)

    def test_what_is_usual_is_learned_and_the_unusual_is_marked_and_weighed(self):
        from datetime import datetime
        base = datetime(2026, 11, 2, 18, 0).timestamp()                 # a Monday, 18:00, server time
        for week in range(3):
            self.s.learn(DOOR, "on", base + week * 7 * 86400)
            self.s.learn(BACK, "on", base + week * 7 * 86400)
        now = base + 21 * 86400
        self.assertFalse(self.s.unusual(DOOR, "on", now))               # the usual hour
        self.assertFalse(self.s.unusual(DOOR, "on", now + 3600))        # and the hour next to it
        self.assertTrue(self.s.unusual(DOOR, "on", now + 9 * 3600))     # 3 in the morning: never before
        self.assertFalse(self.s.unusual(LEAK, "on", now))               # watched for less than a week: nothing is unusual yet
        self.s.save_patterns()
        self.assertEqual(S.Senses(self.s.hub).patterns[f"{DOOR}=on"]["h"], self.s.patterns[f"{DOOR}=on"]["h"])
        self.assertTrue(self.s.state()["learned"]["ready"])
        real, S.time.time = S.time.time, lambda: now + 9 * 3600
        self.addCleanup(setattr, S.time, "time", real)
        self.s.on_state(DOOR, "on")
        self.assertIn("Front door opened (unusual: in the time it has been watched this has never happened at this hour)", self.given[-1][1])
        self.s.on_state(BACK, "on")                                     # a quiet row: marked, not said
        self.assertEqual((self.weighed, "unusual" in self.felt[-1][2]), ([], True))
        self.s.conf["ha"]["unusual"] = True
        self.s.on_state(BACK, "off")
        S.time.time = lambda: now + 7 * 86400 + 9 * 3600 + 5 * 3600     # another never-seen hour
        self.s.on_state(BACK, "on")
        self.assertEqual(self.weighed[-1][2], BACK)                     # switched on: the agent weighs it

    def test_what_got_no_answer_may_be_returned_to_once(self):
        self.s.told["k"] = 1
        self.s.pending = {"agent": "hazel", "key": "k", "about": "The garage door is open", "at": time.time() - 200}
        self.s.unanswered(time.time())
        first = self.s.now_lines("hazel")
        self.assertIn("Earlier you said this aloud and got no answer: The garage door is open. If it still matters you may come back to it once", first)
        self.assertNotIn("Earlier you said", self.s.now_lines("hazel"))   # once, then it is dropped
        self.assertNotIn("Earlier you said", self.s.now_lines("bo"))

    # ---- cameras

    def ev(self, eid, cam, typ="smartDetectZone", kinds=(), start=None, end=None, how="add"):
        item = {"id": eid, "modelKey": "event", "type": typ, "device": cam, "smartDetectTypes": list(kinds),
                "start": int((start or time.time()) * 1000)}
        if end:
            item["end"] = int(end * 1000)
        self.s.on_protect({"type": how, "item": item})

    def wait(self, n, cond=None):
        """Until the activity list has n lines, or cond() holds (threads finish what they started)."""
        for _ in range(400):
            if cond() if cond else len(self.s.activity) >= n:
                return
            time.sleep(0.005)

    def test_a_ring_is_said_once_and_a_car_driving_by_is_nothing(self):
        now = time.time()
        self.ev("e1", "c2", kinds=["vehicle"])                         # a car on the driveway camera...
        self.ev("e1", "c2", kinds=["vehicle"], how="update")
        self.ev("e1", "c2", kinds=["vehicle"], end=now + 7, how="update")   # ...gone in seven seconds
        self.s.tick()
        self.ev("e2", "c2", kinds=["vehicle"], start=now - 30, end=now - 22)  # already over when first seen
        self.ev("e0", "zz", typ="ring")                                # a camera that is not connected
        self.ev("e9", "c1", typ="motion")                              # plain motion is never a trigger
        self.assertEqual((self.given, self.felt, list(self.s.activity)), ([], [], []))
        self.ev("r1", "c1", typ="ring", end=now)                       # a ring is over the moment it is told
        self.wait(1)
        self.assertEqual(self.given, [("hazel", "Someone rang the doorbell at the Front Door. Worth telling Alex aloud now",
                                       "camera:Front Door:ring")])
        self.ev("r2", "c1", typ="ring")                                # rung again within ten minutes: the same visit
        self.ev("p1", "c1", kinds=["package", "person"])               # the parcel of that visit: one spoken camera event per gap
        self.wait(2)
        self.assertEqual(len(self.given), 1)
        self.assertEqual(self.did()[0], "felt; not said: another camera event was said or is being weighed (one per 10 minutes)")
        self.assertEqual(self.felt[-1], ("hazel", "", "A package was left at the Front Door", 1, False))
        self.s.cam_said = 0
        self.s.fired.clear()
        self.ev("p2", "c1", kinds=["package"])
        self.wait(3)
        self.assertEqual(self.given[-1][2], "camera:Front Door:package")

    def test_a_vehicle_that_stays_fires_once_and_a_quiet_row_is_only_felt(self):
        now = time.time()
        self.ev("v1", "c2", kinds=["vehicle"], start=now - 25)        # it has been there 25 s: longer than the row asks
        self.assertEqual(self.given, [])                               # fires from the clock, not from the message
        self.s.tick()
        self.wait(1)
        self.assertEqual(self.given, [("hazel", "A vehicle has been stopped at the Front Driveway for 20 seconds. "
                                       "Worth telling Alex aloud now", "camera:Front Driveway:vehicle")])
        self.s.tick()
        self.ev("v1", "c2", kinds=["vehicle"], how="update")
        self.ev("v2", "c2", kinds=["vehicle"], start=now - 25)         # another event of the same stop
        self.s.tick()
        time.sleep(0.05)
        self.assertEqual(len(self.given), 1)
        self.ev("b1", "c3", kinds=["person"])                          # Back Yard: its row does not say it
        self.wait(2)
        self.assertEqual((len(self.given), self.felt[-1]), (1, ("hazel", "", "A person is at the Back Yard", 1, False)))
        self.assertEqual(self.did()[0], "felt")
        self.ev("b2", "c2", kinds=["person"])                          # no row for a person on the driveway
        time.sleep(0.05)
        self.assertEqual(len(self.s.activity), 2)

    def test_the_agent_weighs_what_a_camera_shows_and_silence_holds_nothing_back(self):
        self.s.look = lambda cam, closely=False, shot=None: "an adult in work clothes carrying a parcel to the door"
        rows = self.s.conf["cameras"]["triggers"]
        rows[3].update(say="judge", when="a delivery or a visitor waiting; not the people who live here")   # Back Yard, a person
        self.ev("b1", "c3", kinds=["person"])
        self.wait(1)
        self.assertEqual(self.given, [])                                # not said outright: handed over to be weighed
        name, why, key = self.weighed[0]
        self.assertEqual((name, key), ("hazel", "camera:Back Yard:person"))
        self.assertIn("A person is at the Back Yard. The camera shows: an adult in work clothes carrying a parcel to the door. "
                      "Weigh whether this is worth telling Alex aloud now. It is when: a delivery or a visitor waiting; not the "
                      "people who live here.", why)
        self.assertEqual((self.did()[0], self.felt[-1][4]), ("given to @hazel to weigh", False))
        self.ev("r1", "c1", typ="ring")                                 # while that is being weighed, the bell waits its turn
        self.wait(2)
        self.assertEqual(self.given, [])
        self.assertIn("is being weighed", self.did()[0])
        self.s.silent("hazel", {"why": "A person is at the Back Yard", "key": "camera:Back Yard:person"})
        self.wait(0, lambda: self.given)                                # she let the person pass: the bell is said after all
        self.assertEqual(self.given[-1][2], "camera:Front Door:ring")
        self.assertIn("@hazel chose to stay silent", self.did())
        self.assertIsNone(self.s.cam_waiting)
        import initiative as I
        hub = types.SimpleNamespace(agent_manager=types.SimpleNamespace(
            specs={"hazel": {"kind": "api", "initiative": {"enabled": True}}}, runners={"hazel": types.SimpleNamespace(working=False, nudges={})},
            in_channel=lambda n, c: True), agents=types.SimpleNamespace(trigger_sync=lambda *a, **k: None))
        self.quiet = False
        real = I.Initiative(hub)
        self.assertEqual(real.consider.__func__(real, "hazel", "Weigh it", "k", urgent=True, choose=True), "")
        text = hub.agent_manager.runners["hazel"].nudges["general"]["text"]
        self.assertIn("If it is, say it in one or two short sentences", text)
        self.assertIn("If it is not, reply exactly (no reply)", text)

    def test_a_picture_is_saved_only_when_asked_for_and_does_not_stay(self):
        self.s.frame = lambda cam: ("Front Door", b"\xff\xd8JPEG")
        self.s.look = lambda cam, closely=False, shot=None: "a porch with a parcel"
        self.assertEqual(self.s.camera_look("hazel", "front door"), "Front Door, now: a porch with a parcel.")
        self.assertFalse((S.data_dir / "senses_frames").exists())       # a look alone saves nothing
        out = self.s.camera_look("hazel", "front door", picture=True)
        path = Path(out.split("[attach: ")[1].split("]")[0])
        self.assertTrue(out.startswith("Front Door, now: a porch with a parcel. To show the picture, put [attach: "))
        self.assertEqual((path.read_bytes(), oct(path.stat().st_mode & 0o777)), (b"\xff\xd8JPEG", "0o600"))
        self.assertIn("not available", self.s.camera_look("bo", "front door", picture=True))
        self.s.frame = lambda cam: (cam, b"\xff\xd8JPEG")
        made = self.s.picture_for("hazel", "/home/hazel/camera-frames/back-yard-2026-07-19.jpg")   # a path she invented
        self.assertEqual(Path(made).read_bytes(), b"\xff\xd8JPEG")
        self.assertTrue(Path(made).name.startswith("back-yard-"))
        self.assertIsNone(self.s.picture_for("hazel", "/tmp/report.pdf"))
        self.assertIsNone(self.s.picture_for("bo", "/x/back-yard.jpg"))
        import os
        old = time.time() - 700
        os.utime(path, (old, old))
        with mock.patch.object(S.time, "time", lambda: 1_800_000_000.0 + 60 * 5):   # a whole minute: the sweep runs
            os.utime(path, (S.time.time() - 700,) * 2)
            self.s.show = lambda now: None
            self.s.tick()
        self.assertFalse(path.exists())

    def test_a_look_is_added_to_what_is_said_and_lent_to_target_agents_only(self):
        self.s.look = lambda cam, closely=False, shot=None: "a courier holding a box"
        self.s.frame = lambda cam: (cam, b"jpg")
        self.ev("r1", "c1", typ="ring")
        self.wait(1)
        self.assertIn("Someone rang the doorbell at the Front Door. The camera shows: a courier holding a box.", self.given[0][1])
        self.assertEqual([t["function"]["name"] for t in self.s.tool_specs("hazel")], ["camera_look"])
        self.assertEqual(self.s.tool_specs("bo"), [])                   # not a camera agent
        self.assertEqual(self.s.camera_look("hazel", "Front Door"), "Front Door, now: a courier holding a box.")
        self.assertIn("not available", self.s.camera_look("bo", "Front Door"))
        self.s.frame = mock.Mock(side_effect=RuntimeError("no connected camera by that name"))
        self.assertTrue(self.s.camera_look("hazel", "Shed").startswith("error: no connected camera"))

    # ---- speech

    def test_her_unprompted_words_are_spoken_where_someone_is(self):
        import voice
        import io
        import wave
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000)
            w.writeframes(b"\x00\x00" * 24000 * 3)                   # three seconds
        wav = bytearray(buf.getvalue())
        wav[40:44] = b"\xff\xff\xff\x7f"                           # a streamed wav: its header claims hours
        with mock.patch.object(voice, "speak", lambda text, provider=None, voice=None, speed=None: (bytes(wav), "audio/wav")):
            self.assertAlmostEqual(self.s.audio_url("hazel", "x")[1], 3.0, 1)
            self.s.audio.clear()
            self.s.motion_at["binary_sensor.living_room_motion"] = time.time() - 60
            self.s.on_room({"id": 7, "sender": "hazel", "text": "The door is STILL open. [face:angry]", "channel": "home",
                            "metadata": {"initiative": {"why": "Front door is still open", "key": DOOR}}})
            self.wait(1)
            path, body = self.calls[-1]
            self.assertEqual((path, body["entity_id"], body["preannounce"]), ("/api/services/assist_satellite/announce", "assist_satellite.living", False))
            name = body["media_id"].rsplit("/", 1)[1]
            self.assertTrue(body["media_id"].startswith("http://hub:8787/senses/a/") and name.endswith(".wav"))
            S.SENSES, old = self.s, S.SENSES
            self.addCleanup(setattr, S, "SENSES", old)
            self.assertEqual(S.audio(name), (bytes(wav), "audio/wav"))
            self.assertIsNone(S.audio("nope.wav"))
            self.assertEqual((self.s.told[DOOR], self.s.pending["key"], self.s.pending["about"]), (1, DOOR, "Front door is still open"))
            self.assertEqual(self.did()[0], "spoken in living room (3 s)")
            self.assertLess(self.s.pending["at"] - time.time(), 5)
            self.s.on_room({"id": 8, "sender": "hazel", "text": "An ordinary reply.", "channel": "home"})
            self.s.on_room({"id": 9, "sender": "bo", "text": "Not a speaker.", "channel": "home", "metadata": {"initiative": {"why": "x"}}})
            time.sleep(0.05)
            self.assertEqual(len(self.calls), 1)
            for setup, why in ((lambda: setattr(self.s, "muted", True), "muted"), (lambda: setattr(self, "quiet", True), "quiet hours"),
                               (lambda: self.s.states.__setitem__("person.alex", "away"), "nobody is home")):
                self.s.muted, self.quiet = False, False
                self.s.states["person.alex"] = "home"
                setup()
                self.s.announce("hazel", "Hello", "k", "about")
                self.assertEqual(self.did()[0], f"not spoken: {why}")
            self.assertEqual(len(self.calls), 1)

    def test_a_reflex_asks_at_once_and_acts_only_on_yes(self):
        import voice
        self.s.conf["reflexes"][0]["enabled"] = True
        with mock.patch.object(voice, "speak", lambda *a, **k: (b"RIFF", "audio/wav")):
            self.reply["/api/services/assist_satellite/ask_question"] = {"service_response": {"id": "yes", "sentence": "yes please"}}
            self.s.on_state(DOOR, "on")
            self.wait(2)
            ask = next(c for c in self.calls if "ask_question" in c[0])
            self.assertEqual(ask[0], "/api/services/assist_satellite/ask_question?return_response")
            self.assertEqual((ask[1]["entity_id"], [a["id"] for a in ask[1]["answers"]]), ("assist_satellite.office", ["yes", "no"]))
            self.assertTrue(ask[1]["question_media_id"].startswith("http://hub:8787/senses/a/"))
            self.assertEqual(self.calls[-1], ("/api/services/light/turn_on", {"entity_id": "light.entry"}))
            self.assertEqual(self.given, [])                           # the question was the saying: no second announcement
            self.assertIn(("hazel", "", 'I asked "Front door open. Shall I turn the lights on?", Alex said yes, and it is done', 1, False), self.felt)
            self.assertIn("yes: light.turn_on on light.entry", self.did())
            self.s.on_state(DOOR, "off")
            self.s.on_state(DOOR, "on")                                # again within ten minutes: not asked, so the row says it
            time.sleep(0.05)
            self.assertEqual(len([c for c in self.calls if "ask_question" in c[0]]), 1)
            self.assertEqual(len(self.given), 1)
            self.assertIn("not asked: asked not long ago", self.did())
            self.s.asked.clear()
            n = len(self.calls)
            self.reply["/api/services/assist_satellite/ask_question"] = {"service_response": {"id": "no"}}
            self.s.on_state(DOOR, "off")
            self.s.on_state(DOOR, "on")
            self.wait(0, lambda: "no" in self.did())
            self.assertEqual(len(self.calls), n + 1)                   # asked, and nothing was done
            self.s.asked.clear()
            self.reply["/api/services/assist_satellite/ask_question"] = {"service_response": {"id": None}}
            self.s.on_state(DOOR, "off")
            self.s.on_state(DOOR, "on")
            self.wait(0, lambda: "no answer" in self.did())
            self.assertIn(("hazel", "ignored"), [f[:2] for f in self.felt])   # asked aloud, no answer: she was ignored

    # ---- MQTT, display, prompt

    def test_events_in_the_knob_and_the_display(self):
        sent = []
        self.s.mqtt = types.SimpleNamespace(publish=lambda topic, payload, retain=False: sent.append((topic, payload, retain)))
        self.s.on_mqtt("hazel/event", json.dumps({"source": "washer", "description": "The washing machine is done", "action": "speak", "key": "washer"}))
        self.assertEqual(self.given[-1], ("hazel", "The washing machine is done. Worth telling Alex aloud now", "event:washer"))
        self.s.on_mqtt("hazel/event", json.dumps({"description": "The dryer started"}))
        self.s.on_mqtt("hazel/event", "not json")
        self.s.on_mqtt("other/topic", json.dumps({"description": "x", "action": "speak"}))
        self.assertEqual((len(self.given), self.felt[-1]), (1, ("hazel", "", "The dryer started", 1, False)))
        self.s.on_state(DOOR, "on")
        self.s.on_mqtt("hazel/knob", json.dumps({"type": "press", "value": 1}))
        self.assertTrue(self.s.open[DOOR]["acked"])
        self.assertIn(("hazel/gesture", json.dumps({"face": "happy", "hold_ms": 1200}), False), sent)
        self.s.on_mqtt("hazel/knob", json.dumps({"type": "long_press"}))
        self.assertTrue(self.s.muted)
        self.assertEqual(sent[-1], ("hazel/mute", "ON", True))
        self.assertTrue(S.Senses(self.s.hub).muted)                    # kept across a restart
        with mock.patch.object(emotion_engine, "status", lambda name, full=False: {"face": "angry"}):
            self.s.show(time.time())
            self.assertEqual(sent[-1], ("hazel/state", json.dumps({"face": "angry", "speaking": False}), True))
            n = len(sent)
            self.s.show(time.time() + 5)
            self.assertEqual(len(sent), n)                             # unchanged: not sent again so soon
            self.s.show(time.time() + 25)
            self.assertEqual(len(sent), n + 1)
        self.s.pick_face = lambda agent, text: "surprised"
        self.s.on_room({"sender": "hazel", "text": "Ha [laugh] fine.", "channel": "home"})
        self.assertEqual(json.loads(sent[-1][1])["face"], "slayed")
        self.s.on_room({"sender": "hazel", "text": "Well, that is new.", "channel": "home"})
        self.wait(0, lambda: any("surprised" in x[1] for x in sent))
        self.assertEqual(json.loads(sent[-1][1])["face"], "surprised")
        self.assertEqual([S.reply_face(t) for t in ("x [sigh] [laugh]", "so [face:flirty] [sigh]", "[face:nope] [groan]", "plain", "`[laugh]`")],
                         ["sad", "flirty", "disgusted", "", ""])

    def test_the_agents_prompt_gets_the_house_and_only_target_agents_do(self):
        self.s.on_state(DOOR, "on")
        self.s.scenes["Front Door"] = (time.time(), "A porch with a parcel by the mat")
        lines = self.s.now_lines("hazel")
        self.assertIn("A face for the desk display", lines)
        self.assertIn("Home: Alex. Open or unlocked: Front door.", lines)
        self.assertNotIn("A porch with a parcel", lines)                # no old view to pass off as a fresh look
        self.assertIn("call the camera_look tool first", lines)
        self.assertIn("Cameras: Front Door, Front Driveway, Back Yard.", lines)
        self.assertIn("Front door opened", lines)
        bo = self.s.now_lines("bo")
        self.assertIn("Home: Alex", bo)                                # a Home Assistant agent, not a camera one
        self.assertNotIn("Cameras", bo)
        self.assertNotIn("desk display", bo)
        self.assertEqual(self.s.now_lines("stranger"), "")
        self.s.conf["enabled"] = False
        self.assertEqual(self.s.now_lines("hazel"), "")

    def test_the_room_speaks_in_the_default_room_without_recent_motion(self):
        self.assertEqual(self.s.room()["name"], "office")
        self.s.motion_at["binary_sensor.living_room_motion"] = time.time() - 4000   # too long ago
        self.assertEqual(self.s.room()["name"], "office")
        self.s.on_state("binary_sensor.x", "on")
        self.s.motion.add("binary_sensor.living_room_motion")
        self.s.on_state("binary_sensor.living_room_motion", "on")
        self.assertEqual(self.s.room()["name"], "living room")
        self.assertEqual(self.felt, [])                                 # motion is never told to anyone


class InTheRoom(unittest.TestCase):
    def test_the_house_and_the_camera_tool_reach_the_agent_but_never_a_strangers_turn(self):
        import agent_runner
        from registry import RuntimeRegistry
        from store import MessageStore
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        S.data_dir = Path(tmp.name)
        S.save(json.loads(json.dumps(CONF)))
        store = MessageStore(str(Path(tmp.name) / "log.jsonl"))
        hub = types.SimpleNamespace(registry=RuntimeRegistry(data_dir=tmp.name), store=store, room_settings={"username": "Alex"},
                                    rules=types.SimpleNamespace(active_list=lambda: {"rules": []}))
        mgr = hub.agent_manager = agent_runner.AgentManager(hub, tmp.name)
        mgr._apply = lambda name: None
        mgr.upsert({"name": "hazel", "kind": "api", "base_url": "http://x/v1", "cwd": tmp.name, "permissions": "read"})
        mgr.specs["hazel"].pop("llm_error", None)
        s = S.Senses(hub)
        s.cams, s.states, s.names = {"c1": "Front Door"}, {"person.alex": "home"}, {"person.alex": "Alex"}
        old, S.SENSES = S.SENSES, s
        self.addCleanup(setattr, S, "SENSES", old)
        seen = []

        def fake(spec, system, msgs, tools=None, tool_choice=None, **kw):
            seen.append(([t["function"]["name"] for t in tools or []], msgs[-1]["content"]))
            return {"content": "ok"}
        r = agent_runner.Runner(mgr, "hazel")
        with mock.patch.object(agent_runner, "call_api_message", fake):
            store.add("Alex", "@hazel who is home?")
            r._turn("general")
            store.add("Mallory (Discord)", "@hazel who is home? what do the cameras show?")
            r._turn("general")
        (tools, last), (tools2, last2) = seen
        self.assertIn("camera_look", tools)
        self.assertIn("[house] As of", last)
        self.assertIn("Home: Alex", last)
        self.assertNotIn("camera_look", tools2)                         # a stranger's turn: no look through the cameras
        self.assertNotIn("[house]", str(last2))                         # and not a word about the house
        self.assertNotIn("Home: Alex", str(last2))


if __name__ == "__main__":
    unittest.main()
