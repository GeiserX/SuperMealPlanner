"""Changes to the menu from the chat: the `plan:` list of a change block.

    plan:
      - set: {date: 2026-01-14, meal: dinner, recipe: tuna-salad}
      - swap: {a: 2026-01-14, b: 2026-01-16, meal: dinner}   # without meal: the whole day
      - pause: {from: 2026-01-12, to: 2026-01-18}            # eat off the plan; ends by itself
      - resume: {from: 2026-01-15, to: 2026-01-18}
      - regenerate: {from: 2026-01-19}                       # a new draw from that Monday
      - shift: {from: 2026-01-12, weeks: 1}                  # the menu from that day on, later
      - use: {date: 2026-01-14, meal: lunch, ingredient: bulgur, instead_of: bread, qty: 60}
      - use: {from: 2026-01-12, to: 2026-01-18, ingredient: bulgur, instead_of: bread}
      - restore: {from: 2026-01-12, to: 2026-01-18, ingredient: bread}  # the recipe's own again
      - undo: true                                           # the last menu change

`use` swaps an ingredient in planned meals, not in the recipe: on one day's meal, or on every meal
of those days whose recipe lists it (meal left out), the meal eats `ingredient` instead of
`instead_of`, `qty` per reference serving or the same amount when left out. The plates, the list,
the batch sheet, the soaks and thaws and the checks all follow it (planner.served). Setting another
dish in that meal drops its swaps; `restore` drops them by hand.

Plans are generated files under state/, outside the data repo's git, so a menu change needs no
token: it is written there, after the planner's validator found no hard rule newly broken. The
plans as they were are kept beside them, so the last change can be undone.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path

from .model import Data
from .planner import (
    PLAN_LOCK,
    Day,
    Dish,
    Plan,
    PlanError,
    Session,
    Swap,
    _eligible,
    generate,
    load_plan,
    paused_days,
    plans_dir,
    repair,
    save_paused,
    validate,
)

log = logging.getLogger("supermealplanner")

MEALS = ("breakfast", "lunch", "side", "snack", "dinner")
FIELDS = {
    "set": {"date", "meal", "recipe", "cook"},
    "swap": {"a", "b", "meal"},
    "pause": {"from", "to"},
    "resume": {"from", "to"},
    "regenerate": {"from"},
    "shift": {"from", "weeks"},
    "use": {"date", "from", "to", "meal", "ingredient", "instead_of", "qty"},
    "restore": {"date", "from", "to", "meal", "ingredient"},
}
USE_MAX = 1000  # g, ml or units per reference serving: more is a typo
SHIFT_MAX = 4  # weeks one shift may move the menu
PAUSE_MAX = 92  # days in one pause: a mistyped year must not silence the plan for good


class MenuError(Exception):
    """A menu change that is not allowed or would break a hard rule. Safe to show."""


@dataclass
class Op:
    kind: str  # set, swap, pause, resume, regenerate, shift, use, restore, undo
    args: dict  # dates already parsed


def _date(value, what: str) -> dt.date:
    try:
        return dt.date.fromisoformat(str(value))
    except ValueError:
        raise MenuError(f"{what} must be a date like 2026-01-14") from None


def _days(args: dict, kind: str) -> tuple[dt.date, dt.date]:
    """One `date`, or `from` and `to` (both included)."""
    if "date" in args:
        if "from" in args or "to" in args:
            raise MenuError(f"{kind} takes a date, or from and to, not both")
        day = _date(args["date"], "date")
        return day, day
    first, last = _date(args.get("from"), "from"), _date(args.get("to"), "to")
    if not first <= last or (last - first).days >= PAUSE_MAX:
        raise MenuError(f"{kind}: from {first} to {last} is not 1 to {PAUSE_MAX} days")
    return first, last


def _unit(data: Data, iid: str, new: dict) -> str | None:
    if iid in data.ingredients:
        return data.ingredients[iid].unit
    if iid not in new:
        return None
    fields = new[iid] if isinstance(new[iid], dict) else {}  # refused with its reason later
    return fields.get("unit", "g")


def parse(
    raw, data: Data, new_recipe: str | None = None, new_ingredients: dict | None = None
) -> list[Op]:
    """Check the `plan:` list of a change block. new_recipe: the id of a recipe the same block
    adds, which a `set` may already use; new_ingredients: the ingredients it adds (id -> fields),
    which a `use` may already use. Raises MenuError with the reason."""
    new_ingredients = new_ingredients if isinstance(new_ingredients, dict) else {}
    if not isinstance(raw, list) or not raw:
        raise MenuError("plan must be a list of menu changes")
    out = []
    for item in raw:
        if not isinstance(item, dict) or len(item) != 1:
            raise MenuError(f"each menu change is one of {', '.join([*FIELDS, 'undo'])}")
        kind, args = next(iter(item.items()))
        if kind == "undo":
            if len(raw) > 1:
                raise MenuError("undo goes alone")
            out.append(Op("undo", {}))
            continue
        if kind not in FIELDS:
            raise MenuError(f"{kind} is not a menu change; use {', '.join([*FIELDS, 'undo'])}")
        if not isinstance(args, dict) or set(args) - FIELDS[kind]:
            raise MenuError(f"{kind} takes {', '.join(sorted(FIELDS[kind]))}")
        meal = args.get("meal")
        if meal is not None and meal not in MEALS:
            raise MenuError(f"{meal} is not a meal; use {', '.join(MEALS)}")
        if kind == "set":
            rid = str(args.get("recipe", ""))
            r = data.recipes.get(rid)
            if meal is None:
                raise MenuError("set needs date, meal and recipe")
            if r is None and rid != new_recipe:
                raise MenuError(f"no recipe {rid}")
            if meal == "side" and r is not None and not r.has("side"):
                raise MenuError(f"{rid} is not a side salad")
            day = _date(args.get("date"), "date")
            op = Op(kind, {"date": day, "meal": meal, "recipe": rid})
            if "cook" in args:  # the day it is cooked, when the household said one
                cook = _date(args["cook"], "cook")
                if meal != "lunch":
                    raise MenuError("cook is the day a lunch is cooked; other meals are made fresh")
                if cook > day:
                    raise MenuError(f"{rid} cannot be cooked on {cook}, after it is eaten on {day}")
                op.args["cook"] = cook
            out.append(op)
        elif kind == "swap":
            a, b = _date(args.get("a"), "a"), _date(args.get("b"), "b")
            if a == b:
                raise MenuError("swap needs two different days")
            out.append(Op(kind, {"a": a, "b": b, "meal": meal}))
        elif kind in ("use", "restore"):
            first, last = _days(args, kind)
            iid = str(args.get("ingredient", ""))
            if _unit(data, iid, new_ingredients) is None:
                raise MenuError(f"no ingredient {iid}")
            op = Op(kind, {"from": first, "to": last, "meal": meal, "ingredient": iid})
            if kind == "use":
                was = str(args.get("instead_of", ""))
                if was not in data.ingredients:
                    raise MenuError(f"no ingredient {was} to use {iid} instead of")
                if was == iid:
                    raise MenuError("use needs two different ingredients")
                qty = args.get("qty")
                if qty is None:
                    if _unit(data, iid, new_ingredients) != data.ingredients[was].unit:
                        raise MenuError(
                            f"{iid} and {was} are in different units: give qty, per serving"
                        )
                elif (
                    isinstance(qty, bool)
                    or not isinstance(qty, int | float)
                    or not 0 < qty <= USE_MAX
                ):
                    raise MenuError(f"qty is the amount per serving, above 0 and up to {USE_MAX}")
                op.args |= {"instead_of": was, "qty": None if qty is None else float(qty)}
            out.append(op)
        elif kind == "regenerate":
            start = _date(args.get("from"), "from")
            if start.weekday() != 0:
                raise MenuError(
                    f"a new draw starts on a Monday and {start} is not one; "
                    "change single dishes with set, or pause the days before it"
                )
            out.append(Op(kind, {"from": start}))
        elif kind == "shift":
            weeks = args.get("weeks")
            if not isinstance(weeks, int) or isinstance(weeks, bool) or not 1 <= weeks <= SHIFT_MAX:
                raise MenuError(f"shift moves the menu 1 to {SHIFT_MAX} whole weeks")
            out.append(Op(kind, {"from": _date(args.get("from"), "from"), "weeks": weeks}))
        else:
            first, last = _date(args.get("from"), "from"), _date(args.get("to"), "to")
            if not first <= last or (last - first).days >= PAUSE_MAX:
                raise MenuError(f"{kind}: from {first} to {last} is not 1 to {PAUSE_MAX} days")
            out.append(Op(kind, {"from": first, "to": last}))
    return out


def describe(ops: list[Op], data: Data, t, names: dict[str, str] | None = None) -> list[str]:
    """One plain line per menu change, in the household's language. names: recipes not in the data
    yet (the one the same block adds)."""

    def name(rid: str | None) -> str:
        if rid in data.recipes:
            return data.recipes[rid].name
        return (names or {}).get(rid) or rid or t("change_none")

    def food(iid: str) -> str:
        if iid in data.ingredients:
            return data.ingredients[iid].name
        return (names or {}).get(iid) or iid

    lines = []
    for op in ops:
        a = op.args
        if op.kind == "set":
            day = _find(_load(data), a["date"], missing_ok=True)
            lines.append(
                t(
                    "change_plan_set",
                    day=t.date(a["date"]),
                    meal=t("meal_" + a["meal"]).lower(),
                    old=name(getattr(day[2], a["meal"]) if day else None),
                    new=name(a["recipe"]),
                )
                + (t("change_plan_cooked", day=t.date(a["cook"])) if a.get("cook") else "")
            )
        elif op.kind == "swap":
            key = "change_plan_swap_meal" if a["meal"] else "change_plan_swap"
            meal = t("meal_" + a["meal"]).lower() if a["meal"] else ""
            lines.append(t(key, a=t.date(a["a"]), b=t.date(a["b"]), meal=meal))
        elif op.kind in ("use", "restore"):
            days = (
                t.date(a["from"])
                if a["from"] == a["to"]
                else t("change_plan_days", first=t.date(a["from"]), last=t.date(a["to"]))
            )
            meal = t("meal_" + a["meal"]).lower() if a["meal"] else t("change_plan_every_meal")
            back = a["ingredient"]
            if op.kind == "restore":  # named as the swap uses it: the meal goes back to its own
                back = _restored(data, a)
            words = {"days": days, "meal": meal, "food": food(back)}
            if op.kind == "use":
                words["old"] = food(a["instead_of"])
                unit = (
                    data.ingredients[a["ingredient"]].unit
                    if a["ingredient"] in data.ingredients
                    else ""
                )
                words["amount"] = (
                    ""
                    if a["qty"] is None
                    else t("change_plan_use_qty", qty=f"{a['qty']:g}", unit=unit)
                )
            lines.append(t(f"change_plan_{op.kind}", **words))
        elif op.kind == "regenerate":
            lines.append(t("change_plan_regenerate", day=t.date(a["from"])))
        elif op.kind == "shift":
            back = a["from"] + dt.timedelta(weeks=a["weeks"])
            lines.append(t("change_plan_shift", day=t.date(a["from"]), back=t.date(back)))
        elif op.kind == "undo":
            lines.append(t("change_plan_undo"))
        else:
            key = f"change_plan_{op.kind}"
            lines.append(t(key, first=t.date(a["from"]), last=t.date(a["to"])))
    return [f"• {x}" for x in lines]


def _restored(data: Data, a: dict) -> str:
    """The recipe's own ingredient a `restore` brings back: the one its swaps replaced."""
    plans = _load(data)
    day = a["from"]
    while day <= a["to"]:
        hit = _find(plans, day, missing_ok=True)
        day += dt.timedelta(days=1)
        for x in hit[2].swaps if hit else []:
            if (a["meal"] in (None, x.meal)) and a["ingredient"] in (x.out, x.use):
                return x.out
    return a["ingredient"]


# ---------------------------------------------------------------- apply


def _load(data: Data) -> dict[Path, Plan]:
    return {p: load_plan(p) for p in sorted(plans_dir(data).glob("*.yaml"))}


def _find(plans: dict[Path, Plan], day: dt.date, missing_ok: bool = False):
    """(path, plan, day) of the newest plan that has `day`."""
    hit = None
    for path, plan in plans.items():
        if plan.start <= day < plan.end and plan.day(day):
            hit = (path, plan, plan.day(day))
    if hit is None and not missing_ok:
        raise MenuError(f"no plan covers {day}")
    return hit


def _put_lunch(plan: Plan, day: Day, recipe: str, cook: dt.date) -> None:
    """Make `recipe`, cooked on `cook`, the lunch of one day: out of the container it was in, into
    the one of that dish and cook date, or a new one."""
    for s in plan.sessions:
        for x in s.dishes:
            if day.date in x.eat:
                x.eat.remove(day.date)
        s.dishes = [x for x in s.dishes if x.eat]
    plan.sessions = [s for s in plan.sessions if s.dishes]
    day.lunch, day.lunch_cook = recipe, cook
    for s in plan.sessions:
        for x in s.dishes:
            if x.recipe == recipe and x.cook == cook:
                x.eat = sorted({*x.eat, day.date})
                return
    session = next(
        (s for s in plan.sessions if s.date == cook and (s.morning or cook < day.date)), None
    )
    if session is None:  # cooked the morning it is eaten when no afternoon comes before
        session = Session(date=cook, morning=cook == day.date)
        plan.sessions.append(session)
        plan.sessions.sort(key=lambda s: (s.date, not s.morning))
    session.dishes.append(Dish(recipe=recipe, cook=cook, eat=[day.date]))


def _salad(data: Data, day: Day) -> None:
    """Keep the daily salad when a main dish changed: add a side salad if neither main is one."""
    if not data.household.rules.salad_daily or day.side:
        return
    mains = [data.recipes.get(day.lunch), data.recipes.get(day.dinner)]
    if any(r is not None and r.has("salad") for r in mains):
        return
    sides = sorted(
        r.id
        for r in data.recipes.values()
        if r.has("side") and r.has("salad") and _eligible(data, r, "lunch", day.date.month)
    )
    if sides:
        day.side = sides[day.date.toordinal() % len(sides)]


def _set(data: Data, plans, op: Op, today: dt.date) -> set[Path]:
    a = op.args
    path, plan, day = _find(plans, a["date"])
    r = data.recipes.get(a["recipe"])
    if r is None:
        raise MenuError(f"no recipe {a['recipe']}")
    # the meal's ingredient swaps were about the dish it replaces
    day.swaps = [x for x in day.swaps if x.meal != a["meal"]]
    if a["meal"] != "lunch":
        setattr(day, a["meal"], r.id)
    else:
        # in the batch session that cooked the lunch it replaces, when that is still to come and
        # the dish keeps until the day; otherwise cooked the same morning
        if a.get("cook"):  # the day the household said: kept to, or refused with the reason
            cook = a["cook"]
            if cook < today:
                raise MenuError(f"{cook} is already past: {r.name} cannot be cooked then")
            gap = (day.date - cook).days
            if gap > r.fridge_days and not r.freezes:
                raise MenuError(
                    f"{r.name} keeps {r.fridge_days} days in the fridge: cooked on {cook} it "
                    f"does not reach {day.date}"
                )
        else:
            # in the batch session that cooked the lunch it replaces, when that is still to come
            # and the dish keeps until the day; otherwise cooked the same morning
            cook = day.lunch_cook
            gap = (day.date - cook).days
            keeps = gap <= r.fridge_days or r.freezes
            if r.kind != "batch" or cook < today or not keeps:
                cook = day.date
        _put_lunch(plan, day, r.id, cook)
    _salad(data, day)
    return {path}


def _swap(data: Data, plans, op: Op) -> set[Path]:
    (pa, plan_a, a), (pb, plan_b, b) = _find(plans, op.args["a"]), _find(plans, op.args["b"])
    for meal in [op.args["meal"]] if op.args["meal"] else MEALS:
        # a meal's ingredient swaps go with it to the other day
        sa, sb = [x for x in a.swaps if x.meal == meal], [x for x in b.swaps if x.meal == meal]
        a.swaps = [x for x in a.swaps if x.meal != meal] + sb
        b.swaps = [x for x in b.swaps if x.meal != meal] + sa
        if meal != "lunch":
            va, vb = getattr(a, meal), getattr(b, meal)
            setattr(a, meal, vb)
            setattr(b, meal, va)
            continue
        la, lb = (a.lunch, a.lunch_cook), (b.lunch, b.lunch_cook)
        for plan, day, (rid, cook) in ((plan_a, a, lb), (plan_b, b, la)):
            # a lunch cooked after its new day, or by another plan, is cooked that morning
            _put_lunch(plan, day, rid, cook if plan.start <= cook <= day.date else day.date)
    _salad(data, a)
    _salad(data, b)
    return {pa, pb}


def _use(data: Data, plans, op: Op, today: dt.date) -> set[Path]:
    """`use` and `restore`: the ingredient swaps of every planned meal from `from` to `to` (one
    meal, or each meal whose recipe lists the ingredient). use: that meal eats `ingredient`
    instead of `instead_of`, which its recipe lists or an earlier swap brought in (that swap is
    replaced, so bread -> couscous -> bulgur is one swap of the bread). restore: the swaps of
    `ingredient` go, so the meal eats its recipe as written. Days already eaten stay as they
    were, and so does a lunch already cooked, except a food served at the meal (`frozen`, like
    bread): the rest is in the pot."""
    a = op.args
    if a["to"] < today:
        raise MenuError(f"{a['to']} is already past: its meals were eaten as they were")
    touched, hits, cooked = set(), 0, []
    day = max(a["from"], today)
    while day <= a["to"]:
        hit = _find(plans, day, missing_ok=True)
        day += dt.timedelta(days=1)
        if hit is None:
            continue
        path, _plan, d = hit
        for meal in [a["meal"]] if a["meal"] else MEALS:
            r = data.recipes.get(getattr(d, meal) or "")
            if r is None:
                continue
            listed = {i.id for i in r.ingredients}
            name = a["ingredient"] if op.kind == "restore" else a["instead_of"]
            gone = [
                x for x in d.swaps if x.meal == meal and x.out in listed and name in (x.out, x.use)
            ]
            was = gone[0].out if gone else name if name in listed else None
            if was is None or (op.kind == "restore" and not gone):
                continue
            if meal == "lunch" and d.lunch_cook < today and not data.ingredients[was].frozen:
                cooked.append(d.date)
                continue
            if op.kind == "restore":
                d.swaps = [x for x in d.swaps if x not in gone]
            else:
                _amount(data, was, a)
                d.swaps = [x for x in d.swaps if x not in gone]
                if was != a["ingredient"]:  # else: back to the recipe's own ingredient
                    d.swaps.append(Swap(meal, was, a["ingredient"], a["qty"]))
            touched.add(path)
            hits += 1
    if not hits and cooked:
        raise MenuError(
            f"the lunch of {cooked[0]} is already cooked: what is in the pot cannot change"
        )
    if not hits:
        what = a["instead_of"] if op.kind == "use" else a["ingredient"]
        where = f"the {a['meal']}" if a["meal"] else "a meal"
        raise MenuError(
            f"{where} planned from {a['from']} to {a['to']} has no {what}"
            + (" swapped" if op.kind == "restore" else "")
        )
    return touched


def _amount(data: Data, was: str, a: dict) -> None:
    """A swap at the same amount needs both ingredients in one unit."""
    use = data.ingredients.get(a["ingredient"])
    if use is None:
        raise MenuError(f"no ingredient {a['ingredient']}")
    if a["qty"] is None and use.unit != data.ingredients[was].unit:
        raise MenuError(f"{use.id} and {was} are in different units: give qty, per serving")


def _regenerate(data: Data, plans, op: Op) -> set[Path]:
    start = op.args["from"]
    path = plans_dir(data) / f"{start.isoformat()}.yaml"
    seed = plans[path].seed + 1000 if path in plans else None  # past every seed that draw tried
    touched = {path}
    for p, plan in list(plans.items()):
        if plan.start >= start:  # replaced by the new draw
            del plans[p]
            touched.add(p)
        elif plan.end > start:  # keeps its days before the new draw, and cooks only for them
            plan.days = [d for d in plan.days if d.date < start]
            for s in plan.sessions:
                for x in s.dishes:
                    x.eat = [e for e in x.eat if e < start]
                s.dishes = [x for x in s.dishes if x.eat]
            plan.sessions = [s for s in plan.sessions if s.dishes]
            touched.add(p)
    previous = max(plans.values(), key=lambda p: p.end) if plans else None
    try:
        from .calendar import busy_afternoons

        busy = busy_afternoons(data.household, start, start + dt.timedelta(days=28))
    except Exception as e:  # noqa: BLE001  a calendar outage must not stop the new draw
        log.warning("calendar unavailable, planning without it: %s", type(e).__name__)
        busy = {}
    try:
        from .db import DB

        ratings = DB(data.state / "supermealplanner.db").ratings()
        plans[path] = generate(
            data, start, 4, busy=busy, previous=previous, seed=seed, ratings=ratings
        )
    except PlanError as e:
        raise MenuError(str(e)[:300]) from None
    return touched


def _shift(data: Data, plans, op: Op) -> set[Path]:
    """Move every planned day from `from` on later by whole weeks, so weekdays (batch days, the
    injection day) still line up. The plan that holds `from` keeps its start and grows by those
    weeks, so the paused gap stays covered and the hourly check draws nothing for it; a plan that
    starts later moves whole. A lunch cooked before `from` for a day that moves is cooked again
    on `from` at the earliest (a paused day, so the first day back cooks it). Calendar marks stay
    with their dates, not with the dishes."""
    first, delta = op.args["from"], dt.timedelta(weeks=op.args["weeks"])

    def cook(c: dt.date) -> dt.date:
        return max(c + delta, first)

    touched = set()
    for path, plan in list(plans.items()):
        if plan.end <= first:
            continue
        touched.add(path)
        for d in plan.days:
            if d.date >= first:
                d.date, d.lunch_cook, d.busy = d.date + delta, cook(d.lunch_cook), []
        sessions: dict[tuple[dt.date, bool], Session] = {}
        for s in plan.sessions:
            for x in s.dishes:
                stay = [e for e in x.eat if e < first]
                move = [e + delta for e in x.eat if e >= first]
                if stay:
                    key = (s.date, s.morning)
                    sessions.setdefault(key, Session(s.date, s.morning, list(s.busy)))
                    sessions[key].dishes.append(Dish(x.recipe, x.cook, stay))
                if move:
                    c = cook(x.cook)
                    key = (c, c == move[0])
                    sessions.setdefault(key, Session(c, c == move[0]))
                    sessions[key].dishes.append(Dish(x.recipe, c, move))
        plan.sessions = sorted(sessions.values(), key=lambda s: (s.date, not s.morning))
        if plan.start > first:  # starts later: the whole plan moves, and so does its file
            plan.start += delta
            del plans[path]
            new = plans_dir(data) / f"{plan.start.isoformat()}.yaml"
            plans[new] = plan
            touched.add(new)
        else:
            plan.weeks += op.args["weeks"]
    return touched


def _undated(errors) -> set[str]:
    """Validator errors without their dates: a shift moves an old error, it does not add one."""
    return {re.sub(r"\d{4}-\d{2}-\d{2}", "", e) for e in errors}


def _undo_dir(data: Data) -> Path:
    return data.state / "menu-undo"


def _snapshot(data: Data, by: str = "person") -> None:
    """Keep the plans and the paused days as they are now, replacing the last copy. by: who made
    the change it is kept for ("bot": an automatic turn), so the bot undoes only its own."""
    keep = _undo_dir(data)
    shutil.rmtree(keep, ignore_errors=True)
    (keep / "plans").mkdir(parents=True)
    (keep / "by").write_text(by, encoding="utf-8")
    for p in plans_dir(data).glob("*.yaml"):
        shutil.copy2(p, keep / "plans" / p.name)
    paused = data.state / "paused.yaml"
    if paused.exists():
        shutil.copy2(paused, keep / "paused.yaml")


def _undo(data: Data, by: str = "person") -> None:
    keep = _undo_dir(data)
    if not (keep / "plans").is_dir():
        raise MenuError("there is no menu change to undo")
    made = (keep / "by").read_text(encoding="utf-8") if (keep / "by").exists() else "person"
    if by == "bot" and made != "bot":
        raise MenuError("the last menu change was the household's: only they undo it")
    for p in plans_dir(data).glob("*.yaml"):
        p.unlink()
    plans_dir(data).mkdir(parents=True, exist_ok=True)
    for p in (keep / "plans").glob("*.yaml"):
        shutil.copy2(p, plans_dir(data) / p.name)
    paused = data.state / "paused.yaml"
    paused.unlink(missing_ok=True)
    if (keep / "paused.yaml").exists():
        shutil.copy2(keep / "paused.yaml", paused)
    shutil.rmtree(keep, ignore_errors=True)


def retire(data: Data, pairs: set[tuple[str, str]], start: dt.date) -> int:
    """A recipe was taken out of a meal: give the days planned from `start` on another dish for it.
    Returns how many days changed. The plans as they were are kept for undo."""
    with PLAN_LOCK:
        changed, days = {}, 0
        for path, plan in _load(data).items():
            before = [asdict(d) for d in plan.days]
            repair(data, plan, start, retired=pairs)
            moved = sum(1 for old, d in zip(before, plan.days, strict=True) if old != asdict(d))
            if moved:
                changed[path] = plan
                days += moved
        if changed:
            _snapshot(data)
            for path, plan in changed.items():
                plan.save(path)
        return days


def apply(data: Data, ops: list[Op], today: dt.date, by: str = "person") -> None:
    """Make the menu changes, all or none. Raises MenuError when a day has no plan or a hard rule
    of the validator would newly break; then nothing is written. The limit on how often a dish
    is cooked is not one of them here: it shapes a draw, and this dish was asked for. by: "bot"
    for an automatic turn's change, which may undo only a change of its own."""
    with PLAN_LOCK:
        if [op.kind for op in ops] == ["undo"]:
            _undo(data, by)
            return
        plans = _load(data)
        known = {p: set(validate(data, plan, repeats=False)) for p, plan in plans.items()}
        was = paused_days(data)
        paused = set(was)
        touched: set[Path] = set()
        for op in ops:
            if op.kind == "set":
                touched |= _set(data, plans, op, today)
            elif op.kind == "swap":
                touched |= _swap(data, plans, op)
            elif op.kind in ("use", "restore"):
                touched |= _use(data, plans, op, today)
            elif op.kind == "regenerate":
                touched |= _regenerate(data, plans, op)
            elif op.kind == "shift":
                touched |= _shift(data, plans, op)
                # the days the menu left are paused, so nothing new is drawn for them; paused
                # days after `from` move with the dishes
                first, delta = op.args["from"], dt.timedelta(weeks=op.args["weeks"])
                gap = {first + dt.timedelta(days=i) for i in range(delta.days)}
                paused = {d if d < first else d + delta for d in paused} | gap
            else:
                span = (op.args["to"] - op.args["from"]).days + 1
                days = {op.args["from"] + dt.timedelta(days=i) for i in range(span)}
                paused = paused | days if op.kind == "pause" else paused - days
        shifted = any(op.kind == "shift" for op in ops)
        before = _undated(e for errs in known.values() for e in errs)
        for p in sorted(touched & set(plans)):
            errs = validate(data, plans[p], repeats=False)
            if shifted:
                new = [e for e in errs if _undated([e]) - before]
            else:
                new = [e for e in errs if e not in known.get(p, ())]
            if new:
                raise MenuError("; ".join(new[:3]))
        if not touched and paused == was:
            return  # nothing changes: the copy for undo stays the one of the last real change
        _snapshot(data, by)
        for p in touched:
            if p in plans:
                plans[p].save(p)
            else:
                p.unlink(missing_ok=True)
        if paused != was:
            # days long gone are dropped, so the file never grows
            save_paused(data, {d for d in paused if d >= today - dt.timedelta(days=35)})
