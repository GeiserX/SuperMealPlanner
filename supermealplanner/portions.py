"""Plates per person, day totals, the weekly shopping list and batch sheets.

Everything here is computed at render time from the plan (which recipes, when) and the current
targets (how much). Changing a target never needs a new plan.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field

from .db import DB
from .model import Data, Ingredient, Recipe
from .planner import MEALS, Plan
from .targets import current_kcal, day_factor

MEAL_ORDER = MEALS


def factors(data: Data, db: DB, day: dt.date) -> dict[str, float]:
    hh = data.household
    return {p.id: day_factor(hh, p, current_kcal(db, p), day) for p in hh.people}


def plural(name: str, n: float, locale: str = "en", explicit: str | None = None) -> str:
    """Plural of a kitchen unit on its first word ("clove of garlic" -> "cloves of garlic")."""
    if n <= 1:
        return name
    if explicit:
        return explicit
    head, _, tail = name.partition(" ")
    if locale == "es":
        if head.endswith(("s", "x")):
            pass
        elif head[-1:] in "aeiouáéó":
            head += "s"
        elif head.endswith("z"):
            head = head[:-1] + "ces"
        else:
            # limón -> limones: the stress mark goes once the word gains a syllable
            if len(head) > 1 and head[-2] in "áéíóú" and head[-1] in "ns":
                head = head[:-2] + "aeiou"["áéíóú".index(head[-2])] + head[-1]
            head += "es"
    else:
        if head.endswith(("s", "x", "ch", "sh")):
            head += "es"
        elif head.endswith("y") and head[-2:-1] not in "aeiou":
            head = head[:-1] + "ies"
        else:
            head += "s"
    return f"{head} {tail}".strip()


def _unit_plural(ing: Ingredient, n: float, locale: str) -> str:
    k = ing.kitchen
    return plural(k.name, n, locale, getattr(k, "plural", None)) if k else "ud"


def fmt_qty(ing: Ingredient, qty: float, locale: str = "en") -> str:
    if ing.unit == "ud":
        return f"{_nice(qty)} {_unit_plural(ing, qty, locale)}"
    unit = ing.unit
    s = f"{round(qty):d} {unit}" if qty >= 10 else f"{qty:.1f} {unit}".replace(".0 ", " ")
    if ing.kitchen and qty >= ing.kitchen.g * 0.4:
        k = qty / ing.kitchen.g
        s += f" (≈ {_nice(k)} {_unit_plural(ing, round(k * 2) / 2, locale)})"
    return s


def _nice(x: float) -> str:
    """Round to the nearest half for kitchen counts."""
    h = round(x * 2) / 2
    if h == 0:
        h = 0.5
    return f"{int(h)}" if h == int(h) else f"{int(h)}½" if h > 1 else "½"


@dataclass
class Plate:
    recipe: Recipe
    factor: float
    lines: list[str]
    kcal: float
    protein: float


def amounts(data: Data, r: Recipe, servings: dict[str, float]) -> dict[str, float]:
    """Ingredient quantities of a recipe for some eaters (person id, or any other key such as
    "guests", who eat the recipe as written), each with their servings. A person's swaps replace
    an ingredient in their share only. Ordered as the recipe lists them, a swap next to the
    ingredient it replaces."""
    people = {p.id: p.swaps for p in data.household.people}
    out: dict[str, float] = {}
    for item in r.ingredients:
        for eater, n in servings.items():
            iid = people.get(eater, {}).get(item.id, item.id)
            out[iid] = out.get(iid, 0) + item.qty * n
    return out


def _lines(data: Data, qty: dict[str, float]) -> list[str]:
    out = []
    for iid, q in qty.items():
        ing = data.ingredients[iid]
        out.append(f"{ing.name}: {fmt_qty(ing, q, data.locale)}")
    return out


def plate(data: Data, r: Recipe, factor: float, swaps: dict[str, str] | None = None) -> Plate:
    """One plate: the recipe at `factor` reference servings, with the eater's swaps."""
    qty: dict[str, float] = {}
    for item in r.ingredients:
        iid = (swaps or {}).get(item.id, item.id)
        qty[iid] = qty.get(iid, 0) + item.qty * factor
    lines = _lines(data, qty)
    return Plate(
        recipe=r,
        factor=factor,
        lines=lines,
        kcal=r.nutrition.kcal * factor,
        protein=r.nutrition.protein_g * factor,
    )


@dataclass
class PersonDay:
    person_id: str
    name: str
    factor: float
    kcal_target: float
    protein_target: float
    plates: dict[str, Plate] = field(default_factory=dict)
    pills: dict[str, list] = field(default_factory=dict)
    toppers: list[str] = field(default_factory=list)
    days_since_injection: int | None = None

    @property
    def kcal(self) -> float:
        return sum(p.kcal for p in self.plates.values())

    @property
    def protein(self) -> float:
        return sum(p.protein for p in self.plates.values())


def person_days(data: Data, db: DB, plan: Plan, day: dt.date) -> list[PersonDay]:
    from .targets import days_since_injection

    d = plan.day(day)
    out = []
    if d is None:
        return out
    for p in data.household.people:
        kcal = current_kcal(db, p)
        f = day_factor(data.household, p, kcal, day)
        pd = PersonDay(
            person_id=p.id,
            name=p.name,
            factor=f,
            kcal_target=round(f * data.household.reference_kcal),
            protein_target=p.protein_g.target,
            pills=p.pills_on(day),
            days_since_injection=days_since_injection(p, day),
        )
        for meal in MEALS:
            rid = getattr(d, meal)
            if rid and rid in data.recipes:
                pd.plates[meal] = plate(data, data.recipes[rid], f, p.swaps)
        short = p.protein_g.target - pd.protein
        for top in data.settings.toppers:
            if short <= 5:
                break
            pd.toppers.append(top.label)
            short -= top.protein_g
        out.append(pd)
    return out


# ---------------------------------------------------------------- shopping


@dataclass
class ShopLine:
    ingredient: Ingredient
    qty: float
    locale: str = "en"
    pick: object | None = None  # stores.Pick for the store being shown
    alternative: object | None = None  # stores.Pick at the alternative store

    @property
    def amount(self) -> str:
        ing = self.ingredient
        if ing.unit == "ud":
            n = math.ceil(self.qty)
            return f"{n} {_unit_plural(ing, n, self.locale)}"
        if self.qty >= 1000:
            s = f"{self.qty / 1000:.2f} {'kg' if ing.unit == 'g' else 'l'}".replace(".00", "")
        else:
            s = f"{int(math.ceil(self.qty / 10) * 10)} {ing.unit}"
        if ing.kitchen:
            n = math.ceil(self.qty / ing.kitchen.g)
            s += f" (≈ {n} {_unit_plural(ing, n, self.locale)})"
        return s


@dataclass
class StoreList:
    id: str
    name: str
    sections: dict[str, list[ShopLine]]
    total: float = 0.0
    unpriced: int = 0

    @property
    def lines(self) -> list[ShopLine]:
        return [x for v in self.sections.values() for x in v]


@dataclass
class Shopping:
    view: str | None  # the store the list is written for, or None for by-ingredient
    stores: list[StoreList]
    views: list[tuple[str, str]]  # (store id, label) the user can switch to
    labels: dict[str, str] = field(default_factory=dict)
    have: list[Ingredient] = field(default_factory=list)  # needed, but already at home


def servings_by_eater(
    data: Data, db: DB, plan: Plan, monday: dt.date
) -> dict[str, dict[str, float]]:
    """Reference servings to buy per recipe and eater (person id, or "guests") for the week
    starting `monday`.

    Lunch dishes are bought for the week they are cooked in; everything else for the day it is eaten.
    Sunday lunch adds the household's guest servings.
    """
    hh = data.household
    week = {monday + dt.timedelta(days=i) for i in range(7)}
    total: dict[str, dict[str, float]] = {}

    def add(rid: str, eaters: dict[str, float]):
        row = total.setdefault(rid, {})
        for eater, n in eaters.items():
            if n:
                row[eater] = row.get(eater, 0) + n

    guests = hh.guests.get("sunday_lunch", 0)
    for s in plan.sessions:
        for x in s.dishes:
            if x.cook in week:
                for e in x.eat:
                    add(x.recipe, factors(data, db, e))
                    if e.weekday() == 6:
                        add(x.recipe, {"guests": guests})
    for d in plan.days:
        if d.date in week:
            f = factors(data, db, d.date)
            for meal in ("breakfast", "dinner", "snack", "side"):
                rid = getattr(d, meal)
                if rid:
                    add(rid, f)
    return total


def servings_for_week(data: Data, db: DB, plan: Plan, monday: dt.date) -> dict[str, float]:
    """Reference servings to buy per recipe for the week starting `monday`, everyone together."""
    return {
        rid: sum(row.values()) for rid, row in servings_by_eater(data, db, plan, monday).items()
    }


def needs(data: Data, db: DB, plan: Plan, monday: dt.date) -> dict[str, float]:
    """Ingredient quantities for the week, in each ingredient's unit, with each person's swaps."""
    need: dict[str, float] = {}
    for rid, row in servings_by_eater(data, db, plan, monday).items():
        if rid not in data.recipes:
            continue
        for iid, q in amounts(data, data.recipes[rid], row).items():
            need[iid] = need.get(iid, 0) + q
    return need


def shopping_list(data: Data, db: DB, plan: Plan, monday: dt.date, t=None, view: str | None = None):
    """The week's list, grouped by store and section, with the product to buy where a catalogue has one.

    view: a store id to write the whole list for that store (e.g. an online order); by default the
    configured primary store, with each ingredient under the store its entry names.
    """
    from .i18n import Translator
    from .stores import catalogs, pick

    t = t or Translator(data.locale)
    cats = catalogs(data)
    cfg = data.settings.shopping
    labels = {sid: s.name for sid, s in data.settings.stores.items()}
    for sid, c in cats.items():
        labels.setdefault(sid, c.name or sid)
    primary = view or cfg.primary
    alt_ids = [a for a in cfg.alternatives if a != primary and a in cats]

    groups: dict[str, dict[str, list[ShopLine]]] = {}
    at_home = db.have()
    have: list[Ingredient] = []
    for iid, q in sorted(
        needs(data, db, plan, monday).items(), key=lambda kv: data.ingredients[kv[0]].name
    ):
        ing = data.ingredients[iid]
        if iid in at_home:
            have.append(ing)
            continue
        store = view or ing.store
        line = ShopLine(ing, q, data.locale)
        if store in cats:
            line.pick = pick(cats[store], iid, q, ing.unit)
        for a in alt_ids:
            if a != store and (alt := pick(cats[a], iid, q, ing.unit)):
                line.alternative = alt
                break
        groups.setdefault(store, {}).setdefault(ing.section, []).append(line)

    stores = []
    order = sorted(groups, key=lambda s: (s != primary, labels.get(s, s)))
    for sid in order:
        sl = StoreList(sid, labels.get(sid, sid), dict(sorted(groups[sid].items())))
        for line in sl.lines:
            if line.pick and line.pick.cost is not None:
                sl.total += line.pick.cost
            else:
                sl.unpriced += 1
        sl.total = round(sl.total, 2)
        stores.append(sl)
    views = [(sid, labels.get(sid, sid)) for sid in cats]
    return Shopping(view=view, stores=stores, views=views, labels=labels, have=have)


# ---------------------------------------------------------------- batch sheets


@dataclass
class Container:
    eat: dt.date
    servings: dict[str, float]
    storage: str
    defrost_on: dt.date | None


@dataclass
class BatchDish:
    recipe: Recipe
    total: float
    containers: list[Container]
    lines: list[str]


def batch_sheet(data: Data, db: DB, plan: Plan, session_date: dt.date) -> list[BatchDish]:
    out = []
    for s in plan.sessions:
        if s.date != session_date:
            continue
        for x in s.dishes:
            r = data.recipes.get(x.recipe)
            if r is None:  # repaired within the hour
                continue
            conts, total, eaters = [], 0.0, {}
            for e in x.eat:
                f = factors(data, db, e)
                if e.weekday() == 6 and data.household.guests.get("sunday_lunch"):
                    f["guests"] = data.household.guests["sunday_lunch"]
                gap = (e - x.cook).days
                storage = "fresh" if gap == 0 else "fridge" if gap <= r.fridge_days else "freezer"
                conts.append(
                    Container(
                        eat=e,
                        servings=f,
                        storage=storage,
                        defrost_on=e - dt.timedelta(days=1) if storage == "freezer" else None,
                    )
                )
                total += sum(f.values())
                for eater, n in f.items():
                    eaters[eater] = eaters.get(eater, 0) + n
            out.append(
                BatchDish(
                    recipe=r,
                    total=total,
                    containers=conts,
                    lines=_lines(data, amounts(data, r, eaters)),
                )
            )
    return out
