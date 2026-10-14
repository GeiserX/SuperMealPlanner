"""A person's pills from a health export: a file another service keeps (settings.health_export).

The export is YAML with `people: {<person id>: {meds: [{name, dose, slot, days, food}]}}`; every
other key is ignored. When it is set and readable, a person it lists with `meds` takes their pills
from it, and household.yaml's pills stay the fallback: for a person the export leaves out, and for
everyone when the export cannot be read and no copy of it was ever kept.

The file is fetched when the data loads (at start, and on every hourly reload), with the ETag of the
last good copy in If-None-Match, so an unchanged file costs one 304. The last good copy is kept under
the data folder (`cache`, beside a `.meta` file with its ETag): a failed fetch uses it. Nothing here
logs the URL, a header or the file.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from pathlib import Path

import httpx
import yaml

from .model import PILL_SLOTS, WEEKDAYS, HealthExport, Household, Pill

log = logging.getLogger("supermealplanner")

# the export's slots: the app's, plus "as_needed", which is taken when needed and is no daily pill
EXPORT_SLOTS = (*PILL_SLOTS, "as_needed")
MAX_BYTES = 256 * 1024

FROM_EXPORT = "the health export"
FROM_CACHE = "the health export, last good copy (this fetch failed)"
NOT_LISTED = "household.yaml (not in the health export)"
UNAVAILABLE = "household.yaml (the health export could not be read)"


def _get(url: str, headers: dict, timeout: float) -> httpx.Response:
    return httpx.get(url, headers=headers, timeout=timeout)


def _doc_people(text: str) -> dict | None:
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError:
        return None
    people = doc.get("people") if isinstance(doc, dict) else None
    return people if isinstance(people, dict) else None


def pills(text: str) -> dict[str, dict[str, list[Pill]]] | None:
    """person id -> slot -> pills, for every person the export lists with meds. None when the text
    is not an export at all; ValueError when it is one with a slot or a day the app cannot place."""
    people = _doc_people(text)
    if people is None:
        return None
    out: dict[str, dict[str, list[Pill]]] = {}
    for pid, entry in people.items():
        if not isinstance(entry, dict) or not isinstance(entry.get("meds"), list):
            continue
        slots: dict[str, list[Pill]] = {}
        for med in entry["meds"]:
            name = str(med.get("name") or "").strip() if isinstance(med, dict) else ""
            if not name:
                raise ValueError(f"health export {pid}: a med without a name")
            slot = med.get("slot")
            if slot not in EXPORT_SLOTS:
                raise ValueError(
                    f"health export {pid} {name}: unknown slot {slot!r}; "
                    f"the app knows {', '.join(EXPORT_SLOTS)}"
                )
            days = med.get("days", "daily")
            if days != "daily" and (
                not isinstance(days, list)
                or not days
                or not all(isinstance(d, str) and d in WEEKDAYS for d in days)
            ):
                raise ValueError(
                    f"health export {pid} {name}: days {days!r} is not daily or a list of weekdays"
                )
            if slot == "as_needed":
                continue
            slots.setdefault(slot, []).append(
                Pill(
                    name=name,
                    dose=str(med.get("dose") or ""),
                    note=str(med.get("food") or ""),
                    days=days,
                )
            )
        out[str(pid)] = slots
    return out


def fetch(root: Path, cfg: HealthExport) -> tuple[str | None, str, str | None]:
    """(text, state, etag): state "new" (a 200 not kept yet), "same" (the copy, confirmed by a 304)
    or "failed" (the last good copy, None when there is none)."""
    body = root / cfg.cache
    meta = body.with_name(body.name + ".meta")
    try:  # the meta first: a writer replaces the file before its meta, never after
        etag = json.loads(meta.read_text(encoding="utf-8")).get("etag")
    except (OSError, ValueError, AttributeError):
        etag = None
    try:
        cached = body.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        cached = None
    headers = {}
    token = os.environ.get(cfg.token_env, "").strip() if cfg.token_env else ""
    if token:
        headers["Authorization"] = f"token {token}"
    if cached is not None and etag:
        headers["If-None-Match"] = str(etag)
    try:
        r = _get(cfg.url, headers, cfg.timeout_s)
    except httpx.HTTPError as e:
        log.warning("health export fetch failed: %s", type(e).__name__)
        return cached, "failed", None
    if r.status_code == 304 and cached is not None:
        return cached, "same", None
    if r.status_code != 200 or len(r.content) > MAX_BYTES:
        log.warning("health export fetch failed: HTTP %s", r.status_code)
        return cached, "failed", None
    try:
        text = r.content.decode("utf-8")
    except UnicodeDecodeError:
        text = None
    if text is None or _doc_people(text) is None:
        log.warning("health export refused: not an export")
        return cached, "failed", None
    return text, "new", r.headers.get("ETag")


def _keep(root: Path, cfg: HealthExport, text: str, etag: str | None) -> None:
    body = root / cfg.cache
    body.parent.mkdir(parents=True, exist_ok=True)
    meta = body.with_name(body.name + ".meta")
    for path, content in ((body, text), (meta, json.dumps({"etag": etag}))):
        tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")  # others may read it meanwhile
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, path)


def apply(root: Path, cfg: HealthExport, household: Household) -> dict[str, str]:
    """Give each person the export's pills when it lists them; returns person id -> where their
    pills came from. Raises ValueError when the export has a slot or a day the app cannot place,
    and then keeps nothing."""
    text, state, etag = fetch(root, cfg)
    found = pills(text) if text is not None else None
    if found is None:
        return {p.id: UNAVAILABLE for p in household.people}
    if state == "new":
        _keep(root, cfg, text, etag)
    sources = {}
    for p in household.people:
        if p.id in found:
            p.pills = found[p.id]
            sources[p.id] = FROM_CACHE if state == "failed" else FROM_EXPORT
        else:
            sources[p.id] = NOT_LISTED
    return sources
