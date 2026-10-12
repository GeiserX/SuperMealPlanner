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
from .changes import GIT_LOCK, git_env
from .model import DataError
from .planner import PlanError, generate, load_plan, plans_dir, repair
from .targets import evaluate, sync_start

log = logging.getLogger("supermealplanner")


def pull(state) -> None:
    root = state.root
    if os.environ.get("SMP_GIT_PULL") != "1" or not (root / ".git").exists():
        return
    env = git_env(os.environ.get("SMP_GIT_TOKEN", ""))
    with GIT_LOCK:  # never in the middle of a change the bot is saving
        r = subprocess.run(
            ["git", "-C", str(root), "pull", "--ff-only", "--quiet"],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
            env=env,
        )
    if r.returncode != 0:
        log.warning("git pull failed: %s", r.stderr.strip()[:300])
    try:
        state.reload()
    except DataError as e:  # a broken push: keep serving the last good data
        where = sorted(
            {
                line.split(":")[0]
                for line in str(e).splitlines()
                if line and not line.startswith(" ")
            }
        )
        log.warning(
            "data repo has errors in %s; keeping the previous version (run smp check)",
            ", ".join(where[:10]),
        )


def ensure_plan(state, today: dt.date | None = None):
    data = state.data
    today = today or state.today()
    horizon = today + dt.timedelta(days=7)
    paths = sorted(plans_dir(data).glob("*.yaml"))
    plans = [load_plan(p) for p in paths]
    # the data changed under the plans: repair the slots that must change, never redo a plan
    for path, plan in zip(paths, plans, strict=True):
        changed, cooked_bad = repair(data, plan, today)
        if changed:
            plan.save(path)
            log.warning(
                "plan %s: replaced %d slots that no longer fit the data", plan.start, changed
            )
        if cooked_bad:
            log.warning(
                "plan %s: %d lunches already cooked break a new household rule; check the fridge",
                plan.start,
                cooked_bad,
            )
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
    try:
        plan = generate(data, start, 4, busy=busy, previous=prev)
    except PlanError as e:  # the data cannot make a plan: say so, keep the rest of the tick running
        log.warning("cannot make the next plan: %s", str(e)[:500])
        return None
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
            log.info("%s: calorie target changed (%s)", p.id, st.changed[0])
