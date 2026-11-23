"""Games: an agent hosts a game for the people in a channel or a room, by voice or in chat.

The agent brings the content (questions, clues, the story); this module keeps what a model loses
count of: who plays, whose turn it is, the round and the score. A game is a name, its rules in
plain words and a number of rounds (Settings > Agents > Games; a few come built in).

Host agents get one tool, game(action, ...): start, result, score, next, state, end. While a game
runs in a channel, the host's prompt carries its rules, the board and every question already asked
(taken from what the host said, so none is repeated), each turn.

  data/games.json         {"enabled", "hosts": [agent], "games": [{"name", "rules", "rounds"}]}
  data/games_state.json   {channel: {"game", "host", "players", "scores", "round", "rounds", "turn"}}
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path

data_dir = Path("./data")   # set by start()
FILE, STATE = "games.json", "games_state.json"
_lock = threading.RLock()
BUILT_IN = [
    {"name": "Trivia", "rounds": 10, "rules": "Ask one question per turn to the player whose turn it is. A right answer scores 1. "
     "Say the answer after a wrong one. With a question bank (Settings > Agents > Games) the questions and answers come "
     "from it, in four levels of difficulty; without one, make up your own on mixed subjects and never repeat one."},
    {"name": "Twenty questions", "rounds": 3, "rules": "Think of a person, place or thing and keep it secret. Players take turns "
     "asking yes or no questions; count them aloud. Whoever names it scores 1 and the round ends; after 20 questions reveal it. "
     "Never change what you thought of."},
    {"name": "Word chain", "rounds": 20, "rules": "Pick a category. Each player in turn says a word in it that starts with the "
     "last letter of the word before. A repeat, a wrong letter or a long silence loses the turn; a good word scores 1."},
    {"name": "Story", "rounds": 12, "rules": "Start a story with one sentence. Each player in turn adds one sentence, and you add "
     "one between players to keep it moving. No score. End it with a closing line after the last round."},
    {"name": "Name that tune", "rounds": 8, "rules": "Pick a well-known song and keep its title secret. Describe it in one "
     "sentence without the title or the artist, then give one more clue per wrong guess, up to three. The first right title "
     "scores 1; naming the artist too scores 1 more."},
]


def load() -> dict:
    try:
        raw = json.loads((data_dir / FILE).read_text("utf-8"))
    except (OSError, ValueError):
        raw = {}
    raw = raw if isinstance(raw, dict) else {}
    games = [{"name": str(g.get("name") or "").strip()[:60], "rules": str(g.get("rules") or "").strip()[:1500],
              "rounds": max(1, min(100, int(g.get("rounds") or 10)))}
             for g in (raw.get("games") if isinstance(raw.get("games"), list) else BUILT_IN)
             if isinstance(g, dict) and str(g.get("name") or "").strip() and str(g.get("rules") or "").strip()]
    return {"enabled": raw.get("enabled") is True,
            "hosts": [str(h).lstrip("@").lower()[:64] for h in (raw.get("hosts") if isinstance(raw.get("hosts"), list) else []) if str(h).strip()],
            "games": games}


def save(new: dict) -> dict:
    conf = {**load(), **{k: new[k] for k in ("enabled", "hosts", "games") if k in (new or {})}}
    (data_dir / "games.tmp").write_text(json.dumps(conf, indent=2), "utf-8")
    (data_dir / "games.tmp").replace(data_dir / FILE)
    return load()


def _state() -> dict:
    try:
        return json.loads((data_dir / STATE).read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def _put(state: dict):
    (data_dir / "games_state.tmp").write_text(json.dumps(state), "utf-8")
    (data_dir / "games_state.tmp").replace(data_dir / STATE)


def hosts(agent: str) -> bool:
    c = load()
    return c["enabled"] and agent in c["hosts"]


def _board(g: dict) -> str:
    who = g["players"][g["turn"] % len(g["players"])] if g["players"] else "nobody"
    score = ", ".join(f"{p} {g['scores'].get(p, 0)}" for p in g["players"]) or "no players"
    return f"{g['game']}: round {g['round']} of {g['rounds']}. Turn: {who}. Score: {score}."


def _question(g: dict) -> str:
    """Trivia: the next question from the bank (trivia.py) for the host to read out; "" without a bank."""
    if g["game"].lower() != "trivia":
        return ""
    import trivia
    q = trivia.draw(g.get("level", 2), g.get("category", ""))
    g["q"] = q
    if not q:
        return (" The question bank has nothing at this level" + (" in that category" if g.get("category") else "")
                + ": make up the questions yourself.") if g.get("banked") else ""
    g["banked"] = True
    return (f" Read this question out, word for word: [{q['category']}] {q['question']} The answer, which you keep to "
            f"yourself until they have answered: {q['answer']}")


def play(agent: str, channel: str, action: str, game: str = "", players=None, player: str = "", points: int = 1,
         level: int = 0, category: str = "") -> str:
    """The host's tool. -> what to tell the players, or an error for the agent."""
    if not hosts(agent):
        return "error: you do not host games"
    with _lock:
        state = _state()
        g = state.get(channel)
        if action == "start":
            conf = load()
            pick = next((x for x in conf["games"] if x["name"].lower() == str(game).strip().lower()), None)
            if not pick:
                return "error: no such game. Games: " + ", ".join(x["name"] for x in conf["games"])
            names = [re.sub(r"\s+", " ", str(p)).strip()[:30] for p in (players if isinstance(players, list) else str(players or "").split(","))]
            names = list(dict.fromkeys(n for n in names if n))
            if not names:
                return "error: ask who is playing first, then start with their names"
            g = state[channel] = {"game": pick["name"], "host": agent, "players": names, "scores": {}, "round": 1,
                                  "rounds": pick["rounds"], "turn": 0, "rules": pick["rules"],
                                  "level": max(1, min(4, int(level or 2))), "category": str(category or "")[:60], "banked": False}
            ask = _question(g)
            _put(state)
            return "Started. " + _board(g) + (f" Level {g['level']} of 4." if g.get("q") else "") + ask
        if not g:
            return "error: no game is running here. Start one: " + ", ".join(x["name"] for x in load()["games"])
        if action == "bad":         # the question in hand is wrong or cannot be asked aloud: never again, and another one
            if g.get("q"):
                import trivia
                trivia.flag(g["q"]["id"])
            ask = _question(g)
            _put(state)
            return "That question is set aside for good." + (ask or " Ask another one of your own.")
        if action == "result":      # one call per answer: the point when it was right, then the next turn
            who = next((p for p in g["players"] if p.lower() == str(player).strip().lower()), None) or g["players"][g["turn"] % len(g["players"])]
            if points and int(points) > 0:
                g["scores"][who] = g["scores"].get(who, 0) + min(10, int(points))
            action = "next"
        if action == "score":
            who = next((p for p in g["players"] if p.lower() == str(player).strip().lower()), None)
            if not who:
                return "error: no such player. Players: " + ", ".join(g["players"])
            g["scores"][who] = g["scores"].get(who, 0) + max(-10, min(10, int(points or 1)))
        elif action == "next":
            g["turn"] += 1
            if g["turn"] % len(g["players"]) == 0:
                g["round"] += 1
            if g["round"] > g["rounds"]:
                action = "end"
        if action == "end":
            state.pop(channel, None)
            _put(state)
            top = max(g["scores"].values(), default=0)
            won = [p for p in g["players"] if g["scores"].get(p, 0) == top and top > 0]
            return ("The game is over. Final score: " + (", ".join(f"{p} {g['scores'].get(p, 0)}" for p in g["players"]))
                    + (f". Winner: {' and '.join(won)}." if won else "."))
        if action not in ("score", "next", "state"):
            return "error: action is one of start, result, bad, score, next, state, end"
        ask = _question(g) if action == "next" else ""
        _put(state)
        return _board(g) + ask


def running(agent: str, channel: str) -> bool:
    """Is this agent hosting a game in this channel right now?"""
    g = _state().get(channel)
    return bool(g and g.get("host") == agent and hosts(agent))


def said(agent: str, channel: str, text: str):
    """The host said something while its game runs: its questions are remembered, so none comes twice."""
    if "?" not in (text or ""):
        return
    with _lock:
        state = _state()
        g = state.get(channel)
        if not g or g.get("host") != agent:
            return
        asked = g.setdefault("asked", [])
        for q in re.findall(r"[^.!?\n]{12,200}\?", re.sub(r"[*_`#]+", "", text)):
            q = " ".join(q.split())
            if q not in asked:
                asked.append(q)
        del asked[:-80]
        _put(state)


def now_lines(agent: str, channel: str) -> str:
    """For the host's prompt while a game runs in this channel; a host with no game learns it can start one."""
    if not hosts(agent):
        return ""
    g = _state().get(channel)
    if not g or g.get("host") != agent:
        return "[games] You can host a game here when people ask for one (the game tool). Games: " + ", ".join(x["name"] for x in load()["games"]) + "."
    asked = g.get("asked") or []
    return (f"[game] You are hosting. The board, which only the game tool changes: {_board(g)} Rules: {g['rules']} "
            "After every answer call the game tool once with action result, the player, and points 1 for a right answer or "
            "0 for a wrong one: it scores and moves to the next turn. Then say the round and the score exactly as the tool "
            "returns them; never count yourself, and never say a score the board does not show. An answer that is right in "
            "substance counts, however short. End the game when they want to stop. Keep it lively and short."
            + (f" The question in hand, from the bank (level {g.get('level')}): [{g['q']['category']}] {g['q']['question']} "
               f"Its answer, kept to yourself until they have answered: {g['q']['answer']}. Never swap it for one of your "
               "own; if it is plainly wrong or cannot be asked aloud, call the tool with action bad." if g.get("q") else
               " Already asked, never ask or rephrase these again: " + " | ".join(asked[-40:]) if asked else ""))


def tool_specs(agent: str) -> list[dict]:
    if not hosts(agent):
        return []
    s = {"type": "string"}
    return [{"type": "function", "function": {
        "name": "game", "description": "Host a game: keeps the players, the turn, the round and the score. start needs game "
        "and players; for Trivia also level (1 easy to 4 hardest, ask the players) and optionally a category. result, after "
        "each answer: player and points (1 right, 0 wrong); it scores, moves on and gives the next question. bad sets a "
        "wrong question aside. score adds points without moving on; next ends a turn without a point; state reads the "
        "board; end stops the game.",
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["start", "result", "bad", "score", "next", "state", "end"]}, "game": s,
            "players": {"type": "array", "items": s}, "player": s, "points": {"type": "integer"},
            "level": {"type": "integer"}, "category": s}, "required": ["action"]}}}]
