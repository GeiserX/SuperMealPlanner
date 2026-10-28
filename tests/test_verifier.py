"""Prep a dish needs before its day (doctor.py), and the verifier that checks an answer changing the
menu, a recipe or the batch days before it goes out (verifier.py): on a copy of the data, then one
judgment call to the provider, then at most one revision in the same conversation."""

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
    assert doctor.problems(data, db, MONDAY) == []


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
