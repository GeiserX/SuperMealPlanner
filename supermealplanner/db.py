"""SQLite store for what people log: weight, waist, the daily line, calorie target changes, and the
one-off reminders the chat sets, the dish ratings and the shared shopping ticks."""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS weight (
  person TEXT NOT NULL, day TEXT NOT NULL, kg REAL NOT NULL, source TEXT DEFAULT 'app',
  PRIMARY KEY (person, day)
);
CREATE TABLE IF NOT EXISTS waist (
  person TEXT NOT NULL, day TEXT NOT NULL, cm REAL NOT NULL,
  PRIMARY KEY (person, day)
);
CREATE TABLE IF NOT EXISTS daily (
  person TEXT NOT NULL, day TEXT NOT NULL,
  hunger INTEGER, craving INTEGER, nausea INTEGER,
  binge INTEGER, stool INTEGER, pills_lunch INTEGER, note TEXT, water INTEGER,
  PRIMARY KEY (person, day)
);
CREATE TABLE IF NOT EXISTS sent (
  reminder TEXT NOT NULL, day TEXT NOT NULL, at TEXT NOT NULL,
  PRIMARY KEY (reminder, day, at)
);
CREATE TABLE IF NOT EXISTS chat (
  id INTEGER PRIMARY KEY AUTOINCREMENT, chat INTEGER NOT NULL, ts TEXT NOT NULL,
  role TEXT NOT NULL, who TEXT, text TEXT NOT NULL, mid INTEGER, asked INTEGER
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS baseline (
  person TEXT PRIMARY KEY, kcal REAL NOT NULL, day TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS target (
  person TEXT NOT NULL, day TEXT NOT NULL, kcal REAL NOT NULL, reason TEXT NOT NULL,
  PRIMARY KEY (person, day)
);
CREATE TABLE IF NOT EXISTS reminder (
  id INTEGER PRIMARY KEY AUTOINCREMENT, chat INTEGER NOT NULL, person TEXT, at REAL NOT NULL,
  text TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending', by INTEGER, created TEXT NOT NULL,
  recipe TEXT, date TEXT
);
CREATE TABLE IF NOT EXISTS rating (
  recipe TEXT NOT NULL, person TEXT NOT NULL, vote INTEGER NOT NULL, day TEXT NOT NULL,
  PRIMARY KEY (recipe, person)
);
-- What came into the pantry: a tick on a week's list (week set; unticking deletes it) or the amount
-- the chat said is at home (source 'set', which replaces what was there). pantry.py works out what
-- is left from these, the plan and each ingredient's keeps_days; nothing else is stored.
CREATE TABLE IF NOT EXISTS pantry (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ingredient TEXT NOT NULL, qty REAL NOT NULL,
  at TEXT NOT NULL, source TEXT NOT NULL, week TEXT
);
-- A phone that has been paired. `token_hash` is sha256 of the bearer the app keeps in its keychain:
-- the token itself is shown once, at pairing, and is never stored here or logged.
CREATE TABLE IF NOT EXISTS device (
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, person TEXT,
  token_hash TEXT NOT NULL UNIQUE, created TEXT NOT NULL, last_seen TEXT, revoked INTEGER DEFAULT 0
);
-- A pairing code the bot gave someone in a private chat: six digits, one use, minutes to live.
CREATE TABLE IF NOT EXISTS pair_code (
  code_hash TEXT PRIMARY KEY, person TEXT, created TEXT NOT NULL, expires REAL NOT NULL,
  used INTEGER DEFAULT 0
);
"""

HAVE = "shop.have"  # ingredient ids the household has at home: left off every list until re-added
TICKED = "shop.ticked"  # {monday: [ingredient ids]} ticked that week, in tick order, on every phone
TICKED_AT = "shop.ticked_at"  # {monday: {ingredient id: when it was ticked}}, beside TICKED
TICKED_WEEKS = 8
PANTRY_DAYS = 120  # pantry entries older than this are forgotten


class DB:
    def __init__(self, path: Path):
        self.path = path
        with self.conn() as c:
            c.executescript(SCHEMA)
            cols = {r["name"] for r in c.execute("PRAGMA table_info(daily)")}
            if "water" not in cols:  # databases from 0.1
                c.execute("ALTER TABLE daily ADD COLUMN water INTEGER")
            cols = {r["name"] for r in c.execute("PRAGMA table_info(chat)")}
            for col in ("mid", "asked"):  # databases before 0.6: the message id, addressed or not
                if col not in cols:
                    c.execute(f"ALTER TABLE chat ADD COLUMN {col} INTEGER")
            cols = {r["name"] for r in c.execute("PRAGMA table_info(reminder)")}
            for col in ("recipe", "date"):  # before 0.25: a reminder about a dish and its day
                if col not in cols:
                    c.execute(f"ALTER TABLE reminder ADD COLUMN {col} TEXT")

    @contextmanager
    def conn(self):
        c = sqlite3.connect(self.path)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        finally:
            c.close()

    # weight
    def add_weight(self, person: str, day: dt.date, kg: float, source: str = "app") -> None:
        if not 25 <= kg <= 300:
            raise ValueError(f"{kg} kg is not a plausible weight")
        with self.conn() as c:
            c.execute(
                "INSERT INTO weight(person, day, kg, source) VALUES (?,?,?,?) "
                "ON CONFLICT(person, day) DO UPDATE SET kg=excluded.kg, source=excluded.source",
                (person, day.isoformat(), kg, source),
            )

    def weights(self, person: str) -> list[tuple[dt.date, float]]:
        with self.conn() as c:
            rows = c.execute(
                "SELECT day, kg FROM weight WHERE person=? ORDER BY day", (person,)
            ).fetchall()
        return [(dt.date.fromisoformat(r["day"]), r["kg"]) for r in rows]

    # waist
    def add_waist(self, person: str, day: dt.date, cm: float) -> None:
        if not 40 <= cm <= 200:
            raise ValueError(f"{cm} cm is not a plausible waist")
        with self.conn() as c:
            c.execute(
                "INSERT INTO waist(person, day, cm) VALUES (?,?,?) "
                "ON CONFLICT(person, day) DO UPDATE SET cm=excluded.cm",
                (person, day.isoformat(), cm),
            )

    def waists(self, person: str) -> list[tuple[dt.date, float]]:
        with self.conn() as c:
            rows = c.execute(
                "SELECT day, cm FROM waist WHERE person=? ORDER BY day", (person,)
            ).fetchall()
        return [(dt.date.fromisoformat(r["day"]), r["cm"]) for r in rows]

    # daily line
    def set_daily(self, person: str, day: dt.date, **fields) -> None:
        allowed = {"hunger", "craving", "nausea", "binge", "stool", "pills_lunch", "note", "water"}
        fields = {k: v for k, v in fields.items() if k in allowed}
        if not fields:
            return
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        updates = ", ".join(f"{k}=excluded.{k}" for k in fields)
        with self.conn() as c:
            c.execute(
                f"INSERT INTO daily(person, day, {cols}) VALUES (?, ?, {marks}) "
                f"ON CONFLICT(person, day) DO UPDATE SET {updates}",
                (person, day.isoformat(), *fields.values()),
            )

    def daily(self, person: str, since: dt.date | None = None) -> list[dict]:
        q = "SELECT * FROM daily WHERE person=?"
        args: list = [person]
        if since:
            q += " AND day>=?"
            args.append(since.isoformat())
        with self.conn() as c:
            rows = c.execute(q + " ORDER BY day", args).fetchall()
        return [dict(r) for r in rows]

    # calorie targets
    def set_target(self, person: str, day: dt.date, kcal: float, reason: str) -> None:
        with self.conn() as c:
            c.execute(
                "INSERT INTO target(person, day, kcal, reason) VALUES (?,?,?,?) "
                "ON CONFLICT(person, day) DO UPDATE SET kcal=excluded.kcal, reason=excluded.reason",
                (person, day.isoformat(), kcal, reason),
            )

    def targets(self, person: str) -> list[dict]:
        with self.conn() as c:
            rows = c.execute(
                "SELECT day, kcal, reason FROM target WHERE person=? ORDER BY day", (person,)
            ).fetchall()
        return [
            {"day": dt.date.fromisoformat(r["day"]), "kcal": r["kcal"], "reason": r["reason"]}
            for r in rows
        ]

    # reminders: each (reminder, day, time) is sent once
    def was_sent(self, reminder: str, day: dt.date, at: str) -> bool:
        with self.conn() as c:
            row = c.execute(
                "SELECT 1 FROM sent WHERE reminder=? AND day=? AND at=?",
                (reminder, day.isoformat(), at),
            ).fetchone()
        return row is not None

    def mark_sent(self, reminder: str, day: dt.date, at: str) -> bool:
        """Record a send; False when it was already sent."""
        with self.conn() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO sent(reminder, day, at) VALUES (?,?,?)",
                (reminder, day.isoformat(), at),
            )
            return cur.rowcount == 1

    # bot conversation, for the AI's short memory
    def add_chat(
        self,
        chat: int,
        role: str,
        who: str | None,
        text: str,
        keep: int = 1000,
        mid: int | None = None,
        asked: bool | None = None,
    ) -> int:
        """Store a message and return its row id. mid: its Telegram message id, so an edit finds
        it; asked: whether it called the bot (None for the bot's own messages)."""
        with self.conn() as c:
            cur = c.execute(
                "INSERT INTO chat(chat, ts, role, who, text, mid, asked) VALUES (?,?,?,?,?,?,?)",
                (
                    chat,
                    dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
                    role,
                    who,
                    text[:4000],
                    mid,
                    None if asked is None else int(asked),
                ),
            )
            c.execute(
                "DELETE FROM chat WHERE chat=? AND id NOT IN "
                "(SELECT id FROM chat WHERE chat=? ORDER BY id DESC LIMIT ?)",
                (chat, chat, keep),
            )
            return cur.lastrowid

    def chat_rows(self, chat: int, n: int, days: int = 0) -> list[dict]:
        """The last n messages of a chat, oldest first, with their row id; with days, also every
        message of those last days."""
        since = (dt.datetime.now(dt.UTC) - dt.timedelta(days=days)).isoformat(timespec="seconds")
        with self.conn() as c:
            rows = c.execute(
                "SELECT id, ts, role, who, text, mid, asked FROM chat WHERE chat=? AND (id IN "
                "(SELECT id FROM chat WHERE chat=? ORDER BY id DESC LIMIT ?) OR (? AND ts >= ?)) "
                "ORDER BY id",
                (chat, chat, n, int(days > 0), since),
            ).fetchall()
        return [dict(r) for r in rows]

    def chat_by_ids(self, chat: int, ids: list[int]) -> list[dict]:
        """These messages of a chat, oldest first."""
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        with self.conn() as c:
            rows = c.execute(
                f"SELECT id, ts, role, who, text, mid, asked FROM chat WHERE chat=? AND id IN "
                f"({marks}) ORDER BY id",
                (chat, *ids),
            ).fetchall()
        return [dict(r) for r in rows]

    def find_chat(self, chat: int, mid: int) -> dict | None:
        """The stored message with this Telegram message id, or None."""
        with self.conn() as c:
            r = c.execute(
                "SELECT id, ts, role, who, text, mid, asked FROM chat WHERE chat=? AND mid=? "
                "ORDER BY id DESC LIMIT 1",
                (chat, mid),
            ).fetchone()
        return dict(r) if r else None

    def answer_after(self, chat: int, row: int) -> str | None:
        """The first answer of the AI after this message, for an edit that questions it."""
        with self.conn() as c:
            r = c.execute(
                "SELECT text FROM chat WHERE chat=? AND id>? AND role='assistant' AND who='ai' "
                "ORDER BY id LIMIT 1",
                (chat, row),
            ).fetchone()
        return r["text"] if r else None

    def edit_chat(self, row: int, text: str, asked: bool | None = None) -> None:
        """An edited message keeps its row: later turns and recaps see its final wording."""
        with self.conn() as c:
            if asked is None:
                c.execute("UPDATE chat SET text=? WHERE id=?", (text[:4000], row))
            else:
                c.execute(
                    "UPDATE chat SET text=?, asked=? WHERE id=?", (text[:4000], int(asked), row)
                )

    def delete_chat(self, row: int) -> None:
        with self.conn() as c:
            c.execute("DELETE FROM chat WHERE id=?", (row,))

    def clear_chat(self, chat: int) -> None:
        with self.conn() as c:
            c.execute("DELETE FROM chat WHERE chat=?", (chat,))

    def recent_chat(self, chat: int, n: int) -> list[dict]:
        with self.conn() as c:
            rows = c.execute(
                "SELECT role, who, text FROM chat WHERE chat=? ORDER BY id DESC LIMIT ?", (chat, n)
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def bump_water(self, person: str, day: dt.date, by: int = 1) -> int:
        rows = self.daily(person, day)
        n = ((rows[0].get("water") or 0) if rows and rows[0]["day"] == day.isoformat() else 0) + by
        self.set_daily(person, day, water=max(0, n))
        return max(0, n)

    # the kcal.start a person's adjustments are based on
    def baseline(self, person: str) -> tuple[float, dt.date] | None:
        with self.conn() as c:
            r = c.execute("SELECT kcal, day FROM baseline WHERE person=?", (person,)).fetchone()
        return (r["kcal"], dt.date.fromisoformat(r["day"])) if r else None

    def set_baseline(self, person: str, kcal: float, day: dt.date) -> None:
        with self.conn() as c:
            c.execute(
                "INSERT INTO baseline(person, kcal, day) VALUES (?,?,?) "
                "ON CONFLICT(person) DO UPDATE SET kcal=excluded.kcal, day=excluded.day",
                (person, kcal, day.isoformat()),
            )

    # one-off reminders: posted in their chat at `at` (epoch seconds), then marked sent. recipe and
    # date: the dish it is about and the day it is eaten, so the app cancels it when the menu
    # drops that dish
    def add_reminder(
        self,
        chat: int,
        person: str | None,
        at: float,
        text: str,
        by: int | None = None,
        recipe: str | None = None,
        date: str | None = None,
    ) -> int:
        with self.conn() as c:
            cur = c.execute(
                "INSERT INTO reminder(chat, person, at, text, by, created, recipe, date) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    chat,
                    person,
                    at,
                    text[:500],
                    by,
                    dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
                    recipe if recipe and date else None,
                    date if recipe and date else None,
                ),
            )
            return cur.lastrowid

    def reminders(self, chat: int | None = None, state: str = "pending") -> list[dict]:
        """A chat's reminders in that state (every chat's without one), soonest first."""
        q, args = "SELECT * FROM reminder WHERE state=?", [state]
        if chat is not None:
            q += " AND chat=?"
            args.append(chat)
        with self.conn() as c:
            return [dict(r) for r in c.execute(q + " ORDER BY at, id", args).fetchall()]

    def reminder(self, rid: int) -> dict | None:
        with self.conn() as c:
            r = c.execute("SELECT * FROM reminder WHERE id=?", (rid,)).fetchone()
        return dict(r) if r else None

    def due_reminders(self, now: float) -> list[dict]:
        with self.conn() as c:
            rows = c.execute(
                "SELECT * FROM reminder WHERE state='pending' AND at<=? ORDER BY at, id", (now,)
            ).fetchall()
        return [dict(r) for r in rows]

    def settle_reminder(self, rid: int, state: str) -> bool:
        """Move a pending reminder to `sent` or `cancelled`; False when it was no longer pending,
        so two taps or a tap and the clock settle it once."""
        with self.conn() as c:
            cur = c.execute(
                "UPDATE reminder SET state=? WHERE id=? AND state='pending'", (state, rid)
            )
            return cur.rowcount == 1

    # dish ratings: one vote per person and recipe, +1 or -1; 0 takes the vote back
    def rate(self, recipe: str, person: str, vote: int, day: dt.date) -> None:
        with self.conn() as c:
            if vote == 0:
                c.execute("DELETE FROM rating WHERE recipe=? AND person=?", (recipe, person))
                return
            c.execute(
                "INSERT INTO rating(recipe, person, vote, day) VALUES (?,?,?,?) "
                "ON CONFLICT(recipe, person) DO UPDATE SET vote=excluded.vote, day=excluded.day",
                (recipe, person, 1 if vote > 0 else -1, day.isoformat()),
            )

    def ratings(self, person: str | None = None) -> dict[str, int]:
        """recipe -> the sum of its votes (everyone's), or that person's own vote."""
        q, args = "SELECT recipe, SUM(vote) v FROM rating GROUP BY recipe", []
        if person:
            q, args = "SELECT recipe, vote v FROM rating WHERE person=?", [person]
        with self.conn() as c:
            return {r["recipe"]: int(r["v"]) for r in c.execute(q, args).fetchall()}

    # the shopping list: what is at home, and what each week's list has ticked
    def have(self) -> set[str]:
        return set(self.get_kv(HAVE) or [])

    def set_have(self, ingredient: str, on: bool) -> None:
        have = self.have()
        have = have | {ingredient} if on else have - {ingredient}
        self.set_kv(HAVE, sorted(have))

    def ticked(self, monday: dt.date) -> set[str]:
        return set(self.tick_order(monday))

    def tick_order(self, monday: dt.date) -> list[str]:
        """The week's ticked items, the first ticked first."""
        return list((self.get_kv(TICKED) or {}).get(monday.isoformat(), []))

    def tick_times(self, monday: dt.date) -> dict[str, str]:
        """When each of the week's ticked items was ticked, as ISO 8601 in UTC.

        A tick from before these times were kept has none and is left out.
        """
        times = (self.get_kv(TICKED_AT) or {}).get(monday.isoformat(), {})
        return {i: times[i] for i in self.tick_order(monday) if i in times}

    def tick(
        self, monday: dt.date, ingredient: str, on: bool, at: dt.datetime | None = None
    ) -> None:
        week = monday.isoformat()
        weeks = self.get_kv(TICKED) or {}
        now = [i for i in weeks.get(week, []) if i != ingredient]
        weeks[week] = now + [ingredient] if on else now
        keep = sorted(weeks)[-TICKED_WEEKS:]  # old weeks are never looked at again
        self.set_kv(TICKED, {k: weeks[k] for k in keep})
        times = self.get_kv(TICKED_AT) or {}
        mine = {i: t for i, t in times.get(week, {}).items() if i != ingredient}
        if on:
            when = (at or dt.datetime.now(dt.UTC)).astimezone(dt.UTC)
            mine[ingredient] = when.isoformat(timespec="seconds")
        times[week] = mine
        self.set_kv(TICKED_AT, {k: times[k] for k in keep if k in times})

    # the pantry's entries (pantry.py decides what they mean)
    def add_lot(
        self, ingredient: str, qty: float, at: dt.datetime, source: str, week: dt.date | None = None
    ) -> None:
        """A tick's entry replaces that week's earlier one for the ingredient; entries older than
        PANTRY_DAYS are dropped, so the table never grows."""
        with self.conn() as c:
            if week is not None:
                c.execute(
                    "DELETE FROM pantry WHERE source='tick' AND week=? AND ingredient=?",
                    (week.isoformat(), ingredient),
                )
            c.execute(
                "INSERT INTO pantry(ingredient, qty, at, source, week) VALUES (?,?,?,?,?)",
                (
                    ingredient,
                    qty,
                    at.astimezone(dt.UTC).isoformat(timespec="seconds"),
                    source,
                    week.isoformat() if week else None,
                ),
            )
            old = (at - dt.timedelta(days=PANTRY_DAYS)).astimezone(dt.UTC)
            c.execute("DELETE FROM pantry WHERE at < ?", (old.isoformat(timespec="seconds"),))

    def drop_tick_lot(self, week: dt.date, ingredient: str) -> None:
        with self.conn() as c:
            c.execute(
                "DELETE FROM pantry WHERE source='tick' AND week=? AND ingredient=?",
                (week.isoformat(), ingredient),
            )

    def lots(self) -> list[dict]:
        """Every entry, the oldest first."""
        with self.conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM pantry ORDER BY at, id").fetchall()]

    # paired phones (see devices.py: this class only stores what that module decides)
    def add_device(self, name: str, person: str | None, token_hash: str, now: dt.datetime) -> int:
        with self.conn() as c:
            cur = c.execute(
                "INSERT INTO device(name, person, token_hash, created) VALUES (?,?,?,?)",
                (name, person, token_hash, now.isoformat(timespec="seconds")),
            )
        return int(cur.lastrowid)

    def device_by_token(self, token_hash: str) -> dict | None:
        """The live device that bearer belongs to, or None. A revoked one answers None."""
        with self.conn() as c:
            r = c.execute(
                "SELECT * FROM device WHERE token_hash=? AND revoked=0", (token_hash,)
            ).fetchone()
        return dict(r) if r else None

    def devices(self) -> list[dict]:
        with self.conn() as c:
            rows = c.execute(
                "SELECT id, name, person, created, last_seen FROM device WHERE revoked=0 "
                "ORDER BY created"
            ).fetchall()
        return [dict(r) for r in rows]

    def touch_device(self, device_id: int, now: dt.datetime) -> None:
        with self.conn() as c:
            c.execute(
                "UPDATE device SET last_seen=? WHERE id=?",
                (now.isoformat(timespec="seconds"), device_id),
            )

    def revoke_device(self, device_id: int) -> bool:
        with self.conn() as c:
            cur = c.execute("UPDATE device SET revoked=1 WHERE id=? AND revoked=0", (device_id,))
        return cur.rowcount > 0

    # pairing codes
    def add_pair_code(
        self, code_hash: str, person: str | None, now: dt.datetime, ttl_s: float
    ) -> None:
        with self.conn() as c:
            c.execute("DELETE FROM pair_code WHERE expires < ?", (now.timestamp(),))
            c.execute(
                "INSERT INTO pair_code(code_hash, person, created, expires) VALUES (?,?,?,?) "
                "ON CONFLICT(code_hash) DO UPDATE SET person=excluded.person, "
                "created=excluded.created, expires=excluded.expires, used=0",
                (code_hash, person, now.isoformat(timespec="seconds"), now.timestamp() + ttl_s),
            )

    def take_pair_code(self, code_hash: str, now: dt.datetime) -> dict | None:
        """Spend a code: returns its row once and never again, so a replay finds nothing."""
        with self.conn() as c:
            r = c.execute(
                "SELECT * FROM pair_code WHERE code_hash=? AND used=0 AND expires > ?",
                (code_hash, now.timestamp()),
            ).fetchone()
            if r is None:
                return None
            c.execute("UPDATE pair_code SET used=1 WHERE code_hash=?", (code_hash,))
        return dict(r)

    # small values the app learns at run time (e.g. a group's new id after an upgrade)
    def get_kv(self, key: str):
        with self.conn() as c:
            r = c.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(r["value"]) if r else None

    def set_kv(self, key: str, value) -> None:
        with self.conn() as c:
            c.execute(
                "INSERT INTO kv(key, value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)),
            )
