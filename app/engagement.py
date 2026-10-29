"""Sticky conversation logic. Pure Python: no Discord imports.

A trigger "engages" an author in a channel; while engaged, their messages get
replies without a trigger until a disengage rule fires.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable

Key = tuple[int, int]  # (channel_id, author_id)

# Outcome actions
REPLY = "reply"
IGNORE = "ignore"
SIGNOFF = "signoff"  # send one short goodbye, already disengaged


def _norm(text: str) -> str:
    return text.replace("’", "'").replace("‘", "'").lower()


def compile_phrases(phrases: Iterable[str]) -> re.Pattern | None:
    """Case-insensitive whole word/phrase matcher. None if no phrases."""
    phrases = [_norm(p.strip()) for p in phrases if p and p.strip()]
    if not phrases:
        return None
    alts = "|".join(re.escape(p) for p in sorted(phrases, key=len, reverse=True))
    return re.compile(rf"(?<!\w)(?:{alts})(?!\w)")


def phrase_match(pattern: re.Pattern | None, text: str) -> str | None:
    if pattern is None:
        return None
    m = pattern.search(_norm(text))
    return m.group(0) if m else None


def addressed_by_name(text: str, names: Iterable[str]) -> str | None:
    """Return the name if `text` starts with it followed by , : or whitespace."""
    t = _norm(text).lstrip()
    for name in sorted({_norm(n).strip() for n in names if n}, key=len, reverse=True):
        if len(name) < 2:
            continue
        if t.startswith(name) and (len(t) == len(name) or t[len(name)] in ",:" or t[len(name)].isspace()):
            return name
    return None


@dataclass
class EngageSettings:
    sticky: bool = True
    timeout_seconds: float = 45
    disengage: re.Pattern | None = None
    signoff: re.Pattern | None = None


@dataclass
class MessageInfo:
    """What the engagement logic needs to know about one message."""
    text: str
    trigger_reason: str | None      # "trigger word", "@mention", "reply to bot", or None
    mentions_other: bool = False    # @mentions a member other than the bot/author
    replies_to_other: bool = False  # Discord-reply to someone other than the bot/author
    other_names: Callable[[], Iterable[str]] = field(default=lambda: ())  # lazy: other members' names


@dataclass
class Outcome:
    action: str                     # REPLY / IGNORE / SIGNOFF
    event: str | None = None        # "engaged" / "re-engaged" / "disengaged" / None
    reason: str = ""


class EngagementTracker:
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._engaged: dict[Key, tuple[float, float]] = {}  # key -> (last_activity, timeout)

    def is_engaged(self, key: Key) -> bool:
        entry = self._engaged.get(key)
        return entry is not None and self._clock() - entry[0] < entry[1]

    def engaged_keys(self) -> list[Key]:
        return list(self._engaged)

    def engage(self, key: Key, timeout: float) -> None:
        self._engaged[key] = (self._clock(), timeout)

    def touch(self, key: Key) -> None:
        """Refresh activity (user message or bot reply). No-op if not engaged."""
        entry = self._engaged.get(key)
        if entry is not None:
            self._engaged[key] = (self._clock(), entry[1])

    def disengage(self, key: Key) -> bool:
        return self._engaged.pop(key, None) is not None

    def clear(self) -> list[Key]:
        keys = list(self._engaged)
        self._engaged.clear()
        return keys

    def expire(self) -> list[Key]:
        """Drop and return engagements past their timeout."""
        now = self._clock()
        gone = [k for k, (t, to) in self._engaged.items() if now - t >= to]
        for k in gone:
            del self._engaged[k]
        return gone

    def evaluate(self, key: Key, msg: MessageInfo, s: EngageSettings) -> Outcome:
        """Decide what to do with a message in a `trigger`-mode channel."""
        timed_out = key in self._engaged and not self.is_engaged(key)
        if timed_out:
            del self._engaged[key]
        engaged = key in self._engaged

        if not s.sticky:
            return Outcome(REPLY, reason=msg.trigger_reason) if msg.trigger_reason else Outcome(IGNORE)

        # Sign-off wins over a trigger: sign-off phrases usually contain the
        # bot's name, which is often a trigger word too.
        signoff = phrase_match(s.signoff, msg.text)
        if signoff and (engaged or msg.trigger_reason):
            self.disengage(key)
            return Outcome(SIGNOFF, "disengaged", f"sign-off phrase '{signoff}'")

        if msg.trigger_reason:
            self.engage(key, s.timeout_seconds)
            return Outcome(REPLY, "re-engaged" if engaged else "engaged", msg.trigger_reason)

        if not engaged:
            return Outcome(IGNORE, "disengaged" if timed_out else None,
                           f"timeout ({s.timeout_seconds:g}s silence)" if timed_out else "")

        reason = None
        if msg.mentions_other:
            reason = "@mentioned someone else"
        elif msg.replies_to_other:
            reason = "replied to someone else"
        elif (phrase := phrase_match(s.disengage, msg.text)):
            reason = f"disengage phrase '{phrase}'"
        elif (name := addressed_by_name(msg.text, msg.other_names())):
            reason = f"addressed '{name}' by name"
        if reason:
            self.disengage(key)
            return Outcome(IGNORE, "disengaged", reason)

        self.touch(key)
        return Outcome(REPLY, reason="engaged conversation")
