"""Menu changes from the chat, changes made at once when the person asked, and the whole day in
the app."""

import datetime as dt

import pytest
import yaml
from fastapi.testclient import TestClient

from supermealplanner import changes, context, menu
from supermealplanner.db import DB
from supermealplanner.i18n import Translator
from supermealplanner.model import load
from supermealplanner.planner import (
    generate,
    load_plan,
    nibbles,
    paused_days,
    plan_for,
    plans_dir,
    validate,
)
from supermealplanner.portions import batch_sheet, needs

from .conftest import MONDAY, recipe
from .test_conversations import TO_BOT, ChatBot, block, plugin_data, reply
from .test_settings_stores_bot import FakeBot, bot_data, msg, run


def D(n: int) -> dt.date:
    return MONDAY + dt.timedelta(days=n)


def planned(data_dir):
    data = load(data_dir)
    generate(data, MONDAY, 4).save(plans_dir(data) / f"{MONDAY}.yaml")
    return data


def change(data, *ops, today=MONDAY):
    menu.apply(data, menu.parse(list(ops), data), today)


def snacks(data_dir):
    """A plan drawn with two more snacks than the fixture's one, then a third snack that uses an
    avoided ingredient (added after the draw, as a recipe someone adds later)."""
    more = [("fruta", None), ("frutos-secos", None)]
    late = [("batido-verde", [{"id": "espinaca", "qty": 50}])]
    for batch in (more, late):
        for rid, items in batch:
            r = recipe(rid, ["snack"], "fixed", "mixed", [], items, kcal=150, prot=5)
            (data_dir / "recipes" / f"{rid}.yaml").write_text(yaml.safe_dump(r, allow_unicode=True))
        if batch is more:
            planned(data_dir)
    return load(data_dir)


# ---------------------------------------------------------------- set and swap


def test_a_dish_is_put_in_any_meal_of_a_day(data_dir):
    data = planned(data_dir)
    before = plan_for(data, D(2))
    dinner = next(
        r for r in ("ensalada-0", "ensalada-1", "ensalada-2") if r != before.day(D(2)).dinner
    )
    change(data, {"set": {"date": str(D(2)), "meal": "dinner", "recipe": dinner}})
    change(data, {"set": {"date": str(D(2)), "meal": "breakfast", "recipe": "desayuno-b"}})
    after = plan_for(data, D(2))
    assert after.day(D(2)).dinner == dinner and after.day(D(2)).breakfast == "desayuno-b"
    assert after.day(D(3)) == before.day(D(3))  # nothing else moved
    assert validate(data, after) == validate(data, before)


def test_a_new_lunch_leaves_its_old_container_and_gets_its_own(data_dir):
    data = planned(data_dir)
    before = plan_for(data, D(1)).day(D(1))
    change(data, {"set": {"date": str(D(1)), "meal": "lunch", "recipe": "pollo-2"}}, today=D(1))
    plan = plan_for(data, D(1))
    day = plan.day(D(1))
    assert day.lunch == "pollo-2" and day.lunch_cook == D(1)  # its batch day is gone: fresh
    dishes = [x for s in plan.sessions for x in s.dishes if D(1) in x.eat]
    assert [(x.recipe, x.cook) for x in dishes] == [("pollo-2", D(1))]
    old = [x for s in plan.sessions for x in s.dishes if x.recipe == before.lunch]
    assert all(D(1) not in x.eat for x in old)


def test_two_days_swap_and_every_lunch_is_still_cooked_before_it_is_eaten(data_dir):
    data = planned(data_dir)
    before = plan_for(data, MONDAY)
    a, b = before.day(D(1)), before.day(D(4))
    change(data, {"swap": {"a": str(D(1)), "b": str(D(4))}})
    after = plan_for(data, MONDAY)
    for meal in ("breakfast", "lunch", "snack", "dinner", "side"):
        assert getattr(after.day(D(1)), meal) == getattr(b, meal)
        assert getattr(after.day(D(4)), meal) == getattr(a, meal)
    assert all(d.lunch_cook <= d.date for d in after.days)
    assert not [e for e in validate(data, after) if "cooked after" in e]
    change(data, {"swap": {"a": str(D(2)), "b": str(D(5)), "meal": "dinner"}})
    again = plan_for(data, MONDAY)
    assert again.day(D(2)).dinner == after.day(D(5)).dinner
    assert again.day(D(2)).lunch == after.day(D(2)).lunch
    # a lunch that a later batch day cooks moves to an earlier day: cooked that morning instead
    late = next(d for d in again.days if d.lunch_cook > D(1))
    change(data, {"swap": {"a": str(D(1)), "b": str(late.date), "meal": "lunch"}})
    moved = plan_for(data, MONDAY).day(D(1))
    assert moved.lunch == late.lunch and moved.lunch_cook == D(1)
    # cooking it once more went over the repeat limit, which never refuses a dish asked for
    assert any("cooked 3 times" in e for e in validate(data, plan_for(data, MONDAY)))


def test_a_menu_change_that_breaks_a_hard_rule_is_refused_and_nothing_is_written(data_dir):
    data = planned(data_dir)
    path = plans_dir(data) / f"{MONDAY}.yaml"
    text = path.read_text()
    for op, why in (
        ({"set": {"date": str(D(2)), "meal": "dinner", "recipe": "fritura"}}, "at dinner"),
        (
            {"set": {"date": str(D(2)), "meal": "lunch", "recipe": "espinacas-con-garbanzos"}},
            "avoided ingredient",
        ),
        ({"set": {"date": str(D(2)), "meal": "dinner", "recipe": "guarnicion-0"}}, "side salad"),
        ({"set": {"date": "2031-01-01", "meal": "dinner", "recipe": "ensalada-0"}}, "no plan"),
    ):
        with pytest.raises(menu.MenuError, match=why):
            change(data, op)
        assert path.read_text() == text
    for raw, why in (
        ([{"set": {"date": str(D(2)), "meal": "dinner", "recipe": "nope"}}], "no recipe"),
        ([{"set": {"date": "soon", "meal": "dinner", "recipe": "fritura"}}], "date"),
        ([{"set": {"date": str(D(2)), "meal": "supper", "recipe": "fritura"}}], "not a meal"),
        ([{"regenerate": {"from": str(D(2))}}], "Monday"),
        ([{"pause": {"from": str(D(3)), "to": str(D(2))}}], "days"),
        ([{"pause": {"from": str(D(0)), "to": "2031-01-01"}}], "days"),
        ([{"undo": True}, {"pause": {"from": str(D(0)), "to": str(D(1))}}], "alone"),
        ([{"delete": {"date": str(D(2))}}], "not a menu change"),
    ):
        with pytest.raises(menu.MenuError, match=why):
            menu.parse(raw, data)


# ---------------------------------------------------------------- pause


def test_paused_days_have_no_dishes_no_shopping_and_no_batch(data_dir, tmp_path):
    data = planned(data_dir)
    db = DB(tmp_path / "x.db")
    raw = plan_for(data, MONDAY, raw=True)
    full = needs(data, db, raw, MONDAY)
    change(data, {"pause": {"from": str(D(1)), "to": str(D(6))}}, today=D(1))
    plan = plan_for(data, MONDAY)
    assert plan.day(D(0)) and plan.day(D(7))
    assert all(plan.day(D(i)) is None for i in range(1, 7))
    assert not [s for s in plan.sessions if D(1) <= s.date <= D(6)]  # nothing cooked meanwhile
    assert not [e for s in plan.sessions for x in s.dishes for e in x.eat if D(1) <= e <= D(6)]
    # the first day back still has its lunch, cooked that morning instead of during the pause
    back = plan.day(D(7))
    assert back.lunch == raw.day(D(7)).lunch and back.lunch_cook == D(7)
    assert any(s.date == D(7) and s.morning for s in plan.sessions)
    assert batch_sheet(data, db, plan, D(4)) == []
    less = needs(data, db, plan, MONDAY)
    assert sum(less.values()) < sum(full.values()) / 3  # only Monday is bought for
    assert plan_for(data, MONDAY, raw=True).days == raw.days  # the plan on disk is untouched


def test_a_pause_ends_by_itself_or_early_and_the_plan_is_back(data_dir):
    data = planned(data_dir)
    before = plan_for(data, MONDAY)
    change(data, {"pause": {"from": str(D(1)), "to": str(D(6))}})
    assert plan_for(data, D(7)).day(D(7)) is not None  # the day after the pause
    change(data, {"resume": {"from": str(D(4)), "to": str(D(6))}})
    assert paused_days(data) == {D(1), D(2), D(3)}
    plan = plan_for(data, MONDAY)
    assert plan.day(D(3)) is None and plan.day(D(4)) == before.day(D(4))
    change(data, {"resume": {"from": str(D(0)), "to": str(D(6))}})
    after = plan_for(data, MONDAY)
    assert after.days == before.days and after.sessions == before.sessions


def test_plan_reminders_stay_silent_on_paused_days(data_dir, tmp_path):
    data = bot_data(data_dir)
    raw = yaml.safe_load((data_dir / "settings.yaml").read_text())
    raw["reminders"] += [
        {"id": "lunch", "kind": "meal", "meal": "lunch", "at": ["13:00"]},
        {"id": "batch", "kind": "batch", "at": ["16:30"]},
        {"id": "shop", "kind": "shopping", "at": ["09:00"]},
    ]
    (data_dir / "settings.yaml").write_text(yaml.safe_dump(raw, allow_unicode=True))
    data = load(data_dir)
    b = FakeBot(data, DB(tmp_path / "b.db"), MONDAY)
    by = {r.id: r for r in data.settings.reminders}
    plan = plan_for(data, MONDAY, raw=True)
    cook = next(s.date for s in plan.sessions if not s.morning and s.date > MONDAY)
    frozen = next(
        d.date - dt.timedelta(days=1)
        for d in plan.days
        if (d.date - d.lunch_cook).days > data.recipes[d.lunch].fridge_days and d.date > D(1)
    )
    days = {"lunch": D(2), "batch": cook, "shop": D(2), "defrost": frozen, "water": D(2)}
    for rid, day in days.items():
        assert b.reminder_text(by[rid], day), rid  # they all speak while the plan runs
    change(data, {"pause": {"from": str(D(1)), "to": str(D(27))}})
    for rid, day in days.items():
        said = b.reminder_text(by[rid], day)
        assert (said is not None) == (rid == "water"), rid  # a plain message still goes
    assert b.day_text(D(2)) == b.t("paused_day")
    assert b.t("paused_short") in run(b.cmd_week("luis", "", {}))
    change(data, {"resume": {"from": str(D(1)), "to": str(D(27))}})
    for rid, day in days.items():
        assert b.reminder_text(by[rid], day), rid


def test_the_shopping_reminder_goes_when_part_of_the_week_is_still_planned(data_dir, tmp_path):
    data = bot_data(data_dir)
    raw = yaml.safe_load((data_dir / "settings.yaml").read_text())
    raw["reminders"].append({"id": "shop", "kind": "shopping", "at": ["09:00"]})
    (data_dir / "settings.yaml").write_text(yaml.safe_dump(raw, allow_unicode=True))
    data = load(data_dir)
    b = FakeBot(data, DB(tmp_path / "b.db"), MONDAY)
    shop = next(r for r in data.settings.reminders if r.id == "shop")
    change(data, {"pause": {"from": str(D(0)), "to": str(D(2))}})
    assert b.reminder_text(shop, D(0))  # Thursday to Sunday still need food
    change(data, {"pause": {"from": str(D(3)), "to": str(D(6))}})
    assert b.reminder_text(shop, D(0)) is None


def test_pages_show_a_paused_day_as_paused(data_dir, monkeypatch):
    monkeypatch.setenv("SMP_SCHEDULER", "0")
    data = planned(data_dir)
    change(data, {"pause": {"from": str(D(0)), "to": str(D(6))}})
    from supermealplanner.web import create_app

    t = Translator("en")
    with TestClient(create_app(data_dir)) as c:
        for path in (f"/?d={D(2)}", f"/week?d={MONDAY}", f"/list?d={MONDAY}", f"/batch?d={MONDAY}"):
            assert c.get(path).status_code == 200, path
        assert t("paused_day") in c.get(f"/?d={D(2)}").text
        assert c.get(f"/week?d={MONDAY}").text.count(t("paused_short")) == 7
        assert t("batch_none") in c.get(f"/batch?d={MONDAY}").text


# ---------------------------------------------------------------- regenerate and undo


def test_a_new_draw_from_a_monday_keeps_the_days_before_it(data_dir):
    data = planned(data_dir)
    old = plan_for(data, MONDAY, raw=True)
    change(data, {"regenerate": {"from": str(D(7))}})
    first = plan_for(data, MONDAY, raw=True)
    assert first.days == old.days[:7]
    assert not [e for s in first.sessions for x in s.dishes for e in x.eat if e >= D(7)]
    new = plan_for(data, D(7))
    assert new.start == D(7) and len(new.days) == 28 and validate(data, new) == []
    assert [d.lunch for d in new.days[:7]] != [d.lunch for d in old.days[7:14]]
    change(data, {"regenerate": {"from": str(D(7))}})  # asked again: another draw
    again = plan_for(data, D(7))
    assert again.seed != new.seed and again.days != new.days
    assert sorted(p.name for p in plans_dir(data).glob("*.yaml")) == [
        f"{MONDAY}.yaml",
        f"{D(7)}.yaml",
    ]
    # the hourly check finds the coming week covered and draws nothing on top
    from supermealplanner.scheduler import ensure_plan

    state = type("S", (), {"data": data, "today": lambda self: MONDAY})()
    assert ensure_plan(state, MONDAY) is None
    assert load_plan(plans_dir(data) / f"{D(7)}.yaml").days == again.days


def test_the_last_menu_change_can_be_undone_once(data_dir):
    data = planned(data_dir)
    before = plan_for(data, MONDAY)
    change(
        data, {"pause": {"from": str(D(1)), "to": str(D(6))}}, {"regenerate": {"from": str(D(7))}}
    )
    assert plan_for(data, D(7)).start == D(7) and paused_days(data)
    change(data, {"pause": {"from": str(D(1)), "to": str(D(6))}})  # already paused: no change
    change(data, {"undo": True})
    after = plan_for(data, MONDAY)
    assert not paused_days(data) and after.days == before.days and after.start == MONDAY
    assert [p.name for p in plans_dir(data).glob("*.yaml")] == [f"{MONDAY}.yaml"]
    with pytest.raises(menu.MenuError, match="no menu change to undo"):
        change(data, {"undo": True})


# ---------------------------------------------------------------- the bot


def test_a_change_the_person_asked_for_is_made_at_once(data_dir, tmp_path, monkeypatch):
    data = plugin_data(data_dir)
    monkeypatch.delenv("SMP_GIT_WRITE_TOKEN", raising=False)  # the menu needs no token
    b = ChatBot(data, DB(tmp_path / "b.db"))
    pause = {"pause": {"from": str(D(1)), "to": str(D(6))}}
    reply(data_dir, text=block({"asked": True, "plan": [pause]}))
    run(b.handle(None, msg("@chefbot esta semana comemos lo que hay")))
    said = ChatBot.texts(b, -100)
    assert len(said) == 2 and b.pending(-100) == []  # no yes/no round
    assert said[1].startswith("Done:") and "Menu paused from Tuesday 13 Oct" in said[1]
    assert "undo" in said[1] and "yes" not in said[1]
    assert paused_days(b.state.data) == {D(i) for i in range(1, 7)}
    reply(data_dir, text=block({"asked": True, "plan": [{"undo": True}]}))
    run(b.handle(None, msg("@chefbot deshaz eso")))
    assert paused_days(b.state.data) == set()


def test_a_change_nobody_asked_for_still_waits_for_a_yes(data_dir, tmp_path, monkeypatch):
    data = plugin_data(data_dir)
    monkeypatch.delenv("SMP_GIT_WRITE_TOKEN", raising=False)
    b = ChatBot(data, DB(tmp_path / "b.db"))
    pause = {"pause": {"from": str(D(1)), "to": str(D(6))}}
    for extra in ({}, {"asked": False}, {"asked": "true"}):  # only a plain true skips the round
        reply(data_dir, text=block({**extra, "plan": [pause]}))
        run(b.handle(None, msg("@chefbot ¿qué hay hoy?")))
        assert "yes" in ChatBot.texts(b, -100)[-1] and len(b.pending(-100)) == 1
        assert paused_days(b.state.data) == set()
    run(b.handle(None, msg("yes", **TO_BOT)))
    assert paused_days(b.state.data) == {D(i) for i in range(1, 7)}
    assert ChatBot.texts(b, -100)[-1] == b.t("menu_applied")


def test_an_asked_settings_change_is_committed_at_once_and_the_safety_rules_hold(
    data_dir, tmp_path, monkeypatch
):
    from .test_conversations import git

    remote = tmp_path / "data.git"
    git(tmp_path, "init", "--bare", "-q", "-b", "main", str(remote))
    data = plugin_data(data_dir)
    (data_dir / ".gitignore").write_text("state/\n*.db\nplugins/*.json*\n")
    git(data_dir, "init", "-q", "-b", "main")
    git(data_dir, "add", ".")
    git(data_dir, "commit", "-q", "-m", "data")
    git(data_dir, "remote", "add", "origin", str(remote))
    git(data_dir, "push", "-q", "-u", "origin", "main")
    monkeypatch.setenv("SMP_GIT_WRITE_TOKEN", "token")
    b = ChatBot(data, DB(tmp_path / "b.db"))
    swap = {"swap": {"a": str(D(2)), "b": str(D(5)), "meal": "dinner"}}
    was = plan_for(data, MONDAY).day(D(2)).dinner
    reply(
        data_dir,
        text=block({"asked": True, "set": {"household.lunch_time": "14:30"}, "plan": [swap]}),
    )
    run(b.handle(None, msg("@chefbot comemos a las 14:30 y cambia miércoles por sábado")))
    said = ChatBot.texts(b, -100)[-1]
    assert b.state.data.household.lunch_time == "14:30"
    assert plan_for(b.state.data, MONDAY).day(D(5)).dinner == was
    sha = git(data_dir, "rev-parse", "--short", "HEAD")
    assert said.startswith("Done:") and "Lunch time: 14:00 → 14:30" in said and sha in said
    body = git(remote, "log", "-1", "--format=%B")
    assert "Asked by Luis in the chat." in body and "confirmed" not in body
    assert "swap" not in body  # the menu is not in the data repo: the commit names settings only
    # asked or not, what the chat may never change stays refused
    for forbidden in (
        {"household.people.luis.pills": {}},
        {"household.people.luis.kcal.start": 1200},
        {"household.people.ana.avoid_ingredients": ["tomate"]},
        {"household.people.luis.avoid_ingredients": []},
        {"settings.reminders.pills": {"at": ["17:00"]}},
    ):
        reply(data_dir, text=block({"asked": True, "set": forbidden}))
        run(b.handle(None, msg("@chefbot hazlo")))
        assert ChatBot.texts(b, -100)[-1].startswith("I can't make that change")
    assert git(data_dir, "rev-parse", "--short", "HEAD") == sha


def test_a_refused_menu_change_says_why_and_strangers_change_nothing(data_dir, tmp_path):
    data = plugin_data(data_dir, group_open=True)
    b = ChatBot(data, DB(tmp_path / "b.db"))
    bad = {"set": {"date": str(D(2)), "meal": "dinner", "recipe": "fritura"}}
    reply(data_dir, text=block({"asked": True, "plan": [bad]}))
    run(b.handle(None, msg("@chefbot fritura el miércoles")))
    said = ChatBot.texts(b, -100)[-1]
    assert said.startswith("I can't make that change") and "at dinner" in said
    assert "Done" not in said
    pause = {"pause": {"from": str(D(1)), "to": str(D(6))}}
    reply(data_dir, text=block({"asked": True, "plan": [pause]}))
    run(b.handle(None, msg("@chefbot pausa", user=555)))
    assert ChatBot.texts(b, -100)[-1] == b.t("change_members_only")
    assert paused_days(b.state.data) == set()


def test_a_new_recipe_can_go_straight_into_the_menu(data):
    block_ = {
        "recipe": {
            "id": "sopa-nueva",
            "name": "Sopa nueva",
            "meals": ["dinner"],
            "kind": "quick",
            "ingredients": [{"id": "tomate", "qty": 200}],
            "nutrition": {"kcal": 300, "protein_g": 10},
        },
        "plan": [{"set": {"date": str(D(2)), "meal": "dinner", "recipe": "sopa-nueva"}}],
    }
    p = changes.parse(block_, data, "luis")
    assert "Sopa nueva" in changes.describe(p, data, Translator("en"))[-1]


def test_the_rules_teach_menu_changes_asked_and_research(data_dir, tmp_path):
    data = planned(data_dir)
    rules = context.rules(data)
    for needle in ("asked: true", "regenerate:", "pause:", "undo: true", "Never say you cannot"):
        assert needle in rules, needle
    assert "look it up" in rules and '"ask your doctor"' in rules and "source" in rules
    assert "only after they say yes" not in rules
    change(data, {"pause": {"from": str(D(1)), "to": str(D(2))}})
    facts = context.facts(data, DB(tmp_path / "x.db"), MONDAY)
    day = plan_for(data, MONDAY).day(D(3))
    assert f"({D(1)}): paused" in facts and f"[{day.snack}]" in facts and "## Next week" in facts
    assert "- fritura: Fritura; dinner; quick" in facts  # the ids a set may name


# ---------------------------------------------------------------- the whole day


def test_nibbles_are_other_snacks_never_a_treat_or_an_avoided_food(data_dir):
    data = snacks(data_dir)
    plan = plan_for(data, MONDAY)
    seen = set()
    for day in plan.days:
        picks = [r.id for r in nibbles(data, day)]
        assert picks == [r.id for r in nibbles(data, day)]  # the same day, the same list
        assert 1 <= len(picks) <= 3 and len(set(picks)) == len(picks)
        assert day.snack not in picks and "treat" not in picks and "batido-verde" not in picks
        seen |= set(picks)
    assert seen == {"fruta", "frutos-secos", "merienda-a"}


def test_the_week_and_the_day_show_breakfast_snack_and_nibbles(data_dir, monkeypatch, tmp_path):
    monkeypatch.setenv("SMP_SCHEDULER", "0")
    data = snacks(data_dir)
    day = plan_for(data, MONDAY).day(D(2))
    names = [data.recipes[x].name for x in (day.breakfast, day.snack)]
    picks = [r.name for r in nibbles(data, day)]
    from supermealplanner.web import create_app

    with TestClient(create_app(data_dir)) as c:
        week = c.get(f"/week?d={MONDAY}").text
        today = c.get(f"/?d={D(2)}").text
    row = week[week.index(f'href="/?d={D(2)}"') : week.index(f'href="/?d={D(3)}"')]
    for name in (*names, *picks, "Breakfast:", "Snack:", "To nibble:"):
        assert name in row, name
    assert "To nibble:" in today and all(p in today for p in picks)
    b = FakeBot(data, DB(tmp_path / "b.db"), MONDAY)
    assert "To nibble: " + ", ".join(picks) in b.day_text(D(2))
    assert "Para picar" == Translator("es")("nibble_title")
