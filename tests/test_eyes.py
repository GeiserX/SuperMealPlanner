"""👀 while the bot works: every person's message a turn takes gets 👀 as soon as the turn starts, or
at once when it joins a running one, in every chat, without ever holding the turn up."""

import asyncio

import pytest

from supermealplanner import bot as botmod
from supermealplanner.bot import TelegramError

from .test_answer_scope import calls, everyone, reactions, refusing
from .test_settings_stores_bot import msg, run
from .test_turns import PLUGIN, buttons, cfg, drain, lines, running, sends, setup, tap, until

AI = "AI"  # the marker the AI call leaves in b.calls


def watched(b, monkeypatch):
    """b.calls also records the moment the AI is called, so the order can be checked."""
    converse = botmod.llm.converse

    async def spy(data, turn, limit):
        b.calls.append((AI, {}))
        return await converse(data, turn, limit)

    monkeypatch.setattr(botmod.llm, "converse", spy)


def order(b):
    return [
        (m, p["message_id"], p["reaction"][0]["emoji"]) if m == "setMessageReaction" else (m,)
        for m, p in b.calls
        if m in ("setMessageReaction", AI, "sendMessage")
    ]


def failing(data_dir):
    (data_dir / "plugins" / "ai.py").write_text(
        "def converse(turn, settings):\n    raise RuntimeError('no_output')\n"
    )


def test_a_mention_gets_eyes_before_the_ai_answers_and_words_leave_them(
    data_dir, tmp_path, monkeypatch
):
    b, _ = setup(data_dir, tmp_path)  # answer_scope asked
    watched(b, monkeypatch)
    run(b.handle(None, msg("@chefbot qué cenamos", message_id=1)))
    assert order(b) == [("setMessageReaction", 1, "👀"), (AI,), ("sendMessage",)]
    assert [p["text"] for p in sends(b, -100)] == ["ok"]


def test_a_private_chat_gets_eyes_at_the_start(data_dir, tmp_path, monkeypatch):
    b, _ = setup(data_dir, tmp_path)
    watched(b, monkeypatch)
    run(b.handle(None, msg("hola", chat=7, user=7, message_id=4)))
    assert order(b) == [("setMessageReaction", 4, "👀"), (AI,), ("sendMessage",)]
    assert reactions(b, chat=7) == [(4, "👀")]


def test_a_line_without_a_mention_gets_eyes_at_the_start_too(data_dir, tmp_path, monkeypatch):
    b, _ = everyone(data_dir, tmp_path)
    watched(b, monkeypatch)
    cfg(data_dir, text="Apuntado.")
    run(b.handle(None, msg("quedan 10 raciones", user=8, message_id=3)))
    assert order(b) == [("setMessageReaction", 3, "👀"), (AI,), ("sendMessage",)]


@pytest.mark.parametrize("scope", ["asked", "all"])
def test_a_message_that_joins_a_running_turn_gets_eyes_at_once(data_dir, tmp_path, scope):
    b, _ = setup(data_dir, tmp_path, answer_scope=scope)
    cfg(data_dir, listen=4, until=2)  # it listens the whole time: never two lines

    async def go():
        first = asyncio.create_task(b.handle(None, msg("@chefbot qué cenamos", message_id=1)))
        await until(lambda: running(data_dir), timeout=30)
        await b.handle(None, msg("@chefbot y mañana", user=8, message_id=2))
        # on it while the answer is still being written
        await until(lambda: (2, "👀") in reactions(b))
        assert running(data_dir)
        await first

    run(go())
    assert reactions(b) == [(1, "👀"), (2, "👀")]


def test_a_voice_note_gets_eyes_on_its_own_message_never_on_the_echo(data_dir, tmp_path):
    b, _ = everyone(data_dir, tmp_path)

    async def transcribe(client, voice):
        return "faltan garbanzos"

    b.transcribe = transcribe
    run(b.handle(None, msg(user=8, message_id=5, voice={"file_id": "a", "duration": 3})))
    echo, answer = sends(b, -100)
    assert echo["text"] == "🎤 faltan garbanzos" and answer["text"] == "ok"
    assert reactions(b) == [(5, "👀")]


def test_a_photo_and_an_edit_that_starts_a_turn_get_eyes(data_dir, tmp_path):
    b, _ = everyone(data_dir, tmp_path)
    photo = [{"file_id": "p", "file_size": 10, "width": 10}]

    async def download(client, file_id):
        return b"x", "p.jpg"

    b.download = download
    run(b.handle(None, msg(user=8, message_id=6, photo=photo, caption="la cena")))
    assert (6, "👀") in reactions(b)
    b.calls.clear()
    # an edit of an answered line: a new turn whose message is the edited one; marked already
    run(b.edited(None, msg("la cena de hoy", user=8, message_id=6)))
    assert reactions(b) == []
    # an edit that makes a line never stored call the bot starts a turn: it gets them
    run(b.edited(None, msg("@chefbot hola", user=8, message_id=9)))
    assert reactions(b) == [(9, "👀")]


def test_no_eyes_on_taps_or_the_bots_own_turns(data_dir, tmp_path):
    b, _ = setup(data_dir, tmp_path)
    cfg(data_dir, text="¿Cuál?\n\n```options\n- Lentejas\n- Garbanzos\n```")
    run(b.handle(None, msg("@chefbot ¿qué hago el lunes?", message_id=1)))
    _, garbanzos = buttons(sends(b, -100)[-1])
    mid = 1000 + len(b.calls)
    cfg(data_dir)
    b.calls.clear()
    run(tap(b, garbanzos["callback_data"], 7, mid))
    assert lines(data_dir, "turns.jsonl")[-1]["new"][-1] == "luis: Garbanzos"
    assert reactions(b) == []  # never on the bot's question the button was under
    # the bot's own turns: the weekly review, a task not done, a follow-up
    b.take(-100, {"first_name": "review"}, None, None, "review", True, review=True)
    b.take(-100, {"id": 7, "first_name": "Luis"}, "luis", None, "check", True, auto=True)
    b.take(-100, {"first_name": "fix"}, None, None, "fix", True, followup=True)
    run(b.drive(None, -100))
    assert reactions(b) == []


def test_no_eyes_on_the_echo_of_something_said_to_the_app(data_dir, tmp_path):
    from supermealplanner.bot import Item

    b, _ = everyone(data_dir, tmp_path)
    b.spoken_rows[5] = "s1"  # row 5 is a spoken turn: its message is the bot's own echo

    async def go():
        b.eye(None, -100, [Item(5, 1000, 7, "luis", 0.0), Item(6, 2, 8, "ana", 0.0)])
        await asyncio.gather(*b.tasks)

    run(go())
    assert reactions(b) == [(2, "👀")]


def test_eyes_go_once_through_silence_and_a_failed_turn(data_dir, tmp_path):
    b, _ = everyone(data_dir, tmp_path)
    cfg(data_dir, text="[no reply]")
    run(b.handle(None, msg("llego a las 8", user=8, message_id=1)))
    assert reactions(b) == [(1, "👀")] and sends(b) == []  # the end of the turn adds nothing
    b.calls.clear()
    failing(data_dir)
    run(b.handle(None, msg("quedan 10", user=8, message_id=2)))
    assert reactions(b) == [(2, "👀")] and sends(b) == []
    (data_dir / "plugins" / "ai.py").write_text(PLUGIN)
    cfg(data_dir, text="Apuntado.")
    run(b.handle(None, msg("y 2 de sopa", user=8, message_id=3)))
    # the held line goes with this turn, still with its one 👀
    assert reactions(b) == [(2, "👀"), (3, "👀")]
    assert [p["text"] for p in sends(b, -100)] == ["Apuntado."]


def test_a_refused_start_eye_holds_nothing_up_and_says_nothing(data_dir, tmp_path):
    b, _ = setup(data_dir, tmp_path)
    refusing(b, "👀")
    run(b.handle(None, msg("@chefbot qué cenamos", message_id=1)))
    assert reactions(b) == [(1, "👀")]  # tried once, no 👍 in its place
    assert [p["text"] for p in sends(b, -100)] == ["ok"]
    # Telegram not answering at all: the same
    b.calls.clear()
    sent = b.call

    async def hangs(client, method, _tries=0, **params):
        if method == "setMessageReaction":
            b.calls.append((method, params))
            raise TelegramError(method, "Gateway Timeout", None)
        return await sent(client, method, _tries, **params)

    b.call = hangs
    run(b.handle(None, msg("@chefbot y mañana", message_id=2)))
    assert [p["text"] for p in sends(b, -100)] == ["ok"]


def test_a_slow_start_eye_never_delays_the_ai(data_dir, tmp_path, monkeypatch):
    b, _ = setup(data_dir, tmp_path)
    watched(b, monkeypatch)
    sent = b.call

    async def slow(client, method, _tries=0, **params):
        if method == "setMessageReaction":  # Telegram answers only once the answer went out
            await until(lambda: sends(b, -100), timeout=10)
        return await sent(client, method, _tries, **params)

    b.call = slow

    async def go():
        await b.handle(None, msg("@chefbot qué cenamos", message_id=1))
        await drain(b)

    run(go())
    # the AI was called and answered while the reaction was still on its way
    assert [m for m, _ in b.calls if m != "sendChatAction"] == [
        AI,
        "sendMessage",
        "setMessageReaction",
    ]


def test_a_refused_start_eye_is_tried_again_at_a_silent_end_then_a_thumb(data_dir, tmp_path):
    b, _ = everyone(data_dir, tmp_path)
    refusing(b, "👀")
    cfg(data_dir, text="[no reply]")
    run(b.handle(None, msg("llego a las 8", user=8, message_id=1)))
    assert reactions(b) == [(1, "👀"), (1, "👀"), (1, "👍")] and sends(b) == []


def test_a_reaction_only_answer_replaces_the_eyes(data_dir, tmp_path):
    b, _ = everyone(data_dir, tmp_path)
    cfg(data_dir, text="```telegram\nreact: 🔥\n```")
    run(b.handle(None, msg("ya está la lasaña", user=8, message_id=1)))
    assert reactions(b) == [(1, "👀"), (1, "🔥")] and sends(b) == []
    # asked mode: on the message the reaction goes on, as before
    (tmp_path / "b2").mkdir()
    b2, _ = setup(data_dir, tmp_path / "b2")
    run(b2.handle(None, msg("@chefbot gracias", message_id=2)))
    cfg(data_dir, text="```telegram\nreact: 🔥\n```")
    b2.calls.clear()
    run(b2.handle(None, msg("@chefbot gracias de nuevo", message_id=3)))
    assert reactions(b2) == [(3, "👀"), (3, "🔥")] and sends(b2) == []


def test_a_failed_turn_leaves_the_eyes_and_asked_mode_still_says_so(data_dir, tmp_path):
    b, _ = setup(data_dir, tmp_path)
    failing(data_dir)
    run(b.handle(None, msg("@chefbot qué cenamos", message_id=1)))
    assert reactions(b) == [(1, "👀")]
    assert [p["text"] for p in sends(b, -100)] == [b.t("bot_error")]
    assert calls(b, "setMessageReaction")[0]["chat_id"] == -100


def test_a_tap_whose_turn_fails_still_goes_with_the_next_turn(data_dir, tmp_path):
    # no 👀 on the bot's question, but the tap itself is a person's message: a failed turn holds it
    b, db = everyone(data_dir, tmp_path)
    cfg(data_dir, text="¿Cuál?\n\n```options\n- Lentejas\n- Garbanzos\n```")
    run(b.handle(None, msg("@chefbot ¿qué hago el lunes?", message_id=1)))
    _, garbanzos = buttons(sends(b, -100)[-1])
    mid = 1000 + len(b.calls)
    failing(data_dir)
    b.calls.clear()
    run(tap(b, garbanzos["callback_data"], 7, mid))
    assert reactions(b) == []
    assert [i.mid for i in b.held[-100]] == [mid]
    assert [x["mid"] for x in db.get_kv("bot.queued")["-100"]] == [mid]


def test_the_echo_of_something_said_to_the_app_gets_no_eyes_after_a_restart(data_dir, tmp_path):
    from .test_turns import restarted

    old, db = everyone(data_dir, tmp_path)
    old.spoken["s1"] = botmod.Spoken("s1", "luis", 7, "qué cenamos", chat=-100)
    echo = {**msg("[voice] qué cenamos", message_id=50), botmod.SPOKEN: "s1"}
    old.take(-100, {"id": 7}, "luis", echo, "qué cenamos", asked=True)
    assert db.get_kv("bot.queued")["-100"][0]["bots_mid"] is True
    new, start = restarted(old)
    run(start())
    assert lines(data_dir, "turns.jsonl") and reactions(new) == []


def test_one_slow_start_eye_never_holds_up_the_others(data_dir, tmp_path):
    b, _ = everyone(data_dir, tmp_path)
    b.debounce = 0.3
    cfg(data_dir, text="Apuntado.")
    sent, landed = b.call, []

    async def call(client, method, _tries=0, **params):
        if method == "setMessageReaction" and params["message_id"] == 1:
            await until(lambda: sends(b, -100), timeout=10)  # stuck until the answer is out
        out = await sent(client, method, _tries, **params)
        if method == "setMessageReaction":
            landed.append((params["message_id"], bool(sends(b, -100))))
        return out

    b.call = call

    async def go():
        first = asyncio.create_task(b.handle(None, msg("quedan 10", user=8, message_id=1)))
        await asyncio.sleep(0.05)
        await b.handle(None, msg("y 2 de sopa", user=8, message_id=2))
        await first
        await drain(b)

    run(go())
    # message 2 had its 👀 before the answer went out, while message 1's was stuck
    assert landed == [(2, False), (1, True)]


def test_a_start_eye_that_lands_after_the_answer_reaction_puts_that_reaction_back(
    data_dir, tmp_path, monkeypatch
):
    b, _ = everyone(data_dir, tmp_path)
    monkeypatch.setattr(botmod, "EYES_WAIT_S", 0.05)
    cfg(data_dir, text="```telegram\nreact: 🔥\n```")
    sent = b.call

    async def call(client, method, _tries=0, **params):
        if method == "setMessageReaction" and params["reaction"][0]["emoji"] == "👀":
            await until(lambda: (1, "🔥") in reactions(b), timeout=10)  # lands after the answer's
        return await sent(client, method, _tries, **params)

    b.call = call

    async def go():
        await b.handle(None, msg("ya está la lasaña", user=8, message_id=1))
        await drain(b)

    run(go())
    assert reactions(b) == [(1, "🔥"), (1, "👀"), (1, "🔥")]  # the answer's reaction is last


def test_a_start_eye_that_lands_while_the_answer_reaction_waits_for_its_reply_loses(
    data_dir, tmp_path, monkeypatch
):
    # Telegram applies the answer's 🔥, then the late 👀, and only then replies to the 🔥
    b, _ = everyone(data_dir, tmp_path)
    monkeypatch.setattr(botmod, "EYES_WAIT_S", 0.05)
    cfg(data_dir, text="```telegram\nreact: 🔥\n```")
    sent = b.call

    async def call(client, method, _tries=0, **params):
        emoji = params.get("reaction", [{}])[0].get("emoji")
        if method == "setMessageReaction" and emoji == "👀":
            await until(lambda: (1, "🔥") in reactions(b), timeout=10)
        out = await sent(client, method, _tries, **params)
        if method == "setMessageReaction" and emoji == "🔥":
            await until(lambda: (1, "👀") in reactions(b), timeout=10)  # its reply comes late
        return out

    b.call = call

    async def go():
        await b.handle(None, msg("ya está la lasaña", user=8, message_id=1))
        await drain(b)

    run(go())
    assert reactions(b)[-1] == (1, "🔥") and reactions(b)[:2] == [(1, "🔥"), (1, "👀")]
