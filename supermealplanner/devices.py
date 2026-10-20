"""Pairing a phone with this household, and the bearer token it keeps afterwards.

The app has no password to type. Someone in the household asks the bot for a code in a PRIVATE chat,
types the six digits into the app once, and the app gets a long-lived token it stores in the phone's
keychain. The token is bound to the person who asked, so the app knows whose plate to show.

What this module guarantees:

  * **A code is one use and short-lived.** `take_code` spends it inside the same transaction that
    reads it, so a replay finds nothing even if two phones send it at the same moment.
  * **Nothing here can be read back.** The database keeps sha256 of the code and sha256 of the token;
    the readable values exist only in the reply that creates them. A leaked database cannot pair.
  * **Guessing is bounded.** Six digits is 10^6, so the window matters: `Guard` refuses the whole
    endpoint for a while once a handful of wrong codes arrive, counted per source and in total.
  * **Revoking is immediate.** `device_for` only ever returns a row with `revoked = 0`, and every
    request goes through it, so a revoked phone stops at the next call it makes.

The bot side (`/vincular`) and the HTTP side (`POST /api/v1/pair`) both live on top of these
functions; neither of them hashes or counts anything itself.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import secrets
import time

CODE_DIGITS = 6
CODE_TTL_S = 600.0  # ten minutes: long enough to walk to the phone, short enough to not sit around
NAME_MAX = 60
# The guard: how many wrong codes may arrive before pairing closes, and for how long.
FAILS_BEFORE_BLOCK = 8
BLOCK_S = 900.0
WINDOW_S = 600.0
TOUCH_S = 300.0  # last_seen is written at most this often per device, not on every request


def hash_secret(value: str) -> str:
    """sha256 of a code or a token. The only form either of them takes on disk."""
    return hashlib.sha256(value.strip().encode("utf-8")).hexdigest()


def new_code() -> str:
    """A fresh pairing code, as digits. `secrets` because this one guards the household's data."""
    return f"{secrets.randbelow(10**CODE_DIGITS):0{CODE_DIGITS}d}"


def spaced(code: str) -> str:
    """418203 -> '418 203', which is how it is read out and typed in."""
    half = len(code) // 2
    return f"{code[:half]} {code[half:]}"


def normalise_code(raw: str) -> str:
    """What the app sends back: digits only, so spaces, dashes and a pasted '418 203' all work."""
    return "".join(ch for ch in str(raw) if ch.isdigit())


def clean_name(raw: str | None) -> str:
    """A device name is shown to the household, so it keeps no control characters and no surprises."""
    text = " ".join(str(raw or "").split())
    text = "".join(ch for ch in text if ch.isprintable())
    return text[:NAME_MAX] or "Phone"


def make_code(db, person: str | None, now: dt.datetime | None = None) -> tuple[str, float]:
    """Issue a code for `person`. Returns the code to show and its life in seconds.

    The readable code is returned, never stored: only its hash reaches the database.
    """
    now = now or dt.datetime.now(dt.UTC)
    code = new_code()
    db.add_pair_code(hash_secret(code), person, now, CODE_TTL_S)
    return code, CODE_TTL_S


def take_code(db, raw: str, now: dt.datetime | None = None) -> dict | None:
    """Spend a code. Returns its row (person, created) or None when it is wrong, spent or expired."""
    now = now or dt.datetime.now(dt.UTC)
    code = normalise_code(raw)
    if len(code) != CODE_DIGITS:
        return None
    return db.take_pair_code(hash_secret(code), now)


def pair(db, raw_code: str, device_name: str | None, now: dt.datetime | None = None) -> dict | None:
    """Turn a code into a device and its token. Returns None when the code does not open anything.

    The token is in the answer and nowhere else: the caller must hand it to the app and forget it.
    """
    now = now or dt.datetime.now(dt.UTC)
    row = take_code(db, raw_code, now)
    if row is None:
        return None
    token = secrets.token_urlsafe(32)
    name = clean_name(device_name)
    device_id = db.add_device(name, row.get("person"), hash_secret(token), now)
    return {"token": token, "device_id": device_id, "name": name, "person": row.get("person")}


def device_for(db, token: str | None, now: dt.datetime | None = None) -> dict | None:
    """The live device a bearer belongs to, with `last_seen` kept roughly fresh. None when the token
    is unknown, malformed or revoked."""
    if not token:
        return None
    row = db.device_by_token(hash_secret(token))
    if row is None:
        return None
    now = now or dt.datetime.now(dt.UTC)
    seen = row.get("last_seen")
    stale = True
    if seen:
        try:
            stale = (now - dt.datetime.fromisoformat(seen)).total_seconds() > TOUCH_S
        except ValueError:
            stale = True
    if stale:
        db.touch_device(int(row["id"]), now)
    return row


class Guard:
    """Rate limit for wrong pairing codes, in memory: a bot that restarts starts it again, which is
    fine because a code only lives ten minutes anyway.

    Counted per source AND in total, so neither one phone nor a spread of them can walk the space.
    """

    def __init__(
        self, fails: int = FAILS_BEFORE_BLOCK, block_s: float = BLOCK_S, window_s: float = WINDOW_S
    ):
        self.fails, self.block_s, self.window_s = fails, block_s, window_s
        self._hits: dict[str, list[float]] = {}
        self._until: dict[str, float] = {}

    def blocked(self, source: str, now: float | None = None) -> float:
        """Seconds still to wait for `source`, 0 when it may try. '*' is every source together."""
        now = time.time() if now is None else now
        return max(0.0, max(self._until.get(source, 0.0), self._until.get("*", 0.0)) - now)

    def fail(self, source: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        for key in (source, "*"):
            hits = [t for t in self._hits.get(key, []) if now - t < self.window_s]
            hits.append(now)
            self._hits[key] = hits
            limit = self.fails if key == source else self.fails * 3
            if len(hits) >= limit:
                self._until[key] = now + self.block_s
                self._hits[key] = []

    def ok(self, source: str, now: float | None = None) -> None:
        """A code that worked clears that source's record, so a typo costs nothing later."""
        now = time.time() if now is None else now
        self._hits.pop(source, None)
        self._until.pop(source, None)
