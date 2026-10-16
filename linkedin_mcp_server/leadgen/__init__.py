"""Lead-generation layer on top of the LinkedIn reading tools.

Phase one: *finding* leads.

The reading tools in :mod:`linkedin_mcp_server.tools` each do one thing once:
one search returns one page, one profile read returns one profile, and nothing
is remembered between calls. This package adds what turns those into a lead
pipeline:

* :mod:`.settings`  -- where the database and ICP file live, daily caps, pacing.
* :mod:`.db`        -- a SQLite store of every lead seen, with a usage ledger.
* :mod:`.budget`    -- per-day caps on LinkedIn actions and a working-hours window.
* :mod:`.icp`       -- the ideal-customer-profile file (``icp.yaml``).
* :mod:`.parse`     -- turns a search-results page into lead records.
* :mod:`.tools`     -- the MCP tools: ``find_icp_leads``, ``list_leads``, ...

Nothing here talks to LinkedIn directly. Every page read still goes through
the existing extractor, so the browser rules in ``AGENTS.md`` keep applying.
"""
