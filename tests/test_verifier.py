"""Prep a dish needs before its day (doctor.py), and the reviewer that checks every answer changing
anything once it is sent (verifier.py): the structural check, then a review job through the
provider over the whole rest of the week, and what it finds fixed in one follow-up of the bot's own,
never reviewed again."""

import asyncio
import datetime as dt
import json

import pytest
import yaml

from supermealplanner import doctor, verifier
from supermealplanner.model import WEEKDAYS, load
from supermealplanner.planner import plan_for

from .conftest import MONDAY
from .test_conversations import ChatBot
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
        cfg.checkup_chat = 99  # a private chat of no one in the household: no turn, the list
        run(b.raise_plan(None))
        assert taken == [] and db.get_kv("verifier.plan_raised")
        db.set_kv("verifier.plan_raised", None)
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


def test_a_private_checkup_chat_of_no_one_gets_the_list_one_line_each(data_dir, tmp_path):
    """No turn can come there (no person's turn to take), so the sure problems go as a list, once."""
    b, db, taken = certain_setup(data_dir, tmp_path)
    b.data.settings.bot.checkup_chat = 99
    _calls, fake = judged({})
    verifier.llm.complete, real = fake, verifier.llm.complete
    try:
        run(verifier.check_plan(b.data, db, MONDAY))
        run(b.raise_plan(None))
        run(b.raise_plan(None))
    finally:
        verifier.llm.complete = real
    (report,) = [x["text"] for x in sends(b, 99)]
    assert taken == [] and report.startswith(
        "⚠️ The plan has something to fix:\n• the batch session"
    )


# a provider that fails while plugins/down exists; every turn it gets is counted
FLAKY = """
def converse(turn, settings):
    with (HERE / "plan_turns.txt").open("a") as f:
        f.write("turn\\n")
    if (HERE / "down").exists():
        raise RuntimeError("down")
    return {"text": "The batch session moves to Wednesday.", "conversation": "c1"}
"""


def plan_turn_setup(data_dir, tmp_path, monkeypatch, down):
    """certain_setup with the real queue and turns, the sure problems raised now (the window is
    over), and the provider down or up."""
    from supermealplanner import bot as bot_module

    b, db, _taken = certain_setup(data_dir, tmp_path)
    del b.take, b.drive
    (data_dir / "plugins" / "ai.py").write_text(PLUGIN + FLAKY)
    if down:
        (data_dir / "plugins" / "down").write_text("")
    monkeypatch.setattr(bot_module, "PLAN_RAISE_H", 0)
    return b, db


def raise_now(b):
    async def go():
        await b.raise_plan(None)
        await asyncio.gather(*b.tasks)

    run(go())


def plan_turns(data_dir):
    path = data_dir / "plugins" / "plan_turns.txt"
    return len(path.read_text().splitlines()) if path.exists() else 0


def earlier(db, seconds):
    """The plan's last turn was queued `seconds` earlier than it was."""
    raised = db.get_kv("verifier.plan_raised")
    if "at" in raised:  # an answered turn keeps no time: nothing waits
        db.set_kv("verifier.plan_raised", {**raised, "at": raised["at"] - seconds})


def answers(b):
    return [
        p["text"] for p in sends(b, -100) if p["text"] == "The batch session moves to Wednesday."
    ]


async def reach(path, turn=None, seconds=10):
    """Wait for the fake provider's checkpoint `path`, never forever; a turn that ended first
    says why."""
    loop = asyncio.get_running_loop()
    end = loop.time() + seconds
    while not path.exists():
        if turn is not None and turn.done():
            turn.result()  # what it raised, if it did
            raise AssertionError("the turn ended before the provider's checkpoint")
        assert loop.time() < end, "the provider's checkpoint never came"
        await asyncio.sleep(0.01)


def test_a_failed_plan_turn_raises_the_same_problems_again_by_itself(
    data_dir, tmp_path, monkeypatch
):
    from supermealplanner.bot import PLAN_RETRY_S

    b, db = plan_turn_setup(data_dir, tmp_path, monkeypatch, down=True)
    raise_now(b)
    assert plan_turns(data_dir) == 1 and sends(b, -100) == []  # failed, and told to no one
    raise_now(b)
    assert plan_turns(data_dir) == 1  # the next try waits
    earlier(db, PLAN_RETRY_S[0])
    (data_dir / "plugins" / "down").unlink()
    raise_now(b)  # the same plan, the same day, nobody wrote
    assert plan_turns(data_dir) == 2 and len(answers(b)) == 1


def test_the_same_plan_drawn_again_never_brings_a_failed_turns_retry_forward(
    data_dir, tmp_path, monkeypatch
):
    from supermealplanner.bot import PLAN_RETRY_S

    b, db = plan_turn_setup(data_dir, tmp_path, monkeypatch, down=True)
    checked_only(b, db)  # judged, so a plan drawn goes at once
    raise_now(b)
    assert plan_turns(data_dir) == 1  # failed
    (data_dir / "plugins" / "down").unlink()

    async def drawn():  # the plan drawn again, the same as before
        await b.raise_plan(None, at_once=True)
        await asyncio.gather(*b.tasks)

    run(drawn())
    assert plan_turns(data_dir) == 1  # its retry still waits
    earlier(db, PLAN_RETRY_S[0])
    raise_now(b)  # the next tick once it is due
    assert plan_turns(data_dir) == 2 and len(answers(b)) == 1


def test_a_failed_turn_whose_plan_went_back_to_what_was_said_raises_nothing(
    data_dir, tmp_path, monkeypatch
):
    import time

    from supermealplanner.bot import PLAN_CHECK_WAIT_S

    b, db = plan_turn_setup(data_dir, tmp_path, monkeypatch, down=False)
    raise_now(b)
    assert plan_turns(data_dir) == 1 and len(answers(b)) == 1  # these problems reached the chat
    said = db.get_kv("verifier.plan_raised")
    # a turn about another plan failed since; the plan as it is says again what was said
    other = {"key": "another-plan", "found": "other", "judged": True}
    failed = {"row": 99999, "at": time.time(), "tries": 1, "pending": other}
    db.set_kv("verifier.plan_raised", {**said, "key": "before", **failed})
    earlier(db, PLAN_CHECK_WAIT_S)
    raise_now(b)
    raised = db.get_kv("verifier.plan_raised")
    assert plan_turns(data_dir) == 1  # never the same problems twice
    assert "row" not in raised and "pending" not in raised and raised["found"] == said["found"]


def test_a_failed_turn_with_nothing_sure_waits_for_the_check_of_its_plan(
    data_dir, tmp_path, monkeypatch
):
    from supermealplanner.bot import PLAN_RETRY_S

    b, db = plan_turn_setup(data_dir, tmp_path, monkeypatch, down=True)
    raise_now(b)
    assert plan_turns(data_dir) == 1  # failed
    (data_dir / "plugins" / "down").unlink()

    async def nothing_sure():
        return []

    b.problems = nothing_sure
    earlier(db, PLAN_RETRY_S[0])
    raise_now(b)  # nothing sure, and the check has not judged the plan: it waits for it
    assert plan_turns(data_dir) == 1 and "row" in db.get_kv("verifier.plan_raised")
    checked_only(b, db)
    raise_now(b)  # judged, with a finding: said that day, not the next
    assert plan_turns(data_dir) == 2


def test_a_plan_turn_still_queued_is_never_raised_twice_even_after_a_restart(
    data_dir, tmp_path, monkeypatch
):
    b, db = plan_turn_setup(data_dir, tmp_path, monkeypatch, down=True)

    async def parked(client, chat):  # the turn waits in the queue (another chat's turn runs)
        pass

    b.drive = parked
    raise_now(b)
    earlier(db, 86400)
    raise_now(b)
    assert len(db.get_kv("bot.queued")["-100"]) == 1 and plan_turns(data_dir) == 0
    # a restart: the new process finds the turn in QUEUED, never raises a second one, and runs it
    b = ChatBot(b.data, db)
    raise_now(b)
    assert len(db.get_kv("bot.queued")["-100"]) == 1

    async def restart():
        await b.resume(None)
        await asyncio.gather(*b.tasks)

    run(restart())
    assert plan_turns(data_dir) == 1 and not db.get_kv("bot.queued")  # it failed
    (data_dir / "plugins" / "down").unlink()
    raise_now(b)  # its retry waits from its failure, not from a day ago
    assert plan_turns(data_dir) == 1
    earlier(db, 600)
    raise_now(b)  # what the lost turn did not say is said
    assert plan_turns(data_dir) == 2 and len(answers(b)) == 1


def test_a_provider_that_keeps_failing_gets_the_plan_turn_a_few_times_a_day(
    data_dir, tmp_path, monkeypatch
):
    from supermealplanner.bot import PLAN_RETRY_S

    b, db = plan_turn_setup(data_dir, tmp_path, monkeypatch, down=True)
    for _ in range(len(PLAN_RETRY_S) + 3):
        raise_now(b)
        earlier(db, 86400)
    assert plan_turns(data_dir) == len(PLAN_RETRY_S) + 1
    raised = db.get_kv("verifier.plan_raised")
    db.set_kv("verifier.plan_raised", {**raised, "on": "2000-01-01"})  # the next day: tried again
    raise_now(b)
    assert plan_turns(data_dir) == len(PLAN_RETRY_S) + 2


def test_a_plan_turn_that_answered_is_never_raised_again(data_dir, tmp_path, monkeypatch):
    b, db = plan_turn_setup(data_dir, tmp_path, monkeypatch, down=False)
    raise_now(b)
    assert plan_turns(data_dir) == 1 and len(answers(b)) == 1
    raised = db.get_kv("verifier.plan_raised")
    db.set_kv("verifier.plan_raised", {**raised, "at": 0})  # hours later
    raise_now(b)
    db.set_kv("verifier.plan_raised", {**raised, "on": "2000-01-01"})  # the same plan, next day
    raise_now(b)
    assert plan_turns(data_dir) == 1 and len(answers(b)) == 1
    assert "row" not in raised  # nothing waits for a retry


def test_a_plan_turn_whose_answer_telegram_lost_is_raised_again(data_dir, tmp_path, monkeypatch):
    from supermealplanner.bot import PLAN_RETRY_S, TelegramError

    b, db = plan_turn_setup(data_dir, tmp_path, monkeypatch, down=False)
    real = b.call

    async def call(client, method, _tries=0, **params):
        if method == "sendMessage":  # the provider answered, the chat never got it
            raise TelegramError(method, "Bad Gateway", 502)
        return await real(client, method, _tries, **params)

    b.call = call
    raise_now(b)
    assert plan_turns(data_dir) == 1 and answers(b) == []
    b.call = real
    earlier(db, PLAN_RETRY_S[0])
    raise_now(b)
    assert plan_turns(data_dir) == 2 and len(answers(b)) == 1


def test_a_plan_turn_that_failed_after_its_answer_showed_is_not_raised_again(
    data_dir, tmp_path, monkeypatch
):
    from supermealplanner.bot import PLAN_RETRY_S

    b, db = plan_turn_setup(data_dir, tmp_path, monkeypatch, down=False)
    real = b.answer_text

    async def answer_text(*a, **k):  # the answer went out, then a later step of the turn broke
        await real(*a, **k)
        raise RuntimeError("broke")

    b.answer_text = answer_text
    raise_now(b)
    assert plan_turns(data_dir) == 1 and len(answers(b)) == 1
    b.answer_text = real
    earlier(db, PLAN_RETRY_S[0])
    raise_now(b)
    assert plan_turns(data_dir) == 1 and len(answers(b)) == 1


# a person's turn takes the plan turn while it runs and does not answer it; the plan turn
# that follows fails while plugins/down exists
PULLS = """
import time

def converse(turn, settings):
    with (HERE / "plan_turns.txt").open("a") as f:
        f.write("turn\\n")
    if turn["review"]:
        if (HERE / "down").exists():
            raise RuntimeError("down")
        return {"text": "The batch session moves to Wednesday.", "conversation": "c1"}
    (HERE / "waiting").write_text("")
    while not (HERE / "go").exists():
        time.sleep(0.01)
    took = [m["id"] for m in turn["incoming"]()]
    (HERE / "took.json").write_text(json.dumps(took))
    return {"text": "Lentils tonight.", "conversation": "c1", "covers": []}
"""


def test_a_plan_turn_another_turn_took_and_did_not_answer_is_raised_again_when_it_fails(
    data_dir, tmp_path, monkeypatch
):
    from supermealplanner.bot import PLAN_RETRY_S

    b, db = plan_turn_setup(data_dir, tmp_path, monkeypatch, down=True)
    plugins = data_dir / "plugins"
    (plugins / "ai.py").write_text(PLUGIN + PULLS)
    real = b.answer_text
    broke = []

    async def answer_text(*a, **k):  # the person's answer went out, then a later step broke
        await real(*a, **k)
        if not broke:
            broke.append(1)
            raise RuntimeError("broke")

    b.answer_text = answer_text

    async def go():
        b.take(-100, {"id": 7, "first_name": "Luis"}, "luis", None, "qué cenamos", True)
        person = asyncio.ensure_future(b.drive(None, -100))
        try:
            await reach(plugins / "waiting", person)
            await b.raise_plan(None)  # the plan turn joins the running one
        finally:
            (plugins / "go").write_text("")  # the provider's thread never waits forever
        await person
        await asyncio.gather(*b.tasks)

    run(go())
    row = db.get_kv("verifier.plan_raised")["row"]
    assert json.loads((plugins / "took.json").read_text()) == [row]  # taken, not answered
    assert broke and plan_turns(data_dir) == 2 and answers(b) == []  # the plan turn failed
    (plugins / "down").unlink()
    earlier(db, PLAN_RETRY_S[0])
    raise_now(b)
    assert plan_turns(data_dir) == 3 and len(answers(b)) == 1


# like PULLS, but the person's answer covers the plan turn it took
COVERS = PULLS.replace('"covers": []', '"covers": took')


@pytest.mark.parametrize("broke_after", [False, True])
def test_a_plan_turn_another_turn_took_and_answered_is_never_raised_again(
    data_dir, tmp_path, monkeypatch, broke_after
):
    from supermealplanner.bot import PLAN_RETRY_S

    b, db = plan_turn_setup(data_dir, tmp_path, monkeypatch, down=False)
    plugins = data_dir / "plugins"
    (plugins / "ai.py").write_text(PLUGIN + COVERS)
    real = b.answer_text

    async def answer_text(*a, **k):  # the answer went out, then maybe a later step broke
        await real(*a, **k)
        if broke_after:
            raise RuntimeError("broke")

    b.answer_text = answer_text

    async def go():
        b.take(-100, {"id": 7, "first_name": "Luis"}, "luis", None, "qué cenamos", True)
        person = asyncio.ensure_future(b.drive(None, -100))
        try:
            await reach(plugins / "waiting", person)
            await b.raise_plan(None)
        finally:
            (plugins / "go").write_text("")
        await person
        await asyncio.gather(*b.tasks)

    run(go())
    assert json.loads((plugins / "took.json").read_text())  # it took the plan turn
    assert plan_turns(data_dir) == 1 and "row" not in db.get_kv("verifier.plan_raised")
    earlier(db, PLAN_RETRY_S[0])
    raise_now(b)
    assert plan_turns(data_dir) == 1
    raised = db.get_kv("verifier.plan_raised")
    db.set_kv("verifier.plan_raised", {**raised, "on": "2000-01-01"})  # the next morning
    raise_now(b)  # the same problems: what the plan turn was raised for was said
    assert plan_turns(data_dir) == 1


@pytest.mark.parametrize("sent", [True, False])
def test_a_restart_after_another_turn_took_the_plan_turn(data_dir, tmp_path, monkeypatch, sent):
    from supermealplanner.bot import PLAN_RETRY_S

    b, db = plan_turn_setup(data_dir, tmp_path, monkeypatch, down=False)
    plugins = data_dir / "plugins"
    (plugins / "ai.py").write_text(PLUGIN + COVERS)
    real = b.answer_text

    async def answer_text(*a, **k):  # the person's answer covers it, then the process stops
        if sent:
            await real(*a, **k)
        raise asyncio.CancelledError

    b.answer_text = answer_text

    async def go():
        b.take(-100, {"id": 7, "first_name": "Luis"}, "luis", None, "qué cenamos", True)
        person = asyncio.ensure_future(b.drive(None, -100))
        try:
            await reach(plugins / "waiting", person)
            await b.raise_plan(None)
        finally:
            (plugins / "go").write_text("")
        try:
            await person
        except asyncio.CancelledError:  # the process stopped
            pass

    run(go())
    assert json.loads((plugins / "took.json").read_text())
    b2 = ChatBot(b.data, db)  # the new process replays the turn: the conversation has both

    async def resume():
        await b2.resume(None)
        await asyncio.gather(*b2.tasks, return_exceptions=True)

    run(resume())
    assert plan_turns(data_dir) == 1 and ("row" in db.get_kv("verifier.plan_raised")) != sent
    (plugins / "ai.py").write_text(PLUGIN + FLAKY)
    earlier(db, PLAN_RETRY_S[0])
    raise_now(b2)
    assert plan_turns(data_dir) == (1 if sent else 2)


def test_an_answer_before_a_pulled_plan_row_is_not_its_answer_after_a_restart(
    data_dir, tmp_path, monkeypatch
):
    from supermealplanner.bot import AI

    b, db = plan_turn_setup(data_dir, tmp_path, monkeypatch, down=False)
    asked = db.add_chat(-100, "user", "luis", "qué cenamos", asked=True)
    db.add_chat(-100, "assistant", AI, "Lentils tonight.")  # that turn's answer to Luis

    async def parked(client, chat):
        pass

    b.drive = parked
    raise_now(b)
    row = db.get_kv("verifier.plan_raised")["row"]
    # the turn that read Luis's question took the plan row too, then the process stopped
    db.set_kv("bot.conversation.-100", {"id": "group-1", "seen": asked, "done": [row]})
    b2 = ChatBot(b.data, db)

    async def resume():
        await b2.resume(None)
        await asyncio.gather(*b2.tasks, return_exceptions=True)

    run(resume())
    assert not db.get_kv("bot.queued")  # the restart replayed the row
    assert db.get_kv("verifier.plan_raised").get("row") == row  # its answer never went out


# the plan turn's answer, or the person's when their message is new to the turn
ASKED = """
def converse(turn, settings):
    with (HERE / "plan_turns.txt").open("a") as f:
        f.write("turn\\n")
    if any("qué cenamos" in m["content"] for m in turn["new"]):
        return {"text": "Lentils tonight.", "conversation": "c1"}
    return {"text": "The batch session moves to Wednesday.", "conversation": "c1"}
"""


def test_a_question_resumed_with_a_plan_turn_that_never_answered_does_not_answer_it(
    data_dir, tmp_path, monkeypatch
):
    from supermealplanner.bot import PLAN_RETRY_S

    b, db = plan_turn_setup(data_dir, tmp_path, monkeypatch, down=False)
    (data_dir / "plugins" / "ai.py").write_text(PLUGIN + ASKED)

    async def answer_text(*a, **k):  # a question comes, then the process stops before the send
        b.take(-100, {"id": 7, "first_name": "Luis"}, "luis", None, "qué cenamos", True)
        raise asyncio.CancelledError

    b.answer_text = answer_text

    async def go(bot, first):
        await (bot.raise_plan(None) if first else bot.resume(None))
        await asyncio.gather(*bot.tasks, return_exceptions=True)

    run(go(b, True))
    assert plan_turns(data_dir) == 1 and answers(b) == []
    b2 = ChatBot(b.data, db)  # both go in one turn: the conversation has the plan's, unsent
    run(go(b2, False))
    assert plan_turns(data_dir) == 2 and "Lentils tonight." in [p["text"] for p in sends(b2, -100)]
    assert "row" in db.get_kv("verifier.plan_raised")  # the question's answer did not raise it
    earlier(db, PLAN_RETRY_S[0])
    raise_now(b2)
    assert plan_turns(data_dir) == 3 and len(answers(b2)) == 1


# the plan turn's provider answers once plugins/go exists
HOLDS = """
import time

def converse(turn, settings):
    with (HERE / "plan_turns.txt").open("a") as f:
        f.write("turn\\n")
    (HERE / "waiting").write_text("")
    while not (HERE / "go").exists():
        time.sleep(0.01)
    return {"text": "The batch session moves to Wednesday.", "conversation": "c1"}
"""


def drawn_meanwhile(b, db, data_dir):
    """The plan changes while the plan turn runs (a new dinner), the check judges it, and the app
    raises it at once: its key, and its problems differ from the ones the turn read."""
    before, checkup = keys(b.data, db), b.data.settings.bot.checkup_at
    dinner(b.data, MONDAY + dt.timedelta(days=2))
    b.state.reload()
    b.data.settings.bot.checkup_at = checkup  # as the test set it
    assert keys(b.data, db) != before
    _brief, key = verifier.plan_brief(b.data, db, b.state.today())
    db.set_kv(verifier.PLAN_KEY, {"key": key})
    return key


def test_a_plan_drawn_while_the_plan_turn_runs_is_raised_once_it_ends(
    data_dir, tmp_path, monkeypatch
):
    b, db = plan_turn_setup(data_dir, tmp_path, monkeypatch, down=False)
    plugins = data_dir / "plugins"
    (plugins / "ai.py").write_text(PLUGIN + HOLDS)
    b.data.settings.bot.checkup_at = ""  # nothing raises it the next morning
    _brief, key = verifier.plan_brief(b.data, db, b.state.today())
    db.set_kv(verifier.PLAN_KEY, {"key": key})

    async def go():
        await b.raise_plan(None, at_once=True)
        try:
            await reach(plugins / "waiting")  # the turn read the plan as it was
            drawn_meanwhile(b, db, data_dir)
            await b.raise_plan(None, at_once=True)  # never a second turn at once
        finally:
            (plugins / "go").write_text("")
        await asyncio.gather(*b.tasks)

    run(go())
    assert plan_turns(data_dir) == 1 and len(answers(b)) == 1
    raise_now(b)  # the first tick after it ended
    assert plan_turns(data_dir) == 2 and len(answers(b)) == 2
    raise_now(b)
    assert plan_turns(data_dir) == 2 and "drawn" not in db.get_kv("verifier.plan_raised")


def test_a_plan_drawn_while_the_plan_turn_waits_is_raised_once(data_dir, tmp_path, monkeypatch):
    b, db = plan_turn_setup(data_dir, tmp_path, monkeypatch, down=False)
    b.data.settings.bot.checkup_at = ""
    _brief, key = verifier.plan_brief(b.data, db, b.state.today())
    db.set_kv(verifier.PLAN_KEY, {"key": key})

    async def parked(client, chat):  # another turn of the chat runs: the plan turn waits
        pass

    b.drive = parked

    async def drawn():
        await b.raise_plan(None, at_once=True)
        drawn_meanwhile(b, db, data_dir)
        await b.raise_plan(None, at_once=True)  # its turn has not read anything yet

    run(drawn())
    del b.drive
    drew = db.get_kv(verifier.PLAN_KEY)["key"]

    async def go():
        await b.drive(None, -100)
        await asyncio.gather(*b.tasks)

    run(go())  # the waiting turn reads the plan drawn meanwhile
    assert plan_turns(data_dir) == 1 and len(answers(b)) == 1
    raised = db.get_kv("verifier.plan_raised")
    assert "drawn" not in raised and raised["key"] == drew  # the plan it read is the one raised
    # the plan moves before the next tick (a step ticked) and is judged: the turn already raised
    # the drawn plan
    dinner(b.data, MONDAY + dt.timedelta(days=3))
    b.state.reload()
    b.data.settings.bot.checkup_at = ""
    _brief, key = verifier.plan_brief(b.data, db, b.state.today())
    db.set_kv(verifier.PLAN_KEY, {"key": key})
    raise_now(b)
    raise_now(b)
    assert plan_turns(data_dir) == 1


def parked_drawn(b, db, data_dir, tries=None):
    """A plan turn is raised and waits behind another turn of the chat (its try `tries` of the
    day), and a plan is drawn and raised at once meanwhile: it sets drawn."""

    async def parked(client, chat):
        pass

    b.drive = parked

    async def drawn():
        await b.raise_plan(None, at_once=True)
        if tries:
            db.set_kv("verifier.plan_raised", {**db.get_kv("verifier.plan_raised"), "tries": tries})
        drawn_meanwhile(b, db, data_dir)
        await b.raise_plan(None, at_once=True)

    run(drawn())
    del b.drive
    assert db.get_kv("verifier.plan_raised").get("drawn")


def drive_plan_turn(b):
    async def go():
        await b.drive(None, -100)
        await asyncio.gather(*b.tasks)

    run(go())


def test_a_drawn_plan_read_by_a_turn_that_fails_keeps_its_own_tries(
    data_dir, tmp_path, monkeypatch
):
    from supermealplanner.bot import PLAN_RETRY_S

    b, db = plan_turn_setup(data_dir, tmp_path, monkeypatch, down=True)
    b.data.settings.bot.checkup_at = ""
    _brief, key = verifier.plan_brief(b.data, db, b.state.today())
    db.set_kv(verifier.PLAN_KEY, {"key": key})
    parked_drawn(b, db, data_dir, tries=len(PLAN_RETRY_S) + 1)  # the day's last try waits
    drive_plan_turn(b)  # it reads the drawn plan, and the provider fails
    assert plan_turns(data_dir) == 1 and db.get_kv("verifier.plan_raised").get("drawn")
    (data_dir / "plugins" / "down").unlink()
    raise_now(b)  # the drawn plan starts its own tries
    assert plan_turns(data_dir) == 2 and len(answers(b)) == 1
    assert db.get_kv("verifier.plan_raised").get("drawn") is None


def test_a_drawn_plan_outlives_a_turn_that_read_a_plan_not_judged_yet(
    data_dir, tmp_path, monkeypatch
):
    b, db = plan_turn_setup(data_dir, tmp_path, monkeypatch, down=False)
    b.data.settings.bot.checkup_at = ""  # only the drawn mark can raise it
    _brief, key = verifier.plan_brief(b.data, db, b.state.today())
    db.set_kv(verifier.PLAN_KEY, {"key": key})
    parked_drawn(b, db, data_dir)
    dinner(b.data, MONDAY + dt.timedelta(days=3))  # the plan moves again before the turn runs
    b.state.reload()
    b.data.settings.bot.checkup_at = ""
    drive_plan_turn(b)  # it reads a plan the check has not judged
    raised = db.get_kv("verifier.plan_raised")
    assert plan_turns(data_dir) == 1 and raised.get("drawn") and "read" not in raised
    judge(b, db, {"nonsense": [OVEN]})  # judged, with a finding that turn never saw
    raise_now(b)
    assert plan_turns(data_dir) == 2 and len(answers(b)) == 2


def test_only_the_plan_turn_marks_the_plan_it_read(data_dir, tmp_path, monkeypatch):
    b, db = plan_turn_setup(data_dir, tmp_path, monkeypatch, down=False)
    plugins = data_dir / "plugins"
    (plugins / "ai.py").write_text(PLUGIN + HOLDS)
    b.data.settings.bot.checkup_at = ""
    _brief, key = verifier.plan_brief(b.data, db, b.state.today())
    db.set_kv(verifier.PLAN_KEY, {"key": key})

    async def go():
        await b.raise_plan(None, at_once=True)
        try:
            await reach(plugins / "waiting")
            drawn_meanwhile(b, db, data_dir)
            await b.raise_plan(None, at_once=True)
        finally:
            (plugins / "go").write_text("")
        await asyncio.gather(*b.tasks)

    run(go())
    before = db.get_kv("verifier.plan_raised")
    assert before.get("drawn")
    # a person writes before the next tick: their turn reads the plan too, and marks nothing
    b.take(-100, {"id": 7, "first_name": "Luis"}, "luis", None, "qué cenamos", True)
    drive_plan_turn(b)
    assert db.get_kv("verifier.plan_raised") == before
    raise_now(b)
    assert plan_turns(data_dir) == 3


def no_problems(b):
    """What the plan had to say is gone (the household fixed it), and the check agrees."""

    async def problems():
        return []

    b.problems = problems
    _brief, key = verifier.plan_brief(b.data, b.state.db, b.state.today())
    b.state.db.set_kv(verifier.PLAN_KEY, {"key": key})


def test_a_plan_turn_that_read_a_plan_the_check_had_not_judged_leaves_it_to_be_raised(
    data_dir, tmp_path, monkeypatch
):
    b, db = plan_turn_setup(data_dir, tmp_path, monkeypatch, down=False)
    _brief, key = verifier.plan_brief(b.data, db, b.state.today())
    db.set_kv(verifier.PLAN_KEY, {"key": key})

    async def parked(client, chat):
        pass

    b.drive = parked
    raise_now(b)
    checkup = b.data.settings.bot.checkup_at
    dinner(b.data, MONDAY + dt.timedelta(days=2))  # the plan changes before the turn runs
    b.state.reload()
    b.data.settings.bot.checkup_at = checkup
    del b.drive

    async def go():
        await b.drive(None, -100)
        await asyncio.gather(*b.tasks)

    run(go())  # it reads the new plan, which the check has not judged yet
    raised = db.get_kv("verifier.plan_raised")
    # what it said is of neither plan as judged: neither counts as raised
    assert plan_turns(data_dir) == 1 and "key" not in raised and "read" not in raised
    judge(b, db, {"nonsense": [OVEN]})  # judged, with a finding that turn never saw
    db.set_kv("verifier.plan_raised", {**raised, "on": "2000-01-01"})  # the next morning
    raise_now(b)
    assert plan_turns(data_dir) == 2  # its findings are raised


def test_a_failed_plan_turn_whose_problems_went_away_owes_no_turn(data_dir, tmp_path, monkeypatch):
    from supermealplanner.bot import PLAN_RETRY_S

    b, db = plan_turn_setup(data_dir, tmp_path, monkeypatch, down=True)
    b.data.settings.bot.checkup_at = ""
    _brief, key = verifier.plan_brief(b.data, db, b.state.today())
    db.set_kv(verifier.PLAN_KEY, {"key": key})

    async def drawn():
        await b.raise_plan(None, at_once=True)
        await asyncio.gather(*b.tasks)

    run(drawn())
    assert "row" in db.get_kv("verifier.plan_raised")
    no_problems(b)
    (data_dir / "plugins" / "down").unlink()
    earlier(db, PLAN_RETRY_S[0])
    raise_now(b)
    assert plan_turns(data_dir) == 1 and "row" not in db.get_kv("verifier.plan_raised")


def drawn_judged(data_dir, tmp_path, monkeypatch, down):
    """No morning checkup, the plan as it is judged with nothing to say but its sure problems,
    and a raise for it as for a plan just drawn."""
    b, db = plan_turn_setup(data_dir, tmp_path, monkeypatch, down=down)
    b.data.settings.bot.checkup_at = ""
    _brief, key = verifier.plan_brief(b.data, db, b.state.today())
    db.set_kv(verifier.PLAN_KEY, {"key": key})
    return b, db


def draw_now(b):
    async def go():
        await b.raise_plan(None, at_once=True)
        await asyncio.gather(*b.tasks)

    run(go())


async def parked(client, chat):  # another turn of the chat runs: the plan turn waits
    pass


def test_a_failed_turns_retry_waits_out_a_consistency_check_that_could_not_run(
    data_dir, tmp_path, monkeypatch
):
    from supermealplanner.bot import PLAN_RETRY_S

    b, db = drawn_judged(data_dir, tmp_path, monkeypatch, down=True)
    draw_now(b)
    assert plan_turns(data_dir) == 1 and "row" in db.get_kv("verifier.plan_raised")
    real = b.problems

    async def could_not_run():
        return None

    b.problems = could_not_run
    (data_dir / "plugins" / "down").unlink()
    earlier(db, PLAN_RETRY_S[0])
    raise_now(b)  # it cannot tell what the plan has to say: never "nothing"
    assert plan_turns(data_dir) == 1 and "row" in db.get_kv("verifier.plan_raised")
    b.problems = real
    earlier(db, PLAN_RETRY_S[1])
    raise_now(b)
    assert plan_turns(data_dir) == 2 and len(answers(b)) == 1


def test_a_plan_turn_that_fails_just_after_midnight_still_waits_its_first_retry(
    data_dir, tmp_path, monkeypatch
):
    from supermealplanner.bot import PLAN_RETRY_S

    b, db = drawn_judged(data_dir, tmp_path, monkeypatch, down=True)
    draw_now(b)
    assert plan_turns(data_dir) == 1
    # queued the day before, failed just now
    db.set_kv("verifier.plan_raised", {**db.get_kv("verifier.plan_raised"), "on": "2000-01-01"})
    (data_dir / "plugins" / "down").unlink()
    raise_now(b)
    assert plan_turns(data_dir) == 1
    earlier(db, PLAN_RETRY_S[0])
    raise_now(b)
    assert plan_turns(data_dir) == 2 and len(answers(b)) == 1


def test_a_plan_turn_whose_plan_moves_as_it_reads_it_counts_nothing_as_raised(
    data_dir, tmp_path, monkeypatch
):
    b, db = drawn_judged(data_dir, tmp_path, monkeypatch, down=False)
    b.drive = parked
    draw_now(b)  # queued for plan A
    real = b.problems

    async def problems():  # the plan moves while the turn gathers what it has to say of it
        b.problems = real
        moved(b)  # plan B: one more problem the consistency check is sure of
        return await real()

    b.problems = problems
    del b.drive
    drive_plan_turn(b)
    assert plan_turns(data_dir) == 1 and len(answers(b)) == 1
    _brief, key = verifier.plan_brief(b.data, db, b.state.today())
    db.set_kv(verifier.PLAN_KEY, {"key": key})  # B judged: what it adds was never said
    draw_now(b)
    assert plan_turns(data_dir) == 2


def test_a_plan_turn_that_read_another_plan_the_check_had_not_judged_counts_nothing(
    data_dir, tmp_path, monkeypatch
):
    b, _db = drawn_judged(data_dir, tmp_path, monkeypatch, down=False)
    plan = min((data_dir / "state" / "plans").glob("*.yaml"))
    before = plan.read_text()
    b.drive = parked
    draw_now(b)  # queued for plan A
    moved(b)  # plan B, which the check has not judged, before the turn reads it
    del b.drive
    drive_plan_turn(b)
    assert plan_turns(data_dir) == 1 and len(answers(b)) == 1
    plan.write_text(before)  # back to plan A, whose problems the turn never read
    b.state.reload()
    b.data.settings.bot.checkup_at = ""
    draw_now(b)
    assert plan_turns(data_dir) == 2


def test_a_plan_turn_says_none_of_a_verdict_that_came_while_its_facts_were_built(
    data_dir, tmp_path, monkeypatch
):
    from supermealplanner import context

    b, db = plan_turn_setup(data_dir, tmp_path, monkeypatch, down=False)
    b.drive = parked
    raise_now(b)  # the window is over: queued with the sure problems alone
    facts, verdicts = context.facts, []

    def facts_then_verdict(*a, **k):  # built with no verdict; then the check ends
        out = facts(*a, **k)
        if not verdicts:
            verdicts.append(judge(b, db, {"nonsense": [OVEN]}))
        return out

    monkeypatch.setattr(context, "facts", facts_then_verdict)
    del b.drive
    drive_plan_turn(b)
    raised = db.get_kv("verifier.plan_raised")
    assert verdicts and plan_turns(data_dir) == 1 and "key" not in raised
    db.set_kv("verifier.plan_raised", {**raised, "on": "2000-01-01"})  # the next morning
    raise_now(b)  # what the check found was never said
    assert plan_turns(data_dir) == 2


def test_a_plan_turn_shows_no_verdict_that_came_while_its_facts_were_built(
    data_dir, tmp_path, monkeypatch
):
    from supermealplanner import context

    b, db = plan_turn_setup(data_dir, tmp_path, monkeypatch, down=False)
    b.drive = parked
    raise_now(b)  # the window is over: queued with the sure problems alone
    problems_now, verdicts, shown = context.problems_now, [], []

    def verdict_then_problems(*a, **k):  # the check ends while the facts are built
        if not verdicts:
            verdicts.append(judge(b, db, {"nonsense": [OVEN]}))
        out = problems_now(*a, **k)
        shown.append("\n".join(out))
        return out

    monkeypatch.setattr(context, "problems_now", verdict_then_problems)
    del b.drive
    drive_plan_turn(b)
    assert verdicts and shown and OVEN not in shown[0]  # the turn could not say it
    raised = db.get_kv("verifier.plan_raised")
    assert plan_turns(data_dir) == 1 and "key" not in raised
    db.set_kv("verifier.plan_raised", {**raised, "on": "2000-01-01"})  # the next morning
    raise_now(b)  # what the check found is said now
    assert plan_turns(data_dir) == 2


def test_a_plan_turn_with_no_kept_conversation_keeps_its_chat_working(
    data_dir, tmp_path, monkeypatch
):
    b, db = plan_turn_setup(data_dir, tmp_path, monkeypatch, down=False)
    (data_dir / "plugins" / "ai.py").write_text(
        PLUGIN + FLAKY.replace('"conversation": "c1"}', '"conversation": "c1", "kept": False}')
    )
    judge(b, db, {"nonsense": [OVEN]})
    raise_now(b)  # answered, and the provider kept no conversation
    assert plan_turns(data_dir) == 1 and len(answers(b)) == 1
    assert isinstance(db.get_kv("bot.conversation.-100")["seen"], int)
    db.set_kv("verifier.plan_raised", None)  # raised again: the chat still answers
    raise_now(b)
    assert plan_turns(data_dir) == 2 and len(answers(b)) == 2


def test_a_plan_turn_whose_plan_moved_with_the_same_problems_counts_them_as_said(
    data_dir, tmp_path, monkeypatch
):
    b, db = drawn_judged(data_dir, tmp_path, monkeypatch, down=False)
    b.drive = parked
    draw_now(b)  # queued for plan A
    moved(b, rid="merluza-cena-1")  # plan B, the same problems, not judged yet
    del b.drive
    drive_plan_turn(b)
    assert plan_turns(data_dir) == 1 and len(answers(b)) == 1
    _brief, key = verifier.plan_brief(b.data, db, b.state.today())
    db.set_kv(verifier.PLAN_KEY, {"key": key})  # B judged: nothing the turn did not say
    draw_now(b)
    assert plan_turns(data_dir) == 1


def test_a_plan_turn_whose_consistency_check_could_not_run_keeps_what_it_was_raised_for(
    data_dir, tmp_path, monkeypatch
):
    b, db = drawn_judged(data_dir, tmp_path, monkeypatch, down=False)
    b.drive = parked
    draw_now(b)  # queued for plan A, its sure problems
    real = b.problems

    async def could_not_run():  # None, once, as the turn gathers what it has to say
        b.problems = real

    b.problems = could_not_run
    del b.drive
    drive_plan_turn(b)
    assert plan_turns(data_dir) == 1 and len(answers(b)) == 1
    raised = db.get_kv("verifier.plan_raised")
    db.set_kv("verifier.plan_raised", {**raised, "key": "another-plan"})  # the same problems
    draw_now(b)
    assert plan_turns(data_dir) == 1


def test_a_drawn_plan_needs_no_turn_with_the_same_problems_or_none(data_dir, tmp_path, monkeypatch):
    from supermealplanner import bot as bot_module

    b, db = plan_turn_setup(data_dir, tmp_path, monkeypatch, down=False)
    _brief, key = verifier.plan_brief(b.data, db, b.state.today())
    db.set_kv(verifier.PLAN_KEY, {"key": key})
    raise_now(b)  # the turn answered
    assert plan_turns(data_dir) == 1
    on = db.get_kv("verifier.plan_raised")["on"]
    # drawn while it was pending, another key, the same problems
    db.set_kv(
        "verifier.plan_raised", {**db.get_kv("verifier.plan_raised"), "key": "old", "drawn": on}
    )
    raise_now(b)
    assert plan_turns(data_dir) == 1 and "drawn" not in db.get_kv("verifier.plan_raised")
    # drawn, another key, nothing to say, and the window after the checkup not over
    monkeypatch.setattr(bot_module, "PLAN_RAISE_H", 24)
    no_problems(b)
    db.set_kv(
        "verifier.plan_raised", {**db.get_kv("verifier.plan_raised"), "key": "old", "drawn": on}
    )
    raise_now(b)
    assert plan_turns(data_dir) == 1 and "drawn" not in db.get_kv("verifier.plan_raised")


def checked_only(b, db, finding="The oven is used twice at once"):
    """The check judges the plan as it is now and finds something; the consistency check is sure
    of nothing, so only the check's findings can raise it."""

    async def problems():
        return []

    b.problems = problems
    _calls, fake = judged({"nonsense": [finding]} if finding else {})
    verifier.llm.complete, real = fake, verifier.llm.complete
    try:
        run(verifier.check_plan(b.data, db, b.state.today()))
    finally:
        verifier.llm.complete = real
    _brief, key = verifier.plan_brief(b.data, db, b.state.today())
    assert db.get_kv(verifier.PLAN_KEY)["key"] == key


def moved(b, day=2, rid="ensalada-garbanzos"):
    """The plan moves (a new dinner) and the check has not judged it yet."""
    checkup = b.data.settings.bot.checkup_at
    dinner(b.data, MONDAY + dt.timedelta(days=day), rid)
    b.state.reload()
    b.data.settings.bot.checkup_at = checkup


def test_a_late_retry_never_marks_a_plan_the_check_has_not_judged(data_dir, tmp_path, monkeypatch):
    from supermealplanner.bot import PLAN_RETRY_S

    b, db = plan_turn_setup(data_dir, tmp_path, monkeypatch, down=True)  # the window is over
    checked_only(b, db)
    raise_now(b)  # the turn fails
    assert plan_turns(data_dir) == 1 and "row" in db.get_kv("verifier.plan_raised")
    moved(b)  # a step ticked: the check cannot judge it yet
    (data_dir / "plugins" / "down").unlink()
    earlier(db, PLAN_RETRY_S[0])
    raise_now(b)  # the retry is due, and waits for the check
    assert plan_turns(data_dir) == 1 and "row" in db.get_kv("verifier.plan_raised")
    checked_only(b, db, "Tuesday's dinner needs the oven while it is in use")
    raise_now(b)
    assert plan_turns(data_dir) == 2 and len(answers(b)) == 1


def test_a_drawn_plan_raised_late_waits_for_its_check(data_dir, tmp_path, monkeypatch):
    b, db = plan_turn_setup(data_dir, tmp_path, monkeypatch, down=False)  # the window is over
    checked_only(b, db)
    raise_now(b)  # the turn answered
    assert plan_turns(data_dir) == 1
    raised = db.get_kv("verifier.plan_raised")
    db.set_kv("verifier.plan_raised", {**raised, "drawn": raised["on"]})  # drawn while it ran
    moved(b)  # and moved again before the check judged it
    raise_now(b)
    assert plan_turns(data_dir) == 1 and db.get_kv("verifier.plan_raised").get("drawn")
    checked_only(b, db, "Tuesday's dinner needs the oven while it is in use")
    raise_now(b)
    assert plan_turns(data_dir) == 2 and len(answers(b)) == 2


def test_a_morning_raise_after_the_window_leaves_an_unjudged_plan_for_the_next_day(
    data_dir, tmp_path, monkeypatch
):
    b, db = plan_turn_setup(data_dir, tmp_path, monkeypatch, down=False)  # the window is over
    checked_only(b, db)
    raise_now(b)
    assert plan_turns(data_dir) == 1
    moved(b)  # the plan changes overnight, and the check has not judged it
    db.set_kv("verifier.plan_raised", {**db.get_kv("verifier.plan_raised"), "on": "2000-01-01"})
    raise_now(b)  # nothing sure to say: today's raise is over, the plan is not raised
    assert plan_turns(data_dir) == 1
    checked_only(b, db, "Tuesday's dinner needs the oven while it is in use")
    raise_now(b)  # judged the same day: tomorrow
    assert plan_turns(data_dir) == 1
    db.set_kv("verifier.plan_raised", {**db.get_kv("verifier.plan_raised"), "on": "2000-01-01"})
    raise_now(b)
    assert plan_turns(data_dir) == 2 and len(answers(b)) == 2


def test_a_morning_raise_that_left_an_unjudged_plan_never_repeats_the_same_problems(
    data_dir, tmp_path, monkeypatch
):
    b, db = plan_turn_setup(data_dir, tmp_path, monkeypatch, down=False)  # the window is over
    checked_only(b, db)
    raise_now(b)
    found = db.get_kv("verifier.plan_raised")["found"]
    moved(b)  # the plan changes overnight, and the check has not judged it
    db.set_kv("verifier.plan_raised", {**db.get_kv("verifier.plan_raised"), "on": "2000-01-01"})
    raise_now(b)
    assert db.get_kv("verifier.plan_raised").get("found") == found
    checked_only(b, db)  # judged: the same problems as the turn already raised
    db.set_kv("verifier.plan_raised", {**db.get_kv("verifier.plan_raised"), "on": "2000-01-01"})
    raise_now(b)
    assert plan_turns(data_dir) == 1 and len(answers(b)) == 1


def test_a_drawn_plan_already_raised_needs_no_turn(data_dir, tmp_path, monkeypatch):
    b, db = plan_turn_setup(data_dir, tmp_path, monkeypatch, down=False)
    _brief, key = verifier.plan_brief(b.data, db, b.state.today())
    db.set_kv(verifier.PLAN_KEY, {"key": key})
    raise_now(b)  # the turn answered
    raised = db.get_kv("verifier.plan_raised")
    db.set_kv("verifier.plan_raised", {**raised, "drawn": raised["on"]})  # the same key
    raise_now(b)
    assert plan_turns(data_dir) == 1 and "drawn" not in db.get_kv("verifier.plan_raised")


def test_a_plan_drawn_on_an_earlier_day_is_not_raised_at_once(data_dir, tmp_path, monkeypatch):
    b, db = plan_turn_setup(data_dir, tmp_path, monkeypatch, down=False)
    raise_now(b)  # today's raise answered: the next one is tomorrow's
    raised = db.get_kv("verifier.plan_raised")
    old = {"key": "old", "found": "old", "drawn": "2000-01-01"}  # another plan, other findings
    db.set_kv("verifier.plan_raised", {**raised, **old})
    raise_now(b)
    assert plan_turns(data_dir) == 1


def test_problems_a_failed_turn_never_said_are_no_repeat_when_they_come_back(
    data_dir, tmp_path, monkeypatch
):
    from supermealplanner.bot import PLAN_RETRY_S

    b, db = plan_turn_setup(data_dir, tmp_path, monkeypatch, down=True)
    b.data.settings.bot.checkup_at = ""
    checked_only(b, db)  # plan A, problems X

    async def drawn():
        await b.raise_plan(None, at_once=True)
        await asyncio.gather(*b.tasks)

    run(drawn())  # its turn fails
    moved(b)  # fixed before the retry: the check finds nothing
    checked_only(b, db, None)
    (data_dir / "plugins" / "down").unlink()
    earlier(db, PLAN_RETRY_S[0])
    raise_now(b)
    assert plan_turns(data_dir) == 1 and "row" not in db.get_kv("verifier.plan_raised")
    moved(b, day=3)  # X comes back on another plan: the chat never heard it
    checked_only(b, db)
    run(drawn())
    assert plan_turns(data_dir) == 2 and len(answers(b)) == 1


def test_a_plan_turn_that_failed_on_an_earlier_day_goes_on_the_first_tick(
    data_dir, tmp_path, monkeypatch
):
    b, db = plan_turn_setup(data_dir, tmp_path, monkeypatch, down=True)
    raise_now(b)  # it fails late at night: its retry would wait an hour that day
    assert plan_turns(data_dir) == 1 and "row" in db.get_kv("verifier.plan_raised")
    db.set_kv("verifier.plan_raised", {**db.get_kv("verifier.plan_raised"), "on": "2000-01-01"})
    earlier(db, 600)  # hours ago: a day went by
    (data_dir / "plugins" / "down").unlink()
    raise_now(b)  # a new day: at once
    assert plan_turns(data_dir) == 2 and len(answers(b)) == 1


def test_a_failed_plan_undone_after_its_retry_was_cancelled_is_raised(
    data_dir, tmp_path, monkeypatch
):
    from supermealplanner.bot import PLAN_RETRY_S

    b, db = plan_turn_setup(data_dir, tmp_path, monkeypatch, down=True)
    b.data.settings.bot.checkup_at = ""
    plan = min((data_dir / "state" / "plans").glob("*.yaml"))
    before = plan.read_text()
    checked_only(b, db)  # plan A, problems X

    async def drawn():
        await b.raise_plan(None, at_once=True)
        await asyncio.gather(*b.tasks)

    run(drawn())  # its turn fails
    moved(b)  # fixed before the retry: the check finds nothing
    checked_only(b, db, None)
    (data_dir / "plugins" / "down").unlink()
    earlier(db, PLAN_RETRY_S[0])
    raise_now(b)
    assert plan_turns(data_dir) == 1 and "row" not in db.get_kv("verifier.plan_raised")
    plan.write_text(before)  # the change is undone: plan A again, whose problems nobody heard
    b.state.reload()
    b.data.settings.bot.checkup_at = ""
    checked_only(b, db)
    run(drawn())
    assert plan_turns(data_dir) == 2 and len(answers(b)) == 1


def judge(b, db, verdict):
    """The check judges the plan as it is now with this verdict; the consistency check runs for
    real (plan_turn_setup's plan has a problem it is sure of)."""
    _calls, fake = judged(verdict)
    verifier.llm.complete, real = fake, verifier.llm.complete
    try:
        run(verifier.check_plan(b.data, db, b.state.today()))
    finally:
        verifier.llm.complete = real
    _brief, key = verifier.plan_brief(b.data, db, b.state.today())
    assert db.get_kv(verifier.PLAN_KEY)["key"] == key


OVEN = "The oven is used twice at once"


def test_a_retry_whose_plan_moved_waits_for_the_check_and_says_its_findings(
    data_dir, tmp_path, monkeypatch
):
    b, db = plan_turn_setup(data_dir, tmp_path, monkeypatch, down=True)  # the window is over
    judge(b, db, {"nonsense": [OVEN]})
    raise_now(b)  # its turn fails
    moved(b)  # a step ticked: the check has not judged the plan yet
    (data_dir / "plugins" / "down").unlink()
    earlier(db, 600)
    raise_now(b)  # the retry is due, and waits for the check
    assert plan_turns(data_dir) == 1
    judge(b, db, {"nonsense": [OVEN]})
    raise_now(b)  # the same day, with what the check found
    assert plan_turns(data_dir) == 2 and len(answers(b)) == 1
    db.set_kv("verifier.plan_raised", {**db.get_kv("verifier.plan_raised"), "on": "2000-01-01"})
    raise_now(b)  # said: never again
    assert plan_turns(data_dir) == 2


def test_a_retry_whose_check_never_ends_goes_with_the_sure_problems_and_leaves_the_findings(
    data_dir, tmp_path, monkeypatch
):
    from supermealplanner import bot as bot_module

    monkeypatch.setattr(bot_module, "PLAN_CHECK_WAIT_S", 0)  # the check never ended in time
    b, db = plan_turn_setup(data_dir, tmp_path, monkeypatch, down=True)  # the window is over
    judge(b, db, {"nonsense": [OVEN]})
    raise_now(b)  # its turn fails
    moved(b)
    (data_dir / "plugins" / "down").unlink()
    earlier(db, 600)
    raise_now(b)  # the retry goes with the sure problems alone
    assert plan_turns(data_dir) == 2 and len(answers(b)) == 1
    judge(b, db, {"nonsense": [OVEN]})  # judged: what the check found was not said yet
    raise_now(b)
    db.set_kv("verifier.plan_raised", {**db.get_kv("verifier.plan_raised"), "on": "2000-01-01"})
    raise_now(b)
    assert plan_turns(data_dir) == 3 and len(answers(b)) == 2


def test_a_retry_whose_plan_moved_goes_after_its_wait_with_no_morning_checkup(
    data_dir, tmp_path, monkeypatch
):
    from supermealplanner import bot as bot_module

    b, db = plan_turn_setup(data_dir, tmp_path, monkeypatch, down=True)  # the window is over
    judge(b, db, {"nonsense": [OVEN]})
    raise_now(b)  # its turn fails
    moved(b)  # a step ticked: the check of the plan as it is never ends
    (data_dir / "plugins" / "down").unlink()
    b.data.settings.bot.checkup_at = ""
    earlier(db, 600)
    raise_now(b)  # the retry is due, and waits for the check
    assert plan_turns(data_dir) == 1
    monkeypatch.setattr(bot_module, "PLAN_CHECK_WAIT_S", 0)  # the wait is over
    raise_now(b)  # with the sure problems alone, though no checkup runs
    assert plan_turns(data_dir) == 2 and len(answers(b)) == 1


def test_a_retry_that_went_without_the_check_and_failed_goes_again_with_no_morning_checkup(
    data_dir, tmp_path, monkeypatch
):
    from supermealplanner import bot as bot_module

    monkeypatch.setattr(bot_module, "PLAN_CHECK_WAIT_S", 0)  # the check never ends in time
    b, db = plan_turn_setup(data_dir, tmp_path, monkeypatch, down=True)  # the window is over
    judge(b, db, {"nonsense": [OVEN]})
    raise_now(b)  # its turn fails
    moved(b)  # the check of the plan as it is never ends
    b.data.settings.bot.checkup_at = ""
    earlier(db, 600)
    raise_now(b)  # the retry goes with the sure problems, and fails too
    assert plan_turns(data_dir) == 2 and not answers(b)
    (data_dir / "plugins" / "down").unlink()
    earlier(db, 1800)
    raise_now(b)  # its own retry, still with no verdict
    assert plan_turns(data_dir) == 3 and len(answers(b)) == 1


def test_a_retry_whose_plan_moves_while_its_problems_are_gathered_waits_for_the_next_tick(
    data_dir, tmp_path, monkeypatch
):
    b, db = plan_turn_setup(data_dir, tmp_path, monkeypatch, down=True)  # the window is over
    judge(b, db, {"nonsense": [OVEN]})
    raise_now(b)  # its turn fails
    (data_dir / "plugins" / "down").unlink()
    earlier(db, 600)
    real = b.problems

    async def moving():  # a step is ticked while the consistency check runs
        b.problems = real
        moved(b)
        return await real()

    b.problems = moving
    raise_now(b)  # neither said with the check's findings left out, nor cancelled
    raised = db.get_kv("verifier.plan_raised")
    assert plan_turns(data_dir) == 1 and raised.get("row") and "key" not in raised


def test_a_late_retry_never_counts_the_failed_plan_as_raised(data_dir, tmp_path, monkeypatch):
    from supermealplanner import bot as bot_module

    monkeypatch.setattr(bot_module, "PLAN_CHECK_WAIT_S", 0)  # the check never ended in time
    b, db = plan_turn_setup(data_dir, tmp_path, monkeypatch, down=True)  # the window is over
    plan = min((data_dir / "state" / "plans").glob("*.yaml"))
    before = plan.read_text()
    judge(b, db, {"nonsense": [OVEN]})
    raise_now(b)  # its turn fails
    moved(b)  # a step ticked: the check has not judged the plan yet
    (data_dir / "plugins" / "down").unlink()
    earlier(db, 600)
    raise_now(b)  # the retry goes with the sure problems alone
    assert plan_turns(data_dir) == 2 and len(answers(b)) == 1
    checkup = b.data.settings.bot.checkup_at
    plan.write_text(before)  # the tick is undone: the failed plan again, whose finding nobody heard
    b.state.reload()
    b.data.settings.bot.checkup_at = checkup
    judge(b, db, {"nonsense": [OVEN]})
    db.set_kv("verifier.plan_raised", {**db.get_kv("verifier.plan_raised"), "on": "2000-01-01"})
    raise_now(b)
    assert plan_turns(data_dir) == 3 and len(answers(b)) == 2


def test_a_failed_raise_of_an_unjudged_plan_keeps_what_reached_the_chat(
    data_dir, tmp_path, monkeypatch
):
    b, db = plan_turn_setup(data_dir, tmp_path, monkeypatch, down=False)  # the window is over
    plan = min((data_dir / "state" / "plans").glob("*.yaml"))
    before = plan.read_text()
    judge(b, db, {"nonsense": [OVEN]})
    raise_now(b)  # plan A reaches the chat
    a = db.get_kv("verifier.plan_raised")["key"]
    moved(b)  # the next day the plan is B, which the check has not judged
    (data_dir / "plugins" / "down").write_text("")
    db.set_kv("verifier.plan_raised", {**db.get_kv("verifier.plan_raised"), "on": "2000-01-01"})
    raise_now(b)  # its late raise goes with the sure problems, and fails
    assert plan_turns(data_dir) == 2 and db.get_kv("verifier.plan_raised")["key"] == a
    checkup = b.data.settings.bot.checkup_at
    plan.write_text(before)  # undone: plan A again, which the chat already heard
    b.state.reload()
    b.data.settings.bot.checkup_at = checkup
    (data_dir / "plugins" / "down").unlink()
    earlier(db, 600)
    raise_now(b)  # its retry is due: nothing is owed
    raised = db.get_kv("verifier.plan_raised")
    assert plan_turns(data_dir) == 2 and raised["key"] == a and "row" not in raised
    db.set_kv("verifier.plan_raised", {**raised, "on": "2000-01-01"})
    raise_now(b)
    assert plan_turns(data_dir) == 2 and len(answers(b)) == 1


BUSY = """
class Busy(Exception):
    retry_after = 600

def converse(turn, settings):
    with (HERE / "plan_turns.txt").open("a") as f:
        f.write("turn\\n")
    if (HERE / "busy").exists():
        raise Busy("busy")
    return {"text": "The batch session moves to Wednesday.", "conversation": "c1"}
"""


def test_a_plan_turn_a_busy_provider_puts_off_is_never_raised_twice(
    data_dir, tmp_path, monkeypatch
):
    from supermealplanner.bot import PLAN_RETRY_S

    b, db = plan_turn_setup(data_dir, tmp_path, monkeypatch, down=False)
    plugins = data_dir / "plugins"
    (plugins / "ai.py").write_text(PLUGIN + BUSY)
    (plugins / "busy").write_text("")
    woken = []

    def wake(client, chat_id, seconds):
        woken.append((chat_id, seconds))
        return asyncio.sleep(0)

    b.wake = wake
    raise_now(b)  # the provider is busy: the turn waits for it
    raised = db.get_kv("verifier.plan_raised")
    assert woken and plan_turns(data_dir) == 1
    assert raised["row"] in [x["row"] for x in db.get_kv("bot.queued")["-100"]]
    earlier(db, PLAN_RETRY_S[-1] * 2)
    raise_now(b)  # still waiting: never a second turn
    assert plan_turns(data_dir) == 1 and len(db.get_kv("bot.queued")["-100"]) == 1
    assert db.get_kv("verifier.plan_raised")["row"] == raised["row"]
    (plugins / "busy").unlink()
    b.resting.pop(-100)  # the provider is back: what wake() does when its time comes
    drive_plan_turn(b)
    assert plan_turns(data_dir) == 2 and len(answers(b)) == 1
    assert "row" not in db.get_kv("verifier.plan_raised")


def test_a_drawn_mark_set_while_the_plan_turn_reads_the_plan_stays(data_dir, tmp_path, monkeypatch):
    b, db = plan_turn_setup(data_dir, tmp_path, monkeypatch, down=True)

    async def parked(client, chat):
        pass

    b.drive = parked
    raise_now(b)
    del b.drive
    drawn_meanwhile(b, db, data_dir)  # judged before the turn runs: it reads this plan
    real, fired = b.problems, []

    async def problems(*a, **k):  # the app draws a plan while the turn works out what it says
        if not fired:
            fired.append(True)
            await b.raise_plan(None, at_once=True)
        return await real(*a, **k)

    b.problems = problems
    drive_plan_turn(b)  # and fails
    raised = db.get_kv("verifier.plan_raised")
    assert fired and raised.get("drawn") and "row" in raised


def test_what_a_plan_turn_said_counts_its_sure_problems_and_only_the_findings_it_raises(
    data_dir, tmp_path, monkeypatch
):
    b, db = plan_turn_setup(data_dir, tmp_path, monkeypatch, down=False)  # the window is over
    judge(b, db, {"nonsense": [OVEN]})

    async def parked(client, chat):
        pass

    b.drive = parked
    raise_now(b)
    del b.drive
    moved(b)  # before its turn runs, plan B is judged: other problems, and a wish
    said_b = "Tuesday's dinner needs the oven while it is in use"
    judge(b, db, {"nonsense": [said_b], "wishes": ["More fish"]})
    drive_plan_turn(b)  # it reads B and says it
    assert plan_turns(data_dir) == 1
    moved(b, day=3, rid="merluza-cena-1")  # the next day: the same problems, another wish
    judge(b, db, {"nonsense": [said_b], "wishes": ["Less rice"]})
    db.set_kv("verifier.plan_raised", {**db.get_kv("verifier.plan_raised"), "on": "2000-01-01"})
    raise_now(b)
    assert plan_turns(data_dir) == 1


def test_a_plan_turn_that_failed_late_waits_from_its_failure(data_dir, tmp_path, monkeypatch):
    b, db = plan_turn_setup(data_dir, tmp_path, monkeypatch, down=True)

    async def parked(client, chat):  # it waits behind another turn of the chat
        pass

    b.drive = parked
    raise_now(b)
    del b.drive
    earlier(db, 3000)  # queued 50 minutes ago
    drive_plan_turn(b)  # and fails now
    raise_now(b)
    assert plan_turns(data_dir) == 1  # its 10 minutes start now
    earlier(db, 600)
    raise_now(b)
    assert plan_turns(data_dir) == 2


@pytest.mark.parametrize("comes_back", ["what_it_said", "what_was_queued"])
def test_a_plan_turn_that_said_another_plan_keeps_what_it_said(
    data_dir, tmp_path, monkeypatch, comes_back
):
    b, db = plan_turn_setup(data_dir, tmp_path, monkeypatch, down=False)  # the window is over
    checked_only(b, db)  # plan A, problems X
    said_b = "Tuesday's dinner needs the oven while it is in use"

    async def parked(client, chat):  # a busy provider holds the turn for a while
        pass

    b.drive = parked
    raise_now(b)
    del b.drive
    queued = db.get_kv("verifier.plan_raised")["pending"]["found"]  # X
    moved(b)  # before its turn runs, plan B is drawn and judged, with problems Y
    checked_only(b, db, said_b)
    drive_plan_turn(b)  # it reads B and says Y
    raised = db.get_kv("verifier.plan_raised")
    assert plan_turns(data_dir) == 1 and "row" not in raised and raised["found"] != queued
    moved(b, day=3)  # the next day the plan moves again: Y again, or X, which the chat never heard
    if comes_back == "what_it_said":
        checked_only(b, db, said_b)
    else:
        checked_only(b, db)
    db.set_kv("verifier.plan_raised", {**db.get_kv("verifier.plan_raised"), "on": "2000-01-01"})
    raise_now(b)
    if comes_back == "what_it_said":  # Y again: already said
        assert plan_turns(data_dir) == 1
    else:
        assert plan_turns(data_dir) == 2 and len(answers(b)) == 2


def test_a_plan_turn_marks_no_plan_that_moved_while_its_facts_were_built(
    data_dir, tmp_path, monkeypatch
):
    from supermealplanner import context

    b, db = plan_turn_setup(data_dir, tmp_path, monkeypatch, down=False)
    _brief, key = verifier.plan_brief(b.data, db, b.state.today())
    db.set_kv(verifier.PLAN_KEY, {"key": key})
    facts, moves = context.facts, []

    def facts_then_move(*a, **k):  # the facts are of A; then the plan moves to B, judged
        out = facts(*a, **k)
        if not moves:
            moves.append(drawn_meanwhile(b, db, data_dir))
        return out

    monkeypatch.setattr(context, "facts", facts_then_move)
    raise_now(b)
    raised = db.get_kv("verifier.plan_raised")
    # it cannot tell which plan its facts were of: neither A nor B counts as raised
    assert moves and plan_turns(data_dir) == 1 and "key" not in raised and "found" not in raised


def test_a_plan_drawn_late_at_night_is_raised_after_midnight_with_no_morning_checkup(
    data_dir, tmp_path, monkeypatch
):
    b, db = plan_turn_setup(data_dir, tmp_path, monkeypatch, down=False)
    b.data.settings.bot.checkup_at = ""  # nothing else raises it
    _brief, key = verifier.plan_brief(b.data, db, b.state.today())
    db.set_kv(verifier.PLAN_KEY, {"key": key})

    async def go():
        await b.raise_plan(None, at_once=True)
        await asyncio.gather(*b.tasks)

    run(go())
    assert plan_turns(data_dir) == 1
    drawn_meanwhile(b, db, data_dir)  # drawn yesterday while that turn was pending
    raised = db.get_kv("verifier.plan_raised")
    db.set_kv("verifier.plan_raised", {**raised, "drawn": "2000-01-01"})
    raise_now(b)
    assert plan_turns(data_dir) == 2 and len(answers(b)) == 2


def test_a_failed_plan_turn_waits_longer_after_each_failure(data_dir, tmp_path, monkeypatch):
    from supermealplanner.bot import PLAN_RETRY_S

    assert PLAN_RETRY_S == (600, 1800, 3600)  # 10, 30 and 60 minutes, as the docs say
    b, db = plan_turn_setup(data_dir, tmp_path, monkeypatch, down=True)
    raise_now(b)
    for n, wait in enumerate((600, 1800, 3600), start=1):
        earlier(db, wait - 5)
        raise_now(b)
        assert plan_turns(data_dir) == n  # not yet
        earlier(db, 5)
        raise_now(b)
        assert plan_turns(data_dir) == n + 1


@pytest.mark.parametrize("checkup", [False, True])
def test_a_plan_drawn_meanwhile_waits_for_its_check_when_the_turn_moved_the_plan(
    data_dir, tmp_path, monkeypatch, checkup
):
    b, db = plan_turn_setup(data_dir, tmp_path, monkeypatch, down=False)
    plugins = data_dir / "plugins"
    (plugins / "ai.py").write_text(PLUGIN + HOLDS)
    if not checkup:
        b.data.settings.bot.checkup_at = ""
    checkup = b.data.settings.bot.checkup_at
    _brief, key = verifier.plan_brief(b.data, db, b.state.today())
    db.set_kv(verifier.PLAN_KEY, {"key": key})

    async def go():
        await b.raise_plan(None, at_once=True)
        try:
            await reach(plugins / "waiting")
            drawn_meanwhile(b, db, data_dir)
            await b.raise_plan(None, at_once=True)
            # then the plan moves again before the turn ends (a step ticked): not judged yet
            dinner(b.data, MONDAY + dt.timedelta(days=3))
            b.state.reload()
            b.data.settings.bot.checkup_at = checkup
        finally:
            (plugins / "go").write_text("")
        await asyncio.gather(*b.tasks)

    run(go())
    raise_now(b)
    # the check has not judged the plan as it is now: it waits, and so does drawn, even after the
    # window of the morning checkup
    assert plan_turns(data_dir) == 1 and db.get_kv("verifier.plan_raised").get("drawn")
    _brief, key = verifier.plan_brief(b.data, db, b.state.today())
    db.set_kv(verifier.PLAN_KEY, {"key": key})  # judged
    raise_now(b)
    assert plan_turns(data_dir) == 2 and len(answers(b)) == 2


def test_a_restart_once_the_plan_turn_answered_never_raises_it_again(
    data_dir, tmp_path, monkeypatch
):
    from supermealplanner.bot import PLAN_RETRY_S

    b, db = plan_turn_setup(data_dir, tmp_path, monkeypatch, down=False)
    real = b.answer_text

    async def answer_text(*a, **k):  # the process stops once the answer went out
        await real(*a, **k)
        raise asyncio.CancelledError

    b.answer_text = answer_text

    async def go(bot, first):
        await (bot.raise_plan(None) if first else bot.resume(None))
        await asyncio.gather(*bot.tasks, return_exceptions=True)

    run(go(b, True))
    assert plan_turns(data_dir) == 1 and len(answers(b)) == 1
    assert db.get_kv("bot.queued") and "row" in db.get_kv("verifier.plan_raised")
    b2 = ChatBot(b.data, db)  # the new process replays the turn: the conversation has it
    run(go(b2, False))
    earlier(db, PLAN_RETRY_S[0])
    raise_now(b2)
    assert plan_turns(data_dir) == 1 and answers(b2) == []


def test_a_new_plan_is_raised_again_after_a_failed_turn_with_the_morning_checkup_off(
    data_dir, tmp_path, monkeypatch
):
    from supermealplanner.bot import PLAN_RETRY_S

    b, db = plan_turn_setup(data_dir, tmp_path, monkeypatch, down=True)
    b.data.settings.bot.checkup_at = ""
    _brief, key = verifier.plan_brief(b.data, db, b.state.today())
    db.set_kv(verifier.PLAN_KEY, {"key": key})  # checked, with the sure problems alone

    async def drawn():
        await b.raise_plan(None, at_once=True)
        await asyncio.gather(*b.tasks)

    run(drawn())
    assert plan_turns(data_dir) == 1
    (data_dir / "plugins" / "down").unlink()
    earlier(db, PLAN_RETRY_S[0])
    raise_now(b)  # the tick: no checkup, the failed turn still goes again
    assert plan_turns(data_dir) == 2 and len(answers(b)) == 1
    raise_now(b)
    assert plan_turns(data_dir) == 2


def test_the_plan_turn_tries_count_per_day_whatever_moves_the_key(data_dir, tmp_path, monkeypatch):
    from supermealplanner.bot import PLAN_RETRY_S

    b, db = plan_turn_setup(data_dir, tmp_path, monkeypatch, down=True)
    for _ in range(len(PLAN_RETRY_S) + 3):
        raise_now(b)
        raised = db.get_kv("verifier.plan_raised")  # a step ticked: the key moved, not the plan
        pending = {**raised["pending"], "key": "before-the-tick"}
        db.set_kv("verifier.plan_raised", {**raised, "pending": pending})
        earlier(db, 86400)
    assert plan_turns(data_dir) == len(PLAN_RETRY_S) + 1
    # a plan the app just drew gets its own tries the same day
    _brief, key = verifier.plan_brief(b.data, db, b.state.today())
    db.set_kv(verifier.PLAN_KEY, {"key": key})

    async def drawn():
        await b.raise_plan(None, at_once=True)
        await asyncio.gather(*b.tasks)

    run(drawn())
    earlier(db, PLAN_RETRY_S[0])
    raise_now(b)
    assert plan_turns(data_dir) == len(PLAN_RETRY_S) + 3


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


def test_a_restart_before_the_plan_answer_went_out_raises_it_again(data_dir, tmp_path, monkeypatch):
    from supermealplanner.bot import PLAN_RETRY_S

    b, db = plan_turn_setup(data_dir, tmp_path, monkeypatch, down=False)

    async def answer_text(*a, **k):  # the conversation took it, then the process stopped
        raise asyncio.CancelledError

    b.answer_text = answer_text

    async def go(bot, first):
        await (bot.raise_plan(None) if first else bot.resume(None))
        await asyncio.gather(*bot.tasks, return_exceptions=True)

    run(go(b, True))
    assert plan_turns(data_dir) == 1 and answers(b) == []
    b2 = ChatBot(b.data, db)  # the new process replays it: taken, but never sent
    run(go(b2, False))
    assert plan_turns(data_dir) == 1 and "row" in db.get_kv("verifier.plan_raised")
    earlier(db, PLAN_RETRY_S[0])
    raise_now(b2)  # what never reached the chat goes again
    assert plan_turns(data_dir) == 2 and len(answers(b2)) == 1


def test_a_restart_never_takes_a_line_of_the_app_for_the_plan_answer(
    data_dir, tmp_path, monkeypatch
):
    from supermealplanner.bot import PLAN_RETRY_S

    b, db = plan_turn_setup(data_dir, tmp_path, monkeypatch, down=False)

    async def answer_text(*a, **k):  # a reminder goes out, then the process stops
        db.add_chat(-100, "assistant", None, "Reminder: thaw the chicken.")
        raise asyncio.CancelledError

    b.answer_text = answer_text

    async def go(bot, first):
        await (bot.raise_plan(None) if first else bot.resume(None))
        await asyncio.gather(*bot.tasks, return_exceptions=True)

    run(go(b, True))
    b2 = ChatBot(b.data, db)
    run(go(b2, False))  # the reminder is no answer of the plan turn
    assert plan_turns(data_dir) == 1 and "row" in db.get_kv("verifier.plan_raised")
    earlier(db, PLAN_RETRY_S[0])
    raise_now(b2)
    assert plan_turns(data_dir) == 2 and len(answers(b2)) == 1


def test_a_plan_turn_still_queued_on_a_later_day_is_not_replaced(data_dir, tmp_path, monkeypatch):
    b, db = plan_turn_setup(data_dir, tmp_path, monkeypatch, down=True)

    async def parked(client, chat):  # the turn waits in the queue
        pass

    b.drive = parked
    raise_now(b)
    raised = db.get_kv("verifier.plan_raised")
    # the next day, with another plan and other findings, its turn has still not run
    db.set_kv("verifier.plan_raised", {**raised, "on": "2000-01-01", "key": "old", "found": "old"})
    raise_now(b)
    assert len(db.get_kv("bot.queued")["-100"]) == 1
    assert db.get_kv("verifier.plan_raised")["row"] == raised["row"]  # still the one to retry


def test_a_failed_turn_of_a_new_plan_is_retried_before_the_morning_checkup(
    data_dir, tmp_path, monkeypatch
):
    from supermealplanner.bot import PLAN_RETRY_S

    b, db = plan_turn_setup(data_dir, tmp_path, monkeypatch, down=True)
    monkeypatch.setattr(db, "was_sent", lambda *a: False)  # today's checkup has not gone yet
    _brief, key = verifier.plan_brief(b.data, db, b.state.today())
    db.set_kv(verifier.PLAN_KEY, {"key": key})

    async def drawn():
        await b.raise_plan(None, at_once=True)
        await asyncio.gather(*b.tasks)

    run(drawn())
    assert plan_turns(data_dir) == 1
    (data_dir / "plugins" / "down").unlink()
    earlier(db, PLAN_RETRY_S[0])
    raise_now(b)  # its retry is due: it goes now, not after the checkup
    assert plan_turns(data_dir) == 2 and len(answers(b)) == 1


def test_an_earlier_turns_answer_is_not_taken_for_the_plan_answer_after_a_restart(
    data_dir, tmp_path, monkeypatch
):
    from supermealplanner.bot import AI, PLAN_RETRY_S

    b, db = plan_turn_setup(data_dir, tmp_path, monkeypatch, down=False)

    async def parked(client, chat):  # another turn of the chat runs: the plan turn waits
        pass

    b.drive = parked
    raise_now(b)
    # that turn answers after the plan-check line was queued, without covering it
    db.add_chat(-100, "assistant", AI, "Lunch is at two.")
    del b.drive

    async def answer_text(*a, **k):  # the plan turn: the conversation took it, then a restart
        raise asyncio.CancelledError

    b.answer_text = answer_text

    async def go(bot, first):
        try:
            await (bot.drive(None, -100) if first else bot.resume(None))
        except asyncio.CancelledError:  # the process stopped
            pass
        await asyncio.gather(*bot.tasks, return_exceptions=True)

    run(go(b, True))
    assert plan_turns(data_dir) == 1 and answers(b) == []
    b2 = ChatBot(b.data, db)  # the replay must not take "Lunch is at two." for its answer
    run(go(b2, False))
    assert plan_turns(data_dir) == 1 and "row" in db.get_kv("verifier.plan_raised")
    earlier(db, PLAN_RETRY_S[0])
    raise_now(b2)
    assert plan_turns(data_dir) == 2 and len(answers(b2)) == 1
