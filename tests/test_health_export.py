"""Pills from a health export (settings.health_export), with household.yaml as the fallback."""

import datetime as dt

import httpx
import pytest
import yaml

from supermealplanner import health, scheduler
from supermealplanner.db import DB
from supermealplanner.model import DataError, check, load
from supermealplanner.planner import generate
from supermealplanner.portions import person_days

from .conftest import MONDAY

EXPORT = {
    "generated": "2026-10-12T08:00:00+02:00",
    "people": {
        "luis": {
            "allergies": [],
            "meds": [
                {
                    "name": "Omega-3",
                    "kind": "supplement",
                    "dose": "1 perla",
                    "slot": "lunch",
                    "days": "daily",
                    "food": "Con la comida",
                },
                {
                    "name": "Inyección",
                    "kind": "medicine",
                    "dose": "1 mg",
                    "slot": "waking",
                    "days": ["monday"],
                    "food": "",
                },
                {
                    "name": "Para dormir",
                    "kind": "medicine",
                    "dose": "5 mg",
                    "slot": "as_needed",
                    "days": "daily",
                    "food": "",
                },
            ],
        },
    },
}


class Remote:
    """The export's URL: answers what the test sets, and records each request's headers."""

    def __init__(self, monkeypatch):
        self.status, self.body, self.etag, self.down = 200, EXPORT, '"v1"', False
        self.sent: list[dict] = []
        monkeypatch.setattr(health, "_get", self.get)

    def get(self, url, headers, timeout):
        self.sent.append(dict(headers))
        if self.down:
            raise httpx.ConnectError("unreachable")
        text = self.body if isinstance(self.body, str) else yaml.safe_dump(self.body)
        return httpx.Response(self.status, content=text.encode(), headers={"ETag": self.etag})


@pytest.fixture
def remote(monkeypatch, data_dir):
    monkeypatch.setenv("TEST_EXPORT_TOKEN", "s3cret")
    (data_dir / "settings.yaml").write_text(
        yaml.safe_dump(
            {
                "health_export": {
                    "url": "http://export.invalid/chef.yaml",
                    "token_env": "TEST_EXPORT_TOKEN",
                }
            }
        )
    )
    return Remote(monkeypatch)


def names(person, slot):
    return [x.name for x in person.pills.get(slot, [])]


def test_the_export_wins_over_household_and_check_says_where_pills_came_from(data_dir, remote):
    data = load(data_dir)
    luis, ana = data.household.person("luis"), data.household.person("ana")
    assert names(luis, "lunch") == ["Omega-3"]  # household.yaml said Vitamin D
    assert luis.pills["lunch"][0].dose == "1 perla"
    assert names(luis, "waking") == ["Inyección"]
    assert "as_needed" not in luis.pills  # taken when needed: no daily pill
    assert names(ana, "waking") == ["Hierro"]  # not in the export: household.yaml
    assert remote.sent[0]["Authorization"] == "token s3cret"
    assert (data_dir / "state/health/export.yaml").exists()  # the last good copy

    out = check(data_dir)
    assert "pills of luis: from the health export" in out
    assert "pills of ana: from household.yaml (not in the health export)" in out


def test_an_unchanged_export_costs_a_304_and_reads_the_copy(data_dir, remote):
    load(data_dir)
    remote.status, remote.body = 304, ""
    data = load(data_dir)
    assert remote.sent[1]["If-None-Match"] == '"v1"'
    assert names(data.household.person("luis"), "lunch") == ["Omega-3"]
    assert data.pill_sources["luis"] == health.FROM_EXPORT


def test_an_outage_falls_back_to_the_last_copy_then_to_household(data_dir, remote):
    remote.down = True
    data = load(data_dir)  # never fetched: household.yaml for everyone
    assert names(data.household.person("luis"), "lunch") == ["Vitamin D"]
    assert set(data.pill_sources.values()) == {health.UNAVAILABLE}

    remote.down = False
    load(data_dir)
    remote.down = True
    data = load(data_dir)  # fetched once: the last good copy
    assert names(data.household.person("luis"), "lunch") == ["Omega-3"]
    assert data.pill_sources["luis"] == health.FROM_CACHE

    remote.down, remote.status = False, 500
    assert load(data_dir).pill_sources["luis"] == health.FROM_CACHE

    remote.status, remote.body = 200, "<html>login</html>"  # not an export: the copy stays
    data = load(data_dir)
    assert names(data.household.person("luis"), "lunch") == ["Omega-3"]
    assert "Omega-3" in (data_dir / "state/health/export.yaml").read_text()


def test_an_unknown_slot_is_refused_and_never_kept(data_dir, remote):
    bad = yaml.safe_load(yaml.safe_dump(EXPORT))
    bad["people"]["luis"]["meds"][0]["slot"] = "brunch"
    remote.body = bad
    with pytest.raises(DataError, match="health export luis Omega-3: unknown slot 'brunch'"):
        check(data_dir)
    assert not (data_dir / "state/health/export.yaml").exists()


@pytest.mark.parametrize("days", [["lunes"], [["monday"]], [{"d": "monday"}], [], "weekly"])
def test_bad_days_are_refused(data_dir, remote, days):
    bad = yaml.safe_load(yaml.safe_dump(EXPORT))
    bad["people"]["luis"]["meds"][1]["days"] = days
    remote.body = bad
    with pytest.raises(DataError, match="days .* is not daily or a list of weekdays"):
        load(data_dir)


def test_a_pill_that_is_not_daily_shows_only_on_its_days(data_dir, remote, tmp_path):
    data = load(data_dir)
    luis = data.household.person("luis")
    tuesday = MONDAY + dt.timedelta(days=1)
    assert names(luis, "waking") == ["Inyección"]
    assert [x.name for x in luis.pills_on(MONDAY)["waking"]] == ["Inyección"]
    assert "waking" not in luis.pills_on(tuesday)

    db = DB(tmp_path / "x.db")
    plan = generate(data, MONDAY, 1)

    def waking(day):
        pd = next(p for p in person_days(data, db, plan, day) if p.person_id == "luis")
        return [x.name for x in pd.pills.get("waking", [])]

    assert waking(MONDAY) == ["Inyección"]
    assert waking(tuesday) == []


def test_household_pills_may_have_days_too(data_dir):
    path = data_dir / "household.yaml"
    raw = yaml.safe_load(path.read_text())
    raw["people"][0]["pills"]["waking"][0]["days"] = ["wednesday"]
    path.write_text(yaml.safe_dump(raw, allow_unicode=True))
    ana = load(data_dir).household.person("ana")
    assert "waking" not in ana.pills_on(MONDAY)
    assert ana.pills_on(MONDAY + dt.timedelta(days=2))["waking"][0].name == "Hierro"
    raw["people"][0]["pills"]["waking"][0]["days"] = []
    path.write_text(yaml.safe_dump(raw, allow_unicode=True))
    with pytest.raises(DataError, match="days is empty"):
        load(data_dir)


def test_a_check_of_a_copy_never_fetches(data_dir, remote):
    check(data_dir, health=False)
    assert remote.sent == []


def test_the_hourly_tick_fetches_the_export_again_without_a_git_pull(data_dir, remote, monkeypatch):
    monkeypatch.delenv("SMP_GIT_PULL", raising=False)

    class State:
        root = data_dir
        data = load(data_dir)

        def reload(self):
            self.data = load(self.root)

    state = State()
    remote.body = {"people": {"luis": {"meds": []}}}
    scheduler.pull(state)
    assert state.data.household.person("luis").pills == {}  # the export now lists none
    assert len(remote.sent) == 2


def test_the_cache_stays_inside_the_data_folder(data_dir):
    (data_dir / "settings.yaml").write_text(
        yaml.safe_dump({"health_export": {"url": "http://x.invalid", "cache": "../out.yaml"}})
    )
    with pytest.raises(DataError, match="inside the data folder"):
        load(data_dir)
