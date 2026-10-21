"""Local web dashboard for the lead-generation layer.

Run with ``uv run leadgen-ui`` (or ``python -m linkedin_mcp_server.leadgen.ui``)
and open http://127.0.0.1:8765. Everything stays on this machine: the page
reads the lead store directly and drives the LinkedIn MCP server as a child
process over streamable HTTP on loopback.
"""
