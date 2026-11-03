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
    assert 'marked "automatic: nobody wrote this"' in b.rules()


# ---------------------------------------------------------------- the plan check's turn


def plan_turns(b, monkeypatch):
    taken = []
    b.take = lambda chat, user, who, m, text, asked, **k: taken.append(text)

    async def drive(client, chat):
        pass

    b.drive = drive
    return taken


def test_the_plan_turn_comes_once_a_day_even_for_a_new_plan_and_not_for_the_same_findings(
    data_dir, tmp_path, monkeypatch
):
    from .test_verifier import D, dinner

    b, db = vsetup(data_dir, tmp_path)
    taken = plan_turns(b, monkeypatch)
    _calls, real = judged({"timeline": ["Nothing cooks the chickpeas for Wednesday."]})
    monkeypatch.setattr(verifier.llm, "complete", real)
    run(verifier.check_plan(b.data, db, MONDAY))
    run(b.raise_plan(None, at_once=True))
    assert taken == [b.t("plan_check_turn")]
    dinner(b.data, D(3), "merluza-cena-1")  # a new plan the same day waits for tomorrow
    run(verifier.check_plan(b.data, db, MONDAY))
    run(b.raise_plan(None, at_once=True))
    assert len(taken) == 1
    # another day, the same findings about this plan: nothing new to say
    raised = db.get_kv("verifier.plan_raised")
    db.set_kv("verifier.plan_raised", {**raised, "on": "2000-01-01", "key": "old"})
    run(b.raise_plan(None, at_once=True))
    assert len(taken) == 1


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
