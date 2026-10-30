"""One message per turn: what a change, the cancelled reminders and the check say joins the answer,
the reminder offer too, and an offer replaces the chat's earlier one. The bot's own plan turn puts its
change to the household instead of refusing it, the app's reasons for a refusal reach only the AI,
and a change can add an ingredient the data lacks."""

import json
from pathlib import Path

import pytest
import yaml

from supermealplanner import changes, context, verifier
from supermealplanner.db import DB
from supermealplanner.model import load

from .test_conversations import ChatBot, block, plugin_data, refusals, reply
from .test_menu import committed
from .test_reminders import ahead, is_offer
from .test_reminders import block as reminders
from .test_settings_stores_bot import msg, run
from .test_turns import buttons, sends, tap

ANSWER = (
    "Ana, noted: on Sunday you are 6 🍛 I change the chicken dish like this:\n"
    "- Chicken: the breast you have frozen plus what you buy, about 780 g for 6.\n"
    "- Rice instead of couscous, half basmati and half brown, 35 g of each per person.\n"
    "- The mango sauce: 1 large mango blended with the onion and a little water.\n"
    "- Cook it on Saturday after lunch; it keeps in the fridge until Sunday.\n\n"
    "For Luis: little sauce on his plate, the coconut is high in saturated fat.\n\n"
    "How much breast is left in the freezer? Then the list asks only for what is missing."
)


def replay(data_dir, tmp_path, monkeypatch):
    """A message like the one that got four answers in a row: guests, a renamed recipe with new
    ingredients and steps, the pantry, a reminder made wrong and three new ones."""
    data, _remote = committed(data_dir, tmp_path, monkeypatch)
    db = DB(tmp_path / "b.db")
    b = ChatBot(data, db)
    _, at = ahead(1, 13)
    old = db.add_reminder(-100, "ana", at, "Buy the couscous for Sunday's chicken", 8)
    soak, _ = ahead(1, 21)
    move, _ = ahead(1, 22)
    cook, _ = ahead(2, 16)
    change = {
        "asked": True,
        "set": {"household.guests.sunday_lunch": 6},
        "recipes": {
            "pollo-0": {
                "name": "Chicken curry with mango sauce and rice",
                "ingredients": [{"id": "pollo", "qty": 130}, {"id": "aceite", "qty": 10}],
                "steps": [
                    "Dice the breast and brown it 4 min.",
                    "Blend the mango with the onion and a little water.",
                    "Cook the rice; serve with the sauce.",
                ],
                "nutrition": {"kcal": 630, "protein_g": 38},
            }
        },
        "pantry": {"pollo": 0},
    }
    text = (
        ANSWER
        + "\n\n"
        + block(change)
        + "\n"
        + reminders(
            ("cancel", str(old)),
            (soak, "Soak 150 g of chickpeas for tomorrow's stew", "ana"),
            (move, "Move the chicken from the freezer to the fridge", "ana"),
            (cook, "In the session after lunch: the chicken for Sunday", "ana"),
        )
    )
    reply(data_dir, text=text)
    return b, db, old


def test_a_turn_with_a_change_cancels_and_reminders_sends_one_message(
    data_dir, tmp_path, monkeypatch
):
    b, db, old = replay(data_dir, tmp_path, monkeypatch)
    run(b.handle(None, msg("@chefbot on Sunday we are 6; rice instead of couscous", user=8)))
    (said,) = sends(b, -100)  # one message, not four
    text = said["text"]
    assert db.reminder(old)["state"] == "cancelled"
    # the change in one line, with the recipe's new name and its page, never the field diffs
    assert "✓ Done: recipe «Chicken curry with mango sauce and rice», guests (sunday_lunch)" in text
    assert "https://meals.example/recipe/pollo-0" in text
    assert "Pollo 0" not in text  # the old name nowhere
    assert "→" not in text and "kcal" not in text and "Dice the breast" not in text
    assert "🗑 Reminder removed" in text  # the cancelled one, in the same message
    assert is_offer(said) and len(buttons(said)) == 3  # the offer under it, a button each
    # the long answer reads short: its detail is folded, the question stays in sight
    assert "<blockquote expandable>" in text
    renamed = b.state.data.recipes["pollo-0"]
    assert renamed.name == "Chicken curry with mango sauce and rice"
    assert b.state.data.household.guests["sunday_lunch"] == 6


def test_an_offer_replaces_the_chats_earlier_one(data_dir, tmp_path):
    from .test_reminders import setup as rsetup

    b, db = rsetup(data_dir, tmp_path)
    first, _ = ahead(1, 21)
    second, _ = ahead(2, 21)
    from .test_turns import cfg

    cfg(data_dir, text="Take it out tonight.\n" + reminders((first, "Defrost the chicken", "ana")))
    run(b.handle(None, msg("@chefbot what do I do tonight?", message_id=1)))
    (one,) = sends(b, -100)
    mid = 1000 + next(n + 1 for n, (m, p) in enumerate(b.calls) if p is one)
    cfg(data_dir, text="Then buy it fresh.\n" + reminders((second, "Buy fresh chicken", "ana")))
    run(b.handle(None, msg("@chefbot it stays frozen, I buy it fresh", message_id=2)))
    offers = [o for o in b._offers() if o["chat"] == -100]
    assert [o["items"][0]["text"] for o in offers] == ["Buy fresh chicken"]  # one live offer
    # the old offer sat under an answer: the answer stays, its buttons go
    gone = [p for m, p in b.calls if m == "editMessageReplyMarkup" and p["message_id"] == mid]
    assert gone and gone[-1]["reply_markup"] == {"inline_keyboard": []}
    run(tap(b, buttons(one)[0]["callback_data"], 8, mid))  # a tap on it sets nothing
    assert db.reminders(-100) == []
    # and the AI is told what is still offered, so a new block lists again what holds
    assert "nobody tapped them yet" in b.chat_reminders(-100)
    assert "Buy fresh chicken" in b.chat_reminders(-100)


def test_a_replaced_offer_of_its_own_is_deleted(data_dir, tmp_path):
    from .test_reminders import setup as rsetup

    b, _db = rsetup(data_dir, tmp_path)
    when, _ = ahead(1, 21)
    alone = {"key": "k1", "chat": -100, "msg": 555, "alone": True, "for": [8], "ts": 1e12}
    b.state.db.set_kv("bot.remind", [{**alone, "items": [{"at": 1, "text": "x", "person": None}]}])
    from .test_turns import cfg

    cfg(data_dir, text="Ok.\n" + reminders((when, "Buy fresh chicken", "ana")))
    run(b.handle(None, msg("@chefbot ok", message_id=1)))
    assert ("deleteMessage", {"chat_id": -100, "message_id": 555}) in b.calls


def test_the_bots_own_plan_turn_puts_its_change_to_the_household(data_dir, tmp_path, monkeypatch):
    data, _ = committed(data_dir, tmp_path, monkeypatch)
    db = DB(tmp_path / "b.db")
    b = ChatBot(data, db)
    reply(
        data_dir,
        text="I propose lunch at 14:30.\n"
        + block({"asked": True, "set": {"household.lunch_time": "14:30"}}),
    )
    b.take(-100, {"first_name": b.t("plan_check_who")}, None, None, b.t("plan_check_turn"), True)
    run(b.drive(None, -100))
    said = [p["text"] for p in sends(b, -100)]
    assert b.t("change_members_only") not in said  # never refused to the household
    assert said[0].startswith("I propose lunch at 14:30.") and "Done" not in said[0]
    confirm = sends(b, -100)[-1]
    assert confirm["text"].startswith("Ana, Luis, shall I") and "Tap Yes or No" in confirm["text"]
    assert b.state.data.household.lunch_time == "14:00"  # nobody said yes yet
    mid = 1000 + next(n + 1 for n, (m, p) in enumerate(b.calls) if p is confirm)
    yes = buttons(confirm)[0]["callback_data"]
    run(tap(b, yes, 555, mid))  # not in the household: nothing
    assert b.state.data.household.lunch_time == "14:00"
    run(tap(b, yes, 8, mid))  # anyone in the household may say yes
    assert b.state.data.household.lunch_time == "14:30"


def test_a_refused_change_tells_the_household_plainly_and_the_ai_why(data_dir, tmp_path):
    data = plugin_data(data_dir)
    db = DB(tmp_path / "b.db")
    b = ChatBot(data, db)
    notes = [{"add": {"person": "ana", "text": f"n{i}"}} for i in range(21)]
    reply(data_dir, text="Noted.\n" + block({"asked": True, "notes": notes}))
    run(b.handle(None, msg("@chefbot remember all this", user=8)))
    (said,) = [p["text"] for p in sends(b, -100)]
    assert said.startswith("Noted.") and said.endswith(f"\n\n{b.t('change_invalid')}")
    assert "at most 20" not in said  # the app's own words stay out of the chat
    assert "at most 20" in refusals(db)[-1]  # and reach the AI with the next turn


def test_a_change_can_add_an_ingredient_the_list_then_buys(data_dir, tmp_path, monkeypatch):
    data, _ = committed(data_dir, tmp_path, monkeypatch)
    db = DB(tmp_path / "b.db")
    b = ChatBot(data, db)
    new = {"name": "Basmati", "section": "despensa", "store": "mercado", "unit": "g"}
    items = [{"id": "pollo", "qty": 130}, {"id": "basmati", "qty": 35}]
    reply(
        data_dir,
        text="With basmati.\n"
        + block(
            {
                "asked": True,
                "ingredients": {"basmati": new},
                "recipes": {"pollo-0": {"ingredients": items}},
            }
        ),
    )
    run(b.handle(None, msg("@chefbot basmati in the chicken", user=8)))
    (said,) = [p["text"] for p in sends(b, -100)]
    assert "«Basmati» on the shopping list" in said
    after = load(data_dir)
    assert after.ingredients["basmati"].section == "despensa"
    assert "basmati" in {i.id for i in after.recipes["pollo-0"].ingredients}
    raw = yaml.safe_load((data_dir / "ingredients.yaml").read_text())
    assert raw["basmati"]["store"] == "mercado" and "lenteja" in raw  # the rest kept


@pytest.mark.parametrize(
    ("fields", "why"),
    [
        ({"lenteja": {"name": "x", "section": "despensa", "store": "mercado"}}, "already exists"),
        ({"Bad Id": {"name": "x", "section": "despensa", "store": "mercado"}}, "lowercase"),
        ({"mango": {"name": "Mango", "section": "nowhere", "store": "mercado"}}, "section"),
        ({"mango": {"name": "Mango", "section": "fruteria", "store": "nowhere"}}, "store"),
        ({"mango": {"name": "Mango", "section": "fruteria", "store": "mercado", "x": 1}}, "field"),
        ({"mango": {"section": "fruteria", "store": "mercado"}}, "needs at least a name"),
    ],
)
def test_only_new_well_formed_ingredients_can_be_added(data, fields, why):
    with pytest.raises(changes.ChangeError, match=why):
        changes.parse({"ingredients": fields}, data, "ana")


def test_reminders_set_with_the_ones_an_answer_cancels_are_put_back_to_the_ai(data_dir, tmp_path):
    data = plugin_data(data_dir)
    db = DB(tmp_path / "b.db")
    b = ChatBot(data, db)
    _, at = ahead(1, 13)
    gone = db.add_reminder(-100, "ana", at, "Cook in the morning session", 8)
    kept = db.add_reminder(-100, "ana", at + 60, "Shop for the morning session", 8)
    later = db.add_reminder(-100, "ana", at, "Unrelated", 8)
    with db.conn() as c:  # set by another answer, a day before
        c.execute("UPDATE reminder SET created='2020-01-01T00:00:00+00:00' WHERE id=?", (later,))
    reply(data_dir, text="Moved to 13:00.\n" + reminders(("cancel", str(gone))))
    run(b.handle(None, msg("@chefbot I am out on Saturday mornings", user=8)))
    path = data_dir / "plugins" / "turns.jsonl"
    turns = [json.loads(x) for x in path.read_text().splitlines()]
    assert len(turns) == 2  # the answer went back once
    note = turns[1]["new"][0]["content"]
    assert f"Reminders {kept} were set together with the ones this answer cancels ({gone})" in note
    assert str(later) not in note.split("(")[0]
    assert verifier.left_behind(db, -100, [{"cancel": later}]) == []  # alone: nothing to say


def test_the_rules_keep_sources_in_a_fold_and_reminders_in_step_with_what_moved(data):
    rules = context.rules(data)
    assert "its source goes in a fold" in rules
    assert "names a time or a" in rules and "replaces the offer still open" in rules
    assert "`ingredients`" in rules and 'buy something "without the list"' in rules


def test_nothing_says_what_happens_when_nobody_answers_or_promises_an_own_words_line(data):
    rules = context.rules(data)
    assert "own words" not in rules  # the app adds no such line under the buttons
    locales = Path(context.__file__).parent / "locales"
    about = {
        n: yaml.safe_load((locales / f"{n}.yaml").read_text(encoding="utf-8"))["bot_about_once"]
        for n in ("en", "es")
    }
    assert "do not answer" not in about["en"] and "do nothing" not in about["en"]
    assert "no respondéis" not in about["es"] and "no hago nada" not in about["es"]
    assert "/reminders" in about["en"] and "/recordatorios" in about["es"]


def test_no_text_says_the_cooking_is_this_afternoon():
    """A session may be cooked in the evening: no text pins today's cooking to the afternoon."""
    locales = Path(context.__file__).parent / "locales"
    en = yaml.safe_load((locales / "en.yaml").read_text(encoding="utf-8"))
    es = yaml.safe_load((locales / "es.yaml").read_text(encoding="utf-8"))
    assert not [k for k, v in en.items() if isinstance(v, str) and "this afternoon" in v]
    assert not [
        k for k, v in es.items() if isinstance(v, str) and "Hoy" in v and "por la tarde" in v
    ]
