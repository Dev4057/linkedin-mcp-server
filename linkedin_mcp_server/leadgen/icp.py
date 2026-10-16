"""The ideal-customer-profile file.

``icp.yaml`` is the one input to the pipeline. It is deliberately small:

.. code-block:: yaml

    name: b2b-saas-marketing-leaders
    titles:
      - Marketing Director
      - Head of Growth
    locations:
      - United States
    keywords: [SaaS, B2B]          # optional, appended to every search
    exclude_keywords: [student, intern, open to work]
    network: [S, O]                 # 2nd and 3rd degree only (optional)
    company_urns: []                # numeric LinkedIn company ids (optional)
    max_pages_per_search: 3
    max_results: 100
    scoring:                        # read by phase two
      must_have: [marketing, growth]
      nice_to_have: [demand gen, outbound, pipeline]
      disqualify: [recruiter, freelance, agency, student]
    leadgen:                        # optional overrides of the env settings
      daily_caps: {searches: 15, profile_views: 60}
      working_hours: "9-19"
      seen_days: 30

Searches are generated as every ``title`` x every ``location`` (a missing
``locations`` list means one location-less search per title). ``keywords``
are appended to the title so "Head of Growth SaaS B2B" is what LinkedIn sees.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

try:  # PyYAML is a runtime dependency of this layer; JSON works without it.
    import yaml
except ImportError:  # pragma: no cover - exercised only when PyYAML is absent
    yaml = None


class ICPError(ValueError):
    """The ICP file is missing something the pipeline needs."""


@dataclass(frozen=True)
class SearchSpec:
    """One concrete people search derived from the ICP."""

    keywords: str
    location: str | None
    network: list[str] | None
    current_company: str | None

    @property
    def label(self) -> str:
        parts = [self.keywords]
        if self.location:
            parts.append(f"in {self.location}")
        if self.current_company:
            parts.append(f"at company {self.current_company}")
        return " ".join(parts)


@dataclass(frozen=True)
class Scoring:
    must_have: list[str] = field(default_factory=list)
    nice_to_have: list[str] = field(default_factory=list)
    disqualify: list[str] = field(default_factory=list)
    min_score: int = 60


@dataclass(frozen=True)
class ICP:
    name: str
    titles: list[str]
    locations: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    exclude_keywords: list[str] = field(default_factory=list)
    network: list[str] | None = None
    company_urns: list[str] = field(default_factory=list)
    max_pages_per_search: int = 3
    max_results: int = 100
    scoring: Scoring = field(default_factory=Scoring)
    leadgen_overrides: dict[str, Any] = field(default_factory=dict)
    source_path: Path | None = None

    def searches(self) -> Iterator[SearchSpec]:
        """Yield every title x location (x company) combination, titles first."""
        suffix = " ".join(self.keywords).strip()
        locations: list[str | None] = list(self.locations) or [None]
        companies: list[str | None] = list(self.company_urns) or [None]
        for title in self.titles:
            keywords = f"{title} {suffix}".strip() if suffix else title
            for location in locations:
                for company in companies:
                    yield SearchSpec(
                        keywords=keywords,
                        location=location,
                        network=list(self.network) if self.network else None,
                        current_company=company,
                    )

    def excluded(self, text: str) -> str | None:
        """Return the first exclude keyword found in ``text`` (case-insensitive)."""
        lowered = (text or "").lower()
        for word in self.exclude_keywords:
            if word.lower() in lowered:
                return word
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "titles": self.titles,
            "locations": self.locations,
            "keywords": self.keywords,
            "exclude_keywords": self.exclude_keywords,
            "network": self.network,
            "company_urns": self.company_urns,
            "max_pages_per_search": self.max_pages_per_search,
            "max_results": self.max_results,
            "scoring": {
                "must_have": self.scoring.must_have,
                "nice_to_have": self.scoring.nice_to_have,
                "disqualify": self.scoring.disqualify,
                "min_score": self.scoring.min_score,
            },
            "search_count": sum(1 for _ in self.searches()),
            "source_path": str(self.source_path) if self.source_path else None,
        }


def _as_str_list(value: Any, key: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, list) and all(isinstance(v, (str, int)) for v in value):
        return [str(v).strip() for v in value if str(v).strip()]
    raise ICPError(f"'{key}' must be a list of strings")


def parse_icp(data: dict[str, Any], *, source_path: Path | None = None) -> ICP:
    if not isinstance(data, dict):
        raise ICPError("ICP file must contain a mapping at the top level")
    titles = _as_str_list(data.get("titles"), "titles")
    if not titles:
        raise ICPError("'titles' is required and must list at least one job title")
    network = data.get("network")
    network_list = _as_str_list(network, "network") if network is not None else None
    if network_list is not None:
        bad = [t for t in network_list if t not in ("F", "S", "O")]
        if bad:
            raise ICPError(f"'network' tokens must be F, S or O; got {bad}")
    company_urns = _as_str_list(data.get("company_urns"), "company_urns")
    bad_urns = [u for u in company_urns if not u.isdigit()]
    if bad_urns:
        raise ICPError(
            f"'company_urns' must be numeric LinkedIn company ids; got {bad_urns}. "
            "Find an id with get_company_profile -> references['about']."
        )
    scoring_raw = data.get("scoring") or {}
    if not isinstance(scoring_raw, dict):
        raise ICPError("'scoring' must be a mapping")
    scoring = Scoring(
        must_have=_as_str_list(scoring_raw.get("must_have"), "scoring.must_have"),
        nice_to_have=_as_str_list(
            scoring_raw.get("nice_to_have"), "scoring.nice_to_have"
        ),
        disqualify=_as_str_list(scoring_raw.get("disqualify"), "scoring.disqualify"),
        min_score=int(scoring_raw.get("min_score", 60)),
    )
    overrides = data.get("leadgen") or {}
    if not isinstance(overrides, dict):
        raise ICPError("'leadgen' must be a mapping of setting overrides")
    return ICP(
        name=str(data.get("name") or (source_path.stem if source_path else "icp")),
        titles=titles,
        locations=_as_str_list(data.get("locations"), "locations"),
        keywords=_as_str_list(data.get("keywords"), "keywords"),
        exclude_keywords=_as_str_list(data.get("exclude_keywords"), "exclude_keywords"),
        network=network_list,
        company_urns=company_urns,
        max_pages_per_search=max(1, int(data.get("max_pages_per_search", 3))),
        max_results=max(1, int(data.get("max_results", 100))),
        scoring=scoring,
        leadgen_overrides=dict(overrides),
        source_path=source_path,
    )


def load_icp(path: Path | str) -> ICP:
    path = Path(path).expanduser()
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        data = json.loads(text)
    else:
        if yaml is None:
            raise ICPError(
                "PyYAML is not installed; save the ICP as icp.json or run `uv sync`."
            )
        data = yaml.safe_load(text)
    return parse_icp(data or {}, source_path=path)
