"""What the household reads from the bot about cooking: every reminder about a dish ends with its
recipe's link, the morning menu says what a dish of the day still needs bought after the shopping
day, and a plugin's report about a change is rendered and folded, never shown as raw Markdown or
cut."""

import asyncio
import datetime as dt
import time
from zoneinfo import ZoneInfo

from supermealplanner import llm, pantry
from supermealplanner.db import DB
from supermealplanner.planner import plan_for
from supermealplanner.portions import ingredients_of, servings_on, shopping_list

from .conftest import MONDAY
from .test_menu import planned
from .test_settings_stores_bot import FakeBot, bot_data
from .test_soak import soaking


def D(n):
    return MONDAY + dt.timedelta(days=n)


def at(d, hour=7):
    """Early on day `d`, before its cooking, as the household's clock reads it."""
    return dt.datetime(d.year, d.month, d.day, hour, tzinfo=ZoneInfo("Europe/Madrid"))


def bot(data_dir, tmp_path, today=MONDAY):
    bot_data(data_dir)
    data = planned(data_dir)
    return FakeBot(data, DB(tmp_path / "b.db"), today), data


def test_a_reminder_set_from_the_chat_about_a_dish_ends_with_its_recipe(data_dir, tmp_path):
    b, data = bot(data_dir, tmp_path)
    day = plan_for(data, D(2)).day(D(2))
    db = b.state.db
    db.add_reminder(-100, None, time.time() - 5, "Haz la cena", recipe=day.dinner, date=str(D(2)))
    db.add_reminder(-100, None, time.time() - 5, "Riega las plantas")
    asyncio.run(b.tick_once(None))
    texts = [t for _, t in b.sent]
    assert f"⏰ Haz la cena\nhttps://meals.example/recipe/{day.dinner}?d={D(2)}" in texts
    assert "⏰ Riega las plantas" in texts  # about no dish: no link


def test_the_batch_and_soak_reminders_link_each_dishs_recipe(data_dir, tmp_path):
    data, soak, _ = soaking(data_dir)
    b = FakeBot(data, DB(tmp_path / "b.db"), MONDAY)
    batch = next(r for r in data.settings.reminders if r.id == "batch")
    assert not soak.morning
    said = b.reminder_text(batch, soak.date)  # the session's own afternoon
    for x in soak.dishes:
        assert f"https://meals.example/recipe/{x.recipe}?d={x.eat[0]}" in said
    eve = b.reminder_text(batch, soak.date - dt.timedelta(days=1))
    soaked = next(
        x
        for x in soak.dishes
        if any(i.id == "garbanzo" for i in data.recipes[x.recipe].ingredients)
    )
    assert f"https://meals.example/recipe/{soaked.recipe}?d={soaked.eat[0]}" in eve


def test_the_morning_menu_says_what_a_dish_still_needs_bought_after_the_shop(data_dir, tmp_path):
    # 29-10: a side salad needed a green pepper, the list still asked for it unticked after
    # Monday's shop, and nothing told the cook until she was making it.
    b, data = bot(data_dir, tmp_path)
    db = b.state.db
    plan = plan_for(data, MONDAY)
    day = D(3)
    asked = {
        x.ingredient.id
        for s in shopping_list(data, db, plan, MONDAY, today=day).stores
        for x in s.lines
    }
    missing = pantry.unbought(data, db, plan, day)
    assert missing and all(ing.id in asked for ing, _ in missing)
    ing, rec = missing[0]
    text = b.menu_text(plan, day)
    assert "🛒" in text and f"{ing.name} ({rec.name})" in text
    assert f"https://meals.example/list?d={day}" in text
    # enough at home for today's cooking (but not the week's): not missing either
    used_today = ingredients_of(data, servings_on(data, db, plan, {day}))[ing.id]
    pantry.correct(data, db, {ing.id: used_today + 1}, at(day))
    still = shopping_list(data, db, plan, MONDAY, today=day)
    assert ing.id in {x.ingredient.id for s in still.stores for x in s.lines}  # the week still asks
    assert ing.id not in {i.id for i, _ in pantry.unbought(data, db, plan, day)}
    # bought: ticked on the list, it is no longer missing
    for iid, _ in [(i.id, r) for i, r in missing]:
        db.tick(MONDAY, iid, True)
    assert pantry.unbought(data, db, plan, day) == []
    assert "🛒" not in b.menu_text(plan, day)
    # on the shopping day itself nothing is missing yet
    assert pantry.unbought(data, DB(tmp_path / "c.db"), plan, MONDAY) == []


def test_a_plugins_report_is_rendered_and_folded_never_raw_or_cut(data_dir, tmp_path, monkeypatch):
    b, _ = bot(data_dir, tmp_path)
    seen = []

    async def send(client, chat_id, text, reply_to=None, record=True, **kw):
        seen.append((text, kw.get("markdown")))

    b.send = send
    report = "Hecho **ya**.\n\n" + ("Una frase larga sobre lo que cambia para vosotros. " * 30)

    async def act(data, proposal):
        return llm.Reply(report)

    monkeypatch.setattr(llm, "act", act)
    pending = {"proposal": {"kind": "code_request"}, "asker": "luis", "user": 7}
    asyncio.run(b._act(None, -100, 1, pending))
    ((text, markdown),) = seen
    assert markdown is True and "```fold" in text  # Telegram HTML, the rest one tap away
    assert text.replace("```fold\n", "").replace("\n```", "").split() == report.split()
