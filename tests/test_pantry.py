"""The virtual pantry (pantry.py): a tick brings in what it bought and unticking takes it back,
each day the plan cooks takes its ingredients out, fresh food goes off, the chat says what is
there, the list leaves off what the pantry has enough of, and the consistency check names food
that goes off before any dish uses it."""

import datetime as dt
import math
from zoneinfo import ZoneInfo

import pytest
import yaml
from fastapi.testclient import TestClient

from supermealplanner import context, doctor, pantry
from supermealplanner.db import DB
from supermealplanner.model import load
from supermealplanner.planner import plan_for
from supermealplanner.portions import ingredients_of, needs, servings_on, shopping_list

from .conftest import MONDAY
from .test_app_api import auth, db_of, pair, planned
from .test_conversations import block as change
from .test_reminders import setup
from .test_settings_stores_bot import CATALOG, msg, run, write
from .test_turns import cfg, sends

ZONE = ZoneInfo("Europe/Madrid")


def D(n):
    return MONDAY + dt.timedelta(days=n)


def at(n, hour=10):
    """A moment of day n, as the household's clock reads it."""
    d = D(n)
    return dt.datetime(d.year, d.month, d.day, hour, tzinfo=ZONE)


def keeps(data_dir, **days):
    raw = yaml.safe_load((data_dir / "ingredients.yaml").read_text())
    for iid, n in days.items():
        raw[iid]["keeps_days"] = n
    (data_dir / "ingredients.yaml").write_text(yaml.safe_dump(raw, allow_unicode=True))
    return load(data_dir)


def qty(data, db, day, iid):
    return next((x.qty for x in pantry.now(data, db, day) if x.ingredient == iid), 0)


def used(data, db, iid, *days):
    plan = plan_for(data, MONDAY)
    return ingredients_of(data, servings_on(data, db, plan, set(days))).get(iid, 0)


def lentils_week(data, db):
    """The first day of the plan's first week that cooks lentils, and how much the week needs."""
    day = next(D(n) for n in range(7) if used(data, db, "lenteja", D(n)))
    return day, needs(data, db, plan_for(data, MONDAY), MONDAY)["lenteja"]


@pytest.fixture
def app(data_dir, monkeypatch):
    monkeypatch.setenv("SMP_SCHEDULER", "0")
    monkeypatch.setenv("SMP_BOT", "0")
    planned(data_dir)
    from supermealplanner.web import create_app

    with TestClient(create_app(data_dir)) as c:
        yield c


# ---------------------------------------------------------------- what comes in


def test_a_tick_brings_in_what_the_line_buys_and_unticking_takes_it_back(data_dir, tmp_path):
    b, db = setup(data_dir, tmp_path)
    need = needs(b.data, db, plan_for(b.data, MONDAY), MONDAY)["pollo"]
    pantry.tick(b.data, db, MONDAY, "pollo", True, at(0, 9))
    assert qty(b.data, db, MONDAY, "pollo") == math.ceil(need / 10) * 10
    assert "pollo" in db.ticked(MONDAY)  # the list keeps the tick, as before
    pantry.tick(b.data, db, MONDAY, "pollo", True, at(0, 9))  # a second tick: one entry
    assert len(db.lots()) == 1
    pantry.tick(b.data, db, MONDAY, "pollo", False, at(0, 9))
    assert qty(b.data, db, MONDAY, "pollo") == 0 and db.lots() == []


def test_a_phone_retrying_a_tick_does_not_bring_the_purchase_in_again(data_dir, tmp_path):
    b, db = setup(data_dir, tmp_path)
    day = next(D(n) for n in range(7) if used(b.data, db, "pollo", D(n)))
    pantry.tick(b.data, db, MONDAY, "pollo", True, at(0, 9))
    after = day + dt.timedelta(days=1)
    left = qty(b.data, db, after, "pollo")
    first = (db.lots()[0]["at"], db.tick_times(MONDAY)["pollo"])
    pantry.tick(b.data, db, MONDAY, "pollo", True, at((after - MONDAY).days, 9))  # the retry
    assert (db.lots()[0]["at"], db.tick_times(MONDAY)["pollo"]) == first
    assert qty(b.data, db, after, "pollo") == left  # what the cooking took stays taken
    pantry.tick(b.data, db, MONDAY, "espinaca", False, at(0))  # an untick of nothing
    assert len(db.lots()) == 1


def test_with_a_catalogue_a_tick_brings_in_the_whole_packs(data_dir, tmp_path):
    _b, db = setup(data_dir, tmp_path)
    write(data_dir, "stores/store.yaml", dict(CATALOG, store="store"))  # the default store
    data = load(data_dir)
    pantry.tick(data, db, MONDAY, "aceite", True, at(0, 9))
    need = needs(data, db, plan_for(data, MONDAY), MONDAY)["aceite"]
    assert need < 1000 and qty(data, db, MONDAY, "aceite") == 1000  # one 1 l bottle


def test_a_tick_of_food_the_week_does_not_cook_brings_nothing(data_dir, tmp_path):
    b, db = setup(data_dir, tmp_path)
    pantry.tick(b.data, db, MONDAY, "espinaca", True, at(0))  # avoided: never on the list
    assert db.lots() == []


# ---------------------------------------------------------------- what goes out


def test_each_day_the_plan_cooks_takes_its_ingredients_out(data_dir, tmp_path):
    b, db = setup(data_dir, tmp_path)
    pantry.correct(b.data, db, {"lenteja": 5000}, at(0, 8))
    gone = used(b.data, db, "lenteja", D(0), D(1), D(2))
    assert gone > 0
    assert qty(b.data, db, D(0), "lenteja") == 5000  # today's cooking is not over yet
    assert math.isclose(qty(b.data, db, D(3), "lenteja"), 5000 - gone)


def test_what_the_chat_says_at_night_is_what_is_left_after_that_days_cooking(data_dir, tmp_path):
    b, db = setup(data_dir, tmp_path)
    day, _ = lentils_week(b.data, db)
    n = (day - MONDAY).days
    pantry.correct(b.data, db, {"lenteja": 1000}, at(n, 22))
    assert qty(b.data, db, D(n + 1), "lenteja") == 1000


def test_fresh_food_goes_off_after_its_keeps_days(data_dir, tmp_path):
    _b, db = setup(data_dir, tmp_path)
    data = keeps(data_dir, espinaca=2)
    pantry.correct(data, db, {"espinaca": 300}, at(0))
    assert qty(data, db, D(1), "espinaca") == 300
    assert next(x for x in pantry.now(data, db, D(1))).expires == D(2)
    assert qty(data, db, D(2), "espinaca") == 0


def test_the_chat_saying_it_ran_out_empties_it_and_puts_it_back_on_the_list(data_dir, tmp_path):
    b, db = setup(data_dir, tmp_path)
    pantry.tick(b.data, db, MONDAY, "pollo", True, at(0, 9))
    db.set_have("aceite", True)
    pantry.correct(b.data, db, {"pollo": 0, "aceite": 0}, at(0, 12))
    assert qty(b.data, db, MONDAY, "pollo") == 0 and "aceite" not in db.have()
    pantry.correct(b.data, db, {"pollo": 400}, at(0, 13))
    assert qty(b.data, db, MONDAY, "pollo") == 400  # replaces, never adds up


# ---------------------------------------------------------------- the list


def test_the_list_leaves_off_what_the_pantry_has_enough_of_for_the_week(data_dir, tmp_path):
    b, db = setup(data_dir, tmp_path)
    _, need = lentils_week(b.data, db)
    plan = plan_for(b.data, MONDAY)

    def on_list(today):
        shop = shopping_list(b.data, db, plan, MONDAY, today=today)
        return {x.ingredient.id for s in shop.stores for x in s.lines}, shop

    assert "lenteja" in on_list(MONDAY)[0]
    pantry.correct(b.data, db, {"lenteja": need / 2}, at(0, 7))
    assert "lenteja" in on_list(MONDAY)[0]  # not enough: still asked for
    pantry.correct(b.data, db, {"lenteja": need + 50}, at(0, 7))
    lines, shop = on_list(MONDAY)
    assert "lenteja" not in lines and "lenteja" in {i.id for i in shop.stocked}
    lines = shopping_list(b.data, db, plan, MONDAY)
    assert "lenteja" in {x.ingredient.id for s in lines.stores for x in s.lines}  # no today


def test_a_tick_on_this_weeks_list_never_takes_its_own_line_off(data_dir, tmp_path):
    b, db = setup(data_dir, tmp_path)
    plan = plan_for(b.data, MONDAY)
    pantry.tick(b.data, db, MONDAY, "pollo", True, at(0, 9))
    shop = shopping_list(b.data, db, plan, MONDAY, today=MONDAY)
    assert "pollo" in {x.ingredient.id for s in shop.stores for x in s.lines}


def test_next_weeks_list_counts_what_this_week_cooks_first(data_dir, tmp_path):
    b, db = setup(data_dir, tmp_path)
    nxt = D(7)
    plan = plan_for(b.data, nxt)
    this = used(b.data, db, "lenteja", *(D(n) for n in range(7)))
    then = needs(b.data, db, plan, nxt).get("lenteja", 0)
    assert this and then
    pantry.correct(b.data, db, {"lenteja": this + then / 2}, at(0, 7))
    shop = shopping_list(b.data, db, plan, nxt, today=MONDAY)
    assert "lenteja" in {x.ingredient.id for s in shop.stores for x in s.lines}
    pantry.correct(b.data, db, {"lenteja": this + then + 10}, at(0, 7))
    shop = shopping_list(b.data, db, plan, nxt, today=MONDAY)
    assert "lenteja" not in {x.ingredient.id for s in shop.stores for x in s.lines}


# ---------------------------------------------------------------- what the AI sees


def test_the_facts_say_what_is_in_the_pantry_and_that_nothing_else_is(data_dir, tmp_path):
    b, db = setup(data_dir, tmp_path)
    facts = context.facts(b.data, db, MONDAY)
    assert "## Pantry" in facts and "Empty: nothing ticked" in facts
    data = keeps(data_dir, pollo=3)
    pantry.tick(data, db, MONDAY, "pollo", True, at(0, 9))
    db.set_have("aceite", True)
    facts = context.facts(data, db, MONDAY)
    assert "- Pollo [pollo]: " in facts and f"since {MONDAY}, good until {D(2)}" in facts
    assert "«I have it»): AOVE" in facts and "Nothing else is at home" in facts


def test_food_that_goes_off_before_any_dish_uses_it_is_a_problem_named_once(data_dir, tmp_path):
    _b, db = setup(data_dir, tmp_path)
    data = keeps(data_dir, espinaca=3)
    db.tick(MONDAY, "espinaca", True)  # bought, and then no dish kept it
    pantry.correct(data, db, {"espinaca": 200}, at(0))
    found = doctor.problems(data, db, MONDAY)
    (p,) = [p for p in found if p.kind == "going_off"]
    assert p.key == f"going_off:espinaca:{D(3)}" and f"good until wednesday {D(2)}" in p.text
    assert not [p for p in found if p.kind == "unused"]  # the same food is not said twice
    assert "Espinaca [espinaca] in the pantry" in context.facts(data, db, MONDAY)


def test_food_a_planned_dish_uses_up_in_time_is_not_a_problem(data_dir, tmp_path):
    b, db = setup(data_dir, tmp_path)
    day, _ = lentils_week(b.data, db)
    small = used(b.data, db, "lenteja", day) / 2
    data = keeps(data_dir, lenteja=(day - MONDAY).days + 2)
    pantry.correct(data, db, {"lenteja": small}, at(0, 7))
    assert not [p for p in doctor.problems(data, db, MONDAY) if p.kind == "going_off"]


# ---------------------------------------------------------------- the chat, the web, the phone


def test_the_chat_corrects_the_pantry_with_a_change_and_says_so(data_dir, tmp_path):
    b, db = setup(data_dir, tmp_path)
    pantry.tick(b.data, db, MONDAY, "pollo", True, at(0, 9))
    cfg(data_dir, text=change({"asked": True, "pantry": {"pollo": 0, "lenteja": 1000}}))
    run(b.handle(None, msg("@chefbot the chicken is finished, there is a kilo of lentils")))
    today = dt.datetime.now(ZONE).date()  # the chat's word is taken as of now
    assert qty(b.data, db, today, "pollo") == 0
    assert qty(b.data, db, today, "lenteja") == 1000
    (said,) = [p["text"] for p in sends(b, -100)]  # the answer and one line under it
    assert said.endswith("\n\n✓ Done: the pantry.\nhttps://meals.example/list")
    assert "Pollo" not in said  # what it touched, never the values


def test_a_pantry_change_with_an_unknown_food_or_a_bad_amount_is_refused(data):
    from supermealplanner import changes

    for bad in ({"nope": 1}, {"pollo": -1}, {"pollo": "a lot"}, {"pollo": True}, {}):
        with pytest.raises(changes.ChangeError):
            changes.parse({"pantry": bad}, data)


def test_the_phone_and_the_page_feed_and_show_the_same_pantry(app, data_dir):
    client = app
    h = auth(pair(client, data_dir)["token"])
    r = client.post(
        "/api/v1/list/tick", json={"ingredient": "pollo", "on": True, "d": str(MONDAY)}, headers=h
    )
    assert r.status_code == 200 and db_of(data_dir).lots()[0]["ingredient"] == "pollo"
    got = client.get("/api/v1/pantry", headers=h).json()
    assert [x["ingredient"] for x in got["items"]] == ["pollo"]
    assert got["items"][0]["unit"] == "g" and got["items"][0]["expires"] is None
    assert client.get("/api/v1/pantry").status_code == 401
    page = client.get(f"/list?d={MONDAY}").text
    assert "In the pantry" in page and "Pollo" in page.split("In the pantry")[1]
    bad = client.post("/api/v1/list/tick", json={"ingredient": "nope"}, headers=h)
    assert bad.status_code == 400


def test_a_tick_on_the_web_page_feeds_the_pantry(app, data_dir):
    client = app
    r = client.post(
        "/list/tick",
        data={"week": str(MONDAY), "item": "pollo", "on": "1"},
    )
    assert r.status_code == 200 and DB(load(data_dir).state / "supermealplanner.db").lots()
