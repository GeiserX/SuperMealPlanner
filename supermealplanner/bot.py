"""Telegram bot for the household group: commands, reminders, and questions answered by the AI.

Setup (settings.yaml, `bot`): create a bot with @BotFather, put its token in the env var named by
token_env, add the bot to the household group, send /chatid there and copy the id into bot.chat_id.
Map each person to their Telegram user id in bot.people. In BotFather, /setprivacy -> Disable lets the
bot read plain messages in the group; with privacy on it still sees commands, mentions and replies.

Only the configured group and private chats with the people in bot.people are answered.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import re
from zoneinfo import ZoneInfo

import httpx

from . import context, llm
from .i18n import Translator
from .planner import plan_for
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
}
NUMBER = re.compile(r"(\d+(?:[.,]\d+)?)")


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
        self.tasks: set[asyncio.Task] = set()

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
        self, client, chat_id: int, text: str, reply_to: int | None = None, record: bool = True
    ) -> None:
        for chunk in _chunks(text):
            params = {"chat_id": chat_id, "text": chunk, "disable_web_page_preview": True}
            if reply_to:
                params["reply_parameters"] = {
                    "message_id": reply_to,
                    "allow_sending_without_reply": True,
                }
            await self.call(client, "sendMessage", **params)
        if record:
            self.state.db.add_chat(chat_id, "assistant", None, text)

    @property
    def chat_id(self) -> int | None:
        configured = self.data.settings.bot.chat_id
        m = self.migration
        if isinstance(m, dict) and configured == m.get("from"):
            return m["to"]  # only while settings still name the old group
        return configured

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
            return member
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
    async def handle(self, client, msg: dict) -> None:
        chat, user = msg["chat"], msg.get("from", {})
        voice = msg.get("voice") or msg.get("audio")
        if voice and not msg.get("text"):
            if not self.allowed(chat, user.get("id", 0)) or not self.data.settings.bot.voice:
                return
            try:
                text = await self.transcribe(client, voice)
            except Exception as e:  # noqa: BLE001
                log.warning("transcription failed: %s", safe(e))
                await self.send(
                    client, chat["id"], self.t("bot_voice_failed"), msg["message_id"], record=False
                )
                return
            who = self.person_of(user.get("id", 0))
            self.state.db.add_chat(chat["id"], "user", who or user.get("first_name"), f"🎤 {text}")
            await self.send(client, chat["id"], f"🎤 {text}", msg["message_id"], record=False)
            async with self.ai:
                await self.ask(client, chat["id"], msg["message_id"], who)
            return
        text = msg["text"].strip()
        if text.startswith("/chatid"):  # setup: household members only, nothing stored
            if user.get("id") in self.data.settings.bot.people.values():
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
        if text.startswith("/"):
            cmd, _, arg = text[1:].partition(" ")
            cmd = cmd.split("@")[0].lower()
            handler = COMMANDS.get(cmd)
            if handler:
                reply = await getattr(self, f"cmd_{handler}")(who, arg.strip(), user)
                await self.send(client, chat["id"], reply, msg["message_id"])
            return
        low = text.lower()
        words = [w.lower() for w in self.data.settings.bot.wake_words]
        mentioned = (self.username and f"@{self.username}".lower() in low) or any(
            re.search(rf"\b{re.escape(w)}\b", low) for w in words
        )
        replied = (msg.get("reply_to_message") or {}).get("from", {}).get(
            "username"
        ) == self.username
        everyone = self.data.settings.bot.respond == "all"
        asked = chat.get("type") == "private" or everyone or mentioned or replied
        if asked or self.data.settings.bot.history_scope == "all":
            self.state.db.add_chat(chat["id"], "user", who or user.get("first_name"), text)
        if asked:
            async with self.ai:
                await self.ask(client, chat["id"], msg["message_id"], who)

    async def ask(self, client, chat_id: int, reply_to: int, asker: str | None = None) -> None:
        if self.data.settings.llm.provider == "none":
            await self.send(client, chat_id, self.t("bot_no_llm"), reply_to)
            return
        await self.call(client, "sendChatAction", chat_id=chat_id, action="typing")
        private = chat_id > 0
        viewer = asker if private else None
        system = await asyncio.to_thread(
            context.build, self.data, self.state.db, self.state.today(), viewer, not private
        )
        history = self.state.db.recent_chat(chat_id, self.data.settings.bot.history)
        messages = []
        for h in history:
            role = "assistant" if h["role"] == "assistant" else "user"
            content = h["text"] if role == "assistant" else f"{h['who'] or 'someone'}: {h['text']}"
            if messages and messages[-1]["role"] == role:
                messages[-1]["content"] += "\n" + content
            else:
                messages.append({"role": role, "content": content})
        if not messages or messages[0]["role"] != "user":
            messages.insert(0, {"role": "user", "content": "(conversation start)"})
        try:
            answer = await asyncio.wait_for(
                llm.complete(self.data, system, messages), timeout=self.data.settings.llm.timeout_s
            )
        except Exception as e:  # noqa: BLE001
            log.warning("AI call failed: %s", safe(e))
            answer = self.t("bot_error")
        await self.send(client, chat_id, answer, reply_to)

    # ------------------------------------------------------------- commands
    async def cmd_help(self, who, arg, user) -> str:
        return self.t("bot_help")

    async def cmd_today(self, who, arg, user) -> str:
        return self.day_text(self.state.today())

    async def cmd_tomorrow(self, who, arg, user) -> str:
        return self.day_text(self.state.today() + dt.timedelta(days=1))

    def day_text(self, day: dt.date) -> str:
        t = self.t
        plan = plan_for(self.data, day)
        if not plan or not plan.day(day):
            return t("no_plan_day")
        pds = person_days(self.data, self.state.db, plan, day)
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

    async def cmd_week(self, who, arg, user) -> str:
        t = self.t
        today = self.state.today()
        monday = today - dt.timedelta(days=today.weekday())
        plan = plan_for(self.data, today)
        if not plan:
            return t("no_plan_week")
        lines = [t("week_of", date=t.date(monday, False))]
        for i in range(7):
            d = plan.day(monday + dt.timedelta(days=i))
            if d:
                lines.append(
                    f"{t.raw('days_short')[i]}: {self.data.recipes[d.lunch].name if d.lunch in self.data.recipes else '?'} / {self.data.recipes[d.dinner].name if d.dinner in self.data.recipes else '?'}"
                )
        lines.append(self.url(f"/week?d={monday.isoformat()}"))
        return "\n".join(lines)

    async def cmd_list(self, who, arg, user) -> str:
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

    async def cmd_weight(self, who, arg, user) -> str:
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

    async def cmd_waist(self, who, arg, user) -> str:
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

    async def cmd_craving(self, who, arg, user) -> str:
        if err := self._need_person(who, user):
            return err
        n = self._number(arg)
        if n is None:
            return self.t("bot_bad_number")
        n = int(max(0, min(10, n)))
        self.state.db.set_daily(who, self.state.today(), craving=n)
        return self.t("bot_logged_craving", n=n, name=self.data.household.person(who).name)

    async def cmd_water(self, who, arg, user) -> str:
        if err := self._need_person(who, user):
            return err
        n = self.state.db.bump_water(who, self.state.today(), int(self._number(arg) or 1))
        return self.t("bot_logged_water", n=n, name=self.data.household.person(who).name)

    async def cmd_pills(self, who, arg, user) -> str:
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
        names = ", ".join(p.name for p in people)
        if r.kind == "message":
            return r.text.format(names=names, today=t.date(day))
        plan = plan_for(data, day)
        if r.kind == "shopping":
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
                    todo.append(t("reminder_pills_lunch", name=p.name))
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


def _chunks(text: str, size: int = 3900):
    while text:
        yield text[:size]
        text = text[size:]
