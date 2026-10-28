"""The AI as one ongoing conversation per chat, Telegram formatting, and settings changes from the chat."""

import asyncio
import json
import logging
import re
import subprocess
import time

import httpx
import pytest
import yaml

from supermealplanner import changes, render
from supermealplanner.bot import PENDING_TTL, Bot, TelegramError, to_html
from supermealplanner.db import DB
from supermealplanner.i18n import Translator
from supermealplanner.model import load

from .conftest import MONDAY
from .test_settings_stores_bot import FakeState, bot_data, msg, run, write

PLUGIN = """
import json, pathlib
HERE = pathlib.Path(__file__).parent

def converse(turn, settings):
    with (HERE / "turns.jsonl").open("a") as f:
        f.write(json.dumps({k: v for k, v in turn.items() if not callable(v)}) + "\\n")
    cfg = json.loads((HERE / "reply.json").read_text()) if (HERE / "reply.json").exists() else {}
    if cfg.get("fail"):
        raise RuntimeError("provider down")
    if cfg.get("busy"):
        e = RuntimeError("no capacity")
        e.retry_after = cfg["busy"]
        raise e
    return {"text": cfg.get("text", "ok"),
            "conversation": cfg.get("conversation", turn["conversation"] or "c1"),
            "kept": cfg.get("kept", True),
            "proposals": cfg.get("proposals", [])}

def forget(conversation, settings):
    (HERE / "forgot.txt").write_text(conversation)

def act(proposal, settings):
    with (HERE / "acts.jsonl").open("a") as f:
        f.write(json.dumps(proposal) + "\\n")
    out = proposal.get("then", "done " + proposal["id"])
    if out == "fail":
        raise RuntimeError("the remote side refused")
    return out
"""


class ChatBot:
    """A real Bot whose Telegram calls are recorded instead of sent."""

    def __new__(cls, data, db):
        b = Bot(FakeState(data, db, MONDAY))
        b.token, b.username, b.bot_id, b.calls = "x", "chefbot", 99, []
        b.debounce = 0  # a test's messages go at once unless it sets one

        async def call(client, method, _tries=0, **params):
            b.calls.append((method, params))
            if params.get("parse_mode") == "HTML" and "<bad>" in params.get("text", ""):
                raise TelegramError(method, "Bad Request: can't parse entities", 400)
            return {"message_id": 1000 + len(b.calls)} if method == "sendMessage" else {}

        b.call = call
        return b

    @staticmethod
    def texts(b, chat=None):
        return [
            p["text"]
            for m, p in b.calls
            if m == "sendMessage" and (chat is None or p["chat_id"] == chat)
        ]


def plugin_data(data_dir, **bot):
    (data_dir / "plugins").mkdir(exist_ok=True)
    (data_dir / "plugins" / "ai.py").write_text(PLUGIN)
    bot_data(data_dir, **bot)
    raw = yaml.safe_load((data_dir / "settings.yaml").read_text())
    raw["llm"] = {"provider": "plugin", "plugin": "plugins/ai.py"}
    write(data_dir, "settings.yaml", raw)
    return load(data_dir)


def turns(data_dir):
    path = data_dir / "plugins" / "turns.jsonl"
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


def reply(data_dir, **cfg):
    (data_dir / "plugins" / "reply.json").write_text(json.dumps(cfg))


TO_BOT = {"reply_to_message": {"from": {"id": 99}}}  # a reply to one of the bot's messages


def acts(data_dir):
    path = data_dir / "plugins" / "acts.jsonl"
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


def settle(b, message):
    """Handle a message and wait for what it started in the background."""

    async def go():
        await b.handle(None, message)
        while b.tasks:
            await asyncio.gather(*list(b.tasks))

    run(go())


# ---------------------------------------------------------------- one conversation per chat


def test_each_message_sends_only_what_the_provider_has_not_seen(data_dir, tmp_path):
    data = plugin_data(data_dir)
    b = ChatBot(data, DB(tmp_path / "b.db"))
    run(b.handle(None, msg("hola", chat=7, user=7)))
    run(b.handle(None, msg("/hoy", chat=7, user=7)))  # a command reply the AI did not write
    run(b.handle(None, msg("¿y mañana?", chat=7, user=7)))
    first, second = turns(data_dir)
    assert first["conversation"] is None
    assert [m["content"] for m in first["new"]] == ["luis: hola"]
    assert second["conversation"] == "c1"
    # the AI's own answer "ok" is not sent back; the /hoy reply and the new question are
    assert [m["role"] for m in second["new"]] == ["assistant", "user"]
    assert second["new"][-1]["content"] == "luis: ¿y mañana?"
    assert [m["content"] for m in second["history"]] == ["luis: hola", "ok"]
    assert second["rules"] == first["rules"]  # stable, so it is sent once per session
    assert "Today is" in second["context"] and "Today is" not in second["rules"]
    assert any(k.startswith("recipes/") for k in second["files"])


def test_each_chat_has_its_own_conversation(data_dir, tmp_path):
    data = plugin_data(data_dir)
    db = DB(tmp_path / "b.db")
    b = ChatBot(data, db)
    run(b.handle(None, msg("hola", chat=7, user=7)))
    reply(data_dir, conversation="group-1")
    run(b.handle(None, msg("@chefbot hola", chat=-100, user=7)))
    assert turns(data_dir)[-1]["conversation"] is None  # the group starts its own
    assert db.get_kv("bot.conversation.7")["id"] == "c1"
    assert db.get_kv("bot.conversation.-100")["id"] == "group-1"


def test_a_turn_the_provider_did_not_keep_is_sent_again(data_dir, tmp_path):
    data = plugin_data(data_dir)
    b = ChatBot(data, DB(tmp_path / "b.db"))
    run(b.handle(None, msg("hola", chat=7, user=7)))
    reply(data_dir, kept=False)
    run(b.handle(None, msg("primera", chat=7, user=7)))
    reply(data_dir)
    run(b.handle(None, msg("segunda", chat=7, user=7)))
    assert [m["content"] for m in turns(data_dir)[-1]["new"]] == ["luis: primera\nluis: segunda"]


def test_a_failed_answer_is_not_recorded_and_the_message_goes_again(data_dir, tmp_path):
    data = plugin_data(data_dir)
    db = DB(tmp_path / "b.db")
    b = ChatBot(data, db)
    run(b.handle(None, msg("hola", chat=7, user=7)))
    reply(data_dir, fail=True)
    run(b.handle(None, msg("primera", chat=7, user=7)))
    assert ChatBot.texts(b)[-1] == b.t("bot_error")
    assert [r["text"] for r in db.recent_chat(7, 10)] == ["hola", "ok", "primera"]
    reply(data_dir)
    run(b.handle(None, msg("segunda", chat=7, user=7)))
    assert [m["content"] for m in turns(data_dir)[-1]["new"]] == ["luis: primera\nluis: segunda"]


def test_a_busy_provider_is_told_once_and_the_messages_are_answered_when_it_is_back(
    data_dir, tmp_path
):
    data = plugin_data(data_dir)
    db = DB(tmp_path / "b.db")
    b = ChatBot(data, db)
    now = [1000.0]
    b.clock = lambda: now[0]
    run(b.handle(None, msg("hola", chat=7, user=7)))
    reply(data_dir, busy=3960)
    run(b.handle(None, msg("primera", chat=7, user=7)))
    assert ChatBot.texts(b)[-1] == b.t("bot_busy", wait=b.t("busy_minutes", n=66))
    asked = len(turns(data_dir))
    run(b.handle(None, msg("segunda", chat=7, user=7)))
    assert len(turns(data_dir)) == asked  # it waits: no call while the provider is busy
    assert ChatBot.texts(b).count(ChatBot.texts(b)[-1]) == 1  # told once
    assert [x["row"] for x in db.get_kv("bot.queued")["7"]]  # a restart still answers them
    now[0] += 3960
    reply(data_dir, text="ya está")
    run(b.wake(None, 7, 0))
    assert [m["content"] for m in turns(data_dir)[-1]["new"]] == ["luis: primera\nluis: segunda"]
    assert ChatBot.texts(b)[-1] == "ya está"
    assert not (db.get_kv("bot.queued") or {}).get("7")


def test_a_busy_turn_is_answered_when_nobody_writes_again(data_dir, tmp_path):
    data = plugin_data(data_dir)
    db = DB(tmp_path / "b.db")
    b = ChatBot(data, db)
    now = [1000.0]
    b.clock = lambda: now[0]
    run(b.handle(None, msg("hola", chat=7, user=7)))
    reply(data_dir, busy=600)
    run(b.handle(None, msg("primera", chat=7, user=7)))
    assert [x["row"] for x in (db.get_kv("bot.queued") or {}).get("7") or []]
    now[0] += 600
    reply(data_dir, text="ya está")
    run(b.wake(None, 7, 0))
    assert [m["content"] for m in turns(data_dir)[-1]["new"]] == ["luis: primera"]
    assert ChatBot.texts(b)[-1] == "ya está"


def test_a_busy_spell_of_hours_says_hours(data_dir, tmp_path):
    data = plugin_data(data_dir)
    b = ChatBot(data, DB(tmp_path / "b.db"))
    run(b.handle(None, msg("hola", chat=7, user=7)))
    reply(data_dir, busy=3 * 3600)
    run(b.handle(None, msg("otra", chat=7, user=7)))
    assert ChatBot.texts(b)[-1] == b.t("bot_busy", wait=b.t("busy_hours", n=3))


def test_a_provider_that_drops_the_conversation_gets_the_whole_history(data_dir, tmp_path):
    data = plugin_data(data_dir)
    db = DB(tmp_path / "b.db")
    b = ChatBot(data, db)
    run(b.handle(None, msg("hola", chat=7, user=7)))
    reply(data_dir, conversation=None)  # e.g. it expired on the provider's side
    run(b.handle(None, msg("otra", chat=7, user=7)))
    assert db.get_kv("bot.conversation.7") is None
    run(b.handle(None, msg("y otra", chat=7, user=7)))
    last = turns(data_dir)[-1]
    assert last["conversation"] is None and last["history"] == []
    assert last["new"][0]["content"].startswith("luis: hola")


def test_new_starts_over_and_tells_the_provider(data_dir, tmp_path):
    data = plugin_data(data_dir)
    db = DB(tmp_path / "b.db")
    b = ChatBot(data, db)
    run(b.handle(None, msg("hola", chat=7, user=7)))
    run(b.handle(None, msg("/nuevo", chat=7, user=7)))
    assert (data_dir / "plugins" / "forgot.txt").read_text() == "c1"
    assert db.get_kv("bot.conversation.7") is None and db.recent_chat(7, 10) == []
    run(b.handle(None, msg("hola otra vez", chat=7, user=7)))
    last = turns(data_dir)[-1]
    assert last["conversation"] is None
    assert [m["content"] for m in last["new"]] == ["luis: hola otra vez"]


def test_a_plugin_is_loaded_once_per_version_of_its_file(data_dir):
    import os

    from supermealplanner import llm

    (data_dir / "plugins").mkdir()
    path = data_dir / "plugins" / "count.py"
    path.write_text(
        "calls = []\ndef complete(s, m, c):\n    calls.append(1)\n    return len(calls)\n"
    )
    write(data_dir, "settings.yaml", {"llm": {"provider": "plugin", "plugin": "plugins/count.py"}})
    data = load(data_dir)
    assert [run(llm.complete(data, "s", [])) for _ in range(3)] == ["1", "2", "3"]
    path.write_text("def complete(s, m, c):\n    return 'new'\n")
    os.utime(path, ns=(1, 10**18))  # a pull that changed the file
    assert run(llm.complete(data, "s", [])) == "new"


def test_built_in_providers_still_get_rules_context_and_history(data_dir, tmp_path):
    from supermealplanner import llm

    (data_dir / "plugins").mkdir()
    (data_dir / "plugins" / "old.py").write_text(
        "def complete(system, messages, settings):\n"
        "    return str(len(messages)) + ('Today is' in system and 'Changing settings' in system)*'!'\n"
    )
    write(data_dir, "settings.yaml", {"llm": {"provider": "plugin", "plugin": "plugins/old.py"}})
    turn = {
        "conversation": None,
        "rules": "r ## Changing settings",
        "context": "Today is x",
        "history": [],
        "new": [{"role": "assistant", "content": "a"}, {"role": "user", "content": "u"}],
        "files": {},
    }
    out = run(llm.converse(load(data_dir), turn))
    assert out.text == "3!" and out.conversation is None  # "(conversation start)" leads


# ---------------------------------------------------------------- answering rules


def test_private_chats_always_get_an_answer_groups_only_when_called(data_dir, tmp_path):
    data = plugin_data(data_dir, wake_words=["chef"])  # left over in older data: calls nothing
    b = ChatBot(data, DB(tmp_path / "b.db"))
    run(b.handle(None, msg("qué tal", chat=7, user=7)))
    run(b.handle(None, msg("qué tal")))
    run(b.handle(None, msg("qué tal, chef")))  # a former wake word, no mention, no reply
    assert len(turns(data_dir)) == 1 and ChatBot.texts(b, -100) == []
    run(b.handle(None, msg("qué tal", reply_to_message={"from": {"id": 99}})))
    run(b.handle(None, msg("qué tal @ChefBot")))
    assert len(turns(data_dir)) == 3


def test_messages_that_arrive_together_get_one_answer(data_dir, tmp_path):
    data = plugin_data(data_dir)
    b = ChatBot(data, DB(tmp_path / "b.db"))

    async def burst():  # e.g. a backlog after a restart: each message is stored before its turn
        await asyncio.gather(
            *(b.handle(None, msg(t, chat=7, user=7)) for t in ("hola", "primera", "segunda"))
        )

    run(burst())
    assert [[m["content"] for m in t["new"]] for t in turns(data_dir)] == [
        ["luis: hola"],
        ["luis: primera\nluis: segunda"],
    ]
    assert ChatBot.texts(b, 7) == ["ok", "ok"]


def test_a_private_turn_never_carries_someone_elses_health_data(data_dir, tmp_path):
    hh = yaml.safe_load((data_dir / "household.yaml").read_text())
    hh["people"][1]["notes"] = ["nota-privada-de-luis"]
    write(data_dir, "household.yaml", hh)
    data = plugin_data(data_dir)
    b = ChatBot(data, DB(tmp_path / "b.db"))
    run(b.handle(None, msg("hola", chat=8, user=8)))  # Ana, privately
    ana = turns(data_dir)[-1]["context"]
    assert "Hierro" in ana  # her own pills are there
    for other in ("Vitamin D", "injects", "nota-privada-de-luis"):  # Luis's are not
        assert other not in ana
    run(b.handle(None, msg("hola", chat=7, user=7)))  # Luis, privately
    luis = turns(data_dir)[-1]["context"]
    assert "Vitamin D" in luis and "nota-privada-de-luis" in luis and "Hierro" not in luis


def test_a_private_turn_lists_only_the_askers_own_avoided_foods(data_dir, tmp_path):
    data = plugin_data(data_dir)  # Luis avoids espinaca
    b = ChatBot(data, DB(tmp_path / "b.db"))
    run(b.handle(None, msg("hola", chat=8, user=8)))  # Ana, privately
    ana = turns(data_dir)[-1]["context"]
    assert "household.people.ana.avoid_ingredients" in ana
    # the recipe list names a spinach dish; who avoids spinach is what must stay out
    private = ana.split("## Recipes")[0] + ana.split("## Settings now")[1]
    assert "household.people.luis" not in ana and "espinaca" not in private
    run(b.handle(None, msg("hola", chat=7, user=7)))  # Luis, privately
    assert "household.people.luis.avoid_ingredients: ['espinaca']" in turns(data_dir)[-1]["context"]
    run(b.handle(None, msg("@chefbot hola")))  # the group sees the household's settings
    assert "household.people.luis.avoid_ingredients" in turns(data_dir)[-1]["context"]


def test_start_warns_when_the_group_cannot_see_mentions(data_dir, tmp_path, caplog):
    data = bot_data(data_dir)
    b = ChatBot(data, DB(tmp_path / "b.db"))

    async def call(client, method, _tries=0, **params):
        return {"status": "member"}

    b.call = call
    with caplog.at_level(logging.WARNING):
        run(b.check_group(None, {"id": 99, "can_read_all_group_messages": False}))
    assert "privacy mode is on" in caplog.text
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        run(b.check_group(None, {"id": 99, "can_read_all_group_messages": True}))
    assert caplog.text == ""


# ---------------------------------------------------------------- Telegram formatting


def test_markdown_becomes_safe_telegram_html():
    out = to_html(
        "# Plan\n**Lentejas** con _comino_ y *pimentón*\n- [vídeo](https://youtu.be/x?a=1&b=2)\n"
        "* `x<y`\n<script>alert(1)</script> @ana_maria_92 a*b*c [no](javascript:alert(1))"
    )
    assert out.startswith("<b>Plan</b>\n<b>Lentejas</b> con <i>comino</i> y <i>pimentón</i>")
    assert '• <a href="https://youtu.be/x?a=1&amp;b=2">vídeo</a>' in out
    assert "• <code>x&lt;y</code>" in out
    assert "&lt;script&gt;" in out and "<script>" not in out
    assert "@ana_maria_92" in out and "a*b*c" in out
    assert '<a href="javascript' not in out
    assert to_html("```\n<b>\n```") == "<pre>&lt;b&gt;</pre>"


def nested(markup: str) -> bool:
    """Every tag closes in order: what Telegram's HTML parser accepts."""
    open_ = []
    for close, tag in re.findall(r"<(/?)(\w+)[^>]*>", markup):
        if not close:
            open_.append(tag)
        elif not open_ or open_.pop() != tag:
            return False
    return not open_


def test_links_with_parentheses_and_crossed_emphasis_stay_valid():
    wiki = to_html("[w](https://en.wikipedia.org/wiki/Paella_(dish)) y más")
    assert wiki == '<a href="https://en.wikipedia.org/wiki/Paella_(dish)">w</a> y más'
    assert not re.search(r'href="[^"]*<', to_html("[x](https://a.example/`b`)"))  # no tag in a URL
    for md in ("**a *b** c*", "*a **b* c**", "~~a **b~~ c**", "_a **b_ c**", "**a ~~b** c~~"):
        assert nested(to_html(md)), md
    assert to_html("_see **this**_") == "<i>see <b>this</b></i>"  # whole tags may nest
    assert to_html("**a ~~b~~ c**") == "<b>a <s>b</s> c</b>"
    assert to_html("**a\nb**") == "**a\nb**"  # emphasis never spans lines


def test_markup_telegram_refuses_goes_again_as_plain_text(data_dir, tmp_path):
    data = bot_data(data_dir)
    b = ChatBot(data, DB(tmp_path / "b.db"))

    async def go():
        await b.send(None, 7, "**hola**", markdown=True)
        await b.send(None, 7, "`<bad>` hola", markdown=True)

    import supermealplanner.bot as botmod

    real = botmod.to_html
    botmod.to_html = lambda md: real(md) if "bad" not in md else "<bad>"
    try:
        run(go())
    finally:
        botmod.to_html = real
    sent = [(p["text"], p.get("parse_mode")) for m, p in b.calls if m == "sendMessage"]
    assert sent == [("<b>hola</b>", "HTML"), ("<bad>", "HTML"), ("`<bad>` hola", None)]


def test_a_code_block_cut_in_two_stays_a_code_block():
    text = "Mira:\n```python\n" + "\n".join(f"x = {i}  # <b>" for i in range(400)) + "\n```\nFin."
    parts = render.split(text)
    assert len(parts) > 1 and all(len(p) <= 3504 for p in parts)
    for part in parts:
        html = to_html(part)
        assert "```" not in html and "<pre>" in html and nested(html)
    assert parts[1].startswith("```python\n")
    # a fence line longer than a whole piece still ends: no loop
    assert len(render.split("```" + "y" * 5000 + "\n" + "z" * 5000)) >= 3


def test_long_answers_are_cut_at_line_breaks():
    text = "\n".join(f"**línea {i}** " + "x" * 80 for i in range(100))
    parts = render.split(text)
    assert len(parts) > 1 and all(len(p) <= 3500 for p in parts)
    assert all(p.count("**") % 2 == 0 for p in parts)


# ---------------------------------------------------------------- settings changes


def block(change: dict) -> str:
    return "Te lo cambio.\n```change\n" + yaml.safe_dump(change, allow_unicode=True) + "```"


def test_only_allowed_settings_can_change(data):
    for path in (
        "household.people.luis.pills",
        "household.people.luis.pills.lunch",
        "household.people.luis.kcal.start",
        "household.people.luis.kcal.floor",
        "household.people.luis.protein_g.target",
        "household.people.luis.goal.target_kg",
        "household.people.luis.glp1",
        "household.people.luis.notes",
        "settings.llm.plugin",
        "settings.bot.people",
        "settings.bot.wake_words",
        "household.people.luis.avoid_ingredients.0",
    ):
        with pytest.raises(changes.ChangeError, match="cannot be changed from the chat"):
            changes.parse({"set": {path: [1]}}, data, "luis")
    with pytest.raises(changes.ChangeError, match="no person"):
        changes.parse({"set": {"household.people.nadie.avoid_ingredients": []}}, data, "luis")
    with pytest.raises(changes.ChangeError, match="already exists"):
        changes.parse({"recipe": {"id": "gazpacho"}}, data)
    p = changes.parse(
        {
            "set": {
                "household.lunch_time": "14:30",
                "household.people.luis.avoid_ingredients": ["espinaca", "salmon"],
            }
        },
        data,
        "luis",
    )
    lines = changes.describe(p, data, Translator("en"))
    assert lines == [
        "• Lunch time: 14:00 → 14:30",
        "• Foods Luis avoids: espinaca → espinaca, salmon",
    ]


def test_no_allowed_path_reaches_health_data():
    for pattern in changes.ALLOWED:
        for word in ("pills", "kcal", "protein_g", "goal", "glp1", "notes", "packs"):
            assert word not in pattern.split("."), pattern


def test_avoided_foods_only_grow_and_only_on_the_askers_own_list(data):
    luis = "household.people.luis.avoid_ingredients"
    with pytest.raises(changes.ChangeError, match="only the person themselves"):
        changes.parse({"set": {luis: ["espinaca", "salmon"]}}, data, "ana")
    with pytest.raises(changes.ChangeError, match="only the person themselves"):
        changes.parse({"set": {luis: ["espinaca", "salmon"]}}, data)  # nobody asked
    with pytest.raises(changes.ChangeError, match="espinaca can only come off .* by hand"):
        changes.parse({"set": {luis: ["salmon"]}}, data, "luis")
    with pytest.raises(changes.ChangeError, match="by hand"):
        changes.parse({"set": {luis: []}}, data, "luis")
    assert changes.parse({"set": {luis: ["espinaca", "salmon"]}}, data, "luis").edits
    ana = "household.people.ana.avoid_ingredients"
    assert changes.parse({"set": {ana: ["salmon"]}}, data, "ana").edits  # she had none


def test_the_pills_reminder_changes_only_for_the_person_it_names(data_dir):
    # "No me recuerdes esto nunca más" must work: the person named drops their
    # own pills reminder from the chat, nobody sets one for someone else, the pills stay by hand.
    data = bot_data(data_dir)  # reminder "pills" is kind pills_lunch
    pills = next(r for r in data.settings.reminders if r.kind == "pills_lunch")
    pills.people = ["luis"]
    for value in (None, {"enabled": False}, {"at": ["23:00"]}):
        assert changes.parse({"set": {"settings.reminders.pills": value}}, data, "luis").edits
    for value in (None, {"enabled": False}):
        with pytest.raises(changes.ChangeError, match="someone else's pills reminder"):
            changes.parse({"set": {"settings.reminders.pills": value}}, data, "ana")
    with pytest.raises(changes.ChangeError, match="only name the person who asks"):
        changes.parse(
            {"set": {"settings.reminders.pills": {"people": ["luis", "ana"]}}}, data, "luis"
        )
    new = {"kind": "pills_lunch", "at": ["16:00"], "people": ["ana"]}
    with pytest.raises(changes.ChangeError, match="pills reminder"):
        changes.parse({"set": {"settings.reminders.otra": new}}, data, "luis")
    assert changes.parse(
        {"set": {"settings.reminders.otra": {**new, "people": ["luis"]}}}, data, "luis"
    ).edits
    with pytest.raises(
        changes.ChangeError, match="new pills reminder"
    ):  # nor turn one into it for all
        changes.parse({"set": {"settings.reminders.water": {"kind": "pills_lunch"}}}, data, "luis")
    assert changes.parse({"set": {"settings.reminders.water": {"at": ["11:00"]}}}, data, "luis")


def test_a_change_keeps_the_files_comments_and_style(data_dir):
    hh = (data_dir / "household.yaml").read_text()
    (data_dir / "household.yaml").write_text(
        "# who eats here\n" + hh.replace("household: Prueba", "household: Prueba  # the name")
    )
    data = load(data_dir)
    p = changes.parse(
        {"set": {"household.lunch_time": "14:30", "household.guests.sunday": 2}}, data
    )
    out = changes.render(data_dir, p)["household.yaml"]
    assert out.startswith("# who eats here\n") and "household: Prueba  # the name" in out
    assert "lunch_time: '14:30'" in out or 'lunch_time: "14:30"' in out
    assert yaml.safe_load(out)["guests"] == {"sunday": 2}


def git(cwd, *args):
    return subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


@pytest.fixture
def repo(data_dir, tmp_path_factory):
    """The data folder as a clone of a bare repo, the way the app pulls it."""
    remote = tmp_path_factory.mktemp("remote") / "data.git"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(remote)], check=True)
    (data_dir / ".gitignore").write_text("state/\n*.db\n")
    git(data_dir, "init", "-q", "-b", "main")
    git(data_dir, "add", ".")
    git(data_dir, "commit", "-q", "-m", "data")
    git(data_dir, "remote", "add", "origin", str(remote))
    git(data_dir, "push", "-q", "-u", "origin", "main")
    return remote


def test_apply_commits_and_pushes_one_change(data_dir, repo):
    data = bot_data(data_dir)
    git(data_dir, "add", ".")
    git(data_dir, "commit", "-q", "-m", "settings")
    git(data_dir, "push", "-q")
    p = changes.parse(
        {
            "set": {
                "settings.reminders.water": {"at": ["11:00"]},
                "settings.reminders.new": {"kind": "message", "text": "Hola", "at": ["09:00"]},
            },
            "recipe": {
                "id": "lentejas-nuevas",
                "name": "Lentejas nuevas",
                "meals": ["lunch"],
                "kind": "batch",
                "ingredients": [{"id": "lenteja", "qty": 70}],
                "nutrition": {"kcal": 500, "protein_g": 30},
            },
        },
        data,
    )
    sha = changes.apply(data_dir, p, "chat: test", "token")
    assert sha and git(repo, "log", "-1", "--format=%s") == "chat: test"
    new = load(data_dir)
    assert next(r for r in new.settings.reminders if r.id == "water").at == ["11:00"]
    assert next(r for r in new.settings.reminders if r.id == "new").text == "Hola"
    assert "lentejas-nuevas" in new.recipes
    assert git(data_dir, "status", "--porcelain") == ""


def test_a_change_that_does_not_validate_changes_nothing(data_dir, repo):
    data = load(data_dir)
    before = git(data_dir, "rev-parse", "HEAD")
    p = changes.parse(
        {"set": {"household.lunch_time": "14:30", "household.rules.fish_min": "x"}}, data
    )
    with pytest.raises(changes.ChangeError, match="would not validate"):
        changes.apply(data_dir, p, "chat: bad", "token")
    assert (
        git(data_dir, "rev-parse", "HEAD") == before
        and git(data_dir, "status", "--porcelain") == ""
    )
    p = changes.parse(
        {
            "recipe": {
                "id": "rota",
                "name": "Rota",
                "meals": ["lunch"],
                "kind": "batch",
                "ingredients": [{"id": "no-existe", "qty": 1}],
                "nutrition": {"kcal": 1, "protein_g": 1},
            }
        },
        data,
    )
    with pytest.raises(changes.ChangeError, match="unknown ingredient"):
        changes.apply(data_dir, p, "chat: bad", "token")
    assert not (data_dir / "recipes" / "rota.yaml").exists()


def test_a_push_that_fails_leaves_the_folder_as_it_was(data_dir, repo):
    data = load(data_dir)
    before = git(data_dir, "rev-parse", "HEAD")
    git(data_dir, "remote", "set-url", "--push", "origin", str(repo) + "-gone")
    p = changes.parse({"set": {"household.lunch_time": "14:30"}}, data)
    with pytest.raises(changes.ChangeError, match="git push failed"):
        changes.apply(data_dir, p, "chat: x", "token")
    assert git(data_dir, "rev-parse", "HEAD") == before
    assert load(data_dir).household.lunch_time == "14:00"


def test_the_write_token_never_shows_on_a_command_line(data_dir, repo, monkeypatch):
    from supermealplanner import scheduler

    calls = []
    real = subprocess.run

    def spy(cmd, *a, **kw):
        calls.append((cmd, kw.get("env") or {}))
        return real(cmd, *a, **kw)

    monkeypatch.setattr(subprocess, "run", spy)
    data = bot_data(data_dir)
    git(data_dir, "add", ".")
    git(data_dir, "commit", "-q", "-m", "settings")
    git(data_dir, "push", "-q")
    p = changes.parse({"set": {"household.lunch_time": "14:30"}}, data)
    changes.apply(data_dir, p, "chat: x", "sekret-w")
    monkeypatch.setenv("SMP_GIT_PULL", "1")
    monkeypatch.setenv("SMP_GIT_TOKEN", "sekret-r")
    scheduler.pull(type("S", (), {"root": data_dir, "reload": lambda self: None})())
    sent = {
        (next(a for a in cmd[3:] if a in ("pull", "push")), e["GIT_CONFIG_VALUE_0"])
        for cmd, e in calls
        if e.get("GIT_CONFIG_KEY_0") == "http.extraHeader"
    }
    assert sent == {
        ("pull", "Authorization: token sekret-w"),  # the change's pull and push
        ("push", "Authorization: token sekret-w"),
        ("pull", "Authorization: token sekret-r"),  # the hourly pull
    }
    assert not any("sekret" in " ".join(cmd) for cmd, _ in calls)


def test_the_bot_applies_a_change_only_after_the_askers_yes(data_dir, repo, tmp_path, monkeypatch):
    data = plugin_data(data_dir)
    git(data_dir, "add", ".")
    git(data_dir, "commit", "-q", "-m", "settings")
    git(data_dir, "push", "-q")
    monkeypatch.setenv("SMP_GIT_WRITE_TOKEN", "token")
    b = ChatBot(data, DB(tmp_path / "b.db"))
    reply(data_dir, text=block({"set": {"household.lunch_time": "14:30"}}))
    run(b.handle(None, msg("@chefbot comemos a las 14:30")))
    said = ChatBot.texts(b, -100)
    assert "```change" not in said[0] and "Te lo cambio." in said[0]
    assert "Lunch time: 14:00 → 14:30" in said[1] and "Tap Yes or No" in said[1]
    run(b.handle(None, msg("yes", user=8, **TO_BOT)))  # someone else: not their change
    run(b.handle(None, msg("ok, thanks", **TO_BOT)))  # not only an answer: the change waits
    run(b.handle(None, msg("no idea what to cook", **TO_BOT)))
    assert b.state.data.household.lunch_time == "14:00"
    run(b.handle(None, msg("yes!", **TO_BOT)))
    assert b.state.data.household.lunch_time == "14:30"  # saved and reloaded
    assert ChatBot.texts(b, -100)[-1].startswith("Done")
    assert git(repo, "log", "-1", "--format=%s").startswith("chat: Lunch time")


def test_no_cancels_and_without_write_access_the_bot_shows_the_change(
    data_dir, tmp_path, monkeypatch
):
    data = plugin_data(data_dir)
    monkeypatch.delenv("SMP_GIT_WRITE_TOKEN", raising=False)
    b = ChatBot(data, DB(tmp_path / "b.db"))
    reply(data_dir, text=block({"set": {"household.shopping_day": "friday"}}))
    run(b.handle(None, msg("@chefbot compramos los viernes")))
    run(b.handle(None, msg("no", **TO_BOT)))
    assert ChatBot.texts(b)[-1] == b.t("change_cancelled")
    run(b.handle(None, msg("@chefbot compramos los viernes")))
    run(b.handle(None, msg("ok", **TO_BOT)))
    assert 'household.yaml: shopping_day = "friday"' in ChatBot.texts(b)[-1]
    assert b.state.data.household.shopping_day != "friday"


def test_in_the_group_only_an_answer_to_the_bot_settles_a_change(data_dir, tmp_path, monkeypatch):
    data = plugin_data(data_dir)
    monkeypatch.delenv("SMP_GIT_WRITE_TOKEN", raising=False)
    db = DB(tmp_path / "b.db")
    b = ChatBot(data, db)
    reply(data_dir, text=block({"set": {"household.shopping_day": "friday"}}))
    run(b.handle(None, msg("@chefbot compramos los viernes")))
    sent, asked = len(ChatBot.texts(b)), len(turns(data_dir))
    run(b.handle(None, msg("ok", reply_to_message={"from": {"id": 8}})))  # to Ana, not the bot
    run(b.handle(None, msg("no")))  # said to the room
    assert db.get_kv("bot.waiting.-100") is not None
    assert len(ChatBot.texts(b)) == sent and len(turns(data_dir)) == asked  # no answer at all
    run(b.handle(None, msg("@ChefBot yes")))  # a mention is addressed to the bot
    assert db.get_kv("bot.waiting.-100") is None
    assert 'household.yaml: shopping_day = "friday"' in ChatBot.texts(b)[-1]
    assert len(turns(data_dir)) == asked  # the answer, not a new question for the AI


def test_in_a_private_chat_a_plain_yes_settles_a_change(data_dir, tmp_path, monkeypatch):
    data = plugin_data(data_dir)
    monkeypatch.delenv("SMP_GIT_WRITE_TOKEN", raising=False)
    db = DB(tmp_path / "b.db")
    b = ChatBot(data, db)
    reply(data_dir, text=block({"set": {"household.shopping_day": "friday"}}))
    run(b.handle(None, msg("compramos los viernes", chat=7, user=7)))
    run(b.handle(None, msg("yes", chat=7, user=7)))
    assert db.get_kv("bot.pending.7") is None
    assert 'household.yaml: shopping_day = "friday"' in ChatBot.texts(b, 7)[-1]


def test_a_yes_after_the_wait_ran_out_changes_nothing(data_dir, tmp_path, monkeypatch):
    data = plugin_data(data_dir)
    monkeypatch.delenv("SMP_GIT_WRITE_TOKEN", raising=False)
    db = DB(tmp_path / "b.db")
    b = ChatBot(data, db)
    reply(data_dir, text=block({"set": {"household.shopping_day": "friday"}}))
    run(b.handle(None, msg("@chefbot compramos los viernes")))
    (pending,) = db.get_kv("bot.waiting.-100")
    db.set_kv("bot.waiting.-100", [{**pending, "ts": time.time() - PENDING_TTL - 1}])
    sent = len(ChatBot.texts(b))
    reply(data_dir, text="¿Qué cambio?")
    run(b.handle(None, msg("yes", **TO_BOT)))
    # neither applied nor shown for a person to make: a new message for the AI
    assert ChatBot.texts(b)[sent:] == ["¿Qué cambio?"]
    assert db.get_kv("bot.waiting.-100") is None


def test_the_bot_lets_a_person_add_only_to_their_own_avoided_foods(data_dir, tmp_path, monkeypatch):
    data = plugin_data(data_dir)
    monkeypatch.delenv("SMP_GIT_WRITE_TOKEN", raising=False)
    db = DB(tmp_path / "b.db")
    b = ChatBot(data, db)
    reply(data_dir, text=block({"set": {"household.people.luis.avoid_ingredients": ["salmon"]}}))
    run(b.handle(None, msg("@chefbot Luis ya no evita espinacas", user=8)))  # Ana, for Luis
    assert "only the person themselves" in ChatBot.texts(b)[-1]
    assert db.get_kv("bot.waiting.-100") is None
    reply(
        data_dir,
        text=block({"set": {"household.people.luis.avoid_ingredients": ["espinaca", "salmon"]}}),
    )
    run(b.handle(None, msg("@chefbot no quiero salmón")))  # Luis, for himself
    assert "Foods Luis avoids: espinaca → espinaca, salmon" in ChatBot.texts(b)[-1]
    assert db.get_kv("bot.waiting.-100")[0]["asker"] == "luis"
    run(b.handle(None, msg("yes", **TO_BOT)))  # checked again on the yes, as Luis's own change
    assert 'avoid_ingredients = ["espinaca", "salmon"]' in ChatBot.texts(b)[-1]


def test_forbidden_changes_and_strangers_are_refused(data_dir, tmp_path):
    data = plugin_data(data_dir, group_open=True)
    db = DB(tmp_path / "b.db")
    b = ChatBot(data, db)
    reply(data_dir, text=block({"set": {"household.people.luis.kcal.start": 1200}}))
    run(b.handle(None, msg("@chefbot bájame las calorías")))
    assert "cannot be changed from the chat" in ChatBot.texts(b)[-1]
    assert db.get_kv("bot.waiting.-100") is None
    reply(data_dir, text=block({"set": {"household.lunch_time": "15:00"}}))
    run(b.handle(None, msg("@chefbot comemos a las 15", user=555)))  # in the group, not household
    assert ChatBot.texts(b)[-1] == b.t("change_members_only")


def test_a_reminder_change_reads_in_the_household_language(data_dir):
    data = bot_data(data_dir)
    p = changes.parse(
        {"set": {"settings.reminders.water": {"at": ["12:00", "17:00"], "days": ["monday"]}}},
        data,
        "luis",
    )
    line = changes.describe(p, data, Translator("es"))[0]
    assert "a las 12:00, 17:00" in line
    assert "días lun" in line
    assert " at " not in line and "monday" not in line


def test_instructions_change_only_as_a_whole_text(data):
    p = changes.parse(
        {"set": {"settings.llm.instructions": "Answer in short lines."}}, data, "luis"
    )
    assert p.edits[0].label == "instructions"
    for bad in (["a list"], "x" * (changes.INSTRUCTIONS_MAX + 1)):
        with pytest.raises(changes.ChangeError, match="whole new text"):
            changes.parse({"set": {"settings.llm.instructions": bad}}, data, "luis")
    with pytest.raises(changes.ChangeError, match="cannot be changed"):
        changes.parse({"set": {"settings.llm.model": "other"}}, data, "luis")


# ---------------------------------------------------------------- a plugin's proposals


def offer(**extra):
    return {
        "id": "j1:1",
        "summary": "Open a pull request: add /soup.",
        "confirm_by": "asker",
        **extra,
    }


def test_a_proposal_waits_for_the_askers_yes_and_the_plugin_carries_it_out(data_dir, tmp_path):
    data = plugin_data(data_dir)
    db = DB(tmp_path / "b.db")
    b = ChatBot(data, db)
    reply(data_dir, text="I can draft that.", proposals=[offer(), {"id": "", "summary": "x"}])
    run(b.handle(None, msg("@chefbot quiero un comando /sopa")))
    assert turns(data_dir)[-1]["proposals"] is True  # the plugin learns the app can confirm
    said = ChatBot.texts(b, -100)
    assert said[0] == "I can draft that."
    assert "Open a pull request: add /soup." in said[1] and "Tap Yes or No" in said[1]
    assert len(said) == 2  # the malformed proposal is dropped
    reply(data_dir, text="ok")
    run(b.handle(None, msg("yes", user=8, **TO_BOT)))  # someone else's yes
    assert acts(data_dir) == []
    run(b.handle(None, msg("yes", **TO_BOT)))
    assert [a["id"] for a in acts(data_dir)] == ["j1:1"]
    assert ChatBot.texts(b, -100)[-1] == "done j1:1"
    assert db.get_kv("bot.waiting.-100") is None
    run(b.handle(None, msg("yes", **TO_BOT)))  # once only
    assert len(acts(data_dir)) == 1


def test_an_admin_proposal_takes_only_an_admins_yes(data_dir, tmp_path):
    data = plugin_data(data_dir, admins=["ana"])
    db = DB(tmp_path / "b.db")
    b = ChatBot(data, db)
    reply(data_dir, text="Drafted.", proposals=[offer(confirm_by="admins")])
    run(b.handle(None, msg("@chefbot quiero un comando /sopa")))  # Luis asks
    assert ChatBot.texts(b, -100)[-1].startswith("Ana, shall I go ahead?")
    reply(data_dir, text="ok")
    run(b.handle(None, msg("yes", **TO_BOT)))  # the asker is not an admin
    assert acts(data_dir) == []
    run(b.handle(None, msg("no", user=8, **TO_BOT)))
    assert ChatBot.texts(b, -100)[-1] == b.t("change_cancelled") and acts(data_dir) == []
    reply(data_dir, text="Drafted.", proposals=[offer(confirm_by="admins")])
    run(b.handle(None, msg("@chefbot quiero un comando /sopa")))
    run(b.handle(None, msg("yes", user=8, **TO_BOT)))
    assert len(acts(data_dir)) == 1


def test_an_admin_proposal_with_no_admins_is_refused(data_dir, tmp_path):
    data = plugin_data(data_dir)
    db = DB(tmp_path / "b.db")
    b = ChatBot(data, db)
    reply(data_dir, text="Drafted.", proposals=[offer(confirm_by="admins")])
    run(b.handle(None, msg("@chefbot quiero un comando /sopa")))
    assert ChatBot.texts(b, -100)[-1] == b.t("proposal_no_admins")
    assert db.get_kv("bot.waiting.-100") is None


def test_a_step_that_changes_nothing_runs_at_once_and_offers_what_follows(data_dir, tmp_path):
    data = plugin_data(data_dir, admins=["ana"])
    db = DB(tmp_path / "b.db")
    b = ChatBot(data, db)
    follow = offer(id="j2:1", summary="PR: add /soup (2 files).", confirm_by="admins")
    draft = offer(
        confirm_by="none",
        summary="Drafting it.",
        then={"text": "Draft ready.", "proposals": [follow]},
    )
    reply(data_dir, text="On it.", proposals=[draft])
    settle(b, msg("@chefbot quiero un comando /sopa"))
    assert [a["id"] for a in acts(data_dir)] == ["j1:1"]  # no yes needed to draft
    said = ChatBot.texts(b, -100)
    assert said[:3] == ["On it.", "Drafting it.", "Draft ready."]
    assert "PR: add /soup (2 files)." in said[3] and said[3].startswith("Ana")
    assert db.get_kv("bot.waiting.-100")[0]["asker"] == "luis"
    run(b.handle(None, msg("yes", user=8, **TO_BOT)))
    assert [a["id"] for a in acts(data_dir)] == ["j1:1", "j2:1"]


def test_what_follows_a_step_runs_even_when_its_message_cannot_be_sent(data_dir, tmp_path):
    data = plugin_data(data_dir)
    db = DB(tmp_path / "b.db")
    b = ChatBot(data, db)
    sent = b.call

    async def call(client, method, _tries=0, **params):
        if params.get("text") == "Server merged.":
            raise TelegramError(method, "ConnectError")
        return await sent(client, method, _tries, **params)

    b.call = call
    follow = offer(id="j2:1", summary="Now the phone.", confirm_by="none")
    draft = offer(
        confirm_by="none",
        summary="Drafting it.",
        then={"text": "Server merged.", "proposals": [follow]},
    )
    reply(data_dir, text="On it.", proposals=[draft])
    settle(b, msg("@chefbot cambia el código"))
    assert [a["id"] for a in acts(data_dir)] == ["j1:1", "j2:1"]
    assert "Now the phone." in ChatBot.texts(b, -100)


def test_a_send_that_never_reached_telegram_is_tried_again(data_dir, tmp_path, monkeypatch):
    b = Bot(FakeState(bot_data(data_dir), DB(tmp_path / "b.db"), MONDAY))
    b.token = "x"
    tries = []

    def answer(request):
        tries.append(request.url.path)
        if len(tries) < 3:
            raise httpx.ConnectError("unreachable")
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})

    async def no_wait(s):
        return None

    monkeypatch.setattr(asyncio, "sleep", no_wait)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as c:
            got = await b.call(c, "sendMessage", chat_id=1, text="hola")
            tries.clear()
            with pytest.raises(TelegramError):
                await b.call(c, "getUpdates")  # its own loop asks again; never twice here
            return got

    assert run(go()) == {"message_id": 7}
    assert len(tries) == 1


def test_strangers_start_nothing_and_a_failed_step_is_said(data_dir, tmp_path):
    data = plugin_data(data_dir, group_open=True)
    db = DB(tmp_path / "b.db")
    b = ChatBot(data, db)
    reply(data_dir, text="Sure.", proposals=[offer(confirm_by="none")])
    settle(b, msg("@chefbot cambia el código", user=555))
    assert ChatBot.texts(b, -100)[-1] == b.t("proposal_members_only") and acts(data_dir) == []
    reply(data_dir, text="Sure.", proposals=[offer(then="fail")])
    run(b.handle(None, msg("@chefbot cambia el código")))
    run(b.handle(None, msg("yes", **TO_BOT)))
    assert "the remote side refused" in ChatBot.texts(b, -100)[-1]


def test_a_plugin_without_act_says_so(data_dir, tmp_path):
    plugin_data(data_dir)
    plugin = data_dir / "plugins" / "ai.py"
    plugin.write_text(plugin.read_text().split("def act(")[0])
    b = ChatBot(load(data_dir), DB(tmp_path / "b.db"))
    reply(data_dir, text="Sure.", proposals=[offer()])
    run(b.handle(None, msg("@chefbot cambia el código")))
    run(b.handle(None, msg("yes", **TO_BOT)))
    assert ChatBot.texts(b, -100)[-1] == b.t("proposal_unsupported")


def test_a_failed_typing_indicator_never_stops_an_answer(data_dir, tmp_path):
    data = plugin_data(data_dir)
    b = ChatBot(data, DB(tmp_path / "b.db"))
    sent = b.call

    async def call(client, method, _tries=0, **params):
        if method == "sendChatAction":
            raise TelegramError(method, "Too Many Requests", 429)
        return await sent(client, method, _tries, **params)

    b.call = call
    run(b.handle(None, msg("hola", chat=7, user=7)))
    assert ChatBot.texts(b, 7) == ["ok"]


def test_admins_must_be_people_the_bot_knows(data_dir):
    with pytest.raises(Exception, match="bot.admins: nadie is not in bot.people"):
        bot_data(data_dir, admins=["nadie"])


def test_each_offer_waits_on_its_own_and_a_yes_runs_only_the_one_it_answers(
    data_dir, tmp_path, monkeypatch
):
    data = plugin_data(data_dir, admins=["luis"])
    monkeypatch.delenv("SMP_GIT_WRITE_TOKEN", raising=False)
    db = DB(tmp_path / "b.db")
    b = ChatBot(data, db)
    reply(data_dir, text=block({"set": {"household.shopping_day": "friday"}}))
    run(b.handle(None, msg("@chefbot compramos los viernes")))
    change_prompt = b.calls[-1]
    reply(data_dir, text="Drafted.", proposals=[offer(confirm_by="admins")])
    run(b.handle(None, msg("@chefbot quiero un comando /sopa")))
    assert len(db.get_kv("bot.waiting.-100")) == 2  # the change still waits
    reply(data_dir, text="ok")
    run(b.handle(None, msg("yes", **TO_BOT)))  # which one?
    assert ChatBot.texts(b, -100)[-1] == b.t("pending_which") and acts(data_dir) == []
    pr_prompt = next(
        1000 + n + 1
        for n, (m, p) in enumerate(b.calls)
        if m == "sendMessage" and "Open a pull request" in p["text"]
    )
    run(b.handle(None, msg("yes", reply_to_message={"from": {"id": 99}, "message_id": pr_prompt})))
    assert [a["id"] for a in acts(data_dir)] == ["j1:1"]  # only the pull request
    (left,) = db.get_kv("bot.waiting.-100")
    assert left["change"] and change_prompt[1]["text"].startswith("Luis")
    run(b.handle(None, msg("yes", **TO_BOT)))  # one left: a plain yes is enough
    assert 'household.yaml: shopping_day = "friday"' in ChatBot.texts(b, -100)[-1]
    assert db.get_kv("bot.waiting.-100") is None


def test_a_newer_change_from_the_same_person_replaces_the_older(data_dir, tmp_path, monkeypatch):
    data = plugin_data(data_dir)
    monkeypatch.delenv("SMP_GIT_WRITE_TOKEN", raising=False)
    db = DB(tmp_path / "b.db")
    b = ChatBot(data, db)
    reply(data_dir, text=block({"set": {"household.shopping_day": "friday"}}))
    run(b.handle(None, msg("@chefbot compramos los viernes")))
    reply(data_dir, text=block({"set": {"household.shopping_day": "saturday"}}))
    run(b.handle(None, msg("@chefbot no, mejor los sábados")))
    (item,) = db.get_kv("bot.waiting.-100")
    assert item["change"]["set"]["household.shopping_day"] == "saturday"


def test_the_households_own_instructions_never_outrank_the_rules(data_dir):
    from supermealplanner import context

    bot_data(data_dir)
    raw = yaml.safe_load((data_dir / "settings.yaml").read_text())
    raw["llm"] = {"instructions": "Be brief."}
    write(data_dir, "settings.yaml", raw)
    rules = context.rules(load(data_dir))
    assert rules.index("Never suggest compensating") < rules.index("Be brief.")
    assert rules.rstrip().endswith(context.LAST_WORD)


def test_the_ai_knows_each_web_page_and_its_address(data_dir):
    from supermealplanner import context

    bot_data(data_dir)
    raw = yaml.safe_load((data_dir / "settings.yaml").read_text())
    raw["base_url"] = "https://meals.example/"
    write(data_dir, "settings.yaml", raw)
    rules = context.rules(load(data_dir))
    assert "at https://meals.example." in rules
    for page in ("/tasks?d=", "/list?d=", "/batch?d=", "/recipe/<id>", "«I have it»", "👍/👎"):
        assert page in rules, page


def prompt_id(b, text: str) -> int:
    """The message id ChatBot gave the last message sent that holds `text`."""
    return max(
        1000 + n + 1 for n, (m, p) in enumerate(b.calls) if m == "sendMessage" and text in p["text"]
    )


def test_a_yes_to_someone_elses_offer_approves_nothing_of_ones_own(data_dir, tmp_path, monkeypatch):
    data = plugin_data(data_dir, admins=["ana"])
    monkeypatch.delenv("SMP_GIT_WRITE_TOKEN", raising=False)
    db = DB(tmp_path / "b.db")
    b = ChatBot(data, db)
    reply(data_dir, text="Drafted.", proposals=[offer(confirm_by="admins")])
    run(b.handle(None, msg("@chefbot quiero un comando /sopa")))  # waits for Ana
    reply(data_dir, text=block({"set": {"household.shopping_day": "friday"}}))
    run(b.handle(None, msg("@chefbot compramos los viernes")))  # waits for Luis
    luis_prompt = prompt_id(b, "shall I change this?")
    reply(data_dir, text="ok")
    to_luis = {"from": {"id": 99}, "message_id": luis_prompt}
    run(b.handle(None, msg("yes", user=8, reply_to_message=to_luis)))
    assert acts(data_dir) == [], "Ana's yes to Luis's prompt opened her pull request"
    assert len(db.get_kv("bot.waiting.-100")) == 2  # both still wait
    ana_prompt = prompt_id(b, "Open a pull request")
    to_ana = {"from": {"id": 99}, "message_id": ana_prompt}
    run(b.handle(None, msg("yes", user=8, reply_to_message=to_ana)))
    assert [a["id"] for a in acts(data_dir)] == ["j1:1"]


def test_a_yes_to_any_piece_of_a_long_offer_counts(data_dir, tmp_path):
    data = plugin_data(data_dir)
    db = DB(tmp_path / "b.db")
    b = ChatBot(data, db)
    long = "Open a pull request:\n" + "\n".join(f"+ line {n}" for n in range(600))
    reply(data_dir, text="Drafted.", proposals=[offer(summary=long)])
    run(b.handle(None, msg("@chefbot quiero un comando /sopa")))
    (item,) = db.get_kv("bot.waiting.-100")
    assert len(item["msgs"]) > 1
    reply(data_dir, text="ok")
    first = {"from": {"id": 99}, "message_id": item["msgs"][0]}
    run(b.handle(None, msg("yes", reply_to_message=first)))
    assert [a["id"] for a in acts(data_dir)] == ["j1:1"]


def test_an_admin_offer_from_a_private_chat_waits_in_the_group(data_dir, tmp_path):
    data = plugin_data(data_dir, admins=["ana"], private_chats=True)
    db = DB(tmp_path / "b.db")
    b = ChatBot(data, db)
    reply(data_dir, text="Drafted.", proposals=[offer(confirm_by="admins")])
    run(b.handle(None, msg("quiero un comando /sopa", chat=7, user=7)))  # Luis, privately
    private, group = ChatBot.texts(b, 7), ChatBot.texts(b, -100)
    assert private[-1] == b.t("proposal_in_group", name="Ana")
    assert group and group[-1].startswith("Ana, shall I go ahead?")
    assert db.get_kv("bot.waiting.7") is None
    (item,) = db.get_kv("bot.waiting.-100")
    assert item["by"] == [8] and item["asker"] == "luis"
    reply(data_dir, text="ok")
    to_prompt = {"from": {"id": 99}, "message_id": item["msg"]}
    run(b.handle(None, msg("yes", user=8, reply_to_message=to_prompt)))
    assert [a["id"] for a in acts(data_dir)] == ["j1:1"]
    assert ChatBot.texts(b, -100)[-1] == "done j1:1"


def test_an_admins_own_private_offer_stays_in_the_private_chat(data_dir, tmp_path):
    data = plugin_data(data_dir, admins=["ana"], private_chats=True)
    db = DB(tmp_path / "b.db")
    b = ChatBot(data, db)
    reply(data_dir, text="Drafted.", proposals=[offer(confirm_by="admins")])
    run(b.handle(None, msg("quiero un comando /sopa", chat=8, user=8)))
    assert ChatBot.texts(b, 8)[-1].startswith("Ana, shall I go ahead?")
    assert ChatBot.texts(b, -100) == []
    reply(data_dir, text="ok")
    run(b.handle(None, msg("yes", chat=8, user=8)))
    assert [a["id"] for a in acts(data_dir)] == ["j1:1"]


def test_an_admin_offer_from_a_private_chat_with_no_group_is_refused(data_dir, tmp_path):
    data = plugin_data(data_dir, admins=["ana"], private_chats=True, chat_id=None)
    db = DB(tmp_path / "b.db")
    b = ChatBot(data, db)
    reply(data_dir, text="Drafted.", proposals=[offer(confirm_by="admins")])
    run(b.handle(None, msg("quiero un comando /sopa", chat=7, user=7)))
    assert ChatBot.texts(b, 7)[-1] == b.t("proposal_admins_no_group")
    assert db.get_kv("bot.waiting.7") is None


def test_a_change_waiting_from_0_3_is_still_answered_and_the_list_never_takes_its_key(
    data_dir, tmp_path, monkeypatch
):
    data = plugin_data(data_dir)
    monkeypatch.delenv("SMP_GIT_WRITE_TOKEN", raising=False)
    db = DB(tmp_path / "b.db")
    b = ChatBot(data, db)
    old = {
        "change": {"set": {"household.shopping_day": "friday"}},
        "by": 7,
        "asker": "luis",
        "name": "Luis",
        "ts": time.time(),
    }
    db.set_kv("bot.pending.-100", old)  # what 0.3.1 left behind
    reply(data_dir, text="Drafted.", proposals=[offer()])
    run(b.handle(None, msg("@chefbot quiero un comando /sopa")))
    assert db.get_kv("bot.pending.-100") is None  # 0.3.1 reads it as a dict: never a list there
    assert [("change" in i) for i in db.get_kv("bot.waiting.-100")] == [True, False]
    reply(data_dir, text="ok")
    run(b.handle(None, msg("no", **TO_BOT)))  # two wait for Luis: which one?
    assert ChatBot.texts(b, -100)[-1] == b.t("pending_which")


def test_new_instructions_stay_a_readable_block_and_show_only_what_changes(data_dir):
    bot_data(data_dir)
    raw = yaml.safe_load((data_dir / "settings.yaml").read_text())
    raw["llm"] = {"instructions": "Responde en español.\nSé breve.\nUsa listas."}
    write(data_dir, "settings.yaml", raw)
    data = load(data_dir)
    new = "Responde en español.\nSé muy breve.\nUsa listas.\n  con sangría\n"
    p = changes.parse({"set": {"settings.llm.instructions": new}}, data, "luis")
    lines = changes.describe(p, data, Translator("es"))
    assert lines == [
        "• Instrucciones del asistente:",
        "  − Sé breve.",
        "  + Sé muy breve.",
        "  +   con sangría",
    ]
    text = changes.render(data_dir, p)["settings.yaml"]
    assert "instructions: |" in text and "\\n" not in text
    assert yaml.safe_load(text)["llm"]["instructions"] == new
    odd = "Una línea con espacio al final \n\tY un tabulador."  # a block cannot carry it: quotes
    p = changes.parse({"set": {"settings.llm.instructions": odd}}, data, "luis")
    assert (
        yaml.safe_load(changes.render(data_dir, p)["settings.yaml"])["llm"]["instructions"] == odd
    )


def test_a_yes_replying_to_a_0_3_confirmation_settles_it(data_dir, tmp_path, monkeypatch):
    data = plugin_data(data_dir)
    monkeypatch.delenv("SMP_GIT_WRITE_TOKEN", raising=False)
    db = DB(tmp_path / "b.db")
    b = ChatBot(data, db)
    old = {
        "change": {"set": {"household.shopping_day": "friday"}},
        "by": 7,
        "asker": "luis",
        "name": "Luis",
        "ts": time.time(),
    }
    db.set_kv("bot.pending.-100", old)  # what 0.3.1 left behind: no id of its message
    reply(data_dir, text="¿Qué?")
    # a real reply always carries the id of the message it answers
    run(b.handle(None, msg("yes", reply_to_message={"from": {"id": 99}, "message_id": 555})))
    assert turns(data_dir) == []  # the yes never reached the AI
    assert ChatBot.texts(b, -100)[-1].startswith(b.t("change_manual"))
    assert db.get_kv("bot.waiting.-100") is None


def test_plain_text_is_cut_without_adding_anything(data_dir, tmp_path):
    from supermealplanner.bot import _chunks

    # an admin's diff of a Markdown file: its context lines hold fences that are not ours
    text = "Cambios:\n ```yaml\n" + "\n".join(f"+línea {i} " + "x" * 60 for i in range(120))
    parts = list(_chunks(text))
    assert len(parts) > 1 and "\n".join(parts) == text
    b = ChatBot(bot_data(data_dir), DB(tmp_path / "b.db"))
    run(b.send(None, -100, text))
    assert "\n".join(ChatBot.texts(b, -100)) == text


def test_a_proposal_cut_off_by_a_restart_is_said_on_the_next_start(data_dir, tmp_path):
    data = plugin_data(data_dir)
    db = DB(tmp_path / "b.db")
    b = ChatBot(data, db)
    reply(data_dir, text="On it.", proposals=[offer(confirm_by="none")])
    settle(b, msg("@chefbot quiero un comando /sopa"))
    assert db.get_kv("bot.running") is None  # a step that ended leaves nothing behind

    seen = []

    async def stopped(client, chat_id, reply_to, pending):
        seen.append(db.get_kv("bot.running"))
        raise asyncio.CancelledError  # the container stops mid-draft

    b._act = stopped
    with pytest.raises(asyncio.CancelledError):
        run(b.act(None, -100, 1, {"proposal": offer(confirm_by="none"), "asker": "luis"}))
    assert len(seen[0]) == 1 and seen[0][0]["chat"] == -100
    after = ChatBot(data, db)  # the bot starts again
    run(after.interrupted(None))
    assert ChatBot.texts(after, -100) == [after.t("proposal_interrupted")]
    assert db.get_kv("bot.running") is None
    run(after.interrupted(None))  # said once
    assert len(ChatBot.texts(after, -100)) == 1


def test_a_new_version_is_told_once_to_the_admins_and_again_only_when_it_changes(
    data_dir, tmp_path, monkeypatch
):
    from supermealplanner import bot as botmod

    data = plugin_data(data_dir, admins=["luis"])
    db = DB(tmp_path / "b.db")
    monkeypatch.setattr(botmod, "__version__", "1.0.0")
    b = ChatBot(data, db)
    run(b.announce_version(None))
    assert ChatBot.texts(b) == ["Now running version 1.0.0."]
    assert [p["chat_id"] for m, p in b.calls if m == "sendMessage"] == [7]  # luis, privately
    again = ChatBot(data, db)  # a restart on the same version
    run(again.announce_version(None))
    assert ChatBot.texts(again) == []
    monkeypatch.setattr(botmod, "__version__", "1.1.0")
    up = ChatBot(data, db)
    run(up.announce_version(None))
    assert ChatBot.texts(up) == ["Now running version 1.1.0 (was 1.0.0)."]
    assert db.get_kv(botmod.ANNOUNCED) == "1.1.0"


def test_the_new_version_goes_to_the_group_when_no_admin_can_be_written_to(
    data_dir, tmp_path, monkeypatch
):
    from supermealplanner import bot as botmod

    data = plugin_data(data_dir, admins=["luis"])
    db = DB(tmp_path / "b.db")
    monkeypatch.setattr(botmod, "__version__", "2.0.0")
    b = ChatBot(data, db)
    sent = b.call

    async def no_private(client, method, _tries=0, **params):
        if method == "sendMessage" and params.get("chat_id") == 7:
            raise TelegramError(method, "Forbidden: bot can't initiate conversation", 403)
        return await sent(client, method, _tries, **params)

    b.call = no_private
    run(b.announce_version(None))
    assert ChatBot.texts(b, -100) == ["Now running version 2.0.0."]
    assert db.get_kv(botmod.ANNOUNCED) == "2.0.0"


def test_a_version_nobody_was_told_about_is_tried_again_at_the_next_start(
    data_dir, tmp_path, monkeypatch
):
    from supermealplanner import bot as botmod

    data = plugin_data(data_dir, admins=["luis"])
    db = DB(tmp_path / "b.db")
    monkeypatch.setattr(botmod, "__version__", "3.0.0")
    b = ChatBot(data, db)

    async def down(client, method, _tries=0, **params):
        raise TelegramError(method, "Bad Gateway", 502)

    b.call = down
    run(b.announce_version(None))
    assert db.get_kv(botmod.ANNOUNCED) is None
    later = ChatBot(data, db)
    run(later.announce_version(None))
    assert ChatBot.texts(later) == ["Now running version 3.0.0."]
