"""python test_tags.py  (no GPU needed)"""
from tags import dropped, plan, translate

T = lambda s: translate(s, False)[0]

# working tags pass through, any case
assert T("Well, [chuckle] okay.") == "Well, [chuckle] okay."
assert T("[ANGRY] Stop that!") == "[angry] Stop that!"
# free-form sound tags map onto working ones; soft laughs become a chuckle
assert T("Oh [laughs softly] you did.") == "Oh [chuckle] you did."
assert T("Oh [nervous laugh] you did.") == "Oh [chuckle] you did."
assert T("Ha! [laughing] Really?") == "Ha! [laugh] Really?"
assert T("[gasps] No way!") == "[gasp] No way!"
assert T("[shouting] Get out!") == "[angry] Get out!"
# pauses become an ellipsis
assert T("Wait [long pause] what?") == "Wait … what?"
# tags the model cannot do are removed, never spoken, and counted
dropped.clear()
assert T("[whispers] Be quiet, please.") == "Be quiet, please."
assert T("I tried. [sighs heavily] Twice.") == "I tried. Twice."
assert dropped == {"[whispers]": 1, "[sighs heavily]": 1}
# emotion tags are removed and steer temperature instead of being read
assert translate("[sad] I miss them.", False) == ("I miss them.", True)
assert translate("[happy] Great news!", True) == ("Great news!", False)
assert dropped == {"[whispers]": 1, "[sighs heavily]": 1}
# things that are not tags stay
assert T("See note [1] and [this is a long bracketed aside that is clearly not a stage direction].").startswith("See note [1] and [this is")

# plan: mood sticks until changed, sentences pack by temperature, empty pieces vanish
p = plan("Hi there. [crying] I miss them so much. I don't know what to do. [calm] Anyway, dinner is at six.", 0.8, 1.1, 300)
assert p == [("Hi there.", 0.8), ("I miss them so much. I don't know what to do.", 1.1), ("Anyway, dinner is at six.", 0.8)], p
assert plan("[whispers]", 0.8, 1.1, 300) == []
assert plan("One. Two. Three.", 0.8, 1.1, 9) == [("One. Two.", 0.8), ("Three.", 0.8)]
# translator off: text untouched, still split
assert plan("[whispers] Hello. Bye.", 0.8, 1.1, 300, enabled=False) == [("[whispers] Hello. Bye.", 0.8)]
print("tags OK")
