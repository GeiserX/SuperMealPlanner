"""Per-person swaps, training days and days with no batch session."""

import datetime as dt

import pytest
import yaml

from supermealplanner import changes, context
from supermealplanner.db import DB
from supermealplanner.i18n import Translator
from supermealplanner.model import DataError, load
from supermealplanner.planner import generate
from supermealplanner.portions import batch_sheet, needs, person_days, shopping_list

from .conftest import MONDAY


def edit(data_dir, household=None, ingredients=None):
    """Change household.yaml and ingredients.yaml in place, then load the data again."""
    for name, fn in (("household.yaml", household), ("ingredients.yaml", ingredients)):
        if fn:
            path = data_dir / name
            raw = yaml.safe_load(path.read_text())
            fn(raw)
            path.write_text(yaml.safe_dump(raw, allow_unicode=True))
    return load(data_dir)


def person(raw, pid):
    return next(p for p in raw["people"] if p["id"] == pid)


def add_swap_ingredients(ing):
    ing["kefir"] = {"name": "Kéfir", "section": "lacteos-huevos", "unit": "g"}
    ing["pavo"] = {"name": "Pavo", "section": "carniceria", "store": "mercado", "unit": "g"}


# ---------------------------------------------------------------- batch.avoid_days


def sessions(plan):
    return {s.date for s in plan.sessions if not s.morning}


def test_no_batch_session_on_an_avoided_day_even_when_others_are_busy(data_dir):
    tuesday = MONDAY + dt.timedelta(days=1)

    def avoid(hh):
        hh["batch"] = {"preferred_days": ["monday", "tuesday", "friday"], "avoid_days": ["tuesday"]}

    data = edit(data_dir, avoid)
    busy = {MONDAY + dt.timedelta(days=7 * w): ["Reunión"] for w in range(4)}
    plan = generate(data, MONDAY, 4, busy=busy)
    assert all(d.weekday() != 1 for d in sessions(plan))
    assert MONDAY + dt.timedelta(days=2) in sessions(plan)  # Wednesday, the next allowed day
    # every allowed afternoon of the first half busy: the session stays on a busy allowed day
    busy = {MONDAY: ["Reunión"], MONDAY + dt.timedelta(days=2): ["Cena fuera"]}
    plan = generate(data, MONDAY, 1, busy=busy)
    assert tuesday not in sessions(plan)
    assert MONDAY in {s.date for s in plan.sessions if not s.morning and s.busy}


def test_a_week_whose_first_half_is_avoided_still_cooks(data_dir):
    def avoid(hh):
        hh["batch"] = {"sessions_per_week": 1, "avoid_days": ["monday", "tuesday", "wednesday"]}

    data = edit(data_dir, avoid)
    plan = generate(data, MONDAY, 4)
    assert sessions(plan) and all(d.weekday() >= 3 for d in sessions(plan))


def test_avoid_days_cannot_take_the_whole_week(data_dir):
    def avoid(hh):
        hh["batch"] = {
            "avoid_days": [
                "monday",
                "tuesday",
                "wednesday",
                "thursday",
                "friday",
                "saturday",
                "sunday",
            ]
        }

    with pytest.raises(DataError, match="no day to cook on"):
        edit(data_dir, avoid)


# ---------------------------------------------------------------- swaps


def swapping(data_dir):
    def swap(hh):
        person(hh, "ana")["swaps"] = {"yogur": "kefir", "pollo": "pavo"}

    return edit(data_dir, swap, add_swap_ingredients)


def test_a_swap_changes_only_that_persons_plate(data_dir, tmp_path):
    data = swapping(data_dir)
    db = DB(tmp_path / "x.db")
    plan = generate(data, MONDAY, 4)
    pds = {pd.person_id: pd for pd in person_days(data, db, plan, MONDAY)}
    assert plan.day(MONDAY).snack == "merienda-a"  # the weekday snack: yogurt
    ana, luis = (" ".join(pds[x].plates["snack"].lines) for x in ("ana", "luis"))
    assert "Kéfir" in ana and "Yogur" not in ana
    assert "Yogur" in luis and "Kéfir" not in luis
    assert data.recipes["merienda-a"].ingredients[0].id == "yogur"  # the recipe stays


def test_the_shopping_list_buys_a_swap_for_one_person_only(data_dir, tmp_path):
    db = DB(tmp_path / "x.db")
    before = load(data_dir)
    plan = generate(before, MONDAY, 4)
    old = needs(before, db, plan, MONDAY)
    data = swapping(data_dir)
    new = needs(data, db, plan, MONDAY)
    assert new["kefir"] > 0 and new["yogur"] > 0  # Ana's share and Luis's share
    assert new["kefir"] + new["yogur"] == pytest.approx(old["yogur"])
    ana = sum(f for d in range(7) for f in [_factor(data, db, "ana", d)])
    luis = sum(f for d in range(7) for f in [_factor(data, db, "luis", d)])
    assert new["kefir"] / new["yogur"] == pytest.approx(ana / luis, rel=0.15)
    names = {
        line.ingredient.id for s in shopping_list(data, db, plan, MONDAY).stores for line in s.lines
    }
    assert {"kefir", "yogur"} <= names


def _factor(data, db, pid, offset):
    from supermealplanner.portions import factors

    return factors(data, db, MONDAY + dt.timedelta(days=offset))[pid]


def test_the_batch_sheet_splits_the_pot_by_swap(data_dir, tmp_path):
    data = swapping(data_dir)
    db = DB(tmp_path / "x.db")
    plan = generate(data, MONDAY, 4)
    hit = None
    for s in plan.sessions:
        for dish in batch_sheet(data, db, plan, s.date):
            if any(i.id == "pollo" for i in dish.recipe.ingredients):
                hit = dish
    assert hit is not None
    text = " ".join(hit.lines)
    assert "Pollo" in text and "Pavo" in text


@pytest.mark.parametrize(
    "swaps, error",
    [
        ({"yogur": "no-existe"}, "unknown ingredient no-existe"),
        ({"yogur": "yogur"}, "for itself"),
        ({"yogur": "aceite"}, "both need one unit"),
        ({"tomate": "espinaca"}, "foods luis avoids"),
    ],
)
def test_bad_swaps_are_refused_at_load(data_dir, swaps, error):
    def swap(hh):
        person(hh, "luis")["swaps"] = swaps

    with pytest.raises(DataError, match=error):
        edit(data_dir, swap)


# ---------------------------------------------------------------- training days


def test_training_days_raise_that_persons_energy_target(data_dir, tmp_path):
    def train(hh):
        person(hh, "ana")["training_days"] = ["wednesday"]
        person(hh, "luis")["training_days"] = ["wednesday"]
        person(hh, "luis")["training_extra_kcal"] = 100

    data = edit(data_dir, train)
    db = DB(tmp_path / "x.db")
    plan = generate(data, MONDAY, 1)

    def target(day, pid):
        pds = person_days(data, db, plan, MONDAY + dt.timedelta(days=day))
        return next(pd for pd in pds if pd.person_id == pid).kcal_target

    assert target(1, "ana") == 1800  # Tuesday: no training
    assert target(2, "ana") == 1950  # Wednesday: 1800 + the default 150
    # Luis is on a GLP-1 (Wednesday is early week, x0.95): the extra is added before that factor
    # (the day factor keeps three decimals, so allow a couple of kcal)
    assert target(2, "luis") - target(1, "luis") == pytest.approx(100 * 0.95, abs=3)


# ---------------------------------------------------------------- from the chat


def test_the_chat_sets_swaps_and_training_days_only_for_the_asker(data_dir):
    data = edit(data_dir, ingredients=add_swap_ingredients)
    t = Translator("en")
    p = changes.parse(
        {
            "set": {
                "household.people.ana.swaps": {"yogur": "kefir"},
                "household.people.ana.training_days": ["monday", "wednesday"],
                "household.batch.avoid_days": ["tuesday"],
            }
        },
        data,
        "ana",
    )
    lines = changes.describe(p, data, t)
    assert "• Ana's ingredient swaps: (none) → Yogur griego → Kéfir" in lines
    assert "• Ana's training days: (none) → Mon, Wed" in lines
    assert "• Days with no batch: (none) → Tue" in lines
    texts = changes.render(data_dir, p)
    changes.validate(data_dir, texts)  # what `smp check` would say about the new files
    hh = yaml.safe_load(texts["household.yaml"])
    assert person(hh, "ana")["swaps"] == {"yogur": "kefir"}
    assert hh["batch"]["avoid_days"] == ["tuesday"]
    # anyone in the household may set the days with no batch
    changes.parse({"set": {"household.batch.avoid_days": ["sunday"]}}, data, "luis")
    for path, value in (
        ("household.people.ana.swaps", {"yogur": "kefir"}),
        ("household.people.ana.training_days", ["friday"]),
    ):
        with pytest.raises(changes.ChangeError, match="only the person themselves"):
            changes.parse({"set": {path: value}}, data, "luis")
    with pytest.raises(changes.ChangeError, match="cannot be changed from the chat"):
        changes.parse({"set": {"household.people.ana.training_extra_kcal": 300}}, data, "ana")
    with pytest.raises(changes.ChangeError, match="whole new map"):
        changes.parse({"set": {"household.people.ana.swaps": ["kefir"]}}, data, "ana")
    bad = changes.parse({"set": {"household.people.ana.swaps": {"yogur": "nada"}}}, data, "ana")
    with pytest.raises(changes.ChangeError, match="would not validate"):
        changes.validate(data_dir, changes.render(data_dir, bad))


def test_the_assistant_knows_the_three_fields(data_dir, tmp_path):
    def all_three(hh):
        hh["batch"] = {"avoid_days": ["tuesday"]}
        person(hh, "ana")["swaps"] = {"yogur": "kefir"}
        person(hh, "ana")["training_days"] = ["monday", "friday"]

    data = edit(data_dir, all_three, add_swap_ingredients)
    rules = context.rules(data)
    for needle in (
        "household.batch.avoid_days",
        "household.people.<asker's id>.swaps",
        "household.people.<asker's id>.training_days",
        "set it instead of a chat note",
    ):
        assert needle in rules, needle
    facts = context.facts(data, DB(tmp_path / "x.db"), MONDAY)
    assert "never on tuesday" in facts
    assert "Eats instead: yogur -> kefir." in facts
    assert "Trains on monday, friday: +150 kcal those days." in facts
    assert "household.people.ana.swaps: {'yogur': 'kefir'}" in facts
