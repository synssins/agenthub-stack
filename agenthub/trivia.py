"""A bank of trivia questions with answers, so a host agent reads questions instead of inventing them.

Nothing ships with AgentHub: each source is downloaded by the owner (Settings > Agents > Games),
once, into data/trivia.sqlite, and used offline from then on. Four levels, 1 the easiest.

  opentdb     Open Trivia Database (opentdb.com), CC BY-SA 4.0: easy / medium / hard -> 1 / 2 / 3
  triviaapi   The Trivia API (the-trivia-api.com), free for non-commercial use: 1 / 2 / 3, niche hard ones -> 4
  jeopardy    Jeopardy! clues collected by fans (github.com/jwolle1/jeopardy_clue_dataset): the high-value
              clues and Final Jeopardy -> 4. Not licensed for redistribution: for use at home.
  own         questions the owner writes

A question is drawn at random among those not asked yet at its level; when a level runs out the
oldest asked come round again. A question flagged wrong is never drawn again.
"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import random
import sqlite3
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)
data_dir = Path("./data")   # set by run.py
SOURCES = {
    "opentdb": ("Open Trivia Database", "About 5,000 checked questions in three difficulties. CC BY-SA 4.0. Takes about ten minutes: the site allows one request every five seconds."),
    "triviaapi": ("The Trivia API", "Several thousand questions in three difficulties; its niche hard ones become level 4. Free for non-commercial use."),
    "jeopardy": ("Jeopardy! clues", "The high-value clues and Final Jeopardy of 40 seasons, as level 4. An 80 MB download collected by fans; not licensed for redistribution."),
}
jobs: dict[str, dict] = {}   # source -> {"state": "running" | "done" | "failed", "got": n, "error": str}
_lock = threading.RLock()
_UA = {"User-Agent": "AgentHub", "Accept": "application/json"}


def _db() -> sqlite3.Connection:
    con = sqlite3.connect(data_dir / "trivia.sqlite", timeout=30)
    con.execute("CREATE TABLE IF NOT EXISTS q (id INTEGER PRIMARY KEY, src TEXT, ext TEXT, level INTEGER, category TEXT, "
                "question TEXT, answer TEXT, asked_at REAL DEFAULT 0, bad INTEGER DEFAULT 0, UNIQUE(src, ext))")
    return con


def add(src: str, level: int, category: str, question: str, answer: str, ext: str = "", con=None) -> bool:
    """One question into the bank. -> whether it was new."""
    question, answer = " ".join(str(question).split())[:500], " ".join(str(answer).split())[:200]
    if not question or not answer:
        return False
    ext = ext or hashlib.sha1(question.lower().encode(), usedforsecurity=False).hexdigest()[:16]
    own = con is None
    con = con or _db()
    try:
        cur = con.execute("INSERT OR IGNORE INTO q (src, ext, level, category, question, answer) VALUES (?, ?, ?, ?, ?, ?)",
                          (src, ext, max(1, min(4, int(level))), " ".join(str(category).split())[:80], question, answer))
        if own:
            con.commit()
        return cur.rowcount > 0
    finally:
        if own:
            con.close()


def draw(level: int, category: str = "") -> dict | None:
    """The next question of this level (and category, when one is named): not asked before if any is left."""
    with _lock:
        con = _db()
        try:
            where, args = "level = ? AND bad = 0", [max(1, min(4, int(level)))]
            if category.strip():
                where += " AND category LIKE ?"
                args.append(f"%{category.strip()}%")
            fresh = con.execute(f"SELECT COUNT(*) FROM q WHERE {where} AND asked_at = 0", args).fetchone()[0]
            if fresh:
                row = con.execute(f"SELECT id, category, question, answer FROM q WHERE {where} AND asked_at = 0 LIMIT 1 OFFSET ?",
                                  args + [random.randrange(fresh)]).fetchone()
            else:      # all asked: the ones asked longest ago come round again
                row = con.execute(f"SELECT id, category, question, answer FROM q WHERE {where} ORDER BY asked_at LIMIT 1", args).fetchone()
            if not row:
                return None
            con.execute("UPDATE q SET asked_at = ? WHERE id = ?", (time.time(), row[0]))
            con.commit()
            return {"id": row[0], "category": row[1], "question": row[2], "answer": row[3]}
        finally:
            con.close()


def flag(qid: int):
    """A question whose answer is wrong or that cannot be asked aloud: never drawn again."""
    con = _db()
    con.execute("UPDATE q SET bad = 1 WHERE id = ?", (int(qid),))
    con.commit()
    con.close()


def delete(qid: int):
    con = _db()
    con.execute("DELETE FROM q WHERE id = ? AND src = 'own'", (int(qid),))
    con.commit()
    con.close()


def status() -> dict:
    con = _db()
    try:
        counts = {s: {str(l): 0 for l in range(1, 5)} for s in list(SOURCES) + ["own"]}
        for src, level, n in con.execute("SELECT src, level, COUNT(*) FROM q WHERE bad = 0 GROUP BY src, level"):
            counts.setdefault(src, {})[str(level)] = n
        own = [{"id": r[0], "level": r[1], "category": r[2], "question": r[3], "answer": r[4]}
               for r in con.execute("SELECT id, level, category, question, answer FROM q WHERE src = 'own' ORDER BY id DESC LIMIT 200")]
        bad = con.execute("SELECT COUNT(*) FROM q WHERE bad = 1").fetchone()[0]
    finally:
        con.close()
    return {"sources": [{"id": s, "name": n, "about": a, "counts": counts[s], "job": jobs.get(s)} for s, (n, a) in SOURCES.items()],
            "levels": {str(l): sum(c.get(str(l), 0) for c in counts.values()) for l in range(1, 5)}, "own": own, "flagged": bad}


def _get(url: str, timeout: float = 40):
    with urllib.request.urlopen(urllib.request.Request(url, headers=_UA), timeout=timeout) as r:
        return json.loads(r.read())


def _opentdb(job: dict):
    un = urllib.parse.unquote
    token = _get("https://opentdb.com/api_token.php?command=request")["token"]
    con = _db()
    try:
        amount = 50
        while True:
            time.sleep(5.2)                        # the site allows one request every five seconds
            d = _get(f"https://opentdb.com/api.php?amount={amount}&encode=url3986&token={token}")
            if d.get("response_code") == 5:
                time.sleep(6)
                continue
            if d.get("response_code") in (1, 4):   # fewer left than asked for: ask for less, then stop
                if amount == 1:
                    return
                amount = max(1, amount // 5)
                continue
            if d.get("response_code") != 0:
                raise RuntimeError(f"the site answered with code {d.get('response_code')}")
            for x in d["results"]:
                job["got"] += add("opentdb", {"easy": 1, "medium": 2, "hard": 3}.get(x.get("difficulty"), 2), un(x.get("category", "")),
                                  un(x["question"]), un(x["correct_answer"]), con=con)
            con.commit()
    finally:
        con.commit()
        con.close()


def _triviaapi(job: dict):
    con = _db()
    try:
        for diff, level in (("easy", 1), ("medium", 2), ("hard", 3)):
            dry = 0
            for _ in range(60):
                new = 0
                for x in _get(f"https://the-trivia-api.com/v2/questions?limit=50&difficulties={diff}"):
                    lvl = 4 if x.get("isNiche") and level == 3 else level
                    new += add("triviaapi", lvl, str(x.get("category", "")).replace("_", " "), (x.get("question") or {}).get("text", ""),
                               x.get("correctAnswer", ""), ext=str(x.get("id") or ""), con=con)
                con.commit()
                job["got"] += new
                dry = dry + 1 if new < 3 else 0
                if dry >= 3:               # the site gives random ones: three rounds of repeats means that was all
                    break
                time.sleep(1.0)
    finally:
        con.commit()
        con.close()


def jeopardy_level4(row: dict) -> bool:
    """The hard end of the board: Final Jeopardy, and the two top rows of Double Jeopardy."""
    try:
        rnd, value = int(row.get("round") or 0), int(row.get("clue_value") or 0)
    except ValueError:
        return False
    if rnd == 3:
        return True
    top = 800 if str(row.get("air_date", "")) < "2001-11-26" else 1600     # the values doubled that day
    return rnd == 2 and value >= top


def _jeopardy(job: dict):
    files = _get("https://api.github.com/repos/jwolle1/jeopardy_clue_dataset/contents")
    name = next((f["download_url"] for f in files if f.get("name", "").startswith("combined_season") and f["name"].endswith(".tsv")), None)
    if not name:
        raise RuntimeError("the clue file was not found in the collection")
    with tempfile.NamedTemporaryFile(suffix=".tsv") as tmp:
        with urllib.request.urlopen(urllib.request.Request(name, headers={"User-Agent": "AgentHub"}), timeout=120) as r:
            while chunk := r.read(1 << 20):
                tmp.write(chunk)
        tmp.flush()
        con = _db()
        try:
            with open(tmp.name, encoding="utf-8", errors="replace", newline="") as f:
                for row in csv.DictReader(f, delimiter="\t", quoting=csv.QUOTE_NONE):
                    clue, reply = row.get("answer") or "", row.get("question") or ""
                    # a clue that shows or plays something cannot be asked aloud
                    if not jeopardy_level4(row) or any(w in clue.lower() for w in ("seen here", "heard here", "pictured", "this clip")):
                        continue
                    job["got"] += add("jeopardy", 4, (row.get("category") or "").title(), clue, reply, con=con)
        finally:
            con.commit()
            con.close()


def download(src: str) -> str | None:
    """Start filling the bank from one source, in the background. -> an error, or None."""
    fn = {"opentdb": _opentdb, "triviaapi": _triviaapi, "jeopardy": _jeopardy}.get(src)
    if not fn:
        return "no such source"
    with _lock:
        if (jobs.get(src) or {}).get("state") == "running":
            return "already downloading"
        job = jobs[src] = {"state": "running", "got": 0, "error": ""}

    def run():
        try:
            fn(job)
            job["state"] = "done"
        except Exception as exc:
            job.update(state="failed", error=str(exc)[:200] or type(exc).__name__)
            log.warning("trivia %s: %s", src, job["error"])
    threading.Thread(target=run, daemon=True, name=f"trivia-{src}").start()
    return None
