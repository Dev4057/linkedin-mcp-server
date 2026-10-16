"""Daily caps, pacing, and a working-hours window for LinkedIn actions.

The guard answers one question before any page load: *may this action happen
now?* If not it raises :class:`BudgetExhausted` with a message that says
which cap was hit and when it resets, so an agent in a loop stops instead of
retrying into a restriction.

Actions are coarse on purpose. ``searches``, ``profile_views``, ``connects``
and ``messages`` are what LinkedIn visibly rate-limits; a finer split would
just be more knobs to set wrong.

The ledger lives in the lead store (``usage`` table) so a restart does not
reset the day.
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from linkedin_mcp_server.leadgen.db import LeadStore
from linkedin_mcp_server.leadgen.settings import LeadgenSettings

logger = logging.getLogger(__name__)

# Which MCP tool spends which budget. Tools absent here are free (they read
# nothing from LinkedIn, or are the leadgen tools that spend explicitly).
TOOL_ACTIONS: dict[str, str] = {
    "search_people": "searches",
    "search_companies": "searches",
    "search_jobs": "searches",
    "search_posts": "searches",
    "get_person_profile": "profile_views",
    "get_company_profile": "profile_views",
    "get_company_employees": "profile_views",
    "get_company_posts": "profile_views",
    "get_sidebar_profiles": "profile_views",
    "get_job_details": "profile_views",
    "connect_with_person": "connects",
    "send_message": "messages",
}


class BudgetExhausted(Exception):
    """Raised when an action would exceed today's cap or fall outside hours."""


@dataclass(frozen=True)
class BudgetStatus:
    day: str
    used: dict[str, int]
    caps: dict[str, int]
    working_hours: str
    within_hours: bool

    def remaining(self, action: str) -> int:
        return max(0, self.caps.get(action, 0) - self.used.get(action, 0))

    def to_dict(self) -> dict:
        return {
            "day": self.day,
            "working_hours": self.working_hours,
            "within_hours": self.within_hours,
            "actions": {
                action: {
                    "used": self.used.get(action, 0),
                    "cap": cap,
                    "remaining": max(0, cap - self.used.get(action, 0)),
                }
                for action, cap in self.caps.items()
            },
        }


class BudgetGuard:
    def __init__(
        self,
        store: LeadStore,
        settings: LeadgenSettings,
        *,
        clock: Callable[[], datetime] = datetime.now,
        rng: random.Random | None = None,
    ) -> None:
        self._store = store
        self._settings = settings
        self._clock = clock
        self._rng = rng or random.Random()

    # -- queries ---------------------------------------------------------------

    def today(self) -> str:
        return self._clock().date().isoformat()

    def status(self) -> BudgetStatus:
        now = self._clock()
        return BudgetStatus(
            day=now.date().isoformat(),
            used=self._store.usage_for_day(now.date().isoformat()),
            caps=dict(self._settings.daily_caps),
            working_hours=self._settings.working_hours.describe(),
            within_hours=self._settings.working_hours.contains(now.hour),
        )

    def remaining(self, action: str) -> int:
        return self.status().remaining(action)

    # -- enforcement -------------------------------------------------------------

    def check(self, action: str, amount: int = 1) -> None:
        """Raise if ``amount`` more of ``action`` is not allowed right now."""
        if action not in self._settings.daily_caps:
            return  # unbudgeted action
        now = self._clock()
        if not self._settings.working_hours.contains(now.hour):
            raise BudgetExhausted(
                f"Outside working hours ({self._settings.working_hours.describe()}); "
                f"it is {now.strftime('%H:%M')} now. Set LEADGEN_HOURS=off to disable."
            )
        day = now.date().isoformat()
        used = self._store.usage(day, action)
        cap = self._settings.daily_caps[action]
        if used + amount > cap:
            raise BudgetExhausted(
                f"Daily cap reached for {action}: {used}/{cap} used today ({day}). "
                "Resets at midnight local time; raise LEADGEN_DAILY_* to change."
            )

    def spend(self, action: str, amount: int = 1) -> int:
        """Check, then record. Returns the new count for today."""
        self.check(action, amount)
        if action not in self._settings.daily_caps:
            return 0
        count = self._store.record_usage(self.today(), action, amount)
        logger.debug(
            "Budget: %s -> %d/%d", action, count, self._settings.daily_caps[action]
        )
        return count

    def action_for_tool(self, tool_name: str) -> str | None:
        return TOOL_ACTIONS.get(tool_name)

    # -- pacing ------------------------------------------------------------------

    def next_delay(self) -> float:
        lo, hi = self._settings.min_delay_seconds, self._settings.max_delay_seconds
        if hi < lo:
            lo, hi = hi, lo
        return self._rng.uniform(lo, hi)

    async def pause(self) -> float:
        """Sleep a random human-looking interval between page loads."""
        delay = self.next_delay()
        if delay > 0:
            await asyncio.sleep(delay)
        return delay
