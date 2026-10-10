import datetime as dt

from supermealplanner.db import DB
from supermealplanner.targets import day_factor, evaluate, trend

from .conftest import MONDAY


def test_glp1_factor_by_weekday(data):
    hh = data.household
    luis = hh.person("luis")
    base = 2300 / 2000
    assert day_factor(hh, luis, 2300, MONDAY) == round(base, 3)
    assert day_factor(hh, luis, 2300, MONDAY + dt.timedelta(days=2)) == round(base * 0.95, 3)
    assert day_factor(hh, luis, 2300, MONDAY + dt.timedelta(days=5)) == round(base * 1.05, 3)
    ana = hh.person("ana")
    assert day_factor(hh, ana, 1800, MONDAY + dt.timedelta(days=2)) == 0.9


def test_trend_smooths():
    days = [dt.date(2026, 1, 1) + dt.timedelta(days=7 * i) for i in range(4)]
    tr = trend(list(zip(days, [90, 80, 90, 80])))
    assert tr[0][1] == 90
    assert 80 < tr[-1][1] < 90


def _weights(db, who, start, kgs):
    for i, kg in enumerate(kgs):
        db.add_weight(who, start + dt.timedelta(days=7 * i), kg)


def test_fast_loss_adds_food_and_flags_doctor(data, tmp_path):
    db = DB(tmp_path / "t.db")
    luis = data.household.person("luis")
    start = dt.date(2026, 9, 1)
    _weights(db, "luis", start, [95, 92, 89, 86, 83])
    st = evaluate(db, data.household, luis, start + dt.timedelta(days=28), apply=True)
    assert st.kcal == 2300 + 150
    assert any(k == "flag_fast_loss" for k, _ in st.flags)
    assert db.targets("luis")[-1]["kcal"] == 2450


def test_slow_loss_cuts_but_never_below_floor(data, tmp_path):
    db = DB(tmp_path / "t.db")
    luis = data.household.person("luis")
    db.set_target("luis", dt.date(2026, 8, 1), 1850, "test")
    start = dt.date(2026, 9, 1)
    _weights(db, "luis", start, [90, 90, 90, 90, 90])
    st = evaluate(db, data.household, luis, start + dt.timedelta(days=28), apply=True)
    assert st.kcal == 1800  # floor, not 1700


def test_no_change_with_few_readings(data, tmp_path):
    db = DB(tmp_path / "t.db")
    luis = data.household.person("luis")
    db.add_weight("luis", dt.date(2026, 9, 1), 95)
    db.add_weight("luis", dt.date(2026, 9, 29), 90)
    st = evaluate(db, data.household, luis, dt.date(2026, 9, 29), apply=True)
    assert st.changed is None and db.targets("luis") == []


def test_inside_band_switches_to_maintenance(data, tmp_path):
    db = DB(tmp_path / "t.db")
    luis = data.household.person("luis")
    _weights(db, "luis", dt.date(2026, 9, 1), [80, 80, 80, 80, 80])
    st = evaluate(db, data.household, luis, dt.date(2026, 9, 29))
    assert st.mode == "maintain"


def test_binges_flag_without_cutting_food(data, tmp_path):
    db = DB(tmp_path / "t.db")
    luis = data.household.person("luis")
    today = dt.date(2026, 9, 29)
    for i in range(4):
        db.set_daily("luis", today - dt.timedelta(days=3 * i), binge=1)
    st = evaluate(db, data.household, luis, today)
    assert any(k == "flag_binges" and p["n"] == 4 for k, p in st.flags)
    assert st.kcal == 2300
