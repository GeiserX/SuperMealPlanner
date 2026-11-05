"""Prep a dish needs before its day (doctor.py), and the reviewer that checks every answer changing
anything once it is sent (verifier.py): the structural check, then a review job through the
provider over the whole rest of the week, and what it finds fixed in one follow-up of the bot's own,
never reviewed again."""

import asyncio
import datetime as dt
import json

import yaml

from supermealplanner import doctor, verifier
from supermealplanner.model import WEEKDAYS, load
from supermealplanner.planner import plan_for

from .conftest import MONDAY
from .test_doctor import D, edit_plan, household, keys
from .test_reminders import ZONE, setup
from .test_settings_stores_bot import msg, run
from .test_turns import buttons, cfg, drain, sends, tap, toasts

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


def test_a_dinner_with_a_dried_pulse_needs_a_cook_and_a_soak_in_time(data_dir, tmp_path):
    _b, db = setup(data_dir, tmp_path)
    data = soak(data_dir)
    dinner(data, D(2))  # Wednesday 14, an assembled salad: the chickpeas are cooked ahead
    assert "prep:ensalada-garbanzos:2026-10-14" not in keys(data, db)  # the chains say it
    p = {p.kind: p for p in doctor.problems(data, db, MONDAY)}["no_cook"]
    assert p.key == "no_cook:ensalada-garbanzos:2026-10-14"
    assert "`dish ensalada-garbanzos 2026-10-14 cook`" in p.text
    cook = ("ensalada-garbanzos", str(D(2)), "cook")
    db.add_reminder(-100, "ana", at(D(2), 10), "Cocer", 7, *cook)
    assert "no_soak:ensalada-garbanzos:2026-10-14" in keys(data, db)
    # a soak after the cook does not count; one the evening before does
    db.add_reminder(-100, "ana", at(D(2), 11), "Remojo", 7, *cook[:2], "soak")
    assert "no_soak:ensalada-garbanzos:2026-10-14" in keys(data, db)
    db.add_reminder(-100, "ana", at(D(1), 20), "Remojo", 7, *cook[:2], "soak")
    assert not [k for k in keys(data, db) if "ensalada-garbanzos" in k]


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
    assert "no_cook:ensalada-garbanzos:2026-10-16" in found  # and nothing cooks the chickpeas
    # someone told to roast and cook it at home the day before, the chickpeas soaked the night
    # before that: nothing left to fix
    db.add_reminder(-100, "ana", at(D(3), 20), "Asar", 7, "ensalada-garbanzos", str(friday), "cook")
    db.add_reminder(
        -100, "ana", at(D(2), 21), "Remojo", 7, "ensalada-garbanzos", str(friday), "soak"
    )
    assert not [k for k in keys(data, db) if "ensalada-garbanzos" in k]


def test_a_session_step_after_a_session_still_needs_its_reminder(data_dir, tmp_path):
    _b, db = setup(data_dir, tmp_path)
    data = soak(data_dir, ahead=[{"step": "Asar el pimiento", "session": True}])
    dinner(data, D(1))  # Tuesday 13: Monday's session comes before
    (p,) = [p for p in doctor.problems(data, db, MONDAY) if p.kind == "prep"]
    assert "Asar el pimiento (in the session of 2026-10-12)" in p.text
    assert "no_cook:ensalada-garbanzos:2026-10-13" in keys(data, db)  # a dinner is not in it


def test_an_offered_reminder_counts_and_one_without_the_dish_link_does_not(data_dir, tmp_path):
    _b, db = setup(data_dir, tmp_path)
    data = soak(data_dir, kind="quick")  # cooked for the dinner: only the soak is missing
    dinner(data, D(2))
    plain = [{"when": "2026-10-13 20:00", "text": "Remojo", "person": None}]
    linked = [{**plain[0], "recipe": "ensalada-garbanzos", "date": "2026-10-14", "kind": "soak"}]
    assert "no_soak:ensalada-garbanzos:2026-10-14" in [
        p.key for p in doctor.problems(data, db, MONDAY, plain)
    ]
    assert "no_soak:ensalada-garbanzos:2026-10-14" not in [
        p.key for p in doctor.problems(data, db, MONDAY, linked)
    ]


def test_too_late_for_a_step_ahead_says_to_move_the_dish(data_dir, tmp_path):
    _b, db = setup(data_dir, tmp_path)
    data = soak(data_dir, ahead=[{"step": "Cocer los garbanzos", "days": 1}])
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
    n = len((HERE / "checks.jsonl").read_text().splitlines())
    verdicts = _cfg().get("verdicts")
    if verdicts:  # one per review, the last one again after them
        return verdicts[min(n, len(verdicts)) - 1]
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
    assert len(problems) == 2 and "the session that day comes after the meal" in problems[0]
    assert "nothing cooks it before the meal" in problems[1]
    assert plan_file.read_text() == before  # only the copy changed
    assert any("ensalada-garbanzos" in x for x in after["menu"])
    assert any(x.startswith("### ensalada-garbanzos") for x in after["recipes"])


def test_a_change_the_app_refuses_is_named_as_such(data_dir, tmp_path):
    b, db = vsetup(data_dir, tmp_path)
    bad = {"plan": [{"set": {"date": "2026-10-14", "meal": "dinner", "recipe": "fritura"}}]}
    problems, _ = verifier.structural(b.data, db, MONDAY, bad, "luis", [])
    assert problems and problems[0].startswith("The app refuses this menu change")


def say(b, text, **m):
    """A message to the bot, and everything it starts (the review, the follow-up) run to the end."""

    async def go():
        await b.handle(None, msg(text, **m))
        await drain(b)

    run(go())


def test_an_answer_goes_out_at_once_and_what_the_review_finds_comes_in_one_follow_up(
    data_dir, tmp_path
):
    b, _db = vsetup(data_dir, tmp_path, ahead=[{"step": "Asar el pimiento", "session": True}])
    fixed = (
        "```change\n"
        + yaml.safe_dump(
            {
                "plan": [
                    {"set": {"date": "2026-10-16", "meal": "dinner", "recipe": "merluza-cena-0"}}
                ]
            }
        )
        + "```"
    )
    verdict = {"health": ["Ana cannot have chickpeas: pick another dinner on 2026-10-16."]}
    cfg(
        data_dir,
        text="Cena del viernes: ensalada.\n" + bad_dinner(),
        revised="Corrección: merluza el viernes.\n" + fixed,
        verdicts=[json.dumps(verdict)],
    )
    say(b, "@chefbot cambia la cena del viernes", message_id=1)
    _first, follow = lines(data_dir, "turns.jsonl")  # the answer, then the bot's own follow-up
    note = follow["new"][-1]
    assert note.startswith(f"{b.t('verify_who')}: [Check of your last answer")
    # the change was already made: what it broke is still this answer's doing, not "standing"
    assert "the session that day comes after the meal" in note
    assert "health: Ana cannot have chickpeas" in note
    (check,) = lines(data_dir, "checks.jsonl")  # the follow-up is not reviewed again
    assert "Cena del viernes: ensalada" in check["brief"] and "already sent" in check["system"]
    texts = [p["text"] for p in sends(b, -100)]
    assert texts[0].startswith("Cena del viernes: ensalada.")  # the answer went out as written
    assert any("Corrección: merluza" in t for t in texts)
    assert not any(b.t("review_failed") in t for t in texts)
    # the follow-up's change waits for the yes of the person who asked (Luis, user 7)
    assert plan_for(b.data, D(4)).day(D(4)).dinner == "ensalada-garbanzos"
    asked = next(p for p in sends(b, -100) if p.get("reply_markup"))
    yes = buttons(asked)[0]["callback_data"]
    run(tap(b, yes, 8, 1))
    assert b.t("tap_not_yours", name=b.mention("luis")) in toasts(b)
    run(tap(b, yes, 7, 1))
    assert plan_for(b.data, D(4)).day(D(4)).dinner == "merluza-cena-0"


def test_the_answer_is_in_the_chat_before_the_review_starts(data_dir, tmp_path, monkeypatch):
    b, _db = vsetup(data_dir, tmp_path)
    seen = []

    async def complete(data, system, messages):
        seen.append([p["text"] for p in sends(b, -100)])
        return "{}"

    monkeypatch.setattr(verifier.llm, "complete", complete)
    cfg(data_dir, text="Hecho.\n" + merluza_wednesday())
    say(b, "@chefbot merluza el miércoles", message_id=1)
    (said,) = seen
    assert said and said[0].startswith("Hecho.")


def test_a_clean_change_goes_out_as_it_came_after_one_check(data_dir, tmp_path):
    b, _db = vsetup(data_dir, tmp_path)
    ok = change({"set": {"date": "2026-10-14", "meal": "dinner", "recipe": "merluza-cena-1"}})
    cfg(data_dir, text="Hecho.\n" + ok, verdict='{"wishes": [], "health": []}')
    say(b, "@chefbot merluza el miércoles", message_id=1)
    assert len(lines(data_dir, "turns.jsonl")) == 1  # no follow-up
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


def test_a_broken_provider_says_nothing_was_checked_and_the_plan_check_goes_again(
    data_dir, tmp_path, monkeypatch
):
    b, db = vsetup(data_dir, tmp_path)

    async def boom(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr(verifier.llm, "complete", boom)
    answer = {"text": "ok\n" + bad_dinner(), "change": None, "made": True, "offered": []}
    assert run(verifier.after(b.data, db, MONDAY, {}, [answer])) is None
    cfg(data_dir, text="Hecho.\n" + merluza_wednesday())
    say(b, "@chefbot merluza el miércoles", message_id=1)
    assert len(lines(data_dir, "turns.jsonl")) == 1  # no follow-up claims anything
    assert db.get_kv(verifier.PLAN_RETRY) is True


def test_a_pulse_cooked_the_night_before_is_soaked_the_night_before_that(data_dir, tmp_path):
    _b, db = setup(data_dir, tmp_path)
    data = soak(data_dir, ahead=[{"step": "Cocer los garbanzos", "days": 1}])
    dinner(data, D(3))  # Thursday 15: cooked on Wednesday night, so soaked on Tuesday
    (p,) = [p for p in doctor.problems(data, db, MONDAY) if p.kind == "prep"]
    assert "Cocer los garbanzos (by 2026-10-14)" in p.text
    dish = ("ensalada-garbanzos", str(D(3)))
    db.add_reminder(-100, "ana", at(D(2), 18), "Cocer", 7, *dish, "cook")
    assert keys(data, db) == ["no_soak:ensalada-garbanzos:2026-10-15"]
    db.add_reminder(-100, "ana", at(D(1), 8), "Remojo", 7, *dish, "soak")  # 34 h before
    assert keys(data, db) == ["soak_long:ensalada-garbanzos:2026-10-15"]
    db.add_reminder(-100, "ana", at(D(1), 21), "Remojo", 7, *dish, "soak")
    assert keys(data, db) == []


def test_the_check_knows_the_swaps_the_app_already_makes(data_dir, tmp_path):
    """A person's swap is on their plate already, though the recipe text keeps the original; never
    in a batch recipe, which is one pot for everyone."""
    raw = yaml.safe_load((data_dir / "household.yaml").read_text())
    raw["people"][0]["swaps"] = {"tomate": "lechuga"}
    (data_dir / "household.yaml").write_text(yaml.safe_dump(raw, allow_unicode=True))
    brief = verifier.brief(load(data_dir), MONDAY, "x", [], {}, None)
    assert (
        "Ana (ana) gets instead, done by the app in the dishes plated per person, never in a "
        in brief
    )
    assert "batch recipe (one pot for everyone, cooked as written): tomate -> lechuga" in brief


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
    system = verifier.PLAN_SYSTEM
    assert "Every real problem in it counts" in system and "never from its name" in system
    assert verifier.WHOLE_WEEK not in system


def test_the_plan_check_sees_when_each_session_cooks_and_the_reminders_already_set(
    data_dir, tmp_path
):
    b, db = vsetup(data_dir, tmp_path)
    text, key = verifier.plan_brief(b.data, db, MONDAY)
    plan = plan_for(b.data, MONDAY)
    s = next(s for s in plan.sessions if not s.morning and s.date > MONDAY)
    x = s.dishes[0]
    eat = ", ".join(e.isoformat() for e in x.eat)
    assert f"{s.date.isoformat()}, after lunch: " in text and f"{x.recipe} for {eat}" in text
    assert "## Reminders set in the household chat" not in text
    at = dt.datetime.combine(s.date, dt.time(21, 30), ZONE).timestamp()
    rid = db.add_reminder(
        b.data.settings.bot.chat_id, "ana", at, "Remojo", recipe=x.recipe, date=eat[:10]
    )
    text, again = verifier.plan_brief(b.data, db, MONDAY)
    row = {
        "key": f"r:{rid}",
        "when": f"{WEEKDAYS[s.date.weekday()]} {s.date.isoformat()} 21:30",
        "kind": "note",
        "state": "unknown",
        "recipe": x.recipe,
        "for": eat[:10],
        "reminder": rid,
        "text": "Remojo",
    }
    assert json.dumps(row, ensure_ascii=False) in text  # a task, with what was said of it
    assert again != key  # a reminder set since is a new plan to check


def test_a_reminder_already_sent_still_covers_its_step(data_dir, tmp_path):
    b, db = vsetup(data_dir, tmp_path)
    chat = b.data.settings.bot.chat_id
    at = dt.datetime.combine(MONDAY, dt.time(9, 0), ZONE).timestamp()
    before = dt.datetime.combine(MONDAY - dt.timedelta(days=1), dt.time(9, 0), ZONE).timestamp()
    db.settle_reminder(db.add_reminder(chat, "ana", at, "Remojo hecho"), "sent")
    db.settle_reminder(db.add_reminder(chat, "ana", before, "Semana pasada"), "sent")
    db.add_reminder(chat + 1, "ana", at, "Otro chat")
    text, _ = verifier.plan_brief(b.data, db, MONDAY)
    assert '"state": "unknown", "reminder": 1, "text": "Remojo hecho"' in text
    assert "Semana pasada" not in text and "Otro chat" not in text
    db.set_kv("bot.migrated_chat", {"from": chat, "to": chat + 1})  # the group moved
    assert "Otro chat" in verifier.plan_brief(b.data, db, MONDAY)[0]


def test_the_plan_check_knows_the_cooking_reminder_says_what_to_take_out_of_the_freezer(
    data_dir, tmp_path
):
    b, db = vsetup(data_dir, tmp_path)
    plan = plan_for(b.data, MONDAY)
    iid = b.data.recipes[plan.day(MONDAY + dt.timedelta(days=1)).lunch].ingredients[0].id
    name = b.data.ingredients[iid].name
    ings = yaml.safe_load((data_dir / "ingredients.yaml").read_text())
    ings[iid]["frozen"] = True
    (data_dir / "ingredients.yaml").write_text(yaml.safe_dump(ings, allow_unicode=True))
    b.state.data = load(data_dir)
    assert (
        "take out of the freezer" not in verifier.plan_brief(b.data, db, MONDAY)[0]
    )  # no reminder
    raw = yaml.safe_load((data_dir / "settings.yaml").read_text())
    raw["reminders"].append({"id": "batch", "kind": "batch", "at": ["21:30"]})
    (data_dir / "settings.yaml").write_text(yaml.safe_dump(raw, allow_unicode=True))
    b.state.data = load(data_dir)
    text, _ = verifier.plan_brief(b.data, db, MONDAY)
    assert "## Said by the app itself" in text
    assert "at 21:30, the cooking reminder says what to take out of the freezer" in text
    assert f"next day's meals: {name}." in text


def test_the_freezer_line_counts_a_cooking_reminder_set_for_some_weekdays_only(data_dir, tmp_path):
    b, db = vsetup(data_dir, tmp_path)
    plan = plan_for(b.data, MONDAY)
    iid = b.data.recipes[plan.day(MONDAY + dt.timedelta(days=1)).lunch].ingredients[0].id
    ings = yaml.safe_load((data_dir / "ingredients.yaml").read_text())
    ings[iid]["frozen"] = True
    (data_dir / "ingredients.yaml").write_text(yaml.safe_dump(ings, allow_unicode=True))
    raw = yaml.safe_load((data_dir / "settings.yaml").read_text())
    rest = ["monday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    raw["reminders"] += [
        {"id": "batch", "kind": "batch", "at": ["21:30"], "days": rest},
        {"id": "batch-tue", "kind": "batch", "at": ["22:45"], "days": ["tuesday"]},
    ]
    (data_dir / "settings.yaml").write_text(yaml.safe_dump(raw, allow_unicode=True))
    b.state.data = load(data_dir)
    text, _ = verifier.plan_brief(b.data, db, MONDAY)
    assert "at 21:30 (Tuesday 22:45), the cooking reminder says what to take out" in text


def test_a_long_finding_is_cut_at_a_sentence_end_never_mid_sentence():
    first = "Mueve el remojo al viernes a las 21:30. " * 15
    long = first + "Y esta frase sigue y sigue sin acabar nunca " * 10
    out = verifier.parse(json.dumps({"timeline": [long, "Corto."]}))["timeline"]
    assert len(out[0]) <= verifier.PROBLEM_CHARS and out[0].endswith("21:30.")
    assert out[1] == "Corto."
    words = verifier.clip("palabra " * 200)
    assert len(words) <= verifier.PROBLEM_CHARS + 1 and words.endswith("palabra…")


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


def certain_setup(data_dir, tmp_path):
    """A plan with a problem the consistency check is sure of (sessions off the batch days), the
    morning checkup done, and the turns and the drive caught."""
    b, _db = vsetup(data_dir, tmp_path)
    household(data_dir, preferred_days=["wednesday", "saturday"])
    b.state.reload()
    db = b.state.db
    assert [p for p in doctor.problems(b.data, db, MONDAY) if p.kind == "off_day"]
    now = dt.datetime.now(ZONE)
    b.data.settings.bot.checkup_at = f"{now:%H:%M}"
    db.mark_sent("checkup", now.date(), b.data.settings.bot.checkup_at)
    taken = []
    b.take = lambda chat, user, who, msg, text, asked, **k: taken.append((chat, text))

    async def drive(client, chat):
        pass

    b.drive = drive
    return b, db, taken


def test_a_certain_problem_gets_the_plan_turn_when_the_judgment_found_nothing(data_dir, tmp_path):
    b, db, taken = certain_setup(data_dir, tmp_path)
    _calls, fake = judged({})
    verifier.llm.complete, real = fake, verifier.llm.complete
    try:
        run(verifier.check_plan(b.data, db, MONDAY))
        assert verifier.plan_findings(b.data, db, MONDAY) == []
        run(b.raise_plan(None))
        run(b.raise_plan(None))
    finally:
        verifier.llm.complete = real
    assert taken == [(-100, b.t("plan_check_turn"))] and sends(b, -100) == []


def test_a_plan_check_that_never_ends_in_the_window_gives_the_turn_and_never_a_list(
    data_dir, tmp_path, monkeypatch
):
    """With an AI the app never writes the problems itself (a wall of text nobody reads): when the
    check does not end in the window, the model still gets the turn, with the sure problems."""
    from supermealplanner import bot as bot_module

    b, db, taken = certain_setup(data_dir, tmp_path)

    async def boom(*a, **k):
        raise RuntimeError("down")

    verifier.llm.complete, real = boom, verifier.llm.complete
    try:
        run(verifier.check_plan(b.data, db, MONDAY))
        run(b.raise_plan(None))
        assert taken == [] and sends(b, -100) == []  # the check may still end in the window
        monkeypatch.setattr(bot_module, "PLAN_RAISE_H", 0)  # the window is over
        run(b.raise_plan(None))
        run(b.raise_plan(None))
    finally:
        verifier.llm.complete = real
    assert sends(b, -100) == []
    assert taken == [(-100, b.t("plan_check_turn"))]


def test_a_step_a_cook_could_misread_is_kept_and_every_chat_sees_it(data_dir, tmp_path):
    b, db = vsetup(data_dir, tmp_path)
    finding = "ensalada-garbanzos step 3: says 15 g per serving but the cook sees the total"
    calls, fake = judged({"clarity": [finding]})
    verifier.llm.complete, real = fake, verifier.llm.complete
    try:
        run(verifier.check_plan(b.data, db, MONDAY))
        assert "5. clarity:" in calls[0]["system"] and '"clarity": []' in calls[0]["system"]
        assert verifier.plan_findings(b.data, db, MONDAY) == [f"clarity: {finding}"]
        assert verifier.plan_findings(b.data, db, MONDAY, "luis") == [f"clarity: {finding}"]
    finally:
        verifier.llm.complete = real


def test_a_plan_already_judged_is_judged_again_when_the_checks_change(
    data_dir, tmp_path, monkeypatch
):
    b, db = vsetup(data_dir, tmp_path)
    calls, fake = judged({})
    monkeypatch.setattr(verifier.llm, "complete", fake)
    run(verifier.check_plan(b.data, db, MONDAY))
    run(verifier.check_plan(b.data, db, MONDAY))
    assert len(calls) == 1
    monkeypatch.setattr(verifier, "PLAN_SYSTEM", verifier.PLAN_SYSTEM + "\nA new check.")
    run(verifier.check_plan(b.data, db, MONDAY))
    assert len(calls) == 2
    assert calls[1]["system"].endswith("A new check.")


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


# ---------------------------------------------------------------- the reviewer on every change


def merluza_wednesday():
    return change({"set": {"date": "2026-10-14", "meal": "dinner", "recipe": "merluza-cena-1"}})


def test_an_answer_that_only_offers_reminders_is_reviewed(data_dir, tmp_path):
    from .test_reminders import ahead, block

    b, _db = vsetup(data_dir, tmp_path)
    when, _ = ahead()
    cfg(data_dir, text="Te aviso.\n" + block((when, "Sacar la merluza")))
    say(b, "@chefbot avísame", message_id=1)
    (check,) = lines(data_dir, "checks.jsonl")
    assert "Sacar la merluza" in check["brief"]


def test_a_follow_up_is_never_reviewed_again_so_one_answer_gets_one_at_most(data_dir, tmp_path):
    b, _db = vsetup(data_dir, tmp_path)
    bad = json.dumps({"timeline": ["Thaw the hake the night before, on 2026-10-13."]})
    cfg(
        data_dir,
        text="Hecho.\n" + merluza_wednesday(),
        revised="Falta: sacar la merluza el martes.\n" + merluza_wednesday(),
        verdict=bad,  # every review finds it
    )
    say(b, "@chefbot merluza el miércoles", message_id=1)
    assert len(lines(data_dir, "turns.jsonl")) == 2  # the answer and its one follow-up
    (check,) = lines(data_dir, "checks.jsonl")
    assert "Check the whole rest of this week" in check["system"]
    say(b, "@chefbot y el jueves", message_id=2)
    assert len(lines(data_dir, "turns.jsonl")) == 4  # the same again, never a loop
    assert len(lines(data_dir, "checks.jsonl")) == 2


def test_a_review_that_times_out_claims_nothing_and_the_plan_check_goes_at_the_next_tick(
    data_dir, tmp_path, monkeypatch
):
    b, db = vsetup(data_dir, tmp_path)

    async def slow(*a, **k):
        await asyncio.sleep(5)
        return "{}"

    monkeypatch.setattr(verifier.llm, "complete", slow)
    monkeypatch.setattr(verifier, "review_timeout", lambda data: 0.05, raising=False)
    cfg(data_dir, text="Hecho.\n" + merluza_wednesday())
    say(b, "@chefbot merluza el miércoles", message_id=1)
    texts = [p["text"] for p in sends(b, -100)]
    assert len(texts) == 1 and texts[0].startswith("Hecho.")
    assert b.t("review_failed") not in texts[0]  # sent before the review: it claims nothing
    assert len(lines(data_dir, "turns.jsonl")) == 1
    assert db.get_kv(verifier.PLAN_RETRY) is True
    started = []

    async def check():
        started.append(1)

    b.check_plan = check
    b.planned_at = b.clock()  # checked a moment ago: the flag skips the wait

    async def tick():
        b.tick_plan()
        await asyncio.gather(*b.tasks)
        b.tick_plan()  # once: the next tick waits again
        await asyncio.gather(*b.tasks)

    run(tick())
    assert started == [1] and db.get_kv(verifier.PLAN_RETRY) is None


def test_the_review_reads_the_whole_week_not_only_what_the_change_touches(data_dir, tmp_path):
    b, db = vsetup(data_dir, tmp_path)
    chat = b.data.settings.bot.chat_id
    rid = db.add_reminder(chat, "ana", at(D(1), 20), "Sacar la merluza")
    db.mark_task(f"r:{rid}", "done", "ana")
    cfg(data_dir, text="Hecho.\n" + merluza_wednesday())
    say(b, "@chefbot merluza el miércoles", message_id=1)
    (check,) = lines(data_dir, "checks.jsonl")
    brief = check["brief"]
    untouched = plan_for(b.data, MONDAY).day(MONDAY).lunch
    assert untouched != "merluza-cena-1" and f"### {untouched}\n" in brief
    assert '"state": "done"' in brief and "Sacar la merluza" in brief
    assert "## Pantry" in brief
    assert "## Cooking sessions" in brief and f"{untouched} for " in brief
    assert "Check the whole rest of this week and the next" in check["system"]


def test_the_chat_shows_the_bot_typing_until_the_answer_goes(data_dir, tmp_path, monkeypatch):
    from supermealplanner import bot as botmod

    b, _db = vsetup(data_dir, tmp_path)
    monkeypatch.setattr(botmod, "TYPING_S", 0.02, raising=False)

    async def slow(*a, **k):
        await asyncio.sleep(0.3)
        return "{}"

    monkeypatch.setattr(verifier.llm, "complete", slow)
    cfg(data_dir, text="Hecho.\n" + merluza_wednesday())
    say(b, "@chefbot merluza el miércoles", message_id=1)
    methods = [m for m, _ in b.calls]
    answer = next(i for i, (m, p) in enumerate(b.calls) if m == "sendMessage")
    assert "sendChatAction" in methods[:answer]
    assert "sendChatAction" not in methods[answer:]  # not while the review runs


def test_a_new_plan_is_told_once_checked_and_what_was_found_is_raised_at_once(
    data_dir, tmp_path, monkeypatch
):
    from supermealplanner import bot as botmod
    from supermealplanner.scheduler import ANNOUNCE

    b, db = vsetup(data_dir, tmp_path)
    taken = []
    b.take = lambda chat, user, who, msg, text, asked, **k: taken.append((chat, text, k))

    async def drive(client, chat):
        pass

    b.drive = drive
    start = plan_for(b.data, MONDAY).start.isoformat()
    db.set_kv(ANNOUNCE, start)
    run(b.tick_announce(None))
    assert sends(b, -100) == [] and db.get_kv(ANNOUNCE) == start  # waits for the check
    assert db.get_kv(verifier.PLAN_RETRY) is True  # which goes at this tick
    _calls, fake = judged({"timeline": ["Nothing cooks the chickpeas for Wednesday."]})
    monkeypatch.setattr(verifier.llm, "complete", fake)
    run(verifier.check_plan(b.data, db, MONDAY))

    async def announce():
        await b.tick_announce(None)
        await asyncio.gather(*b.tasks)

    run(announce())
    (said,) = sends(b, -100)
    assert b.t("review_failed") not in said["text"] and db.get_kv(ANNOUNCE) is None
    # raised now, not the next morning (no checkup_at is set), as a turn that is reviewed
    assert taken == [(-100, b.t("plan_check_turn"), {"review": True})]
    # a check that never ends lets the announcement go, saying so
    db.set_kv(verifier.PLAN_KEY, None)
    db.set_kv(ANNOUNCE, start)
    b.announce_since[start] = b.clock() - botmod.ANNOUNCE_WAIT_S - 1
    run(b.tick_announce(None))
    assert b.t("review_failed") in sends(b, -100)[-1]["text"]
    assert db.get_kv(ANNOUNCE) is None


def test_the_review_is_told_of_a_broken_chain_the_answer_leaves_in_place(data_dir, tmp_path):
    """Monday's dried-bean salad with no soak and no cook, and hake thawed for a lunch Saturday's
    session already cooked: an answer about something else leaves both, and the reviewer reads
    them, as already there, so it can say whether the answer touches them."""
    from .test_menu import change as make

    b, db = vsetup(data_dir, tmp_path)
    tuesday = D(8)
    lunch = {"date": str(tuesday), "meal": "lunch", "recipe": "pescado-batch", "cook": str(D(5))}
    make(b.data, {"set": lunch})
    dinner(b.data, D(7))
    db.add_reminder(-100, "ana", at(D(7), 20), "x", 7, "pescado-batch", str(tuesday), "thaw")
    cfg(data_dir, text="Hecho.\n" + merluza_wednesday())
    say(b, "@chefbot merluza el miércoles", message_id=1)
    (check,) = lines(data_dir, "checks.jsonl")
    standing = check["brief"].split("## Problems already there before the answer")[1]
    standing = standing.split("\n## ")[0]
    assert f"`dish ensalada-garbanzos {D(7)} cook`" in standing  # no cook, so no soak either
    assert "nothing cooks what comes out of the freezer" in standing and "pescado-batch" in standing
    assert "'already there'" in check["system"]


def test_a_step_nobody_marked_is_not_put_to_every_change_answer(data_dir, tmp_path):
    """The morning's day message asks about it with its buttons; a pantry answer that day must not
    be sent back to ask about it too, as the plan check leaves it out."""
    b, db = vsetup(data_dir, tmp_path)
    unsure = doctor.Problem("unconfirmed", "unconfirmed:r:1", "Nobody said whether r:1 was done.")
    chain = doctor.Problem("no_cook", "no_cook:x", "Nothing cooks x.")
    keys = {unsure.key, chain.key}
    after = verifier.week(b.data, b.data, db, MONDAY, None, (), [unsure, chain], keys)
    assert after["standing"] == ["Nothing cooks x."]  # the chain problem stays: the control
    after = verifier.week(b.data, b.data, db, MONDAY, None, (), [unsure, chain], set())
    assert after["new"] == ["Nothing cooks x."]


def test_answers_sent_while_a_review_runs_get_one_review_of_the_latest_state(
    data_dir, tmp_path, monkeypatch
):
    b, _db = vsetup(data_dir, tmp_path)
    briefs, release = [], asyncio.Event

    async def go():
        gate = release()

        async def complete(data, system, messages):
            briefs.append(messages[0]["content"])
            if len(briefs) == 1:
                await gate.wait()  # the first review is still running when the next answer goes
            return json.dumps({"timeline": ["Thaw the hake the night before."]})

        monkeypatch.setattr(verifier.llm, "complete", complete)
        cfg(data_dir, text="Primera.\n" + merluza_wednesday(), revised="Corrección.")
        await b.handle(None, msg("@chefbot merluza el miércoles", message_id=1))
        while not briefs:
            await asyncio.sleep(0.01)
        cfg(data_dir, text="Segunda.\n" + merluza_wednesday(), revised="Corrección.")
        (data_dir / "plugins" / "turns.jsonl").unlink()  # the plugin answers "text" once more
        await b.handle(None, msg("@chefbot y otra cosa", message_id=2))
        gate.set()
        await drain(b)

    run(go())
    first, latest = briefs  # the overtaken one, then one that covers both answers
    assert "Primera." in first and "Segunda." not in first
    assert "Primera." in latest and "Segunda." in latest
    follow = [t for t in lines(data_dir, "turns.jsonl") if b.t("verify_who") in t["new"][-1]]
    assert len(follow) == 1  # one follow-up for both
