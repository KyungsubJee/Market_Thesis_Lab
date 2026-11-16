"""Validate dated research excerpts for a frozen discovery shortlist."""

import re
from urllib.parse import urlsplit

from framework_validation.runner import parse_timestamp

from .discovery_data import canonical_ticker


CATEGORIES = {
    "business_strategy": "사업·전략",
    "sector_outlook": "섹터 전망",
    "thesis_risk": "투자 논리·위험",
}
FIELDS = {"id", "ticker", "category", "source_url", "observed_at", "text"}
MAX_TEXT = 900


def add_qualitative_evidence(run, bundle):
    if (not isinstance(bundle, dict) or type(bundle.get("schema_version")) is not int
            or bundle["schema_version"] != 1 or not isinstance(bundle.get("evidence"), list)):
        raise ValueError("Qualitative evidence bundle requires schema_version 1 and an evidence list")
    shortlist = {pick["ticker"] for horizon in run["horizons"].values() for pick in horizon["shortlist"]}
    cutoff = parse_timestamp(run.get("qualitative_as_of", run["as_of"]))
    seen = set(run["evidence"])
    used = set()
    accepted = {}
    skipped = []
    for item in bundle["evidence"]:
        if (not isinstance(item, dict) or item.keys() != FIELDS
                or any(not isinstance(value, str) or not value.strip() for value in item.values())):
            raise ValueError("A qualitative record must have nonempty id, ticker, category, source_url, observed_at, text")
        evidence_id = item["id"]
        ticker = canonical_ticker(item["ticker"])
        category = item["category"]
        if not re.fullmatch(r"qual-[a-z0-9][a-z0-9-]{0,95}", evidence_id) or evidence_id in seen:
            raise ValueError(f"Invalid or duplicate qualitative evidence ID: {evidence_id}")
        seen.add(evidence_id)
        if category not in CATEGORIES:
            raise ValueError(f"Unsupported qualitative category: {category}")
        url = urlsplit(item["source_url"])
        if (url.scheme not in {"http", "https"} or not url.hostname or url.username is not None
                or any(char.isspace() or char in "<>" for char in item["source_url"])):
            raise ValueError(f"Invalid qualitative source URL: {evidence_id}")
        observed = parse_timestamp(item["observed_at"])
        if observed > cutoff:
            raise ValueError(f"Qualitative evidence is after the analysis cutoff: {evidence_id}")
        if len(item["text"]) > MAX_TEXT:
            raise ValueError(f"Qualitative text must be at most {MAX_TEXT} characters: {evidence_id}")
        if ticker not in shortlist:
            skipped.append({"id": evidence_id, "ticker": ticker, "reason": "not_shortlisted"})
            continue
        if (ticker, category) in used:
            raise ValueError(f"Supply at most one qualitative record per ticker/category: {ticker}/{category}")
        used.add((ticker, category))
        accepted[evidence_id] = {**item, "ticker": ticker, "provenance": "curated_local_bundle"}
    run["evidence"].update(accepted)
    run["qualitative_evidence"] = {
        "evidence_ids": list(accepted), "skipped": skipped,
        "missing_by_ticker": {
            ticker: [category for category in CATEGORIES if (ticker, category) not in used]
            for ticker in sorted(shortlist)
        },
    }
