"""bot.ask_first: false. The household does not want to be asked «shall I change this?» or «shall I
remind you?»: a change the AI comes up with is made at once through the same checks a yes goes
through, a reminder it lists is set at once, the bot's own turns act too (still only on the menu,
the pantry, a step's state and the reminders), and whatever still waited for a tap is accepted by
the bot itself at the next tick. Every test has its control: the same thing with the default
setting still asks."""

import datetime as dt
import re
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


def test_in_direct_mode_too_a_persons_own_change_waits_for_their_yes(data_dir, tmp_path):
    """An unclear turn where Ana and Luis wrote, with the AI's change to Ana's own avoided foods:
    never made in a guessed name, it waits for Ana's yes, as with the default."""
    b, _db = vsetup(data_dir, tmp_path)
    direct(b)
    block_ = {"set": {"household.people.ana.avoid_ingredients": ["garbanzo"]}}
    cfg(data_dir, text="Apuntado.\n```change\n" + yaml.safe_dump(block_) + "```")
    asked = burst(b, 8, 7)
    assert b.mention("ana") in asked["text"]
    (waiting,) = b.pending(-100)
    assert waiting["by"] == [8]


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
    for asking in ("makes it only after their yes", "reads its question out", "the app posts the"):
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


# ---------------------------------------------------------------- review fixes


def test_the_ais_own_idea_on_a_persons_own_things_still_waits_for_their_yes(data_dir, tmp_path):
    """Their avoided foods and their pills reminder are theirs: in direct mode too, a change the
    AI came up with waits for that person's Sí/No."""
    b, _db = vsetup(data_dir, tmp_path)
    direct(b)
    for n, sets in enumerate(
        (
            {"household.people.luis.avoid_ingredients": ["espinaca", "garbanzo"]},
            {"settings.reminders.pills": {"at": ["15:00"]}},
        )
    ):
        cfg(data_dir, text="Te propongo esto.\n```change\n" + yaml.safe_dump({"set": sets}) + "```")
        say(b, "@chefbot ¿algo que cambiar?", message_id=10 + n)  # Luis (user 7) writes
        (waiting,) = b.pending(-100)
        assert waiting["by"] == [7] and waiting["change"]["set"] == sets
        b.drop(-100, waiting)
    assert len(asks_yes(b)) == 2
    assert not any(b.t("change_manual") in p["text"] for p in sends(b, -100))


def test_control_a_persons_own_change_they_asked_for_is_made_at_once(data_dir, tmp_path):
    b, _db = vsetup(data_dir, tmp_path)
    direct(b)
    sets = {"household.people.luis.avoid_ingredients": ["espinaca", "garbanzo"]}
    cfg(
        data_dir,
        text="Apuntado.\n```change\n" + yaml.safe_dump({"asked": True, "set": sets}) + "```",
    )
    say(b, "@chefbot no quiero garbanzos", message_id=1)
    assert not b.pending(-100) and not asks_yes(b)
    assert any(b.t("change_manual") in p["text"] for p in sends(b, -100))


def test_a_persons_own_waiting_change_is_not_accepted_by_the_bot(data_dir, tmp_path):
    b, _db = vsetup(data_dir, tmp_path)
    sets = {"household.people.luis.avoid_ingredients": ["espinaca", "garbanzo"]}
    cfg(data_dir, text="Te propongo esto.\n```change\n" + yaml.safe_dump({"set": sets}) + "```")
    say(b, "@chefbot ¿algo?", message_id=1)
    direct(b)
    b.calls.clear()
    run(b.accept_waiting(None))
    assert b.pending(-100) and sends(b) == []


def test_the_principles_stop_offering_with_buttons_in_direct_mode(data_dir, tmp_path):
    b, _db = vsetup(data_dir, tmp_path)
    for locale, offer in (("en", "offer it with buttons"), ("es", "ofrécelo con botones")):
        b.state.data.settings.locale = locale
        direct(b, False)
        default = context.rules(b.data)
        assert offer in default and "every question comes" in context.principles("en")  # control
        direct(b)
        rules = context.rules(b.data)
        assert offer not in rules
        assert len(re.findall(r"(?m)^\d+\. \*\*", context.principles(locale, True))) == 12
    direct(b)
    b.state.data.settings.locale = "en"
    rules = context.rules(b.data)
    assert "every question comes" not in rules and "Never put it as a question" in rules
    assert "A reminders block replaces the offer" not in rules
    direct(b, False)
    assert "A reminders block replaces the offer" in context.rules(b.data)


def served(b, when):
    """The dinner on the day of `when` (the AI's "YYYY-MM-DD HH:MM"), its date. A day past the
    synthetic plan (it starts on a fixed Monday) gets its week drawn, so the test keeps working
    as the real date moves on."""
    from supermealplanner.planner import Plans, generate, plans_dir

    day = dt.date.fromisoformat(when[:10])
    if Plans(b.data).day(day) is None:
        monday = day - dt.timedelta(days=day.weekday())
        generate(b.data, monday, 1).save(plans_dir(b.data) / f"{monday}.yaml")
    return Plans(b.data).day(day).dinner, day.isoformat()


def test_a_step_listed_again_moves_its_reminder_instead_of_adding_a_second(data_dir, tmp_path):
    b, db = vsetup(data_dir, tmp_path)
    direct(b)
    late, late_at = ahead(2, 22)
    early, early_at = ahead(2, 20)
    rid, day = served(b, late)
    db.add_reminder(-100, "luis", late_at, "Sacar la cena", 7, rid, day, "thaw")
    offer = {"key": "o1", "chat": -100, "msg": 77, "alone": True, "for": [7], "items": []}
    db.set_kv(botmod.REMIND, [{**offer, "ts": time.time()}])
    cfg(
        data_dir,
        text="Mejor antes.\n"
        + block((early, "Sacar la cena antes", "", "", f"dish {rid} {day} thaw")),
    )
    say(b, "@chefbot sácala antes", message_id=1)
    (r,) = db.reminders(-100)
    assert (r["at"], r["text"]) == (early_at, "Sacar la cena antes")
    assert not b._offers()  # the open offer is retired
    assert ("deleteMessage", {"chat_id": -100, "message_id": 77}) in b.calls


def test_an_offered_reminder_whose_dish_left_the_menu_is_not_set(data_dir, tmp_path):
    b, db = vsetup(data_dir, tmp_path)
    when, at = ahead(2, 22)
    rid, day = served(b, when)
    gone = {"at": at, "text": "Sacar otra", "person": "luis", "done": None}
    gone.update(recipe="nope", date=day, kind="thaw")
    kept = {"at": at + 600, "text": "Sacar la cena", "person": "luis", "done": None}
    kept.update(recipe=rid, date=day, kind="thaw")
    offer = {"key": "o1", "chat": -100, "msg": 56, "alone": True, "for": [7], "items": [gone, kept]}
    db.set_kv(botmod.REMIND, [{**offer, "ts": time.time()}])
    direct(b)
    checked = []
    real = b.checkup

    async def checkup(client, chat_id=None, before=None, *a, **k):
        checked.append(chat_id)
        return await real(client, chat_id, before, *a, **k)

    b.checkup = checkup
    run(b.accept_waiting(None))
    assert [r["text"] for r in db.reminders(-100)] == ["Sacar la cena"]
    assert checked == [-100]  # the checkup runs where reminders alone were accepted


def waiting_item(key, change, **extra):
    return {
        "key": key,
        "change": change,
        "chat": -100,
        "by": [7],
        "asker": "luis",
        "name": "Luis",
        "ts": time.time(),
        **extra,
    }


def test_a_waiting_change_replaced_meanwhile_is_not_made(data_dir, tmp_path):
    b, _db = vsetup(data_dir, tmp_path)
    first = waiting_item(
        "k1", {"plan": [{"set": {"date": "2026-10-14", "meal": "dinner", "recipe": HAKE}}]}
    )
    second = waiting_item(
        "k2", {"plan": [{"set": {"date": "2026-10-15", "meal": "dinner", "recipe": HAKE}}]}
    )
    b.wait_for(-100, first, lambda i: False)
    b.wait_for(-100, second, lambda i: False)
    direct(b)
    made = []
    real = b.apply

    async def apply(item, asked=False):
        made.append(item["key"])
        b.drop(-100, second)  # a newer answer replaced it while this one was made
        return await real(item, asked)

    b.apply = apply
    run(b.accept_waiting(None))
    assert made == ["k1"]


def test_a_waiting_change_made_by_hand_says_how_not_that_it_was_dropped(data_dir, tmp_path):
    b, _db = vsetup(data_dir, tmp_path)
    b.wait_for(
        -100, waiting_item("k1", {"set": {"household.lunch_time": "14:30"}}), lambda i: False
    )
    direct(b)
    run(b.accept_waiting(None))
    (said,) = sends(b, -100)
    assert b.t("change_manual") in said["text"]
    assert b.t("auto_accept_dropped") not in said["text"]


def test_the_bots_own_turn_moves_a_step_someone_set_instead_of_adding_a_second(data_dir, tmp_path):
    """The morning plan check lists again a step Luis set the night before, with new amounts: the
    one reminder takes the new words, never a second one beside it (two cook reminders for one
    dish, with different amounts, on 4 Nov)."""
    b, db = vsetup(data_dir, tmp_path)
    direct(b)
    when, at = ahead(2, 22)
    rid, day = served(b, when)
    db.add_reminder(-100, "luis", at, "Prepara 160 g de cuscús", 7, rid, day, "cook")
    cfg(
        data_dir,
        text="Arreglado.\n"
        + block((when, "Prepara 240 g de cuscús", "", "", f"dish {rid} {day} cook")),
    )
    who = {"first_name": b.t("plan_check_who")}
    own_turn(b, lambda: b.take(-100, who, None, None, b.tt("plan_check_turn"), True, review=True))
    (r,) = db.reminders(-100)
    assert (r["at"], r["text"], r["by"]) == (at, "Prepara 240 g de cuscús", 7)
