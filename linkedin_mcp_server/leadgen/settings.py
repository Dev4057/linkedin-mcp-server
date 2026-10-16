"""Settings for the lead-generation layer.

Read from environment variables with safe defaults, so nothing in the main
configuration loader has to change. Every value can also be overridden by a
``leadgen`` block in ``icp.yaml`` (see :mod:`.icp`), which wins over the
environment because it travels with the campaign.

Environment keys:

* ``LEADGEN_DB``            -- SQLite file. Default ``~/.linkedin-mcp/leadgen.sqlite3``.
* ``LEADGEN_ICP``           -- ICP file. Default ``./icp.yaml`` then ``~/.linkedin-mcp/icp.yaml``.
* ``LEADGEN_DAILY_SEARCHES``, ``LEADGEN_DAILY_PROFILE_VIEWS``,
  ``LEADGEN_DAILY_CONNECTS``, ``LEADGEN_DAILY_MESSAGES`` -- daily caps.
* ``LEADGEN_MIN_DELAY`` / ``LEADGEN_MAX_DELAY`` -- seconds to pause between
  page loads inside a lead-finding run.
* ``LEADGEN_HOURS``         -- ``"8-20"`` style local working-hours window, or
  ``"off"`` to disable the window.
* ``LEADGEN_SEEN_DAYS``     -- a profile seen within this many days is skipped.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

DEFAULT_DB = "~/.linkedin-mcp/leadgen.sqlite3"
DEFAULT_ICP_CANDIDATES = ("icp.yaml", "~/.linkedin-mcp/icp.yaml")

# Conservative defaults. LinkedIn's own guidance in AGENTS.md is 100 calls per
# tool per day and 30 invitations; these sit well under that so a run that hits
# the cap is still far from a restriction.
DEFAULT_DAILY_CAPS: dict[str, int] = {
    "searches": 20,
    "profile_views": 80,
    "connects": 15,
    "messages": 10,
}


@dataclass(frozen=True)
class WorkingHours:
    """Local-time window in which LinkedIn actions are allowed."""

    start_hour: int = 8
    end_hour: int = 20
    enabled: bool = True

    def contains(self, hour: int) -> bool:
        if not self.enabled:
            return True
        if self.start_hour <= self.end_hour:
            return self.start_hour <= hour < self.end_hour
        # A window crossing midnight, e.g. 22-6.
        return hour >= self.start_hour or hour < self.end_hour

    def describe(self) -> str:
        if not self.enabled:
            return "any time"
        return f"{self.start_hour:02d}:00-{self.end_hour:02d}:00 local"


@dataclass(frozen=True)
class LeadgenSettings:
    db_path: Path
    icp_path: Path | None
    daily_caps: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_DAILY_CAPS))
    min_delay_seconds: float = 4.0
    max_delay_seconds: float = 12.0
    working_hours: WorkingHours = field(default_factory=WorkingHours)
    seen_days: int = 30

    def with_overrides(self, overrides: dict[str, Any] | None) -> "LeadgenSettings":
        """Apply a ``leadgen`` block from the ICP file."""
        if not overrides:
            return self
        caps = dict(self.daily_caps)
        for key, value in (overrides.get("daily_caps") or {}).items():
            if key in caps:
                caps[key] = int(value)
        hours = self.working_hours
        if "working_hours" in overrides:
            hours = parse_working_hours(str(overrides["working_hours"]))
        return replace(
            self,
            daily_caps=caps,
            min_delay_seconds=float(
                overrides.get("min_delay_seconds", self.min_delay_seconds)
            ),
            max_delay_seconds=float(
                overrides.get("max_delay_seconds", self.max_delay_seconds)
            ),
            working_hours=hours,
            seen_days=int(overrides.get("seen_days", self.seen_days)),
        )


def parse_working_hours(text: str) -> WorkingHours:
    """``"8-20"`` -> WorkingHours(8, 20); ``"off"`` disables the window."""
    cleaned = text.strip().lower()
    if cleaned in {"", "off", "none", "always", "24/7"}:
        return WorkingHours(enabled=False)
    try:
        start_text, end_text = cleaned.split("-", 1)
        start, end = int(start_text), int(end_text)
    except ValueError as e:
        raise ValueError(
            f"LEADGEN_HOURS must look like '8-20' or 'off'; got {text!r}"
        ) from e
    if not (0 <= start <= 24 and 0 <= end <= 24):
        raise ValueError(f"LEADGEN_HOURS hours must be 0-24; got {text!r}")
    return WorkingHours(start_hour=start, end_hour=end, enabled=True)


def _env_int(key: str, default: int) -> int:
    raw = os.environ.get(key)
    if raw is None or not raw.strip():
        return default
    return int(raw)


def _env_float(key: str, default: float) -> float:
    raw = os.environ.get(key)
    if raw is None or not raw.strip():
        return default
    return float(raw)


def _find_icp_path() -> Path | None:
    explicit = os.environ.get("LEADGEN_ICP")
    if explicit and explicit.strip():
        return Path(explicit).expanduser()
    for candidate in DEFAULT_ICP_CANDIDATES:
        path = Path(candidate).expanduser()
        if path.exists():
            return path
    return None


def load_settings() -> LeadgenSettings:
    """Build settings from the environment (no file reads beyond existence checks)."""
    caps = {
        "searches": _env_int("LEADGEN_DAILY_SEARCHES", DEFAULT_DAILY_CAPS["searches"]),
        "profile_views": _env_int(
            "LEADGEN_DAILY_PROFILE_VIEWS", DEFAULT_DAILY_CAPS["profile_views"]
        ),
        "connects": _env_int("LEADGEN_DAILY_CONNECTS", DEFAULT_DAILY_CAPS["connects"]),
        "messages": _env_int("LEADGEN_DAILY_MESSAGES", DEFAULT_DAILY_CAPS["messages"]),
    }
    hours_text = os.environ.get("LEADGEN_HOURS")
    hours = parse_working_hours(hours_text) if hours_text else WorkingHours()
    return LeadgenSettings(
        db_path=Path(os.environ.get("LEADGEN_DB") or DEFAULT_DB).expanduser(),
        icp_path=_find_icp_path(),
        daily_caps=caps,
        min_delay_seconds=_env_float("LEADGEN_MIN_DELAY", 4.0),
        max_delay_seconds=_env_float("LEADGEN_MAX_DELAY", 12.0),
        working_hours=hours,
        seen_days=_env_int("LEADGEN_SEEN_DAYS", 30),
    )
