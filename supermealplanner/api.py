"""The JSON API the phone apps talk to: `/api/v1`, one bearer token per paired device.

The web pages and this API are the same program reading the same plan, so a tick made on a phone
shows on the laptop and the other way round. The pages render Jinja; this returns the same facts as
JSON, computed by calling the very same planner and portion functions, never a second copy of the
rules.

Two things are deliberate:

  * **Every route needs a device token except `POST /pair` and `GET /ping`.** The HTML pages are
    reachable only from the LAN and the tailnet; this API is the part a phone can reach from
    anywhere, so it carries the household's data behind a bearer and nothing else.
  * **The shapes here are a contract.** An app in someone's pocket cannot be updated in step with the
    server, so the fields below are added to, never renamed or removed, and an unknown query
    parameter is ignored rather than refused. `GET /ping` carries the API version an old app can read
    before it asks for anything else.

The token itself and how it is granted: `devices.py`.
"""

from __future__ import annotations

import asyncio
import datetime as dt

from fastapi import APIRouter, HTTPException, Request

from . import __version__, llm
from .i18n import Translator
from .planner import day_menu, paused_days, plan_for
from .portions import (
    MEAL_ORDER,
    batch_for_text,
    batch_sheet,
    batch_split_text,
    person_days,
    plate,
    shopping_list,
    ticked_last,
)
from .targets import evaluate, trend

API_VERSION = 1
CHEF_WAIT_MAX = 25  # the longest one GET /chef/{id} holds, so no client's request times out
TRANSCRIBE_MAX_BYTES = 10 * 1024 * 1024  # a spoken sentence is a few hundred KB even as WAV
AUDIO_TYPES = {  # what POST /transcribe takes, and the file name the transcriber is given
    "audio/wav": "audio.wav",
    "audio/x-wav": "audio.wav",
    "audio/wave": "audio.wav",
    "audio/ogg": "audio.ogg",
    "audio/opus": "audio.opus",
    "audio/mpeg": "audio.mp3",
    "audio/mp4": "audio.m4a",
    "audio/x-m4a": "audio.m4a",
    "audio/aac": "audio.aac",
    "audio/webm": "audio.webm",
    "audio/flac": "audio.flac",
}


def _day(raw: str | None, today: dt.date) -> dt.date:
    if not raw:
        return today
    try:
        return dt.date.fromisoformat(raw)
    except ValueError:
        raise HTTPException(400, "d must be a date as YYYY-MM-DD") from None


def _monday(d: dt.date) -> dt.date:
    return d - dt.timedelta(days=d.weekday())


def _recipe_brief(r) -> dict:
    """What a list or a menu shows about a dish. `/recipe/{id}` has the whole thing."""
    return {
        "id": r.id,
        "name": r.name,
        "kind": r.kind,
        "minutes": r.minutes,
        "tags": list(r.tags),
        "protein": r.protein,
        "video": r.video,
    }


def _plate(p) -> dict:
    return {
        "recipe": _recipe_brief(p.recipe),
        "factor": round(p.factor, 3),
        "lines": list(p.lines),
        "kcal": round(p.kcal),
        "protein_g": round(p.protein, 1),
    }


def _line(x) -> dict:
    """One shopping line, with the text the app shows and the numbers it may want to sort by."""
    pick = x.pick
    return {
        "ingredient": x.ingredient.id,
        "name": x.ingredient.name,
        "section": x.ingredient.section,
        "amount": x.amount,
        "qty": round(x.qty, 2),
        "unit": x.ingredient.unit,
        "product": getattr(pick, "name", None),
        "packs": getattr(pick, "packs", None),
        "price": getattr(pick, "price", None),
    }


def _section(lines: list, order: list[str], times: dict[str, str]) -> list[dict]:
    """A section's lines as the page shows them: still to buy first, then ticked, first ticked first.

    `position` is the line's place in the section's usual order, so an app can put an unticked
    line back where it belongs without asking again.
    """
    pos = {x.ingredient.id: n for n, x in enumerate(lines)}
    done = set(order)
    return [
        _line(x)
        | {
            "position": pos[x.ingredient.id],
            "ticked": x.ingredient.id in done,
            "ticked_at": times.get(x.ingredient.id),
        }
        for x in ticked_last(lines, order)
    ]


def _ticks(db, m: dt.date) -> dict:
    """The week's ticks: the ids sorted (as ever), in tick order, and when each was ticked."""
    order = db.tick_order(m)
    return {
        "monday": m.isoformat(),
        "ticked": sorted(order),
        "ticked_order": order,
        "ticked_at": db.tick_times(m),
    }


def router(state) -> APIRouter:
    """The /api/v1 router for a running `web.App`. Built here so web.py only mounts it."""
    from . import devices as dv

    api = APIRouter(prefix="/api/v1", tags=["app"])
    guard = dv.Guard()

    def people_ids() -> set[str]:
        return {p.id for p in state.data.household.people}

    def device(request: Request) -> dict:
        """The first line of every protected route: the live device behind the bearer, or a 401.

        Written out rather than injected so there is no way to add a route and forget the check —
        a handler without this line has no `row` to answer with and fails its test at once.
        """
        header = request.headers.get("authorization", "")
        token = header[7:].strip() if header[:7].lower() == "bearer " else ""
        row = dv.device_for(state.db, token)
        if row is None:
            raise HTTPException(401, "pair this device first: ask the bot for a code")
        return row

    def whose(row: dict, asked: str | None) -> str | None:
        """Which person a call is about: the one asked for, else the device's own."""
        who = asked or row.get("person")
        return who if who in people_ids() else None

    # ---------------------------------------------------------------- pairing
    @api.get("/ping")
    def ping():
        """The one route an unpaired app may call: is this a SuperMealPlanner, and which version."""
        return {"ok": True, "app": "supermealplanner", "version": __version__, "api": API_VERSION}

    @api.post("/pair")
    def pair(body: dict, request: Request):
        """Turn a code from the bot into a device token. The token is in this answer only."""
        source = (request.client.host if request.client else "?") or "?"
        wait = guard.blocked(source)
        if wait:
            raise HTTPException(
                429,
                "too many wrong codes; try again later",
                headers={"Retry-After": str(int(wait) + 1)},
            )
        got = dv.pair(state.db, str(body.get("code", "")), body.get("device"))
        if got is None:
            guard.fail(source)
            raise HTTPException(401, "that code is wrong, already used or too old")
        guard.ok(source)
        names = {p.id: p.name for p in state.data.household.people}
        return {
            "token": got["token"],
            "device_id": got["device_id"],
            "device": got["name"],
            "person": got["person"],
            "person_name": names.get(got["person"] or ""),
            "household": state.data.household.household,
        }

    @api.get("/devices")
    def list_devices(request: Request):
        """Every paired phone, so the app can show them and take one away."""
        row = device(request)
        names = {p.id: p.name for p in state.data.household.people}
        return {
            "devices": [
                {**d, "person_name": names.get(d.get("person") or ""), "this": d["id"] == row["id"]}
                for d in state.db.devices()
            ]
        }

    @api.delete("/devices/{device_id}")
    def revoke(device_id: int, request: Request):
        """Unpair a phone, this one included: its next call gets a 401."""
        device(request)
        if not state.db.revoke_device(device_id):
            raise HTTPException(404, "no such device")
        return {"ok": True, "revoked": device_id}

    # ---------------------------------------------------------------- what is eaten
    @api.get("/meta")
    def meta(request: Request):
        """What the app needs once at start-up: who lives here, the stores, the language."""
        row = device(request)
        hh = state.data.household
        return {
            "household": hh.household,
            "timezone": hh.timezone,
            "locale": state.data.locale,
            "version": __version__,
            "api": API_VERSION,
            "today": state.today().isoformat(),
            "people": [{"id": p.id, "name": p.name} for p in hh.people],
            "stores": [
                {"id": sid, "name": s.name} for sid, s in state.data.settings.stores.items()
            ],
            "sections": dict(state.data.settings.sections),
            "device": {"id": row["id"], "name": row["name"], "person": row.get("person")},
        }

    @api.get("/day")
    def day(request: Request, d: str | None = None, person: str | None = None):
        """One day: every meal in the order it is eaten, and each person's plate."""
        row = device(request)
        today = state.today()
        date = _day(d, today)
        plan = plan_for(state.data, date)
        pday = plan.day(date) if plan else None
        people = person_days(state.data, state.db, plan, date) if plan else []
        menu = day_menu(state.data, pday) if pday else []
        who = whose(row, person)
        swaps = {p.id: p.swaps for p in state.data.household.people}
        meals = []
        for meal in MEAL_ORDER:
            dishes = [r for m, r in menu if m == meal]
            if not dishes:
                continue
            meals.append(
                {
                    "meal": meal,
                    "dishes": [
                        {
                            "recipe": _recipe_brief(r),
                            "plates": [
                                {
                                    "person": pd.person_id,
                                    "name": pd.name,
                                    **_plate(
                                        plate(state.data, r, pd.factor, swaps.get(pd.person_id))
                                    ),
                                }
                                for pd in people
                            ],
                        }
                        for r in dishes
                    ],
                }
            )
        tomorrow = date + dt.timedelta(days=1)
        defrost = []
        if plan:
            for s in plan.sessions:
                for x in s.dishes:
                    r = state.data.recipes.get(x.recipe)
                    if r and any(
                        e == tomorrow and (e - x.cook).days > r.fridge_days for e in x.eat
                    ):
                        defrost.append(_recipe_brief(r))
        return {
            "day": date.isoformat(),
            "today": today.isoformat(),
            "paused": date in paused_days(state.data),
            "planned": plan is not None and pday is not None,
            "meals": meals,
            "people": [
                {
                    "person": pd.person_id,
                    "name": pd.name,
                    "kcal_target": round(pd.kcal_target),
                    "kcal": round(pd.kcal),
                    "protein_target": round(pd.protein_target, 1),
                    "protein_g": round(pd.protein, 1),
                    "factor": round(pd.factor, 3),
                    "toppers": list(pd.toppers),
                    "days_since_injection": pd.days_since_injection,
                }
                for pd in people
            ],
            "defrost_for_tomorrow": defrost,
            "votes": state.db.ratings(who or "-") if who else {},
            "person": who,
        }

    @api.get("/week")
    def week(request: Request, d: str | None = None):
        """Seven days of dish names, for the week view."""
        device(request)
        today = state.today()
        m = _monday(_day(d, today))
        days = [m + dt.timedelta(days=i) for i in range(7)]
        plan = plan_for(state.data, m) or plan_for(state.data, days[-1])
        paused = paused_days(state.data)
        out = []
        for x in days:
            pd = plan.day(x) if plan else None
            out.append(
                {
                    "day": x.isoformat(),
                    "paused": x in paused,
                    "meals": [
                        {"meal": meal, "recipe": _recipe_brief(r)}
                        for meal, r in (day_menu(state.data, pd) if pd else [])
                    ],
                }
            )
        return {"monday": m.isoformat(), "today": today.isoformat(), "days": out}

    @api.get("/tasks")
    def tasks(request: Request, d: str | None = None):
        """The week's jobs: what to take out of the freezer, what to cook, when to shop."""
        device(request)
        from .web import week_tasks  # same computation the Tasks page renders (web.py imports us)

        m = _monday(_day(d, state.today()))
        return {
            "monday": m.isoformat(),
            "today": state.today().isoformat(),
            "days": [
                {"day": x.isoformat(), "tasks": [{"at": at, "text": text} for at, text in jobs]}
                for x, jobs in week_tasks(state, m)
            ],
        }

    @api.get("/cook")
    def cook(request: Request, d: str | None = None):
        """A batch afternoon: every dish to cook that day, how much, and which lunches it feeds."""
        device(request)
        t = Translator(state.data.locale)
        date = _day(d, state.today())
        plan = plan_for(state.data, date)
        sheet = batch_sheet(state.data, state.db, plan, date) if plan else []
        names = {p.id: p.name for p in state.data.household.people}
        return {
            "day": date.isoformat(),
            "dishes": [
                {
                    "recipe": _recipe_brief(b.recipe),
                    "servings": b.servings,
                    "morning": b.morning,
                    "eat_on": [c.eat.isoformat() for c in b.containers],
                    "for_text": batch_for_text(t, [c.eat for c in b.containers]),
                    "split_text": batch_split_text(b, names),
                    "lines": list(b.lines),
                    "containers": [
                        {
                            "eat": c.eat.isoformat(),
                            "storage": c.storage,
                            "defrost_on": c.defrost_on.isoformat() if c.defrost_on else None,
                            "servings": {k: round(v, 2) for k, v in c.servings.items()},
                        }
                        for c in b.containers
                    ],
                }
                for b in sheet
            ],
        }

    @api.get("/recipe/{rid}")
    def recipe(request: Request, rid: str, d: str | None = None, person: str | None = None):
        """A whole recipe, with this person's plate when there is a plan for the day."""
        row = device(request)
        r = state.data.recipes.get(rid)
        if r is None:
            raise HTTPException(404, "no such recipe")
        who = whose(row, person)
        date = _day(d, state.today())
        mine = None
        if who:
            plan = plan_for(state.data, date)
            for pd in person_days(state.data, state.db, plan, date) if plan else []:
                if pd.person_id == who:
                    swaps = {p.id: p.swaps for p in state.data.household.people}
                    mine = _plate(plate(state.data, r, pd.factor, swaps.get(who)))
        return {
            "recipe": r.model_dump(mode="json"),
            "plate": mine,
            "vote": state.db.ratings(who or "-").get(rid) if who else None,
        }

    # ---------------------------------------------------------------- the shopping list
    @api.get("/list")
    def shopping(request: Request, d: str | None = None, store: str | None = None):
        """The week's list: per store, in the order the aisles come, with what is already ticked."""
        device(request)
        t = Translator(state.data.locale)
        m = _monday(_day(d, state.today()))
        plan = plan_for(state.data, m)
        shop = shopping_list(state.data, state.db, plan, m, t, view=store) if plan else None
        ticks = _ticks(state.db, m)
        return {
            "monday": m.isoformat(),
            "view": store,
            "views": [{"id": i, "label": label} for i, label in (shop.views if shop else [])],
            "labels": dict(shop.labels) if shop else {},
            "stores": [
                {
                    "id": s.id,
                    "name": s.name,
                    "total": round(s.total, 2),
                    "unpriced": s.unpriced,
                    "sections": [
                        {
                            "section": sec,
                            "lines": _section(lines, ticks["ticked_order"], ticks["ticked_at"]),
                        }
                        for sec, lines in s.sections.items()
                    ],
                }
                for s in (shop.stores if shop else [])
            ],
            "ticked": ticks["ticked"],
            "ticked_order": ticks["ticked_order"],
            "ticked_at": ticks["ticked_at"],
            "have": sorted(state.db.have()),
        }

    @api.get("/list/ticks")
    def ticks(request: Request, d: str | None = None):
        """Only the week's ticks: cheap enough for an open list to ask every few seconds, so a tick
        made on another phone or on the web page shows without reloading the whole list."""
        device(request)
        return {"ok": True} | _ticks(state.db, _monday(_day(d, state.today())))

    @api.post("/list/tick")
    def tick(body: dict, request: Request):
        """Tick an ingredient off this week's list, or put it back. Shared with every other phone."""
        device(request)
        ingredient = str(body.get("ingredient", "")).strip()
        if not ingredient:
            raise HTTPException(400, "ingredient is required")
        m = _monday(_day(body.get("d"), state.today()))
        state.db.tick(m, ingredient, bool(body.get("on", True)))
        return {"ok": True} | _ticks(state.db, m)

    @api.post("/list/have")
    def have(body: dict, request: Request):
        """«I have it at home»: the ingredient stays off every list until it is put back."""
        device(request)
        ingredient = str(body.get("ingredient", "")).strip()
        if not ingredient:
            raise HTTPException(400, "ingredient is required")
        state.db.set_have(ingredient, bool(body.get("on", True)))
        return {"ok": True, "have": sorted(state.db.have())}

    # ---------------------------------------------------------------- what people log
    @api.post("/rate")
    def rate(body: dict, request: Request):
        """👍 or 👎 on a dish. The next menus follow these."""
        row = device(request)
        who = whose(row, body.get("person"))
        if not who:
            raise HTTPException(400, "unknown person")
        recipe_id = str(body.get("recipe", "")).strip()
        if recipe_id not in state.data.recipes:
            raise HTTPException(404, "no such recipe")
        try:
            vote = int(body.get("vote", 0))
        except (TypeError, ValueError):
            raise HTTPException(400, "vote must be 1, -1 or 0") from None
        if vote not in (1, -1, 0):
            raise HTTPException(400, "vote must be 1, -1 or 0")
        state.db.rate(recipe_id, who, vote, state.today())
        return {"ok": True, "recipe": recipe_id, "person": who, "vote": vote}

    @api.post("/log")
    def log(body: dict, request: Request):
        """The Log page as JSON: weight, waist, the day's numbers and a note."""
        row = device(request)
        who = whose(row, body.get("person"))
        if not who:
            raise HTTPException(400, "unknown person")
        date = _day(body.get("day"), state.today())
        fields: dict = {}
        try:
            if body.get("kg") is not None:
                state.db.add_weight(who, date, float(body["kg"]))
            if body.get("waist_cm") is not None:
                state.db.add_waist(who, date, float(body["waist_cm"]))
            for key in ("hunger", "craving", "nausea"):
                if body.get(key) is not None:
                    fields[key] = max(0, min(10, int(body[key])))
            if body.get("water") is not None:
                fields["water"] = max(0, min(40, int(body["water"])))
        except (TypeError, ValueError) as e:
            raise HTTPException(400, str(e)) from None
        for key in ("binge", "stool", "pills_lunch"):
            if body.get(key) is not None:
                fields[key] = 1 if body[key] else 0
        if body.get("note"):
            fields["note"] = str(body["note"]).strip()[:500]
        if fields:
            state.db.set_daily(who, date, **fields)
        return {"ok": True, "person": who, "day": date.isoformat()}

    @api.get("/progress")
    def progress(request: Request):
        """Each person's weight trend and where the targets stand."""
        device(request)
        today = state.today()
        t = Translator(state.data.locale)
        out = []
        for p in state.data.household.people:
            weights = state.db.weights(p.id)
            status = evaluate(state.db, state.data.household, p, today)
            out.append(
                {
                    "person": p.id,
                    "name": p.name,
                    "weights": [{"day": x.isoformat(), "kg": kg} for x, kg in weights[-12:]],
                    "trend": [{"day": x.isoformat(), "kg": kg} for x, kg in trend(weights)[-12:]],
                    "trend_kg": status.trend_kg,
                    "rate_kg_week": status.rate_kg_week,
                    "mode": status.mode,
                    "kcal": round(status.kcal),
                    "protein_g": round(status.protein_g, 1),
                    # Translated here, not in the app: the rules that raise a flag live on this side.
                    "flags": [t(key, **params) for key, params in status.flags],
                    "changed": t(status.changed[0], **status.changed[1])
                    if status.changed
                    else None,
                }
            )
        return {"today": today.isoformat(), "people": out}

    # ---------------------------------------------------------------- talking to the chef
    def chef_bot(row: dict):
        """The running bot and the device's person, who must have a private chat with it."""
        bot = state.bot
        if bot is None or not bot.started or bot.client is None:
            raise HTTPException(503, "the bot is not running here")
        person = row.get("person")
        uid = state.data.settings.bot.people.get(person or "")
        if uid is None:
            raise HTTPException(409, "this device's person has no Telegram user in bot.people")
        if not bot.allowed({"type": "private", "id": uid}, uid):
            raise HTTPException(409, "private chats with the bot are turned off")
        return bot, person

    def spoken_out(s) -> dict:
        out = {"id": s.id, "state": s.state}
        if s.state != "thinking":
            out["text"] = s.answer
        return out

    @api.post("/chef", status_code=202)
    async def chef(body: dict, request: Request):
        """Say something to the chef: it goes into the person's private chat with the bot, as if
        typed there. The answer comes from GET /chef/{id}. Async: the turn starts on the loop the
        bot runs on."""
        row = device(request)
        bot, person = chef_bot(row)
        text = " ".join(str(body.get("text") or "").split()).lstrip("/").strip()
        if not text:
            raise HTTPException(400, "text is required")
        return spoken_out(bot.say(person, text))

    @api.get("/chef/{sid}")
    async def chef_answer(sid: str, request: Request, wait: float = 0):
        """The answer to what was said, as soon as it exists: holds up to `wait` seconds (at most
        CHEF_WAIT_MAX) while the chef thinks. Only the person who said it can read it."""
        row = device(request)
        bot = state.bot
        spoken = bot.spoken_for(sid, row.get("person")) if bot is not None else None
        if spoken is None:
            raise HTTPException(404, "no such turn")
        wait = max(0.0, min(float(wait), CHEF_WAIT_MAX))
        if spoken.state == "thinking" and wait:
            try:
                await asyncio.wait_for(spoken.ready.wait(), wait)
            except TimeoutError:
                pass
        return spoken_out(spoken)

    @api.post("/transcribe")
    async def transcribe(request: Request):
        """Speech to text with the transcriber the bot uses for voice notes, so a voice client
        needs no speech service of its own. The body is the audio; the answer is {"text"}."""
        device(request)
        kind = request.headers.get("content-type", "").split(";")[0].strip().lower()
        name = AUDIO_TYPES.get(kind)
        if name is None:
            raise HTTPException(415, "send the audio as one of: " + ", ".join(AUDIO_TYPES))
        if int(request.headers.get("content-length") or 0) > TRANSCRIBE_MAX_BYTES:
            raise HTTPException(413, f"audio over {TRANSCRIBE_MAX_BYTES} bytes")
        audio = await request.body()
        if not audio:
            raise HTTPException(400, "the body is empty")
        if len(audio) > TRANSCRIBE_MAX_BYTES:
            raise HTTPException(413, f"audio over {TRANSCRIBE_MAX_BYTES} bytes")
        if state.data.settings.transcription.provider == "none":
            raise HTTPException(503, "no transcription is configured here")
        try:
            text = await asyncio.wait_for(
                llm.transcribe(state.data, audio, name),
                timeout=state.data.settings.transcription.timeout_s,
            )
        except Exception as e:  # noqa: BLE001  a plugin or a remote service: say it failed
            raise HTTPException(502, f"transcription failed: {type(e).__name__}") from None
        return {"text": " ".join(text.split())}

    return api
