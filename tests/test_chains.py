"""The chains the consistency check follows step by step (doctor.py): a dried pulse soaked, then
cooked, before its meal; thawed food cooked soon after the thaw; a lunch cooked by the session it
counts on. The first two tests are the two times the household was let down: a soak with no cook,
and a thaw whose session moved days later."""

import datetime as dt

import yaml

from supermealplanner import doctor, tasks
from supermealplanner.model import load

from .conftest import MONDAY
from .test_doctor import D, edit_plan
from .test_menu import change
from .test_reminders import ZONE, setup
from .test_verifier import dinner, soak

SALAD = "ensalada-garbanzos"  # an assembled dinner with dried chickpeas (test_verifier.SALAD)


def at(day, hm):
    h, m = (int(x) for x in hm.split(":"))
    return dt.datetime(day.year, day.month, day.day, h, m, tzinfo=ZONE).timestamp()


def found(data, db, today=MONDAY, offered=()):
    return {p.key: p for p in doctor.problems(data, db, today, offered) if p.kind != "avoided"}


def remind(db, day, hm, recipe, eat, kind=None):
    return db.add_reminder(-100, "ana", at(day, hm), "x", 7, recipe, str(eat), kind)


def test_a_soak_with_no_cook_is_caught_and_so_is_one_left_too_long(data_dir, tmp_path):
    """The dried beans for Monday's salad: only a soak was set, on Friday night, and nothing said
    to cook them, so nobody did."""
    _b, db = setup(data_dir, tmp_path)
    data = soak(data_dir)
    monday = D(7)
    dinner(data, monday)
    soaking = remind(db, D(4), "21:30", SALAD, monday, "soak")
    p = found(data, db)[f"no_cook:{SALAD}:{monday}"]
    assert "2026-10-19" in p.text and SALAD in p.text and "only assembled" in p.text
    assert f"`dish {SALAD} {monday} cook`" in p.text
    # the cook on Saturday afternoon, 18.5 h after the soak: the chain holds
    remind(db, D(5), "16:00", SALAD, monday, "cook")
    assert not [k for k in found(data, db) if SALAD in k]
    # the soak on Thursday night instead: 42.5 h in water, too long out of the fridge
    db.settle_reminder(soaking, "cancelled")
    remind(db, D(3), "21:30", SALAD, monday, "soak")
    p = found(data, db)[f"soak_long:{SALAD}:{monday}"]
    assert "thursday 2026-10-15 21:30" in p.text and "saturday 2026-10-17 16:00" in p.text
    assert "soak-fridge" in p.text
    # in the fridge it may soak that long
    remind(db, D(3), "21:00", SALAD, monday, "soak-fridge")
    assert not [k for k in found(data, db) if SALAD in k]


def test_a_thaw_whose_session_moves_days_later_is_caught(data_dir, tmp_path):
    """The hake thawed on Friday night for the Saturday session that cooks Tuesday's lunch; the
    session moved to Monday and the fish would have sat thawed for three days."""
    _b, db = setup(data_dir, tmp_path)
    data = soak(data_dir)
    tuesday = D(8)
    lunch = {"date": str(tuesday), "meal": "lunch", "recipe": "pescado-batch"}
    change(data, {"set": {**lunch, "cook": str(D(5))}})
    remind(db, D(4), "22:00", "pescado-batch", tuesday, "thaw")
    assert f"thaw_long:pescado-batch:{tuesday}" not in found(data, db)
    change(data, {"set": {**lunch, "cook": str(D(7))}})
    p = found(data, db)[f"thaw_long:pescado-batch:{tuesday}"]
    assert "friday 2026-10-16 22:00" in p.text and "monday 2026-10-19" in p.text
    assert "pescado-batch" in p.text and "48 h" in p.text


def test_a_reminder_that_says_no_step_is_no_cook(data_dir, tmp_path):
    _b, db = setup(data_dir, tmp_path)
    data = soak(data_dir)
    dinner(data, D(2))
    rid = remind(db, D(1), "20:00", SALAD, D(2))  # set before reminders said their step
    p = found(data, db)[f"no_cook:{SALAD}:{D(2)}"]
    assert f"Reminders [{rid}]" in p.text and "list it again with the step" in p.text
    # the same steps with their kinds, offered by an answer: the chain holds
    dish = {"text": "x", "recipe": SALAD, "date": str(D(2))}
    offered = [
        {**dish, "when": "2026-10-13 20:00", "kind": "soak"},
        {**dish, "when": "2026-10-14 12:00", "kind": "cook"},
    ]
    assert not [k for k in found(data, db, offered=offered) if SALAD in k]
    p = found(data, db, offered=offered[1:])[f"no_soak:{SALAD}:{D(2)}"]
    assert "wednesday 2026-10-14 12:00" in p.text


def test_a_cooked_dinner_is_its_own_cook_and_lentils_need_no_soak(data_dir, tmp_path):
    """Negative controls: lentils are not marked to soak, so they start no chain; a cooked dinner
    with chickpeas needs only its soak the evening before."""
    _b, db = setup(data_dir, tmp_path)
    data = soak(data_dir, kind="quick")
    dinner(data, D(2), "crema-legumbre-0")  # lentils
    assert not [k for k in found(data, db) if "crema-legumbre-0" in k]
    dinner(data, D(2))
    assert f"no_soak:{SALAD}:{D(2)}" in found(data, db)
    remind(db, D(1), "21:00", SALAD, D(2), "soak")  # 22 h before the dinner is cooked
    assert not [k for k in found(data, db) if SALAD in k]


def test_a_lunch_counting_on_a_session_that_does_not_cook_it_is_caught(data_dir, tmp_path):
    _b, db = setup(data_dir, tmp_path)
    data = soak(data_dir)

    def move(plan):
        """By hand, as a person editing the file: the lunch says Tuesday, no session cooks it."""
        plan.day(D(9)).lunch_cook = D(8)
        for s in plan.sessions:
            for x in s.dishes:
                x.eat = [e for e in x.eat if e != D(9)]

    edit_plan(data, move)
    day = doctor.Plans(data).day(D(9))
    p = found(data, db)[f"no_session:{day.lunch}:{D(9)}"]
    assert "tuesday 2026-10-20" in p.text and day.lunch in p.text


def test_a_thaw_after_the_session_that_already_cooked_the_dish_is_caught(data_dir, tmp_path):
    """Tuesday's hake lunch is cooked in Saturday's session; a thaw set from the chat for Monday
    night takes raw hake out for food cooked three days before, and nothing cooks it."""
    _b, db = setup(data_dir, tmp_path)
    data = soak(data_dir)
    tuesday = D(8)
    change(
        data,
        {
            "set": {
                "date": str(tuesday),
                "meal": "lunch",
                "recipe": "pescado-batch",
                "cook": str(D(5)),
            }
        },
    )
    rid = remind(db, D(7), "20:00", "pescado-batch", tuesday, "thaw")
    p = found(data, db)[f"thaw_uncooked:pescado-batch:{tuesday}"]
    assert "monday 2026-10-19 20:00" in p.text and f"(reminder {rid})" in p.text
    assert "saturday 2026-10-17" in p.text and "pescado-batch" in p.text
    # the same thaw on Friday night, before the session: the chain holds
    db.settle_reminder(rid, "cancelled")
    remind(db, D(4), "22:00", "pescado-batch", tuesday, "thaw")
    assert not [k for k in found(data, db) if "pescado-batch" in k]


def test_the_apps_own_thaw_of_frozen_food_is_used_by_the_meal(data_dir, tmp_path):
    """Food marked frozen is served with the meal (bread): the app takes it out the evening before
    the day it is eaten, also for a lunch a session cooked days before, and that is no problem."""
    _b, db = setup(data_dir, tmp_path)
    raw = yaml.safe_load((data_dir / "ingredients.yaml").read_text())
    raw["merluza"]["frozen"] = True
    (data_dir / "ingredients.yaml").write_text(yaml.safe_dump(raw, allow_unicode=True))
    soak(data_dir)
    data = load(data_dir)
    tuesday = D(8)
    change(
        data,
        {
            "set": {
                "date": str(tuesday),
                "meal": "lunch",
                "recipe": "pescado-batch",
                "cook": str(D(5)),
            }
        },
    )
    keys = [x.key for x in tasks.tasks_for(data, db, D(5), tuesday)]
    assert f"thaw:{tuesday}:lunch:merluza" in keys  # the app's own thaw, on Monday evening
    assert not [k for k in found(data, db) if "pescado-batch" in k]


def test_a_soak_across_the_clock_change_is_counted_in_real_hours(data_dir, tmp_path):
    """The clocks go back on Sunday 25 October: 18:30 on Saturday to 18:00 on Sunday reads 23.5 h
    on the clock face, but the chickpeas are 24.5 h in water."""
    _b, db = setup(data_dir, tmp_path)
    data = soak(data_dir)
    sunday = D(13)
    dinner(data, sunday)
    remind(db, D(13), "18:00", SALAD, sunday, "cook")
    remind(db, D(12), "18:30", SALAD, sunday, "soak")
    p = found(data, db)[f"soak_long:{SALAD}:{sunday}"]
    assert "24 h in water" in p.text or "25 h in water" in p.text


def test_cancelling_the_only_cook_reminder_breaks_the_chain(data_dir, tmp_path):
    _b, db = setup(data_dir, tmp_path)
    data = soak(data_dir)
    dinner(data, D(2))
    remind(db, D(1), "21:00", SALAD, D(2), "soak")
    cook = remind(db, D(2), "12:00", SALAD, D(2), "cook")
    assert not [k for k in found(data, db) if SALAD in k]
    for cancel in (cook, str(cook)):
        assert f"no_cook:{SALAD}:{D(2)}" in found(data, db, offered=[{"cancel": cancel}])


def test_a_thaw_for_a_deleted_recipe_still_on_the_menu_does_not_stop_the_checks(data_dir, tmp_path):
    _b, db = setup(data_dir, tmp_path)
    data = soak(data_dir)
    tuesday = D(8)
    lunch = {"date": str(tuesday), "meal": "lunch", "recipe": "pescado-batch"}
    change(data, {"set": {**lunch, "cook": str(D(5))}})
    remind(db, D(7), "20:00", "pescado-batch", tuesday, "thaw")
    del data.recipes["pescado-batch"]
    doctor.problems(data, db, MONDAY)


def _start(data_dir, start, lunch_time="14:30"):
    """The household's lunch time, and the time its same-day sessions start on some weekdays."""
    hh = yaml.safe_load((data_dir / "household.yaml").read_text())
    hh["lunch_time"] = lunch_time
    hh.setdefault("batch", {})["start"] = start
    (data_dir / "household.yaml").write_text(yaml.safe_dump(hh, allow_unicode=True))


def test_a_pulse_lunch_cooked_in_a_session_that_starts_after_noon_is_soaked_by_the_app(
    data_dir, tmp_path
):
    """Monday's chickpea lunch is cooked that day, in a session that starts at 13:00 for a lunch at
    14:30: the app's own soak the evening before covers it, as it does for a session at 10:00."""
    _b, db = setup(data_dir, tmp_path)
    _start(data_dir, {"monday": "13:00"})
    data = soak(data_dir)
    change(data, {"set": {"date": str(MONDAY), "meal": "lunch", "recipe": "legumbre-0"}})
    _plan, session = doctor.Plans(data).sessions_on(MONDAY)[0]
    assert session.morning and "legumbre-0" in {x.recipe for x in session.dishes}
    assert not [k for k in found(data, db) if k.startswith(("no_soak", "no_cook"))]


def test_a_thaw_before_a_session_that_starts_after_noon_is_cooked_by_it(data_dir, tmp_path):
    """Monday's hake lunch is cooked that day from 13:00: a thaw at 12:30 comes before the cook."""
    _b, db = setup(data_dir, tmp_path)
    _start(data_dir, {"monday": "13:00"})
    data = soak(data_dir)
    change(data, {"set": {"date": str(MONDAY), "meal": "lunch", "recipe": "pescado-batch"}})
    remind(db, MONDAY, "12:30", "pescado-batch", MONDAY, "thaw")
    assert not [k for k in found(data, db) if "pescado-batch" in k]


def test_one_frozen_food_for_two_of_tomorrows_meals_is_one_line_with_the_whole_amount(
    data_dir, tmp_path
):
    """Bread for tomorrow's lunch and dinner showed as two «take out of the freezer» lines of the
    same food on 4 Nov; it is one step: one line, both meals named, the amounts added up."""
    b, db = setup(data_dir, tmp_path)
    raw = yaml.safe_load((data_dir / "ingredients.yaml").read_text())
    raw["merluza"]["frozen"] = True
    (data_dir / "ingredients.yaml").write_text(yaml.safe_dump(raw, allow_unicode=True))
    soak(data_dir)
    data = load(data_dir)
    tuesday = D(8)
    change(
        data,
        {
            "set": {
                "date": str(tuesday),
                "meal": "lunch",
                "recipe": "pescado-batch",
                "cook": str(D(5)),
            }
        },
    )
    change(data, {"set": {"date": str(tuesday), "meal": "dinner", "recipe": "merluza-cena-1"}})
    thaws = [x for x in tasks.tasks_for(data, db, D(7), D(7), bot=b) if x.kind == "thaw"]
    (x,) = [x for x in thaws if x.ingredient == "merluza"]
    assert x.key == f"thaw:{tuesday}:lunch:merluza"  # the key a lunch alone always had
    assert "pescado-batch" in x.text and "merluza-cena-1" in x.text
    assert x.text.count("(") >= 1 and " · " in x.text
