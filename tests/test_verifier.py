"""Prep a dish needs before its day (doctor.py), and the verifier that checks an answer changing the
menu, a recipe or the batch days before it goes out (verifier.py): on a copy of the data, then one
judgment call to the provider, then at most one revision in the same conversation."""

import asyncio
import datetime as dt
import json

import yaml

from supermealplanner import doctor, llm, verifier
from supermealplanner.model import load
from supermealplanner.planner import plan_for

from .conftest import MONDAY
from .test_doctor import D, edit_plan, keys
from .test_reminders import ZONE, setup
from .test_settings_stores_bot import msg, run
from .test_turns import cfg, sends

SALAD = {
    "id": "ensalada-garbanzos",
    "name": "Ensalada de garbanzos",
    "meals": ["dinner"],
    "kind": "assemble",
    "minutes": 10,
    "fridge_days": 1,
    "protein": "legume",
    "tags": ["salad", "legume"],
    "ingredients": [{"id": "garbanzo", "qty": 60}, {"id": "lechuga", "qty": 60}],
    "steps": ["Montar."],
    "nutrition": {"kcal": 500, "protein_g": 25},
}


def soak(data_dir, **recipe):
    """Chickpeas are a dried pulse to soak; the salad dinner uses them."""
    raw = yaml.safe_load((data_dir / "ingredients.yaml").read_text())
    raw["garbanzo"]["soak"] = True
    (data_dir / "ingredients.yaml").write_text(yaml.safe_dump(raw, allow_unicode=True))
    (data_dir / "recipes" / "ensalada-garbanzos.yaml").write_text(
        yaml.safe_dump({**SALAD, **recipe}, allow_unicode=True)
    )
    return load(data_dir)


def dinner(data, day, rid="ensalada-garbanzos"):
    edit_plan(data, lambda plan: setattr(plan.day(day), "dinner", rid))


def at(day, hour):
    return dt.datetime(day.year, day.month, day.day, hour, tzinfo=ZONE).timestamp()


# ---------------------------------------------------------------- the consistency check


def test_lunches_cooked_in_a_session_need_no_prep_reminder(data_dir, tmp_path):
    _b, db = setup(data_dir, tmp_path)
    data = soak(data_dir)  # the garbanzo lunches are cooked in sessions: the batch reminder soaks
    assert [p.kind for p in doctor.problems(data, db, MONDAY) if p.kind != "avoided"] == []


def test_a_dinner_with_a_dried_pulse_and_no_soak_reminder_is_flagged_until_one_comes_in_time(
    data_dir, tmp_path
):
    _b, db = setup(data_dir, tmp_path)
    data = soak(data_dir)
    dinner(data, D(2))  # Wednesday 14
    (p,) = [p for p in doctor.problems(data, db, MONDAY) if p.kind == "prep"]
    assert p.key == "prep:ensalada-garbanzos:2026-10-14"
    assert "soak Garbanzo the night before 2026-10-14" in p.text
    assert "`dish ensalada-garbanzos 2026-10-14`" in p.text
    # a reminder on the day itself is too late to soak
    db.add_reminder(-100, "ana", at(D(2), 10), "Remojo", 7, "ensalada-garbanzos", str(D(2)))
    assert "prep:ensalada-garbanzos:2026-10-14" in keys(data, db)
    db.add_reminder(-100, "ana", at(D(1), 20), "Remojo", 7, "ensalada-garbanzos", str(D(2)))
    assert "prep:ensalada-garbanzos:2026-10-14" not in keys(data, db)


def test_a_dish_counting_on_a_session_held_the_same_evening_is_flagged(data_dir, tmp_path):
    """The case that started this: dinner on a day whose session comes that evening, after it, and
    nobody told to soak, cook or roast ahead."""
    _b, db = setup(data_dir, tmp_path)
    data = soak(data_dir, ahead=[{"step": "Asar el pimiento", "session": True}])
    friday = D(4)  # Friday 16 has a session, in the evening
    assert any(s.date == friday for s in plan_for(data, friday).sessions)
    dinner(data, friday)
    found = {p.key: p for p in doctor.problems(data, db, MONDAY)}
    p = found["prep_late:ensalada-garbanzos:2026-10-16"]
    assert "Asar el pimiento" in p.text and "the session that day comes after the meal" in p.text
    assert "prep:ensalada-garbanzos:2026-10-16" not in found  # said once
    # someone told to roast it at home the day before: nothing left to fix
    db.add_reminder(-100, "ana", at(D(3), 20), "Asar", 7, "ensalada-garbanzos", str(friday))
    assert not [k for k in keys(data, db) if "ensalada-garbanzos" in k]


def test_a_session_step_after_a_session_still_needs_its_reminder(data_dir, tmp_path):
    _b, db = setup(data_dir, tmp_path)
    data = soak(data_dir, ahead=[{"step": "Asar el pimiento", "session": True}])
    dinner(data, D(1))  # Tuesday 13: Monday's session comes before
    (p,) = [p for p in doctor.problems(data, db, MONDAY) if "ensalada" in p.key]
    assert p.kind == "prep"
    assert "the night before 2026-10-12" in p.text  # the pulse soaks before the session cooks it
    assert "Asar el pimiento (in the session of 2026-10-12)" in p.text


def test_an_offered_reminder_counts_and_one_without_the_dish_link_does_not(data_dir, tmp_path):
    _b, db = setup(data_dir, tmp_path)
    data = soak(data_dir)
    dinner(data, D(2))
    plain = [{"when": "2026-10-13 20:00", "text": "Remojo", "person": None}]
    linked = [{**plain[0], "recipe": "ensalada-garbanzos", "date": "2026-10-14"}]
    assert "prep:ensalada-garbanzos:2026-10-14" in [
        p.key for p in doctor.problems(data, db, MONDAY, plain)
    ]
    assert "prep:ensalada-garbanzos:2026-10-14" not in [
        p.key for p in doctor.problems(data, db, MONDAY, linked)
    ]


def test_too_late_to_soak_says_to_move_the_dish(data_dir, tmp_path):
    _b, db = setup(data_dir, tmp_path)
    data = soak(data_dir)
    dinner(data, D(2))
    (p,) = [p for p in doctor.problems(data, db, D(2)) if p.kind == "prep"]
    assert "It is too late for that day" in p.text


# ---------------------------------------------------------------- the verifier

PLUGIN = """
import json, pathlib
HERE = pathlib.Path(__file__).parent

def _cfg():
    return json.loads((HERE / "reply.json").read_text())

def converse(turn, settings):
    cfg = _cfg()
    with (HERE / "turns.jsonl").open("a") as f:
        f.write(json.dumps({"new": [m["content"] for m in turn["new"]],
                            "conversation": turn["conversation"]}) + "\\n")
    n = len((HERE / "turns.jsonl").read_text().splitlines())
    text = cfg["text"] if n == 1 else cfg.get("revised", cfg["text"])
    return {"text": text, "conversation": "c1"}

def complete(system, messages, settings):
    with (HERE / "checks.jsonl").open("a") as f:
        f.write(json.dumps({"system": system, "brief": messages[0]["content"]}) + "\\n")
    return _cfg().get("verdict", "{}")
"""


def lines(data_dir, name):
    path = data_dir / "plugins" / name
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


def change(*ops):
    return "```change\nasked: true\n" + yaml.safe_dump({"plan": list(ops)}) + "```"


def bad_dinner():
    return change({"set": {"date": "2026-10-16", "meal": "dinner", "recipe": "ensalada-garbanzos"}})


def vsetup(data_dir, tmp_path, **recipe):
    b, db = setup(data_dir, tmp_path)
    (data_dir / "plugins" / "ai.py").write_text(PLUGIN)
    b.state.data = soak(data_dir, **recipe)
    return b, db


def test_the_28_10_case_is_caught_before_it_is_made_and_the_files_stay_as_they_were(
    data_dir, tmp_path
):
    b, db = vsetup(data_dir, tmp_path, ahead=[{"step": "Asar el pimiento", "session": True}])
    plan_file = min((data_dir / "state" / "plans").glob("*.yaml"))
    before = plan_file.read_text()
    problems, after = verifier.structural(
        b.data, db, MONDAY, yaml.safe_load(bad_dinner()[10:-3]), "ana", []
    )
    assert len(problems) == 1 and "the session that day comes after the meal" in problems[0]
    assert plan_file.read_text() == before  # only the copy changed
    assert any("ensalada-garbanzos" in x for x in after["menu"])
    assert after["recipes"][0].startswith("### ensalada-garbanzos")


def test_a_change_the_app_refuses_is_named_as_such(data_dir, tmp_path):
    b, db = vsetup(data_dir, tmp_path)
    bad = {"plan": [{"set": {"date": "2026-10-14", "meal": "dinner", "recipe": "fritura"}}]}
    problems, _ = verifier.structural(b.data, db, MONDAY, bad, "luis", [])
    assert problems and problems[0].startswith("The app refuses this menu change")


def test_the_answer_goes_back_once_with_the_problems_and_the_revision_replaces_it(
    data_dir, tmp_path
):
    b, _db = vsetup(data_dir, tmp_path, ahead=[{"step": "Asar el pimiento", "session": True}])
    fixed = change({"set": {"date": "2026-10-16", "meal": "dinner", "recipe": "merluza-cena-0"}})
    verdict = {"health": ["Ana cannot have chickpeas: pick another dinner on 2026-10-16."]}
    cfg(
        data_dir,
        text="Cena del viernes: ensalada.\n" + bad_dinner(),
        revised="Cena del viernes: merluza.\n" + fixed,
        verdict=json.dumps(verdict),
    )
    run(b.handle(None, msg("@chefbot cambia la cena del viernes", message_id=1)))
    turns = lines(data_dir, "turns.jsonl")
    assert len(turns) == 2  # the answer and one revision, nothing more
    note = turns[1]["new"][0]
    assert turns[1]["conversation"] == "c1"
    assert "the session that day comes after the meal" in note
    assert "health: Ana cannot have chickpeas" in note
    (check,) = lines(data_dir, "checks.jsonl")
    assert "1. wishes" in check["system"] and "3. health" in check["system"]
    assert "Cena del viernes: ensalada" in check["brief"]
    texts = [p["text"] for p in sends(b, -100)]
    assert any("merluza" in t for t in texts) and not any("ensalada." in t for t in texts)
    assert plan_for(b.data, D(4)).day(D(4)).dinner == "merluza-cena-0"


def test_a_clean_change_goes_out_as_it_came_after_one_check(data_dir, tmp_path):
    b, _db = vsetup(data_dir, tmp_path)
    ok = change({"set": {"date": "2026-10-14", "meal": "dinner", "recipe": "merluza-cena-1"}})
    cfg(data_dir, text="Hecho.\n" + ok, verdict='{"wishes": [], "health": []}')
    run(b.handle(None, msg("@chefbot merluza el miércoles", message_id=1)))
    assert len(lines(data_dir, "turns.jsonl")) == 1
    assert len(lines(data_dir, "checks.jsonl")) == 1


def test_a_reply_that_changes_no_menu_is_not_checked(data_dir, tmp_path):
    b, _db = vsetup(data_dir, tmp_path)
    cfg(data_dir, text="Hoy hay lentejas.")
    run(b.handle(None, msg("@chefbot qué comemos", message_id=1)))
    assert lines(data_dir, "checks.jsonl") == [] and len(lines(data_dir, "turns.jsonl")) == 1


def test_a_verdict_that_is_not_json_counts_as_no_problems():
    assert verifier.parse("I think it is fine.") == {}
    assert verifier.parse('ok {"timeline": ["x", 3, ""], "other": ["y"]}') == {"timeline": ["x"]}


def test_health_records_reach_the_check_only_for_whom_the_chat_may_see(data_dir, tmp_path):
    _b, _db = vsetup(data_dir, tmp_path)
    (data_dir / "state" / "health").mkdir(parents=True, exist_ok=True)
    export = {
        "people": {
            "ana": {"allergies": ["garbanzo"], "meds": [], "notes": "never shown"},
            "luis": {"diet": ["little salt"]},
        }
    }
    (data_dir / "state" / "health" / "export.yaml").write_text(yaml.safe_dump(export))
    raw = yaml.safe_load((data_dir / "settings.yaml").read_text())
    raw["health_export"] = {"url": "https://health.example/export.yaml"}
    (data_dir / "settings.yaml").write_text(yaml.safe_dump(raw))
    data = load(data_dir, health=False)
    assert verifier.health_records(data) == {
        "ana": {"allergies": ["garbanzo"], "meds": []},
        "luis": {"diet": ["little salt"]},
    }
    assert verifier.health_records(data, "luis") == {"luis": {"diet": ["little salt"]}}
    brief = verifier.brief(data, MONDAY, "x", [], {}, "luis")
    assert "little salt" in brief and "garbanzo" not in brief


def test_a_broken_provider_hands_the_first_answer_on(data_dir, tmp_path):
    b, db = vsetup(data_dir, tmp_path)
    reply = llm.Reply("ok\n" + bad_dinner(), "c1")

    async def boom(*a, **k):
        raise RuntimeError("down")

    verifier.llm.complete, real = boom, verifier.llm.complete
    try:
        out = run(verifier.review(b.data, db, MONDAY, {"new": [], "history": []}, reply))
    finally:
        verifier.llm.complete = real
    assert out is reply


def test_a_step_done_the_night_before_moves_the_soak_a_day_earlier(data_dir, tmp_path):
    _b, db = setup(data_dir, tmp_path)
    data = soak(data_dir, ahead=[{"step": "Cocer los garbanzos", "days": 1}])
    dinner(data, D(3))  # Thursday 15: cooked on Wednesday night, so soaked on Tuesday
    (p,) = [p for p in doctor.problems(data, db, MONDAY) if p.kind == "prep"]
    assert (
        "the night before 2026-10-14" in p.text and "Cocer los garbanzos (by 2026-10-14)" in p.text
    )
    db.add_reminder(-100, "ana", at(D(2), 21), "Remojo", 7, "ensalada-garbanzos", str(D(3)))
    assert "prep:ensalada-garbanzos:2026-10-15" in keys(data, db)  # Wednesday night is too late
    db.add_reminder(-100, "ana", at(D(1), 21), "Remojo", 7, "ensalada-garbanzos", str(D(3)))
    assert "prep:ensalada-garbanzos:2026-10-15" not in keys(data, db)


def test_the_check_knows_the_swaps_the_app_already_makes(data_dir, tmp_path):
    """A person's swap is on their plate already, though the recipe text keeps the original."""
    raw = yaml.safe_load((data_dir / "household.yaml").read_text())
    raw["people"][0]["swaps"] = {"tomate": "lechuga"}
    (data_dir / "household.yaml").write_text(yaml.safe_dump(raw, allow_unicode=True))
    brief = verifier.brief(load(data_dir), MONDAY, "x", [], {}, None)
    assert "Ana (ana) gets instead, done by the app: tomate -> lechuga" in brief


# ---------------------------------------------------------------- the plan as it stands


def judged(verdict):
    """A fake provider for the plan check: the verdict it gives, and each call it got."""
    calls = []

    async def complete(data, system, messages):
        calls.append({"system": system, "brief": messages[0]["content"]})
        return json.dumps(verdict)

    return calls, complete


def test_the_plan_check_reads_every_recipe_of_the_two_weeks_and_judges_all_of_it(
    data_dir, tmp_path
):
    b, db = vsetup(data_dir, tmp_path)
    text, key = verifier.plan_brief(b.data, db, MONDAY)
    plan, end = plan_for(b.data, MONDAY), MONDAY + dt.timedelta(days=13)
    dishes = {getattr(d, m) for d in plan.days if d.date <= end for m in ("lunch", "dinner")}
    for rid in dishes:
        assert f"### {rid}\n" in text  # the whole text, never only the name
    assert "Not shown" not in text
    assert key == verifier.plan_brief(b.data, db, MONDAY)[1]  # the clock line is not in the key
    system = verifier.VERIFY.format(scope=verifier.PLAN_SCOPE, only="")
    assert "Every real problem in it counts" in system and "never from its name" in system
    assert verifier.ONLY_PROPOSAL not in system


def test_recipes_too_long_to_send_are_named_rather_than_judged_by_name(data_dir, tmp_path):
    b, db = vsetup(data_dir, tmp_path)
    real, verifier.RECIPES_MAX = verifier.RECIPES_MAX, 300
    try:
        text, _ = verifier.plan_brief(b.data, db, MONDAY)
    finally:
        verifier.RECIPES_MAX = real
    assert "(Not shown, too long to send: " in text


def test_the_plan_is_judged_once_per_version_and_the_ai_sees_what_was_found(data_dir, tmp_path):
    b, db = vsetup(data_dir, tmp_path)
    calls, fake = judged({"health": ["Ana: too much salt on Tuesday"], "timeline": ["Thaw it"]})
    verifier.llm.complete, real = fake, verifier.llm.complete
    try:
        run(verifier.check_plan(b.data, db, MONDAY))
        run(verifier.check_plan(b.data, db, MONDAY))
        assert len(calls) == 1  # same plan: not judged again
        assert verifier.SKIP and "Every real problem" in calls[0]["system"]
        group = verifier.plan_findings(b.data, db, MONDAY)
        assert group == ["timeline: Thaw it", "health: Ana: too much salt on Tuesday"] or set(
            group
        ) == {"timeline: Thaw it", "health: Ana: too much salt on Tuesday"}
        # a private chat sees no one's health
        assert verifier.plan_findings(b.data, db, MONDAY, "luis") == ["timeline: Thaw it"]
        from supermealplanner import context

        assert "- timeline: Thaw it" in context.facts(b.data, db, MONDAY)
        dinner(
            b.data, D(3), "merluza-cena-1"
        )  # the plan changed: the old findings are not about it
        assert verifier.plan_findings(b.data, db, MONDAY) == []
        run(verifier.check_plan(b.data, db, MONDAY))
        assert len(calls) == 2
    finally:
        verifier.llm.complete = real


def test_a_failed_plan_check_keeps_nothing_and_runs_again(data_dir, tmp_path):
    b, db = vsetup(data_dir, tmp_path)

    async def boom(*a, **k):
        raise RuntimeError("down")

    verifier.llm.complete, real = boom, verifier.llm.complete
    try:
        assert run(verifier.check_plan(b.data, db, MONDAY)) is None
    finally:
        verifier.llm.complete = real
    assert db.get_kv(verifier.PLAN_KEY) is None


def test_the_morning_raises_what_the_plan_check_found_once_a_day(data_dir, tmp_path):
    b, db = vsetup(data_dir, tmp_path)
    now = dt.datetime.now(ZONE)
    b.data.settings.bot.checkup_at = f"{now:%H:%M}"
    taken, driven = [], []
    b.take = lambda chat, user, who, msg, text, asked, **k: taken.append((chat, text))

    async def drive(client, chat):
        driven.append(chat)

    b.drive = drive

    async def raise_plan():
        await b.raise_plan(None)
        await asyncio.gather(*b.tasks)

    _calls, fake = judged({"nonsense": ["The oven is used twice at once"]})
    verifier.llm.complete, real = fake, verifier.llm.complete
    try:
        run(raise_plan())
        assert taken == []  # nothing checked yet
        run(verifier.check_plan(b.data, db, MONDAY))
        run(raise_plan())
        assert taken == []  # the checkup's own line goes first
        db.mark_sent("checkup", now.date(), b.data.settings.bot.checkup_at)
        run(raise_plan())  # a check that ended after the checkup still gets its turn
        run(raise_plan())
        assert taken == [(-100, b.t("plan_check_turn"))] and driven == [-100]
        dinner(b.data, D(3), "merluza-cena-1")  # a new plan the same day waits for tomorrow
        run(verifier.check_plan(b.data, db, MONDAY))
        run(raise_plan())
        assert len(taken) == 1
    finally:
        verifier.llm.complete = real


def test_a_private_checkup_chat_gets_the_plan_turn_as_its_person_or_not_at_all(data_dir, tmp_path):
    b, db = vsetup(data_dir, tmp_path)
    now = dt.datetime.now(ZONE)
    cfg = b.data.settings.bot
    cfg.checkup_at = f"{now:%H:%M}"
    db.mark_sent("checkup", now.date(), cfg.checkup_at)
    taken = []
    b.take = lambda chat, user, who, msg, text, asked, **k: taken.append((chat, who))

    async def drive(client, chat):
        pass

    b.drive = drive
    _calls, fake = judged({"nonsense": ["The oven is used twice at once"]})
    verifier.llm.complete, real = fake, verifier.llm.complete
    try:
        run(verifier.check_plan(b.data, db, MONDAY))
        cfg.checkup_chat = 99  # a private chat of no one in the household
        run(b.raise_plan(None))
        assert taken == [] and db.get_kv("verifier.plan_raised") is None
        cfg.checkup_chat = cfg.people["ana"]
        run(b.raise_plan(None))
        assert taken == [(cfg.people["ana"], "ana")]
    finally:
        verifier.llm.complete = real
    system = verifier.VERIFY.format(scope=verifier.PLAN_SCOPE, only="")
    assert "never name there anyone's notes" in system


def test_a_verdict_that_is_not_json_is_not_kept_as_a_check(data_dir, tmp_path):
    b, db = vsetup(data_dir, tmp_path)

    async def garbled(*a, **k):
        return "I could not check it"

    verifier.llm.complete, real = garbled, verifier.llm.complete
    try:
        assert run(verifier.check_plan(b.data, db, MONDAY)) is None
        assert db.get_kv(verifier.PLAN_KEY) is None
        _calls, fake = judged({})
        verifier.llm.complete = fake
        assert run(verifier.check_plan(b.data, db, MONDAY))["found"] == {}  # all clear is kept
    finally:
        verifier.llm.complete = real


def test_a_food_someone_avoids_stays_out_of_private_chats_and_the_plan_check(data_dir, tmp_path):
    b, db = vsetup(data_dir, tmp_path)
    from supermealplanner import context

    assert "uses espinaca" in context.facts(b.data, db, MONDAY)  # the group
    assert "uses espinaca" not in context.facts(b.data, db, MONDAY, "ana")
    assert "uses espinaca" not in verifier.plan_brief(b.data, db, MONDAY)[0]


def test_the_plan_check_runs_in_the_background_at_most_every_ten_minutes(data_dir, tmp_path):
    b, _db = vsetup(data_dir, tmp_path)
    started, now = [], [1000.0]
    b.clock = lambda: now[0]

    async def check():
        started.append(now[0])

    b.check_plan = check

    async def tick():
        b.tick_plan()
        await asyncio.gather(*b.tasks)

    async def ticks():
        await tick()
        await tick()
        now[0] += 601
        b.running[1] = object()  # a turn runs: wait for it
        await tick()
        assert started == [1000.0]
        b.running.clear()
        await tick()

    run(ticks())
    assert started == [1000.0, 1601.0]
