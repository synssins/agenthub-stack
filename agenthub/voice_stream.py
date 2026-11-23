"""A device's microphone as a live stream: where speech starts and ends is decided here, not on the device.

The device sends its microphone as it hears it (/api/device/stream: 16 kHz, 16-bit, mono). A Segmenter
cuts that into stretches of speech, the way the devices did themselves:

  start   a tenth of a second counts as speech starting when it is ONSET_DB over the room's own level,
          ONSET_WINDOWS times in a row. PRE of audio from before that is kept: a first syllable is quiet.
  end     the speaker is near the device and the room is not, so the speech is over once the level has
          fallen well below the speaker's own (OFF of the loudest tenth) and stayed there for QUIET tenths.
  soon    SOON tenths into that pause the speech has probably ended: transcribing can start now, and its
          text is ready when the pause is over. Speech that goes on makes that text stale.

Nothing here keeps audio: a stretch is handed on and forgotten.
"""

from __future__ import annotations

import io
import math
import wave
from array import array
from collections import deque

RATE = 16000
WINDOW = RATE // 10 * 2     # bytes in a tenth of a second
PRE = 5                     # tenths kept from before speech was noticed
ONSET_DB = 12.0
ONSET_WINDOWS = 2
LOWEST_DB = -55.0           # quieter than this is never speech, however silent the room
ON, OFF = 0.5, 0.3          # fractions of the loudest tenth: the speaker talking, the speaker stopped
SPEECH = 3                  # tenths of speaking before anything can end
QUIET = 6                   # tenths of pause that end it, unless the device asks for its own
SOON = 2                    # tenths of pause after which the end is likely
NOISE = 15                  # a loud start with no speech after it (a door, a cough) is dropped after this many tenths
LONGEST = 20 * RATE * 2     # bytes: one stretch is cut here
TAIL = 2                    # tenths kept after the last word


def _rms(window: bytes) -> float:
    a = array("h")
    a.frombytes(window)
    return math.sqrt(sum(v * v for v in a) / len(a))


def _db(rms: float) -> float:
    return 20 * math.log10(rms / 32768) if rms > 0 else -120.0


def wav(pcm: bytes) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm)
    return out.getvalue()


class Segmenter:
    def __init__(self, quiet: int = QUIET):
        self.end_after = max(SOON + 1, min(int(quiet), 20))
        self.rest = b""                         # audio short of a whole tenth
        self.levels: deque = deque(maxlen=50)   # the room's last quiet tenths, in dB
        self.reset()

    def reset(self):
        """Drop what is being collected: the device made a sound of its own."""
        self.rest = b""
        self.pre: deque = deque(maxlen=PRE)
        self.loud = 0
        self.seg: bytearray | None = None
        self.peak = self.speech = self.quiet = self.last = 0

    @property
    def active(self) -> bool:
        return self.seg is not None

    def so_far(self) -> bytes:
        return bytes(self.seg or b"")

    def _floor(self) -> float:
        return sorted(self.levels)[len(self.levels) // 5] if self.levels else -60.0

    def _cut(self) -> bytes:
        return bytes(self.seg[:min(len(self.seg), self.last + TAIL * WINDOW)])

    def feed(self, pcm: bytes) -> list[tuple]:
        """Audio as it arrives, in any size. -> what happened: ("start",), ("soon", speech so far),
        ("end", the speech), ("drop",) for a noise that was not speech."""
        out: list[tuple] = []
        data = self.rest + pcm
        whole = len(data) - len(data) % WINDOW
        self.rest = data[whole:]
        for i in range(0, whole, WINDOW):
            w = data[i:i + WINDOW]
            rms = _rms(w)
            if self.seg is None:
                self.pre.append(w)
                db = _db(rms)
                if db > max(self._floor() + ONSET_DB, LOWEST_DB):
                    self.loud += 1
                else:
                    self.loud = 0
                    self.levels.append(db)
                if self.loud >= ONSET_WINDOWS:
                    self.seg = bytearray()
                    levels = [_rms(p) for p in self.pre]
                    self.peak = max(levels)   # the quiet before the first word is measured against the word
                    for p, level in zip(self.pre, levels):
                        self._window(p, level)
                    self.pre.clear()
                    self.loud = 0
                    out.append(("start",))
                continue
            self._window(w, rms)
            if self.speech >= SPEECH and self.quiet == SOON:
                out.append(("soon", self._cut()))
            if (self.speech >= SPEECH and self.quiet >= self.end_after) or len(self.seg) >= LONGEST:
                out.append(("end", self._cut()))
                self.seg, self.peak, self.speech, self.quiet, self.last = None, 0, 0, 0, 0
            elif self.speech < SPEECH and self.quiet >= NOISE:
                out.append(("drop",))
                self.seg, self.peak, self.speech, self.quiet, self.last = None, 0, 0, 0, 0
        return out

    def _window(self, w: bytes, rms: float):
        self.seg += w
        self.peak = max(self.peak, rms)
        if rms >= ON * self.peak:
            self.speech += 1
        if rms < OFF * self.peak:
            self.quiet += 1
        else:
            self.quiet, self.last = 0, len(self.seg)
