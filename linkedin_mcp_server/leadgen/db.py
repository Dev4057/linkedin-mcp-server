"""SQLite store for leads and a usage ledger.

One file, two tables:

* ``leads``  -- every person the pipeline has seen, keyed by LinkedIn username.
  A row is created the first time a search returns someone and updated on
  every later sighting. ``status`` is the pipeline stage; ``score`` and
  ``signals`` are filled by phase two's ``score_lead``.
* ``usage``  -- one row per (day, action) with a count, which is what the
  budget guard reads. Kept here rather than in memory so a cap survives a
  server restart; otherwise a restart would hand out a fresh day's quota.

SQLite is enough: a single user, a few thousand rows, one writer at a time
(the server already serializes tool calls).
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

STATUSES = (
    "new",  # found by a search, nothing else known
    "qualified",  # scored above the bar
    "disqualified",  # scored below the bar or hard-disqualified
    "enriched",  # email found (phase two)
    "contacted",  # in an outreach sequence (phase two)
    "replied",
    "closed",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    username      TEXT PRIMARY KEY,
    name          TEXT,
    headline      TEXT,
    location      TEXT,
    company       TEXT,
    title         TEXT,
    profile_url   TEXT NOT NULL,
    source_query  TEXT,
    snippet       TEXT,
    score         INTEGER,
    score_reason  TEXT,
    signals       TEXT,            -- JSON list
    status        TEXT NOT NULL DEFAULT 'new',
    email         TEXT,
    notes         TEXT,
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL,
    last_viewed   TEXT,            -- last full profile read
    times_seen    INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS leads_status ON leads(status);
CREATE INDEX IF NOT EXISTS leads_score ON leads(score);

CREATE TABLE IF NOT EXISTS usage (
    day     TEXT NOT NULL,
    action  TEXT NOT NULL,
    count   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, action)
);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    kind        TEXT NOT NULL,
    summary     TEXT              -- JSON
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Lead:
    username: str
    profile_url: str
    name: str | None = None
    headline: str | None = None
    location: str | None = None
    company: str | None = None
    title: str | None = None
    source_query: str | None = None
    snippet: str | None = None
    score: int | None = None
    score_reason: str | None = None
    signals: list[str] = field(default_factory=list)
    status: str = "new"
    email: str | None = None
    notes: str | None = None
    first_seen: str | None = None
    last_seen: str | None = None
    last_viewed: str | None = None
    times_seen: int = 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _row_to_lead(row: sqlite3.Row) -> Lead:
    data = dict(row)
    raw_signals = data.pop("signals", None)
    try:
        signals = json.loads(raw_signals) if raw_signals else []
    except ValueError:
        signals = []
    return Lead(signals=signals, **data)


class LeadStore:
    """Thread-safe wrapper around one SQLite file."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- leads -------------------------------------------------------------

    def upsert_lead(self, lead: Lead) -> tuple[Lead, bool]:
        """Insert a lead or refresh an existing one.

        Returns ``(stored_lead, is_new)``. On an existing row only the
        descriptive fields a search can see are refreshed; score, status,
        email and notes are left alone because they were earned later.
        """
        now = _now()
        with self._lock, self._conn:
            existing = self._conn.execute(
                "SELECT * FROM leads WHERE username = ?", (lead.username,)
            ).fetchone()
            if existing is None:
                self._conn.execute(
                    """INSERT INTO leads (username, name, headline, location, company,
                       title, profile_url, source_query, snippet, score, score_reason,
                       signals, status, email, notes, first_seen, last_seen, last_viewed,
                       times_seen)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)""",
                    (
                        lead.username,
                        lead.name,
                        lead.headline,
                        lead.location,
                        lead.company,
                        lead.title,
                        lead.profile_url,
                        lead.source_query,
                        lead.snippet,
                        lead.score,
                        lead.score_reason,
                        json.dumps(lead.signals),
                        lead.status if lead.status in STATUSES else "new",
                        lead.email,
                        lead.notes,
                        now,
                        now,
                        lead.last_viewed,
                    ),
                )
                is_new = True
            else:
                self._conn.execute(
                    """UPDATE leads SET
                         name = COALESCE(?, name),
                         headline = COALESCE(?, headline),
                         location = COALESCE(?, location),
                         company = COALESCE(?, company),
                         title = COALESCE(?, title),
                         snippet = COALESCE(?, snippet),
                         source_query = COALESCE(?, source_query),
                         last_seen = ?,
                         times_seen = times_seen + 1
                       WHERE username = ?""",
                    (
                        lead.name,
                        lead.headline,
                        lead.location,
                        lead.company,
                        lead.title,
                        lead.snippet,
                        lead.source_query,
                        now,
                        lead.username,
                    ),
                )
                is_new = False
            row = self._conn.execute(
                "SELECT * FROM leads WHERE username = ?", (lead.username,)
            ).fetchone()
        return _row_to_lead(row), is_new

    def get_lead(self, username: str) -> Lead | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM leads WHERE username = ?", (username,)
            ).fetchone()
        return _row_to_lead(row) if row else None

    def update_lead(self, username: str, **fields: Any) -> Lead | None:
        """Set any of: status, score, score_reason, signals, email, notes,
        company, title, last_viewed."""
        allowed = {
            "status",
            "score",
            "score_reason",
            "signals",
            "email",
            "notes",
            "company",
            "title",
            "last_viewed",
            "headline",
            "location",
            "name",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"Cannot update field(s): {sorted(unknown)}")
        if "status" in fields and fields["status"] not in STATUSES:
            raise ValueError(
                f"Unknown status {fields['status']!r}; expected one of {STATUSES}"
            )
        if "signals" in fields:
            fields["signals"] = json.dumps(list(fields["signals"] or []))
        if not fields:
            return self.get_lead(username)
        assignments = ", ".join(f"{k} = ?" for k in fields)
        with self._lock, self._conn:
            cur = self._conn.execute(
                f"UPDATE leads SET {assignments} WHERE username = ?",
                (*fields.values(), username),
            )
            if cur.rowcount == 0:
                return None
        return self.get_lead(username)

    def mark_viewed(self, username: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE leads SET last_viewed = ? WHERE username = ?",
                (_now(), username),
            )

    def seen_recently(self, username: str, days: int) -> bool:
        """True if the full profile was read within ``days``.

        Only ``last_viewed`` counts. Appearing in a search result again is
        free and expected; what the cache protects is the costly page load.
        """
        lead = self.get_lead(username)
        if lead is None or not lead.last_viewed:
            return False
        viewed = datetime.fromisoformat(lead.last_viewed)
        return viewed >= datetime.now(timezone.utc) - timedelta(days=days)

    def list_leads(
        self,
        *,
        status: str | None = None,
        min_score: int | None = None,
        source_query: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Lead]:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if min_score is not None:
            clauses.append("score >= ?")
            params.append(min_score)
        if source_query:
            clauses.append("source_query = ?")
            params.append(source_query)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = (
            f"SELECT * FROM leads {where} "
            "ORDER BY COALESCE(score, -1) DESC, last_seen DESC LIMIT ? OFFSET ?"
        )
        params.extend([limit, offset])
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_lead(r) for r in rows]

    def count_leads(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM leads GROUP BY status"
            ).fetchall()
        counts = {r["status"]: r["n"] for r in rows}
        counts["total"] = sum(counts.values())
        return counts

    def export(self, leads: Iterable[Lead], fmt: str = "csv") -> str:
        """Render leads as CSV or JSON text."""
        rows = [lead.to_dict() for lead in leads]
        if fmt == "json":
            return json.dumps(rows, indent=2)
        if fmt != "csv":
            raise ValueError(f"Unknown export format {fmt!r}; use 'csv' or 'json'")
        columns = [
            "name",
            "title",
            "company",
            "headline",
            "location",
            "profile_url",
            "email",
            "score",
            "score_reason",
            "signals",
            "status",
            "source_query",
            "first_seen",
            "last_seen",
        ]
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            row = dict(row)
            row["signals"] = "; ".join(row.get("signals") or [])
            writer.writerow(row)
        return buf.getvalue()

    # -- usage ledger --------------------------------------------------------

    def usage(self, day: str, action: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT count FROM usage WHERE day = ? AND action = ?", (day, action)
            ).fetchone()
        return int(row["count"]) if row else 0

    def usage_for_day(self, day: str) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT action, count FROM usage WHERE day = ?", (day,)
            ).fetchall()
        return {r["action"]: int(r["count"]) for r in rows}

    def record_usage(self, day: str, action: str, amount: int = 1) -> int:
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO usage (day, action, count) VALUES (?, ?, ?)
                   ON CONFLICT(day, action) DO UPDATE SET count = count + excluded.count""",
                (day, action, amount),
            )
            row = self._conn.execute(
                "SELECT count FROM usage WHERE day = ? AND action = ?", (day, action)
            ).fetchone()
        return int(row["count"])

    # -- runs ----------------------------------------------------------------

    def start_run(self, kind: str) -> int:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO runs (started_at, kind) VALUES (?, ?)", (_now(), kind)
            )
            return int(cur.lastrowid or 0)

    def finish_run(self, run_id: int, summary: dict[str, Any]) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE runs SET finished_at = ?, summary = ? WHERE id = ?",
                (_now(), json.dumps(summary), run_id),
            )

    def recent_runs(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["summary"] = json.loads(d["summary"]) if d["summary"] else None
            except ValueError:
                pass
            out.append(d)
        return out
