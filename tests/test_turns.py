"""One answer per burst, messages that join a running answer, edits, and the buttons: yes or no to
a change, the provider's asks, and choices under an answer."""

import asyncio
import json
import time

from supermealplanner.db import DB

from .test_conversations import ChatBot, plugin_data
from .test_settings_stores_bot import msg, run

PLUGIN = """
import json, pathlib, threading, time
HERE = pathlib.Path(__file__).parent
DECIDED = threading.Event()

def _cfg():
    p = HERE / "reply.json"
    return json.loads(p.read_text()) if p.exists() else {}

def converse(turn, settings):
    cfg = _cfg()
    log = {"new": [m["content"] for m in turn["new"]], "incoming": [], "ask": "ask" in turn}
    (HERE / "running").write_text("1")
    covers = []
    if cfg.get("ask"):
        turn["ask"]({"id": "r1", "summary": "Add: lunch with Ana",
                     "confirm_by": cfg.get("confirm_by", "asker"),
                     "expires_at": time.time() + 60})
        DECIDED.wait(10)
    end = time.time() + cfg.get("listen", 0)
    while time.time() < end:
        got = turn["incoming"]()
        log["incoming"] += [m["content"] for m in got]
        covers += [m["id"] for m in got]
        if cfg.get("until") and len(log["incoming"]) >= cfg["until"]:
            break
        time.sleep(0.02)
    with (HERE / "turns.jsonl").open("a") as f:
        f.write(json.dumps(log) + "\\n")
    (HERE / "running").unlink(missing_ok=True)
    return {"text": cfg.get("text", "ok"), "conversation": "c1",
            "covers": covers if cfg.get("cover", True) else [],
            "proposals": cfg.get("proposals", [])}

def act(proposal, settings):
    with (HERE / "acts.jsonl").open("a") as f:
        f.write(json.dumps(proposal) + "\\n")
    return "done " + proposal["id"]

def answer(request, decision, settings):
    with (HERE / "answers.jsonl").open("a") as f:
        f.write(json.dumps({"request": request["id"], **decision}) + "\\n")
    DECIDED.set()
    return {"ok": True, "error": None}
"""


def setup(data_dir, tmp_path, **bot):
    data = plugin_data(data_dir, **bot)
    (data_dir / "plugins" / "ai.py").write_text(PLUGIN)
    db = DB(tmp_path / "b.db")
    return ChatBot(data, db), db


def cfg(data_dir, **c):
    (data_dir / "plugins" / "reply.json").write_text(json.dumps(c))


def lines(data_dir, name):
    path = data_dir / "plugins" / name
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


def sends(b, chat=None):
    return [p for m, p in b.calls if m == "sendMessage" and chat in (None, p["chat_id"])]


def buttons(params):
    return [btn for row in params["reply_markup"]["inline_keyboard"] for btn in row]


def tap(b, data, user, mid, chat=-100):
    query = {
        "id": "q",
        "from": {"id": user, "first_name": "X"},
        "message": {
            "message_id": mid,
            "chat": {"id": chat, "type": "group" if chat < 0 else "private"},
        },
        "data": data,
    }
    return b.tapped(None, query)


def toasts(b):
    return [p.get("text") for m, p in b.calls if m == "answerCallbackQuery"]


async def until(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < end, "timed out"
        await asyncio.sleep(0.01)


async def drain(b):
    while b.tasks:
        await asyncio.gather(*list(b.tasks))


def running(data_dir):
    return (data_dir / "plugins" / "running").exists()


# ---------------------------------------------------------------- one answer per burst


def test_a_burst_within_the_debounce_gets_one_plain_answer(data_dir, tmp_path):
    b, _ = setup(data_dir, tmp_path)
    b.debounce = 0.3

    async def go():
        first = asyncio.create_task(b.handle(None, msg("hola", chat=7, user=7, message_id=1)))
        await asyncio.sleep(0.1)
        await b.handle(None, msg("¿qué cenamos?", chat=7, user=7, message_id=2))
        await first

    run(go())
    assert [t["new"] for t in lines(data_dir, "turns.jsonl")] == [
        ["luis: hola\nluis: ¿qué cenamos?"]
    ]
    (answer,) = sends(b, 7)
    assert answer["text"] == "ok" and "reply_parameters" not in answer  # covers two: plain


def test_one_message_gets_a_reply_to_it(data_dir, tmp_path):
    b, _ = setup(data_dir, tmp_path)
    run(b.handle(None, msg("hola", chat=7, user=7, message_id=5)))
    (answer,) = sends(b, 7)
    assert answer["reply_parameters"]["message_id"] == 5


def test_a_message_during_the_answer_joins_it(data_dir, tmp_path):
    b, _db = setup(data_dir, tmp_path)
    cfg(data_dir, listen=3, until=1)

    async def go():
        first = asyncio.create_task(b.handle(None, msg("@chefbot hola", message_id=1)))
        await until(lambda: running(data_dir))
        await b.handle(None, msg("@chefbot y mañana?", message_id=2))
        await first

    run(go())
    (turn,) = lines(data_dir, "turns.jsonl")
    assert turn["new"] == ["luis: @chefbot hola"]
    assert turn["incoming"] == ["luis: @chefbot y mañana?"]
    (answer,) = sends(b, -100)
    assert "reply_parameters" not in answer
    # covered: the next turn does not send it again
    cfg(data_dir)
    run(b.handle(None, msg("@chefbot gracias", message_id=3)))
    assert lines(data_dir, "turns.jsonl")[-1]["new"] == ["luis: @chefbot gracias"]


def test_a_message_the_answer_did_not_take_makes_the_next_turn(data_dir, tmp_path):
    b, _ = setup(data_dir, tmp_path)
    cfg(data_dir, listen=3, until=1, cover=False)

    async def go():
        first = asyncio.create_task(b.handle(None, msg("hola", chat=7, user=7, message_id=1)))
        await until(lambda: running(data_dir))
        cfg(data_dir)  # the next turn listens to nothing
        await b.handle(None, msg("y mañana?", chat=7, user=7, message_id=2))
        await first

    run(go())
    first, second = lines(data_dir, "turns.jsonl")
    assert first["new"] == ["luis: hola"]
    assert second["new"] == ["luis: y mañana?"]
    assert [p.get("reply_parameters", {}).get("message_id") for p in sends(b, 7)] == [1, 2]


def test_a_voice_note_still_transcribing_holds_the_turn(data_dir, tmp_path):
    b, _ = setup(data_dir, tmp_path)
    b.debounce = 0.05

    async def slow(client, voice):
        await asyncio.sleep(0.4)
        return "y de postre fruta"

    b.transcribe = slow

    async def go():
        note = asyncio.create_task(b.handle(None, msg(chat=7, user=7, voice={"file_id": "f"})))
        await asyncio.sleep(0.05)
        await b.handle(None, msg("hoy cenamos pescado", chat=7, user=7, message_id=2))
        await note

    run(go())
    (turn,) = lines(data_dir, "turns.jsonl")
    assert turn["new"] == ["luis: hoy cenamos pescado\nluis: 🎤 y de postre fruta"]


# ---------------------------------------------------------------- yes or no with buttons


def test_a_proposal_is_tapped_by_the_asker_and_only_by_them(data_dir, tmp_path):
    b, db = setup(data_dir, tmp_path)
    cfg(
        data_dir, proposals=[{"id": "p1", "summary": "Open a pull request.", "confirm_by": "asker"}]
    )
    run(b.handle(None, msg("@chefbot add /soup", message_id=1)))
    offer = sends(b, -100)[-1]
    assert "If you want something else, reply to this message." in offer["text"]
    yes, no = buttons(offer)
    assert (yes["text"], no["text"]) == ("Yes", "No")
    mid = 1000 + len(b.calls)  # the fake's id for the last message sent
    run(tap(b, yes["callback_data"], 8, mid))  # Ana: not hers to answer
    assert lines(data_dir, "acts.jsonl") == [] and len(b.pending(-100)) == 1
    assert toasts(b)[-1] == "Only Luis can answer this."
    run(tap(b, yes["callback_data"], 7, mid))
    assert [a["id"] for a in lines(data_dir, "acts.jsonl")] == ["p1"]
    assert b.pending(-100) == []
    assert (
        "editMessageReplyMarkup",
        {"chat_id": -100, "message_id": mid, "reply_markup": {"inline_keyboard": []}},
    ) in b.calls
    assert [r["text"] for r in db.recent_chat(-100, 2)] == ["Yes", "done p1"]  # as typed
    run(tap(b, yes["callback_data"], 7, mid))  # a second tap: answered already
    assert len(lines(data_dir, "acts.jsonl")) == 1 and toasts(b)[-1] == b.t("tap_gone")


def test_a_typed_yes_still_answers_and_takes_the_buttons_away(data_dir, tmp_path):
    b, _ = setup(data_dir, tmp_path)
    cfg(
        data_dir, proposals=[{"id": "p2", "summary": "Open a pull request.", "confirm_by": "asker"}]
    )
    run(b.handle(None, msg("@chefbot add /soup", message_id=1)))
    cfg(data_dir)
    run(b.handle(None, msg("yes", message_id=2, reply_to_message={"from": {"id": 99}})))
    assert [a["id"] for a in lines(data_dir, "acts.jsonl")] == ["p2"]
    assert any(m == "editMessageReplyMarkup" for m, _ in b.calls)


# ---------------------------------------------------------------- choices under an answer


def test_choices_are_buttons_a_tap_is_the_askers_next_message(data_dir, tmp_path):
    b, _ = setup(data_dir, tmp_path)
    cfg(data_dir, text="¿Cuál prefieres el lunes?\n\n```options\n- Lentejas\n- Garbanzos\n```")
    run(b.handle(None, msg("@chefbot ¿qué hago el lunes?", message_id=1)))
    answer = sends(b, -100)[-1]
    assert "```options" not in answer["text"] and "Or tell me something else." in answer["text"]
    lentejas, garbanzos = buttons(answer)
    assert lentejas["text"] == "Lentejas"
    mid = 1000 + len(b.calls)
    cfg(data_dir)
    run(tap(b, garbanzos["callback_data"], 8, mid))  # Ana was not asked
    assert len(lines(data_dir, "turns.jsonl")) == 1
    assert toasts(b)[-1] == "Only Luis can answer this."
    run(tap(b, garbanzos["callback_data"], 7, mid))
    assert lines(data_dir, "turns.jsonl")[-1]["new"][-1] == "luis: Garbanzos"
    marked = [p for m, p in b.calls if m == "editMessageReplyMarkup"][-1]
    assert buttons(marked)[0]["text"] == "✓ Garbanzos"
    assert sends(b, -100)[-1]["reply_parameters"]["message_id"] == mid  # answers the question
    run(tap(b, lentejas["callback_data"], 7, mid))  # chosen once
    assert len(lines(data_dir, "turns.jsonl")) == 2


def test_ignored_choices_block_nothing(data_dir, tmp_path):
    b, _ = setup(data_dir, tmp_path)
    cfg(data_dir, text="¿Cuál?\n```options\n- A\n- B\n```")
    run(b.handle(None, msg("@chefbot ¿qué hago?", message_id=1)))
    cfg(data_dir)
    run(b.handle(None, msg("@chefbot mejor dime la lista", message_id=2)))
    assert lines(data_dir, "turns.jsonl")[-1]["new"][-1] == "luis: @chefbot mejor dime la lista"
    assert sends(b, -100)[-1]["text"] == "ok"


# ---------------------------------------------------------------- the provider's asks


def test_an_ask_is_approved_by_the_asker_only_and_the_result_is_said(data_dir, tmp_path):
    b, _ = setup(data_dir, tmp_path)
    cfg(data_dir, ask=True)

    def posted():
        return [p for p in sends(b, -100) if "do you approve" in p["text"]]

    async def go():
        turn = asyncio.create_task(b.handle(None, msg("@chefbot comida con Ana", message_id=1)))
        await until(lambda: posted())
        ask = posted()[0]
        assert "Add: lunch with Ana" in ask["text"]
        approve, reject = buttons(ask)
        assert (approve["text"], reject["text"]) == ("Approve", "Reject")
        mid = next(1000 + n + 1 for n, (m, p) in enumerate(b.calls) if p is ask)
        await tap(b, approve["callback_data"], 8, mid)
        assert lines(data_dir, "answers.jsonl") == []
        await tap(b, approve["callback_data"], 7, mid)
        await turn
        await drain(b)
        return mid

    mid = run(go())
    (decision,) = lines(data_dir, "answers.jsonl")
    assert decision["approved"] is True and decision["by"] == "7"
    done = [p for p in sends(b, -100) if p["text"] == "Done."]
    assert done and done[0]["reply_parameters"]["message_id"] == mid
    assert lines(data_dir, "turns.jsonl")[0]["ask"] is True


def test_an_ask_nobody_can_answer_is_declined_at_once(data_dir, tmp_path):
    b, _ = setup(data_dir, tmp_path)  # no admins
    cfg(data_dir, ask=True, confirm_by="admins")

    async def go():
        await b.handle(None, msg("@chefbot comida con Ana", message_id=1))
        await drain(b)

    run(go())
    (decision,) = lines(data_dir, "answers.jsonl")
    assert decision["approved"] is False and decision["note"] == "no one could be asked"
    assert not [p for p in sends(b, -100) if "do you approve" in p["text"]]


def test_an_edit_during_the_turn_declines_its_ask_and_reaches_the_answer(data_dir, tmp_path):
    b, db = setup(data_dir, tmp_path)
    cfg(data_dir, ask=True, listen=3, until=1)

    async def go():
        turn = asyncio.create_task(b.handle(None, msg("@chefbot comida con Ana", message_id=1)))
        await until(lambda: any("do you approve" in p["text"] for p in sends(b, -100)))
        await b.edited(None, msg("@chefbot comida con Ana a las 15", message_id=1))
        await turn
        await drain(b)

    run(go())
    (decision,) = lines(data_dir, "answers.jsonl")
    assert decision["approved"] is False
    assert decision["note"] == "editado: comida con Ana a las 15"
    assert any(p["text"] == b.t("ask_edited") for p in sends(b, -100))
    (turn,) = lines(data_dir, "turns.jsonl")
    (correction,) = turn["incoming"]
    assert "edited a message of this turn" in correction
    assert "Before: comida con Ana\nNow: comida con Ana a las 15" in correction
    assert db.find_chat(-100, 1)["text"] == "@chefbot comida con Ana a las 15"


# ---------------------------------------------------------------- edits


def test_an_edit_that_adds_the_mention_is_answered(data_dir, tmp_path):
    b, _db = setup(data_dir, tmp_path, history_scope="all")
    run(b.handle(None, msg("qué cenamos", message_id=1)))
    assert lines(data_dir, "turns.jsonl") == []
    run(b.edited(None, msg("@chefbot qué cenamos", message_id=1)))
    assert lines(data_dir, "turns.jsonl")[-1]["new"] == ["luis: @chefbot qué cenamos"]
    assert sends(b, -100)[-1]["reply_parameters"]["message_id"] == 1


def test_an_edit_still_without_the_mention_only_updates_the_log(data_dir, tmp_path):
    b, db = setup(data_dir, tmp_path, history_scope="all")
    run(b.handle(None, msg("qué cenamos", message_id=1)))
    run(b.edited(None, msg("qué cenamos hoy", message_id=1)))
    assert lines(data_dir, "turns.jsonl") == []
    assert db.find_chat(-100, 1)["text"] == "qué cenamos hoy"


def test_an_edit_of_an_answered_message_starts_a_turn_with_the_change(data_dir, tmp_path):
    b, db = setup(data_dir, tmp_path)
    run(b.handle(None, msg("cenamos a las 9", chat=7, user=7, message_id=1)))
    run(b.edited(None, msg("cenamos a las 10", chat=7, user=7, message_id=1)))
    _first, second = lines(data_dir, "turns.jsonl")
    (new,) = second["new"]
    assert "edited a message you already answered" in new
    assert "Before: cenamos a las 9\nNow: cenamos a las 10\nYour answer was: ok" in new
    assert db.find_chat(7, 1)["text"] == "cenamos a las 10"


def test_an_edit_that_changes_nothing_or_starts_with_a_slash_does_nothing(data_dir, tmp_path):
    b, db = setup(data_dir, tmp_path)
    run(b.handle(None, msg("hola", chat=7, user=7, message_id=1)))
    run(b.edited(None, msg(" hola ", chat=7, user=7, message_id=1)))
    run(b.edited(None, msg("/nuevo", chat=7, user=7, message_id=1)))
    assert len(lines(data_dir, "turns.jsonl")) == 1
    assert db.find_chat(7, 1)["text"] == "hola"


def test_an_edit_of_a_queued_message_sends_the_new_text(data_dir, tmp_path):
    b, _ = setup(data_dir, tmp_path)
    b.debounce = 0.3

    async def go():
        first = asyncio.create_task(
            b.handle(None, msg("cenamos a las 9", chat=7, user=7, message_id=1))
        )
        await asyncio.sleep(0.1)
        await b.edited(None, msg("cenamos a las 10", chat=7, user=7, message_id=1))
        await first

    run(go())
    assert [t["new"] for t in lines(data_dir, "turns.jsonl")] == [["luis: cenamos a las 10"]]


def test_the_bot_asks_telegram_for_edits_and_taps_and_routes_them(data_dir, tmp_path):
    b, _ = setup(data_dir, tmp_path, history_scope="all")
    run(b.handle(None, msg("qué cenamos", message_id=1)))
    seen = []

    class Stop(Exception):
        pass

    updates = [
        {"update_id": 1, "edited_message": msg("@chefbot qué cenamos", message_id=1)},
        {"update_id": 2, "callback_query": {"id": "q", "from": {"id": 7}, "data": "x"}},
    ]

    async def call(client, method, _tries=0, **params):
        b.calls.append((method, params))
        if method == "getUpdates":
            if seen:
                raise Stop
            seen.append(params["allowed_updates"])
            return updates
        return {"message_id": 1000 + len(b.calls)} if method == "sendMessage" else {}

    b.call = call

    async def go():
        try:
            await b.poll(None)
        except Stop:
            pass
        await drain(b)

    run(go())
    assert seen == [["message", "edited_message", "callback_query"]]
    assert lines(data_dir, "turns.jsonl")[-1]["new"] == ["luis: @chefbot qué cenamos"]
    assert ("answerCallbackQuery", {"callback_query_id": "q"}) in b.calls
