"""A person's meal_factors size one meal only: a smaller dinner is a smaller day, and nothing it
loses moves to lunch or any other meal."""

import pytest
import yaml
from fastapi.testclient import TestClient

from supermealplanner.db import DB
from supermealplanner.model import DataError, check, load
from supermealplanner.planner import MEALS, generate, plans_dir
from supermealplanner.portions import batch_sheet, person_days, servings_by_eater, thaw_for

from .conftest import MONDAY

DINNER = 0.8


def _set(data_dir, person: str, factors: dict) -> None:
    path = data_dir / "household.yaml"
    hh = yaml.safe_load(path.read_text())
    next(p for p in hh["people"] if p["id"] == person)["meal_factors"] = factors
    path.write_text(yaml.safe_dump(hh, allow_unicode=True))


def _dinner_day(plan):
    """A day whose dinner recipe no other meal of that day serves, so its pages size it as dinner."""
    return next(
        d for d in plan.days if d.dinner and all(getattr(d, m) != d.dinner for m in MEALS[:-1])
    )


def test_a_smaller_dinner_shrinks_that_plate_only(data_dir, tmp_path):
    before = load(data_dir)
    plan = generate(before, MONDAY, 4)
    _set(data_dir, "ana", {"dinner": DINNER})
    after = load(data_dir)
    db = DB(tmp_path / "t.db")
    day = _dinner_day(plan).date
    old = {p.person_id: p for p in person_days(before, db, plan, day)}
    new = {p.person_id: p for p in person_days(after, db, plan, day)}
    a, b = old["ana"], new["ana"]
    assert b.plates["dinner"].factor == pytest.approx(a.plates["dinner"].factor * DINNER)
    assert b.plates["dinner"].kcal == pytest.approx(a.plates["dinner"].kcal * DINNER)
    for meal in a.plates:
        if meal != "dinner":
            assert b.plates[meal].factor == a.plates[meal].factor, meal  # nothing moves here
    assert b.factor == a.factor and b.kcal_target == a.kcal_target  # the target stays
    assert b.kcal < a.kcal  # the day simply has less food
    assert new["luis"].plates["dinner"].factor == old["luis"].plates["dinner"].factor


def test_the_shopping_list_buys_each_meal_at_its_own_size(data_dir, tmp_path):
    before = load(data_dir)
    plan = generate(before, MONDAY, 4)
    _set(data_dir, "ana", {"dinner": DINNER})
    _set(data_dir, "luis", {"lunch": 1.2})
    after = load(data_dir)
    db = DB(tmp_path / "t.db")
    old = servings_by_eater(before, db, plan, MONDAY)
    new = servings_by_eater(after, db, plan, MONDAY)
    week = plan.days[:7]
    dinners = {d.dinner for d in week} - {getattr(d, m) for d in week for m in MEALS[:-1]}
    lunches = {d.lunch for d in week} - {getattr(d, m) for d in week for m in MEALS if m != "lunch"}
    assert dinners and lunches
    for rid in dinners:
        assert new[rid]["ana"] == pytest.approx(old[rid]["ana"] * DINNER), rid
        assert new[rid]["luis"] == pytest.approx(old[rid]["luis"]), rid
    for rid in lunches:  # batch lunches, bought the week they are cooked
        assert new[rid]["ana"] == pytest.approx(old[rid]["ana"]), rid
        assert new[rid]["luis"] == pytest.approx(old[rid]["luis"] * 1.2), rid


def test_the_batch_sheet_cooks_a_bigger_lunch(data_dir, tmp_path):
    before = load(data_dir)
    plan = generate(before, MONDAY, 4)
    _set(data_dir, "ana", {"lunch": 1.2})
    after = load(data_dir)
    db = DB(tmp_path / "t.db")
    s = next(s for s in plan.sessions if s.dishes)
    old = batch_sheet(before, db, plan, s.date)[0]
    new = batch_sheet(after, db, plan, s.date)[0]
    assert new.eaters["ana"] == pytest.approx(old.eaters["ana"] * 1.2)
    assert new.eaters["luis"] == pytest.approx(old.eaters["luis"])


def _client(data_dir, monkeypatch):
    monkeypatch.setenv("SMP_SCHEDULER", "0")
    monkeypatch.setenv("SMP_BOT", "0")
    from supermealplanner.web import create_app

    return TestClient(create_app(data_dir))


def test_a_recipe_served_at_dinner_is_plated_for_dinner(data_dir, monkeypatch):
    from supermealplanner import devices as dv

    data = load(data_dir)
    plan = generate(data, MONDAY, 4)
    plan.save(plans_dir(data) / f"{MONDAY}.yaml")
    d = _dinner_day(plan)
    _set(data_dir, "ana", {"dinner": DINNER})
    day_f = 1800 / 2000  # ana: no training, no GLP-1
    with _client(data_dir, monkeypatch) as c:
        code, _ = dv.make_code(DB(load(data_dir).state / "supermealplanner.db"), "ana")
        token = c.post("/api/v1/pair", json={"code": code, "device": "Teléfono"}).json()["token"]
        h = {"Authorization": f"Bearer {token}"}
        r = c.get(f"/api/v1/recipe/{d.dinner}?d={d.date}", headers=h).json()
        assert r["plate"]["factor"] == pytest.approx(day_f * DINNER)
        mine = next(p for p in r["plates"] if p["person"] == "ana")
        assert mine["factor"] == pytest.approx(day_f * DINNER)
        lunch = c.get(f"/api/v1/recipe/{d.lunch}?d={d.date}", headers=h).json()
        assert lunch["plate"]["factor"] == pytest.approx(day_f)  # lunch keeps the day's factor
        day = c.get(f"/api/v1/day?d={d.date}", headers=h).json()
        by_meal = {m["meal"]: m["dishes"][0] for m in day["meals"]}
        ana = next(p for p in by_meal["dinner"]["plates"] if p["person"] == "ana")
        assert ana["factor"] == pytest.approx(day_f * DINNER)
        luis = next(p for p in by_meal["dinner"]["plates"] if p["person"] == "luis")
        assert by_meal["dinner"]["total"]["factor"] == pytest.approx(
            day_f * DINNER + luis["factor"], abs=1e-3
        )
        page = c.get(f"/recipe/{d.dinner}?d={d.date}").text
        assert f"×{day_f * DINNER:.2f}" in page and f"×{day_f:.2f}" not in page
        today = c.get(f"/?d={d.date}").text
        assert f"×{day_f * DINNER:.2f}" in today  # ana's dinner plate
        assert f"×{day_f * DINNER + luis['factor']:.2f}" in today  # the dinner for everyone


@pytest.mark.parametrize(
    "factors, says",
    [
        ({"dinner": 0.4}, "meal_factors.dinner"),
        ({"dinner": 1.6}, "meal_factors.dinner"),
        ({"nibble": 0.8}, "unknown meals"),
        ({"supper": 0.8}, "unknown meals"),
    ],
)
def test_smp_check_refuses_a_bad_meal_factor(data_dir, factors, says):
    _set(data_dir, "ana", factors)
    with pytest.raises(DataError, match=says):
        check(data_dir, health=False)


def test_the_edges_are_allowed(data_dir):
    _set(data_dir, "ana", {"breakfast": 0.5, "dinner": 1.5})
    assert check(data_dir, health=False).startswith("ok")


def test_the_thaw_task_takes_out_the_smaller_dinner(data_dir, tmp_path):
    path = data_dir / "ingredients.yaml"
    ings = yaml.safe_load(path.read_text())
    ings["merluza"]["frozen"] = True
    path.write_text(yaml.safe_dump(ings, allow_unicode=True))
    before = load(data_dir)
    plan = generate(before, MONDAY, 4)
    _set(data_dir, "ana", {"dinner": DINNER})
    after = load(data_dir)
    db = DB(tmp_path / "t.db")
    d = next(d for d in plan.days if d.dinner.startswith("merluza-cena"))

    def dinner(data):
        return next(x.qty for x in thaw_for(data, db, plan, d.date) if x.meal == "dinner")

    assert dinner(after) == pytest.approx(dinner(before) - 150 * 1800 / 2000 * (1 - DINNER))
