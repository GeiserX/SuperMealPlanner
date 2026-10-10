"""A synthetic household: two people, one on a GLP-1, enough recipes for a 4-week plan."""

import datetime as dt
from pathlib import Path

import pytest
import yaml

HOUSEHOLD = {
    "household": "Prueba",
    "people": [
        {
            "id": "ana",
            "name": "Ana",
            "sex": "female",
            "birth_year": 1990,
            "height_cm": 165,
            "goal": {"mode": "maintain", "band_kg": [58, 61]},
            "kcal": {"start": 1800, "floor": 1500, "step": 100},
            "protein_g": {"target": 90, "floor": 70},
            "pills": {"waking": [{"name": "Hierro"}], "lunch": []},
        },
        {
            "id": "luis",
            "name": "Luis",
            "sex": "male",
            "birth_year": 1988,
            "height_cm": 180,
            "goal": {"mode": "lose", "target_kg": 80, "band_kg": [79, 81]},
            "kcal": {"start": 2300, "floor": 1800, "step": 150},
            "protein_g": {"target": 115, "floor": 100},
            "glp1": {"day": "monday", "early_week_factor": 0.95, "late_week_factor": 1.05},
            "packs": ["glp1", "binge-eating-safe"],
            "avoid_ingredients": ["espinaca"],
            "avoid_tags_at_dinner": ["cold-soup", "fried"],
            "pills": {"lunch": [{"name": "Vitamin D", "dose": "600 µg"}]},
        },
    ],
}

INGREDIENTS = {
    "lenteja": {"name": "Lenteja", "section": "despensa", "unit": "g", "flags": ["legume"]},
    "garbanzo": {"name": "Garbanzo", "section": "despensa", "unit": "g", "flags": ["legume"]},
    "merluza": {"name": "Merluza", "section": "pescaderia", "store": "mercado", "unit": "g"},
    "salmon": {"name": "Salmón", "section": "pescaderia", "store": "mercado", "unit": "g"},
    "pollo": {
        "name": "Pollo",
        "section": "carniceria",
        "store": "mercado",
        "unit": "g",
        "kitchen": {"name": "contramuslo", "g": 110},
    },
    "egg": {
        "name": "Huevo",
        "section": "lacteos-huevos",
        "unit": "ud",
        "kitchen": {"name": "egg", "g": 60},
    },
    "lechuga": {"name": "Lechuga", "section": "fruteria", "store": "mercado", "unit": "g"},
    "espinaca": {"name": "Espinaca", "section": "fruteria", "store": "mercado", "unit": "g"},
    "aceite": {"name": "AOVE", "section": "despensa", "unit": "ml"},
    "yogur": {"name": "Yogur griego", "section": "lacteos-huevos", "unit": "g"},
    "tomate": {"name": "Tomate", "section": "fruteria", "store": "mercado", "unit": "g"},
}


def recipe(rid, meals, kind, protein="mixed", tags=(), items=None, freezes=True, kcal=600, prot=35):
    return {
        "id": rid,
        "name": rid.replace("-", " ").capitalize(),
        "meals": meals,
        "kind": kind,
        "minutes": 20,
        "fridge_days": 2,
        "freezes": freezes,
        "protein": protein,
        "tags": list(tags),
        "ingredients": items or [{"id": "aceite", "qty": 10}, {"id": "tomate", "qty": 100}],
        "steps": ["Cocinar."],
        "nutrition": {"kcal": kcal, "protein_g": prot},
    }


def recipes():
    out = []
    for i in range(4):
        out.append(
            recipe(
                f"legumbre-{i}",
                ["lunch"],
                "batch",
                "legume",
                ["legume", "light" if i % 2 else "hearty"],
                [{"id": "lenteja" if i % 2 else "garbanzo", "qty": 70}, {"id": "aceite", "qty": 8}],
            )
        )
    for i in range(3):
        out.append(
            recipe(
                f"pollo-{i}",
                ["lunch"],
                "batch",
                "pollo",
                ["guests"] if i == 0 else [],
                [{"id": "pollo", "qty": 150}, {"id": "aceite", "qty": 8}],
            )
        )
    out.append(
        recipe("pescado-batch", ["lunch"], "batch", "fish", [], [{"id": "merluza", "qty": 150}])
    )
    out.append(
        recipe("salmon-batch", ["lunch"], "batch", "oily-fish", [], [{"id": "salmon", "qty": 130}])
    )
    out.append(recipe("pollo-sin-congelar", ["lunch"], "batch", "chicken", [], freezes=False))
    out.append(
        recipe(
            "espinacas-con-garbanzos",
            ["lunch"],
            "batch",
            "legume",
            ["legume"],
            [{"id": "espinaca", "qty": 100}, {"id": "garbanzo", "qty": 70}],
        )
    )
    for i in range(4):
        out.append(
            recipe(
                f"merluza-cena-{i}",
                ["dinner"],
                "quick",
                "fish",
                ["light"],
                [{"id": "merluza", "qty": 150}],
                kcal=450,
            )
        )
    for i in range(4):
        out.append(
            recipe(
                f"salmon-cena-{i}",
                ["dinner"],
                "quick",
                "oily-fish",
                [],
                [{"id": "salmon", "qty": 130}],
                kcal=500,
            )
        )
    for i in range(6):
        out.append(
            recipe(
                f"ensalada-{i}",
                ["dinner"],
                "assemble",
                "egg",
                ["salad", "light"],
                [{"id": "lechuga", "qty": 80}, {"id": "egg", "qty": 2}],
                kcal=450,
            )
        )
    for i in range(3):
        out.append(
            recipe(
                f"crema-legumbre-{i}",
                ["dinner"],
                "quick",
                "legume",
                ["legume"],
                [{"id": "lenteja", "qty": 50}],
                kcal=450,
            )
        )
    out.append(recipe("gazpacho", ["dinner", "lunch"], "assemble", "mixed", ["cold-soup"]))
    out.append(recipe("fritura", ["dinner"], "quick", "fish", ["fried"]))
    out.append(
        recipe(
            "desayuno-a",
            ["breakfast"],
            "fixed",
            "dairy",
            [],
            [{"id": "yogur", "qty": 200}],
            kcal=400,
            prot=30,
        )
    )
    out.append(
        recipe(
            "desayuno-b",
            ["breakfast"],
            "fixed",
            "egg",
            [],
            [{"id": "egg", "qty": 2}],
            kcal=400,
            prot=30,
        )
    )
    out.append(
        recipe(
            "merienda-a",
            ["snack"],
            "fixed",
            "dairy",
            [],
            [{"id": "yogur", "qty": 150}],
            kcal=200,
            prot=15,
        )
    )
    out.append(
        recipe(
            "treat",
            ["snack"],
            "fixed",
            "dairy",
            ["treat"],
            [{"id": "yogur", "qty": 150}],
            kcal=200,
            prot=15,
        )
    )
    for i in range(2):
        out.append(
            recipe(
                f"guarnicion-{i}",
                ["lunch", "dinner"],
                "assemble",
                "mixed",
                ["salad", "side"],
                [{"id": "lechuga", "qty": 60}],
                kcal=100,
                prot=2,
            )
        )
    return out


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    (tmp_path / "recipes").mkdir()
    (tmp_path / "household.yaml").write_text(yaml.safe_dump(HOUSEHOLD, allow_unicode=True))
    (tmp_path / "ingredients.yaml").write_text(yaml.safe_dump(INGREDIENTS, allow_unicode=True))
    for r in recipes():
        (tmp_path / "recipes" / f"{r['id']}.yaml").write_text(yaml.safe_dump(r, allow_unicode=True))
    return tmp_path


@pytest.fixture
def data(data_dir):
    from supermealplanner.model import load

    return load(data_dir)


MONDAY = dt.date(2026, 10, 12)
