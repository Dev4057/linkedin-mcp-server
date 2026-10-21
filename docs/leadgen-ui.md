# Leadgen console guide

## What this is

A local control panel for the lead-finding pipeline. It runs on your computer, reads the same lead database the MCP tools use, and drives the LinkedIn engine for you. Nothing leaves your machine, and the console never stores your LinkedIn password.

The strip across the top is the pipeline. Each box is one stage; when a run is active the stage in use lights up and packets travel along the wires. The meter on the right is today's remaining budget.

## Start here (first time)

1. Open **Target profile** (page 2). Edit the job titles, locations and exclusions. Press **Save profile**.
2. Open **LinkedIn & engine** (page 3). Press **Sign in**; a LinkedIn window opens, sign in there. Wait until the panel says *Signed in*.
3. On the same page press **Start engine**. Wait for *Running*.
4. Open **Find leads** (page 4). Press **Run find_icp_leads**. Watch the live log.
5. Open **Leads** (page 5) to see what came in. Download CSV when you want to hand the list to Apollo or a CRM.

After the first time, only steps 3–5 are needed each day (the session stays signed in).

## Pages

### 1 · Overview

A dashboard: how many leads are in the store, how many are still unscored, how many are qualified, and how much of today's budget is used. **Recent runs** lists the last results. **Activity** is the shared log every page writes to.

### 2 · Target profile

The YAML file that defines who you want. Every **title × location** becomes one LinkedIn search; the right-hand list shows exactly which searches will run. The file is checked as you type; **Save profile** is refused if it is invalid, so you cannot save a broken profile.

| Field | Meaning |
|---|---|
| `titles` | Job titles to search. Required. |
| `locations` | One search per location. Leave empty for worldwide. |
| `keywords` | Appended to every search, e.g. `[SaaS, B2B]`. |
| `exclude_keywords` | Anyone whose name, headline or location contains one of these is skipped. |
| `network` | `F` 1st-degree, `S` 2nd, `O` 3rd+. Leave empty for all. |
| `company_urns` | Numeric LinkedIn company ids to restrict to. Usually empty. |
| `max_pages_per_search` | LinkedIn shows 10 people per page. 3 pages = up to 30 per search. |
| `max_results` | Stop a run after this many *new* leads. |
| `scoring` | Used by the scoring step (next phase). Safe to fill in now. |
| `leadgen` | Daily caps, working hours and delays for this campaign. Wins over environment settings. |

### 3 · LinkedIn & engine

**LinkedIn session** is your sign-in, kept in a browser profile under `~/.linkedin-mcp`.

- **Check session**: asks the server whether the saved session is still valid.
- **Sign in**: opens LinkedIn in its own window. You type your email and password *there*, never here. If LinkedIn asks for a code or captcha, answer it in that window.
- **Import from browser**: copies the session from a browser where you are already signed in (Chrome, Edge, Brave, …). No typing at all.
- **Sign out**: clears the saved session.

**Engine** is the LinkedIn browser plus the tool server, started as a background process.

- **Start engine** before finding leads or using the tool console. Tick *show the browser window* if you want to watch what it does.
- **Stop engine** when you are done; this closes the browser.

The console only ever runs one job at a time, so two tabs cannot double-spend the budget.

### 4 · Find leads

Runs `find_icp_leads`: every search in the profile, page by page. The two boxes override the profile's `max_results` and `max_pages_per_search` for this run only.

The progress bar and live log show which search and page is being read. **Stop** cancels between pages. When the run finishes, **Last result** shows:

- `found` people seen in this run, `new` saved for the first time, `already_known` seen before, `skipped` removed by exclusions
- `stopped_because`: `completed`, `reached max_results`, `Daily cap reached…`, or `LinkedIn rate-limited…`
- `per_search`: pages, found and new for each search

A run stops on its own when today's search budget is used up. That is by design: the caps are what keep the account safe.

### 5 · Leads

Every person ever found. Search by name, company or headline; filter by status or minimum score. Edit inline:

- **Score** 0–100 (green ≥ 70, amber ≥ 40, red below). Filled by hand now, by the scoring step later.
- **Status**: `new` → `qualified` / `disqualified` → `enriched` (email found) → `contacted` → `replied` → `closed`.
- **Notes**: free text, saved when you leave the box.

**Download CSV / JSON** exports whatever the current filter shows. The CSV columns match what Apollo's enrichment and most CRMs expect (name, title, company, profile URL, email, score, status).

### 6 · Tool console

Every tool the engine offers, callable one at a time: read one profile, search one company's employees, look up a job. Pick a tool, fill the arguments (required ones are marked *), press **Run**. The chip next to Run says what the call costs against the daily budget. Results appear as JSON.

Use this for research on a single lead; use **Find leads** for the whole profile.

### 7 · Guide

This page.

## Budget and safety

| Action | Default cap per day | Spent by |
|---|---|---|
| searches | 20 | every search page, including each page of Find leads |
| profile views | 80 | reading a person, company or job page |
| connects | 15 | connection requests |
| messages | 10 | messages |

Caps reset at midnight local time and survive restarts. Outside working hours (08:00–20:00 by default) every LinkedIn action is refused. Change both in the `leadgen:` block of the target profile.

These limits sit well below LinkedIn's visible thresholds, but LinkedIn's terms still prohibit automated access. Keep the caps modest, keep the pauses on, and do not run the engine from two machines on the same account.

## Keyboard

Press `1`–`7` to switch pages when no text box is focused.

## Files on disk

| What | Where |
|---|---|
| Target profile | `icp.yaml` in the folder you started the console from (or `LEADGEN_ICP=`) |
| Lead database | `~/.linkedin-mcp/leadgen.sqlite3` (or `LEADGEN_DB=`) |
| LinkedIn session | `~/.linkedin-mcp/` |

## Starting and stopping the console

```
uv run leadgen-ui            # opens http://127.0.0.1:8765
uv run leadgen-ui --port 9000 --no-open
```

Close the terminal (or press Ctrl+C) to stop it; the engine is stopped with it.
