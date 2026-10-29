"""The eve of a batch session: the dried pulses to leave in water that night, in the batch
reminder (also when that day is paused), on the Tasks page and through the API."""

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
    assert f"({meal.capitalize()}: {data.recipes[rid].name})" in lines[0]
    assert int(lines[0].split(": ")[1].split(" g")[0]) >= 150  # every eater's share
    assert f"/?d={hit.date}" in said
    after = said.splitlines()[said.splitlines().index(lines[0]) + 1]
    assert after.strip().endswith(f"/recipe/{rid}?d={hit.date}")  # the dish's recipe, one tap away
    assert "🧊" not in (b.reminder_text(batch, miss.date - dt.timedelta(days=1)) or "")
    # tomorrow paused: nothing is eaten, nothing to take out
    change(data, {"pause": {"from": str(hit.date), "to": str(hit.date)}}, today=eve)
    assert "🧊" not in (b.reminder_text(batch, eve) or "")
