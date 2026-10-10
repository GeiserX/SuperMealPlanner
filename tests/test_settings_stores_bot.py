import asyncio
import datetime as dt
from zoneinfo import ZoneInfo

import pytest
import yaml

from supermealplanner.db import DB
from supermealplanner.i18n import Translator, available
from supermealplanner.model import DataError, load
from supermealplanner.planner import generate, plans_dir
from supermealplanner.portions import shopping_list

from .conftest import MONDAY


def write(data_dir, name, obj):
    (data_dir / name).parent.mkdir(parents=True, exist_ok=True)
    (data_dir / name).write_text(yaml.safe_dump(obj, allow_unicode=True))


def test_every_locale_has_every_key():
    from pathlib import Path

    import supermealplanner

    loc_dir = Path(supermealplanner.__path__[0]) / "locales"
    en = set(yaml.safe_load((loc_dir / "en.yaml").read_text()))
    for loc in available():
        keys = set(yaml.safe_load((loc_dir / f"{loc}.yaml").read_text()))
        assert en - keys == set(), (loc, sorted(en - keys))


def test_translator_falls_back_and_formats():
    t = Translator("es")
    assert t("tab_today") == "Hoy"
    assert t("no-such-key") == "no-such-key"
    assert t.date(MONDAY) == "lunes 12 oct"
    assert t.money(1234.5) == "1.234,50 €"
    assert Translator("en").money(3) == "€3.00"


def test_tag_mapping_lets_recipes_use_their_own_words(data_dir):
    p = data_dir / "recipes" / "legumbre-0.yaml"
    r = yaml.safe_load(p.read_text())
    r["tags"] = ["legumbre"]
    r["protein"] = "legumbre-seca"
    p.write_text(yaml.safe_dump(r))
    write(data_dir, "settings.yaml", {"tags": {"legumbre": "legume", "legumbre-seca": "legume"}})
    data = load(data_dir)
    assert data.recipes["legumbre-0"].has("legume")
    write(data_dir, "settings.yaml", {"tags": {"legumbre": "not-a-tag"}})
    with pytest.raises(DataError, match="not a planner tag"):
        load(data_dir)


def test_settings_reject_unknown_people(data_dir):
    write(data_dir, "settings.yaml", {"bot": {"people": {"nobody": 1}}})
    with pytest.raises(DataError, match="unknown person nobody"):
        load(data_dir)


CATALOG = {
    "store": "super",
    "name": "Super",
    "products": {
        "aceite": [
            {
                "id": "1",
                "name": "AOVE con aditivo",
                "pack": {"qty": 1000, "unit": "ml"},
                "price_eur": 9,
                "clean": False,
            },
            {
                "id": "2",
                "name": "AOVE 1 L",
                "pack": {"qty": 1000, "unit": "ml"},
                "price_eur": 8,
                "clean": True,
            },
        ],
        "tomate": [
            {
                "id": "3",
                "name": "Tomate",
                "pack": {"qty": 1000, "unit": "g"},
                "price_eur": 2,
                "note": "a granel",
            }
        ],
    },
}


def test_store_picks_clean_product_and_counts_packs(data_dir, tmp_path):
    write(data_dir, "stores/super.yaml", CATALOG)
    write(data_dir, "settings.yaml", {"shopping": {"primary": "super"}})
    ing = yaml.safe_load((data_dir / "ingredients.yaml").read_text())
    for v in ing.values():
        v["store"] = "super"
    write(data_dir, "ingredients.yaml", ing)
    data = load(data_dir)
    plan = generate(data, MONDAY, 4)
    shop = shopping_list(data, DB(tmp_path / "x.db"), plan, MONDAY, Translator("es"))
    lines = {line.ingredient.id: line for sl in shop.stores for line in sl.lines}
    oil = lines["aceite"].pick
    assert oil.product.id == "2" and oil.packs >= 1 and oil.cost == oil.packs * 8
    from supermealplanner.stores import catalogs, pick

    tomato = pick(catalogs(data)["super"], "tomate", 1250, "g")
    assert tomato.product.loose and tomato.packs == 1.3 and tomato.cost == 2.6
    assert "kg" in tomato.text(Translator("es"))
    assert shop.stores[0].total > 0


def test_store_view_writes_whole_list_for_one_store(data_dir, tmp_path):
    write(data_dir, "stores/super.yaml", CATALOG)
    data = load(data_dir)
    plan = generate(data, MONDAY, 4)
    shop = shopping_list(data, DB(tmp_path / "x.db"), plan, MONDAY, view="super")
    assert [sl.id for sl in shop.stores] == ["super"]


# ---------------------------------------------------------------- bot


class FakeState:
    def __init__(self, data, db, today):
        self.data, self.db, self._today = data, db, today

    def today(self):
        return self._today


class FakeBot:
    """Bot with Telegram replaced by a list of sent messages."""

    def __new__(cls, data, db, today):
        from supermealplanner.bot import Bot

        b = Bot(FakeState(data, db, today))
        b.token, b.username, b.sent, b.asked = "x", "chefbot", [], []

        async def send(client, chat_id, text, reply_to=None, record=True):
            b.sent.append((chat_id, text))

        async def ask(client, chat_id, reply_to):
            b.asked.append(chat_id)

        async def transcribe(client, voice):
            return "¿qué cenamos hoy?"

        b.send, b.ask, b.transcribe = send, ask, transcribe
        return b


def bot_data(data_dir, **bot):
    write(
        data_dir,
        "settings.yaml",
        {
            "base_url": "https://meals.example",
            "bot": {"enabled": True, "chat_id": -100, "people": {"luis": 7, "ana": 8}, **bot},
            "reminders": [
                {
                    "id": "water",
                    "kind": "message",
                    "text": "Agua, {names}",
                    "at": ["10:00"],
                    "people": ["luis"],
                },
                {
                    "id": "weigh",
                    "kind": "message",
                    "text": "Pesarse",
                    "at": ["08:30"],
                    "days": ["monday"],
                },
                {"id": "pills", "kind": "pills_lunch", "at": ["16:00"]},
                {"id": "defrost", "kind": "defrost", "at": ["21:00"]},
            ],
        },
    )
    data = load(data_dir)
    generate(data, MONDAY, 4).save(plans_dir(data) / f"{MONDAY}.yaml")
    return data


def msg(text=None, chat=-100, user=7, **extra):
    m = {
        "message_id": 1,
        "chat": {"id": chat, "type": "group" if chat < 0 else "private"},
        "from": {"id": user, "first_name": "L"},
    }
    if text is not None:
        m["text"] = text
    m.update(extra)
    return m


def run(coro):
    return asyncio.run(coro)


def test_bot_logs_weight_for_the_sender(data_dir, tmp_path):
    data = bot_data(data_dir)
    db = DB(tmp_path / "b.db")
    b = FakeBot(data, db, MONDAY)
    run(b.handle(None, msg("/peso 81,4")))
    assert db.weights("luis") == [(MONDAY, 81.4)]
    assert "81.4" in b.sent[-1][1]


def test_bot_ignores_strangers_and_other_chats(data_dir, tmp_path):
    data = bot_data(data_dir)
    db = DB(tmp_path / "b.db")
    b = FakeBot(data, db, MONDAY)
    run(b.handle(None, msg("/weight 80", chat=-999)))
    run(b.handle(None, msg("/weight 80", chat=12345, user=12345)))
    assert db.weights("luis") == [] and b.sent == []


def test_ai_wakes_on_mention_wake_word_or_reply_only(data_dir, tmp_path):
    data = bot_data(data_dir, wake_words=["chef"])
    b = FakeBot(data, DB(tmp_path / "b.db"), MONDAY)
    run(b.handle(None, msg("qué bien huele")))
    assert b.asked == []
    run(b.handle(None, msg("Chef, ¿qué hay hoy?")))
    run(b.handle(None, msg("hola @chefbot")))
    run(b.handle(None, msg("vale", reply_to_message={"from": {"username": "chefbot"}})))
    assert len(b.asked) == 3


def test_respond_all_sends_every_message_to_the_ai(data_dir, tmp_path):
    data = bot_data(data_dir, respond="all")
    b = FakeBot(data, DB(tmp_path / "b.db"), MONDAY)
    run(b.handle(None, msg("qué bien huele")))
    assert b.asked == [-100]


def test_voice_notes_are_transcribed_and_go_to_the_ai(data_dir, tmp_path):
    data = bot_data(data_dir)
    db = DB(tmp_path / "b.db")
    b = FakeBot(data, db, MONDAY)
    run(b.handle(None, msg(voice={"file_id": "f"})))
    assert b.asked == [-100]
    assert "cenamos" in db.recent_chat(-100, 5)[-1]["text"]


def test_reminders_fire_once_in_their_window(data_dir, tmp_path):
    data = bot_data(data_dir)
    db = DB(tmp_path / "b.db")
    b = FakeBot(data, db, MONDAY)
    tz = ZoneInfo("Europe/Madrid")
    at = dt.datetime(2026, 10, 12, 10, 3, tzinfo=tz)  # Monday
    assert [r.id for r, _ in b.due(at)] == ["water"]
    assert b.reminder_text(b.due(at)[0][0], MONDAY) == "Agua, Luis"
    assert [r.id for r, _ in b.due(at.replace(hour=8, minute=31))] == ["weigh"]
    assert b.due(at.replace(day=13, hour=8, minute=31)) == []  # weigh-in is Mondays only
    assert b.due(at.replace(hour=10, minute=11)) == []  # window is 10 minutes
    assert db.mark_sent("water", MONDAY, "10:00") and not db.mark_sent("water", MONDAY, "10:00")


def test_pills_reminder_skips_who_already_logged(data_dir, tmp_path):
    data = bot_data(data_dir)
    db = DB(tmp_path / "b.db")
    b = FakeBot(data, db, MONDAY)
    r = next(x for x in data.settings.reminders if x.id == "pills")
    assert "Luis" in b.reminder_text(r, MONDAY)
    run(b.handle(None, msg("/pills")))
    assert b.reminder_text(r, MONDAY) is None


def test_defrost_reminder_only_when_tomorrow_comes_from_the_freezer(data_dir, tmp_path):
    data = bot_data(data_dir)
    db = DB(tmp_path / "b.db")
    b = FakeBot(data, db, MONDAY)
    r = next(x for x in data.settings.reminders if x.id == "defrost")
    from supermealplanner.planner import plan_for

    plan = plan_for(data, MONDAY)
    for d in plan.days[:-1]:
        nxt = plan.day(d.date + dt.timedelta(days=1))
        frozen = (nxt.date - nxt.lunch_cook).days > data.recipes[nxt.lunch].fridge_days
        assert (b.reminder_text(r, d.date) is not None) == frozen


def test_llm_and_transcription_plugins(data_dir):
    (data_dir / "plugins").mkdir()
    (data_dir / "plugins" / "fake.py").write_text(
        "def complete(system, messages, settings):\n    return 'ok:' + messages[-1]['content']\n"
        "async def transcribe(audio, filename, settings):\n    return audio.decode()\n"
    )
    write(
        data_dir,
        "settings.yaml",
        {
            "llm": {"provider": "plugin", "plugin": "plugins/fake.py"},
            "transcription": {"provider": "plugin", "plugin": "plugins/fake.py"},
        },
    )
    from supermealplanner import llm

    data = load(data_dir)
    assert run(llm.complete(data, "sys", [{"role": "user", "content": "hola"}])) == "ok:hola"
    assert run(llm.transcribe(data, b"hola", "v.ogg")) == "hola"
    write(data_dir, "settings.yaml", {"llm": {"provider": "plugin", "plugin": "../outside.py"}})
    with pytest.raises(llm.LLMError, match="inside the data directory"):
        run(llm.complete(load(data_dir), "s", []))


def test_context_mentions_people_and_today(data_dir, tmp_path):
    from supermealplanner.context import build

    data = bot_data(data_dir)
    text = build(data, DB(tmp_path / "c.db"), MONDAY)
    assert "Luis" in text and "Ana" in text and "Never change" in text
