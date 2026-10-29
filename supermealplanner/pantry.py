"""The virtual pantry: what is at home, worked out so that nobody has to keep it up to date.

Only what came in is stored (db.py, table pantry):
- a tick on a week's shopping list adds what that line buys: the packs the store's catalogue
  sells, or the line's amount without a catalogue. Unticking takes the entry away again;
- the chat saying how much there is ("there is 1 kg of rice", "the oil ran out") replaces what was
  there with that amount. Said before the household's lunch time it is what that day's cooking
  starts from; from lunch time on, what it left.

Everything else follows from the plan and the data, the same way every time:
- each day the plan cooks takes its dishes' ingredients out (planner.Plans and
  portions.servings_on: the lunches cooked that day and the other meals eaten that day, at each
  person's servings), the oldest entry first. A dish is taken out at the end of its cook day;
- an ingredient with `keeps_days` (ingredients.yaml) goes off that many days after it came in. One
  without keeps until it is used up.

«I have it» (db.have) stays what it was: at home in an amount nobody counted, never used up, off
every list until someone puts it back.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from .db import DB
from .model import WEEKDAYS, Data, Ingredient, Recipe
from .planner import Plans, plan_for
from .portions import fmt_qty, ingredients_of, needs, servings_on
from .targets import current_kcal

EPS = 1e-6


@dataclass
class Lot:
    ingredient: str
    qty: float
    day: dt.date  # the household's date it came in
    expires: dt.date | None  # the first day it is no longer good
    source: str  # tick | set
    week: dt.date | None  # the list's week, for a tick
    late: bool = False  # said from lunch time on: what is left after that day's cooking


@dataclass
class Item:
    """One ingredient in the pantry now: every entry of it together."""

    ingredient: str
    name: str
    qty: float
    amount: str
    since: dt.date
    expires: dt.date | None


def _lots(data: Data, db: DB, skip_from: dt.date | None = None) -> list[Lot]:
    """The stored entries as lots. skip_from: leave out the ticks of that week's list and later."""
    zone = ZoneInfo(data.household.timezone)
    out = []
    for r in db.lots():
        ing = data.ingredients.get(r["ingredient"])
        if ing is None:
            continue
        week = dt.date.fromisoformat(r["week"]) if r["week"] else None
        if skip_from and r["source"] == "tick" and week and week >= skip_from:
            continue
        when = dt.datetime.fromisoformat(r["at"]).astimezone(zone)
        day, keeps = when.date(), ing.keeps_days
        expires = day + dt.timedelta(days=keeps) if keeps else None
        late = r["source"] == "set" and f"{when:%H:%M}" >= data.household.lunch_time
        out.append(Lot(r["ingredient"], float(r["qty"]), day, expires, r["source"], week, late))
    return out


class Walk:
    """The pantry day by day, from its oldest entry. uses(d) is what the plan cooks on day d."""

    def __init__(self, data: Data, db: DB, plans: Plans | None = None):
        self.data, self.db = data, db
        self.plans = plans or Plans(data)
        self.kcal = {p.id: current_kcal(db, p) for p in data.household.people}
        self._uses: dict[dt.date, dict[str, float]] = {}

    def uses(self, d: dt.date) -> dict[str, float]:
        if d not in self._uses:
            plan = self.plans.plan(d)
            self._uses[d] = (
                ingredients_of(self.data, servings_on(self.data, self.db, plan, {d}, self.kcal))
                if plan
                else {}
            )
        return self._uses[d]

    def run(
        self, until: dt.date, skip_from: dt.date | None = None
    ) -> tuple[dict[str, list[Lot]], list[tuple[Lot, dt.date]]]:
        """The lots left on day `until` (everything that came in up to that day, the cooking of the
        days before it taken out) and the lots that went off on the way with food left, with the
        day they went off."""
        lots = _lots(self.data, self.db, skip_from)
        stock: dict[str, list[Lot]] = {}
        gone: list[tuple[Lot, dt.date]] = []
        if not lots:
            return stock, gone
        i, d = 0, min(x.day for x in lots)
        while d <= until:
            for iid, kept in stock.items():
                for lot in kept:
                    if lot.expires and lot.expires <= d and lot.qty > EPS:
                        gone.append((lot, d))
                stock[iid] = [x for x in kept if not (x.expires and x.expires <= d)]
            today = []
            while i < len(lots) and lots[i].day <= d:
                today.append(lots[i])
                i += 1
            # a tick comes in before the day's cooking; what the chat said is there from lunch time
            # on is what that cooking left
            for lot in [x for x in today if not x.late or d == until]:
                self._put(stock, lot)
            if d < until:
                for iid, q in self.uses(d).items():
                    for lot in stock.get(iid, []):
                        take = min(lot.qty, q)
                        lot.qty -= take
                        q -= take
                        if q <= EPS:
                            break
                for lot in [x for x in today if x.late]:
                    self._put(stock, lot)
            stock = {k: [x for x in v if x.qty > EPS] for k, v in stock.items()}
            d += dt.timedelta(days=1)
        return {k: v for k, v in stock.items() if v}, gone

    @staticmethod
    def _put(stock: dict[str, list[Lot]], lot: Lot) -> None:
        lot = Lot(**vars(lot))  # a walk never changes the lots another walk reads
        if lot.source == "set":  # what the chat says is there replaces what was
            stock[lot.ingredient] = []
        stock.setdefault(lot.ingredient, []).append(lot)


def now(data: Data, db: DB, today: dt.date, walk: Walk | None = None) -> list[Item]:
    """What is in the pantry today, by ingredient name."""
    stock, _ = (walk or Walk(data, db)).run(today)
    out = []
    for iid, lots in stock.items():
        ing = data.ingredients[iid]
        qty = sum(x.qty for x in lots)
        ends = [x.expires for x in lots if x.expires]
        out.append(
            Item(
                iid,
                ing.name,
                qty,
                fmt_qty(ing, qty, data.locale),
                min(x.day for x in lots),
                min(ends) if ends else None,
            )
        )
    return sorted(out, key=lambda x: x.name)


def covered(
    data: Data, db: DB, monday: dt.date, today: dt.date, walk: Walk | None = None
) -> set[str]:
    """The ingredients a week's list need not ask for: the pantry, without what that week's list
    (or a later one) bought, holds enough of each for the cooking of that week still to come, in
    lots that are still good on the last day it is used."""
    start, end = max(monday, today), monday + dt.timedelta(days=7)
    if start >= end:
        return set()
    walk = walk or Walk(data, db)
    stock, _ = walk.run(start, skip_from=monday)
    need: dict[str, float] = {}
    last: dict[str, dt.date] = {}
    d = start
    while d < end:
        for iid, q in walk.uses(d).items():
            need[iid] = need.get(iid, 0) + q
            last[iid] = d
        d += dt.timedelta(days=1)
    out = set()
    for iid, q in need.items():
        good = sum(x.qty for x in stock.get(iid, []) if not x.expires or x.expires > last[iid])
        if q > EPS and good + EPS >= q:
            out.add(iid)
    return out


def unbought(data: Data, db: DB, plan, day: dt.date) -> list[tuple[Ingredient, Recipe]]:
    """What a dish of `day` needs that its week's list still asks for, unticked, once that week's
    shopping day has passed, and that the pantry does not hold enough of for that day's cooking:
    the shop came and went without it. With the recipe it is for, the first one that day that uses
    it."""
    from .portions import amounts, shopping_list

    monday = day - dt.timedelta(days=day.weekday())
    shop_day = monday + dt.timedelta(days=WEEKDAYS.index(data.household.shopping_day))
    if day <= shop_day:
        return []
    shop = shopping_list(data, db, plan, monday, today=day)
    asked = {x.ingredient.id for s in shop.stores for x in s.lines} - db.ticked(monday)
    walk = Walk(data, db)
    stock, _ = walk.run(day)
    need = walk.uses(day)
    asked = {
        iid for iid in asked if sum(x.qty for x in stock.get(iid, [])) + EPS < need.get(iid, 0)
    }
    out: dict[str, tuple[Ingredient, Recipe]] = {}
    for rid, row in servings_on(data, db, plan, {day}).items():
        rec = data.recipes.get(rid)
        if rec is None:
            continue
        for iid in amounts(data, rec, row):
            if iid in asked and iid not in out:
                out[iid] = (data.ingredients[iid], rec)
    return sorted(out.values(), key=lambda x: x[0].name)


def going_off(
    data: Data, db: DB, today: dt.date, end: dt.date, walk: Walk | None = None
) -> list[tuple[Lot, dt.date]]:
    """Lots that go off after today and up to `end` with food left, the plan cooking as planned:
    no planned dish uses them up in time."""
    _, gone = (walk or Walk(data, db)).run(end + dt.timedelta(days=1))
    return [(lot, d) for lot, d in gone if today < d <= end + dt.timedelta(days=1)]


def bought(data: Data, db: DB, monday: dt.date, iid: str) -> float | None:
    """What a tick on a week's list line brings in: the packs (or kilos) the store's catalogue
    sells for it, else the line's amount as the list rounds it. None when the week's list does not
    have that ingredient."""
    from .stores import catalogs, pick

    plan = plan_for(data, monday)
    need = needs(data, db, plan, monday).get(iid) if plan else None
    if not need:
        return None
    ing = data.ingredients[iid]
    cats = catalogs(data)
    p = pick(cats[ing.store], iid, need, ing.unit) if ing.store in cats else None
    if p is not None and p.product.pack.unit == ing.unit:
        return p.packs * 1000 if p.product.loose else p.packs * p.product.pack.qty
    return float(math.ceil(need)) if ing.unit == "ud" else float(math.ceil(need / 10) * 10)


def tick(
    data: Data, db: DB, monday: dt.date, iid: str, on: bool, at: dt.datetime | None = None
) -> None:
    """A tick on a week's list, from the web page or a phone: the list keeps it, and the pantry
    gets what it bought. Unticking takes it out of the pantry again. A tick that changes nothing (a
    phone retrying) leaves both alone, so the purchase keeps its day and what cooking took from it."""
    if on == (iid in db.ticked(monday)):
        return
    at = at or dt.datetime.now(dt.UTC)
    db.tick(monday, iid, on, at)
    qty = bought(data, db, monday, iid) if on else None
    if qty:
        db.add_lot(iid, qty, at, "tick", monday)
    else:
        db.drop_tick_lot(monday, iid)


def correct(data: Data, db: DB, amounts: dict[str, float], at: dt.datetime | None = None) -> None:
    """What the chat says is at home now, by ingredient, in its unit. 0: it ran out, which also
    takes it off «I have it», so the list asks for it again."""
    at = at or dt.datetime.now(dt.UTC)
    for iid, qty in amounts.items():
        db.add_lot(iid, float(qty), at, "set")
        if qty == 0:
            db.set_have(iid, False)
