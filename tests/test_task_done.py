"""Done or not done: every message that reminds of a task carries its two buttons, the answer is
stored with the task and a later one (a tap, or words through a change block) replaces it, a "not
done" (or no answer by the next morning for a step a dish counts on) starts a turn that reorganises
the week, and nothing counts a task as done until someone said so. The hake test is a Saturday
session nobody did, and on Sunday the bot spoke of its containers as if they existed."""

import datetime as dt

from supermealplanner import bot as botmod
from supermealplanner import context, doctor, tasks, verifier
from supermealplanner.model import Reminder
from supermealplanner.planner import Plans, plan_for

from .conftest import MONDAY
from .test_chains import SALAD, at, remind
from .test_menu import change
from .test_reminders import setup
from .test_settings_stores_bot import run
from .test_turns import buttons, cfg, drain, lines, sends, toasts
from .test_verifier import dinner, soak


def D(n: int) -> dt.date:
    return MONDAY + dt.timedelta(days=n)


def at_clock(day: dt.date, hm: str):
    """botmod's clock stopped at that local time."""
    h, m = (int(x) for x in hm.split(":"))

    class Clock(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return dt.datetime(day.year, day.month, day.day, h, m, tzinfo=tz)

    return Clock


def tick(b, day: dt.date, hm: str, *reminders: Reminder):
    """The reminders of the settings due at that time, sent as the clock would."""
    b.data.settings.reminders = list(reminders)
    b.due = lambda now: [(r, hm) for r in reminders]
    real = botmod.dt.datetime
    botmod.dt.datetime = at_clock(day, hm)
    try:

        async def go():
            await b.tick_reminders(None)
            await drain(b)

        run(go())
    finally:
        botmod.dt.datetime = real


def press(b, params, label: str, user: int, mid: int = 1001):
    """A tap on the button with that label, from the message those params sent."""
    btn = next(x for x in buttons(params) if x["text"] == label)
    query = {
        "id": "q",
        "from": {"id": user, "first_name": "X"},
        "message": {
            "message_id": mid,
            "chat": {
                "id": params["chat_id"],
                "type": "group" if params["chat_id"] < 0 else "private",
            },
            "reply_markup": params["reply_markup"],
        },
        "data": btn["callback_data"],
    }

    async def go():
        await b.tapped(None, query)
        await drain(b)

    run(go())


def markups(b):
    return [p for m, p in b.calls if m == "editMessageReplyMarkup"]


def labels(params):
    return [x["text"] for row in params["reply_markup"]["inline_keyboard"] for x in row]


BATCH = Reminder(id="batch", kind="batch", at=["21:30"])
DAY = Reminder(id="day", kind="day", at=["08:00"])
DEFROST = Reminder(id="defrost", kind="defrost", at=["21:00"])


def evening_session(data):
    return next(s for s in plan_for(data, MONDAY).sessions if not s.morning and s.date > MONDAY)


def test_the_batch_reminder_carries_done_and_not_done_and_the_same_tap_again_changes_nothing(
    data_dir, tmp_path
):
    b, db = setup(data_dir, tmp_path, group_open=True)
    s = evening_session(b.data)
    tick(b, s.date, "21:30", BATCH)
    (msg,) = [p for p in sends(b, -100) if "Batch cooking today" in p["text"]]
    key = tasks.session_key(s.date, False)
    jobs = [x.key for x in b.batch_jobs(s.date)]
    assert key in jobs
    data = [x["callback_data"] for x in buttons(msg)]
    assert all(d.startswith("d:") and len(d.encode()) <= 64 for d in data)
    assert len(data) == 2 * len(jobs)  # one row per task: done, not done
    many = len(jobs) > 1
    done = ("1. " if many else "") + "✅ Done"
    press(b, msg, done, 50)  # a stranger let in by group_open: nothing
    assert toasts(b)[-1] == "Only the household can answer this."
    assert tasks.state(db, key) == tasks.UNKNOWN
    press(b, msg, done, 7)
    assert tasks.state(db, key) == tasks.DONE
    assert db.task_states([key]) == {key: "done"}
    pre = "1. " if many else ""
    assert labels(markups(b)[-1])[:2] == [pre + "✓ Done", pre + "✗ Not done"]  # both stay
    assert any(r["text"].startswith("✓ Done: ") for r in db.recent_chat(-100, 5))
    logged = len(db.recent_chat(-100, 50))
    press(b, markups(b)[-1], pre + "✓ Done", 8)  # the same answer again: nothing new
    assert tasks.state(db, key) == tasks.DONE
    assert len(db.recent_chat(-100, 50)) == logged
    assert not lines(data_dir, "turns.jsonl")  # and no turn


def test_not_done_then_done_after_all_changes_the_answer_and_the_lunches_count_again(
    data_dir, tmp_path
):
    """The reminder goes out before the step: Ana taps Not done at 21:30 because she has not
    started, cooks at 23:00 and taps Done. The answer follows her, and the week is checked again."""
    b, db = setup(data_dir, tmp_path)
    cfg(data_dir, text="Fine.")
    s = evening_session(b.data)
    tick(b, s.date, "21:30", BATCH)
    (msg,) = [p for p in sends(b, -100) if "Batch cooking today" in p["text"]]
    key = tasks.session_key(s.date, False)
    pre = "1. " if len(b.batch_jobs(s.date)) > 1 else ""
    press(b, msg, pre + "✗ Not done", 7)
    assert tasks.state(db, key) == tasks.NOT_DONE
    assert labels(markups(b)[-1])[:2] == [pre + "✅ Done", pre + "✓ Not done"]
    later = s.date + dt.timedelta(days=1)
    eaten = {e for x in s.dishes for e in x.eat if e >= later}
    assert {f"uncooked:{e}" for e in eaten} <= {p.key for p in doctor.problems(b.data, db, later)}
    press(b, markups(b)[-1], pre + "✅ Done", 7)
    assert tasks.state(db, key) == tasks.DONE
    found = {p.key for p in doctor.problems(b.data, db, later)}
    assert not {f"uncooked:{e}" for e in eaten} & found
    turns = lines(data_dir, "turns.jsonl")
    assert "Task not done" in turns[0]["new"][-1]
    assert "Task done after all" in turns[-1]["new"][-1] and key in turns[-1]["new"][-1]


def test_an_answer_in_words_is_stored_by_a_change_block_and_a_wrong_key_is_refused(
    data_dir, tmp_path
):
    """The household answers the morning's question in a message: the AI records it with `tasks`,
    and unknown takes it back. A key the task list does not have changes nothing."""
    b, db = setup(data_dir, tmp_path)
    s = evening_session(b.data)
    key = tasks.session_key(s.date, False)
    b.state.today = lambda: s.date + dt.timedelta(days=1)

    def answer(raw):
        pending = {"change": {"asked": True, **raw}, "asker": "ana", "chat": -100, "name": "Ana"}
        return run(b.apply(pending, asked=True))

    said = answer({"tasks": {key: "done"}})
    assert tasks.state(db, key) == tasks.DONE and said
    answer({"tasks": {key: "unknown"}})
    assert tasks.state(db, key) == tasks.UNKNOWN
    assert (
        answer({"tasks": {"cook:2020-01-01:pm": "done"}})
        == "I couldn't make that change; nothing was changed."
    )
    assert tasks.state(db, "cook:2020-01-01:pm") == tasks.UNKNOWN


def test_a_session_not_done_starts_a_turn_and_its_lunches_are_uncooked(data_dir, tmp_path):
    b, db = setup(data_dir, tmp_path)
    cfg(data_dir, text="Then we cook on Monday.")
    s = evening_session(b.data)
    tick(b, s.date, "21:30", BATCH)
    (msg,) = [p for p in sends(b, -100) if "Batch cooking today" in p["text"]]
    key = tasks.session_key(s.date, False)
    first = buttons(msg)[1]["text"]  # the session is the first task: its "not done"
    press(b, msg, first, 8)
    assert tasks.state(db, key) == tasks.NOT_DONE
    (turn,) = lines(data_dir, "turns.jsonl")
    asked = turn["new"][-1]
    assert "Task not done" in asked and key in asked and "Ana says" in asked
    assert sends(b, -100)[-1]["text"] == "Then we cook on Monday."
    later = s.date + dt.timedelta(days=1)
    eaten = {e for x in s.dishes for e in x.eat if e >= later}
    found = {p.key for p in doctor.problems(b.data, db, later)}
    assert eaten and {f"uncooked:{e}" for e in eaten} <= found


def test_on_sunday_an_unmarked_saturday_session_is_asked_and_its_containers_are_ifs(
    data_dir, tmp_path
):
    """The hake: Tuesday's lunch counts on the Saturday session (frozen till then). Nobody marked
    the session; on Sunday the day message asks, a turn starts, and the freezer line says "if"."""
    b, db = setup(data_dir, tmp_path)
    cfg(data_dir, text="Was Saturday's cooking done?")
    tuesday = D(8)
    change(
        b.data,
        {
            "set": {
                "date": str(tuesday),
                "meal": "lunch",
                "recipe": "pescado-batch",
                "cook": str(D(5)),
            }
        },
    )
    key = tasks.session_key(D(5), False)
    assert key in {x.key for x in b.kitchen_tasks(D(5), D(5))}
    tick(b, D(6), "08:00", DAY)
    msg = sends(b, -100)[0]
    assert "Nobody said whether these were done:" in msg["text"]
    assert "Only if the cooking session of" in msg["text"]  # today's lunch, from Friday's
    assert any(x["callback_data"].startswith("d:") for x in buttons(msg))
    (turn,) = lines(data_dir, "turns.jsonl")
    asked = turn["new"][-1]  # after the day message itself
    assert "Tasks not answered" in asked and key in asked
    # Monday evening's freezer line: only if the session was done, until someone says so
    said = b.reminder_text(DEFROST, D(7))
    assert "Only if the cooking session of" in said
    ask = {p.key for p in doctor.problems(b.data, db, D(6))}
    assert f"unconfirmed:{key}" in ask
    # the plan check leaves it to the morning's own turn: no second turn about the same step
    assert "Nobody said whether the task" not in verifier.plan_brief(b.data, db, D(6))[0]
    # asked once: the next morning's day message does not ask again, and no second turn starts
    seen = len(sends(b, -100))
    tick(b, D(7), "08:00", DAY)
    (day,) = [p for p in sends(b, -100)[seen:] if p["text"].startswith("📅")]
    first = msg["text"].split("Nobody said whether these were done:")[1].strip().splitlines()
    again = day["text"].partition("Nobody said whether these were done:")[2]
    asked_before = [x.split(". ", 1)[-1] for x in first]
    assert asked_before and not any(x in again for x in asked_before)
    assert all(key not in t["new"][-1] for t in lines(data_dir, "turns.jsonl")[1:])
    db.mark_task(key, "done", "ana")
    assert "Only if" not in b.reminder_text(DEFROST, D(7))
    assert f"unconfirmed:{key}" not in {p.key for p in doctor.problems(b.data, db, D(6))}


def test_a_chat_reminder_carries_the_buttons_and_an_unmarked_cook_counts_as_not_made(
    data_dir, tmp_path, monkeypatch
):
    """The beans: a cook set from the chat for Saturday, nobody marked it. On Sunday the salad
    does not count it as cooked; marked not done it has no cook; marked done it is clean."""
    _b, db = setup(data_dir, tmp_path)
    data = soak(data_dir)
    monday = D(7)
    dinner(data, monday)
    soaked = remind(db, D(5), "10:00", SALAD, monday, "soak")
    db.mark_task(f"r:{soaked}", "done", "ana")  # the soak went out before, and was done
    cook = remind(db, D(5), "16:00", SALAD, monday, "cook")
    b = _b
    b.state.data = data
    r = db.reminder(cook)
    monkeypatch.setattr(botmod.time, "time", lambda: r["at"] + 30)
    db.settle_reminder(soaked, "sent")
    run(b.tick_once(None))
    (msg,) = [p for p in sends(b, -100) if p["text"].startswith("⏰ Ana: x")]
    assert [x["text"] for x in buttons(msg)] == ["✅ Done", "✗ Not done"]

    def found(today=None):
        today = today or D(6)
        return {p.key for p in doctor.problems(data, db, today) if SALAD in p.key or "r:" in p.key}

    assert found(D(5)) == set()  # on the day itself it still counts
    assert found() == {f"unconfirmed:r:{cook}"}
    db.mark_task(f"r:{cook}", "not_done", "ana")
    assert f"no_cook:{SALAD}:{monday}" in found()
    db.mark_task(f"r:{cook}", "done", "ana")  # a later answer replaces the first
    assert found() == set()


def test_the_facts_say_which_tasks_are_done_not_done_or_unknown(data_dir, tmp_path):
    b, db = setup(data_dir, tmp_path)
    found = [x for x in tasks.tasks_for(b.data, db, MONDAY, D(13)) if x.kind == "cook"]
    one, two, three = found[:3]
    db.mark_task(one.key, "done", "ana")
    db.mark_task(two.key, "not_done", "luis")
    text = context.facts(b.data, db, MONDAY)
    assert "## Tasks" in text
    rows = [x for x in text.splitlines() if x.startswith('- {"key"')]
    by = {x.split('"key": "')[1].split('"')[0]: x for x in rows}
    assert '"state": "done"' in by[one.key]
    assert '"state": "not_done"' in by[two.key]
    assert '"state": "unknown"' in by[three.key]


def test_the_morning_turn_is_only_about_steps_a_dish_still_counts_on(data_dir, tmp_path):
    """Nobody taps for days: the day message lists what nobody answered, once, but the turn names
    only the steps doctor's unconfirmed says something still hangs on, and none at all when nothing
    does. The plan check leaves those to the morning, so it raises no second turn about them."""
    b, db = setup(data_dir, tmp_path)
    cfg(data_dir, text="Ok.")
    for n in range(1, 11):
        seen = len(lines(data_dir, "turns.jsonl"))
        tick(b, D(n), "08:00", DAY)
        hangs = {
            p.key.removeprefix("unconfirmed:")
            for p in doctor.problems(b.data, db, D(n))
            if p.kind == "unconfirmed"
        }
        new = lines(data_dir, "turns.jsonl")[seen:]
        for turn in new:
            asked = turn["new"][-1]
            named = {k for k in asked.split("(")[1:] for k in [k.split(")")[0]]}
            assert named <= hangs | {""}, (D(n), named, hangs)
        if not hangs:
            assert not new


def test_nothing_from_before_the_buttons_is_asked_or_doubted(data_dir, tmp_path):
    """The first morning after the update: the sessions of the last days never had buttons, so the
    day message asks about none of them and no container says "if"."""
    b, db = setup(data_dir, tmp_path)
    cfg(data_dir, text="Ok.")
    db.set_kv(tasks.SINCE, D(6).isoformat())
    tick(b, D(6), "08:00", DAY)
    assert all("Nobody said whether" not in p["text"] for p in sends(b, -100))
    assert not lines(data_dir, "turns.jsonl")
    assert not [p for p in doctor.problems(b.data, db, D(6)) if p.kind == "unconfirmed"]


def test_a_freezer_lunch_from_an_old_session_carries_its_buttons_and_the_check_agrees(
    data_dir, tmp_path
):
    """A lunch eaten on D11 from the D2 session: nine days later the day message still says "if"
    of it, so it carries that session's buttons, and the consistency check says the same."""
    b, db = setup(data_dir, tmp_path)
    change(
        b.data,
        {
            "set": {
                "date": str(D(11)),
                "meal": "lunch",
                "recipe": "pescado-batch",
                "cook": str(D(2)),
            }
        },
    )
    key = next(k for k in tasks.session_keys(Plans(b.data), Plans(b.data).day(D(11))))
    tick(b, D(11), "08:00", DAY)
    msg = next(p for p in sends(b, -100) if p["text"].startswith("📅"))
    assert "Only if the cooking session of" in msg["text"]
    sid = next(k for k, v in db.get_kv(botmod.TASK_BUTTONS).items() if v["key"] == key)
    assert any(x["callback_data"] == f"d:{sid}:y" for x in buttons(msg))
    assert f"unconfirmed:{key}" in {p.key for p in doctor.problems(b.data, db, D(11))}
    db.mark_task(key, "done", "ana")
    assert "Only if" not in (b.reminder_text(DAY, D(11)) or "")
    assert f"unconfirmed:{key}" not in {p.key for p in doctor.problems(b.data, db, D(11))}


def test_what_the_not_done_turn_proposes_waits_for_a_household_tap(data_dir, tmp_path):
    """The reorganise turn after a Not done is the bot's own, like the weekly review: a change it
    proposes is not made at once and carries buttons a household member can tap."""
    b, _db = setup(data_dir, tmp_path)
    iid = next(iter(b.data.ingredients))
    cfg(data_dir, text=f"Then this.\n```change\nasked: true\npantry:\n  {iid}: 0\n```")
    s = evening_session(b.data)
    tick(b, s.date, "21:30", BATCH)
    (msg,) = [p for p in sends(b, -100) if "Batch cooking today" in p["text"]]
    pre = "1. " if len(b.batch_jobs(s.date)) > 1 else ""
    press(b, msg, pre + "✗ Not done", 7)
    (turn,) = lines(data_dir, "turns.jsonl")
    assert "Task not done" in turn["new"][-1]
    offered = [p for p in sends(b, -100) if "reply_markup" in p and p is not msg]
    yes = [x for p in offered for x in buttons(p) if x["callback_data"].startswith("c:")]
    assert yes  # waits for a yes
    press(b, next(p for p in offered if any(x in buttons(p) for x in yes)), yes[0]["text"], 8)
    assert not (toasts(b)[-1] or "").startswith("Only ")  # not "Only Ana can answer this."


def test_a_not_done_in_a_private_chat_is_that_persons_turn(data_dir, tmp_path, monkeypatch):
    """A private-chat reminder's Not done: the turn is the tapper's, so the facts are only what
    they may see, and they can confirm what it proposes themselves."""
    b, db = setup(data_dir, tmp_path)
    iid = next(iter(b.data.ingredients))
    cfg(data_dir, text=f"Then this.\n```change\npantry:\n  {iid}: 0\n```")
    s = evening_session(b.data)
    dish = s.dishes[0]
    eat = str(dish.eat[0])
    rid = db.add_reminder(7, "ana", at(s.date, "18:00"), "x", 7, dish.recipe, eat, "cook")
    monkeypatch.setattr(botmod.time, "time", lambda: db.reminder(rid)["at"] + 30)
    viewers, real = [], context.facts

    def facts(data, db, today, viewer=None, *rest, **kw):
        viewers.append(viewer)
        return real(data, db, today, viewer, *rest, **kw)

    monkeypatch.setattr(botmod.context, "facts", facts)
    run(b.tick_once(None))
    (msg,) = [p for p in sends(b, 7) if p["text"].startswith("⏰")]
    press(b, msg, "✗ Not done", 7)
    (turn,) = lines(data_dir, "turns.jsonl")
    assert "Task not done" in turn["new"][-1]
    assert viewers == [b.person_of(7)]  # the tapper's turn: only what they may see
    offered = [p for p in sends(b, 7) if "reply_markup" in p and p is not msg]
    yes = [x for p in offered for x in buttons(p) if x["callback_data"].startswith("c:")]
    assert yes
    press(b, next(p for p in offered if any(x in buttons(p) for x in yes)), yes[0]["text"], 7)
    assert not (toasts(b)[-1] or "").startswith("Only ")
