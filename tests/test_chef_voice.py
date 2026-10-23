"""Talking to the chef from a phone: `POST /api/v1/chef`, then `GET /api/v1/chef/{id}?wait=`.

What each test pins, and why:

  * **Same bot, same conversation.** What is said goes into the person's private chat with the bot
    and through the very path a typed message takes, so a change, a pending yes and the turn's
    merging behave as they do in Telegram. The chat shows what was said, then the answer.
  * **Read out, not rendered.** The app gets the answer without Markdown, links, emojis or buttons:
    choices become "You can say: A or B", a question for a yes says "say yes or no".
  * **Only the asker reads it, and no request holds long.** Another person's token gets a 404, and
    one request waits CHEF_WAIT_MAX at most, so a client that loops never times out.
"""

import asyncio
import datetime as dt
import re
import time

import pytest
from fastapi.testclient import TestClient

from supermealplanner import api
from supermealplanner import devices as dv
from supermealplanner.bot import Spoken, speakable
from supermealplanner.db import DB
from supermealplanner.i18n import Translator
from supermealplanner.model import load
from supermealplanner.planner import paused_days

from .conftest import MONDAY
from .test_conversations import ChatBot, block, plugin_data, reply, turns
from .test_settings_stores_bot import msg, run

LUIS, ANA = 7, 8  # their Telegram ids in the test household (bot.people)


def D(n: int) -> dt.date:
    return MONDAY + dt.timedelta(days=n)


@pytest.fixture
def chef(data_dir, tmp_path, monkeypatch):
    """The real app with a real bot whose Telegram calls are recorded instead of sent."""
    monkeypatch.setenv("SMP_SCHEDULER", "0")
    monkeypatch.setenv("SMP_BOT", "0")
    monkeypatch.delenv("SMP_GIT_WRITE_TOKEN", raising=False)  # menu changes need no token
    data = plugin_data(data_dir)
    from supermealplanner.web import create_app

    app = create_app(data_dir)
    with TestClient(app) as client:
        b = ChatBot(data, DB(tmp_path / "b.db"))
        b.started, b.client = True, object()  # up, with a Telegram client (a stand-in)
        app.state.smp.bot = b
        yield client, b


def token(data_dir, client, person="luis") -> dict:
    code, _ = dv.make_code(DB(load(data_dir).state / "supermealplanner.db"), person)
    r = client.post("/api/v1/pair", json={"code": code, "device": "Phone"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def say(client, headers, text) -> str:
    r = client.post("/api/v1/chef", json={"text": text}, headers=headers)
    assert r.status_code == 202, r.text
    assert r.json()["state"] in ("thinking", "done")
    return r.json()["id"]


def answer(client, headers, sid, wait=10) -> dict:
    r = client.get(f"/api/v1/chef/{sid}", params={"wait": wait}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def sent(b, chat=LUIS) -> list[dict]:
    return [p for m, p in b.calls if m == "sendMessage" and p["chat_id"] == chat]


def test_a_spoken_question_is_answered_in_the_chat_and_read_back(chef, data_dir):
    client, b = chef
    me = token(data_dir, client)
    reply(data_dir, text="**Tonight**: [hake](https://meals.example/recipe/x) 🐟\n- with a salad")
    sid = say(client, me, "what's for dinner?")
    got = answer(client, me, sid)
    assert got == {"id": sid, "state": "done", "text": "Tonight: hake. With a salad."}
    echo, said = sent(b)
    assert echo["text"] == "🎙️ what's for dinner?"
    assert "<b>Tonight</b>" in said["text"]  # the chat keeps its own rendering
    assert said["reply_parameters"]["message_id"] == 1001  # answers what the chat shows was said
    (turn,) = turns(data_dir)
    assert turn["new"][-1]["content"] == "luis: [voice] what's for dinner?"
    assert turn["chat"]["id"] == LUIS  # the person's private chat, the same conversation
    assert "[voice]" in turn["rules"]  # the AI is told how to answer one


def test_a_change_asked_for_by_voice_is_made_at_once(chef, data_dir):
    client, b = chef
    me = token(data_dir, client)
    pause = {"pause": {"from": str(D(1)), "to": str(D(6))}}
    reply(data_dir, text=block({"asked": True, "plan": [pause]}))
    got = answer(client, me, say(client, me, "this week we eat what we have"))
    assert got["state"] == "done"
    assert got["text"].startswith("Te lo cambio. Done:") and "Menu paused" in got["text"]
    assert paused_days(b.state.data) == {D(i) for i in range(1, 7)}
    assert b.pending(LUIS) == []


def test_a_spoken_yes_settles_what_waits_for_that_person(chef, data_dir):
    client, b = chef
    me = token(data_dir, client)
    pause = {"pause": {"from": str(D(1)), "to": str(D(6))}}
    reply(data_dir, text=block({"plan": [pause]}))
    asked = answer(client, me, say(client, me, "should we eat what we have?"))
    # a question at the end, so a voice client that listens again after one keeps the mic open
    assert asked["text"].endswith("Yes or no?") and "Tap" not in asked["text"]
    assert "Tap Yes or No" in sent(b)[-1]["text"]  # the chat still has its buttons
    assert len(b.pending(LUIS)) == 1 and paused_days(b.state.data) == set()
    got = answer(client, me, say(client, me, "Yes."))
    assert got == {"id": got["id"], "state": "done", "text": b.t("menu_applied")}
    assert paused_days(b.state.data) == {D(i) for i in range(1, 7)}
    assert len(turns(data_dir)) == 1  # the yes never went to the AI, as a typed one does not
    deadline = time.monotonic() + 5  # the group's copy of the yes, by name, with what it did
    while len(sent(b, -100)) < 2:
        assert time.monotonic() < deadline
        time.sleep(0.02)
    assert sent(b, -100)[-1]["text"] == f"🎙️ Luis (by voice): Yes.\n\n{b.t('menu_applied')}"
    assert sent(b)[-2]["text"] == "🎙️ Yes."


def test_choices_are_read_as_a_short_list_and_the_chat_keeps_its_buttons(chef, data_dir):
    client, b = chef
    me = token(data_dir, client)
    reply(data_dir, text="Which one on Monday?\n\n```options\n- Lentils\n- Chickpeas\n```")
    got = answer(client, me, say(client, me, "what do I cook on Monday?"))
    assert got["text"] == "Which one on Monday? You can say: Lentils or Chickpeas."
    labels = [x["text"] for row in sent(b)[-1]["reply_markup"]["inline_keyboard"] for x in row]
    assert labels == ["Lentils", "Chickpeas"]


def test_only_the_person_who_said_it_reads_the_answer(chef, data_dir):
    client, b = chef
    luis, ana = token(data_dir, client), token(data_dir, client, "ana")
    sid = say(client, luis, "hello")
    assert answer(client, luis, sid)["state"] == "done"
    r = client.get(f"/api/v1/chef/{sid}", headers=ana)
    assert r.status_code == 404
    assert client.get("/api/v1/chef/nope", headers=luis).status_code == 404
    assert client.get(f"/api/v1/chef/{sid}").status_code == 401
    assert client.post("/api/v1/chef", json={"text": "hi"}).status_code == 401
    assert client.post("/api/v1/chef", json={"text": " "}, headers=luis).status_code == 400
    deadline = time.monotonic() + 5  # the group's copy is sent after the answer is ready
    while not sent(b, -100):
        assert time.monotonic() < deadline
        time.sleep(0.02)
    sends = [p["chat_id"] for m, p in b.calls if m == "sendMessage"]
    assert sends == [LUIS, LUIS, -100]  # the echo, the answer, and the group's copy


def test_one_request_waits_no_longer_than_the_cap(chef, data_dir, monkeypatch):
    client, b = chef
    me = token(data_dir, client)
    b.spoken["slow"] = Spoken("slow", "luis", LUIS, "think hard")  # never answered
    monkeypatch.setattr(api, "CHEF_WAIT_MAX", 0.3)
    start = time.monotonic()
    got = answer(client, me, "slow", wait=5)
    assert got == {"id": "slow", "state": "thinking"}
    assert 0.25 <= time.monotonic() - start < 3


def test_a_turn_from_long_ago_is_gone(chef, data_dir):
    client, b = chef
    me = token(data_dir, client)
    sid = say(client, me, "hello")
    assert answer(client, me, sid)["state"] == "done"
    b.spoken[sid].ts -= 3601
    assert client.get(f"/api/v1/chef/{sid}", headers=me).status_code == 404


def test_without_a_bot_or_a_telegram_user_the_chef_says_why(chef, data_dir):
    client, b = chef
    nobody = token(data_dir, client, None)
    assert client.post("/api/v1/chef", json={"text": "hi"}, headers=nobody).status_code == 409
    me = token(data_dir, client)
    b.client = None  # between two runs of the bot's loop: its client is closed
    assert client.post("/api/v1/chef", json={"text": "hi"}, headers=me).status_code == 503
    b.client, b.started = object(), False
    assert client.post("/api/v1/chef", json={"text": "hi"}, headers=me).status_code == 503


def test_spoken_words_never_run_a_command(chef, data_dir):
    client, _ = chef
    me = token(data_dir, client)
    got = answer(client, me, say(client, me, "/pair"))
    assert got["state"] == "done" and not any(ch.isdigit() for ch in got["text"])
    (turn,) = turns(data_dir)  # it went to the AI as words
    assert turn["new"][-1]["content"] == "luis: [voice] pair"
    assert client.post("/api/v1/chef", json={"text": " / "}, headers=me).status_code == 400


def test_a_typed_command_answered_meanwhile_is_not_heard(data_dir, tmp_path):
    data = plugin_data(data_dir)
    b = ChatBot(data, DB(tmp_path / "b.db"))
    reply(data_dir, text="Fish tonight.")
    slow = b.ask

    async def ask(*args):
        await asyncio.sleep(0.2)  # the provider thinks; a typed /pair comes in meanwhile
        return await slow(*args)

    b.ask = ask

    async def go():
        spoken = b.say("luis", "what's for dinner?")
        await asyncio.sleep(0.1)
        await b.handle(None, msg("/pair", chat=LUIS, user=LUIS, message_id=5))
        await asyncio.wait_for(spoken.ready.wait(), 5)
        return spoken

    spoken = run(go())
    assert any(re.search(r"\d{3} \d{3}", x) for x in ChatBot.texts(b, LUIS))  # the code, typed
    assert (spoken.state, spoken.answer) == ("done", "Fish tonight.")


def test_a_spoken_message_merged_into_a_burst_gets_the_answer_that_covered_it(data_dir, tmp_path):
    data = plugin_data(data_dir)
    b = ChatBot(data, DB(tmp_path / "b.db"))
    b.debounce = 0.3
    reply(data_dir, text="Fish tonight.")

    async def go():
        typed = asyncio.create_task(b.handle(None, msg("hi", chat=LUIS, user=LUIS)))
        await asyncio.sleep(0.1)
        spoken = b.say("luis", "and for dinner?")
        await asyncio.wait_for(spoken.ready.wait(), 5)
        await typed
        return spoken

    spoken = run(go())
    (turn,) = turns(data_dir)
    assert turn["new"][-1]["content"] == "luis: hi\nluis: [voice] and for dinner?"
    assert (spoken.state, spoken.answer) == ("done", "Fish tonight.")


def test_a_failed_answer_is_said_as_failed(data_dir, tmp_path):
    data = plugin_data(data_dir)
    b = ChatBot(data, DB(tmp_path / "b.db"))
    reply(data_dir, fail=True)

    async def go():
        spoken = b.say("luis", "hello?")
        await asyncio.wait_for(spoken.ready.wait(), 5)
        return spoken

    spoken = run(go())
    assert spoken.state == "failed" and spoken.answer == b.t("bot_error").rstrip(".") + "."


def test_speech_drops_what_only_a_screen_shows():
    t = Translator("en")
    said = speakable(
        [
            "## Plan\n1. Soak the `lentils` ✅\n2. _Cook_ them\n```yaml\nx: 1\n```",
            "Saved as change 1a2b3c4d; see https://example.com/x.",
            "• Lunch time: 14:00 → 14:30",
        ],
        t,
    )
    assert said == (
        "Plan. Soak the lentils. Cook them. Saved as change; see. Lunch time: 14:00 to 14:30."
    )


def spoken_turn(data_dir, tmp_path, **bot):
    """One spoken turn at the bot, with what it started in the background run to the end."""
    data = plugin_data(data_dir, **bot)
    b = ChatBot(data, DB(tmp_path / "b.db"))
    reply(data_dir, text="**Tonight**: hake 🐟")

    async def go():
        spoken = b.say("luis", "what's for dinner?")
        await asyncio.wait_for(spoken.ready.wait(), 5)
        while b.tasks:
            await asyncio.gather(*list(b.tasks))

    run(go())
    return b


def test_the_group_reads_a_copy_of_each_spoken_exchange(data_dir, tmp_path):
    b = spoken_turn(data_dir, tmp_path)
    (copy,) = ChatBot.texts(b, -100)
    assert copy == "🎙️ Luis (by voice): what's for dinner?\n\nTonight: hake."
    assert b.state.db.chat_rows(-100, 20, 0) == []  # in no group history, so no turn takes it
    (turn,) = turns(data_dir)
    assert turn["chat"]["id"] == LUIS  # the conversation stayed in the private chat
    reply(data_dir, text="ok")
    run(b.handle(None, msg("@chefbot hi")))  # the group's next turn does not see the copy
    later = turns(data_dir)[-1]
    assert later["chat"]["id"] == -100
    assert "by voice" not in str(later["new"]) + str(later["history"])


def test_no_copy_when_the_setting_is_off_or_there_is_no_group(data_dir, tmp_path):
    b = spoken_turn(data_dir, tmp_path, voice_to_group=False)
    assert ChatBot.texts(b, -100) == [] and ChatBot.texts(b, LUIS)
    (tmp_path / "x").mkdir()
    b = spoken_turn(data_dir, tmp_path / "x", chat_id=None)
    assert [p["chat_id"] for m, p in b.calls if m == "sendMessage"] == [LUIS, LUIS]


def test_speech_is_turned_into_text_by_the_voice_note_transcriber(chef, data_dir, monkeypatch):
    client, _b = chef
    me = token(data_dir, client)
    heard = []

    async def fake(data, audio, filename):
        heard.append((audio, filename))
        return "  what is   for dinner? "

    monkeypatch.setattr(api.llm, "transcribe", fake)
    url = "/api/v1/transcribe"
    wav = {**me, "content-type": "audio/wav"}
    assert client.post(url, content=b"RIFF", headers=wav).status_code == 503  # none configured
    client.app.state.smp.data.settings.transcription.provider = "plugin"
    r = client.post(url, content=b"RIFF", headers=wav)
    assert r.status_code == 200 and r.json() == {"text": "what is for dinner?"}
    assert heard == [(b"RIFF", "audio.wav")]
    ogg = {**me, "content-type": "audio/ogg; codecs=opus"}
    assert client.post(url, content=b"OggS", headers=ogg).json()["text"] == "what is for dinner?"
    assert heard[-1][1] == "audio.ogg"
    assert (
        client.post(url, content=b"RIFF", headers={"content-type": "audio/wav"}).status_code == 401
    )
    assert (
        client.post(url, content=b"x", headers={**me, "content-type": "text/plain"}).status_code
        == 415
    )
    assert client.post(url, content=b"", headers=wav).status_code == 400
    monkeypatch.setattr(api, "TRANSCRIBE_MAX_BYTES", 3)
    assert client.post(url, content=b"RIFF", headers=wav).status_code == 413

    async def broken(data, audio, filename):
        raise RuntimeError("down")

    monkeypatch.setattr(api, "TRANSCRIBE_MAX_BYTES", 10_000)
    monkeypatch.setattr(api.llm, "transcribe", broken)
    assert client.post(url, content=b"RIFF", headers=wav).status_code == 502
