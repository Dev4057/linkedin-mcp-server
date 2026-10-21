"""Starlette app behind the lead-generation dashboard.

Loopback only. No authentication, because the page and the server are the
same user on the same machine; binding to anything but 127.0.0.1 is refused.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import (
    FileResponse,
    JSONResponse,
    PlainTextResponse,
    StreamingResponse,
)
from starlette.routing import Route

from linkedin_mcp_server.leadgen.db import STATUSES
from linkedin_mcp_server.leadgen.icp import ICPError, parse_icp
from linkedin_mcp_server.leadgen.runtime import get_runtime
from linkedin_mcp_server.leadgen.settings import DEFAULT_ICP_CANDIDATES
from linkedin_mcp_server.leadgen.ui import engine as engine_mod

logger = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"
GUIDE = Path(__file__).parent / "guide.md"


# ---------------------------------------------------------------------------
# event bus + jobs


@dataclass
class Job:
    id: int
    kind: str
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    status: str = "running"  # running | done | failed | cancelled
    progress: float = 0
    total: float | None = None
    message: str = ""
    result: Any = None
    error: str | None = None
    task: asyncio.Task[Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "status": self.status,
            "progress": self.progress,
            "total": self.total,
            "message": self.message,
            "result": self.result,
            "error": self.error,
        }


class Hub:
    """Fan-out of server-sent events to every open dashboard tab."""

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[str]] = set()
        self.history: list[dict[str, Any]] = []
        self.jobs: dict[int, Job] = {}
        self._next_job = 1

    def subscribe(self) -> asyncio.Queue[str]:
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=500)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[str]) -> None:
        self._subscribers.discard(q)

    async def emit(self, event: str, data: Any) -> None:
        payload = json.dumps({"event": event, "data": data, "ts": time.time()})
        if event == "log":
            self.history.append({"event": event, "data": data, "ts": time.time()})
            self.history = self.history[-300:]
        for q in list(self._subscribers):
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                self.unsubscribe(q)

    async def log(self, level: str, message: str) -> None:
        await self.emit("log", {"level": level, "message": message})

    def new_job(self, kind: str) -> Job:
        job = Job(id=self._next_job, kind=kind)
        self._next_job += 1
        self.jobs[job.id] = job
        return job

    @property
    def active_job(self) -> Job | None:
        for job in self.jobs.values():
            if job.status == "running":
                return job
        return None


hub = Hub()
engine = engine_mod.Engine()
_session_cache: engine_mod.SessionStatus | None = None


# ---------------------------------------------------------------------------
# helpers


def _icp_path() -> Path:
    rt = get_runtime()
    if rt.settings.icp_path is not None:
        return rt.settings.icp_path
    return Path(DEFAULT_ICP_CANDIDATES[0]).resolve()


def _read_icp_text() -> str:
    path = _icp_path()
    if path.exists():
        return path.read_text(encoding="utf-8")
    example = Path(__file__).resolve().parents[3] / "icp.example.yaml"
    if example.exists():
        return example.read_text(encoding="utf-8")
    return (
        "name: my-icp\ntitles:\n  - Marketing Director\nlocations:\n  - United States\n"
    )


def _parse_icp_text(text: str) -> dict[str, Any]:
    data = yaml.safe_load(text) or {}
    icp = parse_icp(data)
    return {
        "icp": icp.to_dict(),
        "searches": [
            {
                "keywords": s.keywords,
                "location": s.location,
                "network": s.network,
                "current_company": s.current_company,
            }
            for s in icp.searches()
        ],
    }


def _bad(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


async def _refresh_runtime_icp() -> None:
    """Re-read icp.yaml so leadgen overrides (caps, hours) take effect."""
    rt = get_runtime()
    rt.settings = rt.settings.__class__(
        **{**rt.settings.__dict__, "icp_path": _icp_path()}
    )
    try:
        rt.load_icp()
    except Exception:  # noqa: BLE001 - a broken file is reported by /api/icp
        pass


def _stats() -> dict[str, Any]:
    rt = get_runtime()
    return {
        "budget": rt.budget.status().to_dict(),
        "leads": rt.store.count_leads(),
        "settings": {
            "db_path": str(rt.settings.db_path),
            "icp_path": str(_icp_path()),
            "seen_days": rt.settings.seen_days,
            "delay_seconds": [
                rt.settings.min_delay_seconds,
                rt.settings.max_delay_seconds,
            ],
            "working_hours": rt.settings.working_hours.describe(),
        },
        "recent_runs": rt.store.recent_runs(8),
    }


# ---------------------------------------------------------------------------
# routes: page + state


async def index(_: Request) -> FileResponse:
    return FileResponse(STATIC / "index.html")


async def guide(_: Request) -> PlainTextResponse:
    return PlainTextResponse(
        GUIDE.read_text(encoding="utf-8") if GUIDE.exists() else "# Guide missing"
    )


async def state(_: Request) -> JSONResponse:
    text = _read_icp_text()
    try:
        parsed = _parse_icp_text(text)
        icp_error = None
    except (ICPError, yaml.YAMLError, ValueError) as e:
        parsed = {"icp": None, "searches": []}
        icp_error = str(e)
    active = hub.active_job
    return JSONResponse(
        {
            "icp_text": text,
            "icp": parsed["icp"],
            "searches": parsed["searches"],
            "icp_error": icp_error,
            "icp_exists": _icp_path().exists(),
            "session": _session_cache.to_dict() if _session_cache else None,
            "engine": engine.status(),
            "tools": engine.tools,
            "stats": _stats(),
            "statuses": list(STATUSES),
            "active_job": active.to_dict() if active else None,
            "jobs": [j.to_dict() for j in list(hub.jobs.values())[-20:]],
            "log": hub.history[-120:],
        }
    )


async def stats(_: Request) -> JSONResponse:
    return JSONResponse(_stats())


# ---------------------------------------------------------------------------
# routes: ICP


async def icp_get(_: Request) -> JSONResponse:
    text = _read_icp_text()
    try:
        return JSONResponse(
            {
                "text": text,
                "path": str(_icp_path()),
                **_parse_icp_text(text),
                "error": None,
            }
        )
    except (ICPError, yaml.YAMLError, ValueError) as e:
        return JSONResponse(
            {
                "text": text,
                "path": str(_icp_path()),
                "icp": None,
                "searches": [],
                "error": str(e),
            }
        )


async def icp_validate(request: Request) -> JSONResponse:
    body = await request.json()
    try:
        return JSONResponse({"ok": True, **_parse_icp_text(body.get("text", ""))})
    except (ICPError, yaml.YAMLError, ValueError) as e:
        return JSONResponse({"ok": False, "error": str(e)})


async def icp_put(request: Request) -> JSONResponse:
    body = await request.json()
    text = body.get("text", "")
    try:
        parsed = _parse_icp_text(text)
    except (ICPError, yaml.YAMLError, ValueError) as e:
        return _bad(f"Not saved: {e}")
    path = _icp_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    await _refresh_runtime_icp()
    await hub.log("ok", f"ICP saved to {path} ({len(parsed['searches'])} searches)")
    return JSONResponse({"ok": True, "path": str(path), **parsed, "stats": _stats()})


# ---------------------------------------------------------------------------
# routes: session + engine


async def session_check(_: Request) -> JSONResponse:
    global _session_cache
    _session_cache = await engine_mod.check_session()
    await hub.log(
        "ok" if _session_cache.valid else "warn",
        "LinkedIn session is valid"
        if _session_cache.valid
        else "No LinkedIn session yet: use Sign in or Import from browser on the LinkedIn & engine page",
    )
    await hub.emit("session", _session_cache.to_dict())
    return JSONResponse(_session_cache.to_dict())


def _session_job(kind: str, coro_factory):  # type: ignore[no-untyped-def]
    async def handler(request: Request) -> JSONResponse:
        global _session_cache
        if hub.active_job is not None:
            return _bad("Another job is running; wait for it to finish.", 409)
        body = {}
        if request.method == "POST":
            try:
                body = await request.json()
            except Exception:  # noqa: BLE001
                body = {}
        job = hub.new_job(kind)

        async def run() -> None:
            global _session_cache
            try:
                await hub.emit("job", job.to_dict())
                _session_cache = await coro_factory(body)
                job.result = _session_cache.to_dict()
                job.status = "done" if _session_cache.valid else "failed"
                if not _session_cache.valid:
                    job.error = _session_cache.detail
                await hub.emit("session", _session_cache.to_dict())
            except Exception as e:  # noqa: BLE001
                job.status, job.error = "failed", str(e)
                await hub.log("error", f"{kind} failed: {e}")
            finally:
                job.finished_at = time.time()
                await hub.emit("job", job.to_dict())

        job.task = asyncio.create_task(run())
        return JSONResponse(job.to_dict())

    return handler


session_login = _session_job(
    "login", lambda body: engine_mod.login_interactive(hub.log)
)
session_import = _session_job(
    "import",
    lambda body: engine_mod.import_from_browser(
        str(body.get("browser") or "auto"), hub.log
    ),
)
session_logout = _session_job("logout", lambda body: engine_mod.logout(hub.log))


async def engine_start(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    engine.headless = not bool(body.get("show_browser", False))
    try:
        status = await engine.start(hub.log)
    except Exception as e:  # noqa: BLE001
        await hub.log("error", str(e))
        return _bad(str(e), 500)
    await hub.emit("engine", status)
    return JSONResponse({**status, "tools": engine.tools})


async def engine_stop(_: Request) -> JSONResponse:
    if hub.active_job is not None:
        return _bad("A job is running; stop it first.", 409)
    status = await engine.stop(hub.log)
    await hub.emit("engine", status)
    return JSONResponse(status)


async def engine_status(_: Request) -> JSONResponse:
    return JSONResponse({**engine.status(), "tools": engine.tools})


# ---------------------------------------------------------------------------
# routes: jobs (find_icp_leads and any tool)


async def _run_tool_job(
    kind: str, tool: str, arguments: dict[str, Any]
) -> JSONResponse:
    if hub.active_job is not None:
        return _bad("Another job is already running.", 409)
    if not engine.running:
        return _bad("Start the engine first (Session page).", 409)
    job = hub.new_job(kind)

    async def progress(p: float, total: float | None, message: str | None) -> None:
        job.progress, job.total, job.message = p, total, message or ""
        await hub.emit(
            "progress",
            {"job": job.id, "progress": p, "total": total, "message": message},
        )

    async def run() -> None:
        await hub.log(
            "info", f"▶ {tool} {json.dumps(arguments) if arguments else ''}".rstrip()
        )
        await hub.emit("job", job.to_dict())
        try:
            job.result = await engine.call_tool(tool, arguments, progress=progress)
            job.status = "done"
            if (
                isinstance(job.result, dict)
                and "new" in job.result
                and "found" in job.result
            ):
                r = job.result
                await hub.log(
                    "ok",
                    f"✔ {tool}: {r.get('found')} found, {r.get('new')} new, "
                    f"{r.get('already_known')} known, {r.get('skipped')} skipped — {r.get('stopped_because')}",
                )
            else:
                await hub.log("ok", f"✔ {tool} finished")
        except asyncio.CancelledError:
            job.status = "cancelled"
            await hub.log("warn", f"■ {tool} cancelled")
            raise
        except Exception as e:  # noqa: BLE001
            job.status, job.error = "failed", str(e)
            await hub.log("error", f"✖ {tool}: {e}")
        finally:
            job.finished_at = time.time()
            await hub.emit("job", job.to_dict())
            await hub.emit("stats", _stats())

    job.task = asyncio.create_task(run())
    return JSONResponse(job.to_dict())


async def run_find(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    args = {
        k: int(v)
        for k, v in body.items()
        if k in ("max_results", "max_pages_per_search") and v
    }
    return await _run_tool_job("find_icp_leads", "find_icp_leads", args)


async def run_tool(request: Request) -> JSONResponse:
    name = request.path_params["name"]
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    arguments = body.get("arguments") or {}
    if not isinstance(arguments, dict):
        return _bad("arguments must be an object")
    if name not in {t["name"] for t in engine.tools}:
        return _bad(f"Unknown tool {name!r}", 404)
    return await _run_tool_job(f"tool:{name}", name, arguments)


async def job_stop(_: Request) -> JSONResponse:
    job = hub.active_job
    if job is None or job.task is None:
        return _bad("No job running", 404)
    job.task.cancel()
    return JSONResponse({"ok": True, "job": job.id})


async def job_get(request: Request) -> JSONResponse:
    job = hub.jobs.get(int(request.path_params["id"]))
    if job is None:
        return _bad("No such job", 404)
    return JSONResponse(job.to_dict())


async def events(request: Request) -> StreamingResponse:
    q = hub.subscribe()

    async def gen():  # type: ignore[no-untyped-def]
        try:
            yield f"data: {json.dumps({'event': 'hello', 'data': {'engine': engine.status()}})}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    item = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"data: {item}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            hub.unsubscribe(q)

    return StreamingResponse(
        gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"}
    )


# ---------------------------------------------------------------------------
# routes: leads


async def leads_list(request: Request) -> JSONResponse:
    p = request.query_params
    rt = get_runtime()
    status = p.get("status") or None
    if status and status not in STATUSES:
        return _bad("unknown status")
    min_score = int(p["min_score"]) if p.get("min_score") else None
    limit = min(int(p.get("limit", 200)), 2000)
    offset = int(p.get("offset", 0))
    q = (p.get("q") or "").strip().lower()
    leads = rt.store.list_leads(
        status=status,
        min_score=min_score,
        limit=limit if not q else 2000,
        offset=0 if q else offset,
    )
    rows = [lead.to_dict() for lead in leads]
    if q:
        rows = [
            r
            for r in rows
            if q
            in " ".join(
                str(r.get(k) or "")
                for k in (
                    "name",
                    "headline",
                    "company",
                    "title",
                    "location",
                    "username",
                    "source_query",
                )
            ).lower()
        ]
        rows = rows[offset : offset + limit]
    return JSONResponse(
        {"count": len(rows), "leads": rows, "totals": rt.store.count_leads()}
    )


async def lead_patch(request: Request) -> JSONResponse:
    username = request.path_params["username"].strip().lower()
    body = await request.json()
    allowed = {
        "status",
        "score",
        "score_reason",
        "signals",
        "email",
        "notes",
        "company",
        "title",
    }
    fields = {k: v for k, v in body.items() if k in allowed}
    if "score" in fields and fields["score"] not in (None, ""):
        fields["score"] = max(0, min(100, int(fields["score"])))
    elif "score" in fields:
        fields["score"] = None
    try:
        lead = get_runtime().store.update_lead(username, **fields)
    except ValueError as e:
        return _bad(str(e))
    if lead is None:
        return _bad("No such lead", 404)
    await hub.emit("stats", _stats())
    return JSONResponse(lead.to_dict())


async def leads_export(request: Request) -> PlainTextResponse:
    p = request.query_params
    fmt = p.get("format", "csv")
    rt = get_runtime()
    leads = rt.store.list_leads(
        status=p.get("status") or None,
        min_score=int(p["min_score"]) if p.get("min_score") else None,
        limit=5000,
    )
    try:
        content = rt.store.export(leads, fmt=fmt)
    except ValueError as e:
        return PlainTextResponse(str(e), status_code=400)
    media = "application/json" if fmt == "json" else "text/csv"
    return PlainTextResponse(
        content,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="leads.{fmt}"'},
    )


# ---------------------------------------------------------------------------
# app


def create_app() -> Starlette:
    routes = [
        Route("/", index),
        Route("/api/state", state),
        Route("/api/stats", stats),
        Route("/api/guide", guide),
        Route("/api/icp", icp_get, methods=["GET"]),
        Route("/api/icp", icp_put, methods=["PUT"]),
        Route("/api/icp/validate", icp_validate, methods=["POST"]),
        Route("/api/session/check", session_check, methods=["POST"]),
        Route("/api/session/login", session_login, methods=["POST"]),
        Route("/api/session/import", session_import, methods=["POST"]),
        Route("/api/session/logout", session_logout, methods=["POST"]),
        Route("/api/engine/start", engine_start, methods=["POST"]),
        Route("/api/engine/stop", engine_stop, methods=["POST"]),
        Route("/api/engine/status", engine_status),
        Route("/api/run/find", run_find, methods=["POST"]),
        Route("/api/run/tool/{name}", run_tool, methods=["POST"]),
        Route("/api/run/stop", job_stop, methods=["POST"]),
        Route("/api/jobs/{id:int}", job_get),
        Route("/api/events", events),
        Route("/api/leads", leads_list),
        Route("/api/leads/{username}", lead_patch, methods=["PATCH"]),
        Route("/api/leads/export", leads_export),
    ]

    @contextlib.asynccontextmanager
    async def lifespan(_: Starlette):  # type: ignore[no-untyped-def]
        await hub.log(
            "info", f"Console ready. Lead store: {get_runtime().settings.db_path}"
        )
        try:
            yield
        finally:
            await engine.stop(hub.log)

    return Starlette(routes=routes, lifespan=lifespan)
