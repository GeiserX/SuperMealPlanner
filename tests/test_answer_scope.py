"""bot.answer_scope: with "all", a group line without a mention starts a turn as an addressed one
does, reaches the AI marked "[no mention]", and may be answered with only a reaction or with
nothing in words ([no reply]), which leaves the 👀 of the turn's start so no line is left with
nothing."""

import asyncio

from supermealplanner import context
from supermealplanner.bot import TelegramError

from .test_conversations import TO_BOT, ChatBot
from .test_settings_stores_bot import msg, run
from .test_turns import (
    PLUGIN,
    cfg,
    drain,
    feed,
    lines,
    restarted,
    running,
    sends,
    setup,
    until,
)


def calls(b, method):
    return [p for m, p in b.calls if m == method]


def reactions(b, chat=-100):
    """(message id, emoji) of each reaction the bot put in the chat, in order."""
    return [
        (p["message_id"], p["reaction"][0]["emoji"])
        for p in calls(b, "setMessageReaction")
        if p["chat_id"] == chat
    ]


def everyone(data_dir, tmp_path, **bot):
    return setup(data_dir, tmp_path, answer_scope="all", **bot)


# ---------------------------------------------------------------- a line without a mention


def test_a_line_without_a_mention_starts_a_turn_marked_no_mention(data_dir, tmp_path):
    b, db = everyone(data_dir, tmp_path)
    run(b.handle(None, msg("quedan 10 raciones de lasaña", user=8, message_id=1)))
    (turn,) = lines(data_dir, "turns.jsonl")
    assert turn["new"] == ["[no mention] ana: quedan 10 raciones de lasaña"]
    assert turn["chat"] == {"id": -100, "user": 8}
    # logged as not addressed, so the mark stays on it in later turns
    assert db.find_chat(-100, 1)["asked"] == 0
    (answer,) = sends(b, -100)
    assert answer["text"] == "ok" and answer["reply_parameters"]["message_id"] == 1


def test_asked_mode_still_only_logs_a_line_without_a_mention(data_dir, tmp_path):
    b, _ = setup(data_dir, tmp_path, history_scope="all")  # answer_scope asked, the default
    run(b.handle(None, msg("quedan 10 raciones de lasaña", user=8, message_id=1)))
    assert lines(data_dir, "turns.jsonl") == [] and sends(b) == []
    run(b.handle(None, msg("@chefbot hola", message_id=2)))
    assert lines(data_dir, "turns.jsonl")[-1]["new"] == [
        "[not to you] ana: quedan 10 raciones de lasaña\nluis: @chefbot hola"
    ]


def test_a_burst_of_lines_without_a_mention_is_one_turn(data_dir, tmp_path):
    b, _ = everyone(data_dir, tmp_path)
    b.debounce = 0.3
    cfg(data_dir, text="```telegram\nreact: 👍\n```")

    async def go():
        first = asyncio.create_task(b.handle(None, msg("quedan 10 raciones", user=8, message_id=1)))
        await asyncio.sleep(0.1)
        await b.handle(None, msg("y he sacado 100 g de merluza", user=8, message_id=2))
        await first

    run(go())
    assert [t["new"] for t in lines(data_dir, "turns.jsonl")] == [
        ["[no mention] ana: quedan 10 raciones\n[no mention] ana: y he sacado 100 g de merluza"]
    ]
    # 👀 on each as the turn starts; the reaction then replaces it on every one, and no text at all
    assert reactions(b) == [(1, "👀"), (2, "👀"), (1, "👍"), (2, "👍")]
    assert sends(b, -100) == []


def test_a_line_without_a_mention_joins_a_running_turn(data_dir, tmp_path):
    b, _ = everyone(data_dir, tmp_path)
    cfg(data_dir, listen=3, until=1)

    async def go():
        first = asyncio.create_task(b.handle(None, msg("@chefbot qué cenamos", message_id=1)))
        await until(lambda: running(data_dir))
        await b.handle(None, msg("hay merluza fuera", user=8, message_id=2))
        await first

    run(go())
    (turn,) = lines(data_dir, "turns.jsonl")
    assert turn["new"] == ["luis: @chefbot qué cenamos"]
    assert turn["incoming"] == ["[no mention] ana: hay merluza fuera"]


def test_a_line_without_a_mention_queued_at_a_restart_is_answered_after_it(data_dir, tmp_path):
    old, db = everyone(data_dir, tmp_path)
    feed(old, {"update_id": 41, "message": msg("quedan 10 raciones", user=8, message_id=5)})

    async def dies_before_the_turn():
        await old.ai.acquire()
        poll = asyncio.create_task(old.poll(None))
        await until(lambda: old.queues.get(-100))
        poll.cancel()

    run(dies_before_the_turn())
    assert db.get_kv("bot.queued")["-100"][0]["heard"] is True
    new, start = restarted(old)
    cfg(data_dir, text="[no reply]")
    run(start())
    assert [t["new"] for t in lines(data_dir, "turns.jsonl")] == [
        ["[no mention] ana: quedan 10 raciones"]
    ]
    # restored as a line without a mention: its deliberate silence is still clean
    assert ChatBot.texts(new) == [] and db.get_kv("bot.queued") is None


def test_messages_of_bots_never_start_a_turn(data_dir, tmp_path):
    b, db = everyone(data_dir, tmp_path, group_open=True)
    other = {"id": 555, "first_name": "OtherBot", "is_bot": True}
    run(b.handle(None, {**msg("beep", message_id=1), "from": other}))
    run(b.handle(None, {**msg("mine", message_id=2), "from": {"id": 99, "first_name": "Chef"}}))
    assert lines(data_dir, "turns.jsonl") == [] and sends(b) == []
    assert db.find_chat(-100, 1)["asked"] == 0  # still kept as context


def test_someone_the_bot_would_not_answer_never_starts_a_turn(data_dir, tmp_path):
    b, _ = everyone(data_dir, tmp_path)  # group_open off: only bot.people
    run(b.handle(None, msg("hola", user=12345, message_id=1)))
    assert lines(data_dir, "turns.jsonl") == [] and sends(b) == []


def test_a_private_chat_is_answered_as_before(data_dir, tmp_path):
    b, _ = everyone(data_dir, tmp_path)
    run(b.handle(None, msg("hola", chat=7, user=7, message_id=1)))
    assert lines(data_dir, "turns.jsonl")[-1]["new"] == ["luis: hola"]


# ---------------------------------------------------------------- what the answer may be


def test_a_reaction_only_answer_sends_no_text(data_dir, tmp_path):
    b, _ = everyone(data_dir, tmp_path)
    cfg(data_dir, text="```telegram\nreact: 👍\n```")
    run(b.handle(None, msg("he sacado la merluza", user=8, message_id=7)))
    r = calls(b, "setMessageReaction")[-1]
    assert (r["chat_id"], r["message_id"]) == (-100, 7)
    assert reactions(b) == [(7, "👀"), (7, "👍")] and sends(b, -100) == []


def test_a_deliberate_no_reply_sends_no_words_and_counts_as_handled(data_dir, tmp_path):
    b, db = everyone(data_dir, tmp_path)
    cfg(data_dir, text="[no reply]")
    run(b.handle(None, msg("¿vienes luego?", user=8, message_id=1)))
    assert len(lines(data_dir, "turns.jsonl")) == 1
    assert sends(b) == [] and reactions(b) == [(1, "👀")]
    assert db.get_kv("bot.queued") is None and not b.queues.get(-100)
    # the conversation keeps the turn: the next one does not send the line again
    cfg(data_dir)
    run(b.handle(None, msg("@chefbot qué cenamos", message_id=2)))
    assert lines(data_dir, "turns.jsonl")[-1]["new"] == ["luis: @chefbot qué cenamos"]
    assert [p["text"] for p in sends(b, -100)] == ["ok"]


def test_the_no_reply_marker_never_reaches_the_chat(data_dir, tmp_path):
    b, _ = everyone(data_dir, tmp_path)
    cfg(data_dir, text="Apuntado: 10 raciones. [No Reply]")
    run(b.handle(None, msg("quedan 10 raciones", user=8, message_id=1)))
    assert [p["text"] for p in sends(b, -100)] == ["Apuntado: 10 raciones."]


def test_a_mention_answered_with_only_no_reply_is_still_an_error(data_dir, tmp_path):
    # a mention always gets words: a silent answer to one is a failed answer, as an empty one was
    b, _ = everyone(data_dir, tmp_path)
    cfg(data_dir, text="[no reply]")
    run(b.handle(None, msg("@chefbot qué cenamos", message_id=1)))
    assert [p["text"] for p in sends(b, -100)] == [b.t("bot_error")]


def test_a_failed_turn_of_lines_without_a_mention_tells_the_chat_nothing(data_dir, tmp_path):
    b, _ = everyone(data_dir, tmp_path)
    (data_dir / "plugins" / "ai.py").write_text(
        "def converse(turn, settings):\n    raise RuntimeError('no_output')\n"
    )
    run(b.handle(None, msg("quedan 10 raciones", user=8, message_id=1)))
    assert sends(b) == []


# ---------------------------------------------------------------- edits


def test_an_edit_of_an_answered_line_without_a_mention_is_handled_as_addressed(data_dir, tmp_path):
    b, db = everyone(data_dir, tmp_path)
    run(b.handle(None, msg("quedan 10 raciones", user=8, message_id=1)))
    run(b.edited(None, msg("quedan 8 raciones", user=8, message_id=1)))
    _first, second = lines(data_dir, "turns.jsonl")
    (new,) = second["new"]
    assert new.startswith("[no mention] ana: [Ana edited a message without a mention.")
    assert "Before: quedan 10 raciones\nNow: quedan 8 raciones" in new
    # it may have had no answer (logged before the mode was on): no claim that it had one
    assert "was: ok" not in new and "already answered" not in new
    assert db.find_chat(-100, 1)["text"] == "quedan 8 raciones"


def test_an_edit_that_only_adds_the_mention_to_an_answered_line_does_nothing(data_dir, tmp_path):
    b, _ = everyone(data_dir, tmp_path)
    run(b.handle(None, msg("quedan 10 raciones", user=8, message_id=1)))
    run(b.edited(None, msg("@chefbot quedan 10 raciones", user=8, message_id=1)))
    assert len(lines(data_dir, "turns.jsonl")) == 1


def test_an_edit_of_a_queued_line_without_a_mention_sends_the_new_text(data_dir, tmp_path):
    b, _ = everyone(data_dir, tmp_path)
    b.debounce = 0.3

    async def go():
        first = asyncio.create_task(b.handle(None, msg("quedan 10", user=8, message_id=1)))
        await asyncio.sleep(0.1)
        await b.edited(None, msg("quedan 8", user=8, message_id=1))
        await first
        await drain(b)

    run(go())
    assert [t["new"] for t in lines(data_dir, "turns.jsonl")] == [["[no mention] ana: quedan 8"]]


# ---------------------------------------------------------------- the rules


def test_the_rules_explain_the_mark_and_the_silence_only_in_all_mode(data):
    asked = context.rules(data)
    assert "[not to you]" in asked and "[no mention]" not in asked and "[no reply]" not in asked
    assert "Act only on what is addressed to you" in asked
    data.settings.bot.answer_scope = "all"
    rules = context.rules(data)
    assert "[not to you]" not in rules
    flat = " ".join(rules.split())
    assert 'starts with "[no mention]"' in flat
    assert "answer with only a reaction (👍: seen and noted)" in flat
    assert "say briefly that you took note" in flat
    assert "write only [no reply], alone" in flat
    assert "always gets an answer in words" in flat
    assert "Act only on what is addressed to you" not in rules  # the principle says the new rule
    assert "the rest of what the household writes is for you too" in flat
    data.settings.locale = "es"
    assert "lo demás que escribe la casa también es para ti" in " ".join(
        context.rules(data).split()
    )


def test_a_reply_to_the_bot_is_not_marked(data_dir, tmp_path):
    b, _ = everyone(data_dir, tmp_path)
    run(b.handle(None, msg("vale", user=8, message_id=1, **TO_BOT)))
    assert lines(data_dir, "turns.jsonl")[-1]["new"] == ["ana: vale"]


# ---------------------------------------------------------------- review fixes


def test_an_edit_of_a_silent_line_without_a_mention_can_stay_silent(data_dir, tmp_path):
    # people fix typos in what they say to each other: that edit may get nothing, cleanly
    b, _ = everyone(data_dir, tmp_path)
    cfg(data_dir, text="[no reply]")
    run(b.handle(None, msg("llego a las 8", user=8, message_id=1)))
    run(b.edited(None, msg("llego a las 9", user=8, message_id=1)))
    assert sends(b) == []
    (note,) = lines(data_dir, "turns.jsonl")[-1]["new"]
    assert note.startswith("[no mention] ana: [Ana edited a message without a mention.")
    assert "you already answered" not in note
    # handled: the note does not go again with the next turn
    cfg(data_dir)
    run(b.handle(None, msg("@chefbot qué cenamos", message_id=2)))
    assert lines(data_dir, "turns.jsonl")[-1]["new"] == ["luis: @chefbot qué cenamos"]


def test_an_edit_during_the_turn_of_a_line_without_a_mention_can_stay_silent(data_dir, tmp_path):
    b, _ = everyone(data_dir, tmp_path)
    cfg(data_dir, listen=3, until=1, cover=False, text="[no reply]")

    async def go():
        first = asyncio.create_task(b.handle(None, msg("llego a las 8", user=8, message_id=1)))
        await until(lambda: running(data_dir))
        await b.edited(None, msg("llego a las 9", user=8, message_id=1))
        await first
        await drain(b)

    run(go())
    turns = lines(data_dir, "turns.jsonl")
    assert turns[0]["incoming"][0].startswith("[no mention] ana: [Ana edited a message of this")
    assert sends(b) == []


def test_a_bare_yes_without_a_mention_to_a_waiting_offer_starts_no_turn(data_dir, tmp_path):
    # the offer's yes keeps its own handling: unaddressed, it is context only, as before
    b, _ = everyone(data_dir, tmp_path)
    cfg(
        data_dir,
        text="I can draft that.",
        proposals=[{"id": "j1:1", "summary": "Add soup.", "confirm_by": "asker"}],
    )
    run(b.handle(None, msg("@chefbot quiero sopa", message_id=1)))
    assert len(lines(data_dir, "turns.jsonl")) == 1
    run(b.handle(None, msg("yes", message_id=3)))
    assert len(lines(data_dir, "turns.jsonl")) == 1 and lines(data_dir, "acts.jsonl") == []
    # someone else's "yes" is ordinary talk: heard as usual
    run(b.handle(None, msg("yes", user=8, message_id=4)))
    assert len(lines(data_dir, "turns.jsonl")) == 2


def test_a_turn_of_lines_without_a_mention_shows_no_typing(data_dir, tmp_path):
    b, _ = everyone(data_dir, tmp_path)
    cfg(data_dir, text="[no reply]")
    run(b.handle(None, msg("llego a las 8", user=8, message_id=1)))
    assert len(lines(data_dir, "turns.jsonl")) == 1
    assert calls(b, "sendChatAction") == []
    run(b.handle(None, msg("@chefbot hola", message_id=2)))
    assert calls(b, "sendChatAction")  # a mention still shows it


def test_the_all_mode_rules_settle_reactions_changes_and_bare_answers(data):
    data.settings.bot.answer_scope = "all"
    flat = " ".join(context.rules(data).split())
    assert "A change you make posts its own short confirmation" in flat
    assert "never [no reply] and never only a reaction" in flat
    assert 'A bare "yes" or "no" without a mention never answers an offer' in flat


def test_an_edit_of_a_line_kept_before_all_mode_carries_no_unrelated_answer(data_dir, tmp_path):
    # logged as context in asked mode, then the mode changes: what the bot said next may be about
    # something else, so the correction quotes no answer
    b, _ = setup(data_dir, tmp_path, history_scope="all")
    run(b.handle(None, msg("llego a las 8", user=8, message_id=1)))
    cfg(data_dir, text="lentejas")
    run(b.handle(None, msg("@chefbot qué cenamos", message_id=2)))
    b.data.settings.bot.answer_scope = "all"
    cfg(data_dir, text="[no reply]")
    run(b.edited(None, msg("llego a las 9", user=8, message_id=1)))
    (note,) = lines(data_dir, "turns.jsonl")[-1]["new"]
    assert note.startswith("[no mention] ana: [Ana edited a message without a mention.")
    assert "lentejas" not in note and "already answered" not in note
    assert sends(b, -100)[-1]["text"] == "lentejas"  # and the edit sent nothing


# ---------------------------------------------------------------- never silent


def burst(b, *texts, mention=False):
    """A burst of group lines from ana (8), one turn; message ids from 1."""
    b.debounce = 0.3

    async def go():
        tasks = []
        for n, text in enumerate(texts, 1):
            text = f"@chefbot {text}" if mention else text
            tasks.append(asyncio.create_task(b.handle(None, msg(text, user=8, message_id=n))))
            await asyncio.sleep(0.05)
        await asyncio.gather(*tasks)
        await drain(b)

    run(go())


def refusing(b, *emojis):
    """Telegram refuses these reactions (the chat's allowed reactions, say)."""
    sent = b.call

    async def call(client, method, _tries=0, **params):
        if method == "setMessageReaction" and params["reaction"][0]["emoji"] in emojis:
            b.calls.append((method, params))
            raise TelegramError(method, "Bad Request: REACTION_INVALID", 400)
        return await sent(client, method, _tries, **params)

    b.call = call


def test_no_reply_marks_every_line_of_the_turn_read(data_dir, tmp_path):
    b, _ = everyone(data_dir, tmp_path)
    cfg(data_dir, text="[no reply]")
    burst(b, "llego a las 8", "y compro pan")
    assert len(lines(data_dir, "turns.jsonl")) == 1
    assert reactions(b) == [(1, "👀"), (2, "👀")] and sends(b) == []


def test_an_empty_answer_of_the_plugin_marks_the_lines_read(data_dir, tmp_path):
    # the provider's own silence (an empty answer) is no different from the marker
    b, _ = everyone(data_dir, tmp_path)
    cfg(data_dir, text="")
    burst(b, "llego a las 8", "y compro pan")
    assert reactions(b) == [(1, "👀"), (2, "👀")] and sends(b) == []


def test_a_text_answer_adds_no_reactions_to_the_start_eyes(data_dir, tmp_path):
    b, _ = everyone(data_dir, tmp_path)
    cfg(data_dir, text="Apuntado.")
    burst(b, "quedan 10 raciones", "y 2 de sopa")
    assert [p["text"] for p in sends(b, -100)] == ["Apuntado."]
    assert reactions(b) == [(1, "👀"), (2, "👀")]


def test_a_refused_read_mark_falls_back_to_a_thumb_once_and_never_to_words(data_dir, tmp_path):
    b, _ = everyone(data_dir, tmp_path)
    refusing(b, "👀")
    cfg(data_dir, text="[no reply]")
    run(b.handle(None, msg("llego a las 8", user=8, message_id=1)))
    # refused at the start, tried again at the silent end, then a thumb
    assert reactions(b) == [(1, "👀"), (1, "👀"), (1, "👍")] and sends(b) == []
    # both refused: logged and left, still no words
    b.calls.clear()
    refusing(b, "👀", "👍")
    run(b.handle(None, msg("llego a las 9", user=8, message_id=2)))
    assert reactions(b) == [(2, "👀"), (2, "👀"), (2, "👍")] and sends(b) == []


def test_a_refused_reaction_only_answer_falls_back_to_a_thumb_not_to_text(data_dir, tmp_path):
    b, _ = everyone(data_dir, tmp_path)
    refusing(b, "🔥")
    cfg(data_dir, text="```telegram\nreact: 🔥\n```")
    run(b.handle(None, msg("ya está la lasaña", user=8, message_id=1)))
    assert reactions(b) == [(1, "👀"), (1, "🔥"), (1, "👍")] and sends(b) == []


def test_a_mention_is_never_answered_with_only_a_read_mark(data_dir, tmp_path):
    b, _ = everyone(data_dir, tmp_path)
    cfg(data_dir, text="[no reply]")
    burst(b, "qué cenamos", "y mañana", mention=True)
    assert [p["text"] for p in sends(b, -100)] == [b.t("bot_error")]
    assert reactions(b) == [(1, "👀"), (2, "👀")]  # the start's, nothing more


def test_a_failed_turn_keeps_its_eyes_and_its_lines_go_with_the_next_one(data_dir, tmp_path):
    b, _ = everyone(data_dir, tmp_path)
    plugin = data_dir / "plugins" / "ai.py"
    plugin.write_text("def converse(turn, settings):\n    raise RuntimeError('no_output')\n")
    run(b.handle(None, msg("quedan 10 raciones", user=8, message_id=1)))
    assert sends(b) == [] and reactions(b) == [(1, "👀")]
    plugin.write_text(PLUGIN)
    cfg(data_dir, text="```telegram\nreact: 👍\n```")
    run(b.handle(None, msg("y 2 de sopa", user=8, message_id=2)))
    (turn,) = lines(data_dir, "turns.jsonl")
    assert turn["new"] == ["[no mention] ana: quedan 10 raciones\n[no mention] ana: y 2 de sopa"]
    # the answer covers the line of the failed turn too; its 👀 went once
    assert reactions(b) == [(1, "👀"), (2, "👀"), (1, "👍"), (2, "👍")] and sends(b) == []
    assert not b.held


def test_asked_mode_reacts_and_stays_quiet_as_before(data_dir, tmp_path):
    b, _ = setup(data_dir, tmp_path)  # answer_scope asked
    cfg(data_dir, text="```telegram\nreact: 👍\n```")
    burst(b, "gracias", "de verdad", mention=True)
    # 👀 on each at the start; the reaction on the newest message only
    assert reactions(b) == [(1, "👀"), (2, "👀"), (2, "👍")]
    b.calls.clear()
    # a turn the bot asked itself that finds nothing to say shows nothing at all
    cfg(data_dir, text="")
    b.take(-100, {"id": 7, "first_name": "Luis"}, "luis", None, "check", True, auto=True)
    run(b.drive(None, -100))
    assert reactions(b) == [] and sends(b) == []
    # a failed turn holds nothing
    (data_dir / "plugins" / "ai.py").write_text(
        "def converse(turn, settings):\n    raise RuntimeError('no_output')\n"
    )
    run(b.handle(None, msg("@chefbot hola", user=8, message_id=9)))
    assert not b.held


def test_a_turn_that_fails_while_posting_holds_the_lines_it_was_handed_too(data_dir, tmp_path):
    b, _ = everyone(data_dir, tmp_path)
    cfg(data_dir, listen=3, until=1, text="Apuntado.")
    sent = b.call
    down = [True]

    async def call(client, method, _tries=0, **params):
        if method == "sendMessage" and down[0]:
            b.calls.append((method, params))
            raise TelegramError(method, "Internal Server Error", 500)
        return await sent(client, method, _tries, **params)

    b.call = call

    async def go():
        first = asyncio.create_task(b.handle(None, msg("quedan 10", user=8, message_id=1)))
        await until(lambda: running(data_dir))
        await b.handle(None, msg("y 2 de sopa", user=8, message_id=2))
        await first

    run(go())
    assert reactions(b) == [(1, "👀"), (2, "👀")]  # only the start's: the turn failed
    down[0] = False
    cfg(data_dir, text="```telegram\nreact: 👍\n```")
    run(b.handle(None, msg("y pan", user=8, message_id=3)))
    assert reactions(b) == [(1, "👀"), (2, "👀"), (3, "👀"), (1, "👍"), (2, "👍"), (3, "👍")]


def test_a_mention_answered_with_a_reaction_keeps_its_words_fallback(data_dir, tmp_path):
    # a reaction the AI chose for a mention goes as it always did (words when Telegram refuses
    # it); the lines without a mention beside it get the reaction too
    b, _ = everyone(data_dir, tmp_path)
    b.debounce = 0.3
    refusing(b, "🔥")
    cfg(data_dir, text="```telegram\nreact: 🔥\n```")

    async def go():
        first = asyncio.create_task(b.handle(None, msg("ya está la lasaña", user=8, message_id=1)))
        await asyncio.sleep(0.05)
        await b.handle(None, msg("@chefbot gracias", user=8, message_id=2))
        await first

    run(go())
    assert len(lines(data_dir, "turns.jsonl")) == 1
    assert [p["text"] for p in sends(b, -100)] == ["🔥"]
    assert sorted(reactions(b)) == [(1, "👀"), (1, "👍"), (1, "🔥"), (2, "👀"), (2, "🔥")]


def test_the_lines_of_a_failed_turn_survive_a_restart(data_dir, tmp_path):
    old, db = everyone(data_dir, tmp_path)
    plugin = data_dir / "plugins" / "ai.py"
    plugin.write_text("def converse(turn, settings):\n    raise RuntimeError('no_output')\n")
    run(old.handle(None, msg("quedan 10 raciones", user=8, message_id=1)))
    assert [x["mid"] for x in db.get_kv("bot.queued")["-100"]] == [1]
    plugin.write_text(PLUGIN)
    cfg(data_dir, text="[no reply]")
    new, start = restarted(old)
    run(start())
    assert reactions(new) == [(1, "👀")]
    assert not (db.get_kv("bot.queued") or {}).get("-100")


def test_a_held_line_waits_for_a_persons_turn_not_the_bots_own(data_dir, tmp_path):
    b, _ = everyone(data_dir, tmp_path)
    plugin = data_dir / "plugins" / "ai.py"
    plugin.write_text("def converse(turn, settings):\n    raise RuntimeError('no_output')\n")
    run(b.handle(None, msg("quedan 10 raciones", user=8, message_id=1)))
    plugin.write_text(PLUGIN)
    cfg(data_dir, text="")
    b.take(-100, {"id": 7, "first_name": "Luis"}, "luis", None, "check", True, auto=True)
    run(b.drive(None, -100))
    # its line stays held: the bot's own turn neither marks it again nor answers for it
    assert reactions(b) == [(1, "👀")] and b.held
    cfg(data_dir, text="```telegram\nreact: 👍\n```")
    run(b.handle(None, msg("y 2 de sopa", user=8, message_id=2)))
    assert reactions(b) == [(1, "👀"), (2, "👀"), (1, "👍"), (2, "👍")]


def test_a_read_mark_lost_to_a_telegram_outage_goes_with_the_next_turn(data_dir, tmp_path):
    b, _ = everyone(data_dir, tmp_path)
    sent = b.call
    down = [True]

    async def call(client, method, _tries=0, **params):
        if method == "setMessageReaction" and down[0]:
            b.calls.append((method, params))
            raise TelegramError(method, "Bad Gateway", 502)
        return await sent(client, method, _tries, **params)

    b.call = call
    cfg(data_dir, text="[no reply]")
    run(b.handle(None, msg("llego a las 8", user=8, message_id=1)))
    assert reactions(b) == [(1, "👀"), (1, "👀"), (1, "👍")] and sends(b) == []
    down[0] = False
    b.calls.clear()
    cfg(data_dir, text="```telegram\nreact: 👍\n```")
    run(b.handle(None, msg("y compro pan", user=8, message_id=2)))
    assert reactions(b) == [(1, "👀"), (2, "👀"), (1, "👍"), (2, "👍")]


def test_a_read_mark_never_goes_on_the_bots_own_voice_echo(data_dir, tmp_path):
    from supermealplanner.bot import Item

    b, _ = everyone(data_dir, tmp_path)
    b.spoken_rows[5] = "s1"  # row 5 is a spoken turn: its message is the bot's own echo
    covered = [Item(5, 1000, 7, "luis", 0.0), Item(6, 2, 8, "ana", 0.0, heard=True)]
    run(b.mark(None, -100, covered, "👀"))
    assert reactions(b) == [(2, "👀")]


def test_a_reaction_reaches_a_mention_written_before_a_line_without_one(data_dir, tmp_path):
    b, _ = everyone(data_dir, tmp_path)
    b.debounce = 0.3
    refusing(b, "🔥")
    cfg(data_dir, text="```telegram\nreact: 🔥\n```")

    async def go():
        first = asyncio.create_task(b.handle(None, msg("@chefbot gracias", user=8, message_id=1)))
        await asyncio.sleep(0.05)
        await b.handle(None, msg("ya está la lasaña", user=8, message_id=2))
        await first

    run(go())
    assert len(lines(data_dir, "turns.jsonl")) == 1
    # the mention keeps its words fallback; the line beside it falls back to a thumb
    assert [p["text"] for p in sends(b, -100)] == ["🔥"]
    assert sorted(reactions(b)) == [(1, "👀"), (1, "🔥"), (2, "👀"), (2, "👍"), (2, "🔥")]
