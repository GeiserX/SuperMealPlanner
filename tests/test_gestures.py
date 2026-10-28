"""How an answer looks and acts in Telegram: the renderer, the gestures block (including hostile
model output) and the bot sending them after the text."""

import re

import yaml

from supermealplanner import gestures, render
from supermealplanner.bot import TelegramError
from supermealplanner.db import DB
from supermealplanner.model import load

from .test_conversations import ChatBot, plugin_data, reply
from .test_settings_stores_bot import msg, run, write

TAGS = r"b|i|u|s|tg-spoiler|code|pre|blockquote|a"


def calls(b, method):
    return [p for m, p in b.calls if m == method]


def test_render_folds_quotes_and_code():
    out = render.to_html("**Hoy**\n> nota\n```fold\n- 200 g ||arroz||\n```\n```diff\n+ pan\n```")
    assert out == (
        "<b>Hoy</b>\n<blockquote>nota</blockquote>\n<blockquote expandable>• 200 g "
        '<tg-spoiler>arroz</tg-spoiler></blockquote>\n<pre><code class="language-diff">+ pan'
        "</code></pre>"
    )


def test_render_hostile_markup_never_opens_a_tag():
    for md in (
        "<blockquote expandable>x",
        "||<b>x</b>||",
        "```fold\n</blockquote><b>\n```",
        '```py" onclick="x\ny\n```',
        '[x](https://a.org/"><script>)',
    ):
        out = render.to_html(md)
        assert sorted(re.findall(rf"<({TAGS})[ >]", out)) == sorted(
            re.findall(rf"</({TAGS})>", out)
        ), (md, out)
        assert "<script" not in out and "onclick" not in out


def test_gestures_hostile_output_is_dropped():
    text, g = gestures.extract(
        "x\n```telegram\nreact: <b>\nsticker: abc\nphoto: http://a.org/a.jpg\n"
        "photo: https://u:p@evil.org/a.jpg\nsend_message: -100 hola\n```\n"
        "```document run.sh\necho\n```\n"
    )
    assert text == "x"
    assert g == {"react": None, "sticker": None, "photos": [], "documents": []}


def test_a_reaction_alone_reacts_and_says_nothing(data_dir, tmp_path):
    b = ChatBot(plugin_data(data_dir), DB(tmp_path / "b.db"))
    reply(data_dir, text="```telegram\nreact: 👍\n```")
    run(b.handle(None, msg("¡gracias!", chat=7, user=7, message_id=42)))
    [r] = calls(b, "setMessageReaction")
    assert (r["chat_id"], r["message_id"], r["reaction"]) == (
        7,
        42,
        [{"type": "emoji", "emoji": "👍"}],
    )
    assert ChatBot.texts(b, 7) == []


def test_a_failed_reaction_with_no_text_says_the_emoji(data_dir, tmp_path):
    b = ChatBot(plugin_data(data_dir), DB(tmp_path / "b.db"))
    real = b.call

    async def call(client, method, _tries=0, **params):
        if method == "setMessageReaction":
            b.calls.append((method + "!", params))
            raise TelegramError(method, "Bad Request: REACTION_INVALID", 400)
        return await real(client, method, _tries, **params)

    b.call = call
    reply(data_dir, text="```telegram\nreact: 🎉\n```")
    run(b.handle(None, msg("listo", chat=7, user=7)))
    assert ChatBot.texts(b, 7) == ["🎉"]


def test_the_shopping_list_goes_as_a_file_after_the_text(data_dir, tmp_path):
    b = ChatBot(plugin_data(data_dir), DB(tmp_path / "b.db"))
    reply(
        data_dir,
        text="Aquí la tienes 🛒\n````document lista.md | Semana 44\n- pan\n````\n",
    )
    run(b.handle(None, msg("la lista", chat=7, user=7)))
    assert ChatBot.texts(b, 7) == ["Aquí la tienes 🛒"]
    [doc] = calls(b, "sendDocument")
    assert doc["_files"] == {"document": ("lista.md", b"- pan\n", "text/markdown")}
    assert (doc["chat_id"], doc["caption"]) == (7, "Semana 44")


def test_a_refused_file_never_loses_the_text(data_dir, tmp_path):
    b = ChatBot(plugin_data(data_dir), DB(tmp_path / "b.db"))
    real = b.call

    async def call(client, method, _tries=0, **params):
        if method == "sendDocument":
            raise TelegramError(method, "Bad Request: file is empty", 400)
        return await real(client, method, _tries, **params)

    b.call = call
    reply(data_dir, text="Resumen.\n```document receta.txt\npasos\n```")
    run(b.handle(None, msg("la receta", chat=7, user=7)))
    assert ChatBot.texts(b, 7) == ["Resumen."]


def test_stickers_photos_and_the_voice_come_from_the_settings(data_dir, tmp_path):
    data = plugin_data(data_dir)
    b = ChatBot(data, DB(tmp_path / "b.db"))
    reply(data_dir, text="Hecho.\n```telegram\nsticker: 🎉\nphoto: https://a.org/p.jpg\n```")
    run(b.handle(None, msg("hola", chat=7, user=7)))
    assert calls(b, "sendSticker") == [] and calls(b, "sendPhoto") == []
    assert "sticker:" not in b.rules() and "photo:" not in b.rules()

    raw = yaml.safe_load((data_dir / "settings.yaml").read_text())
    raw["bot"].update(style="Cálido y alegre 🍅", sticker_set="cocina", photos=True)
    write(data_dir, "settings.yaml", raw)
    b = ChatBot(load(data_dir), DB(tmp_path / "c.db"))
    real = b.call

    async def call(client, method, _tries=0, **params):
        if method == "getStickerSet":
            b.calls.append((method, params))
            return {"stickers": [{"emoji": "🎉", "file_id": "STK"}]}
        return await real(client, method, _tries, **params)

    b.call = call
    run(b.handle(None, msg("hola", chat=7, user=7)))
    assert calls(b, "sendSticker")[0]["sticker"] == "STK"
    assert calls(b, "sendPhoto")[0]["photo"] == "https://a.org/p.jpg"
    rules = b.rules()
    assert "sticker: 🎉" in rules and rules.rstrip().endswith("Cálido y alegre 🍅")
