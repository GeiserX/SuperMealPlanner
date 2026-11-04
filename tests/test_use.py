"""An ingredient swapped in planned meals (the `use` menu change): one day's meal eats another
ingredient than its recipe lists, or every meal of some days does, without touching the recipe.
Everything that reads a planned meal follows it (the plates, the list, the batch sheet, the thaws
and soaks, the consistency check, the reviewer), the validator refuses one that brings in a food
someone avoids, and undo, restore, set and swap treat it like the rest of the menu."""

import datetime as dt

import pytest
import yaml
from fastapi.testclient import TestClient

from supermealplanner import context, doctor, menu, verifier
from supermealplanner.db import DB
from supermealplanner.i18n import Translator
from supermealplanner.model import load
from supermealplanner.planner import day_menu, generate, load_plan, plan_for, plans_dir
from supermealplanner.portions import batch_sheet, needs, person_days, thaw_for

from .conftest import MONDAY


def D(n: int) -> dt.date:
    return MONDAY + dt.timedelta(days=n)


def bread(data_dir):
    """Every batch lunch and every salad dinner comes with bread, kept frozen; bulgur and a food
    one person avoids at dinner (a flag) are in the ingredient list too. A plan drawn on that."""
    raw = yaml.safe_load((data_dir / "ingredients.yaml").read_text())
    raw["pan"] = {"name": "Pan", "section": "panaderia", "unit": "g", "frozen": True}
    raw["bulgur"] = {"name": "Bulgur", "section": "despensa", "unit": "g", "flags": ["gluten"]}
    raw["cuscus"] = {"name": "Cuscús", "section": "despensa", "unit": "g", "flags": ["gluten"]}
    (data_dir / "ingredients.yaml").write_text(yaml.safe_dump(raw, allow_unicode=True))
    for path in (data_dir / "recipes").glob("*.yaml"):
        r = yaml.safe_load(path.read_text())
        if r["kind"] == "batch" or r["id"].startswith("ensalada-"):
            r["ingredients"].append({"id": "pan", "qty": 40})
            path.write_text(yaml.safe_dump(r, allow_unicode=True))
    data = load(data_dir)
    generate(data, MONDAY, 4).save(plans_dir(data) / f"{MONDAY}.yaml")
    return data


def change(data, *ops, today=MONDAY):
    menu.apply(data, menu.parse(list(ops), data), today)


def salad_dinner(data, day):
    change(data, {"set": {"date": str(day), "meal": "dinner", "recipe": "ensalada-0"}})


def use(day=None, meal=None, ingredient="bulgur", instead_of="pan", **extra):
    args = {"ingredient": ingredient, "instead_of": instead_of, **extra}
    if day is not None:
        args["date"] = str(day)
    if meal is not None:
        args["meal"] = meal
    return {"use": args}


def db(tmp_path):
    return DB(tmp_path / "t.db")


def plate_of(data, base, day, meal):
    plan = plan_for(data, day)
    return [pd.plates[meal].lines for pd in person_days(data, base, plan, day)]


# ---------------------------------------------------------------- one meal


def test_one_meal_eats_the_other_ingredient_and_every_reader_follows(data_dir, tmp_path):
    data = bread(data_dir)
    base = db(tmp_path)
    salad_dinner(data, D(2))
    recipe_file = (data_dir / "recipes" / "ensalada-0.yaml").read_text()
    week = needs(data, base, plan_for(data, MONDAY), MONDAY)
    change(data, use(D(2), "dinner", qty=30))
    # the plan holds it, the recipe does not
    assert (data_dir / "recipes" / "ensalada-0.yaml").read_text() == recipe_file
    raw = load_plan(plans_dir(data) / f"{MONDAY}.yaml")
    (swap,) = raw.day(D(2)).swaps
    assert (swap.meal, swap.out, swap.use, swap.qty) == ("dinner", "pan", "bulgur", 30)
    # plates: bulgur at 30 g a serving, no bread, at that dinner only
    for lines in plate_of(data, base, D(2), "dinner"):
        assert any(x.startswith("Bulgur") for x in lines) and not any("Pan" in x for x in lines)
    dinner = dict(day_menu(data, plan_for(data, D(2)).day(D(2))))["dinner"]
    assert [i.id for i in dinner.ingredients] == ["lechuga", "egg", "bulgur"]
    # the list: the bread of that dinner is gone and the bulgur is bought instead
    after = needs(data, base, plan_for(data, MONDAY), MONDAY)
    factor = sum(pd.factor for pd in person_days(data, base, plan_for(data, D(2)), D(2)))
    assert after["pan"] == pytest.approx(week["pan"] - 40 * factor)
    assert after["bulgur"] == pytest.approx(30 * factor)
    # the thaw the evening before says no bread for that dinner
    thaws = thaw_for(data, base, plan_for(data, D(2)), D(2))
    assert not [x for x in thaws if x.meal == "dinner"]
    # control: the day after keeps its bread
    salad_dinner(data, D(3))
    assert [x for x in thaw_for(data, base, plan_for(data, D(3)), D(3)) if x.meal == "dinner"]


def test_the_same_amount_by_default_and_a_unit_change_needs_one(data_dir):
    data = bread(data_dir)
    salad_dinner(data, D(2))
    change(data, use(D(2), "dinner"))
    dinner = dict(day_menu(data, plan_for(data, D(2)).day(D(2))))["dinner"]
    assert {i.id: i.qty for i in dinner.ingredients}["bulgur"] == 40
    with pytest.raises(menu.MenuError, match="different units: give qty"):
        menu.parse([use(D(2), "dinner", ingredient="egg", instead_of="lechuga")], data)
    change(data, use(D(2), "dinner", ingredient="egg", instead_of="lechuga", qty=1))
    dinner = dict(day_menu(data, plan_for(data, D(2)).day(D(2))))["dinner"]
    assert [(i.id, i.qty) for i in dinner.ingredients] == [("egg", 1), ("egg", 2), ("bulgur", 40)]


def test_a_range_swaps_every_meal_that_has_it_until_sunday(data_dir, tmp_path):
    data = bread(data_dir)
    base = db(tmp_path)
    plan = plan_for(data, MONDAY)
    # the bread this week's sessions cook for next week's lunches is still bought this week
    later = sum(
        40 * sum(pd.factor for pd in person_days(data, base, plan, e))
        for s in plan.sessions
        if MONDAY <= s.date <= D(6)
        for x in s.dishes
        for e in x.eat
        if e > D(6)
    )
    assert needs(data, base, plan, MONDAY)["pan"] > later  # control
    raw = {"from": str(D(0)), "to": str(D(6)), "ingredient": "bulgur", "instead_of": "pan"}
    change(data, {"use": raw})
    plan = plan_for(data, MONDAY)
    assert needs(data, base, plan, MONDAY).get("pan", 0) == pytest.approx(later)
    for i in range(7):
        for meal, r in day_menu(data, plan.day(D(i))):
            assert "pan" not in {x.id for x in r.ingredients}, (D(i), meal)
    # next week is untouched
    assert needs(data, base, plan_for(data, D(7)), D(7))["pan"] > 0


def test_nothing_to_swap_is_refused_and_nothing_is_written(data_dir):
    data = bread(data_dir)
    before = (plans_dir(data) / f"{MONDAY}.yaml").read_text()
    with pytest.raises(menu.MenuError, match="has no lenteja"):
        change(data, use(D(2), "breakfast", instead_of="lenteja"))
    assert (plans_dir(data) / f"{MONDAY}.yaml").read_text() == before


# ---------------------------------------------------------------- the batch sheet


def test_a_batch_lunch_swapped_on_one_of_its_days_splits_on_the_sheet(data_dir, tmp_path):
    data = bread(data_dir)
    base = db(tmp_path)
    plan = plan_for(data, MONDAY)
    s, x = next(
        (s, x) for s in plan.sessions for x in s.dishes if len(x.eat) == 2 and not s.morning
    )
    (whole,) = [b for b in batch_sheet(data, base, plan, s.date) if b.recipe.id == x.recipe]
    change(data, use(x.eat[1], "lunch"))
    (b,) = [
        b
        for b in batch_sheet(data, base, plan_for(data, s.date), s.date)
        if b.recipe.id == x.recipe
    ]
    second = sum(pd.factor for pd in person_days(data, base, plan_for(data, x.eat[1]), x.eat[1]))
    assert b.need["pan"] == pytest.approx(whole.need["pan"] - 40 * second)
    assert b.need["bulgur"] == pytest.approx(40 * second)
    assert any(line.startswith("Bulgur") for line in b.lines)


def test_a_dried_pulse_swapped_in_is_soaked_and_checked_like_one_in_the_recipe(data_dir, tmp_path):
    data = bread(data_dir)
    raw = yaml.safe_load((data_dir / "ingredients.yaml").read_text())
    raw["garbanzo"]["soak"] = True
    (data_dir / "ingredients.yaml").write_text(yaml.safe_dump(raw, allow_unicode=True))
    data = load(data_dir)
    base = db(tmp_path)
    salad_dinner(data, D(2))  # an assembled salad: nothing cooks a pulse for it
    key = "no_cook:ensalada-0:2026-10-14"
    assert key not in {p.key for p in doctor.problems(data, base, MONDAY)}  # control
    change(data, use(D(2), "dinner", ingredient="garbanzo"))
    assert key in {p.key for p in doctor.problems(data, base, MONDAY)}


# ---------------------------------------------------------------- hard rules


def test_a_swap_never_brings_in_a_food_someone_avoids(data_dir):
    data = bread(data_dir)
    salad_dinner(data, D(2))
    before = (plans_dir(data) / f"{MONDAY}.yaml").read_text()
    with pytest.raises(menu.MenuError, match="avoided ingredient"):
        change(data, use(D(2), "dinner", ingredient="espinaca"))
    assert (plans_dir(data) / f"{MONDAY}.yaml").read_text() == before


def test_a_swap_never_brings_a_flag_someone_avoids_at_dinner(data_dir):
    raw = yaml.safe_load((data_dir / "household.yaml").read_text())
    raw["people"][1]["avoid_flags_at_dinner"] = ["gluten"]
    (data_dir / "household.yaml").write_text(yaml.safe_dump(raw, allow_unicode=True))
    data = bread(data_dir)
    salad_dinner(data, D(2))
    with pytest.raises(menu.MenuError, match="bulgur is \\['gluten'\\], Luis avoids it"):
        change(data, use(D(2), "dinner"))
    change(data, use(D(2), "lunch"))  # control: at lunch it is allowed


# ---------------------------------------------------------------- the rest of the menu


def test_undo_restore_set_and_swap_treat_a_swap_like_the_rest_of_the_menu(data_dir):
    data = bread(data_dir)
    salad_dinner(data, D(2))
    salad_dinner(data, D(4))

    def swaps(day):
        return [(x.meal, x.out, x.use) for x in plan_for(data, day).day(day).swaps]

    change(data, use(D(2), "dinner"))
    change(data, {"undo": True})
    assert swaps(D(2)) == []
    change(data, use(D(2), "dinner"))
    # a later use of what the swap brought in is one swap of the bread
    change(data, use(D(2), "dinner", ingredient="cuscus", instead_of="bulgur"))
    assert swaps(D(2)) == [("dinner", "pan", "cuscus")]
    change(data, {"swap": {"a": str(D(2)), "b": str(D(4)), "meal": "dinner"}})
    assert swaps(D(2)) == [] and swaps(D(4)) == [("dinner", "pan", "cuscus")]
    change(data, {"restore": {"date": str(D(4)), "ingredient": "pan"}})
    assert swaps(D(4)) == []
    change(data, use(D(4), "dinner"))
    salad_dinner(data, D(4))  # another dish in that meal: its swaps were about the old one
    assert swaps(D(4)) == []


def test_the_plan_file_of_a_day_without_swaps_is_written_as_before(data_dir):
    data = bread(data_dir)
    salad_dinner(data, D(2))
    text = (plans_dir(data) / f"{MONDAY}.yaml").read_text()
    assert "swaps" not in text
    change(data, use(D(2), "dinner"))
    assert (plans_dir(data) / f"{MONDAY}.yaml").read_text().count("swaps:") == 1


def test_the_change_is_said_in_the_households_words(data_dir):
    data = bread(data_dir)
    t = Translator("es")
    (line,) = menu.describe(menu.parse([use(D(2), "dinner")], data), data, t)
    assert "Bulgur en lugar de Pan" in line and "cena" in line
    (line,) = menu.describe(menu.parse([use(D(2), "dinner", qty=30)], data), data, t)
    assert "Bulgur (30 g por ración) en lugar de Pan" in line
    raw = {"from": str(D(0)), "to": str(D(6)), "ingredient": "pan"}
    (line,) = menu.describe(menu.parse([{"restore": raw}], data), data, t)
    assert "otra vez Pan" in line and t("change_plan_every_meal") in line


# ---------------------------------------------------------------- what the AI and the reviewer see


def test_the_ai_and_the_plan_check_see_the_swap(data_dir, tmp_path):
    data = bread(data_dir)
    base = db(tmp_path)
    salad_dinner(data, D(2))
    _, key = verifier.plan_brief(data, base, MONDAY)
    change(data, use(D(2), "dinner"))
    lines = context.menu_now(data, Translator("en"), MONDAY)
    (wed,) = [x for x in lines if "(2026-10-14)" in x]
    assert "dinner Ensalada 0 [ensalada-0] (using Bulgur [bulgur] instead of Pan [pan])" in wed
    _, new = verifier.plan_brief(data, base, MONDAY)
    assert new != key  # a changed plan: the plan check judges it again


def test_the_day_and_recipe_pages_show_the_meal_as_that_day_serves_it(data_dir, monkeypatch):
    monkeypatch.setenv("SMP_SCHEDULER", "0")
    data = bread(data_dir)
    salad_dinner(data, D(2))
    change(data, use(D(2), "dinner"))
    from supermealplanner.web import create_app

    with TestClient(create_app(data_dir)) as c:
        page = c.get(f"/recipe/ensalada-0?d={D(2)}").text
        assert "Bulgur" in page and "Pan:" not in page
        assert "Pan" in c.get(f"/recipe/ensalada-0?d={D(3)}").text  # control: another day
        assert "Bulgur" in c.get(f"/?d={D(2)}").text


def test_a_bad_swap_never_breaks_a_page_or_a_change(data_dir):
    data = bread(data_dir)
    salad_dinner(data, D(2))
    change(data, use(D(2), "dinner"))
    path = plans_dir(data) / f"{MONDAY}.yaml"
    raw = yaml.safe_load(path.read_text())
    (day,) = [d for d in raw["days"] if d["date"] == str(D(2))]
    day["swaps"] = [
        {"meal": "dinner", "out": "pan", "use": "bulgur", "qty": "plenty"},  # edited by hand
        {"meal": "dinner", "out": "lechuga", "use": "gone-food", "qty": None},  # left the data
    ]
    path.write_text(yaml.safe_dump(raw, allow_unicode=True))
    dinner = dict(day_menu(data, plan_for(data, D(2)).day(D(2))))["dinner"]
    assert [i.id for i in dinner.ingredients] == ["lechuga", "egg", "pan"]  # as written
    lines = context.menu_now(data, Translator("en"), MONDAY)
    assert "gone-food" not in "\n".join(lines)
    # a new ingredient given as something other than its fields: no crash here, the change's
    # own ingredients check says why it is refused
    ops = [use(D(2), "dinner", ingredient="quinoa", instead_of="pan")]
    assert menu.parse(ops, data, None, {"quinoa": 42})[0].kind == "use"
