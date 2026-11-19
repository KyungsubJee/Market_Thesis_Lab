"""Generate separate holdings and discovery reports in a shared US research run."""

import argparse
from concurrent.futures import Future, ThreadPoolExecutor
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import sys
from threading import Lock
from time import perf_counter
from uuid import uuid4

from framework_validation.live_runner import CodexModel
from framework_validation.runner import parse_timestamp

from .coverage import appendix_lines, checklist, event_coverage, event_notices, withhold_unverified_medium
from .business_discovery import (
    dedup_business_deep, prepare_business, prepare_deep_dedup, reextract_business, replay_business,
    revalidate_business_deep, retry_business_deep, run_business, validate_deep_retry,
)
from .decision_briefs import synthesize
from .discovery import annotate, collect_discovery, render
from .discovery_collect import collect_qualitative, load_registry
from .discovery_data import DiscoveryClient
from .events import EVENT_PREFIX, enrich_events, load_manifest
from .instruments import canonical_holding, profile
from .model import PrototypeModel
from .multi_market import apply_supplement, load_supplement, multi_market_snapshots
from .qualitative_assessment import assess, collect_public_context, compare_public_context, merge_supplement
from .report import collect, load_holdings, one_line, render_report, source_index
from .us_data import SEC_AGENT, as_utc, capture, collect_us_snapshots, utc_now
from .week3 import measurement
from .source_review import session_review_views


class SharedClient(DiscoveryClient):
    """Reuse the same fetched body, collection timestamp and outcome per request."""

    def __init__(self, *args):
        super().__init__(*args)
        self._requests = {}
        self._lock = Lock()

    def text(self, url, *, sec=False):
        key = url, sec
        with self._lock:
            if key in self._requests:
                future, users = self._requests[key]
                self._requests[key] = future, users + 1
                owner = False
            else:
                future = Future()
                self._requests[key] = future, 1
                owner = True
        if owner:
            try:
                future.set_result(super().text(url, sec=sec))
            except Exception as exc:
                future.set_exception(exc)
        return future.result()

    def audit(self):
        rows = []
        for (url, sec), (future, users) in sorted(self._requests.items()):
            entry = {"url": url, "sec": sec, "requesters": users}
            if error := future.exception():
                entry.update(status="unavailable", error=str(error))
            else:
                entry.update(status="available", collected_at=as_utc(future.result()[1]))
            rows.append(entry)
        return {"provider_requests": len(rows), "reused_requests": sum(row["requesters"] - 1 for row in rows),
                "requests": rows}


def collect_inputs(tickers, client, *, business_cache=None):
    started = utc_now()
    ordinary = [ticker for ticker in tickers if not profile(ticker)
                and re.fullmatch(r"[A-Z][A-Z0-9]{0,9}(?:-[A-Z0-9]{1,4})?", ticker)]
    new_markets = [ticker for ticker in tickers if profile(ticker)]
    errors = {ticker: "지원하는 종목 코드나 상품명이 아닙니다." for ticker in tickers
              if ticker not in ordinary and ticker not in new_markets}
    with ThreadPoolExecutor(max_workers=3) as pool:
        holdings = pool.submit(capture, lambda: collect_us_snapshots(ordinary, client=client, collection_started=started)) if ordinary else None
        specialist = pool.submit(capture, lambda: multi_market_snapshots(new_markets, client, started)) if new_markets else None
        discovery = pool.submit(capture, lambda: collect_discovery(ordinary + new_markets, client, collection_started=started,
                                                                  business_mode=True))
        snapshots, holdings_error = holdings.result() if holdings else ({}, None)
        special_snapshots, special_error = specialist.result() if specialist else ({}, None)
        discovered, discovery_error = discovery.result()
    if holdings_error:
        errors.update(dict.fromkeys(ordinary, holdings_error))
    if special_error:
        errors.update(dict.fromkeys(new_markets, special_error))
    snapshots = {**(snapshots or {}), **(special_snapshots or {})}
    if discovered is not None:
        discovered["discovery_method"] = "business_first"
    cutoff = as_utc(utc_now())
    for _, fixture in (snapshots or {}).values():
        fixture["as_of"] = cutoff
    if discovered is not None:
        discovered["as_of"] = cutoff
        if "universe" in discovered:
            prepare_business(discovered, client, business_cache or Path(__file__).with_name(".business_cache"))
        else:
            discovered = None
            discovery_error = "Discovery collection did not return a candidate universe"
    return {"as_of": cutoff, "collection_started_at": as_utc(started), "snapshots": snapshots or {},
            "holding_errors": errors, "discovery": discovered, "discovery_error": discovery_error}


def run_reports(tickers, captured_data, model_name, timeout, collect_only, *, workers=1, client=None, business_cache=None):
    run_id = uuid4().hex
    cutoff = captured_data["as_of"]
    snapshots = captured_data["snapshots"]
    errors = captured_data["holding_errors"]
    event_status = {}
    for ticker, (_, fixture) in snapshots.items():
        events = event_coverage(ticker, fixture["evidence"], captured_data.get("event_collection", []), cutoff)
        event_status[ticker] = events
        fixture["missing_fields"] = list(dict.fromkeys([
            *fixture["missing_fields"], *event_notices(ticker, events),
        ]))
    timer = perf_counter()
    if collect_only:
        holdings = {"generated_at": as_utc(utc_now()), "model_requested": None, "source_mode": "us-live",
                    "holdings": tickers, "sections": []}
        for ticker in tickers:
            if ticker not in snapshots:
                section = {"ticker": ticker, "status": "execution_error", "sources": {}, "final": None}
            else:
                fixture = snapshots[ticker][1]
                section = {"ticker": ticker, "status": "not_requested", "as_of": cutoff, "final": None,
                           "horizon": fixture["horizon"], "sources": source_index(fixture),
                           "missing_fields": fixture["missing_fields"],
                           "required_evidence_ids": fixture["required_evidence_ids"]}
            holdings["sections"].append(section)
    else:
        def analyze(group):
            return collect(group, snapshots, lambda: PrototypeModel(model_name, timeout, 20, audit_inputs=True),
                           model_name, source_mode="us-live")

        if workers > 1 and len(tickers) > 1:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                results = list(pool.map(lambda ticker: analyze([ticker]), tickers))
            holdings = {**results[0], "holdings": list(tickers),
                        "sections": [section for run in results for section in run["sections"]]}
        else:
            holdings = analyze(tickers)
    for section in holdings["sections"]:
        if section["ticker"] in errors:
            section["error"] = errors[section["ticker"]]
        if (section["status"] == "complete"
                and event_status[section["ticker"]]["filing"]["status"] == "metadata_only"):
            section["status"] = "review_required"
            section["coverage_gate"] = "material_filing_metadata_only"
    holding_calls = [call for section in holdings["sections"] for call in section.get("calls", [])]
    holdings.update(run_id=run_id, as_of=cutoff, wall_seconds=round(perf_counter() - timer, 3),
                    operations=measurement(holding_calls), events_requested="event_collection" in captured_data)
    discovery = captured_data["discovery"]
    if discovery is not None:
        timer = perf_counter()
        discovery["run_id"] = run_id
        if discovery.get("discovery_method") == "business_first":
            run_business(discovery, client or SharedClient(), None if collect_only else CodexModel(model_name, timeout, 2),
                         business_cache or Path(__file__).with_name(".business_cache"))
        else:
            withhold_unverified_medium(discovery)
            annotate(discovery, None if collect_only else CodexModel(model_name, timeout, 1))
        discovery.update(model_requested=None if collect_only else model_name,
                         wall_seconds=round(perf_counter() - timer, 3), operations=measurement(discovery["calls"]))
    return run_id, holdings, discovery, measurement([*holding_calls, *(discovery or {}).get("calls", [])])


def restore_run(previous):
    cutoff = previous["as_of"]
    snapshots = {}
    for ticker, fixture in previous["holdings_inputs"].items():
        if fixture["as_of"] != cutoff or fixture["ticker"] != ticker:
            raise ValueError(f"Saved holdings snapshot mismatch for {ticker}")
        snapshots[ticker] = (None, {**fixture, "evidence": [
            item for item in fixture["evidence"] if not item["id"].startswith(EVENT_PREFIX)
        ]})
    discovery = previous["discovery"]
    if discovery is not None:
        if discovery["as_of"] != cutoff:
            raise ValueError("Saved discovery cutoff mismatch")
        discovery = {**discovery, "evidence": {
            key: item for key, item in discovery["evidence"].items() if not key.startswith(EVENT_PREFIX)
        }}
        for key in ("notes", "calls", "model_input", "model_input_sha256", "operations"):
            discovery.pop(key, None)
    errors = {section["ticker"]: section["error"] for section in previous["holdings"]["sections"]
              if section["ticker"] not in snapshots and section.get("error")}
    return {"as_of": cutoff, "collection_started_at": as_utc(utc_now()), "snapshots": snapshots,
            "holding_errors": errors, "discovery": discovery, "discovery_error": previous.get("discovery_error"),
            "replayed_run_id": previous["run_id"]}


def retry_discovery(record, model):
    """Re-execute only discovery annotation from captured evidence."""
    discovery = record["discovery"]
    if discovery and discovery.get("discovery_method") == "business_first":
        raise ValueError("Use --business-replay on the captured evidence to retain the two-stage business workflow")
    if discovery is None or discovery["model_status"] != "error":
        raise ValueError("Saved run does not have a failed discovery model call to retry")
    started = perf_counter()
    previous_calls = discovery["calls"]
    discovery.setdefault("previous_model_inputs", []).append({
        "model_input": discovery["model_input"], "model_input_sha256": discovery["model_input_sha256"],
    })
    withhold_unverified_medium(discovery)
    annotate(discovery, model, recover_optional=True)
    elapsed = perf_counter() - started
    discovery["calls"] = [*previous_calls, *discovery["calls"]]
    discovery["wall_seconds"] = round(discovery["wall_seconds"] + elapsed, 3)
    discovery["operations"] = measurement(discovery["calls"])
    holding_calls = [call for section in record["holdings"]["sections"] for call in section.get("calls", [])]
    record["operations"] = measurement([*holding_calls, *discovery["calls"]])
    record["wall_seconds"] = round(record["wall_seconds"] + elapsed, 3)
    record.setdefault("discovery_retries", []).append({
        "attempt": len(discovery["calls"]), "seconds": round(elapsed, 3),
        "status": discovery["model_status"], "holdings_reused": True, "frozen_evidence_reused": True,
    })


def add_public_context(record, model, *, cutoff=None, supplement=None, decision_mode=False):
    """Add an independently dated analysis without changing the base decisions."""
    started_at = utc_now()
    cutoff = parse_timestamp(cutoff) if cutoff else started_at
    if not parse_timestamp(record['as_of']) <= cutoff <= started_at:
        raise ValueError('Qualitative cutoff must be between the base cutoff and now')
    holdings, discovery = record['holdings'], record['discovery']
    public_context = collect_public_context(holdings, discovery, cutoff)
    if supplement is not None:
        merge_supplement(public_context, supplement, cutoff=cutoff, collected_at=utc_now())
    stamp = as_utc(cutoff)
    record.update(qualitative_as_of=stamp, public_context=public_context)
    for run in (holdings, discovery):
        if run is not None:
            run.update(base_as_of=record['as_of'], qualitative_as_of=stamp)
            if base_run_id := record.get('qualitative_replayed_from'):
                run.update(run_id=record['run_id'], qualitative_replayed_from=base_run_id)
    if decision_mode:
        audit = apply_decision_briefs(record, model)
    else:
        findings, audit = assess(holdings, discovery, public_context, stamp, model)
        audit['model_requested'] = getattr(model, 'model_requested', None)
        record.update(qualitative_findings=findings, qualitative_assessment=audit)
    return audit['calls']


def apply_decision_briefs(record, model):
    record["coverage"] = checklist(record)
    briefs, macro, audit = synthesize(record, model)
    audit["model_requested"] = getattr(model, "model_requested", None)
    record.update(decision_briefs=briefs, decision_macro=macro, decision_assessment=audit)
    return audit


def replay_decision(previous, model):
    """Render a new interpretation without fetching or changing input observations."""
    record = deepcopy(previous)
    record.update(run_id=uuid4().hex, judgment_replayed_from=previous["run_id"])
    started = perf_counter()
    audit = apply_decision_briefs(record, model)
    old = previous.get("decision_assessment") or previous.get("qualitative_assessment") or {}
    record.update(wall_seconds=round(perf_counter() - started, 3), operations=audit["operations"],
                  judgment_comparison={"previous_operations": old.get("operations"),
                                       "current_operations": audit["operations"]})
    return record


def replay_public_context(previous, model, *, cutoff=None, supplement=None):
    """Preserve the saved base snapshot, shortlist and actions verbatim."""
    record = deepcopy(previous)
    if previous['holdings']['as_of'] != previous['as_of'] or (
        previous['discovery'] is not None and previous['discovery']['as_of'] != previous['as_of']
    ):
        raise ValueError('Saved research sections have inconsistent base cutoffs')
    timer = perf_counter()
    record.update(run_id=uuid4().hex, qualitative_replayed_from=previous['run_id'],
                  base_operations=previous.get('base_operations', previous['operations']))
    record.pop('public_comparison', None)
    calls = add_public_context(record, model, cutoff=cutoff, supplement=supplement)
    record.update(wall_seconds=round(perf_counter() - timer, 3), operations=measurement(calls))
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--holdings", type=Path)
    inputs.add_argument("--replay", type=Path, help="Reanalyze a saved integration run with its original cutoff and price data")
    inputs.add_argument("--retry-discovery", type=Path, help="Retry only failed discovery from saved integrated evidence")
    inputs.add_argument("--render-saved", type=Path, help="Rebuild both reports from one saved integrated evidence file")
    inputs.add_argument("--qualitative-replay", type=Path, help="Assess fresh public context against a saved run's unchanged base judgments")
    inputs.add_argument("--judgment-replay", type=Path, help="Synthesize frozen price, filing and public data without fetching again")
    inputs.add_argument("--business-replay", type=Path, help="Review the frozen forty-company universe by business evidence")
    inputs.add_argument("--business-reextract", type=Path, help="Re-extract the saved original business HTML, no network or model")
    inputs.add_argument("--retry-business-deep", type=Path, help="Retry only the failed deep stage using its exact saved prompt and input")
    inputs.add_argument("--dedup-business-deep", type=Path, help="Compare one completed deep stage after removing exact text mirrors only")
    inputs.add_argument("--revalidate-business-deep", type=Path, help="Revalidate captured deep output with the supplied-source comparison policy, no model call")
    parser.add_argument("--business-cache", type=Path, default=Path(__file__).with_name(".business_cache"))
    parser.add_argument("--event-sources", type=Path, help="Optional dated issuer and independent public news URLs")
    parser.add_argument("--evidence-supplement", type=Path, help="Dated source records keyed by held ticker")
    parser.add_argument("--qualitative-sources", type=Path, help="Optional reviewed dated public permalinks as JSON")
    parser.add_argument("--qualitative-as-of", help="Optional zoned public-context cutoff (default: start of public collection)")
    parser.add_argument("--compare-public-context", type=Path, help="Compare public coverage and model tokens with a saved public run")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--sec-user-agent", default=os.environ.get("SEC_USER_AGENT", SEC_AGENT))
    parser.add_argument("--model")
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--workers", type=int, choices=(1, 2, 3), default=3, help="Concurrent independent holdings analyses")
    parser.add_argument("--collect-only", action="store_true", help="Save evidence and Markdown without LLM or PDF")
    args = parser.parse_args()
    if args.timeout < 1 or args.output_dir.exists():
        parser.error("timeout must be positive and output directory must be new")
    if (args.retry_discovery or args.render_saved) and (args.qualitative_sources or args.qualitative_as_of):
        parser.error("Use --qualitative-replay to collect public context against saved results")
    if args.compare_public_context and not (args.render_saved or args.qualitative_replay):
        parser.error("--compare-public-context requires --qualitative-replay or --render-saved")
    try:
        comparison_base = (json.loads(args.compare_public_context.read_text(encoding='utf-8-sig'))
                           if args.compare_public_context else None)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    if args.retry_discovery and (args.collect_only or args.event_sources or args.evidence_supplement or args.model):
        parser.error("--retry-discovery preserves the captured data and model")
    if args.revalidate_business_deep:
        if args.model or args.event_sources or args.evidence_supplement or args.qualitative_sources or args.qualitative_as_of:
            parser.error("--revalidate-business-deep reuses the exact captured output and source input")
        try:
            previous = json.loads(args.revalidate_business_deep.read_text(encoding="utf-8-sig"))
            record = revalidate_business_deep(previous)
            write_pdf = None
            if not args.collect_only:
                from .report_pdf import register_fonts, write_pdf
                register_fonts()
            return save_run(args.output_dir, record, write_pdf)
        except (OSError, ValueError, ImportError, KeyError, TypeError) as exc:
            parser.error(str(exc))
    if args.retry_business_deep:
        if args.collect_only or args.event_sources or args.evidence_supplement or args.qualitative_sources or args.qualitative_as_of:
            parser.error("--retry-business-deep reuses the exact captured data and requires one model call")
        try:
            previous = json.loads(args.retry_business_deep.read_text(encoding="utf-8-sig"))
            validate_deep_retry(previous)
            model_name = previous["discovery"].get("model_requested") or "gpt-6.1-sol"
            if args.model and args.model != model_name:
                parser.error("--retry-business-deep preserves the original model")
            from .report_pdf import register_fonts, write_pdf
            register_fonts()
            record = retry_business_deep(previous, CodexModel(model_name, args.timeout, 1))
            return save_run(args.output_dir, record, write_pdf)
        except (OSError, ValueError, ImportError, KeyError, TypeError) as exc:
            parser.error(str(exc))
    if args.dedup_business_deep:
        if args.collect_only or args.event_sources or args.evidence_supplement or args.qualitative_sources or args.qualitative_as_of:
            parser.error("--dedup-business-deep compares captured input with one new deep model call")
        try:
            previous = json.loads(args.dedup_business_deep.read_text(encoding="utf-8-sig"))
            prepare_deep_dedup(previous)
            model_name = previous["discovery"].get("model_requested") or "gpt-6.1-sol"
            if args.model and args.model != model_name:
                parser.error("--dedup-business-deep preserves the original model")
            from .report_pdf import register_fonts, write_pdf
            register_fonts()
            record = dedup_business_deep(previous, CodexModel(model_name, args.timeout, 1))
            return save_run(args.output_dir, record, write_pdf)
        except (OSError, ValueError, ImportError, KeyError, TypeError) as exc:
            parser.error(str(exc))
    if args.retry_discovery:
        try:
            from .report_pdf import register_fonts, write_pdf
            register_fonts()
            record = json.loads(args.retry_discovery.read_text(encoding="utf-8"))
            model_name = record["discovery"]["model_requested"]
            retry_discovery(record, CodexModel(model_name, args.timeout, 1))
        except (OSError, ValueError, ImportError, KeyError, TypeError) as exc:
            parser.error(str(exc))
        return save_run(args.output_dir, record, write_pdf)
    if args.business_reextract:
        try:
            previous = json.loads(args.business_reextract.read_text(encoding="utf-8-sig"))
            return save_run(args.output_dir, reextract_business(previous), None)
        except (OSError, ValueError, ImportError, KeyError, TypeError) as exc:
            parser.error(str(exc))
    if args.business_replay:
        try:
            previous = json.loads(args.business_replay.read_text(encoding="utf-8-sig"))
            model_name = args.model or "gpt-6.1-sol"
            model, write_pdf = None, None
            if not args.collect_only:
                from .report_pdf import register_fonts, write_pdf
                register_fonts()
                model = CodexModel(model_name, args.timeout, 2)
            client = SharedClient(args.sec_user_agent)
            record = replay_business(previous, client, model, args.business_cache)
            record["shared_requests"] = client.audit()
            return save_run(args.output_dir, record, write_pdf)
        except (OSError, ValueError, ImportError, KeyError, TypeError) as exc:
            parser.error(str(exc))
    if args.render_saved:
        try:
            record = json.loads(args.render_saved.read_text(encoding="utf-8"))
            if comparison_base is not None:
                record['public_comparison'] = compare_public_context(comparison_base, record)
            write_pdf = None
            if not args.collect_only:
                from .report_pdf import register_fonts, write_pdf
                register_fonts()
            return save_run(args.output_dir, record, write_pdf)
        except (OSError, ValueError, ImportError, KeyError, TypeError) as exc:
            parser.error(str(exc))
    if args.judgment_replay:
        if args.event_sources or args.evidence_supplement or args.qualitative_sources or args.qualitative_as_of:
            parser.error("--judgment-replay uses the captured evidence and both original cutoffs")
        try:
            previous = json.loads(args.judgment_replay.read_text(encoding='utf-8'))
            model_name = args.model or (previous.get("qualitative_assessment") or {}).get("model_requested") or (
                previous["holdings"].get("model_requested") or "gpt-6.1-sol"
            )
            write_pdf = None
            model = None
            if not args.collect_only:
                from .report_pdf import register_fonts, write_pdf
                register_fonts()
                model = CodexModel(model_name, args.timeout, 1)
                model.model_requested = model_name
            return save_run(args.output_dir, replay_decision(previous, model), write_pdf)
        except (OSError, ValueError, ImportError, KeyError, TypeError, RuntimeError) as exc:
            parser.error(str(exc))
    if args.qualitative_replay:
        if args.event_sources or args.evidence_supplement:
            parser.error("--qualitative-replay preserves prior filing, ranking and holdings evidence")
        try:
            previous = json.loads(args.qualitative_replay.read_text(encoding='utf-8'))
            supplement = json.loads(args.qualitative_sources.read_text(encoding='utf-8-sig')) if args.qualitative_sources else None
            model_name = args.model or previous['holdings'].get('model_requested') or 'gpt-6.1-sol'
            write_pdf = None
            model = None
            if not args.collect_only:
                from .report_pdf import register_fonts, write_pdf
                register_fonts()
                model = CodexModel(model_name, args.timeout, 1)
                model.model_requested = model_name
            record = replay_public_context(previous, model, cutoff=args.qualitative_as_of, supplement=supplement)
            if comparison_base is not None:
                record['public_comparison'] = compare_public_context(comparison_base, record)
            return save_run(args.output_dir, record, write_pdf)
        except (OSError, ValueError, ImportError, KeyError, TypeError, RuntimeError) as exc:
            parser.error(str(exc))
    try:
        qualitative_supplement = json.loads(args.qualitative_sources.read_text(encoding='utf-8-sig')) if args.qualitative_sources else None
        manifest = load_manifest(args.event_sources)
        supplements = load_supplement(args.evidence_supplement)
        previous = json.loads(args.replay.read_text(encoding="utf-8")) if args.replay else None
        tickers = previous["holdings"]["holdings"] if previous else list(
            dict.fromkeys(map(canonical_holding, load_holdings(args.holdings)))
        )
        model_name = args.model or (previous or {}).get("holdings", {}).get("model_requested") or "gpt-6.1-sol"
        captured_data = restore_run(previous) if previous else None
        if not args.collect_only:
            from .report_pdf import register_fonts, write_pdf
            register_fonts()
    except (OSError, ValueError, ImportError, KeyError, TypeError, re.error) as exc:
        parser.error(str(exc))
    timer = perf_counter()
    client = SharedClient(args.sec_user_agent)
    if captured_data is None:
        captured_data = collect_inputs(tickers, client, business_cache=args.business_cache)
    apply_supplement(captured_data["snapshots"], supplements)
    enrich_events(captured_data, client, manifest, discover_news=not args.replay)
    collection_seconds = round(perf_counter() - timer, 3)
    print(f"Collected inputs as of {captured_data['as_of']}", flush=True)
    run_id, holdings, discovery, operations = run_reports(tickers, captured_data, model_name, args.timeout,
                                                         args.collect_only, workers=args.workers, client=client,
                                                         business_cache=args.business_cache)
    cutoff = captured_data["as_of"]
    record = {
        "run_id": run_id, "as_of": cutoff, "collection_started_at": captured_data["collection_started_at"],
        "collection_seconds": collection_seconds, "wall_seconds": round(perf_counter() - timer, 3),
        "holdings": holdings, "discovery": discovery, "discovery_error": captured_data["discovery_error"],
        "holdings_inputs": {ticker: fixture for ticker, (_, fixture) in captured_data["snapshots"].items()},
        "shared_requests": client.audit(), "operations": operations,
        "event_collection": captured_data["event_collection"],
        **({"replayed_run_id": captured_data["replayed_run_id"]} if previous else {}),
    }
    if not args.replay or args.qualitative_sources or args.qualitative_as_of:
        model = None if args.collect_only else CodexModel(model_name, args.timeout, 1)
        if model is not None:
            model.model_requested = model_name
        calls = add_public_context(record, model, cutoff=args.qualitative_as_of, supplement=qualitative_supplement,
                                   decision_mode=True)
        holding_calls = [call for section in holdings['sections'] for call in section.get('calls', [])]
        record.update(operations=measurement([*holding_calls, *(discovery or {}).get('calls', []), *calls]),
                      wall_seconds=round(perf_counter() - timer, 3))
    return save_run(args.output_dir, record, None if args.collect_only else write_pdf)


def save_run(output_dir, record, write_pdf):
    run_id, cutoff = record["run_id"], record["as_of"]
    holdings, discovery = record["holdings"], record["discovery"]
    business_mode = bool(discovery and discovery.get("discovery_method") == "business_first")
    business_only = business_mode and not record.get("business_recovery") and bool(
        record.get("business_replayed_from") or record.get("business_reextracted_from"))
    coverage = checklist(record)
    if record.get("news_context"):
        from .news_reanalysis import augment_news_coverage
        coverage = augment_news_coverage(record, coverage)
    record["coverage"] = coverage
    findings = record.get('qualitative_findings')
    context = record.get('public_context')
    public_comparison = record.get('public_comparison')
    report_context = {'public_comparison': public_comparison} if public_comparison is not None else {}
    report_context["as_of"] = cutoff
    if record.get("qualitative_as_of"):
        report_context["qualitative_as_of"] = record["qualitative_as_of"]
    briefs = record.get("decision_briefs", {})
    macro = record.get("decision_macro", {})
    decision_audit = record.get("decision_assessment") or {}
    decision_failed = decision_audit.get("status") == "error"
    if replayed_from := record.get("judgment_replayed_from"):
        report_context.update(run_id=run_id, judgment_replayed_from=replayed_from)
    holding_view, discovery_view = {}, {}
    if record.get("decision_assessment") or briefs:
        report_context.pop("public_comparison", None)
        reviewed, review_notes, review_audits = session_review_views(holdings.get("sections", []), briefs.get("holdings", {}), cutoff)
        if review_audits:
            record["presentation_review"] = {"method": "saved_latest_session_fact_check_v1", "tickers": review_audits,
                                             "new_model_calls": 0, "raw_model_output_modified": False}
        holding_view = {"decision_briefs": reviewed, "decision_macro": macro,
                        "source_verification_notes": review_notes, "session_review_audits": review_audits}
        discovery_view = {"decision_briefs": briefs.get("discovery", {}),
                          "decision_macro": {key: value for key, value in macro.items() if key == "US"}}
    holdings_markdown = render_report({**holdings, **report_context, **holding_view,
                                       "coverage": coverage["holdings"]}, findings, context)
    discovery_markdown = render({**discovery, **report_context, **discovery_view, "coverage": coverage["discovery"]},
                                findings, context) if discovery is not None else (
        f"# 미국 주식 신규 후보\n\n- 실행 ID `{run_id}`\n- 분석 기준 UTC `{cutoff}`\n\n"
        f"후보 수집 실패 · {one_line(record['discovery_error'])}\n"
    )
    if business_mode and record.get("news_context"):
        # Retain collection/gap appendices without displaying execution metrics.
        from .news_reanalysis import news_discovery_coverage_lines
        before, separator, after = discovery_markdown.partition("<!-- pagebreak -->")
        discovery_markdown = (before + news_discovery_coverage_lines(record, coverage)
                              + (separator + after if separator else ""))
    if discovery and discovery.get("qualitative_collection_error"):
        discovery_markdown += f"\n정성 자료 수집 오류 · {one_line(discovery['qualitative_collection_error'])}\n"
    if decision_failed and not business_only:
        diagnostic = "\n## 실행 상태\n\n통합 판단을 검증하지 못해 PDF를 생성하지 않았습니다. 상세 오류는 근거 기록에 있습니다.\n"
        holdings_markdown += diagnostic
        discovery_markdown += diagnostic
    business_failed = business_mode and discovery.get("model_status") == "error"
    if business_failed:
        discovery_markdown = "사업 검토 결과를 검증하지 못해 후보 제안을 보류했습니다. 상세 오류는 근거 기록에서 확인할 수 있습니다.\n\n" + discovery_markdown
    output_dir.mkdir(parents=True, exist_ok=False)
    (output_dir / "evidence.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if public_comparison is not None and not business_only:
        (output_dir / 'public-comparison.json').write_text(
            json.dumps(public_comparison, ensure_ascii=False, indent=2) + '\n', encoding='utf-8',
        )
    discovery_report = ("discovery", discovery_markdown if business_mode else
                        discovery_markdown + appendix_lines(coverage["discovery"]))
    reports = (discovery_report,) if business_only else (
        ("holdings", holdings_markdown + appendix_lines(coverage["holdings"])), discovery_report)
    for filename, markdown in reports:
        (output_dir / f"{filename}.md").write_text(markdown, encoding="utf-8")
    if write_pdf is not None and not (business_failed or decision_failed and not business_only):
        for filename, markdown in reports:
            write_pdf(output_dir / f"{filename}.pdf", markdown)
    print(output_dir.resolve(), flush=True)
    if decision_failed and not business_only:
        print(f"Decision synthesis failed: {decision_audit['error']}", file=sys.stderr, flush=True)
    if business_only:
        return int(business_failed or discovery is None or not discovery["universe"])
    if "judgment_replayed_from" in record:
        return int(record["decision_assessment"]["status"] == "error")
    return int((record.get('decision_assessment') or record.get('qualitative_assessment') or {}).get('status') == 'error'
               or discovery is None or not discovery["universe"] or discovery["model_status"] == "error" or any(
        section["status"] == "execution_error" or section.get("path") == "blocked" for section in holdings["sections"]
    ))


if __name__ == "__main__":
    raise SystemExit(main())
