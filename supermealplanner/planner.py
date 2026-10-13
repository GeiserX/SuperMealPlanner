"""Deterministic 4-week planner.

Shape of a week (research/README.md section 7):
- Two batch sessions on free afternoons (calendar), cooking the lunches. Each batch dish covers up to
  two lunches. A lunch eaten more than `fridge_days` after cooking comes from the freezer.
- Dinners are quick or assembled the same day. Light dishes early in the week after a GLP-1
  injection, heartier ones from Friday.
- Breakfast and the afternoon snack rotate through fixed staples; the weekend treat goes on Saturday.
- A side salad is added whenever neither lunch nor dinner is a salad.
The weekly rules (legumes, fish, oily fish) are checked after a greedy pass; a failing draft is redrawn
with another seed. Same inputs, same plan.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import random
import threading
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

import yaml

from .model import WEEKDAYS, Data, Recipe

ATTEMPTS = 400
VARIETY_DAYS = 6  # the same recipe is not cooked again within this many days
PLAN_LOCK = threading.Lock()  # one writer at a time for the plan files and the paused days

log = logging.getLogger(__name__)


@dataclass
class Dish:
    recipe: str
    cook: dt.date
    eat: list[dt.date]


@dataclass
class Session:
    date: dt.date
    morning: bool = False  # cooked the same morning (first days of a plan)
    busy: list[str] = field(default_factory=list)  # calendar events, if no free afternoon was left
    dishes: list[Dish] = field(default_factory=list)


@dataclass
class Day:
    date: dt.date
    breakfast: str
    lunch: str
    lunch_cook: dt.date
    dinner: str
    snack: str
    side: str | None = None
    busy: list[str] = field(default_factory=list)

    def storage(self, data: Data) -> str:
        gap = (self.date - self.lunch_cook).days
        if gap == 0:
            return "fresh"
        r = data.recipes[self.lunch]
        return "fridge" if gap <= r.fridge_days else "freezer"


@dataclass
class Plan:
    start: dt.date
    weeks: int
    seed: int
    days: list[Day]
    sessions: list[Session]

    def day(self, d: dt.date) -> Day | None:
        for x in self.days:
            if x.date == d:
                return x
        return None

    @property
    def end(self) -> dt.date:
        return self.start + dt.timedelta(days=7 * self.weeks)

    # ---- yaml ----
    def to_dict(self) -> dict:
        def conv(o):
            if isinstance(o, dt.date):
                return o.isoformat()
            if isinstance(o, list):
                return [conv(x) for x in o]
            if isinstance(o, dict):
                return {k: conv(v) for k, v in o.items()}
            return o

        return conv(
            {
                "start": self.start,
                "weeks": self.weeks,
                "seed": self.seed,
                "sessions": [asdict(s) for s in self.sessions],
                "days": [asdict(d) for d in self.days],
            }
        )

    @classmethod
    def from_dict(cls, raw: dict) -> Plan:
        D = dt.date.fromisoformat
        sessions = [
            Session(
                date=D(s["date"]),
                morning=s.get("morning", False),
                busy=s.get("busy", []),
                dishes=[
                    Dish(recipe=x["recipe"], cook=D(x["cook"]), eat=[D(e) for e in x["eat"]])
                    for x in s["dishes"]
                ],
            )
            for s in raw["sessions"]
        ]
        days = [
            Day(
                date=D(d["date"]),
                breakfast=d["breakfast"],
                lunch=d["lunch"],
                lunch_cook=D(d["lunch_cook"]),
                dinner=d["dinner"],
                snack=d["snack"],
                side=d.get("side"),
                busy=d.get("busy", []),
            )
            for d in raw["days"]
        ]
        return cls(
            start=D(raw["start"]),
            weeks=raw["weeks"],
            seed=raw["seed"],
            days=days,
            sessions=sessions,
        )

    def save(self, path: Path) -> None:
        """Write atomically: a reader never sees half a plan."""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".yaml.tmp")
        tmp.write_text(
            yaml.safe_dump(self.to_dict(), allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
        os.replace(tmp, path)


def load_plan(path: Path) -> Plan:
    return Plan.from_dict(yaml.safe_load(path.read_text(encoding="utf-8")))


def plans_dir(data: Data) -> Path:
    return data.state / "plans"


def plan_for(data: Data, day: dt.date, raw: bool = False) -> Plan | None:
    """The newest plan that covers `day`, without the days the household paused. raw: the plan as
    it is on disk, for code that changes it."""
    best = None
    for p in sorted(plans_dir(data).glob("*.yaml")):
        plan = load_plan(p)
        if plan.start <= day < plan.end:
            best = plan
    return best if raw or best is None else without(best, paused_days(data))


# ---------------------------------------------------------------- paused days


def paused_days(data: Data) -> set[dt.date]:
    """The days the household said it eats off the plan (state/paused.yaml, one date per line).
    A pause ends by itself: the days after it are planned as before."""
    try:
        raw = yaml.safe_load((data.state / "paused.yaml").read_text(encoding="utf-8")) or []
    except (OSError, yaml.YAMLError):
        return set()
    out = set()
    for x in raw if isinstance(raw, list) else []:
        try:
            out.add(x if isinstance(x, dt.date) else dt.date.fromisoformat(str(x)))
        except ValueError:
            continue
    return out


def save_paused(data: Data, days: set[dt.date]) -> None:
    path = data.state / "paused.yaml"
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(yaml.safe_dump(sorted(d.isoformat() for d in days)), encoding="utf-8")
    os.replace(tmp, path)


def without(plan: Plan, hidden: set[dt.date]) -> Plan:
    """The plan as the household sees it while `hidden` days are paused: those days have no dishes,
    and nothing is cooked on them. A lunch for a later day that a paused day would have cooked is
    cooked on the first day back instead (that morning when it is eaten the same day). The plan on
    disk is untouched, so taking the pause away brings everything back."""
    if not hidden:
        return plan
    one = dt.timedelta(days=1)

    def moved(cook: dt.date, eat: dt.date) -> dt.date:
        while cook in hidden and cook < eat:
            cook += one
        return cook

    days = [
        replace(d, lunch_cook=moved(d.lunch_cook, d.date))
        for d in plan.days
        if d.date not in hidden
    ]
    sessions: dict[tuple[dt.date, bool], Session] = {}
    for s in plan.sessions:
        for x in s.dishes:
            eat = [e for e in x.eat if e not in hidden]
            if not eat:
                continue
            cook = moved(x.cook, eat[0])
            key = (s.date, s.morning) if cook == x.cook else (cook, cook == eat[0])
            if key not in sessions:
                busy = list(s.busy) if cook == x.cook else []
                sessions[key] = Session(date=key[0], morning=key[1], busy=busy)
            sessions[key].dishes.append(Dish(recipe=x.recipe, cook=cook, eat=eat))
    ordered = sorted(sessions.values(), key=lambda s: (s.date, not s.morning))
    return Plan(start=plan.start, weeks=plan.weeks, seed=plan.seed, days=days, sessions=ordered)


def nibbles(data: Data, day: Day, n: int = 3) -> list[Recipe]:
    """Up to `n` other snacks to pick at between meals that day: never a treat, never with an
    avoided ingredient, never the day's own snack. The same day always gets the same list."""
    ids = sorted(
        r.id
        for r in data.recipes.values()
        if "snack" in r.meals
        and not r.has("treat")
        and r.id != day.snack
        and _eligible(data, r, "snack", day.date.month)
    )
    if not ids:
        return []
    k = day.date.toordinal() % len(ids)
    return [data.recipes[i] for i in (ids[k:] + ids[:k])[:n]]


# ---------------------------------------------------------------- selection helpers


def _eligible(data: Data, r: Recipe, meal: str, month: int) -> bool:
    if meal not in r.meals or not r.in_season(month):
        return False
    avoided = data.household.avoided()
    if any(i.id in avoided for i in r.ingredients):
        return False
    if meal == "dinner":
        for p in data.household.people:
            if any(r.has(t) for t in p.avoid_tags_at_dinner):
                return False
            flags = {f for i in r.ingredients for f in data.ingredients[i.id].flags}
            if flags & set(p.avoid_flags_at_dinner):
                return False
    return True


def _avoided_in(data: Data, r: Recipe) -> list[str]:
    avoided = data.household.avoided()
    return sorted({i.id for i in r.ingredients if i.id in avoided})


def fixed_conflicts(data: Data) -> list[str]:
    """One line per fixed breakfast or snack (treats included) that uses an avoided ingredient.
    The planner leaves those out of the rotation instead of failing the plan."""
    out = []
    for r in sorted(data.recipes.values(), key=lambda r: r.id):
        for meal in ("breakfast", "snack"):
            if meal not in r.meals or not (
                r.kind == "fixed" or (meal == "snack" and r.has("treat"))
            ):
                continue
            bad = _avoided_in(data, r)
            if bad:
                out.append(
                    f"fixed {meal} {r.id} uses avoided {', '.join(bad)}: left out of the plan"
                )
    return out


def _glp1_phase(data: Data, d: dt.date) -> str:
    """early (1-3 days after an injection), late (4-6), injection day, or none."""
    for p in data.household.people:
        if p.glp1:
            since = (d.weekday() - WEEKDAYS.index(p.glp1.day)) % 7
            return "inj" if since == 0 else ("early" if since <= 3 else "late")
    return "none"


def _choose_sessions(data: Data, monday: dt.date, busy: dict[dt.date, list[str]]) -> list[Session]:
    pref = [WEEKDAYS.index(d) for d in data.household.batch.preferred_days]
    first = [w for w in pref if w <= 2] + [0, 1, 2]
    second = [w for w in pref if w >= 3] + [4, 5, 6, 3]
    out = []
    for cands in (first, second)[: data.household.batch.sessions_per_week]:
        seen, pick = [], None
        for w in cands:
            if w in seen:
                continue
            seen.append(w)
            d = monday + dt.timedelta(days=w)
            if d not in busy:
                pick = Session(date=d)
                break
        if pick is None:
            d = monday + dt.timedelta(days=seen[0])
            pick = Session(date=d, busy=busy.get(d, []))
        out.append(pick)
    return out


@dataclass
class _Slot:
    meal: str  # lunch (a batch dish) or dinner
    dates: list[dt.date]  # lunches covered, or the dinner date
    cook: dt.date
    session: Session | None = None
    recipe: str | None = None


def _week_of(d: dt.date, start: dt.date) -> int:
    return (d - start).days // 7


def generate(
    data: Data,
    start: dt.date,
    weeks: int = 4,
    busy: dict[dt.date, list[str]] | None = None,
    previous: Plan | None = None,
    seed: int | None = None,
) -> Plan:
    if start.weekday() != 0:
        raise ValueError(f"{start} is not a Monday")
    busy = busy or {}
    base_seed = seed if seed is not None else int(start.strftime("%Y%m%d"))
    for warning in fixed_conflicts(data):
        log.warning(warning)
    last_err: list[str] = []
    for attempt in range(ATTEMPTS):
        plan = _draft(data, start, weeks, busy, previous, base_seed + attempt)
        errs = validate(data, plan)
        if not errs:
            return plan
        last_err = errs
    raise PlanError("no plan satisfies the rules:\n" + "\n".join(last_err))


class PlanError(Exception):
    pass


def _draft(data, start, weeks, busy, previous, seed) -> Plan:
    rng = random.Random(seed)
    hh = data.household
    rules = hh.rules
    end = start + dt.timedelta(days=7 * weeks)
    dates = [start + dt.timedelta(days=i) for i in range(7 * weeks)]

    # sessions and the lunches each covers
    sessions: list[Session] = []
    for w in range(weeks):
        sessions += _choose_sessions(data, start + dt.timedelta(days=7 * w), busy)
    first_morning = Session(date=start, morning=True)
    sessions = [first_morning] + sessions
    slots: list[_Slot] = []
    for i, s in enumerate(sessions):
        nxt = sessions[i + 1].date if i + 1 < len(sessions) else end - dt.timedelta(days=1)
        if s.morning:
            covered = [d for d in dates if s.date <= d <= nxt]
        else:
            covered = [d for d in dates if s.date < d <= nxt]
        if i + 1 == len(sessions):
            covered = [d for d in dates if d > s.date]
        if s.morning and sessions[1].date == start:
            covered = [start]
        for j in range(0, len(covered), 2):
            slots.append(_Slot("lunch", covered[j : j + 2], s.date, s))
    for d in dates:
        slots.append(_Slot("dinner", [d], d))
    slots.sort(key=lambda s: (s.dates[0], 0 if s.meal == "lunch" else 1))

    # recent use, including the end of the previous plan
    last_cooked: dict[str, dt.date] = {}
    count: dict[str, int] = {}
    if previous:
        for d in previous.days:
            last_cooked[d.lunch] = max(last_cooked.get(d.lunch, d.date), d.lunch_cook)
            last_cooked[d.dinner] = d.date

    def weekly_need(week: int, tag: str, minimum: int) -> float:
        have = 0
        for s in slots:
            if s.recipe and _week_of(s.dates[0], start) == week:
                r = data.recipes[s.recipe]
                if r.has(tag):
                    have += sum(1 for x in s.dates if _week_of(x, start) == week)
        remaining = sum(
            1 for s in slots if s.recipe is None and _week_of(s.dates[0], start) == week
        )
        missing = minimum - have
        if missing <= 0:
            return 0.0
        return 1.0 + missing / max(1, remaining)

    for slot in slots:
        d0 = slot.dates[0]
        month = d0.month
        week = _week_of(d0, start)
        need_kind = {"batch"} if slot.meal == "lunch" else {"quick", "assemble"}
        if slot.meal == "lunch" and slot.session and slot.session.morning:
            need_kind = {"batch", "quick"}
        gap_max = max((x - slot.cook).days for x in slot.dates)
        cands = []
        for r in data.recipes.values():
            if r.kind not in need_kind or not _eligible(data, r, slot.meal, month):
                continue
            if r.has("side"):  # a side salad is never the main dish
                continue
            if slot.meal == "lunch" and gap_max > r.fridge_days and not r.freezes:
                continue
            if count.get(r.id, 0) >= rules.max_repeats_per_4_weeks:
                continue
            lc = last_cooked.get(r.id)
            if lc and (slot.cook - lc).days < VARIETY_DAYS:
                continue
            score = rng.random()
            score += 3 * weekly_need(week, "legume", rules.legumes_min) * r.has("legume")
            score += 3 * weekly_need(week, "fish", rules.fish_min) * r.has("fish")
            score += 3 * weekly_need(week, "oily-fish", rules.oily_fish_min) * r.has("oily-fish")
            phase = _glp1_phase(data, d0)
            if phase == "early":
                score += 0.8 * r.has("light") - 0.8 * r.has("hearty")
            elif phase == "late":
                score += 0.4 * r.has("hearty")
            if any(x.weekday() == 6 for x in slot.dates):
                score += 0.6 * r.has("guests")
            cands.append((score, r.id))
        if not cands:
            # relax variety for this slot rather than fail the whole plan
            cands = [
                (rng.random(), r.id)
                for r in data.recipes.values()
                if r.kind in need_kind
                and not r.has("side")
                and _eligible(data, r, slot.meal, month)
            ]
        if not cands:
            raise PlanError(
                f"no {slot.meal} recipe fits {d0} (kinds {sorted(need_kind)}, month {month}): "
                "add recipes for this meal or season"
            )
        cands.sort(reverse=True)
        pick = cands[0][1]
        slot.recipe = pick
        count[pick] = count.get(pick, 0) + 1
        last_cooked[pick] = slot.cook

    # fixed meals
    def rotation(meal: str, exclude_tag: str | None = None) -> list[str]:
        ids = [
            r.id
            for r in data.recipes.values()
            if meal in r.meals
            and r.kind == "fixed"
            and not (exclude_tag and r.has(exclude_tag))
            and not _avoided_in(data, r)
        ]
        rng.shuffle(ids)
        return ids or [""]

    breakfasts = rotation("breakfast")
    snacks = rotation("snack", "treat")
    treats = [
        r.id
        for r in data.recipes.values()
        if "snack" in r.meals and r.has("treat") and not _avoided_in(data, r)
    ]
    sides = [
        r.id
        for r in data.recipes.values()
        if r.has("side") and r.has("salad") and _eligible(data, r, "lunch", start.month)
    ]
    rng.shuffle(sides)

    days: list[Day] = []
    lunch_of: dict[dt.date, _Slot] = {}
    for s in slots:
        if s.meal == "lunch":
            for d in s.dates:
                lunch_of[d] = s
    dinner_of = {s.dates[0]: s for s in slots if s.meal == "dinner"}
    for i, d in enumerate(dates):
        ls, ds = lunch_of[d], dinner_of[d]
        snack = (
            treats[i // 7 % len(treats)]
            if (treats and d.weekday() == 5)
            else snacks[i % len(snacks)]
        )
        day = Day(
            date=d,
            breakfast=breakfasts[i % len(breakfasts)],
            lunch=ls.recipe,
            lunch_cook=ls.cook,
            dinner=ds.recipe,
            snack=snack,
            busy=busy.get(d, []),
        )
        has_salad = data.recipes[day.lunch].has("salad") or data.recipes[day.dinner].has("salad")
        if rules.salad_daily and sides and not has_salad:
            day.side = sides[i % len(sides)]
        days.append(day)

    for s in sessions:
        s.dishes = [
            Dish(recipe=x.recipe, cook=x.cook, eat=x.dates)
            for x in slots
            if x.meal == "lunch" and x.session is s
        ]
    sessions = [s for s in sessions if s.dishes]
    return Plan(start=start, weeks=weeks, seed=seed, days=days, sessions=sessions)


# ---------------------------------------------------------------- repair


def unknown_recipes(data: Data, plan: Plan) -> set[str]:
    ids = {
        getattr(d, m) for d in plan.days for m in ("breakfast", "lunch", "dinner", "snack", "side")
    }
    ids |= {x.recipe for s in plan.sessions for x in s.dishes}
    return {i for i in ids if i and i not in data.recipes}


def _breaks_household(data: Data, rid: str, meal: str) -> bool:
    """True when the recipe is gone, uses an avoided ingredient, or is avoided at dinner."""
    r = data.recipes.get(rid)
    if r is None:
        return True
    if any(i.id in data.household.avoided() for i in r.ingredients):
        return True
    if meal == "dinner":
        flags = {f for i in r.ingredients for f in data.ingredients[i.id].flags}
        for p in data.household.people:
            if any(r.has(t) for t in p.avoid_tags_at_dinner) or flags & set(
                p.avoid_flags_at_dinner
            ):
                return True
    return False


def repair(data: Data, plan: Plan, today: dt.date) -> int:
    """Fix a plan in place after the data changed, touching only the slots that must change.

    A plan is never redone, because lunches may already be cooked. A slot gets a substitute when its
    recipe was deleted or renamed (any date, so old weeks still display), or, from today on, when it
    now breaks the household's avoided ingredients or dinner rules. A lunch cooked before today stays
    as it is (it is in the fridge); the caller is told about it. Returns (slots changed, cooked
    lunches that now break a rule).
    """
    uses: dict[str, int] = {}
    last: dict[str, dt.date] = {}
    for d in plan.days:
        for rid, when in ((d.dinner, d.date), (d.lunch, d.lunch_cook)):
            uses[rid] = uses.get(rid, 0) + 1
            last[rid] = max(last.get(rid, when), when)

    def pick(meal, kinds, when, old=None, gap=0, tag=None):
        want = None
        if old in data.recipes:  # keep the protein of the dish it replaces
            want = data.recipes[old].protein
        cands = []
        for r in data.recipes.values():
            if r.kind not in kinds or not _eligible(data, r, meal, when.month):
                continue
            wants_side = tag == "side"
            if r.has("side") != wants_side:  # side salads replace side salads only
                continue
            if meal == "lunch" and gap > r.fridge_days and not r.freezes:
                continue
            if meal in ("lunch", "dinner") and _breaks_household(data, r.id, meal):
                continue
            recent = r.id in last and abs((when - last[r.id]).days) < VARIETY_DAYS
            cands.append((r.protein != want, recent, uses.get(r.id, 0), r.id))
        if not cands:
            return None
        best = min(cands)[3]
        uses[best] = uses.get(best, 0) + 1
        last[best] = max(last.get(best, when), when)
        return best

    changed, cooked_bad = 0, 0
    # lunches: one container group = (recipe, cook date), shared by its days and its session dish
    groups: dict[tuple[str, dt.date], list[Day]] = {}
    for d in plan.days:
        groups.setdefault((d.lunch, d.lunch_cook), []).append(d)
    for (rid, cook), days in groups.items():
        gone = rid not in data.recipes
        bad = gone or (cook >= today and _breaks_household(data, rid, "lunch"))
        if not bad:
            if cook < today and _breaks_household(data, rid, "lunch") and days[-1].date >= today:
                cooked_bad += 1
            continue
        gap = max((d.date - cook).days for d in days)
        new = pick("lunch", {"batch", "quick"}, cook, rid, gap)
        if not new:
            continue
        for d in days:
            d.lunch = new
        for s in plan.sessions:
            for x in s.dishes:
                if x.recipe == rid and x.cook == cook:
                    x.recipe = new
        changed += 1
    for d in plan.days:
        for meal, kinds, tag in (
            ("dinner", {"quick", "assemble"}, None),
            ("breakfast", {"fixed"}, None),
            ("snack", {"fixed"}, None),
            ("side", {"assemble"}, "side"),
        ):
            rid = getattr(d, meal)
            if not rid:
                continue
            gone = rid not in data.recipes
            if gone or (d.date >= today and _breaks_household(data, rid, meal)):
                new = pick("lunch" if meal == "side" else meal, kinds, d.date, rid, tag=tag)
                if new:
                    setattr(d, meal, new)
                    changed += 1
    return changed, cooked_bad


# ---------------------------------------------------------------- validation


def validate(data: Data, plan: Plan, repeats: bool = True) -> list[str]:
    """Every hard rule a plan must meet. Empty list = valid. repeats: also the limit on how often
    a dish is cooked, which is for a draw; a dish someone asked for by name may go over it."""
    errs: list[str] = []
    hh = data.household
    rules = hh.rules
    avoided = hh.avoided()
    for d in plan.days:
        for meal in ("breakfast", "lunch", "dinner", "snack", "side"):
            rid = getattr(d, meal)
            if not rid:
                continue
            r = data.recipes.get(rid)
            if r is None:
                errs.append(f"{d.date} {meal}: unknown recipe {rid}")
                continue
            if any(i.id in avoided for i in r.ingredients):
                errs.append(f"{d.date} {meal}: {rid} uses an avoided ingredient")
        if d.lunch not in data.recipes or d.dinner not in data.recipes:
            continue  # reported as unknown above
        dinner = data.recipes[d.dinner]
        for meal in ("lunch", "dinner"):
            if data.recipes[getattr(d, meal)].has("side"):
                errs.append(f"{d.date} {meal}: a side salad is not a main dish")
        for p in hh.people:
            bad = [t for t in p.avoid_tags_at_dinner if dinner.has(t)]
            if bad:
                errs.append(f"{d.date} dinner {d.dinner}: {p.name} avoids {bad} at dinner")
        lunch = data.recipes[d.lunch]
        gap = (d.date - d.lunch_cook).days
        if gap < 0:
            errs.append(f"{d.date} lunch cooked after it is eaten")
        elif gap > lunch.fridge_days and not lunch.freezes:
            errs.append(f"{d.date} lunch {d.lunch}: {gap} days old and does not freeze")
        if rules.salad_daily and not (lunch.has("salad") or dinner.has("salad") or d.side):
            errs.append(f"{d.date}: no salad")

    for w in range(plan.weeks):
        week = [d for d in plan.days if _week_of(d.date, plan.start) == w]
        if len(week) < 7:
            continue

        if any(d.lunch not in data.recipes or d.dinner not in data.recipes for d in week):
            continue

        def n(tag: str, week: list[Day] = week) -> int:
            return sum(
                data.recipes[d.lunch].has(tag) + data.recipes[d.dinner].has(tag) for d in week
            )

        for tag, minimum, label in (
            ("legume", rules.legumes_min, "legumes"),
            ("fish", rules.fish_min, "fish"),
            ("oily-fish", rules.oily_fish_min, "oily fish"),
        ):
            if n(tag) < minimum:
                errs.append(f"week {w + 1}: {label} {n(tag)} < {minimum}")
    if not repeats:
        return errs
    fresh: dict[str, int] = {}
    for s in plan.sessions:
        for x in s.dishes:
            fresh[x.recipe] = fresh.get(x.recipe, 0) + 1
    for d in plan.days:
        fresh[d.dinner] = fresh.get(d.dinner, 0) + 1
    limit = rules.max_repeats_per_4_weeks * max(1, plan.weeks // 4)
    for rid, k in fresh.items():
        if k > limit:
            errs.append(f"{rid} cooked {k} times (limit {limit})")
    return errs
