"""Household data and app settings, loaded from the data directory.

The data directory is the only place that knows who eats here and how the app should behave.
Code never hard-codes a person, a shop, a language or an AI provider. See docs/data-format.md.
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

Meal = Literal["breakfast", "lunch", "dinner", "snack"]
# Tags the planner understands. Recipes may use any words; settings.tags maps them to these.
CANONICAL_TAGS = (
    "legume",
    "fish",
    "oily-fish",
    "egg",
    "salad",
    "side",
    "cold-soup",
    "fried",
    "light",
    "hearty",
    "guests",
    "treat",
    "winter",
    "summer",
)
Weekday = Literal["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
WEEKDAYS: list[str] = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
PILL_SLOTS = ("waking", "breakfast", "lunch", "dinner", "bedtime")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Loose(BaseModel):
    model_config = ConfigDict(extra="allow")


# ---------- ingredients ----------


class KitchenUnit(Strict):
    name: str
    g: float = Field(gt=0)
    plural: str | None = None  # when the language's rule gets it wrong


class Ingredient(Loose):
    id: str = ""
    name: str
    section: str = "other"
    store: str = "store"  # default store id for this ingredient
    unit: Literal["g", "ml", "ud"] = "g"
    kitchen: KitchenUnit | None = None
    flags: list[str] = []
    clean_label: str | None = None


# ---------- recipes ----------


class RecipeItem(Loose):
    id: str
    qty: float = Field(gt=0)
    note: str | None = None


class Nutrition(Loose):
    kcal: float = Field(ge=0)
    protein_g: float = Field(ge=0)
    fiber_g: float = 0
    sat_fat_g: float = 0
    salt_g: float = 0
    source: str = ""


class Recipe(Loose):
    id: str
    name: str
    meals: list[Meal]
    kind: Literal["batch", "quick", "assemble", "fixed"]
    minutes: int = 0
    fridge_days: int = Field(default=2, ge=0, le=4)
    freezes: bool = False
    protein: str = "mixed"
    tags: list[str] = []
    season: Literal["all"] | list[int] = "all"
    serves_note: str = ""
    ingredients: list[RecipeItem]
    steps: list[str] = []
    nutrition: Nutrition

    video: str | None = None  # optional link to someone cooking it

    def has(self, tag: str) -> bool:
        """Planner tags are English (CANONICAL_TAGS); a household maps its own words in settings.tags."""
        if tag in self.tags:
            return True
        if tag == "legume":
            return self.protein == "legume"
        if tag == "fish":
            return self.protein in ("fish", "oily-fish")
        if tag == "oily-fish":
            return self.protein == "oily-fish"
        if tag == "egg":
            return self.protein == "egg"
        return False

    def in_season(self, month: int) -> bool:
        return self.season == "all" or month in self.season


# ---------- household ----------


class Pill(Loose):
    name: str
    dose: str = ""
    note: str = ""


class Goal(Loose):
    mode: Literal["lose", "maintain", "gain"] = "maintain"
    target_kg: float | None = None
    band_kg: tuple[float, float] | None = None
    max_loss_kg_week: float = 0.5
    waist_cm_max: float | None = None


class Kcal(Loose):
    start: float = Field(gt=0)
    floor: float = Field(gt=0)
    step: float = 150
    every_weeks: int = 4


class Protein(Loose):
    target: float = Field(gt=0)
    floor: float = Field(gt=0)


class Glp1(Loose):
    drug: str = "semaglutide"
    day: Weekday = "monday"
    early_week_factor: float = 1.0
    late_week_factor: float = 1.0


class Person(Loose):
    id: str
    name: str
    sex: Literal["male", "female"]
    birth_year: int
    height_cm: float
    activity_factor: float = 1.4
    goal: Goal = Goal()
    kcal: Kcal
    protein_g: Protein
    fluid_l: tuple[float, float] | None = None
    weigh_every: Literal["day", "week", "month"] = "week"
    glp1: Glp1 | None = None
    packs: list[str] = []
    avoid_ingredients: list[str] = []
    avoid_flags_at_dinner: list[str] = []
    avoid_tags_at_dinner: list[str] = []
    pills: dict[str, list[Pill]] = {}
    notes: list[str] = []

    @model_validator(mode="after")
    def _pill_slots(self) -> Person:
        bad = set(self.pills) - set(PILL_SLOTS)
        if bad:
            raise ValueError(f"unknown pill slots {sorted(bad)}; use {PILL_SLOTS}")
        return self

    def age(self, on: dt.date) -> int:
        return on.year - self.birth_year


class Calendar(Loose):
    ics_env: str | None = None
    busy_afternoon: tuple[str, str] = ("16:00", "20:00")


class Batch(Loose):
    preferred_days: list[Weekday] = ["monday", "friday", "saturday"]
    sessions_per_week: int = Field(default=2, ge=1, le=2)


class Rules(Loose):
    legumes_min: int = 4
    fish_min: int = 3
    oily_fish_min: int = 2
    salad_daily: bool = True
    max_repeats_per_4_weeks: int = 2


class Household(Loose):
    household: str
    timezone: str = "Europe/Madrid"
    locale: str = "en"
    reference_kcal: float = 2000
    lunch_time: str = "14:00"
    shopping_day: Weekday = "monday"
    guests: dict[str, float] = {}
    calendar: Calendar = Calendar()
    batch: Batch = Batch()
    people: list[Person]
    rules: Rules = Rules()

    def person(self, pid: str) -> Person:
        for p in self.people:
            if p.id == pid:
                return p
        raise KeyError(pid)

    def avoided(self) -> set[str]:
        out: set[str] = set()
        for p in self.people:
            out.update(p.avoid_ingredients)
        return out


# ---------- settings (how the app behaves) ----------


class Store(Loose):
    name: str
    catalog: str | None = None  # path under the data dir, e.g. stores/supermarket.yaml


class Shopping(Loose):
    primary: str | None = None  # store id the list is written for by default
    alternatives: list[str] = []  # stores shown as a second option, and as their own list view


class Reminder(Loose):
    id: str
    kind: Literal["message", "defrost", "batch", "shopping", "pills_lunch", "meal"] = "message"
    meal: Literal["breakfast", "lunch", "dinner"] = "lunch"  # for kind "meal"
    text: str = ""  # for kind "message"
    at: list[str]  # "HH:MM" times in the household timezone
    days: list[Weekday] | Literal["daily"] = "daily"
    people: list[str] = []  # who it is for (mention / pills check); empty = everyone
    enabled: bool = True

    @model_validator(mode="after")
    def _check(self) -> Reminder:
        for at in self.at:
            if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", at):
                raise ValueError(f"reminder {self.id}: time {at!r} is not HH:MM")
        if self.kind == "message":
            try:
                self.text.format(names="", today="")
            except (KeyError, IndexError, ValueError) as e:
                raise ValueError(
                    f"reminder {self.id}: text may only use {{names}} and {{today}} ({e})"
                ) from None
        return self


class Bot(Loose):
    enabled: bool = False
    token_env: str = "TELEGRAM_BOT_TOKEN"
    chat_id: int | None = None  # the household group; reminders go here
    people: dict[str, int] = {}  # person id -> Telegram user id
    history: int = 20  # recent messages kept as context for the AI
    wake_words: list[str] = []  # the AI answers a group message that contains one of these words
    respond: Literal["mentions", "all"] = "mentions"  # "all": every group message goes to the AI
    voice: bool = True  # transcribe voice notes and always hand them to the AI
    private_chats: bool = True  # people in bot.people may also talk to the bot privately
    group_open: bool = False  # True: anyone in the group may use the bot, not only bot.people
    sync_profile: bool = True  # at start, set the bot's command menu and descriptions in Telegram
    share_logs_in_group: bool = (
        False  # True: the AI may discuss binge, craving and note logs in the group
    )
    history_scope: Literal["asked", "all"] = "asked"  # which group messages are kept as AI context


class LLM(Loose):
    provider: Literal["none", "openai", "anthropic", "plugin"] = "none"
    model: str = ""
    base_url: str = (
        ""  # openai: any OpenAI-compatible endpoint (OpenAI, Ollama, OpenRouter, LM Studio)
    )
    api_key_env: str = "LLM_API_KEY"
    plugin: str | None = (
        None  # provider "plugin": a .py file under the data dir defining complete()
    )
    max_tokens: int = 1200
    timeout_s: int = 600
    instructions: str = ""  # extra system instructions for this household


class Topper(Loose):
    label: str
    protein_g: float
    kcal: float = 0


DEFAULT_TOPPERS = [
    Topper(label="1 plain Greek yogurt", protein_g=7, kcal=120),
    Topper(label="1 boiled egg", protein_g=6, kcal=75),
    Topper(label="1 can of tuna in water", protein_g=20, kcal=100),
    Topper(label="100 g skyr or quark", protein_g=11, kcal=65),
]


class Transcription(Loose):
    provider: Literal["none", "openai", "plugin"] = "none"
    base_url: str = (
        ""  # OpenAI-compatible /audio/transcriptions (OpenAI, faster-whisper-server, ...)
    )
    model: str = "whisper-1"
    api_key_env: str = "STT_API_KEY"
    language: str | None = None  # ISO code; None lets the model detect it
    plugin: str | None = (
        None  # a .py file under the data dir defining transcribe(audio, filename, settings)
    )
    timeout_s: int = 600


class Settings(Loose):
    locale: str | None = None  # falls back to household.locale
    base_url: str = ""  # public address of the web app, used for links in bot messages
    stores: dict[str, Store] = {}
    shopping: Shopping = Shopping()
    sections: dict[str, str] = {}  # optional labels for ingredient sections
    bot: Bot = Bot()
    llm: LLM = LLM()
    transcription: Transcription = Transcription()
    reminders: list[Reminder] = []
    tags: dict[str, str] = {}  # your tag or protein word -> a planner tag (CANONICAL_TAGS)
    toppers: list[Topper] = (
        DEFAULT_TOPPERS  # quick extras suggested when a day falls short on protein
    )


# ---------- loading ----------


class DataError(Exception):
    """The data directory is missing something or contradicts itself."""


class Data(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    root: Path
    household: Household
    ingredients: dict[str, Ingredient]
    recipes: dict[str, Recipe]
    settings: Settings = Settings()

    @property
    def locale(self) -> str:
        return self.settings.locale or self.household.locale

    @property
    def state(self) -> Path:
        """Generated files (plans, database). Never edited by hand; ignored by the data repo."""
        p = self.root / "state"
        p.mkdir(parents=True, exist_ok=True)
        return p


def _read_yaml(path: Path):
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise DataError(f"{path}: not valid YAML: {e}") from e


def load(root: str | Path) -> Data:
    root = Path(root)
    errors: list[str] = []

    hh_path = root / "household.yaml"
    if not hh_path.exists():
        raise DataError(f"{hh_path} is missing")
    try:
        household = Household.model_validate(_read_yaml(hh_path))
    except ValidationError as e:
        raise DataError(f"{hh_path}: {e}") from e

    ingredients: dict[str, Ingredient] = {}
    ing_path = root / "ingredients.yaml"
    raw_ing = _read_yaml(ing_path) if ing_path.exists() else {}
    for iid, entry in (raw_ing or {}).items():
        try:
            ingredients[iid] = Ingredient.model_validate({**entry, "id": iid})
        except ValidationError as e:
            errors.append(f"ingredients.yaml {iid}: {e}")

    recipes: dict[str, Recipe] = {}
    for path in sorted((root / "recipes").glob("*.yaml")):
        try:
            r = Recipe.model_validate(_read_yaml(path))
        except ValidationError as e:
            errors.append(f"{path.name}: {e}")
            continue
        if r.id in recipes:
            errors.append(f"{path.name}: duplicate recipe id {r.id}")
        recipes[r.id] = r

    for r in recipes.values():
        for item in r.ingredients:
            if item.id not in ingredients:
                errors.append(f"recipe {r.id}: unknown ingredient {item.id}")

    settings = Settings()
    st_path = root / "settings.yaml"
    if st_path.exists():
        try:
            settings = Settings.model_validate(_read_yaml(st_path) or {})
        except ValidationError as e:
            errors.append(f"settings.yaml: {e}")
    tagmap = settings.tags
    for bad in sorted(set(tagmap.values()) - set(CANONICAL_TAGS)):
        errors.append(f"settings.yaml tags: {bad} is not a planner tag {CANONICAL_TAGS}")
    for r in recipes.values():
        r.tags = [tagmap.get(t, t) for t in r.tags]
        r.protein = tagmap.get(r.protein, r.protein)
    for person in household.people:
        person.avoid_tags_at_dinner = [tagmap.get(t, t) for t in person.avoid_tags_at_dinner]

    people = {p.id for p in household.people}
    for pid in settings.bot.people:
        if pid not in people:
            errors.append(f"settings.yaml bot.people: unknown person {pid}")
    for r in settings.reminders:
        if r.kind == "message" and not r.text:
            errors.append(f"settings.yaml reminder {r.id}: kind message needs text")
        for pid in r.people:
            if pid not in people:
                errors.append(f"settings.yaml reminder {r.id}: unknown person {pid}")

    if errors:
        raise DataError("\n".join(errors))
    return Data(
        root=root, household=household, ingredients=ingredients, recipes=recipes, settings=settings
    )
