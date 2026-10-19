"""The web's shared shopping ticks, «I have it», the week's tasks and 👍/👎 on dishes."""

import datetime as dt
from collections import Counter

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
                "reminders": [
                    {"id": "compra", "kind": "shopping", "at": ["20:00"], "days": ["monday"]},
                    {"id": "batch", "kind": "batch", "at": ["21:30"]},
                    {"id": "agua", "kind": "message", "text": "Agua", "at": ["12:00"]},
                ]
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


def test_today_shows_the_batch_sheet_itself(data_dir, data, monkeypatch):
    c, plan = _app(data_dir, data, monkeypatch)
    s = next(s for s in plan.sessions if not s.morning)
    with c:
        page = c.get(f"/?d={s.date}").text
    assert "Batch today" in page and 'class="containers"' in page


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
        for path in (f"/?d={MONDAY}", f"/batch?d={MONDAY}"):
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
