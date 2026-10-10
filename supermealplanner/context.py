"""What the AI is told before it answers: the household, today's and tomorrow's plates, the week, recent logs."""

from __future__ import annotations

import datetime as dt

from .db import DB
from .i18n import Translator
from .model import Data
from .planner import plan_for
from .portions import person_days
from .targets import current_kcal

RULES = """You are the household's meal-planning assistant inside a family chat.
- Answer in the household's language ({locale}), briefly and concretely, like a helpful cook.
- Use the plan and data below; say so when something is not in them.
- Never change, start or stop a medication or supplement dose. Medical decisions belong to their
  clinicians; you can suggest what to ask.
- Never suggest compensating for a binge by eating less afterwards.
- When someone wants to change the plan, targets, recipes or settings, say exactly what would change
  (which file and value in the data directory) so a person can apply it.
"""


def build(data: Data, db: DB, today: dt.date) -> str:
    t = Translator(data.locale)
    hh = data.household
    out = [
        RULES.format(locale=data.locale),
        data.settings.llm.instructions or "",
        f"Today is {t.date(today)} ({today.isoformat()}).",
    ]

    out.append("\n## People")
    for p in hh.people:
        g = p.goal
        line = (
            f"- {p.name} (id {p.id}): {p.sex}, born {p.birth_year}, {p.height_cm:g} cm. "
            f"Goal {g.mode}"
            + (f" to {g.target_kg} kg" if g.target_kg else "")
            + (f", band {g.band_kg[0]}-{g.band_kg[1]} kg" if g.band_kg else "")
            + f". Energy {current_kcal(db, p):.0f} kcal (floor {p.kcal.floor:.0f}), protein {p.protein_g.target:.0f} g."
        )
        if p.glp1:
            line += f" On {p.glp1.drug}, injects on {p.glp1.day}."
        if p.avoid_ingredients:
            line += f" Avoids: {', '.join(p.avoid_ingredients)}."
        out.append(line)
        for slot, pills in p.pills.items():
            if pills:
                out.append(
                    f"  - pills {slot}: " + "; ".join(f"{x.name} {x.dose}".strip() for x in pills)
                )
        for note in p.notes:
            out.append(f"  - note: {note}")
        w = db.weights(p.id)[-4:]
        if w:
            out.append("  - last weigh-ins: " + ", ".join(f"{d.isoformat()} {k} kg" for d, k in w))
        logs = db.daily(p.id, today - dt.timedelta(days=7))
        for r in logs:
            bits = [
                f"{k} {r[k]}"
                for k in ("hunger", "craving", "nausea", "binge", "water", "pills_lunch")
                if r.get(k) is not None
            ]
            if bits or r.get("note"):
                out.append(
                    f"  - {r['day']}: {', '.join(bits)}"
                    + (f" ({r['note']})" if r.get("note") else "")
                )

    try:
        from .calendar import upcoming_events

        events = upcoming_events(hh, today, 7)
    except Exception:  # noqa: BLE001  a calendar outage must not stop the answer
        events = []
    if events:
        out.append("\n## Calendar, next 7 days (guests, meals out, busy afternoons)")
        out.extend(f"- {when}: {title}" for when, title in events)

    r = hh.rules
    out.append(
        f"\n## Weekly rules\nlegumes >= {r.legumes_min}, fish >= {r.fish_min} (oily >= {r.oily_fish_min}), "
        f"salad every day: {r.salad_daily}. Batch sessions on {', '.join(hh.batch.preferred_days)}."
    )

    for day in (today, today + dt.timedelta(days=1)):
        plan = plan_for(data, day)
        if not plan:
            continue
        out.append(f"\n## {t.date(day)}")
        for pd in person_days(data, db, plan, day):
            out.append(
                f"- {pd.name} (x{pd.factor:.2f}, {pd.kcal:.0f} kcal, {pd.protein:.0f} g protein):"
            )
            for meal, pl in pd.plates.items():
                out.append(f"  - {meal}: {pl.recipe.name}: " + "; ".join(pl.lines))

    plan = plan_for(data, today)
    if plan:
        monday = today - dt.timedelta(days=today.weekday())
        out.append("\n## This week")
        for i in range(7):
            d = plan.day(monday + dt.timedelta(days=i))
            if d:
                side = f" + {data.recipes[d.side].name}" if d.side else ""
                out.append(
                    f"- {t.date(d.date)}: lunch {data.recipes[d.lunch].name}{side}; dinner {data.recipes[d.dinner].name}"
                )
    return "\n".join(x for x in out if x is not None)
