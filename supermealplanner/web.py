"""The web app: Today, Week, List, Batch, Log, Progress. Server-rendered, installable as a PWA."""

from __future__ import annotations

import asyncio
import datetime as dt
import hmac
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import __version__
from .db import DB
from .i18n import Translator
from .model import Data, load
from .planner import nibbles, paused_days, plan_for
from .portions import MEAL_ORDER, batch_sheet, person_days, plate, shopping_list
from .scheduler import tick
from .targets import current_kcal, day_factor, evaluate, reason_text, trend

log = logging.getLogger("supermealplanner")
HERE = Path(__file__).parent

# Addresses from 0.1, kept so saved links and home-screen icons still open.
LEGACY = {"/semana": "/week", "/lista": "/list", "/registro": "/log", "/progreso": "/progress"}


class App:
    def __init__(self, root: Path):
        self.root = root
        self.reload()

    def reload(self) -> None:
        self.data: Data = load(self.root)
        self.db = DB(self.data.state / "supermealplanner.db")

    def today(self) -> dt.date:
        return dt.datetime.now(ZoneInfo(self.data.household.timezone)).date()


def create_app(root: str | Path | None = None) -> FastAPI:
    root = Path(root or os.environ.get("SMP_DATA", "/data"))
    state = App(root)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        tasks = []
        if os.environ.get("SMP_SCHEDULER", "1") == "1":
            tasks.append(asyncio.create_task(_loop(state)))
        if state.data.settings.bot.enabled and os.environ.get("SMP_BOT", "1") == "1":
            from .bot import Bot

            tasks.append(asyncio.create_task(_guard(Bot(state).run(), "bot")))
        yield
        for task in tasks:
            task.cancel()

    app = FastAPI(title="SuperMealPlanner", version=__version__, lifespan=lifespan, docs_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    tpl = Jinja2Templates(directory=HERE / "templates")

    def ctx(request: Request, **kw):
        data = state.data
        t = Translator(data.locale)
        who = request.cookies.get("who")
        if who not in {p.id for p in data.household.people}:
            who = None
        return {
            "request": request,
            "hh": data.household,
            "who": who,
            "version": __version__,
            "t": t,
            "lang": data.locale,
            "section_label": lambda s: data.settings.sections.get(
                s, s.replace("-", " ").capitalize()
            ),
            "reason_text": lambda raw: reason_text(t, raw),
            **kw,
        }

    def parse_day(d: str | None) -> dt.date:
        return dt.date.fromisoformat(d) if d else state.today()

    def monday(d: dt.date) -> dt.date:
        return d - dt.timedelta(days=d.weekday())

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "version": __version__}

    def _redirect_to(new: str):
        def handler(request: Request):
            q = f"?{request.url.query}" if request.url.query else ""
            return RedirectResponse(new + q, status_code=301)

        return handler

    for old, new in LEGACY.items():
        app.add_api_route(old, _redirect_to(new), methods=["GET"], include_in_schema=False)

    @app.get("/", response_class=HTMLResponse)
    def today(request: Request, d: str | None = None):
        day = parse_day(d)
        plan = plan_for(state.data, day)
        people = person_days(state.data, state.db, plan, day) if plan else []
        pday = plan.day(day) if plan else None
        tomorrow = day + dt.timedelta(days=1)
        defrost, session = [], None
        if plan:
            for s in plan.sessions:
                if s.date == day and not s.morning:
                    session = s
                for x in s.dishes:
                    r = state.data.recipes.get(x.recipe)
                    if r and any(
                        e == tomorrow and (e - x.cook).days > r.fridge_days for e in x.eat
                    ):
                        defrost.append(r)
        return tpl.TemplateResponse(
            request,
            "today.html",
            ctx(
                request,
                tab="today",
                day=day,
                plan=plan,
                pday=pday,
                paused=day in paused_days(state.data),
                nibbles=nibbles(state.data, pday) if pday else [],
                people=people,
                session=session,
                defrost=defrost,
                recipes=state.data.recipes,
                meals=MEAL_ORDER,
                prev=day - dt.timedelta(days=1),
                next=tomorrow,
            ),
        )

    @app.get("/week", response_class=HTMLResponse)
    def week(request: Request, d: str | None = None):
        m = monday(parse_day(d))
        days = [m + dt.timedelta(days=i) for i in range(7)]
        plan = plan_for(state.data, m) or plan_for(state.data, days[-1])
        rows = [(x, plan.day(x) if plan else None) for x in days]
        picks = {x: nibbles(state.data, pd) for x, pd in rows if pd}
        return tpl.TemplateResponse(
            request,
            "week.html",
            ctx(
                request,
                tab="week",
                monday=m,
                rows=rows,
                picks=picks,
                paused=paused_days(state.data),
                plan=plan,
                recipes=state.data.recipes,
                prev=m - dt.timedelta(days=7),
                next=m + dt.timedelta(days=7),
            ),
        )

    @app.get("/list", response_class=HTMLResponse)
    def shopping(request: Request, d: str | None = None, store: str | None = None):
        m = monday(parse_day(d))
        plan = plan_for(state.data, m)
        t = Translator(state.data.locale)
        shop = shopping_list(state.data, state.db, plan, m, t, view=store) if plan else None
        return tpl.TemplateResponse(
            request,
            "list.html",
            ctx(
                request,
                tab="list",
                monday=m,
                shop=shop,
                store=store,
                prev=m - dt.timedelta(days=7),
                next=m + dt.timedelta(days=7),
            ),
        )

    @app.get("/batch", response_class=HTMLResponse)
    def batch(request: Request, d: str | None = None):
        m = monday(parse_day(d))
        plan = plan_for(state.data, m)
        sessions = []
        if plan:
            for s in plan.sessions:
                if m <= s.date < m + dt.timedelta(days=7):
                    sessions.append((s, batch_sheet(state.data, state.db, plan, s.date)))
        names = {p.id: p.name for p in state.data.household.people}
        return tpl.TemplateResponse(
            request,
            "batch.html",
            ctx(
                request,
                tab="batch",
                monday=m,
                sessions=sessions,
                names=names,
                prev=m - dt.timedelta(days=7),
                next=m + dt.timedelta(days=7),
            ),
        )

    @app.get("/recipe/{rid}", response_class=HTMLResponse)
    def recipe(request: Request, rid: str, d: str | None = None):
        r = state.data.recipes.get(rid)
        if not r:
            raise HTTPException(404)
        day = parse_day(d)
        plates = []
        for p in state.data.household.people:
            f = day_factor(state.data.household, p, current_kcal(state.db, p), day)
            plates.append((p, plate(state.data, r, f, p.swaps)))
        return tpl.TemplateResponse(
            request, "recipe.html", ctx(request, r=r, day=day, plates=plates)
        )

    @app.get("/receta/{rid}")
    def receta(rid: str, d: str | None = None):
        return RedirectResponse(f"/recipe/{rid}" + (f"?d={d}" if d else ""), status_code=301)

    def same_site(request: Request) -> bool:
        """A form post must come from this app: a page elsewhere must not write health logs."""
        origin = request.headers.get("origin") or request.headers.get("referer")
        if not origin:
            return True  # not a browser cross-site post (curl, the phone shortcut)
        from urllib.parse import urlsplit

        return urlsplit(origin).netloc == request.headers.get("host")

    @app.get("/who/{pid}")
    def who(pid: str, back: str = "/log"):
        local = back.startswith("/") and not back.startswith(("//", "/\\"))
        resp = RedirectResponse(back if local else "/", status_code=303)
        resp.set_cookie("who", pid, max_age=3600 * 24 * 400, samesite="lax")
        return resp

    @app.get("/log", response_class=HTMLResponse)
    def log_page(request: Request, ok: str | None = None):
        day = state.today()
        who = request.cookies.get("who")
        today_row = {}
        if who:
            rows = state.db.daily(who, day)
            today_row = rows[0] if rows and rows[0]["day"] == day.isoformat() else {}
        return tpl.TemplateResponse(
            request, "log.html", ctx(request, tab="log", day=day, today_row=today_row, ok=ok)
        )

    @app.post("/log")
    def log_post(
        request: Request,
        person: str = Form(...),
        day: str = Form(...),
        kg: str = Form(""),
        waist: str = Form(""),
        hunger: str = Form(""),
        craving: str = Form(""),
        nausea: str = Form(""),
        water: str = Form(""),
        binge: str = Form(""),
        stool: str = Form(""),
        pills_lunch: str = Form(""),
        note: str = Form(""),
    ):
        if not same_site(request):
            raise HTTPException(403, "cross-site form post")
        if person not in {p.id for p in state.data.household.people}:
            raise HTTPException(400, "unknown person")
        try:
            d = dt.date.fromisoformat(day)
            if kg.strip():
                state.db.add_weight(person, d, float(kg.replace(",", ".")))
            if waist.strip():
                state.db.add_waist(person, d, float(waist.replace(",", ".")))
            fields = {}
            for k, v in (("hunger", hunger), ("craving", craving), ("nausea", nausea)):
                if v.strip():
                    fields[k] = max(0, min(10, int(v)))
            if water.strip():
                fields["water"] = max(0, min(40, int(water)))
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        for k, v in (("binge", binge), ("stool", stool), ("pills_lunch", pills_lunch)):
            if v in ("0", "1"):
                fields[k] = int(v)
        if note.strip():
            fields["note"] = note.strip()[:500]
        state.db.set_daily(person, d, **fields)
        return RedirectResponse("/log?ok=1", status_code=303)

    @app.get("/progress", response_class=HTMLResponse)
    def progress(request: Request):
        today = state.today()
        out = []
        for p in state.data.household.people:
            w = state.db.weights(p.id)
            tr = trend(w)
            st = evaluate(state.db, state.data.household, p, today)
            out.append(
                {
                    "p": p,
                    "status": st,
                    "weights": w[-12:],
                    "waist": state.db.waists(p.id)[-6:],
                    "spark": _spark(tr),
                    "targets": state.db.targets(p.id)[-5:],
                }
            )
        return tpl.TemplateResponse(
            request, "progress.html", ctx(request, tab="log", rows=out, today=today)
        )

    @app.post("/api/weight")
    @app.post("/api/peso")
    async def api_weight(request: Request):
        """For a phone shortcut: Authorization: Bearer <SMP_API_TOKEN>, JSON {person, kg, day?}."""
        token = os.environ.get("SMP_API_TOKEN", "").encode()
        got = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        if not token or not hmac.compare_digest(token, got.encode("utf-8", "replace")):
            raise HTTPException(401)
        try:
            body = await request.json()
            person = body.get("person")
            if person not in {p.id for p in state.data.household.people}:
                raise HTTPException(400, "unknown person")
            day = dt.date.fromisoformat(body["day"]) if body.get("day") else state.today()
            state.db.add_weight(person, day, float(body["kg"]), source="api")
        except (ValueError, KeyError, TypeError, AttributeError) as e:
            raise HTTPException(400, f"bad request: {type(e).__name__}") from None
        return JSONResponse({"ok": True, "person": person, "day": day.isoformat()})

    @app.get("/manifest.webmanifest")
    def manifest():
        t = Translator(state.data.locale)
        return JSONResponse(
            {
                "name": t("app_name"),
                "short_name": t("app_short"),
                "start_url": "/",
                "display": "standalone",
                "background_color": "#f6f3ee",
                "theme_color": "#2f5d50",
                "lang": state.data.locale,
                "icons": [
                    {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png"},
                    {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png"},
                    {
                        "src": "/static/icon-maskable-512.png",
                        "sizes": "512x512",
                        "type": "image/png",
                        "purpose": "maskable",
                    },
                ],
            },
            media_type="application/manifest+json",
        )

    @app.get("/sw.js")
    def sw():
        return FileResponse(HERE / "static" / "sw.js", media_type="text/javascript")

    app.state.smp = state
    return app


def _spark(points, w: int = 280, h: int = 60) -> str:
    """An SVG polyline of the trend, or empty when there is nothing to draw."""
    if len(points) < 2:
        return ""
    xs = [(d - points[0][0]).days for d, _ in points]
    ys = [k for _, k in points]
    x0, x1 = min(xs), max(xs) or 1
    y0, y1 = min(ys), max(ys)
    if y1 - y0 < 0.5:
        y0, y1 = y0 - 0.5, y1 + 0.5
    pts = " ".join(
        f"{(x - x0) / (x1 - x0) * (w - 8) + 4:.1f},{h - 4 - (y - y0) / (y1 - y0) * (h - 8):.1f}"
        for x, y in zip(xs, ys, strict=True)
    )
    return (
        f'<svg viewBox="0 0 {w} {h}" class="spark"><polyline points="{pts}" fill="none" '
        f'stroke="currentColor" stroke-width="2"/></svg>'
    )


async def _guard(coro, name: str) -> None:
    try:
        await coro
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        from .bot import safe

        log.warning("%s stopped: %s", name, safe(e))


async def _loop(state: App) -> None:
    """Hourly: pull the data repo, keep a plan ready, run the weekly targets."""
    while True:
        try:
            await asyncio.to_thread(tick, state)
        except Exception as e:  # noqa: BLE001  keep serving; the next tick retries
            from .bot import safe

            log.warning("scheduler tick failed: %s", safe(e))
        await asyncio.sleep(3600)
