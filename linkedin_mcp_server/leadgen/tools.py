"""MCP tools for the lead-generation layer (phase one: finding leads).

* ``find_icp_leads``  -- run every search in icp.yaml, page through results,
  de-duplicate against the store, save new leads. Charged per page.
* ``list_leads``      -- read the store (filter by status / score / query).
* ``update_lead``     -- change status, score, notes, email on one lead.
* ``export_leads``    -- CSV or JSON of the leads that pass a score bar.
* ``leadgen_stats``   -- today's budget use, store counts, recent runs.
* ``show_icp``        -- the parsed ICP and the searches it will generate.

Only ``find_icp_leads`` touches LinkedIn. The rest read the database.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from linkedin_mcp_server.config.schema import DEFAULT_TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.core.exceptions import AuthenticationError
from linkedin_mcp_server.dependencies import get_ready_extractor, handle_auth_error
from linkedin_mcp_server.error_handler import raise_tool_error
from linkedin_mcp_server.leadgen.db import STATUSES
from linkedin_mcp_server.leadgen.finder import find_leads
from linkedin_mcp_server.leadgen.icp import ICPError, SearchSpec
from linkedin_mcp_server.leadgen.runtime import get_runtime

logger = logging.getLogger(__name__)

# A whole-ICP run reads many pages with pauses between them, so it gets a
# longer timeout than a single-page tool.
FIND_TIMEOUT_MULTIPLIER = 10


def register_leadgen_tools(
    mcp: FastMCP, *, tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS
) -> None:
    """Register the lead-generation tools with the MCP server."""

    @mcp.tool(
        timeout=tool_timeout * FIND_TIMEOUT_MULTIPLIER,
        title="Find ICP Leads",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"leadgen", "search"},
    )
    async def find_icp_leads(
        ctx: Context,
        max_results: Annotated[int, Field(ge=1, le=1000)] | None = None,
        max_pages_per_search: Annotated[int, Field(ge=1, le=20)] | None = None,
    ) -> dict[str, Any]:
        """
        Find people matching the ideal customer profile and save them as leads.

        Reads icp.yaml (titles, locations, keywords, exclusions), runs one
        LinkedIn people search per title x location, pages through each,
        skips anyone matching exclude_keywords, and stores every new person in
        the leads database with status "new". People already in the database
        are counted as already_known and not stored twice.

        Stops early when max_results new leads are found, when a search runs
        dry, when LinkedIn rate-limits, or when today's search budget is used
        up (see leadgen_stats). Each page read costs one "searches" unit.

        Args:
            ctx: FastMCP context for progress reporting
            max_results: Stop after this many NEW leads (default from icp.yaml, 100).
            max_pages_per_search: Pages to read per search (default from icp.yaml, 3).

        Returns:
            Summary: searches planned/run, pages read, found, new, already_known,
            skipped (with examples), stopped_because, per_search breakdown and the
            usernames of new leads. Use list_leads to read the leads themselves.
        """
        runtime = get_runtime()
        try:
            icp = runtime.load_icp()
        except (FileNotFoundError, ICPError, OSError) as e:
            raise ToolError(str(e)) from e

        try:
            extractor = await get_ready_extractor(ctx, tool_name="find_icp_leads")

            async def search(spec: SearchSpec, page: int) -> dict[str, Any]:
                logger.info("find_icp_leads: %s (page %d)", spec.label, page)
                return await extractor.search_people(
                    spec.keywords,
                    spec.location,
                    network=spec.network,
                    current_company=spec.current_company,
                    page=page,
                )

            async def progress(done: int, total: int, message: str) -> None:
                await ctx.report_progress(progress=done, total=total, message=message)

            result = await find_leads(
                icp=icp,
                runtime=runtime,
                search=search,
                progress=progress,
                max_results=max_results,
                max_pages=max_pages_per_search,
            )
            await ctx.report_progress(progress=100, total=100, message="Complete")
            summary = result.to_dict()
            summary["budget"] = runtime.budget.status().to_dict()
            return summary
        except ToolError:
            raise
        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "find_icp_leads")
        except Exception as e:
            raise_tool_error(e, "find_icp_leads")  # NoReturn

    @mcp.tool(
        title="List Leads",
        annotations={"readOnlyHint": True},
        tags={"leadgen"},
    )
    async def list_leads(
        status: str | None = None,
        min_score: Annotated[int, Field(ge=0, le=100)] | None = None,
        source_query: str | None = None,
        limit: Annotated[int, Field(ge=1, le=500)] = 50,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> dict[str, Any]:
        """
        List leads from the local database (no LinkedIn access).

        Args:
            status: Filter by pipeline status: new, qualified, disqualified,
                enriched, contacted, replied, closed.
            min_score: Only leads scored at or above this value.
            source_query: Only leads found by this search label
                (as shown in find_icp_leads per_search[].query).
            limit: Page size (default 50).
            offset: Skip this many rows.

        Returns:
            {count, leads: [...]} ordered by score (highest first) then recency.
        """
        if status is not None and status not in STATUSES:
            raise ToolError(f"Unknown status {status!r}; expected one of {STATUSES}")
        store = get_runtime().store
        leads = store.list_leads(
            status=status,
            min_score=min_score,
            source_query=source_query,
            limit=limit,
            offset=offset,
        )
        return {"count": len(leads), "leads": [lead.to_dict() for lead in leads]}

    @mcp.tool(
        title="Update Lead",
        annotations={"idempotentHint": True},
        tags={"leadgen"},
    )
    async def update_lead(
        linkedin_username: str,
        status: str | None = None,
        score: Annotated[int, Field(ge=0, le=100)] | None = None,
        score_reason: str | None = None,
        signals: list[str] | None = None,
        email: str | None = None,
        notes: str | None = None,
        company: str | None = None,
        title: str | None = None,
    ) -> dict[str, Any]:
        """
        Update one lead in the local database.

        Args:
            linkedin_username: The lead's LinkedIn username (from list_leads).
            status: New status (new, qualified, disqualified, enriched,
                contacted, replied, closed).
            score: 0-100 fit score.
            score_reason: One line explaining the score.
            signals: Buying signals observed (e.g. "hiring SDRs", "posted about outbound").
            email: Work email once found.
            notes: Free text.
            company: Correct the company name.
            title: Correct the job title.

        Returns:
            The updated lead, or an error if the username is unknown.
        """
        fields = {
            k: v
            for k, v in {
                "status": status,
                "score": score,
                "score_reason": score_reason,
                "signals": signals,
                "email": email,
                "notes": notes,
                "company": company,
                "title": title,
            }.items()
            if v is not None
        }
        try:
            lead = get_runtime().store.update_lead(
                linkedin_username.strip().lower(), **fields
            )
        except ValueError as e:
            raise ToolError(str(e)) from e
        if lead is None:
            raise ToolError(f"No lead with username {linkedin_username!r}")
        return lead.to_dict()

    @mcp.tool(
        title="Export Leads",
        annotations={"readOnlyHint": True},
        tags={"leadgen"},
    )
    async def export_leads(
        format: str = "csv",
        status: str | None = None,
        min_score: Annotated[int, Field(ge=0, le=100)] | None = None,
        limit: Annotated[int, Field(ge=1, le=5000)] = 1000,
    ) -> dict[str, Any]:
        """
        Export leads as CSV or JSON text, ready for Apollo enrichment or a CRM.

        Args:
            format: "csv" (default) or "json".
            status: Optional status filter (see list_leads).
            min_score: Only leads scored at or above this value.
            limit: Maximum rows (default 1000).

        Returns:
            {format, count, content} where content is the file text.
        """
        if status is not None and status not in STATUSES:
            raise ToolError(f"Unknown status {status!r}; expected one of {STATUSES}")
        store = get_runtime().store
        leads = store.list_leads(status=status, min_score=min_score, limit=limit)
        try:
            content = store.export(leads, fmt=format)
        except ValueError as e:
            raise ToolError(str(e)) from e
        return {"format": format, "count": len(leads), "content": content}

    @mcp.tool(
        title="Lead-gen Stats",
        annotations={"readOnlyHint": True},
        tags={"leadgen"},
    )
    async def leadgen_stats() -> dict[str, Any]:
        """
        Today's LinkedIn budget use, lead counts by status, and recent runs.

        Returns:
            {budget: {day, working_hours, within_hours, actions: {...}},
             leads: {total, new, qualified, ...},
             settings: {db_path, icp_path, seen_days, delay_seconds},
             recent_runs: [...]}
        """
        runtime = get_runtime()
        settings = runtime.settings
        return {
            "budget": runtime.budget.status().to_dict(),
            "leads": runtime.store.count_leads(),
            "settings": {
                "db_path": str(settings.db_path),
                "icp_path": str(settings.icp_path) if settings.icp_path else None,
                "seen_days": settings.seen_days,
                "delay_seconds": [
                    settings.min_delay_seconds,
                    settings.max_delay_seconds,
                ],
            },
            "recent_runs": runtime.store.recent_runs(5),
        }

    @mcp.tool(
        title="Show ICP",
        annotations={"readOnlyHint": True},
        tags={"leadgen"},
    )
    async def show_icp() -> dict[str, Any]:
        """
        Show the parsed icp.yaml and the exact searches find_icp_leads will run.

        Returns:
            {icp: {...}, searches: [{keywords, location, network, current_company}]}
        """
        try:
            icp = get_runtime().load_icp()
        except (FileNotFoundError, ICPError, OSError) as e:
            raise ToolError(str(e)) from e
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
