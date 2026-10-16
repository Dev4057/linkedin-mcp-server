"""Turn one people-search results page into lead records.

The search tool returns ``{url, sections: {"search_results": text},
references: {"search_results": [{kind, url, text, ...}]}}``. The references
are the reliable part: every ``kind == "person"`` entry is one result with its
``/in/<username>/`` path and, usually, the display name. The innerText is the
loose part: a block of lines per result (name, degree marker, headline,
location, actions) whose exact shape LinkedIn changes without notice.

So the parser is reference-driven. The references decide *who* is on the
page; the text is only mined, best-effort, for headline and location, and the
raw block is kept as ``snippet`` so a later scoring step (or a human) sees
what the page said even when the heuristics miss.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from linkedin_mcp_server.leadgen.db import Lead

_USERNAME_RE = re.compile(r"/in/([^/?#]+)/?")
_VIEW_PROFILE_RE = re.compile(r"^view\s+.+?(?:'s|’s)?\s+profile$", re.IGNORECASE)
_DEGREE_RE = re.compile(
    r"^(?:[•·]\s*)?(?:1st|2nd|3rd|3rd\+|1st\+|2nd\+)(?:\s+degree)?(?:\s+connection)?$",
    re.IGNORECASE,
)
_NOISE_LINES = {
    "connect",
    "message",
    "follow",
    "following",
    "pending",
    "premium",
    "linkedin member",
    "open to work",
    "mutual connection",
    "mutual connections",
    "save",
    "saved",
}
_TITLE_COMPANY_SPLIT = re.compile(r"\s+(?:at|@)\s+|\s+[|•·]\s+|\s+-\s+", re.IGNORECASE)


@dataclass
class ParsedPage:
    leads: list[Lead]
    skipped: list[dict[str, Any]] = field(default_factory=list)
    rate_limited: bool = False
    error: dict[str, Any] | None = None

    @property
    def usernames(self) -> list[str]:
        return [lead.username for lead in self.leads]


def username_from_url(url: str) -> str | None:
    match = _USERNAME_RE.search(url or "")
    if not match:
        return None
    return match.group(1).strip().lower()


def _clean_name(text: str | None) -> str | None:
    if not text:
        return None
    name = text.strip()
    if _VIEW_PROFILE_RE.match(name):
        name = re.sub(r"^view\s+", "", name, flags=re.IGNORECASE)
        name = re.sub(r"(?:'s|’s)?\s+profile$", "", name, flags=re.IGNORECASE)
    name = re.sub(r"\s+", " ", name)
    # LinkedIn appends status badges to the anchor text in some locales.
    for badge in (" • 1st", " • 2nd", " • 3rd", " · 1st", " · 2nd", " · 3rd"):
        if name.lower().endswith(badge):
            name = name[: -len(badge)]
    return name.strip() or None


def _is_noise(line: str, name: str | None) -> bool:
    lowered = line.strip().lower()
    if not lowered:
        return True
    if name and lowered == name.lower():
        return True
    if _VIEW_PROFILE_RE.match(line.strip()):
        return True
    if _DEGREE_RE.match(line.strip()):
        return True
    if lowered in _NOISE_LINES:
        return True
    if lowered.startswith(("current:", "past:", "summary:", "skills:")):
        return True
    if re.fullmatch(r"\d+\s+mutual connections?", lowered):
        return True
    if "mutual connection" in lowered and len(lowered) < 60:
        return True
    return False


def split_title_company(headline: str | None) -> tuple[str | None, str | None]:
    """``"Marketing Director at Acme Corp"`` -> ``("Marketing Director", "Acme Corp")``.

    Only the first separator counts; a headline like "Founder | Speaker |
    Author" yields title "Founder" and company "Speaker", which is wrong
    but harmless -- the full headline is kept alongside.
    """
    if not headline:
        return None, None
    parts = _TITLE_COMPANY_SPLIT.split(headline, maxsplit=1)
    if len(parts) == 2 and parts[0].strip() and parts[1].strip():
        return parts[0].strip(), parts[1].strip()
    return headline.strip(), None


def _blocks_by_name(text: str, names: list[str]) -> dict[str, str]:
    """Slice the page text into one block per result, keyed by display name.

    Each block starts at the first line equal to the name and ends where
    the next result's name line begins. Names that never appear as a whole
    line get no block.
    """
    lines = text.splitlines()
    starts: list[tuple[int, str]] = []
    wanted = {n.lower(): n for n in names if n}
    seen: set[str] = set()
    for idx, line in enumerate(lines):
        key = line.strip().lower()
        if key in wanted and key not in seen:
            starts.append((idx, wanted[key]))
            seen.add(key)
    blocks: dict[str, str] = {}
    for pos, (start, name) in enumerate(starts):
        end = (
            starts[pos + 1][0] if pos + 1 < len(starts) else min(len(lines), start + 40)
        )
        blocks[name] = "\n".join(lines[start:end]).strip()
    return blocks


def parse_search_page(
    result: dict[str, Any],
    *,
    source_query: str | None = None,
    exclude: Any = None,
) -> ParsedPage:
    """Parse a ``search_people`` result.

    ``exclude`` may be an object with an ``excluded(text) -> str | None``
    method (the ICP); a lead whose name/headline/location matches is reported
    under ``skipped`` instead of ``leads``.
    """
    sections = result.get("sections") or {}
    text = sections.get("search_results") or ""
    errors = result.get("section_errors") or {}
    error = errors.get("search_results")
    if error:
        rate_limited = error.get("error_type") in {"rate_limited", "rate_limit"} or (
            "rate" in str(error.get("error_message", "")).lower()
        )
        return ParsedPage(leads=[], rate_limited=rate_limited, error=error)

    refs = (result.get("references") or {}).get("search_results") or []
    people: list[tuple[str, str, str | None]] = []  # (username, url, name)
    seen: set[str] = set()
    for ref in refs:
        if ref.get("kind") != "person":
            continue
        username = username_from_url(ref.get("url", ""))
        if not username or username in seen or username == "me":
            continue
        seen.add(username)
        people.append(
            (
                username,
                f"https://www.linkedin.com/in/{username}/",
                _clean_name(ref.get("text")),
            )
        )

    blocks = _blocks_by_name(text, [name for _, _, name in people if name])

    page = ParsedPage(leads=[])
    for username, url, name in people:
        block = blocks.get(name or "", "")
        body = [ln.strip() for ln in block.splitlines() if not _is_noise(ln, name)]
        headline = body[0] if body else None
        location = body[1] if len(body) > 1 else None
        title, company = split_title_company(headline)
        haystack = " ".join(filter(None, [name, headline, location, block]))
        if exclude is not None and hasattr(exclude, "excluded"):
            hit = exclude.excluded(haystack)
            if hit:
                page.skipped.append(
                    {"username": username, "name": name, "reason": f"excluded: {hit}"}
                )
                continue
        page.leads.append(
            Lead(
                username=username,
                profile_url=url,
                name=name,
                headline=headline,
                location=location,
                title=title,
                company=company,
                source_query=source_query,
                snippet=block or None,
            )
        )
    return page
