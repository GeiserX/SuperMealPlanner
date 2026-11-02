"""One task list: the chat's reminders, the Cook page and the day's page read the same tasks, and
a session cooked before its own plan starts is on the day it is cooked."""

import datetime as dt
from zoneinfo import ZoneInfo

import yaml
from fastapi.testclient import TestClient

from supermealplanner import llm
from supermealplanner.db import DB
from supermealplanner.model import load
from supermealplanner.planner import generate, plan_for, plans_dir

from .conftest import MONDAY
from .test_menu import change
from .test_settings_stores_bot import FakeBot, bot_data


def D(n: int) -> dt.date:
    return MONDAY + dt.timedelta(days=n)


def _with_batch(data_dir, **ings):
    """bot_data plus an evening batch reminder, and the ingredient flags given (soak, frozen)."""
    bot_data(data_dir)
    raw = yaml.safe_load((data_dir / "settings.yaml").read_text())
    raw["reminders"].append({"id": "batch", "kind": "batch", "at": ["21:30"]})
    (data_dir / "settings.yaml").write_text(yaml.safe_dump(raw, allow_unicode=True))
    found = yaml.safe_load((data_dir / "ingredients.yaml").read_text())
    for iid, flag in ings.items():
        found[iid][flag] = True
    (data_dir / "ingredients.yaml").write_text(yaml.safe_dump(found, allow_unicode=True))
    return load(data_dir)


def _client(data_dir, monkeypatch):
    monkeypatch.setenv("SMP_SCHEDULER", "0")
    monkeypatch.setenv("SMP_BOT", "0")
    from supermealplanner.web import create_app

    return TestClient(create_app(data_dir))


def _section(page: str, marker: str) -> str:
    """The <section> of a page that holds `marker`."""
    head = page[: page.index(marker)]
    start = head.rindex("<section")
    return page[start : page.index("</section>", start)]


def test_a_reminder_the_chat_set_for_a_dinner_is_on_the_cook_page_and_the_day_page(
    data_dir, monkeypatch
):
    data = _with_batch(data_dir)
    dinner = plan_for(data, D(3)).day(D(3)).dinner
    zone = ZoneInfo(data.household.timezone)
    at = dt.datetime.combine(D(2), dt.time(21, 30), zone).timestamp()
    with _client(data_dir, monkeypatch) as c:
        c.app.state.smp.db.add_reminder(
            -100, "ana", at, "Deja las alubias en remojo", 8, dinner, D(3).isoformat()
        )
        cook = at + 3600  # a reminder to cook, set from the chat with its step
        c.app.state.smp.db.add_reminder(
            -100, "ana", cook, "Cuece las alubias", 8, dinner, D(3).isoformat(), "cook"
        )
        batch = c.get(f"/batch?d={MONDAY}").text
        day = c.get(f"/?d={D(2)}").text
        other = c.get(f"/?d={D(3)}").text
    assert "Deja las alubias en remojo" in batch
    assert f'data-day="{D(2)}"' in _section(batch, "Deja las alubias en remojo")  # on its day
    assert "Deja las alubias en remojo" in _section(day, "Deja las alubias en remojo")
    assert "21:30" in _section(day, "Deja las alubias en remojo")
    assert "Deja las alubias en remojo" not in other
    assert "Cuece las alubias" in batch and "Cuece las alubias" in day


def test_a_session_before_its_plan_starts_is_on_the_cook_page_and_the_reminder_of_its_day(
    data_dir, monkeypatch, tmp_path
):
    data = _with_batch(data_dir)
    for p in plans_dir(data).glob("*.yaml"):
        p.unlink()
    generate(data, MONDAY, 1).save(plans_dir(data) / f"{MONDAY}.yaml")  # plan A: this week
    generate(data, D(7), 4).save(plans_dir(data) / f"{D(7)}.yaml")  # plan B: from next Monday
    lunch = data.recipes[plan_for(data, D(8)).day(D(8)).lunch]
    # next plan's Tuesday lunch, cooked on this Saturday (frozen till then)
    change(
        data,
        {"set": {"date": str(D(8)), "meal": "lunch", "recipe": lunch.id, "cook": str(D(5))}},
        today=MONDAY,
    )
    assert plan_for(data, D(8)).day(D(8)).lunch_cook == D(5)
    link = f'href="/recipe/{lunch.id}?d={D(5)}"'
    with _client(data_dir, monkeypatch) as c:
        page = c.get(f"/batch?d={MONDAY}").text
    assert link in page, "the Saturday cooking for next Tuesday is not on this week's Cook page"
    assert t_date(data, D(5)) in _section(page, link)
    b = FakeBot(data, DB(tmp_path / "b.db"), D(5))
    batch = next(r for r in data.settings.reminders if r.id == "batch")
    said = b.reminder_text(batch, D(5))
    assert said and lunch.name in said
    keys = [x.key for x in b.kitchen_tasks(D(5), D(5))]
    assert f"cook:{D(5)}:pm" in keys


def t_date(data, day: dt.date) -> str:
    from supermealplanner.i18n import Translator

    return Translator(data.locale).date(day).capitalize()


def test_a_reminders_line_says_which_step_it_is():
    text = (
        "Ok.\n```reminders\n"
        "- 2026-01-17 21:30 | Deja las alubias en remojo | ana | | dish ensalada-1 2026-01-18 soak\n"
        "- 2026-01-17 22:00 | Saca la merluza | | | dish merluza-cena-1 2026-01-18 soak-fridge\n"
        "- 2026-01-17 18:00 | Compra pan | | | dish ensalada-1 2026-01-18\n"
        "```"
    )
    _, found = llm.extract_reminders(text)
    assert [(r.get("recipe"), r.get("date"), r.get("kind")) for r in found] == [
        ("ensalada-1", "2026-01-18", "soak"),
        ("merluza-cena-1", "2026-01-18", "soak-fridge"),
        ("ensalada-1", "2026-01-18", None),
    ]
    assert found[0]["person"] == "ana"
    # an unknown word is not a kind: the dish field is not read at all, as before
    _, odd = llm.extract_reminders(
        "```reminders\n- 2026-01-17 21:30 | x | | | dish ensalada-1 2026-01-18 dance\n```"
    )
    assert "kind" not in odd[0]


def test_a_kind_set_from_the_chat_is_kept_and_listed_with_the_task(data_dir, tmp_path):
    data = _with_batch(data_dir)
    dinner = plan_for(data, D(3)).day(D(3)).dinner
    b = FakeBot(data, DB(tmp_path / "b.db"), MONDAY)
    zone = ZoneInfo(data.household.timezone)
    at = dt.datetime.combine(D(2), dt.time(21, 30), zone).timestamp()
    rid = b.state.db.add_reminder(-100, None, at, "Remojo", 8, dinner, str(D(3)), "soak")
    task = next(x for x in b.kitchen_tasks(D(2), D(2)) if x.reminder == rid)
    assert (task.key, task.kind, task.recipe, task.eat, task.source) == (
        f"r:{rid}",
        "soak",
        dinner,
        D(3),
        "chat",
    )
    assert "soak" in b.chat_reminders(-100)  # the AI sees the step it set


def test_the_batch_reminder_says_exactly_the_tasks_of_its_evening(data_dir, tmp_path):
    """A guard: the chat's evening message and the task list never drift apart."""
    data = _with_batch(data_dir, garbanzo="soak", merluza="frozen")
    b = FakeBot(data, DB(tmp_path / "b.db"), MONDAY)
    batch = next(r for r in data.settings.reminders if r.id == "batch")
    seen = set()
    for i in range(14):
        day = D(i)
        said = b.reminder_text(batch, day) or ""
        jobs = b.kitchen_tasks(day, day + dt.timedelta(days=1))
        tonight = [x for x in jobs if x.day == day and x.kind in ("soak", "thaw")]
        listed = [
            line
            for part in said.split("\n\n")
            if part.startswith(("🫘", "🧊"))
            for line in part.splitlines()[1:]
            if line.startswith("• ")
        ]
        assert sorted(listed) == sorted(x.line for x in tonight), day
        for x in jobs:
            if x.key == f"cook:{day}:pm":
                assert x.line in said, (day, x.key)
                seen.add("cook")
        seen |= {x.kind for x in tonight}
    assert {"cook", "soak", "thaw"} <= seen  # the two weeks hold every kind at least once
