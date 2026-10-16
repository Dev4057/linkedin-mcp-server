"""The lead-finding loop behind ``find_icp_leads``.

Kept apart from the MCP tool so it can be driven by any ``search`` coroutine
(the real extractor, or a fake in tests). The loop:

1. derive the searches from the ICP (title x location x company);
2. for each search, read page 1, 2, ... up to ``max_pages_per_search``;
3. parse each page, drop excluded people, upsert the rest;
4. stop early when the ICP's ``max_results`` new leads are reached, when a
   page returns nobody new (the search is exhausted), when LinkedIn
   rate-limits, or when the daily search budget runs out;
5. pause a random interval between page loads.

Every page load is one ``searches`` unit of budget, charged *before* the
load so a refusal never costs a request.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol

from linkedin_mcp_server.leadgen.budget import BudgetExhausted
from linkedin_mcp_server.leadgen.icp import ICP, SearchSpec
from linkedin_mcp_server.leadgen.parse import parse_search_page
from linkedin_mcp_server.leadgen.runtime import LeadgenRuntime

logger = logging.getLogger(__name__)

SearchFn = Callable[[SearchSpec, int], Awaitable[dict[str, Any]]]


class Progress(Protocol):
    async def __call__(self, done: int, total: int, message: str) -> None: ...


@dataclass
class FindResult:
    icp_name: str
    searches_planned: int
    searches_run: int
    pages_read: int
    found: int
    new: int
    already_known: int
    skipped: list[dict[str, Any]] = field(default_factory=list)
    stopped_because: str = "completed"
    per_search: list[dict[str, Any]] = field(default_factory=list)
    new_usernames: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "icp": self.icp_name,
            "searches_planned": self.searches_planned,
            "searches_run": self.searches_run,
            "pages_read": self.pages_read,
            "found": self.found,
            "new": self.new,
            "already_known": self.already_known,
            "skipped": len(self.skipped),
            "skipped_examples": self.skipped[:10],
            "stopped_because": self.stopped_because,
            "per_search": self.per_search,
            "new_usernames": self.new_usernames[:200],
        }


async def find_leads(
    *,
    icp: ICP,
    runtime: LeadgenRuntime,
    search: SearchFn,
    progress: Progress | None = None,
    max_results: int | None = None,
    max_pages: int | None = None,
    pause: bool = True,
) -> FindResult:
    specs = list(icp.searches())
    cap_results = max_results or icp.max_results
    cap_pages = max_pages or icp.max_pages_per_search
    result = FindResult(
        icp_name=icp.name,
        searches_planned=len(specs),
        searches_run=0,
        pages_read=0,
        found=0,
        new=0,
        already_known=0,
    )
    run_id = runtime.store.start_run("find_icp_leads")
    seen_this_run: set[str] = set()
    total_steps = len(specs) * cap_pages

    async def report(message: str) -> None:
        if progress is not None:
            await progress(result.pages_read, total_steps, message)

    try:
        for spec in specs:
            if result.new >= cap_results:
                result.stopped_because = f"reached max_results ({cap_results})"
                break
            result.searches_run += 1
            search_summary = {"query": spec.label, "pages": 0, "found": 0, "new": 0}
            result.per_search.append(search_summary)
            for page_number in range(1, cap_pages + 1):
                if result.new >= cap_results:
                    break
                try:
                    runtime.budget.spend("searches")
                except BudgetExhausted as e:
                    result.stopped_because = str(e)
                    logger.info("find_leads stopping: %s", e)
                    return result
                await report(f"{spec.label} - page {page_number}")
                raw = await search(spec, page_number)
                result.pages_read += 1
                search_summary["pages"] += 1
                page = parse_search_page(raw, source_query=spec.label, exclude=icp)
                result.skipped.extend(page.skipped)
                if page.rate_limited:
                    result.stopped_because = "LinkedIn rate-limited the search page"
                    return result
                if page.error and not page.leads:
                    search_summary["error"] = page.error.get("error_message") or str(
                        page.error
                    )
                    break
                page_new = 0
                for lead in page.leads:
                    if lead.username in seen_this_run:
                        continue
                    seen_this_run.add(lead.username)
                    result.found += 1
                    search_summary["found"] += 1
                    _, is_new = runtime.store.upsert_lead(lead)
                    if is_new:
                        page_new += 1
                        result.new += 1
                        search_summary["new"] += 1
                        result.new_usernames.append(lead.username)
                    else:
                        result.already_known += 1
                if not page.leads:
                    # An empty page means the search ran out of results.
                    break
                if page_new == 0:
                    # Every person on this page was already in the store, so the
                    # pages after it were almost certainly read on an earlier run.
                    # Stopping here is what makes a daily re-run cheap.
                    break
                if pause and page_number < cap_pages:
                    await runtime.budget.pause()
        if result.new >= cap_results and result.stopped_because == "completed":
            result.stopped_because = f"reached max_results ({cap_results})"
        return result
    finally:
        runtime.store.finish_run(run_id, result.to_dict())
