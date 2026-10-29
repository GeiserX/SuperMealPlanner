"""Telegram bot for the household group: commands, reminders, and questions answered by the AI.

Setup (settings.yaml, `bot`): create a bot with @BotFather, put its token in the env var named by
token_env, add the bot to the household group, send /chatid there and copy the id into bot.chat_id.
Map each person to their Telegram user id in bot.people.

In a private chat the bot always answers. A voice note is always answered, in the group too, because
it cannot mention the bot, and so is a photo, a video, a GIF, a round video note, a sticker or any
file. In the group it answers only a text message that mentions its @handle or replies to it.

A photo or a file goes to a provider that can look at it (llm.sees_files) as turn["attachments"],
its caption as the message's text. It is queued like any message, by its Telegram file id, and its
bytes are fetched when its turn starts: FILE_MAX each, TURN_FILES_MAX in all. One too big, or one
whose download fails, gets a short note under it.

One answer per burst: a chat's messages wait DEBOUNCE_S after the newest one, and for each voice
note of the chat still being transcribed (as long as transcription takes, VOICE_WAIT_S), and go to
the AI together. What arrives while it writes is handed to the running answer (llm.py,
turn["incoming"]). An answer that did not take one of those is never sent: the turn runs again at
once with all of them, told its last answer did not go out (AGAIN, once in a row), so the chat gets
one answer and no proposal it already outdated. Only what arrives after that makes the next turn. An
answer that covers one message replies to it; one that covers several is sent plainly. An edited
message is handled by where it is: still waiting, its new text goes; in the running answer, a
correction joins it; already answered, the correction starts a new answer; an edit that adds the
mention is a new message. Changes, proposals and the provider's asks come with inline buttons; a
typed yes or no still works, and only the person asked (or an admin) can tap.

When an answer tells someone to do something at a time, the AI lists those steps and the bot offers
each as a reminder with buttons: here (the bot posts it in the chat at that time), in the calendar
(with bot.reminder_calendar, a turn in which the provider adds the event; that tap approves its first
ask when the ask adds an event at that very time) or nothing. An offer nobody taps expires and
nothing happens. /reminders lists the ones set, with a button to cancel each.

Telegram shows a bot with privacy mode on (the default) only commands and replies to it, so for
mentions (and edits of messages without one) make the bot a group admin or turn privacy off
(BotFather, /setprivacy -> Disable). The bot logs a warning at start when it cannot see them.

A restart loses no message: each one Telegram hands over is stored before its update is confirmed,
and each one the bot takes stays stored until a turn that took it ends, so the next process answers
what was waiting, being transcribed or being answered when the last one stopped (resume).

Only the configured group and private chats with the people in bot.people are answered.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import mimetypes
import os
import re
import secrets
import threading
import time
import unicodedata
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from . import __version__, changes, context, doctor, gestures, llm, menu, pantry, render, verifier
from .i18n import Translator
from .model import WEEKDAYS
from .planner import ICONS, day_menu, paused_days, plan_for
from .portions import (
    batch_dish_text,
    batch_sheet,
    person_days,
    shopping_list,
    soak_for,
    thaw_for,
)

log = logging.getLogger("supermealplanner.bot")
API = "https://api.telegram.org/bot{token}/{method}"


class TelegramError(Exception):
    """A failed Bot API call. Its text never contains the URL, which holds the token."""

    def __init__(
        self, method: str, description: str, code: int | None = None, params: dict | None = None
    ):
        super().__init__(f"telegram {method}: {code} {description}")
        self.code = code
        self.params = params or {}


ANNOUNCED = "bot.announced_version"  # the last version the bot told its admins about


def safe(e: BaseException) -> str:
    """An exception as text that is safe to log: HTTP errors keep only their status, never the URL."""
    if isinstance(e, httpx.HTTPStatusError):
        return f"HTTP {e.response.status_code}"
    if isinstance(e, httpx.HTTPError):
        return type(e).__name__
    return f"{type(e).__name__}: {str(e)[:300]}"


# command -> handler name; Spanish aliases share handlers
COMMANDS = {
    "start": "help",
    "help": "help",
    "ayuda": "help",
    "today": "today",
    "hoy": "today",
    "tomorrow": "tomorrow",
    "manana": "tomorrow",
    "week": "week",
    "semana": "week",
    "list": "list",
    "lista": "list",
    "weight": "weight",
    "peso": "weight",
    "waist": "waist",
    "cintura": "waist",
    "craving": "craving",
    "antojo": "craving",
    "water": "water",
    "agua": "water",
    "pills": "pills",
    "pastillas": "pills",
    "about": "about",
    "presentacion": "about",
    "new": "new",
    "nuevo": "new",
    "batch": "batch",
    "cocinar": "batch",
    "reminders": "reminders",
    "recordatorios": "reminders",
    "pair": "pair",
    "vincular": "pair",
    "devices": "devices",
    "dispositivos": "devices",
}
USERNAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{3,31}")  # Telegram usernames (4-letter ones exist)
NUMBER = re.compile(r"(\d+(?:[.,]\d+)?)")
AI = "ai"  # the `who` of the AI's own answers in the chat log
PENDING_TTL = 3600  # a proposed change waits this long for its yes
# What waits for a yes, as a list. 0.3.x kept one change as a dict under bot.pending.<chat> and reads
# nothing else, so the list lives under its own key: a rollback to 0.3.x finds nothing it cannot read.
WAITING = "bot.waiting.{}"
LEGACY_PENDING = "bot.pending.{}"
RUNNING = "bot.running"  # proposals the plugin is carrying out now, across every chat
# A restart (a redeploy replaces the container at any moment) loses no message. INBOX holds
# Telegram's offset and each message it handed over until the bot has taken it; QUEUED holds, per
# chat, each message taken for an answer until a turn that took it ends. resume() replays both.
INBOX = "bot.inbox"  # {"offset": next update id, "messages": [Telegram message, ...]}
QUEUED = "bot.queued"  # {"<chat id>": [{"row", "mid", "uid", "who", "grant"}, ...]}
# ask() returns this when its answer was not sent because it missed messages handed to it: the
# chat's next turn, right away, answers them all
AGAIN: list = []
ACT_TIMEOUT = 3600  # the longest a plugin may take on a proposal (a code draft takes minutes)
# a chat's turn starts this long after its newest message, so a burst goes together
DEBOUNCE_S = 2.5
# A turn waits for a voice note of its chat still being transcribed: VOICE_WAIT_S plus VOICE_PER_S
# for each second of audio, never longer than transcription.timeout_s. The servers Qwen runs on took
# 2.3 to 2.5 s per second of audio plus about 10 s to start (2026-10-28: a 47 s note took 119 s), so
# a fixed 30 s let a second note miss the turn it belonged to.
VOICE_WAIT_S = 30
VOICE_PER_S = 3
PLAN_RAISED = "verifier.plan_raised"  # {"key", "on"}: the plan last raised in the chat, and the day
PLAN_RAISE_H = 2  # hours after bot.checkup_at a finished plan check may still raise its problems
PLAN_EVERY_S = 600  # how often the plan as it stands is checked again (judged only when it changed)
# A provider that is out of capacity raises an error with `retry_after` (seconds): the chat's messages
# wait that long, at least BUSY_MIN_S, and are answered then, with nobody writing again.
BUSY_MIN_S = 60
FILE_MAX = 20 * 1024 * 1024  # the largest file the Bot API lets a bot download
TURN_FILES_MAX = 25 * 1024 * 1024  # the photos and files one turn carries at most
# what in a message is a file for the AI, document last: Telegram sets it on an animation too
FILE_KEYS = ("photo", "video", "animation", "video_note", "sticker", "document")
# what a replied-to message is, as its reply tag names it (anything else is a "message")
REPLY_KINDS = {
    "photo": "photo",
    "video": "video",
    "animation": "animation",
    "video_note": "video note",
    "sticker": "sticker",
    "voice": "voice note",
    "audio": "audio",
    "document": "file",
}
OPTIONS = "bot.options.{}"  # choices offered as buttons under an answer, per chat
OPTIONS_TTL = 12 * 3600  # a choice can be tapped this long
OPTIONS_KEEP = 20
REMIND = "bot.remind"  # reminders offered with buttons and not yet settled, across every chat
REMIND_TTL = 1800  # an offer can be tapped this long; then nothing is done
REMIND_WHAT = 20  # characters of a reminder's text on its buttons
REMIND_DAYS = 60  # the furthest ahead a reminder may be
LATE_S = 600  # a reminder posted this much after its time says so
# A spoken turn from the app (POST /api/v1/chef): the message it becomes carries its id under this
# key, which no Telegram message has. Its answer is kept this long for the app to read.
SPOKEN = "_spoken"
SPOKEN_TTL = 3600
SPOKEN_MAX = 2000  # the longest thing anyone says in one go, as characters
# (chat id, list) pairs: each list collects what the bot says in that chat while a spoken turn
# listens. A context variable, so only the task running that turn (and what it starts) is heard,
# never a typed command answered in the same chat at the same moment.
HEARING: ContextVar[tuple[tuple[int, list[str]], ...]] = ContextVar("hearing", default=())


@dataclass
class Item:
    """A message that called the bot, waiting for its answer or in one."""

    row: int  # its row in the chat log
    mid: int | None  # its Telegram message id (what the answer replies to)
    uid: int | None  # who sent it
    who: str | None  # their person id
    ts: float  # when it arrived (the bot's clock)
    # a tap on a reminder's "in the calendar": approves the turn's first ask when it adds the
    # event at that time {"by": user id, "at": epoch seconds, "used": bool}
    grant: dict | None = None
    # a photo or a file it carries, fetched when its turn starts {"file_id", "name", "mime", "size"}
    file: dict | None = None


@dataclass
class Spoken:
    """Something said to the app, on its way through the household group (or, with no group or
    bot.voice_to_group off, the person's private chat with the bot)."""

    id: str
    person: str
    uid: int  # their Telegram user id
    text: str
    chat: int = 0  # where the turn happens: the group, or their private chat (0: uid)
    ts: float = field(default_factory=time.time)
    state: str = "thinking"  # then "done", or "failed" when nothing answered it
    answer: str = ""  # what the bot said, cleaned to be read out
    row: int | None = None  # its row in the chat log, once it is queued for an answer
    ready: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass
class Turn:
    """One answer being written for a chat."""

    chat: int
    items: list[Item]  # the messages it was started with
    pulled: list[Item] = field(default_factory=list)  # handed to it while it ran
    asks: list[str] = field(default_factory=list)  # keys of the provider's asks it posted
    closed: bool = False

    def rows(self) -> set[int]:
        return {i.row for i in [*self.items, *self.pulled]}

    def users(self) -> list[int]:
        return list(dict.fromkeys(i.uid for i in [*self.items, *self.pulled] if i.uid))

    def grants(self) -> list[dict]:
        return [i.grant for i in [*self.items, *self.pulled] if i.grant is not None]


class Bot:
    def __init__(self, state):
        self.state = state  # web.App: .data, .db, .today()
        cfg = state.data.settings.bot
        self.token = os.environ.get(cfg.token_env, "")
        self.username = ""
        self.offset = (state.db.get_kv(INBOX) or {}).get("offset", 0)
        # a group Telegram upgraded to a supergroup: {"from": old id, "to": new id}
        self.migration: dict | None = state.db.get_kv("bot.migrated_chat")
        self.ai = asyncio.Semaphore(1)  # one AI answer at a time, in arrival order
        self.usernames: dict[str, str] = {}  # person id -> Telegram username, from getChatMember
        self.tasks: set[asyncio.Task] = set()
        self.started = False
        self.bot_id = 0
        self.writing = asyncio.Lock()  # one settings change at a time
        self.debounce, self.voice_wait, self.clock = DEBOUNCE_S, VOICE_WAIT_S, time.monotonic
        # per chat: messages waiting for a turn. The provider's thread takes from it while a turn
        # runs (turn["incoming"]), so it is guarded by a thread lock, never held across an await
        self.queues: dict[int, list[Item]] = {}
        self.qlock = threading.Lock()
        self.driving: set[int] = set()  # chats with a loop running their turns
        # chat -> until when each note being transcribed holds the chat's turn (clock())
        self.voicing: dict[int, list[float]] = {}
        self.running: dict[int, Turn] = {}
        self.asks: dict[str, dict] = {}  # open asks of the provider, by key
        self.resting: dict[int, float] = {}  # chat -> clock() its answer waits for (busy provider)
        self.again: set[int] = set()  # chats whose next turn rewrites an answer that was not sent
        self.told_busy: set[int] = set()  # chats told of the wait, until an answer goes out
        self.client: httpx.AsyncClient | None = None  # the running loop's, for the app's turns
        self.spoken: dict[str, Spoken] = {}  # what was said to the app, by turn id
        self.spoken_rows: dict[int, str] = {}  # chat log row -> the spoken turn it is
        self.stickers: dict = {}  # the settings' sticker set, read once ({"name", "set"})
        self.planned_at: float | None = None  # clock() of the last plan check started

    # ------------------------------------------------------------- plumbing
    @property
    def data(self):
        return self.state.data

    @property
    def t(self) -> Translator:
        return Translator(self.data.locale)

    async def call(
        self, client: httpx.AsyncClient, method: str, _tries: int = 0, _files=None, **params
    ):
        """One Bot API call. `_files` ({field: (name, bytes, mime)}) sends it as multipart, the
        other params as form fields (non-strings as JSON)."""
        try:
            if _files:
                form = {
                    k: v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
                    for k, v in params.items()
                    if v is not None
                }
                r = await client.post(
                    API.format(token=self.token, method=method), data=form, files=_files, timeout=70
                )
            else:
                r = await client.post(
                    API.format(token=self.token, method=method), json=params, timeout=70
                )
            body = r.json()
        except httpx.ConnectError as e:
            # The request never reached Telegram, so sending it again cannot post it twice.
            if method != "getUpdates" and _tries < 2:
                await asyncio.sleep(2 * (_tries + 1))
                return await self.call(client, method, _tries + 1, _files, **params)
            raise TelegramError(method, safe(e)) from None
        except (httpx.HTTPError, ValueError) as e:
            raise TelegramError(method, safe(e)) from None
        if body.get("ok"):
            return body["result"]
        err = TelegramError(
            method, body.get("description", ""), body.get("error_code"), body.get("parameters")
        )
        if err.code == 429 and method != "getUpdates" and _tries < 2:  # flood control: wait as told
            await asyncio.sleep(min(int(err.params.get("retry_after", 5)), 60))
            return await self.call(client, method, _tries + 1, _files, **params)
        raise err

    async def download(self, client, file_id: str) -> tuple[bytes, str]:
        """A file Telegram holds for the bot, FILE_MAX at most, with its path on Telegram's side."""
        f = await self.call(client, "getFile", file_id=file_id)
        if (f.get("file_size") or 0) > FILE_MAX:
            raise TelegramError("getFile", "file is too big", 413)
        url = f"https://api.telegram.org/file/bot{self.token}/{f['file_path']}"
        try:
            r = await client.get(url, timeout=120)
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise TelegramError("getFile download", safe(e)) from None
        return r.content, f["file_path"]

    async def transcribe(self, client, voice: dict) -> str:
        audio, path = await self.download(client, voice["file_id"])
        name = path.rsplit("/", 1)[-1] or "voice.ogg"
        return await asyncio.wait_for(
            llm.transcribe(self.data, audio, name),
            timeout=self.data.settings.transcription.timeout_s,
        )

    async def send(
        self,
        client,
        chat_id: int,
        text: str,
        reply_to: int | None = None,
        record: bool = True,
        markdown: bool = False,
        who: str | None = None,
        ids: list[int] | None = None,
        buttons: list[list[tuple[str, str]]] | None = None,
        spoken: str | None = None,
    ) -> int | None:
        """markdown: the text is the AI's Markdown, sent as Telegram HTML; a chunk Telegram refuses
        to parse goes again as plain text. Returns the id of the last message sent; `ids` collects
        the id of every piece. buttons: rows of (label, callback data), under the last piece.
        spoken: what a spoken turn listening in this chat hears instead of `text` ("" for
        nothing), when the message only makes sense on a screen (buttons to tap)."""
        sent = None
        chunks = render.split(text) if markdown else list(_chunks(text))
        for n, chunk in enumerate(chunks):
            params = {"chat_id": chat_id, "disable_web_page_preview": True}
            if buttons and n == len(chunks) - 1:
                params["reply_markup"] = keyboard(buttons)
            if reply_to:
                params["reply_parameters"] = {
                    "message_id": reply_to,
                    "allow_sending_without_reply": True,
                }
            if markdown:
                try:
                    sent = await self.call(
                        client, "sendMessage", text=to_html(chunk), parse_mode="HTML", **params
                    )
                    if ids is not None and isinstance(sent, dict) and sent.get("message_id"):
                        ids.append(sent["message_id"])
                    continue
                except TelegramError as e:
                    if e.code != 400:
                        raise
                    log.info("Telegram refused the formatting, sending plain text: %s", e)
            sent = await self.call(client, "sendMessage", text=chunk, **params)
            if ids is not None and isinstance(sent, dict) and sent.get("message_id"):
                ids.append(sent["message_id"])
        if record:
            self.state.db.add_chat(chat_id, "assistant", who, text)
        heard = text if spoken is None else spoken
        for chat, ears in HEARING.get():
            if chat == chat_id and heard:
                ears.append(heard)
        return sent.get("message_id") if isinstance(sent, dict) else None

    async def announce_version(self, client) -> None:
        """A new version says so once: privately to each admin, or in the group when no admin can
        be written to. The version is kept only once a message went out, so a failed send retries
        at the next start; a restart on the same version says nothing."""
        before = self.state.db.get_kv(ANNOUNCED)
        if not __version__ or before == __version__:
            return
        t = self.t
        text = (
            t("bot_upgraded", version=__version__, previous=before)
            if before
            else t("bot_upgraded_first", version=__version__)
        )
        cfg = self.data.settings.bot
        targets = [cfg.people[p] for p in cfg.admins if p in cfg.people]
        sent = False
        for uid in targets:
            try:
                await self.send(client, uid, text, record=False)
                sent = True
            except Exception as e:  # noqa: BLE001  an admin who never opened a private chat
                log.info("could not tell admin about the new version: %s", safe(e))
        if not sent and self.chat_id is not None:
            try:
                await self.send(client, self.chat_id, text, record=False)
                sent = True
            except Exception as e:  # noqa: BLE001  the next start tries again
                log.warning("could not announce the new version: %s", safe(e))
        if sent:
            self.state.db.set_kv(ANNOUNCED, __version__)

    @property
    def chat_id(self) -> int | None:
        configured = self.data.settings.bot.chat_id
        m = self.migration
        if isinstance(m, dict) and configured == m.get("from"):
            return m["to"]  # only while settings still name the old group
        return configured

    def mention(self, pid: str) -> str:
        """How the bot names a person it addresses: their @handle, so Telegram notifies them;
        the plain name when they have none."""
        for handle in (self.data.settings.bot.handles.get(pid), self.usernames.get(pid)):
            name = (handle or "").strip().removeprefix("@")
            if USERNAME.fullmatch(name):  # a typo in settings never replaces a working handle
                return f"@{name}"
        return self.data.household.person(pid).name

    async def learn_usernames(self, client) -> None:
        """Look up each person's Telegram username once at start (group first, then their own chat)."""
        for pid, uid in self.data.settings.bot.people.items():
            for chat in (self.chat_id, uid):
                if chat is None:
                    continue
                try:
                    m = await self.call(client, "getChatMember", chat_id=chat, user_id=uid)
                    username = (m.get("user") or {}).get("username")
                except (TelegramError, KeyError, AttributeError, TypeError) as e:
                    log.info("username lookup failed: %s", safe(e))
                    continue
                if username:
                    self.usernames[pid] = username
                break

    def person_of(self, user_id: int) -> str | None:
        for pid, uid in self.data.settings.bot.people.items():
            if uid == user_id:
                return pid
        return None

    def allowed(self, chat: dict, user_id: int) -> bool:
        """Private chats with household members, and the household group. In the group only household
        members are answered unless bot.group_open is set."""
        cfg = self.data.settings.bot
        member = user_id in cfg.people.values()
        if chat.get("type") == "private":
            return member and cfg.private_chats
        if self.chat_id is None or chat["id"] != self.chat_id:
            return False
        return member or cfg.group_open

    def url(self, path: str) -> str:
        base = self.data.settings.base_url.rstrip("/")
        return f"{base}{path}" if base else path

    # ------------------------------------------------------------- loops
    async def run(self) -> None:
        """Keep the bot alive: any failure restarts it after a growing pause, never ends it."""
        if not self.token:
            log.warning("bot enabled but %s is empty", self.data.settings.bot.token_env)
            return
        backoff = 5
        while True:
            try:
                async with httpx.AsyncClient() as client:
                    self.client = client
                    await self.call(
                        client, "deleteWebhook"
                    )  # polling and a webhook exclude each other
                    me = await self.call(client, "getMe")
                    self.username = me.get("username", "")
                    self.bot_id = me.get("id", 0)
                    if self.data.settings.bot.sync_profile:
                        await self.sync_profile(client)
                    await self.learn_usernames(client)
                    await self.check_group(client, me)
                    if not self.started:  # once per process: a loop restart cuts nothing off
                        await self.announce_version(client)
                        await self.interrupted(client)
                        await self.resume(client)
                        self.started = True  # only now: a failed resume runs again on the retry
                    log.warning("bot @%s up", self.username)
                    backoff = 5
                    loops = [
                        asyncio.create_task(self.poll(client)),
                        asyncio.create_task(self.reminders(client)),
                    ]
                    try:
                        done, _ = await asyncio.wait(loops, return_when=asyncio.FIRST_EXCEPTION)
                        for task in done:
                            task.result()  # re-raise what stopped it
                    finally:
                        for task in loops:  # never leave a loop running on a closed client
                            task.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("bot stopped (%s); restarting in %ss", safe(e), backoff)
            finally:
                self.client = None  # closed: the app's turns wait for the next one (api.chef)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 300)

    async def poll(self, client) -> None:
        while True:
            try:
                updates = await self.call(
                    client,
                    "getUpdates",
                    offset=self.offset,
                    timeout=50,
                    allowed_updates=["message", "edited_message", "callback_query"],
                )
            except TelegramError as e:
                if e.code == 409:
                    log.warning("another instance is polling this bot token; waiting")
                    await asyncio.sleep(30)
                elif e.code == 401:
                    raise
                else:
                    log.warning("getUpdates failed: %s", e)
                    await asyncio.sleep(10)
                continue
            for u in updates:
                msg = u.get("message")
                kept = bool(msg) and _for_handle(msg)
                # stored before Telegram is told it arrived (the next getUpdates), in one write;
                # a failed write leaves the offset where it was, so the update comes again
                self.inbox(add=msg if kept else None, offset=u["update_id"] + 1)
                if u.get("edited_message") or u.get("callback_query"):
                    work = (
                        self.edited(client, u["edited_message"])
                        if u.get("edited_message")
                        else self.tapped(client, u["callback_query"])
                    )
                    task = asyncio.create_task(self.quietly(work))
                    self.tasks.add(task)
                    task.add_done_callback(self.tasks.discard)
                    continue
                if not msg:
                    continue
                if msg.get("migrate_to_chat_id") and msg["chat"]["id"] == self.chat_id:
                    self.migration = {"from": msg["chat"]["id"], "to": msg["migrate_to_chat_id"]}
                    self.state.db.set_kv("bot.migrated_chat", self.migration)
                    log.warning("the group became %s: set bot.chat_id to it", self.migration["to"])
                    continue
                if _for_handle(msg):
                    task = asyncio.create_task(self.guarded(client, msg))
                    self.tasks.add(task)
                    task.add_done_callback(self.tasks.discard)

    async def guarded(self, client, msg: dict) -> None:
        """One message in its own task, so a slow AI never blocks the next command."""
        # no overall limit: the AI and transcription calls carry their own, and waiting in line
        # for the one AI slot must not count against an answer
        try:
            await self.handle(client, msg)
        except Exception as e:  # noqa: BLE001
            self.inbox(done=msg)
            log.warning("handling a message failed: %s", safe(e))
            try:
                await self.send(client, msg["chat"]["id"], self.t("bot_error"), record=False)
            except Exception as e2:  # noqa: BLE001  the reply is best effort
                log.warning("could not send the error reply: %s", safe(e2))
        else:
            self.inbox(done=msg)

    def spawn(self, work) -> None:
        task = asyncio.create_task(work)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def resume(self, client) -> None:
        """Once at start: what the last process left goes on. Each chat's queued messages (still
        waiting, or taken by a turn that never ended) get a turn, and each message Telegram handed
        over that the bot never took is handled again."""
        queued = self.state.db.get_kv(QUEUED) or {}
        inbox = (self.state.db.get_kv(INBOX) or {}).get("messages") or []
        # a stop between queueing a message and dropping it from the inbox leaves it in both
        taken = {(int(k), x.get("mid")) for k, kept in queued.items() for x in kept}
        twice = [m for m in inbox if (m["chat"]["id"], m.get("message_id")) in taken]
        for msg in twice:
            self.inbox(done=msg)
        inbox = [m for m in inbox if m not in twice]
        with self.qlock:
            for key, kept in queued.items():
                items = [
                    Item(
                        x["row"],
                        x.get("mid"),
                        x.get("uid"),
                        x.get("who"),
                        self.clock(),
                        x.get("grant"),
                        x.get("file"),
                    )
                    for x in kept
                ]
                self.queues[int(key)] = [*items, *self.queues.get(int(key), [])]
        for key in queued:
            self.spawn(self.drive(client, int(key)))
        for msg in inbox:
            self.spawn(self.guarded(client, msg))
        if queued or inbox:
            log.warning("resumed: chats=%d messages=%d", len(queued), len(inbox))

    def inbox(
        self, add: dict | None = None, done: dict | None = None, offset: int | None = None
    ) -> None:
        """INBOX: store the next `offset`, with `add` (the message Telegram just handed over), in
        one write, and move `self.offset` once it is stored; or drop `done`, a message the bot has
        taken (queued, answered or ignored)."""
        db = self.state.db
        messages = (db.get_kv(INBOX) or {}).get("messages") or []
        if add is not None:
            messages.append(add)
        if done is not None:
            key = (done["chat"]["id"], done.get("message_id"))
            left = [m for m in messages if (m["chat"]["id"], m.get("message_id")) != key]
            if len(left) == len(messages):
                return
            messages = left
        db.set_kv(
            INBOX, {"offset": offset if offset is not None else self.offset, "messages": messages}
        )
        if offset is not None:
            self.offset = offset

    def queued(self, chat_id: int, add: Item | None = None) -> None:
        """QUEUED for one chat: with `add`, one more message to answer; without, what is left once
        a turn ended or /nuevo emptied the queue, which is what the chat's queue still holds (a
        message handed to the turn and not covered went back to it). Called with `qlock` held."""
        db, key = self.state.db, str(chat_id)
        every = db.get_kv(QUEUED) or {}
        kept = every.get(key) or []
        if add is not None:
            kept.append(
                {
                    "row": add.row,
                    "mid": add.mid,
                    "uid": add.uid,
                    "who": add.who,
                    "grant": add.grant,
                    "file": add.file,
                }
            )
        else:
            left = {i.row for i in self.queues.get(chat_id) or []}
            kept = [x for x in kept if x["row"] in left]
        if kept:
            every[key] = kept
        else:
            every.pop(key, None)
        db.set_kv(QUEUED, every or None)

    async def quietly(self, work) -> None:
        """An edit or a tap in its own task; a failure is logged, never posted."""
        try:
            await work
        except Exception as e:  # noqa: BLE001
            log.warning("handling an edit or a tap failed: %s", safe(e))

    async def reminders(self, client) -> None:
        while True:
            try:
                await self.tick_reminders(client)
            except Exception as e:  # noqa: BLE001
                log.warning("reminder tick failed: %s", safe(e))
            try:
                await self.tick_announce(client)
            except Exception as e:  # noqa: BLE001
                log.warning("new menu announcement failed: %s", safe(e))
            try:
                await self.tick_once(client)
            except Exception as e:  # noqa: BLE001
                log.warning("one-off reminder tick failed: %s", safe(e))
            try:
                await self.tick_checkup(client)
            except Exception as e:  # noqa: BLE001
                log.warning("morning checkup failed: %s", safe(e))
            try:
                self.tick_plan()
                await self.raise_plan(client)
            except Exception as e:  # noqa: BLE001
                log.warning("plan check failed: %s", safe(e))
            try:
                await self.expire_asks(client)
                await self.expire_offers(client)
            except Exception as e:  # noqa: BLE001
                log.warning("ask expiry failed: %s", safe(e))
            await asyncio.sleep(30)

    # ------------------------------------------------------------- messages
    def mentioned(self, text: str) -> bool:
        """The text mentions the bot's @handle. Nothing else in a group message calls it."""
        if not self.username:
            return False
        return bool(re.search(rf"@{re.escape(self.username)}\b", text, re.IGNORECASE))

    def replied(self, msg: dict) -> bool:
        """The message replies to one of the bot's own."""
        sender = (msg.get("reply_to_message") or {}).get("from") or {}
        if self.bot_id and sender.get("id") == self.bot_id:
            return True
        return bool(self.username) and sender.get("username") == self.username

    def reply_tag(self, msg: dict) -> str:
        """What a reply points at, in front of its text: "(replying to luis's photo of <UTC
        time>) ", so the AI can find an older message it cannot see. The time, not the message id:
        an id the bot sees may not match the same message elsewhere. Nothing for a reply to the
        bot's own message, which the AI already has."""
        to = msg.get("reply_to_message")
        if not isinstance(to, dict) or self.replied(msg) or to.get("forum_topic_created"):
            return ""
        sender = to.get("from") or {}
        who = (
            self.person_of(sender.get("id", 0))
            or sender.get("first_name")
            or (to.get("sender_chat") or {}).get("title")
            or "someone"
        )
        kind = next((k for k in REPLY_KINDS if to.get(k)), "message")
        when = to.get("date")
        at = (
            f" of {dt.datetime.fromtimestamp(when, dt.UTC).isoformat()}"
            if isinstance(when, int)
            else ""
        )
        return f"(replying to {who}'s {REPLY_KINDS.get(kind, kind)}{at}) "

    async def handle(self, client, msg: dict) -> None:
        chat, user = msg["chat"], msg.get("from", {})
        private = chat.get("type") == "private"
        cfg = self.data.settings.bot
        voice = msg.get("voice") or msg.get("audio")
        if voice and not msg.get("text"):
            if not self.allowed(chat, user.get("id", 0)) or not cfg.voice:
                return
            # a voice note always calls the bot, in the group too: it cannot mention or reply by itself.
            # While it is transcribed, the chat's next turn waits for it (hold_for), so a burst of
            # notes, or a note and a text, gets one answer.
            until = self.clock() + self.hold_for(voice)
            notes = self.voicing.setdefault(chat["id"], [])
            notes.append(until)
            try:
                try:
                    text = await self.transcribe(client, voice)
                except Exception as e:  # noqa: BLE001
                    log.warning("transcription failed: %s", safe(e))
                    await self.send(
                        client,
                        chat["id"],
                        self.t("bot_voice_failed"),
                        msg["message_id"],
                        record=False,
                    )
                    return
                who = self.person_of(user.get("id", 0))
                await self.send(client, chat["id"], f"🎤 {text}", msg["message_id"], record=False)
                tagged = f"{self.reply_tag(msg)}🎤 {text}"
                self.take(chat["id"], user, who, msg, tagged, asked=True)
                self.inbox(done=msg)  # queued now: QUEUED keeps it from here
            finally:
                notes.remove(until)
                if not notes:
                    self.voicing.pop(chat["id"], None)
            await self.drive(client, chat["id"])
            return
        if any(msg.get(k) for k in FILE_KEYS) and not msg.get("text"):
            await self.on_file(client, msg)
            return
        text = msg["text"].strip()
        if text.startswith("/chatid"):  # setup: household members only, nothing stored
            if user.get("id") in cfg.people.values():
                await self.send(
                    client, chat["id"], self.t("bot_chat_id", id=chat["id"]), record=False
                )
            return
        if not self.allowed(chat, user.get("id", 0)):
            log.info(
                "ignored chat %s (%s) from user %s", chat["id"], chat.get("type"), user.get("id")
            )
            return
        who = self.person_of(user.get("id", 0))
        if await self.answer_pending(client, chat["id"], user, msg, text):
            return
        if text.startswith("/"):
            cmd, _, arg = text[1:].partition(" ")
            cmd = cmd.split("@")[0].lower()
            handler = COMMANDS.get(cmd)
            if handler:
                reply = await getattr(self, f"cmd_{handler}")(who, arg.strip(), user, chat)
                reply, rows = reply if isinstance(reply, tuple) else (reply, None)
                await self.send(
                    client,
                    chat["id"],
                    reply,
                    msg["message_id"],
                    record=handler != "new",
                    buttons=rows,
                )
            return
        asked = private or self.mentioned(text) or self.replied(msg) or SPOKEN in msg
        if asked or cfg.history_scope == "all":
            self.take(chat["id"], user, who, msg, self.reply_tag(msg) + text, asked)
            self.inbox(done=msg)  # logged, and queued when asked: QUEUED keeps it from here
        if asked:
            await self.drive(client, chat["id"])

    async def on_file(self, client, msg: dict) -> None:
        """A photo, a video, a GIF, a video note, a sticker or any file. Like a voice note it always
        calls the bot,
        in the group too; its caption is the message. It is queued with its file id, and its bytes
        are fetched when its turn starts (fetch_files)."""
        chat, user = msg["chat"], msg.get("from", {})
        if not self.allowed(chat, user.get("id", 0)):
            return
        mid = msg.get("message_id")

        async def note(key: str) -> None:
            await self.send(client, chat["id"], self.t(key), mid, record=False)

        if not llm.sees_files(self.data):
            await note("bot_file_unseen")
            return
        if msg.get("photo"):
            sizes = [s for s in msg["photo"] if isinstance(s, dict) and s.get("file_id")]
            if not sizes:
                return
            big = max(sizes, key=lambda s: (s.get("file_size") or 0, s.get("width") or 0))
            label, mime, name = "photo", "image/jpeg", "photo.jpg"
            file_id, size = big["file_id"], big.get("file_size")
        else:
            kind = next(k for k in FILE_KEYS if msg.get(k))
            media = msg[kind]
            if not isinstance(media, dict) or not media.get("file_id"):
                return
            mime, name = _media(kind, media)
            label = "file" if kind == "document" else kind.replace("_", " ")
            file_id, size = media["file_id"], media.get("file_size")
        if isinstance(size, int) and size > FILE_MAX:
            await note("bot_file_too_big")
            return
        file = {"file_id": file_id, "name": f"inbox/{mid}-{name}", "mime": mime, "size": size}
        caption = (msg.get("caption") or "").strip()
        text = f"{self.reply_tag(msg)}[{label}: {file['name']}] {caption}".rstrip()
        who = self.person_of(user.get("id", 0))
        self.take(chat["id"], user, who, msg, text, asked=True, file=file)
        self.inbox(done=msg)  # queued now: QUEUED keeps it from here
        await self.drive(client, chat["id"])

    async def fetch_files(self, client, chat_id: int, items: list[Item]) -> list[dict]:
        """The photos and files of a turn's messages, as turn["attachments"]: FILE_MAX each and
        TURN_FILES_MAX in all. One that cannot be fetched or does not fit gets a note under its
        message, and the message still goes without it."""
        out, total = [], 0
        for i in items:
            if not i.file:
                continue
            try:
                data, _ = await self.download(client, i.file["file_id"])
            except TelegramError as e:
                log.warning("file not fetched: %s", safe(e))
                big = e.code == 413 or "too big" in str(e).lower()
                key = "bot_file_too_big" if big else "bot_file_failed"
                await self.send(client, chat_id, self.t(key), i.mid, record=False)
                continue
            if len(data) > FILE_MAX or total + len(data) > TURN_FILES_MAX:
                await self.send(client, chat_id, self.t("bot_file_too_big"), i.mid, record=False)
                continue
            total += len(data)
            out.append(
                {"name": i.file["name"], "mime": i.file["mime"], "data": data, "from": i.who}
            )
        return out

    def take(
        self,
        chat_id: int,
        user: dict,
        who,
        msg: dict | None,
        text: str,
        asked: bool,
        log_mid: bool = True,
        grant: dict | None = None,
        file: dict | None = None,
    ) -> int:
        """Log a message and, when it calls the bot, queue it for the chat's next turn (or the
        running one). No await in here: a turn that starts reads the log and the queue together.
        log_mid False: the answer replies to msg, but the log row is not that message (an edit's
        correction), so an edit of msg still finds msg's own row. A spoken turn from the app is
        marked "[voice]" for the AI, which then answers for the ear (context.WHO)."""
        spoken = self.spoken.get((msg or {}).get(SPOKEN) or "")
        if spoken is not None:
            text = f"[voice] {text}"
        row = self.state.db.add_chat(
            chat_id,
            "user",
            who or user.get("first_name"),
            text,
            mid=(msg or {}).get("message_id") if log_mid else None,
            asked=asked,
        )
        if asked:
            if spoken is not None:
                spoken.row = row
                self.spoken_rows[row] = spoken.id
            mid = (msg or {}).get("message_id")
            item = Item(row, mid, user.get("id"), who, self.clock(), grant, file)
            with self.qlock:
                self.queues.setdefault(chat_id, []).append(item)
                self.queued(chat_id, add=item)
        return row

    async def drive(self, client, chat_id: int) -> None:
        """Run the chat's turns until its queue is empty. One loop per chat: a message that arrives
        while it runs only joins the queue, and the loop (or the running turn) takes it."""
        if chat_id in self.driving:
            return
        self.driving.add(chat_id)
        try:
            while True:
                if self.resting.get(chat_id, 0) > self.clock():
                    return  # the provider is busy: wake() runs the chat's turn when it is back
                await self.settle(chat_id)
                async with self.ai:
                    with self.qlock:
                        items = self.queues.pop(chat_id, [])
                    if not items:
                        return
                    # read with the queue, before any await: a later message goes to the turn
                    # through incoming(), never in both
                    rows = self.state.db.chat_rows(
                        chat_id, self.data.settings.bot.history, self.data.settings.bot.history_days
                    )
                    # what the bot says in the chat during the turn is what a spoken turn hears
                    ears: list[str] = []
                    listening = HEARING.set((*HEARING.get(), (chat_id, ears)))
                    covered = None
                    try:
                        covered = await self.ask(client, chat_id, items, rows)
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:  # noqa: BLE001  the next turn of the chat still runs
                        log.warning("a turn failed: %s", safe(e))
                        try:
                            await self.send(
                                client,
                                chat_id,
                                self.t("bot_error"),
                                _reply_to(items),
                                record=False,
                            )
                        except Exception as e2:  # noqa: BLE001
                            log.warning("could not send the error reply: %s", safe(e2))
                    finally:
                        HEARING.reset(listening)
                    if covered is AGAIN:
                        continue  # its messages are queued again, for the turn that answers them
                    self.answered(items, covered, ears)
                    # the turn ended (a cancelled one did not: a stop keeps its messages queued)
                    with self.qlock:
                        self.queued(chat_id)
        finally:
            self.driving.discard(chat_id)

    # ------------------------------------------------------------- spoken turns (the app)
    def voice_chat(self, uid: int) -> int:
        """Where spoken turns happen: the household group, so everyone reads them, unless there is
        none or bot.voice_to_group is off; then the person's private chat with the bot."""
        if self.data.settings.bot.voice_to_group and self.chat_id is not None:
            return self.chat_id
        return uid

    def say(self, person: str, text: str) -> Spoken:
        """Something `person` said to the app: it goes into the group (voice_chat) as if they had
        written it there to the bot, so it is the same conversation with the same rules. Returns
        at once; the answer arrives on the Spoken (hear)."""
        self._forget_spoken()
        uid = self.data.settings.bot.people[person]
        # words, never a command: a spoken "/pair" must not hand a pairing code to the device
        text = text.lstrip().lstrip("/").strip()[:SPOKEN_MAX]
        spoken = Spoken(secrets.token_urlsafe(12), person, uid, text, chat=self.voice_chat(uid))
        self.spoken[spoken.id] = spoken
        self.spawn(self.hear(spoken))
        return spoken

    def spoken_for(self, sid: str, person: str | None) -> Spoken | None:
        """A spoken turn, only for the person who said it."""
        self._forget_spoken()
        spoken = self.spoken.get(sid)
        return spoken if spoken is not None and spoken.person == person else None

    async def hear(self, spoken: Spoken) -> None:
        """The chat shows what was said, then it is handled like a message written to the bot: a
        yes or no to what waits for this person, a command, or a message for the AI. A message queued for
        the AI is finished by the turn that answers it (answered); anything else here."""
        client, ears = self.client, []
        chat, name = spoken.chat or spoken.uid, self.data.household.person(spoken.person).name
        private = chat == spoken.uid
        try:
            heard = (
                self.t("voice_heard", text=spoken.text)
                if private
                else self.t("voice_heard_group", name=name, text=spoken.text)
            )
            mid = await self.send(client, chat, heard, record=False, spoken="")
            msg = {
                "message_id": mid,
                "chat": {"id": chat, "type": "private" if private else "group"},
                "from": {"id": spoken.uid, "first_name": name},
                "text": spoken.text,
                SPOKEN: spoken.id,
            }
            listening = HEARING.set((*HEARING.get(), (chat, ears)))
            try:
                await self.handle(client, msg)
            finally:
                HEARING.reset(listening)
        except Exception as e:  # noqa: BLE001  the app hears that it failed
            log.warning("a spoken turn failed: %s", safe(e))
            if spoken.row is None:
                self._finish(spoken, "failed", [])
            return
        if spoken.row is None:  # answered without a turn of the AI
            self._finish(spoken, "done" if ears else "failed", ears)

    def answered(self, items: list[Item], covered: list[Item] | None, ears: list[str]) -> None:
        """A turn ended: each spoken message it answered gets what the bot said in it. One it was
        started with and did not answer (the provider failed or is busy) gets what the bot said
        instead, as failed; a busy provider still answers it later, in the chat and here."""
        done = {i.row for i in covered or []}
        for i in [*(covered or []), *items]:
            spoken = self.spoken.get(self.spoken_rows.get(i.row) or "")
            if spoken is None or (spoken.state == "done" and i.row not in done):
                continue
            self._finish(spoken, "done" if i.row in done and ears else "failed", ears)

    def _finish(self, spoken: Spoken, state: str, ears: list[str]) -> None:
        said = speakable(ears, self.t)
        spoken.state = state if said else "failed"
        spoken.answer = said or self.t("voice_failed")
        spoken.ready.set()

    def _forget_spoken(self) -> None:
        old = time.time() - SPOKEN_TTL
        for sid in [k for k, v in self.spoken.items() if v.ts < old]:
            self.spoken.pop(sid, None)
        for row in [r for r, sid in self.spoken_rows.items() if sid not in self.spoken]:
            self.spoken_rows.pop(row, None)

    def hold_for(self, voice: dict) -> float:
        """How long a voice note being transcribed holds its chat's turn (VOICE_WAIT_S)."""
        seconds = voice.get("duration")
        seconds = seconds if isinstance(seconds, int | float) and seconds > 0 else 0
        cap = self.data.settings.transcription.timeout_s
        return min(self.voice_wait + VOICE_PER_S * seconds, cap)

    async def settle(self, chat_id: int) -> None:
        """Wait until the chat's newest message is DEBOUNCE_S old and no voice note of it is still
        being transcribed (until its hold_for has passed at most). A note whose transcription fails
        lets go at once."""
        while True:
            with self.qlock:
                newest = max((i.ts for i in self.queues.get(chat_id) or []), default=None)
            if newest is None:
                return
            now = self.clock()
            wait = newest + self.debounce - now
            notes = self.voicing.get(chat_id)
            if notes:
                wait = max(wait, max(notes) - now)
            if wait <= 0:
                return
            await asyncio.sleep(min(wait, 0.5))

    def rules(self) -> str:
        """The AI's instructions, with the people's Telegram handles."""
        out = context.rules(self.data)
        bot = self.data.settings.bot
        out += "\n\n" + gestures.guide("en", bot.style, bool(bot.sticker_set), bot.photo_hosts)
        handles = {p.id: self.mention(p.id) for p in self.data.household.people}
        named = [
            f"{self.data.household.person(pid).name} = {h}"
            for pid, h in handles.items()
            if h.startswith("@")
        ]
        if named:
            out += (
                "\n\n## Telegram handles\nWhen you address or refer to a person for something they "
                "must do, use their handle: " + ", ".join(named)
            )
        return out

    async def ask(
        self, client, chat_id: int, items: list[Item], rows: list[dict]
    ) -> list[Item] | None:
        """One AI answer for the queued messages `items`. A provider that keeps the conversation
        gets only what it has not seen; the household's data goes with every message, fresh.
        Messages that arrive meanwhile are handed to it; the ones its answer does not take go back
        to the queue for the next turn, unless the answer missed one of them: then nothing of it is
        sent and AGAIN is returned, so the next turn, at once, answers all of them (once in a row).
        Returns the messages the answer covered, None when there was no answer."""
        data, db = self.data, self.state.db
        if data.settings.llm.provider == "none":
            await self.send(client, chat_id, self.t("bot_no_llm"), _reply_to(items))
            return None
        key = f"bot.conversation.{chat_id}"
        conv = db.get_kv(key) or {}
        seen = conv.get("seen", 0) if conv.get("id") else 0
        done = set(conv.get("done") or []) if conv.get("id") else set()
        fresh = [
            r
            for r in rows
            if r["id"] > seen and r["id"] not in done and not (seen and r["who"] == AI)
        ]
        if conv.get("id") and not any(r["role"] == "user" for r in fresh):
            return None  # an earlier turn already took these messages
        turn_state = Turn(chat_id, items)
        self.running[chat_id] = turn_state
        reply, deferred, again = None, False, False
        rewrite = chat_id in self.again  # this turn rewrites an answer that was not sent
        self.again.discard(chat_id)
        try:
            try:
                await self.call(client, "sendChatAction", chat_id=chat_id, action="typing")
            except TelegramError as e:  # only a courtesy: the answer still goes
                log.info("typing indicator failed: %s", e)
            attachments = await self.fetch_files(client, chat_id, items)
            private = chat_id > 0
            last_item = items[-1]
            viewer = last_item.who if private else None
            facts = await asyncio.to_thread(
                context.facts, data, db, self.state.today(), viewer, not private
            )
            facts += self.chat_reminders(chat_id)
            turn = {
                "conversation": conv.get("id"),
                "rules": self.rules(),
                "context": facts,
                "new": _messages(fresh) + ([_unsent(fresh)] if rewrite else []),
                "history": _messages([r for r in rows if r not in fresh]),
                "files": await asyncio.to_thread(_files, data.root),
                "attachments": attachments,
                # the chat and the person whose message started the turn
                "chat": {"id": chat_id, "user": items[0].uid},
                "incoming": self._incoming(turn_state),
            }
            if llm.asks(data):
                turn["ask"] = self._asker(client, turn_state)
            try:
                reply = await asyncio.wait_for(
                    llm.converse(data, turn), timeout=data.settings.llm.timeout_s
                )
                # a change to the menu, a recipe or the batch days is checked, and fixed once,
                # before anyone sees it
                reply = await verifier.review(
                    data, db, self.state.today(), turn, reply, viewer, last_item.who
                )
            except Exception as e:  # noqa: BLE001
                log.warning("AI call failed: %s", safe(e))
                later = _retry_after(e)
                if later is not None:  # out of capacity: the same messages go again by themselves
                    deferred = True
                    await self.rest(client, chat_id, items, later)
                    return None
                # never recorded: the unanswered messages go again with the next one
                await self.send(
                    client, chat_id, self.t("bot_error"), _reply_to(items), record=False
                )
                return None
        finally:
            with self.qlock:
                turn_state.closed = True  # a provider thread that outlives the turn takes nothing
                missed = [
                    i for i in turn_state.pulled if reply is None or i.row not in reply.covers
                ]
                again = reply is not None and bool(missed) and not rewrite
                if deferred:  # the whole turn waits for the provider, in its order
                    self.queues[chat_id] = [*items, *missed, *self.queues.get(chat_id, [])]
                elif again:  # nothing of this answer goes out: the next turn takes all of it
                    self.queues[chat_id] = [
                        *items,
                        *turn_state.pulled,
                        *self.queues.get(chat_id, []),
                    ]
                    self.again.add(chat_id)
                elif reply is not None and missed:  # the next turn sends them, at the head
                    self.queues[chat_id] = [*missed, *self.queues.get(chat_id, [])]
            self.running.pop(chat_id, None)
            await self.close_asks(client, turn_state)
        if again:
            # the same messages go again as new; a provider that keeps the conversation also has
            # this answer, and the next turn tells it the chat never saw it
            if reply.conversation:
                db.set_kv(key, {"id": reply.conversation, "seen": seen, "done": sorted(done)})
            else:
                db.set_kv(key, None)
            log.info("an answer missed messages that came while it was written: writing it again")
            return AGAIN
        self.told_busy.discard(chat_id)  # answered: a later busy spell is told again
        covered = [*items, *(i for i in turn_state.pulled if i.row in reply.covers)]
        reply_to = _reply_to(covered)
        anchor = covered[-1]  # changes and proposals act for the newest message's sender
        text, change = changes.extract(reply.text)
        text, acts = gestures.extract(text, data.settings.bot.photo_hosts, self.t("file_name"))
        text, offered = llm.extract_options(text)
        offered = reply.options or offered
        text, listed = llm.extract_reminders(text)
        listed = reply.reminders or listed
        acted = acts["react"] or acts["sticker"] or acts["photos"] or acts["documents"]
        if not text and change is None and not reply.proposals and not listed and not acted:
            await self.send(client, chat_id, self.t("bot_error"), reply_to, record=False)
            return None
        if text:
            await self.answer_text(client, chat_id, text, reply_to, offered, covered)
        if acted:
            said = text or change is not None or reply.proposals or listed  # each says something
            await self.gesture(client, chat_id, acts, anchor.mid, bool(said), reply_to)
        if reply.conversation:
            last = max((r["id"] for r in rows), default=seen)
            if reply.kept:
                took = {i.row for i in covered if i.row > last}
                done = sorted({d for d in done if d > last} | took)
                db.set_kv(key, {"id": reply.conversation, "seen": last, "done": done})
            else:
                db.set_kv(key, {"id": reply.conversation, "seen": seen, "done": sorted(done)})
        else:
            db.set_kv(key, None)
        made = True  # a reminder made wrong by a change goes only once the change is made
        if change is not None:
            made = await self.propose(client, chat_id, anchor.mid, anchor.who, anchor.uid, change)
        for proposal in reply.proposals:
            await self.offer(client, chat_id, anchor.mid, anchor.who, anchor.uid, proposal)
        if listed:
            if not made:
                listed = [r for r in listed if "cancel" not in r]
            await self.offer_reminders(client, chat_id, listed, covered, reply_to)
        return covered

    async def rest(self, client, chat_id: int, items: list[Item], seconds: float) -> None:
        """The provider is out of capacity for `seconds`: say so once, with the wait, and answer
        the chat's messages by itself when it is back. A message that arrives meanwhile waits too."""
        seconds = max(float(seconds), BUSY_MIN_S)
        self.resting[chat_id] = self.clock() + seconds
        if chat_id not in self.told_busy:
            self.told_busy.add(chat_id)
            minutes = max(1, round(seconds / 60))
            wait = (
                self.t("busy_minutes", n=minutes)
                if minutes < 90
                else self.t("busy_hours", n=round(minutes / 60))
            )
            await self.send(
                client, chat_id, self.t("bot_busy", wait=wait), _reply_to(items), record=False
            )
        self.spawn(self.wake(client, chat_id, seconds))

    async def wake(self, client, chat_id: int, seconds: float) -> None:
        await asyncio.sleep(seconds)
        if self.resting.get(chat_id, 0) > self.clock():
            return  # a later rest set a later time and woke itself
        self.resting.pop(chat_id, None)
        await self.drive(client, chat_id)

    async def gesture(self, client, chat_id, acts, to, texted, reply_to=None) -> None:
        """What the answer's gestures block asks for (gestures.py), each on its own after the text,
        so a file or a reaction Telegram refuses never loses the answer. A reaction goes on message
        `to`. When the answer said nothing and none of it reached the chat (no message to react to,
        a refusal, no sticker set), the emoji is said instead, or that something went wrong."""
        shown = texted
        sends = [
            (
                "sendDocument",
                {"caption": d["caption"]},
                {"document": (d["name"], d["data"], d["mime"])},
            )
            for d in acts["documents"]
        ]
        sends += [
            ("sendPhoto", {"photo": p["url"], "caption": p["caption"]}, None)
            for p in acts["photos"]
        ]
        sticker = await self.sticker(client, acts["sticker"])
        if sticker:
            sends.append(("sendSticker", {"sticker": sticker}, None))
        for method, params, files in sends:
            try:
                await self.call(client, method, _files=files, chat_id=chat_id, **params)
                shown = True
            except TelegramError as e:
                log.warning("%s failed: %s", method, e)
        if acts["react"] and to:
            try:
                reaction = [{"type": "emoji", "emoji": acts["react"]}]
                await self.call(
                    client, "setMessageReaction", chat_id=chat_id, message_id=to, reaction=reaction
                )
                return
            except TelegramError as e:
                log.info("reaction failed: %s", e)
        if shown:
            return
        said = acts["react"] or acts["sticker"]
        if said:
            await self.send(client, chat_id, said, reply_to, who=AI)
        else:
            await self.send(client, chat_id, self.t("bot_error"), reply_to, record=False)

    async def sticker(self, client, emoji: str | None) -> str | None:
        """The file_id of the settings' sticker set (bot.sticker_set) for `emoji`, read once."""
        name = self.data.settings.bot.sticker_set
        if not emoji or not name:
            return None
        if self.stickers.get("name") != name:
            try:
                self.stickers = {
                    "name": name,
                    "set": await self.call(client, "getStickerSet", name=name),
                }
            except TelegramError as e:
                log.warning("sticker set failed: %s", e)
                return None
        return gestures.sticker_for(self.stickers.get("set"), emoji)

    async def answer_text(self, client, chat_id, text, reply_to, offered, covered) -> None:
        """The answer, with the choices it offers as buttons under it. A long one shows its first
        lines and folds the rest; a voice listener still hears all of it."""
        shown = render.brief(text)
        if not offered:
            await self.send(client, chat_id, shown, reply_to, markdown=True, who=AI, spoken=text)
            return
        key = secrets.token_hex(4)
        rows = [[(label, f"o:{key}:{n}")] for n, label in enumerate(offered)]
        sent = await self.send(
            client,
            chat_id,
            f"{shown}\n\n_{self.t('options_hint')}_",
            reply_to,
            markdown=True,
            who=AI,
            buttons=rows,
            spoken=f"{text}\n\n{self.t('voice_options', choices=_either(self.t, offered))}",
        )
        users = list(dict.fromkeys(i.uid for i in covered if i.uid))
        kept = [
            o
            for o in self.state.db.get_kv(OPTIONS.format(chat_id)) or []
            if time.time() - o.get("ts", 0) <= OPTIONS_TTL
        ]
        item = {"key": key, "msg": sent, "labels": offered, "for": users, "ts": time.time()}
        self.state.db.set_kv(OPTIONS.format(chat_id), [*kept, item][-OPTIONS_KEEP:])

    def _incoming(self, turn_state: Turn):
        """turn["incoming"]: the chat's messages queued since the last call, for the provider's
        thread. Once the turn is over it hands over nothing, so a late call steals nothing from the
        next turn. A photo or a file stays queued: the next turn carries its bytes."""
        db, chat_id = self.state.db, turn_state.chat

        def incoming() -> list[dict]:
            with self.qlock:
                if turn_state.closed:
                    return []
                queue = self.queues.pop(chat_id, [])
                got = [i for i in queue if not i.file]
                if len(got) < len(queue):
                    self.queues[chat_id] = [i for i in queue if i.file]
                turn_state.pulled.extend(got)
            rows = {r["id"]: r for r in db.chat_by_ids(chat_id, [i.row for i in got])}
            return [
                {
                    "id": i.row,
                    "role": "user",
                    "content": f"{rows[i.row]['who'] or 'someone'}: {rows[i.row]['text']}",
                    "ts": rows[i.row]["ts"],
                }
                for i in got
                if i.row in rows
            ]

        return incoming

    # ------------------------------------------------------------- settings changes
    async def propose(self, client, chat_id, reply_to, asker, user_id, change) -> bool:
        """Make a change the person asked for at once; show one the AI came up with on its own and
        wait for a yes from the person it answered. True when the change is made now."""
        t = self.t
        if isinstance(change, str):
            await self.send(client, chat_id, t("change_invalid", error=change), reply_to)
            return False
        if asker is None or user_id is None:
            await self.send(client, chat_id, t("change_members_only"), reply_to)
            return False
        try:
            proposal = changes.parse(change, self.data, asker)
        except changes.ChangeError as e:
            await self.send(client, chat_id, t("change_invalid", error=e), reply_to)
            return False
        name = self.data.household.person(asker).name
        if proposal.asked:
            item = {"change": change, "asker": asker, "name": name}
            before = await self.problem_keys()
            await self.send(client, chat_id, await self.apply(item, asked=True), reply_to)
            if item.get("applied"):
                # a change still waiting for a yes on what this one just set is outdated now
                await self.unwait(
                    client, chat_id, self.wait_for(chat_id, None, self.outdates(proposal))
                )
                await self.checkup(client, chat_id, before, reply_to)
            return bool(item.get("applied"))
        lines = changes.describe(proposal, self.data, t)
        msgs: list[int] = []
        sent = await self.send(
            client,
            chat_id,
            t("change_confirm", name=self.mention(asker), lines="\n".join(lines)),
            reply_to,
            ids=msgs,
            buttons=self.yes_no(key := secrets.token_hex(4)),
            spoken=t("voice_change_confirm", lines="\n".join(lines)),
        )
        item = {
            "key": key,
            "change": change,
            "by": [user_id],
            "asker": asker,
            "name": name,
            "ts": time.time(),
            "msg": sent,
            "msgs": msgs,
        }
        # a newer change from the same person replaces their earlier one ("no, better at 15:00"),
        # and so does one on the same things from anyone
        outdated = self.outdates(proposal)
        gone = self.wait_for(
            chat_id, item, lambda i: ("change" in i and i.get("asker") == asker) or outdated(i)
        )
        await self.unwait(client, chat_id, gone)
        return False

    def pending(self, chat_id: int) -> list[dict]:
        """What waits for a yes in this chat, oldest first, without what has expired. A change 0.3.x
        left under the old key moves to the new one."""
        db, key, old_key = self.state.db, WAITING.format(chat_id), LEGACY_PENDING.format(chat_id)
        raw = db.get_kv(key)
        items = list(raw) if isinstance(raw, list) else []
        old = db.get_kv(old_key)
        if old is not None:
            items += old if isinstance(old, list) else [old] if isinstance(old, dict) else []
            db.set_kv(old_key, None)
        live = [i for i in items if time.time() - i.get("ts", 0) <= PENDING_TTL]
        if live != raw and (raw is not None or live):
            db.set_kv(key, live or None)
        return live

    def wait_for(self, chat_id: int, item: dict | None, replaces) -> list[dict]:
        """Keep an item waiting next to the others (None: no new one); `replaces(other)` drops one
        it supersedes. Returns the ones dropped, whose buttons unwait() takes away."""
        items = self.pending(chat_id)
        gone = [i for i in items if replaces(i)]
        kept = [*(i for i in items if i not in gone), *([item] if item is not None else [])]
        if gone or item is not None:
            self.state.db.set_kv(WAITING.format(chat_id), kept or None)
        return gone

    async def unwait(self, client, chat_id: int, gone: list[dict]) -> None:
        """What a newer answer outdated no longer waits: its message says so instead of buttons,
        so nobody taps a change that no longer fits."""
        for i in gone:
            await self.unbutton(client, chat_id, i.get("msg"), self.t("change_replaced"))

    def outdates(self, proposal):
        """`replaces` for wait_for: a waiting change that sets any of what `proposal` sets."""
        new = changes.touches(proposal)

        def replaces(item: dict) -> bool:
            if "change" not in item:
                return False
            try:
                old = changes.parse(item["change"], self.data, item.get("asker"))
            except changes.ChangeError:
                return False
            return bool(new & changes.touches(old))

        return replaces

    def drop(self, chat_id: int, item: dict) -> None:
        items = [i for i in self.pending(chat_id) if i != item]
        self.state.db.set_kv(WAITING.format(chat_id), items or None)
        return False

    async def answer_pending(self, client, chat_id: int, user: dict, msg: dict, text: str) -> bool:
        """A yes or no from the person a change waits for. True when the message was that answer.
        With several waiting for the same person, the answer has to reply to the one it means."""
        mine = []
        for item in self.pending(chat_id):
            by = item.get("by")
            if user.get("id") in (by if isinstance(by, list) else [by]):
                mine.append(item)
        if not mine:
            return False
        # in the group, only an answer addressed to the bot: an "ok" replying to someone else is
        # their conversation, not a yes
        addressed = self.replied(msg) or self.mentioned(text) or SPOKEN in msg
        if msg["chat"].get("type") != "private" and not addressed:
            return False
        if self.username:  # "@chefbot sí" is the answer too
            text = re.sub(rf"@{re.escape(self.username)}\b", " ", text, flags=re.IGNORECASE)
        # only a message that is nothing but the answer: "sí", "vale, sí", "no". "si quieres..."
        # or "no sé qué cenar" are other messages and leave the change waiting
        words = re.sub(r"[^\w\s]", " ", text.lower()).split()
        yes = bool(words) and set(words) <= set(self.t.raw("change_yes_words") or [])
        no = bool(words) and set(words) <= set(self.t.raw("change_no_words") or [])
        if not (yes or no):
            return False
        to = (msg.get("reply_to_message") or {}).get("message_id") if self.replied(msg) else None
        meant = [i for i in mine if to is not None and to in (i.get("msgs") or [i.get("msg")])]
        if to is not None and not meant:
            # a change 0.3.x left waiting never kept the id of its message: any reply to the bot
            # may be the yes to it
            meant = [i for i in mine if i.get("msg") is None and not i.get("msgs")]
        if to is not None and not meant:
            # a yes to another of the bot's messages (someone else's offer, an answer) is not a
            # yes to what waits for this person: it settles nothing
            return False
        if not meant and len(mine) > 1:
            await self.send(client, chat_id, self.t("pending_which"), msg["message_id"])
            return True
        pending = (meant or mine)[0]
        who = self.person_of(user.get("id", 0)) or user.get("first_name")
        self.state.db.add_chat(
            chat_id, "user", who, text.strip(), mid=msg["message_id"], asked=True
        )
        await self.settle_pending(client, chat_id, pending, yes, msg["message_id"])
        return True

    async def settle_pending(
        self, client, chat_id: int, pending: dict, yes: bool, reply_to
    ) -> None:
        """Carry out the answer to what waited (typed or tapped): its buttons go first, so it is
        answered once."""
        self.drop(chat_id, pending)
        await self.unbutton(client, chat_id, pending.get("msg"))
        if yes and "proposal" in pending:
            await self.act(client, chat_id, reply_to, pending)
            return
        before = await self.problem_keys() if yes else set()
        reply = await self.apply(pending) if yes else self.t("change_cancelled")
        await self.send(client, chat_id, reply, reply_to)
        if pending.get("applied"):
            await self.checkup(client, chat_id, before, reply_to)

    # ------------------------------------------------------------- buttons
    def yes_no(self, key: str) -> list[list[tuple[str, str]]]:
        return [[(self.t("button_yes"), f"c:{key}:y"), (self.t("button_no"), f"c:{key}:n")]]

    async def unbutton(self, client, chat_id: int, message_id, label: str | None = None) -> None:
        """Take the buttons off a message (best effort); label: one inert button saying what was
        chosen instead."""
        if not message_id:
            return
        markup = keyboard([[(label, "x")]]) if label else {"inline_keyboard": []}
        try:
            await self.call(
                client,
                "editMessageReplyMarkup",
                chat_id=chat_id,
                message_id=message_id,
                reply_markup=markup,
            )
        except TelegramError as e:
            log.info("could not take the buttons off a message: %s", e)

    async def toast(self, client, query: dict, text: str | None = None) -> None:
        params = {"callback_query_id": query["id"]}
        if text:
            params["text"] = text
        try:
            await self.call(client, "answerCallbackQuery", **params)
        except TelegramError as e:
            log.info("could not answer a tap: %s", e)

    async def tapped(self, client, query: dict) -> None:
        """A tap on one of the bot's buttons: a yes or no to a change or a proposal (c:), to an ask
        of the provider (a:), one of the choices under an answer (o:), where a reminder offered
        goes (r:), or the cancel of a reminder set (k:). Only the person asked (or
        an admin, as the item says) may tap; anyone else gets a note and nothing changes."""
        user, message = query.get("from") or {}, query.get("message") or {}
        chat = message.get("chat") or {}
        kind, _, rest = str(query.get("data") or "").partition(":")
        key, _, choice = rest.partition(":")
        if (
            kind not in ("c", "a", "o", "r", "k")
            or not chat
            or not self.allowed(chat, user.get("id", 0))
        ):
            await self.toast(client, query)
            return
        if kind == "c":
            await self.tapped_confirm(client, query, chat["id"], key, choice == "y")
        elif kind == "a":
            await self.tapped_ask(client, query, chat["id"], key, choice == "y")
        elif kind == "r":
            await self.tapped_remind(client, query, chat, key, choice)
        elif kind == "k":
            await self.tapped_cancel(client, query, chat["id"], key, choice)
        else:
            await self.tapped_option(client, query, chat, key, choice)

    async def tapped_confirm(self, client, query, chat_id: int, key: str, yes: bool) -> None:
        user = query.get("from") or {}
        mid = (query.get("message") or {}).get("message_id")
        item = next((i for i in self.pending(chat_id) if i.get("key") == key), None)
        if item is None:
            await self.toast(client, query, self.t("tap_gone"))
            await self.unbutton(client, chat_id, mid)
            return
        by = item.get("by")
        if user.get("id") not in (by if isinstance(by, list) else [by]):
            names = ", ".join(self.mention(p) for p in self._people(by))
            await self.toast(client, query, self.t("tap_not_yours", name=names))
            return
        self.drop(chat_id, item)  # before any await: a second tap finds nothing
        await self.toast(client, query)
        who = self.person_of(user.get("id", 0)) or user.get("first_name")
        word = self.t("button_yes" if yes else "button_no")
        self.state.db.add_chat(chat_id, "user", who, word, asked=True)  # as a typed answer is
        await self.settle_pending(client, chat_id, item, yes, mid)

    def _people(self, ids) -> list[str]:
        ids = ids if isinstance(ids, list) else [ids]
        return [pid for pid, uid in self.data.settings.bot.people.items() if uid in ids]

    async def tapped_option(self, client, query, chat: dict, key: str, choice: str) -> None:
        """A choice under an answer: that person's next message, as if typed. The buttons give way
        to the one chosen."""
        user = query.get("from") or {}
        mid = (query.get("message") or {}).get("message_id")
        db, kv = self.state.db, OPTIONS.format(chat["id"])
        offers = db.get_kv(kv) or []
        item = next((o for o in offers if o.get("key") == key), None)
        if item is None or time.time() - item.get("ts", 0) > OPTIONS_TTL:
            await self.toast(client, query, self.t("tap_gone"))
            await self.unbutton(client, chat["id"], mid)
            return
        if item.get("for") and user.get("id") not in item["for"]:
            names = ", ".join(self.mention(p) for p in self._people(item["for"]))
            await self.toast(client, query, self.t("tap_not_yours", name=names))
            return
        labels = item.get("labels") or []
        if not choice.isdigit() or int(choice) >= len(labels):
            await self.toast(client, query)
            return
        label = labels[int(choice)]
        db.set_kv(kv, [o for o in offers if o.get("key") != key] or None)  # chosen once
        await self.toast(client, query)
        await self.unbutton(client, chat["id"], mid, f"✓ {label}")
        who = self.person_of(user.get("id", 0))
        # the answer to it replies to the question the choice came from
        self.take(chat["id"], user, who, {"message_id": mid}, label, asked=True)
        await self.drive(client, chat["id"])

    # ------------------------------------------------------------- the provider's asks
    def _asker(self, client, turn_state: Turn):
        """turn["ask"]: the provider's thread asks a person for a yes; the bot posts it."""
        loop = asyncio.get_running_loop()

        def ask(request: dict) -> None:
            def post():
                task = asyncio.create_task(self.quietly(self.pose(client, turn_state, request)))
                self.tasks.add(task)
                task.add_done_callback(self.tasks.discard)

            loop.call_soon_threadsafe(post)

        return ask

    def _approvers(self, turn_state: Turn, confirm_by) -> list[int]:
        """Who may answer an ask: the person the turn answers when that person is in the household,
        or the admins when it is asked of them, when the turn carries several people's messages, or
        when the asker is a stranger let in by bot.group_open."""
        users = turn_state.users()
        if confirm_by == "asker" and len(users) == 1 and self.person_of(users[0]) is not None:
            return users
        cfg = self.data.settings.bot
        return [cfg.people[pid] for pid in cfg.admins if pid in cfg.people]

    async def pose(self, client, turn_state: Turn, request: dict) -> None:
        """Post an ask with Approve and Reject. One that nobody can answer is declined at once, so
        the provider never waits for nothing."""
        if not isinstance(request, dict) or not isinstance(request.get("id"), str):
            return
        chat_id, summary = turn_state.chat, str(request.get("summary") or "").strip()
        if any(a.get("request", {}).get("id") == request["id"] for a in self.asks.values()):
            return  # asked already
        expires = request.get("expires_at")
        expires = float(expires) if isinstance(expires, int | float) else time.time() + 300
        grant = next((g for g in turn_state.grants() if not g.get("used")), None)
        if grant is not None and not turn_state.closed and summary and expires > time.time():
            grant["used"] = True  # the turn's first ask uses it up, whatever that ask is
            when = request.get("when")
            if (
                request.get("action") == "add"
                and isinstance(when, int | float)
                and abs(when - grant["at"]) < 60
            ):
                await self.preapproved(client, turn_state, request, grant, summary)
                return
        ids = self._approvers(turn_state, request.get("confirm_by"))
        if turn_state.closed or not ids or not summary or expires <= time.time():
            await self.decline(request, "no one could be asked")
            return
        key = secrets.token_hex(4)
        names = ", ".join(self.mention(p) for p in self._people(ids))
        try:
            sent = await self.send(
                client,
                chat_id,
                self.t("ask_confirm", name=names, summary=summary),
                turn_state.items[-1].mid,
                record=False,
                spoken=self.t("voice_ask", summary=summary),
                buttons=[
                    [
                        (self.t("button_approve"), f"a:{key}:y"),
                        (self.t("button_reject"), f"a:{key}:n"),
                    ]
                ],
            )
        except TelegramError as e:
            log.warning("could not post an ask: %s", e)
            await self.decline(request, "no one could be asked")
            return
        self.asks[key] = {
            "request": request,
            "by": ids,
            "chat": chat_id,
            "msg": sent,
            "expires": expires,
        }
        if turn_state.closed:  # the turn ended while it was posted
            await self.close_asks(client, turn_state, [key])
        else:
            turn_state.asks.append(key)

    async def preapproved(self, client, turn_state: Turn, request, grant, summary) -> None:
        """The ask a reminder's "in the calendar" tap asked for: approved by that tap, and the
        chat sees what was approved and how it went."""
        try:
            out = await llm.answer(
                self.data,
                request,
                {
                    "approved": True,
                    "by": str(grant["by"]),
                    "note": "approved with the reminder's calendar button",
                },
            )
        except Exception as e:  # noqa: BLE001
            log.warning("a preapproved ask failed: %s", safe(e))
            out = {"ok": False, "error": type(e).__name__}
        said = (
            self.t("ask_done")
            if out.get("ok")
            else self.t("ask_failed", error=str(out.get("error") or "?")[:300])
        )
        await self.send(
            client,
            turn_state.chat,
            f"{self.t('ask_preapproved', summary=summary)}\n{said}",
            turn_state.items[-1].mid,
            record=False,
        )

    async def decline(self, request: dict, note: str, by: str = "bot") -> dict:
        try:
            return await llm.answer(self.data, request, {"approved": False, "by": by, "note": note})
        except Exception as e:  # noqa: BLE001  the ask expires on the provider's side anyway
            log.warning("could not decline an ask: %s", safe(e))
            return {"ok": None, "error": None}

    async def tapped_ask(self, client, query, chat_id: int, key: str, yes: bool) -> None:
        user = query.get("from") or {}
        item = self.asks.get(key)
        mid = (query.get("message") or {}).get("message_id")
        if item is None or item["expires"] <= time.time():
            self.asks.pop(key, None)
            await self.toast(client, query, self.t("tap_gone"))
            await self.unbutton(client, chat_id, mid)
            return
        if user.get("id") not in item["by"]:
            names = ", ".join(self.mention(p) for p in self._people(item["by"]))
            await self.toast(client, query, self.t("tap_not_yours", name=names))
            return
        self.asks.pop(key)  # answered once
        await self.toast(client, query)
        label = self.t("button_approve" if yes else "button_reject")
        await self.unbutton(client, chat_id, item["msg"], f"✓ {label}")
        try:
            out = await llm.answer(
                self.data,
                item["request"],
                {"approved": yes, "by": str(user.get("id")), "note": None},
            )
        except Exception as e:  # noqa: BLE001
            log.warning("an ask's answer failed: %s", safe(e))
            out = {"ok": False, "error": type(e).__name__}
        if not yes:
            return  # the turn's answer says what came of it
        said = (
            self.t("ask_done")
            if out.get("ok")
            else self.t("ask_failed", error=str(out.get("error") or "?")[:300])
        )
        await self.send(client, chat_id, said, item["msg"], record=False)

    async def close_asks(self, client, turn_state: Turn, keys=None, note=None) -> None:
        """The turn ended (or a message of it was edited): its open asks lose their buttons. With
        a note, each is declined with it first and the chat hears why."""
        for key in keys if keys is not None else list(turn_state.asks):
            item = self.asks.pop(key, None)
            if key in turn_state.asks:
                turn_state.asks.remove(key)
            if item is None:
                continue
            if note:
                await self.decline(item["request"], note)
            await self.unbutton(client, item["chat"], item["msg"])
            if note:
                await self.send(
                    client, item["chat"], self.t("ask_edited"), item["msg"], record=False
                )

    async def expire_asks(self, client) -> None:
        for key, item in list(self.asks.items()):
            if item["expires"] <= time.time():
                self.asks.pop(key, None)
                await self.unbutton(client, item["chat"], item["msg"])

    # ------------------------------------------------------------- edits
    async def edited(self, client, msg: dict) -> None:
        """An edited message, handled by where it is (see the module's docstring). It is never a
        yes or a no, and never runs a command."""
        chat, user = msg["chat"], msg.get("from", {})
        text = (msg.get("text") or "").strip()
        if not text or text.startswith("/") or not self.allowed(chat, user.get("id", 0)):
            return
        chat_id, db = chat["id"], self.state.db
        who = self.person_of(user.get("id", 0))
        name = self.data.household.person(who).name if who else user.get("first_name") or "?"
        calls = chat.get("type") == "private" or self.mentioned(text) or self.replied(msg)
        text = self.reply_tag(msg) + text  # as it was stored
        row = db.find_chat(chat_id, msg["message_id"])
        if row is None:  # never stored: a new message if it now calls the bot
            if calls:
                self.take(chat_id, user, who, msg, text, asked=True)
                await self.drive(client, chat_id)
            return
        before, now = self.clean(row["text"]), self.clean(text)
        asked = bool(row["asked"]) if row["asked"] is not None else calls
        if before == now and (asked or not calls):
            return  # formatting only: nothing changes, not even who it calls
        turn_state = self.running.get(chat_id)
        with self.qlock:
            queued = next((i for i in self.queues.get(chat_id, []) if i.row == row["id"]), None)
            if queued is not None:  # still waiting: the new text goes, the debounce starts over
                queued.ts = self.clock()
        if queued is not None:
            db.edit_chat(row["id"], text)
            return
        if turn_state is not None and row["id"] in turn_state.rows():
            db.edit_chat(row["id"], text)
            note = (
                f"[{name} edited a message of this turn. Reconsider your answer with the new "
                f"wording.]\nBefore: {before}\nNow: {now}"
            )
            # queued before any await: the running turn takes it, or the chat's loop runs it next
            self.take(chat_id, user, who, None, note, asked=True)
            await self.close_asks(client, turn_state, note=f"editado: {now}")
            await self.drive(client, chat_id)  # the turn may have ended meanwhile
            return
        if not asked:
            if not calls:
                db.edit_chat(row["id"], text)
                return
            # the edit adds the mention: a new message to answer. Its row moves to now, so a turn
            # that already took the old wording as context does not count it as seen
            db.delete_chat(row["id"])
            self.take(chat_id, user, who, msg, text, asked=True)
            await self.drive(client, chat_id)
            return
        db.edit_chat(row["id"], text)
        said = (db.answer_after(chat_id, row["id"]) or "")[:1500]
        note = (
            f"[{name} edited a message you already answered. Check whether your answer still "
            f"holds with the new wording, and say what changes.]\nBefore: {before}\nNow: {now}"
            + (f"\nYour answer was: {said}" if said else "")
        )
        self.take(chat_id, user, who, msg, note, asked=True, log_mid=False)
        await self.drive(client, chat_id)

    def clean(self, text: str) -> str:
        """A message's words without the bot's @handle."""
        if self.username:
            text = re.sub(rf"@{re.escape(self.username)}\b", " ", text, flags=re.IGNORECASE)
        return " ".join(text.split())

    async def apply(self, pending: dict, asked: bool = False) -> str:
        """Make a change and say what came of it. Settings and recipes are one git commit in the
        data repo; menu changes are written under state/. asked: nobody saw the change yet, so the
        answer lists it."""
        t = self.t
        try:
            # the data may have moved on since the proposal
            proposal = changes.parse(pending["change"], self.data, pending.get("asker"))
        except changes.ChangeError as e:
            return t("change_invalid", error=e)
        lines = changes.describe(proposal, self.data, t)
        out, sha = [], None
        retired = changes.retired(proposal, self.data)
        if proposal.edits or proposal.recipe is not None or proposal.recipes or proposal.notes:
            token = os.environ.get("SMP_GIT_WRITE_TOKEN", "")
            if not token:
                by_hand = t("change_manual") + "\n\n" + "\n".join(changes.manual(proposal))
                if not (proposal.plan or proposal.pantry):
                    return by_hand
                out.append(by_hand)
            else:
                saved = lines[: len(lines) - len(proposal.plan) - len(proposal.pantry)]
                # the subject names what changed; the body has the steps too
                subject = "; ".join(
                    x.removeprefix("• ").rstrip(":") for x in saved if x.startswith("• ")
                )
                how = "" if asked else " and confirmed there"
                message = (
                    f"chat: {subject[:120]}\n\nAsked by {pending.get('name', '?')} in the chat"
                    f"{how}.\n\n" + "\n".join(saved) + "\n"
                )
                try:
                    async with self.writing:
                        sha = await asyncio.to_thread(
                            changes.apply, self.data.root, proposal, message, token
                        )
                        await asyncio.to_thread(self.state.reload)
                except changes.ChangeError as e:
                    log.warning("settings change failed: %s", e)
                    return t("change_failed", error=e)
                except Exception as e:  # noqa: BLE001
                    log.warning("settings change failed: %s", safe(e))
                    return t("change_failed", error=type(e).__name__)
                out.append(t("change_saved" if asked else "change_applied", sha=sha))
        if retired and sha:
            # the recipe left a meal: the days already planned from tomorrow on lose it too
            try:
                start = self.state.today() + dt.timedelta(days=1)
                n = await asyncio.to_thread(menu.retire, self.data, retired, start)
            except Exception as e:  # noqa: BLE001  the recipe is saved; the plan keeps it for now
                log.warning("replacing a retired recipe failed: %s", safe(e))
                n = None
            if n is None:
                out.append(t("recipe_replan_failed"))
            elif n:
                out.append(t("recipe_replanned", n=n))
        if proposal.plan:
            try:
                async with self.writing:
                    await asyncio.to_thread(
                        menu.apply, self.data, proposal.plan, self.state.today()
                    )
            except menu.MenuError as e:
                return "\n".join([*out, t("change_invalid", error=e)])
            except Exception as e:  # noqa: BLE001
                log.warning("menu change failed: %s", safe(e))
                return "\n".join([*out, t("change_failed", error=type(e).__name__)])
            out.append(t("menu_applied"))
        if proposal.pantry:  # the database, like the menu: no token needed
            await asyncio.to_thread(pantry.correct, self.data, self.state.db, proposal.pantry)
            out.append(t("pantry_applied"))
        if asked:
            out.insert(0, t("change_done", lines="\n".join(lines)))
        pending["applied"] = True  # every way it failed returned above
        return "\n".join(out)

    # ------------------------------------------------------------- proposals
    async def offer(self, client, chat_id, reply_to, asker, user_id, proposal: dict) -> None:
        """Show something the plugin would do outside the chat and wait for the right yes: the
        asker's, or an admin's. A step that changes nothing runs at once, in the background."""
        t, cfg = self.t, self.data.settings.bot
        if asker is None or user_id is None:
            await self.send(client, chat_id, t("proposal_members_only"), reply_to)
            return
        pending = {
            "proposal": proposal,
            "asker": asker,
            "user": user_id,
            "name": self.data.household.person(asker).name,
            "ts": time.time(),
        }
        if proposal["confirm_by"] == "none":
            await self.send(client, chat_id, proposal["summary"], reply_to)
            task = asyncio.create_task(self.act(client, chat_id, reply_to, pending))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
            return
        if proposal["confirm_by"] == "admins":
            admins = [pid for pid in cfg.admins if pid in cfg.people]
            ids = [cfg.people[pid] for pid in admins]
            names = ", ".join(self.mention(pid) for pid in admins)
        else:
            ids, names = [user_id], self.mention(asker)
        if not ids:
            await self.send(client, chat_id, t("proposal_no_admins"), reply_to)
            return
        where, answer_to = chat_id, reply_to
        if chat_id > 0 and user_id not in ids:
            # nobody else can write in this private chat: the admins answer in the group
            if self.chat_id is None:
                await self.send(client, chat_id, t("proposal_admins_no_group"), reply_to)
                return
            where, answer_to = self.chat_id, None
        msgs: list[int] = []
        key = secrets.token_hex(4)
        sent = await self.send(
            client,
            where,
            t("proposal_confirm", name=names, summary=proposal["summary"]),
            answer_to,
            ids=msgs,
            buttons=self.yes_no(key),
            spoken=t("voice_proposal_confirm", summary=proposal["summary"]),
        )
        if where != chat_id:
            await self.send(client, chat_id, t("proposal_in_group", name=names), reply_to)
        item = {**pending, "key": key, "by": ids, "msg": sent, "msgs": msgs}
        gone = self.wait_for(
            where, item, lambda i: (i.get("proposal") or {}).get("id") == proposal["id"]
        )
        await self.unwait(client, where, gone)

    async def act(self, client, chat_id: int, reply_to: int, pending: dict) -> None:
        """Have the plugin carry out a proposal and post what came of it. Follow-ups are offered on
        behalf of the same asker. While it runs it is written down, so a restart can say it was
        cut off instead of leaving the asker waiting."""
        mark = {"id": uuid.uuid4().hex, "chat": chat_id, "reply_to": reply_to, "ts": time.time()}
        db = self.state.db
        db.set_kv(RUNNING, [*(db.get_kv(RUNNING) or []), mark])
        try:
            await self._act(client, chat_id, reply_to, pending)
        except asyncio.CancelledError:
            raise  # the bot is stopping: the mark stays, and the next start says so
        except BaseException:
            self._unmark(mark)
            raise
        self._unmark(mark)

    def _unmark(self, mark: dict) -> None:
        db = self.state.db
        left = [m for m in db.get_kv(RUNNING) or [] if m.get("id") != mark["id"]]
        db.set_kv(RUNNING, left or None)

    async def interrupted(self, client) -> None:
        """Once at start: tell each chat whose proposal was running when the bot stopped that it
        was cut off, so the asker knows to ask again."""
        db = self.state.db
        for m in db.get_kv(RUNNING) or []:
            try:
                await self.send(
                    client,
                    m["chat"],
                    self.t("proposal_interrupted"),
                    m.get("reply_to"),
                    record=False,
                )
            except Exception as e:  # noqa: BLE001  best effort, like any other notice
                log.warning("could not report an interrupted proposal: %s", safe(e))
        db.set_kv(RUNNING, None)

    async def _act(self, client, chat_id: int, reply_to: int, pending: dict) -> None:
        try:
            out = await asyncio.wait_for(
                llm.act(self.data, pending["proposal"]), timeout=ACT_TIMEOUT
            )
            if out.text:
                try:
                    await self.send(client, chat_id, out.text, reply_to)
                except TelegramError as e:  # what follows still runs: the work is already done
                    log.warning("could not say how a proposal went: %s", safe(e))
            for proposal in out.proposals:
                await self.offer(
                    client, chat_id, reply_to, pending["asker"], pending.get("user"), proposal
                )
        except Exception as e:  # noqa: BLE001
            log.warning("a proposal failed: %s", safe(e))
            said = (
                self.t("proposal_unsupported")
                if isinstance(e, llm.NoAct)
                else self.t("proposal_failed", error=safe(e))
            )
            try:
                await self.send(
                    client,
                    chat_id,
                    said,
                    reply_to,
                    record=False,
                )
            except Exception as e2:  # noqa: BLE001  the reply is best effort
                log.warning("could not report the failed proposal: %s", safe(e2))

    async def check_group(self, client, me: dict) -> None:
        """Say once at start when the group's mentions cannot reach the bot."""
        extra = self.data.settings.bot.model_extra or {}
        if extra.get("respond") == "all" or extra.get("wake_words"):
            log.warning(
                "bot.respond and bot.wake_words are gone: in a group the bot answers only its "
                "@handle and replies to it"
            )
        if self.chat_id is None or me.get("can_read_all_group_messages"):
            return
        try:
            m = await self.call(client, "getChatMember", chat_id=self.chat_id, user_id=me.get("id"))
        except TelegramError as e:
            log.info("could not check the bot's group rights: %s", e)
            return
        if m.get("status") not in ("administrator", "creator"):
            log.warning(
                "privacy mode is on and the bot is not an admin of group %s: it sees only commands "
                "and replies, not mentions (make it an admin, or BotFather /setprivacy)",
                self.chat_id,
            )

    # ------------------------------------------------------------- profile
    MENU = (
        "today",
        "tomorrow",
        "week",
        "list",
        "batch",
        "weight",
        "waist",
        "craving",
        "water",
        "pills",
        "about",
        "reminders",
        "pair",
        "devices",
        "new",
        "help",
    )

    async def sync_profile(self, client) -> None:
        """Command menu and descriptions in English (default) and in the household's language."""
        for locale in dict.fromkeys(("en", self.data.locale)):
            t = Translator(locale)
            lang = {} if locale == "en" else {"language_code": locale}
            names = t.raw("bot_commands") or {}
            calls = (
                (
                    "setMyCommands",
                    {
                        "commands": [
                            {"command": names.get(c, c), "description": t(f"bot_cmd_{c}")}
                            for c in self.MENU
                        ]
                    },
                ),
                ("setMyShortDescription", {"short_description": t("bot_short")}),
                ("setMyDescription", {"description": t("bot_about_intro")}),
            )
            for method, params in calls:  # each on its own: one failure never skips the rest
                try:
                    await self.call(client, method, **params, **lang)
                except TelegramError as e:  # the profile is nice to have; never stop the bot
                    log.warning("could not update the bot profile (%s %s): %s", locale, method, e)

    # ------------------------------------------------------------- commands
    async def cmd_about(self, who, arg, user, chat=None) -> str:
        return self.presentation()

    def presentation(self) -> str:
        """Everything the bot does for this household, from the real settings."""
        t, data = self.t, self.data
        cfg = data.settings
        menu = t("bot_about_menu")
        if data.household.calendar.ics_env:
            menu += " " + t("bot_about_calendar")
        lines = [
            t("bot_about_title"),
            "",
            menu,
            "",
            t("bot_about_daily"),
            "",
            t("bot_about_list"),
        ]
        reminders = [r for r in cfg.reminders if r.enabled]
        if reminders:
            lines += ["", t("bot_about_reminders")]
            for r in reminders:
                days = (
                    t("bot_about_daily_days")
                    if r.days == "daily"
                    else ", ".join(t.raw("days")[WEEKDAYS.index(d)] for d in r.days)
                )
                # plain names: describing a reminder must not notify the people it is for
                who = ", ".join(data.household.person(p).name for p in r.people) if r.people else ""
                everyone = who or ", ".join(p.name for p in data.household.people)
                if r.kind == "message":  # the text as it will be sent, up to its first ": "
                    sent = r.text.format(names=everyone, today=t.date(self.state.today()))
                    label = sent.split(": ")[0] if ": " in sent else sent
                elif r.kind == "meal":
                    label = t("bot_about_kind_meal", meal=t(f"meal_{r.meal}").lower())
                else:
                    label = t(f"bot_about_kind_{r.kind}")
                key = (
                    "bot_about_reminder_for"
                    if who and r.kind != "message"
                    else "bot_about_reminder"
                )
                lines.append(
                    t(key, label=label.strip(), times=", ".join(r.at), days=days, people=who)
                )
        lines += ["", t("bot_about_log"), "", t("bot_about_ask")]
        if cfg.bot.voice and cfg.transcription.provider != "none":
            lines.append(t("bot_about_voice"))
        if llm.sees_files(data):
            lines.append(t("bot_about_files"))
        if cfg.llm.provider == "none":
            lines.append(t("bot_no_llm"))
        else:
            lines += ["", t("bot_about_changes"), "", t("bot_about_once")]
        if cfg.bot.private_chats:
            lines += ["", t("bot_about_private")]
        if cfg.base_url:
            lines += ["", cfg.base_url]
        return "\n".join(lines)

    async def cmd_help(self, who, arg, user, chat=None) -> str:
        return self.t("bot_help")

    async def cmd_new(self, who, arg, user, chat=None) -> str:
        """Start the chat's AI conversation over: the provider's session and the kept messages go."""
        chat_id = (chat or {}).get("id")
        if chat_id is None:
            return self.t("bot_new")
        async with self.ai:  # never in the middle of an answer for this chat
            conv = self.state.db.get_kv(f"bot.conversation.{chat_id}") or {}
            self.state.db.set_kv(f"bot.conversation.{chat_id}", None)
            self.state.db.set_kv(WAITING.format(chat_id), None)
            self.state.db.set_kv(LEGACY_PENDING.format(chat_id), None)
            self.state.db.clear_chat(chat_id)
            self.state.db.set_kv(OPTIONS.format(chat_id), None)
            with self.qlock:
                self.queues.pop(chat_id, None)
                self.queued(chat_id)
            self.again.discard(chat_id)
        if conv.get("id"):
            try:
                await llm.forget(self.data, conv["id"])
            except Exception as e:  # noqa: BLE001  the new conversation starts either way
                log.warning("could not drop the old conversation: %s", safe(e))
        return self.t("bot_new")

    async def cmd_today(self, who, arg, user, chat=None) -> str:
        return self.day_text(self.state.today(), self._viewer(who, chat))

    async def cmd_tomorrow(self, who, arg, user, chat=None) -> str:
        return self.day_text(self.state.today() + dt.timedelta(days=1), self._viewer(who, chat))

    @staticmethod
    def _viewer(who, chat) -> str | None:
        """In a private chat, the person writing: only their own pills are shown."""
        return who if chat and chat.get("type") == "private" else None

    def day_text(self, day: dt.date, viewer: str | None = None) -> str:
        t = self.t
        plan = plan_for(self.data, day)
        if not plan or not plan.day(day):
            return t("paused_day" if day in paused_days(self.data) else "no_plan_day")
        pds = person_days(self.data, self.state.db, plan, day)
        # pills are household data: never for people outside it (bot.group_open)
        show_pills = not self.data.settings.bot.group_open
        lines = [t.date(day).capitalize()]
        menu_rows = day_menu(self.data, plan.day(day))
        for meal, rec in menu_rows:
            if meal == "nibble":
                continue
            lines.append(f"\n{ICONS[meal]} {t('meal_' + meal)}: {rec.name}")
            for pd in pds:
                pl = pd.plates.get(meal)
                if pl:
                    lines.append(f"  {pd.name}: " + "; ".join(pl.lines))
                pills = pd.pills.get(meal) if meal in ("breakfast", "lunch", "dinner") else None
                if pills and show_pills and viewer in (None, pd.person_id):
                    names = " · ".join(f"{x.name} {x.dose}".strip() for x in pills)
                    lines.append(f"  💊 {pd.name}: {names}")
        if show_pills:  # pills that go with no meal
            for pd in pds:
                if viewer not in (None, pd.person_id):
                    continue
                for slot in ("waking", "bedtime"):
                    if pd.pills.get(slot):
                        names = " · ".join(f"{x.name} {x.dose}".strip() for x in pd.pills[slot])
                        lines.append(f"💊 {pd.name}, {t('pills_' + slot).lower()}: {names}")
        picks = [rec for meal, rec in menu_rows if meal == "nibble"]
        if picks:
            lines.append("\n" + t("nibble", items=", ".join(r.name for r in picks)))
        lines.append("")
        for pd in pds:
            lines.append(
                t(
                    "kcal_protein",
                    kcal=round(pd.kcal),
                    kcal_target=pd.kcal_target,
                    protein=round(pd.protein),
                    protein_target=int(pd.protein_target),
                )
                + f" ({pd.name})"
            )
        lines.append(self.url(f"/?d={day.isoformat()}"))
        return "\n".join(lines)

    async def cmd_week(self, who, arg, user, chat=None) -> str:
        t = self.t
        today = self.state.today()
        monday = today - dt.timedelta(days=today.weekday())
        plan = plan_for(self.data, today)
        if not plan:
            return t("no_plan_week")
        lines = [t("week_of", date=t.date(monday, False))]
        paused = paused_days(self.data)
        for i in range(7):
            d = plan.day(monday + dt.timedelta(days=i))
            if not d and monday + dt.timedelta(days=i) in paused:
                lines.append(f"{t.raw('days_short')[i]}: {t('paused_short')}")
            if d:
                dishes = " · ".join(
                    f"{ICONS[meal]} {rec.name}"
                    for meal, rec in day_menu(self.data, d)
                    if meal not in ("side", "nibble")
                )
                lines.append(f"{t.raw('days_short')[i]}: {dishes}")
        lines.append(self.url(f"/week?d={monday.isoformat()}"))
        return "\n".join(lines)

    async def cmd_list(self, who, arg, user, chat=None) -> str:
        t = self.t
        today = self.state.today()
        monday = today - dt.timedelta(days=today.weekday())
        plan = plan_for(self.data, monday)
        if not plan:
            return t("no_plan_week")
        shop = shopping_list(self.data, self.state.db, plan, monday, t, today=today)
        lines = [t("shopping_title", date=t.date(monday, False))]
        for store in shop.stores:
            lines.append(f"\n{store.name}")
            for line in store.lines:
                lines.append(f"- {line.text}")
            if store.total:
                lines.append(t("shopping_total", total=t.money(store.total)))
        lines.append(self.url(f"/list?d={monday.isoformat()}"))
        return "\n".join(lines)

    async def cmd_batch(self, who, arg, user, chat=None) -> str:
        t = self.t
        today = self.state.today()
        monday = today - dt.timedelta(days=today.weekday())
        plan = plan_for(self.data, monday)
        sessions = (
            sorted(
                (s for s in plan.sessions if monday <= s.date < monday + dt.timedelta(days=7)),
                key=lambda s: (s.date, not s.morning),
            )
            if plan
            else []
        )
        blocks = []
        for s in sessions:
            dishes = self.batch_text(self.data, plan, s.date, s)
            if dishes:
                when = t("batch_morning" if s.morning else "batch_afternoon")
                blocks.append(t("batch_cook", date=t.date(s.date), when=when) + "\n\n" + dishes)
        if not blocks:
            return t("batch_none")
        lines = [t("batch_title", date=t.date(monday, False)), *("\n" + b for b in blocks)]
        lines.append(self.url(f"/batch?d={monday.isoformat()}"))
        return "\n".join(lines)

    def _need_person(self, who, user):
        if who is None:
            return self.t("bot_unknown_person", id=user.get("id"))
        return None

    def _number(self, arg: str) -> float | None:
        m = NUMBER.search(arg)
        return float(m.group(1).replace(",", ".")) if m else None

    async def cmd_weight(self, who, arg, user, chat=None) -> str:
        if err := self._need_person(who, user):
            return err
        kg = self._number(arg)
        if kg is None:
            return self.t("bot_bad_number")
        try:
            self.state.db.add_weight(who, self.state.today(), kg, source="bot")
        except ValueError:
            return self.t("bot_bad_number")
        return self.t("bot_logged_weight", kg=kg, name=self.data.household.person(who).name)

    async def cmd_waist(self, who, arg, user, chat=None) -> str:
        if err := self._need_person(who, user):
            return err
        cm = self._number(arg)
        if cm is None:
            return self.t("bot_bad_number")
        try:
            self.state.db.add_waist(who, self.state.today(), cm)
        except ValueError:
            return self.t("bot_bad_number")
        return self.t("bot_logged_waist", cm=cm, name=self.data.household.person(who).name)

    async def cmd_craving(self, who, arg, user, chat=None) -> str:
        if err := self._need_person(who, user):
            return err
        n = self._number(arg)
        if n is None:
            return self.t("bot_bad_number")
        n = int(max(0, min(10, n)))
        self.state.db.set_daily(who, self.state.today(), craving=n)
        return self.t("bot_logged_craving", n=n, name=self.data.household.person(who).name)

    async def cmd_water(self, who, arg, user, chat=None) -> str:
        if err := self._need_person(who, user):
            return err
        n = self.state.db.bump_water(who, self.state.today(), int(self._number(arg) or 1))
        return self.t("bot_logged_water", n=n, name=self.data.household.person(who).name)

    async def cmd_pair(self, who, arg, user, chat=None) -> str:
        """A code that pairs a phone app with this household, for ten minutes, once.

        Private chat only. A code is a credential: in the group everyone present would read it,
        guests included when bot.group_open is set.
        """
        if err := self._need_person(who, user):
            return err
        if (chat or {}).get("type") != "private":
            return self.t("bot_pair_private_only")
        from . import devices as dv

        code, ttl = dv.make_code(self.state.db, who)
        return self.t("bot_pair_code", code=dv.spaced(code), minutes=int(ttl // 60))

    async def cmd_devices(self, who, arg, user, chat=None) -> str:
        """The paired phones, and `/dispositivos quitar <n>` to take one away."""
        if err := self._need_person(who, user):
            return err
        if (chat or {}).get("type") != "private":
            return self.t("bot_pair_private_only")
        names = {p.id: p.name for p in self.data.household.people}
        word, _, rest = arg.strip().partition(" ")
        if word.lower() in {"quitar", "remove", "revoke", "borrar"}:
            n = self._number(rest)
            if n is None:
                return self.t("bot_bad_number")
            if not self.state.db.revoke_device(int(n)):
                return self.t("bot_devices_unknown", id=int(n))
            return self.t("bot_devices_revoked", id=int(n))
        rows = self.state.db.devices()
        if not rows:
            return self.t("bot_devices_none")
        lines = [
            self.t(
                "bot_devices_row",
                id=d["id"],
                name=d["name"],
                who=names.get(d.get("person") or "", "—"),
                seen=(d.get("last_seen") or "—")[:16].replace("T", " "),
            )
            for d in rows
        ]
        return self.t("bot_devices_head") + "\n" + "\n".join(lines)

    async def cmd_pills(self, who, arg, user, chat=None) -> str:
        if err := self._need_person(who, user):
            return err
        self.state.db.set_daily(who, self.state.today(), pills_lunch=1)
        return self.t("bot_pills_logged", name=self.data.household.person(who).name)

    # ------------------------------------------------------------- reminders
    def due(self, now: dt.datetime) -> list:
        """(reminder, at) pairs whose time is within the last 10 minutes today."""
        from .model import WEEKDAYS

        out = []
        for r in self.data.settings.reminders:
            if not r.enabled:
                continue
            if r.days != "daily" and WEEKDAYS[now.weekday()] not in r.days:
                continue
            for at in r.at:
                h, m = (int(x) for x in at.split(":"))
                when = now.replace(hour=h, minute=m, second=0, microsecond=0)
                if when <= now < when + dt.timedelta(minutes=10):
                    out.append((r, at))
        return out

    def review(self, chat: int, day: dt.date) -> None:
        """The weekly review: a turn the bot asks itself, before the week's shopping, to check the
        coming week against everything the household said, fix what does not fit with menu
        changes, and tell the group what to do. It is answered like any message, so the AI uses
        the same changes and reminders; nobody has to ask."""
        monday = day + dt.timedelta(days=(7 - day.weekday()) % 7)
        text = self.t("review_turn", week=self.t.date(monday), d=monday.isoformat())
        self.take(chat, {"first_name": self.t("review_who")}, None, None, text, True)

    def reminder_text(self, r, day: dt.date) -> str | None:
        t, data, db = self.t, self.data, self.state.db
        people = [p for p in data.household.people if not r.people or p.id in r.people]
        names = ", ".join(self.mention(p.id) for p in people)
        if r.kind == "message":
            return r.text.format(names=names, today=t.date(day))
        plan = plan_for(data, day)
        if r.kind == "shopping":
            rest = {day + dt.timedelta(days=i) for i in range(7 - day.weekday())}
            if rest <= paused_days(data):  # the rest of the week is paused: nothing to buy for
                return None
            return t("reminder_shopping", url=self.url(f"/list?d={day.isoformat()}"))
        if r.kind == "batch":
            said = [
                x
                for x in (self.batch_reminder(plan, day), self.soak_text(day), self.thaw_text(day))
                if x
            ]
            return "\n\n".join(said) or None
        if not plan:
            return None
        if r.kind == "day":
            return self.menu_text(plan, day)
        if r.kind == "defrost":
            tomorrow = plan.day(day + dt.timedelta(days=1))
            if (
                tomorrow
                and tomorrow.lunch in data.recipes
                and (tomorrow.date - tomorrow.lunch_cook).days
                > data.recipes[tomorrow.lunch].fridge_days
            ):
                return t(
                    "reminder_defrost",
                    dish=data.recipes[tomorrow.lunch].name,
                    url=self.url(f"/recipe/{tomorrow.lunch}?d={tomorrow.date.isoformat()}"),
                )
            return None
        if r.kind == "meal":
            d = plan.day(day)
            rid = getattr(d, r.meal) if d else None
            if not rid or rid not in data.recipes:
                return None
            rec = data.recipes[rid]
            where = ""
            if r.meal == "lunch":
                gap = (day - d.lunch_cook).days
                where = (
                    t("lunch_fresh")
                    if gap == 0
                    else t(
                        "lunch_fridge" if gap <= rec.fridge_days else "lunch_freezer",
                        date=t.date(d.lunch_cook),
                    )
                )
            text = t(
                "reminder_meal",
                meal=t("meal_" + r.meal),
                dish=rec.name,
                where=where,
                url=self.url(f"/recipe/{rid}?d={day.isoformat()}"),
            )
            return text + (f"\n▶ {rec.video}" if rec.video else "")
        if r.kind == "pills_lunch":
            todo = []
            for p in people:
                if not p.pills_on(day).get("lunch"):
                    continue
                rows = db.daily(p.id, day)
                done = (
                    rows and rows[0]["day"] == day.isoformat() and rows[0].get("pills_lunch") == 1
                )
                if not done:
                    todo.append(t("reminder_pills_lunch", name=self.mention(p.id)))
            return "\n".join(todo) or None
        return None

    def batch_reminder(self, plan, day: dt.date) -> str | None:
        """Today's afternoon session and tomorrow morning's: a session cooked the morning a lunch
        is eaten (a Saturday before lunch, the first day back from a pause) is said the evening
        before, or nobody hears of it. Not when the chat already set its own reminder about it
        for that evening."""
        t, data = self.t, self.data
        said = []
        for s in plan.sessions if plan else []:
            if s.date == day and not s.morning:
                dishes = self.batch_text(data, plan, day, s)
                if dishes:  # every dish deleted: nothing to remind about yet
                    url = self.url(f"/batch?d={day.isoformat()}")
                    said.append(t("reminder_batch", dishes=dishes, url=url))
                    break
        tomorrow = day + dt.timedelta(days=1)
        later = plan_for(data, tomorrow)  # tomorrow may start the next plan
        for s in later.sessions if later and tomorrow not in paused_days(data) else []:
            if s.date == tomorrow and s.morning and not self._session_reminded(s, day):
                dishes = self.batch_text(data, later, tomorrow, s)
                if dishes:
                    url = self.url(f"/batch?d={tomorrow.isoformat()}")
                    said.append(t("reminder_batch_morning", dishes=dishes, url=url))
                    break
        return "\n\n".join(said) or None

    def _session_reminded(self, session, day: dt.date) -> bool:
        """A reminder set in the chat about one of the session's dishes (its recipe and a day it
        is eaten), due the evening of `day`: the chat already said it."""
        zone = self._zone()
        start = dt.datetime.combine(day, dt.time(12, 0), zone).timestamp()
        end = dt.datetime.combine(day + dt.timedelta(days=1), dt.time(), zone).timestamp()
        dishes = {(x.recipe, e.isoformat()) for x in session.dishes for e in x.eat}
        rows = [
            *self.state.db.reminders(self.chat_id),
            *self.state.db.reminders(self.chat_id, "sent"),
        ]
        return any(
            start <= r["at"] < end and (r.get("recipe"), r.get("date")) in dishes for r in rows
        )

    def soak_text(self, day: dt.date) -> str | None:
        """The eve of a batch session: the dried pulses to leave in water tonight, even when
        today is paused."""
        t = self.t
        tomorrow = day + dt.timedelta(days=1)
        plan = plan_for(self.data, tomorrow)
        soaks = soak_for(self.data, self.state.db, plan, tomorrow) if plan else []
        if not soaks:
            return None
        items = "\n".join(
            t("reminder_soak_item", name=x.ingredient.name, amount=x.amount, dish=x.recipe.name)
            for x in soaks
        )
        return t("reminder_soak", items=items, url=self.url(f"/batch?d={tomorrow.isoformat()}"))

    def thaw_text(self, day: dt.date) -> str | None:
        """The evening before a day whose meals use something kept frozen (bread): take it out
        tonight, even when today is paused."""
        t = self.t
        tomorrow = day + dt.timedelta(days=1)
        if tomorrow in paused_days(self.data):
            return None
        plan = plan_for(self.data, tomorrow)
        thaws = thaw_for(self.data, self.state.db, plan, tomorrow) if plan else []
        if not thaws:
            return None
        items = "\n".join(
            t(
                "reminder_thaw_item",
                name=x.ingredient.name,
                amount=x.amount,
                meal=t("meal_" + x.meal),
                dish=x.recipe.name,
            )
            for x in thaws
        )
        return t("reminder_thaw", items=items, url=self.url(f"/?d={tomorrow.isoformat()}"))

    def batch_text(self, data, plan, day: dt.date, session) -> str:
        """The session's dishes, one block each: servings, cooking time, lunches, split by person."""
        ids = {x.recipe for x in session.dishes}
        names = {p.id: p.name for p in data.household.people}
        sheet = batch_sheet(data, self.state.db, plan, day)
        return "\n\n".join(batch_dish_text(self.t, b, names) for b in sheet if b.recipe.id in ids)

    def menu_text(self, plan, day: dt.date) -> str | None:
        """The whole day's menu in a few lines, with where lunch comes from and the day's page."""
        t, data = self.t, self.data
        d = plan.day(day)
        if not d:
            return None
        lines = [t("reminder_day", date=t.date(day))]
        picks = []
        for meal, rec in day_menu(data, d):
            if meal == "nibble":
                picks.append(rec.name)
                continue
            line = f"{ICONS[meal]} {t('meal_' + meal)}: {rec.name}"
            if meal == "lunch":
                gap = (day - d.lunch_cook).days
                where = (
                    t("lunch_fresh")
                    if gap == 0
                    else t(
                        "lunch_fridge" if gap <= rec.fridge_days else "lunch_freezer",
                        date=t.date(d.lunch_cook),
                    )
                )
                line += f" · {where.lower()}"
            lines.append(line)
        if picks:
            lines.append(f"{ICONS['nibble']} {t('nibble_title')}: {', '.join(picks)}")
        lines.append(self.url(f"/?d={day.isoformat()}"))
        return "\n".join(lines)

    async def tick_reminders(self, client) -> None:
        chat = self.chat_id
        if chat is None:
            return
        now = dt.datetime.now(ZoneInfo(self.data.household.timezone))
        for r, at in self.due(now):
            if self.state.db.was_sent(r.id, now.date(), at):
                continue
            if r.kind == "review":
                self.review(chat, now.date())
                self.state.db.mark_sent(r.id, now.date(), at)
                self.spawn(self.drive(client, chat))
                continue
            text = self.reminder_text(r, now.date())
            if text:
                await self.send(client, chat, text)
            self.state.db.mark_sent(r.id, now.date(), at)  # only after Telegram took it

    def announcement(self, start: dt.date) -> str | None:
        """The new menu in a few lines: the links to each week's menu, list and batch sheet."""
        t = self.t
        plan = plan_for(self.data, start)
        if not plan:
            return None
        lines = [t("plan_ready", first=t.date(plan.start), last=t.date(plan.end - dt.timedelta(1)))]
        monday = plan.start
        while monday < plan.end:
            d = monday.isoformat()
            lines.append(
                t(
                    "plan_ready_week",
                    date=t.date(monday, False),
                    week=self.url(f"/week?d={d}"),
                    list=self.url(f"/list?d={d}"),
                    batch=self.url(f"/batch?d={d}"),
                )
            )
            monday += dt.timedelta(days=7)
        return "\n".join(lines)

    async def tick_announce(self, client) -> None:
        from .scheduler import ANNOUNCE

        chat, db = self.chat_id, self.state.db
        raw = db.get_kv(ANNOUNCE)
        if chat is None or not raw:
            return
        text = self.announcement(dt.date.fromisoformat(raw))
        if text:
            await self.send(client, chat, text)
        db.set_kv(ANNOUNCE, None)  # only after Telegram took it

    # ------------------------------------------------------------- one-off reminders
    def _zone(self) -> ZoneInfo:
        return ZoneInfo(self.data.household.timezone)

    def _when(self, at: float) -> str:
        """A reminder's time as the household reads it: "Sunday 18 Oct, 18:00"."""
        local = dt.datetime.fromtimestamp(at, self._zone())
        return self.t("reminder_when", date=self.t.date(local.date()), time=f"{local:%H:%M}")

    def _person(self, pid) -> str | None:
        return pid if pid in {p.id for p in self.data.household.people} else None

    def _offered(self, listed: list[dict], who) -> list[dict]:
        """The reminders an answer listed that can be kept: a time still to come, within
        REMIND_DAYS, for a person of the household (the person answered when it names nobody)."""
        zone, now, out = self._zone(), time.time(), []
        for r in listed:
            try:
                at = dt.datetime.strptime(r["when"], "%Y-%m-%d %H:%M").replace(tzinfo=zone)
            except (KeyError, TypeError, ValueError):
                continue
            if not now + 60 <= at.timestamp() <= now + REMIND_DAYS * 86400:
                continue
            person = self._person(r.get("person")) or who
            item = {"at": at.timestamp(), "text": r["text"], "person": person, "done": None}
            if r.get("set"):
                item["set"] = True
            if r.get("recipe") and self._links(r["recipe"], r.get("date")):
                item["recipe"], item["date"] = r["recipe"], r["date"]
            out.append(item)
        return out

    def _links(self, recipe: str, date) -> bool:
        """A dish link the menu backs: that recipe on that day. A wrong one is dropped, so the
        reminder is not cancelled the moment it is set."""
        try:
            day = dt.date.fromisoformat(str(date))
        except ValueError:
            return False
        if doctor.serves(self.data, recipe, day):
            return True
        log.info("a reminder's dish link is not on the menu: %s on %s", recipe, day)
        return False

    def _offers(self) -> list[dict]:
        return [o for o in self.state.db.get_kv(REMIND) or [] if isinstance(o, dict)]

    def _remind_rows(self, key: str, items: list[dict]) -> list[list[tuple[str, str]]]:
        """Each reminder's buttons (here, in the calendar, nothing), each on its own row and saying
        what it sets (_remind_label), or one inert button saying what became of it. Without the
        calendar only «here» is offered: an offer nobody taps does nothing anyway, and the offer
        says so."""
        rows = []
        calendar = self.data.settings.bot.reminder_calendar
        for n, r in enumerate(items):
            if r.get("done"):
                rows.append([("✓ " + self._remind_label(r, r["done"]), "x")])
                continue
            rows.append([(self._remind_label(r, "here"), f"r:{key}:{n}h")])
            if calendar:
                rows.append([(self._remind_label(r, "calendar"), f"r:{key}:{n}c")])
                rows.append([(self._remind_label(r, "nothing"), f"r:{key}:{n}x")])
        return rows

    def _remind_label(self, r: dict, choice: str) -> str:
        """A reminder button: "🔔 Mon 2, 21:30 · Soak the chickpeas", so two reminders' buttons
        never read the same. Short enough for a button on its own row (REMIND_WHAT)."""
        local = dt.datetime.fromtimestamp(r["at"], self._zone())
        when = f"{self.t.raw('days_short')[local.weekday()]} {local.day}, {local:%H:%M}"
        what = " ".join(str(r.get("text") or "").split())
        if len(what) > REMIND_WHAT:
            what = what[: REMIND_WHAT - 1].rsplit(" ", 1)[0].rstrip(" ,.;:") + "…"
        return self.t(f"remind_{choice}", when=when, what=what)

    def chat_reminders(self, chat_id: int) -> str:
        """The reminders set in this chat still to come, with their ids, so the AI can cancel the
        ones a change made wrong instead of asking the household to delete them."""
        rows = self.state.db.reminders(chat_id)[:20]
        if not rows:
            return ""
        out = ["\n## Reminders set in this chat, still to come (id: when, for whom: text)"]
        for r in rows:
            pid = self._person(r["person"])
            who = f", {self.data.household.person(pid).name}" if pid else ""
            local = dt.datetime.fromtimestamp(r["at"], self._zone())
            dish = f" (about {r['recipe']} on {r['date']})" if r.get("recipe") else ""
            out.append(f"- {r['id']}: {local:%Y-%m-%d %H:%M}{who}: {r['text']}{dish}")
        return "\n".join(out)

    async def offer_reminders(self, client, chat_id, listed, covered, reply_to) -> None:
        """One message with the reminders an answer listed and buttons for each. Nothing happens
        until a tap; untapped, the offer expires (REMIND_TTL) as nothing. Reminders the person
        asked for ("set") are set at once, and cancelled ones (only this chat's) go, with one
        message saying both."""
        t, db = self.t, self.state.db
        done: list[str] = []
        before = await self.problem_keys()
        uid = covered[-1].uid if covered else None
        member = self.person_of(uid or 0) is not None
        for r in listed:
            old = db.reminder(r["cancel"]) if "cancel" in r else None
            # the /reminders rule: a stranger let in by group_open cancels only their own
            mine = old is not None and (member or uid == old["by"])
            if mine and old["chat"] == chat_id and db.settle_reminder(old["id"], "cancelled"):
                done.append(t("remind_cancelled", when=self._when(old["at"]), text=old["text"]))
        items = self._offered(
            [r for r in listed if "cancel" not in r], covered[-1].who if covered else None
        )
        for r in [r for r in items if r.get("set")] if uid else []:
            db.add_reminder(
                chat_id, r["person"], r["at"], r["text"], uid, r.get("recipe"), r.get("date")
            )
            done.append(t("remind_set_log", when=self._when(r["at"]), text=r["text"]))
        if done:  # recorded in the chat by send, so the AI hears it
            await self.send(client, chat_id, "\n".join(done), reply_to)
            await self.checkup(client, chat_id, before, reply_to)
        items = [r for r in items if not (uid and r.get("set"))]
        if not items:
            return
        t, people = self.t, self.data.settings.bot.people
        many = len(items) > 1
        lines = [
            (f"{n + 1}. " if many else "") + f"{self._when(r['at'])}: {r['text']}"
            for n, r in enumerate(items)
        ]
        if self.data.settings.bot.reminder_calendar:  # what the two buttons' signs mean
            lines.append(t("remind_legend"))
        persons = list(dict.fromkeys(r["person"] for r in items if r["person"]))
        names = ", ".join(self.mention(p) for p in persons)
        key = secrets.token_hex(4)
        sent = await self.send(
            client,
            chat_id,
            t(
                "remind_offer" if names else "remind_offer_nobody",
                name=names,
                lines="\n".join(lines),
            ),
            reply_to,
            buttons=self._remind_rows(key, items),
            spoken=self.t("voice_reminders"),
        )
        users = [i.uid for i in covered if i.uid] + [people[p] for p in persons if p in people]
        offer = {
            "key": key,
            "chat": chat_id,
            "msg": sent,
            "for": list(dict.fromkeys(users)),
            "items": items,
            "ts": time.time(),
        }
        live = [o for o in self._offers() if time.time() - o.get("ts", 0) <= REMIND_TTL]
        self.state.db.set_kv(REMIND, [*live, offer])

    async def rekey(self, client, chat_id: int, message_id, rows) -> None:
        if not message_id:
            return
        try:
            await self.call(
                client,
                "editMessageReplyMarkup",
                chat_id=chat_id,
                message_id=message_id,
                reply_markup=keyboard(rows),
            )
        except TelegramError as e:
            log.info("could not change the buttons of a message: %s", e)

    async def tapped_remind(self, client, query, chat: dict, key: str, choice: str) -> None:
        """Where an offered reminder goes: here (stored, posted at its time), in the calendar (a
        turn in which the provider adds it, approved by this tap) or nothing."""
        user, t = query.get("from") or {}, self.t
        mid = (query.get("message") or {}).get("message_id")
        offers = self._offers()
        offer = next(
            (o for o in offers if o.get("key") == key and o.get("chat") == chat["id"]), None
        )
        n, c = choice[:-1], choice[-1:]
        if offer is None or time.time() - offer.get("ts", 0) > REMIND_TTL:
            await self.toast(client, query, t("tap_gone"))
            if offer is None:
                await self.unbutton(client, chat["id"], mid)
            return
        if offer["for"] and user.get("id") not in offer["for"]:
            names = ", ".join(self.mention(p) for p in self._people(offer["for"]))
            await self.toast(client, query, t("tap_not_yours", name=names))
            return
        items = offer["items"]
        calendar = self.data.settings.bot.reminder_calendar
        if (
            not n.isdigit()
            or int(n) >= len(items)
            or c not in ("h", "c", "x")
            or (c == "c" and not calendar)
        ):
            await self.toast(client, query)
            return
        item = items[int(n)]
        if item.get("done"):
            await self.toast(client, query, t("tap_gone"))
            return
        item["done"] = {"h": "here", "c": "calendar", "x": "nothing"}[c]
        # saved before any await: a second tap finds it settled
        rest = [o for o in offers if o.get("key") != key]
        open_items = any(not i.get("done") for i in items)
        self.state.db.set_kv(REMIND, [*rest, offer] if open_items else rest or None)
        who = self.person_of(user.get("id", 0))
        when = self._when(item["at"])
        await self.toast(client, query, t("remind_set", when=when) if c == "h" else None)
        await self.rekey(client, chat["id"], offer["msg"], self._remind_rows(key, items))
        if c == "h":
            person = item["person"] or who
            self.state.db.add_reminder(
                chat["id"],
                person,
                item["at"],
                item["text"],
                user.get("id"),
                item.get("recipe"),
                item.get("date"),
            )
            # the AI hears it with the rest of the chat
            self.state.db.add_chat(
                chat["id"], "assistant", None, t("remind_set_log", when=when, text=item["text"])
            )
        elif c == "c":
            local = dt.datetime.fromtimestamp(item["at"], self._zone())
            text = t("remind_calendar_turn", when=when, iso=local.isoformat(), text=item["text"])
            grant = {"by": user.get("id"), "at": item["at"], "used": False}
            self.take(chat["id"], user, who, {"message_id": offer["msg"]}, text, True, grant=grant)
            await self.drive(client, chat["id"])

    async def expire_offers(self, client) -> None:
        """Offers nobody settled in REMIND_TTL: nothing is done, and their buttons say so."""
        offers = self._offers()
        now = time.time()
        gone = [o for o in offers if now - o.get("ts", 0) > REMIND_TTL]
        if not gone:
            return
        self.state.db.set_kv(REMIND, [o for o in offers if o not in gone] or None)
        for o in gone:
            for r in o.get("items") or []:
                r["done"] = r.get("done") or "timeout"
            await self.rekey(
                client, o["chat"], o.get("msg"), self._remind_rows(o["key"], o["items"])
            )

    async def tick_once(self, client) -> None:
        """Post every one-off reminder whose time has come, naming the person it is for, then mark
        it sent. One Telegram refuses for good (the chat is gone) is marked failed."""
        now = time.time()
        for r in self.state.db.due_reminders(now):
            try:
                gone = doctor.stale(self.data, r)
            except Exception as e:  # noqa: BLE001  a broken plan must not hold reminders back
                log.warning("could not check a reminder's dish: %s", safe(e))
                gone = False
            if gone:  # the menu dropped its dish since the last check
                await self.checkup(client)
                continue
            pid = self._person(r["person"])
            text = f"⏰ {self.mention(pid)}: {r['text']}" if pid else f"⏰ {r['text']}"
            if now - r["at"] > LATE_S:
                text += "\n" + self.t("reminder_late", when=self._when(r["at"]))
            try:
                await self.send(client, r["chat"], text)
            except TelegramError as e:
                if e.code in (400, 403):
                    log.warning("a reminder could not be posted: %s", e)
                    self.state.db.settle_reminder(r["id"], "failed")
                    continue
                raise
            self.state.db.settle_reminder(r["id"], "sent")  # only after Telegram took it

    # ------------------------------------------------------------- the consistency check
    async def problems(self) -> list[doctor.Problem] | None:
        """What the consistency check finds now; None when it could not run (never in the way of
        the change or the reminder that called it)."""
        try:
            return await asyncio.to_thread(
                doctor.problems, self.data, self.state.db, self.state.today()
            )
        except Exception as e:  # noqa: BLE001
            log.warning("the consistency check failed: %s", safe(e))
            return None

    async def problem_keys(self) -> set[str]:
        return {p.key for p in await self.problems() or []}

    def problem_line(self, p: doctor.Problem) -> str:
        """A problem in a few words of the household's language."""
        t = self.t
        args = {k: t.date(v) if isinstance(v, dt.date) else v for k, v in p.args.items()}
        return t("problem_" + p.kind, **args)

    async def checkup(
        self, client, chat_id: int | None = None, before: set[str] | None = None, reply_to=None
    ) -> list[doctor.Problem]:
        """Run the consistency check after a write (before: the problems it found just before it)
        or in the morning (before None). A linked reminder the menu no longer backs is cancelled
        here, with one line in its chat. In chat_id: one line naming the problems the write left
        that were not there before, or in the morning all of them. Food bought and on no dish is
        left to the AI, which sees it every turn: it can stay true for days once it is frozen."""
        db, t = self.state.db, self.t
        found = await self.problems()
        if found is None:
            return []
        said: dict[int, list[str]] = {}
        for p in found:
            r = db.reminder(p.reminder) if p.reminder else None
            if r and db.settle_reminder(r["id"], "cancelled"):
                line = t("doctor_reminder_cancelled", when=self._when(r["at"]), text=r["text"])
                said.setdefault(r["chat"], []).append(line)
        rest = [p for p in found if p.kind not in ("stale_reminder", "unused")]
        # in the morning, a recipe nobody can eat is left to the AI: it stays true until someone
        # edits the recipe, and the household may keep it so on purpose
        new = (
            [p for p in rest if p.kind != "avoided"]
            if before is None
            else [p for p in rest if p.key not in before]
        )
        if chat_id is not None and new:
            items = "; ".join(self.problem_line(p) for p in new)
            key = "doctor_morning" if before is None else "doctor_new"
            said.setdefault(chat_id, []).append(t(key, items=items))
        for chat, lines in said.items():
            try:
                await self.send(
                    client, chat, "\n".join(lines), reply_to if chat == chat_id else None
                )
            except TelegramError as e:  # the fix is made; only the notice is lost
                log.warning("could not post what the checkup found: %s", safe(e))
        return rest

    async def tick_checkup(self, client) -> None:
        """Once each morning at bot.checkup_at: the check's safe fixes, and a report in
        bot.checkup_chat (the group when unset) only when something is wrong."""
        cfg = self.data.settings.bot
        chat = cfg.checkup_chat if cfg.checkup_chat is not None else self.chat_id
        if not cfg.checkup_at or chat is None:
            return
        now = dt.datetime.now(self._zone())
        h, m = (int(x) for x in cfg.checkup_at.split(":"))
        when = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if not when <= now < when + dt.timedelta(minutes=10):
            return
        if self.state.db.was_sent("checkup", now.date(), cfg.checkup_at):
            return
        await self.checkup(client, chat)
        self.state.db.mark_sent("checkup", now.date(), cfg.checkup_at)

    def tick_plan(self) -> None:
        """Every PLAN_EVERY_S, the checks of the plan as it stands (verifier.check_plan) in the
        background, never while a turn runs; the judgment runs only for a plan it has not seen."""
        if self.data.settings.llm.provider == "none" or self.running:
            return
        if self.planned_at is not None and self.clock() - self.planned_at < PLAN_EVERY_S:
            return
        self.planned_at = self.clock()
        self.spawn(self.check_plan())

    async def check_plan(self) -> None:
        try:
            await verifier.check_plan(self.data, self.state.db, self.state.today())
        except Exception as e:  # noqa: BLE001
            log.warning("plan check failed: %s", safe(e))

    async def raise_plan(self, client) -> None:
        """Once a day, in the PLAN_RAISE_H after the morning checkup: when the plan check found
        problems in the plan as it is, a turn the bot asks itself in bot.checkup_chat, so the chef
        proposes the fixes in the chat like any change. A check that ends after the checkup still
        gets its turn that morning; a plan raised once is not raised again."""
        db, data, cfg = self.state.db, self.data, self.data.settings.bot
        chat = cfg.checkup_chat if cfg.checkup_chat is not None else self.chat_id
        if data.settings.llm.provider == "none" or not cfg.checkup_at or chat is None:
            return
        now = dt.datetime.now(self._zone())
        h, m = (int(x) for x in cfg.checkup_at.split(":"))
        when = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if not when <= now < when + dt.timedelta(hours=PLAN_RAISE_H):
            return
        if not db.was_sent("checkup", now.date(), cfg.checkup_at):
            return  # after the checkup's own line
        kept, raised = db.get_kv(verifier.PLAN_KEY) or {}, db.get_kv(PLAN_RAISED) or {}
        if not kept.get("key") or raised.get("on") == now.date().isoformat():
            return
        if raised.get("key") == kept["key"]:
            return
        if not await asyncio.to_thread(verifier.plan_findings, data, db, self.state.today()):
            return
        # a private checkup chat: the turn is that person's, so it sees only what they may see
        who = self.person_of(chat) if chat > 0 else None
        if chat > 0 and who is None:
            return  # a private chat of no one in the household
        db.set_kv(PLAN_RAISED, {"key": kept["key"], "on": now.date().isoformat()})
        user = {"first_name": self.t("plan_check_who")}
        self.take(chat, user, who, None, self.t("plan_check_turn"), True)
        self.spawn(self.drive(client, chat))

    async def cmd_reminders(self, who, arg, user, chat=None):
        """The chat's reminders still to come, each with a button to cancel it."""
        t = self.t
        rows = self.state.db.reminders((chat or {}).get("id"))[:20]
        if not rows:
            return t("reminders_none")
        lines, buttons = [t("reminders_title")], []
        for n, r in enumerate(rows, 1):
            pid = self._person(r["person"])
            # plain names: listing a reminder must not notify the people it is for
            name = f" · {self.data.household.person(pid).name}" if pid else ""
            lines.append(f"{n}. {self._when(r['at'])}{name}: {r['text']}")
            buttons.append([(t("reminders_cancel", n=n), f"k:{r['id']}:{n}")])
        return "\n".join(lines), buttons

    async def tapped_cancel(self, client, query, chat_id: int, rid: str, n: str) -> None:
        """Cancel a reminder from the /reminders list; its button says so."""
        db, t = self.state.db, self.t
        r = db.reminder(int(rid)) if rid.isdigit() else None
        message = query.get("message") or {}
        if r is None or r["chat"] != chat_id:
            await self.toast(client, query, t("tap_gone"))
            return
        uid = (query.get("from") or {}).get("id")
        if self.person_of(uid or 0) is None and uid != r["by"]:  # a stranger under group_open
            await self.toast(client, query, t("reminder_not_yours"))
            return
        if not db.settle_reminder(r["id"], "cancelled"):
            await self.toast(client, query, t("tap_gone"))
            return
        await self.toast(client, query, t("reminder_cancelled"))
        db.add_chat(
            chat_id,
            "assistant",
            None,
            t("reminder_cancelled_log", when=self._when(r["at"]), text=r["text"]),
        )
        tapped = query.get("data")
        rows = [
            [
                (f"✓ {t('reminders_cancelled', n=n)}", "x")
                if b.get("callback_data") == tapped
                else (b.get("text", ""), b.get("callback_data", "x"))
                for b in row
            ]
            for row in (message.get("reply_markup") or {}).get("inline_keyboard") or []
        ]
        await self.rekey(client, chat_id, message.get("message_id"), rows)


def _for_handle(msg: dict) -> bool:
    """A message the bot handles: text, a voice note, a photo or a file of any kind."""
    return any(msg.get(k) for k in ("text", "voice", "audio", *FILE_KEYS))


def _media(kind: str, media: dict) -> tuple[str, str]:
    """A file's type and its name in inbox/. A document keeps its own name; a video, a GIF, a
    round video note or a sticker is named for what it is."""
    mime = str(media.get("mime_type") or "").lower()
    if kind == "document":
        mime = mime or "application/octet-stream"
        return mime, _file_name(media.get("file_name"), mime)
    if kind == "sticker":
        if media.get("is_animated"):
            return "application/x-tgsticker", "sticker.tgs"
        if media.get("is_video"):
            return "video/webm", "sticker.webm"
        return "image/webp", "sticker.webp"
    mime = mime or "video/mp4"
    return mime, kind.replace("_", "-") + (mimetypes.guess_extension(mime) or ".mp4")


def _file_name(name, mime: str) -> str:
    """A file's name as one plain path segment, keeping its extension; a name without one gets the
    extension its kind has, when it has one."""
    folded = unicodedata.normalize("NFKD", str(name or ""))
    plain = "".join(c for c in folded if not unicodedata.combining(c))
    stem, dot, ext = plain.rpartition(".")
    if not dot:
        stem, ext = plain, ""
    stem = re.sub(r"[^A-Za-z0-9_-]+", "-", stem).strip("-")[:60] or "file"
    ext = re.sub(r"[^A-Za-z0-9]+", "", ext)[:10].lower()
    if ext:
        return f"{stem}.{ext}"
    return stem + (mimetypes.guess_extension(mime) or "")


def _either(t: Translator, labels: list[str]) -> str:
    """'A, B or C', to be read out."""
    labels = [str(x) for x in labels]
    if len(labels) < 2:
        return "".join(labels)
    return ", ".join(labels[:-1]) + t("voice_or") + labels[-1]


_FENCE = re.compile(r"```.*?(```|$)", re.DOTALL)
_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_URL = re.compile(r"<?https?://\S+>?")
_SHA = re.compile(r"\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}\b")
_BOLD = re.compile(r"(\*\*|__|~~)(.+?)\1")
_ITALIC = re.compile(r"(?<!\w)[*_](\S(?:.*?\S)?)[*_](?!\w)")
_BULLET = re.compile(r"^\s*(?:#{1,6}\s+|[-*•·]\s+|\d{1,2}[.)]\s+|>\s*)")
_END = ".!?…:;,"


def speakable(parts: list[str], t: Translator) -> str:
    """What the bot said in a chat, made to be read out: no Markdown, links, code, emojis or commit
    ids, each line a sentence. The chat keeps its own rendering; this is only what the app reads."""
    out = []
    for part in parts:
        part = _FENCE.sub(" ", part)
        part = _LINK.sub(r"\1", part)
        part = _URL.sub("", part)
        part = re.sub(r"\([^()]*" + _SHA.pattern + r"[^()]*\)", "", part)  # "(change 1a2b3c4)"
        part = _SHA.sub("", part)
        part = _BOLD.sub(r"\2", part)
        part = _ITALIC.sub(r"\1", part).replace("`", "")
        part = part.replace("→", t("voice_arrow"))
        for line in part.splitlines():
            line = _BULLET.sub("", line)
            line = "".join(
                ch
                for ch in line
                if unicodedata.category(ch) != "So" and ch not in "\u200d\ufe0f\u20e3"
            )
            line = " ".join(line.split()).strip()
            if not line:
                continue
            if not out or out[-1][-1] in ".!?…":  # a list item becomes a sentence of its own
                line = line[0].upper() + line[1:]
            out.append(line if line[-1] in _END else f"{line}.")
    said = " ".join(out)
    return re.sub(r"\s+([.,;:!?])", r"\1", said).strip()


def _retry_after(e: BaseException) -> float | None:
    """The seconds a busy provider asks to wait (its error's `retry_after`), or None."""
    later = getattr(e, "retry_after", None)
    if isinstance(later, bool) or not isinstance(later, (int, float)) or later <= 0:
        return None
    return float(later)


UNSENT = (
    "(bot) Your last answer was never sent: the messages above came while you wrote it. Nobody saw "
    "it, and nothing it proposed was shown. Answer all of them now, in one message."
)


def _unsent(fresh: list[dict]) -> dict:
    """The line that tells a turn it rewrites an answer the chat never saw."""
    return {"role": "user", "content": UNSENT, "ids": [], "ts": fresh[-1]["ts"] if fresh else None}


def _reply_to(items: list[Item]) -> int | None:
    """An answer replies to the message it answers only when it answers exactly one."""
    return items[0].mid if len(items) == 1 else None


def keyboard(rows: list[list[tuple[str, str]]]) -> dict:
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in rows]}


def _messages(rows: list[dict]) -> list[dict]:
    """Chat log rows as model messages; who wrote a user message goes in front of it, and a group
    message that did not call the bot is marked "[not to you]". Each carries the ids of the rows
    merged into it and when the last of them was written."""
    out: list[dict] = []
    for h in rows:
        role = "assistant" if h["role"] == "assistant" else "user"
        content = h["text"] if role == "assistant" else f"{h['who'] or 'someone'}: {h['text']}"
        if role == "user" and h.get("asked") == 0:  # said in the group, not to the bot
            content = f"[not to you] {content}"
        if out and out[-1]["role"] == role:
            out[-1]["content"] += "\n" + content
            out[-1]["ids"].append(h.get("id"))
            out[-1]["ts"] = h.get("ts")
        else:
            out.append({"role": role, "content": content, "ids": [h.get("id")], "ts": h.get("ts")})
    return out


def _files(root: Path) -> dict[str, str]:
    """The recipe library, for a provider that can read files (to compare a video with a recipe)."""
    out = {}
    for path in [root / "ingredients.yaml", *sorted((root / "recipes").glob("*.yaml"))]:
        if path.is_file():
            out[str(path.relative_to(root))] = path.read_text(encoding="utf-8")
    return out


def _chunks(text: str, size: int = 3500):
    """Plain text in pieces Telegram accepts, cut at a line break when there is one. Nothing is
    added to what was written (Markdown is cut by render.split, which repairs its fences)."""
    while len(text) > size:
        cut = text.rfind("\n", 1, size)
        if cut <= 0:
            cut = size
        head, text = text[:cut], text[cut:].lstrip("\n")
        yield head
    if text:
        yield text


to_html = render.to_html  # the AI's Markdown as Telegram HTML (render.py)
