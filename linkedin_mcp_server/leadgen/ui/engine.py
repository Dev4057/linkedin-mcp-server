"""The MCP server as a child process, plus session (login) helpers.

The dashboard never imports the browser stack. It starts the ordinary server
(``python -m linkedin_mcp_server --transport streamable-http``) on a loopback
port and talks to it with the fastmcp client, which keeps every existing rule
(profile lease, sequential tool calls, budget middleware) in force. Login and
status checks are the server's own ``--login`` / ``--status`` /
``--import-from-browser`` / ``--logout`` flags, run as short subprocesses, so
the dashboard never sees or stores a LinkedIn password.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from fastmcp import Client

logger = logging.getLogger(__name__)

ENGINE_HOST = "127.0.0.1"
DEFAULT_ENGINE_PORT = 8766

LogFn = Callable[[str, str], Awaitable[None]]  # (level, message)


def _server_argv(*extra: str) -> list[str]:
    return [sys.executable, "-m", "linkedin_mcp_server", *extra]


@dataclass
class SessionStatus:
    valid: bool
    detail: str
    checked_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "detail": self.detail,
            "checked_at": self.checked_at,
        }


async def _run_cli(*args: str, timeout: float = 600) -> tuple[int, str]:
    """Run the server CLI with *args*, return (exit code, combined output)."""
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    proc = await asyncio.create_subprocess_exec(
        *_server_argv(*args),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=env,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, "timed out"
    return proc.returncode or 0, out.decode("utf-8", errors="replace")


def _human_lines(out: str) -> str:
    """Drop JSON log records; keep the lines a person was meant to read."""
    kept = [
        ln for ln in out.splitlines() if ln.strip() and not ln.lstrip().startswith("{")
    ]
    return "\n".join(kept).strip()


async def check_session() -> SessionStatus:
    code, out = await _run_cli("--status", timeout=60)
    text = _human_lines(out)
    if code == 0:
        return SessionStatus(True, text or "Session valid")
    return SessionStatus(False, text or "No valid LinkedIn session")


async def login_interactive(log: LogFn) -> SessionStatus:
    """Open the server's login window; returns when the user has signed in."""
    await log(
        "info", "Opening a LinkedIn login window. Sign in there; nothing is typed here."
    )
    code, out = await _run_cli("--login", timeout=600)
    for line in _human_lines(out).splitlines()[-15:]:
        await log("info", line)
    return await check_session()


async def import_from_browser(browser: str, log: LogFn) -> SessionStatus:
    await log("info", f"Importing the LinkedIn session from {browser}...")
    code, out = await _run_cli("--import-from-browser", browser, timeout=180)
    for line in _human_lines(out).splitlines()[-15:]:
        await log("info", line)
    return await check_session()


async def logout(log: LogFn) -> SessionStatus:
    await log("warn", "Clearing the stored LinkedIn session.")
    code, out = await _run_cli("--logout", timeout=60)
    for line in _human_lines(out).splitlines()[-8:]:
        await log("info", line)
    return await check_session()


class Engine:
    """Owns the child MCP server process and a client to it."""

    def __init__(
        self, port: int = DEFAULT_ENGINE_PORT, *, headless: bool = True
    ) -> None:
        self.port = port
        self.headless = headless
        self._proc: subprocess.Popen[bytes] | None = None
        self._started_at: float | None = None
        self._lock = asyncio.Lock()
        self.tools: list[dict[str, Any]] = []

    @property
    def url(self) -> str:
        return f"http://{ENGINE_HOST}:{self.port}/mcp"

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def status(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "pid": self._proc.pid if self.running and self._proc else None,
            "url": self.url,
            "uptime_seconds": (time.time() - self._started_at)
            if self.running and self._started_at
            else 0,
            "tool_count": len(self.tools),
            "headless": self.headless,
        }

    async def start(self, log: LogFn) -> dict[str, Any]:
        async with self._lock:
            if self.running:
                return self.status()
            argv = _server_argv(
                "--transport",
                "streamable-http",
                "--host",
                ENGINE_HOST,
                "--port",
                str(self.port),
                "--path",
                "/mcp",
                "--log-level",
                "INFO",
            )
            if not self.headless:
                argv.append("--no-headless")
            await log("info", f"Starting LinkedIn engine on {self.url}")
            creation = {}
            if sys.platform == "win32":
                creation["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
            self._proc = subprocess.Popen(
                argv,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                **creation,
            )
            self._started_at = time.time()
        # Wait for tools/list to answer.
        deadline = time.time() + 90
        last_error = ""
        while time.time() < deadline:
            if not self.running:
                raise RuntimeError("The engine process exited during startup.")
            try:
                async with Client(self.url, timeout=10) as client:
                    tools = await client.list_tools()
                self.tools = [
                    {
                        "name": t.name,
                        "title": getattr(t, "title", None) or t.name,
                        "description": (t.description or "").strip(),
                        "input_schema": t.inputSchema,
                        "tags": sorted(
                            (getattr(t, "meta", None) or {})
                            .get("fastmcp", {})
                            .get("tags", [])
                        ),
                    }
                    for t in tools
                ]
                await log("ok", f"Engine ready: {len(self.tools)} tools")
                return self.status()
            except Exception as e:  # noqa: BLE001 - startup polling
                last_error = str(e)
                await asyncio.sleep(1.0)
        await self.stop(log)
        raise RuntimeError(f"Engine did not become ready: {last_error}")

    async def stop(self, log: LogFn) -> dict[str, Any]:
        async with self._lock:
            proc = self._proc
            self._proc = None
            self.tools = []
        if proc is None or proc.poll() is not None:
            return self.status()
        await log("info", "Stopping LinkedIn engine...")
        try:
            proc.terminate()
            for _ in range(50):
                if proc.poll() is not None:
                    break
                await asyncio.sleep(0.1)
            else:
                proc.kill()
        except Exception:  # noqa: BLE001
            logger.exception("Failed to stop engine")
        await log("ok", "Engine stopped")
        return self.status()

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        progress: Callable[[float, float | None, str | None], Awaitable[None]]
        | None = None,
        timeout: float = 3600,
    ) -> Any:
        if not self.running:
            raise RuntimeError(
                "The engine is not running. Start it from the Session page."
            )

        async def _progress(p: float, total: float | None, message: str | None) -> None:
            if progress is not None:
                await progress(p, total, message)

        async with Client(
            self.url, timeout=timeout, progress_handler=_progress
        ) as client:
            result = await client.call_tool(name, arguments or {}, timeout=timeout)
        data = getattr(result, "data", None)
        if data is not None:
            return data
        structured = getattr(result, "structured_content", None)
        if structured is not None:
            return structured
        content = getattr(result, "content", None) or []
        return [getattr(c, "text", str(c)) for c in content]
