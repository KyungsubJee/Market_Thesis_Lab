"""Generate separate holdings and discovery reports in a shared US research run."""

import argparse
from concurrent.futures import Future, ThreadPoolExecutor
import json
import os
from pathlib import Path
import re
from threading import Lock
from time import perf_counter
from uuid import uuid4

from framework_validation.live_runner import CodexModel

from .discovery import annotate, collect_discovery, render
from .discovery_collect import collect_qualitative, load_registry
from .discovery_data import DiscoveryClient, canonical_ticker
from .events import EVENT_PREFIX, enrich_events, load_manifest
from .model import PrototypeModel
from .report import collect, load_holdings, one_line, render_report, source_index
from .us_data import SEC_AGENT, as_utc, capture, collect_us_snapshots, utc_now
from .week3 import measurement


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


def collect_inputs(tickers, client):
    started = utc_now()
    valid = [ticker for ticker in tickers if re.fullmatch(r"[A-Z][A-Z0-9]{0,9}(?:-[A-Z0-9]{1,4})?", ticker)]
    errors = {ticker: "지원하는 미국 종목 코드 형식과 일치하지 않습니다." for ticker in tickers if ticker not in valid}
    with ThreadPoolExecutor(max_workers=2) as pool:
        holdings = pool.submit(capture, lambda: collect_us_snapshots(valid, client=client, collection_started=started))
        discovery = pool.submit(capture, lambda: collect_discovery(valid, client, collection_started=started))
        snapshots, holdings_error = holdings.result()
        discovered, discovery_error = discovery.result()
    if holdings_error:
        errors.update(dict.fromkeys(valid, holdings_error))
    if discovered is not None:
        _, qualitative_error = capture(lambda: collect_qualitative(discovered, client, load_registry()))
        if qualitative_error:
            discovered["qualitative_collection_error"] = qualitative_error
    cutoff = as_utc(utc_now())
    for _, fixture in (snapshots or {}).values():
        fixture["as_of"] = cutoff
    if discovered is not None:
        discovered["as_of"] = cutoff
    return {"as_of": cutoff, "collection_started_at": as_utc(started), "snapshots": snapshots or {},
            "holding_errors": errors, "discovery": discovered, "discovery_error": discovery_error}


def run_reports(tickers, captured_data, model_name, timeout, collect_only):
    run_id = uuid4().hex
    cutoff = captured_data["as_of"]
    snapshots = captured_data["snapshots"]
    errors = captured_data["holding_errors"]
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
        holdings = collect(tickers, snapshots, lambda: PrototypeModel(model_name, timeout, 20, audit_inputs=True),
                           model_name, source_mode="us-live")
    for section in holdings["sections"]:
        if section["ticker"] in errors:
            section["error"] = errors[section["ticker"]]
    holding_calls = [call for section in holdings["sections"] for call in section.get("calls", [])]
    holdings.update(run_id=run_id, as_of=cutoff, wall_seconds=round(perf_counter() - timer, 3),
                    operations=measurement(holding_calls), events_requested="event_collection" in captured_data)
    discovery = captured_data["discovery"]
    if discovery is not None:
        timer = perf_counter()
        discovery["run_id"] = run_id
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
    if discovery is None or discovery["model_status"] != "error":
        raise ValueError("Saved run does not have a failed discovery model call to retry")
    started = perf_counter()
    previous_calls = discovery["calls"]
    discovery.setdefault("previous_model_inputs", []).append({
        "model_input": discovery["model_input"], "model_input_sha256": discovery["model_input_sha256"],
    })
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--holdings", type=Path)
    inputs.add_argument("--replay", type=Path, help="Reanalyze a saved integration run with its original cutoff and price data")
    inputs.add_argument("--retry-discovery", type=Path, help="Retry only failed discovery from saved integrated evidence")
    parser.add_argument("--event-sources", type=Path, help="Optional dated issuer and independent public news URLs")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--sec-user-agent", default=os.environ.get("SEC_USER_AGENT", SEC_AGENT))
    parser.add_argument("--model")
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--collect-only", action="store_true", help="Save evidence and Markdown without LLM or PDF")
    args = parser.parse_args()
    if args.timeout < 1 or args.output_dir.exists():
        parser.error("timeout must be positive and output directory must be new")
    if args.retry_discovery and (args.collect_only or args.event_sources or args.model):
        parser.error("--retry-discovery preserves the captured data and model")
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
    try:
        manifest = load_manifest(args.event_sources)
        previous = json.loads(args.replay.read_text(encoding="utf-8")) if args.replay else None
        tickers = previous["holdings"]["holdings"] if previous else list(
            dict.fromkeys(map(canonical_ticker, load_holdings(args.holdings)))
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
        captured_data = collect_inputs(tickers, client)
    enrich_events(captured_data, client, manifest, discover_news=not args.replay)
    collection_seconds = round(perf_counter() - timer, 3)
    print(f"Collected inputs as of {captured_data['as_of']}", flush=True)
    run_id, holdings, discovery, operations = run_reports(tickers, captured_data, model_name, args.timeout, args.collect_only)
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
    return save_run(args.output_dir, record, None if args.collect_only else write_pdf)


def save_run(output_dir, record, write_pdf):
    run_id, cutoff = record["run_id"], record["as_of"]
    holdings, discovery = record["holdings"], record["discovery"]
    usage = holdings["operations"]["usage"]
    holdings_markdown = render_report(holdings) + (
        f"\n## 실행 사용량\n\n- 모델 호출 {holdings['operations']['calls']}회 · 입력 {usage['input_tokens']} · "
        f"캐시 입력 {usage['cached_input_tokens']} · 출력 {usage['output_tokens']} 토큰\n"
    )
    discovery_markdown = render(discovery) if discovery is not None else (
        f"# 미국 주식 신규 후보\n\n- 실행 ID `{run_id}`\n- 분석 기준 UTC `{cutoff}`\n\n"
        f"후보 수집 실패 · {one_line(record['discovery_error'])}\n"
    )
    if discovery and discovery.get("qualitative_collection_error"):
        discovery_markdown += f"\n정성 자료 수집 오류 · {one_line(discovery['qualitative_collection_error'])}\n"
    output_dir.mkdir(parents=True, exist_ok=False)
    (output_dir / "evidence.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    reports = (("holdings", holdings_markdown), ("discovery", discovery_markdown))
    for filename, markdown in reports:
        (output_dir / f"{filename}.md").write_text(markdown, encoding="utf-8")
    if write_pdf is not None:
        for filename, markdown in reports:
            write_pdf(output_dir / f"{filename}.pdf", markdown)
    print(output_dir.resolve(), flush=True)
    return int(discovery is None or not discovery["universe"] or discovery["model_status"] == "error" or any(
        section["status"] == "execution_error" or section.get("path") == "blocked" for section in holdings["sections"]
    ))


if __name__ == "__main__":
    raise SystemExit(main())
