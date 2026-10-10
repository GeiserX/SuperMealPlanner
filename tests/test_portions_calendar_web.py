import datetime as dt

from fastapi.testclient import TestClient

from supermealplanner.calendar import busy_afternoons_from_ics
from supermealplanner.db import DB
from supermealplanner.planner import generate, plans_dir
from supermealplanner.portions import person_days, servings_for_week, shopping_list

from .conftest import MONDAY

ICS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:test
BEGIN:VEVENT
UID:1
DTSTART;TZID=Europe/Madrid:20261013T170000
DTEND;TZID=Europe/Madrid:20261013T180000
SUMMARY:Reunión
END:VEVENT
BEGIN:VEVENT
UID:2
DTSTART;VALUE=DATE:20261014
DTEND;VALUE=DATE:20261015
SUMMARY:Festivo
END:VEVENT
BEGIN:VEVENT
UID:3
DTSTART;TZID=Europe/Madrid:20261015T090000
DTEND;TZID=Europe/Madrid:20261015T110000
SUMMARY:Mañana
END:VEVENT
BEGIN:VEVENT
UID:4
DTSTART;TZID=Europe/Madrid:20261012T190000
DTEND;TZID=Europe/Madrid:20261012T200000
RRULE:FREQ=WEEKLY;COUNT=3
SUMMARY:Clase semanal
END:VEVENT
END:VCALENDAR
"""


def test_calendar_busy_afternoons(data):
    busy = busy_afternoons_from_ics(ICS, MONDAY, MONDAY + dt.timedelta(days=21), data.household)
    assert busy[MONDAY + dt.timedelta(days=1)] == ["Reunión"]
    assert MONDAY + dt.timedelta(days=2) not in busy  # all-day
    assert MONDAY + dt.timedelta(days=3) not in busy  # morning only
    assert MONDAY in busy and MONDAY + dt.timedelta(days=14) in busy  # recurring


def test_plates_scale_per_person(data, tmp_path):
    db = DB(tmp_path / "t.db")
    plan = generate(data, MONDAY, 4)
    pds = {p.person_id: p for p in person_days(data, db, plan, MONDAY + dt.timedelta(days=5))}
    assert pds["luis"].factor > pds["ana"].factor
    assert pds["luis"].plates["lunch"].kcal > pds["ana"].plates["lunch"].kcal
    assert pds["luis"].days_since_injection == 5 and pds["ana"].days_since_injection is None


def test_shopping_sums_both_plates(data, tmp_path):
    db = DB(tmp_path / "t.db")
    plan = generate(data, MONDAY, 4)
    sv = servings_for_week(data, db, plan, MONDAY)
    d = plan.days[3]
    both = 1800 / 2000 + (2300 / 2000) * 0.95
    assert (
        abs(
            sv[d.breakfast]
            - sum(
                (1800 / 2000 + 2300 / 2000 * f)
                for x, f in zip(plan.days[:7], [1, 0.95, 0.95, 0.95, 1.05, 1.05, 1.05])
                if x.breakfast == d.breakfast
            )
        )
        < 0.05
    )
    assert both > 0
    shop = shopping_list(data, db, plan, MONDAY)
    names = [line.ingredient.id for sl in shop.stores for line in sl.lines]
    assert "aceite" in names and "espinaca" not in names


def test_web_pages(data_dir, data, monkeypatch):
    monkeypatch.setenv("SMP_SCHEDULER", "0")
    monkeypatch.setenv("SMP_API_TOKEN", "secret")
    plan = generate(data, MONDAY, 4)
    plan.save(plans_dir(data) / f"{MONDAY}.yaml")
    from supermealplanner.web import create_app

    with TestClient(create_app(data_dir)) as c:
        for path in (
            "/healthz",
            f"/?d={MONDAY}",
            f"/week?d={MONDAY}",
            f"/list?d={MONDAY}",
            f"/batch?d={MONDAY}",
            "/log",
            "/progress",
            "/manifest.webmanifest",
            f"/recipe/{plan.days[0].lunch}?d={MONDAY}",
            f"/semana?d={MONDAY}",  # 0.1 addresses redirect
            "/registro",
            f"/receta/{plan.days[0].lunch}",
        ):
            r = c.get(path)
            assert r.status_code == 200, (path, r.text[:500])
        assert "Vitamin D" in c.get(f"/?d={MONDAY}").text
        assert "Today" in c.get(f"/?d={MONDAY}").text
        assert c.post("/api/weight", json={"person": "luis", "kg": 90}).status_code == 401
        r = c.post(
            "/api/weight",
            json={"person": "luis", "kg": 90, "day": "2026-10-12"},
            headers={"Authorization": "Bearer secret"},
        )
        assert r.status_code == 200
        c.get("/who/ana")
        r = c.post(
            "/log", data={"person": "ana", "day": "2026-10-12", "kg": "58,5", "craving": "3"}
        )
        assert r.status_code == 200 and "Saved" in r.text
        assert "58.5" in c.get("/progress").text


def test_plural_kitchen_units():
    from supermealplanner.portions import plural

    assert plural("diente de ajo", 16, "es") == "dientes de ajo"
    assert plural("filete", 8, "es") == "filetes"
    assert plural("limón", 2, "es") == "limones"
    assert plural("huevo", 1, "es") == "huevo"
    assert plural("nuez", 3, "es") == "nueces"
    assert plural("clove of garlic", 3, "en") == "cloves of garlic"
    assert plural("berry", 2, "en") == "berries"
    assert plural("leaf", 2, "en", explicit="leaves") == "leaves"
