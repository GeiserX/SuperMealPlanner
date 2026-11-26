"""What a change leaves wrong in the plan is fixed by the bot's own turn, and the chat never reads
about it: no «left something to fix» line, no «I left the menu as it was», only a question when
the fix needs a fact no one but the household knows."""

import asyncio

from supermealplanner.planner import plan_for

from .test_act_without_asking import direct
from .test_conversations import block as change
from .test_doctor import D
from .test_quiet_turns import own_turn
from .test_reminders import setup
from .test_settings_stores_bot import msg, run
from .test_turns import cfg, heard, lines, sends


def split(data_dir, b):
    """An answer that cooks Thursday's lunch on Wednesday: the dish is then cooked twice."""
    thu = plan_for(b.data, D(3)).day(D(3))
    set_ = {"date": str(D(3)), "meal": "lunch", "recipe": thu.lunch, "cook": str(D(2))}
    cfg(data_dir, text=change({"asked": True, "plan": [{"set": set_}]}))


def handle_all(b, text):
    async def go():
        await b.handle(None, msg(text, message_id=1))
        await asyncio.gather(*b.tasks)
        await b.drive(None, -100)
        await asyncio.gather(*b.tasks)

    run(go())


def test_a_change_that_leaves_a_problem_is_fixed_without_a_word(data_dir, tmp_path):
    b, _db = setup(data_dir, tmp_path)
    direct(b)
    split(data_dir, b)
    handle_all(b, "@chefbot cook Thursday's lunch on Wednesday")
    said = "\n".join(p["text"] for p in sends(b, -100))
    # MUT: put back the doctor_new line in checkup → the household reads «left something to fix»
    assert "⚠️" not in said and "left something to fix" not in said, said
    turns = lines(data_dir, "turns.jsonl")
    fix = [t for t in turns if any("[Quiet fix" in x for x in t["new"])]
    # MUT: drop the fix_quietly call in checkup → nothing fixes what the change left
    assert len(fix) == 1, turns
    assert "is cooked twice" in "\n".join(fix[0]["new"])
    assert len(sends(b, -100)) == 1  # the person's answer, and nothing of the fix


def test_a_quiet_fix_says_nothing_but_a_question(data_dir, tmp_path):
    b, _db = setup(data_dir, tmp_path)
    direct(b)
    cfg(data_dir, text="Arreglado: he movido la cena.")
    own_turn(b, lambda: b.fix_quietly(None, -100, ["the soak of Friday's dish has no reminder"]))
    # MUT: drop the `"?" not in text` guard → the AI's own words tell the chat of the fix
    assert sends(b, -100) == []
    assert "Arreglado: he movido la cena." in heard(b)  # the AI still knows what it did
    cfg(data_dir, text="¿Cuánto arroz queda en casa?")
    own_turn(b, lambda: b.fix_quietly(None, -100, ["the rice may not last until Friday"]))
    assert [p["text"] for p in sends(b, -100)] == ["¿Cuánto arroz queda en casa?"]


def test_an_automatic_write_starts_no_other_fix(data_dir, tmp_path):
    """Automatic turns never chain: what the quiet fix's own write leaves goes to the AI's memory
    and to the morning's plan check, never to another turn."""
    b, _db = setup(data_dir, tmp_path)
    direct(b)
    split(data_dir, b)
    handle_all(b, "@chefbot cook Thursday's lunch on Wednesday")
    before = len(lines(data_dir, "turns.jsonl"))

    # the problem the split made is there; asked as if a write of the bot's own had just made it
    async def write(fixes):
        await b.checkup(None, -100, set(), fixes=fixes)
        await asyncio.gather(*b.tasks)

    run(write(False))
    # MUT: ignore `fixes` in checkup → a quiet fix starts another, and another
    assert len(lines(data_dir, "turns.jsonl")) == before
    assert "is cooked twice" in heard(b)
    run(write(True))  # a write of a person's turn: one quiet fix runs (the control)
    turns = lines(data_dir, "turns.jsonl")
    assert len(turns) == before + 1 and "[Quiet fix" in turns[-1]["new"][-1], turns[before:]


def test_without_direct_changes_the_line_stays(data_dir, tmp_path):
    """bot.ask_first on: a fix waits for a tap anyway, so the one line still names the problem."""
    b, _db = setup(data_dir, tmp_path)
    split(data_dir, b)
    handle_all(b, "@chefbot cook Thursday's lunch on Wednesday")
    said = "\n".join(p["text"] for p in sends(b, -100))
    assert "left something to fix" in said


def take_quiet(b, chat=-100):
    b.take(
        chat, {"first_name": "check"}, None, None, "[Quiet fix] x", True, followup=True, silent=True
    )


def test_a_quiet_fix_never_shares_a_turn_with_a_person(data_dir, tmp_path):
    """A person writes while a quiet fix waits: each goes in a turn of its own, in the order they
    came, and neither reads the other's message, so the fix is never answered as theirs, out loud
    and the person's message is never taken by the fix."""
    b, _db = setup(data_dir, tmp_path)
    direct(b)
    cfg(data_dir, text="Hecho, ya está.")

    async def go():
        take_quiet(b)
        b.take(-100, {"id": 7, "first_name": "L"}, "luis", {"message_id": 5}, "@chefbot hola", True)
        await b.drive(None, -100)
        await asyncio.gather(*b.tasks)

    run(go())
    turns = [t["new"] for t in lines(data_dir, "turns.jsonl")]
    # MUT: drop the split in drive → one turn takes both, and the fix's words go to the group
    # MUT: drop the cut in _rows_for → the fix's turn reads «hola», and the person gets no answer
    assert len(turns) == 2, turns
    assert "[Quiet fix]" in turns[0][-1] and not any("hola" in x for x in turns[0]), turns
    assert any("hola" in x for x in turns[1]) and not any("[Quiet fix]" in x for x in turns[1]), (
        turns
    )
    assert [p["text"] for p in sends(b, -100)] == ["Hecho, ya está."]  # the person's answer only


def test_a_quiet_fix_sends_only_the_line_that_asks_and_never_types(data_dir, tmp_path):
    b, _db = setup(data_dir, tmp_path)
    direct(b)
    cfg(data_dir, text="He movido la cena al jueves.\n¿Cuánto arroz queda en casa?")
    own_turn(b, lambda: take_quiet(b))
    # MUT: send the whole text when it holds a "?" → the fix is narrated in the group
    assert [p["text"] for p in sends(b, -100)] == ["¿Cuánto arroz queda en casa?"]
    assert "He movido la cena al jueves." in heard(b)
    # MUT: drop `not quiet` from the typing condition → the group sees the bot typing
    assert not [m for m, _ in b.calls if m == "sendChatAction"], b.calls


def test_a_busy_provider_is_never_told_for_a_quiet_fix(data_dir, tmp_path):
    from supermealplanner.bot import Item

    b, _db = setup(data_dir, tmp_path)
    quiet = Item(1, None, None, None, 0.0, followup=True, silent=True)
    person = Item(2, 5, 7, "luis", 0.0)

    async def go(items):
        await b.rest(None, -100, items, 60)
        for t in b.tasks:
            t.cancel()

    run(go([quiet]))
    # MUT: drop `not _nobody_asked(items)` in rest → «no capacity» posted when nobody wrote
    assert sends(b, -100) == []
    run(go([person]))  # the control: a person's message is told of the wait
    assert len(sends(b, -100)) == 1


def test_quiet_fixes_stop_after_fix_tries_until_a_person_writes(data_dir, tmp_path):
    from supermealplanner.bot import FIX_TRIES

    b, _db = setup(data_dir, tmp_path)
    direct(b)
    cfg(data_dir, text="[no reply]")
    for _ in range(FIX_TRIES + 2):
        own_turn(b, lambda: b.fix_quietly(None, -100, ["x"]))
    # MUT: drop the runs check in quiet_budget → every write and every review starts one more
    assert len(lines(data_dir, "turns.jsonl")) == FIX_TRIES
    assert b.t("fix_gave_up", n=FIX_TRIES) in heard(b)
    run(b.handle(None, msg("@chefbot hola", message_id=9)))  # a person writes: fixes may run again
    before = len(lines(data_dir, "turns.jsonl"))
    own_turn(b, lambda: b.fix_quietly(None, -100, ["x"]))
    assert len(lines(data_dir, "turns.jsonl")) == before + 1


def test_a_quiet_fix_in_a_private_chat_acts_for_its_person(data_dir, tmp_path):
    """A change made in a private chat leaves a problem: the fix there acts as that person, so the
    menu change it writes is made (the group's is the bot's own; a private chat has no bot of its own)."""
    b, _db = setup(data_dir, tmp_path)
    direct(b)
    thu = plan_for(b.data, D(3)).day(D(3))
    wed = plan_for(b.data, D(2)).day(D(2))
    set_ = {"date": str(D(2)), "meal": "dinner", "recipe": thu.lunch}
    cfg(data_dir, text="[no reply]\n" + change({"plan": [{"set": set_}]}))

    async def go():
        b.fix_quietly(None, 7, ["x"])
        await b.drive(None, 7)
        await asyncio.gather(*b.tasks)

    run(go())
    # MUT: take the private fix with who None → «only the household's members» refuses its change
    assert plan_for(b.data, D(2)).day(D(2)).dinner == thu.lunch != wed.dinner
    assert sends(b, 7) == []


LUIS = {"id": 7, "first_name": "L"}


def test_turns_run_in_the_order_the_messages_came(data_dir, tmp_path):
    """A person's message put back at the head of the queue (an answer left it out, the provider
    was busy) ahead of an older quiet fix: the fix still runs before it, alone, so the person's
    turn never reads the fix's words, answers them aloud, and leaves the fix with nothing to do."""
    b, _db = setup(data_dir, tmp_path)
    direct(b)
    cfg(data_dir, text="Hecho, ya está.")

    async def go():
        b.take(-100, LUIS, "luis", {"message_id": 5}, "@chefbot hola", True)
        take_quiet(b)
        b.take(-100, LUIS, "luis", {"message_id": 6}, "@chefbot y otra", True)
        q = b.queues[-100]
        b.queues[-100] = [q[0], q[2], q[1]]  # how ask() puts back what an answer left out
        await b.drive(None, -100)
        await asyncio.gather(*b.tasks)

    run(go())
    turns = ["\n".join(t["new"]) for t in lines(data_dir, "turns.jsonl")]
    # MUT: drop the sort in drive → the second person's turn reads the fix, and the fix runs no turn
    assert len(turns) == 3, turns
    assert "hola" in turns[0] and "[Quiet fix]" not in turns[0], turns
    assert "[Quiet fix]" in turns[1] and "luis" not in turns[1], turns
    assert "y otra" in turns[2] and "[Quiet fix]" not in turns[2], turns
    assert [p["text"] for p in sends(b, -100)] == ["Hecho, ya está."] * 2


def test_a_held_line_goes_before_a_quiet_fix_that_would_read_it(data_dir, tmp_path):
    """A person's line a failed turn held (bot.answer_scope all) is older than the quiet fix that
    comes next: it gets its own turn first, so the fix never reads it as taken and leaves it
    unanswered."""
    b, _db = setup(data_dir, tmp_path)
    direct(b)
    cfg(data_dir, text="Hecho, ya está.")

    async def go():
        b.take(-100, LUIS, "luis", {"message_id": 5}, "@chefbot hola", True)
        b.held[-100] = b.queues.pop(-100)  # its turn failed before anything showed
        take_quiet(b)
        await b.drive(None, -100)
        await asyncio.gather(*b.tasks)

    run(go())
    turns = ["\n".join(t["new"]) for t in lines(data_dir, "turns.jsonl")]
    # MUT: drop the held-first step in drive → the fix reads «hola», and Luis is never answered
    assert len(turns) == 2, turns
    assert "hola" in turns[0] and "[Quiet fix]" not in turns[0], turns
    assert "[Quiet fix]" in turns[1] and "hola" not in turns[1], turns
    assert [p["text"] for p in sends(b, -100)] == ["Hecho, ya está."]


def test_a_quiet_fix_line_with_a_link_to_the_app_is_not_a_question(data_dir, tmp_path):
    b, _db = setup(data_dir, tmp_path)
    direct(b)
    told = "He movido la cena: [semana](https://meals.example/week?d=2026-10-14)"
    asks = '¿Queda arroz en <a href="https://meals.example/list?d=2026-10-14">casa</a>?'
    cfg(data_dir, text=f"{told}\n{asks}")
    own_turn(b, lambda: take_quiet(b))
    said = [p["text"] for p in sends(b, -100)]
    # MUT: count any "?" as a question → the line that tells of the fix goes to the chat
    assert len(said) == 1 and "semana" not in said[0] and "arroz" in said[0], said


# ------------------------------------------------- a failed or busy quiet fix (conversations' AI)


def conv_bot(data_dir, tmp_path):
    from supermealplanner.db import DB

    from .test_conversations import ChatBot, plugin_data

    return direct(ChatBot(plugin_data(data_dir), DB(tmp_path / "c.db")))


def news(data_dir):
    from .test_conversations import turns

    return ["\n".join(m["content"] for m in t["new"]) for t in turns(data_dir)]


def test_a_failed_quiet_fix_is_never_read_by_a_persons_turn(data_dir, tmp_path):
    from .test_conversations import ChatBot, reply

    b = conv_bot(data_dir, tmp_path)
    reply(data_dir, fail=True)
    run(own_turn_in(b, 7, lambda: b.fix_quietly(None, 7, ["the rice may not last"])))
    reply(data_dir, text="hola Luis")
    run(b.handle(None, msg("hola", chat=7, user=7)))
    # MUT: drop `quiet` from _rows_for → Luis's turn reads the fix's words and may answer them
    assert "Quiet fix" not in news(data_dir)[-1] and "hola" in news(data_dir)[-1], news(data_dir)
    assert ChatBot.texts(b, 7) == ["hola Luis"]  # no error told for the fix


def test_a_failed_quiet_fix_goes_again(data_dir, tmp_path, monkeypatch):
    from supermealplanner import bot as botmod

    from .test_conversations import ChatBot, reply

    monkeypatch.setattr(botmod, "HELD_RETRY_S", (0, 0, 0))
    b = conv_bot(data_dir, tmp_path)
    reply(data_dir, fail=True)

    async def go():
        b.fix_quietly(None, 7, ["the rice may not last"])
        await b.drive(None, 7)
        reply(data_dir, text="Arreglado.")  # the provider is back for the retry
        while [t for t in b.tasks if not t.done()]:
            await asyncio.gather(*b.tasks)

    run(go())
    fixes = [x for x in news(data_dir) if "the rice may not last" in x]
    # MUT: drop the retry_quiet spawn → one failed try, and the problem stays unfixed
    assert len(fixes) == 2, news(data_dir)
    assert ChatBot.texts(b, 7) == []


def test_a_busy_quiet_fix_tells_no_one_but_a_person_who_writes_hears_of_the_wait(
    data_dir, tmp_path
):
    from .test_conversations import ChatBot, reply

    b = conv_bot(data_dir, tmp_path)
    now = [1000.0]
    b.clock = lambda: now[0]
    reply(data_dir, busy=600)
    run(own_turn_in(b, 7, lambda: b.fix_quietly(None, 7, ["x"])))
    # MUT: tell the wait for every turn → the chat reads «busy» for a fix it never heard of
    assert ChatBot.texts(b, 7) == []
    run(b.handle(None, msg("hola", chat=7, user=7)))
    # MUT: drop the notice at drive's resting check → Luis waits ten minutes without a word
    assert ChatBot.texts(b, 7) == [b.t("bot_busy", wait=b.t("busy_minutes", n=10))]


def own_turn_in(b, chat, start):
    """The turn `start` queues, and only it: the retry or the wake it leaves is cancelled."""

    async def go():
        start()
        await b.drive(None, chat)

    return go()
