"""The bot stays quiet by construction: its own turns (the weekly review, the plan check, a step not
done, a follow-up) are never reviewed into another turn, never rewrite a recipe nobody asked about,
the plan check speaks at most once a day and only for what goes wrong in the kitchen or for health,
a follow-up comes only for those findings, and a reminder listed again at its own time keeps its
place with the new words instead of being cancelled and offered again."""

import asyncio
import datetime as dt
import json

import yaml

from supermealplanner import verifier

from .conftest import MONDAY
from .test_reminders import ZONE, ahead, block, is_offer
from .test_settings_stores_bot import msg, run
from .test_turns import cfg, sends
from .test_verifier import judged, lines, merluza_wednesday, say, vsetup


def own_turn(b, start):
    async def go():
        start()
        await b.drive(None, -100)
        await asyncio.gather(*b.tasks)

    run(go())


# ---------------------------------------------------------------- automatic turns never chain


def test_the_bots_own_turns_are_never_reviewed_into_another_turn(data_dir, tmp_path):
    """A plan check's turn whose answer changes the menu, with a review that would find a real
    problem: no review runs and no follow-up turn follows, so one automatic message stays one."""
    b, _db = vsetup(data_dir, tmp_path)
    cfg(
        data_dir,
        text="Propongo la merluza el miércoles.\n" + merluza_wednesday(),
        verdict=json.dumps({"timeline": ["Thaw the hake the night before."]}),
    )
    who = {"first_name": b.t("plan_check_who")}
    own_turn(b, lambda: b.take(-100, who, None, None, b.t("plan_check_turn"), True, review=True))
    own_turn(b, lambda: b.review(-100, MONDAY))
    own_turn(b, lambda: b.reorganise(None, -100, None, b.t("task_unanswered_turn", tasks="x")))
    assert len(lines(data_dir, "turns.jsonl")) == 3  # the three turns, no follow-up
    assert lines(data_dir, "checks.jsonl") == []
    # control: the same answer to someone's message is reviewed and followed up
    say(b, "@chefbot merluza el miércoles", message_id=9)
    assert len(lines(data_dir, "checks.jsonl")) == 1
    assert len(lines(data_dir, "turns.jsonl")) == 5


# ---------------------------------------------------------------- what brings a follow-up


def test_a_follow_up_comes_only_for_what_goes_wrong_in_the_kitchen_or_for_health(
    data_dir, tmp_path
):
    """Wish and wording polish brings no message of its own; a timeline problem does."""
    b, _db = vsetup(data_dir, tmp_path)
    polish = {"wishes": ["Say «sesión» instead of «batch»."], "clarity": ["Name the pan size."]}
    cfg(data_dir, text="Hecho.\n" + merluza_wednesday(), verdict=json.dumps(polish))
    say(b, "@chefbot merluza el miércoles", message_id=1)
    assert len(lines(data_dir, "checks.jsonl")) == 1  # reviewed
    assert len(lines(data_dir, "turns.jsonl")) == 1  # and nothing follows
    for check in ("timeline", "health", "nonsense"):
        assert check in verifier.ACT
    assert "wishes" not in verifier.ACT and "clarity" not in verifier.ACT
    assert "Only timeline, health and nonsense" in verifier.VERIFY


# ---------------------------------------------------------------- no recipe edits by itself


def test_an_automatic_turn_never_edits_a_recipe(data_dir, tmp_path):
    """The plan check's turn rewrote recipes nobody asked about; the menu move it also proposed is
    all that waits for a yes."""
    b, _db = vsetup(data_dir, tmp_path)
    rid = "merluza-cena-1"
    block_ = {
        "plan": [{"set": {"date": "2026-10-14", "meal": "dinner", "recipe": rid}}],
        "recipes": {rid: {"name": "Otro nombre"}},
    }
    cfg(data_dir, text="Propongo esto.\n```change\n" + yaml.safe_dump(block_) + "```")
    who = {"first_name": b.t("plan_check_who")}
    own_turn(b, lambda: b.take(-100, who, None, None, b.t("plan_check_turn"), True, review=True))
    (waiting,) = b.pending(-100)
    assert set(waiting["change"]) == {"plan"}
    assert not any("Otro nombre" in p["text"] for p in sends(b, -100))
    # the same block from someone's message keeps its recipe edit: they asked
    cfg(data_dir, text="Hecho.\n```change\n" + yaml.safe_dump(block_) + "```")
    say(b, "@chefbot cambia el nombre y la cena", message_id=5)
    assert any("recipes" in (w.get("change") or {}) for w in b.pending(-100))


def test_the_rules_tell_the_ai_what_an_automatic_turn_may_touch(data_dir, tmp_path):
    b, _db = vsetup(data_dir, tmp_path)
    assert "marks as automatic, one nobody wrote" in b.rules()
    assert "automatic: nobody wrote this" not in b.rules()  # by meaning: the turns say it in es too


# ---------------------------------------------------------------- the plan check's turn


def plan_turns(b, monkeypatch):
    taken = []
    b.take = lambda chat, user, who, m, text, asked, **k: taken.append(text)

    async def drive(client, chat):
        pass

    b.drive = drive
    return taken


def test_a_new_plan_is_raised_once_and_never_for_the_same_findings(data_dir, tmp_path, monkeypatch):
    """A plan the app draws the same day as another one's turn still gets its own turn when it
    finds something else: a soak it needs tonight cannot wait for tomorrow's check."""
    from .test_verifier import D, dinner

    b, db = vsetup(data_dir, tmp_path)
    taken = plan_turns(b, monkeypatch)
    _calls, real = judged({"timeline": ["Nothing cooks the chickpeas for Wednesday."]})
    monkeypatch.setattr(verifier.llm, "complete", real)
    run(verifier.check_plan(b.data, db, MONDAY))
    run(b.raise_plan(None, at_once=True))
    assert taken == [b.t("plan_check_turn")]
    run(b.raise_plan(None, at_once=True))  # the same plan: raised once
    assert len(taken) == 1
    # a new plan the same day, with findings of its own: raised now, not tomorrow
    dinner(b.data, D(3), "merluza-cena-1")
    _calls, other = judged({"timeline": ["Nothing thaws the hake for Thursday."]})
    monkeypatch.setattr(verifier.llm, "complete", other)
    run(verifier.check_plan(b.data, db, MONDAY))
    run(b.raise_plan(None, at_once=True))
    assert len(taken) == 2
    # another day, another plan, the same findings: nothing new to say
    raised = db.get_kv("verifier.plan_raised")
    db.set_kv("verifier.plan_raised", {**raised, "on": "2000-01-01", "key": "old"})
    run(b.raise_plan(None, at_once=True))
    assert len(taken) == 2


def test_the_plan_turn_never_comes_for_wording_alone(data_dir, tmp_path, monkeypatch):
    b, db = vsetup(data_dir, tmp_path)
    taken = plan_turns(b, monkeypatch)
    _calls, polish = judged({"clarity": ["Name the pan size."], "wishes": ["More garlic."]})
    monkeypatch.setattr(verifier.llm, "complete", polish)
    run(verifier.check_plan(b.data, db, MONDAY))
    assert verifier.plan_findings(b.data, db, MONDAY)  # the AI still sees them among its facts
    run(b.raise_plan(None, at_once=True))
    assert taken == []


# ---------------------------------------------------------------- a reminder in other words


def test_a_reminder_listed_again_at_its_time_keeps_its_place_with_the_new_words(data_dir, tmp_path):
    """The answer cancels tonight's dinner reminder and offers one at the same time that adds an
    egg: the reminder stays set, with the new words, and nothing is offered again."""
    b, db = vsetup(data_dir, tmp_path)
    when, at = ahead(1, 22)
    person = b._remind_for("ana")
    rid = db.add_reminder(-100, person, at, "Heat the rice tray", 7)
    other, other_at = ahead(2, 14)
    cfg(
        data_dir,
        text="Apuntado.\n"
        + block(
            ("cancel", str(rid)),
            (when, "Heat the rice tray, with 1 boiled egg each", "ana"),
            (other, "Add the last 2 eggs to the salad", "ana"),
        ),
    )
    run(b.handle(None, msg("@chefbot I boiled 6 eggs", message_id=1)))
    (kept,) = db.reminders(-100)
    assert (kept["id"], kept["text"]) == (rid, "Heat the rice tray, with 1 boiled egg each")
    assert dt.datetime.fromtimestamp(kept["at"], ZONE).hour == 22
    said = "\n".join(p["text"] for p in sends(b, -100))
    assert "Reminder removed" not in said and "Reminder kept, with new words" in said
    (offer,) = [p for p in sends(b, -100) if is_offer(p)]
    offered = offer["text"].split("shall I remind you?")[1]
    assert "Add the last 2 eggs" in offered and "boiled egg each" not in offered
    # a cancel with nothing at its time is still a cancel
    cfg(data_dir, text="Fuera.\n" + block(("cancel", str(rid))))
    run(b.handle(None, msg("@chefbot no rice tonight", message_id=2)))
    assert db.reminders(-100) == [] and db.reminder(rid)["state"] == "cancelled"
    assert other_at  # the other one was only offered


def test_a_reminder_at_the_same_time_for_another_dish_is_not_a_reword(data_dir, tmp_path):
    """Tonight's soak for one dish cancelled, a step of another dish listed at the same minute:
    two steps, so the old one is removed (and said so) and the new one waits for its tap."""
    b, db = vsetup(data_dir, tmp_path)
    when, at = ahead(1, 21)
    person = b._remind_for("ana")
    day = when[:10]
    b._links = lambda recipe, date: True  # both dishes are on the menu that day
    rid = db.add_reminder(-100, person, at, "Soak the chickpeas", 7, "ensalada-garbanzos", day)
    cfg(
        data_dir,
        text="Cambiado.\n"
        + block(
            ("cancel", str(rid)),
            (when, "Take the hake out of the freezer", "ana", f"dish merluza-cena-1 {day}"),
        ),
    )
    run(b.handle(None, msg("@chefbot no chickpeas, hake instead", message_id=1)))
    assert db.reminder(rid)["state"] == "cancelled"
    assert db.reminder(rid)["text"] == "Soak the chickpeas"
    said = "\n".join(p["text"] for p in sends(b, -100))
    assert "Reminder removed" in said and "Reminder kept, with new words" not in said
    assert any("Take the hake out" in p["text"] for p in sends(b, -100) if is_offer(p))


def test_a_reword_carries_the_new_dish_link(data_dir, tmp_path):
    b, db = vsetup(data_dir, tmp_path)
    _when, at = ahead(1, 21)
    rid = db.add_reminder(-100, "ana", at, "Soak", 7)
    new = {"at": at, "person": "ana", "text": "Soak the chickpeas", "recipe": "ensalada-garbanzos"}
    new |= {"date": "2026-10-14", "kind": "soak"}
    left, items, _said = b.reword(-100, [rid], [new], 7, True)
    assert (left, items) == ([], [])
    r = db.reminder(rid)
    assert (r["state"], r["text"]) == ("pending", "Soak the chickpeas")
    assert (r["recipe"], r["date"], r["kind"]) == ("ensalada-garbanzos", "2026-10-14", "soak")


def test_an_automatic_turn_rewords_a_reminder_only_on_a_tap(data_dir, tmp_path):
    """A follow-up that lists a reminder again in other words changes nothing until someone taps
    yes on the offer; then the reminder takes the new words in place, with no second one."""
    from .test_reminders import mid_of, offer_of
    from .test_turns import buttons, tap

    b, db = vsetup(data_dir, tmp_path)
    when, at = ahead(1, 21)
    rid = db.add_reminder(-100, b._remind_for("ana"), at, "Soak the beans and the chickpeas", 7)
    cfg(
        data_dir,
        text="Corrijo.\n" + block(("cancel", str(rid)), (when, "Soak the chickpeas", "ana")),
    )
    who = {"first_name": b.t("verify_who"), "id": 8}
    own_turn(b, lambda: b.take(-100, who, "ana", None, "x", True, followup=True))
    r = db.reminder(rid)
    assert (r["state"], r["text"]) == ("pending", "Soak the beans and the chickpeas")
    said = "\n".join(p["text"] for p in sends(b, -100))
    assert "Reminder removed" not in said and "Reminder kept" not in said
    offer = offer_of(b)
    (here,) = buttons(offer)
    run(tap(b, here["callback_data"], 8, mid_of(b, offer)))
    (kept,) = db.reminders(-100)
    assert (kept["id"], kept["text"]) == (rid, "Soak the chickpeas")


# ---------------------------------------------------------------- the checkups an answer brings


def test_a_change_made_with_its_step_offered_says_nothing_is_missing(data_dir, tmp_path):
    """The answer moves a dish that needs a soak and offers the soak reminder in the same message:
    the checkup counts the offer, so no «left something to fix» line names that soak."""
    from .test_verifier import soak

    b, _db = vsetup(data_dir, tmp_path)
    b.state.data = soak(data_dir, kind="quick")  # cooked for the dinner: only the soak is needed
    move = {
        "asked": True,
        "plan": [{"set": {"date": "2026-10-14", "meal": "dinner", "recipe": "ensalada-garbanzos"}}],
    }
    soak_line = (
        "2026-10-13 20:00",
        "Soak the chickpeas",
        "ana",
        "dish ensalada-garbanzos 2026-10-14 soak",
    )
    cfg(data_dir, text="Hecho.\n```change\n" + yaml.safe_dump(move) + "```\n" + block(soak_line))
    run(b.handle(None, msg("@chefbot chickpea salad on Wednesday", message_id=1)))
    said = "\n".join(p["text"] for p in sends(b, -100))
    assert "Done: the menu" in said
    assert "left something to fix" not in said


# ---------------------------------------------------------------- a change it cannot make


def test_an_automatic_turns_change_that_cannot_be_made_is_told_only_to_the_ai(data_dir, tmp_path):
    """The plan check's turn marks a new ingredient as at home: the ingredient is dropped (nobody
    asked for it), so the pantry line points at nothing. The chat hears no «couldn't make that
    change» right after the text; the AI reads why in the chat's record."""
    from .test_conversations import refusals

    b, db = vsetup(data_dir, tmp_path)
    block_ = {
        "ingredients": {
            "pasta-corta": {"name": "Pasta corta", "section": "pantry", "store": "store"}
        },
        "pantry": {"pasta-corta": 500},
    }
    cfg(data_dir, text="Lo apunto.\n```change\n" + yaml.safe_dump(block_) + "```")
    who = {"first_name": b.t("plan_check_who")}
    own_turn(b, lambda: b.take(-100, who, None, None, b.t("plan_check_turn"), True, review=True))
    said = "\n".join(p["text"] for p in sends(b, -100))
    assert "Lo apunto." in said
    assert b.t("change_invalid") not in said
    assert any("no ingredient pasta-corta" in r for r in refusals(db))


def test_a_person_who_adds_an_ingredient_and_has_it_at_home_sees_the_change(data_dir, tmp_path):
    """Someone says they have a food the data lacks: the answer adds it and sets it in the pantry.
    The yes it waits for names both, instead of the turn failing on the new id."""
    b, _db = vsetup(data_dir, tmp_path)
    section = next(iter(b.data.ingredients.values())).section
    block_ = {
        "ingredients": {
            "pasta-corta": {"name": "Pasta corta", "section": section, "store": "store"}
        },
        "pantry": {"pasta-corta": 500},
    }
    cfg(data_dir, text="La añado.\n```change\n" + yaml.safe_dump(block_) + "```")
    run(b.handle(None, msg("@chefbot tengo pasta corta", message_id=1)))
    said = "\n".join(p["text"] for p in sends(b, -100))
    assert "Something went wrong" not in said
    assert "Pasta corta" in said and "500 g" in said


# ---------------------------------------------------------------- the morning line


def test_the_morning_line_leaves_food_going_off_to_the_ai(data_dir, tmp_path):
    """Lots going off are many and change every day: the AI sees them with what each dish takes,
    and the morning line never lists them."""
    from supermealplanner import pantry

    from .test_pantry import at as p_at
    from .test_pantry import keeps

    b, db = vsetup(data_dir, tmp_path)
    b.state.data = keeps(data_dir, espinaca=3)
    db.tick(MONDAY, "espinaca", True)
    pantry.correct(b.data, db, {"espinaca": 200}, p_at(0))
    assert [p for p in run(b.problems()) if p.kind == "going_off"]
    run(b.checkup(None, -100))
    assert not [p for p in sends(b, -100) if "Espinaca" in p["text"]]
