"""Photos and files: they always reach the AI, like a voice note, and go to a plugin as
turn["attachments"]; what the bot cannot open, cannot fit or cannot download is said under it."""

import asyncio
import base64
import json

import yaml

from supermealplanner.bot import FILE_MAX, TelegramError
from supermealplanner.db import DB
from supermealplanner.model import load

from .test_conversations import ChatBot, plugin_data
from .test_settings_stores_bot import msg, run, write
from .test_turns import drain, feed, restarted, until

PLUGIN = """
import base64, json, pathlib, time
HERE = pathlib.Path(__file__).parent

def converse(turn, settings):
    cfg = json.loads((HERE / "reply.json").read_text()) if (HERE / "reply.json").exists() else {}
    log = {"new": [m["content"] for m in turn["new"]], "incoming": [],
           "attachments": [{"name": a["name"], "mime": a["mime"], "from": a["from"],
                            "data": base64.b64encode(a["data"]).decode()}
                           for a in turn["attachments"]]}
    (HERE / "running").write_text("1")
    end = time.time() + cfg.get("listen", 0)
    covers = []
    while time.time() < end:
        got = turn["incoming"]()
        log["incoming"] += [m["content"] for m in got]
        covers += [m["id"] for m in got]
        time.sleep(0.02)
    with (HERE / "turns.jsonl").open("a") as f:
        f.write(json.dumps(log) + "\\n")
    (HERE / "running").unlink(missing_ok=True)
    return {"text": "ok", "conversation": "c1", "covers": covers}
"""

JPEG = b"\xff\xd8\xff fake jpeg"
PDF = b"%PDF-1.7 fake"
PHOTO = [
    {"file_id": "small", "file_size": 900, "width": 90},
    {"file_id": "big", "file_size": 90_000, "width": 1280},
]


def setup(data_dir, tmp_path, files=None):
    data = plugin_data(data_dir)
    (data_dir / "plugins" / "ai.py").write_text(PLUGIN)
    b = ChatBot(data, DB(tmp_path / "b.db"))
    b.fetched = []
    files = {"big": JPEG, "doc": PDF} if files is None else files

    async def download(client, file_id):
        b.fetched.append(file_id)
        got = files.get(file_id)
        if isinstance(got, Exception):
            raise got
        if got is None:
            raise TelegramError("getFile download", "HTTP 404")
        return got, f"photos/{file_id}.jpg"

    b.download = download
    return b


def turns(data_dir):
    path = data_dir / "plugins" / "turns.jsonl"
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


def sent(b, chat=None):
    return ChatBot.texts(b, chat)


def settle(b, message):
    async def go():
        await b.handle(None, message)
        await drain(b)

    run(go())


def test_a_photo_in_the_group_reaches_the_ai_with_its_caption_and_its_largest_size(
    data_dir, tmp_path
):
    b = setup(data_dir, tmp_path)
    settle(b, msg(photo=PHOTO, caption="¿Qué cocino con esto?", message_id=5))
    assert b.fetched == ["big"]  # nobody mentioned the bot: a photo calls it anyway
    [turn] = turns(data_dir)
    assert turn["new"] == ["luis: [photo: inbox/5-photo.jpg] ¿Qué cocino con esto?"]
    [a] = turn["attachments"]
    assert a == {
        "name": "inbox/5-photo.jpg",
        "mime": "image/jpeg",
        "from": "luis",
        "data": base64.b64encode(JPEG).decode(),
    }
    assert sent(b, -100) == ["ok"]


def test_an_image_or_pdf_sent_as_a_file_goes_under_a_plain_name(data_dir, tmp_path):
    b = setup(data_dir, tmp_path)
    doc = {"file_id": "doc", "file_name": "Receta de la abuela.PDF", "mime_type": "application/pdf"}
    settle(b, msg(document=doc, chat=7, message_id=6))
    [turn] = turns(data_dir)
    assert turn["new"] == ["luis: [file: inbox/6-Receta-de-la-abuela.PDF]"]
    assert [(a["name"], a["mime"]) for a in turn["attachments"]] == [
        ("inbox/6-Receta-de-la-abuela.PDF", "application/pdf")
    ]
    nameless = {"file_id": "doc", "file_name": "ñ/..", "mime_type": "image/png"}
    settle(b, msg(document=nameless, chat=7, message_id=7))
    assert turns(data_dir)[-1]["attachments"][0]["name"] == "inbox/7-n.png"


def test_a_file_the_bot_cannot_open_or_that_is_too_big_is_said_and_never_fetched(
    data_dir, tmp_path
):
    b = setup(data_dir, tmp_path)
    es = load(data_dir).locale == "es"
    zipped = {"file_id": "z", "file_name": "menu.zip", "mime_type": "application/zip"}
    settle(b, msg(document=zipped, chat=7))
    huge = [{"file_id": "big", "file_size": FILE_MAX + 1}]
    settle(b, msg(photo=huge, chat=7))
    assert b.fetched == [] and turns(data_dir) == []
    kind, big = sent(b, 7)
    assert ("Puedo abrir fotos" if es else "I can open photos") in kind
    assert "20 MB" in big


def test_a_file_that_fails_to_download_is_said_and_its_message_still_goes(data_dir, tmp_path):
    b = setup(data_dir, tmp_path, files={})
    settle(b, msg(photo=PHOTO, caption="mira", chat=7, message_id=5))
    [turn] = turns(data_dir)
    assert turn["attachments"] == [] and turn["new"] == ["luis: [photo: inbox/5-photo.jpg] mira"]
    failed, answer = sent(b, 7)
    assert answer == "ok" and failed != "ok"
    b2 = setup(data_dir, tmp_path, files={"big": TelegramError("getFile", "file is too big", 413)})
    settle(b2, msg(photo=PHOTO, chat=7, message_id=8))
    assert "20 MB" in sent(b2, 7)[0]


def test_a_file_past_the_turns_budget_is_left_out_with_a_note(data_dir, tmp_path):
    from supermealplanner import bot

    big = b"x" * (bot.TURN_FILES_MAX // 2 + 1)
    b = setup(data_dir, tmp_path, files={"a": big, "b": big})
    b.debounce = 0.2  # both photos go in one turn

    async def burst():
        first = asyncio.create_task(
            b.handle(None, msg(photo=[{"file_id": "a"}], chat=7, message_id=1))
        )
        await until(lambda: b.queues.get(7))
        await b.handle(None, msg(photo=[{"file_id": "b"}], chat=7, message_id=2))
        await first

    run(burst())
    [turn] = turns(data_dir)
    assert [a["name"] for a in turn["attachments"]] == ["inbox/1-photo.jpg"]
    assert len(turn["new"]) == 1 and "inbox/2-photo.jpg" in turn["new"][0]
    note = [p for m, p in b.calls if m == "sendMessage" and "25 MB" in p["text"]]
    assert [p["reply_parameters"]["message_id"] for p in note] == [2]


def test_strangers_and_other_chats_get_nothing(data_dir, tmp_path):
    b = setup(data_dir, tmp_path)
    settle(b, msg(photo=PHOTO, user=999))
    settle(b, msg(photo=PHOTO, chat=-999))
    assert b.fetched == [] and turns(data_dir) == [] and b.calls == []


def test_a_provider_that_cannot_look_at_files_says_so(data_dir, tmp_path):
    plugin_data(data_dir)
    raw = yaml.safe_load((data_dir / "settings.yaml").read_text())
    raw["llm"] = {"provider": "openai", "model": "m"}
    write(data_dir, "settings.yaml", raw)
    b = ChatBot(load(data_dir), DB(tmp_path / "b.db"))
    settle(b, msg(photo=PHOTO, chat=7))
    [note] = sent(b, 7)
    assert "IA" in note or "AI" in note
    assert b.state.db.get_kv("bot.queued") is None


def test_a_photo_sent_while_an_answer_is_written_waits_for_the_next_turn(data_dir, tmp_path):
    b = setup(data_dir, tmp_path)
    (data_dir / "plugins" / "reply.json").write_text(json.dumps({"listen": 0.6}))

    async def go():
        first = asyncio.create_task(b.handle(None, msg("¿qué cenamos?", chat=7, message_id=1)))
        await until(lambda: (data_dir / "plugins" / "running").exists())
        await b.handle(None, msg(photo=PHOTO, chat=7, message_id=2))
        await b.handle(None, msg("y esto", chat=7, message_id=3))
        await first
        await drain(b)

    run(go())
    one, two = turns(data_dir)
    assert one["incoming"] == ["luis: y esto"] and one["attachments"] == []
    assert two["new"] == ["luis: [photo: inbox/2-photo.jpg]"]
    assert [a["name"] for a in two["attachments"]] == ["inbox/2-photo.jpg"]


def test_a_photo_queued_before_a_restart_still_carries_its_file(data_dir, tmp_path):
    old = setup(data_dir, tmp_path)
    feed(old, {"update_id": 41, "message": msg(photo=PHOTO, chat=7, message_id=5)})

    async def dies_before_the_turn():
        old.ask = lambda *a, **k: asyncio.Event().wait()  # the turn never gets going
        poll = asyncio.create_task(old.poll(None))
        await until(lambda: old.state.db.get_kv("bot.queued"))
        poll.cancel()

    run(dies_before_the_turn())
    new, start = restarted(old)
    new.download = old.download
    run(start())
    [turn] = turns(data_dir)
    assert [a["name"] for a in turn["attachments"]] == ["inbox/5-photo.jpg"]
    assert ChatBot.texts(new, 7) == ["ok"]
