# Lead generation (phase one: finding leads)

This fork adds a lead-finding layer on top of the LinkedIn reading tools.
Phase one turns "search once, forget everything" into "search the whole ICP,
remember everyone, stay inside daily limits".

## What it adds

| Piece | Where | What it does |
|---|---|---|
| `icp.yaml` | repo root (or `~/.linkedin-mcp/icp.yaml`, or `LEADGEN_ICP=`) | The ideal customer profile: titles, locations, keywords, exclusions. |
| Lead store | `~/.linkedin-mcp/leadgen.sqlite3` (or `LEADGEN_DB=`) | Every lead ever found, with status/score/notes, plus a usage ledger. |
| Budget guard | `linkedin_mcp_server/leadgen/budget.py` | Daily caps per action, random delays, working-hours window. Charged on **every** LinkedIn-reading tool via middleware. |
| Paging | `search_people(page=N)` | Read page 2, 3, ... of a search (10 people per page). |
| Tools | `linkedin_mcp_server/leadgen/tools.py` | `find_icp_leads`, `list_leads`, `update_lead`, `export_leads`, `leadgen_stats`, `show_icp`. |

Nothing bypasses the existing extractor: every page is still read through the
same browser session, with the same rules.

## Quick start

```bash
cp icp.example.yaml icp.yaml     # edit titles / locations / exclusions
uv sync
uv run -m linkedin_mcp_server --login    # once, to save the LinkedIn session
uv run -m linkedin_mcp_server            # or via your MCP client config
```

Then, from Claude (or any MCP client):

1. `show_icp` - confirms the file parsed and lists the exact searches.
2. `find_icp_leads` - runs them, pages, de-duplicates, saves. Returns a summary.
3. `list_leads status=new` - see what came in.
4. `leadgen_stats` - budget used today, lead counts, recent runs.
5. `export_leads format=csv min_score=0` - hand the list to Apollo / a CRM.

## How a run behaves

```
for each title x location in icp.yaml:
    for page in 1..max_pages_per_search:
        check budget ("searches")   -> stop run if exhausted
        read page                   -> 1 search unit spent
        parse people from references (name, headline, location, snippet)
        drop anyone matching exclude_keywords
        upsert into the store; count new vs already_known
        stop this search if the page had nobody new, or nobody at all
        random pause (4-12 s by default)
    stop run if max_results new leads reached
```

`already_known` grows on every re-run; that is the memory working. A second
run on the same ICP typically reads one page per search and stops.

## Budget and safety

Defaults (per day, local midnight reset, persisted in the store):

| Action | Cap | Spent by |
|---|---|---|
| searches | 20 | `search_people`, `search_companies`, `search_jobs`, `search_posts`, each page of `find_icp_leads` |
| profile_views | 80 | `get_person_profile`, `get_company_profile`, `get_company_employees`, `get_company_posts`, `get_sidebar_profiles`, `get_job_details` |
| connects | 15 | `connect_with_person` |
| messages | 10 | `send_message` |

Outside working hours (08:00-20:00 local by default) every budgeted call is
refused with a clear message. A refused call never loads a page.

Override with environment variables (`LEADGEN_DAILY_SEARCHES`,
`LEADGEN_DAILY_PROFILE_VIEWS`, `LEADGEN_DAILY_CONNECTS`,
`LEADGEN_DAILY_MESSAGES`, `LEADGEN_HOURS=8-20|off`, `LEADGEN_MIN_DELAY`,
`LEADGEN_MAX_DELAY`, `LEADGEN_SEEN_DAYS`) or, per campaign, with the
`leadgen:` block in `icp.yaml`, which wins.

These caps are deliberately below the limits in `AGENTS.md` (100 calls per
tool per day, 30 invitations). LinkedIn's User Agreement still prohibits
automated access; this layer reduces the risk, it does not remove it.

## Lead record

```
username, name, headline, location, company, title, profile_url,
source_query, snippet, score, score_reason, signals[], status, email, notes,
first_seen, last_seen, last_viewed, times_seen
```

Statuses: `new` -> `qualified` / `disqualified` -> `enriched` -> `contacted`
-> `replied` -> `closed`. Phase one only ever writes `new`; `update_lead`
moves a lead along by hand, and phase two's `score_lead` will do it
automatically.

`company` and `title` are a best-effort split of the headline on
"at" / "@" / "|" / "-". The raw `snippet` is always kept so nothing is lost
when the heuristic misreads a headline like "Founder | Speaker".

## What phase two adds (not in this change)

* `score_lead` - reads the profile and company, scores 0-100 against
  `scoring:` in the ICP, writes `score`, `score_reason`, `signals`, `status`.
* Signal tools - recent posts, job changes, hiring at the company.
* `draft_connection_notes` - a 300-character note per qualified lead.
* Apollo enrichment and the email sequence live outside this server.

## Tests

```bash
uv run pytest tests/test_leadgen.py
```

The three contract fixtures that pin the tool inventory
(`tests/fixtures/tool-contract/tools.json`,
`tests/fixtures/policy-traces/v1/facade-contract.json`, and the counts in
`tests/linkedin/`) were re-recorded for the new tools and the `page`
parameter; the diffs are additions only.

## Dashboard

A local web console wraps all of the above: `uv run leadgen-ui` opens
http://127.0.0.1:8765 with pages for the target profile, LinkedIn sign-in,
engine control, running `find_icp_leads` with a live log, the leads table,
a tool console and a guide. See [leadgen-ui.md](leadgen-ui.md).
