"""The reviewer: a full second look at every answer that changes anything, once it is sent, and at
every new or changed plan.

It runs on an answer with any change block (the menu, a recipe, the pantry, a setting, a task's
state, notes), with reminders it offers or cancels, and on a turn the bot asked itself to review or
reorganise the week (the weekly review, the plan check's morning turn, a step not done). An answer
that changes nothing is not reviewed.

The answer goes out as soon as it is written: answering quickly matters more than answering once.
The review then runs in the background (after) on what was sent and on the state it left. When it
finds what would go wrong in the kitchen or for health (the structural problems, and ACT: timeline,
health, nonsense; wishes and clarity are polish and bring nothing), the bot takes one turn of its own that fixes them in one short follow-up message
(corrections said briefly, reminders cancelled and written again, a menu change offered for a
yes). Reviews coalesce: an answer sent while a review runs is covered by one review of the latest
state, the answers since the last review together. The answer of a turn the bot asked itself (a
follow-up, the plan check's turn) is never reviewed, so one answer gets one follow-up at most and
automatic turns never chain.

What it reads is the whole rest of this week and the next as the answer left them, never only the
diff: the menu, the cooking sessions, the text of every recipe on it, every task with what the
household said of it (done, not done, unknown), the pantry and what is going off, every problem the
consistency check finds (doctor.problems, the soak, cook and thaw chains among them) and each
person's health record. The problems that were there before the answer are context: the review
lists one only when the answer touches it or could fix it, so a standing problem the answer cannot
fix does not bring a follow-up to every answer. A turn the bot asked itself about the week counts
them all. A step nobody marked stays out: the morning's day message asks about it, and its own turn
reorganises the week.

Two checks:
- structural, plain code: a change that still waits for a yes is made on a copy of the data folder
  and the consistency check runs on the copy, with the reminders the answer offers counted as set; a
  change already made is read from the data itself, against the problems taken just before it was
  made. What it finds that was not there before is this answer's doing. A menu change the app would
  refuse is one of them, with the app's reason.
- judgment, a review job through the household's AI provider (llm.complete; it can be a session
  that reads files and the web): separate checks of (1) what the household asked for, (2) the
  timeline (what is cooked when, which session prepares what, what has to be soaked, defrosted or
  cooked ahead, and a reminder at the right time for each), (3) health (allergies, intolerances,
  diet rules, medicines with their food rules and fasting windows), (4) plain nonsense and (5)
  clarity: whether someone cooking from the steps, with nobody to ask, understands them on one
  reading. When the answer moves something, or a step was not done, the timeline says how to
  reorganise the rest of the week.

A review has llm.review_timeout_s. When it cannot run (the provider fails, gives no verdict, or
takes too long), nothing says the answer was checked, and the plan check runs again at the next
tick (PLAN_RETRY), so what is wrong in the week still comes up.

The same checks run on the plan as it stands (check_plan): once for each new or changed menu of
this week and the next, with the full text of every recipe on it. What they find is kept with the
plan's key, so the AI sees it among the problems to fix in every turn until the plan changes, and
the bot raises it in the chat (in the morning, or at once for a plan the app just drew) so the chef
proposes the fixes like any change.

Health data stays where it was: it goes only to the provider, which already gets it, in a private
chat only the asker's, and nothing here logs a problem's text, only counts.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import logging
import re
import shutil
import tempfile
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from . import changes, doctor, llm, menu, tasks
from .db import DB
from .i18n import Translator
from .model import WEEKDAYS, Data, DataError, load
from .planner import MEALS, Plans
from .portions import MORNING, session_at

log = logging.getLogger(__name__)

CHECKS = ("wishes", "timeline", "health", "nonsense", "clarity")
# the checks whose findings bring a turn of the bot's own (the follow-up, the plan check's turn):
# what goes wrong in the kitchen or for someone's health. wishes and clarity are polish: the AI
# still sees them among its facts, but they never make the bot speak by itself
ACT = ("timeline", "health", "nonsense")
PROBLEMS_MAX, PROBLEM_CHARS = 8, 800  # per check: a long list is noise, not a check
RECIPES_MAX = 150_000  # characters of recipe text the judgment gets; the two weeks fit in it
PLAN_KEY = "verifier.plan"  # the plan check's findings, with the key of the plan they are about
PLAN_RETRY = "verifier.plan_retry"  # a review could not run: the plan check goes at the next tick
PLAN_SEEN = (
    "timeline",
    "nonsense",
    "clarity",
)  # what a private chat sees of them: no one's notes or health
SKIP = (
    "unused",
    "stale_reminder",
)  # food on no dish has its own section; the app cancels the other
HEALTH_FIELDS = ("allergies", "intolerances", "diet", "meds", "fasting")

VERIFY = """You check a meal-planning assistant's answer, already sent to the household. The answer
changes something (the menu, a recipe, the cooking days, the pantry, a setting, the reminders or
what was done of a task), or answers the weekly review or a step that was not done. You do not
answer the household: you list what is wrong, so the assistant fixes it in one short follow-up.
{scope}
Make three checks, separately, each on its own, then a fourth and a fifth:
1. wishes: does the proposal, and the menu it leaves, match what the household asked for? The
   notes and chat notes of each person, the household's own instructions and the weekly rules
   below. When people want different things, the person who cooks wins, unless health says no.
2. timeline: what is cooked when, which cooking session prepares what, and what has to be soaked,
   defrosted or cooked ahead. A dish must never count on a session that comes after it is eaten.
   Every step done ahead needs a reminder at the right time (soak the night before, defrost the
   night before, cook or roast before the meal); the proposal's reminders block counts. Two
   reminders at about the same time that repeat a step are a problem. The structural problems
   the app found are certain: list each again here.
3. health: each person's health record below (allergies, intolerances, diet rules, medicines and
   their food rules, fasting). An allergy or intolerance in that person's plate is always a
   problem; a diet rule broken is one; a meal inside a fasting window is one.
4. nonsense: anything plainly wrong: a dish at the wrong meal, impossible amounts, times or
   temperatures, a step that needs equipment the kitchen does not have, an oven step without the
   air-fryer alternative when the kitchen has one, ultra-processed products where real food would
   do, a device used only because it is there, a contradiction with the rest of the answer.
5. clarity: read each recipe's steps as someone cooking it for the first time, with the recipe in
   hand and nobody to ask. Each step must be doable on one reading. A problem is: an ingredient
   that goes in only in part, or is split or served apart, with no word on how much goes in this
   step and what happens to the rest; an amount per serving where the cook sees the total for the
   table, so the two can be taken for each other; a size, time or point of doneness nobody can
   tell; a step that uses something no earlier step made; steps in the wrong order. Quote the
   words, say how they can be misread, and give the step rewritten. Plain and short is not a
   problem: only what a cook could get wrong.

Only timeline, health and nonsense make the assistant write again by itself, so put there only what
would go wrong in the kitchen or for someone's health: a step nobody does, a wrong time or amount,
food that runs out or goes off before it is eaten, a health problem, a contradiction with what
someone said. wishes and clarity are for what would merely be better: keep them few, never repeat
there what the other checks say. A recipe's wording is never a reason to rewrite it.

Judge a recipe from its text below, never from its name: its ingredients, steps and `ahead`
steps are there. A recipe whose text is not below is not yours to judge. A task whose state is not
done may not have happened: never count on it. Its state is only what was stored at its `marked`
time; what the household wrote in the group after it is listed under «Said since», with its time.
The household's latest words win, but only words you can read there: when a line under «Said
since», written after the task was marked or due, says it was done (it was cooked after all, some
is left), counting on it is not a problem. The proposal's own word is never enough: without such a
line, a plan that counts on a task not done is a timeline problem, worded as the question to ask
the household (was it done?).
Each problem in one or two sentences: what is wrong and the change that fixes it, with the dates
and recipe ids. {only} Answer with JSON only, no other text:
{{"wishes": [], "timeline": [], "health": [], "nonsense": [], "clarity": []}}
"""
WHOLE_WEEK = (
    "Check the whole rest of this week and the next as the answer leaves them, not only what it "
    "changes. A problem that was there before the answer (the app lists them under 'already "
    "there') counts only when the answer touches it (the same day, dish, session or step) or "
    "could fix it in passing; otherwise leave it out, it is known. When the answer moves a dish, "
    "a session or a step, say in timeline what that shifts in the rest of the week and how to "
    "reorganise it."
)
WHOLE_WEEK_TURN = (
    "Check the whole rest of this week and the next as the answer leaves them: this turn is about "
    "the week, so every problem in it counts, the ones already there too. When a step was not done "
    "or something moved, say in timeline what that shifts in the rest of the week and how to "
    "reorganise it."
)
PLAN_SCOPE = """
Here there is no proposal: the menu of this week and the next is checked as it stands, the plan
the app made and every change since. Every real problem in it counts. What you put in timeline,
nonsense and clarity is read by every person, in their own chat too: never name there anyone's notes,
health or the foods they avoid; those go in wishes or health.
"""
PLAN_SYSTEM = VERIFY.format(scope=PLAN_SCOPE, only="")


def in_scope(change, offered=(), turn: dict | None = None) -> bool:
    """The answer changes anything: a change block with something in it, reminders it offers or
    cancels, or a turn the bot asked itself to review or reorganise the week (turn["review"])."""
    if isinstance(change, dict) and any(k != "asked" for k in change):
        return True
    return bool(offered) or bool((turn or {}).get("review"))


def review_timeout(data: Data) -> float:
    """How long one review job may take: llm.review_timeout_s, or the answer's own timeout."""
    cfg = data.settings.llm
    return cfg.review_timeout_s or cfg.timeout_s


async def after(
    data: Data,
    db: DB,
    today: dt.date,
    turn: dict,
    answers: list[dict],
    viewer=None,
    asker=None,
    before: set[str] | None = None,
) -> list[str] | None:
    """One review of answers already sent, on the state they left: what is wrong, [] when nothing
    is, None when the review could not run. answers: each {"text": the whole answer, its blocks
    too, "change": its change block, "made": whether it was made, "offered": its reminders}, oldest
    first; several when answers went out while the review before ran. before: the problems' keys
    taken just before the first of them changed anything."""
    text = "\n\n---\n\n".join(a["text"] for a in answers)
    offered = [r for a in answers for r in a.get("offered") or []]
    # a change already made is in the data; the ones still waiting for a yes are made on a copy,
    # in the order they were offered
    waiting = [a["change"] for a in answers if isinstance(a.get("change"), dict) and not a["made"]]
    try:
        items = await findings(
            data,
            db,
            today,
            turn,
            text,
            waiting,
            offered,
            viewer,
            asker,
            before,
            review_timeout(data),
        )
    except Exception as e:  # noqa: BLE001  nothing says it was checked; the plan check retries
        log.warning("verifier failed: %s", type(e).__name__)
        return None
    log.info("verifier: %d problems", len(items))
    return items


async def findings(
    data: Data,
    db: DB,
    today: dt.date,
    turn: dict,
    text: str,
    waiting: list[dict] | dict | None,
    offered: list[dict],
    viewer=None,
    asker=None,
    before: set[str] | None = None,
    timeout: float | None = None,
) -> list[str]:
    """One review: what the structural check and the judgment found. waiting: the blocks still
    waiting for a yes, made on a copy; without one (or when the app would refuse them, which it
    said), the week as it stands. before: the problems there before the answers."""
    chat = (turn.get("chat") or {}).get("id")
    behind = left_behind(db, chat, offered) if chat is not None else []
    if isinstance(waiting, dict):
        waiting = [waiting]
    waiting = [c for c in waiting or [] if isinstance(c, dict) and any(k != "asked" for k in c)]
    found = None
    if waiting:
        found = await asyncio.to_thread(
            structural, data, db, today, waiting, asker, offered, before
        )
    if found is not None:
        problems, after = found
    else:
        after = await asyncio.to_thread(as_is, data, db, today, offered, before)
        problems = after.pop("new", [])
    scope = WHOLE_WEEK_TURN if turn.get("review") else WHOLE_WEEK
    judged = await judge(data, today, text, problems, after, viewer, scope, timeout)
    log.info(
        "verifier: %d structural, %s judged",
        len(problems),
        {c: len(ps) for c, ps in judged.items()},
    )
    # a follow-up only for what goes wrong in the kitchen or for health (ACT), never for polish
    return [*behind, *problems, *(f"{c}: {p}" for c, ps in judged.items() if c in ACT for p in ps)]


SIBLINGS_S = 60  # reminders set within this long of each other came from one answer


def left_behind(db: DB, chat: int, offered: list[dict]) -> list[str]:
    """Reminders this answer leaves as they were while it cancels others set in the same answer
    (one plan, one session): when a time moved, the ones whose text describes it were often
    forgotten. Said to the AI in the follow-up, which keeps them or cancels and rewrites them."""
    ids = {int(r["cancel"]) for r in offered if str(r.get("cancel", "")).isdigit()}
    if not ids:
        return []
    listed = {r.get("text") for r in offered if "cancel" not in r}
    when = []
    for rid in ids:
        r = db.reminder(rid)
        if r and r["chat"] == chat and r.get("created"):
            when.append(dt.datetime.fromisoformat(r["created"]).timestamp())
    left = [
        r
        for r in db.reminders(chat)
        if r["id"] not in ids
        and r["text"] not in listed
        and r.get("created")
        and any(
            abs(dt.datetime.fromisoformat(r["created"]).timestamp() - w) <= SIBLINGS_S for w in when
        )
    ]
    if not left:
        return []
    keep = ", ".join(str(r["id"]) for r in left)
    gone = ", ".join(str(i) for i in sorted(ids))
    said = (
        f"Reminders {keep} were set together with the ones this answer cancels ({gone}) and stay "
        "as they are. If the text or the time of one of them describes what moved (a session's "
        "day or hour, a dish), cancel it and list it again with the right words; if it still "
        "holds, leave it and say nothing about it."
    )
    return [said]


def structural(
    data: Data,
    db: DB,
    today: dt.date,
    change: dict | list[dict],
    asker,
    offered,
    before: set[str] | None = None,
) -> tuple[list[str], dict] | None:
    """The consistency check's problems these changes add, made on a copy one after the other, and
    what the judgment needs of the copy (the menu after them, the recipes they touch). before: the
    problems there before the answers (an answer already made counts as theirs too); taken here
    when not given. None when a block would be refused before anything is made."""
    blocks = change if isinstance(change, list) else [change]
    try:
        changes.parse(blocks[0], data, asker)
    except changes.ChangeError:
        return None
    if before is None:
        before = {p.key for p in doctor.problems(data, db, today)}
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "data"
        shutil.copytree(data.root, copy, ignore=shutil.ignore_patterns(".git", "state"))
        for name in ("plans", "menu-undo", "paused.yaml"):
            src = data.root / "state" / name
            if src.is_dir():
                shutil.copytree(src, copy / "state" / name)
            elif src.exists():
                (copy / "state").mkdir(exist_ok=True)
                shutil.copy2(src, copy / "state" / name)
        sim = data
        for n, block in enumerate(blocks):
            try:
                proposal = changes.parse(block, sim, asker)
                texts = changes.render(copy, proposal)
            except changes.ChangeError:
                if n == 0:
                    return None
                continue  # a later block the app would refuse: the ones before it still count
            for name, text in texts.items():
                (copy / name).parent.mkdir(parents=True, exist_ok=True)
                (copy / name).write_text(text, encoding="utf-8")
            prev = sim
            try:
                sim = load(copy, health=False)
            except DataError as e:
                return [f"The app refuses this change: {str(e)[:300]}"], as_is(
                    data, db, today, offered, before
                )
            retired = changes.retired(proposal, prev)
            if retired:
                menu.retire(sim, retired, today + dt.timedelta(days=1))
            if proposal.plan:
                try:
                    menu.apply(sim, proposal.plan, today)
                except menu.MenuError as e:
                    return [f"The app refuses this menu change: {e}"], as_is(
                        data, db, today, offered, before
                    )
        found = doctor.problems(sim, db, today, offered)
        new = [p.text for p in found if p.kind not in SKIP and p.key not in before]
        merged: dict = {}
        for block in blocks:
            for k, v in block.items():
                if isinstance(v, dict) and isinstance(merged.get(k), dict):
                    merged[k] = {**merged[k], **v}
                else:
                    merged.setdefault(k, v)
        after = week(data, sim, db, today, merged, offered, found, before)
        after.pop("new", None)  # the structural problems: said apart, as this change's doing
        return new, after


def as_is(data: Data, db: DB, today: dt.date, offered, before: set[str] | None = None) -> dict:
    """The week as it stands, with the reminders the answer offers: what a refused change, or an
    answer with nothing to make, leaves. before: the problems there without the answer; worked
    out here when not given (it reads every plan: never on the event loop)."""
    if before is None:
        before = {p.key for p in doctor.problems(data, db, today)}
    return week(data, data, db, today, None, offered, None, before)


def week(
    data: Data,
    sim: Data,
    db: DB,
    today: dt.date,
    change: dict | None = None,
    offered=(),
    found: list | None = None,
    before: set[str] | None = None,
) -> dict:
    """The whole rest of this week and the next as the answer leaves them (sim: the data after the
    change; the data itself when it makes none): the menu, the sessions, every task with what was
    said of it, the text of every recipe on the menu and of each one the change edits or adds, the
    pantry, and every problem the consistency check finds there: "standing" the ones already there
    without the answer (keys in before), "new" the others."""
    t = Translator(sim.locale)
    plans = Plans(sim)
    monday = today - dt.timedelta(days=today.weekday())
    dishes: set[str] = set()
    for i in range(14):
        d = plans.day(monday + dt.timedelta(days=i))
        if d:
            dishes |= {getattr(d, m) for m in MEALS}
    if change:
        dishes |= set(
            (change.get("recipes") or {}) if isinstance(change.get("recipes"), dict) else ()
        )
        rec = change.get("recipe")
        if isinstance(rec, dict) and rec.get("id"):
            dishes.add(str(rec["id"]))
    from .context import pantry_now

    if found is None:
        found = doctor.problems(sim, db, today, list(offered))
    # a recipe nobody can have names someone's food: never in a review that reaches a chat. A step
    # nobody marked is asked about by the morning's day message, and its own turn reorganises the
    # week: every change answer that day asking about it too is noise (as in plan_brief)
    shown = [p for p in found if p.kind not in (*SKIP, "avoided", "unconfirmed")]
    before = before or set()
    return {
        "menu": doctor_menu(sim, t, today),
        "sessions": sessions_now(sim, plans, monday),
        "reminders": tasks.tasks_now(sim, db, monday, monday + dt.timedelta(days=13))
        + thaw_now(sim, dishes),
        "pantry": pantry_now(sim, db, today),
        "standing": [p.text for p in shown if p.key in before],
        "new": [p.text for p in shown if p.key not in before],
        "recipes": recipe_texts(sim, dishes),
    }


def recipe_texts(data: Data, ids: set[str]) -> list[str]:
    """The whole text of each recipe, up to RECIPES_MAX; the ones left out are named, so the check
    knows they are not shown rather than judging them from their names."""
    texts, size, left = [], 0, []
    for rid in sorted(x for x in ids if x and x in data.recipes):
        body = yaml.safe_dump(
            data.recipes[rid].model_dump(exclude_none=True, exclude_defaults=True),
            allow_unicode=True,
            sort_keys=False,
        )
        if size + len(body) > RECIPES_MAX:
            left.append(rid)
            continue
        texts.append(f"### {rid}\n{body}")
        size += len(body)
    if left:
        texts.append(f"(Not shown, too long to send: {', '.join(left)}.)")
    return texts


def doctor_menu(data: Data, t: Translator, today: dt.date) -> list[str]:
    """This week's and next week's menu with the recipe ids, without the list of every recipe."""
    from .context import menu_now

    lines = menu_now(data, t, today)
    cut = next((i for i, x in enumerate(lines) if x.startswith("\n## Recipes")), len(lines))
    return lines[:cut]


async def judge(
    data: Data,
    today: dt.date,
    answer: str,
    problems: list[str],
    after: dict,
    viewer,
    scope: str = WHOLE_WEEK,
    timeout: float | None = None,
) -> dict[str, list[str]]:
    """The provider's checks, each a list of problems. timeout: what is left of the review's time,
    never more than one review's."""
    packet = brief(data, today, answer, problems, after, viewer)
    system = VERIFY.format(scope="", only=scope)
    limit = review_timeout(data) if timeout is None else min(timeout, review_timeout(data))
    out = await asyncio.wait_for(
        llm.complete(data, system, [{"role": "user", "content": packet}]), timeout=limit
    )
    if not answered(out):
        raise llm.LLMError("the review gave no verdict")
    return parse(out)


# ---------------------------------------------------------------- the plan as it stands

PLAN_ANSWER = "(No proposal: the plan of this week and the next as it stands.)"


def plan_brief(data: Data, db: DB, today: dt.date) -> tuple[str, str]:
    """The brief of the plan check and its key. The key leaves out the brief's clock line, so the
    same plan with the same data and the same problems gives the same key all day. It holds what
    the check asks, so a plan already judged is judged again when the checks change."""
    found = doctor.problems(data, db, today)
    # a recipe nobody can have is about the library, not this plan, and names someone's food: the
    # findings of this check reach private chats, so it stays out of the brief. A step nobody
    # marked is asked about by the morning's day message, and its own turn reorganises the week:
    # the plan check raising it too would be a second turn about the same step
    problems = [p.text for p in found if p.kind not in (*SKIP, "avoided", "unconfirmed")]
    t = Translator(data.locale)
    monday = today - dt.timedelta(days=today.weekday())
    plans = Plans(data)
    dishes: set[str] = set()
    for i in range(14):
        day = plans.day(monday + dt.timedelta(days=i))
        if day:
            dishes |= {getattr(day, m) for m in MEALS}
    after = {
        "menu": doctor_menu(data, t, today),
        "sessions": sessions_now(data, plans, monday),
        # every task of the two weeks with what was said of it: the chat's reminders among them
        "reminders": tasks.tasks_now(data, db, monday, monday + dt.timedelta(days=13))
        + thaw_now(data, dishes),
        "recipes": recipe_texts(data, dishes),
    }
    text = brief(data, today, PLAN_ANSWER, problems, after, None)
    body = text.split("\n", 1)[1]
    return text, hashlib.sha256((PLAN_SYSTEM + body).encode()).hexdigest()


def sessions_now(data: Data, plans: Plans, monday: dt.date) -> list[str]:
    """The cooking sessions of the two weeks: when each one is and what it cooks for which days.
    The menu alone does not say it, and a timeline judged without it guesses."""
    batch = [r for r in data.settings.reminders if r.kind == "batch" and r.enabled]

    def evening(d: dt.date) -> str:
        at = [
            x for r in batch if r.days == "daily" or WEEKDAYS[d.weekday()] in r.days for x in r.at
        ]
        return (
            f"in the evening, from the cooking reminder at {', '.join(at)}" if at else "after lunch"
        )

    out = []
    for i in range(14):  # every plan's: a session may come before its own plan starts
        for _, s in plans.sessions_on(monday + dt.timedelta(days=i)):
            dishes = "; ".join(
                f"{x.recipe} for {', '.join(e.isoformat() for e in x.eat)}" for x in s.dishes
            )
            if s.morning:  # the day of its lunch: at the household's start, else in the morning
                at = session_at(data, s.date, True)
                when = (
                    f"from {at}, before lunch" if at != MORNING else "in the morning, before lunch"
                )
            else:
                when = evening(s.date)
            out.append(f"- {s.date:%A} {s.date.isoformat()}, {when}: {dishes}")
    if not out:
        return []
    return ["", "## Cooking sessions (each lunch is cooked in the one that lists its day)", *out]


def thaw_now(data: Data, dishes: set[str]) -> list[str]:
    """What the app's own cooking reminder says every evening without a chat reminder: what to
    take out of the freezer for the next day's meals. A thaw it says is not missing. A reminder
    set for some weekdays only counts on those days (a later time on one day, none on another)."""
    by_day = {
        w: tuple(
            sorted(
                {
                    x
                    for r in data.settings.reminders
                    if r.kind == "batch" and r.enabled and (r.days == "daily" or w in r.days)
                    for x in r.at
                }
            )
        )
        for w in WEEKDAYS
    }
    frozen = sorted(
        {
            data.ingredients[i.id].name
            for rid in dishes
            if rid in data.recipes
            for i in data.recipes[rid].ingredients
            if data.ingredients[i.id].frozen
        }
    )
    if not any(by_day.values()) or not frozen:
        return []
    usual = max(set(by_day.values()), key=lambda t: (list(by_day.values()).count(t), bool(t)))
    other = [
        f"{w.capitalize()} {', '.join(t)}" if t else f"never on {w.capitalize()}"
        for w, t in by_day.items()
        if t != usual
    ]
    at = ", ".join(usual) if usual else "no time"
    if other:
        at += f" ({'; '.join(other)})"
    said = (
        f"- The evening before, at {at}, the cooking reminder says what to take out of"
        f" the freezer for the next day's meals: {', '.join(frozen)}."
    )
    return ["", "## Said by the app itself", said]


async def check_plan(data: Data, db: DB, today: dt.date) -> dict | None:
    """The checks on the plan as it stands, once per plan: {"key", "found", "at"}, kept in the
    database. Runs the judgment only when the key is new; None when it could not run."""
    text, key = await asyncio.to_thread(plan_brief, data, db, today)
    kept = db.get_kv(PLAN_KEY) or {}
    if kept.get("key") == key:
        return kept
    try:
        out = await asyncio.wait_for(
            llm.complete(data, PLAN_SYSTEM, [{"role": "user", "content": text}]),
            timeout=review_timeout(data),
        )
    except Exception as e:  # noqa: BLE001  tried again at the next tick
        log.warning("plan check failed: %s", type(e).__name__)
        return None
    if not answered(out):
        log.warning("plan check: the provider gave no verdict")
        return None  # nothing kept: tried again at the next tick
    found = parse(out)
    log.info("plan check: %s", {c: len(ps) for c, ps in found.items()})
    kept = {"key": key, "found": found, "at": dt.datetime.now(dt.UTC).isoformat()}
    db.set_kv(PLAN_KEY, kept)
    return kept


def plan_findings(data: Data, db: DB, today: dt.date, viewer=None) -> list[str]:
    """What the plan check found about the plan as it is now, for the AI's facts: nothing when the
    plan changed since (the next check is about the new one). In a private chat only the timeline
    and the plain mistakes: the others may name someone else's notes or health."""
    kept = db.get_kv(PLAN_KEY) or {}
    if not kept.get("found") or kept.get("key") != plan_brief(data, db, today)[1]:
        return []
    return [
        f"{c}: {p}"
        for c, ps in kept["found"].items()
        if viewer is None or c in PLAN_SEEN
        for p in ps
    ]


def answered(text: str) -> bool:
    """The provider gave a JSON object, even an empty one: a verdict, not a failure."""
    m = re.search(r"\{.*\}", text or "", re.DOTALL)
    try:
        return isinstance(json.loads(m.group(0)), dict) if m else False
    except ValueError:
        return False


def clip(text: str) -> str:
    """A finding cut to PROBLEM_CHARS at the last sentence end within it, never mid-sentence; at
    the last word when no sentence ends in time."""
    if len(text) <= PROBLEM_CHARS:
        return text
    head = text[: PROBLEM_CHARS + 1]
    ends = [m.end() for m in re.finditer(r"[.!?…](?=\s)", head)]
    if ends:
        return head[: ends[-1]]
    return head[: head.rfind(" ")].rstrip(" ,;:") + "…" if " " in head else text[:PROBLEM_CHARS]


def parse(text: str) -> dict[str, list[str]]:
    """The checks' problems from the provider's JSON; a check it left out, or anything that is not
    a list of texts, counts as none."""
    m = re.search(r"\{.*\}", text or "", re.DOTALL)
    try:
        raw = json.loads(m.group(0)) if m else {}
    except ValueError:
        raw = {}
    out: dict[str, list[str]] = {}
    for check in CHECKS:
        items = raw.get(check) if isinstance(raw, dict) else None
        kept = [
            clip(" ".join(str(x).split()))
            for x in (items if isinstance(items, list) else [])
            if isinstance(x, str) and x.strip()
        ]
        if kept:
            out[check] = kept[:PROBLEMS_MAX]
    return out


def brief(data: Data, today: dt.date, answer: str, problems: list[str], after: dict, viewer) -> str:
    """Everything the checks need, as one message."""
    hh = data.household
    now = dt.datetime.now(ZoneInfo(hh.timezone))
    if now.date() != today:
        now = dt.datetime.combine(today, dt.time(12, 0))
    out = [
        f"Now: {now:%A %Y-%m-%d %H:%M}. Lunch at {hh.lunch_time}.",
        "",
        "## The proposal",
        answer,
    ]
    out += ["", "## Structural problems the app found in it (certain)"]
    out += [f"- {p}" for p in problems] or ["- none"]
    out += ["", "## What the household asked for"]
    for p in hh.people:
        if viewer is not None and p.id != viewer:
            continue
        for note in [*p.notes, *p.chat_notes]:
            out.append(f"- {p.name} ({p.id}): {note}")
        if p.avoid_ingredients:
            out.append(f"- {p.name} ({p.id}) never eats: {', '.join(p.avoid_ingredients)}")
        if p.swaps:  # already on their plate and the list: the recipe text stays as written
            swaps = ", ".join(f"{a} -> {b}" for a, b in p.swaps.items())
            out.append(
                f"- {p.name} ({p.id}) gets instead, done by the app in the dishes plated per "
                f"person, never in a batch recipe (one pot for everyone, cooked as written): {swaps}"
            )
    if data.settings.llm.instructions:
        out += ["", "### The household's own instructions", data.settings.llm.instructions]
    r = hh.rules
    out.append(
        f"\nWeekly rules: legumes >= {r.legumes_min}, fish >= {r.fish_min} (oily >= "
        f"{r.oily_fish_min}), salad every day: {r.salad_daily}. Cooking sessions on "
        f"{', '.join(hh.batch.preferred_days)}"
        + (f", never on {', '.join(hh.batch.avoid_days)}" if hh.batch.avoid_days else "")
        + "."
    )
    if hh.equipment:
        out += ["", "## Kitchen equipment (nothing else)", "- " + ", ".join(hh.equipment)]
    health = health_records(data, viewer)
    if health:
        out += [
            "",
            "## Health records",
            yaml.safe_dump(health, allow_unicode=True, sort_keys=False),
        ]
    out += ["", "## The menu after the change", *after.get("menu", [])]
    out.append(
        "(Only this week and the next are shown. A session may cook for lunches after them: a "
        "dish missing from this menu is not evidence that it is on no meal.)"
    )
    out += after.get("sessions", []) + after.get("reminders", []) + after.get("pantry", [])
    if after.get("standing"):
        out += ["", "## Problems already there before the answer (certain; the app's check)"]
        out += [f"- {p}" for p in after["standing"]]
    if after.get("recipes"):
        out += ["", "## Recipes (every dish of the two weeks, and each one the change edits)"]
        out += after["recipes"]
    return "\n".join(out)


def health_records(data: Data, viewer=None) -> dict:
    """The health export's records of the people this chat may see (everyone in the group, the
    asker in a private chat): only HEALTH_FIELDS, from the last good copy the app keeps."""
    cfg = data.settings.health_export
    if cfg is None:
        return {}
    try:
        doc = yaml.safe_load((data.root / cfg.cache).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    people = doc.get("people") if isinstance(doc, dict) else None
    if not isinstance(people, dict):
        return {}
    ids = {p.id for p in data.household.people}
    return {
        str(pid): {k: v[k] for k in HEALTH_FIELDS if k in v}
        for pid, v in people.items()
        if isinstance(v, dict) and str(pid) in ids and (viewer is None or str(pid) == viewer)
    }
