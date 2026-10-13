import datetime as dt

import pytest

from supermealplanner.model import DataError, load
from supermealplanner.planner import Plan, generate, validate

from .conftest import MONDAY


def test_load_counts(data):
    assert len(data.household.people) == 2
    assert "legumbre-0" in data.recipes


def test_unknown_ingredient_fails(data_dir):
    p = data_dir / "recipes" / "legumbre-0.yaml"
    p.write_text(p.read_text().replace("garbanzo", "no-existe"))
    with pytest.raises(DataError, match="unknown ingredient no-existe"):
        load(data_dir)


def test_plan_is_valid_and_complete(data):
    plan = generate(data, MONDAY, 4)
    assert validate(data, plan) == []
    assert len(plan.days) == 28
    assert all(d.lunch and d.dinner and d.breakfast and d.snack for d in plan.days)


def test_plan_is_deterministic(data):
    a = generate(data, MONDAY, 4).to_dict()
    b = generate(data, MONDAY, 4).to_dict()
    assert a == b


def test_plan_roundtrips_yaml(data, tmp_path):
    from supermealplanner.planner import load_plan

    plan = generate(data, MONDAY, 4)
    plan.save(tmp_path / "p.yaml")
    assert load_plan(tmp_path / "p.yaml").to_dict() == plan.to_dict()


def test_avoided_ingredients_and_dinner_tags(data):
    plan = generate(data, MONDAY, 4)
    used = {getattr(d, m) for d in plan.days for m in ("lunch", "dinner", "side")}
    assert "espinacas-con-garbanzos" not in used
    assert not {d.dinner for d in plan.days} & {"gazpacho", "fritura"}


def _add_recipe(data_dir, r):
    import yaml

    (data_dir / "recipes" / f"{r['id']}.yaml").write_text(yaml.safe_dump(r, allow_unicode=True))


def test_breakfast_and_snack_rotation_skips_avoided_ingredients(data_dir, caplog):
    from .conftest import recipe

    spinach = [{"id": "espinaca", "qty": 50}, {"id": "egg", "qty": 2}]
    _add_recipe(data_dir, recipe("desayuno-verde", ["breakfast"], "fixed", "egg", [], spinach))
    _add_recipe(data_dir, recipe("merienda-verde", ["snack"], "fixed", "egg", [], spinach))
    data = load(data_dir)
    with caplog.at_level("WARNING", logger="supermealplanner.planner"):
        plan = generate(data, MONDAY, 4)
    assert validate(data, plan) == []
    assert "desayuno-verde" not in {d.breakfast for d in plan.days}
    assert "merienda-verde" not in {d.snack for d in plan.days}
    assert all(d.breakfast and d.snack for d in plan.days)
    assert "fixed breakfast desayuno-verde uses avoided espinaca" in caplog.text
    assert "fixed snack merienda-verde uses avoided espinaca" in caplog.text


def test_only_fixed_snack_with_avoided_ingredient_still_plans(data_dir, caplog):
    p = data_dir / "recipes" / "merienda-a.yaml"
    p.write_text(p.read_text().replace("id: yogur", "id: espinaca"))
    data = load(data_dir)
    with caplog.at_level("WARNING", logger="supermealplanner.planner"):
        plan = generate(data, MONDAY, 4)
    assert validate(data, plan) == []
    assert "merienda-a" not in {d.snack for d in plan.days}
    assert {d.snack for d in plan.days if d.date.weekday() == 5} == {"treat"}
    assert "fixed snack merienda-a uses avoided espinaca" in caplog.text


def test_weekly_minimums(data):
    plan = generate(data, MONDAY, 4)
    for w in range(4):
        week = plan.days[7 * w : 7 * w + 7]
        legumes = sum(
            data.recipes[d.lunch].has("legume") + data.recipes[d.dinner].has("legume") for d in week
        )
        oily = sum(
            data.recipes[d.lunch].has("oily-fish") + data.recipes[d.dinner].has("oily-fish")
            for d in week
        )
        assert legumes >= 4 and oily >= 2


def test_salad_every_day(data):
    plan = generate(data, MONDAY, 4)
    for d in plan.days:
        assert d.side or data.recipes[d.lunch].has("salad") or data.recipes[d.dinner].has("salad")


def test_freezer_only_for_freezable(data):
    plan = generate(data, MONDAY, 4)
    for d in plan.days:
        r = data.recipes[d.lunch]
        if (d.date - d.lunch_cook).days > r.fridge_days:
            assert r.freezes, d


def test_lunch_never_before_cooking(data):
    plan = generate(data, MONDAY, 4)
    assert all(d.lunch_cook <= d.date for d in plan.days)


def test_busy_afternoon_moves_the_session(data):
    busy = {MONDAY: ["Reunión"], MONDAY + dt.timedelta(days=4): ["Cena fuera"]}
    plan = generate(data, MONDAY, 1, busy=busy)
    dates = {s.date for s in plan.sessions if not s.morning}
    assert MONDAY not in dates and MONDAY + dt.timedelta(days=4) not in dates
    assert MONDAY + dt.timedelta(days=1) in dates  # Tuesday instead of Monday
    assert MONDAY + dt.timedelta(days=5) in dates  # Saturday instead of Friday


def test_validator_catches_a_broken_plan(data):
    """Positive control: the validator must be able to fail."""
    plan = generate(data, MONDAY, 4)
    raw = plan.to_dict()
    raw["days"][2]["dinner"] = "gazpacho"
    raw["days"][3]["lunch"] = "espinacas-con-garbanzos"
    for d in raw["days"][7:14]:
        if data.recipes[d["lunch"]].has("legume"):
            d["lunch"] = "pollo-1"
        if data.recipes[d["dinner"]].has("legume"):
            d["dinner"] = "ensalada-0"
    errs = validate(data, Plan.from_dict(raw))
    assert any("avoids" in e and "cold-soup" in e for e in errs)
    assert any("avoided ingredient" in e for e in errs)
    assert any("week 2: legumes" in e for e in errs)


def test_side_salads_never_main(data):
    plan = generate(data, MONDAY, 4)
    for d in plan.days:
        assert not data.recipes[d.dinner].has("side")
        assert not data.recipes[d.lunch].has("side")


def test_rejects_non_monday(data):
    with pytest.raises(ValueError):
        generate(data, MONDAY + dt.timedelta(days=1))
