import re

from supermealplanner.db import DB
from supermealplanner.i18n import Translator
from supermealplanner.model import load
from supermealplanner.planner import generate, plans_dir
from supermealplanner.portions import batch_dish_text, batch_for_text, batch_sheet

from .conftest import MONDAY
from .test_settings_stores_bot import FakeBot, bot_data
from .test_web_tasks_list_ratings import _app


def _session(data):
    plan = generate(data, MONDAY, 4)
    return plan, next(s for s in plan.sessions if s.dishes)


def test_the_batch_sheet_shows_whole_servings_then_the_split_by_person(data_dir, data, monkeypatch):
    c, plan = _app(data_dir, data, monkeypatch)
    s = next(s for s in plan.sessions if s.dishes and not s.morning)
    with c:
        page = c.get(f"/batch?d={s.date}").text
    dishes = batch_sheet(data, DB(data_dir / "x.db"), plan, s.date)
    assert dishes
    for b in dishes:
        assert f"{b.servings} servings" in page or "1 serving" in page
    assert not re.search(r"\d+\.\d servings", page)  # never a decimal total
    assert "Per person: " in page and "Cook on " in page and "For the lunch of " in page


def test_a_total_rounds_to_a_whole_number_of_servings(data, tmp_path):
    plan, s = _session(data)
    b = batch_sheet(data, DB(tmp_path / "b.db"), plan, s.date)[0]
    b.total = 3.5
    assert b.servings == 4
    b.total = 0.2
    assert b.servings == 1


def test_the_dish_text_says_when_it_is_cooked_and_which_lunches_it_is_for(data, tmp_path):
    plan, s = _session(data)
    t = Translator("en")
    b = batch_sheet(data, DB(tmp_path / "b.db"), plan, s.date)[0]
    text = batch_dish_text(t, b, {"ana": "Ana"})
    assert t.date(s.date) in text and "Per person: " in text
    assert batch_for_text(t, [MONDAY, MONDAY.replace(day=MONDAY.day + 1)]) == (
        "For the lunch of monday and tuesday"
    )
    es = Translator("es")
    assert batch_for_text(es, [MONDAY, MONDAY.replace(day=MONDAY.day + 1)]) == (
        "Para la comida del lunes y del martes"
    )


def test_the_batch_reminder_lists_each_dish_with_its_servings_and_days(data_dir, tmp_path):
    data = bot_data(data_dir)
    plan, s = _session(data)
    plan.save(plans_dir(data) / f"{MONDAY}.yaml")
    data = load(data_dir)
    bot = FakeBot(data, DB(tmp_path / "b.db"), MONDAY)
    text = bot.batch_text(data, plan, s.date, s)
    assert "servings" in text or "1 serving" in text
    assert "Cook on " in text and "For the lunch of " in text and "Per person: " in text
