"""Telegram bot for the household group: commands, reminders, and questions answered by the AI.

Setup (settings.yaml, `bot`): create a bot with @BotFather, put its token in the env var named by
token_env, add the bot to the household group, send /chatid there and copy the id into bot.chat_id.
Map each person to their Telegram user id in bot.people.

In a private chat the bot always answers. A voice note is always answered, in the group too, because
it cannot mention the bot. In the group it answers only a text message that mentions its
@handle or replies to it. Telegram shows a bot with privacy mode on (the default) only commands and
replies to it, so for mentions make the bot a group admin or turn privacy off (BotFather,
/setprivacy -> Disable). The bot logs a warning at start when it cannot see them.

Only the configured group and private chats with the people in bot.people are answered.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import html
import logging
import os
import re
import time
import uuid
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
ACT_TIMEOUT = 3600  # the longest a plugin may take on a proposal (a code draft takes minutes)


class Bot:
    def __init__(self, state):
        self.state = state  # web.App: .data, .db, .today()
        cfg = state.data.settings.bot
        self.token = os.environ.get(cfg.token_env, "")
        self.username = ""
        self.offset = 0
        # a group Telegram upgraded to a supergroup: {"from": old id, "to": new id}
        self.migration: dict | None = state.db.get_kv("bot.migrated_chat")
        self.ai = asyncio.Semaphore(1)  # one AI answer at a time, in arrival order
        self.usernames: dict[str, str] = {}  # person id -> Telegram username, from getChatMember
        self.tasks: set[asyncio.Task] = set()
        self.started = False
        self.bot_id = 0
        self.writing = asyncio.Lock()  # one settings change at a time

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

    async def transcribe(self, client, voice: dict) -> str:
        f = await self.call(client, "getFile", file_id=voice["file_id"])
        url = f"https://api.telegram.org/file/bot{self.token}/{f['file_path']}"
        try:
            r = await client.get(url, timeout=120)
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise TelegramError("getFile download", safe(e)) from None
        name = f["file_path"].rsplit("/", 1)[-1] or "voice.ogg"
        return await asyncio.wait_for(
            llm.transcribe(self.data, r.content, name),
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
    ) -> int | None:
        """markdown: the text is the AI's Markdown, sent as Telegram HTML; a chunk Telegram refuses
        to parse goes again as plain text. Returns the id of the last message sent; `ids` collects
        the id of every piece."""
        sent = None
        for chunk in _chunks(text, fences=markdown):
            params = {"chat_id": chat_id, "disable_web_page_preview": True}
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
                        self.started = True
                        await self.interrupted(client)
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
                    allowed_updates=["message"],
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
                self.offset = u["update_id"] + 1
                msg = u.get("message")
                if not msg:
                    continue
                if msg.get("migrate_to_chat_id") and msg["chat"]["id"] == self.chat_id:
                    self.migration = {"from": msg["chat"]["id"], "to": msg["migrate_to_chat_id"]}
                    self.state.db.set_kv("bot.migrated_chat", self.migration)
                    log.warning("the group became %s: set bot.chat_id to it", self.migration["to"])
                    continue
                if msg.get("text") or msg.get("voice") or msg.get("audio"):
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
            log.warning("handling a message failed: %s", safe(e))
            try:
                await self.send(client, msg["chat"]["id"], self.t("bot_error"), record=False)
            except Exception as e2:  # noqa: BLE001  the reply is best effort
                log.warning("could not send the error reply: %s", safe(e2))

    async def reminders(self, client) -> None:
        while True:
            try:
                await self.tick_reminders(client)
            except Exception as e:  # noqa: BLE001
                log.warning("reminder tick failed: %s", safe(e))
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
            # a voice note always calls the bot, in the group too: it cannot mention or reply by itself
            asked, keep_all = True, cfg.history_scope == "all"
            try:
                text = await self.transcribe(client, voice)
            except Exception as e:  # noqa: BLE001
                log.warning("transcription failed: %s", safe(e))
                if asked:
                    await self.send(
                        client,
                        chat["id"],
                        self.t("bot_voice_failed"),
                        msg["message_id"],
                        record=False,
                    )
                return
            who = self.person_of(user.get("id", 0))
            if asked or keep_all:
                self.state.db.add_chat(
                    chat["id"], "user", who or user.get("first_name"), f"🎤 {text}"
                )
            if asked:
                await self.send(client, chat["id"], f"🎤 {text}", msg["message_id"], record=False)
                async with self.ai:
                    await self.ask(client, chat["id"], msg["message_id"], who, user.get("id"))
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
                await self.send(
                    client, chat["id"], reply, msg["message_id"], record=handler != "new"
                )
            return
        asked = private or self.mentioned(text) or self.replied(msg)
        if asked or cfg.history_scope == "all":
            self.state.db.add_chat(chat["id"], "user", who or user.get("first_name"), text)
        if asked:
            async with self.ai:
                await self.ask(client, chat["id"], msg["message_id"], who, user.get("id"))

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

    async def ask(
        self,
        client,
        chat_id: int,
        reply_to: int,
        asker: str | None = None,
        user_id: int | None = None,
    ) -> None:
        """One AI answer. A provider that keeps the conversation gets only what it has not seen;
        the household's data goes with every message, fresh."""
        data, db = self.data, self.state.db
        if data.settings.llm.provider == "none":
            await self.send(client, chat_id, self.t("bot_no_llm"), reply_to)
            return
        key = f"bot.conversation.{chat_id}"
        conv = db.get_kv(key) or {}
        rows = db.chat_rows(chat_id, data.settings.bot.history)
        seen = conv.get("seen", 0) if conv.get("id") else 0
        fresh = [r for r in rows if r["id"] > seen and not (seen and r["who"] == AI)]
        if conv.get("id") and not any(r["role"] == "user" for r in fresh):
            # messages that came in together: an earlier turn already took this one with them
            return
        try:
            await self.call(client, "sendChatAction", chat_id=chat_id, action="typing")
        except TelegramError as e:  # only a courtesy: the answer still goes
            log.info("typing indicator failed: %s", e)
        private = chat_id > 0
        viewer = asker if private else None
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
        }
        try:
            reply = await asyncio.wait_for(
                llm.converse(data, turn), timeout=data.settings.llm.timeout_s
            )
        except Exception as e:  # noqa: BLE001
            log.warning("AI call failed: %s", safe(e))
            # never recorded: the unanswered message goes again with the next one
            await self.send(client, chat_id, self.t("bot_error"), reply_to, record=False)
            return
        text, change = changes.extract(reply.text)
        if not text and change is None and not reply.proposals:
            await self.send(client, chat_id, self.t("bot_error"), reply_to, record=False)
            return
        if text:
            await self.send(client, chat_id, text, reply_to, markdown=True, who=AI)
        if reply.conversation:
            last = max((r["id"] for r in rows), default=seen)
            db.set_kv(key, {"id": reply.conversation, "seen": last if reply.kept else seen})
        else:
            db.set_kv(key, None)
        if change is not None:
            await self.propose(client, chat_id, reply_to, asker, user_id, change)
        for proposal in reply.proposals:
            await self.offer(client, chat_id, reply_to, asker, user_id, proposal)

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
        )
        item = {
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
        self.drop(chat_id, pending)
        if yes and "proposal" in pending:
            await self.act(client, chat_id, msg["message_id"], pending)
            return True
        reply = await self.apply(pending) if yes else self.t("change_cancelled")
        await self.send(client, chat_id, reply, msg["message_id"])
        return True

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
        sent = await self.send(
            client,
            where,
            t("proposal_confirm", name=names, summary=proposal["summary"]),
            answer_to,
            ids=msgs,
        )
        if where != chat_id:
            await self.send(client, chat_id, t("proposal_in_group", name=names), reply_to)
        item = {**pending, "by": ids, "msg": sent, "msgs": msgs}
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
        if cfg.llm.provider == "none":
            lines.append(t("bot_no_llm"))
        else:
            lines += ["", t("bot_about_changes")]
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
                if not p.pills.get("lunch"):
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


def _messages(rows: list[dict]) -> list[dict]:
    """Chat log rows as model messages; who wrote a user message goes in front of it."""
    out: list[dict] = []
    for h in rows:
        role = "assistant" if h["role"] == "assistant" else "user"
        content = h["text"] if role == "assistant" else f"{h['who'] or 'someone'}: {h['text']}"
        if out and out[-1]["role"] == role:
            out[-1]["content"] += "\n" + content
        else:
            out.append({"role": role, "content": content})
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
