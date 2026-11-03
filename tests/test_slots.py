"""One consistent message per slot, settled before it goes out. Reminders of one chat within
MERGE_S of each other go out as one message; when several answers each set their own for that time,
the steps repeat and contradict each other and the cook cannot tell which to follow. The answer is
not held back for it: a slot that holds several, or one the state puts in doubt (a step nobody
marked done, food that ran out), is rewritten by one AI pass up to half an hour before it goes out,
and goes out as it was set when that pass fails."""

import asyncio
import datetime as dt
import json

from supermealplanner import bot as botmod
from supermealplanner import llm, pantry, tasks, verifier

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


# ---------------------------------------------------------------- the answer is not held back


def test_an_answer_that_sets_a_reminder_next_to_another_is_not_sent_back_for_it(
    data_dir, tmp_path, monkeypatch
):
    """Answer quickly, settle the slot later: the slot is rewritten before it goes out, so the
    review does not spend a revision round on two reminders at about the same time."""
    b, db = setup(data_dir, tmp_path)
    day = soon()
    db.add_reminder(-100, "ana", at(day, "21:30"), "Freeze the beef and the salmon", 7)
    offered = [{"when": when(day, "21:40"), "text": "Keep 1 green pepper, cut the rest", "set": 1}]

    async def judged(*a, **k):  # the judgment finds nothing: only the app's own check counts
        return {}

    monkeypatch.setattr(verifier, "judge", judged)
    turn = {"chat": {"id": -100}}
    assert (
        run(verifier.findings(b.data, db, MONDAY, turn, "Done.", None, offered, asker="ana")) == []
    )


# ---------------------------------------------------------------- when it goes out


def fake_complete(monkeypatch, answer, calls=None, waits=None):
    async def complete(data, system, messages, timeout=None):
        if calls is not None:
            calls.append(messages[0]["content"])
        if waits is not None:
            waits.append(timeout)
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


def test_a_rewrite_starts_early_enough_for_a_job_of_minutes_and_tells_the_provider_its_time(
    data_dir, tmp_path, monkeypatch
):
    """A provider that runs a job per call takes minutes: the rewrite starts half an hour before
    the slot, and the provider is told how long the app waits, so its job stops by then."""
    b, db = setup(data_dir, tmp_path)
    day = soon()
    a = db.add_reminder(-100, "ana", at(day, "21:30"), "Freeze the beef", 7)
    c = db.add_reminder(-100, "ana", at(day, "21:40"), "Keep 1 green pepper", 7)
    calls, waits = [], []
    fake_complete(monkeypatch, {"steps": [{"text": "x", "ids": [a, c]}]}, calls, waits)
    clock(monkeypatch, at(day, "21:00"))
    run(b.prepare_slots())
    assert len(calls) == 1
    (wait,) = waits
    assert wait == min(30 * 60 - botmod.SLOT_MARGIN_S, verifier.review_timeout(b.data))
    assert botmod._slot_key([db.reminder(a), db.reminder(c)]) in db.get_kv(botmod.SLOTS)


def test_the_provider_gets_the_shorter_wait_as_its_timeout(data_dir, tmp_path, monkeypatch):
    b, _ = setup(data_dir, tmp_path)
    seen = []

    def plugin(system, messages, settings):
        seen.append(settings["timeout_s"])
        return "ok"

    monkeypatch.setattr(llm, "_plugin", lambda data: plugin)
    run(llm.complete(b.data, "s", [{"role": "user", "content": "m"}], timeout=300.7))
    run(llm.complete(b.data, "s", [{"role": "user", "content": "m"}]))
    assert seen == [300, b.data.settings.llm.timeout_s]


def test_two_steps_that_name_the_same_reminder_have_buttons_of_their_own(
    data_dir, tmp_path, monkeypatch
):
    """One reminder that said two things becomes two steps: a tap on one marks only what it says,
    and the buttons of that reminder alone in another message never take over the step's."""
    b, db = setup(data_dir, tmp_path)
    day = soon()
    a = db.add_reminder(-100, "ana", at(day, "21:30"), "Freeze the beef and keep 1 pepper", 7)
    c = db.add_reminder(-100, "ana", at(day, "21:40"), "Keep 1 pepper", 7)
    steps = [
        {"text": "Freeze the beef", "ids": [a]},
        {"text": "Keep 2 peppers, 1 for X and 1 for Y", "ids": [a, c]},
    ]
    fake_complete(monkeypatch, {"steps": steps})
    clock(monkeypatch, at(day, "21:26"))
    run(b.prepare_slots())
    clock(monkeypatch, at(day, "21:30") + 30)
    run(b.tick_once(None))
    (sent,) = sends(b, -100)
    data = [x["callback_data"] for x in buttons(sent)]
    assert len(set(data)) == 4
    press(b, sent, "1. ✅ Done", 7)
    assert tasks.state(db, f"r:{a}") == tasks.DONE
    assert tasks.state(db, f"r:{c}") == tasks.UNKNOWN
    b.task_buttons(b.once_tasks([db.reminder(a)]))  # the morning asks about it on its own
    press(b, sent, "2. ✅ Done", 7)
    assert tasks.state(db, f"r:{c}") == tasks.DONE


def test_chat_reminders_after_the_apps_own_time_are_rewritten_for_its_message(
    data_dir, tmp_path, monkeypatch
):
    """The app's 21:30 message takes in the chat's reminders up to 22:00: the slot is the one it
    sends, even when no chat reminder is due at 21:30."""
    b, db = setup(data_dir, tmp_path)
    s = evening_session(b.data)
    b.data.settings.reminders = [BATCH]
    a = db.add_reminder(-100, "ana", at(s.date, "21:45"), "Keep 1 green pepper", 7)
    c = db.add_reminder(-100, "ana", at(s.date, "21:50"), "Keep 1 green pepper, cut the rest", 7)
    calls = []
    steps = [{"text": "Keep 2 green peppers; cut the rest", "ids": [a, c]}]
    fake_complete(monkeypatch, {"steps": steps}, calls)
    clock(monkeypatch, at(s.date, "21:20"))
    run(b.prepare_slots())
    assert len(calls) == 1
    clock(monkeypatch, at(s.date, "21:30") + 10)
    tick(b, s.date, "21:30", BATCH)
    (sent,) = sends(b, -100)
    assert "Batch cooking today" in sent["text"]
    assert "Keep 2 green peppers; cut the rest" in sent["text"]
    assert "Keep 1 green pepper" not in sent["text"]
    assert {r["id"] for r in db.reminders(-100, "sent")} == {a, c}


def test_food_that_ran_out_puts_a_lone_reminder_about_its_dish_in_doubt(data_dir, tmp_path):
    b, db = setup(data_dir, tmp_path)
    rec = next(
        r for r in b.data.recipes.values() if any(i.id in b.data.ingredients for i in r.ingredients)
    )
    iid = next(i.id for i in rec.ingredients if i.id in b.data.ingredients)
    day = soon()
    rid = db.add_reminder(
        -100, "ana", at(day, "21:30"), "Cook it", 7, rec.id, day.isoformat(), "cook"
    )
    pantry.correct(b.data, db, {iid: 500})
    assert not any("ran out" in x for x in b.slot_doubts([db.reminder(rid)]))
    pantry.correct(b.data, db, {iid: 0})  # it ran out
    (doubt,) = [x for x in b.slot_doubts([db.reminder(rid)]) if "ran out" in x]
    assert b.data.ingredients[iid].name in doubt


def test_the_rewrite_reads_what_the_chat_said_after_the_reminders_were_set(data_dir, tmp_path):
    """The cook said nothing would be frozen after the thaw reminder was set: only the chat says
    so, and the rewrite reads it."""
    b, db = setup(data_dir, tmp_path)
    day = soon()
    rid = db.add_reminder(-100, "ana", at(day, "21:30"), "Thaw the turkey", 7)
    before = db.add_chat(-100, "user", "ana", "Freeze everything")
    after = db.add_chat(-100, "user", "ana", "Nothing gets frozen")
    set_at = dt.datetime.now(dt.UTC) - dt.timedelta(hours=1)
    with db.conn() as conn:
        conn.execute(
            "UPDATE chat SET ts=? WHERE id=?",
            ((set_at - dt.timedelta(hours=1)).isoformat(timespec="seconds"), before),
        )
        conn.execute("UPDATE reminder SET created=? WHERE id=?", (set_at.isoformat(), rid))
    assert after
    facts = b.slot_facts([db.reminder(rid)], [])
    assert "ana: Nothing gets frozen" in facts
    assert "Freeze everything" not in facts


# ---------------------------------------------------------------- what the rewrite may not do


def test_a_rewrite_with_ids_or_dropped_that_are_not_lists_is_not_used():
    """The model can answer a number or true where a list of ids goes: the rewrite is unusable,
    never an error that stops the other slots, and true is never reminder 1."""
    step = {"text": "Freeze it"}
    assert botmod._slot_answer(json.dumps({"steps": [step | {"ids": 1}]}), {1}) is None
    assert botmod._slot_answer(json.dumps({"steps": [step | {"ids": [True]}]}), {1}) is None
    got = {"steps": [step | {"ids": [1]}], "dropped": 2}
    assert botmod._slot_answer(json.dumps(got), {1, 2}) is None
    ok = {"steps": [step | {"ids": [1]}], "dropped": [2]}
    assert botmod._slot_answer(json.dumps(ok), {1, 2}) == {
        "steps": [{"text": "Freeze it", "ids": [1]}],
        "dropped": [2],
    }


def test_a_slot_that_cannot_be_checked_goes_out_as_it_was_and_the_next_one_is_still_rewritten(
    data_dir, tmp_path, monkeypatch
):
    b, db = setup(data_dir, tmp_path)
    day = soon()
    db.add_reminder(-100, "ana", at(day, "21:30"), "Freeze the beef", 7)
    db.add_reminder(-100, "ana", at(day, "21:40"), "Soak the chickpeas", 7)
    c = db.add_reminder(-200, "ana", at(day, "21:50"), "Keep 1 green pepper", 7)
    d = db.add_reminder(-200, "ana", at(day, "21:55"), "Keep 1 green pepper", 7)
    tried = []

    def doubts(rs):
        tried.append({r["id"] for r in rs})
        if c not in tried[-1]:
            raise ValueError("broken plan")
        return []

    monkeypatch.setattr(b, "slot_doubts", doubts)
    calls = []
    fake_complete(monkeypatch, {"steps": [{"text": "Keep 1 green pepper", "ids": [c, d]}]}, calls)
    clock(monkeypatch, at(day, "21:26"))
    run(b.prepare_slots())
    assert len(calls) == 1  # the other chat's slot was still reached
    assert list(db.get_kv(botmod.SLOTS)) == [botmod._slot_key(db.reminders(-200))]
    run(b.prepare_slots())
    assert len(tried) == 2  # the broken slot is not tried again at every tick


def test_the_rewrite_keeps_doses_word_for_word_and_never_cuts_food(data_dir, tmp_path, monkeypatch):
    b, db = setup(data_dir, tmp_path)
    day = soon()
    db.add_reminder(-100, "ana", at(day, "21:30"), "Freeze the beef", 7)
    db.add_reminder(-100, "ana", at(day, "21:40"), "Soak the chickpeas", 7)
    systems = []

    async def complete(data, system, messages, timeout=None):
        systems.append(system)
        return "not json"

    monkeypatch.setattr(botmod.llm, "complete", complete)
    clock(monkeypatch, at(day, "21:26"))
    run(b.prepare_slots())
    (system,) = systems
    assert "dose is kept word for word" in system and "never dropped" in system
    assert "never suggest eating less after a binge" in system
    assert "this is for food only" in system
