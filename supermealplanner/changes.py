"""Settings and menu changes the AI may make from the chat, for the person who asked.

The AI ends an answer with one fenced block:

    ```change
    asked: true     # the person asked for exactly this: applied at once. Without it, after a yes
    set:
      household.lunch_time: "14:30"
      settings.reminders.water: {at: ["11:00", "17:00"]}
    recipe: {id: lentil-stew, name: Lentil stew, ...}
    plan:           # the menu itself, see menu.py
      - set: {date: 2026-01-14, meal: dinner, recipe: tuna-salad}
    ```

Only the paths in ALLOWED can change; pills, calorie and protein targets and goals never can. Avoided
foods are often medical (a drug interaction, a kidney diet): a person may only add to their own list,
and only by hand can a food come off it. The pills reminder never changes from the chat. Applying
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
    "household.rules.legumes_min": "rule",
    "household.rules.fish_min": "rule",
    "household.rules.oily_fish_min": "rule",
    "household.rules.salad_daily": "rule",
    "household.rules.max_repeats_per_4_weeks": "rule",
    "household.people.*.avoid_ingredients": "avoid",
    "settings.shopping.primary": "shopping_primary",
    "settings.shopping.alternatives": "shopping_alternatives",
    "settings.sections.*": "section",
    "settings.reminders.*": "reminder",
    "settings.llm.instructions": "instructions",
}
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


PROTECTED_REMINDER_KINDS = {"pills_lunch"}  # never added, edited or removed from the chat


def parse(raw: dict, data: Data, asker: str | None = None) -> Proposal:
    """Check a proposed change against the allow-list, for the household member who asked. Raises
    ChangeError with the reason."""
    unknown = set(raw) - {"set", "recipe", "plan", "asked"}
    if unknown:
        raise ChangeError(f"unknown keys {sorted(unknown)}; use set, recipe, plan and asked")
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
        if label == "avoid":
            if arg not in people:
                raise ChangeError(f"no person {arg}")
            if arg != asker:
                raise ChangeError("only the person themselves can add to the foods they avoid")
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
            if kinds & PROTECTED_REMINDER_KINDS:
                raise ChangeError("the pills reminder can only be changed by hand")
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
    if raw.get("plan") is not None:
        try:
            out.plan = menu.parse(raw["plan"], data, (out.recipe or {}).get("id"))
        except menu.MenuError as e:
            raise ChangeError(str(e)) from None
    if not out.edits and out.recipe is None and not out.plan:
        raise ChangeError("the change is empty")
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
        if e.label == "avoid":
            name = data.household.person(e.arg).name
        what = t(f"change_{e.label}", name=name)
        old = _current(data, e)
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
    new = {p.recipe["id"]: p.recipe.get("name")} if p.recipe else None
    return lines + menu.describe(p.plan, data, t, new)


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
    for name in dict.fromkeys(e.file for e in p.edits):
        path = root / name
        y, doc = _yaml(path.read_text(encoding="utf-8") if path.exists() else "")
        for e in p.edits:
            if e.file == name:
                _set(doc, e.keys, e.value)
        buf = io.StringIO()
        y.dump(doc, buf)
        out[name] = buf.getvalue()
    if p.recipe is not None:
        out[f"recipes/{p.recipe['id']}.yaml"] = yaml.safe_dump(
            p.recipe, allow_unicode=True, sort_keys=False
        )
    return out


def validate(root: Path, texts: dict[str, str]) -> None:
    """Run `smp check` on a copy of the data folder with the new texts. Raises ChangeError."""
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "data"
        shutil.copytree(root, copy, ignore=shutil.ignore_patterns(".git", "state"))
        for name, text in texts.items():
            (copy / name).parent.mkdir(parents=True, exist_ok=True)
            (copy / name).write_text(text, encoding="utf-8")
        try:
            check(copy)
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
