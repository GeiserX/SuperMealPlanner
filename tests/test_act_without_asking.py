"""bot.ask_first: false. The household does not want to be asked «shall I change this?» or «shall I
remind you?»: a change the AI comes up with is made at once through the same checks a yes goes
through, a reminder it lists is set at once, the bot's own turns act too (still only on the menu,
the pantry, a step's state and the reminders), and whatever still waited for a tap is accepted by
the bot itself at the next tick. Every test has its control: the same thing with the default
setting still asks."""

import asyncio
import datetime as dt
import time

import yaml

from supermealplanner import bot as botmod
from supermealplanner import context, doctor
from supermealplanner.i18n import Translator

from .test_quiet_turns import own_turn
from .test_reminders import ahead, block, is_offer
from .test_settings_stores_bot import run
from .test_turns import cfg, sends
from .test_verifier import say, vsetup
from .test_whose_yes import burst

WED = dt.date(2026, 10, 14)
HAKE = "merluza-cena-1"


def direct(b, on=True):
    b.state.data.settings.bot.ask_first = not on
    return b


def own_idea(day="2026-10-14", recipe=HAKE, **extra):
    """A change block the AI came up with: no `asked`."""
    raw = {"plan": [{"set": {"date": day, "meal": "dinner", "recipe": recipe}}], **extra}
    return "```change\n" + yaml.safe_dump(raw) + "```"


def asks_yes(b, chat=-100):
    return [
        p
        for p in sends(b, chat)
        if any(
            btn["callback_data"].startswith("c:")
            for row in (p.get("reply_markup") or {}).get("inline_keyboard", [])
            for btn in row
        )
    ]


# ---------------------------------------------------------------- a change the AI comes up with


def test_the_ais_own_change_is_made_at_once_with_no_yes_no(data_dir, tmp_path):
    b, _db = vsetup(data_dir, tmp_path)
    direct(b)
    cfg(data_dir, text="Pongo merluza el miércoles.\n" + own_idea())
    say(b, "@chefbot ¿qué cenamos el miércoles?", message_id=1)
    assert doctor.serves(b.data, HAKE, WED)
    assert not b.pending(-100) and not asks_yes(b)


def test_control_by_default_the_ais_own_change_still_waits_for_a_yes(data_dir, tmp_path):
    b, _db = vsetup(data_dir, tmp_path)
    cfg(data_dir, text="Propongo merluza el miércoles.\n" + own_idea())
    say(b, "@chefbot ¿qué cenamos el miércoles?", message_id=1)
    assert not doctor.serves(b.data, HAKE, WED)
    assert b.pending(-100) and asks_yes(b)


def test_a_change_that_would_be_refused_is_still_refused_with_its_reason(data_dir, tmp_path):
    """Someone else's avoided foods: refused as before, never made, never left waiting."""
    from .test_conversations import refusals

    b, db = vsetup(data_dir, tmp_path)
    direct(b)
    block_ = {"set": {"household.people.ana.avoid_ingredients": ["garbanzo"]}}
    cfg(data_dir, text="Hecho.\n```change\n" + yaml.safe_dump(block_) + "```")
    say(b, "@chefbot Ana no come garbanzos", message_id=1)  # Luis writes (user 7)
    assert any("only the person themselves" in r for r in refusals(db))
    assert not b.pending(-100) and not asks_yes(b)
    assert b.t("change_invalid") in "\n".join(p["text"] for p in sends(b, -100))


def test_a_persons_own_change_is_made_only_when_that_person_wrote(data_dir, tmp_path):
    """An unclear turn where Ana and Luis wrote, with a change to Ana's own avoided foods: it is
    made in Ana's name (here, by hand: the test data repo has no write token), never waiting."""
    b, _db = vsetup(data_dir, tmp_path)
    direct(b)
    block_ = {"set": {"household.people.ana.avoid_ingredients": ["garbanzo"]}}
    cfg(data_dir, text="Apuntado.\n```change\n" + yaml.safe_dump(block_) + "```")
    b.debounce = 0.3

    async def go():
        from .test_conversations import TO_BOT
        from .test_settings_stores_bot import msg

        first = asyncio.create_task(
            b.handle(None, msg("@chefbot no quiero garbanzos", user=8, message_id=1))
        )
        await asyncio.sleep(0.1)
        await b.handle(None, msg("@chefbot y la cena?", user=7, message_id=2, **TO_BOT))
        await first

    run(go())
    assert not b.pending(-100) and not asks_yes(b)
    assert any(b.t("change_manual") in p["text"] for p in sends(b, -100))


def test_control_by_default_that_change_waits_for_the_persons_yes(data_dir, tmp_path):
    b, _db = vsetup(data_dir, tmp_path)
    block_ = {"set": {"household.people.ana.avoid_ingredients": ["garbanzo"]}}
    cfg(data_dir, text="Apuntado.\n```change\n" + yaml.safe_dump(block_) + "```")
    asked = burst(b, 8, 7)
    assert b.mention("ana") in asked["text"]
    (waiting,) = b.pending(-100)
    assert waiting["by"] == [8]


# ---------------------------------------------------------------- reminders


def test_a_listed_reminder_is_set_at_once_for_the_person_it_names(data_dir, tmp_path):
    b, db = vsetup(data_dir, tmp_path)
    direct(b)
    when, at = ahead(2, 22)
    cfg(data_dir, text="Sácala la noche antes.\n" + block((when, "Sacar la merluza", "ana")))
    say(b, "@chefbot ¿cuándo saco la merluza?", message_id=1)
    (r,) = db.reminders(-100)
    assert (r["person"], r["at"], r["text"]) == ("ana", at, "Sacar la merluza")
    out = sends(b, -100)
    assert not any(is_offer(p) for p in out)
    assert not any(p.get("reply_markup") for p in out)  # no buttons at all
    assert any("⏰" in p["text"] and "Sacar la merluza" in p["text"] for p in out)


def test_control_by_default_a_listed_reminder_is_only_offered(data_dir, tmp_path):
    b, db = vsetup(data_dir, tmp_path)
    when, _ = ahead(2, 22)
    cfg(data_dir, text="Sácala la noche antes.\n" + block((when, "Sacar la merluza", "ana")))
    say(b, "@chefbot ¿cuándo saco la merluza?", message_id=1)
    assert db.reminders(-100) == []
    assert any(is_offer(p) for p in sends(b, -100))


# ---------------------------------------------------------------- the bot's own turns


def test_an_automatic_turn_acts_at_once_but_only_on_what_it_may_touch(data_dir, tmp_path):
    b, _db = vsetup(data_dir, tmp_path)
    direct(b)
    cfg(data_dir, text="Arreglado.\n" + own_idea(recipes={HAKE: {"name": "Otro nombre"}}))
    who = {"first_name": b.t("plan_check_who")}
    own_turn(b, lambda: b.take(-100, who, None, None, b.tt("plan_check_turn"), True, review=True))
    assert doctor.serves(b.data, HAKE, WED)  # the menu move is made
    assert b.data.recipes[HAKE].name != "Otro nombre"  # the recipe edit is still dropped
    assert not b.pending(-100) and not asks_yes(b)


def test_control_by_default_an_automatic_turns_change_waits(data_dir, tmp_path):
    b, _db = vsetup(data_dir, tmp_path)
    cfg(data_dir, text="Propongo esto.\n" + own_idea())
    who = {"first_name": b.t("plan_check_who")}
    own_turn(b, lambda: b.take(-100, who, None, None, b.tt("plan_check_turn"), True, review=True))
    assert not doctor.serves(b.data, HAKE, WED)
    assert b.pending(-100)


def test_the_automatic_turns_never_say_a_tap_confirms_when_nothing_waits(data_dir, tmp_path):
    b, _db = vsetup(data_dir, tmp_path)
    keys = (
        "plan_check_turn",
        "verify_turn",
        "task_not_done_turn",
        "task_unanswered_turn",
        "task_done_after_all_turn",
        "bot_about_changes",
        "bot_about_once",
    )
    words = {"name": "Ana", "task": "x", "step": "cook", "date": "d", "tasks": "x"}
    words["problems"] = "- x"
    for locale, marks in (("en", ("tap", "your yes")), ("es", ("toque", "vuestro sí"))):
        t = Translator(locale)

        def asks(text, marks=marks):
            return any(m in text for m in marks)

        assert all(asks(t(k, **words)) for k in keys)  # control: the default asks
        assert not any(asks(t(f"{k}_direct", **words)) for k in keys)
    assert b.tt("verify_turn", problems="- x") == b.t("verify_turn", problems="- x")
    direct(b)
    assert b.tt("verify_turn", problems="- x") == b.t("verify_turn_direct", problems="- x")


# ---------------------------------------------------------------- the AI's instructions


def test_the_rules_say_nothing_waits_and_keep_only_questions_of_fact(data_dir, tmp_path):
    b, _db = vsetup(data_dir, tmp_path)
    default = context.rules(b.data)
    assert "makes it only after their yes" in default  # control
    assert "the app posts the" in default and "Doing, not asking" not in default
    direct(b)
    rules = context.rules(b.data)
    for asking in ("makes it only after their yes", "waits for their yes", "the app posts the"):
        assert asking not in rules
    assert "Doing, not asking" in rules
    assert "never ask whether to" in rules and "shall I remind you?" in rules
    # the one kind of question that stays: a fact the bot cannot know, like what is left at home
    assert "How many cooked lentils are left in the fridge?" in rules


# ---------------------------------------------------------------- what waited before


def waiting_change_and_offer(data_dir, tmp_path):
    """With the default setting: a change waiting for a yes and a reminder offer nobody tapped."""
    b, db = vsetup(data_dir, tmp_path)
    when, at = ahead(2, 22)
    cfg(
        data_dir,
        text="Propongo merluza.\n" + own_idea() + "\n" + block((when, "Sacar la merluza", "ana")),
    )
    say(b, "@chefbot ¿qué cenamos el miércoles?", message_id=1)
    assert b.pending(-100) and b._offers()
    b.calls.clear()
    return b, db, at


def test_what_waited_is_accepted_once_by_the_bot_at_the_next_tick(data_dir, tmp_path):
    b, db, at = waiting_change_and_offer(data_dir, tmp_path)
    direct(b)
    run(b.accept_waiting(None))
    assert doctor.serves(b.data, HAKE, WED)
    assert not b.pending(-100) and not b._offers()
    (r,) = db.reminders(-100)
    assert (r["person"], r["at"]) == ("ana", at)
    (said,) = [p for p in sends(b, -100) if p["text"].startswith(b.t("auto_accepted", lines=""))]
    assert "Sacar la merluza" in said["text"]
    # the old messages lose their buttons
    assert any(m == "editMessageReplyMarkup" for m, _ in b.calls)
    b.calls.clear()
    run(b.accept_waiting(None))  # once: nothing more to accept, nothing more said
    assert sends(b) == [] and len(db.reminders(-100)) == 1


def test_control_by_default_the_tick_accepts_nothing(data_dir, tmp_path):
    b, db, _at = waiting_change_and_offer(data_dir, tmp_path)
    run(b.accept_waiting(None))
    assert b.pending(-100) and b._offers() and db.reminders(-100) == []
    assert not doctor.serves(b.data, HAKE, WED) and sends(b) == []


def test_a_waiting_change_that_no_longer_fits_is_dropped_and_a_past_offer_too(data_dir, tmp_path):
    from .test_conversations import refusals

    b, db = vsetup(data_dir, tmp_path)
    item = {
        "key": "k1",
        "change": {"plan": [{"set": {"date": "2026-10-14", "meal": "dinner", "recipe": "nope"}}]},
        "chat": -100,
        "by": [7],
        "asker": "luis",
        "name": "Luis",
        "ts": time.time(),
        "msg": 55,
    }
    b.wait_for(-100, item, lambda i: False)
    past = {"at": time.time() - 5, "text": "Ya pasó", "person": "luis", "done": None}
    offer = {"key": "o1", "chat": -100, "msg": 56, "alone": True, "for": [7], "items": [past]}
    db.set_kv(botmod.REMIND, [{**offer, "ts": time.time()}])
    direct(b)
    run(b.accept_waiting(None))  # never raises
    assert not b.pending(-100) and not b._offers() and db.reminders(-100) == []
    assert any("nope" in r for r in refusals(db))  # the reason goes to the AI
    (said,) = sends(b, -100)
    assert b.t("auto_accept_dropped") in said["text"] and "Ya pasó" not in said["text"]
