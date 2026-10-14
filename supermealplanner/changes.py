"""Settings and menu changes the AI may make from the chat, for the person who asked.

The AI ends an answer with one fenced block:

    ```change
    asked: true     # the person asked for exactly this: applied at once. Without it, after a yes
    set:
      household.lunch_time: "14:30"
      settings.reminders.water: {at: ["11:00", "17:00"]}
    recipe: {id: lentil-stew, name: Lentil stew, ...}
    recipes:        # fields of recipes that exist; a meal taken away also leaves the planned days
      porridge: {meals: []}
    notes:          # facts about a person, kept in their chat_notes
      - add: {person: ana, text: Works 9 to 14 on Tuesdays}
      - remove: {person: ana, text: Works 9 to 14 on Tuesdays}
    plan:           # the menu itself, see menu.py
      - set: {date: 2026-01-14, meal: dinner, recipe: tuna-salad}
    ```

Only the paths in ALLOWED can change; pills, calorie and protein targets and goals never can. Avoided
foods are often medical (a drug interaction, a kidney diet): a person may only add to their own list,
and only by hand can a food come off it. A pills reminder changes only for a person it names. A person's
`notes` are written by hand and are often medical: the chat only adds to and removes from the
separate `chat_notes` list. Applying
edits the files in the data folder (keeping their comments), runs the same validation as
`smp check` on a copy first, and makes one git commit that is pushed to the data repo, so every
change can be reverted. Without a git checkout and SMP_GIT_WRITE_TOKEN nothing is written: the bot
shows the change for a person to make.
"""

from __future__ import annotations

import difflib
import io
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.scalarstring import DoubleQuotedScalarString, LiteralScalarString
from ruamel.yaml.util import load_yaml_guess_indent

from . import menu
from .model import Data, DataError, check

log = logging.getLogger("supermealplanner")

# path pattern ("*" is one segment) -> label key in the locale files (change_<key>)
ALLOWED = {
    "household.lunch_time": "lunch_time",
    "household.shopping_day": "shopping_day",
    "household.guests.*": "guests",
    "household.batch.preferred_days": "batch_days",
    "household.batch.sessions_per_week": "batch_sessions",
    "household.batch.avoid_days": "batch_avoid_days",
    "household.rules.legumes_min": "rule",
    "household.rules.fish_min": "rule",
    "household.rules.oily_fish_min": "rule",
    "household.rules.salad_daily": "rule",
    "household.rules.max_repeats_per_4_weeks": "rule",
    "household.people.*.avoid_ingredients": "avoid",
    "household.people.*.swaps": "swaps",
    "household.people.*.training_days": "training_days",
    "settings.shopping.primary": "shopping_primary",
    "settings.shopping.alternatives": "shopping_alternatives",
    "settings.sections.*": "section",
    "settings.reminders.*": "reminder",
    "settings.llm.instructions": "instructions",
}
# what a change may set on a recipe that exists; id and kind, and how it keeps, stay by hand
RECIPE_FIELDS = (
    "name",
    "meals",
    "tags",
    "season",
    "serves_note",
    "video",
    "steps",
    "ingredients",
    "nutrition",
)
NOTES_MAX = 20  # chat notes per person
NOTE_MAX = 300  # characters in one
INSTRUCTIONS_MAX = 4000  # characters of the assistant's own instructions a chat change may set
LISTS_BY_ID = {"people", "reminders"}  # lists whose items are addressed by their id
BLOCK = re.compile(r"```change[ \t]*\n(.*?)```", re.DOTALL)
RECIPE_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
SEGMENT = re.compile(r"[A-Za-z0-9_-]{1,64}")
GIT_LOCK = threading.Lock()  # one git command sequence at a time in the data folder


class ChangeError(Exception):
    """A change that is not allowed, does not validate, or could not be saved. Safe to show."""


@dataclass
class Edit:
    file: str  # household.yaml or settings.yaml
    keys: list[str]  # the path inside that file
    value: object
    label: str  # key of ALLOWED
    arg: str = ""  # the "*" segment: a guest, a person id, a reminder id, a section


@dataclass
class Proposal:
    edits: list[Edit] = field(default_factory=list)
    recipe: dict | None = None
    recipes: dict[str, dict] = field(default_factory=dict)  # recipe id -> the fields that change
    notes: list[tuple[str, str, str]] = field(default_factory=list)  # (add|remove, person, text)
    plan: list[menu.Op] = field(default_factory=list)  # menu changes, written under state/
    asked: bool = False  # the person asked for exactly this: no yes/no round
    raw: dict = field(default_factory=dict)  # JSON-safe, kept while the bot waits for a yes


def extract(text: str) -> tuple[str, dict | str | None]:
    """The answer without its change block, and the block parsed (a dict), its parse error (a str)
    or None when there is no block."""
    blocks = list(BLOCK.finditer(text))
    if not blocks:
        return text.strip(), None
    clean = BLOCK.sub("", text).strip()
    try:
        raw = yaml.safe_load(blocks[-1].group(1))
    except yaml.YAMLError as e:
        return clean, f"not valid YAML ({str(e).splitlines()[0]})"
    if not isinstance(raw, dict):
        return clean, "the block is not a mapping"
    return clean, json.loads(json.dumps(raw, default=str))  # dates and the like become text


def _match(path: str) -> tuple[str, str] | None:
    parts = path.split(".")
    for pattern, label in ALLOWED.items():
        pat = pattern.split(".")
        if len(pat) == len(parts) and all(p in ("*", s) for p, s in zip(pat, parts, strict=True)):
            star = next((s for p, s in zip(pat, parts, strict=True) if p == "*"), "")
            return label, star
    return None


PILLS_REMINDER_KINDS = {"pills_lunch"}  # changed or dropped from the chat only by a person it names
# a person's own: only they may change these from the chat
OWN = {"avoid", "swaps", "training_days"}


def parse(raw: dict, data: Data, asker: str | None = None) -> Proposal:
    """Check a proposed change against the allow-list, for the household member who asked. Raises
    ChangeError with the reason."""
    known = {"set", "recipe", "recipes", "notes", "plan", "asked"}
    if set(raw) - known:
        raise ChangeError(
            f"unknown keys {sorted(set(raw) - known)}; use {', '.join(sorted(known))}"
        )
    out = Proposal(raw=raw, asked=raw.get("asked") is True)
    sets = raw.get("set") or {}
    if not isinstance(sets, dict):
        raise ChangeError("set must map a path to its new value")
    people = {p.id for p in data.household.people}
    for path, value in sets.items():
        path = str(path)
        if not all(SEGMENT.fullmatch(s) for s in path.split(".")):
            raise ChangeError(f"{path} is not a settings path")
        hit = _match(path)
        if not hit:
            raise ChangeError(f"{path} cannot be changed from the chat")
        label, arg = hit
        top, *keys = path.split(".")
        if label in OWN:
            if arg not in people:
                raise ChangeError(f"no person {arg}")
            if arg != asker:
                raise ChangeError(
                    "only the person themselves can change their avoided foods, swaps or "
                    "training days"
                )
        if label == "swaps" and not (
            isinstance(value, dict) and all(isinstance(v, str) for v in value.values())
        ):
            raise ChangeError(f"{path} must be the whole new map of ingredient id to ingredient id")
        if label in ("training_days", "batch_avoid_days") and not isinstance(value, list):
            raise ChangeError(f"{path} must be the whole new list of weekdays")
        if label == "avoid":
            if not isinstance(value, list):
                raise ChangeError(f"{path} must be the whole new list")
            dropped = [
                x for x in data.household.person(arg).avoid_ingredients if x not in map(str, value)
            ]
            if dropped:
                raise ChangeError(
                    f"{', '.join(dropped)} can only come off the avoided foods by hand; "
                    "from the chat foods can only be added"
                )
        if label == "reminder":
            now = next((r for r in data.settings.reminders if r.id == arg), None)
            kinds = {
                now.kind if now else None,
                value.get("kind") if isinstance(value, dict) else None,
            }
            if kinds & PILLS_REMINDER_KINDS:
                # A pills reminder belongs to the people it names: each of them may change it or
                # drop it from the chat (the person who stops taking a pill at lunch, for example),
                # nobody may set one for someone else, and the pills themselves stay by hand.
                named = set(now.people) if now is not None else set()
                if (
                    now is not None
                    and now.kind in PILLS_REMINDER_KINDS
                    and named
                    and asker not in named
                ):
                    raise ChangeError(
                        "someone else's pills reminder can only be changed by that person or by hand"
                    )
                if isinstance(value, dict) and "people" in value:
                    extra = set(map(str, value["people"] or [])) - named - {asker}
                    if extra:
                        raise ChangeError("a pills reminder can only name the person who asks")
                if (now is None or now.kind not in PILLS_REMINDER_KINDS) and (
                    not isinstance(value, dict)
                    or list(map(str, value.get("people") or [])) != [asker]
                ):
                    raise ChangeError(
                        "a new pills reminder can only be set for the person who asks"
                    )
        if label == "instructions" and not (
            isinstance(value, str) and len(value) <= INSTRUCTIONS_MAX
        ):
            raise ChangeError(
                f"{path} must be the whole new text, at most {INSTRUCTIONS_MAX} characters"
            )
        if label == "reminder" and value is not None and not isinstance(value, dict):
            raise ChangeError(f"{path} must be the reminder's fields, or null to remove it")
        if label == "reminder" and isinstance(value, dict) and "id" in value and value["id"] != arg:
            raise ChangeError(f"{path} cannot rename the reminder")
        out.edits.append(Edit(f"{top}.yaml", keys, value, label, arg))
    recipe = raw.get("recipe")
    if recipe is not None:
        if not isinstance(recipe, dict):
            raise ChangeError("recipe must be one recipe in the data format")
        rid = str(recipe.get("id", ""))
        if not RECIPE_ID.fullmatch(rid):
            raise ChangeError("the new recipe needs an id of lowercase letters, digits and dashes")
        if rid in data.recipes or (data.root / "recipes" / f"{rid}.yaml").exists():
            raise ChangeError(f"a recipe {rid} already exists; only new recipes can be added")
        out.recipe = recipe
    out.recipes = _recipe_edits(raw.get("recipes"), data)
    out.notes = _notes(raw.get("notes"), data)
    if raw.get("plan") is not None:
        try:
            out.plan = menu.parse(raw["plan"], data, (out.recipe or {}).get("id"))
        except menu.MenuError as e:
            raise ChangeError(str(e)) from None
    if not (out.edits or out.recipe is not None or out.recipes or out.notes or out.plan):
        raise ChangeError("the change is empty")
    return out


def _recipe_edits(raw, data: Data) -> dict[str, dict]:
    if raw is None:
        return {}
    if not isinstance(raw, dict) or not raw:
        raise ChangeError("recipes must map a recipe id to the fields that change")
    for rid, fields in raw.items():
        if rid not in data.recipes:
            raise ChangeError(f"no recipe {rid}; a new one goes under recipe")
        if not isinstance(fields, dict) or not fields:
            raise ChangeError(f"recipes.{rid} must be the fields that change")
        bad = set(fields) - set(RECIPE_FIELDS)
        if bad:
            raise ChangeError(
                f"recipes.{rid}: {', '.join(sorted(map(str, bad)))} cannot be changed from the "
                f"chat; only {', '.join(RECIPE_FIELDS)}"
            )
    return raw


def _notes(raw, data: Data) -> list[tuple[str, str, str]]:
    if raw is None:
        return []
    if not isinstance(raw, list) or not raw:
        raise ChangeError("notes must be a list of add and remove")
    out = []
    have = {p.id: list(p.chat_notes) for p in data.household.people}
    for item in raw:
        if (
            not isinstance(item, dict)
            or len(item) != 1
            or next(iter(item)) not in ("add", "remove")
        ):
            raise ChangeError("each note change is add or remove, with person and text")
        op, args = next(iter(item.items()))
        if not isinstance(args, dict) or set(args) != {"person", "text"}:
            raise ChangeError(f"{op} needs person and text")
        pid, text = str(args["person"]), " ".join(str(args["text"]).split())
        if pid not in have:
            raise ChangeError(f"no person {pid}")
        if op == "remove":
            if text not in have[pid]:
                raise ChangeError(
                    "only a note the chat added can be removed, by its exact text; "
                    "notes written by hand are changed by hand"
                )
            have[pid].remove(text)
        else:
            if not text or len(text) > NOTE_MAX:
                raise ChangeError(f"a note is 1 to {NOTE_MAX} characters")
            if text in have[pid]:
                raise ChangeError("that note is already there")
            if len(have[pid]) >= NOTES_MAX:
                raise ChangeError(f"at most {NOTES_MAX} chat notes per person; remove one first")
            have[pid].append(text)
        out.append((op, pid, text))
    return out


def retired(p: Proposal, data: Data) -> set[tuple[str, str]]:
    """(recipe id, meal) pairs a recipe edit takes away: the planned days must lose them too."""
    out = set()
    for rid, fields in p.recipes.items():
        if isinstance(fields.get("meals"), list) and rid in data.recipes:
            out |= {(rid, m) for m in data.recipes[rid].meals if m not in fields["meals"]}
    return out


# ---------------------------------------------------------------- describe


def _show(value, t) -> str:
    if value is None:
        return t("change_none")
    if isinstance(value, bool):
        return t("change_yes") if value else t("change_no")
    if isinstance(value, list):
        return ", ".join(_show(v, t) for v in value) or t("change_none")
    if isinstance(value, dict):
        return "; ".join(
            f"{t(f'change_field_{k}')} {_field(k, v, t)}" for k, v in value.items() if k != "id"
        )
    return str(value)


WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def _field(key: str, value, t) -> str:
    """A reminder field in the household's language: weekdays and "daily" are words, not codes."""
    if key == "days":
        if value == "daily":
            return t("change_daily")
        if isinstance(value, list):
            short = t.raw("days_short")
            return ", ".join(short[WEEKDAYS.index(v)] if v in WEEKDAYS else str(v) for v in value)
    return _show(value, t)


def _current(data: Data, edit: Edit):
    node = (data.household if edit.file == "household.yaml" else data.settings).model_dump()
    for k in edit.keys:
        if isinstance(node, list):
            node = next((x for x in node if isinstance(x, dict) and x.get("id") == k), None)
        elif isinstance(node, dict):
            node = node.get(k)
        else:
            return None
    return node


def describe(p: Proposal, data: Data, t) -> list[str]:
    """One plain line per change, with the value it has now and the one it would get."""
    lines = []
    for e in p.edits:
        name = e.arg
        if e.label in OWN:
            name = data.household.person(e.arg).name
        what = t(f"change_{e.label}", name=name)
        old = _current(data, e)
        if e.label == "swaps":
            lines.append(f"• {what}: {_swaps(old, data, t)} → {_swaps(e.value, data, t)}")
            continue
        if e.label in ("training_days", "batch_avoid_days"):
            days = [_field("days", v, t) if v else t("change_none") for v in (old, e.value)]
            lines.append(f"• {what}: {days[0]} → {days[1]}")
            continue
        if e.label == "instructions":  # a whole text: only the lines that change
            lines.append(f"• {what}:")
            lines += _text_diff(old if isinstance(old, str) else "", str(e.value or ""))
            continue
        if e.label == "reminder" and old is not None and isinstance(e.value, dict):
            new = {**old, **e.value}  # only the fields that change are sent
            old = {k: old.get(k) for k in e.value}
            new = {k: new.get(k) for k in e.value}
        else:
            new = e.value
        lines.append(f"• {what}: {_show(old, t)} → {_show(new, t)}")
    if p.recipe is not None:
        lines.append(
            "• "
            + t(
                "change_recipe",
                name=p.recipe.get("name", p.recipe.get("id")),
                n=len(p.recipe.get("ingredients") or []),
            )
        )
    for rid, fields in p.recipes.items():
        r = data.recipes[rid]
        for key, value in fields.items():
            old = getattr(r, key, None)
            lines.append(
                "• "
                + t("change_recipe_edit", name=r.name, field=t(f"change_recipe_field_{key}"))
                + f": {_recipe_value(key, _plain(old), t)} → {_recipe_value(key, value, t)}"
            )
    for op, pid, text in p.notes:
        name = data.household.person(pid).name
        lines.append("• " + t(f"change_note_{op}", name=name, text=text))
    new = {p.recipe["id"]: p.recipe.get("name")} if p.recipe else None
    return lines + menu.describe(p.plan, data, t, new)


def _swaps(value, data: Data, t) -> str:
    """A person's swaps as "Apple → Pear", with the ingredients' names."""
    if not value:
        return t("change_none")

    def name(iid) -> str:
        ing = data.ingredients.get(str(iid))
        return ing.name if ing else str(iid)

    return ", ".join(f"{name(a)} → {name(b)}" for a, b in value.items())


def _plain(value):
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value.model_dump() if hasattr(value, "model_dump") else value


def _recipe_value(key: str, value, t) -> str:
    """A recipe field in a line of chat: the long ones as a count, the rest in full."""
    if value is None or value in ("", []):
        return t("change_none")
    if key == "meals" and isinstance(value, list):
        return ", ".join(t(f"meal_{m}").lower() if m in MEALS else str(m) for m in value)
    if key == "steps" and isinstance(value, list):
        return t("change_recipe_steps", n=len(value))
    if key == "ingredients" and isinstance(value, list):
        return ", ".join(
            f"{i.get('id')} {i.get('qty'):g}" if isinstance(i.get("qty"), int | float) else str(i)
            for i in value
            if isinstance(i, dict)
        )
    if key == "nutrition" and isinstance(value, dict):
        return ", ".join(f"{k} {v}" for k, v in value.items() if v not in (None, "", 0))
    return _show(value, t)


MEALS = ("breakfast", "lunch", "dinner", "snack")


def _text_diff(old: str, new: str) -> list[str]:
    """The lines a new text removes (−) and adds (+), without the ones it keeps."""
    out = []
    for line in difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=0):
        if line.startswith(("---", "+++", "@@")):
            continue
        out.append(("  + " if line[0] == "+" else "  − ") + line[1:])
    return out


def manual(p: Proposal) -> list[str]:
    """The change as a person would make it by hand: file, path, value."""
    lines = []
    for e in p.edits:
        lines.append(f"{e.file}: {'.'.join(e.keys)} = {json.dumps(e.value, ensure_ascii=False)}")
    if p.recipe is not None:
        lines.append(f"recipes/{p.recipe['id']}.yaml:")
        lines.append(yaml.safe_dump(p.recipe, allow_unicode=True, sort_keys=False).strip())
    for rid, fields in p.recipes.items():
        for key, value in fields.items():
            lines.append(f"recipe {rid}: {key} = {json.dumps(value, ensure_ascii=False)}")
    for op, pid, text in p.notes:
        lines.append(
            f"household.yaml: people.{pid}.chat_notes {op} {json.dumps(text, ensure_ascii=False)}"
        )
    return lines


# ---------------------------------------------------------------- apply


def _yaml(text: str) -> tuple[YAML, object]:
    _, indent, offset = load_yaml_guess_indent(text) if text.strip() else (None, 2, 0)
    y = YAML()
    y.preserve_quotes = True
    y.width = 4096
    y.indent(mapping=2, sequence=indent or 2, offset=offset or 0)
    doc = y.load(text) if text.strip() else None
    return y, doc if doc is not None else CommentedMap()


def _child(node, key: str, create_list: bool):
    if isinstance(node, list):
        for item in node:
            if isinstance(item, dict) and str(item.get("id")) == key:
                return item
        raise ChangeError(f"no entry {key}")
    if not isinstance(node, dict):
        raise ChangeError(f"{key} is not inside a mapping")
    if node.get(key) is None:
        node[key] = CommentedSeq() if create_list else CommentedMap()
    return node[key]


def _set(doc, keys: list[str], value) -> None:
    node = doc
    for k in keys[:-1]:
        node = _child(node, k, k in LISTS_BY_ID)
    last = keys[-1]
    if isinstance(node, list):  # an item addressed by id: merge its fields, null removes it
        idx = next(
            (i for i, x in enumerate(node) if isinstance(x, dict) and str(x.get("id")) == last),
            None,
        )
        if value is None:
            if idx is not None:
                del node[idx]
            return
        if idx is None:
            item = CommentedMap(id=last)
            node.append(item)
        else:
            item = node[idx]
        for k, v in value.items():
            if v is None:
                item.pop(k, None)
            else:
                _assign(item, k, v)
        return
    if value is None:
        node.pop(last, None)
    else:
        _assign(node, last, value)


def _quote(value):
    """Quote every string the app's own YAML reader would take for something else ("14:30" is a
    number to it, "yes" a boolean), so the value reads back exactly as proposed."""
    if isinstance(value, list):
        return [_quote(v) for v in value]
    if isinstance(value, dict):
        return {k: _quote(v) for k, v in value.items()}
    if isinstance(value, str) and "\n" in value.strip("\n"):
        # several lines stay a readable block; ruamel falls back to quotes when a block cannot
        # carry the text exactly
        return LiteralScalarString(value)
    if isinstance(value, str):
        try:
            same = yaml.safe_load(value) == value
        except yaml.YAMLError:
            same = False
        return value if same else DoubleQuotedScalarString(value)
    return value


def _assign(node, key, value) -> None:
    value = _quote(value)
    old = node.get(key)
    if isinstance(old, list) and isinstance(value, list):  # in place: keeps flow style and comments
        del old[:]
        old.extend(value)
    else:
        node[key] = value


def render(root: Path, p: Proposal) -> dict[str, str]:
    """The new text of every file the change touches, comments kept."""
    out: dict[str, str] = {}
    names = [e.file for e in p.edits] + (["household.yaml"] if p.notes else [])
    for name in dict.fromkeys(names):
        path = root / name
        y, doc = _yaml(path.read_text(encoding="utf-8") if path.exists() else "")
        for e in p.edits:
            if e.file == name:
                _set(doc, e.keys, e.value)
        for op, pid, text in p.notes if name == "household.yaml" else ():
            person = _child(_child(doc, "people", True), pid, False)
            if person.get("chat_notes") is None:
                person["chat_notes"] = CommentedSeq()
            if op == "add":
                person["chat_notes"].append(_quote(text))
            else:
                person["chat_notes"].remove(text)
        buf = io.StringIO()
        y.dump(doc, buf)
        out[name] = buf.getvalue()
    if p.recipe is not None:
        out[f"recipes/{p.recipe['id']}.yaml"] = yaml.safe_dump(
            p.recipe, allow_unicode=True, sort_keys=False
        )
    for rid, fields in p.recipes.items():
        path = _recipe_file(root, rid)
        y, doc = _yaml(path.read_text(encoding="utf-8"))
        for key, value in fields.items():
            if value is None:
                doc.pop(key, None)
            else:
                _assign(doc, key, value)
        buf = io.StringIO()
        y.dump(doc, buf)
        out[str(path.relative_to(root))] = buf.getvalue()
    return out


def _recipe_file(root: Path, rid: str) -> Path:
    """The file that holds a recipe: named after its id, or whichever says `id: <rid>`."""
    named = root / "recipes" / f"{rid}.yaml"
    for path in [named, *sorted((root / "recipes").glob("*.yaml"))]:
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        if isinstance(raw, dict) and raw.get("id") == rid:
            return path
    raise ChangeError(f"no file for recipe {rid}")


def validate(root: Path, texts: dict[str, str]) -> None:
    """Run `smp check` on a copy of the data folder with the new texts. Raises ChangeError."""
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "data"
        shutil.copytree(root, copy, ignore=shutil.ignore_patterns(".git", "state"))
        for name, text in texts.items():
            (copy / name).parent.mkdir(parents=True, exist_ok=True)
            (copy / name).write_text(text, encoding="utf-8")
        try:
            check(copy, health=False)  # pills never change from the chat
        except DataError as e:
            raise ChangeError(f"the data would not validate: {str(e)[:500]}") from None


def git_env(token: str = "") -> dict[str, str] | None:
    """The environment for one git call that sends the token. It goes in the environment, never
    on the command line, where any process could read it, nor in the checkout's config."""
    if not token:
        return None
    env = dict(os.environ)
    n = int(env.get("GIT_CONFIG_COUNT") or 0)
    env["GIT_CONFIG_COUNT"] = str(n + 1)
    env[f"GIT_CONFIG_KEY_{n}"] = "http.extraHeader"
    env[f"GIT_CONFIG_VALUE_{n}"] = f"Authorization: token {token}"
    return env


def _git(root: Path, *args: str, token: str = "") -> subprocess.CompletedProcess:
    cmd = ["git", "-C", str(root)]
    cmd += [
        "-c",
        "user.name=SuperMealPlanner",
        "-c",
        "user.email=supermealplanner@localhost",
        *args,
    ]
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=120, check=False, env=git_env(token)
    )


def apply(root: Path, p: Proposal, message: str, token: str) -> str:
    """Pull, edit, validate, commit and push one change. Returns the commit's short id. On any
    failure the data folder is left as it was and ChangeError says why."""
    if not (root / ".git").exists():
        raise ChangeError("the data folder is not a git checkout")
    with GIT_LOCK:
        if _git(root, "status", "--porcelain", "--untracked-files=no").stdout.strip():
            raise ChangeError("the data folder has local edits; nothing was changed")
        r = _git(root, "pull", "--ff-only", "--quiet", token=token)
        if r.returncode != 0:
            raise ChangeError(f"could not update the data repo: {r.stderr.strip()[:200]}")
        before = _git(root, "rev-parse", "HEAD").stdout.strip()
        texts = render(root, p)
        validate(root, texts)
        new = [root / name for name in texts if not (root / name).exists()]
        for name, text in texts.items():
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_text(text, encoding="utf-8")
        steps = (
            ("add", "--", *texts),
            ("commit", "--quiet", "-m", message),
            ("push", "--quiet"),
        )
        for step in steps:
            r = _git(root, *step, token=token if step[0] == "push" else "")
            if r.returncode != 0:
                _git(root, "reset", "--hard", "--quiet", before)
                for path in new:  # a new file git never took
                    path.unlink(missing_ok=True)
                raise ChangeError(f"git {step[0]} failed: {r.stderr.strip()[:200]}")
        return _git(root, "rev-parse", "--short", "HEAD").stdout.strip()
