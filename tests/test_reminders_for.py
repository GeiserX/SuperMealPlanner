"""bot.reminders_for: every reminder set from the chat is for the person who cooks, whoever asked
for it or tapped it. A kitchen step reminded to the person who only asked about it pings the one
who will not do it."""

from supermealplanner import bot as botmod

from .test_chains import at
from .test_merge_reminders import soon
from .test_reminders import ahead, setup
from .test_settings_stores_bot import run
from .test_turns import sends


def test_a_reminder_asked_for_by_someone_else_is_for_the_cook(data_dir, tmp_path):
    b, _ = setup(data_dir, tmp_path, reminders_for="ana")
    when, _ = ahead()
    named = {"when": when, "text": "Cook the beans", "person": "luis"}
    (a, c) = b._offered([named, {"when": when, "text": "Soak the chickpeas"}], "luis")
    assert a["person"] == "ana" and c["person"] == "ana"


def test_without_the_setting_the_asker_keeps_it(data_dir, tmp_path):
    b, _ = setup(data_dir, tmp_path)
    when, _ = ahead()
    (r,) = b._offered([{"when": when, "text": "Soak the chickpeas"}], "luis")
    assert r["person"] == "luis"


def test_a_stored_reminder_for_someone_else_goes_out_to_the_cook(data_dir, tmp_path, monkeypatch):
    b, db = setup(data_dir, tmp_path, reminders_for="ana")
    day = soon()
    db.add_reminder(-100, "luis", at(day, "17:30"), "Cook the beans", 7)
    monkeypatch.setattr(botmod.time, "time", lambda: at(day, "17:30") + 30)
    run(b.tick_once(None))
    (sent,) = sends(b, -100)
    assert sent["text"].startswith("⏰ Ana")
    assert "Luis" not in sent["text"]
