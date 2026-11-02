"""A cooking session says the time it really starts: the household's start for a session cooked
the day its lunch is eaten, and "at night" for an evening one from 20:00 on."""

import datetime as dt

import pytest
import yaml
from fastapi.testclient import TestClient

from supermealplanner.db import DB
from supermealplanner.i18n import Translator
from supermealplanner.model import DataError, load
from supermealplanner.planner import plan_for

from .conftest import MONDAY
from .test_settings_stores_bot import FakeBot, bot_data


def _data(data_dir, batch_at="21:30", start=None):
    """bot_data plus a batch reminder at `batch_at`, and batch.start when given."""
    if start is not None:
        hh = yaml.safe_load((data_dir / "household.yaml").read_text())
        hh.setdefault("batch", {})["start"] = start
        (data_dir / "household.yaml").write_text(yaml.safe_dump(hh, allow_unicode=True))
    bot_data(data_dir)
    raw = yaml.safe_load((data_dir / "settings.yaml").read_text())
    raw["reminders"].append({"id": "batch", "kind": "batch", "at": [batch_at]})
    (data_dir / "settings.yaml").write_text(yaml.safe_dump(raw, allow_unicode=True))
    return load(data_dir)


def _cook(b, day, half):
    return next(x for x in b.kitchen_tasks(day, day) if x.key == f"cook:{day}:{half}")


def _page(data_dir, monkeypatch, path):
    monkeypatch.setenv("SMP_SCHEDULER", "0")
    monkeypatch.setenv("SMP_BOT", "0")
    from supermealplanner.web import create_app

    with TestClient(create_app(data_dir)) as c:
        return c.get(path).text


def test_a_same_day_session_starts_at_the_households_start_for_that_weekday(
    data_dir, tmp_path, monkeypatch
):
    data = _data(data_dir, start={"monday": "13:00"})
    assert any(s.date == MONDAY and s.morning for s in plan_for(data, MONDAY).sessions)
    b = FakeBot(data, DB(tmp_path / "b.db"), MONDAY)
    task = _cook(b, MONDAY, "am")
    assert task.at == "13:00"  # the Tasks and Cook pages show this time
    assert "from 13:00" in task.text and "in the morning" not in task.text
    batch = next(r for r in data.settings.reminders if r.id == "batch")
    said = b.reminder_text(batch, MONDAY - dt.timedelta(days=1))  # the evening before
    assert said and said.startswith("🍲 Batch cooking tomorrow from 13:00:")
    assert "from 13:00" in task.line  # the sheet in the chat: "Cook on <day>, from 13:00"
    page = _page(data_dir, monkeypatch, f"/batch?d={MONDAY}")
    assert "from 13:00" in page and "in the morning" not in page
    tasks = _page(data_dir, monkeypatch, f"/tasks?d={MONDAY}")
    assert "13:00" in tasks


def test_without_a_start_a_same_day_session_stays_in_the_morning_at_ten(data_dir, tmp_path):
    data = _data(data_dir, start={"saturday": "13:00"})  # another weekday's start
    b = FakeBot(data, DB(tmp_path / "b.db"), MONDAY)
    task = _cook(b, MONDAY, "am")
    assert task.at == "10:00" and "in the morning" in task.text
    batch = next(r for r in data.settings.reminders if r.id == "batch")
    said = b.reminder_text(batch, MONDAY - dt.timedelta(days=1))
    assert said and said.startswith("🍲 Batch cooking tomorrow morning")


def test_a_start_that_is_not_a_time_is_refused_at_load(data_dir):
    with pytest.raises(DataError, match="batch.start"):
        _data(data_dir, start={"saturday": "1pm"})


def test_an_evening_session_from_eight_on_is_said_to_be_at_night(data_dir, tmp_path):
    data = _data(data_dir, batch_at="21:30")
    day = next(s.date for s in plan_for(data, MONDAY).sessions if not s.morning)
    b = FakeBot(data, DB(tmp_path / "b.db"), MONDAY)
    task = _cook(b, day, "pm")
    assert task.at == "21:30"
    assert "at night" in task.text and "in the afternoon" not in task.text
    assert "at night" in task.line  # the sheet in the chat: "Cook on <day>, at night"
    early = FakeBot(_data(data_dir, batch_at="17:00"), DB(tmp_path / "c.db"), MONDAY)
    task = _cook(early, day, "pm")
    assert "in the afternoon" in task.text and "at night" not in task.text


def test_the_spanish_words_for_a_sessions_time():
    from supermealplanner.portions import session_when

    t = Translator("es")
    assert session_when(t, True, "10:00") == "por la mañana"
    assert session_when(t, True, "13:00") == "desde las 13:00"
    assert session_when(t, False, "17:00") == "por la tarde"
    assert session_when(t, False, "20:00") == "por la noche"
    assert session_when(t, False, "21:30") == "por la noche"
    assert t("reminder_batch_from", time="13:00", dishes="x", url="u").startswith(
        "🍲 Mañana desde las 13:00"
    )


def test_a_start_at_or_after_lunch_is_refused_at_load(data_dir):
    """The session cooks that day's lunch, so it starts before it (the synthetic lunch: 14:00)."""
    for at in ("14:00", "15:30"):
        with pytest.raises(DataError, match="before lunch_time"):
            _data(data_dir, start={"saturday": at})
    assert _data(data_dir, start={"saturday": "13:59"}).household.batch.start


def test_the_assistant_and_the_check_know_when_a_session_starts(data_dir, tmp_path):
    """The chat AI sees the setting and may change it; the check of its answers is told the same
    start the pages say, not "in the morning"."""
    from supermealplanner import changes, context, verifier
    from supermealplanner.planner import Plans

    data = _data(data_dir, start={"monday": "13:00"})
    facts = context.facts(data, DB(tmp_path / "x.db"), MONDAY)
    assert "start {'monday': '13:00'}" in facts
    assert "household.batch.start.saturday" in context.rules(data)
    sessions = verifier.sessions_now(data, Plans(data), MONDAY)
    monday = next(x for x in sessions if f"Monday {MONDAY}" in x)
    assert "from 13:00, before lunch" in monday and "in the morning" not in monday
    # set from the chat, by anyone in the household; null takes it away
    p = changes.parse({"set": {"household.batch.start.saturday": "12:30"}}, data, "luis")
    assert changes.describe(p, data, Translator("en")) == ["• Batch start (Sat): (none) → 12:30"]
    texts = changes.render(data_dir, p)
    changes.validate(data_dir, texts)
    hh = yaml.safe_load(texts["household.yaml"])
    assert hh["batch"]["start"] == {"monday": "13:00", "saturday": "12:30"}
    late = changes.parse({"set": {"household.batch.start.saturday": "15:00"}}, data, "luis")
    with pytest.raises(changes.ChangeError, match="would not validate"):
        changes.validate(data_dir, changes.render(data_dir, late))
    gone = changes.parse({"set": {"household.batch.start.monday": None}}, data, "luis")
    hh = yaml.safe_load(changes.render(data_dir, gone)["household.yaml"])
    assert not hh["batch"].get("start")
    elsewhere = _data(data_dir, start={"saturday": "13:00"})
    sessions = verifier.sessions_now(elsewhere, Plans(elsewhere), MONDAY)
    assert "in the morning, before lunch" in next(x for x in sessions if f"Monday {MONDAY}" in x)
