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
