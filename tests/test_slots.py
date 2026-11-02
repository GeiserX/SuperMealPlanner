"""One reminder per slot, checked when it goes out. Reminders of one chat within MERGE_S of each
other go out as one message; when several answers each set their own for that time, the steps
repeat and contradict each other and the cook cannot tell which to follow. An answer that adds a
reminder to a slot is sent back to write the slot again as one; a slot that still holds several,
or one the state puts in doubt (a step nobody marked done), is rewritten by one AI pass just before
it goes out, and goes out as it was set when that pass fails."""

import asyncio
import datetime as dt
import json

from supermealplanner import bot as botmod
from supermealplanner import llm, tasks, verifier

from .conftest import MONDAY
from .test_chains import at
from .test_reminders import setup
from .test_settings_stores_bot import run
from .test_task_done import BATCH, at_clock, evening_session, press, tick
from .test_turns import buttons, sends


def soon(days=2):
    return dt.date.today() + dt.timedelta(days=days)


def when(day, hm):
    return f"{day.isoformat()} {hm}"


# ---------------------------------------------------------------- the answer: one per slot


def test_an_answer_that_adds_a_reminder_to_a_slot_is_sent_back_to_write_it_as_one(
    data_dir, tmp_path, monkeypatch
):
    b, db = setup(data_dir, tmp_path)
    day = soon()
    old = db.add_reminder(-100, "ana", at(day, "21:30"), "Freeze the beef and the salmon", 7)
    offered = [{"when": when(day, "21:40"), "text": "Keep 1 green pepper, cut the rest", "set": 1}]

    async def judged(*a, **k):  # the judgment finds nothing: only the app's own check counts
        return {}

    monkeypatch.setattr(verifier, "judge", judged)
    reply = llm.Reply(text="Done.")
    reply.reminders = offered
    turn = {"chat": {"id": -100}}
    found = run(verifier.findings(b.data, db, MONDAY, turn, reply, None, "ana"))
    (problem,) = found
    assert "2 reminders would go out together at about 21:30" in problem
    assert f"reminder {old} (already set): «Freeze the beef and the salmon»" in problem
    assert "listed in this answer: «Keep 1 green pepper, cut the rest»" in problem
    assert f"cancel {old} and list one reminder" in problem
    assert "1 for X and 1 for Y" in problem  # amounts split explicitly
    assert problem.endswith("end its line with set.")


def test_the_revision_that_cancels_the_others_and_writes_one_passes(data_dir, tmp_path):
    b, db = setup(data_dir, tmp_path)
    day = soon()
    old = db.add_reminder(-100, "ana", at(day, "21:30"), "Freeze the beef", 7)
    one = {"when": when(day, "21:30"), "text": "Freeze the beef; keep 1 green pepper", "set": 1}
    assert verifier.crowded(b.data, db, -100, [{"cancel": str(old)}, one]) == []
    # two in one answer for one time are the same problem, with nothing to cancel
    two = [one, {"when": when(day, "21:45"), "text": "Soak the chickpeas"}]
    (problem,) = verifier.crowded(b.data, db, -100, [{"cancel": str(old)}, *two])
    assert "cancel" not in problem.split("One time holds")[1].split("list one")[0]
    assert "end its line with set" not in problem


def test_steps_for_different_dishes_keep_their_own_reminders(data_dir, tmp_path):
    """The chains read each reminder by its dish field and kind: a soak folded into a thaw would
    leave one of them missing, so those stay apart; anything else is folded in."""
    b, db = setup(data_dir, tmp_path)
    day = soon()
    db.add_reminder(-100, "ana", at(day, "21:30"), "Soak", 7, "lentils", "2030-01-02", "soak")
    thaw = {
        "when": when(day, "21:35"),
        "text": "Thaw the hake",
        "recipe": "hake",
        "date": "2030-01-03",
        "kind": "thaw",
    }
    assert verifier.crowded(b.data, db, -100, [thaw]) == []
    note = {"when": when(day, "21:40"), "text": "Keep 1 green pepper"}
    assert len(verifier.crowded(b.data, db, -100, [thaw, note])) == 1  # the note is folded in
    far = {"when": when(day, "23:30"), "text": "Shop"}
    assert verifier.crowded(b.data, db, -100, [far]) == []  # another slot


# ---------------------------------------------------------------- when it goes out


def fake_complete(monkeypatch, answer, calls=None):
    async def complete(data, system, messages):
        if calls is not None:
            calls.append(messages[0]["content"])
        if isinstance(answer, Exception):
            raise answer
        if answer == "slow":
            await asyncio.sleep(5)
        return answer if isinstance(answer, str) else json.dumps(answer)

    monkeypatch.setattr(botmod.llm, "complete", complete)


def clock(monkeypatch, t):
    monkeypatch.setattr(botmod.time, "time", lambda: t)


def test_a_slot_of_several_reminders_goes_out_as_its_rewrite_with_one_button_per_step(
    data_dir, tmp_path, monkeypatch
):
    b, db = setup(data_dir, tmp_path)
    day = soon()
    a = db.add_reminder(-100, "ana", at(day, "21:30"), "Freeze the beef, turkey and salmon", 7)
    c = db.add_reminder(-100, "ana", at(day, "21:30"), "Keep 1 green pepper, cut the rest", 7)
    d = db.add_reminder(-100, "ana", at(day, "21:45"), "Keep 1 green pepper, cut the rest", 7)
    calls = []
    fake_complete(
        monkeypatch,
        {
            "steps": [
                {"text": "Freeze the beef, the turkey and the salmon", "ids": [a]},
                {"text": "Keep 2 green peppers, 1 for X and 1 for Y; cut the rest", "ids": [c, d]},
            ],
            "dropped": [],
        },
        calls,
    )
    clock(monkeypatch, at(day, "21:26"))  # a few minutes before
    run(b.prepare_slots())
    assert len(calls) == 1
    assert f"- {c}: " in calls[0] and "Keep 1 green pepper" in calls[0]  # the slot, with ids
    clock(monkeypatch, at(day, "21:30") + 30)
    run(b.tick_once(None))
    (sent,) = sends(b, -100)
    assert sent["text"] == (
        "⏰ Ana\n"
        "1. Freeze the beef, the turkey and the salmon\n"
        "2. Keep 2 green peppers, 1 for X and 1 for Y; cut the rest"
    )
    assert [x["text"] for x in buttons(sent)] == [
        "1. ✅ Done",
        "1. ✗ Not done",
        "2. ✅ Done",
        "2. ✗ Not done",
    ]
    press(b, sent, "2. ✅ Done", 7)  # the step says two reminders: both are done
    assert tasks.state(db, f"r:{c}") == tasks.DONE
    assert tasks.state(db, f"r:{d}") == tasks.DONE
    assert tasks.state(db, f"r:{a}") == tasks.UNKNOWN
    assert {r["id"] for r in db.reminders(-100, "sent")} == {a, c, d}
    run(b.prepare_slots())
    assert len(calls) == 1  # tried once per slot


def test_a_step_the_rewrite_drops_is_cancelled_once_the_message_is_out(
    data_dir, tmp_path, monkeypatch
):
    b, db = setup(data_dir, tmp_path)
    day = soon()
    a = db.add_reminder(-100, "ana", at(day, "21:30"), "Nothing gets frozen tonight", 7)
    c = db.add_reminder(-100, "ana", at(day, "21:40"), "Thaw the turkey on Friday", 7)
    fake_complete(
        monkeypatch, {"steps": [{"text": "Nothing gets frozen", "ids": [a]}], "dropped": [c]}
    )
    clock(monkeypatch, at(day, "21:27"))
    run(b.prepare_slots())
    clock(monkeypatch, at(day, "21:30") + 10)
    run(b.tick_once(None))
    (sent,) = sends(b, -100)
    assert sent["text"] == "⏰ Ana: Nothing gets frozen"
    assert len(buttons(sent)) == 2
    assert db.reminder(a)["state"] == "sent" and db.reminder(c)["state"] == "cancelled"


def test_a_slot_that_changed_after_its_rewrite_goes_out_as_it_was_set(
    data_dir, tmp_path, monkeypatch
):
    b, db = setup(data_dir, tmp_path)
    day = soon()
    a = db.add_reminder(-100, "ana", at(day, "21:30"), "Freeze the beef", 7)
    c = db.add_reminder(-100, "ana", at(day, "21:40"), "Freeze the beef too", 7)
    fake_complete(monkeypatch, {"steps": [{"text": "Freeze the beef", "ids": [a, c]}]})
    clock(monkeypatch, at(day, "21:26"))
    run(b.prepare_slots())
    db.add_reminder(-100, "ana", at(day, "21:50"), "Soak the chickpeas", 7)  # after the rewrite
    clock(monkeypatch, at(day, "21:30") + 10)
    run(b.tick_once(None))
    (sent,) = sends(b, -100)
    assert sent["text"] == (
        "⏰ Ana\n1. Freeze the beef\n2. Freeze the beef too\n3. Soak the chickpeas"
    )


def test_a_rewrite_that_fails_times_out_or_misses_a_reminder_leaves_the_slot_as_it_was(
    data_dir, tmp_path, monkeypatch
):
    monkeypatch.setattr(botmod, "SLOT_TIMEOUT_S", 0.05)
    for n, answer in enumerate(
        [
            llm.LLMError("down"),
            "slow",
            "not json",
            {"steps": [{"text": "Freeze it", "ids": [1]}]},  # the second reminder in nothing
        ]
    ):
        (tmp_path / str(n)).mkdir()
        b, db = setup(data_dir, tmp_path / str(n))
        day = soon()
        db.add_reminder(-100, "ana", at(day, "21:30"), "Freeze the beef", 7)
        db.add_reminder(-100, "ana", at(day, "21:40"), "Soak the chickpeas", 7)
        fake_complete(monkeypatch, answer)
        clock(monkeypatch, at(day, "21:26"))
        run(b.prepare_slots())
        assert db.get_kv(botmod.SLOTS) is None
        clock(monkeypatch, at(day, "21:30") + 10)
        run(b.tick_once(None))
        (sent,) = sends(b, -100)
        assert sent["text"] == "⏰ Ana\n1. Freeze the beef\n2. Soak the chickpeas"
        assert len(buttons(sent)) == 4


def test_one_reminder_with_nothing_against_it_is_not_rewritten(data_dir, tmp_path, monkeypatch):
    b, db = setup(data_dir, tmp_path)
    day = soon()
    db.add_reminder(-100, "ana", at(day, "21:30"), "Freeze the beef", 7)
    calls = []
    fake_complete(monkeypatch, {"steps": []}, calls)
    clock(monkeypatch, at(day, "21:26"))
    run(b.prepare_slots())
    assert calls == []


def test_a_step_counting_on_a_soak_nobody_marked_done_is_rewritten_with_an_if(
    data_dir, tmp_path, monkeypatch
):
    """The cook step counted on chickpeas soaked the night before; the soak reminder went out,
    nobody said it was done, and the cook had missed it among many messages."""
    b, db = setup(data_dir, tmp_path)
    s = evening_session(b.data)
    dish = s.dishes[0]
    eat = dish.eat[0]
    eve = s.date - dt.timedelta(days=1)
    soak = db.add_reminder(
        -100, "ana", at(eve, "21:30"), "Soak the chickpeas", 7, dish.recipe, str(eat), "soak"
    )
    db.settle_reminder(soak, "sent")
    cook = db.add_reminder(
        -100, "ana", at(s.date, "18:00"), "Cook the chickpeas", 7, dish.recipe, str(eat), "cook"
    )
    calls = []
    text = "If you soaked the chickpeas last night, cook them; if not, soak them now"
    fake_complete(monkeypatch, {"steps": [{"text": text, "ids": [cook]}]}, calls)
    clock(monkeypatch, at(s.date, "17:56"))
    real = botmod.dt.datetime
    botmod.dt.datetime = at_clock(s.date, "17:56")
    try:
        doubts = b.slot_doubts([db.reminder(cook)])
        run(b.prepare_slots())
    finally:
        botmod.dt.datetime = real
    (doubt,) = [x for x in doubts if f"r:{soak}" in x]
    assert f"Reminder {cook} counts on step r:{soak} (soak," in doubt
    assert "nobody marked it done" in doubt
    assert len(calls) == 1 and doubt in calls[0]
    clock(monkeypatch, at(s.date, "18:00") + 10)
    run(b.tick_once(None))
    (sent,) = sends(b, -100)
    assert sent["text"].startswith(f"⏰ Ana: {text}\n")  # then the recipe link
    # once the soak is marked done there is nothing against the cook step
    db.mark_task(f"r:{soak}", tasks.DONE, "ana")
    botmod.dt.datetime = at_clock(s.date, "17:56")
    try:
        assert b.slot_doubts([db.reminder(cook)]) == []
    finally:
        botmod.dt.datetime = real


def test_the_apps_message_carries_the_slots_rewrite_and_keeps_its_own_blocks(
    data_dir, tmp_path, monkeypatch
):
    b, db = setup(data_dir, tmp_path)
    s = evening_session(b.data)
    a = db.add_reminder(-100, "ana", at(s.date, "21:30"), "Keep 1 green pepper", 7)
    c = db.add_reminder(-100, "ana", at(s.date, "21:40"), "Keep 1 green pepper for Friday", 7)
    fake_complete(monkeypatch, {"steps": [{"text": "Keep 2 green peppers", "ids": [a, c]}]})
    clock(monkeypatch, at(s.date, "21:26"))
    run(b.prepare_slots())
    tick(b, s.date, "21:30", BATCH)
    (sent,) = sends(b, -100)
    assert "Batch cooking today" in sent["text"]  # the app's own block as it was
    jobs = b.batch_jobs(s.date)
    assert f"{len(jobs) + 1}. Keep 2 green peppers" in sent["text"]
    assert "Keep 1 green pepper" not in sent["text"]
    assert len(buttons(sent)) == 2 * (len(jobs) + 1)
    assert {r["id"] for r in db.reminders(-100, "sent")} == {a, c}
