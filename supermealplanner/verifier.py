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
  medicines with their food rules and fasting windows), and plain nonsense.

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
from .model import Data, DataError, load
from .planner import MEALS, Plans

log = logging.getLogger(__name__)

CHECKS = ("wishes", "timeline", "health", "nonsense")
PROBLEMS_MAX, PROBLEM_CHARS = 8, 400  # per check: a long list is noise, not a check
RECIPES_MAX = 40_000  # characters of recipe text the judgment gets
SKIP = (
    "unused",
    "stale_reminder",
)  # food on no dish has its own section; the app cancels the other
HEALTH_FIELDS = ("allergies", "intolerances", "diet", "meds", "fasting")

VERIFY = """You check a meal-planning assistant's answer before the household sees it. The answer
proposes a change to the menu, a recipe or the cooking days. You do not answer the household: you
list what is wrong with the proposal, so the assistant fixes it.

Make three checks, separately, each on its own, then a fourth:
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

Only real problems in this proposal, each in one or two sentences: what is wrong and the change
that fixes it, with the dates and recipe ids. Never a problem about something the proposal does
not touch and that was already so. Answer with JSON only, no other text:
{"wishes": [], "timeline": [], "health": [], "nonsense": []}
"""

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
        if not in_scope(change):
            return reply
        offered = reply.reminders or llm.extract_reminders(reply.text)[1]
        found = await asyncio.to_thread(structural, data, db, today, change, asker, offered)
        if found is None:
            return reply  # the app refuses the block anyway and says why
        problems, after = found
        judged = await judge(data, today, reply.text, problems, after, viewer)
        items = [*problems, *(f"{c}: {p}" for c, ps in judged.items() for p in ps)]
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
    texts, size = [], 0
    for rid in sorted(x for x in touched if x and x in sim.recipes):
        body = yaml.safe_dump(
            sim.recipes[rid].model_dump(exclude_none=True, exclude_defaults=True),
            allow_unicode=True,
            sort_keys=False,
        )
        if size + len(body) > RECIPES_MAX:
            break
        texts.append(f"### {rid}\n{body}")
        size += len(body)
    return {"menu": lines, "recipes": texts}


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
    out = await asyncio.wait_for(
        llm.complete(data, VERIFY, [{"role": "user", "content": packet}]),
        timeout=data.settings.llm.timeout_s,
    )
    return parse(out)


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
            " ".join(str(x).split())[:PROBLEM_CHARS]
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
