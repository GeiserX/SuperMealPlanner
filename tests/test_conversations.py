"""The AI as one ongoing conversation per chat, Telegram formatting, and settings changes from the chat."""

import asyncio
import json
import logging
import re
import subprocess
import time

import pytest
import yaml

from supermealplanner import changes
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
        f.write(json.dumps(turn) + "\\n")
    cfg = json.loads((HERE / "reply.json").read_text()) if (HERE / "reply.json").exists() else {}
    if cfg.get("fail"):
        raise RuntimeError("provider down")
    return {"text": cfg.get("text", "ok"),
            "conversation": cfg.get("conversation", turn["conversation"] or "c1"),
            "kept": cfg.get("kept", True)}

def forget(conversation, settings):
    (HERE / "forgot.txt").write_text(conversation)
"""


class ChatBot:
    """A real Bot whose Telegram calls are recorded instead of sent."""

    def __new__(cls, data, db):
        b = Bot(FakeState(data, db, MONDAY))
        b.token, b.username, b.bot_id, b.calls = "x", "chefbot", 99, []

        async def call(client, method, _tries=0, **params):
            b.calls.append((method, params))
            if params.get("parse_mode") == "HTML" and "<bad>" in params.get("text", ""):
                raise TelegramError(method, "Bad Request: can't parse entities", 400)
            return {}

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
    assert "household.people.luis" not in ana and "espinaca" not in ana
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


def test_long_answers_are_cut_at_line_breaks():
    from supermealplanner.bot import _chunks

    text = "\n".join(f"**línea {i}** " + "x" * 80 for i in range(100))
    parts = list(_chunks(text))
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


def test_the_pills_reminder_never_changes_from_the_chat(data_dir):
    data = bot_data(data_dir)  # reminder "pills" is kind pills_lunch
    for value in (
        None,
        {"enabled": False},
        {"at": ["23:00"]},
    ):
        with pytest.raises(changes.ChangeError, match="pills reminder"):
            changes.parse({"set": {"settings.reminders.pills": value}}, data, "luis")
    new = {"kind": "pills_lunch", "at": ["16:00"], "enabled": False}
    with pytest.raises(changes.ChangeError, match="pills reminder"):
        changes.parse({"set": {"settings.reminders.otra": new}}, data, "luis")
    with pytest.raises(changes.ChangeError, match="pills reminder"):  # nor turn one into it
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
    assert "Lunch time: 14:00 → 14:30" in said[1] and "yes" in said[1]
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
    assert db.get_kv("bot.pending.-100") is not None
    assert len(ChatBot.texts(b)) == sent and len(turns(data_dir)) == asked  # no answer at all
    run(b.handle(None, msg("@ChefBot yes")))  # a mention is addressed to the bot
    assert db.get_kv("bot.pending.-100") is None
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
    pending = db.get_kv("bot.pending.-100")
    db.set_kv("bot.pending.-100", {**pending, "ts": time.time() - PENDING_TTL - 1})
    sent = len(ChatBot.texts(b))
    reply(data_dir, text="¿Qué cambio?")
    run(b.handle(None, msg("yes", **TO_BOT)))
    # neither applied nor shown for a person to make: a new message for the AI
    assert ChatBot.texts(b)[sent:] == ["¿Qué cambio?"]
    assert db.get_kv("bot.pending.-100") is None


def test_the_bot_lets_a_person_add_only_to_their_own_avoided_foods(data_dir, tmp_path, monkeypatch):
    data = plugin_data(data_dir)
    monkeypatch.delenv("SMP_GIT_WRITE_TOKEN", raising=False)
    db = DB(tmp_path / "b.db")
    b = ChatBot(data, db)
    reply(data_dir, text=block({"set": {"household.people.luis.avoid_ingredients": ["salmon"]}}))
    run(b.handle(None, msg("@chefbot Luis ya no evita espinacas", user=8)))  # Ana, for Luis
    assert "only the person themselves" in ChatBot.texts(b)[-1]
    assert db.get_kv("bot.pending.-100") is None
    reply(
        data_dir,
        text=block({"set": {"household.people.luis.avoid_ingredients": ["espinaca", "salmon"]}}),
    )
    run(b.handle(None, msg("@chefbot no quiero salmón")))  # Luis, for himself
    assert "Foods Luis avoids: espinaca → espinaca, salmon" in ChatBot.texts(b)[-1]
    assert db.get_kv("bot.pending.-100")["asker"] == "luis"
    run(b.handle(None, msg("yes", **TO_BOT)))  # checked again on the yes, as Luis's own change
    assert 'avoid_ingredients = ["espinaca", "salmon"]' in ChatBot.texts(b)[-1]


def test_forbidden_changes_and_strangers_are_refused(data_dir, tmp_path):
    data = plugin_data(data_dir, group_open=True)
    db = DB(tmp_path / "b.db")
    b = ChatBot(data, db)
    reply(data_dir, text=block({"set": {"household.people.luis.kcal.start": 1200}}))
    run(b.handle(None, msg("@chefbot bájame las calorías")))
    assert "cannot be changed from the chat" in ChatBot.texts(b)[-1]
    assert db.get_kv("bot.pending.-100") is None
    reply(data_dir, text=block({"set": {"household.lunch_time": "15:00"}}))
    run(b.handle(None, msg("@chefbot comemos a las 15", user=555)))  # in the group, not household
    assert ChatBot.texts(b)[-1] == b.t("change_members_only")
