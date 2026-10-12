"""What the AI is told before it answers.

rules(): the instructions, which stay the same from one message to the next.
facts(): the household, today's and tomorrow's plates, the week, recent logs and the settings it may
propose to change, as of now.
"""

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
- Write Markdown: **bold**, _italic_, [text](https://...) links and "- " lists. No tables.
- When you can search the web and someone asks for a video or a recipe from elsewhere, give the link
  and compare it with our recipe: say what differs (ingredients, amounts, steps, time).
- The household's data below is as of now; it changes between messages, so trust it over anything
  said earlier in the chat.
"""

CHANGES = """
## Changing settings
You may propose a change to these settings only: lunch time, shopping day, guests, batch days and
sessions per week, the weekly rules, foods to add to the asker's own avoided foods, the shopping
list's stores and section names, reminders other than the pills reminder, a new recipe, and your own
extra instructions (when someone asks you to answer differently from now on). Never
pills, the pills reminder, calories, protein or goals, never someone else's avoided foods, and never
a food taken off an avoided list (it may be there for a medical reason): for those, say that a
person has to edit them by hand. To propose a change, say it in your answer and end the answer with
one block, sending only the keys that change:

```change
set:
  household.lunch_time: "14:30"
  household.shopping_day: monday
  household.guests.<occasion>: 2
  household.batch.preferred_days: [monday, thursday]
  household.batch.sessions_per_week: 2
  household.rules.fish_min: 3          # also legumes_min, oily_fish_min, salad_daily, max_repeats_per_4_weeks
  household.people.<asker's id>.avoid_ingredients: [<ingredient id>]   # the whole new list: the current one plus the new foods
  settings.shopping.primary: <store id>
  settings.shopping.alternatives: [<store id>]
  settings.sections.<section id>: <label>
  settings.reminders.<reminder id>: {at: ["12:00"], days: daily, enabled: true}   # the fields that change; null removes it
  settings.llm.instructions: |   # the whole new text: the current instructions with the change made
    ...
recipe: {id: <new-id>, name: ..., meals: [lunch], kind: batch, ingredients: [{id: <ingredient id>, qty: 80}], steps: [...], nutrition: {kcal: 550, protein_g: 35}}
```

A new reminder needs id, kind (message, meal, batch, shopping, defrost), at, and text
for kind message ({names} and {today} are filled in). The app shows the change to the person who
asked and applies it only after they say yes, so never claim it is done.
"""


SENSITIVE = ("hunger", "craving", "nausea", "binge")


def build(
    data: Data, db: DB, today: dt.date, viewer: str | None = None, group: bool = False
) -> str:
    """Rules and facts in one text, for a provider that gets a system prompt each time."""
    return f"{rules(data)}\n\n{facts(data, db, today, viewer, group)}"


def rules(data: Data) -> str:
    """The instructions: the same for every message, so a provider that keeps a conversation sends
    them once."""
    return "\n".join(
        x for x in (RULES.format(locale=data.locale), CHANGES, data.settings.llm.instructions) if x
    )


def facts(
    data: Data, db: DB, today: dt.date, viewer: str | None = None, group: bool = False
) -> str:
    """The household's data as of now. viewer: in a private chat, the person asking; only their own
    pills, notes and logs are included. group: in the household group, sensitive logs (binges,
    cravings, notes) stay out unless bot.share_logs_in_group is set."""
    t = Translator(data.locale)
    hh = data.household
    out = [f"Today is {t.date(today)} ({today.isoformat()})."]

    out.append("\n## People")
    for p in hh.people:
        if viewer is not None and p.id != viewer:  # a private chat: others by name only
            out.append(
                f"- {p.name} (id {p.id}): shares the meals; their health data is private here."
            )
            continue
        g = p.goal
        line = (
            f"- {p.name} (id {p.id}): {p.sex}, born {p.birth_year}, {p.height_cm:g} cm. "
            f"Goal {g.mode}"
            + (f" to {g.target_kg} kg" if g.target_kg else "")
            + (f", band {g.band_kg[0]}-{g.band_kg[1]} kg" if g.band_kg else "")
            + f". Energy {current_kcal(db, p):.0f} kcal (floor {p.kcal.floor:.0f}), protein {p.protein_g.target:.0f} g."
        )
        own = viewer is None or p.id == viewer
        if p.glp1 and own:
            line += f" On {p.glp1.drug}, injects on {p.glp1.day}."
        if p.avoid_ingredients:
            line += f" Avoids: {', '.join(p.avoid_ingredients)}."
        out.append(line)
        if viewer is not None and p.id != viewer:
            continue  # someone else's health data stays out of a private chat
        for slot, pills in p.pills.items():
            if pills:
                out.append(
                    f"  - pills {slot}: " + "; ".join(f"{x.name} {x.dose}".strip() for x in pills)
                )
        for note in p.notes:
            out.append(f"  - note: {note}")
        share = not group or data.settings.bot.share_logs_in_group
        w = db.weights(p.id)[-4:] if share else []
        if w:
            out.append("  - last weigh-ins: " + ", ".join(f"{d.isoformat()} {k} kg" for d, k in w))
        open_logs = not group or data.settings.bot.share_logs_in_group
        fields = [*(SENSITIVE if open_logs else ()), "water", "pills_lunch"]
        for r in db.daily(p.id, today - dt.timedelta(days=7)):
            bits = [f"{k} {r[k]}" for k in fields if r.get(k) is not None]
            note = r.get("note") if open_logs else None
            if bits or note:
                out.append(f"  - {r['day']}: {', '.join(bits)}" + (f" ({note})" if note else ""))

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
                side = (
                    f" + {data.recipes[d.side].name if d.side in data.recipes else '?'}"
                    if d.side
                    else ""
                )
                out.append(
                    f"- {t.date(d.date)}: lunch {data.recipes[d.lunch].name if d.lunch in data.recipes else '?'}{side}; dinner {data.recipes[d.dinner].name if d.dinner in data.recipes else '?'}"
                )
    out.append(settings_now(data, viewer))
    return "\n".join(x for x in out if x is not None)


def settings_now(data: Data, viewer: str | None = None) -> str:
    """The values a change may touch, with the ids a change block names. viewer: in a private chat,
    only their own avoided foods are listed (they can be medical, and only the asker may add to
    their own list anyway)."""
    hh, st = data.household, data.settings
    stores = ", ".join(f"{sid} ({s.name})" for sid, s in st.stores.items()) or "none"
    out = [
        "\n## Settings now",
        f"- household.lunch_time: {hh.lunch_time}; household.shopping_day: {hh.shopping_day}",
        f"- household.guests: {hh.guests or '{}'}",
        f"- household.batch: days {hh.batch.preferred_days}, {hh.batch.sessions_per_week} sessions a week",
        f"- household.rules: {hh.rules.model_dump()}",
        f"- stores: {stores}; primary {st.shopping.primary}; alternatives {st.shopping.alternatives}",
        f"- settings.sections: {st.sections or '{}'}",
    ]
    for p in hh.people:
        if viewer is None or p.id == viewer:
            out.append(f"- household.people.{p.id}.avoid_ingredients: {p.avoid_ingredients}")
    out.append("- reminders:")
    for r in st.reminders:
        out.append(
            f"  - {r.id}: kind {r.kind}, at {r.at}, days {r.days}"
            + (f", meal {r.meal}" if r.kind == "meal" else "")
            + (f", people {r.people}" if r.people else "")
            + (f", text {r.text!r}" if r.text else "")
            + ("" if r.enabled else ", disabled")
        )
    return "\n".join(out)
