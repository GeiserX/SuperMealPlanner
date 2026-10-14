"""Reminders the AI offers when it tells someone to do something at a time: one message with buttons
(here, in the calendar, nothing), nothing done without a tap, the reminders posted at their time and
kept across a restart, /reminders to cancel one, and the calendar tap that approves its own event.
Also: group lines not addressed to the bot reach the AI marked as such."""

import asyncio
import datetime as dt
import json
import time
from zoneinfo import ZoneInfo

from supermealplanner import bot as botmod
from supermealplanner import context, llm
from supermealplanner.db import DB

from .test_conversations import ChatBot, plugin_data
from .test_settings_stores_bot import msg, run
from .test_turns import buttons, cfg, drain, lines, sends, tap, toasts, until

PLUGIN = """
import json, pathlib, threading, time
HERE = pathlib.Path(__file__).parent

def _cfg():
    p = HERE / "reply.json"
    return json.loads(p.read_text()) if p.exists() else {}

def converse(turn, settings):
    cfg = _cfg()
    log = {"new": [m["content"] for m in turn["new"]],
           "history": [m["content"] for m in turn["history"]]}
    for extra in cfg.get("asks", []):
        done = threading.Event()
        (HERE / "waiting").write_text("1")
        turn["ask"]({"id": extra.get("id", "r1"), "summary": "Add: an event",
                     "confirm_by": "asker", "expires_at": time.time() + 60, **extra})
        for _ in range(500):
            if (HERE / "answers.jsonl").exists() and len(
                (HERE / "answers.jsonl").read_text().splitlines()) >= extra.get("n", 1):
                break
            time.sleep(0.01)
    with (HERE / "turns.jsonl").open("a") as f:
        f.write(json.dumps(log) + "\\n")
    out = {"text": cfg.get("text", "ok"), "conversation": "c1"}
    if "reminders" in cfg:
        out["reminders"] = cfg["reminders"]
    return out

def answer(request, decision, settings):
    with (HERE / "answers.jsonl").open("a") as f:
        f.write(json.dumps({"request": request["id"], **decision}) + "\\n")
    return {"ok": True, "error": None}
"""

ZONE = ZoneInfo("Europe/Madrid")


def setup(data_dir, tmp_path, **bot):
    data = plugin_data(data_dir, **bot)
    (data_dir / "plugins" / "ai.py").write_text(PLUGIN)
    db = DB(tmp_path / "b.db")
    return ChatBot(data, db), db


def ahead(days=2, hour=18):
    """A time still to come, as the AI writes it, and its epoch seconds."""
    d = dt.datetime.now(ZONE).date() + dt.timedelta(days=days)
    local = dt.datetime(d.year, d.month, d.day, hour, 0, tzinfo=ZONE)
    return local.strftime("%Y-%m-%d %H:%M"), local.timestamp()


def block(*rows):
    return "```reminders\n" + "\n".join(f"- {' | '.join(r)}" for r in rows) + "\n```"


def offer_of(b):
    return next(p for p in sends(b, -100) if "If you do not answer, I do nothing." in p["text"])


def mid_of(b, params):
    return next(1000 + n + 1 for n, (_m, p) in enumerate(b.calls) if p is params)


def markups(b):
    return [p for m, p in b.calls if m == "editMessageReplyMarkup"]


def labels(params):
    return [btn["text"] for btn in buttons(params)]


# ---------------------------------------------------------------- the offer


def test_steps_at_a_time_are_offered_as_reminders_with_buttons_and_the_no_answer_line(
    data_dir, tmp_path
):
    b, db = setup(data_dir, tmp_path)
    sunday, _ = ahead(2, 18)
    saturday, _ = ahead(1, 22)
    cfg(
        data_dir,
        text="Cook it on Sunday afternoon.\n\n"
        + block((sunday, "Cook Monday's lunch"), (saturday, "Move the fish to the fridge", "ana")),
    )
    run(b.handle(None, msg("@chefbot what do I cook on Monday?", message_id=1)))
    answer, offer = sends(b, -100)[-2:]
    assert answer["text"] == "Cook it on Sunday afternoon."  # the block never reaches the chat
    assert offer["text"].startswith("Luis, Ana, shall I remind you?")
    assert "1. " in offer["text"] and "Cook Monday's lunch" in offer["text"]
    assert "18:00" in offer["text"] and "22:00" in offer["text"]
    assert offer["text"].endswith("If you do not answer, I do nothing.")
    # calendar off by default: here and nothing per reminder
    assert labels(offer) == [
        "1 · Reminder here",
        "1 · No",
        "2 · Reminder here",
        "2 · No",
    ]
    assert db.reminders(-100) == []  # nothing until a tap


def test_reminders_in_the_providers_dict_are_offered_and_past_or_bad_ones_dropped(
    data_dir, tmp_path
):
    b, _ = setup(data_dir, tmp_path, reminder_calendar=True)
    good, _ = ahead(3, 9)
    past, _ = ahead(-1, 9)
    far, _ = ahead(90, 9)
    cfg(
        data_dir,
        text="Shop on Wednesday.",
        reminders=[
            {"when": good, "text": "Do the shopping"},
            {"when": past, "text": "Too late"},
            {"when": far, "text": "Too far"},
            {"when": "tomorrow", "text": "No date"},
        ],
    )
    run(b.handle(None, msg("@chefbot when do I shop?", message_id=1)))
    offer = offer_of(b)
    assert "Do the shopping" in offer["text"]
    assert "Too late" not in offer["text"] and "Too far" not in offer["text"]
    assert labels(offer) == ["Reminder here", "In the calendar", "No"]


def test_an_answer_without_steps_offers_nothing(data_dir, tmp_path):
    b, _ = setup(data_dir, tmp_path)
    cfg(data_dir, text="Lentils.")
    run(b.handle(None, msg("@chefbot what is for lunch?", message_id=1)))
    assert [p["text"] for p in sends(b, -100)] == ["Lentils."]


def test_the_block_parser_reads_lines_and_ignores_the_rest():
    text, found = llm.extract_reminders(
        "Hi\n```reminders\n- 2026-11-01 18:00 | Cook | ana\n- nonsense\n* 2026-11-02 09:00 | Shop\n```"
    )
    assert text == "Hi"
    assert found == [
        {"when": "2026-11-01 18:00", "text": "Cook", "person": "ana"},
        {"when": "2026-11-02 09:00", "text": "Shop", "person": None},
    ]


# ---------------------------------------------------------------- here, nothing, expiry


def test_a_tap_on_here_stores_it_and_the_clock_posts_it_once_naming_the_person(
    data_dir, tmp_path, monkeypatch
):
    b, db = setup(data_dir, tmp_path)
    when, at = ahead(2, 18)
    cfg(data_dir, text="Sunday.\n" + block((when, "Cook Monday's lunch", "ana")))
    run(b.handle(None, msg("@chefbot when?", message_id=1)))
    offer = offer_of(b)
    here, _nothing = buttons(offer)
    mid = mid_of(b, offer)
    run(tap(b, here["callback_data"], 5, mid))  # a stranger: nothing
    assert db.reminders(-100) == []
    run(tap(b, here["callback_data"], 7, mid))  # Luis asked
    (r,) = db.reminders(-100)
    assert (r["person"], r["at"], r["text"], r["by"]) == ("ana", at, "Cook Monday's lunch", 7)
    assert "I will remind you here on" in toasts(b)[-1]
    assert labels(markups(b)[-1]) == ["✓ Reminder here"]
    run(tap(b, here["callback_data"], 7, mid))  # settled once
    assert len(db.reminders(-100)) == 1
    run(b.tick_once(None))
    assert not [p for p in sends(b, -100) if p["text"].startswith("⏰ Ana")]  # not yet
    monkeypatch.setattr(botmod.time, "time", lambda: at + 30)
    run(b.tick_once(None))
    run(b.tick_once(None))
    posted = [p["text"] for p in sends(b, -100) if p["text"].startswith("⏰ Ana")]
    assert posted == ["⏰ Ana: Cook Monday's lunch"]
    assert db.reminders(-100) == [] and db.reminders(-100, "sent")[0]["id"] == r["id"]


def test_only_the_people_it_was_for_can_tap_an_offer(data_dir, tmp_path):
    b, db = setup(data_dir, tmp_path)
    when, _ = ahead(2, 18)
    cfg(data_dir, text="Sunday.\n" + block((when, "Cook Monday's lunch")))  # for Luis, who asked
    run(b.handle(None, msg("@chefbot when?", message_id=1)))
    offer = offer_of(b)
    here, _nothing = buttons(offer)
    run(tap(b, here["callback_data"], 8, mid_of(b, offer)))  # Ana is in the household, not asked
    assert toasts(b)[-1] == "Only Luis can answer this."
    assert db.reminders(-100) == [] and not markups(b)


def test_a_reminder_set_survives_a_restart_and_a_late_one_says_so(data_dir, tmp_path, monkeypatch):
    b, db = setup(data_dir, tmp_path)
    _, at = ahead(1, 8)
    db.add_reminder(-100, "luis", at, "Take the fish out", 7)
    again = ChatBot(b.data, DB(tmp_path / "b.db"))  # a new process on the same database
    monkeypatch.setattr(botmod.time, "time", lambda: at + 3600)
    run(again.tick_once(None))
    (text,) = [p["text"] for p in sends(again, -100)]
    assert text.startswith("⏰ Luis: Take the fish out\n(late: it was for")


def test_nothing_or_no_tap_does_nothing_and_the_buttons_say_so(data_dir, tmp_path, monkeypatch):
    b, db = setup(data_dir, tmp_path)
    one, _ = ahead(2, 18)
    two, _ = ahead(2, 20)
    cfg(data_dir, text="Two steps.\n" + block((one, "Cook"), (two, "Clean")))
    run(b.handle(None, msg("@chefbot plan?", message_id=1)))
    offer = offer_of(b)
    _h1, nothing1, h2, _n2 = buttons(offer)
    run(tap(b, nothing1["callback_data"], 7, mid_of(b, offer)))
    assert labels(markups(b)[-1]) == ["✓ 1 · No", "2 · Reminder here", "2 · No"]
    now = time.time()
    monkeypatch.setattr(botmod.time, "time", lambda: now + botmod.REMIND_TTL + 5)
    run(b.expire_offers(None))
    assert labels(markups(b)[-1]) == ["✓ 1 · No", "✓ 2 · No answer: nothing"]
    run(tap(b, h2["callback_data"], 7, mid_of(b, offer)))  # too late
    assert toasts(b)[-1] == "This is no longer waiting for an answer."
    assert db.reminders(-100) == [] and db.get_kv(botmod.REMIND) is None


# ---------------------------------------------------------------- /reminders


def test_reminders_lists_them_and_a_cancel_button_cancels_one(data_dir, tmp_path, monkeypatch):
    b, db = setup(data_dir, tmp_path)
    _, soon = ahead(1, 9)
    _, later = ahead(2, 9)
    keep = db.add_reminder(-100, "ana", later, "Shop", 8)
    drop = db.add_reminder(-100, "luis", soon, "Cook", 7)
    db.add_reminder(-200, "luis", soon, "Elsewhere", 7)  # another chat
    run(b.handle(None, msg("/recordatorios", message_id=1)))
    listing = sends(b, -100)[-1]
    text = listing["text"]
    assert text.startswith("⏰ Reminders to come:\n1. ")
    assert "· Luis: Cook" in text and "· Ana: Shop" in text and "Elsewhere" not in text
    assert "@" not in text  # listing a reminder notifies nobody
    cancel1, cancel2 = buttons(listing)
    assert (cancel1["text"], cancel2["text"]) == ("Cancel 1", "Cancel 2")
    run(tap(b, cancel1["callback_data"], 8, mid_of(b, listing)))  # any household member
    assert [r["id"] for r in db.reminders(-100)] == [keep]
    assert db.reminders(-100, "cancelled")[0]["id"] == drop
    run(tap(b, cancel1["callback_data"], 8, mid_of(b, listing)))
    assert toasts(b)[-1] == "This is no longer waiting for an answer."
    monkeypatch.setattr(botmod.time, "time", lambda: soon + 30)
    run(b.tick_once(None))
    assert not [p for p in sends(b, -100) if p["text"].startswith("⏰ Luis: Cook")]


def test_a_stranger_let_in_by_group_open_cannot_cancel_a_reminder(data_dir, tmp_path):
    b, db = setup(data_dir, tmp_path, group_open=True)
    _, soon = ahead(1, 9)
    rid = db.add_reminder(-100, "luis", soon, "Cook", 7)
    own = db.add_reminder(-100, None, soon, "Theirs", 50)
    run(tap(b, f"k:{rid}:1", 50, 1))
    assert toasts(b)[-1] == "Only the household or whoever set it can cancel a reminder."
    assert {r["id"] for r in db.reminders(-100)} == {rid, own}
    run(tap(b, f"k:{own}:2", 50, 1))  # the one they set themselves
    assert [r["id"] for r in db.reminders(-100)] == [rid]


def test_reminders_with_none_says_so(data_dir, tmp_path):
    b, _ = setup(data_dir, tmp_path)
    run(b.handle(None, msg("/reminders", message_id=1)))
    assert sends(b, -100)[-1]["text"].startswith("No reminders to come in this chat.")


# ---------------------------------------------------------------- in the calendar


def calendar_offer(b, data_dir, when):
    cfg(data_dir, text="Sunday.\n" + block((when, "Cook Monday's lunch")))
    run(b.handle(None, msg("@chefbot when?", message_id=1)))
    offer = offer_of(b)
    _here, calendar, _nothing = buttons(offer)
    return offer, calendar


def test_in_the_calendar_starts_a_turn_whose_add_at_that_time_the_tap_approves(data_dir, tmp_path):
    b, db = setup(data_dir, tmp_path, reminder_calendar=True)
    when, at = ahead(2, 18)
    offer, calendar = calendar_offer(b, data_dir, when)
    cfg(data_dir, text="Added.", asks=[{"id": "j:ap1", "action": "add", "when": at}])

    async def go():
        await tap(b, calendar["callback_data"], 7, mid_of(b, offer))
        await drain(b)

    run(go())
    turn = lines(data_dir, "turns.jsonl")[-1]
    assert "Add an event to the calendar for this reminder" in turn["new"][-1]
    assert "«Cook Monday's lunch»" in turn["new"][-1]
    (decision,) = lines(data_dir, "answers.jsonl")
    assert decision["approved"] is True and decision["by"] == "7"
    said = [p["text"] for p in sends(b, -100)]
    assert any(t.startswith("Approved with the «In the calendar» button:") for t in said)
    assert not [p for p in sends(b, -100) if "do you approve" in p["text"]]
    assert labels(markups(b)[0]) == ["✓ In the calendar"]
    assert db.reminders(-100) == []  # not here as well


def test_in_the_calendar_approves_only_an_add_at_that_time_and_only_once(data_dir, tmp_path):
    b, _ = setup(data_dir, tmp_path, reminder_calendar=True)
    when, at = ahead(2, 18)
    offer, calendar = calendar_offer(b, data_dir, when)
    # the first ask is an add an hour off: asked as usual, and it uses the tap up
    cfg(
        data_dir,
        text="Added.",
        asks=[
            {"id": "j:ap1", "action": "add", "when": at + 3600, "n": 1},
            {"id": "j:ap2", "action": "add", "when": at, "n": 2},
        ],
    )

    def asked():
        return [p for p in sends(b, -100) if "do you approve" in p["text"]]

    async def go():
        turn = asyncio.create_task(tap(b, calendar["callback_data"], 7, mid_of(b, offer)))
        for n in (1, 2):
            await until(lambda n=n: len(asked()) >= n)
            approve, _reject = buttons(asked()[n - 1])
            await tap(b, approve["callback_data"], 7, mid_of(b, asked()[n - 1]))
        await turn
        await drain(b)

    run(go())
    assert len(asked()) == 2  # neither was approved by the calendar tap
    assert [d["request"] for d in lines(data_dir, "answers.jsonl")] == ["j:ap1", "j:ap2"]


def test_a_change_or_delete_is_never_approved_by_the_calendar_tap(data_dir, tmp_path):
    b, _ = setup(data_dir, tmp_path, reminder_calendar=True)
    when, at = ahead(2, 18)
    offer, calendar = calendar_offer(b, data_dir, when)
    cfg(data_dir, text="Done.", asks=[{"id": "j:ap1", "action": "delete", "when": at}])

    def asked():
        return [p for p in sends(b, -100) if "do you approve" in p["text"]]

    async def go():
        turn = asyncio.create_task(tap(b, calendar["callback_data"], 7, mid_of(b, offer)))
        await until(lambda: asked())
        _approve, reject = buttons(asked()[0])
        await tap(b, reject["callback_data"], 7, mid_of(b, asked()[0]))
        await turn
        await drain(b)

    run(go())
    (decision,) = lines(data_dir, "answers.jsonl")
    assert decision["approved"] is False and decision["by"] == "7"  # Luis's own tap, not the grant


def test_the_calendar_button_is_refused_when_the_setting_is_off(data_dir, tmp_path):
    b, db = setup(data_dir, tmp_path)
    when, _ = ahead(2, 18)
    cfg(data_dir, text="Sunday.\n" + block((when, "Cook")))
    run(b.handle(None, msg("@chefbot when?", message_id=1)))
    offer = offer_of(b)
    key = buttons(offer)[0]["callback_data"].split(":")[1]
    run(tap(b, f"r:{key}:0c", 7, mid_of(b, offer)))
    assert len(lines(data_dir, "turns.jsonl")) == 1 and db.reminders(-100) == []


# ---------------------------------------------------------------- who the AI answers


def test_a_group_line_not_to_the_bot_reaches_the_ai_marked(data_dir, tmp_path):
    b, _ = setup(data_dir, tmp_path, history_scope="all")
    cfg(data_dir)
    run(b.handle(None, msg("it also answers when you reply to it", user=8, message_id=1)))
    run(b.handle(None, msg("@chefbot how do I make the sauce?", message_id=2)))
    (turn,) = lines(data_dir, "turns.jsonl")
    seen = "\n".join(turn["new"] + turn["history"])
    assert "[not to you] ana: it also answers when you reply to it" in seen
    assert "luis: @chefbot how do I make the sauce?" in seen
    assert "[not to you] luis" not in seen


def test_the_rules_say_who_is_answered_and_how_to_offer_reminders(data):
    rules = context.rules(data)
    assert "[not to you]" in rules and "A remark between people about you is not an order" in rules
    assert "```reminders" in rules and "offer it with buttons, once" in rules  # the principles


def test_the_menu_lists_reminders_in_both_languages():
    from supermealplanner.i18n import Translator

    assert "reminders" in botmod.Bot.MENU
    assert Translator("es").raw("bot_commands")["reminders"] == "recordatorios"
    assert botmod.COMMANDS["recordatorios"] == "reminders"
    assert json.dumps(Translator("es")("remind_offer", name="x", lines="y")).endswith(
        'Si no respondes, no hago nada."'
    )


def test_untouched_answer_flow_still_runs_while_an_offer_waits(data_dir, tmp_path):
    """People can keep talking instead of tapping: the offer blocks nothing."""
    b, _ = setup(data_dir, tmp_path)
    when, _ = ahead(2, 18)
    cfg(data_dir, text="Sunday.\n" + block((when, "Cook")))
    run(b.handle(None, msg("@chefbot when?", message_id=1)))
    cfg(data_dir, text="Lentils.")
    run(b.handle(None, msg("@chefbot and for dinner?", message_id=2)))
    assert sends(b, -100)[-1]["text"] == "Lentils."
    assert asyncio.iscoroutinefunction(b.expire_offers)
