"""The web's shared shopping ticks, «I have it», the week's tasks and 👍/👎 on dishes."""

import datetime as dt
from collections import Counter

import pytest
import yaml
from fastapi.testclient import TestClient

from supermealplanner.db import DB
from supermealplanner.planner import generate, plans_dir
from supermealplanner.portions import shopping_list

from .conftest import MONDAY


def _mains(plan) -> Counter:
    return Counter(x for d in plan.days for x in (d.lunch, d.dinner))


def test_a_disliked_dish_leaves_the_menu_and_a_liked_one_comes_up_more(data):
    base = _mains(generate(data, MONDAY, 4))
    top = base.most_common(1)[0][0]
    assert top not in _mains(generate(data, MONDAY, 4, ratings={top: -1}))
    rare = min(base, key=lambda r: (base[r], r))
    assert _mains(generate(data, MONDAY, 4, ratings={rare: 2}))[rare] > base[rare]


def test_the_same_seed_and_ratings_give_the_same_plan(data):
    a = generate(data, MONDAY, 4, ratings={"x": 1})
    b = generate(data, MONDAY, 4, ratings={"x": 1})
    assert a.to_dict() == b.to_dict()


def test_what_is_at_home_leaves_the_list_until_put_back(data, tmp_path):
    db = DB(tmp_path / "t.db")
    plan = generate(data, MONDAY, 4)

    def ids(shop):
        return {line.ingredient.id for sl in shop.stores for line in sl.lines}

    assert "aceite" in ids(shopping_list(data, db, plan, MONDAY))
    db.set_have("aceite", True)
    shop = shopping_list(data, db, plan, MONDAY)
    assert "aceite" not in ids(shop) and [i.id for i in shop.have] == ["aceite"]
    db.set_have("aceite", False)
    assert "aceite" in ids(shopping_list(data, db, plan, MONDAY))


def _app(data_dir, data, monkeypatch):
    monkeypatch.setenv("SMP_SCHEDULER", "0")
    monkeypatch.setenv("SMP_BOT", "0")
    plan = generate(data, MONDAY, 4)
    plan.save(plans_dir(data) / f"{MONDAY}.yaml")
    from supermealplanner.web import create_app

    return TestClient(create_app(data_dir)), plan


def test_ticks_are_shared_and_have_moves_a_line(data_dir, data, monkeypatch):
    c, _ = _app(data_dir, data, monkeypatch)
    with c:
        r = c.post("/list/tick", data={"week": MONDAY.isoformat(), "item": "aceite", "on": "1"})
        assert r.status_code == 200
        page = TestClient(c.app).get(f"/list?d={MONDAY}").text  # another phone: no cookies
        assert 'data-item="aceite" checked' in page
        c.post("/list/tick", data={"week": MONDAY.isoformat(), "item": "aceite", "on": "0"})
        assert 'data-item="aceite" checked' not in c.get(f"/list?d={MONDAY}").text
        r = c.post("/list/have", data={"item": "aceite", "back": f"/list?d={MONDAY}"})
        assert r.status_code == 200 and "At home, left off the list" in r.text
        assert 'data-item="aceite"' not in r.text
        assert c.post("/list/tick", data={"week": "x", "item": "aceite"}).status_code == 400
        assert c.post("/list/have", data={"item": "nope"}).status_code == 400
        hostile = {"origin": "https://evil.example"}
        assert c.post("/list/have", data={"item": "aceite"}, headers=hostile).status_code == 403


def test_a_vote_needs_a_person_counts_once_and_the_same_tap_takes_it_back(
    data_dir, data, monkeypatch
):
    c, plan = _app(data_dir, data, monkeypatch)
    rid = plan.days[0].dinner
    with c:
        assert "Rate as:" in c.get(f"/?d={MONDAY}").text
        assert c.post("/rate", data={"recipe": rid, "vote": "1"}).status_code == 400
        c.get("/who/ana")
        c.post("/rate", data={"recipe": rid, "vote": "1"})
        c.post("/rate", data={"recipe": rid, "vote": "-1"})
        db = c.app.state.smp.db
        assert db.ratings() == {rid: -1}
        c.post("/rate", data={"recipe": rid, "vote": "-1"})
        assert db.ratings() == {}
        c.post("/rate", data={"recipe": rid, "vote": "1"})
        assert "👍 1" in c.get(f"/recipe/{rid}").text
        assert c.post("/rate", data={"recipe": "nope", "vote": "1"}).status_code == 400


def test_the_tasks_page_shows_what_the_reminders_send_and_the_chat_set(data_dir, data, monkeypatch):
    (data_dir / "settings.yaml").write_text(
        yaml.safe_dump(
            {
                "base_url": "https://meals.example",
                "reminders": [
                    {"id": "compra", "kind": "shopping", "at": ["20:00"], "days": ["monday"]},
                    {"id": "batch", "kind": "batch", "at": ["21:30"]},
                    {"id": "agua", "kind": "message", "text": "Agua", "at": ["12:00"]},
                ],
            }
        )
    )
    from supermealplanner.model import load

    data = load(data_dir)
    c, plan = _app(data_dir, data, monkeypatch)
    evening = next(s for s in plan.sessions if not s.morning and s.date < MONDAY + dt.timedelta(7))
    with c:
        db = c.app.state.smp.db
        noon = dt.datetime(2026, 10, 14, 14, 0, tzinfo=dt.UTC).timestamp()
        db.add_reminder(1, None, noon, "Haz la caballa")
        page = c.get(f"/tasks?d={MONDAY}").text
    assert "Shopping day" in page
    assert "Haz la caballa" in page
    assert page.count("21:30") >= 1
    assert data.recipes[evening.dishes[0].recipe].name in page
    assert "Agua" not in page  # a plain message is not a kitchen task
    assert '<a href="https://meals.example/list?d=' in page  # the reminder's words, as a link
    assert "](https://" not in page  # never the chat's link markup as text


def test_today_shows_only_the_meals_with_portions_and_steps_and_no_batch_sheet_or_pills(
    data_dir, data, monkeypatch
):
    from supermealplanner.planner import day_menu

    c, plan = _app(data_dir, data, monkeypatch)
    s = next(s for s in plan.sessions if not s.morning)
    day = next(d for d in plan.days if d.date == s.date)
    with c:
        page = c.get(f"/?d={s.date}").text
        batch = c.get(f"/batch?d={s.date}").text
    assert 'class="containers"' not in page and "💊" not in page
    assert 'class="containers"' in batch  # the batch tab keeps the whole sheet
    menu = day_menu(data, day)
    links = [page.index(f'class="dish" href="/recipe/{r.id}?d={s.date}"') for _, r in menu]
    assert links == sorted(links)  # in eating order, the snacks to nibble on last
    assert page.count('class="steps"') == sum(1 for _, r in menu if r.steps)
    for _, r in menu:
        part = page.split(f'class="dish" href="/recipe/{r.id}?d={s.date}"', 1)[1]
        part = part.split('class="dish"', 1)[0]
        assert "Ana" in part and "Luis" in part, r.id  # each dish with each person's portion


def test_a_day_with_a_morning_and_an_afternoon_batch_shows_each_with_its_own_dishes(
    data_dir, data, monkeypatch
):
    monkeypatch.setenv("SMP_SCHEDULER", "0")
    monkeypatch.setenv("SMP_BOT", "0")
    plan = generate(data, MONDAY, 4)
    morning = next(s for s in plan.sessions if s.morning and s.date == MONDAY)
    evening = next(s for s in plan.sessions if not s.morning)
    evening.date = MONDAY  # both on the same day
    mine = {x.recipe for x in morning.dishes}
    theirs = {x.recipe for x in evening.dishes} - mine
    assert mine and theirs
    plan.save(plans_dir(data) / f"{MONDAY}.yaml")
    from supermealplanner.web import create_app

    with TestClient(create_app(data_dir)) as c:
        for path in (f"/batch?d={MONDAY}",):
            page = c.get(path).text
            parts = {
                "morning": page.split("in the morning", 1)[1].split("<section", 1)[0],
                "afternoon": page.split("in the afternoon", 1)[1].split("<section", 1)[0],
            }
            for rid in theirs:
                assert f"/recipe/{rid}?" not in parts["morning"], (path, rid)
                assert f"/recipe/{rid}?" in parts["afternoon"], (path, rid)
            for rid in mine - theirs:
                assert f"/recipe/{rid}?" not in parts["afternoon"], (path, rid)


def test_today_and_the_week_show_every_meal_and_snack_of_each_day(data_dir, data, monkeypatch):
    from supermealplanner.planner import day_menu

    c, plan = _app(data_dir, data, monkeypatch)
    with c:
        today = c.get(f"/?d={MONDAY}").text
        week = c.get(f"/week?d={MONDAY}").text
    for _, rec in day_menu(data, plan.days[0]):
        assert f"/recipe/{rec.id}?d={MONDAY}" in today, rec.id
    for d in plan.days[:7]:
        for _, rec in day_menu(data, d):
            assert f"/recipe/{rec.id}?d={d.date}" in week, (d.date, rec.id)


def test_sections_follow_the_configured_walking_order(data, tmp_path):
    plan = generate(data, MONDAY, 4)

    def orders():
        shop = shopping_list(data, DB(tmp_path / "o.db"), plan, MONDAY)
        return [list(sl.sections) for sl in shop.stores]

    assert all(o == sorted(o) for o in orders())
    data.settings.section_order = ["pescaderia", "fruteria", "despensa"]
    walked = orders()
    assert any(len(o) > 1 for o in walked)
    for o in walked:
        listed = [s for s in data.settings.section_order if s in o]
        assert o[: len(listed)] == listed
        assert o[len(listed) :] == sorted(o[len(listed) :])


def test_the_list_page_folds_each_section(data_dir, data, monkeypatch):
    c, _ = _app(data_dir, data, monkeypatch)
    html = c.get(f"/list?d={MONDAY}").text
    assert "<details" in html and "data-fold=" in html and "smp-fold:" in html


def test_ticked_lines_go_to_the_bottom_of_their_section_in_the_order_they_were_ticked(
    data_dir, data, monkeypatch
):
    import re

    c, _ = _app(data_dir, data, monkeypatch)

    def sections(page: str) -> list[list[str]]:
        uls = re.findall(r'<ul class="check">(.*?)</ul>', page, re.DOTALL)
        return [re.findall(r'data-item="([^"]+)"', u) for u in uls]

    def tick(item: str, on: str = "1") -> None:
        c.post("/list/tick", data={"week": MONDAY.isoformat(), "item": item, "on": on})

    with c:
        usual = next(s for s in sections(c.get(f"/list?d={MONDAY}").text) if len(s) >= 3)
        a, b, *rest = usual
        tick(b)
        tick(a)
        # MUT: keep the usual order → the ticked lines stay on top of what is still to buy.
        page = TestClient(c.app).get(f"/list?d={MONDAY}").text  # another phone sees the same
        assert next(s for s in sections(page) if set(s) == set(usual)) == [*rest, b, a]
        tick(b, "0")
        page = c.get(f"/list?d={MONDAY}").text
        assert next(s for s in sections(page) if set(s) == set(usual)) == [b, *rest, a]
        assert "data-pos=" in page and "place(b)" in page


@pytest.mark.parametrize(
    ("clock", "shown"),
    [
        ("08:30", {"breakfast"}),
        ("12:00", {"lunch", "side"}),
        ("15:59", {"lunch", "side"}),
        ("16:00", {"snack"}),
        ("19:00", {"dinner"}),
        ("23:59", {"dinner"}),
    ],
)
def test_today_unfolds_only_the_meal_of_the_moment(data_dir, data, monkeypatch, clock, shown):
    import re

    from supermealplanner.web import App

    at = dt.datetime.combine(MONDAY, dt.time.fromisoformat(clock))
    monkeypatch.setattr(App, "now", lambda self: at)
    c, _ = _app(data_dir, data, monkeypatch)
    with c:
        page = c.get(f"/?d={MONDAY}").text
    folds = dict(re.findall(r'<details class="meal fold" data-meal="(\w+)"( open)?>', page))
    assert {"breakfast", "lunch", "dinner"} <= set(folds)  # every meal is there, folded or not
    assert {m for m, o in folds.items() if o} == shown & set(folds)
    assert page.count("<summary><h2>") == len(folds)  # each opens and closes from its title
