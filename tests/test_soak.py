"""The eve of a batch session: the dried pulses to leave in water that night, in the batch
reminder (also when that day is paused), on the Tasks page and through the API."""

import asyncio
import datetime as dt

import yaml
from fastapi.testclient import TestClient

from supermealplanner.db import DB
from supermealplanner.planner import plan_for

from .conftest import MONDAY
from .test_app_api import auth, pair
from .test_menu import change, planned
from .test_settings_stores_bot import FakeBot, bot_data


def soaking(data_dir):
    """Chickpeas need soaking, lentils do not; a batch reminder; a 4-week plan."""
    bot_data(data_dir)
    raw = yaml.safe_load((data_dir / "settings.yaml").read_text())
    raw["reminders"].append({"id": "batch", "kind": "batch", "at": ["21:30"]})
    (data_dir / "settings.yaml").write_text(yaml.safe_dump(raw, allow_unicode=True))
    ings = yaml.safe_load((data_dir / "ingredients.yaml").read_text())
    ings["garbanzo"]["soak"] = True
    (data_dir / "ingredients.yaml").write_text(yaml.safe_dump(ings, allow_unicode=True))
    data = planned(data_dir)
    plan = plan_for(data, MONDAY)

    def uses(s, iid):
        return any(i.id == iid for x in s.dishes for i in data.recipes[x.recipe].ingredients)

    soak = next(s for s in plan.sessions if s.date > MONDAY and uses(s, "garbanzo"))
    dry = next(
        s
        for s in plan.sessions
        if s.date > MONDAY and uses(s, "lenteja") and not uses(s, "garbanzo")
    )
    return data, soak, dry


def test_the_batch_reminder_says_what_to_soak_the_eve_of_the_session(data_dir, tmp_path):
    data, soak, dry = soaking(data_dir)
    b = FakeBot(data, DB(tmp_path / "b.db"), MONDAY)
    batch = next(r for r in data.settings.reminders if r.id == "batch")
    eve = soak.date - dt.timedelta(days=1)
    said = b.reminder_text(batch, eve)
    assert said and "🫘 Tonight, leave to soak" in said
    dish = next(
        data.recipes[x.recipe]
        for x in soak.dishes
        if any(i.id == "garbanzo" for i in data.recipes[x.recipe].ingredients)
    )
    line = next(x for x in said.splitlines() if x.startswith("• Garbanzo"))
    assert dish.name in line and " g, for " in line
    grams = int(line.split(": ")[1].split(" g")[0])
    assert grams >= 70  # the whole dish, every eater's share
    assert "🫘" not in (b.reminder_text(batch, dry.date - dt.timedelta(days=1)) or "")  # lentils
    # the eve is paused: the soaking still goes
    change(data, {"pause": {"from": str(eve), "to": str(eve)}}, today=eve)
    assert "• Garbanzo" in b.reminder_text(batch, eve)
    # the session's own day is paused: nothing cooks, nothing to soak
    change(data, {"resume": {"from": str(eve), "to": str(eve)}}, today=eve)
    change(data, {"pause": {"from": str(soak.date), "to": str(soak.date)}}, today=eve)
    assert "🫘" not in (b.reminder_text(batch, eve) or "")


def test_the_soaking_shows_on_the_tasks_page_and_through_the_api(data_dir, monkeypatch):
    monkeypatch.setenv("SMP_SCHEDULER", "0")
    monkeypatch.setenv("SMP_BOT", "0")
    data, soak, _ = soaking(data_dir)
    eve = soak.date - dt.timedelta(days=1)
    from supermealplanner.web import create_app

    with TestClient(create_app(data_dir)) as c:
        page = c.get(f"/tasks?d={eve}").text
        h = auth(pair(c, data_dir)["token"])
        day = c.get(f"/api/v1/day?d={eve}", headers=h).json()
        tasks = c.get(f"/api/v1/tasks?d={eve}", headers=h).json()
        other = c.get(f"/api/v1/day?d={soak.date}", headers=h).json()
    assert "Garbanzo" in page and "Tonight, leave to soak" in page
    items = day["soak_for_tomorrow"]
    assert items and {x["ingredient"] for x in items} == {"garbanzo"}
    x = items[0]
    assert x["unit"] == "g" and x["qty"] >= 70 and x["amount"].endswith(" g")
    assert x["recipe"]["id"] in {d.recipe for d in soak.dishes}
    texts = next(d for d in tasks["days"] if d["day"] == eve.isoformat())["tasks"]
    assert any("• Garbanzo" in t["text"] for t in texts)
    assert all(y["ingredient"] == "garbanzo" for y in other["soak_for_tomorrow"])
    assert data.ingredients["lenteja"].soak is False


def test_the_batch_reminder_says_what_to_take_out_of_the_freezer_the_evening_before(
    data_dir, tmp_path
):
    bot_data(data_dir)
    raw = yaml.safe_load((data_dir / "settings.yaml").read_text())
    raw["reminders"].append({"id": "batch", "kind": "batch", "at": ["21:30"]})
    (data_dir / "settings.yaml").write_text(yaml.safe_dump(raw, allow_unicode=True))
    ings = yaml.safe_load((data_dir / "ingredients.yaml").read_text())
    ings["merluza"]["frozen"] = True
    (data_dir / "ingredients.yaml").write_text(yaml.safe_dump(ings, allow_unicode=True))
    data = planned(data_dir)
    plan = plan_for(data, MONDAY)
    b = FakeBot(data, DB(tmp_path / "b.db"), MONDAY)
    batch = next(r for r in data.settings.reminders if r.id == "batch")

    def frozen_in(d):
        return [
            (meal, getattr(d, meal))
            for meal in ("breakfast", "lunch", "side", "snack", "dinner")
            if getattr(d, meal)
            and any(i.id == "merluza" for i in data.recipes[getattr(d, meal)].ingredients)
        ]

    days = [d for d in plan.days if d.date > MONDAY]
    hit = next(d for d in days if frozen_in(d))
    miss = next(d for d in days if not frozen_in(d))
    eve = hit.date - dt.timedelta(days=1)
    said = b.reminder_text(batch, eve)
    assert said and "🧊 Tonight, take out of the freezer" in said
    lines = [x for x in said.splitlines() if x.startswith("• Merluza: ")]
    assert len(lines) == len(frozen_in(hit))  # one line per meal that uses it
    meal, rid = frozen_in(hit)[0]
    assert (
        f"({meal.capitalize()}: [{data.recipes[rid].name}](https://" in lines[0]
    )  # the dish to tap
    assert int(lines[0].split(": ")[1].split(" g")[0]) >= 150  # every eater's share
    assert f"/?d={hit.date}" in said
    assert f"/recipe/{rid}?d={hit.date})" in lines[0]  # the dish's recipe, one tap away
    assert "🧊" not in (b.reminder_text(batch, miss.date - dt.timedelta(days=1)) or "")
    # tomorrow paused: nothing is eaten, nothing to take out
    change(data, {"pause": {"from": str(hit.date), "to": str(hit.date)}}, today=eve)
    assert "🧊" not in (b.reminder_text(batch, eve) or "")


def test_what_is_frozen_is_taken_out_the_evening_before_it_is_eaten_even_when_cooked_ahead(
    data_dir, tmp_path
):
    """Frozen food (bread) is served with the meal: a lunch cooked ahead in a session still needs
    it the evening before the day it is eaten, one day's share, never all of it before the session."""
    bot_data(data_dir)
    raw = yaml.safe_load((data_dir / "settings.yaml").read_text())
    raw["reminders"].append({"id": "batch", "kind": "batch", "at": ["21:30"]})
    (data_dir / "settings.yaml").write_text(yaml.safe_dump(raw, allow_unicode=True))
    data = planned(data_dir)
    plan = plan_for(data, MONDAY)
    ahead = next(
        d
        for d in plan.days
        if MONDAY < d.lunch_cook < d.date and plan.day(d.lunch_cook).lunch != d.lunch
    )
    ings = yaml.safe_load((data_dir / "ingredients.yaml").read_text())
    first = data.recipes[ahead.lunch].ingredients[0].id
    ings[first]["frozen"] = True
    (data_dir / "ingredients.yaml").write_text(yaml.safe_dump(ings, allow_unicode=True))
    data = planned(data_dir)
    plan = plan_for(data, MONDAY)
    b = FakeBot(data, DB(tmp_path / "b.db"), MONDAY)
    batch = next(r for r in data.settings.reminders if r.id == "batch")
    tag = f"(Lunch: [{data.recipes[ahead.lunch].name}]("
    said = b.reminder_text(batch, ahead.date - dt.timedelta(days=1)) or ""
    assert sum(tag in x for x in said.splitlines()) == 1
    assert plan.day(ahead.lunch_cook).lunch != ahead.lunch  # so the session's eve tells them apart
    said = b.reminder_text(batch, ahead.lunch_cook - dt.timedelta(days=1)) or ""
    assert not any(tag in x for x in said.splitlines())  # not all of it before the session


def test_reminders_due_at_the_same_time_go_as_one_message(data_dir, tmp_path):
    import supermealplanner.bot as botmod

    bot_data(data_dir)
    raw = yaml.safe_load((data_dir / "settings.yaml").read_text())
    raw["reminders"] = [
        {"id": "a", "kind": "message", "text": "First thing.", "at": ["21:30"]},
        {"id": "b", "kind": "message", "text": "Second thing.", "at": ["21:30"]},
        {"id": "c", "kind": "message", "text": "Later thing.", "at": ["22:00"]},
    ]
    (data_dir / "settings.yaml").write_text(yaml.safe_dump(raw, allow_unicode=True))
    data = planned(data_dir)
    db = DB(tmp_path / "b.db")
    b = FakeBot(data, db, MONDAY)
    sent = []

    async def send(client, chat_id, text, reply_to=None, record=True):
        sent.append(text)

    class Clock(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return dt.datetime.combine(MONDAY, dt.time(21, 31), tz)

    real = botmod.dt.datetime
    botmod.dt.datetime = Clock
    try:
        b.send = send
        asyncio.run(b.tick_reminders(None))
        asyncio.run(b.tick_reminders(None))
    finally:
        botmod.dt.datetime = real
    assert sent == ["First thing.\n\nSecond thing."]  # one message, and only once
    assert db.was_sent("a", MONDAY, "21:30") and db.was_sent("b", MONDAY, "21:30")


def test_links_go_as_words_and_nothing_else_turns_into_markup():
    """A reminder's links are words to tap; the rest of a plain message stays exactly as written."""
    from supermealplanner import render

    text = "⏰ @ana_92: pasa *la* merluza <ya>\n[Receta](https://x.example/recipe/a?d=1&b=2)"
    assert render.has_link(text) and not render.has_link("https://x.example/recipe/a")
    assert render.links_html(text) == (
        "⏰ @ana_92: pasa *la* merluza &lt;ya&gt;\n"
        '<a href="https://x.example/recipe/a?d=1&amp;b=2">Receta</a>'
    )
