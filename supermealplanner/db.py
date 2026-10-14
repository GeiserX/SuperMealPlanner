"""SQLite store for what people log: weight, waist, the daily line, and calorie target changes."""

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
"""


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
