"""Energy targets that follow the weight trend, with guard rails.

The rules (docs/how-it-works.md, "Targets"):
- Trend is an exponential moving average of weigh-ins, 10% per day (Hacker's Diet smoothing).
- Calories move by one step at most, no more often than every `every_weeks`, and only once the
  readings cover most of that window (at least 3 of them). Protein never moves.
- Never below the person's floor. Losing faster than goal.max_loss_kg_week adds food; faster than
  1.4 kg/week raises a doctor flag (rapid loss and gallstones).
- Inside or below the goal band the mode is maintenance: nobody keeps losing past their goal.
- With the binge-eating-safe pack, a window with any binge never cuts food.
- Editing kcal.start in the household file resets the target to it.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field

from .db import DB
from .model import Household, Person

ALPHA_PER_DAY = 0.1
DOCTOR_LOSS = 1.4  # kg/week: tell a doctor
SLOW_LOSS = 0.2  # kg/week: slower than this while losing, cut one step
MAINTAIN_DRIFT = 0.5  # kg over the window


def mifflin_st_jeor(p: Person, kg: float, on: dt.date) -> float:
    base = 10 * kg + 6.25 * p.height_cm - 5 * p.age(on)
    return base + (5 if p.sex == "male" else -161)


def trend(weights: list[tuple[dt.date, float]]) -> list[tuple[dt.date, float]]:
    """Smoothed weight per reading. Gaps between readings decay the old trend as days pass."""
    out: list[tuple[dt.date, float]] = []
    t: float | None = None
    last: dt.date | None = None
    for day, kg in weights:
        if t is None:
            t = kg
        else:
            days = max(1, (day - last).days)
            k = 1 - (1 - ALPHA_PER_DAY) ** days
            t = t + k * (kg - t)
        out.append((day, round(t, 2)))
        last = day
    return out


@dataclass
class Status:
    person: str
    kcal: float
    protein_g: float
    trend_kg: float | None = None
    rate_kg_week: float | None = None
    mode: str = "maintain"
    flags: list[tuple[str, dict]] = field(default_factory=list)  # (translation key, params)
    changed: tuple[str, dict] | None = None


def current_kcal(db: DB, p: Person) -> float:
    """The latest adjustment made since kcal.start was last set, else kcal.start; never below the floor.

    Editing kcal.start in the household file resets the target: adjustments made before the edit
    no longer count (sync_start records when the edit was seen).
    """
    base = db.baseline(p.id)
    kcal = p.kcal.start
    if base and base[0] == p.kcal.start:
        rows = [r for r in db.targets(p.id) if r["day"] > base[1]]
        if rows:
            kcal = rows[-1]["kcal"]
    elif base is None:
        rows = db.targets(p.id)
        if rows:
            kcal = rows[-1]["kcal"]
    return max(kcal, p.kcal.floor)


def sync_start(db: DB, p: Person, today: dt.date) -> None:
    """Remember the kcal.start in force, so a later edit of it is noticed and resets the target."""
    base = db.baseline(p.id)
    if base is None or base[0] != p.kcal.start:
        db.set_baseline(p.id, p.kcal.start, today if base else dt.date.min)


def _rate(tr: list[tuple[dt.date, float]], window_days: int, today: dt.date):
    """kg/week over the window from the trend line, the number of readings in it, and whether those
    readings cover enough of the window to act on (three quarters of it; a young trend lags)."""
    since = today - dt.timedelta(days=window_days)
    pts = [(d, k) for d, k in tr if d >= since]
    if len(pts) < 2:
        return None, len(pts), False
    (d0, k0), (d1, k1) = pts[0], pts[-1]
    days = (d1 - d0).days
    if days < 7:
        return None, len(pts), False
    started_early = tr[0][0] <= today - dt.timedelta(days=window_days)
    covered = days >= window_days * 0.75 and started_early
    return (k1 - k0) / days * 7, len(pts), covered


def evaluate(db: DB, hh: Household, p: Person, today: dt.date, apply: bool = False) -> Status:
    kcal = current_kcal(db, p)
    tr = trend(db.weights(p.id))
    st = Status(person=p.id, kcal=kcal, protein_g=p.protein_g.target)
    if tr:
        st.trend_kg = tr[-1][1]
    window = p.kcal.every_weeks * 7
    rate, n, covered = _rate(tr, window, today)
    st.rate_kg_week = None if rate is None else round(rate, 2)

    mode = p.goal.mode
    band = p.goal.band_kg
    if mode == "lose" and st.trend_kg is not None:
        top = band[1] if band else p.goal.target_kg
        if top is not None and st.trend_kg <= top:  # at or below the goal: stop losing
            mode = "maintain"
    st.mode = mode

    if rate is not None and -rate >= DOCTOR_LOSS:
        st.flags.append(("flag_fast_loss", {"rate": f"{-rate:.1f}"}))

    # binge-eating-safe pack: binges go to a professional, never to less food
    no_cuts = False
    if "binge-eating-safe" in p.packs:
        recent = sum(1 for r in db.daily(p.id, today - dt.timedelta(days=28)) if r.get("binge"))
        if recent >= 4:
            st.flags.append(("flag_binges", {"n": recent}))
        in_window = sum(
            1 for r in db.daily(p.id, today - dt.timedelta(days=window)) if r.get("binge")
        )
        no_cuts = in_window > 0

    base = db.baseline(p.id)
    last = [r for r in db.targets(p.id) if not base or r["day"] > base[1]]
    last_day = last[-1]["day"] if last else None
    if base and base[1] != dt.date.min:  # an edited start counts as the latest change
        last_day = max(last_day or base[1], base[1])
    due = last_day is None or (today - last_day).days >= window
    if rate is None or n < 3 or not covered or not due:
        return st

    new, reason = kcal, None
    if mode == "lose":
        if -rate > p.goal.max_loss_kg_week:
            new, reason = kcal + p.kcal.step, ("reason_fast_loss", {"rate": f"{-rate:.2f}"})
        elif -rate < SLOW_LOSS:
            new, reason = kcal - p.kcal.step, ("reason_slow_loss", {"rate": f"{-rate:.2f}"})
    elif mode == "maintain":
        drift = rate * window / 7
        if drift < -MAINTAIN_DRIFT:
            new, reason = kcal + p.kcal.step, ("reason_maintain_down", {"drift": f"{drift:+.1f}"})
        elif p.goal.mode == "lose" and drift > MAINTAIN_DRIFT:
            new, reason = kcal - p.kcal.step, ("reason_maintain_up", {"drift": f"{drift:+.1f}"})
    elif mode == "gain" and rate < 0.1:
        new, reason = kcal + p.kcal.step, ("reason_no_gain", {})

    if no_cuts and new < kcal:
        new, reason = kcal, None
    new = max(new, p.kcal.floor)
    if reason and new != kcal:
        st.kcal = new
        st.changed = reason
        if apply:
            db.set_target(p.id, today, new, json.dumps({"key": reason[0], **reason[1]}))
    return st


def day_factor(hh: Household, p: Person, kcal: float, day: dt.date) -> float:
    """How much of a reference serving this person eats on this day."""
    f = kcal / hh.reference_kcal
    if p.glp1:
        from .model import WEEKDAYS

        inj = WEEKDAYS.index(p.glp1.day)
        since = (day.weekday() - inj) % 7  # 0 = injection day
        if 1 <= since <= 3:
            f *= p.glp1.early_week_factor
        elif since >= 4:
            f *= p.glp1.late_week_factor
    return round(f, 3)


def days_since_injection(p: Person, day: dt.date) -> int | None:
    if not p.glp1:
        return None
    from .model import WEEKDAYS

    return (day.weekday() - WEEKDAYS.index(p.glp1.day)) % 7


def reason_text(t, raw: str) -> str:
    """A stored target-change reason, in the reader's language."""
    try:
        d = json.loads(raw)
        key = d.pop("key")
        return t(key, **d)
    except (ValueError, KeyError, TypeError):
        return raw
