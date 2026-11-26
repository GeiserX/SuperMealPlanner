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
