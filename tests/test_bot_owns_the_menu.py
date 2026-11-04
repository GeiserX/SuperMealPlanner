"""bot.ask_first: false and the bot's own turns (the plan check, the weekly review, a follow-up):
the menu is theirs. They may swap an ingredient in a planned meal, build or redraw a week and undo
their own change, and every such change is checked on a copy (the structural check) before it is
made: one that adds a problem is not made, and one more turn of the bot's own fixes it."""

import datetime as dt

import yaml

from supermealplanner import bot as botmod
from supermealplanner import context, doctor, verifier
from supermealplanner.planner import plan_for

from .conftest import MONDAY
from .test_act_without_asking import direct
from .test_quiet_turns import own_turn
from .test_reminders import ahead
from .test_turns import cfg, sends
from .test_verifier import lines, say, vsetup

WED = dt.date(2026, 10, 14)
HAKE = "merluza-cena-1"


def block(*ops):
    """A change block the AI came up with in its own turn: no `asked`."""
    return "```change\n" + yaml.safe_dump({"plan": list(ops)}) + "```"


def plan_check(b):
    who = {"first_name": b.t("plan_check_who")}
    own_turn(b, lambda: b.take(-100, who, None, None, b.tt("plan_check_turn"), True, review=True))


def swaps(b, day):
    return [(x.meal, x.out, x.use) for x in plan_for(b.data, day).day(day).swaps]


def test_the_plan_check_swaps_an_ingredient_in_a_planned_meal_by_itself(data_dir, tmp_path):
    b, db = vsetup(data_dir, tmp_path)
    direct(b)
    use = {"date": str(WED), "meal": "dinner", "ingredient": "salmon", "instead_of": "merluza"}
    cfg(
        data_dir,
        text="Salmón en lugar de merluza el miércoles.\n"
        + block({"set": {"date": str(WED), "meal": "dinner", "recipe": HAKE}}, {"use": use}),
    )
    plan_check(b)
    assert doctor.serves(b.data, HAKE, WED)
    assert swaps(b, WED) == [("dinner", "merluza", "salmon")]
    assert not b.pending(-100)
    assert db.get_kv(verifier.PLAN_RETRY)  # the plan check judges the changed plan at once


def test_control_without_the_plan_key_the_bots_own_turn_could_not_touch_the_menu(
    data_dir, tmp_path, monkeypatch
):
    monkeypatch.setattr(botmod, "AUTO_KEYS", frozenset({"pantry", "tasks"}))
    b, _db = vsetup(data_dir, tmp_path)
    direct(b)
    use = {"date": str(WED), "meal": "dinner", "ingredient": "salmon", "instead_of": "merluza"}
    cfg(
        data_dir,
        text="Hecho.\n"
        + block({"set": {"date": str(WED), "meal": "dinner", "recipe": HAKE}}, {"use": use}),
    )
    plan_check(b)
    assert not doctor.serves(b.data, HAKE, WED)


def test_the_bots_own_turn_redraws_a_week_and_undoes_its_own_change(data_dir, tmp_path):
    b, _db = vsetup(data_dir, tmp_path)
    direct(b)
    nxt = MONDAY + dt.timedelta(days=7)
    before = plan_for(b.data, nxt).days
    cfg(data_dir, text="Semana nueva.\n" + block({"regenerate": {"from": str(nxt)}}))
    plan_check(b)
    assert plan_for(b.data, nxt).start == nxt  # a whole new week, drawn by itself
    cfg(data_dir, text="Lo deshago.\n" + block({"undo": True}))
    (data_dir / "plugins" / "turns.jsonl").unlink()  # the plugin answers its first text again
    plan_check(b)
    assert plan_for(b.data, nxt).start == MONDAY and plan_for(b.data, nxt).days == before


def test_the_bots_own_turn_never_undoes_the_households_change(data_dir, tmp_path):
    from supermealplanner import menu

    b, _db = vsetup(data_dir, tmp_path)
    direct(b)
    menu.apply(
        b.data,
        menu.parse([{"set": {"date": str(WED), "meal": "dinner", "recipe": HAKE}}], b.data),
        MONDAY,
    )
    cfg(data_dir, text="Lo deshago.\n" + block({"undo": True}))
    plan_check(b)
    assert doctor.serves(b.data, HAKE, WED)  # the household's change stays


def test_a_menu_change_of_its_own_that_adds_a_problem_is_not_made(data_dir, tmp_path):
    """Friday's dinner moved to a salad of dried chickpeas that nothing soaks or cooks: the check on
    a copy finds it, the menu stays as it was, and one follow-up turn (checked the same way) tries
    again; it repeats the same change, which is held again, and no third turn comes."""
    b, _db = vsetup(data_dir, tmp_path, ahead=[{"step": "Asar el pimiento", "session": True}])
    direct(b)
    friday = dt.date(2026, 10, 16)
    bad = block({"set": {"date": str(friday), "meal": "dinner", "recipe": "ensalada-garbanzos"}})
    cfg(data_dir, text="Ensalada de garbanzos el viernes.\n" + bad)
    plan_check(b)
    assert not doctor.serves(b.data, "ensalada-garbanzos", friday)
    texts = [p["text"] for p in sends(b, -100)]
    # the turn says a fix follows; its follow-up, held too, asks the household
    assert [x for x in texts if b.t("auto_check_held") in x] == [b.t("auto_check_held")]
    assert [x for x in texts if b.t("auto_check_held_last") in x] == [b.t("auto_check_held_last")]
    assert not any("Ensalada de garbanzos el viernes" in x for x in texts)  # never told as made
    turns = lines(data_dir, "turns.jsonl")
    assert len(turns) == 2
    said = "\n".join(turns[1]["new"])
    assert "was NOT made" in said and "nothing cooks it" in said
    assert "reminders of that answer were not set" in said
    assert not b.pending(-100)  # never left waiting for the tick to accept it


def held_with_a_reminder(data_dir, tmp_path, recipe):
    b, db = vsetup(data_dir, tmp_path, ahead=[{"step": "Asar el pimiento", "session": True}])
    direct(b)
    stale = db.add_reminder(-100, None, ahead(3, 9)[1], "Sacar la merluza", 7)
    raw = {
        "plan": [{"set": {"date": "2026-10-16", "meal": "dinner", "recipe": recipe}}],
        "pantry": {"lechuga": 300},
    }
    when, _ = ahead(2, 20)
    reminder = f"```reminders\n- {when} | Remojar los garbanzos\n- cancel | {stale}\n```"
    cfg(data_dir, text="Hecho.\n```change\n" + yaml.safe_dump(raw) + "```\n" + reminder)
    plan_check(b)
    return b, db, stale


def test_a_held_change_keeps_its_pantry_and_cancels_and_drops_its_new_reminders(data_dir, tmp_path):
    b, db, stale = held_with_a_reminder(data_dir, tmp_path, "ensalada-garbanzos")
    assert not doctor.serves(b.data, "ensalada-garbanzos", dt.date(2026, 10, 16))
    assert {x["ingredient"] for x in db.lots()} == {"lechuga"}  # the pantry part is made
    assert db.reminder(stale)["state"] == "cancelled"  # a stale one goes all the same
    texts = [r["text"] for r in db.reminders(-100)]
    assert "Remojar los garbanzos" not in texts  # may be written for the menu not made
    logged = "\n".join(r["text"] for r in db.chat_rows(-100, 50) if r["role"] == "assistant")
    assert "the check found 2 problems" in logged


def test_control_a_change_that_passes_sets_its_reminders(data_dir, tmp_path):
    b, db, stale = held_with_a_reminder(data_dir, tmp_path, HAKE)
    assert doctor.serves(b.data, HAKE, dt.date(2026, 10, 16))
    assert "Remojar los garbanzos" in [r["text"] for r in db.reminders(-100)]
    assert db.reminder(stale)["state"] == "cancelled"


def test_control_the_same_change_asked_for_by_a_person_is_made(data_dir, tmp_path):
    """Someone's own request is made as asked and reviewed once sent (verifier.after), as before."""
    b, _db = vsetup(data_dir, tmp_path, ahead=[{"step": "Asar el pimiento", "session": True}])
    direct(b)
    friday = dt.date(2026, 10, 16)
    bad = block({"set": {"date": str(friday), "meal": "dinner", "recipe": "ensalada-garbanzos"}})
    cfg(data_dir, text="Hecho.\n" + bad)
    say(b, "@chefbot ensalada de garbanzos el viernes", message_id=1)
    assert doctor.serves(b.data, "ensalada-garbanzos", friday)


def test_the_rules_give_the_bot_the_menu_and_prefer_it_over_a_reminder(data_dir, tmp_path):
    b, _db = vsetup(data_dir, tmp_path)
    rules = context.rules(b.data)
    assert "- use: in planned meals, one ingredient instead of another" in rules
    assert "build a whole\n  week" in rules and "undo your own last change" in rules
    assert "change the menu (set, use, regenerate) instead of setting a reminder" in rules
    assert "  - use: {date: 2026-01-14, meal: lunch" in rules
