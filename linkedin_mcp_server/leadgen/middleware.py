"""Middleware that charges every LinkedIn-reading tool call to the daily budget.

Sits inside the serializing middleware so the check happens right before the
browser is used. A call that would exceed its cap is refused with a
``ToolError`` naming the cap; nothing is loaded.

The leadgen tools (``find_icp_leads`` and friends) are not listed in
``TOOL_ACTIONS``: they spend per page themselves, which is more accurate than
one charge per call for a tool that loads many pages.
"""

from __future__ import annotations

import logging

import mcp.types as mt
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import ToolResult

from linkedin_mcp_server.leadgen.budget import BudgetExhausted
from linkedin_mcp_server.leadgen.runtime import get_runtime

logger = logging.getLogger(__name__)


class BudgetMiddleware(Middleware):
    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        tool_name = context.message.name
        runtime = get_runtime()
        action = runtime.budget.action_for_tool(tool_name)
        if action is None:
            return await call_next(context)
        try:
            runtime.budget.spend(action)
        except BudgetExhausted as e:
            logger.info("Refused %s: %s", tool_name, e)
            raise ToolError(f"{tool_name} refused by the lead-gen budget: {e}") from e
        return await call_next(context)
