"""The plan check's turn (Bot.raise_plan): what is wrong in the plan reaches the checkup chat once,
again when the turn that should say it fails, and never twice in the same chat. The bot's clock is
the test's (Clock): every wait is set by hand, never measured on the real clock."""

import asyncio
import datetime as dt
import json
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from supermealplanner import bot as bot_module
from supermealplanner import doctor, verifier
from supermealplanner.planner import plan_for

from .conftest import MONDAY
from .test_conversations import PLUGIN, ChatBot
from .test_doctor import household
from .test_reminders import ZONE
from .test_settings_stores_bot import run
from .test_turns import sends
from .test_verifier import dinner, judged, vsetup

ANSWER = "The batch session moves to Wednesday."
OVEN = "The oven is used twice at once"
TUESDAY_OVEN = "Tuesday's dinner needs the oven while it is in use"
HAKE = "merluza-cena-1"

# the plan turn's provider: every turn it gets is written down; it fails while plugins/down exists,
# and answers with plugins/reply.txt when there is one
TURNS = """
def converse(turn, settings):
    keep = {k: turn[k] for k in ("new", "history", "context", "review")}
    with (HERE / "plan_turns.jsonl").open("a") as f:
        f.write(json.dumps(keep, default=str) + "\\n")
    if (HERE / "down").exists():
        raise RuntimeError("down")
    reply = HERE / "reply.txt"
    text = reply.read_text() if reply.exists() else "The batch session moves to Wednesday."
    offers = HERE / "proposals.json"
    proposals = json.loads(offers.read_text()) if offers.exists() else []
    return {"text": text, "conversation": "c1", "proposals": proposals}
"""


class Clock:
    """The bot's wall clock (bot.py's datetime.now and time.time), moved by hand."""

    def __init__(self, at: dt.datetime):
        self.at = at

    def set(self, day: dt.date, hour: int, minute: int = 0) -> None:
        self.at = dt.datetime(day.year, day.month, day.day, hour, minute, tzinfo=ZONE)

    def tick(self, seconds: float) -> None:
        self.at += dt.timedelta(seconds=seconds)


@pytest.fixture
def clock(monkeypatch):
    """Monday 08:05, five minutes after the morning checkup."""
    c = Clock(dt.datetime(MONDAY.year, MONDAY.month, MONDAY.day, 8, 5, tzinfo=ZONE))

    class Datetime(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return c.at.astimezone(tz) if tz is not None else c.at.replace(tzinfo=None)

    fake_dt = SimpleNamespace(
        date=dt.date, time=dt.time, timedelta=dt.timedelta, UTC=dt.UTC, datetime=Datetime
    )
    fake_time = SimpleNamespace(
        time=lambda: c.at.timestamp(), monotonic=time.monotonic, sleep=time.sleep
    )
    monkeypatch.setattr(bot_module, "dt", fake_dt)
    monkeypatch.setattr(bot_module, "time", fake_time)
    return c


def setup(data_dir, tmp_path, monkeypatch, down=False, window=False, checkup=True):
    """A plan with a problem the consistency check is sure of (sessions off the batch days), no
    verdict of the plan check yet, the morning checkup done at 08:00, and the plan turn's provider
    (down, or answering). The 2 hours a morning raise waits for the check are over unless `window`.
    checkup False: bot.checkup_at is off."""
    b, _db = vsetup(data_dir, tmp_path)
    household(data_dir, preferred_days=["wednesday", "saturday"])
    b.state.reload()
    db = b.state.db
    assert [p for p in doctor.problems(b.data, db, MONDAY) if p.kind == "off_day"]
    b.data.settings.bot.checkup_at = "08:00" if checkup else ""
    db.mark_sent("checkup", MONDAY, "08:00")
    plugins = data_dir / "plugins"
    (plugins / "ai.py").write_text(PLUGIN + TURNS)
    if down:
        (plugins / "down").write_text("")
    if not window:
        monkeypatch.setattr(bot_module, "PLAN_RAISE_H", 0)
    return b, db


def down(data_dir, on=True):
    path = data_dir / "plugins" / "down"
    if on:
        path.write_text("")
    elif path.exists():
        path.unlink()


def tick(b):
    """One tick of the plan raise, and whatever it started."""

    async def go():
        await b.raise_plan(None)
        await asyncio.gather(*b.tasks)

    run(go())


def draw(b):
    """The app drew a plan and told the chat: its raise, now."""

    async def go():
        await b.raise_plan(None, at_once=True)
        await asyncio.gather(*b.tasks)

    run(go())


def drive(b, chat=-100):
    async def go():
        await b.drive(None, chat)
        await asyncio.gather(*b.tasks)

    run(go())


async def parked(client, chat):  # another turn of the chat runs: the plan turn waits
    pass


def turns(data_dir) -> list[dict]:
    path = data_dir / "plugins" / "plan_turns.jsonl"
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


def answers(b, chat=-100) -> int:
    return len([p for p in sends(b, chat) if p["text"] == ANSWER])


def new_text(turn: dict) -> str:
    return "\n".join(m["content"] for m in turn["new"])


def history_text(turn: dict) -> str:
    return "\n".join(m["content"] for m in turn["history"])


def judge(b, db, verdict):
    """The plan check judges the plan as it is now with this verdict."""
    _calls, fake = judged(verdict)
    verifier.llm.complete, real = fake, verifier.llm.complete
    check = verifier.check_plan(b.data, db, b.state.today())
    try:
        asyncio.get_running_loop()
    except RuntimeError:  # called from the test itself
        run(check)
    else:  # called while a turn runs: the check ends on its own thread
        with ThreadPoolExecutor(1) as pool:
            pool.submit(asyncio.run, check).result()
    finally:
        verifier.llm.complete = real
    _brief, key = verifier.plan_brief(b.data, db, b.state.today())
    assert db.get_kv(verifier.PLAN_KEY)["key"] == key


def nothing_sure(b):
    """The consistency check is sure of nothing: only the check's findings can raise the plan."""

    async def problems(offered=()):
        return []

    b.problems = problems


def edit(b, day=2, rid="ensalada-garbanzos"):
    """The plan moves (another dinner; the salad brings a soak and a cook nobody does), and the
    check has not judged it. The bot's settings stay as the test set them."""
    bot = b.data.settings.bot
    dinner(b.data, MONDAY + dt.timedelta(days=day), rid)
    b.state.reload()
    b.state.data.settings.bot = bot


def plan_file(data_dir):
    return min((data_dir / "state" / "plans").glob("*.yaml"))


def undo(b, path, text):
    """The plan file goes back to `text` (an undo); the bot's settings stay."""
    bot = b.data.settings.bot
    path.write_text(text)
    b.state.reload()
    b.state.data.settings.bot = bot


def next_morning(b, clock, db, hour=12):
    """The next day, after its morning checkup."""
    clock.set(clock.at.date() + dt.timedelta(days=1), hour)
    db.mark_sent("checkup", clock.at.date(), b.data.settings.bot.checkup_at or "08:00")


SALAD_COOK = "dish ensalada-garbanzos 2026-10-14 cook"  # what edit() adds to the sure problems


# ---------------------------------------------------------------- the eleven cases of #142


def test_a_turn_queued_before_midnight_that_fails_after_it_waits_its_ten_minutes(
    data_dir, tmp_path, monkeypatch, clock
):
    """#142 case 1: no morning checkup; a plan drawn while the turn waited was raised at once after
    that turn failed, with no 10 minute wait."""
    b, db = setup(data_dir, tmp_path, monkeypatch, down=True, checkup=False)
    judge(b, db, {})
    clock.set(MONDAY, 23, 50)
    b.drive = parked
    draw(b)  # queued, waiting behind another turn of the chat
    edit(b)
    judge(b, db, {})
    clock.set(MONDAY, 23, 58)
    draw(b)  # another plan drawn meanwhile
    del b.drive
    clock.set(MONDAY + dt.timedelta(days=1), 0, 2)
    drive(b)  # the turn runs after midnight, and fails
    assert len(turns(data_dir)) == 1
    down(data_dir, False)
    clock.tick(60)
    tick(b)
    assert len(turns(data_dir)) == 1  # its 10 minutes start at the failure
    clock.tick(540)
    tick(b)
    assert len(turns(data_dir)) == 2 and answers(b) == 1


def test_a_clean_plan_that_settles_a_retry_keeps_what_reached_the_chat(
    data_dir, tmp_path, monkeypatch, clock
):
    """#142 case 2: a clean plan cancelled the retry and dropped what last reached the chat, so a
    later plan with the same findings said them again."""
    b, db = setup(data_dir, tmp_path, monkeypatch, checkup=False)
    nothing_sure(b)
    judge(b, db, {"nonsense": [OVEN]})
    draw(b)  # OVEN reaches the chat
    assert len(turns(data_dir)) == 1 and answers(b) == 1
    edit(b)
    judge(b, db, {"nonsense": [TUESDAY_OVEN]})
    down(data_dir)
    draw(b)  # another plan, another finding: its turn fails
    assert len(turns(data_dir)) == 2 and answers(b) == 1
    edit(b, day=3, rid=HAKE)
    judge(b, db, {})  # fixed before the retry: nothing to say
    down(data_dir, False)
    clock.tick(600)
    tick(b)
    assert len(turns(data_dir)) == 2
    edit(b, day=4, rid=HAKE)
    judge(b, db, {"nonsense": [OVEN]})  # OVEN again: the chat heard it
    draw(b)
    assert len(turns(data_dir)) == 2 and answers(b) == 1


# a person's turn: it waits for plugins/go, then takes what came meanwhile and covers it; the
# plan turn answers ANSWER
JOINS = """
import time
from concurrent.futures import ThreadPoolExecutor

def converse(turn, settings):
    keep = {k: turn[k] for k in ("new", "history", "context", "review")}
    with (HERE / "plan_turns.jsonl").open("a") as f:
        f.write(json.dumps(keep, default=str) + "\\n")
    if turn["review"]:
        return {"text": "The batch session moves to Wednesday.", "conversation": "c1"}
    (HERE / "waiting").write_text("")
    while not (HERE / "go").exists():
        time.sleep(0.01)
    took = [m["id"] for m in turn["incoming"]()]
    (HERE / "took.json").write_text(json.dumps(took))
    return {"text": "Lentils tonight.", "conversation": "c1", "covers": took}
"""


async def reach(path, turn=None, seconds=10):
    """Wait for the fake provider's checkpoint `path`, never forever."""
    loop = asyncio.get_running_loop()
    end = loop.time() + seconds
    while not path.exists():
        if turn is not None and turn.done():
            turn.result()
            raise AssertionError("the turn ended before the provider's checkpoint")
        assert loop.time() < end, "the provider's checkpoint never came"
        await asyncio.sleep(0.01)


def test_a_plan_turn_never_joins_a_running_turn_whose_facts_are_of_another_plan(
    data_dir, tmp_path, monkeypatch, clock
):
    """#142 case 3: the plan-check row joined a person's turn whose facts were of the plan before,
    and its problems counted as said."""
    b, _db = setup(data_dir, tmp_path, monkeypatch)
    plugins = data_dir / "plugins"
    (plugins / "ai.py").write_text(PLUGIN + JOINS)

    async def go():
        b.take(-100, {"id": 7, "first_name": "Luis"}, "luis", None, "qué cenamos", True)
        person = asyncio.ensure_future(b.drive(None, -100))
        try:
            await reach(plugins / "waiting", person)  # its facts are of the plan before
            edit(b)  # the salad brings a cook nobody does
            await b.raise_plan(None)
        finally:
            (plugins / "go").write_text("")
        await person
        await asyncio.gather(*b.tasks)

    run(go())
    assert json.loads((plugins / "took.json").read_text()) == []  # not handed to that turn
    plan_turns = [t for t in turns(data_dir) if t["review"]]
    assert len(plan_turns) == 1 and SALAD_COOK in plan_turns[0]["context"]  # its own facts
    assert answers(b) == 1


def test_a_change_made_before_the_answer_failed_is_in_the_next_turns_history(
    data_dir, tmp_path, monkeypatch, clock
):
    """#142 case 4: with bot.ask_first off, the plan turn made its menu change and then its answer
    never reached the chat: the next try did not know the change was made."""
    from supermealplanner.bot import TelegramError

    from .test_act_without_asking import WED, own_idea

    b, _db = setup(data_dir, tmp_path, monkeypatch)
    b.data.settings.bot.ask_first = False
    (data_dir / "plugins" / "reply.txt").write_text("Arreglado.\n" + own_idea())
    real = b.call

    async def call(client, method, _tries=0, **params):
        if method == "sendMessage" and "Arreglado" in params.get("text", ""):
            raise TelegramError(method, "Bad Gateway", 502)
        return await real(client, method, _tries, **params)

    b.call = call
    tick(b)
    assert len(turns(data_dir)) == 1 and doctor.serves(b.data, HAKE, WED)  # made, never read
    b.call = real
    clock.tick(bot_module.PLAN_CHECK_WAIT_S)
    tick(b)
    assert len(turns(data_dir)) == 2
    # in what the next turn reads: its new messages (the provider kept the conversation) or
    # its history. It says what the app changed, never the answer's own words (they may hold a
    # person's weigh-in or notes)
    seen = new_text(turns(data_dir)[1]) + history_text(turns(data_dir)[1])
    assert "never reached the chat" in seen and "Arreglado." not in seen
    (unsent,) = [
        r["text"] for r in _db.chat_rows(-100, 50) if bot_module.UNSENT_CHANGE in r["text"]
    ]
    assert unsent != bot_module.UNSENT_CHANGE and "Arreglado." not in unsent


def test_a_change_whose_answer_went_out_before_the_turn_broke_is_not_called_unsent(
    data_dir, tmp_path, monkeypatch, clock
):
    from .test_act_without_asking import WED, own_idea

    b, db = setup(data_dir, tmp_path, monkeypatch)
    b.data.settings.bot.ask_first = False
    (data_dir / "plugins" / "reply.txt").write_text("Arreglado.\n" + own_idea())
    real = b.answer_text

    async def answer_text(*a, **k):  # the answer went out, then a later step of the turn broke
        await real(*a, **k)
        raise RuntimeError("broke")

    b.answer_text = answer_text
    tick(b)
    texts = [r["text"] for r in db.chat_rows(-100, 50)]
    assert doctor.serves(b.data, HAKE, WED) and any(t.startswith("Arreglado.") for t in texts)
    assert not any(bot_module.UNSENT_CHANGE in t for t in texts)


def test_a_plan_whose_announcement_went_before_its_check_is_raised_once_judged(
    data_dir, tmp_path, monkeypatch, clock
):
    """#142 case 5: no morning checkup; the announcement of a new plan went without its check, and
    nothing raised what the check found later."""
    from supermealplanner.scheduler import ANNOUNCE

    b, db = setup(data_dir, tmp_path, monkeypatch, checkup=False)
    start = plan_for(b.data, MONDAY).start.isoformat()
    db.set_kv(ANNOUNCE, start)
    b.announce_since[start] = b.clock() - bot_module.ANNOUNCE_WAIT_S - 1
    run(b.tick_announce(None))
    assert b.t("review_failed") in sends(b, -100)[-1]["text"] and turns(data_dir) == []
    judge(b, db, {"nonsense": [OVEN]})
    tick(b)
    (turn,) = turns(data_dir)
    assert OVEN in turn["context"] and answers(b) == 1


def test_a_plan_that_moves_while_its_announcement_goes_is_raised_once_judged(
    data_dir, tmp_path, monkeypatch, clock
):
    """#142 case 6: no morning checkup; the plan moved while its announcement was sent, and the
    raise of the new plan was lost."""
    from supermealplanner.scheduler import ANNOUNCE

    b, db = setup(data_dir, tmp_path, monkeypatch, checkup=False)
    judge(b, db, {})
    db.set_kv(ANNOUNCE, plan_for(b.data, MONDAY).start.isoformat())
    real, moved = b.send, []

    async def send(*a, **k):  # a step is ticked while the announcement goes out
        out = await real(*a, **k)
        if not moved:
            moved.append(edit(b))
        return out

    b.send = send
    run(b.tick_announce(None))
    b.send = real
    assert moved and turns(data_dir) == []  # the plan as it is waits for its check
    judge(b, db, {"nonsense": [OVEN]})
    tick(b)
    (turn,) = turns(data_dir)
    assert OVEN in turn["context"] and SALAD_COOK in turn["context"] and answers(b) == 1


def test_a_retry_that_should_wait_half_an_hour_waits_it_across_midnight(
    data_dir, tmp_path, monkeypatch, clock
):
    """#142 case 7: after midnight the day's count started again, so a retry that had to wait 30
    or 60 minutes went after 10."""
    b, db = setup(data_dir, tmp_path, monkeypatch, down=True, checkup=False)
    judge(b, db, {})
    clock.set(MONDAY, 23, 35)
    draw(b)  # fails
    clock.set(MONDAY, 23, 45)
    tick(b)  # its first retry fails too: the next waits 30 minutes, to 00:15
    assert len(turns(data_dir)) == 2
    down(data_dir, False)
    clock.set(MONDAY + dt.timedelta(days=1), 0, 0)
    tick(b)
    assert len(turns(data_dir)) == 2
    clock.set(MONDAY + dt.timedelta(days=1), 0, 14)
    tick(b)
    assert len(turns(data_dir)) == 2
    clock.tick(60)
    tick(b)
    assert len(turns(data_dir)) == 3 and answers(b) == 1


def test_a_finding_the_turns_facts_never_showed_is_raised_after_it(
    data_dir, tmp_path, monkeypatch, clock
):
    """#142 case 8: a queued turn showed another plan's verdict while it read its own, and the
    finding it was queued for counted as said. (A turn whose own gathering fails:
    test_a_plan_turn_whose_consistency_check_fails_is_raised_again.)"""
    b, db = setup(data_dir, tmp_path, monkeypatch)
    path = plan_file(data_dir)
    before = path.read_text()
    judge(b, db, {"nonsense": [OVEN]})
    b.drive = parked
    tick(b)  # queued to say OVEN
    del b.drive
    edit(b)
    judge(b, db, {})  # another plan judged: the verdict kept is of it
    undo(b, path, before)  # back to the plan the turn was queued for
    drive(b)  # its facts show no verdict of this plan
    assert len(turns(data_dir)) == 1 and OVEN not in turns(data_dir)[0]["context"]
    judge(b, db, {"nonsense": [OVEN]})
    tick(b)
    assert len(turns(data_dir)) == 2 and OVEN in turns(data_dir)[1]["context"]


def test_what_a_private_checkup_could_not_see_reaches_the_group_when_the_checkup_moves_there(
    data_dir, tmp_path, monkeypatch, clock
):
    """#142 case 9: the turn in a person's own chat never showed a health finding, but it counted
    as said, so the group never got it once the checkup moved there."""
    b, db = setup(data_dir, tmp_path, monkeypatch)
    cfg = b.data.settings.bot
    ana = cfg.people["ana"]
    cfg.checkup_chat = ana
    health = "Luis gets the salt he must avoid"
    judge(b, db, {"health": [health]})
    tick(b)
    assert len(turns(data_dir)) == 1 and health not in turns(data_dir)[0]["context"]
    assert answers(b, ana) == 1
    cfg.checkup_chat = None  # the checkup goes to the group from now on
    next_morning(b, clock, db)
    tick(b)
    assert len(turns(data_dir)) == 2 and health in turns(data_dir)[1]["context"]
    assert answers(b, -100) == 1


def test_a_turn_for_a_late_finding_acts_only_on_what_the_chat_has_not_heard(
    data_dir, tmp_path, monkeypatch, clock
):
    """#142 case 10: no morning checkup; the turn for a finding the check made after the sure
    problems went out asked the model to act on those sure problems again."""
    monkeypatch.setattr(bot_module, "PLAN_CHECK_WAIT_S", 0)  # the check never ends in time
    b, db = setup(data_dir, tmp_path, monkeypatch, down=True, checkup=False)
    judge(b, db, {})
    draw(b)  # its turn fails
    edit(b)  # a step ticked: the check has not judged the plan
    down(data_dir, False)
    clock.tick(600)
    tick(b)  # the retry goes with the sure problems alone
    assert len(turns(data_dir)) == 2 and answers(b) == 1
    judge(b, db, {"nonsense": [OVEN]})
    tick(b)
    assert len(turns(data_dir)) == 3
    asked = new_text(turns(data_dir)[2])
    assert b.t("plan_check_new", items="").split("\n")[0] in asked  # the rest is background
    # only the finding is new: the sessions off their days were said by the turn before
    assert OVEN in asked and "not on a batch day" not in asked


def test_with_no_morning_checkup_only_a_plan_the_app_draws_is_raised(
    data_dir, tmp_path, monkeypatch, clock
):
    """#142 case 11: the docs said that with no morning checkup the check's findings are raised as
    soon as it judges a plan; an edit from the chat is never raised then."""
    from pathlib import Path

    b, db = setup(data_dir, tmp_path, monkeypatch, checkup=False)
    edit(b)  # an edit from the chat, judged with a finding
    judge(b, db, {"nonsense": [OVEN]})
    tick(b)
    assert turns(data_dir) == []
    docs = Path(__file__).parent.parent / "docs" / "how-it-works.md"
    text = " ".join(docs.read_text().split())
    assert (
        "With no morning checkup, what the check finds is raised as soon as it judges" not in text
    )
    assert "With no morning checkup, only a plan the app draws is raised" in text


# ---------------------------------------------------------------- tries and waits


def test_a_failed_plan_turn_raises_the_same_problems_again_by_itself(
    data_dir, tmp_path, monkeypatch, clock
):
    b, _db = setup(data_dir, tmp_path, monkeypatch, down=True)
    tick(b)
    assert len(turns(data_dir)) == 1 and sends(b, -100) == []  # failed, and told to no one
    tick(b)
    assert len(turns(data_dir)) == 1  # the next try waits
    clock.tick(600)
    down(data_dir, False)
    tick(b)  # the same plan, the same day, nobody wrote
    assert len(turns(data_dir)) == 2 and answers(b) == 1


def test_a_failed_plan_turn_waits_longer_after_each_failure(data_dir, tmp_path, monkeypatch, clock):
    from supermealplanner.bot import PLAN_RETRY_S

    assert PLAN_RETRY_S == (600, 1800, 3600)  # 10, 30 and 60 minutes, as the docs say
    b, _db = setup(data_dir, tmp_path, monkeypatch, down=True)
    tick(b)
    for n, wait in enumerate(PLAN_RETRY_S, start=1):
        clock.tick(wait - 1)
        tick(b)
        assert len(turns(data_dir)) == n  # not yet
        clock.tick(1)
        tick(b)
        assert len(turns(data_dir)) == n + 1


PLAN_TRIES = len(bot_module.PLAN_RETRY_S) + 1  # plan-check turns a day: 4


def test_a_provider_that_keeps_failing_gets_the_plan_turn_a_few_times_a_day(
    data_dir, tmp_path, monkeypatch, clock
):
    b, db = setup(data_dir, tmp_path, monkeypatch, down=True)
    for _ in range(PLAN_TRIES + 3):
        tick(b)
        clock.tick(3600)
    assert len(turns(data_dir)) == PLAN_TRIES == 4
    next_morning(b, clock, db, hour=9)  # the next day: tried again
    tick(b)
    assert len(turns(data_dir)) == PLAN_TRIES + 1


def test_a_provider_that_failed_all_day_is_not_called_at_night(
    data_dir, tmp_path, monkeypatch, clock
):
    """With a morning checkup, a try left from the day before waits for this morning's checkup:
    the household chat never gets a plan check at midnight."""
    b, db = setup(data_dir, tmp_path, monkeypatch, down=True)
    for _ in range(PLAN_TRIES + 1):
        tick(b)
        clock.tick(3600)
    assert len(turns(data_dir)) == PLAN_TRIES
    down(data_dir, False)
    tuesday = MONDAY + dt.timedelta(days=1)
    for hour in (0, 3, 7):
        clock.set(tuesday, hour, 30)
        tick(b)
    assert len(turns(data_dir)) == PLAN_TRIES and answers(b) == 0
    clock.set(tuesday, 8, 5)
    db.mark_sent("checkup", tuesday, "08:00")
    tick(b)
    assert len(turns(data_dir)) == PLAN_TRIES + 1 and answers(b) == 1


def test_the_plan_turn_tries_count_per_day_whatever_moves_the_plan(
    data_dir, tmp_path, monkeypatch, clock
):
    b, _db = setup(data_dir, tmp_path, monkeypatch, down=True)
    for n in range(PLAN_TRIES + 2):
        tick(b)
        edit(b, day=n % 5)  # a step ticked: the plan's key moves
        clock.tick(bot_module.PLAN_CHECK_WAIT_S + 60)  # past any wait for its check
    assert len(turns(data_dir)) == PLAN_TRIES


def test_a_plan_drawn_after_the_days_tries_waits_for_the_next_day(
    data_dir, tmp_path, monkeypatch, clock
):
    """The day's tries are the day's, whatever the app draws: a provider that failed them all is
    not called again that day for a new plan either; the next morning raises it."""
    b, db = setup(data_dir, tmp_path, monkeypatch, down=True)
    for _ in range(PLAN_TRIES + 1):
        tick(b)
        clock.tick(3600)
    assert len(turns(data_dir)) == PLAN_TRIES
    down(data_dir, False)
    edit(b)
    judge(b, db, {})
    draw(b)
    assert len(turns(data_dir)) == PLAN_TRIES
    next_morning(b, clock, db, hour=9)
    tick(b)
    assert len(turns(data_dir)) == PLAN_TRIES + 1 and answers(b) == 1
    assert SALAD_COOK in turns(data_dir)[-1]["context"]


def test_a_plan_drawn_after_a_failed_try_is_owed_anew_and_goes_at_once(
    data_dir, tmp_path, monkeypatch, clock
):
    """A plan the app draws is a raise owed anew, told right after its announcement: a try that
    failed before it never delays it (the day's PLAN_TRIES still count both)."""
    b, db = setup(data_dir, tmp_path, monkeypatch, down=True)
    nothing_sure(b)
    judge(b, db, {"nonsense": [OVEN]})
    tick(b)
    assert len(turns(data_dir)) == 1  # failed
    down(data_dir, False)
    clock.tick(60)
    draw(b)  # the plan drawn again, the same as before
    assert len(turns(data_dir)) == 2 and answers(b) == 1
    clock.tick(600)
    tick(b)
    assert len(turns(data_dir)) == 2


def test_a_plan_turn_that_answered_is_never_raised_again(data_dir, tmp_path, monkeypatch, clock):
    b, db = setup(data_dir, tmp_path, monkeypatch)
    tick(b)
    assert len(turns(data_dir)) == 1 and answers(b) == 1
    clock.tick(5 * 3600)  # hours later
    tick(b)
    next_morning(b, clock, db)  # the same plan, the next day
    tick(b)
    assert len(turns(data_dir)) == 1 and answers(b) == 1


def test_a_plan_turn_whose_answer_telegram_lost_is_raised_again(
    data_dir, tmp_path, monkeypatch, clock
):
    from supermealplanner.bot import TelegramError

    b, _db = setup(data_dir, tmp_path, monkeypatch)
    real = b.call

    async def call(client, method, _tries=0, **params):
        if method == "sendMessage":  # the provider answered, the chat never got it
            raise TelegramError(method, "Bad Gateway", 502)
        return await real(client, method, _tries, **params)

    b.call = call
    tick(b)
    assert len(turns(data_dir)) == 1 and answers(b) == 0
    b.call = real
    clock.tick(600)
    tick(b)
    assert len(turns(data_dir)) == 2 and answers(b) == 1


def test_a_plan_turn_that_failed_after_its_answer_showed_is_not_raised_again(
    data_dir, tmp_path, monkeypatch, clock
):
    b, _db = setup(data_dir, tmp_path, monkeypatch)
    real = b.answer_text

    async def answer_text(*a, **k):  # the answer went out, then a later step of the turn broke
        await real(*a, **k)
        raise RuntimeError("broke")

    b.answer_text = answer_text
    tick(b)
    assert len(turns(data_dir)) == 1 and answers(b) == 1
    b.answer_text = real
    clock.tick(600)
    tick(b)
    assert len(turns(data_dir)) == 1 and answers(b) == 1


def test_a_plan_turn_that_failed_late_waits_from_its_failure(
    data_dir, tmp_path, monkeypatch, clock
):
    b, _db = setup(data_dir, tmp_path, monkeypatch, down=True)
    b.drive = parked
    tick(b)
    del b.drive
    clock.tick(3000)  # queued 50 minutes ago
    drive(b)  # and fails now
    tick(b)
    assert len(turns(data_dir)) == 1  # its 10 minutes start now
    clock.tick(600)
    tick(b)
    assert len(turns(data_dir)) == 2


def test_a_plan_turn_that_fails_just_after_midnight_still_waits_its_first_retry(
    data_dir, tmp_path, monkeypatch, clock
):
    b, db = setup(data_dir, tmp_path, monkeypatch, down=True, checkup=False)
    judge(b, db, {})
    clock.set(MONDAY, 23, 58)
    b.drive = parked
    draw(b)  # queued the day before
    del b.drive
    clock.set(MONDAY + dt.timedelta(days=1), 0, 1)
    drive(b)  # failed just now
    down(data_dir, False)
    tick(b)
    assert len(turns(data_dir)) == 1
    clock.tick(600)
    tick(b)
    assert len(turns(data_dir)) == 2 and answers(b) == 1


def test_a_plan_turn_that_failed_late_at_night_goes_again_the_next_morning(
    data_dir, tmp_path, monkeypatch, clock
):
    b, db = setup(data_dir, tmp_path, monkeypatch, down=True)
    clock.set(MONDAY, 23, 0)
    tick(b)  # it fails late at night
    assert len(turns(data_dir)) == 1
    down(data_dir, False)
    next_morning(b, clock, db, hour=8)
    tick(b)
    assert len(turns(data_dir)) == 2 and answers(b) == 1


def test_a_new_plan_is_raised_again_after_a_failed_turn_with_the_morning_checkup_off(
    data_dir, tmp_path, monkeypatch, clock
):
    b, db = setup(data_dir, tmp_path, monkeypatch, down=True, checkup=False)
    judge(b, db, {})  # judged, with the sure problems alone
    draw(b)
    assert len(turns(data_dir)) == 1
    down(data_dir, False)
    clock.tick(600)
    tick(b)  # no checkup: the failed turn still goes again
    assert len(turns(data_dir)) == 2 and answers(b) == 1
    tick(b)
    assert len(turns(data_dir)) == 2


def test_a_failed_turn_of_a_new_plan_is_retried_before_the_morning_checkup(
    data_dir, tmp_path, monkeypatch, clock
):
    b, db = setup(data_dir, tmp_path, monkeypatch, down=True)
    clock.set(MONDAY, 7, 0)  # today's checkup has not gone yet
    monkeypatch.setattr(db, "was_sent", lambda *a: False)
    judge(b, db, {})
    draw(b)
    assert len(turns(data_dir)) == 1
    down(data_dir, False)
    clock.tick(600)
    tick(b)  # its retry is due: it goes now, not after the checkup
    assert len(turns(data_dir)) == 2 and answers(b) == 1


def test_a_failed_turns_retry_waits_out_a_consistency_check_that_could_not_run(
    data_dir, tmp_path, monkeypatch, clock
):
    b, db = setup(data_dir, tmp_path, monkeypatch, down=True, checkup=False)
    judge(b, db, {})
    draw(b)
    assert len(turns(data_dir)) == 1
    real = b.problems

    async def could_not_run(offered=()):
        return None

    b.problems = could_not_run
    down(data_dir, False)
    clock.tick(600)
    tick(b)  # it cannot tell what the plan has to say: never "nothing"
    assert len(turns(data_dir)) == 1
    b.problems = real
    tick(b)
    assert len(turns(data_dir)) == 2 and answers(b) == 1


def test_a_failed_plan_turn_whose_problems_went_away_owes_no_turn(
    data_dir, tmp_path, monkeypatch, clock
):
    b, db = setup(data_dir, tmp_path, monkeypatch, down=True, checkup=False)
    judge(b, db, {})
    draw(b)
    nothing_sure(b)  # the household fixed it, and the check agrees
    judge(b, db, {})
    down(data_dir, False)
    clock.tick(600)
    tick(b)
    clock.tick(3600)
    tick(b)
    assert len(turns(data_dir)) == 1


def test_a_failed_turn_with_nothing_sure_waits_for_the_check_of_its_plan(
    data_dir, tmp_path, monkeypatch, clock
):
    b, db = setup(data_dir, tmp_path, monkeypatch, down=True)
    tick(b)  # the window is over: the sure problems alone, and it fails
    down(data_dir, False)
    nothing_sure(b)
    clock.tick(600)
    tick(b)  # nothing sure, and the check has not judged the plan: it waits for it
    assert len(turns(data_dir)) == 1
    judge(b, db, {"nonsense": [OVEN]})
    tick(b)  # judged, with a finding: said that day, not the next
    assert len(turns(data_dir)) == 2 and answers(b) == 1


def test_a_failed_turn_whose_plan_went_back_to_what_was_said_raises_nothing(
    data_dir, tmp_path, monkeypatch, clock
):
    b, db = setup(data_dir, tmp_path, monkeypatch, checkup=False)
    path = plan_file(data_dir)
    before = path.read_text()
    judge(b, db, {})
    draw(b)  # these problems reached the chat
    assert answers(b) == 1
    edit(b)
    judge(b, db, {"nonsense": [OVEN]})
    down(data_dir)
    draw(b)  # another plan: its turn fails
    undo(b, path, before)  # the plan goes back to what was said
    judge(b, db, {})
    down(data_dir, False)
    clock.tick(600)
    tick(b)
    assert len(turns(data_dir)) == 2 and answers(b) == 1  # never the same problems twice


def test_a_failed_plan_undone_after_its_retry_was_cancelled_is_raised(
    data_dir, tmp_path, monkeypatch, clock
):
    b, db = setup(data_dir, tmp_path, monkeypatch, down=True, checkup=False)
    path = plan_file(data_dir)
    before = path.read_text()
    nothing_sure(b)
    judge(b, db, {"nonsense": [OVEN]})  # plan A, problems X
    draw(b)  # its turn fails
    edit(b)  # fixed before the retry: the check finds nothing
    judge(b, db, {})
    down(data_dir, False)
    clock.tick(600)
    tick(b)
    assert len(turns(data_dir)) == 1
    undo(b, path, before)  # plan A again, whose problems nobody heard
    judge(b, db, {"nonsense": [OVEN]})
    draw(b)
    assert len(turns(data_dir)) == 2 and answers(b) == 1


def test_problems_a_failed_turn_never_said_are_raised_when_they_come_back(
    data_dir, tmp_path, monkeypatch, clock
):
    b, db = setup(data_dir, tmp_path, monkeypatch, down=True, checkup=False)
    nothing_sure(b)
    judge(b, db, {"nonsense": [OVEN]})  # plan A, problems X
    draw(b)  # its turn fails
    edit(b)  # fixed before the retry
    judge(b, db, {})
    down(data_dir, False)
    clock.tick(600)
    tick(b)
    assert len(turns(data_dir)) == 1
    edit(b, day=3)  # X comes back on another plan: the chat never heard it
    judge(b, db, {"nonsense": [OVEN]})
    draw(b)
    assert len(turns(data_dir)) == 2 and answers(b) == 1


# ---------------------------------------------------------------- queued, running, restarted


def test_a_plan_turn_still_queued_is_never_raised_twice_even_after_a_restart(
    data_dir, tmp_path, monkeypatch, clock
):
    b, db = setup(data_dir, tmp_path, monkeypatch, down=True)
    b.drive = parked
    tick(b)
    clock.tick(86400)
    db.mark_sent("checkup", clock.at.date(), "08:00")  # the next morning's checkup went
    tick(b)
    assert len(db.get_kv("bot.queued")["-100"]) == 1 and turns(data_dir) == []
    # a restart: the new process finds the turn in QUEUED, never raises a second one, and runs it
    b = ChatBot(b.data, db)
    tick(b)
    assert len(db.get_kv("bot.queued")["-100"]) == 1

    async def restart():
        await b.resume(None)
        await asyncio.gather(*b.tasks)

    run(restart())
    assert len(turns(data_dir)) == 1 and not db.get_kv("bot.queued")  # it failed
    down(data_dir, False)
    tick(b)  # its retry waits from its failure, not from a day ago
    assert len(turns(data_dir)) == 1
    clock.tick(600)
    tick(b)  # what the lost turn did not say is said
    assert len(turns(data_dir)) == 2 and answers(b) == 1


def test_a_plan_turn_still_queued_on_a_later_day_is_not_replaced(
    data_dir, tmp_path, monkeypatch, clock
):
    b, db = setup(data_dir, tmp_path, monkeypatch, down=True)
    b.drive = parked
    tick(b)
    row = db.get_kv("bot.queued")["-100"][0]["row"]
    edit(b)  # the next day, with another plan and other problems, its turn has still not run
    judge(b, db, {"nonsense": [OVEN]})
    next_morning(b, clock, db)
    tick(b)
    assert [x["row"] for x in db.get_kv("bot.queued")["-100"]] == [row]


BUSY = """
class Busy(Exception):
    retry_after = 600

def converse(turn, settings):
    keep = {k: turn[k] for k in ("new", "history", "context", "review")}
    with (HERE / "plan_turns.jsonl").open("a") as f:
        f.write(json.dumps(keep, default=str) + "\\n")
    if (HERE / "busy").exists():
        raise Busy("busy")
    return {"text": "The batch session moves to Wednesday.", "conversation": "c1"}
"""


def test_a_plan_turn_a_busy_provider_puts_off_is_never_raised_twice(
    data_dir, tmp_path, monkeypatch, clock
):
    b, db = setup(data_dir, tmp_path, monkeypatch)
    plugins = data_dir / "plugins"
    (plugins / "ai.py").write_text(PLUGIN + BUSY)
    (plugins / "busy").write_text("")
    woken = []

    def wake(client, chat_id, seconds):
        woken.append((chat_id, seconds))
        return asyncio.sleep(0)

    b.wake = wake
    tick(b)  # the provider is busy: the turn waits for it
    queued = db.get_kv("bot.queued")["-100"]
    assert woken and len(turns(data_dir)) == 1 and len(queued) == 1
    clock.tick(2 * 3600)
    tick(b)  # still waiting: never a second turn
    assert len(turns(data_dir)) == 1 and db.get_kv("bot.queued")["-100"] == queued
    (plugins / "busy").unlink()
    b.resting.pop(-100)  # the provider is back: what wake() does when its time comes
    drive(b)
    assert len(turns(data_dir)) == 2 and answers(b) == 1
    clock.tick(3600)
    tick(b)
    assert len(turns(data_dir)) == 2


def restarted(b, first, db):
    """The plan turn in `b` (first) or its replay in a new process."""

    async def go(bot):
        await (bot.raise_plan(None) if first else bot.resume(None))
        await asyncio.gather(*bot.tasks, return_exceptions=True)

    bot = b if first else ChatBot(b.data, db)
    run(go(bot))
    return bot


def test_a_restart_before_the_plan_answer_went_out_raises_it_again(
    data_dir, tmp_path, monkeypatch, clock
):
    b, db = setup(data_dir, tmp_path, monkeypatch)

    async def answer_text(*a, **k):  # the conversation took it, then the process stopped
        raise asyncio.CancelledError

    b.answer_text = answer_text
    restarted(b, True, db)
    assert len(turns(data_dir)) == 1 and answers(b) == 0
    b2 = restarted(b, False, db)  # the new process replays it: taken, but never sent
    assert len(turns(data_dir)) == 1
    clock.tick(600)
    tick(b2)  # what never reached the chat goes again
    assert len(turns(data_dir)) == 2 and answers(b2) == 1


def test_a_restart_once_the_plan_turn_answered_never_raises_it_again(
    data_dir, tmp_path, monkeypatch, clock
):
    b, db = setup(data_dir, tmp_path, monkeypatch)
    real = b.answer_text

    async def answer_text(*a, **k):  # the process stops once the answer went out
        await real(*a, **k)
        raise asyncio.CancelledError

    b.answer_text = answer_text
    restarted(b, True, db)
    assert len(turns(data_dir)) == 1 and answers(b) == 1 and db.get_kv("bot.queued")
    b2 = restarted(b, False, db)  # the new process replays the turn: the conversation has it
    clock.tick(600)
    tick(b2)
    next_morning(b, clock, db)
    tick(b2)
    assert len(turns(data_dir)) == 1 and answers(b2) == 0


def test_a_restart_never_takes_a_line_of_the_app_for_the_plan_answer(
    data_dir, tmp_path, monkeypatch, clock
):
    b, db = setup(data_dir, tmp_path, monkeypatch)

    async def answer_text(*a, **k):  # a reminder goes out, then the process stops
        db.add_chat(-100, "assistant", None, "Reminder: thaw the chicken.")
        raise asyncio.CancelledError

    b.answer_text = answer_text
    restarted(b, True, db)
    b2 = restarted(b, False, db)  # the reminder is no answer of the plan turn
    assert len(turns(data_dir)) == 1
    clock.tick(600)
    tick(b2)
    assert len(turns(data_dir)) == 2 and answers(b2) == 1


def test_an_earlier_turns_answer_is_not_taken_for_the_plan_answer_after_a_restart(
    data_dir, tmp_path, monkeypatch, clock
):
    from supermealplanner.bot import AI

    b, db = setup(data_dir, tmp_path, monkeypatch)
    b.drive = parked
    tick(b)
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
    assert len(turns(data_dir)) == 1 and answers(b) == 0
    b2 = ChatBot(b.data, db)  # the replay must not take "Lunch is at two." for its answer
    run(go(b2, False))
    assert len(turns(data_dir)) == 1
    clock.tick(600)
    tick(b2)
    assert len(turns(data_dir)) == 2 and answers(b2) == 1


def test_an_answer_before_a_replayed_plan_row_is_not_its_answer_after_a_restart(
    data_dir, tmp_path, monkeypatch, clock
):
    from supermealplanner.bot import AI

    b, db = setup(data_dir, tmp_path, monkeypatch)
    asked = db.add_chat(-100, "user", "luis", "qué cenamos", asked=True)
    db.add_chat(-100, "assistant", AI, "Lentils tonight.")  # that turn's answer to Luis
    b.drive = parked
    tick(b)
    row = db.get_kv("bot.queued")["-100"][0]["row"]
    # the turn that read Luis's question took the plan row too, then the process stopped
    db.set_kv("bot.conversation.-100", {"id": "group-1", "seen": asked, "done": [row]})
    b2 = ChatBot(b.data, db)

    async def resume():
        await b2.resume(None)
        return await asyncio.gather(*b2.tasks, return_exceptions=True)

    assert not [e for e in run(resume()) if isinstance(e, BaseException)]
    assert not db.get_kv("bot.queued")  # the restart replayed the row
    clock.tick(600)
    tick(b2)  # its answer never went out: it goes again
    assert len(turns(data_dir)) == 1 and answers(b2) == 1


# the plan turn's answer, or the person's when their message is new to the turn
ASKED = """
def converse(turn, settings):
    keep = {k: turn[k] for k in ("new", "history", "context", "review")}
    with (HERE / "plan_turns.jsonl").open("a") as f:
        f.write(json.dumps(keep, default=str) + "\\n")
    if any("qué cenamos" in m["content"] for m in turn["new"]):
        return {"text": "Lentils tonight.", "conversation": "c1"}
    return {"text": "The batch session moves to Wednesday.", "conversation": "c1"}
"""


def test_a_question_resumed_with_a_plan_turn_that_never_answered_does_not_answer_it(
    data_dir, tmp_path, monkeypatch, clock
):
    b, db = setup(data_dir, tmp_path, monkeypatch)
    (data_dir / "plugins" / "ai.py").write_text(PLUGIN + ASKED)

    async def answer_text(*a, **k):  # a question comes, then the process stops before the send
        b.take(-100, {"id": 7, "first_name": "Luis"}, "luis", None, "qué cenamos", True)
        raise asyncio.CancelledError

    b.answer_text = answer_text
    restarted(b, True, db)
    assert len(turns(data_dir)) == 1 and answers(b) == 0
    b2 = restarted(b, False, db)  # both go in one turn: the conversation has the plan's, unsent
    assert len(turns(data_dir)) == 2 and "Lentils tonight." in [p["text"] for p in sends(b2)]
    clock.tick(600)
    tick(b2)  # the question's answer did not raise it
    assert len(turns(data_dir)) == 3 and answers(b2) == 1


def test_a_plan_turn_with_no_kept_conversation_keeps_its_chat_working(
    data_dir, tmp_path, monkeypatch, clock
):
    b, db = setup(data_dir, tmp_path, monkeypatch)
    (data_dir / "plugins" / "ai.py").write_text(
        PLUGIN + TURNS.replace('"conversation": "c1"}', '"conversation": "c1", "kept": False}')
    )
    judge(b, db, {"nonsense": [OVEN]})
    tick(b)  # answered, and the provider kept no conversation
    assert len(turns(data_dir)) == 1 and answers(b) == 1
    assert isinstance(db.get_kv("bot.conversation.-100")["seen"], int)
    db.set_kv("verifier.plan_raised", None)  # raised again: the chat still answers
    tick(b)
    assert len(turns(data_dir)) == 2 and answers(b) == 2


def test_a_plan_turn_judged_after_a_running_turns_facts_waits_for_a_turn_of_its_own(
    data_dir, tmp_path, monkeypatch, clock
):
    b, db = setup(data_dir, tmp_path, monkeypatch)
    plugins = data_dir / "plugins"
    (plugins / "ai.py").write_text(PLUGIN + JOINS)

    async def go():
        b.take(-100, {"id": 7, "first_name": "Luis"}, "luis", None, "qué cenamos", True)
        person = asyncio.ensure_future(b.drive(None, -100))
        try:
            await reach(plugins / "waiting", person)  # its facts are built: no verdict in them
            judge(b, db, {"nonsense": [OVEN]})
            await b.raise_plan(None)  # raised for what the check found
        finally:
            (plugins / "go").write_text("")  # the provider's thread never waits forever
        await person
        await asyncio.gather(*b.tasks)

    run(go())
    assert json.loads((plugins / "took.json").read_text()) == []  # not taken by that turn
    assert len(turns(data_dir)) == 2 and answers(b) == 1  # its own turn said it
    assert OVEN in turns(data_dir)[1]["context"]
    tick(b)
    assert len(turns(data_dir)) == 2


def test_a_persons_turn_settles_no_raise(data_dir, tmp_path, monkeypatch, clock):
    """A person's turn runs alone while the plan turn is queued behind it: its answer leaves the
    raise owed, and the plan turn that follows settles it."""
    b, db = setup(data_dir, tmp_path, monkeypatch, checkup=False)
    judge(b, db, {})
    plugins = data_dir / "plugins"
    (plugins / "ai.py").write_text(PLUGIN + JOINS)
    real, ended = b.plan_answered, []

    def plan_answered(items, took):
        real(items, took)
        ended.append(("open" in b.plan_record(), any(i.review for i in items)))

    b.plan_answered = plan_answered

    async def go():
        b.take(-100, {"id": 7, "first_name": "Luis"}, "luis", None, "qué cenamos", True)
        person = asyncio.ensure_future(b.drive(None, -100))
        try:
            await reach(plugins / "waiting", person)  # Luis's turn runs alone
            await b.raise_plan(None, at_once=True)  # the plan turn queues behind it
        finally:
            (plugins / "go").write_text("")
        await person
        await asyncio.gather(*b.tasks)

    run(go())
    assert json.loads((plugins / "took.json").read_text()) == []  # Luis's turn never took it
    assert ended == [
        (True, False),
        (False, True),
    ]  # owed after Luis's answer, settled after its own
    assert len(turns(data_dir)) == 2 and turns(data_dir)[1]["review"] and answers(b) == 1
    tick(b)
    assert len(turns(data_dir)) == 2


# the plan turn's provider answers once plugins/go exists
HOLDS = """
import time
from concurrent.futures import ThreadPoolExecutor

def converse(turn, settings):
    keep = {k: turn[k] for k in ("new", "history", "context", "review")}
    with (HERE / "plan_turns.jsonl").open("a") as f:
        f.write(json.dumps(keep, default=str) + "\\n")
    (HERE / "waiting").write_text("")
    while not (HERE / "go").exists():
        time.sleep(0.01)
    if (HERE / "down").exists():
        raise RuntimeError("down")
    return {"text": "The batch session moves to Wednesday.", "conversation": "c1"}
"""


def drawn_meanwhile(b, db):
    """The app draws another plan (a new dinner) while a turn is pending, the check judges it, and
    its problems differ from the plan before."""
    before = {p.key for p in doctor.problems(b.data, db, MONDAY)}
    edit(b)
    assert {p.key for p in doctor.problems(b.data, db, MONDAY)} != before
    judge(b, db, {})


def holding(b, data_dir, during):
    """A plan turn raised as for a plan drawn, held by its provider while `during` runs."""
    plugins = data_dir / "plugins"
    (plugins / "ai.py").write_text(PLUGIN + HOLDS)

    async def go():
        await b.raise_plan(None, at_once=True)
        try:
            await reach(plugins / "waiting")  # the turn read the plan as it was
            await during()
        finally:
            (plugins / "go").write_text("")
        await asyncio.gather(*b.tasks)

    run(go())


@pytest.mark.parametrize("fails", [False, True])
def test_a_plan_drawn_while_the_plan_turn_runs_is_raised_once_it_ends(
    data_dir, tmp_path, monkeypatch, clock, fails
):
    b, db = setup(data_dir, tmp_path, monkeypatch, down=fails, checkup=False)
    judge(b, db, {})

    async def during():
        drawn_meanwhile(b, db)
        await b.raise_plan(None, at_once=True)  # never a second turn at once

    holding(b, data_dir, during)
    assert len(turns(data_dir)) == 1 and answers(b) == 0 + (not fails)
    (data_dir / "plugins" / "ai.py").write_text(PLUGIN + TURNS)
    down(data_dir, False)
    clock.tick(600 if fails else 0)
    tick(b)  # the plan drawn meanwhile, once its wait is over
    assert len(turns(data_dir)) == 2 and SALAD_COOK in turns(data_dir)[1]["context"]
    assert answers(b) == 1 + (not fails)
    tick(b)
    assert len(turns(data_dir)) == 2


def test_a_plan_drawn_while_an_unjudged_plan_turn_runs_is_raised_in_full(
    data_dir, tmp_path, monkeypatch, clock
):
    b, db = setup(data_dir, tmp_path, monkeypatch, checkup=False)
    judge(b, db, {})
    b.drive = parked
    draw(b)  # queued for plan A, judged
    edit(b, day=3, rid=HAKE)  # the turn reads a plan the check has not judged
    del b.drive
    plugins = data_dir / "plugins"
    (plugins / "ai.py").write_text(PLUGIN + HOLDS)

    async def go():
        turn = asyncio.ensure_future(b.drive(None, -100))
        try:
            await reach(plugins / "waiting", turn)
            drawn_meanwhile(b, db)  # judged with nothing found, its sure problems
            await b.raise_plan(None, at_once=True)
        finally:
            (plugins / "go").write_text("")
        await turn
        await asyncio.gather(*b.tasks)

    run(go())
    assert len(turns(data_dir)) == 1
    (plugins / "ai.py").write_text(PLUGIN + TURNS)
    tick(b)  # the plan drawn meanwhile, raised as any drawn plan
    assert len(turns(data_dir)) == 2 and answers(b) == 2


def test_a_plan_drawn_while_the_plan_turn_waits_is_raised_once(
    data_dir, tmp_path, monkeypatch, clock
):
    b, db = setup(data_dir, tmp_path, monkeypatch, checkup=False)
    judge(b, db, {})
    b.drive = parked
    draw(b)
    drawn_meanwhile(b, db)
    draw(b)  # its turn has not read anything yet
    del b.drive
    drive(b)  # the waiting turn reads the plan drawn meanwhile
    assert len(turns(data_dir)) == 1 and answers(b) == 1
    assert SALAD_COOK in turns(data_dir)[0]["context"]
    # the plan moves before the next tick (a step ticked) and is judged: the turn already raised
    # the drawn plan, and an edit is no plan the app drew
    edit(b, day=3)
    judge(b, db, {})
    tick(b)
    tick(b)
    assert len(turns(data_dir)) == 1


def test_a_drawn_plan_outlives_a_turn_that_read_a_plan_not_judged_yet(
    data_dir, tmp_path, monkeypatch, clock
):
    b, db = setup(data_dir, tmp_path, monkeypatch, checkup=False)
    judge(b, db, {})
    b.drive = parked
    draw(b)
    drawn_meanwhile(b, db)
    draw(b)
    edit(b, day=3)  # the plan moves again before the turn runs
    del b.drive
    drive(b)  # it reads a plan the check has not judged
    assert len(turns(data_dir)) == 1
    judge(b, db, {"nonsense": [OVEN]})  # judged, with a finding that turn never saw
    tick(b)
    assert len(turns(data_dir)) == 2 and answers(b) == 2 and OVEN in turns(data_dir)[1]["context"]


def test_a_drawn_plan_needs_no_turn_with_the_same_problems_or_none(
    data_dir, tmp_path, monkeypatch, clock
):
    b, db = setup(data_dir, tmp_path, monkeypatch, checkup=False)
    judge(b, db, {})
    draw(b)  # the turn answered
    assert len(turns(data_dir)) == 1
    edit(b, rid=HAKE)  # another plan, the same problems
    judge(b, db, {})
    draw(b)
    nothing_sure(b)  # another one with nothing to say
    edit(b, day=3, rid=HAKE)
    judge(b, db, {})
    draw(b)
    assert len(turns(data_dir)) == 1


def test_a_drawn_plan_already_raised_needs_no_turn(data_dir, tmp_path, monkeypatch, clock):
    b, db = setup(data_dir, tmp_path, monkeypatch)
    judge(b, db, {})
    tick(b)  # the turn answered
    draw(b)  # the same plan drawn
    assert len(turns(data_dir)) == 1


def test_a_plan_drawn_late_at_night_is_raised_after_midnight_with_no_morning_checkup(
    data_dir, tmp_path, monkeypatch, clock
):
    b, db = setup(data_dir, tmp_path, monkeypatch, checkup=False)
    judge(b, db, {})
    clock.set(MONDAY, 23, 50)

    async def during():
        clock.set(MONDAY, 23, 58)
        drawn_meanwhile(b, db)  # drawn before midnight while that turn runs
        await b.raise_plan(None, at_once=True)
        clock.set(MONDAY + dt.timedelta(days=1), 0, 5)

    holding(b, data_dir, during)
    (data_dir / "plugins" / "ai.py").write_text(PLUGIN + TURNS)
    tick(b)
    assert len(turns(data_dir)) == 2 and answers(b) == 2


@pytest.mark.parametrize("checkup", [False, True])
def test_a_plan_drawn_meanwhile_waits_for_its_check_when_the_turn_moved_the_plan(
    data_dir, tmp_path, monkeypatch, clock, checkup
):
    b, db = setup(data_dir, tmp_path, monkeypatch, checkup=checkup)
    judge(b, db, {})

    async def during():
        drawn_meanwhile(b, db)
        await b.raise_plan(None, at_once=True)
        edit(b, day=3)  # then the plan moves again before the turn ends: not judged yet

    holding(b, data_dir, during)
    (data_dir / "plugins" / "ai.py").write_text(PLUGIN + TURNS)
    tick(b)
    # the check has not judged the plan as it is now: it waits, even after the morning's window
    assert len(turns(data_dir)) == 1
    judge(b, db, {})
    tick(b)
    assert len(turns(data_dir)) == 2 and answers(b) == 2


def test_a_plan_drawn_that_moves_while_its_problems_are_gathered_is_still_raised(
    data_dir, tmp_path, monkeypatch, clock
):
    b, db = setup(data_dir, tmp_path, monkeypatch, checkup=False)
    judge(b, db, {"nonsense": [OVEN]})
    real = b.problems

    async def moving(offered=()):  # a step is ticked while the consistency check runs
        b.problems = real
        edit(b)
        return await real()

    b.problems = moving
    draw(b)  # decided on the next tick
    assert turns(data_dir) == []
    judge(b, db, {"nonsense": [OVEN]})  # the plan as it is now
    tick(b)
    assert len(turns(data_dir)) == 1 and answers(b) == 1
    tick(b)
    assert len(turns(data_dir)) == 1


# ---------------------------------------------------------------- what the check has judged


def test_a_late_retry_waits_for_the_check_of_a_plan_that_moved(
    data_dir, tmp_path, monkeypatch, clock
):
    b, db = setup(data_dir, tmp_path, monkeypatch, down=True)  # the window is over
    nothing_sure(b)
    judge(b, db, {"nonsense": [OVEN]})
    tick(b)  # the turn fails
    edit(b)  # a step ticked: the check cannot judge it yet
    down(data_dir, False)
    clock.tick(600)
    tick(b)  # the retry is due, and waits for the check
    assert len(turns(data_dir)) == 1
    judge(b, db, {"nonsense": [TUESDAY_OVEN]})
    tick(b)
    assert len(turns(data_dir)) == 2 and answers(b) == 1


def test_a_drawn_plan_raised_late_waits_for_its_check(data_dir, tmp_path, monkeypatch, clock):
    b, db = setup(data_dir, tmp_path, monkeypatch)  # the window is over
    nothing_sure(b)
    judge(b, db, {"nonsense": [OVEN]})
    tick(b)  # the turn answered
    edit(b)  # a plan drawn, before the check judged it
    draw(b)
    assert len(turns(data_dir)) == 1
    judge(b, db, {"nonsense": [TUESDAY_OVEN]})
    tick(b)
    assert len(turns(data_dir)) == 2 and answers(b) == 2


def test_a_morning_raise_after_the_window_waits_for_the_check_of_an_unjudged_plan(
    data_dir, tmp_path, monkeypatch, clock
):
    b, db = setup(data_dir, tmp_path, monkeypatch)  # the window is over
    nothing_sure(b)
    judge(b, db, {"nonsense": [OVEN]})
    tick(b)
    assert len(turns(data_dir)) == 1
    edit(b)  # the plan changes overnight, and the check has not judged it
    next_morning(b, clock, db)
    tick(b)  # nothing sure to say: it waits for the check
    assert len(turns(data_dir)) == 1
    judge(b, db, {"nonsense": [TUESDAY_OVEN]})
    tick(b)  # judged: what it found goes that day
    assert len(turns(data_dir)) == 2 and answers(b) == 2


def test_a_morning_raise_that_waited_for_an_unjudged_plan_never_repeats_the_same_problems(
    data_dir, tmp_path, monkeypatch, clock
):
    b, db = setup(data_dir, tmp_path, monkeypatch)  # the window is over
    nothing_sure(b)
    judge(b, db, {"nonsense": [OVEN]})
    tick(b)
    edit(b)  # the plan changes overnight, and the check has not judged it
    next_morning(b, clock, db)
    tick(b)
    judge(b, db, {"nonsense": [OVEN]})  # judged: the same problems as the turn already raised
    tick(b)
    next_morning(b, clock, db)
    tick(b)
    assert len(turns(data_dir)) == 1 and answers(b) == 1


def test_a_retry_whose_plan_moved_waits_for_the_check_and_says_its_findings(
    data_dir, tmp_path, monkeypatch, clock
):
    b, db = setup(data_dir, tmp_path, monkeypatch, down=True)  # the window is over
    judge(b, db, {"nonsense": [OVEN]})
    tick(b)  # its turn fails
    edit(b)  # a step ticked: the check has not judged the plan yet
    down(data_dir, False)
    clock.tick(600)
    tick(b)  # the retry is due, and waits for the check
    assert len(turns(data_dir)) == 1
    judge(b, db, {"nonsense": [OVEN]})
    tick(b)  # the same day, with what the check found
    assert len(turns(data_dir)) == 2 and answers(b) == 1 and OVEN in turns(data_dir)[1]["context"]
    next_morning(b, clock, db)
    tick(b)  # said: never again
    assert len(turns(data_dir)) == 2


def test_a_retry_whose_check_never_ends_goes_with_the_sure_problems_and_leaves_the_findings(
    data_dir, tmp_path, monkeypatch, clock
):
    monkeypatch.setattr(bot_module, "PLAN_CHECK_WAIT_S", 0)  # the check never ended in time
    b, db = setup(data_dir, tmp_path, monkeypatch, down=True)  # the window is over
    judge(b, db, {"nonsense": [OVEN]})
    tick(b)  # its turn fails
    edit(b)
    down(data_dir, False)
    clock.tick(600)
    tick(b)  # the retry goes with the sure problems alone
    assert len(turns(data_dir)) == 2 and answers(b) == 1
    judge(b, db, {"nonsense": [OVEN]})  # judged: what the check found was not said yet
    tick(b)
    next_morning(b, clock, db)
    tick(b)
    assert len(turns(data_dir)) == 3 and answers(b) == 2


def test_a_retry_whose_plan_moved_goes_after_its_wait_with_no_morning_checkup(
    data_dir, tmp_path, monkeypatch, clock
):
    from supermealplanner.bot import PLAN_CHECK_WAIT_S

    b, db = setup(data_dir, tmp_path, monkeypatch, down=True)  # the window is over
    judge(b, db, {"nonsense": [OVEN]})
    tick(b)  # its turn fails
    edit(b)  # a step ticked: the check of the plan as it is never ends
    down(data_dir, False)
    b.data.settings.bot.checkup_at = ""
    clock.tick(600)
    tick(b)  # the retry is due, and waits for the check
    assert len(turns(data_dir)) == 1
    clock.tick(PLAN_CHECK_WAIT_S)  # the wait is over
    tick(b)  # with the sure problems alone, though no checkup runs
    assert len(turns(data_dir)) == 2 and answers(b) == 1
    judge(b, db, {"nonsense": [OVEN]})  # judged at last: what it found is still owed
    tick(b)
    assert len(turns(data_dir)) == 3 and answers(b) == 2
    tick(b)
    assert len(turns(data_dir)) == 3


def test_a_retry_whose_plan_moved_overnight_still_owes_the_checks_findings(
    data_dir, tmp_path, monkeypatch, clock
):
    b, db = setup(data_dir, tmp_path, monkeypatch, down=True)  # the window is over
    judge(b, db, {"nonsense": [OVEN]})
    tick(b)  # its turn fails
    b.data.settings.bot.checkup_at = ""
    clock.tick(86400)  # longer ago than the wait for the check
    down(data_dir, False)
    edit(b)  # the plan as it is today, not judged yet
    tick(b)  # with the sure problems alone
    assert len(turns(data_dir)) == 2 and answers(b) == 1
    judge(b, db, {"nonsense": [OVEN]})
    tick(b)  # what the check found was still owed
    assert len(turns(data_dir)) == 3 and answers(b) == 2


def test_a_plan_owed_only_its_checks_findings_says_no_sure_problem_again(
    data_dir, tmp_path, monkeypatch, clock
):
    monkeypatch.setattr(bot_module, "PLAN_CHECK_WAIT_S", 0)  # the check never ended in time
    b, db = setup(data_dir, tmp_path, monkeypatch, down=True)  # the window is over
    judge(b, db, {"nonsense": [OVEN]})
    tick(b)  # its turn fails
    edit(b)
    b.data.settings.bot.checkup_at = ""
    down(data_dir, False)
    clock.tick(600)
    tick(b)  # with the sure problems alone: what the check finds is owed
    assert len(turns(data_dir)) == 2 and answers(b) == 1
    real = b.problems

    async def one_fixed(offered=()):  # the answer fixed one of the sure problems
        listed = await real()
        first = next(p for p in listed if p.kind == "off_day")
        return [p for p in listed if p is not first]

    b.problems = one_fixed
    judge(b, db, {})  # the check finds nothing
    tick(b)
    clock.tick(3600)
    tick(b)
    assert len(turns(data_dir)) == 2


def test_a_failed_turn_whose_plan_came_back_unjudged_waits_for_its_check(
    data_dir, tmp_path, monkeypatch, clock
):
    b, db = setup(data_dir, tmp_path, monkeypatch, down=True, checkup=False)
    path = plan_file(data_dir)
    before = path.read_text()
    judge(b, db, {"nonsense": [OVEN]})
    draw(b)  # its turn fails
    edit(b)
    judge(b, db, {})  # the other plan judged: the check's verdict is of it
    undo(b, path, before)  # back to the failed turn's plan, its verdict no longer kept
    down(data_dir, False)
    clock.tick(600)
    tick(b)  # the check may still judge it: its retry waits
    assert len(turns(data_dir)) == 1
    judge(b, db, {"nonsense": [OVEN]})
    tick(b)
    assert len(turns(data_dir)) == 2 and answers(b) == 1 and OVEN in turns(data_dir)[1]["context"]


def test_a_retry_that_went_without_the_check_and_failed_goes_again_with_no_morning_checkup(
    data_dir, tmp_path, monkeypatch, clock
):
    monkeypatch.setattr(bot_module, "PLAN_CHECK_WAIT_S", 0)  # the check never ends in time
    b, db = setup(data_dir, tmp_path, monkeypatch, down=True)  # the window is over
    judge(b, db, {"nonsense": [OVEN]})
    tick(b)  # its turn fails
    edit(b)  # the check of the plan as it is never ends
    b.data.settings.bot.checkup_at = ""
    clock.tick(600)
    tick(b)  # the retry goes with the sure problems, and fails too
    assert len(turns(data_dir)) == 2 and answers(b) == 0
    clock.tick(1800)
    tick(b)  # its own retry, still with no verdict, fails too
    assert len(turns(data_dir)) == 3 and answers(b) == 0
    down(data_dir, False)
    clock.tick(3600)
    tick(b)
    assert len(turns(data_dir)) == 4 and answers(b) == 1


@pytest.mark.parametrize("judged_before", [True, False])
def test_a_retry_from_the_evening_before_waits_for_the_next_mornings_check(
    data_dir, tmp_path, monkeypatch, clock, judged_before
):
    b, db = setup(data_dir, tmp_path, monkeypatch, down=True)
    if judged_before:
        judge(b, db, {"nonsense": [OVEN]})
    clock.set(MONDAY, 20)
    tick(b)  # its turn fails in the evening
    monkeypatch.setattr(bot_module, "PLAN_RAISE_H", 2)  # the next morning, just after the checkup
    next_morning(b, clock, db, hour=8)
    down(data_dir, False)
    if judged_before:
        edit(b)  # the plan as it is that morning: its check has not ended
    tick(b)
    assert len(turns(data_dir)) == 1
    judge(b, db, {"nonsense": [OVEN]})
    tick(b)  # with what the check found
    assert len(turns(data_dir)) == 2 and answers(b) == 1 and OVEN in turns(data_dir)[1]["context"]


def test_a_plan_turn_that_read_a_plan_the_check_had_not_judged_leaves_it_to_be_raised(
    data_dir, tmp_path, monkeypatch, clock
):
    b, db = setup(data_dir, tmp_path, monkeypatch)
    judge(b, db, {})
    b.drive = parked
    tick(b)
    edit(b)  # the plan changes before the turn runs
    del b.drive
    drive(b)  # it reads the new plan, which the check has not judged yet
    assert len(turns(data_dir)) == 1
    judge(b, db, {"nonsense": [OVEN]})  # judged, with a finding that turn never saw
    tick(b)
    assert len(turns(data_dir)) == 2 and OVEN in turns(data_dir)[1]["context"]


def test_a_plan_turn_says_none_of_a_verdict_that_came_while_its_facts_were_built(
    data_dir, tmp_path, monkeypatch, clock
):
    from supermealplanner import context

    b, db = setup(data_dir, tmp_path, monkeypatch)
    b.drive = parked
    tick(b)  # the window is over: queued with the sure problems alone
    facts, verdicts = context.facts, []

    def facts_then_verdict(*a, **k):  # built with no verdict; then the check ends
        out = facts(*a, **k)
        if not verdicts:
            verdicts.append(judge(b, db, {"nonsense": [OVEN]}))
        return out

    monkeypatch.setattr(context, "facts", facts_then_verdict)
    del b.drive
    drive(b)
    assert verdicts and len(turns(data_dir)) == 1 and OVEN not in turns(data_dir)[0]["context"]
    tick(b)  # what the check found was never said
    assert len(turns(data_dir)) == 2 and OVEN in turns(data_dir)[1]["context"]


def test_a_plan_turn_shows_no_verdict_that_came_while_its_facts_were_built(
    data_dir, tmp_path, monkeypatch, clock
):
    from supermealplanner import context

    b, db = setup(data_dir, tmp_path, monkeypatch)
    b.drive = parked
    tick(b)  # the window is over: queued with the sure problems alone
    problems_now, verdicts = context.problems_now, []

    def verdict_then_problems(*a, **k):  # the check ends while the facts are built
        if not verdicts:
            verdicts.append(judge(b, db, {"nonsense": [OVEN]}))
        return problems_now(*a, **k)

    monkeypatch.setattr(context, "problems_now", verdict_then_problems)
    del b.drive
    drive(b)
    assert verdicts and OVEN not in turns(data_dir)[0]["context"]  # the turn could not say it
    tick(b)  # what the check found is said now
    assert len(turns(data_dir)) == 2 and OVEN in turns(data_dir)[1]["context"]


def test_what_a_plan_turn_adds_after_its_facts_were_gathered_is_raised_later(
    data_dir, tmp_path, monkeypatch, clock
):
    from supermealplanner import context

    b, db = setup(data_dir, tmp_path, monkeypatch, checkup=False)
    judge(b, db, {})
    facts, moves = context.facts, []

    def facts_then_move(*a, **k):  # the facts are of plan A; then the plan moves to B, judged
        out = facts(*a, **k)
        if not moves:
            moves.append(drawn_meanwhile(b, db))
        return out

    monkeypatch.setattr(context, "facts", facts_then_move)
    draw(b)
    assert moves and len(turns(data_dir)) == 1 and SALAD_COOK not in turns(data_dir)[0]["context"]
    monkeypatch.setattr(context, "facts", facts)
    draw(b)  # B is drawn: what it adds was never said
    assert len(turns(data_dir)) == 2 and SALAD_COOK in turns(data_dir)[1]["context"]


def test_a_plan_turn_whose_plan_moved_with_the_same_problems_counts_them_as_said(
    data_dir, tmp_path, monkeypatch, clock
):
    b, db = setup(data_dir, tmp_path, monkeypatch, checkup=False)
    judge(b, db, {})
    b.drive = parked
    draw(b)  # queued for plan A
    edit(b, rid=HAKE)  # plan B, the same problems, not judged yet
    del b.drive
    drive(b)
    assert len(turns(data_dir)) == 1 and answers(b) == 1
    judge(b, db, {})  # B judged: nothing the turn did not say
    draw(b)
    assert len(turns(data_dir)) == 1


def test_a_plan_turn_that_read_another_plan_counts_what_its_facts_showed(
    data_dir, tmp_path, monkeypatch, clock
):
    b, db = setup(data_dir, tmp_path, monkeypatch, checkup=False)
    path = plan_file(data_dir)
    before = path.read_text()
    judge(b, db, {})
    b.drive = parked
    draw(b)  # queued for plan A
    edit(b)  # plan B, A's problems and the salad's, before the turn reads it
    del b.drive
    drive(b)
    assert len(turns(data_dir)) == 1 and answers(b) == 1
    undo(b, path, before)  # back to plan A: every problem of it was in B's facts
    judge(b, db, {})
    draw(b)
    assert len(turns(data_dir)) == 1


def test_a_private_checkup_chat_is_never_raised_what_it_cannot_see(
    data_dir, tmp_path, monkeypatch, clock
):
    b, db = setup(data_dir, tmp_path, monkeypatch)
    cfg = b.data.settings.bot
    ana = cfg.people["ana"]
    cfg.checkup_chat = ana
    health = "Luis gets the salt he must avoid"
    judge(b, db, {"health": [health]})
    tick(b)  # the turn answers in ana's chat, which never sees health findings
    assert len(turns(data_dir)) == 1 and answers(b, ana) == 1
    edit(b, rid=HAKE)  # another plan, the same problems
    judge(b, db, {"health": [health]})
    next_morning(b, clock, db)
    tick(b)  # nothing it did not say
    assert len(turns(data_dir)) == 1


def test_a_retry_whose_plan_moves_while_its_problems_are_gathered_waits_for_the_next_tick(
    data_dir, tmp_path, monkeypatch, clock
):
    b, db = setup(data_dir, tmp_path, monkeypatch, down=True)  # the window is over
    judge(b, db, {"nonsense": [OVEN]})
    tick(b)  # its turn fails
    down(data_dir, False)
    clock.tick(600)
    real = b.problems

    async def moving(offered=()):  # a step is ticked while the consistency check runs
        b.problems = real
        edit(b)
        return await real()

    b.problems = moving
    tick(b)  # neither said with the check's findings left out, nor settled
    assert len(turns(data_dir)) == 1
    judge(b, db, {"nonsense": [OVEN]})
    tick(b)
    assert len(turns(data_dir)) == 2 and OVEN in turns(data_dir)[1]["context"]


def test_a_late_retry_never_counts_the_failed_plan_as_raised(
    data_dir, tmp_path, monkeypatch, clock
):
    monkeypatch.setattr(bot_module, "PLAN_CHECK_WAIT_S", 0)  # the check never ended in time
    b, db = setup(data_dir, tmp_path, monkeypatch, down=True)  # the window is over
    path = plan_file(data_dir)
    before = path.read_text()
    judge(b, db, {"nonsense": [OVEN]})
    tick(b)  # its turn fails
    edit(b)  # a step ticked: the check has not judged the plan yet
    down(data_dir, False)
    clock.tick(600)
    tick(b)  # the retry goes with the sure problems alone
    assert len(turns(data_dir)) == 2 and answers(b) == 1
    undo(b, path, before)  # the failed plan again, whose finding nobody heard
    judge(b, db, {"nonsense": [OVEN]})
    next_morning(b, clock, db)
    tick(b)
    assert len(turns(data_dir)) == 3 and answers(b) == 2 and OVEN in turns(data_dir)[2]["context"]


def test_a_failed_raise_of_an_unjudged_plan_keeps_what_reached_the_chat(
    data_dir, tmp_path, monkeypatch, clock
):
    b, db = setup(data_dir, tmp_path, monkeypatch)  # the window is over
    path = plan_file(data_dir)
    before = path.read_text()
    judge(b, db, {"nonsense": [OVEN]})
    tick(b)  # plan A reaches the chat
    edit(b)  # the next day the plan is B, which the check has not judged
    down(data_dir)
    next_morning(b, clock, db)
    tick(b)  # its late raise goes with the sure problems, and fails
    assert len(turns(data_dir)) == 2
    undo(b, path, before)  # undone: plan A again, which the chat already heard
    judge(b, db, {"nonsense": [OVEN]})
    down(data_dir, False)
    clock.tick(600)
    tick(b)  # its retry is due: nothing is owed
    next_morning(b, clock, db)
    tick(b)
    assert len(turns(data_dir)) == 2 and answers(b) == 1


def test_what_a_plan_turn_said_counts_its_sure_problems_and_only_the_findings_it_raises(
    data_dir, tmp_path, monkeypatch, clock
):
    b, db = setup(data_dir, tmp_path, monkeypatch)  # the window is over
    judge(b, db, {"nonsense": [OVEN]})
    b.drive = parked
    tick(b)
    del b.drive
    edit(b)  # before its turn runs, plan B is judged: other problems, and a wish
    judge(b, db, {"nonsense": [TUESDAY_OVEN], "wishes": ["More fish"]})
    drive(b)  # it reads B and says it
    assert len(turns(data_dir)) == 1
    edit(b, day=3, rid=HAKE)  # the next day: the same problems, another wish
    judge(b, db, {"nonsense": [TUESDAY_OVEN], "wishes": ["Less rice"]})
    next_morning(b, clock, db)
    tick(b)
    assert len(turns(data_dir)) == 1


@pytest.mark.parametrize("comes_back", ["what_it_said", "what_was_queued"])
def test_a_plan_turn_that_said_another_plan_keeps_what_it_said(
    data_dir, tmp_path, monkeypatch, clock, comes_back
):
    b, db = setup(data_dir, tmp_path, monkeypatch)  # the window is over
    nothing_sure(b)
    judge(b, db, {"nonsense": [OVEN]})  # plan A, problems X
    b.drive = parked
    tick(b)
    del b.drive
    edit(b)  # before its turn runs, plan B is drawn and judged, with problems Y
    judge(b, db, {"nonsense": [TUESDAY_OVEN]})
    drive(b)  # it reads B and says Y
    assert len(turns(data_dir)) == 1
    edit(b, day=3)  # the next day the plan moves again: Y again, or X, which the chat never heard
    judge(b, db, {"nonsense": [TUESDAY_OVEN if comes_back == "what_it_said" else OVEN]})
    next_morning(b, clock, db)
    tick(b)
    if comes_back == "what_it_said":  # Y again: already said
        assert len(turns(data_dir)) == 1
    else:
        assert len(turns(data_dir)) == 2 and answers(b) == 2


# ---------------------------------------------------------------- the record 0.49.2 left


def test_the_record_0_49_2_left_neither_crashes_nor_doubles_its_queued_turn(
    data_dir, tmp_path, monkeypatch, clock
):
    b, db = setup(data_dir, tmp_path, monkeypatch, down=True)
    b.drive = parked
    tick(b)
    row = db.get_kv("bot.queued")["-100"][0]["row"]
    old = {
        "key": "k",
        "found": "f",
        "on": MONDAY.isoformat(),
        "row": row,
        "at": 0,
        "tries": 2,
        "pending": {"key": "k", "found": "f", "judged": True},
        "drawn": MONDAY.isoformat(),
        "owed": True,
        "read": "k",
        "read_found": "f",
        "read_judged": True,
    }
    db.set_kv("verifier.plan_raised", old)  # as 0.49.2 wrote it, its turn still queued
    tick(b)
    judge(b, db, {})
    draw(b)  # a plan drawn meanwhile: its raise waits for that turn
    assert [x["row"] for x in db.get_kv("bot.queued")["-100"]] == [row]  # never a second
    del b.drive
    down(data_dir, False)
    drive(b)  # it runs, and its answer reaches the chat: what its facts showed was said
    assert len(turns(data_dir)) == 1 and answers(b) == 1
    next_morning(b, clock, db)
    tick(b)
    assert len(turns(data_dir)) == 1


def test_the_morning_raise_comes_once_a_day_never_for_each_edit(
    data_dir, tmp_path, monkeypatch, clock
):
    """The morning's turn said everything the check judged: an edit from the chat later that day,
    with a problem of its own, reaches the AI's facts but raises nothing until the next morning."""
    b, db = setup(data_dir, tmp_path, monkeypatch)
    judge(b, db, {})
    tick(b)
    assert len(turns(data_dir)) == 1 and answers(b) == 1
    edit(b)  # the salad brings a cook nobody does
    judge(b, db, {})
    for _ in range(3):
        clock.tick(3600)
        tick(b)
    assert len(turns(data_dir)) == 1
    next_morning(b, clock, db)
    tick(b)
    assert len(turns(data_dir)) == 2 and SALAD_COOK in turns(data_dir)[1]["context"]


def test_two_raises_at_the_same_time_queue_one_plan_turn(data_dir, tmp_path, monkeypatch, clock):
    b, db = setup(data_dir, tmp_path, monkeypatch)
    b.drive = parked

    async def both():
        await asyncio.gather(b.raise_plan(None), b.raise_plan(None))

    run(both())
    assert len(db.get_kv("bot.queued")["-100"]) == 1


# ---------------------------------------------------------------- the second review of #143


def plan_note(b, turn: dict) -> str:
    """What the plan turn was told is new (plan_check_new), or "" when it was told nothing."""
    head = b.t("plan_check_new", items="").split("\n")[0]
    text = new_text(turn)
    return text.split(head, 1)[1] if head in text else ""


def plan_rows(b, db, chat=-100) -> list[str]:
    """The plan check's rows in the chat's history, as the next turns read them."""
    marks = {b.t("plan_check_turn"), b.t("plan_check_turn_direct")}
    return [
        r["text"] for r in db.chat_rows(chat, 200) if r["role"] == "user" and r["text"] in marks
    ]


def test_the_new_problems_note_comes_from_the_facts_the_turn_shows(
    data_dir, tmp_path, monkeypatch, clock
):
    """A turn queued to say X waited while the plan moved to one with Y instead: it was told only X
    was new, and Y counted as heard though nobody named it. The note is built when the facts are,
    and the chat's history keeps only the plan check's own line, never a finding."""
    b, db = setup(data_dir, tmp_path, monkeypatch)  # the window is over
    path = plan_file(data_dir)
    before = path.read_text()
    judge(b, db, {"nonsense": [OVEN]})
    tick(b)  # the sessions off their days and OVEN reach the chat
    assert answers(b) == 1
    edit(b)  # the next day the plan has a cook nobody does (X)
    judge(b, db, {"nonsense": [OVEN]})
    next_morning(b, clock, db)
    b.drive = parked
    tick(b)  # queued to say X
    del b.drive
    undo(b, path, before)  # before it runs, X is gone and the check finds Y
    judge(b, db, {"nonsense": [OVEN, TUESDAY_OVEN]})
    drive(b)
    note = plan_note(b, turns(data_dir)[1])
    assert TUESDAY_OVEN in note and note.count("•") == 1 and answers(b) == 2
    assert len(plan_rows(b, db)) == 2  # the rows say nothing of what was found


def test_a_turn_before_the_check_judged_its_plan_keeps_the_findings_the_chat_heard(
    data_dir, tmp_path, monkeypatch, clock
):
    """A turn whose plan the check had not judged showed no finding: the findings the chat heard
    before still count as heard, so the check finding them again raises nothing."""
    b, db = setup(data_dir, tmp_path, monkeypatch)  # the window is over
    judge(b, db, {"nonsense": [OVEN]})
    tick(b)  # OVEN reaches the chat
    edit(b)  # overnight the plan moves, and the check has not judged it
    next_morning(b, clock, db)
    tick(b)  # the cook nobody does goes, with the sure problems alone
    assert len(turns(data_dir)) == 2 and OVEN not in turns(data_dir)[1]["context"]
    judge(b, db, {"nonsense": [OVEN]})  # judged: OVEN, which the chat heard the day before
    tick(b)
    assert len(turns(data_dir)) == 2 and answers(b) == 2


@pytest.mark.parametrize("left", ["row", "drawn", "pending"])
def test_what_0_49_2_still_owed_is_raised_after_the_upgrade(
    data_dir, tmp_path, monkeypatch, clock, left
):
    """0.49.2 left a raise owed (a failed turn waiting for its retry, a plan drawn, a turn pending):
    with no morning checkup nothing else would ever raise it."""
    b, db = setup(data_dir, tmp_path, monkeypatch, checkup=False)
    judge(b, db, {})
    old = {"key": "k", "found": "f", "on": MONDAY.isoformat(), "at": 0, "tries": 1}
    old |= {
        "row": {"row": 12345},  # its row is in no queue: the turn failed
        "drawn": {"drawn": MONDAY.isoformat(), "owed": True},
        "pending": {"pending": {"key": "k", "found": "f", "judged": True}},
    }[left]
    db.set_kv("verifier.plan_raised", old)
    tick(b)
    assert len(turns(data_dir)) == 1 and answers(b) == 1


def test_a_turn_0_49_2_queued_in_the_group_counts_for_the_group(
    data_dir, tmp_path, monkeypatch, clock
):
    """The checkup moved to a person's own chat after 0.49.2 queued its turn in the group: that
    turn's answer is the group's, never that person's."""
    b, db = setup(data_dir, tmp_path, monkeypatch)
    cfg = b.data.settings.bot
    b.drive = parked
    tick(b)  # queued in the group
    row = db.get_kv("bot.queued")["-100"][0]["row"]
    db.set_kv("verifier.plan_raised", {"on": MONDAY.isoformat(), "row": row, "at": 0, "tries": 1})
    ana = cfg.people["ana"]
    cfg.checkup_chat = ana
    del b.drive
    drive(b)  # it answers in the group
    assert answers(b, -100) == 1
    next_morning(b, clock, db)
    tick(b)  # ana never heard it
    assert answers(b, ana) == 1


def test_a_list_telegram_refused_is_sent_again_after_its_wait(
    data_dir, tmp_path, monkeypatch, clock
):
    from supermealplanner.bot import TelegramError

    b, _db = setup(data_dir, tmp_path, monkeypatch)
    b.data.settings.bot.checkup_chat = 99  # a private chat of no one: the list, never a turn
    real, refused = b.call, []

    async def call(client, method, _tries=0, **params):
        if method == "sendMessage" and params.get("chat_id") == 99 and not refused:
            refused.append(method)
            raise TelegramError(method, "Bad Gateway", 502)
        return await real(client, method, _tries, **params)

    b.call = call
    tick(b)
    assert refused and sends(b, 99) == []
    tick(b)
    assert sends(b, 99) == []  # it waits, as a failed turn does
    clock.tick(600)
    tick(b)
    tick(b)
    assert len(sends(b, 99)) == 1


def test_a_verdict_that_comes_at_night_waits_for_the_mornings_checkup(
    data_dir, tmp_path, monkeypatch, clock
):
    """Monday's turn went with the sure problems alone; what the check finds at night goes after
    Tuesday's checkup, never at night."""
    b, db = setup(data_dir, tmp_path, monkeypatch)  # the window is over
    tick(b)
    assert len(turns(data_dir)) == 1 and answers(b) == 1
    tuesday = MONDAY + dt.timedelta(days=1)
    clock.set(tuesday, 0, 5)
    judge(b, db, {"nonsense": [OVEN]})
    tick(b)
    clock.set(tuesday, 2, 0)
    tick(b)
    assert len(turns(data_dir)) == 1
    clock.set(tuesday, 8, 5)
    db.mark_sent("checkup", tuesday, "08:00")
    tick(b)
    assert len(turns(data_dir)) == 2 and OVEN in turns(data_dir)[1]["context"]


def test_a_turn_queued_before_midnight_that_failed_after_it_waits_for_the_checkup(
    data_dir, tmp_path, monkeypatch, clock
):
    b, db = setup(data_dir, tmp_path, monkeypatch, down=True)
    judge(b, db, {})
    clock.set(MONDAY, 23, 55)
    b.drive = parked
    draw(b)  # queued before midnight
    del b.drive
    tuesday = MONDAY + dt.timedelta(days=1)
    clock.set(tuesday, 0, 5)
    drive(b)  # it fails after midnight
    down(data_dir, False)
    clock.set(tuesday, 0, 15)
    tick(b)
    assert len(turns(data_dir)) == 1
    clock.set(tuesday, 8, 5)
    db.mark_sent("checkup", tuesday, "08:00")
    tick(b)
    assert len(turns(data_dir)) == 2 and answers(b) == 1


def test_an_edit_while_the_check_is_awaited_raises_nothing_when_it_finds_nothing(
    data_dir, tmp_path, monkeypatch, clock
):
    """The morning turn went with the sure problems alone, so only what the check finds is still
    owed: an edit from the chat meanwhile waits for the next morning like any edit."""
    b, db = setup(data_dir, tmp_path, monkeypatch)  # the window is over
    tick(b)
    assert len(turns(data_dir)) == 1
    clock.set(MONDAY, 14)
    edit(b)  # an edit from the chat: a cook nobody does
    judge(b, db, {})  # the check finds nothing
    tick(b)
    assert len(turns(data_dir)) == 1
    next_morning(b, clock, db)
    tick(b)
    assert len(turns(data_dir)) == 2 and SALAD_COOK in turns(data_dir)[1]["context"]


@pytest.mark.parametrize("scope", ["asked", "all"])
def test_a_plan_turn_whose_answer_was_silence_is_raised_again(
    data_dir, tmp_path, monkeypatch, clock, scope
):
    """With answer_scope all a silent answer puts a 👀 on its lines: that is no answer either."""
    b, _db = setup(data_dir, tmp_path, monkeypatch)
    b.data.settings.bot.answer_scope = scope
    reply = data_dir / "plugins" / "reply.txt"
    reply.write_text("[no reply]")
    tick(b)
    assert len(turns(data_dir)) == 1 and sends(b, -100) == []  # nothing reached the chat
    reply.unlink()
    tick(b)
    assert len(turns(data_dir)) == 1  # its retry waits
    clock.tick(600)
    tick(b)
    assert len(turns(data_dir)) == 2 and answers(b) == 1


def test_a_plan_turn_whose_silent_answer_proposed_a_step_is_raised_again(
    data_dir, tmp_path, monkeypatch, clock
):
    """Nobody asked the plan check for a step: it is dropped, never a "members only" notice that
    would count as the plan's problems said."""
    b, _db = setup(data_dir, tmp_path, monkeypatch)
    reply = data_dir / "plugins" / "reply.txt"
    reply.write_text("[no reply]")
    offers = data_dir / "plugins" / "proposals.json"
    offers.write_text(json.dumps([{"id": "j1:1", "summary": "Open a PR.", "confirm_by": "none"}]))
    tick(b)
    assert len(turns(data_dir)) == 1 and sends(b, -100) == []  # nothing reached the chat
    reply.unlink()
    offers.unlink()
    clock.tick(600)
    tick(b)
    assert len(turns(data_dir)) == 2 and answers(b) == 1


def test_a_queued_turn_left_with_nothing_new_is_told_so_and_counts_no_failure(
    data_dir, tmp_path, monkeypatch, clock
):
    b, db = setup(data_dir, tmp_path, monkeypatch, checkup=False)
    path = plan_file(data_dir)
    before = path.read_text()
    judge(b, db, {})
    draw(b)  # the sure problems reach the chat
    edit(b)
    judge(b, db, {})
    b.drive = parked
    draw(b)  # queued for the cook nobody does
    del b.drive
    undo(b, path, before)  # before it runs, the plan is back to what the chat heard
    judge(b, db, {})
    (data_dir / "plugins" / "reply.txt").write_text("[no reply]")
    drive(b)
    assert len(turns(data_dir)) == 2 and answers(b) == 1
    assert b.t("plan_check_heard") in new_text(turns(data_dir)[1])
    assert not (db.get_kv("verifier.plan_raised") or {}).get("fails")  # not a failed try
    clock.tick(600)
    tick(b)
    assert len(turns(data_dir)) == 2


def test_a_plan_drawn_at_night_after_a_settled_raise_goes_at_once(
    data_dir, tmp_path, monkeypatch, clock
):
    """Monday's failed try was settled when the plan was fixed: a plan drawn after midnight goes
    with its announcement, never held for the checkup by that failure."""
    b, db = setup(data_dir, tmp_path, monkeypatch, down=True)
    tick(b)  # Monday's raise fails
    real = b.problems
    nothing_sure(b)  # then the plan is fixed, and the check agrees: nothing is owed
    judge(b, db, {})
    down(data_dir, False)
    clock.tick(600)
    tick(b)
    assert len(turns(data_dir)) == 1
    b.problems = real
    clock.set(MONDAY + dt.timedelta(days=1), 1, 0)
    edit(b)
    judge(b, db, {})
    draw(b)
    assert len(turns(data_dir)) == 2 and answers(b) == 1


def test_a_new_raise_waits_ten_minutes_after_its_own_first_failure(
    data_dir, tmp_path, monkeypatch, clock
):
    b, db = setup(data_dir, tmp_path, monkeypatch, down=True, checkup=False)
    judge(b, db, {})
    draw(b)  # fails
    real = b.problems
    nothing_sure(b)
    clock.tick(600)
    tick(b)  # nothing is owed now: settled
    b.problems = real
    clock.tick(600)
    edit(b)
    judge(b, db, {})
    draw(b)  # another plan: its turn fails too
    assert len(turns(data_dir)) == 2
    down(data_dir, False)
    clock.tick(600)
    tick(b)
    assert len(turns(data_dir)) == 3 and answers(b) == 1


def test_a_day_never_gets_more_failed_plan_turns_than_its_tries(
    data_dir, tmp_path, monkeypatch, clock
):
    """A turn that reached the chat between failures does not start the day's count again."""
    b, db = setup(data_dir, tmp_path, monkeypatch, down=True, checkup=False)
    judge(b, db, {})
    draw(b)
    for wait in (600, 1800):
        clock.tick(wait)
        tick(b)
    assert len(turns(data_dir)) == 3 and answers(b) == 0
    down(data_dir, False)
    clock.tick(3600)
    tick(b)
    assert answers(b) == 1
    down(data_dir)
    edit(b)
    judge(b, db, {})
    draw(b)  # the day's 4th failed try
    assert len(turns(data_dir)) == 5
    clock.tick(600)
    tick(b)
    assert len(turns(data_dir)) == 5


def test_a_failed_turn_for_a_late_finding_still_has_a_deadline(
    data_dir, tmp_path, monkeypatch, clock
):
    """With no morning checkup and the check down, a failed turn for what the check found once
    still lets a new sure problem go after its wait."""
    b, db = setup(data_dir, tmp_path, monkeypatch, checkup=False)
    draw(b)  # the check has not judged the plan: it waits for it
    clock.tick(bot_module.PLAN_CHECK_WAIT_S)
    tick(b)  # the sure problems alone
    assert len(turns(data_dir)) == 1 and answers(b) == 1
    judge(b, db, {"nonsense": [OVEN]})
    down(data_dir)
    tick(b)  # the turn for what the check found fails
    assert len(turns(data_dir)) == 2
    edit(b)  # the plan moves, and the check stays down
    down(data_dir, False)
    clock.tick(600)
    tick(b)
    assert len(turns(data_dir)) == 2  # it waits for the check of the plan as it is
    clock.tick(bot_module.PLAN_CHECK_WAIT_S)
    tick(b)
    assert len(turns(data_dir)) == 3 and SALAD_COOK in turns(data_dir)[2]["context"]


def test_a_plan_drawn_with_no_ai_gets_its_list_with_no_morning_checkup(
    data_dir, tmp_path, monkeypatch, clock
):
    b, _db = setup(data_dir, tmp_path, monkeypatch, checkup=False)
    b.data.settings.llm.provider = "none"
    draw(b)
    (report,) = [x["text"] for x in sends(b, -100)]
    assert report.startswith("⚠️ The plan has something to fix:")
    draw(b)
    assert len(sends(b, -100)) == 1


def test_two_raises_never_skip_the_wait_after_a_turn_that_failed_between_them(
    data_dir, tmp_path, monkeypatch, clock
):
    """Two raises overlap: the first queues a turn that fails before the second reads the record
    again. The second waits as any retry does."""
    b, _db = setup(data_dir, tmp_path, monkeypatch, down=True)
    tick(b)  # fails
    clock.tick(600)
    real_problems, real_drive, ended, calls = b.problems, b.drive, [], []

    async def drive_(client, chat):
        await real_drive(client, chat)
        ended.append(chat)

    async def problems(offered=()):
        calls.append(offered)
        if len(calls) == 2:  # the second raise reads the record once the first one's turn ended
            for _ in range(500):  # bounded: a raise that spawned no drive fails here, not hangs
                if ended:
                    break
                await asyncio.sleep(0.01)
            assert ended, "the first raise's drive never finished"
        return await real_problems(offered)

    b.drive, b.problems = drive_, problems

    async def both():
        await asyncio.gather(b.raise_plan(None), b.raise_plan(None))
        await asyncio.gather(*b.tasks)

    run(both())
    assert ended and len(turns(data_dir)) == 2  # the first try and one retry, never a third


def test_a_plan_row_queued_just_before_a_stop_is_the_plan_turn_after_the_restart(
    data_dir, tmp_path, monkeypatch, clock
):
    """The process stopped between queueing the plan check's row and writing it in the record: the
    restart takes that row for the plan turn, so the chat never gets a second one."""
    b, db = setup(data_dir, tmp_path, monkeypatch)
    judge(b, db, {})
    real = b.state.db.set_kv
    stopped = []

    def set_kv(key, value):
        if key == "verifier.plan_raised" and isinstance(value, dict) and "turn" in value:
            stopped.append(value)
            raise SystemExit  # the stop, right after take()
        return real(key, value)

    b.drive = parked
    monkeypatch.setattr(db, "set_kv", set_kv)
    with pytest.raises(SystemExit):
        tick(b)
    monkeypatch.setattr(db, "set_kv", real)
    assert stopped and len(db.get_kv("bot.queued")["-100"]) == 1
    b2 = restarted(b, False, db)  # the new process runs the queued row as the plan turn
    assert len(turns(data_dir)) == 1 and answers(b2) == 1
    tick(b2)
    clock.tick(3600)
    tick(b2)
    assert len(turns(data_dir)) == 1 and answers(b2) == 1


def test_a_plan_turn_whose_queue_the_chat_emptied_is_raised_again(
    data_dir, tmp_path, monkeypatch, clock
):
    b, db = setup(data_dir, tmp_path, monkeypatch)
    b.drive = parked
    tick(b)
    assert db.get_kv("bot.queued")
    run(b.cmd_new(None, "", {}, {"id": -100}))  # /nuevo empties the chat's queue
    del b.drive
    assert not db.get_kv("bot.queued")
    tick(b)  # its turn will never answer: a failed try
    assert turns(data_dir) == []
    clock.tick(600)
    tick(b)
    assert len(turns(data_dir)) == 1 and answers(b) == 1


def test_a_plan_turn_whose_consistency_check_fails_is_raised_again(
    data_dir, tmp_path, monkeypatch, clock
):
    """The turn's own gathering of the plan's problems fails (not the raise's): nothing reached the
    chat, so the raise stays owed and goes again."""
    b, _db = setup(data_dir, tmp_path, monkeypatch)
    b.drive = parked
    tick(b)
    del b.drive
    real = doctor.problems

    def broken(*a, **k):
        raise RuntimeError("broken")

    monkeypatch.setattr(doctor, "problems", broken)
    drive(b)
    monkeypatch.setattr(doctor, "problems", real)
    assert turns(data_dir) == [] and answers(b) == 0
    clock.tick(600)
    tick(b)
    assert len(turns(data_dir)) == 1 and answers(b) == 1


def test_a_plan_that_moves_while_the_turn_gathers_its_problems_is_gathered_again(
    data_dir, tmp_path, monkeypatch, clock
):
    """The plan moved after the turn read its problems and before it read its key: the turn reads
    both again, so its facts show the problems of the plan its key names."""
    b, _db = setup(data_dir, tmp_path, monkeypatch)
    b.drive = parked
    tick(b)
    del b.drive
    real, moved = doctor.problems, []

    def problems(*a, **k):
        out = real(*a, **k)
        if not moved:
            moved.append(edit(b))  # a cook nobody does, right after the problems were read
        return out

    monkeypatch.setattr(doctor, "problems", problems)
    drive(b)
    assert moved and SALAD_COOK in turns(data_dir)[0]["context"]


def test_a_turn_for_a_late_finding_never_counts_an_edits_problem_as_heard(
    data_dir, tmp_path, monkeypatch, clock
):
    """Only what the check finds was owed after the morning's sure problems: the turn for it shows
    an edit's sure problem only as background, so that problem goes the next morning."""
    b, db = setup(data_dir, tmp_path, monkeypatch)  # the window is over
    tick(b)  # the sure problems alone
    clock.set(MONDAY, 14)
    edit(b)  # an edit from the chat: a cook nobody does
    judge(b, db, {"nonsense": [OVEN]})
    tick(b)  # what the check found goes, and only that is new
    assert len(turns(data_dir)) == 2
    note = plan_note(b, turns(data_dir)[1])
    assert OVEN in note and note.count("•") == 1
    next_morning(b, clock, db)
    tick(b)
    assert len(turns(data_dir)) == 3 and answers(b) == 3
    assert SALAD_COOK in turns(data_dir)[2]["context"]


def test_what_0_49_2_owed_waits_for_the_check_at_most_its_wait(
    data_dir, tmp_path, monkeypatch, clock
):
    """The raise 0.49.2 still owed waits for the check of a plan it has not judged, then goes with
    the sure problems alone: its deadline is set once, never again at each tick."""
    b, db = setup(data_dir, tmp_path, monkeypatch, checkup=False)
    db.set_kv("verifier.plan_raised", {"on": MONDAY.isoformat(), "drawn": MONDAY.isoformat()})
    tick(b)
    assert turns(data_dir) == []  # it waits for the check
    clock.tick(bot_module.PLAN_CHECK_WAIT_S - 60)
    tick(b)
    assert turns(data_dir) == []
    clock.tick(60)
    tick(b)
    assert len(turns(data_dir)) == 1 and answers(b) == 1
