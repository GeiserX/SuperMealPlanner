"""The follow-up that fixes what the review of a sent answer found (bot.verify_after): it keeps
what it does not correct, waits for a turn still being written, carries its cancels with the change
it offers, reads a change that waited for a yes as it stands when the review runs, and never gives
a yes to someone who did not write."""

import asyncio
import datetime as dt
import json

import yaml

from supermealplanner import verifier

from .conftest import MONDAY
from .test_reminders import ahead, block
from .test_settings_stores_bot import msg, run
from .test_turns import buttons, cfg, drain, sends, setup, tap
from .test_verifier import bad_dinner, lines, merluza_wednesday, say, vsetup
from .test_whose_yes import burst, waits_for

# ---------------------------------------------------------------- what the follow-up offers


def test_a_follow_up_that_corrects_one_reminder_leaves_the_others_offered(data_dir, tmp_path):
    """The 2 Nov case: three steps offered, the review finds only the fish time wrong. The soak
    and the cook must still be there to tap, or the soak is never set."""
    b, db = vsetup(data_dir, tmp_path)
    soak, _ = ahead(1, 22)
    fish, _ = ahead(2, 9)
    fish_ok, _ = ahead(2, 8)
    cook, _ = ahead(2, 18)
    first = block(
        (soak, "Remojar los garbanzos"), (fish, "Sacar el pescado"), (cook, "Cocinar la cena")
    )
    cfg(
        data_dir,
        text="Plan de mañana.\n" + first,
        revised="La hora del pescado: antes.\n" + block((fish_ok, "Sacar el pescado")),
        verdict=json.dumps({"timeline": ["The fish comes out too late."]}),
    )
    say(b, "@chefbot qué hago mañana", message_id=1)
    assert len(lines(data_dir, "turns.jsonl")) == 2  # the answer and its follow-up
    offers = [o for o in b._offers() if o["chat"] == -100]
    assert len(offers) == 2
    old, new = offers
    assert [r["text"] for r in new["items"]] == ["Sacar el pescado"]
    assert [r.get("done") for r in old["items"]] == [None, "replaced", None]
    # the soak of the first answer can still be tapped, and it is set
    run(tap(b, f"r:{old['key']}:0h", 7, 1))
    assert [r["text"] for r in db.reminders(-100)] == ["Remojar los garbanzos"]
    # the old fish button is gone: only the corrected one sets it
    run(tap(b, f"r:{old['key']}:1h", 7, 1))
    assert [r["text"] for r in db.reminders(-100)] == ["Remojar los garbanzos"]


# ---------------------------------------------------------------- cancels with a change


def test_a_follow_up_with_a_change_and_a_cancel_cancels_it_with_the_yes(data_dir, tmp_path):
    """Nothing is frozen after all: the thaw reminder goes, with the menu change it comes with."""
    b, db = vsetup(data_dir, tmp_path)
    _, thaw_at = ahead(3, 21)
    rid = db.add_reminder(-100, "ana", thaw_at, "Descongelar el pavo", 7)
    swap = yaml.safe_dump(
        {"plan": [{"set": {"date": "2026-10-16", "meal": "dinner", "recipe": "merluza-cena-0"}}]}
    )
    cfg(
        data_dir,
        text="Hecho.\n" + merluza_wednesday(),
        revised="Sin congelar: pavo fresco.\n```change\n"
        + swap
        + "```\n"
        + block(("cancel", str(rid))),
        verdict=json.dumps({"timeline": ["The thaw reminder counts on freezing."]}),
    )
    say(b, "@chefbot merluza el miércoles", message_id=1)
    assert len(lines(data_dir, "turns.jsonl")) == 2
    assert [r["id"] for r in db.reminders(-100)] == [rid]  # waits with the change
    asked = next(p for p in sends(b, -100) if p.get("reply_markup") and "Sin" not in p["text"])
    run(tap(b, buttons(asked)[0]["callback_data"], 7, 1))
    assert db.reminders(-100) == []  # cancelled with the yes


def test_a_no_to_the_follow_ups_change_keeps_its_reminders(data_dir, tmp_path):
    b, db = vsetup(data_dir, tmp_path)
    _, thaw_at = ahead(3, 21)
    rid = db.add_reminder(-100, "ana", thaw_at, "Descongelar el pavo", 7)
    swap = yaml.safe_dump(
        {"plan": [{"set": {"date": "2026-10-16", "meal": "dinner", "recipe": "merluza-cena-0"}}]}
    )
    cfg(
        data_dir,
        text="Hecho.\n" + merluza_wednesday(),
        revised="Pavo fresco.\n```change\n" + swap + "```\n" + block(("cancel", str(rid))),
        verdict=json.dumps({"timeline": ["x"]}),
    )
    say(b, "@chefbot merluza el miércoles", message_id=1)
    asked = next(p for p in sends(b, -100) if p.get("reply_markup") and "Pavo" not in p["text"])
    run(tap(b, buttons(asked)[1]["callback_data"], 7, 1))
    assert [r["id"] for r in db.reminders(-100)] == [rid]


# ---------------------------------------------------------------- a change still waiting


def test_a_follow_ups_change_joins_the_change_its_answer_left_waiting(data_dir, tmp_path):
    """The answer offers a change for a yes (a dish moved and a new ingredient); the review finds
    one thing, and the follow-up writes only that correction (what is at home of the new food).
    Both wait together for one yes: the correction never throws away what it corrects."""
    b, _db = vsetup(data_dir, tmp_path)
    b.state.today = lambda: MONDAY + dt.timedelta(days=1)
    new = {"name": "Basmati", "section": "despensa", "store": "mercado", "unit": "g"}
    first = {
        "plan": [{"set": {"date": "2026-10-14", "meal": "dinner", "recipe": "merluza-cena-1"}}],
        "ingredients": {"basmati": new},
    }
    cfg(
        data_dir,
        text="Una idea.\n```change\n" + yaml.safe_dump(first) + "```",
        revised="Y lo que hay.\n```change\n" + yaml.safe_dump({"pantry": {"basmati": 500}}) + "```",
        verdict=json.dumps({"timeline": ["Say how much basmati is at home."]}),
    )
    say(b, "@chefbot qué cenamos el miércoles", message_id=1)
    assert len(lines(data_dir, "turns.jsonl")) == 2  # the answer and its follow-up
    (waiting,) = [i for i in b.pending(-100) if "change" in i]
    assert waiting["change"]["plan"] == first["plan"]
    assert waiting["change"]["ingredients"] == {"basmati": new}
    assert waiting["change"]["pantry"] == {"basmati": 500}


def test_a_step_the_follow_up_says_was_done_is_recorded_before_any_tap(data_dir, tmp_path):
    """The follow-up's change says a step was done (the household said so) and moves a dish: the
    step is done at once, and only the dish waits for a yes. A fact never waits for a tap, so a
    change nobody taps cannot leave a done step as not done."""
    from supermealplanner import tasks

    b, db = vsetup(data_dir, tmp_path)
    b.state.today = lambda: MONDAY + dt.timedelta(days=1)
    key = tasks.tasks_for(b.data, db, MONDAY, MONDAY + dt.timedelta(days=6))[0].key
    db.mark_task(key, "not_done", "ana")
    move = {"plan": [{"set": {"date": "2026-10-14", "meal": "dinner", "recipe": "merluza-cena-1"}}]}
    cfg(
        data_dir,
        text="Hecho.\n" + merluza_wednesday(),
        revised="Corregido.\n```change\n"
        + yaml.safe_dump({**move, "tasks": {key: "done"}})
        + "```",
        verdict=json.dumps({"timeline": ["The cook said the step was done."]}),
    )
    say(b, "@chefbot merluza el miércoles", message_id=1)
    assert len(lines(data_dir, "turns.jsonl")) == 2
    assert tasks.state(db, key) == tasks.DONE  # before any tap
    (waiting,) = [i for i in b.pending(-100) if "change" in i]
    assert "tasks" not in waiting["change"] and waiting["change"]["plan"] == move["plan"]


# ---------------------------------------------------------------- a turn still being written


def test_a_review_that_ends_while_the_next_turn_is_written_waits_for_it(
    data_dir, tmp_path, monkeypatch
):
    """Answer A is reviewed while the cook writes again: its findings are about a state B may
    change, so nothing follows A alone; one review of A and B, after B, makes one follow-up."""
    b, _db = vsetup(data_dir, tmp_path)
    checks = []

    async def complete(data, system, messages):
        checks.append(messages[0]["content"])
        if len(checks) == 1:  # the cook writes while A's review runs: B is queued
            b.take(
                -100, {"id": 7, "first_name": "L"}, "luis", {"message_id": 2}, "y el jueves?", True
            )
        return json.dumps({"timeline": [f"problem {len(checks)}"]})

    monkeypatch.setattr(verifier.llm, "complete", complete)
    cfg(
        data_dir,
        text="Hecho A.\n" + merluza_wednesday(),
        revised="Hecho B.\n" + merluza_wednesday(),
    )

    async def go():
        await b.handle(None, msg("@chefbot merluza el miércoles", message_id=1))
        await drain(b)
        await b.drive(None, -100)  # B's turn, then the review of both
        await drain(b)

    run(go())
    turns = [t["new"] for t in lines(data_dir, "turns.jsonl")]
    notes = [t for t in turns if any(b.t("verify_who") in x for x in t)]
    assert len(notes) == 1  # one follow-up, after B
    assert len(turns) == 3 and turns[2] is notes[0]
    assert len(checks) == 2 and "Hecho B." in checks[1]  # the second review covers B too
    assert "problem 1" not in notes[0][-1] and "problem 2" in notes[0][-1]


# ---------------------------------------------------------------- a change that waited


def test_a_change_refused_while_its_review_ran_is_not_checked(data_dir, tmp_path, monkeypatch):
    """The person taps No while the review runs: it reviews again without that change, and no
    follow-up fixes or offers again a change nobody made."""
    b, _db = vsetup(data_dir, tmp_path, ahead=[{"step": "Asar el pimiento", "session": True}])
    checks = []
    gate = asyncio.Event()

    async def complete(data, system, messages):
        checks.append(messages[0]["content"])
        if len(checks) == 1:
            await gate.wait()
        return "{}"

    monkeypatch.setattr(verifier.llm, "complete", complete)
    waiting = bad_dinner().replace("asked: true\n", "")
    cfg(data_dir, text="Te propongo ensalada.\n" + waiting)

    async def go():
        await b.handle(None, msg("@chefbot qué cenamos el viernes", message_id=1))
        while not checks:
            await asyncio.sleep(0.01)
        asked = next(p for p in sends(b, -100) if p.get("reply_markup"))
        await tap(b, buttons(asked)[1]["callback_data"], 7, 1)  # No
        gate.set()
        await drain(b)

    run(go())
    assert "the session that day comes after the meal" in checks[0]
    assert len(checks) == 2 and "the session that day comes after the meal" not in checks[1]
    assert len(lines(data_dir, "turns.jsonl")) == 1  # no follow-up


def test_two_changes_waiting_are_both_checked(data_dir, tmp_path, monkeypatch):
    b, db = vsetup(data_dir, tmp_path, ahead=[{"step": "Asar el pimiento", "session": True}])

    async def complete(data, system, messages):
        return "{}"

    monkeypatch.setattr(verifier.llm, "complete", complete)
    first = yaml.safe_load(bad_dinner()[10:-3])
    first.pop("asked")
    second = yaml.safe_load(merluza_wednesday()[10:-3])
    second.pop("asked")
    answers = [
        {"text": "a", "change": first, "made": False, "offered": []},
        {"text": "b", "change": second, "made": False, "offered": []},
    ]
    found = run(verifier.after(b.data, db, MONDAY, {}, answers))
    assert any("the session that day comes after the meal" in p for p in found)


# ---------------------------------------------------------------- whose yes


def third_person(data_dir):
    """Eva: the cook, in the household and on the bot, who says nothing in the burst."""
    raw = yaml.safe_load((data_dir / "household.yaml").read_text())
    eva = {k: v for k, v in raw["people"][0].items() if k != "pills"}
    eva.update(id="eva", name="Eva")
    raw["people"].append(eva)
    (data_dir / "household.yaml").write_text(yaml.safe_dump(raw, allow_unicode=True))


def eva_setup(data_dir, tmp_path):
    third_person(data_dir)
    return setup(data_dir, tmp_path, reminders_for="eva", people={"luis": 7, "ana": 8, "eva": 9})


def own_change(pid, foods):
    body = {"asked": True, "set": {f"household.people.{pid}.avoid_ingredients": foods}}
    return "```change\n" + yaml.safe_dump(body) + "```"


def test_a_cook_who_did_not_write_never_gets_the_yes(data_dir, tmp_path):
    b, _ = eva_setup(data_dir, tmp_path)
    cfg(data_dir, text="Comida a las 14:30.\n" + block_lunch())
    asked = burst(b, 8, 7)  # Ana first, Luis newest, Eva silent
    assert b.mention("ana") in asked["text"] and b.mention("eva") not in asked["text"]


def block_lunch():
    return "```change\n" + yaml.safe_dump({"set": {"household.lunch_time": "14:30"}}) + "```"


def test_nobody_changes_the_silent_cooks_own_foods_in_her_name(data_dir, tmp_path):
    b, _ = eva_setup(data_dir, tmp_path)
    cfg(data_dir, text="Añadido.\n" + own_change("eva", ["espinaca"]))
    b.debounce = 0.3
    made = []

    async def apply(pending, asked=False):  # what would be written in the data repo
        made.append(pending)
        pending["applied"] = True
        return "ok"

    b.apply = apply

    async def go():
        first = asyncio.create_task(b.handle(None, msg("@chefbot hola", user=8, message_id=1)))
        await asyncio.sleep(0.1)
        await b.handle(None, msg("@chefbot que Eva no coma espinaca", user=7, message_id=2))
        await first
        await drain(b)

    run(go())
    assert made == []
    assert not any(9 in (i.get("by") or []) for i in b.pending(-100))


def test_in_a_mixed_burst_a_person_still_changes_their_own_foods_with_their_yes(data_dir, tmp_path):
    b, _ = setup(data_dir, tmp_path, reminders_for="ana")
    cfg(data_dir, text="Añadido.\n" + own_change("luis", ["espinaca", "lechuga"]))
    asked = burst(b, 8, 7)  # Ana (the cook) first, Luis asks for his own
    assert b.mention("luis") in asked["text"]
    waits_for(b, asked, "luis", 8)  # his yes, never Ana's: not refused, not made in her name
