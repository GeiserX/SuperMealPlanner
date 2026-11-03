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
from supermealplanner.db import DB, FROZEN_DAYS, PANTRY_DAYS
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
    assert said.endswith(
        '\n\n✓ Done: the pantry.\n<a href="https://meals.example/list">Shopping list</a>'
    )
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


# ---------------------------------------------------------------- «I have it» on fresh food


def test_i_have_it_on_fresh_food_holds_only_while_it_keeps(data_dir, tmp_path):
    """Fresh meat marked «I have it» once was eaten days later and never came back on a list:
    the cook found no meat for a dinner the plan counted on. A fresh mark lasts keeps_days."""
    _b, db = setup(data_dir, tmp_path)
    data = keeps(data_dir, pollo=2)
    db.set_have("pollo", True, D(0))
    db.set_have("aceite", True, D(0))
    assert pantry.at_home(data, db, D(1)) == {"pollo", "aceite"}
    assert pantry.at_home(data, db, D(2)) == {"aceite"}  # the oil keeps; the chicken is gone
    assert pantry.at_home(data, db, D(30)) == {"aceite"}
    db.set_have("pollo", True, D(30))  # marked again: off the list again
    assert "pollo" in pantry.at_home(data, db, D(31))


def test_a_fresh_mark_from_before_the_day_was_kept_no_longer_holds(data_dir, tmp_path):
    _b, db = setup(data_dir, tmp_path)
    data = keeps(data_dir, pollo=2)
    db.set_kv("shop.have", ["pollo", "aceite"])  # how it was stored before
    assert pantry.at_home(data, db, D(0)) == {"aceite"}


def test_the_list_asks_again_for_fresh_food_marked_long_ago(data_dir, tmp_path):
    _b, db = setup(data_dir, tmp_path)
    data = keeps(data_dir, pollo=2)
    plan = plan_for(data, MONDAY)
    assert needs(data, db, plan, MONDAY).get("pollo"), "the fixture week must use chicken"
    db.set_have("pollo", True, D(-10))
    shop = shopping_list(data, db, plan, MONDAY, today=MONDAY)
    lines = {line.ingredient.id for sl in shop.stores for line in sl.lines}
    assert "pollo" in lines and "pollo" not in {i.id for i in shop.have}


# ---------------------------------------------------------------- a label's date, the freezer


def chicken_days(data, db):
    """The days of the plan's first week that cook chicken."""
    return [D(n) for n in range(7) if used(data, db, "pollo", D(n))]


def test_a_label_date_keeps_fresh_food_until_that_day(data_dir, tmp_path):
    """Tray meat bought on Monday carries a label a week later; by keeps_days alone the app had it
    gone by Wednesday. A use-by said in the chat is the last good day."""
    _b, db = setup(data_dir, tmp_path)
    data = keeps(data_dir, espinaca=2)
    pantry.correct(data, db, {"espinaca": [pantry.Part(300, use_by=D(5))]}, at(0))
    assert qty(data, db, D(5), "espinaca") == 300
    assert next(x for x in pantry.now(data, db, D(5))).expires == D(6)
    assert qty(data, db, D(6), "espinaca") == 0


def test_frozen_food_keeps_for_months_and_a_thaw_starts_its_days_again(data_dir, tmp_path):
    _b, db = setup(data_dir, tmp_path)
    data = keeps(data_dir, espinaca=3)
    pantry.correct(data, db, {"espinaca": [pantry.Part(300, frozen=True)]}, at(0))
    (x,) = pantry.now(data, db, D(60))
    assert x.qty == 300 and x.frozen == 300 and x.expires == D(FROZEN_DAYS)
    assert qty(data, db, D(FROZEN_DAYS), "espinaca") == 0  # the cap
    pantry.correct(data, db, {"espinaca": 300}, at(60))  # out of the freezer to thaw
    (x,) = pantry.now(data, db, D(61))
    assert x.frozen == 0 and x.expires == D(63)


def test_the_cooking_takes_what_goes_off_first_and_the_freezer_waits(data_dir, tmp_path):
    b, db = setup(data_dir, tmp_path)
    day, _ = lentils_week(b.data, db)
    gone = used(b.data, db, "lenteja", day)
    assert gone < 1000
    parts = [pantry.Part(1000, frozen=True), pantry.Part(1000)]
    pantry.correct(b.data, db, {"lenteja": parts}, at(0, 7))
    (x,) = [
        x for x in pantry.now(b.data, db, day + dt.timedelta(days=1)) if x.ingredient == "lenteja"
    ]
    assert math.isclose(x.qty, 2000 - gone) and x.frozen == 1000  # both parts, set together
    assert x.frozen_amount


def test_a_frozen_or_labelled_entry_outlives_the_pantrys_forgetting(data_dir, tmp_path):
    _b, db = setup(data_dir, tmp_path)
    db.add_lot("espinaca", 100, at(0), "set")
    db.add_lot("salmon", 100, at(0), "set", frozen=True)
    db.add_lot("merluza", 100, at(0), "set", use_by=D(PANTRY_DAYS + 30))
    db.add_lot("lenteja", 100, at(PANTRY_DAYS + 1), "set")  # the add that forgets the old ones
    assert {r["ingredient"] for r in db.lots()} == {"salmon", "merluza", "lenteja"}
    db.add_lot("lenteja", 100, at(FROZEN_DAYS + 1), "set")
    assert {r["ingredient"] for r in db.lots()} == {"lenteja"}


def forget(db):
    """The add, long after, that makes the pantry forget its old entries."""
    db.add_lot("merluza", 100, at(PANTRY_DAYS + 10), "set")


@pytest.mark.parametrize(
    "first",
    [pantry.Part(300, frozen=True), pantry.Part(300, use_by=D(PANTRY_DAYS + 60))],
    ids=["frozen", "labelled"],
)
@pytest.mark.parametrize("then", [0, 300], ids=["ran out", "thawed"])
def test_forgetting_never_brings_back_what_a_later_amount_replaced(data_dir, tmp_path, first, then):
    """A frozen or labelled entry outlived the forgetting, but the later amount that replaced it
    did not, so food that ran out or was thawed long ago came back in the pantry."""
    _b, db = setup(data_dir, tmp_path)
    data = keeps(data_dir, espinaca=3)
    pantry.correct(data, db, {"espinaca": [first]}, at(0))
    pantry.correct(data, db, {"espinaca": then}, at(5))
    day = D(PANTRY_DAYS + 11)
    assert qty(data, db, day, "espinaca") == 0
    forget(db)
    assert qty(data, db, day, "espinaca") == 0


def test_forgetting_keeps_the_ticks_beside_a_kept_frozen_entry(data_dir, tmp_path):
    """The forgetting dropped a tick beside a frozen entry it kept, so the replayed cooking took
    the tick's food out of the freezer instead."""
    b, db = setup(data_dir, tmp_path)
    day, _ = lentils_week(b.data, db)
    gone = used(b.data, db, "lenteja", day)
    assert 0 < gone < 2000
    pantry.correct(b.data, db, {"lenteja": [pantry.Part(5000, frozen=True)]}, at(0, 7))
    db.add_lot("lenteja", 2000, at(0, 8), "tick", week=MONDAY)
    later = D(PANTRY_DAYS + 11)
    before = next(x for x in pantry.now(b.data, db, later) if x.ingredient == "lenteja")
    forget(db)
    after = next(x for x in pantry.now(b.data, db, later) if x.ingredient == "lenteja")
    assert (after.qty, after.frozen) == (before.qty, before.frozen)
    assert len([r for r in db.lots() if r["ingredient"] == "lenteja"]) == 2


def test_the_list_does_not_ask_again_for_meat_its_label_or_the_freezer_keeps(data_dir, tmp_path):
    """From Wednesday the list asked again for Saturday's meat, gone by keeps_days; blanched
    frozen food came back on the list three days after it went into the freezer."""
    _b, db = setup(data_dir, tmp_path)
    data = keeps(data_dir, pollo=2)
    plan = plan_for(data, MONDAY)
    days = chicken_days(data, db)
    assert days[-1] >= D(3), "the fixture week must cook chicken late in the week"
    week = used(data, db, "pollo", *days) + 50

    def asked(today):
        shop = shopping_list(data, db, plan, MONDAY, today=today)
        return "pollo" in {x.ingredient.id for s in shop.stores for x in s.lines}

    pantry.correct(data, db, {"pollo": week}, at(0, 7))
    assert asked(D(0))  # keeps 2 days: gone before the last chicken day
    pantry.correct(data, db, {"pollo": [pantry.Part(week, use_by=days[-1])]}, at(0, 8))
    assert not asked(D(0)) and not asked(D(2))
    pantry.correct(data, db, {"pollo": [pantry.Part(week, frozen=True)]}, at(0, 9))
    assert not asked(D(0)) and not asked(D(2))


def test_the_day_message_does_not_call_labelled_meat_missing(data_dir, tmp_path):
    _b, db = setup(data_dir, tmp_path)
    data = keeps(data_dir, pollo=2)
    days = [d for d in chicken_days(data, db) if d > D(2)]
    assert days, "the fixture week must cook chicken after Wednesday"
    day = days[0]
    plan = plan_for(data, day)
    week = used(data, db, "pollo", *chicken_days(data, db)) + 50
    pantry.correct(data, db, {"pollo": week}, at(0, 7))
    assert "pollo" in {i.id for i, _ in pantry.unbought(data, db, plan, day)}
    pantry.correct(data, db, {"pollo": [pantry.Part(week, use_by=day)]}, at(0, 8))
    assert "pollo" not in {i.id for i, _ in pantry.unbought(data, db, plan, day)}


def test_a_pantry_change_takes_a_use_by_frozen_and_parts(data):
    from supermealplanner import changes

    p = changes.parse(
        {
            "pantry": {
                "pollo": {"qty": 500, "use_by": "2026-01-17"},
                "lenteja": [{"qty": 400, "frozen": True}, {"qty": 300}],
                "aceite": 0,
            }
        },
        data,
    )
    assert p.pantry == {
        "pollo": [pantry.Part(500, dt.date(2026, 1, 17))],
        "lenteja": [pantry.Part(400, frozen=True), pantry.Part(300)],
        "aceite": [pantry.Part(0)],
    }
    for bad in (
        {"pollo": {"qty": 5, "use_by": "soon"}},
        {"pollo": {"qty": 5, "frozen": "yes"}},
        {"pollo": {"qty": 5, "colour": "red"}},
        {"pollo": {"use_by": "2026-01-17"}},
        {"pollo": []},
    ):
        with pytest.raises(changes.ChangeError):
            changes.parse({"pantry": bad}, data)


def test_the_chat_says_it_froze_the_food_and_the_pantry_keeps_it(data_dir, tmp_path):
    b, db = setup(data_dir, tmp_path)
    said = {"asked": True, "pantry": {"pollo": {"qty": 500, "frozen": True}}}
    cfg(data_dir, text=change(said))
    run(b.handle(None, msg("@chefbot I froze the chicken")))
    (lot,) = db.lots()
    assert lot["frozen"] == 1 and lot["use_by"] is None and lot["qty"] == 500
    from supermealplanner import changes
    from supermealplanner.i18n import Translator

    p = changes.parse({"pantry": {"pollo": {"qty": 500, "use_by": "2026-01-17"}}}, b.data)
    (line,) = changes.describe(p, b.data, Translator("en"))
    assert line.startswith("• In the pantry: Pollo, 500 g")
    assert line.endswith(", good until " + Translator("en").date(dt.date(2026, 1, 17)))
    p = changes.parse({"pantry": {"pollo": {"qty": 500, "frozen": True}}}, b.data)
    assert changes.describe(p, b.data, Translator("es"))[0].endswith(" en el congelador")


def test_the_ai_is_told_when_to_give_a_use_by_or_frozen_and_sees_the_freezer(data_dir, tmp_path):
    b, db = setup(data_dir, tmp_path)
    assert "`use_by`" in context.CHANGES and "frozen: true" in context.CHANGES
    assert "lo he congelado" in context.CHANGES
    pantry.correct(b.data, db, {"lenteja": [pantry.Part(400, frozen=True)]}, at(0, 7))
    assert "of it in the freezer" in context.facts(b.data, db, MONDAY)


def test_the_page_and_the_phone_show_what_is_in_the_freezer(app, data_dir):
    client = app
    h = auth(pair(client, data_dir)["token"])
    data = load(data_dir)
    pantry.correct(data, db_of(data_dir), {"lenteja": [pantry.Part(400, frozen=True)]})
    got = client.get("/api/v1/pantry", headers=h).json()
    (x,) = got["items"]
    assert x["frozen"] == 400 and x["expires"]
    assert "in the freezer" in client.get(f"/list?d={MONDAY}").text.split("In the pantry")[1]


def test_a_second_statement_in_the_same_second_replaces_the_first(data_dir, tmp_path):
    data = load(data_dir)
    db = DB(tmp_path / "s.db")
    iid = next(iter(data.ingredients))
    at = dt.datetime(2026, 1, 14, 20, 0, tzinfo=dt.UTC)
    pantry.correct(data, db, {iid: 500}, at)
    pantry.correct(data, db, {iid: 0}, at)  # it ran out, said within the same second
    assert iid not in {x.ingredient for x in pantry.now(data, db, at.date())}
