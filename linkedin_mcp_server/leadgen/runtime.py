"""Process-wide lead-generation runtime: settings, store, budget guard.

Created lazily on first use so that importing the server never opens a
database, and reset-able so tests can point it at a temporary file.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from linkedin_mcp_server.leadgen.budget import BudgetGuard
from linkedin_mcp_server.leadgen.db import LeadStore
from linkedin_mcp_server.leadgen.icp import ICP, load_icp
from linkedin_mcp_server.leadgen.settings import LeadgenSettings, load_settings


@dataclass
class LeadgenRuntime:
    settings: LeadgenSettings
    store: LeadStore
    budget: BudgetGuard

    def load_icp(self) -> ICP:
        """Read the ICP file fresh each time; it is small and edited often."""
        if self.settings.icp_path is None:
            raise FileNotFoundError(
                "No ICP file found. Create icp.yaml in the working directory "
                "(or ~/.linkedin-mcp/icp.yaml), or set LEADGEN_ICP=/path/to/icp.yaml. "
                "See docs/leadgen.md for the format."
            )
        icp = load_icp(self.settings.icp_path)
        if icp.leadgen_overrides:
            # The campaign file wins over the environment for caps and pacing.
            self.settings = self.settings.with_overrides(icp.leadgen_overrides)
            self.budget = BudgetGuard(self.store, self.settings)
        return icp


_runtime: LeadgenRuntime | None = None
_lock = threading.Lock()


def get_runtime() -> LeadgenRuntime:
    global _runtime
    with _lock:
        if _runtime is None:
            settings = load_settings()
            store = LeadStore(settings.db_path)
            _runtime = LeadgenRuntime(
                settings=settings, store=store, budget=BudgetGuard(store, settings)
            )
        return _runtime


def set_runtime(runtime: LeadgenRuntime | None) -> None:
    """Replace the singleton (tests) or drop it (``None``)."""
    global _runtime
    with _lock:
        if _runtime is not None and _runtime is not runtime:
            _runtime.store.close()
        _runtime = runtime
