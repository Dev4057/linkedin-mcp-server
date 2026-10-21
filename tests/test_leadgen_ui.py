"""Tests for the lead-generation dashboard backend (no browser, no engine)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from linkedin_mcp_server.leadgen.budget import BudgetGuard
from linkedin_mcp_server.leadgen.db import Lead, LeadStore
from linkedin_mcp_server.leadgen.runtime import LeadgenRuntime, set_runtime
from linkedin_mcp_server.leadgen.settings import LeadgenSettings, WorkingHours
from linkedin_mcp_server.leadgen.ui import engine as engine_mod
from linkedin_mcp_server.leadgen.ui import server as ui


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    icp = tmp_path / "icp.yaml"
    icp.write_text("name: t\ntitles: [CMO]\nlocations: [US]\n")
    settings = LeadgenSettings(
        db_path=tmp_path / "db.sqlite3",
        icp_path=icp,
        daily_caps={"searches": 5, "profile_views": 5, "connects": 1, "messages": 1},
        min_delay_seconds=0,
        max_delay_seconds=0,
        working_hours=WorkingHours(enabled=False),
    )
    store = LeadStore(settings.db_path)
    set_runtime(
        LeadgenRuntime(
            settings=settings, store=store, budget=BudgetGuard(store, settings)
        )
    )
    ui.hub.jobs.clear()
    ui.hub.history.clear()
    monkeypatch.setattr(ui, "_session_cache", None)
    monkeypatch.setattr(ui, "engine", engine_mod.Engine(port=1))  # never started
    try:
        with TestClient(ui.create_app()) as c:
            yield c
    finally:
        set_runtime(None)


def test_index_and_guide_are_served(client: TestClient) -> None:
    assert "Leadgen console" in client.get("/").text
    assert client.get("/api/guide").text.startswith("# Leadgen console guide")


def test_state_includes_icp_searches_and_budget(client: TestClient) -> None:
    s = client.get("/api/state").json()
    assert s["icp"]["titles"] == ["CMO"]
    assert s["searches"] == [
        {"keywords": "CMO", "location": "US", "network": None, "current_company": None}
    ]
    assert s["stats"]["budget"]["actions"]["searches"]["cap"] == 5
    assert s["engine"]["running"] is False
    assert "new" in s["statuses"]


def test_icp_validate_and_save(client: TestClient, tmp_path: Path) -> None:
    bad = client.post("/api/icp/validate", json={"text": "locations: [US]\n"}).json()
    assert bad["ok"] is False and "titles" in bad["error"]
    refused = client.put("/api/icp", json={"text": "locations: [US]\n"})
    assert refused.status_code == 400
    assert (tmp_path / "icp.yaml").read_text().startswith("name: t")

    good = client.put(
        "/api/icp",
        json={"text": "titles: [CFO, COO]\nleadgen: {daily_caps: {searches: 2}}\n"},
    ).json()
    assert good["ok"] and [s["keywords"] for s in good["searches"]] == ["CFO", "COO"]
    assert (tmp_path / "icp.yaml").read_text().startswith("titles:")
    # The leadgen block took effect on the running budget.
    assert good["stats"]["budget"]["actions"]["searches"]["cap"] == 2


def test_leads_list_patch_and_export(client: TestClient) -> None:
    from linkedin_mcp_server.leadgen.runtime import get_runtime

    store = get_runtime().store
    store.upsert_lead(
        Lead(
            username="ann",
            profile_url="p",
            name="Ann A",
            headline="CMO at Acme",
            company="Acme",
        )
    )
    store.upsert_lead(
        Lead(
            username="bob",
            profile_url="p",
            name="Bob B",
            headline="CFO at Beta",
            company="Beta",
        )
    )

    assert client.get("/api/leads").json()["count"] == 2
    assert [r["username"] for r in client.get("/api/leads?q=acme").json()["leads"]] == [
        "ann"
    ]

    patched = client.patch(
        "/api/leads/ann", json={"status": "qualified", "score": 88, "notes": "call"}
    ).json()
    assert (
        patched["status"] == "qualified"
        and patched["score"] == 88
        and patched["notes"] == "call"
    )
    assert client.patch("/api/leads/ann", json={"status": "bogus"}).status_code == 400
    assert client.patch("/api/leads/nobody", json={"notes": "x"}).status_code == 404

    assert [
        r["username"] for r in client.get("/api/leads?min_score=50").json()["leads"]
    ] == ["ann"]
    csv_text = client.get("/api/leads/export?format=csv&status=qualified").text
    assert csv_text.splitlines()[0].startswith("name,title,company")
    assert len(csv_text.splitlines()) == 2
    assert (
        json.loads(client.get("/api/leads/export?format=json").text)[0]["username"]
        == "ann"
    )
    assert client.get("/api/leads/export?format=xml").status_code == 400


def test_run_requires_engine(client: TestClient) -> None:
    r = client.post("/api/run/find", json={"max_results": 10})
    assert r.status_code == 409 and "engine" in r.json()["error"].lower()
    assert (
        client.post("/api/run/tool/search_people", json={"arguments": {}}).status_code
        == 404
    )
    assert client.post("/api/run/stop").status_code == 404


def test_session_check_uses_cli(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_run(*args: str, timeout: float = 0) -> tuple[int, str]:
        assert args == ("--status",)
        return 0, "Profile mode: source"

    monkeypatch.setattr(engine_mod, "_run_cli", fake_run)
    s = client.post("/api/session/check").json()
    assert s["valid"] is True and "source" in s["detail"]
    assert client.get("/api/state").json()["session"]["valid"] is True


@pytest.mark.asyncio
async def test_hub_fans_out_events_and_keeps_log_history() -> None:
    hub = ui.Hub()
    a, b = hub.subscribe(), hub.subscribe()
    await hub.log("ok", "hello")
    await hub.emit("stats", {"x": 1})
    for q in (a, b):
        first = json.loads(q.get_nowait())
        assert first["event"] == "log" and first["data"]["message"] == "hello"
        assert json.loads(q.get_nowait())["event"] == "stats"
    assert [h["data"]["message"] for h in hub.history] == ["hello"]
    hub.unsubscribe(b)
    await hub.log("info", "again")
    assert json.loads(a.get_nowait())["data"]["message"] == "again"
    assert b.empty()
    job = hub.new_job("find_icp_leads")
    assert hub.active_job is job
    job.status = "done"
    assert hub.active_job is None
