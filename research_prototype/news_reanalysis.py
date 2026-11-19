"""Reanalyze frozen holdings and the saved candidate shortlist with dated news."""

from copy import deepcopy
import json

from framework_validation.runner import parse_timestamp
from hashlib import sha256
from pathlib import Path
import re
from contextlib import nullcontext


def digest(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def news_roles(item):
    """Only relevant analysis roles receive the bounded original source."""
    roles = {"event_attention"}
    if item.get("content_basis") != "verified_body":
        return sorted(roles)
    roles.add("fundamental")
    text = str(item.get("title", "")) + " " + str(item.get("text", ""))
    if re.search(r"semiconductor|industry|sector|supply.chain|반도체|산업|업황|천연가스|natural gas|nasdaq|나스닥", text, re.I):
        roles.add("sector")
    if re.search(r"federal reserve|interest.rate|inflation|central.bank|연준|기준금리|물가|통화정책", text, re.I):
        roles.add("macro_market")
    if item.get("reaction"):
        roles.add("price_trading")
    return sorted(roles)


def integrate_news_fixture(fixture, items, news_as_of):
    result = deepcopy(fixture)
    cutoff = parse_timestamp(news_as_of)
    if cutoff < parse_timestamp(fixture["as_of"]):
        raise ValueError("News cutoff precedes frozen market cutoff")
    result["news_as_of"] = news_as_of
    seen = {item["id"] for item in fixture["evidence"]}
    for item in items:
        if item["id"] in seen:
            raise ValueError("News cannot replace a frozen source ID")
        seen.add(item["id"])
        if not item.get("available_at") or parse_timestamp(item["available_at"]) > cutoff:
            raise ValueError("News publication availability exceeds the frozen public cutoff")
        if item.get("published_at") and parse_timestamp(item["published_at"]) > cutoff:
            raise ValueError("News publication is after the public cutoff")
        if item.get("publisher_published_at") and parse_timestamp(item["publisher_published_at"]) > cutoff:
            raise ValueError("Publisher original publication exceeds the frozen cutoff")
        if item.get("content_basis") == "verified_body":
            if not item.get("extraction_verified") or not item.get("text"):
                raise ValueError("Verified body requires actual extracted source text")
            if item.get("publisher_updated_at") and parse_timestamp(item["publisher_updated_at"]) > cutoff:
                raise ValueError("A post-cutoff updated body cannot be treated as the frozen article")
        result["evidence"].append({**deepcopy(item), "category": "news_context", "analysis_roles": news_roles(item)})
    return result


def prepare_record(previous, bundle):
    from uuid import uuid4
    cutoff = previous.get("qualitative_as_of", previous["as_of"])
    if parse_timestamp(bundle["as_of"]) != parse_timestamp(cutoff):
        raise ValueError("News bundle must use the exact frozen public cutoff")
    record = deepcopy(previous)
    for key in ("decision_briefs", "decision_macro", "decision_assessment", "presentation_review", "qualitative_findings", "public_comparison"):
        record.pop(key, None)
    record.update(run_id=uuid4().hex, news_context=deepcopy(bundle), public_context={},
                  news_processing=[], qualitative_as_of=cutoff)
    record["news_integration"] = {
        "baseline_run_id": previous["run_id"],
        "scope": {"rerun_preliminary_screen": False, "frozen_candidate_tickers": list(dict.fromkeys(
            row["ticker"] for period in (previous.get("discovery") or {}).get("horizons", {}).values()
            for row in period.get("shortlist", []))),
                  "reused_base_data": True, "expected_model": "gpt-6.1-sol"},
        "frozen_hashes": {ticker: digest(fixture) for ticker, fixture in previous["holdings_inputs"].items()},
        "original_decisions": {"holdings": deepcopy(previous["holdings"]),
                               "decision_assessment": deepcopy(previous.get("decision_assessment"))},
        "original_public_context": deepcopy(previous.get("public_context")),
        "original_discovery": deepcopy(previous.get("discovery")),
        "new_calls": [], "role_inputs": [], "recheck": [],
    }
    record["holdings_inputs"] = {ticker: integrate_news_fixture(fixture, bundle["items"].get(ticker, []), cutoff)
                                 for ticker, fixture in previous["holdings_inputs"].items()}
    record["holdings"]["sections"] = []
    return record


def audit_holdings(record):
    integration = record["news_integration"]
    for section in record["holdings"]["sections"]:
        ticker = section["ticker"]
        for call in section.get("calls", []):
            evidence = call.get("input_payload", {}).get("evidence", {})
            news_ids = [key for key, source in evidence.items() if source.get("category") == "news_context"]
            integration["role_inputs"].append({"ticker": ticker, "role": call["role"], "stage": call["stage"], "news_ids": news_ids})
            output = call.get("output", {})
            for item in output.get("item_reviews", []):
                original = evidence[item["item_id"]]
                record["news_processing"].append({**item, "ticker": ticker, "role": call["role"], "stage": call["stage"],
                    "status": item["disposition"], "target_roles": original.get("analysis_roles", []),
                    "available_at": original["available_at"], "body_verification_status": original.get("content_basis")})
        review_calls = [call for call in section.get("calls", []) if call["role"] == "reviewer"]
        integration["recheck"].append({"ticker": ticker, "path": section.get("path"), "gate_decisions": section.get("gate_decisions", []),
            "independent_news_reviews": [call.get("output", {}).get("news_review") for call in review_calls],
            "not_triggered_reason": ("필수 기초 자료 차단을 유지해 모델 분석을 실행하지 않았습니다." if section.get("path") == "blocked" else
                                     "추가 단계 여부는 저장된 독립 검토와 중요성 게이트 결과를 따릅니다."), "mock_branch": False})


def reanalyze_holdings(record, model_name, timeout, workers=3, monitor=None):
    from concurrent.futures import ThreadPoolExecutor
    from .model import NewsPrototypeModel
    from .report import collect
    from .week3 import measurement
    snapshots = {ticker: (None, fixture) for ticker, fixture in record["holdings_inputs"].items()}
    tickers = record["holdings"]["holdings"]

    completed = record["news_integration"].setdefault("completed_holdings", {})
    def analyze(ticker):
        prior = completed.get(ticker)
        if prior and not prior.get("error") and prior.get("status") != "execution_error":
            return {**record["holdings"], "sections": [deepcopy(prior)]}
        def factory():
            model = NewsPrototypeModel(model_name, timeout, 20, audit_inputs=True)
            model.successful_stage_cache = deepcopy(prior.get("calls", [])) if prior else []
            return model
        result = collect([ticker], snapshots, factory, model_name, source_mode="frozen_base_news_reanalysis")
        section = result["sections"][0]
        if section.get("path") == "blocked" and any(item.get("category") == "news_context" for item in snapshots[ticker][1]["evidence"]):
            model = factory()
            fixture = snapshots[ticker][1]
            context = {key: fixture[key] for key in ("ticker", "as_of", "news_as_of", "horizon", "question", "missing_fields")}
            context["evidence"] = deepcopy(section["sources"])
            context["question"] += " 필수 가격 자료가 없어 거래 판단은 보류합니다. 뉴스의 주장·전망 영향과 확인 과제만 분석하며 행동 제안을 생성하지 마세요."
            try:
                section["news_analysis"] = model.run("event_attention", "analysis", context)
                section.setdefault("trace", []).append({"role": "event_attention", "stage": "analysis", "scope": "blocked_news_only",
                                                         "evidence_ids": section["news_analysis"]["evidence_ids"]})
            except (OSError, ValueError, RuntimeError) as exc:
                section["error"] = str(exc)
            section["calls"] = model.calls
            section["news_only_analysis"] = True
        with monitor.lock if monitor else nullcontext():
            if prior:
                record["news_integration"].setdefault("holding_attempt_history", {}).setdefault(ticker, []).append(deepcopy(prior))
            completed[ticker] = result["sections"][0]
            if monitor:
                monitor.write(f"checkpoint-holding-{ticker}.json", record)
        if monitor:
            monitor.event("holding_checkpoint", ticker=ticker, status=result["sections"][0]["status"])
        return result

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(analyze, tickers))
    sections = [section for result in results for section in result["sections"]]
    # These are source-coverage gates, independent of the fresh model's decision.
    old = {section["ticker"]: section for section in record["news_integration"]["original_decisions"]["holdings"]["sections"]}
    for section in sections:
        if old[section["ticker"]].get("coverage_gate") == "material_filing_metadata_only" and section["status"] == "complete":
            section.update(status="review_required", coverage_gate="material_filing_metadata_only")
    calls = [call for section in sections for call in section.get("calls", [])]
    record["holdings"] = {**results[0], "run_id": record["run_id"], "as_of": record["as_of"],
                          "holdings": tickers, "sections": sections, "operations": measurement(calls)}
    record["news_processing"] = []
    record["news_integration"].update(role_inputs=[], recheck=[])
    audit_holdings(record)
    return record["holdings"]


def run_experiment(record, model_name, timeout, workers=3, monitor=None):
    from framework_validation.live_runner import CodexModel
    from .integrated import apply_decision_briefs
    from .news_candidates import reanalyze_candidates
    from .week3 import measurement
    from time import perf_counter
    started = perf_counter()
    record["news_integration"].pop("execution_error", None)

    def phase(name, function):
        def measured():
            try:
                return function()
            finally:
                refresh_operations(record)
        if monitor:
            return monitor.phase(name, measured, lambda result, args: record)()
        return measured()

    phases = record["news_integration"].setdefault("completed_phases", [])
    if "holdings" not in phases:
        phase("holdings_news_reanalysis", lambda: reanalyze_holdings(record, model_name, timeout, workers, monitor))
        if any(section.get("error") or section["status"] == "execution_error" for section in record["holdings"]["sections"]):
            raise RuntimeError("Holdings stage failed; resume this checkpoint to reuse successful symbols and identical successful role stages")
        phases.append("holdings")
    def candidates():
        if record["discovery"].get("news_candidate_baseline"):
            record["news_integration"].setdefault("candidate_attempt_history", []).extend(deepcopy(record["discovery"].get("calls", [])))
        record["discovery"] = reanalyze_candidates(record["news_integration"]["original_discovery"], record["news_context"], CodexModel(model_name, timeout, 1))
    if "candidates" not in phases:
        phase("fixed_candidate_news_reanalysis", candidates)
        if record["discovery"].get("model_status") == "error":
            raise RuntimeError("Candidate batch failed; saved raw output and successful holdings are retained for stage-only resume")
        phases.append("candidates")
    # Business cards are already rendered from the candidate batch. This common
    # formatting call describes holdings only, using freshly reviewed actions.
    def decision():
        if record.get("decision_assessment"):
            record["news_integration"].setdefault("decision_attempt_history", []).extend(deepcopy(record["decision_assessment"].get("calls", [])))
        view = {**record, "discovery": None}
        apply_decision_briefs(view, CodexModel(model_name, timeout, 1))
        for key in ("decision_briefs", "decision_macro", "decision_assessment"):
            record[key] = view[key]
    if "decision" not in phases:
            phase("decision_brief", decision)
            if record["decision_assessment"]["status"] == "error":
                raise RuntimeError("Final formatting failed; resume keeps holdings and candidate batch unchanged")
            phases.append("decision")
    refresh_operations(record)
    record.update(wall_seconds=round(perf_counter() - started, 3))
    return record


def refresh_operations(record):
    from .week3 import measurement
    discovery = record["discovery"]
    sections = list(record["news_integration"].get("completed_holdings", {}).values()) or record["holdings"]["sections"]
    calls = [call for section in sections for call in section.get("calls", [])]
    calls = [call for call in calls if not call.get("reused_successful_stage")]
    for attempts in record["news_integration"].get("holding_attempt_history", {}).values():
        calls += [call for attempt in attempts for call in attempt.get("calls", []) if not call.get("reused_successful_stage")]
    calls += record["news_integration"].get("candidate_attempt_history", [])
    calls += record["news_integration"].get("decision_attempt_history", [])
    if discovery and discovery.get("news_candidate_baseline"):
        calls += discovery.get("calls", [])
    calls += record.get("decision_assessment", {}).get("calls", [])
    record["news_integration"].update(new_calls=calls, operations=measurement(calls))
    record["operations"] = measurement(calls)


def augment_news_coverage(record, coverage):
    """Add collection provenance without changing any required-evidence gate."""
    for branch in ("holdings", "discovery"):
        for ticker, row in coverage[branch]["tickers"].items():
            items = record["news_context"]["items"].get(ticker, [])
            body = [item for item in items if item.get("content_basis") == "verified_body"]
            analysis = sum(item.get("publisher_type") == "public_analysis_author" for item in body)
            headlines = sum(item.get("content_basis") == "headline_only" for item in items)
            opinion = sum(item.get("content_basis") == "individual_opinion" for item in items)
            row["checks"].extend([
                ("보강", "추가 공개자료 원문 추출(기사·분석글 포함)", "available" if body else "missing",
                 f"본문 {len(body)}건(공개 분석글 {analysis}건 포함); 원문 확보는 주장 사실의 독립검증이 아님"),
                ("보강", "추가 제목 단서·개인 의견", "partial" if headlines or opinion else "not_run",
                 f"제목 {headlines}건·개인 의견 {opinion}건; 거래 직접 근거 아님. 0건은 완전한 검색·사건 부재를 뜻하지 않음"),
            ])
            # The original lookup was an earlier independent collection route.
            row["checks"] = [(importance, "기초분석 전 기존 독립뉴스 조회" if label == "독립 뉴스" else label, status, reference)
                             for importance, label, status, reference in row["checks"]]
    return coverage


def news_discovery_coverage_lines(record, coverage):
    from .coverage import appendix_table_rows
    lines = ["", "<!-- pagebreak -->", "", "## 부록 · 추가 공개자료 확인", "",
             "기사·공개 분석글의 원문 확보와 기사 주장의 사실 확인은 다릅니다. 원문·시각·처리 사유는 근거 JSON에 보관했습니다."]
    selected = {pick["ticker"] for result in record["discovery"]["horizons"].values() for pick in result["shortlist"]}
    for ticker in selected:
        row = coverage["discovery"]["tickers"].get(ticker)
        if row:
            lines += ["", "### " + row.get("label", ticker), "", *appendix_table_rows(row["checks"][-2:])]
    return "\n".join(lines) + "\n"


def main(argv=None):
    import argparse
    from .integrated import save_run
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--news-bundle", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--monitor-dir", type=Path)
    parser.add_argument("--resume", type=Path, help="Resume an experiment checkpoint, preserving successful symbols/stages")
    parser.add_argument("--collect-only", action="store_true")
    parser.add_argument("--pdf", action="store_true")
    parser.add_argument("--model", default="gpt-6.1-sol", choices=["gpt-6.1-sol"])
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--workers", type=int, default=3, choices=[1, 2, 3])
    args = parser.parse_args(argv)
    previous = json.loads(args.baseline.read_text(encoding="utf-8-sig"))
    bundle = json.loads(args.news_bundle.read_text(encoding="utf-8-sig"))
    record = json.loads(args.resume.read_text(encoding="utf-8-sig")) if args.resume else prepare_record(previous, bundle)
    if args.resume and (record["news_integration"]["baseline_run_id"] != previous["run_id"] or record["news_context"] != bundle):
        raise ValueError("Resume requires the identical baseline and news bundle")
    monitor = None
    if args.monitor_dir:
        from .monitoring import Monitor
        monitor = Monitor(args.monitor_dir)
        monitor.install()
        monitor.event("run_started", mode="frozen_base_news_reanalysis", baseline=str(args.baseline))
        monitor.write("checkpoint-news-inputs.json", record)
    if args.collect_only:
        args.output_dir.mkdir(parents=True, exist_ok=False)
        (args.output_dir / "evidence.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        if monitor:
            monitor.finish(0)
        return 0
    code, error = 1, None
    try:
        run_experiment(record, args.model, args.timeout, args.workers, monitor)
        writer = None
        if args.pdf:
            from .report_pdf import register_fonts, write_pdf
            register_fonts()
            writer = write_pdf
        code = save_run(args.output_dir, record, writer)
        return code
    except BaseException as exc:
        error = str(exc)
        record.setdefault("news_integration", {})["execution_error"] = error
        refresh_operations(record)
        args.output_dir.mkdir(parents=True, exist_ok=True)
        (args.output_dir / "evidence.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        raise
    finally:
        if monitor:
            monitor.finish(code, error)


if __name__ == "__main__":
    raise SystemExit(main())
