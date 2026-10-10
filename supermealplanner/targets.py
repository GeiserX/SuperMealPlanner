"""Energy targets that follow the weight trend, with guard rails.

The rules (research/README.md section 4, research/targets.md):
- Trend is an exponential moving average of weigh-ins, 10% per day (Hacker's Diet smoothing).
- Calories move by one step at most, no more often than every `every_weeks`, and only with
  at least 3 readings in that window. Protein never moves.
- Never below the person's floor. Losing faster than 0.75 kg/week raises calories; faster than
  1.4 kg/week raises a doctor flag (rapid loss and gallstones).
- Inside the goal band the mode becomes maintenance.
- At steady weight in maintain mode: a drift down over 0.5 kg in the window raises calories.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field

from .db import DB
from .model import Household, Person

ALPHA_PER_DAY = 0.1
FAST_LOSS = 0.75  # kg/week: too fast, add food
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
    rows = db.targets(p.id)
    return rows[-1]["kcal"] if rows else p.kcal.start


def _rate(tr: list[tuple[dt.date, float]], window_days: int, today: dt.date):
    """kg/week over the window from the trend line, and the number of readings in it."""
    since = today - dt.timedelta(days=window_days)
    pts = [(d, k) for d, k in tr if d >= since]
    if len(pts) < 2:
        return None, len(pts)
    (d0, k0), (d1, k1) = pts[0], pts[-1]
    days = (d1 - d0).days
    if days < 7:
        return None, len(pts)
    return (k1 - k0) / days * 7, len(pts)


def evaluate(db: DB, hh: Household, p: Person, today: dt.date, apply: bool = False) -> Status:
    kcal = current_kcal(db, p)
    tr = trend(db.weights(p.id))
    st = Status(person=p.id, kcal=kcal, protein_g=p.protein_g.target)
    if tr:
        st.trend_kg = tr[-1][1]
    window = p.kcal.every_weeks * 7
    rate, n = _rate(tr, window, today)
    st.rate_kg_week = None if rate is None else round(rate, 2)

    mode = p.goal.mode
    band = p.goal.band_kg
    if mode == "lose" and st.trend_kg is not None and band and band[0] <= st.trend_kg <= band[1]:
        mode = "maintain"
    st.mode = mode

    if rate is not None and -rate >= DOCTOR_LOSS:
        st.flags.append(("flag_fast_loss", {"rate": f"{-rate:.1f}"}))

    # binge-eating-safe pack: weekly binges over the window go to a professional, not to less food
    if "binge-eating-safe" in p.packs:
        since = today - dt.timedelta(days=28)
        binges = sum(1 for r in db.daily(p.id, since) if r.get("binge"))
        if binges >= 4:
            st.flags.append(("flag_binges", {"n": binges}))

    last = db.targets(p.id)
    last_day = last[-1]["day"] if last else None
    due = last_day is None or (today - last_day).days >= window
    if rate is None or n < 3 or not due:
        return st

    new, reason = kcal, None
    if mode == "lose":
        if -rate > FAST_LOSS:
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
