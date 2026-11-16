"""Validate one US-stock report and restore evidence without a live data fetch."""

from framework_validation.optimized_runner import SNAPSHOT_INPUT_FIELDS, snapshot_digest
from framework_validation.runner import parse_timestamp

from .etf_data import SOXL_TERMS


USAGE_FIELDS = (
    "input_tokens", "cached_input_tokens", "cache_write_input_tokens",
    "output_tokens", "reasoning_output_tokens",
)


def validate_single_stock(run):
    if run.get("source_mode") != "us-live" or len(run.get("sections", [])) != 1:
        raise ValueError("Week 3 requires a single US live stock capture")
    section = run["sections"][0]
    if run.get("holdings") != [section["ticker"]]:
        raise ValueError("The holdings ticker does not match the capture")
    return section


def restore_snapshot(run):
    section = validate_single_stock(run)
    sources = section["sources"]
    evidence_ids = section["snapshot_evidence_ids"]
    if section.get("excluded_evidence") or len(evidence_ids) != len(sources) or set(evidence_ids) != set(sources):
        raise ValueError("The original snapshot cannot be reconstructed from the admitted sources")
    evidence = [sources[evidence_id] for evidence_id in evidence_ids]
    cutoff = parse_timestamp(section["as_of"])
    if any(item["id"] != evidence_id or parse_timestamp(item["available_at"]) > cutoff
           for evidence_id, item in zip(evidence_ids, evidence)):
        raise ValueError("The capture contains invalid or out-of-cutoff evidence")
    fixture = {key: section[key] for key in SNAPSHOT_INPUT_FIELDS if key != "evidence"}
    fixture["evidence"] = evidence
    ticker = section["ticker"].lower()
    # Week 2 stored only admitted sources and the hash. Recover its original
    # mandatory source IDs even when a provider was missing from the capture.
    fixture["case_id"] = section["case_id"]
    required_terms = SOXL_TERMS if ticker == "soxl" else f"{ticker}-sec-periodic-results"
    fixture["required_evidence_ids"] = list(dict.fromkeys([
        f"{ticker}-stock-price-window", required_terms,
        *section.get("required_evidence_ids", []),
    ]))
    if snapshot_digest(fixture) != section.get("snapshot_sha256"):
        raise ValueError("Frozen snapshot hash does not match the capture")
    return fixture


def measurement(calls):
    """Aggregate actual telemetry. One missing field makes its total unknown."""
    if calls is None:
        return {
            "calls": None, "calls_with_usage": None, "prompt_chars": None, "model_seconds": None,
            "usage": dict.fromkeys(USAGE_FIELDS), "billed_usd": None,
            "extra_calls": None, "failed_calls": None, "calls_without_usage": None,
            "largest_input_call": None,
        }

    def total(values):
        return sum(values) if all(value is not None for value in values) else None

    measured_inputs = [
        (call["usage"]["input_tokens"], call)
        for call in calls if (call.get("usage") or {}).get("input_tokens") is not None
    ]
    largest = None
    if measured_inputs:
        tokens, call = max(measured_inputs, key=lambda item: item[0])
        largest = {"role": call["role"], "stage": call["stage"], "input_tokens": tokens}
    recorded = sum(bool(call.get("usage")) for call in calls)
    return {
        "calls": len(calls),
        "calls_with_usage": recorded,
        "calls_without_usage": len(calls) - recorded,
        "extra_calls": sum(call["stage"] not in {"analysis", "draft", "review"} for call in calls),
        "failed_calls": sum(bool(call.get("error")) for call in calls),
        "largest_input_call": largest,
        "prompt_chars": total([call.get("prompt_chars") for call in calls]),
        "model_seconds": total([call.get("seconds") for call in calls]),
        "usage": {
            field: total([(call.get("usage") or {}).get(field) for call in calls])
            for field in USAGE_FIELDS
        },
        "billed_usd": None,
    }


def validate_coverage(section):
    sources = section["sources"]
    ticker = section["ticker"].lower()
    final = section.get("final") or {}
    bars = {}
    dates = {}
    for role, evidence_id in (
        ("stock", f"{ticker}-stock-price-window"),
        ("market", "us-sp500-price-window"),
        ("sector", f"{ticker}-sector-price-window"),
    ):
        source = sources.get(evidence_id, {})
        dates[role] = [row["session_date"] for row in source.get("observations", [])]
        observation = source.get("observation_date")
        provider_session = source.get("provider_latest_regular_quote_session")
        bars[role] = {
            "status": (
                "missing" if not observation else
                "newer_session_unconfirmed" if provider_session and provider_session > observation else
                "provider_last_observed"
            ),
            "evidence_id": evidence_id if source else None,
            "observation_date": observation,
            "provider_latest_session": provider_session,
            "session_completion_bound": source.get("observation_end_at"),
        }
    roles = {}
    for role in ("macro_market", "sector"):
        entry = next(
            (item for item in section.get("trace", [])
             if item["role"] == role and item["stage"] == "analysis" and "output" in item),
            None,
        )
        cited = entry["output"]["evidence_ids"] if entry else []
        roles[role] = {
            "evidence_ids": cited,
            "status": "with_citations" if cited else "no_citations" if entry else "unavailable",
        }
    filings = [
        evidence_id for evidence_id, source in sources.items()
        if source.get("form") in {"8-K", "8-K/A"}
    ]
    stock_source = sources.get(bars["stock"]["evidence_id"], {})
    fund = ticker == "soxl"
    terms = sources.get(SOXL_TERMS, {})
    window_alignment = {}
    for sessions in (1, 5, 20, 59):
        length = sessions + 1  # N daily changes require N+1 close observations.
        window_alignment[str(sessions)] = (
            len({tuple(series[-length:]) for series in dates.values()}) == 1
            if all(len(series) >= length for series in dates.values()) else None
        )
    return {
        "instrument_type": "daily_leveraged_etf" if fund else "equity",
        "analysis_sections": roles,
        "daily_bars": bars,
        "same_session": (
            len({bar["observation_date"] for bar in bars.values()}) == 1
            if all(bar["observation_date"] for bar in bars.values()) else None
        ),
        "lookback_session_alignment": window_alignment,
        "sec_8k_body": {
            "status": "not_applicable" if fund else "metadata_only" if filings else "not_captured",
            "evidence_ids": filings,
        },
        "consensus": {"status": "not_applicable" if fund else "not_captured"},
        "fund_structure": {
            "terms_id": SOXL_TERMS if terms else None,
            "holdings_id": "soxl-etf-holdings" if "soxl-etf-holdings" in sources else None,
            "sec_prospectus_id": "soxl-sec-etf-prospectus" if "soxl-sec-etf-prospectus" in sources else None,
            "official_tracking_status": "not_measured",
            "issuer_nav_date": terms.get("observation_date"),
            "issuer_nav_usd": terms.get("metrics", {}).get("issuer_reported_nav_usd"),
            "issuer_market_price_usd": terms.get("metrics", {}).get("issuer_reported_market_price_usd"),
        } if fund else None,
        "valuation": {
            "status": "not_established",
            "observed_close_usd": stock_source.get("metrics", {}).get("last_close"),
            "price_evidence_id": bars["stock"]["evidence_id"],
            "estimated_fair_value": None,
            "verified_model_assumptions": [],
            "judgment_action": final.get("action"),
            "judgment_evidence_ids": final.get("evidence_ids", []),
        },
    }


def attach_validation(run, *, mode, source_run=None, wall_seconds=None):
    section = validate_single_stock(run)
    original = validate_single_stock(source_run) if source_run else None
    run["week3"] = {
        "mode": mode,
        "snapshot_verified": source_run is not None,
        "original_run_id": source_run["run_id"] if source_run else None,
        "analysis_cutoff": section["as_of"],
        "snapshot_sha256": section.get("snapshot_sha256"),
        "coverage": validate_coverage(section),
        "operations": {
            "this_run": measurement([] if mode == "render_replay" else section.get("calls")),
            "original_capture": measurement(original.get("calls")) if original is not None else None,
            "wall_seconds": wall_seconds,
        },
    }
