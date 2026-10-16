"""Tests for the lead-generation layer (phase one: finding leads)."""

from __future__ import annotations

import random
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from linkedin_mcp_server.leadgen.budget import BudgetExhausted, BudgetGuard
from linkedin_mcp_server.leadgen.db import Lead, LeadStore
from linkedin_mcp_server.leadgen.finder import find_leads
from linkedin_mcp_server.leadgen.icp import ICPError, SearchSpec, load_icp, parse_icp
from linkedin_mcp_server.leadgen.parse import parse_search_page, split_title_company
from linkedin_mcp_server.leadgen.runtime import LeadgenRuntime
from linkedin_mcp_server.leadgen.settings import (
    LeadgenSettings,
    WorkingHours,
    parse_working_hours,
)
from linkedin_mcp_server.linkedin.contracts import FilterValidationError
from linkedin_mcp_server.linkedin.search_urls import build_people_search_url

# ---------------------------------------------------------------------------
# helpers


def _settings(tmp_path: Path, **kw: Any) -> LeadgenSettings:
    base = LeadgenSettings(
        db_path=tmp_path / "leads.sqlite3",
        icp_path=None,
        daily_caps={"searches": 3, "profile_views": 5, "connects": 1, "messages": 1},
        min_delay_seconds=0,
        max_delay_seconds=0,
        working_hours=WorkingHours(enabled=False),
        seen_days=30,
    )
    return base if not kw else LeadgenSettings(**{**base.__dict__, **kw})


def _runtime(tmp_path: Path, **kw: Any) -> LeadgenRuntime:
    settings = _settings(tmp_path, **kw)
    store = LeadStore(settings.db_path)
    return LeadgenRuntime(
        settings=settings, store=store, budget=BudgetGuard(store, settings)
    )


def _page(people: list[tuple[str, str, str, str]]) -> dict[str, Any]:
    """Build a fake search_people result from (username, name, headline, location)."""
    lines: list[str] = ["People", "10 results"]
    refs: list[dict[str, Any]] = []
    for username, name, headline, location in people:
        lines += [
            name,
            f"View {name}'s profile",
            "• 2nd",
            headline,
            location,
            "Connect",
        ]
        refs.append({"kind": "person", "url": f"/in/{username}/", "text": name})
    return {
        "url": "https://www.linkedin.com/search/results/people/?keywords=x",
        "sections": {"search_results": "\n".join(lines)},
        "references": {"search_results": refs},
    }


ICP_YAML = """
name: test-icp
titles: [Marketing Director, Head of Growth]
locations: [United States]
keywords: [SaaS]
exclude_keywords: [recruiter, student]
network: [S, O]
max_pages_per_search: 2
max_results: 50
scoring:
  must_have: [marketing]
  disqualify: [agency]
leadgen:
  daily_caps: {searches: 2}
  working_hours: "off"
"""


# ---------------------------------------------------------------------------
# search URL paging


def test_people_search_url_adds_page_only_after_first() -> None:
    base = build_people_search_url("analyst")
    assert build_people_search_url("analyst", page=None) == base
    assert build_people_search_url("analyst", page=1) == base
    assert build_people_search_url("analyst", page=3) == base + "&page=3"


def test_people_search_url_refuses_page_zero() -> None:
    with pytest.raises(FilterValidationError):
        build_people_search_url("analyst", page=0)


# ---------------------------------------------------------------------------
# ICP


def test_icp_generates_title_x_location_searches(tmp_path: Path) -> None:
    path = tmp_path / "icp.yaml"
    path.write_text(ICP_YAML)
    icp = load_icp(path)
    specs = list(icp.searches())
    assert [s.keywords for s in specs] == [
        "Marketing Director SaaS",
        "Head of Growth SaaS",
    ]
    assert all(s.location == "United States" for s in specs)
    assert all(s.network == ["S", "O"] for s in specs)
    assert icp.excluded("Senior Recruiter at Foo") == "recruiter"
    assert icp.excluded("Marketing Director at Foo") is None
    assert icp.leadgen_overrides["daily_caps"] == {"searches": 2}


def test_icp_requires_titles() -> None:
    with pytest.raises(ICPError):
        parse_icp({"locations": ["US"]})


def test_icp_rejects_bad_network_and_urns() -> None:
    with pytest.raises(ICPError):
        parse_icp({"titles": ["CEO"], "network": ["X"]})
    with pytest.raises(ICPError):
        parse_icp({"titles": ["CEO"], "company_urns": ["Acme"]})


def test_settings_overrides_from_icp(tmp_path: Path) -> None:
    settings = _settings(tmp_path).with_overrides(
        {
            "daily_caps": {"searches": 7, "bogus": 1},
            "working_hours": "9-17",
            "seen_days": 10,
        }
    )
    assert settings.daily_caps["searches"] == 7
    assert "bogus" not in settings.daily_caps
    assert settings.working_hours == WorkingHours(9, 17, True)
    assert settings.seen_days == 10


def test_parse_working_hours() -> None:
    assert parse_working_hours("off").enabled is False
    assert parse_working_hours("8-20").contains(8)
    assert not parse_working_hours("8-20").contains(20)
    assert parse_working_hours("22-6").contains(23)
    assert parse_working_hours("22-6").contains(3)
    assert not parse_working_hours("22-6").contains(12)
    with pytest.raises(ValueError):
        parse_working_hours("nine to five")


# ---------------------------------------------------------------------------
# parser


def test_parse_search_page_reads_people_from_references() -> None:
    page = parse_search_page(
        _page(
            [
                (
                    "priya-sharma-1",
                    "Priya Sharma",
                    "Marketing Director at Acme Corp",
                    "San Francisco Bay Area",
                ),
                ("jdoe", "Jane Doe", "Head of Growth | Beta Inc", "New York, NY"),
            ]
        ),
        source_query="q",
    )
    assert page.usernames == ["priya-sharma-1", "jdoe"]
    priya = page.leads[0]
    assert priya.name == "Priya Sharma"
    assert priya.headline == "Marketing Director at Acme Corp"
    assert priya.location == "San Francisco Bay Area"
    assert (priya.title, priya.company) == ("Marketing Director", "Acme Corp")
    assert priya.profile_url == "https://www.linkedin.com/in/priya-sharma-1/"
    assert "Priya Sharma" in (priya.snippet or "")
    assert page.leads[1].company == "Beta Inc"


def test_parse_search_page_dedupes_and_ignores_non_person_refs() -> None:
    raw = _page([("a", "A One", "CEO at X", "Berlin")])
    raw["references"]["search_results"].append(
        {"kind": "person", "url": "/in/a/", "text": "A One"}
    )
    raw["references"]["search_results"].append(
        {"kind": "company", "url": "/company/x/"}
    )
    page = parse_search_page(raw)
    assert page.usernames == ["a"]


def test_parse_search_page_applies_exclusions() -> None:
    icp = parse_icp({"titles": ["x"], "exclude_keywords": ["recruiter"]})
    page = parse_search_page(
        _page(
            [
                ("r1", "Rec Ruiter", "Senior Recruiter at Hire", "LA"),
                ("ok", "Ok Person", "CMO at Y", "LA"),
            ]
        ),
        exclude=icp,
    )
    assert page.usernames == ["ok"]
    assert page.skipped[0]["username"] == "r1"
    assert "recruiter" in page.skipped[0]["reason"]


def test_parse_search_page_reports_rate_limit() -> None:
    page = parse_search_page(
        {
            "url": "u",
            "sections": {},
            "section_errors": {
                "search_results": {
                    "error_type": "rate_limited",
                    "error_message": "slow down",
                }
            },
        }
    )
    assert page.rate_limited and page.leads == []


def test_split_title_company() -> None:
    assert split_title_company("VP Sales @ Foo") == ("VP Sales", "Foo")
    assert split_title_company("Founder") == ("Founder", None)
    assert split_title_company(None) == (None, None)


# ---------------------------------------------------------------------------
# store


def test_store_upsert_is_new_once_then_refreshes(tmp_path: Path) -> None:
    store = LeadStore(tmp_path / "db.sqlite3")
    lead, is_new = store.upsert_lead(
        Lead(username="u1", profile_url="p", name="N", headline="H")
    )
    assert is_new and lead.times_seen == 1 and lead.status == "new"
    store.update_lead("u1", status="qualified", score=80, signals=["hiring"])
    again, is_new = store.upsert_lead(
        Lead(username="u1", profile_url="p", headline="H2")
    )
    assert not is_new
    assert again.times_seen == 2
    assert again.headline == "H2"
    assert (
        again.status == "qualified"
        and again.score == 80
        and again.signals == ["hiring"]
    )
    assert store.count_leads() == {"qualified": 1, "total": 1}


def test_store_update_validates(tmp_path: Path) -> None:
    store = LeadStore(tmp_path / "db.sqlite3")
    store.upsert_lead(Lead(username="u1", profile_url="p"))
    with pytest.raises(ValueError):
        store.update_lead("u1", status="nope")
    with pytest.raises(ValueError):
        store.update_lead("u1", bogus="x")
    assert store.update_lead("missing", notes="x") is None


def test_store_list_and_export(tmp_path: Path) -> None:
    store = LeadStore(tmp_path / "db.sqlite3")
    for i, score in enumerate([10, 90, 50]):
        store.upsert_lead(Lead(username=f"u{i}", profile_url="p", name=f"N{i}"))
        store.update_lead(f"u{i}", score=score)
    assert [lead.username for lead in store.list_leads(min_score=50)] == ["u1", "u2"]
    csv_text = store.export(store.list_leads(), fmt="csv")
    assert csv_text.splitlines()[0].startswith("name,title,company")
    assert len(csv_text.splitlines()) == 4
    with pytest.raises(ValueError):
        store.export([], fmt="xml")


def test_store_seen_recently_counts_profile_views_only(tmp_path: Path) -> None:
    store = LeadStore(tmp_path / "db.sqlite3")
    store.upsert_lead(Lead(username="u1", profile_url="p"))
    assert not store.seen_recently("u1", 30)
    store.mark_viewed("u1")
    assert store.seen_recently("u1", 30)
    assert not store.seen_recently("u1", 0)


# ---------------------------------------------------------------------------
# budget


def test_budget_caps_and_persists_across_instances(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = LeadStore(settings.db_path)
    guard = BudgetGuard(store, settings)
    assert guard.spend("searches") == 1
    assert guard.spend("searches") == 2
    assert guard.spend("searches") == 3
    with pytest.raises(BudgetExhausted, match="Daily cap reached for searches: 3/3"):
        guard.spend("searches")
    # A new guard over the same file still sees today's spend.
    assert BudgetGuard(store, settings).remaining("searches") == 0
    assert guard.spend("unbudgeted_action") == 0


def test_budget_resets_on_a_new_day(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = LeadStore(settings.db_path)
    day = [datetime(2026, 10, 15, 12)]
    guard = BudgetGuard(store, settings, clock=lambda: day[0])
    for _ in range(3):
        guard.spend("searches")
    with pytest.raises(BudgetExhausted):
        guard.check("searches")
    day[0] = datetime(2026, 10, 16, 12)
    guard.check("searches")
    assert guard.status().remaining("searches") == 3


def test_budget_refuses_outside_working_hours(tmp_path: Path) -> None:
    settings = _settings(tmp_path, working_hours=WorkingHours(9, 18, True))
    store = LeadStore(settings.db_path)
    guard = BudgetGuard(store, settings, clock=lambda: datetime(2026, 10, 15, 23, 5))
    with pytest.raises(BudgetExhausted, match="Outside working hours"):
        guard.check("searches")
    assert guard.status().within_hours is False


def test_budget_delay_within_bounds(tmp_path: Path) -> None:
    settings = _settings(tmp_path, min_delay_seconds=2.0, max_delay_seconds=5.0)
    guard = BudgetGuard(LeadStore(settings.db_path), settings, rng=random.Random(1))
    for _ in range(20):
        assert 2.0 <= guard.next_delay() <= 5.0


def test_budget_maps_tools_to_actions(tmp_path: Path) -> None:
    guard = BudgetGuard(LeadStore(tmp_path / "db"), _settings(tmp_path))
    assert guard.action_for_tool("search_people") == "searches"
    assert guard.action_for_tool("get_person_profile") == "profile_views"
    assert guard.action_for_tool("connect_with_person") == "connects"
    assert guard.action_for_tool("find_icp_leads") is None


# ---------------------------------------------------------------------------
# finder


@pytest.mark.asyncio
async def test_find_leads_pages_dedupes_and_saves(tmp_path: Path) -> None:
    runtime = _runtime(
        tmp_path,
        daily_caps={"searches": 100, "profile_views": 1, "connects": 1, "messages": 1},
    )
    icp = parse_icp(
        {
            "titles": ["CMO"],
            "locations": ["US", "UK"],
            "max_pages_per_search": 3,
            "max_results": 100,
        }
    )
    calls: list[tuple[str, int]] = []
    pages = {
        ("CMO", "US", 1): _page(
            [("a", "A A", "CMO at A", "x"), ("b", "B B", "CMO at B", "x")]
        ),
        ("CMO", "US", 2): _page(
            [("c", "C C", "CMO at C", "x"), ("a", "A A", "CMO at A", "x")]
        ),
        ("CMO", "US", 3): _page([]),
        ("CMO", "UK", 1): _page(
            [("b", "B B", "CMO at B", "x"), ("d", "D D", "CMO at D", "x")]
        ),
        ("CMO", "UK", 2): _page([]),
    }

    async def search(spec: SearchSpec, page: int) -> dict[str, Any]:
        calls.append((spec.location or "", page))
        return pages[(spec.keywords, spec.location, page)]

    result = await find_leads(icp=icp, runtime=runtime, search=search, pause=False)
    assert result.new == 4
    assert result.found == 4  # the repeat of "a" and "b" within the run is not counted
    assert result.pages_read == 5
    assert result.stopped_because == "completed"
    assert sorted(result.new_usernames) == ["a", "b", "c", "d"]
    assert runtime.store.count_leads()["total"] == 4
    assert runtime.budget.status().used["searches"] == 5
    assert runtime.store.recent_runs(1)[0]["summary"]["new"] == 4

    # Second run: everything is already known, so paging stops after page 1
    # of each search and nothing new is stored.
    calls.clear()
    result2 = await find_leads(icp=icp, runtime=runtime, search=search, pause=False)
    assert result2.new == 0
    assert (
        result2.already_known == 3
    )  # a, b, d; the second b is a repeat within the run
    assert calls == [("US", 1), ("UK", 1)]


@pytest.mark.asyncio
async def test_find_leads_stops_at_max_results(tmp_path: Path) -> None:
    runtime = _runtime(
        tmp_path,
        daily_caps={"searches": 100, "profile_views": 1, "connects": 1, "messages": 1},
    )
    icp = parse_icp({"titles": ["CMO"], "max_pages_per_search": 5})

    async def search(spec: SearchSpec, page: int) -> dict[str, Any]:
        return _page(
            [(f"p{page}{i}", f"P{page}{i}", "CMO at X", "x") for i in range(10)]
        )

    result = await find_leads(
        icp=icp, runtime=runtime, search=search, max_results=15, pause=False
    )
    assert result.new == 20  # page granularity: the second page completes
    assert result.pages_read == 2
    assert "max_results" in result.stopped_because


@pytest.mark.asyncio
async def test_find_leads_stops_when_budget_runs_out(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)  # searches cap = 3
    icp = parse_icp({"titles": ["CMO", "CFO"], "max_pages_per_search": 3})

    async def search(spec: SearchSpec, page: int) -> dict[str, Any]:
        return _page(
            [(f"{spec.keywords}{page}", f"N {spec.keywords}{page}", "x at y", "z")]
        )

    result = await find_leads(icp=icp, runtime=runtime, search=search, pause=False)
    assert result.pages_read == 3
    assert "Daily cap reached" in result.stopped_because
    assert result.new == 3


@pytest.mark.asyncio
async def test_find_leads_stops_on_rate_limit_and_skips_exclusions(
    tmp_path: Path,
) -> None:
    runtime = _runtime(
        tmp_path,
        daily_caps={"searches": 100, "profile_views": 1, "connects": 1, "messages": 1},
    )
    icp = parse_icp({"titles": ["CMO", "CFO"], "exclude_keywords": ["student"]})

    async def search(spec: SearchSpec, page: int) -> dict[str, Any]:
        if spec.keywords == "CFO":
            return {
                "url": "u",
                "sections": {},
                "section_errors": {"search_results": {"error_type": "rate_limited"}},
            }
        return _page(
            [("s", "S S", "Student at Uni", "x"), ("ok", "O K", "CMO at Z", "x")]
        )

    result = await find_leads(icp=icp, runtime=runtime, search=search, pause=False)
    assert result.new == 1
    assert result.skipped[0]["username"] == "s"
    assert result.stopped_because == "LinkedIn rate-limited the search page"
