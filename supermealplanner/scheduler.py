"""What runs on its own, once an hour.

1. Pull the data repo when it is a git checkout and SMP_GIT_PULL=1, then reload it.
   SMP_GIT_TOKEN, if set, authenticates an https pull (Gitea or GitHub token, read-only).
2. Keep a plan ready: when no plan covers the next 7 days, generate the next 4 weeks from the
   Monday after the newest plan (or this Monday), reading busy afternoons from the calendar.
3. Run the weekly target check for each person. It only changes a target when the rules allow it.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import subprocess

from .calendar import busy_afternoons
from .model import DataError
from .planner import generate, load_plan, plans_dir, validate
from .targets import evaluate, sync_start

log = logging.getLogger("supermealplanner")


def pull(state) -> None:
    root = state.root
    if os.environ.get("SMP_GIT_PULL") != "1" or not (root / ".git").exists():
        return
    cmd = ["git", "-C", str(root)]
    token = os.environ.get("SMP_GIT_TOKEN", "")
    if token:  # passed per call, never written to the checkout's config
        cmd += ["-c", f"http.extraHeader=Authorization: token {token}"]
    r = subprocess.run(
        [*cmd, "pull", "--ff-only", "--quiet"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if r.returncode != 0:
        log.warning("git pull failed: %s", r.stderr.strip()[:300])
    try:
        state.reload()
    except DataError as e:  # a broken push: keep serving the last good data
        log.warning("data repo has errors, keeping the previous version:\n%s", str(e)[:2000])


def ensure_plan(state, today: dt.date | None = None):
    data = state.data
    today = today or state.today()
    horizon = today + dt.timedelta(days=7)
    plans = [load_plan(p) for p in sorted(plans_dir(data).glob("*.yaml"))]
    covering = [p for p in plans if p.start <= today < p.end]
    shown = max(covering, key=lambda p: p.start, default=None)  # the one the app displays
    if shown is not None and validate(
        data, shown
    ):  # recipes or rules changed under the plan: redo the weeks from this Monday
        monday = today - dt.timedelta(days=today.weekday())
        prev = max((p for p in plans if p.end <= monday), key=lambda p: p.end, default=None)
        try:
            busy = busy_afternoons(data.household, monday, monday + dt.timedelta(days=28))
        except Exception:  # noqa: BLE001  plan without the calendar rather than not at all
            busy = {}
        plan = generate(data, monday, 4, busy=busy, previous=prev)
        plan.save(plans_dir(data) / f"{monday.isoformat()}.yaml")
        log.warning("the plan no longer matched the data; made a new one from %s", monday)
        return plan
    if any(p.start <= horizon < p.end for p in plans):
        return None
    covering_today = [p for p in plans if p.start <= today < p.end]
    if covering_today:
        prev = max(covering_today, key=lambda p: p.end)
        start = prev.end
    else:
        prev = max(plans, key=lambda p: p.end) if plans else None
        start = today - dt.timedelta(days=today.weekday())
    end = start + dt.timedelta(days=28)
    try:
        busy = busy_afternoons(data.household, start, end)
    except Exception as e:  # noqa: BLE001  a calendar outage must not stop the plan
        from .bot import safe

        log.warning("calendar unavailable, planning without it: %s", safe(e))
        busy = {}
    plan = generate(data, start, 4, busy=busy, previous=prev)
    path = plans_dir(data) / f"{start.isoformat()}.yaml"
    plan.save(path)
    log.info("new plan %s", path)
    return plan


def tick(state) -> None:
    pull(state)
    ensure_plan(state)
    today = state.today()
    for p in state.data.household.people:
        sync_start(state.db, p, today)
        st = evaluate(state.db, state.data.household, p, today, apply=True)
        if st.changed:
            log.info("%s: %s kcal (%s)", p.id, st.kcal, st.changed)
