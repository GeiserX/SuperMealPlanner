"""The JSON API the phone apps use, and the pairing that lets them in.

What each block pins, and why it exists:

  * **The door.** Only `/ping` and `/pair` answer without a token. Every other route is the
    household's data on a network a phone can reach from anywhere, so a missing or revoked bearer
    must be a 401 and not a page.
  * **A code opens once.** One use, ten minutes, and a wrong one costs the guesser their budget.
  * **Nothing readable on disk.** The database keeps hashes; a dump of it cannot pair a phone.
  * **One plan, two faces.** A tick made through the API is the same tick the web page shows, so the
    rules are not written twice.
"""

import datetime as dt

import pytest
from fastapi.testclient import TestClient

from supermealplanner import devices as dv
from supermealplanner.db import DB
from supermealplanner.model import load
from supermealplanner.planner import generate, plans_dir
from supermealplanner.portions import MEAL_ORDER

from .conftest import MONDAY


def planned(data_dir):
    data = load(data_dir)
    generate(data, MONDAY, 4).save(plans_dir(data) / f"{MONDAY}.yaml")
    return data


@pytest.fixture
def client(data_dir, monkeypatch):
    monkeypatch.setenv("SMP_SCHEDULER", "0")
    monkeypatch.setenv("SMP_BOT", "0")
    planned(data_dir)
    from supermealplanner.web import create_app

    with TestClient(create_app(data_dir)) as c:
        yield c


def db_of(data_dir) -> DB:
    return DB(load(data_dir).state / "supermealplanner.db")


def pair(client, data_dir, person="ana", device="iPhone de prueba"):
    """What the bot does (issue a code) and what the app does (spend it), in one step."""
    code, _ttl = dv.make_code(db_of(data_dir), person)
    r = client.post("/api/v1/pair", json={"code": code, "device": device})
    assert r.status_code == 200, r.text
    return r.json()


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------- the door


def test_only_ping_and_pair_answer_without_a_token(client):
    # MUT: drop the `device` dependency from a route → that route answers 200 to anyone.
    r = client.get("/api/v1/ping")
    assert r.status_code == 200 and r.json()["api"] >= 1 and r.json()["app"] == "supermealplanner"
    for path in (
        "/api/v1/meta",
        "/api/v1/day",
        "/api/v1/week",
        "/api/v1/tasks",
        "/api/v1/cook",
        f"/api/v1/list?d={MONDAY}",
        "/api/v1/progress",
        "/api/v1/devices",
    ):
        assert client.get(path).status_code == 401, path
    for path, body in (
        ("/api/v1/list/tick", {"ingredient": "pollo"}),
        ("/api/v1/list/have", {"ingredient": "pollo"}),
        ("/api/v1/rate", {"recipe": "x", "vote": 1}),
        ("/api/v1/log", {"kg": 70}),
    ):
        assert client.post(path, json=body).status_code == 401, path


def test_a_made_up_or_revoked_token_is_refused(client, data_dir):
    assert client.get("/api/v1/meta", headers=auth("not-a-real-token")).status_code == 401
    got = pair(client, data_dir)
    assert client.get("/api/v1/meta", headers=auth(got["token"])).status_code == 200
    assert (
        client.delete(f"/api/v1/devices/{got['device_id']}", headers=auth(got["token"])).status_code
        == 200
    )
    # MUT: let device_by_token ignore `revoked` → the phone keeps working after it was taken away.
    assert client.get("/api/v1/meta", headers=auth(got["token"])).status_code == 401


# ---------------------------------------------------------------- pairing


def test_a_code_opens_exactly_once(client, data_dir):
    db = db_of(data_dir)
    code, ttl = dv.make_code(db, "ana")
    assert ttl == dv.CODE_TTL_S
    first = client.post("/api/v1/pair", json={"code": code, "device": "iPhone"})
    assert first.status_code == 200 and first.json()["person"] == "ana"
    # MUT: read the code without spending it in take_pair_code → the replay pairs a second phone.
    assert client.post("/api/v1/pair", json={"code": code, "device": "otro"}).status_code == 401


def test_a_code_is_typed_as_it_is_read_out(client, data_dir):
    code, _ = dv.make_code(db_of(data_dir), "ana")
    spaced = dv.spaced(code)
    assert " " in spaced
    assert client.post("/api/v1/pair", json={"code": spaced, "device": "iPhone"}).status_code == 200


def test_an_expired_code_is_dead(client, data_dir):
    db = db_of(data_dir)
    old = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=dv.CODE_TTL_S + 60)
    code = dv.new_code()
    db.add_pair_code(dv.hash_secret(code), "ana", old, dv.CODE_TTL_S)
    # MUT: drop `expires > ?` from take_pair_code → a code from last week still pairs.
    assert client.post("/api/v1/pair", json={"code": code, "device": "iPhone"}).status_code == 401


def test_a_wrong_code_is_refused_and_guessing_runs_out(client, data_dir):
    for _ in range(dv.FAILS_BEFORE_BLOCK):
        assert client.post("/api/v1/pair", json={"code": "000000"}).status_code == 401
    blocked = client.post("/api/v1/pair", json={"code": "000000"})
    # MUT: never call guard.fail() in the pair route → six digits can be walked at full speed.
    assert blocked.status_code == 429 and int(blocked.headers["Retry-After"]) > 0
    # A real code is refused too while the door is shut, so the block cannot be stepped around.
    code, _ = dv.make_code(db_of(data_dir), "ana")
    assert client.post("/api/v1/pair", json={"code": code}).status_code == 429


def test_the_guard_forgets_a_source_that_got_it_right():
    g = dv.Guard(fails=3, block_s=100, window_s=100)
    g.fail("a"), g.fail("a")
    g.ok("a")
    g.fail("a"), g.fail("a")
    assert g.blocked("a") == 0  # the two before the success do not count towards the block
    g.fail("a")
    assert g.blocked("a") > 0


def test_nothing_readable_reaches_the_database(client, data_dir):
    got = pair(client, data_dir)
    with db_of(data_dir).conn() as c:
        rows = c.execute("SELECT token_hash FROM device").fetchall()
        codes = c.execute("SELECT code_hash FROM pair_code").fetchall()
    # MUT: store the token itself in add_device → the token is in the answer AND on disk.
    assert got["token"] not in [r["token_hash"] for r in rows]
    assert [r["token_hash"] for r in rows] == [dv.hash_secret(got["token"])]
    assert all(len(r["code_hash"]) == 64 for r in codes)


def test_a_device_name_is_cleaned_up(client, data_dir):
    got = pair(client, data_dir, device="  iPhone\u0007 de\n Sergio  " + "x" * 200)
    assert "\u0007" not in got["device"] and "\n" not in got["device"]
    assert len(got["device"]) <= dv.NAME_MAX
    assert dv.clean_name("") == "Phone"


def test_devices_can_be_listed_and_taken_away_one_by_one(client, data_dir):
    mine = pair(client, data_dir, person="ana", device="iPhone de Ana")
    other = pair(client, data_dir, person="luis", device="iPhone de Luis")
    listed = client.get("/api/v1/devices", headers=auth(mine["token"])).json()["devices"]
    assert {d["name"] for d in listed} == {"iPhone de Ana", "iPhone de Luis"}
    assert [d["this"] for d in listed if d["name"] == "iPhone de Ana"] == [True]
    assert (
        client.delete(
            f"/api/v1/devices/{other['device_id']}", headers=auth(mine["token"])
        ).status_code
        == 200
    )
    assert (
        client.delete(
            f"/api/v1/devices/{other['device_id']}", headers=auth(mine["token"])
        ).status_code
        == 404
    )
    assert client.get("/api/v1/meta", headers=auth(other["token"])).status_code == 401
    assert client.get("/api/v1/meta", headers=auth(mine["token"])).status_code == 200


# ---------------------------------------------------------------- what the app reads


def test_the_app_learns_the_household_and_whose_phone_it_is(client, data_dir):
    got = pair(client, data_dir, person="luis")
    meta = client.get("/api/v1/meta", headers=auth(got["token"])).json()
    assert meta["household"] == "Prueba" and meta["locale"]
    assert {p["id"] for p in meta["people"]} == {"ana", "luis"}
    assert meta["device"]["person"] == "luis"
    assert meta["api"] >= 1


def test_a_day_comes_with_every_meal_in_order_and_a_plate_per_person(client, data_dir):
    got = pair(client, data_dir)
    day = client.get(f"/api/v1/day?d={MONDAY}", headers=auth(got["token"])).json()
    assert day["planned"] and day["day"] == str(MONDAY)
    meals = [m["meal"] for m in day["meals"]]
    assert "lunch" in meals and "dinner" in meals
    # MUT: iterate the menu instead of MEAL_ORDER in the day route → dinner can come before lunch.
    assert meals == [m for m in MEAL_ORDER if m in meals]
    plates = day["meals"][0]["dishes"][0]["plates"]
    assert {p["person"] for p in plates} == {"ana", "luis"}
    assert all(p["kcal"] > 0 and p["lines"] for p in plates)
    assert {p["person"] for p in day["people"]} == {"ana", "luis"}


def test_the_week_the_tasks_and_the_cooking_all_answer(client, data_dir):
    got = pair(client, data_dir)
    h = auth(got["token"])
    week = client.get(f"/api/v1/week?d={MONDAY}", headers=h).json()
    assert week["monday"] == str(MONDAY) and len(week["days"]) == 7
    assert any(d["meals"] for d in week["days"])
    tasks = client.get(f"/api/v1/tasks?d={MONDAY}", headers=h).json()
    assert len(tasks["days"]) == 7
    cook = client.get(f"/api/v1/cook?d={MONDAY}", headers=h).json()
    assert cook["day"] == str(MONDAY)
    if cook["dishes"]:
        first = cook["dishes"][0]
        assert first["servings"] >= 1 and first["eat_on"] and first["lines"]


def test_a_recipe_carries_its_steps_and_this_person_s_plate(client, data_dir):
    got = pair(client, data_dir, person="ana")
    h = auth(got["token"])
    rid = client.get(f"/api/v1/day?d={MONDAY}", headers=h).json()["meals"][0]["dishes"][0][
        "recipe"
    ]["id"]
    r = client.get(f"/api/v1/recipe/{rid}?d={MONDAY}", headers=h).json()
    assert r["recipe"]["id"] == rid and r["recipe"]["ingredients"]
    assert r["plate"] and r["plate"]["kcal"] > 0
    assert client.get("/api/v1/recipe/not-a-recipe", headers=h).status_code == 404


def test_a_bad_date_is_a_plain_400(client, data_dir):
    got = pair(client, data_dir)
    r = client.get("/api/v1/day?d=yesterday", headers=auth(got["token"]))
    assert r.status_code == 400 and "YYYY-MM-DD" in r.json()["detail"]


# ---------------------------------------------------------------- one plan, two faces


def test_a_tick_from_the_app_is_the_tick_the_web_page_shows(client, data_dir):
    got = pair(client, data_dir)
    h = auth(got["token"])
    shop = client.get(f"/api/v1/list?d={MONDAY}", headers=h).json()
    line = shop["stores"][0]["sections"][0]["lines"][0]
    assert line["ingredient"] not in shop["ticked"]
    ticked = client.post(
        "/api/v1/list/tick",
        json={"ingredient": line["ingredient"], "on": True, "d": str(MONDAY)},
        headers=h,
    ).json()
    assert line["ingredient"] in ticked["ticked"]
    # MUT: give the API its own tick store → the page below no longer agrees with the phone.
    assert line["ingredient"] in db_of(data_dir).ticked(MONDAY)
    page = client.get(f"/list?d={MONDAY}")
    assert page.status_code == 200 and line["ingredient"] in page.text
    again = client.get(f"/api/v1/list?d={MONDAY}", headers=h).json()
    assert line["ingredient"] in again["ticked"]


def test_i_have_it_at_home_keeps_an_ingredient_off_the_list(client, data_dir):
    got = pair(client, data_dir)
    h = auth(got["token"])
    shop = client.get(f"/api/v1/list?d={MONDAY}", headers=h).json()
    ing = shop["stores"][0]["sections"][0]["lines"][0]["ingredient"]
    assert client.post("/api/v1/list/have", json={"ingredient": ing, "on": True}, headers=h).json()[
        "have"
    ] == [ing]
    assert ing in client.get(f"/api/v1/list?d={MONDAY}", headers=h).json()["have"]
    assert db_of(data_dir).have() == {ing}


def test_an_empty_ingredient_is_refused_rather_than_ticked(client, data_dir):
    h = auth(pair(client, data_dir)["token"])
    assert client.post("/api/v1/list/tick", json={"ingredient": "  "}, headers=h).status_code == 400
    assert client.post("/api/v1/list/have", json={}, headers=h).status_code == 400


def test_a_vote_from_the_app_reaches_the_ratings_the_planner_reads(client, data_dir):
    got = pair(client, data_dir, person="luis")
    h = auth(got["token"])
    rid = client.get(f"/api/v1/day?d={MONDAY}", headers=h).json()["meals"][0]["dishes"][0][
        "recipe"
    ]["id"]
    assert (
        client.post("/api/v1/rate", json={"recipe": rid, "vote": -1}, headers=h).status_code == 200
    )
    assert db_of(data_dir).ratings("luis")[rid] == -1
    assert client.get(f"/api/v1/day?d={MONDAY}", headers=h).json()["votes"][rid] == -1
    assert (
        client.post("/api/v1/rate", json={"recipe": rid, "vote": 7}, headers=h).status_code == 400
    )
    assert (
        client.post("/api/v1/rate", json={"recipe": "nope", "vote": 1}, headers=h).status_code
        == 404
    )


def test_logging_from_the_app_lands_where_the_log_page_writes(client, data_dir):
    got = pair(client, data_dir, person="ana")
    h = auth(got["token"])
    r = client.post(
        "/api/v1/log", json={"kg": 59.4, "water": 6, "craving": 3, "note": "ok"}, headers=h
    )
    assert r.status_code == 200 and r.json()["person"] == "ana"
    db = db_of(data_dir)
    assert db.weights("ana")[-1][1] == 59.4
    row = db.daily("ana")[-1]
    assert row["water"] == 6 and row["craving"] == 3 and row["note"] == "ok"
    # Out-of-range numbers are clamped, not refused, as the Log page does.
    client.post("/api/v1/log", json={"water": 999, "craving": -5}, headers=h)
    row = db_of(data_dir).daily("ana")[-1]
    assert row["water"] == 40 and row["craving"] == 0
    assert client.post("/api/v1/log", json={"kg": 7}, headers=h).status_code == 400


def test_a_phone_may_log_for_someone_else_in_the_household_but_not_a_stranger(client, data_dir):
    h = auth(pair(client, data_dir, person="ana")["token"])
    assert (
        client.post("/api/v1/log", json={"person": "luis", "kg": 81.0}, headers=h).json()["person"]
        == "luis"
    )
    assert (
        client.post("/api/v1/log", json={"person": "nobody", "kg": 81.0}, headers=h).status_code
        == 400
    )


def test_progress_reads_the_trend_and_says_it_in_the_household_s_language(client, data_dir):
    h = auth(pair(client, data_dir, person="ana")["token"])
    for day, kg in ((MONDAY, 60.0), (MONDAY + dt.timedelta(days=7), 59.5)):
        client.post("/api/v1/log", json={"person": "ana", "day": str(day), "kg": kg}, headers=h)
    got = client.get("/api/v1/progress", headers=h).json()
    ana = next(p for p in got["people"] if p["person"] == "ana")
    assert [w["kg"] for w in ana["weights"]] == [60.0, 59.5]
    assert ana["trend"] and ana["kcal"] > 0
    assert all(isinstance(f, str) for f in ana["flags"])


def test_an_unpaired_device_is_not_told_which_people_live_here(client):
    """401 carries no household detail: an unpaired caller learns nothing about the family."""
    body = client.get("/api/v1/meta").text
    assert "Prueba" not in body and "ana" not in body and "luis" not in body


# ---------------------------------------------------------------- the bot hands out the code


def _bot(data_dir, tmp_path):
    from .test_settings_stores_bot import FakeBot, bot_data

    data = bot_data(data_dir)
    return FakeBot(data, DB(tmp_path / "bot.db"), MONDAY)


def test_the_bot_gives_a_code_in_private_and_refuses_in_the_group(data_dir, tmp_path):
    import asyncio

    b = _bot(data_dir, tmp_path)
    priv = asyncio.run(b.cmd_pair("ana", "", {"id": 8}, {"id": 8, "type": "private"}))
    assert "/" not in priv.splitlines()[0]  # no link, just the code
    digits = dv.normalise_code(priv.splitlines()[0])
    assert len(digits) == dv.CODE_DIGITS
    # MUT: drop the private-chat check in cmd_pair → the whole group reads someone's key.
    group = asyncio.run(b.cmd_pair("ana", "", {"id": 8}, {"id": -100, "type": "supergroup"}))
    assert group == b.t("bot_pair_private_only") and dv.normalise_code(group) == ""
    # The code the bot read out is the code the app may spend, bound to whoever asked.
    row = dv.take_code(b.state.db, digits)
    assert row and row["person"] == "ana"


def test_the_bot_lists_and_disconnects_a_phone(data_dir, tmp_path):
    import asyncio

    b = _bot(data_dir, tmp_path)
    assert b.t("bot_devices_none") in asyncio.run(
        b.cmd_devices("ana", "", {"id": 8}, {"id": 8, "type": "private"})
    )
    got = dv.pair(b.state.db, dv.make_code(b.state.db, "ana")[0], "iPhone de Ana")
    listed = asyncio.run(b.cmd_devices("ana", "", {"id": 8}, {"id": 8, "type": "private"}))
    assert "iPhone de Ana" in listed and "Ana" in listed
    gone = asyncio.run(
        b.cmd_devices("ana", f"quitar {got['device_id']}", {"id": 8}, {"id": 8, "type": "private"})
    )
    assert str(got["device_id"]) in gone
    assert dv.device_for(b.state.db, got["token"]) is None


def test_every_route_but_ping_and_pair_asks_for_a_token(client):
    """The door, proven over the router itself rather than over a list written by hand: a route
    added later without the device check fails this the day it appears.

    FastAPI keeps an included router as one nested object rather than flattening it, so the walk
    below follows anything that carries `routes` of its own.
    """

    def walk(routes):
        for r in routes:
            inner = getattr(r, "original_router", None)  # FastAPI's _IncludedRouter wrapper
            if inner is not None:
                yield from walk(inner.routes)
            elif hasattr(r, "routes"):
                yield from walk(r.routes)
            elif getattr(r, "path", "").startswith("/api/v1"):
                yield r

    open_paths = {"/api/v1/ping", "/api/v1/pair"}
    seen, checked = set(), 0
    for route in walk(client.app.routes):
        seen.add(route.path)
        for method in sorted(set(route.methods) - {"HEAD", "OPTIONS"}):
            url = route.path.replace("{device_id}", "1").replace("{rid}", "x")
            r = client.request(method, url, json={} if method != "GET" else None)
            if route.path in open_paths:
                # /pair does answer 401 to an empty body — but for the code, never for the token.
                assert "pair this device first" not in r.text, route.path
            else:
                assert r.status_code == 401, f"{method} {route.path} answered {r.status_code}"
                assert "pair this device first" in r.text, route.path
                checked += 1
    assert open_paths <= seen and checked >= 14, (sorted(seen), checked)
