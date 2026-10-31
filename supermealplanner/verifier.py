"""The verifier: a second look at an answer that changes the plan, the menu or the recipes, before it
reaches the household.

It runs only on an answer whose change block touches the menu (`plan`), a recipe (`recipe`,
`recipes`) or the batch days (`household.batch.*`): that is where a dish can end up eaten before what
it needs is ready. Every other answer goes out as it is.

Two checks:
- structural, plain code: the change is made on a copy of the data folder and the consistency check
  (doctor.problems) runs on the copy, with the reminders the answer offers counted as set. What it
  finds that was not there before is this change's doing. A menu change the app would refuse is one
  of them, with the app's reason.
- judgment, one call to the household's AI provider (llm.complete): three separate checks of the
  proposal, each answered on its own: (1) what the household asked for (each person's notes and chat
  notes, the household's own instructions, the weekly rules), (2) the timeline (what is cooked when,
  which session prepares what, what has to be soaked, defrosted or cooked ahead, and a reminder at
  the right time for each), (3) health (the health export's allergies, intolerances, diet rules,
  medicines with their food rules and fasting windows), and plain nonsense, and (4) clarity: whether
  someone cooking from the steps, with nobody to ask, understands them on one reading.

The same checks run on the plan as it stands (check_plan): once for each new or changed menu of
this week and the next, with the full text of every recipe on it. What they find is kept with the
plan's key, so the AI sees it among the problems to fix in every turn until the plan changes, and
the bot raises it once in the morning so the chef proposes the fixes in the chat.

When either finds something, the problems go back to the provider once, in the same conversation,
and its new answer replaces the first; nobody saw the first. One check and at most one revision per
answer: what the revision cannot fix, it says in its answer, and a change that still leaves a
structural problem gets the consistency check's own line once it is made. Any failure here hands the
first answer on as it was: the verifier never stops an answer.

Health data stays where it was: it goes only to the provider, which already gets it, in a private
chat only the asker's, and nothing here logs a problem's text.
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
from dataclasses import replace
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from . import changes, doctor, llm, menu
from .db import DB
from .i18n import Translator
from .model import WEEKDAYS, Data, DataError, load
from .planner import MEALS, Plans

log = logging.getLogger(__name__)

CHECKS = ("wishes", "timeline", "health", "nonsense", "clarity")
PROBLEMS_MAX, PROBLEM_CHARS = 8, 800  # per check: a long list is noise, not a check
RECIPES_MAX = 150_000  # characters of recipe text the judgment gets; the two weeks fit in it
PLAN_KEY = "verifier.plan"  # the plan check's findings, with the key of the plan they are about
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

VERIFY = """You check a meal-planning assistant's answer before the household sees it. The answer
proposes a change to the menu, a recipe or the cooking days. You do not answer the household: you
list what is wrong with the proposal, so the assistant fixes it.
{scope}
Make three checks, separately, each on its own, then a fourth and a fifth:
1. wishes: does the proposal, and the menu it leaves, match what the household asked for? The
   notes and chat notes of each person, the household's own instructions and the weekly rules
   below. When people want different things, the person who cooks wins, unless health says no.
2. timeline: what is cooked when, which cooking session prepares what, and what has to be soaked,
   defrosted or cooked ahead. A dish must never count on a session that comes after it is eaten.
   Every step done ahead needs a reminder at the right time (soak the night before, defrost the
   night before, cook or roast before the meal); the proposal's reminders block counts. The
   structural problems the app found are certain: list each again here.
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

Judge a recipe from its text below, never from its name: its ingredients, steps and `ahead`
steps are there. A recipe whose text is not below is not yours to judge.
Each problem in one or two sentences: what is wrong and the change that fixes it, with the dates
and recipe ids. {only} Answer with JSON only, no other text:
{{"wishes": [], "timeline": [], "health": [], "nonsense": [], "clarity": []}}
"""
ONLY_PROPOSAL = (
    "Only real problems in this proposal: never one about something it does not touch and that "
    "was already so."
)
PLAN_SCOPE = """
Here there is no proposal: the menu of this week and the next is checked as it stands, the plan
the app made and every change since. Every real problem in it counts. What you put in timeline,
nonsense and clarity is read by every person, in their own chat too: never name there anyone's notes,
health or the foods they avoid; those go in wishes or health.
"""
PLAN_SYSTEM = VERIFY.format(scope=PLAN_SCOPE, only="")

REVISE = """[The app checked your answer before anyone saw it; nobody has seen it yet.] It found:
{problems}

Write your answer again, whole, with these fixed: the new answer replaces the one above, with its
change, reminders and options blocks. If you cannot fix one, keep the rest and say in one short line
what is left and why. Do not mention this check."""


def in_scope(change) -> bool:
    """The change touches the menu, a recipe or the batch days."""
    if not isinstance(change, dict):
        return False
    if change.get("plan") or change.get("recipe") or change.get("recipes"):
        return True
    sets = change.get("set")
    return isinstance(sets, dict) and any(str(k).startswith("household.batch") for k in sets)


async def review(
    data: Data, db: DB, today: dt.date, turn: dict, reply: llm.Reply, viewer=None, asker=None
) -> llm.Reply:
    """The reply, revised once when the checks found problems in the change it proposes; the reply
    as it was when there is nothing to check or anything fails."""
    try:
        _, change = changes.extract(reply.text)
        offered = reply.reminders or llm.extract_reminders(reply.text)[1]
        chat = (turn.get("chat") or {}).get("id")
        behind = left_behind(db, chat, offered) if chat is not None else []
        if not in_scope(change):
            return await revise(data, turn, reply, behind) if behind else reply
        found = await asyncio.to_thread(structural, data, db, today, change, asker, offered)
        if found is None:
            return reply  # the app refuses the block anyway and says why
        problems, after = found
        judged = await judge(data, today, reply.text, problems, after, viewer)
        items = [*behind, *problems, *(f"{c}: {p}" for c, ps in judged.items() for p in ps)]
        log.info(
            "verifier: %d structural, %s judged",
            len(problems),
            {c: len(ps) for c, ps in judged.items()},
        )
        if not items:
            return reply
        return await revise(data, turn, reply, items)
    except Exception as e:  # noqa: BLE001  never in the way of the answer
        log.warning("verifier failed: %s", type(e).__name__)
        return reply


SIBLINGS_S = 60  # reminders set within this long of each other came from one answer


def left_behind(db: DB, chat: int, offered: list[dict]) -> list[str]:
    """Reminders this answer leaves as they were while it cancels others set in the same answer
    (one plan, one session): when a time moved, the ones whose text describes it were often
    forgotten. Said to the AI once, which keeps them or cancels and rewrites them."""
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
    data: Data, db: DB, today: dt.date, change: dict, asker, offered
) -> tuple[list[str], dict] | None:
    """The consistency check's problems this change adds, on a copy, and what the judgment needs of
    the copy (the menu after the change, the recipes it touches). None when the block would be
    refused before anything is made."""
    try:
        proposal = changes.parse(change, data, asker)
    except changes.ChangeError:
        return None
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
        try:
            texts = changes.render(data.root, proposal)
        except changes.ChangeError:
            return None
        for name, text in texts.items():
            (copy / name).parent.mkdir(parents=True, exist_ok=True)
            (copy / name).write_text(text, encoding="utf-8")
        try:
            sim = load(copy, health=False)
        except DataError as e:
            return [f"The app refuses this change: {str(e)[:300]}"], _seen(data, data, today)
        retired = changes.retired(proposal, data)
        if retired:
            menu.retire(sim, retired, today + dt.timedelta(days=1))
        if proposal.plan:
            try:
                menu.apply(sim, proposal.plan, today)
            except menu.MenuError as e:
                return [f"The app refuses this menu change: {e}"], _seen(data, data, today)
        found = doctor.problems(sim, db, today, offered)
        new = [p.text for p in found if p.kind not in SKIP and p.key not in before]
        return new, _seen(data, sim, today, change)


def _seen(data: Data, sim: Data, today: dt.date, change: dict | None = None) -> dict:
    """The menu of this week and the next as the change leaves it, and the text of the recipes it
    touches: the ones it edits or adds and the dishes it puts on a day."""
    t = Translator(sim.locale)
    lines = doctor_menu(sim, t, today)
    old, new = Plans(data), Plans(sim)
    monday = today - dt.timedelta(days=today.weekday())
    touched: set[str] = set()
    for i in range(14):
        d = monday + dt.timedelta(days=i)
        a, b = old.day(d), new.day(d)
        dishes_a = {getattr(a, m) for m in MEALS} if a else set()
        dishes_b = {getattr(b, m) for m in MEALS} if b else set()
        touched |= dishes_b - dishes_a
    if change:
        touched |= set(
            (change.get("recipes") or {}) if isinstance(change.get("recipes"), dict) else ()
        )
        rec = change.get("recipe")
        if isinstance(rec, dict) and rec.get("id"):
            touched.add(str(rec["id"]))
    return {"menu": lines, "recipes": recipe_texts(sim, touched)}


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
    data: Data, today: dt.date, answer: str, problems: list[str], after: dict, viewer
) -> dict[str, list[str]]:
    """The provider's three checks and the nonsense one, each a list of problems."""
    packet = brief(data, today, answer, problems, after, viewer)
    system = VERIFY.format(scope="", only=ONLY_PROPOSAL)
    out = await asyncio.wait_for(
        llm.complete(data, system, [{"role": "user", "content": packet}]),
        timeout=data.settings.llm.timeout_s,
    )
    return parse(out)


# ---------------------------------------------------------------- the plan as it stands

PLAN_ANSWER = "(No proposal: the plan of this week and the next as it stands.)"


def plan_brief(data: Data, db: DB, today: dt.date) -> tuple[str, str]:
    """The brief of the plan check and its key. The key leaves out the brief's clock line, so the
    same plan with the same data and the same problems gives the same key all day. It holds what
    the check asks, so a plan already judged is judged again when the checks change."""
    found = doctor.problems(data, db, today)
    # a recipe nobody can have is about the library, not this plan, and names someone's food: the
    # findings of this check reach private chats, so it stays out of the brief
    problems = [p.text for p in found if p.kind not in (*SKIP, "avoided")]
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
        "reminders": reminders_now(data, db, monday) + thaw_now(data, dishes),
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

    out, seen = [], set()
    for i in range(14):
        plan = plans.plan(monday + dt.timedelta(days=i))
        for s in plan.sessions if plan else []:
            if s.date != monday + dt.timedelta(days=i) or (s.date, s.morning) in seen:
                continue
            seen.add((s.date, s.morning))
            dishes = "; ".join(
                f"{x.recipe} for {', '.join(e.isoformat() for e in x.eat)}" for x in s.dishes
            )
            when = "in the morning, before lunch" if s.morning else evening(s.date)
            out.append(f"- {s.date:%A} {s.date.isoformat()}, {when}: {dishes}")
    if not out:
        return []
    return ["", "## Cooking sessions (each lunch is cooked in the one that lists its day)", *out]


def household_chat(data: Data, db: DB) -> int | None:
    """The group the reminders go to: the one Telegram moved it to while settings still name the
    old one, as the bot resolves it."""
    configured = data.settings.bot.chat_id
    m = db.get_kv("bot.migrated_chat")
    if isinstance(m, dict) and configured is not None and configured == m.get("from"):
        return m["to"]
    return configured


def reminders_now(data: Data, db: DB, monday: dt.date) -> list[str]:
    """The reminders set in the household chat for the two weeks, still to come or already sent:
    a step one of them covers is not missing. Each one as JSON, so its text is data."""
    chat = household_chat(data, db)
    if chat is None:
        return []
    zone = ZoneInfo(data.household.timezone)
    since = dt.datetime.combine(monday, dt.time(), zone).timestamp()
    rows = [r for r in db.reminders(chat, "sent") if r["at"] >= since] + db.reminders(chat)
    if not rows:
        return []
    out = ["", "## Reminders set in the household chat (sent, or still to come)"]
    for r in rows:
        local = dt.datetime.fromtimestamp(r["at"], zone)
        row = {"when": f"{local:%A %Y-%m-%d %H:%M}", "state": r["state"], "text": r["text"]}
        if r.get("recipe"):
            row |= {"recipe": r["recipe"], "date": r["date"]}
        out.append("- " + json.dumps(row, ensure_ascii=False))
    return out


def thaw_now(data: Data, dishes: set[str]) -> list[str]:
    """What the app's own cooking reminder says every evening without a chat reminder: what to
    take out of the freezer for the next day's meals. A thaw it says is not missing."""
    at = sorted(
        {
            x
            for r in data.settings.reminders
            if r.kind == "batch" and r.enabled and r.days == "daily"
            for x in r.at
        }
    )
    frozen = sorted(
        {
            data.ingredients[i.id].name
            for rid in dishes
            if rid in data.recipes
            for i in data.recipes[rid].ingredients
            if data.ingredients[i.id].frozen
        }
    )
    if not at or not frozen:
        return []
    said = (
        f"- The evening before, at {', '.join(at)}, the cooking reminder says what to take out of"
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
            timeout=data.settings.llm.timeout_s,
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
            out.append(f"- {p.name} ({p.id}) gets instead, done by the app: {swaps}")
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
    out += after.get("sessions", []) + after.get("reminders", [])
    if after.get("recipes"):
        out += ["", "## Recipes it touches", *after["recipes"]]
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


async def revise(data: Data, turn: dict, reply: llm.Reply, problems: list[str]) -> llm.Reply:
    """One more turn in the same conversation with the problems; its answer replaces the first.
    An empty or failed revision keeps the first."""
    ts = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    note = REVISE.format(problems="\n".join(f"- {p}" for p in problems))
    again = {
        **turn,
        "conversation": reply.conversation or turn.get("conversation"),
        "new": [{"role": "user", "content": note, "ids": [], "ts": ts}],
        "history": [
            *turn.get("history", []),
            *turn.get("new", []),
            {"role": "assistant", "content": reply.text, "ids": [], "ts": ts},
        ],
        "attachments": [],
    }
    try:
        out = await asyncio.wait_for(llm.converse(data, again), timeout=data.settings.llm.timeout_s)
    except Exception as e:  # noqa: BLE001
        log.warning("verifier: the revision failed: %s", type(e).__name__)
        return reply
    if not out.text.strip() and not out.proposals:
        return reply
    return replace(
        out,
        conversation=out.conversation or reply.conversation,
        covers=reply.covers | out.covers,
    )
