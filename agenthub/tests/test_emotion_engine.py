"""Emotion engine: per-agent settings, how feelings move and fade, the model's answer, the prompt line."""
import json
import random
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent_runner
import emotion_engine as E
import mqtt_out
from registry import RuntimeRegistry
from store import MessageStore

ON = {"enabled": True, "baseline": {"pleasure": 0.3, "arousal": 0.1, "dominance": 0.3}}


def cfg(**over):
    return E.clean({**ON, **over})


def traits(**over):
    return {**{t: 0.5 for t in E.TRAITS}, **over}


class Settings(unittest.TestCase):
    def test_stored_settings_are_complete_and_in_range(self):
        self.assertEqual(E.clean(None), E.DEFAULTS)
        self.assertEqual(E.clean("junk")["enabled"], False)
        c = E.clean({"enabled": "yes", "hexaco": {"openness": 7, "agreeableness": "x", "extra": 1},
                     "baseline": {"pleasure": -9}, "settle_min": 0, "floor": 0.4, "variation_size": 2})
        self.assertFalse(c["enabled"])                      # only a real true switches it on
        self.assertEqual((c["hexaco"]["openness"], c["hexaco"]["agreeableness"]), (1.0, 0.5))
        self.assertNotIn("extra", c["hexaco"])
        self.assertEqual((c["baseline"]["pleasure"], c["settle_min"], c["floor"], c["variation_size"]), (-1.0, 1, 0.0, 1.0))


class Feelings(unittest.TestCase):
    def test_words_and_face_from_the_numbers(self):
        r = lambda *v: E.read(*v)[0]
        self.assertEqual([r(.5, .5, 0), r(.5, -.5, 0), r(-.5, .5, 0), r(-.5, .5, -.5), r(-.5, -.5, 0), r(.3, 0, 0),
                          r(-.3, 0, 0), r(-.3, 0, -.3), r(0, .5, 0), r(0, .5, -.5), r(0, -.5, 0), r(.2, -.5, 0), r(0, 0, 0)],
                         ["cheerful", "content", "annoyed", "hurt", "down", "pleased", "displeased", "hurt", "alert",
                          "uneasy", "bored", "relaxed", "calm"])
        self.assertEqual(E.read(.7, .1, .5)[1], "very pleased, sure of itself")
        self.assertEqual(E.read(0, 0, -.5)[1], "even-tempered, unsure of itself")
        f = E.face
        self.assertEqual([f(.5, .5, 0), f(.5, 0, 0), f(-.5, .5, 0), f(-.5, .5, -.5), f(-.5, -.5, 0), f(-.5, 0, 0),
                          f(-.5, 0, -.5), f(0, .5, 0), f(0, 0, 0)],
                         ["happy", "neutral", "angry", "fearful", "sad", "disgusted", "sad", "surprised", "neutral"])

    def test_a_blow_reads_as_hurt_and_fades_back_to_the_resting_state(self):
        c = cfg()
        s = E.State.load({}, c)
        self.assertEqual(E.read(*s.felt(c))[1], "pleased, sure of itself")     # the resting state reads as itself
        s.judged((-0.6, 0.4, -0.5), c, "told to shut up")
        self.assertEqual(E.read(*s.felt(c))[0], "hurt")
        self.assertEqual(s.why(c), ["told to shut up"])
        t0 = s.last_update
        s.drift(c, t0 + 10 * 60)
        self.assertLess(s.pleasure, 0.2)                    # ten minutes on: still off its usual
        s.drift(c, t0 + 3 * 3600)
        for a in E.AXES:
            self.assertAlmostEqual(getattr(s, a), c["baseline"][a], delta=0.03)
        self.assertEqual(s.why(c), [] if time.time() - t0 > 3600 else ["told to shut up"])

    def test_personality_scales_what_an_event_does(self):
        def after(change, **hx):
            c = cfg(hexaco=traits(**hx))
            s = E.State.load({}, c)
            s.judged(change, c)
            return s.pleasure
        blow, lift = (-0.4, 0, 0), (0.3, 0, 0)
        self.assertLess(after(blow, emotionality=0.9), after(blow, emotionality=0.1))       # sensitive: hit harder
        self.assertGreater(after(lift, emotionality=0.9), after(lift, emotionality=0.1))
        self.assertLess(after(blow, agreeableness=0.1), after(blow, agreeableness=0.9))     # easily annoyed
        self.assertEqual(after(lift, agreeableness=0.1), after(lift, agreeableness=0.9))    # only the unpleasant part

        def grudge(a):
            c = cfg(hexaco=traits(agreeableness=a))
            s = E.State.load({}, c)
            s.push((-0.6, 0, 0), c)
            s.drift(c, s.last_update + 900)
            return s.pleasure
        self.assertLess(grudge(0.1), grudge(0.9))                                           # slow to forgive

    def test_a_feeling_can_last_a_random_time(self):
        fixed = cfg()
        self.assertEqual(E.State.load({}, fixed).minutes(fixed), 30)
        c = cfg(settle_random=True, settle_from=10, settle_to=60)
        s = E.State.load({}, c)
        seen = set()
        for _ in range(30):
            s.push((-0.3, 0, 0), c)
            self.assertTrue(10 <= s.minutes(c) <= 60)
            seen.add(round(s.minutes(c)))
        self.assertGreater(len(seen), 5)                                # drawn afresh for each feeling
        before = s.lasts
        s.push((0.01, 0, 0), c)                                         # nothing worth calling a feeling: the time stays
        self.assertEqual(s.lasts, before)
        self.assertEqual(E.State.load(E.asdict(s), c).lasts, before)   # kept across a restart
        self.assertEqual(E.clean({"settle_from": 50, "settle_to": 20})["settle_to"], 50)

    def test_kinds_have_fixed_effects_scaled_by_their_trait_and_count(self):
        def after(kind, times=1, **hx):
            c = cfg(hexaco=traits(**hx))
            s = E.State.load({}, c)
            s.kind(kind, c, times)
            return s
        self.assertLess(after("ignored", 3).pleasure, after("ignored", 1).pleasure)         # the third time stings most
        self.assertEqual(E.read(*after("ignored", 3).felt(cfg()))[0], "annoyed")
        self.assertLess(after("failure", conscientiousness=0.9).dominance, after("failure", conscientiousness=0.1).dominance)
        self.assertLess(after("lonely", extraversion=0.9).pleasure, after("lonely", extraversion=0.1).pleasure)
        self.assertLess(after("bored", openness=0.9).arousal, after("bored", openness=0.1).arousal)
        self.assertGreater(after("praise").pleasure, 0.3)

    def test_silence_becomes_loneliness_and_boredom_by_the_clock(self):
        c = cfg()                                    # lonely after 4 h, bored after 3 h, traits average
        s = E.State.load({}, c)
        t = s.contact_at
        self.assertEqual(s.idle(c, t + 2.9 * 3600), [])
        self.assertEqual(s.idle(c, t + 3.1 * 3600), ["bored"])
        self.assertEqual(s.idle(c, t + 3.2 * 3600), [])              # not again until twice as long
        self.assertEqual(s.idle(c, t + 4.1 * 3600), ["lonely"])
        self.assertEqual(s.why(c)[-1], "Nobody has spoken to me for 4 hours")
        self.assertEqual(s.idle(c, t + 8.1 * 3600), ["lonely", "bored"])
        self.assertEqual(sorted(s.idle(c, t + 16.1 * 3600)), ["bored", "lonely"])
        self.assertEqual(s.idle(c, t + 99 * 3600), [])               # three times in one stretch, no more
        low = s.pleasure
        s.stirred(contact=True)                                      # somebody speaks: both clocks start over
        self.assertEqual((s.lonely, s.bored), (0, 0))
        self.assertEqual(s.idle(c, time.time() + 3600), [])
        self.assertLess(low, c["baseline"]["pleasure"])
        off = cfg(loneliness=False, boredom=False)
        s2 = E.State.load({}, off)
        self.assertEqual(s2.idle(off, s2.contact_at + 99 * 3600), [])
        self.assertEqual(s2.pleasure, off["baseline"]["pleasure"])   # switched off: silence costs nothing
        social = cfg(hexaco=traits(extraversion=1.0), boredom=False, lonely_after_h=2)
        s3 = E.State.load({}, social)
        self.assertEqual(s3.idle(social, s3.contact_at + 1.1 * 3600), ["lonely"])   # 2 h x (1.5 - 1.0) = 1 h
        s4 = E.State.load({}, c)
        s4.stir_at = s4.contact_at - 1
        s4.stirred()                                                 # something happened, nobody spoke
        self.assertEqual(s4.idle(c, s4.contact_at + 4.1 * 3600), ["lonely", "bored"])

    def test_there_is_less_room_near_the_ends_and_the_range_holds(self):
        c = cfg()
        s = E.State.load({}, c)
        for _ in range(30):
            s.push((0.6, 0.6, 0.6), c)
        self.assertTrue(all(0.9 < getattr(s, a) <= 1.0 for a in E.AXES))
        for _ in range(60):
            s.push((-0.9, -0.9, -0.9), c)
        self.assertTrue(all(-1.0 <= getattr(s, a) < -0.9 for a in E.AXES))

    def test_a_long_bad_spell_leaves_a_mood_that_lingers_but_not_below_the_floor(self):
        c = cfg(floor=-0.2)
        s = E.State.load({}, c)
        t = s.last_update
        for i in range(48):                      # four hours of being put down every five minutes
            t += 300
            s.drift(c, t)
            s.push((-0.5, 0.2, -0.3), c)
        self.assertLess(s.mood_pleasure, 0.0)
        self.assertGreaterEqual(s.mood_pleasure, -0.2)
        s.drift(c, t + 3600)                     # an hour of peace: the feelings eased, the mood still pulls them down
        self.assertLess(s.pleasure, c["baseline"]["pleasure"] - 0.05)
        self.assertNotEqual(E.read(*s.felt(c, True))[0], "pleased")
        s.drift(c, t + 3 * 86400)
        self.assertAlmostEqual(s.mood_pleasure, c["baseline"]["pleasure"], delta=0.03)

    def test_morning_draws_the_days_shift_and_starts_fresh(self):
        rng = random.Random(4)
        c = cfg(variation=True, variation_size=0.6)
        s = E.State.load({}, c)
        self.assertTrue(s.morning(c, "2026-01-01", rng))
        self.assertFalse(s.morning(c, "2026-01-01", rng))            # once a day
        shifts = []
        for day in range(2, 40):
            s.push((-0.8, 0, 0), c, why="a bad evening")
            s.morning(c, f"2026-01-{day:02d}", rng)
            shifts.append(s.offset["pleasure"])
            self.assertAlmostEqual(s.pleasure, s.base(c, "pleasure"))       # yesterday is gone
            self.assertLessEqual(abs(s.offset["pleasure"]), 0.6)
            self.assertLessEqual(max(abs(v) for v in s.traits.values()), 0.6 * 0.2 + 1e-9)   # character barely moves
        self.assertTrue(min(shifts) < -0.15 and max(shifts) > 0.15)   # some mornings are bad, some good
        s.offset = {"pleasure": -0.3}
        self.assertAlmostEqual(s.base(c, "pleasure"), 0.0)
        plain = cfg(fresh_start=False)
        s2 = E.State.load({}, plain)
        s2.morning(plain, "d1", rng)
        s2.push((-0.8, 0, 0), plain)
        low = s2.pleasure
        s2.morning(plain, "d2", rng)
        self.assertEqual((s2.pleasure, s2.offset, s2.traits), (low, {}, {}))   # no variation, and the mood carries over

    def test_the_models_answer_is_read_loosely_and_junk_is_refused(self):
        p = E.parse
        self.assertEqual(p('{"pleasure": -0.6, "arousal": +0.4, "dominance": -0.5, "because": "That hit hard."}'),
                         ((-0.6, 0.4, -0.5), "That hit hard."))
        self.assertEqual(p('<think>hm</think>```json\n{ "pleasure": "+0.3", "arousal": "0", "dominance": .1 }\n```'),
                         ((0.3, 0.0, 0.1), ""))
        self.assertEqual(p('{"pleasure": 1.2, "arousal": 0, "dominance": 0}')[0][0], 1.0)
        for junk in ("", "I cannot", '{"pleasure": 0.1}', '{"pleasure": 9, "arousal": 0, "dominance": 0}'):
            self.assertIsNone(p(junk))


class Engine(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.store = MessageStore(str(self.dir / "log.jsonl"))
        hub = types.SimpleNamespace(registry=RuntimeRegistry(data_dir=tmp.name), store=self.store,
                                    rules=types.SimpleNamespace(active_list=lambda: {"rules": []}))
        self.mgr = agent_runner.AgentManager(hub, tmp.name)
        self.mgr._apply = lambda name: None
        self.mgr.upsert({"name": "ada", "label": "Ada", "kind": "api", "base_url": "http://x/v1", "cwd": tmp.name,
                         "persona": "a warm helper", "emotion": dict(ON)})
        self.mgr.upsert({"name": "bo", "kind": "api", "base_url": "http://x/v1", "cwd": tmp.name})
        E.data_dir = mqtt_out.data_dir = self.dir
        E._conf = None
        self.answers, self.asked, self.changes = [], [], []
        self.eng = E.Engine(self.mgr, ask=self.fake, threads=False)
        self.eng.on_change = lambda: self.changes.append(1)
        self.sent = []
        self._send, mqtt_out._send = mqtt_out._send, lambda conf, msgs: self.sent.extend(msgs)
        self.addCleanup(setattr, mqtt_out, "_send", self._send)
        self._engine, E.ENGINE = E.ENGINE, self.eng
        self.addCleanup(setattr, E, "ENGINE", self._engine)

    def fake(self, spec, system, user):
        self.asked.append((spec["name"], system, user))
        out = self.answers.pop(0)
        if isinstance(out, Exception):
            raise out
        return out

    def test_what_is_said_moves_the_agent_and_reaches_its_prompt(self):
        self.assertIn("pleased", self.eng.line("ada"))
        self.assertEqual(self.eng.line("bo"), "")                       # engine off: nothing
        self.assertIsNone(self.eng.status("bo"))
        self.eng.heard("bo", "Alex", "shut up")
        self.eng.heard("ada", "ada", "talking to myself")               # its own words never count
        self.eng.heard("ada", "Alex", "shut   up\n")
        self.answers.append('{"pleasure": -0.6, "arousal": 0.4, "dominance": -0.5, "because": "That hit hard; I feel small."}')
        self.eng.tick()
        name, system, user = self.asked[0]
        self.assertEqual(len(self.asked), 1)
        self.assertIn("feelings of Ada", system)
        self.assertIn("About Ada: a warm helper", system)
        self.assertIn("Agreeableness 0.5 (average)", system)
        self.assertIn('- Alex said to me: "shut up"', user)
        self.assertIn("resting state +0.30 / +0.10 / +0.30", user)
        line = self.eng.line("ada")
        self.assertIn("you feel very hurt and on edge (pleasure -0.3, arousal +0.5, dominance -0.2)", line)
        self.assertIn("your mood over the last hours: pleased, sure of yourself. Why: That hit hard; I feel small. Let it", line)
        self.assertNotIn("itself", line)
        st = self.eng.status("ada", full=True)
        self.assertEqual((st["word"], st["face"], st["why"]), ("hurt", "fearful", ["That hit hard; I feel small"]))
        self.assertLess(st["pleasure"], 0)
        self.assertTrue(self.changes)                                   # the members list hears the new word
        self.eng.tick()
        self.assertEqual(len(self.asked), 1)                            # nothing new: the model is not asked again

    def test_junk_or_a_dead_model_keeps_the_state_and_tries_again_a_few_times(self):
        before = self.eng.status("ada", full=True)["pleasure"]
        self.eng.heard("ada", "Alex", "you are the best")
        self.answers += ["no idea", RuntimeError("down")]
        self.eng.tick()
        self.assertEqual(self.eng.status("ada", full=True)["pleasure"], before)
        self.eng.tick()
        self.assertEqual(len(self.asked), 1)                            # waits before the next try
        self.eng.retry_at["ada"] = 0
        self.eng.tick()
        self.assertEqual((len(self.asked), len(self.eng.pending["ada"])), (2, 1))
        self.eng.retry_at["ada"] = 0
        self.answers.append('{"pleasure": 0.6, "arousal": 0.1, "dominance": 0.2, "because": "That was kind."}')
        self.eng.tick()
        self.assertGreater(self.eng.status("ada", full=True)["pleasure"], before)
        self.assertEqual(self.eng.pending["ada"], [])
        self.eng.heard("ada", "Alex", "hm")
        for _ in range(3):
            self.answers.append("junk")
            self.eng.retry_at["ada"] = 0
            self.eng.tick()
        self.assertEqual(self.eng.pending["ada"], [])                   # three tries, then it is let go

    def test_state_is_saved_survives_a_restart_and_can_be_reset(self):
        self.eng.event("ada", "ignored", "I told Alex about the door and got no answer", times=3)
        self.eng.tick()
        saved = json.loads((self.dir / "agents" / "ada" / "emotion.json").read_text())
        self.assertLess(saved["pleasure"], 0.0)
        again = E.Engine(self.mgr, ask=self.fake, threads=False)
        st = again.status("ada", full=True)
        self.assertEqual(st["word"], "annoyed")
        self.assertEqual(st["why"], ["I told Alex about the door and got no answer"])
        self.assertTrue(again.reset("ada"))
        self.assertEqual((again.status("ada", full=True)["pleasure"], again.status("ada", full=True)["why"]), (0.3, []))
        self.assertFalse(again.reset("bo"))
        self.assertFalse(self.eng.event("bo", "ignored"))
        self.assertFalse(self.eng.event("ada", "", ""))                 # neither a kind nor words
        self.assertTrue(self.eng.event("ada", "", "The front door opened"))
        self.assertEqual(self.eng.pending["ada"][-1][1], "The front door opened")

    def test_mqtt_and_the_status_map_carry_the_mood_and_lose_it_when_switched_off(self):
        mqtt_out.save({"enabled": True, "host": "broker", "prefix": "hub"})
        self.eng.tick()
        for _ in range(100):
            if self.sent:
                break
            time.sleep(0.02)
        topic, payload = self.sent[-1]
        self.assertEqual(topic, "hub/ada/mood")
        self.assertEqual({k: json.loads(payload)[k] for k in ("word", "face", "pleasure")},
                         {"word": "pleased", "face": "neutral", "pleasure": 0.3})
        import agents
        import app
        old = app.agent_manager
        app.agent_manager = self.mgr
        self.addCleanup(setattr, app, "agent_manager", old)
        self.mgr.hub.registry.seed({"ada": {"label": "Ada", "color": "#888888", "type": "api"}})
        self.mgr.hub.registry.register("ada", "Ada")
        status = agents.AgentTrigger(self.mgr.hub.registry, str(self.dir)).get_status()
        self.assertEqual(status["ada"]["mood"]["word"], "pleased")
        self.mgr.specs["ada"]["emotion"]["enabled"] = False
        n = len(self.changes)
        self.eng.tick()
        for _ in range(100):
            if self.sent[-1] == ("hub/ada/mood", ""):
                break
            time.sleep(0.02)
        self.assertEqual(self.sent[-1], ("hub/ada/mood", ""))            # the retained message is cleared
        self.assertEqual(len(self.changes), n + 1)
        self.assertIsNone(self.eng.status("ada"))

    def test_the_turn_note_and_saved_settings(self):
        r = agent_runner.Runner(self.mgr, "ada")
        self.assertIn("[emotion] You have feelings", r._now_note("general"))
        self.assertNotIn("[emotion]", r._turn_note("general"))          # the kept room note never holds a mood
        # an outside service's lines (mood.txt): its own [emotion] line gives way, the rest stays
        (self.dir / "agents" / "ada" / "mood.txt").write_text("[emotion] Currently grumpy (P:-0.5)\nA face: put [face:<id>] in a reply.")
        note = r._now_note("general")
        self.assertEqual(note.count("[emotion]"), 1)
        self.assertNotIn("grumpy", note)
        self.assertIn("A face: put [face:<id>]", note)
        self.eng.heard("ada", "Alex", "7", game=True)
        self.assertIn('playing the game I am hosting, answered: "7" (a short answer in a game is not curt', self.eng.pending["ada"][-1][1])
        self.eng.heard("ada", "Alex", "@ada hello @adam")
        self.assertEqual(self.eng.pending["ada"][-1][1], 'Alex said to me: "hello @adam"')
        self.assertEqual(agent_runner.Runner(self.mgr, "bo")._now_note("general"), "")
        # a form from before the engine sends no "emotion": the stored settings stay
        self.mgr.upsert({"name": "ada", "kind": "api", "llm": self.mgr.specs["ada"]["llm"], "cwd": str(self.dir)})
        self.assertTrue(self.mgr.specs["ada"]["emotion"]["enabled"])
        self.mgr.upsert({"name": "ada", "kind": "api", "llm": self.mgr.specs["ada"]["llm"], "cwd": str(self.dir),
                         "emotion": {"enabled": True, "hexaco": {"openness": 3}}})
        self.assertEqual(self.mgr.specs["ada"]["emotion"]["hexaco"]["openness"], 1.0)
        self.assertNotIn("emotion", self.mgr.specs["bo"])               # never set: nothing is added

    def test_being_ignored_reactions_failures_and_interruptions(self):
        p = lambda: self.eng.status("ada", full=True)["pleasure"]
        base = p()
        self.assertTrue(self.eng.event("ada", "ignored", "I said the door was open and got no answer", times=2))
        hit = base - p()
        self.assertGreater(hit, 0.2)
        self.eng.reset("ada")
        self.mgr.specs["ada"]["emotion"].update(ignored_strength=0.5)
        self.eng.event("ada", "ignored", "again", times=2)
        self.assertAlmostEqual(base - p(), hit / 2, delta=0.02)        # how much it minds is a setting
        self.eng.reset("ada")
        self.mgr.specs["ada"]["emotion"].update(ignored=False)
        self.assertFalse(self.eng.event("ada", "ignored", "again"))    # and it can be switched off
        self.assertEqual(p(), base)
        self.eng.reaction("ada", "Alex", "👍")
        self.assertGreater(p(), base)
        self.assertIn("Alex reacted to what I said with 👍", self.eng.status("ada", full=True)["why"])
        self.eng.reset("ada")
        self.eng.reaction("ada", "Alex", "👎")
        self.assertLess(p(), base)
        self.eng.reset("ada")
        self.eng.reaction("ada", "Alex", "🤔")                         # means nothing either way
        self.eng.reaction("ada", "ada", "👍")                           # its own reaction
        self.assertEqual(p(), base)
        r = agent_runner.Runner(self.mgr, "ada")
        r.working = True
        r.halt_turn()
        self.assertLess(p(), base)
        self.assertEqual(self.eng.status("ada", full=True)["why"], ["I was stopped in the middle of answering"])
        self.eng.reset("ada")
        r.activity = [{"k": "tool", "name": "web_search", "err": True}, {"k": "tool", "name": "read_file"}]
        r._act_t0, r.stream_id, r.thread = time.monotonic(), "s", None
        r._deliver("general", "That did not work.")
        self.assertLess(p(), base)
        self.assertEqual(self.eng.status("ada", full=True)["why"], ["A tool I used failed: web_search"])

    def test_the_event_route_for_outside_services(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        import app
        old = app.agent_manager
        app.agent_manager = self.mgr
        self.addCleanup(setattr, app, "agent_manager", old)
        api = FastAPI()
        api.add_api_route("/api/emotion/event", app.emotion_event, methods=["POST"])
        api.add_api_route("/api/managed-agents/{name}/emotion", app.managed_agents_emotion, methods=["GET"])
        c = TestClient(api, base_url="http://127.0.0.1")
        r = c.post("/api/emotion/event", json={"agent": "@Ada", "kind": "ignored", "text": "no answer about the door", "times": 3})
        self.assertEqual((r.status_code, r.json()["word"]), (200, "annoyed"))
        self.assertEqual(c.post("/api/emotion/event", json={"agent": "ada", "text": "The front door opened"}).status_code, 200)
        self.assertEqual(self.eng.pending["ada"][-1][1], "The front door opened")
        self.assertEqual(c.post("/api/emotion/event", json={"agent": "ada", "kind": "nope"}).status_code, 400)
        self.assertEqual(c.post("/api/emotion/event", json={"agent": "bo", "kind": "praise"}).status_code, 400)
        self.assertEqual(c.post("/api/emotion/event", json={"agent": "ada"}).status_code, 400)
        self.assertEqual(c.get("/api/managed-agents/bo/emotion").json(), {"off": True})
        self.assertEqual(c.get("/api/managed-agents/zed/emotion").status_code, 404)

    def test_no_loneliness_at_night_and_the_morning_starts_the_clocks(self):
        self.assertIsNone(E.save({"morning": 7, "night": 22}))
        self.assertEqual([E.night(h) for h in (21, 22, 3, 6, 7, 12)], [False, True, True, True, False, False])
        st = self.eng.state("ada", self.eng.cfg("ada"))
        st.contact_at = st.stir_at = time.time() - 30 * 3600
        real = E.night
        E.night = lambda hour=None: True
        self.addCleanup(setattr, E, "night", real)
        self.eng.tick()
        self.assertEqual((st.lonely, st.bored), (0, 0))                # asleep: nothing builds
        E.night = lambda hour=None: False
        self.eng.tick()
        self.assertEqual((st.lonely, st.bored), (1, 1))
        st.day = "1999-01-01"                                           # the next morning
        E.load.__globals__["_conf"] = None
        E.save({"morning": 0})
        self.eng.tick()
        self.assertEqual((st.lonely, st.bored), (0, 0))
        self.assertLess(time.time() - st.contact_at, 5)

    def test_feel_first_and_mood_in_the_voice(self):
        self.assertEqual((self.eng.voice("ada", 1.0), self.eng.voice("ada", None, "Hi")), (1.0, ""))   # off: untouched
        self.mgr.specs["ada"]["emotion"].update(react_first=True, voice_mood=True)
        self.eng.threads = True
        self.eng.heard("ada", "Alex", "that was unkind of me to say")
        self.answers.append('{"pleasure": -0.6, "arousal": -0.3, "dominance": -0.4, "because": "Unkind words hurt."}')
        line = self.eng.line("ada")                                     # the line waits for the feeling
        self.assertIn("Why: Unkind words hurt", line)
        self.assertIn("never repeat or describe what was said to you", line)
        self.assertIn("never quotes or retells", self.asked[0][1])
        self.assertLess(self.eng.voice("ada", 1.0), 1.0)                # low: slower
        self.assertEqual(self.eng.voice("ada", None, "Fine."), "[sigh]")
        self.eng.reset("ada")
        self.eng.event("ada", "praise", "thanked")
        self.assertEqual(self.eng.voice("ada", None, "Fine."), "")

    def test_sleep_the_diary_and_an_empty_house(self):
        self.assertEqual((self.eng.tool_specs("ada"), self.eng.diary_write("ada", "x")), ([], "error: you keep no diary"))
        self.mgr.specs["ada"]["emotion"].update(sleeps=True, diary=True)
        real = E.night
        self.addCleanup(setattr, E, "night", real)
        E.night = lambda hour=None: True
        self.assertIn("you were asleep: you are drowsy", self.eng.line("ada"))
        E.night = lambda hour=None: False
        self.assertNotIn("asleep", self.eng.line("ada"))
        self.assertEqual(self.eng.diary_write("ada", "A quiet day.  I felt  fine."), "Written in your diary.")
        self.eng.diary_write("ada", "Second entry.")
        rows = self.eng.diary("ada")
        self.assertEqual([r["text"] for r in rows], ["A quiet day. I felt fine.", "Second entry."])
        import profiles
        old = {"id": "old1", "at": 1.0, "day": "2020-01-05", "text": "The furnace broke and everyone was cold."}
        profiles.write("ada", "diary.jsonl", "\n".join(json.dumps(r) for r in [old] + rows))
        self.assertNotIn("furnace", self.eng.line("ada"))                # only today and yesterday stay with it
        self.assertIn("furnace", self.eng.diary_read("ada", about="what happened with the furnace?"))
        self.assertIn("2020-01-05: The furnace", self.eng.diary_read("ada", day="2020-01-05"))
        self.assertEqual(self.eng.diary_read("ada", about="unicorns"), "Nothing in your diary about that.")
        self.assertEqual([t["function"]["name"] for t in self.eng.tool_specs("ada")], ["diary_write", "diary_read"])
        self.mgr.specs["ada"]["emotion"]["interests"] = "Power ballads\n\nReally bad jokes"
        self.assertIn("- Power ballads\n- Really bad jokes", E.interests("ada"))
        first = self.eng.interest("ada")
        self.assertIn(first, ("Power ballads", "Really bad jokes"))
        self.assertEqual(self.eng.interest("ada"), "")                    # one a day
        self.eng.state("ada", self.eng.cfg("ada")).interest_day = "1999-01-01"
        self.assertNotIn(self.eng.interest("ada"), ("", first))           # and not the same one again
        self.assertIn("From your diary: (", self.eng.line("ada"))
        self.assertIn("A quiet day. I felt fine.", self.eng.line("ada"))
        rows = self.eng.diary("ada")
        self.assertTrue(self.eng.diary_delete("ada", rows[1]["id"]))
        self.assertFalse(self.eng.diary_delete("ada", "nope"))
        self.assertNotIn("A quiet day", self.eng.line("ada"))           # deleted: gone from what it remembers
        # the end of the day: a turn of its own, and nothing it says there is posted
        sent = []
        self.eng.hub = types.SimpleNamespace(agents=types.SimpleNamespace(trigger_sync=lambda n, message="", channel="general": sent.append((n, channel))))
        r = self.mgr.runners["ada"] = agent_runner.Runner(self.mgr, "ada")
        hour = E.now_local().hour
        E.save({"night": hour})
        E._conf = None
        st, cfg = self.eng.state("ada", self.eng.cfg("ada")), self.eng.cfg("ada")
        self.eng.diary_time("ada", st, cfg)
        self.eng.diary_time("ada", st, cfg)
        self.assertEqual(sent, [("ada", "general")])                     # once a day
        self.assertTrue(r.nudges["general"]["private"])
        r._nudge, r.activity, r._act_t0, r.stream_id, r.thread = r.nudges.pop("general"), [], time.monotonic(), "s", None
        n = len(self.store.get_recent(50))
        r._deliver("general", "Dear diary, this should never be posted.")
        self.assertEqual(len(self.store.get_recent(50)), n)
        st.contact_at = time.time() - 9 * 3600                           # an empty house is not neglect
        self.eng.rest("ada")
        self.assertEqual(st.idle(cfg, time.time()), [])

    def test_a_deleted_agent_is_forgotten_and_its_folder_stays_gone(self):
        self.eng.event("ada", "praise", "thanked")
        self.mgr.delete("ada")
        self.eng.tick()
        self.assertNotIn("ada", self.eng.states)
        self.assertFalse((self.dir / "agents" / "ada").exists())

    def test_engine_settings_and_the_local_day(self):
        self.assertEqual(E.load(), {"server": "", "model": "", "morning": 7, "night": 22, "timezone": ""})
        self.assertIn("unknown time zone", E.save({"timezone": "Mars/Olympus"}))
        self.assertEqual(E.save({"server": "nope"}), "no such LLM server")
        self.assertIsNone(E.save({"timezone": "America/Chicago", "morning": 6}))
        self.assertEqual((E.load()["timezone"], E.load()["morning"]), ("America/Chicago", 6))
        self.assertEqual(str(E.now_local().tzinfo), "America/Chicago")
        sid = self.mgr.specs["ada"]["llm"]
        self.assertIsNone(E.save({"server": sid, "model": "small"}))
        m = self.eng._model(self.mgr.specs["ada"])
        self.assertEqual((m["model"], m["base_url"]), ("small", "http://x/v1"))
        self.assertIsNone(E.save({"server": ""}))
        self.assertEqual(E.load()["model"], "")                          # no server: no model either


if __name__ == "__main__":
    unittest.main()
