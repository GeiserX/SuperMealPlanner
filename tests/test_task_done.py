"""Done or not done: every message that reminds of a task carries its two buttons, the first answer
is stored with the task, a "not done" (or no answer by the next morning) starts a turn that
reorganises the week, and nothing counts a task as done until someone said so. The third test is
the hake case: a Saturday session nobody did, and on Sunday the bot spoke of its containers as if
they existed."""

import datetime as dt

from supermealplanner import bot as botmod
from supermealplanner import context, doctor, tasks
from supermealplanner.model import Reminder
from supermealplanner.planner import plan_for

from .conftest import MONDAY
from .test_chains import SALAD, remind
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
            "chat": {"id": params["chat_id"], "type": "group"},
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


def test_the_batch_reminder_carries_done_and_not_done_and_the_first_answer_stays(
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
    assert labels(markups(b)[-1])[0] == ("1. " if many else "") + "✓ Done"
    assert any(r["text"].startswith("✓ Done: ") for r in db.recent_chat(-100, 5))
    press(b, msg, ("1. " if many else "") + "✗ Not done", 8)  # a second tap changes nothing
    assert tasks.state(db, key) == tasks.DONE
    assert not lines(data_dir, "turns.jsonl")  # and starts no turn


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
    # asked once: the next morning does not ask again about the same task
    tick(b, D(7), "08:00", DAY)
    assert "Nobody said whether" not in sends(b, -100)[-1]["text"]
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
    with db.conn() as c:
        c.execute("DELETE FROM task_state WHERE key=?", (f"r:{cook}",))
    db.mark_task(f"r:{cook}", "done", "ana")
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
