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

        async def ask(client, chat_id, reply_to, asker=None):
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


def test_loose_flag_and_dates_in_catalogues(data_dir):
    import datetime as _dt

    from supermealplanner.stores import catalogs, pick

    cat = dict(CATALOG, checked=_dt.date(2026, 10, 10))
    cat["products"] = dict(CATALOG["products"])
    cat["products"]["lechuga"] = [
        {"name": "Jengibre", "pack": {"qty": 1000, "unit": "g"}, "price_eur": 8.25, "loose": True}
    ]
    write(data_dir, "stores/super.yaml", cat)
    p = pick(catalogs(load(data_dir))["super"], "lechuga", 20, "g")
    assert p.packs == 0.1 and p.cost == 0.83


def test_check_reports_a_broken_catalogue(data_dir, capsys):
    from supermealplanner.cli import main

    write(data_dir, "stores/super.yaml", {"store": "super", "products": {"nope": []}})
    assert main(["--data", str(data_dir), "check"]) == 2
    assert "unknown ingredients" in capsys.readouterr().err
    write(data_dir, "stores/super.yaml", CATALOG)
    assert main(["--data", str(data_dir), "check"]) == 0


# ---------------------------------------------------------------- review fixes


def test_group_members_only_and_chatid_for_household(data_dir, tmp_path):
    data = bot_data(data_dir)
    db = DB(tmp_path / "b.db")
    b = FakeBot(data, db, MONDAY)
    run(b.handle(None, msg("/weight 70", user=999)))  # a guest in the household group
    run(b.handle(None, msg("/chatid", chat=-5, user=999)))  # a stranger anywhere
    assert b.sent == [] and db.recent_chat(-5, 5) == []
    run(b.handle(None, msg("/chatid", chat=-5, user=7)))
    assert "-5" in b.sent[-1][1] and db.recent_chat(-5, 5) == []


def test_ai_context_privacy(data_dir, tmp_path):
    from supermealplanner.context import build

    data = bot_data(data_dir)
    db = DB(tmp_path / "c.db")
    db.set_daily("ana", MONDAY, binge=1, note="secreto de Ana")
    db.set_daily("luis", MONDAY, craving=9)
    private_luis = build(data, db, MONDAY, viewer="luis")
    assert "secreto de Ana" not in private_luis and "Vitamin D" in private_luis
    assert "Hierro" not in private_luis  # Ana's pills
    group = build(data, db, MONDAY, group=True)
    assert "secreto de Ana" not in group and "binge 1" not in group and "craving 9" not in group
    data.settings.bot.share_logs_in_group = True
    assert "secreto de Ana" in build(data, db, MONDAY, group=True)


def test_reminder_is_retried_when_the_send_fails(data_dir, tmp_path):
    data = bot_data(data_dir)
    db = DB(tmp_path / "b.db")
    b = FakeBot(data, db, MONDAY)

    async def failing(client, chat_id, text, reply_to=None, record=True):
        raise RuntimeError("429")

    import supermealplanner.bot as botmod

    class Clock(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return dt.datetime(2026, 10, 12, 10, 2, tzinfo=tz)

    real = botmod.dt.datetime
    botmod.dt.datetime = Clock
    try:
        b.send = failing
        with pytest.raises(RuntimeError):
            run(b.tick_reminders(None))
        assert not db.was_sent("water", MONDAY, "10:00")
        sent = []

        async def ok(client, chat_id, text, reply_to=None, record=True):
            sent.append(text)

        b.send = ok
        run(b.tick_reminders(None))
        assert sent and db.was_sent("water", MONDAY, "10:00")
    finally:
        botmod.dt.datetime = real


def test_bad_reminders_are_rejected_at_load(data_dir):
    write(data_dir, "settings.yaml", {"reminders": [{"id": "x", "text": "hi", "at": ["8:3O"]}]})
    with pytest.raises(DataError, match="not HH:MM"):
        load(data_dir)
    write(
        data_dir,
        "settings.yaml",
        {"reminders": [{"id": "x", "text": "hi {nombre}", "at": ["08:30"]}]},
    )
    with pytest.raises(DataError, match="names"):
        load(data_dir)


def test_safe_never_shows_a_url():
    import httpx

    from supermealplanner.bot import safe

    req = httpx.Request("GET", "https://api.telegram.org/file/bot123:SECRET/voice.ogg")
    e = httpx.HTTPStatusError(
        "boom https://api.telegram.org/file/bot123:SECRET",
        request=req,
        response=httpx.Response(404, request=req),
    )
    assert "SECRET" not in safe(e) and "404" in safe(e)
    assert "SECRET" not in safe(httpx.ConnectError("fail https://x/bot123:SECRET"))


def test_a_deleted_recipe_is_swapped_in_place(data_dir):
    from supermealplanner.planner import plan_for
    from supermealplanner.scheduler import ensure_plan

    data = load(data_dir)
    generate(data, MONDAY, 4).save(plans_dir(data) / f"{MONDAY}.yaml")
    before = plan_for(data, MONDAY)
    gone = before.days[2].lunch
    (data_dir / "recipes" / f"{gone}.yaml").unlink()

    class S:
        def __init__(self):
            self.data = load(data_dir)

        def today(self):
            return MONDAY + dt.timedelta(days=2)

    s = S()
    assert ensure_plan(s) is None  # no new plan, a patch
    after = plan_for(s.data, MONDAY)
    assert all(d.lunch != gone and d.dinner != gone for d in after.days)
    for b, a in zip(before.days, after.days, strict=True):  # everything else is untouched
        if b.lunch != gone:
            assert a.lunch == b.lunch and a.lunch_cook == b.lunch_cook
        if b.dinner != gone:
            assert a.dinner == b.dinner
    assert [s.date for s in after.sessions] == [s.date for s in before.sessions]


def test_a_rule_change_never_rewrites_the_current_plan(data_dir):
    from supermealplanner.planner import plan_for
    from supermealplanner.scheduler import ensure_plan

    data = load(data_dir)
    generate(data, MONDAY, 4).save(plans_dir(data) / f"{MONDAY}.yaml")
    before = plan_for(data, MONDAY).to_dict()
    for p in (data_dir / "recipes").glob("legumbre-*.yaml"):
        r = yaml.safe_load(p.read_text())
        r["freezes"] = False
        p.write_text(yaml.safe_dump(r))

    class S:
        data = load(data_dir)

        def today(self):
            return MONDAY + dt.timedelta(days=3)

    assert ensure_plan(S()) is None
    assert plan_for(S.data, MONDAY).to_dict() == before


def test_an_impossible_plan_does_not_stop_the_target_checks(data_dir, tmp_path):
    from supermealplanner.scheduler import tick
    from supermealplanner.web import App

    for p in (data_dir / "recipes").glob("*.yaml"):
        if "dinner" in yaml.safe_load(p.read_text())["meals"]:
            p.unlink()
    app = App(data_dir)
    tick(app)  # cannot plan (no dinners): logs it and carries on
    assert app.db.baseline("luis") is not None


def test_broken_data_push_keeps_the_last_good_data(data_dir, monkeypatch):
    from supermealplanner.scheduler import pull
    from supermealplanner.web import App

    app = App(data_dir)
    (data_dir / ".git").mkdir()
    monkeypatch.setenv("SMP_GIT_PULL", "1")
    (data_dir / "household.yaml").write_text("people: [")
    pull(app)  # git fails (not a repo), reload fails: the old data stays
    assert app.data.household.person("luis").name == "Luis"


def test_web_rejects_cross_site_posts_redirects_and_bad_input(data_dir, data, monkeypatch):
    from fastapi.testclient import TestClient

    from supermealplanner.web import create_app

    monkeypatch.setenv("SMP_SCHEDULER", "0")
    monkeypatch.setenv("SMP_API_TOKEN", "secret")
    with TestClient(create_app(data_dir)) as c:
        form = {"person": "ana", "day": "2026-10-12", "kg": "58"}
        assert (
            c.post("/log", data=form, headers={"origin": "https://evil.example"}).status_code == 403
        )
        assert c.post("/log", data=form, headers={"origin": "http://testserver"}).status_code == 200
        assert c.post("/log", data={**form, "kg": "500"}).status_code == 400
        assert c.post("/log", data={**form, "craving": "abc"}).status_code == 400
        r = c.get("/who/ana?back=//evil.example", follow_redirects=False)
        assert r.headers["location"] == "/"
        assert (
            c.post(
                "/api/weight", json={}, headers={"Authorization": "Bearer ñ".encode()}
            ).status_code
            == 401
        )
        assert (
            c.post(
                "/api/weight",
                json={"person": "ana", "kg": "x"},
                headers={"Authorization": "Bearer secret"},
            ).status_code
            == 400
        )


def test_restart_leaves_no_loop_behind(data_dir, tmp_path):
    from supermealplanner.bot import Bot, TelegramError

    data = bot_data(data_dir)
    state = FakeState(data, DB(tmp_path / "b.db"), MONDAY)
    b = Bot(state)
    b.token = "x"
    started = []

    async def call(client, method, _tries=0, **params):
        if method == "getUpdates":
            raise TelegramError(method, "Unauthorized", 401)
        return {"username": "chefbot"}

    async def reminders(client):
        started.append(asyncio.current_task())
        await asyncio.Event().wait()  # never ends on its own: only a cancel stops it

    b.call, b.reminders = call, reminders

    async def go():
        import supermealplanner.bot as botmod

        real = botmod.asyncio.sleep
        calls = {"n": 0}

        async def fast(t):
            calls["n"] += 1
            if calls["n"] > 3:
                raise asyncio.CancelledError
            await real(0)

        botmod.asyncio.sleep = fast
        try:
            with pytest.raises(asyncio.CancelledError):
                await b.run()
        finally:
            botmod.asyncio.sleep = real
        await real(0)
        return [t for t in started if not t.done()]

    assert run(go()) == []


def test_group_messages_are_kept_only_when_the_ai_is_asked(data_dir, tmp_path):
    data = bot_data(data_dir)
    db = DB(tmp_path / "b.db")
    b = FakeBot(data, db, MONDAY)
    run(b.handle(None, msg("hablando de otra cosa")))
    assert db.recent_chat(-100, 5) == []
    data.settings.bot.history_scope = "all"
    run(b.handle(None, msg("ahora sí se guarda")))
    assert db.recent_chat(-100, 5)[-1]["text"] == "ahora sí se guarda"


def test_private_chat_hides_other_peoples_glp1(data_dir, tmp_path):
    from supermealplanner.context import build

    data = bot_data(data_dir)
    text = build(data, DB(tmp_path / "c.db"), MONDAY, viewer="ana")
    assert "semaglutide" not in text
    assert "semaglutide" in build(data, DB(tmp_path / "d.db"), MONDAY, viewer="luis")


def test_group_upgrade_survives_a_restart(data_dir, tmp_path):
    from supermealplanner.bot import Bot

    data = bot_data(data_dir)
    db = DB(tmp_path / "b.db")
    db.set_kv("bot.migrated_chat", {"from": -100, "to": -1009})
    assert Bot(FakeState(data, db, MONDAY)).chat_id == -1009
    data.settings.bot.chat_id = -2002  # settings moved on: the old upgrade no longer applies
    assert Bot(FakeState(data, db, MONDAY)).chat_id == -2002


def test_a_new_allergy_reaches_the_plan_but_not_cooked_lunches(data_dir):
    from supermealplanner.planner import plan_for, repair

    data = load(data_dir)
    generate(data, MONDAY, 4).save(plans_dir(data) / f"{MONDAY}.yaml")
    hh = yaml.safe_load((data_dir / "household.yaml").read_text())
    hh["people"][0]["avoid_ingredients"] = ["salmon"]  # Ana now avoids salmon
    (data_dir / "household.yaml").write_text(yaml.safe_dump(hh))
    data = load(data_dir)
    plan = plan_for(data, MONDAY)
    today = MONDAY + dt.timedelta(days=3)
    salmon = {r.id for r in data.recipes.values() if any(i.id == "salmon" for i in r.ingredients)}
    cooked_before = {(d.date, d.lunch) for d in plan.days if d.lunch_cook < today}
    changed, cooked_bad = repair(data, plan, today)
    assert changed > 0
    for d in plan.days:
        if d.date >= today:
            assert d.dinner not in salmon
            if d.lunch_cook >= today:
                assert d.lunch not in salmon
    assert {(d.date, d.lunch) for d in plan.days if d.lunch_cook < today} == cooked_before
    assert repair(data, plan, today) == (0, cooked_bad)  # stable


def test_substitutes_keep_the_protein_and_spread_out(data_dir):
    from supermealplanner.planner import plan_for, repair

    data = load(data_dir)
    generate(data, MONDAY, 4).save(plans_dir(data) / f"{MONDAY}.yaml")
    plan = plan_for(data, MONDAY)
    dinners = [d.dinner for d in plan.days]
    fish = [r for r in set(dinners) if data.recipes[r].protein == "fish"]
    for r in fish:
        (data_dir / "recipes" / f"{r}.yaml").unlink()
    old = {d.date: d.dinner for d in plan.days}
    data = load(data_dir)
    repair(data, plan, MONDAY)
    new = [d.dinner for d in plan.days if old[d.date] in fish]
    assert new and all(n in data.recipes for n in new)
    assert len(set(new)) > 1  # not every slot on one recipe


def test_repair_respects_the_freezer_and_missing_sessions(data_dir):
    from supermealplanner.planner import plan_for, repair

    data = load(data_dir)
    generate(data, MONDAY, 4).save(plans_dir(data) / f"{MONDAY}.yaml")
    plan = plan_for(data, MONDAY)
    plan.sessions = []  # a hand-edited plan with no session entries
    victims = sorted({d.lunch for d in plan.days})[:2]
    for r in victims:
        (data_dir / "recipes" / f"{r}.yaml").unlink(missing_ok=True)
    before = {}
    for d in plan.days:
        before.setdefault((d.lunch, d.lunch_cook), []).append(d.date)
    data = load(data_dir)
    repair(data, plan, MONDAY)
    for d in plan.days:
        r = data.recipes[d.lunch]
        assert (d.date - d.lunch_cook).days <= r.fridge_days or r.freezes, d
    for dates in before.values():  # a pot cooked once still feeds the same dish to all its days
        assert len({plan.day(x).lunch for x in dates}) == 1


def test_pages_survive_a_recipe_without_substitute(data_dir, data, monkeypatch):
    from fastapi.testclient import TestClient

    from supermealplanner.web import create_app

    monkeypatch.setenv("SMP_SCHEDULER", "0")
    plan = generate(data, MONDAY, 4)
    plan.days[0].breakfast = "no-longer-exists"
    plan.save(plans_dir(data) / f"{MONDAY}.yaml")
    with TestClient(create_app(data_dir)) as c:
        assert c.get(f"/?d={MONDAY}").status_code == 200
        assert c.get(f"/week?d={MONDAY}").status_code == 200
        assert c.get(f"/list?d={MONDAY}").status_code == 200


def test_today_and_batch_survive_a_deleted_batch_dish(data_dir, data, monkeypatch):
    from fastapi.testclient import TestClient

    from supermealplanner.web import create_app

    monkeypatch.setenv("SMP_SCHEDULER", "0")
    plan = generate(data, MONDAY, 4)
    for s in plan.sessions:
        for x in s.dishes:
            x.recipe = "deleted-dish"
    plan.save(plans_dir(data) / f"{MONDAY}.yaml")
    with TestClient(create_app(data_dir)) as c:
        for path in (f"/?d={MONDAY}", f"/batch?d={MONDAY}"):
            assert c.get(path).status_code == 200, path


def test_no_batch_reminder_when_every_dish_was_deleted(data_dir, tmp_path):
    from supermealplanner.model import Reminder
    from supermealplanner.planner import plan_for

    data = bot_data(data_dir)
    b = FakeBot(data, DB(tmp_path / "b.db"), MONDAY)
    r = Reminder(id="batch", kind="batch", at=["12:00"])
    plan = plan_for(data, MONDAY)
    day = next(s.date for s in plan.sessions if not s.morning)
    assert b.reminder_text(r, day)
    for s in plan.sessions:
        for x in s.dishes:
            x.recipe = "deleted-dish"
    plan.save(plans_dir(data) / f"{MONDAY}.yaml")
    assert b.reminder_text(r, day) is None


def test_presentation_lists_the_real_reminders(data_dir, tmp_path):
    data = bot_data(data_dir)
    b = FakeBot(data, DB(tmp_path / "b.db"), MONDAY)
    text = run(b.cmd_about("luis", "", {"id": 7}))
    assert "10:00" in text and "08:30" in text and "16:00" in text
    assert "https://meals.example" in text
    run(b.handle(None, msg("/presentacion")))
    assert b.sent[-1][1] == text


def test_private_chats_can_be_switched_off(data_dir, tmp_path):
    data = bot_data(data_dir)
    b = FakeBot(data, DB(tmp_path / "b.db"), MONDAY)
    run(b.handle(None, msg("/hoy", chat=7, user=7)))
    assert len(b.sent) == 1
    data.settings.bot.private_chats = False
    run(b.handle(None, msg("/hoy", chat=7, user=7)))
    assert len(b.sent) == 1
    assert "🔒" not in b.presentation()


def test_profile_sync_sets_menus_in_both_languages(data_dir, tmp_path):
    data = bot_data(data_dir)
    data.settings.locale = "es"
    b = FakeBot(data, DB(tmp_path / "b.db"), MONDAY)
    calls = []

    async def call(client, method, _tries=0, **params):
        calls.append((method, params))
        return True

    b.call = call
    run(b.sync_profile(None))
    menus = {
        p.get("language_code", "en"): [c["command"] for c in p["commands"]]
        for m, p in calls
        if m == "setMyCommands"
    }
    assert menus["en"][0] == "today" and menus["es"][0] == "hoy" and "presentacion" in menus["es"]
    assert {m for m, _ in calls} == {"setMyCommands", "setMyShortDescription", "setMyDescription"}


def test_today_message_lists_each_meals_pills(data_dir, tmp_path):
    data = bot_data(data_dir)
    b = FakeBot(data, DB(tmp_path / "b.db"), MONDAY)
    text = b.day_text(MONDAY)
    assert "💊 Luis: Vitamin D" in text  # his lunch pill, from the fixture
    assert "Hierro" not in text  # Ana's iron is on waking, not with a meal


def test_binge_promise_only_with_the_pack(data_dir, tmp_path):
    data = bot_data(data_dir)
    b = FakeBot(data, DB(tmp_path / "b.db"), MONDAY)
    line = b.t("bot_about_binge_safe")
    assert line in b.presentation()  # Luis has the pack in the fixture
    data.household.person("luis").packs = []
    assert line not in b.presentation()
