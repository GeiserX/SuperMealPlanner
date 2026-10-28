"""The consistency check (doctor.py): sessions on days that are no longer batch days, one dish
cooked twice, lunches that do not keep or that nobody cooked, reminders about a dish the menu
dropped; and where the bot runs it: after a change, before a reminder fires, each morning."""

import datetime as dt
import sqlite3
import time

import yaml

from supermealplanner import context, doctor, llm, menu
from supermealplanner.db import DB
from supermealplanner.model import load
from supermealplanner.planner import load_plan, plan_for, plans_dir

from .conftest import MONDAY
from .test_conversations import block as change
from .test_reminders import ZONE, ahead, block, setup
from .test_settings_stores_bot import msg, run
from .test_turns import cfg, sends


def D(n):
    return MONDAY + dt.timedelta(days=n)


def keys(data, db, today=MONDAY):
    return [p.key for p in doctor.problems(data, db, today)]


def household(data_dir, **batch):
    raw = yaml.safe_load((data_dir / "household.yaml").read_text())
    raw["batch"] = {**raw.get("batch", {}), **batch}
    (data_dir / "household.yaml").write_text(yaml.safe_dump(raw, allow_unicode=True))


def edit_plan(data, fn):
    path = plans_dir(data) / f"{MONDAY}.yaml"
    plan = load_plan(path)
    fn(plan)
    plan.save(path)


def test_a_fresh_plan_has_no_problems(data_dir, tmp_path):
    b, db = setup(data_dir, tmp_path)
    assert doctor.problems(b.data, db, MONDAY) == []
    assert "Problems the app found" not in context.facts(b.data, db, MONDAY)


# ---------------------------------------------------------------- batch days


def test_sessions_left_on_old_batch_days_are_flagged_and_the_facts_say_what_to_change(
    data_dir, tmp_path
):
    _b, db = setup(data_dir, tmp_path)  # sessions on Mon 12, Fri 16, Mon 19, Fri 23
    household(data_dir, preferred_days=["wednesday", "saturday"])
    data = load(data_dir)
    assert keys(data, db) == ["off_day:2026-10-16", "off_day:2026-10-19", "off_day:2026-10-23"]
    facts = context.facts(data, db, MONDAY)
    assert "## Problems the app found: fix them" in facts
    assert "The batch session on friday 2026-10-16" in facts
    assert "the planner would cook on saturday 2026-10-17" in facts
    assert "`regenerate` from 2026-10-19" in facts  # a week still to come may be drawn again


def test_a_session_on_a_day_with_no_batch_is_flagged_even_when_it_is_a_batch_day(
    data_dir, tmp_path
):
    _b, db = setup(data_dir, tmp_path)
    household(data_dir, avoid_days=["friday"])
    assert keys(load(data_dir), db) == ["off_day:2026-10-16", "off_day:2026-10-23"]


def test_a_session_moved_because_the_batch_day_was_busy_is_not_flagged(data_dir, tmp_path):
    _b, db = setup(data_dir, tmp_path)
    household(data_dir, preferred_days=["monday", "saturday"])
    data = load(data_dir)
    assert "off_day:2026-10-16" in keys(data, db)

    def busy(plan):
        plan.day(D(5)).busy = ["Football"]  # Saturday 17 is taken: the planner picks Friday

    edit_plan(data, busy)
    assert "off_day:2026-10-16" not in keys(data, db)


def test_a_one_off_session_beside_the_usual_one_is_not_flagged(data_dir, tmp_path):
    b, db = setup(data_dir, tmp_path)
    data = b.data
    lunch = plan_for(data, D(3)).day(D(3)).lunch
    rid = "legumbre-0" if lunch != "legumbre-0" else "legumbre-2"  # keeps the week's legumes
    ops = menu.parse(
        [{"set": {"date": str(D(3)), "meal": "lunch", "recipe": rid, "cook": str(D(2))}}], data
    )
    menu.apply(data, ops, MONDAY)  # cooked on Wednesday, while Monday's session stays
    assert not [k for k in keys(data, db) if k.startswith("off_day")]


# ---------------------------------------------------------------- lunches


def test_one_dish_cooked_on_two_days_for_two_lunches_in_a_row_is_flagged(data_dir, tmp_path):
    b, db = setup(data_dir, tmp_path)
    data = b.data
    plan = plan_for(data, D(3))
    thu, fri = plan.day(D(3)), plan.day(D(4))
    assert thu.lunch == fri.lunch and thu.lunch_cook == fri.lunch_cook == MONDAY
    ops = menu.parse(
        [{"set": {"date": str(D(3)), "meal": "lunch", "recipe": thu.lunch, "cook": str(D(2))}}],
        data,
    )
    menu.apply(data, ops, MONDAY)
    (p,) = [p for p in doctor.problems(data, db, MONDAY) if p.kind == "split"]
    assert p.key == f"split:{thu.lunch}:2026-10-15"
    assert "one cook on 2026-10-12 keeps for both" in p.text
    assert "`set` 2026-10-15 lunch" in p.text and "`cook: 2026-10-12`" in p.text


def test_a_lunch_cooked_too_long_before_its_day_that_does_not_freeze_is_flagged(data_dir, tmp_path):
    b, db = setup(data_dir, tmp_path)

    def stale(plan):
        day = plan.day(D(4))
        day.lunch, day.lunch_cook = "pollo-sin-congelar", MONDAY  # 4 days, keeps 2, no freezer

    edit_plan(b.data, stale)
    (p,) = [p for p in doctor.problems(b.data, db, MONDAY) if p.kind == "too_old"]
    assert p.key == "too_old:2026-10-16" and "`cook:` 2026-10-14 to 2026-10-16" in p.text


def test_a_lunch_whose_past_cook_date_no_session_cooked_is_flagged(data_dir, tmp_path):
    b, db = setup(data_dir, tmp_path)
    today = D(2)
    assert not [k for k in keys(b.data, db, today) if k.startswith("uncooked")]

    def phantom(plan):
        plan.day(D(2)).lunch_cook = D(1)  # Tuesday had no session for it

    edit_plan(b.data, phantom)
    assert "uncooked:2026-10-14" in keys(b.data, db, today)
    assert "uncooked:2026-10-14" not in keys(b.data, db, D(1))  # a cook still to come is fine


# ---------------------------------------------------------------- reminders about a dish


def test_the_block_parser_reads_a_dish_link_after_the_person_or_after_set():
    _, found = llm.extract_reminders(
        "Ok\n```reminders\n"
        "- 2026-11-01 22:00 | Defrost the hake | ana | | dish hake-bake 2026-11-02\n"
        "- 2026-11-01 23:00 | Soak | | set | dish lentil-stew 2026-11-03\n"
        "- 2026-11-01 09:00 | Plain\n```"
    )
    assert found == [
        {
            "when": "2026-11-01 22:00",
            "text": "Defrost the hake",
            "person": "ana",
            "recipe": "hake-bake",
            "date": "2026-11-02",
        },
        {
            "when": "2026-11-01 23:00",
            "text": "Soak",
            "person": None,
            "set": True,
            "recipe": "lentil-stew",
            "date": "2026-11-03",
        },
        {"when": "2026-11-01 09:00", "text": "Plain", "person": None},
    ]


def test_a_reminder_asked_for_keeps_its_dish_link_and_a_link_off_the_menu_is_dropped(
    data_dir, tmp_path
):
    b, db = setup(data_dir, tmp_path)
    when, _ = ahead(1, 22)
    dinner = plan_for(b.data, D(3)).day(D(3)).dinner
    rows = [
        (when, "Defrost", "", "set", f"dish {dinner} {D(3)}"),
        (when, "Wrong link", "", "set", f"dish gazpacho {D(3)}"),  # not that day's menu
    ]
    cfg(data_dir, text="Done.\n" + block(*rows))
    run(b.handle(None, msg("@chefbot remind me", message_id=1)))
    got = {r["text"]: (r["recipe"], r["date"]) for r in db.reminders(-100)}
    assert got == {"Defrost": (dinner, str(D(3))), "Wrong link": (None, None)}


def test_a_dish_swapped_out_cancels_its_reminder_and_one_line_says_so(data_dir, tmp_path):
    b, db = setup(data_dir, tmp_path)
    day = plan_for(b.data, D(3)).day(D(3))
    other = next(f"merluza-cena-{i}" for i in range(4) if f"merluza-cena-{i}" != day.dinner)
    _, at = ahead(1, 21)
    fish = db.add_reminder(-100, "ana", at, "Move the fish to the fridge", 7, day.dinner, str(D(3)))
    breakfast = db.add_reminder(-100, "ana", at, "Buy bread", 7, day.breakfast, str(D(3)))
    plain = db.add_reminder(-100, "ana", at, "Call the shop", 7)
    cfg(
        data_dir,
        text=change(
            {
                "asked": True,
                "plan": [{"set": {"date": str(D(3)), "meal": "dinner", "recipe": other}}],
            }
        ),
    )
    run(b.handle(None, msg("@chefbot another fish on Thursday", message_id=1)))
    assert db.reminder(fish)["state"] == "cancelled"
    assert db.reminder(breakfast)["state"] == db.reminder(plain)["state"] == "pending"
    said = [p["text"] for p in sends(b, -100) if "the menu no longer has its dish" in p["text"]]
    assert said == [
        (
            f"🗑 Reminder cancelled, the menu no longer has its dish: {b._when(at)}: "
            "Move the fish to the fridge"
        )
    ]


def test_a_due_reminder_whose_dish_left_the_menu_is_cancelled_not_posted(data_dir, tmp_path):
    b, db = setup(data_dir, tmp_path)
    day = plan_for(b.data, D(3)).day(D(3))
    rid = db.add_reminder(-100, None, time.time() - 5, "Thaw it", 7, "gazpacho", str(D(3)))
    keep = db.add_reminder(-100, None, time.time() - 5, "Cook it", 7, day.lunch, str(D(3)))
    run(b.tick_once(None))
    texts = [p["text"] for p in sends(b, -100)]
    assert db.reminder(rid)["state"] == "cancelled" and not any("Thaw it" in x for x in texts[1:])
    assert db.reminder(keep)["state"] == "sent" and "⏰ Cook it" in texts


def test_old_reminder_rows_keep_working_after_the_new_columns(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as c:
        c.execute(
            "CREATE TABLE reminder (id INTEGER PRIMARY KEY AUTOINCREMENT, chat INTEGER NOT NULL, "
            "person TEXT, at REAL NOT NULL, text TEXT NOT NULL, state TEXT NOT NULL DEFAULT "
            "'pending', by INTEGER, created TEXT NOT NULL)"
        )
        c.execute("INSERT INTO reminder(chat, at, text, created) VALUES (-1, 1, 'old', 'x')")
    db = DB(path)
    (old,) = db.reminders(-1)
    assert (old["text"], old["recipe"], old["date"]) == ("old", None, None)
    new = db.add_reminder(-1, None, 2, "new", None, "r", "2026-01-01")
    assert (db.reminder(new)["recipe"], db.reminder(new)["date"]) == ("r", "2026-01-01")


# ---------------------------------------------------------------- after a change, each morning


def test_a_change_that_splits_a_dish_says_so_in_one_line_under_the_answer(data_dir, tmp_path):
    b, _db = setup(data_dir, tmp_path)
    thu = plan_for(b.data, D(3)).day(D(3))
    set_ = {"date": str(D(3)), "meal": "lunch", "recipe": thu.lunch, "cook": str(D(2))}
    cfg(data_dir, text=change({"asked": True, "plan": [{"set": set_}]}))
    run(b.handle(None, msg("@chefbot cook Thursday's lunch on Wednesday", message_id=1)))
    (line,) = [p["text"] for p in sends(b, -100) if p["text"].startswith("⚠️")]
    assert line.startswith("⚠️ This change left something to fix: ")
    assert "is cooked twice; one cook on" in line and "\n" not in line


def test_a_change_names_only_the_problems_it_made(data_dir, tmp_path):
    b, db = setup(data_dir, tmp_path)
    household(data_dir, preferred_days=["wednesday", "saturday"])  # by hand, before the chat
    b.state.reload()
    assert keys(b.data, db)
    day = plan_for(b.data, D(3)).day(D(3))
    other = next(
        r.id
        for r in b.data.recipes.values()
        if r.id.startswith("salmon-cena") and r.id != day.dinner
    )
    cfg(
        data_dir,
        text=change(
            {
                "asked": True,
                "plan": [{"set": {"date": str(D(3)), "meal": "dinner", "recipe": other}}],
            }
        ),
    )
    run(b.handle(None, msg("@chefbot salmon on Thursday", message_id=1)))
    assert not [p for p in sends(b, -100) if p["text"].startswith("⚠️")]


def test_the_morning_checkup_reports_once_and_only_when_something_is_wrong(data_dir, tmp_path):
    b, db = setup(data_dir, tmp_path)
    now = dt.datetime.now(ZONE)
    b.data.settings.bot.checkup_at = f"{now:%H:%M}"
    run(b.tick_checkup(None))
    assert sends(b, -100) == []  # nothing wrong: nothing said
    household(data_dir, preferred_days=["wednesday", "saturday"])
    b.state.reload()
    b.data.settings.bot.checkup_at = f"{now:%H:%M}"
    with sqlite3.connect(db.path) as c:  # the quiet run above counted as this morning's
        c.execute("DELETE FROM sent WHERE reminder='checkup'")
    run(b.tick_checkup(None))
    run(b.tick_checkup(None))
    (report,) = [p["text"] for p in sends(b, -100)]
    assert report.startswith("⚠️ The plan has something to fix: the batch session on Friday 16")


def test_the_morning_checkup_can_be_turned_off(data_dir, tmp_path):
    b, _db = setup(data_dir, tmp_path)
    household(data_dir, preferred_days=["wednesday", "saturday"])
    b.state.reload()
    b.data.settings.bot.checkup_at = ""
    run(b.tick_checkup(None))
    assert sends(b, -100) == []


# ---------------------------------------------------------------- limits


def test_the_limits_the_validator_enforces_are_in_the_facts(data):
    facts = context.facts(data, DB(data.root / "x.db"), MONDAY)
    assert "household.batch.sessions_per_week: 1 to 2" in facts
    assert "a new recipe: fridge_days 0 to 4" in facts
    assert "shift 1 to 4 weeks" in facts and "a pause at most 92 days" in facts
