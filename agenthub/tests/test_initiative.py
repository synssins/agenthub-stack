"""Initiative: an agent that may start talking by itself; what starts it, what holds it back."""
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent_runner
import emotion_engine as E
import initiative as I
from registry import RuntimeRegistry
from store import MessageStore

ON = {"enabled": True, "channel": "home", "gap_min": 60, "quiet_from": 22, "quiet_to": 7}


class Initiative(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.store = MessageStore(str(self.dir / "log.jsonl"))
        self.triggered = []
        hub = types.SimpleNamespace(registry=RuntimeRegistry(data_dir=tmp.name), store=self.store,
                                    rules=types.SimpleNamespace(active_list=lambda: {"rules": []}),
                                    agents=types.SimpleNamespace(trigger_sync=lambda name, message="", channel="general":
                                                                 self.triggered.append((name, channel))))
        self.mgr = hub.agent_manager = agent_runner.AgentManager(hub, tmp.name)
        self.mgr._apply = lambda name: None
        self.mgr.upsert({"name": "ada", "kind": "api", "base_url": "http://x/v1", "cwd": tmp.name, "channels": ["home"],
                         "emotion": {"enabled": True}, "initiative": dict(ON)})
        self.mgr.upsert({"name": "bo", "kind": "api", "base_url": "http://x/v1", "cwd": tmp.name})
        self.r = self.mgr.runners["ada"] = agent_runner.Runner(self.mgr, "ada")
        self.mgr.specs["ada"].pop("llm_error", None)      # the test server has no model list; turns are faked
        E.data_dir = self.dir
        E._conf = None
        self.ini = I.Initiative(hub)
        self.eng = E.Engine(self.mgr, ask=lambda *a: "", threads=False)
        for mod, attr, new in ((I, "INITIATIVE", self.ini), (E, "ENGINE", self.eng)):
            self.addCleanup(setattr, mod, attr, getattr(mod, attr))
            setattr(mod, attr, new)
        self.hour = 12
        p = mock.patch.object(E, "now_local", lambda: types.SimpleNamespace(hour=self.hour, strftime=lambda f: "12:00 PM"))
        p.start()
        self.addCleanup(p.stop)

    def test_settings(self):
        self.assertEqual(I.clean(None), I.DEFAULTS)
        self.assertEqual(I.clean({"enabled": 1, "gap_min": 0, "quiet_from": 99, "channel": 5}),
                         {"enabled": False, "channel": "5", "gap_min": 5, "quiet_from": 23, "quiet_to": 7})
        q = lambda h, a=22, b=7: I.quiet({"quiet_from": a, "quiet_to": b}, h)
        self.assertEqual([q(21), q(22), q(3), q(7), q(12)], [False, True, True, False, False])
        self.assertEqual([q(12, 9, 9), q(9, 9, 9)], [False, False])           # the same hour twice: never quiet
        self.assertEqual(self.mgr.specs["ada"]["initiative"], ON)
        self.mgr.upsert({"name": "ada", "kind": "api", "llm": self.mgr.specs["ada"]["llm"]})   # an older form: kept
        self.assertTrue(self.mgr.specs["ada"]["initiative"]["enabled"])
        self.assertNotIn("initiative", self.mgr.specs["bo"])

    def test_what_holds_it_back_and_what_starts_a_turn(self):
        self.assertEqual(self.ini.consider("bo", "x"), "initiative is off for this agent")
        self.hour = 23
        self.assertEqual(self.ini.consider("ada", "x", urgent=True), "quiet hours")   # nothing gets past the night
        self.hour = 12
        self.r.working = True
        self.assertEqual(self.ini.consider("ada", "x"), "the agent is busy")
        self.assertEqual(self.ini.consider("ada", "The smoke alarm is sounding", urgent=True), "")   # waits for its turn
        self.r.nudges.clear()
        self.triggered.clear()
        self.r.working = False
        self.assertEqual(self.ini.consider("ada", "Nobody has spoken to me for 4 hours"), "")
        self.assertEqual(self.triggered, [("ada", "home")])
        nudge = self.r.nudges["home"]
        self.assertIn("Nobody has spoken to me for 4 hours. If you want to say something", nudge["text"])
        self.assertIn("reply exactly (no reply)", nudge["text"])
        self.assertEqual(self.ini.consider("ada", "again"), "the agent is busy")       # one at a time
        self.r.nudges.clear()
        self.ini.spoke("ada")
        self.assertEqual(self.ini.consider("ada", "small talk"), "it said something not long ago")
        self.assertEqual(self.ini.consider("ada", "The front door is open", key="door", urgent=True), "")
        self.assertEqual(self.r.nudges["home"]["key"], "door")
        self.assertIn("worth saying to the people around you now", self.r.nudges["home"]["text"])
        self.assertNotIn("(no reply)", self.r.nudges["home"]["text"])
        self.r.nudges.clear()
        self.mgr.specs["ada"]["initiative"]["channel"] = "gone"                        # not a member: general
        self.ini.consider("ada", "x", urgent=True)
        self.assertEqual(self.triggered[-1], ("ada", "general"))

    def run_turn(self, answer):
        calls = []

        def fake(spec, system, msgs, tools=None, tool_choice=None, **kw):
            calls.append(msgs)
            return {"content": answer}
        with mock.patch.object(agent_runner, "call_api_message", fake):
            self.r._turn("home")
        return calls

    def test_a_turn_nobody_asked_for_speaks_or_stays_silent(self):
        self.store.add("Alex", "@ada hello", channel="home")
        self.run_turn("Hi.")                                             # an ordinary turn first
        n = len(self.store.get_recent(50, channel="home"))
        self.assertEqual(self.run_turn("again?"), [])                    # nothing new: no model call at all
        self.ini.consider("ada", "The front door is open", key="door", urgent=True)
        calls = self.run_turn("The front door is open, by the way.")
        last = calls[0][-1]["content"]
        self.assertTrue(last.startswith("(Right now; not a chat message)"))
        self.assertIn("[emotion] You have feelings", last)
        self.assertIn("[initiative] Nobody asked you anything just now. The front door is open.", last)
        msg = self.store.get_recent(1, channel="home")[0]
        self.assertEqual((msg["sender"], msg["text"]), ("ada", "The front door is open, by the way."))
        self.assertEqual(msg["metadata"]["initiative"], {"why": "The front door is open", "key": "door"})
        self.assertLess(time.time() - self.ini.spoke_at["ada"], 5)
        self.assertEqual(self.r.nudges, {})
        notes = self.mgr.state["ada"].get("notes", {})
        self.assertNotIn("[initiative]", str(notes))                     # what was true for one turn is not kept
        self.assertNotIn("[emotion]", str(notes))
        self.ini.consider("ada", "A quiet moment", urgent=True)
        self.run_turn("(no reply)")                                       # it chose silence: nothing is posted
        self.assertEqual(len(self.store.get_recent(50, channel="home")), n + 1)
        self.store.add("Alex", "@ada thanks", channel="home")
        calls = self.run_turn("Welcome.")
        self.assertNotIn("[initiative]", str(calls[0]))                   # and the nudge is gone from later turns
        self.assertNotIn("initiative", self.store.get_recent(1, channel="home")[0]["metadata"])

    def test_silence_and_quiet_moments_start_one(self):
        st = self.eng.state("ada", self.eng.cfg("ada"))
        st.contact_at = time.time() - 5 * 3600
        st.stir_at = time.time()
        real, E.night = E.night, lambda hour=None: False
        self.addCleanup(setattr, E, "night", real)
        self.eng.tick()
        self.assertEqual(self.triggered, [("ada", "home")])               # lonely: it may say so
        self.assertIn("Nobody has spoken to me for 5 hours", self.r.nudges["home"]["text"])
        self.r.nudges.clear()
        self.triggered.clear()
        self.eng.ask = lambda *a: '{"pleasure": 0, "arousal": 0.05, "dominance": 0, "because": "Just the hall light."}'
        self.eng.event("ada", "", "Motion: Hall")
        self.eng.tick()
        self.assertEqual(self.ini.news, {})                               # routine: nothing to speak of later
        self.eng.ask = lambda *a: '{"pleasure": -0.1, "arousal": 0.4, "dominance": -0.2, "because": "A stranger at the door worries me."}'
        self.eng.event("ada", "", "Camera: an unknown person at the front door")
        self.eng.event("bo", "", "ignored: no engine")
        self.eng.tick()
        self.assertEqual(self.ini.news, {"ada": ["A stranger at the door worries me."]})
        self.ini.tick()
        self.assertEqual(self.triggered, [])                              # the gap has not passed
        self.ini.started = self.ini.asked_at["ada"] = time.time() - 3700
        self.ini.tick()
        self.assertEqual(self.triggered, [("ada", "home")])
        self.assertIn("a quiet moment. Since you last spoke up: A stranger at the door worries me", self.r.nudges["home"]["text"])
        self.r.nudges.clear()
        self.ini.asked_at["ada"] = time.time() - 3700
        self.ini.tick()
        self.assertEqual(len(self.triggered), 1)                          # nothing new since: it is left alone

    def test_the_event_route_can_ask_for_it_to_be_said(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        import app
        api = FastAPI()
        api.add_api_route("/api/emotion/event", app.emotion_event, methods=["POST"])
        c = TestClient(api, base_url="http://127.0.0.1")
        r = c.post("/api/emotion/event", json={"agent": "ada", "text": "Front door opened", "speak": True, "key": "door"})
        self.assertEqual((r.status_code, r.json()["speaks"], r.json()["felt"]), (200, True, True))
        self.assertEqual(self.r.nudges["home"]["key"], "door")
        r = c.post("/api/emotion/event", json={"agent": "ada", "text": "Front door opened", "speak": True})
        self.assertEqual((r.json()["speaks"], r.json()["why_not"]), (False, "the agent is busy"))
        self.assertEqual(c.post("/api/emotion/event", json={"agent": "bo", "text": "x", "speak": True}).status_code, 400)
        self.mgr.specs["ada"]["emotion"]["enabled"] = False              # no feelings, but it may still speak up
        self.r.nudges.clear()
        r = c.post("/api/emotion/event", json={"agent": "ada", "text": "Front door opened", "speak": True})
        self.assertEqual((r.status_code, r.json()["felt"], r.json()["speaks"]), (200, False, True))


if __name__ == "__main__":
    unittest.main()
