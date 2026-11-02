"""Reminders at about the same time go out as one message: the chat's own among themselves and
inside the app's reminder of that time, each step on its own numbered line with its own buttons. A
new reminder for about the same time joins the one already there, and the same words twice are
not set again. Seven 21:30 pings for one cook in one evening was noise."""

import datetime as dt

from supermealplanner import bot as botmod
from supermealplanner import context, verifier
from supermealplanner.bot import TelegramError

from .test_chains import at
from .test_reminders import ahead, block, setup
from .test_settings_stores_bot import msg, run
from .test_task_done import BATCH, evening_session, tick
from .test_turns import buttons, cfg, sends, tap, toasts


def soon(days=2):
    return dt.date.today() + dt.timedelta(days=days)


def test_three_reminders_at_about_the_same_time_go_out_as_one_message(
    data_dir, tmp_path, monkeypatch
):
    b, db = setup(data_dir, tmp_path)
    day = soon()
    ids = [
        db.add_reminder(-100, "ana", at(day, "21:30"), "Freeze the weekend's shopping", 7),
        db.add_reminder(-100, "luis", at(day, "21:30"), "Keep 1 green pepper for Friday", 7),
        db.add_reminder(-100, "ana", at(day, "21:45"), "Soak the chickpeas", 7),
    ]
    monkeypatch.setattr(botmod.time, "time", lambda: at(day, "21:30") + 30)
    run(b.tick_once(None))
    (sent,) = sends(b, -100)  # one message, the 21:45 one early with the rest
    assert sent["text"] == (
        "⏰ Ana, Luis\n"  # each person named once, at the top
        "1. Freeze the weekend's shopping\n"
        "2. Keep 1 green pepper for Friday\n"
        "3. Soak the chickpeas"
    )
    rows = sent["reply_markup"]["inline_keyboard"]
    assert [[x["text"] for x in row] for row in rows] == [
        [f"{n}. ✅ Done", f"{n}. ✗ Not done"] for n in (1, 2, 3)
    ]
    assert db.reminders(-100) == []
    assert sorted(r["id"] for r in db.reminders(-100, "sent")) == sorted(ids)
    run(b.tick_once(None))
    assert len(sends(b, -100)) == 1  # sent once


def test_the_apps_evening_message_takes_in_a_reminder_of_that_time(data_dir, tmp_path):
    b, db = setup(data_dir, tmp_path)
    s = evening_session(b.data)
    rid = db.add_reminder(-100, "ana", at(s.date, "21:30"), "Keep 1 green pepper for Friday", 7)
    tick(b, s.date, "21:30", BATCH)
    (sent,) = sends(b, -100)
    assert "Batch cooking today" in sent["text"]
    assert sent["text"].startswith("⏰ Ana\n\n")
    jobs = b.batch_jobs(s.date)
    assert f"{len(jobs) + 1}. Keep 1 green pepper for Friday" in sent["text"]
    assert len(buttons(sent)) == 2 * (len(jobs) + 1)  # a done / not done row for each
    assert db.reminders(-100) == [] and db.reminders(-100, "sent")[0]["id"] == rid


def test_a_message_telegram_refuses_fails_every_reminder_it_carried(
    data_dir, tmp_path, monkeypatch
):
    b, db = setup(data_dir, tmp_path)
    day = soon()
    db.add_reminder(-100, "ana", at(day, "21:30"), "Freeze the shopping", 7)
    db.add_reminder(-100, "ana", at(day, "21:40"), "Soak the chickpeas", 7)
    tries = []

    async def refused(client, chat_id, text, reply_to=None, **kw):
        tries.append(text)
        raise TelegramError("sendMessage", "Bad Request: chat not found", 400)

    b.send = refused
    monkeypatch.setattr(botmod.time, "time", lambda: at(day, "21:30") + 30)
    run(b.tick_once(None))
    assert len(tries) == 1  # one message for both
    assert db.reminders(-100) == [] and db.reminders(-100, "sent") == []
    assert len(db.reminders(-100, "failed")) == 2


def test_reminders_far_apart_stay_two_messages(data_dir, tmp_path, monkeypatch):
    b, db = setup(data_dir, tmp_path)
    day = soon()
    db.add_reminder(-100, "ana", at(day, "18:00"), "Shop", 7)
    db.add_reminder(-100, "ana", at(day, "21:30"), "Soak the chickpeas", 7)
    monkeypatch.setattr(botmod.time, "time", lambda: at(day, "18:00") + 30)
    run(b.tick_once(None))
    monkeypatch.setattr(botmod.time, "time", lambda: at(day, "21:30") + 30)
    run(b.tick_once(None))
    assert [p["text"] for p in sends(b, -100)] == ["⏰ Ana: Shop", "⏰ Ana: Soak the chickpeas"]


def test_the_same_words_at_about_the_same_time_are_not_set_again(data_dir, tmp_path):
    b, db = setup(data_dir, tmp_path)
    when, t = ahead(1, 21)
    db.add_reminder(-100, "ana", t + 600, "Order the shopping!", 7)
    cfg(data_dir, text="Done.\n" + block((when, "order the  shopping", "ana", "set")))
    run(b.handle(None, msg("@chefbot remind Ana at 21:00 to order the shopping", message_id=1)))
    assert len(db.reminders(-100)) == 1  # nothing new stored
    (said,) = sends(b, -100)
    assert "The 21:10 reminder already says this, not set again" in said["text"]


def test_a_new_reminder_near_another_joins_it_and_the_chat_says_so(data_dir, tmp_path):
    b, db = setup(data_dir, tmp_path)
    when, t = ahead(1, 21)
    db.add_reminder(-100, "ana", t + 900, "Freeze the shopping", 7)
    cfg(data_dir, text="Done.\n" + block((when, "Soak the chickpeas", "ana", "set")))
    run(b.handle(None, msg("@chefbot remind Ana at 21:00 to soak", message_id=1)))
    assert len(db.reminders(-100)) == 2  # stored as usual, its own step
    (said,) = sends(b, -100)
    assert "(added to the 21:15 reminder: they go out as one message)" in said["text"]


def test_a_tap_near_the_apps_own_reminder_joins_it_and_a_repeat_tap_sets_nothing(
    data_dir, tmp_path
):
    b, db = setup(data_dir, tmp_path)
    when, t = ahead(2, 10)  # the household's 10:00 message
    cfg(data_dir, text="Sunday.\n" + block((when[:-2] + "15", "Fill the water jug", "luis")))
    run(b.handle(None, msg("@chefbot when?", message_id=1)))
    offer = next(p for p in sends(b, -100) if buttons(p))
    mid = next(1000 + n + 1 for n, (_m, p) in enumerate(b.calls) if p is offer)
    run(tap(b, buttons(offer)[0]["callback_data"], 7, mid))
    assert "(added to the 10:00 reminder" in toasts(b)[-1]
    assert len(db.reminders(-100)) == 1
    db.add_reminder(-100, "luis", t + 1200, "Fill the water jug", 7)  # the same words again
    cfg(data_dir, text="Again.\n" + block((when[:-2] + "05", "fill the water jug", "luis")))
    run(b.handle(None, msg("@chefbot when?", message_id=2)))
    offer = next(p for p in sends(b, -100)[::-1] if buttons(p))
    mid = next(1000 + n + 1 for n, (_m, p) in enumerate(b.calls) if p is offer)
    run(tap(b, buttons(offer)[0]["callback_data"], 7, mid))
    assert "already says this" in toasts(b)[-1]
    assert len(db.reminders(-100)) == 2  # nothing new


def test_the_ai_and_the_reviewer_are_told_about_reminders_at_the_same_time():
    assert "at about that time already covers the step, offer nothing more" in context.REMINDERS
    assert "Two\n   reminders at about the same time that repeat a step" in verifier.VERIFY
