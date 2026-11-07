"""A cooking session's sheet says how long each dish takes for the amount cooked that day, not
the recipe's minutes for a small pot, and how long the whole session takes; the app reads the
same through the API."""

from supermealplanner.db import DB
from supermealplanner.i18n import Translator
from supermealplanner.planner import Plans, plan_for
from supermealplanner.portions import (
    BASE_SERVINGS,
    batch_sheet,
    duration_text,
    session_minutes,
    session_sheets,
)

from .conftest import MONDAY
from .test_app_api import auth, client, pair  # noqa: F401
from .test_batch_servings import _session
from .test_web_tasks_list_ratings import _app


def test_more_servings_take_more_time_than_the_recipe_says(data, tmp_path):
    plan, s = _session(data)
    b = batch_sheet(data, DB(tmp_path / "b.db"), plan, s.date)[0]
    b.recipe = b.recipe.model_copy(update={"minutes": 60})
    b.total = BASE_SERVINGS
    assert b.minutes == 60  # the recipe's own amount: its own time
    b.total = 1
    assert b.minutes == 60  # less never takes less: the pot still simmers as long
    b.total = 3 * BASE_SERVINGS
    assert b.minutes == 120  # half the time is preparation, and it grows with the amount
    b.total = 2.3 * BASE_SERVINGS
    assert b.minutes == 100  # rounded up to five minutes
    assert session_minutes([b, b]) == 200


def test_a_duration_reads_in_hours_and_minutes():
    t, es = Translator("en"), Translator("es")
    assert duration_text(t, 45) == "45 min"
    assert duration_text(t, 120) == "2 h"
    assert duration_text(es, 135) == "2 h 15 min"


def test_the_cook_page_shows_each_dish_s_time_and_the_session_s_total(data_dir, data, monkeypatch):
    c, plan = _app(data_dir, data, monkeypatch)
    day = next(s.date for s in plan.sessions if s.dishes)
    sessions = session_sheets(data, DB(data_dir / "x.db"), Plans(data), day)
    assert any(b.total > BASE_SERVINGS for _, ds in sessions for b in ds)
    with c:
        page = c.get(f"/batch?d={day}").text
    t = Translator("en")
    for _, dishes in sessions:
        total = duration_text(t, session_minutes(dishes))
        assert f"Estimated time for the whole session: {total}" in page
        for b in dishes:
            said = f"About {duration_text(t, b.minutes)} for this amount (the recipe: "
            assert said + f"{b.recipe.minutes} min)" in page


def test_the_api_sends_each_dish_s_time_and_each_session_s_total(client, data_dir):  # noqa: F811
    from supermealplanner.model import load

    data = load(data_dir)
    plans = Plans(data)
    day = next(s.date for s in plan_for(data, MONDAY).sessions if s.dishes)
    sessions = session_sheets(data, DB(data_dir / "y.db"), plans, day)
    h = auth(pair(client, data_dir)["token"])
    cook = client.get(f"/api/v1/cook?d={day}", headers=h).json()
    dishes = [b for _, ds in sessions for b in ds]
    assert [d["cook_minutes"] for d in cook["dishes"]] == [b.minutes for b in dishes]
    assert any(d["cook_minutes"] > d["recipe"]["minutes"] for d in cook["dishes"])
    assert [x["cook_minutes"] for x in cook["sessions"]] == [
        session_minutes(ds) for _, ds in sessions
    ]
    assert [x["recipes"] for x in cook["sessions"]] == [
        [b.recipe.id for b in ds] for _, ds in sessions
    ]
    assert cook["cook_minutes"] == session_minutes(dishes)
    assert cook["cook_minutes_text"] == duration_text(
        Translator(data.locale), session_minutes(dishes)
    )
    assert all(x["at"] for x in cook["sessions"])
