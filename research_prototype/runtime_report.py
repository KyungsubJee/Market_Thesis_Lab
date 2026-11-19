"""Build an independent Korean run/telemetry report without any model or fetch."""

import argparse
from collections import defaultdict
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import re

from framework_validation.runner import parse_timestamp

from .coverage import STATUS, appendix_table_rows
from .instruments import holding_label, profile
from .report import decision_time, one_line
from .week3 import measurement


ROLE_LABELS = {"business_screen": "40개 기업 예비 검토", "business_deep": "기업 심층·기간별 판단",
               "reviewer": "독립 검토", "synthesis": "종합 판단", "analyst": "분석",
               "macro_market": "거시·시장", "sector": "산업·섹터", "fundamental": "기업 실적·가치",
               "price_trading": "가격·거래량", "event_attention": "주요 사건·관심 변화",
               "decision_briefs": "통합 판단", "qualitative_assessment": "공개 정성 자료 검토"}
FAILED = {"error", "failed", "unavailable", "authentication_required"}


def safe(value, limit=220):
    text = re.sub(r"([?&](?:api_key|apikey|token|key)=)[^&\s]+", r"\1[숨김]", "" if value is None else str(value), flags=re.I)
    text = one_line(text).replace("|", " / ")
    return text if len(text) <= limit else text[:limit - 1] + "…"


def number(value):
    return "미집계" if value is None else f"{value:,.2f}".rstrip("0").rstrip(".") if isinstance(value, float) else f"{value:,}"


def call_fingerprint(call):
    extra = {"ticker", "call_id", "trace_path", "monitor_id", "monitor_label", "record_sha256", "evidence_link"}
    original = {key: value for key, value in call.items() if key not in extra}
    return sha256(json.dumps(original, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def load_monitor(directory):
    if directory is None:
        return {"summary": {}, "events": [], "calls": [], "files": [], "provided": False}
    directory = Path(directory)
    files = []
    def read(path):
        raw = path.read_bytes()
        files.append({"path": str(path.resolve()), "sha256": sha256(raw).hexdigest()})
        return raw.decode("utf-8-sig")
    summary_path, events_path = directory / "summary.json", directory / "events.jsonl"
    summary = json.loads(read(summary_path)) if summary_path.exists() else {}
    scope = str(directory.resolve())
    events = [{**json.loads(line), "monitor_id": scope, "monitor_label": directory.name}
              for line in read(events_path).splitlines() if line.strip()] if events_path.exists() else []
    calls = []
    for path in sorted((directory / "calls").glob("*.json")):
        entry = json.loads(read(path))
        call = deepcopy(entry.get("record") or {})
        fingerprint = call_fingerprint(call)
        for key in ("call_id", "ticker", "role", "stage", "seconds", "usage", "transport_status", "validation_status"):
            if key in entry:
                call[key] = entry[key]
        call["trace_path"] = str(path.resolve())
        call.update(monitor_id=scope, monitor_label=directory.name, record_sha256=fingerprint)
        if entry.get("error"):
            call["error"] = entry["error"]
        calls.append(call)
    return {"summary": summary, "events": events, "calls": calls, "files": files, "provided": True,
            "directory": scope}


def load_monitors(directory, additional=()):
    paths = ([directory] if directory is not None else []) + list(additional)
    if not paths:
        return load_monitor(None)
    if len({str(Path(path).resolve()) for path in paths}) != len(paths):
        raise ValueError("The same monitor directory cannot be counted twice")
    components = [load_monitor(path) for path in paths]
    if len(components) == 1:
        result = components[0]
        result["components"] = [{"directory": result["directory"], "summary": result["summary"], "call_files": len(result["calls"])}]
        return result
    summaries = [part["summary"] for part in components]
    starts = [summary.get("started_at") for summary in summaries]
    ends = [summary.get("finished_at") for summary in summaries]
    begin = min(starts, key=parse_timestamp) if all(starts) else None
    end = max(ends, key=parse_timestamp) if all(ends) else None
    elapsed = (parse_timestamp(end) - parse_timestamp(begin)).total_seconds() if begin and end else None
    counts, walls = [summary.get("call_count") for summary in summaries], [summary.get("wall_seconds") for summary in summaries]
    summary = {"started_at": begin, "finished_at": end, "wall_seconds": elapsed,
               "summed_program_seconds": sum(walls) if all(value is not None for value in walls) else None,
               "status": "complete" if all(value.get("status") == "complete" for value in summaries) else "오류 또는 종료 미확인 포함",
               "call_count": sum(counts) if all(value is not None for value in counts) else None}
    return {"summary": summary, "provided": True,
            "calls": [call for part in components for call in part["calls"]],
            "events": [event for part in components for event in part["events"]],
            "files": [file for part in components for file in part["files"]],
            "components": [{"directory": part["directory"], "summary": part["summary"], "call_files": len(part["calls"])} for part in components]}


def evidence_calls(record):
    """Fallback current-stage logs only; copied aggregate/history logs are excluded."""
    calls = []
    for section in record.get("holdings", {}).get("sections", []):
        calls.extend({**call, "ticker": section["ticker"]} for call in section.get("calls", []))
    discovery = record.get("discovery") or {}
    if "business_screen" in discovery:
        for key in ("business_screen", "business_deep"):
            calls.extend(discovery.get(key, {}).get("calls", []))
    else:
        calls.extend(discovery.get("calls", []))
    for key in ("decision_assessment", "qualitative_assessment"):
        calls.extend((record.get(key) or {}).get("calls", []))
    return deepcopy(calls)


def phase_rows(events):
    rows, started = [], {}
    for event in events:
        phase = event.get("phase")
        if not phase:
            continue
        key = (event.get("monitor_id", ""), phase)
        if event.get("event") == "phase_started":
            started[key] = (event.get("elapsed_seconds"), event.get("monitor_label"))
        elif event.get("event") in {"phase_completed", "phase_error"}:
            seconds = event.get("seconds")
            start = started.get(key, (None, None))[0]
            if seconds is None and start is not None and event.get("elapsed_seconds") is not None:
                seconds = event["elapsed_seconds"] - start
            rows.append({"phase": phase, "monitor_id": key[0], "monitor_label": event.get("monitor_label"),
                         "seconds": seconds, "status": event["event"], "error": event.get("error")})
            started.pop(key, None)
    rows.extend({"phase": phase, "monitor_id": scope, "monitor_label": item[1], "seconds": None, "status": "not_finished"}
                for (scope, phase), item in started.items())
    return rows


def source_failures(record):
    failures = []
    # Only diagnostic registries: avoid mistaking model/source narrative for telemetry.
    roots = {key: record.get(key) for key in ("shared_requests", "event_collection", "public_context")}
    discovery = record.get("discovery") or {}
    roots["discovery_sources"] = discovery.get("sources")
    roots["business_collection"] = discovery.get("business_collection", {}).get("packets")
    recovery = record.get("business_recovery") or {}
    original = recovery.get("original_discovery") or {}
    roots["initial_discovery_sources"] = original.get("sources")
    roots["initial_business_collection"] = original.get("business_collection", {}).get("packets")
    roots["recovery_collection_requests"] = discovery.get("business_collection_recovery", {}).get("source_requests")
    def visit(value, path):
        if isinstance(value, dict):
            if value.get("error") or value.get("status") in FAILED:
                failures.append({"path": path, "status": value.get("status", "error"),
                                 "error": safe(value.get("error") or value.get("status")),
                                 "url": safe(value.get("url") or value.get("source_url"))})
            for key, child in value.items():
                if key not in {"evidence", "text", "passages", "body", "raw_output", "prompt"}:
                    visit(child, f"{path}/{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, f"{path}/{index}")
    for key, value in roots.items():
        visit(value, key)
    return failures


def supplement_summary(value):
    if not value:
        return {"provided": False, "counts": {}, "records": [], "sources": []}
    sources = value.get("sources", [])
    counts = {key: sum(source.get("status") == key for source in sources)
              for key in ("verified_core_text", "partial_core_text", "dated_unrevalidated")}
    counts.update(sources=len(sources), records=len(value.get("records", [])))
    return {"provided": True, "created_at": value.get("created_at"), "counts": counts,
            "records": deepcopy(value.get("records", [])), "sources": deepcopy(sources),
            "trace": deepcopy(value.get("_trace", {})), "policy": value.get("policy")}


def judgment_quality(record):
    """Measure actual decision paths and formal adoption, not collection success."""
    sections = record.get("holdings", {}).get("sections", [])
    analyzed, blocked, escalations, unresolved, changes, metadata = [], [], [], [], [], []
    coverage = record.get("coverage", {}).get("holdings", {}).get("tickers", {})
    specialist = debate = 0
    for section in sections:
        ticker, calls = section["ticker"], section.get("calls", [])
        if section.get("path") == "blocked" or coverage.get(ticker, {}).get("blocking"):
            blocked.append(ticker)
        if calls:
            analyzed.append(ticker)
        reconcile = sum(call.get("stage") == "reconcile" for call in calls)
        rereview = sum(call.get("stage") == "rereview" for call in calls)
        specialist += sum(call.get("stage") == "specialist" for call in calls)
        debate += sum(call.get("stage") == "debate" for call in calls)
        if reconcile or rereview:
            escalations.append({"ticker": ticker, "reconcile": reconcile, "rereview": rereview})
        reviews = [call.get("output") or {} for call in calls if call.get("role") == "reviewer"]
        issues = [issue for issue in (reviews[-1].get("issues", []) if reviews else [])
                  if isinstance(issue, dict) and issue.get("material")]
        for issue in issues:
            unresolved.append({"ticker": ticker, "id": issue.get("id"), "claim": issue.get("claim"),
                               "status": section.get("status"), "impact": issue.get("impact")})
        drafts = [call.get("output") or {} for call in calls if call.get("stage") == "draft"]
        before = drafts[0].get("action") if drafts else None
        after = (section.get("final") or {}).get("action")
        if before and after and before != after:
            changes.append({"ticker": ticker, "before": before, "after": after,
                            "status": section.get("status"), "material_issues_remain": bool(issues)})
        if coverage.get(ticker, {}).get("filing", {}).get("status") == "metadata_only":
            metadata.append(ticker)
    public = record.get("public_context") or {}
    public_ids = {item["id"] for value in public.values() for item in value.get("items", []) if item.get("id")}
    briefs = record.get("decision_briefs", {}).get("holdings", {})
    fields = ("issue", "current", "outlook", "counterargument", "proposal", "reassessment")
    citation_count, cited_ids, cited_cards = 0, set(), []
    for ticker, brief in briefs.items():
        used = [key for field in fields for key in (brief.get(field) or {}).get("evidence_ids", []) if key in public_ids]
        citation_count += len(used)
        cited_ids.update(used)
        if used:
            cited_cards.append(ticker)
    return {"analyzed_tickers": analyzed, "blocked_tickers": blocked, "escalations": escalations,
            "specialist_calls": specialist, "debate_calls": debate, "unresolved_material": unresolved,
            "action_changes": changes, "metadata_only_tickers": metadata,
            "public_adoption": {"collected_unique_items": len(public_ids), "collected_tickers": len(public),
                                "briefs": len(briefs), "fields_per_brief": len(fields),
                                "citation_count": citation_count, "cited_unique_items": len(cited_ids), "cited_tickers": cited_cards}}


def exit_classification(record, monitor, operations, holdings, artifacts):
    components = monitor.get("components", []) or [{"directory": "", "summary": monitor["summary"]}]
    phases = phase_rows(monitor["events"])
    saved = bool(artifacts) and all(artifacts.get(name, {}).get("exists") for name in ("holdings.pdf", "discovery.pdf"))
    model_complete = (operations["calls"] > 0 and operations["failed_calls"] == 0 and
                      (record.get("discovery") or {}).get("model_status") == "complete" and
                      (record.get("decision_assessment") or {}).get("status") != "error")
    limited = any(row["blocked"] or row["status"] == "review_required" for row in holdings)
    blocked = any(row["blocked"] for row in holdings)
    cause = ("필수 시세 검증 미충족으로 일부 종목 보류" if any(row["blocked"] and not row["observed_session"] for row in holdings)
             else "필수 자료 미충족으로 일부 종목 보류")
    exits = []
    for component in components:
        summary, scope = component["summary"], component.get("directory", "")
        exception = bool(summary.get("error")) or any(phase["status"] == "phase_error" and phase.get("monitor_id") == scope for phase in phases)
        if summary.get("exit_code") == 0:
            interpretation = "정상 종료"
        elif exception:
            interpretation = "예외 기록이 있는 종료 · 오류 기록 확인 필요"
        elif summary.get("exit_code") == 1 and model_complete and saved and blocked:
            interpretation = f"PDF 생성·모델 호출 완료 · {cause} (종료 코드 1)"
        else:
            interpretation = "종료 코드 원인 미확정 · 예외 또는 자료 제한을 추가 확인"
        exits.append({"monitor_id": scope, "status": summary.get("status"), "exit_code": summary.get("exit_code"),
                      "recorded_error": summary.get("error"), "exception_recorded": exception, "interpretation": interpretation})
    clean_limit = bool(exits) and all(item["exit_code"] in (0, 1) and not item["exception_recorded"] for item in exits) and model_complete and saved and blocked
    return {"summary": f"PDF 생성·모델 호출 완료 · {cause}" if clean_limit else "실행별 종료 기록과 오류 구분 확인 필요",
            "model_calls_without_recorded_error": model_complete, "report_pdfs_present": saved,
            "quality_limits_present": limited, "runs": exits}


def build_audit(record, monitor=None, *, expected_us=None, expected_kr=None, supplement_audit=None, artifacts=None):
    monitor = monitor or {"summary": {}, "events": [], "calls": [], "files": [], "provided": False}
    calls = deepcopy(monitor["calls"] if monitor["provided"] else evidence_calls(record))
    for call in calls:
        call.setdefault("role", "역할 미기록")
        call.setdefault("stage", "단계 미기록")
        if not call.get("error") and (call.get("transport_status") in FAILED or call.get("validation_status") in FAILED):
            call["error"] = "수집된 호출 기록에 전송 또는 검증 실패가 표시됐습니다."
    summary = monitor["summary"]
    integrity = []
    if monitor["provided"]:
        if summary.get("call_count") is not None and summary["call_count"] != len(calls):
            integrity.append("모니터 요약의 호출 수와 calls 파일 수가 일치하지 않습니다.")
        ids = [(call.get("monitor_id"), call.get("call_id")) for call in calls]
        if len(ids) != len(set(ids)) or any(call.get("call_id") is None for call in calls):
            integrity.append("모니터 호출 ID가 누락되거나 중복됐습니다.")
        if not summary.get("finished_at"):
            integrity.append("모니터 종료시각이 없어 전체 실행 완료를 확인하지 못했습니다.")
        if any(call.get("transport_status") == "running" or call.get("validation_status") == "pending" for call in calls):
            integrity.append("전송 또는 검증이 끝나지 않은 호출 기록이 있습니다.")
        for component in monitor.get("components", []):
            if component["summary"].get("call_count") != component["call_files"]:
                integrity.append(f"{component['directory']}의 요약 호출 수와 호출 파일 수가 일치하지 않습니다.")
    else:
        integrity.append("별도 모니터가 없어 현재 evidence 호출만 집계했습니다. 모든 재시도 포함 여부는 미확인입니다.")
    if any(not call.get("usage") for call in calls):
        integrity.append("일부 호출의 사용량이 없어 전체 토큰 합계가 확정되지 않았습니다.")
    rows, gaps = [], []
    coverage = record.get("coverage", {}).get("holdings", {}).get("tickers", {})
    for section in record.get("holdings", {}).get("sections", []):
        ticker = section["ticker"]
        sources = section.get("sources", {})
        stock = sources.get(f"{ticker.lower()}-stock-price-window", {})
        country = profile(ticker).country if profile(ticker) else "US"
        expected = expected_kr if country == "KR" else expected_us
        observed = stock.get("observation_date")
        coverage_row = coverage.get(ticker, {})
        blocked = bool(coverage_row.get("blocking") or section.get("path") == "blocked"
                       or section.get("status") in {"blocked", "execution_error"})
        view = record.get("presentation_review", {}).get("tickers", {}).get(ticker, {})
        rows.append({"ticker": ticker, "label": holding_label(ticker, sources.values()), "country": country,
                     "observed_session": observed, "expected_session": expected,
                     "session_status": "미확인" if not observed or not expected else "일치" if observed == expected else "불일치",
                     "status": view.get("display_status", section.get("status")), "original_status": section.get("status"), "blocked": blocked,
                     "action": (section.get("final") or {}).get("action"), "coverage_gate": section.get("coverage_gate")})
        for importance, label, status, reference in coverage_row.get("checks", []):
            if "필수" in importance and status not in {"available", "not_applicable"}:
                gaps.append({"ticker": ticker, "importance": importance, "field": label, "status": status,
                             "impact": "해당 판단은 자료 확인 전 보류" if not blocked else "종목 판단 보류 유지",
                             "reference": reference})
        for key in coverage_row.get("blocking", []):
            gaps.append({"ticker": ticker, "importance": "필수", "field": key, "status": "missing", "impact": "종목 판단 보류 유지"})
    discovery = record.get("discovery") or {}
    gaps.extend({**gap, "status": "missing", "importance": "필수", "impact": gap.get("judgment_impact") or gap.get("reason")}
                for gap in discovery.get("missing_required", []))
    stages = {key: discovery.get(key, {}) for key in ("business_screen", "business_deep")}
    http = [event for event in monitor["events"] if event.get("event") == "source_request"]
    sidecar = discovery.get("business_collection_recovery", {}).get("source_requests") or []
    if isinstance(sidecar, dict):
        sidecar = sidecar.get("requests", [])
    recovery = record.get("business_recovery") or {}
    old_collection = (recovery.get("original_discovery") or {}).get("business_collection", {})
    monitored_failures = [{"path": f"monitor/{event.get('monitor_label', '')}/source_request/{index}",
                           "status": event.get("status"), "error": safe(event.get("error") or event.get("status")),
                           "url": safe(event.get("url"))}
                          for index, event in enumerate(http) if event.get("status") in FAILED or event.get("error")]
    final_calls = evidence_calls(record)
    fingerprints = {call_fingerprint(call) for call in final_calls}
    for call in calls:
        call["evidence_link"] = ("final_evidence" if call.get("record_sha256", call_fingerprint(call)) in fingerprints
                                 else "earlier_or_unmatched")
    hashes = []
    for key, stage in stages.items():
        if stage.get("input_sha256") and stage.get("input") is not None:
            actual = sha256(json.dumps(stage["input"], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            hashes.append({"stage": key, "expected": stage["input_sha256"], "actual": actual, "matches": actual == stage["input_sha256"]})
            if actual != stage["input_sha256"]:
                integrity.append(f"{key} 입력 해시가 저장 기록과 일치하지 않습니다.")
    operations = measurement(calls)
    return {"run_id": record.get("run_id"), "as_of": record.get("as_of"), "monitor_summary": summary,
            "call_scope": "monitor_call_files" if monitor["provided"] else "evidence_current_calls",
            "calls": [{key: call.get(key) for key in ("call_id", "ticker", "role", "stage", "usage", "seconds", "error", "trace_path",
                                                       "monitor_id", "monitor_label", "record_sha256", "evidence_link",
                                                       "transport_status", "validation_status")} for call in calls],
            "operations": operations, "holdings": rows, "required_gaps": gaps,
            "exit_classification": exit_classification(record, monitor, operations, rows, artifacts),
            "report_artifacts": deepcopy(artifacts or {}), "presentation_review": deepcopy(record.get("presentation_review") or {}),
            "http_operations": {"requests": len(http), "failed_requests": sum(event.get("status") in FAILED for event in http),
                                "available": bool(http)},
            "sidecar_http_operations": {"requests": len(sidecar), "failed_requests": sum(item.get("status") in FAILED for item in sidecar)},
            "business_recovery": ({"holdings_reused": recovery.get("holdings_reused"), "new_holding_calls": recovery.get("new_holding_calls"),
                                   "same_cutoff": recovery.get("same_cutoff"), "same_universe": recovery.get("same_universe"),
                                   "same_price_snapshots": recovery.get("same_price_snapshots"),
                                   "initial_available_businesses": old_collection.get("available_business_count"),
                                   "recovered_available_businesses": discovery.get("business_collection", {}).get("available_business_count")}
                                  if recovery else {}),
            "explicit_deep_retries": len(discovery.get("business_deep_retries", [])),
            "source_failures": monitored_failures + source_failures(record), "phases": phase_rows(monitor["events"]),
            "monitor_runs": monitor.get("components", []),
            "input_hashes": hashes, "integrity_notices": integrity, "monitor_files": monitor["files"],
            "supplement_refresh": supplement_summary(supplement_audit),
            "judgment_quality": judgment_quality(record),
            "discovery_status": discovery.get("model_status"),
            "stage_status": {key: stage.get("status", "미기록") for key, stage in stages.items()},
            "projections": {key: stage["input_projection"] for key, stage in stages.items() if stage.get("input_projection")},
            "revalidations": [{key: item.get(key) for key in ("status", "policy", "output_modified", "new_model_calls", "new_collection_requests")}
                              for item in discovery.get("business_deep_revalidations", [])],
            "reuse_markers": {key: value for key, value in record.items() if key.endswith("replayed_from") or key.endswith("retried_from")}}


def render_audit(audit, evidence_path, evidence_sha256):
    ops, summary = audit["operations"], audit["monitor_summary"]
    lines = ["# 전체 실행과 토큰 사용 검증", "", f"- 실행 ID · {safe(audit['run_id'])}",
             f"- 분석 기준 · {decision_time(audit['as_of'])} (한국시간)",
             f"- 실행 결과 해석 · {safe(audit['exit_classification']['summary'])}",
             f"- 모니터 원상태 · {safe(summary.get('status') or '모니터 미집계')} (원래 종료 상태 보존)",
             f"- 발굴 판단 상태 · {safe(audit['discovery_status'] or '미기록')}",
             "- 이 보고서는 프로그램의 수집·모델·검증 기록을 집계합니다. Codex 앱과 Root·하위 에이전트의 내부 도구 호출은 포함하지 않습니다.",
             "- 모델 소요시간 합계는 병렬 실행을 포함하므로 전체 경과시간과 다릅니다. 비용 금액은 청구 기록이 없어 산정하지 않았습니다.", ""]
    lines.append(f"- 실제 모델 호출 {number(ops['calls'])}회 · 오류 기록 {number(ops['failed_calls'])}건 · 사용량 미기록 {number(ops['calls_without_usage'])}회")
    usage = ops["usage"]
    lines.append(f"- 입력 {number(usage['input_tokens'])} · 출력 {number(usage['output_tokens'])} · 캐시 입력 {number(usage['cached_input_tokens'])} 토큰")
    lines.append(f"- 전체 경과 {number(summary.get('wall_seconds'))}초 · 모델 시간 합계 {number(ops['model_seconds'])}초")
    if len(audit["monitor_runs"]) > 1:
        lines.append(f"- 전체 경과는 첫 실행 시작부터 마지막 복구 종료까지의 관측 구간이며 사이 대기시간도 포함합니다. 각 프로그램 경과시간 합계는 {number(summary.get('summed_program_seconds'))}초입니다.")
    final_count = sum(call["evidence_link"] == "final_evidence" for call in audit["calls"])
    lines.append(f"- 최종 evidence와 호출 기록이 일치하는 호출 {final_count}회 / 초기·교체 또는 미연결 호출 {len(audit['calls']) - final_count}회. 전체 토큰에는 두 범주를 모두 포함했습니다.")
    if recovery := audit["business_recovery"]:
        lines.append(f"- **사업 자료 복구 발생** · 본문 확보 기업 {number(recovery['initial_available_businesses'])}→{number(recovery['recovered_available_businesses'])}개 · 복구 단계의 추가 보유 분석 {number(recovery['new_holding_calls'])}회")
        match = lambda value: "일치" if value is True else "불일치" if value is False else "미기록"
        reused = "예" if recovery['holdings_reused'] is True else "아니오" if recovery['holdings_reused'] is False else "미기록"
        lines.append(f"- 복구 감사의 동일성 · 기준시각 {match(recovery['same_cutoff'])} / 후보군 {match(recovery['same_universe'])} / 시세 {match(recovery['same_price_snapshots'])} · 원래 보유 분석 재사용 {reused}")
    lines.append(f"- 명시적으로 저장된 심층 재시도 {audit['explicit_deep_retries']}건 · 같은 역할의 반복 토론은 재시도로 추정하지 않았습니다.")
    rows = audit["holdings"]
    lines.append(f"- 보유 {len(rows)}종목 · 판단 보류 {sum(row['blocked'] for row in rows)}종목 · 기대 세션 일치 {sum(row['session_status'] == '일치' for row in rows)}종목 / 날짜 미확인·불일치 {sum(row['session_status'] != '일치' for row in rows)}종목")
    supplement = audit["supplement_refresh"]
    if supplement["provided"]:
        counts = supplement["counts"]
        lines.append(f"- **공식 보강자료 재확인** · 고유 출처 {counts['sources']}개 / 종목별 기록 {counts['records']}개 · 핵심 본문 확인 {counts['verified_core_text']}개 / 일부 확인 {counts['partial_core_text']}개 / 이전 자료·오늘 본문 미확인 {counts['dated_unrevalidated']}개")
        lines.append("- 본문 확인은 기업·상품 식별, 기간, 지정 수치의 핵심 문구 점검입니다. 전체 서술·현재 NAV·모든 공시 사건의 검증 완료를 뜻하지 않습니다.")
        limited = sorted({row.get("ticker", "공통") for row in supplement["records"] if row.get("status") != "verified_core_text"})
        if limited:
            lines.append(f"- **입력 품질 확인 필요 · {', '.join(limited)}** · 기존 발표일·수치를 유지한 자료 중 오늘 본문 재확인이 안 된 항목이 있습니다. 오늘 새로 검증한 사실로 취급하지 않았습니다.")
    else:
        lines.append("- 공식 보강자료 재확인 감사가 첨부되지 않아, 저장된 자료의 보유와 오늘 본문 재확인을 구분해 확인하지 못했습니다.")
    quality = audit["judgment_quality"]
    adoption = quality["public_adoption"]
    if adoption["collected_unique_items"]:
        lines.append(f"- **신규 공개정보의 실제 근거 채택** · 수집 {adoption['collected_unique_items']}건 / 보유 브리프 {adoption['briefs']}개 × 근거 필드 {adoption['fields_per_brief']}개에서 고유 {adoption['cited_unique_items']}건 인용")
        if not adoption["cited_unique_items"]:
            lines.append("- 신규 공개정보는 수집됐으나 이번 제안 근거로 실제 채택된 정보는 0건입니다(근거 ID 인용 기준). 수집 성공을 정성 판단 통합 성공으로 표시하지 않았습니다. 기존 공식 보강자료 반영과는 별도입니다.")
    review_tickers = sorted(set(quality["metadata_only_tickers"]) | {issue["ticker"] for issue in quality["unresolved_material"]})
    if review_tickers:
        lines.append(f"- **검토 필요 상태 · {', '.join(review_tickers)}** · 중요 공시 본문 또는 독립 검토의 중요 쟁점이 미해결입니다.")
    if summary.get("started_at"):
        lines.append(f"- 실제 시작 · {decision_time(summary['started_at'])} (한국시간)")
    if summary.get("finished_at"):
        lines.append(f"- 실제 종료 · {decision_time(summary['finished_at'])} (한국시간)")
    if audit["integrity_notices"]:
        lines.extend(["", "## 기록 검증에서 확인할 사항", "", *(f"- {safe(note)}" for note in audit["integrity_notices"])])
    if audit["reuse_markers"]:
        lines.extend(["", "## 저장본·재시도 연결", "", *(f"- {safe(key)} · {safe(value)}" for key, value in audit["reuse_markers"].items())])
    lines.extend(["", "## 보유 종목의 관측일과 판단 상태", "",
                  "기대 거래일은 실행 명령에 지정한 날짜와 비교했습니다. 날짜 일치는 데이터 정확성 전체를 보증하지 않습니다."])
    actions = {"maintain": "유지", "increase": "확대", "reduce": "축소", "sell": "매도", "review_required": "판단 보류"}
    for row in audit["holdings"]:
        status = "판단 보류" if row["blocked"] else actions.get(row["action"], row["status"] or "미기록")
        if not row["blocked"] and row["status"] == "review_required":
            status = "검토 필요 · 잠정 " + status
        lines.append(f"- **{safe(row['label'])}** · 실제 관측 {safe(row['observed_session'] or '미확인')} / 기대 {safe(row['expected_session'] or '미지정')} · {row['session_status']} · {safe(status)}")
    lines.extend(["", "## 판단 경로와 미해결 검토 사항", "",
                  f"- 보유 모델 분석 {len(quality['analyzed_tickers'])}종목 · 입력 차단으로 보류 {len(quality['blocked_tickers'])}종목 · specialist 단계 {quality['specialist_calls']}회 / debate 단계 {quality['debate_calls']}회"])
    for item in quality["escalations"]:
        lines.append(f"- {safe(item['ticker'])} · 결론 조정 {item['reconcile']}회 / 독립 재검토 {item['rereview']}회")
    for ticker in quality["metadata_only_tickers"]:
        lines.append(f"- **{safe(ticker)} · 중요 공시 본문 미확인** · 접수 정보만 확보돼 사건 해석과 제안에는 검토가 필요합니다.")
    for item in quality["action_changes"]:
        status = "중요 쟁점이 남아 검토 필요" if item["material_issues_remain"] else safe(item["status"])
        lines.append(f"- **{safe(item['ticker'])} 제안 변경** · {safe(actions.get(item['before'], item['before']))}→{safe(actions.get(item['after'], item['after']))} · {status}")
    for issue in quality["unresolved_material"]:
        lines.append(f"- **{safe(issue['ticker'])} 미해결 중요 쟁점** · {safe(issue['claim'], 350)}")
    lines.extend(["", "## 역할별 호출과 토큰", ""])
    for component in audit["monitor_runs"]:
        scoped = [call for call in audit["calls"] if call.get("monitor_id") == component["directory"]]
        measured = measurement(scoped)
        scoped_usage = measured["usage"]
        lines.append(f"- **실행별 합계 · {safe(Path(component['directory']).name)}** · 프로그램 {safe(component['summary'].get('status') or '미확인')} · {measured['calls']}회 / 오류 {measured['failed_calls']}건 · 입력 {number(scoped_usage['input_tokens'])} / 출력 {number(scoped_usage['output_tokens'])} · 프로그램 {number(component['summary'].get('wall_seconds'))}초 / 모델합계 {number(measured['model_seconds'])}초")
    for item in audit["exit_classification"]["runs"]:
        lines.append(f"- **종료 해석 · {safe(Path(item['monitor_id']).name if item['monitor_id'] else '실행')}** · 원 exit {safe(item['exit_code'])} / status {safe(item['status'])} · {item['interpretation']}")
    groups = defaultdict(list)
    for call in audit["calls"]:
        groups[(call.get("monitor_label") or "증거 기록", call.get("ticker") or "공통", call["role"], call["stage"])].append(call)
    for (scope, ticker, role, stage), calls in groups.items():
        measured = measurement(calls)
        u = measured["usage"]
        lines.append(f"- **{safe(scope)} · {safe(ticker)} · {safe(ROLE_LABELS.get(role, role))} / {safe(stage)}** · {len(calls)}회, 오류 {measured['failed_calls']}건 · 입력 {number(u['input_tokens'])} / 출력 {number(u['output_tokens'])} / 캐시 {number(u['cached_input_tokens'])} · {number(measured['model_seconds'])}초")
    for call in audit["calls"]:
        if call.get("error"):
            lines.append(f"- 호출 오류 {safe(call.get('call_id') or call.get('role'))} · {safe(call['error'])}")
    lines.extend(["", "## 수집·검증 단계", ""])
    for phase in audit["phases"]:
        status = {"phase_completed": "완료", "phase_error": "실패", "not_finished": "종료 미확인"}[phase["status"]]
        scope = f"{safe(phase.get('monitor_label'))} · " if phase.get("monitor_label") else ""
        lines.append(f"- {scope}{safe(phase['phase'])} · {status} · {number(phase['seconds'])}초" + (f" · {safe(phase['error'])}" if phase.get("error") else ""))
    for stage, status in audit["stage_status"].items():
        lines.append(f"- {ROLE_LABELS[stage]} · {safe(status)}")
    presentation = audit["presentation_review"]
    if presentation:
        lines.extend(["", "## 저장된 사실과 대조한 표시 정정", "",
                      f"- 추가 모델 호출 {number(presentation.get('new_model_calls'))}회 · 모델 원문 수정 여부 {safe(presentation.get('raw_model_output_modified'))}",
                      "- 원래 모델 출력과 제안은 보존하고, 저장된 최신 세션 사실에 모순되는 표시만 정정했습니다. 최신 하루의 확인을 과거 전체 시계열 검증으로 확대하지 않았습니다."])
        for ticker, item in presentation.get("tickers", {}).items():
            facts = item.get("verified_facts", {})
            fields = ", ".join(change.get("field", "미기록") for change in item.get("changes", []))
            lines.append(f"- **{safe(ticker)}** · 확인된 최신 종료 {safe(facts.get('closed_at_kst'))} (한국시간), 종가 {number(facts.get('last_close'))} · 정정 필드 {safe(fields)} · 원상태 {safe(item.get('original_status'))}→표시 {safe(item.get('display_status'))} · 행동 변경 없음 {safe(item.get('action_unchanged'))}")
    lines.extend(["", "## 중복 제거와 재검증 기록", ""])
    for stage, projection in audit["projections"].items():
        lines.append(f"- {ROLE_LABELS[stage]} · 중복 text {len(projection.get('removed_texts', []))}개 · 입력 JSON {number(projection.get('before_chars'))}→{number(projection.get('after_chars'))}자 · 삭제 문단 {number(projection.get('dropped_passages'))}개")
    if audit["projections"]:
        lines.append("- 위 감소량은 입력 문자의 차이입니다. 비교 모델 실행이 없으면 토큰·금액 절감률로 환산하지 않습니다.")
    else:
        lines.append("- 이번 실행에 저장된 중복 제거 감사 기록이 없습니다.")
    for review in audit["revalidations"]:
        lines.append(f"- 재검증 {safe(review['status'])} · 추가 모델 {number(review['new_model_calls'])}회 · 원문 수정 {safe(review['output_modified'])} · 최초 오류는 호출 기록에 유지")
    failures = audit["source_failures"]
    lines.extend(["", "## 수집 경로의 실패 기록", "", f"- 진단 레코드 {len(failures)}건입니다. 같은 요청이 여러 진단 경로에 나타날 수 있어 고유 HTTP 실패 횟수로 해석하지 않습니다."])
    if audit["http_operations"]["available"]:
        http = audit["http_operations"]
        lines.append(f"- 요청 모니터에 기록된 실제 공급원 요청 {http['requests']}회 · 실패 응답 {http['failed_requests']}회")
    if audit["sidecar_http_operations"]["requests"]:
        sidecar = audit["sidecar_http_operations"]
        lines.append(f"- 별도 사업 자료 복구 수집 기록 {sidecar['requests']}회 · 실패 응답 {sidecar['failed_requests']}회. 프로그램 모니터 밖의 별도 수집 경로입니다.")
    for failure in failures[:16]:
        lines.append(f"- {safe(failure['path'], 115)} · {failure['error']}" + (f" · {safe(failure['url'], 180)}" if failure.get("url") else ""))
    if len(failures) > 16:
        lines.append("- 나머지 진단 위치는 runtime-verification.json에 보관했습니다.")
    if supplement["provided"]:
        lines.extend(["", "## 공식 보강자료 재확인 범위", "",
                      "HTTP 성공 응답만으로 본문 재확인을 인정하지 않았습니다. 아래 상태는 입력 품질을 설명하며 기존 판단 차단 규칙을 추가하지 않습니다."])
        names = {"verified_core_text": "핵심 본문 확인", "partial_core_text": "일부 본문 확인",
                 "dated_unrevalidated": "이전 자료 · 오늘 본문 미확인"}
        limited_records = [row for row in supplement["records"] if row.get("status") != "verified_core_text"]
        for item in limited_records:
            label = names.get(item.get("status"), "상태 미확인")
            lines.append(f"- **{safe(item.get('ticker'))} · {label}** · {safe(item.get('id'))} · HTTP {safe(item.get('http_status'))} / 추출 {number(item.get('extracted_characters'))}자 · 원 발표일 {safe(item.get('published_at') or '미기록')}")
        if not limited_records:
            lines.append("- 감사에서 부분 확인·오늘 본문 미확인으로 분류된 종목별 기록은 없습니다.")
    lines.extend(["", "## 원본 연결과 무결성", "", f"- 근거 JSON · {safe(evidence_path, 600)}",
                  f"- 근거 파일 SHA-256 · `{evidence_sha256}`",
                  f"- 모니터 파일 {len(audit['monitor_files'])}개의 위치·해시는 runtime-verification.json에 기록했습니다."])
    for check in audit["input_hashes"]:
        lines.append(f"- {ROLE_LABELS[check['stage']]} 입력 해시 · {'일치' if check['matches'] else '불일치'}")
    if supplement.get("trace"):
        lines.append(f"- 공식 보강자료 감사 · {safe(supplement['trace']['path'], 600)}")
        lines.append(f"- 공식 보강자료 감사 SHA-256 · `{supplement['trace']['sha256']}`")
    if audit["required_gaps"]:
        lines.extend(["", "## 필수 자료 공백과 판단 영향", ""])
        checks = []
        from .discovery import FIELD_NAMES, HORIZONS
        translations = {**FIELD_NAMES, "latest_verified_quarter": "확인된 최신 분기 실적"}
        for gap in audit["required_gaps"]:
            status = gap.get("status", "missing")
            field = gap.get("field", "")
            impact = str(gap.get("impact") or "자료 확인 전 해당 판단 보류")
            for key, label in translations.items():
                impact = impact.replace(key, label)
            checks.append((safe(gap.get("importance", "필수")), safe(f"{gap.get('ticker', '공통')} · {HORIZONS.get(field, FIELD_NAMES.get(field, field))}"),
                           status if status in STATUS else "missing", safe(impact)))
        rows = list(appendix_table_rows(checks))
        rows[0] = "| 중요도 | 자료·분석기간 | 수집 상태 | 판단 영향 |"
        lines.extend(rows)
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--monitor-dir", type=Path)
    parser.add_argument("--additional-monitor-dir", type=Path, action="append", default=[])
    parser.add_argument("--supplement-audit", type=Path)
    parser.add_argument("--expected-us-session")
    parser.add_argument("--expected-kr-session")
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--pdf", action="store_true")
    args = parser.parse_args()
    raw = args.evidence.read_bytes()
    record = json.loads(raw.decode("utf-8-sig"))
    supplement = None
    if args.supplement_audit:
        supplement_raw = args.supplement_audit.read_bytes()
        supplement = json.loads(supplement_raw.decode("utf-8-sig"))
        supplement["_trace"] = {"path": str(args.supplement_audit.resolve()), "sha256": sha256(supplement_raw).hexdigest()}
    artifacts = {name: {"path": str((args.evidence.parent / name).resolve()), "exists": (args.evidence.parent / name).is_file()}
                 for name in ("holdings.pdf", "discovery.pdf", "holdings.md", "discovery.md")}
    audit = build_audit(record, load_monitors(args.monitor_dir, args.additional_monitor_dir), expected_us=args.expected_us_session, expected_kr=args.expected_kr_session,
                        supplement_audit=supplement, artifacts=artifacts)
    audit.update(evidence_path=str(args.evidence.resolve()), evidence_sha256=sha256(raw).hexdigest())
    markdown = render_audit(audit, audit["evidence_path"], audit["evidence_sha256"])
    args.output_dir.mkdir(parents=True, exist_ok=True)
    stem = args.output_dir / "runtime-verification"
    if args.evidence.resolve() in {stem.with_suffix(suffix).resolve() for suffix in (".json", ".md", ".pdf")}:
        raise ValueError("Runtime outputs must not overwrite the source evidence")
    stem.with_suffix(".json").write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    stem.with_suffix(".md").write_text(markdown, encoding="utf-8")
    if args.pdf:
        from .report_pdf import write_pdf
        write_pdf(stem.with_suffix(".pdf"), markdown)
    if args.evidence.read_bytes() != raw:
        raise ValueError("Evidence changed while runtime report was being built; rebuild from a finished run")
    print(stem.resolve())
    return int(bool(audit["integrity_notices"]))


if __name__ == "__main__":
    raise SystemExit(main())
