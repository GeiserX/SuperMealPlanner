"""SuperMealPlanner command line: check data, make a plan, print a day or a shopping list, serve the app."""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
from pathlib import Path

from . import __version__
from .db import DB
from .model import DataError, load


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="supermealplanner")
    ap.add_argument("--version", action="version", version=__version__)
    ap.add_argument("--data", default=os.environ.get("SMP_DATA", "/data"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="validate the data directory")
    pp = sub.add_parser("plan", help="generate a 4-week plan")
    pp.add_argument("--start", required=True, help="a Monday, YYYY-MM-DD")
    pp.add_argument("--weeks", type=int, default=4)
    pp.add_argument("--no-calendar", action="store_true")
    pp.add_argument("--write", action="store_true", help="save to state/plans")
    sh = sub.add_parser("shopping", help="print the shopping list of a week")
    sh.add_argument("--week", required=True, help="any day of the week, YYYY-MM-DD")
    sh.add_argument("--store", help="write the whole list for this store")
    sv = sub.add_parser("serve")
    sv.add_argument("--host", default="0.0.0.0")
    sv.add_argument("--port", type=int, default=8080)
    a = ap.parse_args(argv)

    try:
        data = load(a.data)
    except DataError as e:
        print(f"data error:\n{e}", file=sys.stderr)
        return 2

    if a.cmd == "check":
        print(
            f"ok: {len(data.household.people)} people, {len(data.ingredients)} ingredients, "
            f"{len(data.recipes)} recipes"
        )
        return 0

    if a.cmd == "plan":
        from .calendar import busy_afternoons
        from .planner import generate, plan_for, plans_dir

        start = dt.date.fromisoformat(a.start)
        end = start + dt.timedelta(days=7 * a.weeks)
        busy = {} if a.no_calendar else busy_afternoons(data.household, start, end)
        prev = plan_for(data, start - dt.timedelta(days=1))
        plan = generate(data, start, a.weeks, busy=busy, previous=prev)
        for s in plan.sessions:
            kind = "mañana" if s.morning else "tarde"
            dishes = ", ".join(f"{x.recipe} x{len(x.eat)}" for x in s.dishes)
            note = f"  (ocupado: {', '.join(s.busy)})" if s.busy else ""
            print(f"batch {s.date} {kind}: {dishes}{note}")
        for d in plan.days:
            print(
                f"{d.date} {d.date.strftime('%a')}  comida={d.lunch}  cena={d.dinner}"
                f"{'  +' + d.side if d.side else ''}"
            )
        if a.write:
            path = plans_dir(data) / f"{start.isoformat()}.yaml"
            plan.save(path)
            print(f"saved {path}")
        return 0

    if a.cmd == "shopping":
        from .i18n import Translator
        from .planner import plan_for
        from .portions import shopping_list

        day = dt.date.fromisoformat(a.week)
        monday = day - dt.timedelta(days=day.weekday())
        plan = plan_for(data, monday)
        if not plan:
            print("no plan covers that week", file=sys.stderr)
            return 1
        t = Translator(data.locale)
        db = DB(data.state / "supermealplanner.db")
        shop = shopping_list(data, db, plan, monday, t, view=a.store)
        for sl in shop.stores:
            total = f"  ({t.money(sl.total)})" if sl.total else ""
            print(f"\n== {sl.name}{total}")
            for section, lines in sl.sections.items():
                print(f"  -- {section}")
                for line in lines:
                    what = line.pick.text(t) if line.pick else line.ingredient.name
                    print(f"     {what}  [{line.ingredient.name}: {line.amount}]")
        return 0

    if a.cmd == "serve":
        import uvicorn

        from .web import create_app

        uvicorn.run(create_app(Path(a.data)), host=a.host, port=a.port, proxy_headers=True)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
