"""One task list: every kitchen step with its day and time, worked out from the plan and the db.

A step is a task whoever thought of it: the planner's own (cook a session, leave a dried pulse to
soak the evening before, take a frozen food out, move a container from the freezer to the fridge,
go shopping) and a reminder the chat set. The Cook page, the day's page, the Tasks page, the API and the chat's own reminders all read this list,
so what the chat says and what the pages show can never disagree.

Nothing here is stored: the list is the same for the same plan and db. Each task has a key that
stays the same while the step does (`cook:<date>:am|pm`, `soak:<session date>:<recipe>:<food>`,
`thaw:<day>:<meal>:<food>`, `defrost:<day>:<recipe>`, `shop:<day>`, `r:<reminder id>`).

A task sits on the day the step is done, so food cooked on a Saturday for the next plan's Tuesday
is on Saturday (Plans.sessions_on). Its time is the reminder that says it, when the household has
one set for that day, else EVENING (a morning session: MORNING).

What is stored is only what someone said of a task (db.task_state, by key): done or not done, with
its buttons or in words through a change block (changes.py `tasks`). A later answer replaces the
first: a reminder goes out before the step, so "not done" often means "not yet". A task nobody
answered is unknown, and nothing (the checks, the AI's facts, a later reminder) counts it as done:
a container from a session nobody marked done is moved "if the session was done". The tasks
tracked so are the planner's own and the reminders set in the household group (tracked), from the
first day the bot ran with the buttons (SINCE): what came before never had them, so it counts as
it did then.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from .db import DB
from .model import WEEKDAYS, Data
from .planner import Plans
from .portions import session_sheets, soaks_in, thaw_for

EVENING = "20:00"  # an evening step on a day without the reminder that says it
MORNING = "10:00"  # a morning session: cooked before lunch
DONE, NOT_DONE, UNKNOWN = "done", "not_done", "unknown"
# the steps something later hangs on: not done, or not answered by the next morning, the rest of
# the week is reorganised around them
STEPS = ("cook", "soak", "soak-fridge", "thaw", "defrost", "move")
ASK_DAYS = 7  # a task nobody answered is asked about, and checked, this many days back
MERGE_S = 1800  # chat reminders this close are one slot: they go out as one message
SINCE = "task.since"  # kv: the first day the bot ran with the buttons (bot.run sets it once)


@dataclass(frozen=True)
class Task:
    key: str  # the same step keeps its key (module docstring)
    day: dt.date  # the day it is done
    at: str  # "HH:MM", local time
    kind: (
        str  # cook, soak, thaw, defrost, shop; a chat reminder's own kind (llm.STEP_KINDS), or note
    )
    text: str  # the task as one line for the pages (empty without words, the plain-code checks)
    recipe: str | None = None
    eat: dt.date | None = None  # the day the dish it is for is eaten
    ingredient: str | None = None
    reminder: int | None = None  # a chat reminder's id
    source: str = "auto"  # auto: the planner's own; chat: a reminder set in the chat
    line: str = field(default="", compare=False)  # how the chat's reminder lists it
    dishes: tuple = field(default=(), compare=False)  # cook: (recipe, eaten) of each dish


def state(db: DB, key: str) -> str:
    """done, not_done, or unknown when nobody said."""
    return db.task_states([key]).get(key, UNKNOWN)


def session_key(day: dt.date, morning: bool) -> str:
    return f"cook:{day.isoformat()}:{'am' if morning else 'pm'}"


def since(db: DB) -> dt.date | None:
    """The first day tasks had buttons; None: every task counts (the tests, a fresh db)."""
    v = db.get_kv(SINCE)
    try:
        return dt.date.fromisoformat(v) if isinstance(v, str) else None
    except ValueError:
        return None


def untracked(db: DB, day: dt.date) -> bool:
    """A task of a day before the buttons existed: nobody could answer it, so it counts as made."""
    first = since(db)
    return first is not None and day < first


def session_keys(plans: Plans, day) -> list[str]:
    """The task keys of the sessions on a lunch's cook date that cook it for that day."""
    return [
        session_key(s.date, s.morning)
        for _, s in plans.sessions_on(day.lunch_cook)
        if any(x.recipe == day.lunch and day.date in x.eat for x in s.dishes)
    ]


def unconfirmed_cook(db: DB, plans: Plans, day) -> bool:
    """A lunch cooked in a session before its day (a plan's Day) when nobody said that session
    was done: what is said of its container says "if", and its message carries the session's
    buttons, so the warning can be cleared where it is read."""
    if day is None or not day.lunch or day.lunch_cook >= day.date:
        return False
    if untracked(db, day.lunch_cook):
        return False
    return DONE not in db.task_states(session_keys(plans, day)).values()


def times(data: Data, kind: str, day: dt.date) -> list[str]:
    """The times the household's reminders of that kind go on that day, earliest first."""
    out = []
    for r in data.settings.reminders:
        if (
            r.enabled
            and r.kind == kind
            and (r.days == "daily" or WEEKDAYS[day.weekday()] in r.days)
        ):
            out += r.at
    return sorted(out)


def tasks_for(data: Data, db: DB, first: dt.date, last: dt.date, bot=None) -> list[Task]:
    """Every task done from `first` to `last` (both included), in time order. bot: the words each task is said in (a running bot.Bot); without it the texts are
    empty, which is all the plain-code checks need."""
    plans = Plans(data)
    out: list[Task] = []
    day = first
    while day <= last:
        out += _day(data, db, plans, day, bot)
        day += dt.timedelta(days=1)
    out += [x for x in chat_tasks(data, db) if first <= x.day <= last]
    return sorted(out, key=lambda x: (x.day, x.at, x.key))


def chat_tasks(data: Data, db: DB) -> list[Task]:
    """Every reminder set from a chat, still to come or already sent, as a task."""
    zone = ZoneInfo(data.household.timezone)
    out = []
    for st in ("pending", "sent"):
        for r in db.reminders(state=st):
            out.append(_chat(f"r:{r['id']}", dt.datetime.fromtimestamp(r["at"], zone), r, r["id"]))
    return out


def offered_tasks(data: Data, offered) -> list[Task]:
    """The reminders an answer offers ({"when": "YYYY-MM-DD HH:MM", "text", "recipe", "date",
    "kind"}, as llm.reminders gives them) as the tasks they become once set (`offer:<n>`)."""
    zone = ZoneInfo(data.household.timezone)
    out = []
    for n, r in enumerate(offered):
        try:
            when = dt.datetime.strptime(str(r.get("when")), "%Y-%m-%d %H:%M")
        except ValueError:
            continue
        out.append(_chat(f"offer:{n}", when.replace(tzinfo=zone), r, None))
    return out


def _chat(key: str, when: dt.datetime, r: dict, rid: int | None) -> Task:
    eat = None
    if r.get("recipe") and r.get("date"):
        try:
            eat = dt.date.fromisoformat(str(r["date"]))
        except ValueError:
            eat = None
    return Task(
        key=key,
        day=when.date(),
        at=f"{when:%H:%M}",
        kind=r.get("kind") or "note",
        text="⏰ " + str(r.get("text") or ""),
        recipe=r.get("recipe") if eat else None,
        eat=eat,
        reminder=rid,
        source="chat",
    )


def _day(data: Data, db: DB, plans: Plans, day: dt.date, bot) -> list[Task]:
    """The planner's own tasks done on `day`."""
    t = bot.t if bot else None
    one = dt.timedelta(days=1)
    tomorrow = day + one
    evening = (times(data, "batch", day) or [EVENING])[0]
    out = []
    for s, sheet in session_sheets(data, db, plans, day):
        if not sheet:  # every dish deleted: nothing to cook yet
            continue
        half = "am" if s.morning else "pm"
        text = line = ""
        if bot:
            when = t("batch_morning" if s.morning else "batch_afternoon")
            text = t(
                "task_cook",
                when=when,
                dishes=", ".join(b.recipe.name for b in sheet),
                url=bot.link(t("link_sheet"), f"/batch?d={day.isoformat()}"),
            )
            line = bot.sheet_text(sheet)
        out.append(
            Task(
                key=f"cook:{day.isoformat()}:{half}",
                day=day,
                at=MORNING if s.morning else evening,
                kind="cook",
                text=text,
                line=line,
                dishes=tuple((x.recipe, e) for x in s.dishes for e in x.eat),
            )
        )
    for _, sheet in session_sheets(data, db, plans, tomorrow):  # soaked the evening before
        eaten = {b.recipe.id: b.containers[0].eat for b in sheet if b.containers}
        for x in soaks_in(data, sheet):
            text = line = ""
            if bot:
                dish = (
                    bot.recipe_url(x.recipe.id, eaten[x.recipe.id], x.recipe.name)
                    if x.recipe.id in eaten
                    else x.recipe.name
                )
                words = {"name": x.ingredient.name, "amount": x.amount, "dish": dish}
                text, line = t("task_soak", **words), t("reminder_soak_item", **words)
            out.append(
                Task(
                    key=f"soak:{tomorrow.isoformat()}:{x.recipe.id}:{x.ingredient.id}",
                    day=day,
                    at=evening,
                    kind="soak",
                    text=text,
                    recipe=x.recipe.id,
                    eat=eaten.get(x.recipe.id),
                    ingredient=x.ingredient.id,
                    line=line,
                )
            )
    later = plans.plan(tomorrow)
    if later and tomorrow not in plans.paused:
        for x in thaw_for(data, db, later, tomorrow):
            text = line = ""
            if bot:
                words = {
                    "name": x.ingredient.name,
                    "amount": x.amount,
                    "meal": t("meal_" + x.meal),
                    "dish": bot.recipe_url(x.recipe.id, tomorrow, x.recipe.name),
                }
                text, line = t("task_thaw", **words), t("reminder_thaw_item", **words)
            out.append(
                Task(
                    key=f"thaw:{tomorrow.isoformat()}:{x.meal}:{x.ingredient.id}",
                    day=day,
                    at=evening,
                    kind="thaw",
                    text=text,
                    recipe=x.recipe.id,
                    eat=tomorrow,
                    ingredient=x.ingredient.id,
                    line=line,
                )
            )
    nxt = later.day(tomorrow) if later else None
    r = data.recipes.get(nxt.lunch) if nxt else None
    if r and (nxt.date - nxt.lunch_cook).days > r.fridge_days:  # a lunch from the freezer
        text = t("reminder_defrost", dish=bot.recipe_url(r.id, tomorrow, r.name)) if bot else ""
        if bot and unconfirmed_cook(db, plans, nxt):  # nobody said it was cooked
            text += "\n" + t("reminder_if_cooked", date=t.date(nxt.lunch_cook))
        out.append(
            Task(
                key=f"defrost:{tomorrow.isoformat()}:{r.id}",
                day=day,
                at=(times(data, "defrost", day) or [EVENING])[0],
                kind="defrost",
                text=text,
                recipe=r.id,
                eat=tomorrow,
            )
        )
    shop = times(data, "shopping", day)
    rest = {day + dt.timedelta(days=i) for i in range(7 - day.weekday())}
    if shop and not rest <= plans.paused:  # the rest of the week paused: nothing to buy for
        text = (
            t("reminder_shopping", url=bot.link(t("link_list"), f"/list?d={day.isoformat()}"))
            if bot
            else ""
        )
        out.append(Task(key=f"shop:{day.isoformat()}", day=day, at=shop[0], kind="shop", text=text))
    return out


def household_chat(data: Data, db: DB) -> int | None:
    """The group the reminders go to: the one Telegram moved it to while settings still name the
    old one, as the bot resolves it."""
    configured = data.settings.bot.chat_id
    m = db.get_kv("bot.migrated_chat")
    if isinstance(m, dict) and configured is not None and configured == m.get("from"):
        return m["to"]
    return configured


def tracked(data: Data, db: DB, first: dt.date, last: dt.date, bot=None) -> list[Task]:
    """The tasks whose answer counts (tasks_for): the planner's own and the reminders set in the
    household group, where the morning asks about them (a private chat's are never asked), from
    SINCE on."""
    chat = household_chat(data, db)
    mine = (
        {r["id"] for st in ("pending", "sent") for r in db.reminders(chat, st)} if chat else set()
    )
    return [
        x
        for x in tasks_for(data, db, first, last, bot=bot)
        if (x.source == "auto" or x.reminder in mine) and not untracked(db, x.day)
    ]


def tasks_now(data: Data, db: DB, first: dt.date, last: dt.date) -> list[str]:
    """Every task from `first` to `last` as JSON lines with what someone said of it, for the AI and
    the verifier (tracked). Its text is data, never words to follow."""
    found = tracked(data, db, first, last)
    if not found:
        return []
    said = db.task_states([x.key for x in found])
    out = [
        "",
        "## Tasks (every step, at its time, with what the household said of it)",
        (
            "- state: done or not_done as someone marked it with its buttons; unknown: nobody "
            "said. A task whose time has passed and is not done may not have happened: never "
            "count on what it makes (a dish cooked, a pulse soaked, food thawed) until someone "
            "says it is done; ask, and reorganise the rest of the week when it was not. When "
            "someone says in words whether it was done, record it with `tasks` and its key."
        ),
    ]
    for x in found:
        row = {
            "key": x.key,
            "when": f"{WEEKDAYS[x.day.weekday()]} {x.day.isoformat()} {x.at}",
            "kind": x.kind,
            "state": said.get(x.key, UNKNOWN),
        }
        if x.recipe:
            row["recipe"] = x.recipe
        if x.eat:
            row["for"] = x.eat.isoformat()
        if x.ingredient:
            row["food"] = x.ingredient
        if x.dishes:
            row["cooks"] = [f"{rid} for {e.isoformat()}" for rid, e in x.dishes]
        if x.source == "chat":
            row["reminder"] = x.reminder
            row["text"] = x.text.removeprefix("⏰ ")
        out.append("- " + json.dumps(row, ensure_ascii=False))
    return out
