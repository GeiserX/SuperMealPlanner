"""Store catalogues: which real product to buy for each ingredient, how many packs, and the price.

A catalogue is a YAML file in the data directory (see docs/data-format.md, "stores"). Each ingredient
maps to products in preference order. The first clean product wins; a product flagged not clean is
used only when there is nothing else.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict

LOOSE = re.compile(r"granel|a peso|por kg|loose|by (the )?kg|by weight", re.IGNORECASE)


class Pack(BaseModel):
    model_config = ConfigDict(extra="allow")
    qty: float
    unit: str


class Product(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str = ""
    name: str
    pack: Pack
    price_eur: float | None = None
    url: str | None = None
    label: str | None = None
    clean: bool = True
    note: str = ""

    @property
    def loose(self) -> bool:
        return bool(LOOSE.search(self.note or ""))


class Catalog(BaseModel):
    model_config = ConfigDict(extra="allow")
    store: str
    name: str = ""
    region: str | None = None
    checked: str | None = None
    products: dict[str, list[Product]] = {}

    def best(self, ingredient_id: str) -> Product | None:
        options = self.products.get(ingredient_id) or []
        clean = [p for p in options if p.clean]
        return (clean or options or [None])[0]


@lru_cache(maxsize=32)
def _read(path: str, mtime: float) -> Catalog:
    return Catalog.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")))


def catalogs(data) -> dict[str, Catalog]:
    """Catalogues named in settings.stores, plus any stores/*.yaml file in the data directory."""
    out: dict[str, Catalog] = {}
    paths: dict[str, Path] = {}
    for sid, store in data.settings.stores.items():
        if store.catalog:
            paths[sid] = data.root / store.catalog
    sdir = data.root / "stores"
    if sdir.is_dir():
        for p in sorted(sdir.glob("*.yaml")):
            paths.setdefault(p.stem, p)
    for sid, p in paths.items():
        if p.exists():
            out[sid] = _read(str(p), p.stat().st_mtime)
    return out


@dataclass
class Pick:
    store: str
    product: Product
    packs: float  # whole packs, or kilograms for loose produce
    cost: float | None

    def text(self, t) -> str:
        p = self.product
        if p.loose:
            amount = (
                f"{self.packs:.1f} kg".replace(".", ",")
                if t.locale == "es"
                else f"{self.packs:.1f} kg"
            )
            s = f"{p.name} · {amount}"
        else:
            s = t("packs", n=int(self.packs), name=p.name)
        if self.cost is not None:
            s += f" · {t.money(self.cost)}"
        return s


def pick(catalog: Catalog, ingredient_id: str, qty: float, unit: str) -> Pick | None:
    p = catalog.best(ingredient_id)
    if p is None:
        return None
    if p.loose and p.pack.unit == unit and unit == "g":
        kg = max(0.1, math.ceil(qty / 100) / 10)
        price_kg = p.price_eur / (p.pack.qty / 1000) if p.price_eur else None
        return Pick(catalog.store, p, kg, round(price_kg * kg, 2) if price_kg else None)
    if p.pack.unit != unit or p.pack.qty <= 0:
        return Pick(catalog.store, p, 1, p.price_eur)
    n = max(1, math.ceil(qty / p.pack.qty - 0.05))  # a sliver over a pack does not buy another
    return Pick(catalog.store, p, n, round(n * p.price_eur, 2) if p.price_eur else None)
