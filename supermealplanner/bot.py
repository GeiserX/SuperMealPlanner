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

One answer per burst: a chat's messages wait DEBOUNCE_S after the newest one (and up to VOICE_WAIT_S
for a voice note still being transcribed) and go to the AI together; what arrives while it writes is
handed to the running answer (llm.py, turn["incoming"]), and only what that answer did not take makes
the next turn. An answer that covers one message replies to it; one that covers several is sent
plainly. An edited message is handled by where it is: still waiting, its new text goes; in the
running answer, a correction joins it; already answered, the correction starts a new answer; an edit
that adds the mention is a new message. Changes, proposals and the provider's asks come with inline
buttons; a typed yes or no still works, and only the person asked (or an admin) can tap.

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
import html
import logging
import mimetypes
import os
import re
import secrets
import threading
import time
import unicodedata
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from . import changes, context, llm, menu
from .i18n import Translator
from .model import WEEKDAYS
from .planner import nibbles, paused_days, plan_for
from .portions import MEAL_ORDER, person_days, shopping_list

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
    "reminders": "reminders",
    "recordatorios": "reminders",
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
ACT_TIMEOUT = 3600  # the longest a plugin may take on a proposal (a code draft takes minutes)
# a chat's turn starts this long after its newest message, so a burst goes together
DEBOUNCE_S = 2.5
VOICE_WAIT_S = 30  # a turn waits this long at most for a voice note of its chat still transcribing
FILE_MAX = 20 * 1024 * 1024  # the largest file the Bot API lets a bot download
TURN_FILES_MAX = 25 * 1024 * 1024  # the photos and files one turn carries at most
# what in a message is a file for the AI, document last: Telegram sets it on an animation too
FILE_KEYS = ("photo", "video", "animation", "video_note", "sticker", "document")
OPTIONS = "bot.options.{}"  # choices offered as buttons under an answer, per chat
OPTIONS_TTL = 12 * 3600  # a choice can be tapped this long
OPTIONS_KEEP = 20
REMIND = "bot.remind"  # reminders offered with buttons and not yet settled, across every chat
REMIND_TTL = 1800  # an offer can be tapped this long; then nothing is done
REMIND_DAYS = 60  # the furthest ahead a reminder may be
LATE_S = 600  # a reminder posted this much after its time says so


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
        self.voicing: dict[int, list[float]] = {}  # chat -> start of each note being transcribed
        self.running: dict[int, Turn] = {}
        self.asks: dict[str, dict] = {}  # open asks of the provider, by key

    # ------------------------------------------------------------- plumbing
    @property
    def data(self):
        return self.state.data

    @property
    def t(self) -> Translator:
        return Translator(self.data.locale)

    async def call(self, client: httpx.AsyncClient, method: str, _tries: int = 0, **params):
        try:
            r = await client.post(
                API.format(token=self.token, method=method), json=params, timeout=70
            )
            body = r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise TelegramError(method, safe(e)) from None
        if body.get("ok"):
            return body["result"]
        err = TelegramError(
            method, body.get("description", ""), body.get("error_code"), body.get("parameters")
        )
        if err.code == 429 and method != "getUpdates" and _tries < 2:  # flood control: wait as told
            await asyncio.sleep(min(int(err.params.get("retry_after", 5)), 60))
            return await self.call(client, method, _tries + 1, **params)
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
    ) -> int | None:
        """markdown: the text is the AI's Markdown, sent as Telegram HTML; a chunk Telegram refuses
        to parse goes again as plain text. Returns the id of the last message sent; `ids` collects
        the id of every piece. buttons: rows of (label, callback data), under the last piece."""
        sent = None
        chunks = list(_chunks(text, fences=markdown))
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
        return sent.get("message_id") if isinstance(sent, dict) else None

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
                await self.tick_once(client)
            except Exception as e:  # noqa: BLE001
                log.warning("one-off reminder tick failed: %s", safe(e))
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

    async def handle(self, client, msg: dict) -> None:
        chat, user = msg["chat"], msg.get("from", {})
        private = chat.get("type") == "private"
        cfg = self.data.settings.bot
        voice = msg.get("voice") or msg.get("audio")
        if voice and not msg.get("text"):
            if not self.allowed(chat, user.get("id", 0)) or not cfg.voice:
                return
            # a voice note always calls the bot, in the group too: it cannot mention or reply by itself.
            # While it is transcribed, the chat's next turn waits for it (VOICE_WAIT_S at most).
            start = self.clock()
            notes = self.voicing.setdefault(chat["id"], [])
            notes.append(start)
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
                self.take(chat["id"], user, who, msg, f"🎤 {text}", asked=True)
                self.inbox(done=msg)  # queued now: QUEUED keeps it from here
            finally:
                notes.remove(start)
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
        asked = private or self.mentioned(text) or self.replied(msg)
        if asked or cfg.history_scope == "all":
            self.take(chat["id"], user, who, msg, text, asked)
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
        text = f"[{label}: {file['name']}] {(msg.get('caption') or '').strip()}".rstrip()
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
        correction), so an edit of msg still finds msg's own row."""
        row = self.state.db.add_chat(
            chat_id,
            "user",
            who or user.get("first_name"),
            text,
            mid=(msg or {}).get("message_id") if log_mid else None,
            asked=asked,
        )
        if asked:
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
                    try:
                        await self.ask(client, chat_id, items, rows)
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
                    # the turn ended (a cancelled one did not: a stop keeps its messages queued)
                    with self.qlock:
                        self.queued(chat_id)
        finally:
            self.driving.discard(chat_id)

    async def settle(self, chat_id: int) -> None:
        """Wait until the chat's newest message is DEBOUNCE_S old and no voice note of it is still
        being transcribed (for VOICE_WAIT_S at most from the note's start)."""
        while True:
            with self.qlock:
                newest = max((i.ts for i in self.queues.get(chat_id) or []), default=None)
            if newest is None:
                return
            now = self.clock()
            wait = newest + self.debounce - now
            notes = self.voicing.get(chat_id)
            if notes:
                wait = max(wait, min(notes) + self.voice_wait - now)
            if wait <= 0:
                return
            await asyncio.sleep(min(wait, 0.5))

    def rules(self) -> str:
        """The AI's instructions, with the people's Telegram handles."""
        out = context.rules(self.data)
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

    async def ask(self, client, chat_id: int, items: list[Item], rows: list[dict]) -> None:
        """One AI answer for the queued messages `items`. A provider that keeps the conversation
        gets only what it has not seen; the household's data goes with every message, fresh.
        Messages that arrive meanwhile are handed to it; the ones its answer does not take go back
        to the queue for the next turn."""
        data, db = self.data, self.state.db
        if data.settings.llm.provider == "none":
            await self.send(client, chat_id, self.t("bot_no_llm"), _reply_to(items))
            return
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
            return  # an earlier turn already took these messages
        turn_state = Turn(chat_id, items)
        self.running[chat_id] = turn_state
        reply = None
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
            turn = {
                "conversation": conv.get("id"),
                "rules": self.rules(),
                "context": facts,
                "new": _messages(fresh),
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
            except Exception as e:  # noqa: BLE001
                log.warning("AI call failed: %s", safe(e))
                # never recorded: the unanswered messages go again with the next one
                await self.send(
                    client, chat_id, self.t("bot_error"), _reply_to(items), record=False
                )
                return
        finally:
            with self.qlock:
                turn_state.closed = True  # a provider thread that outlives the turn takes nothing
                missed = [
                    i for i in turn_state.pulled if reply is None or i.row not in reply.covers
                ]
                if reply is not None and missed:  # the next turn sends them, at the head
                    self.queues[chat_id] = [*missed, *self.queues.get(chat_id, [])]
            self.running.pop(chat_id, None)
            await self.close_asks(client, turn_state)
        covered = [*items, *(i for i in turn_state.pulled if i.row in reply.covers)]
        reply_to = _reply_to(covered)
        anchor = covered[-1]  # changes and proposals act for the newest message's sender
        text, change = changes.extract(reply.text)
        text, offered = llm.extract_options(text)
        offered = reply.options or offered
        text, listed = llm.extract_reminders(text)
        listed = reply.reminders or listed
        if not text and change is None and not reply.proposals and not listed:
            await self.send(client, chat_id, self.t("bot_error"), reply_to, record=False)
            return
        if text:
            await self.answer_text(client, chat_id, text, reply_to, offered, covered)
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
        if change is not None:
            await self.propose(client, chat_id, anchor.mid, anchor.who, anchor.uid, change)
        for proposal in reply.proposals:
            await self.offer(client, chat_id, anchor.mid, anchor.who, anchor.uid, proposal)
        if listed:
            await self.offer_reminders(client, chat_id, listed, covered, reply_to)

    async def answer_text(self, client, chat_id, text, reply_to, offered, covered) -> None:
        """The answer, with the choices it offers as buttons under it."""
        if not offered:
            await self.send(client, chat_id, text, reply_to, markdown=True, who=AI)
            return
        key = secrets.token_hex(4)
        rows = [[(label, f"o:{key}:{n}")] for n, label in enumerate(offered)]
        sent = await self.send(
            client,
            chat_id,
            f"{text}\n\n_{self.t('options_hint')}_",
            reply_to,
            markdown=True,
            who=AI,
            buttons=rows,
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
    async def propose(self, client, chat_id, reply_to, asker, user_id, change) -> None:
        """Make a change the person asked for at once; show one the AI came up with on its own and
        wait for a yes from the person it answered."""
        t = self.t
        if isinstance(change, str):
            await self.send(client, chat_id, t("change_invalid", error=change), reply_to)
            return
        if asker is None or user_id is None:
            await self.send(client, chat_id, t("change_members_only"), reply_to)
            return
        try:
            proposal = changes.parse(change, self.data, asker)
        except changes.ChangeError as e:
            await self.send(client, chat_id, t("change_invalid", error=e), reply_to)
            return
        name = self.data.household.person(asker).name
        if proposal.asked:
            item = {"change": change, "asker": asker, "name": name}
            await self.send(client, chat_id, await self.apply(item, asked=True), reply_to)
            return
        lines = changes.describe(proposal, self.data, t)
        msgs: list[int] = []
        sent = await self.send(
            client,
            chat_id,
            t("change_confirm", name=self.mention(asker), lines="\n".join(lines)),
            reply_to,
            ids=msgs,
            buttons=self.yes_no(key := secrets.token_hex(4)),
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
        # a newer change from the same person replaces their earlier one ("no, better at 15:00")
        self.wait_for(chat_id, item, lambda i: "change" in i and i.get("asker") == asker)

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

    def wait_for(self, chat_id: int, item: dict, replaces) -> None:
        """Keep an item waiting next to the others; `replaces(other)` drops one it supersedes."""
        items = [i for i in self.pending(chat_id) if not replaces(i)]
        self.state.db.set_kv(WAITING.format(chat_id), [*items, item])

    def drop(self, chat_id: int, item: dict) -> None:
        items = [i for i in self.pending(chat_id) if i != item]
        self.state.db.set_kv(WAITING.format(chat_id), items or None)

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
        if msg["chat"].get("type") != "private" and not (self.replied(msg) or self.mentioned(text)):
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
        reply = await self.apply(pending) if yes else self.t("change_cancelled")
        await self.send(client, chat_id, reply, reply_to)

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
                if not proposal.plan:
                    return by_hand
                out.append(by_hand)
            else:
                saved = lines[: len(lines) - len(proposal.plan)]
                subject = "; ".join(x.removeprefix("• ") for x in saved)
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
        if asked:
            out.insert(0, t("change_done", lines="\n".join(lines)))
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
        )
        if where != chat_id:
            await self.send(client, chat_id, t("proposal_in_group", name=names), reply_to)
        item = {**pending, "key": key, "by": ids, "msg": sent, "msgs": msgs}
        self.wait_for(where, item, lambda i: (i.get("proposal") or {}).get("id") == proposal["id"])

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
                await self.send(client, chat_id, out.text, reply_to)
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
        "weight",
        "waist",
        "craving",
        "water",
        "pills",
        "about",
        "reminders",
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
        for meal in MEAL_ORDER:
            rid = getattr(plan.day(day), meal)
            if not rid or rid not in self.data.recipes:
                continue
            lines.append(f"\n{t('meal_' + meal)}: {self.data.recipes[rid].name}")
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
        picks = nibbles(self.data, plan.day(day))
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
                    f"{icon} {self.data.recipes[rid].name if rid in self.data.recipes else '?'}"
                    for icon, rid in zip(
                        "☕🍲🍎🌙", (d.breakfast, d.lunch, d.snack, d.dinner), strict=True
                    )
                    if rid
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
        shop = shopping_list(self.data, self.state.db, plan, monday, t)
        lines = [t("shopping_title", date=t.date(monday, False))]
        for store in shop.stores:
            lines.append(f"\n{store.name}")
            for line in store.lines:
                lines.append(f"- {line.text}")
            if store.total:
                lines.append(t("shopping_total", total=t.money(store.total)))
        lines.append(self.url(f"/list?d={monday.isoformat()}"))
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
        if not plan:
            return None
        if r.kind == "defrost":
            tomorrow = plan.day(day + dt.timedelta(days=1))
            if (
                tomorrow
                and tomorrow.lunch in data.recipes
                and (tomorrow.date - tomorrow.lunch_cook).days
                > data.recipes[tomorrow.lunch].fridge_days
            ):
                return t("reminder_defrost", dish=data.recipes[tomorrow.lunch].name)
            return None
        if r.kind == "batch":
            for s in plan.sessions:
                if s.date == day and not s.morning:
                    dishes = ", ".join(
                        data.recipes[x.recipe].name for x in s.dishes if x.recipe in data.recipes
                    )
                    if not dishes:  # every dish was deleted: nothing to remind about yet
                        continue
                    return t(
                        "reminder_batch", dishes=dishes, url=self.url(f"/batch?d={day.isoformat()}")
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

    async def tick_reminders(self, client) -> None:
        chat = self.chat_id
        if chat is None:
            return
        now = dt.datetime.now(ZoneInfo(self.data.household.timezone))
        for r, at in self.due(now):
            if self.state.db.was_sent(r.id, now.date(), at):
                continue
            text = self.reminder_text(r, now.date())
            if text:
                await self.send(client, chat, text)
            self.state.db.mark_sent(r.id, now.date(), at)  # only after Telegram took it

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
            out.append({"at": at.timestamp(), "text": r["text"], "person": person, "done": None})
        return out

    def _offers(self) -> list[dict]:
        return [o for o in self.state.db.get_kv(REMIND) or [] if isinstance(o, dict)]

    def _remind_rows(self, key: str, items: list[dict]) -> list[list[tuple[str, str]]]:
        """Each reminder's buttons (here, in the calendar, nothing), or one inert button saying
        what became of it."""
        t, rows = self.t, []
        calendar = self.data.settings.bot.reminder_calendar
        for n, r in enumerate(items):
            pre = f"{n + 1} · " if len(items) > 1 else ""
            if r.get("done"):
                rows.append([(f"✓ {pre}{t('remind_' + r['done'])}", "x")])
                continue
            first = [(pre + t("remind_here"), f"r:{key}:{n}h")]
            if calendar:
                first.append((pre + t("remind_calendar"), f"r:{key}:{n}c"))
            rows += [first, [(pre + t("remind_nothing"), f"r:{key}:{n}x")]]
        return rows

    async def offer_reminders(self, client, chat_id, listed, covered, reply_to) -> None:
        """One message with the reminders an answer listed and buttons for each. Nothing happens
        until a tap; untapped, the offer expires (REMIND_TTL) as nothing."""
        items = self._offered(listed, covered[-1].who if covered else None)
        if not items:
            return
        t, people = self.t, self.data.settings.bot.people
        many = len(items) > 1
        lines = [
            (f"{n + 1}. " if many else "") + f"{self._when(r['at'])}: {r['text']}"
            for n, r in enumerate(items)
        ]
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
            self.state.db.add_reminder(chat["id"], person, item["at"], item["text"], user.get("id"))
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


def _chunks(text: str, size: int = 3500, fences: bool = True):
    """Pieces Telegram accepts, cut at a line break when there is one, so Markdown stays whole. A
    code block cut in two is closed at the cut and opened again in the next piece. Plain text
    (fences=False) is only cut: nothing is added to what was written."""
    floor = 0  # the reopened fence at the start of the text: a cut must land past it
    room = size - 4 if fences else size  # for the closing fence a cut may add
    while len(text) > size:
        cut = text.rfind("\n", floor + 1, room)
        if cut <= floor:
            cut = room
        head, text = text[:cut], text[cut:].lstrip("\n")
        fence = _open_fence(head) if fences else None
        floor = 0
        if fence is not None:
            head += "\n```"
            text = f"{fence}\n{text}"
            floor = len(fence)
        yield head
    if text:
        yield text


def _open_fence(text: str) -> str | None:
    """The line that opened a code block still open at the end of text, or None."""
    fence = None
    for line in text.split("\n"):
        if line.lstrip().startswith("```"):
            fence = None if fence is not None else line.strip()[:64] or "```"
    return fence


def to_html(md: str) -> str:
    """The AI's Markdown as Telegram HTML: bold, italic, strike, code, links, headings, lists.
    Everything else is escaped, so the text can never break out of the markup."""
    kept: list[str] = []

    def stash(markup: str) -> str:
        kept.append(markup)
        return f"\x00{len(kept) - 1}\x00"

    def inline(s: str) -> str:  # s is already escaped, so every "<" is a tag added here
        # the text inside may hold whole tags, never half of one: "**a *b** c*" stays crossed
        # Markdown instead of becoming crossed HTML that Telegram refuses
        body = r"((?:[^<\n]|<([bs])>[^<\n]*</\2>)+?)"  # one line, as before
        s = re.sub(r"\*\*(?=\S)" + body + r"(?<=\S)\*\*", r"<b>\1</b>", s)
        s = re.sub(r"(?<!\w)__(?=\S)" + body + r"(?<=\S)__(?!\w)", r"<b>\1</b>", s)
        s = re.sub(r"~~(?=\S)" + body + r"(?<=\S)~~", r"<s>\1</s>", s)
        s = re.sub(r"(?<![\w*])\*(?=[^\s*])" + body + r"(?<=[^\s*])\*(?![\w*])", r"<i>\1</i>", s)
        return re.sub(r"(?<!\w)_(?=[^\s_])" + body + r"(?<=[^\s_])_(?!\w)", r"<i>\1</i>", s)

    s = md.replace("\x00", "")
    s = re.sub(
        r"```[^\n`]*\n?(.*?)```",
        lambda m: stash(f"<pre>{html.escape(m.group(1).strip(chr(10)), quote=False)}</pre>"),
        s,
        flags=re.DOTALL,
    )
    s = re.sub(r"`([^`\n]+)`", lambda m: stash(f"<code>{html.escape(m.group(1), False)}</code>"), s)
    s = re.sub(
        # one level of parentheses in the URL (Wikipedia's "Paella_(dish)"); a code span is never
        # part of one
        r"\[([^\]\n]+)\]\((https?://(?:[^()\s\x00]|\([^()\s\x00]*\))+)\)",
        lambda m: stash(
            f'<a href="{html.escape(m.group(2), quote=True)}">'
            f"{inline(html.escape(m.group(1), quote=False))}</a>"
        ),
        s,
    )
    s = html.escape(s, quote=False)
    s = re.sub(r"(?m)^#{1,6}[ \t]+(.+?)[ \t#]*$", r"<b>\1</b>", s)
    s = re.sub(r"(?m)^([ \t]*)[-*+][ \t]+", r"\1• ", s)
    s = inline(s)
    while "\x00" in s:
        s = re.sub(r"\x00(\d+)\x00", lambda m: kept[int(m.group(1))], s)
    return s
