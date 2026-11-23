"""Who answers a message that names nobody with @.

  named()  a plain name in the text ("what do you think, Quill?") addresses that agent
  pick()   a model reads the participants and the latest messages and chooses one

Both are off by default (Settings > Chat). app.py falls back to a random participant when
the picking model fails, so a message is never left unanswered because of it.
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

PICK_PROMPT = ("You route messages in a group chat. Choose the one participant best suited to answer the "
               "latest message, by role, expertise and what the conversation is about. Reply with that "
               "participant's handle only, nothing else.")
PICK_TIMEOUT = 45


def _word(name: str) -> str:
    return rf"(?<![\w@-]){re.escape(name)}(?![\w-])"


def named(text: str, candidates: dict[str, str]) -> list[str]:
    """Agents whose handle or label appears in the text as a whole word. candidates: {handle: label}."""
    return [n for n, label in candidates.items()
            if any(len(k) > 1 and re.search(_word(k), text, re.I) for k in {n, label or n})]


def pick(spec: dict, candidates: dict[str, str], recent: list[str], text: str, call=None) -> str | None:
    """Ask spec's model which candidate should answer. candidates: {handle: what it does}.
    -> a handle, or None when the model failed or named nobody."""
    import urllib.error
    if call is None:
        import agent_runner
        call = agent_runner.call_api_message
    who = "\n".join(f"@{n}: {about or 'no description'}" for n, about in candidates.items())
    prompt = (f"Participants:\n{who}\n\nRecent messages:\n" + ("\n".join(recent) or "(none)")
              + f"\n\nLatest message:\n{text}\n\nHandle of the participant who should answer:")
    spec = dict(spec, timeout=PICK_TIMEOUT)
    try:
        try:  # no thinking: this is a one-word answer
            reply = call(spec, PICK_PROMPT, [{"role": "user", "content": prompt}], effort="none")
        except urllib.error.HTTPError as exc:
            if exc.code not in (400, 422):
                raise
            reply = call(spec, PICK_PROMPT, [{"role": "user", "content": prompt}])
    except Exception as exc:
        log.warning("responder pick failed (%s): %s", spec.get("name"), str(exc)[:200])
        return None
    answer = re.sub(r"<think>.*?(</think>|$)", "", reply.get("content") or "", flags=re.S)
    hits = [(m.start(), n) for n in candidates for m in [re.search(_word(n), answer.replace("@", " "), re.I)] if m]
    return min(hits)[1] if hits else None
