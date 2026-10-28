"""The dish for everyone together: what the cook weighs out before splitting it into plates.

* **The sum of the plates.** Each person's servings and swaps, added up, with the kitchen count
  («≈ 3 contramuslos») worked out on the total, not on one plate.
* **On both faces.** The Today and recipe pages show it above the split, and `/api/v1` sends it
  for every dish of the day and for a recipe, so the phone can show the same.
"""

import pytest
from fastapi.testclient import TestClient

from supermealplanner.db import DB
from supermealplanner.planner import generate, plans_dir
from supermealplanner.portions import fmt_qty, person_days, plate, together

from .conftest import MONDAY
from .test_app_api import auth, pair
from .test_fields import swapping


def test_the_total_adds_up_every_plate_with_each_persons_swaps(data_dir, tmp_path):
    data = swapping(data_dir)  # Ana eats turkey where the recipe says chicken
    db = DB(tmp_path / "x.db")
    r = next(x for x in data.recipes.values() if x.ingredients[0].id == "pollo")
    f = {pd.person_id: pd.factor for pd in person_days(data, db, generate(data, MONDAY, 4), MONDAY)}
    all_ = together(data, r, f)
    assert all_.factor == pytest.approx(f["ana"] + f["luis"])
    assert all_.kcal == pytest.approx(
        sum(plate(data, r, f[p.id], p.swaps).kcal for p in data.household.people)
    )
    pollo, pavo = data.ingredients["pollo"], data.ingredients["pavo"]
    assert f"Pollo: {fmt_qty(pollo, 150 * f['luis'], data.locale)}" in all_.lines
    assert f"Pavo: {fmt_qty(pavo, 150 * f['ana'], data.locale)}" in all_.lines
    both = together(data, r, {"luis": 2.0, "x": 1.0})  # 450 g of chicken: about four thighs
    assert "Pollo: 450 g (≈ 4 contramuslos)" in both.lines


@pytest.fixture
def client(data_dir, monkeypatch):
    monkeypatch.setenv("SMP_SCHEDULER", "0")
    monkeypatch.setenv("SMP_BOT", "0")
    data = swapping(data_dir)
    plan = generate(data, MONDAY, 4)
    plan.save(plans_dir(data) / f"{MONDAY}.yaml")
    from supermealplanner.web import create_app

    with TestClient(create_app(data_dir)) as c:
        yield c, data, plan


def test_today_and_the_recipe_page_show_the_total_above_the_split(client):
    c, data, plan = client
    rid = plan.day(MONDAY).lunch
    db = DB(data.state / "supermealplanner.db")
    f = {pd.person_id: pd.factor for pd in person_days(data, db, plan, MONDAY)}
    first = together(data, data.recipes[rid], f).lines[0]
    for path in (f"/?d={MONDAY}", f"/recipe/{rid}?d={MONDAY}"):
        page = c.get(path).text
        part = page.split(f"/recipe/{rid}", 1)[1] if path.startswith("/?") else page
        assert "Everyone together" in part, path
        assert part.index("Everyone together") < part.index("<h3>Ana"), path
        assert first in part, path


def test_the_api_sends_the_total_of_every_dish_and_of_a_recipe(client, data_dir):
    c, _, _ = client
    h = auth(pair(c, data_dir)["token"])
    day = c.get(f"/api/v1/day?d={MONDAY}", headers=h).json()
    for meal in day["meals"]:
        for dish in meal["dishes"]:
            total = dish["total"]
            assert total["factor"] == pytest.approx(
                sum(p["factor"] for p in dish["plates"]), abs=0.01
            )
            assert total["kcal"] == pytest.approx(sum(p["kcal"] for p in dish["plates"]), abs=2)
            assert total["lines"] and total["recipe"]["id"] == dish["recipe"]["id"]
    lunch = next(m for m in day["meals"] if m["meal"] == "lunch")["dishes"][0]
    r = c.get(f"/api/v1/recipe/{lunch['recipe']['id']}?d={MONDAY}", headers=h).json()
    assert r["total"] == lunch["total"]
    assert {p["person"] for p in r["plates"]} == {"ana", "luis"}
    assert [p["lines"] for p in r["plates"]] == [p["lines"] for p in lunch["plates"]]
