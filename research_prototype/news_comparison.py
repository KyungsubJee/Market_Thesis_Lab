"""Compare frozen news-integration experiments without collecting data or using a model."""

import argparse
from collections import Counter
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path

from framework_validation.runner import parse_timestamp

from .decision_briefs import FIELDS
from .runtime_report import evidence_calls, judgment_quality, load_monitors, number, safe
from .report import decision_links
from .week3 import measurement


def digest(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def items_of(record, experiment=False):
    context = (record.get("news_context") or {}).get("items", {}) if experiment else record.get("public_context", {})
    if isinstance(context, list):
        return context
    return [{**item, "ticker": item.get("ticker", ticker)} for ticker, value in context.items()
            for item in (value.get("items", []) if isinstance(value, dict) else value)]


def references(value, allowed):
    if isinstance(value, dict):
        return set(value).intersection(allowed) | set().union(*(references(child, allowed) for child in value.values()), set())
    if isinstance(value, list):
        return set().union(*(references(child, allowed) for child in value), set())
    if isinstance(value, str):
        return {key for key in allowed if key == value or key in value}
    return set()


def call_input(call):
    return call.get("input_payload") or call.get("input") or call.get("prompt") or {}


def call_output(call):
    return call.get("output") or call.get("raw_output") or call.get("unvalidated_output") or {}


def without_processing_ledger(value):
    if isinstance(value, dict):
        return {key: without_processing_ledger(child) for key, child in value.items()
                if key not in {"item_reviews", "news_reviews", "news_review", "source_reviews", "checked_item_ids"}}
    if isinstance(value, list):
        return [without_processing_ledger(child) for child in value]
    return value


def selected(record):
    return {pick["ticker"] for result in (record.get("discovery") or {}).get("horizons", {}).values()
            for pick in result.get("shortlist", [])}


def displayed_brief(record, ticker):
    brief = deepcopy(record.get("decision_briefs", {}).get("holdings", {}).get(ticker, {}))
    review = record.get("presentation_review", {}).get("tickers", {}).get(ticker, {})
    for change in review.get("changes", []):
        if isinstance(change.get("after"), dict):
            brief[change["field"]] = deepcopy(change["after"])
    return brief


def immutable_sources(record):
    result = {}
    for ticker, fixture in record.get("holdings_inputs", {}).items():
        for source in fixture.get("evidence", []):
            result[f"snapshot/{ticker}/{source['id']}"] = digest(source)
    sources = [(f"holdings/{section['ticker']}", section.get("sources", {}))
               for section in record.get("holdings", {}).get("sections", [])]
    sources.append(("discovery", (record.get("discovery") or {}).get("evidence", {})))
    for prefix, rows in sources:
        for key, source in rows.items():
            if prefix.startswith("holdings/") or key.startswith(("price-", "sec-")) or key.endswith(("price-window", "sec-periodic-results")):
                result[f"{prefix}/{key}"] = digest(source)
    return result


def paid_calls(record, monitor=None, *, experiment=False):
    if monitor and monitor.get("provided"):
        return monitor["calls"]
    if experiment:
        return (record.get("news_integration") or {}).get("new_calls")
    return evidence_calls(record)


def processing_entries(record):
    rows, seen = [], set()
    for root in (record.get("news_processing"), (record.get("discovery") or {}).get("news_processing")):
        entries = root if isinstance(root, list) else root.get("source_reviews", []) if isinstance(root, dict) else []
        for entry in entries:
            fingerprint = digest(entry)
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            rows.append({**entry, "item_id": entry.get("item_id") or entry.get("source_id"),
                         "status": entry.get("status") or entry.get("disposition") or "미기록"})
    return rows


def call_ticker(call):
    return call.get("ticker") or (call.get("input_payload") or {}).get("ticker")


def cost_groups(calls, blocked_tickers=()):
    names = ("holdings", "blocked_news_only", "report_synthesis", "business_screen", "business_deep", "other")
    if calls is None:
        return {key: measurement(None) for key in names}
    groups = {key: [] for key in names}
    for call in calls:
        role = call.get("role")
        if call_ticker(call) in blocked_tickers and role == "event_attention":
            key = "blocked_news_only"
        elif role in {"business_screen", "business_deep"} or call.get("stage") in {"business_screen", "business_deep"}:
            key = role if role in {"business_screen", "business_deep"} else call["stage"]
        elif role in {"decision_brief", "decision_briefs"}:
            key = "report_synthesis"
        elif call.get("ticker") or role in {"macro_market", "sector", "fundamental", "price_trading", "event_attention", "reviewer", "synthesis"}:
            key = "holdings"
        else:
            key = "other"
        groups[key].append(call)
    return {key: measurement(value) for key, value in groups.items()}


def role_costs(before_calls, after_calls, blocked_tickers):
    def group(calls):
        result = {}
        for call in calls or []:
            scope = "차단 종목 뉴스 검토" if call_ticker(call) in blocked_tickers else "보유·공통 단계"
            key = (scope, call.get("role", "미기록"), call.get("stage", "미기록"))
            result.setdefault(key, []).append(call)
        return result
    old, new = group(before_calls), group(after_calls)
    return [{"scope": key[0], "role": key[1], "stage": key[2],
             "before": measurement(old.get(key, []) if before_calls is not None else None),
             "after": measurement(new.get(key, []) if after_calls is not None else None)} for key in sorted(old.keys() | new.keys())]


def recovery_audit(record, calls):
    history = record.get("news_integration", {}).get("holding_attempt_history", {})
    reused, identical = 0, 0
    for section in record.get("holdings", {}).get("sections", []):
        prior = [call for attempt in history.get(section["ticker"], []) for call in attempt.get("calls", [])]
        for call in section.get("calls", []):
            if call.get("reused_successful_stage"):
                reused += 1
                identical += any(all(old.get(key) == call.get(key) for key in ("role", "stage", "prompt", "input_payload", "output"))
                                 and not old.get("error") for old in prior)
    completed = ((record.get("discovery") or {}).get("model_status") == "complete" and
                 (record.get("decision_assessment") or {}).get("status") == "complete" and
                 not any(section.get("error") or section.get("status") == "execution_error" for section in record.get("holdings", {}).get("sections", [])))
    return {"retry_tickers": sorted(history), "reused_successful_roles": reused, "matching_original_roles": identical,
            "initial_failed_calls": [{"ticker": call_ticker(call), "role": call.get("role"), "stage": call.get("stage"),
                                      "error": call.get("error"), "call_id": call.get("call_id"), "monitor_id": call.get("monitor_id")}
                                     for call in calls or [] if call.get("error") or call.get("validation_status") == "error"],
            "final_model_stages_completed": completed}


def news_metrics(record, calls, *, experiment=False):
    items = items_of(record, experiment)
    ids = {item["id"] for item in items if item.get("id")}
    inputs, outputs, narrative_outputs, failed_outputs, detailed = set(), set(), set(), set(), []
    early = set()
    for call in calls or []:
        seen = references(call_input(call), ids)
        used = references(call_output(call), ids)
        inputs.update(seen)
        failed = bool(call.get("error") or call.get("validation_status") == "error" or call.get("transport_status") == "error")
        if failed:
            failed_outputs.update(used)
        else:
            outputs.update(used)
            narrative_outputs.update(references(without_processing_ledger(call_output(call)), ids))
        if call.get("stage") in {"analysis", "specialist", "debate"}:
            early.update(seen)
        if seen or used:
            detailed.append({"ticker": call_ticker(call), "role": call.get("role"), "stage": call.get("stage"),
                             "input_ids": sorted(seen), "output_ids": sorted(used) if not failed else [],
                             "failed_output_ids": sorted(used) if failed else [], "accepted_output": not failed,
                             "call_id": call.get("call_id"), "monitor_id": call.get("monitor_id")})
    if not experiment:
        # The saved final synthesis input is authoritative even if fallback call records omit its prompt.
        inputs.update(ids.intersection((record.get("decision_assessment") or {}).get("input", {}).get("sources", {})))
    cited = set()
    for branch in record.get("decision_briefs", {}).values():
        for brief in branch.values():
            fields = brief.values() if not any(key in brief for key in FIELDS) else [brief]
            for field_group in fields:
                for field in FIELDS:
                    cited.update((field_group.get(field) or {}).get("evidence_ids", []) if isinstance(field_group, dict) else [])
    for analysis in (record.get("discovery") or {}).get("business_analyses", {}).values():
        for field in ("business", "strategy", "moat", "evidence", "thesis", "valuation", "counterargument"):
            cited.update((analysis.get(field) or {}).get("evidence_ids", []))
        for horizon in analysis.get("horizons", {}).values():
            for field in ("proposal", "reassessment"):
                cited.update((horizon.get(field) or {}).get("evidence_ids", []))
    reviews = processing_entries(record)
    reviewed = {entry.get("item_id") for entry in reviews if entry.get("reason")}
    status_counts = Counter(entry.get("status", "미기록") for entry in reviews)
    unique_items = {item["id"]: item for item in items if item.get("id")}
    body_counts = Counter(item.get("body_verification_status") or item.get("body_status") or item.get("grade") or "미기록" for item in unique_items.values())
    early_zero = [row.get("ticker") for row in record.get("event_collection", [])
                  if row.get("kind") == "headline_lookup" and row.get("status") == "complete_zero"]
    return {"collected_records": len(items), "unique_urls": len({item.get("source_url") or item.get("url") for item in items if item.get("source_url") or item.get("url")}),
            "collected_unique_ids": len(ids),
            "source_ids": sorted(ids), "model_input_unique": len(inputs), "analysis_input_unique": len(early),
            "analysis_output_unique": len(outputs), "final_cited_unique": len(cited.intersection(ids)),
            "failed_output_unique": len(failed_outputs),
            "nonledger_output_unique": len(narrative_outputs),
            "reviewed_unique": len(reviewed.intersection(ids)), "processing_records": len(reviews),
            "input_without_processing_reason": sorted(inputs - reviewed),
            "early_zero_lookup_tickers": early_zero,
            "article_shares_not_in_model": sum(item.get("community_subtype") == "article_share" and item.get("id") not in inputs for item in items),
            "collector_exclusions": len((record.get("news_context") or {}).get("excluded", [])) if experiment else None,
            "processing_status_counts": dict(status_counts), "body_status_counts": dict(body_counts),
            "role_trace": detailed, "admitted_ids": sorted(inputs), "cited_ids": sorted(cited.intersection(ids))}


def compare_runs(baseline, experiment, baseline_monitor=None, experiment_monitor=None):
    before_calls = paid_calls(baseline, baseline_monitor)
    after_calls = paid_calls(experiment, experiment_monitor, experiment=True)
    before = news_metrics(baseline, before_calls)
    after = news_metrics(experiment, after_calls, experiment=True)
    critical, notices = [], []
    old_sections = {section["ticker"]: section for section in baseline.get("holdings", {}).get("sections", [])}
    new_sections = {section["ticker"]: section for section in experiment.get("holdings", {}).get("sections", [])}
    blocked_tickers = {ticker for ticker, section in old_sections.items() if section.get("path") == "blocked"}
    if old_sections.keys() != new_sections.keys():
        critical.append("보유 종목 집합이 고정 조건과 다릅니다.")
    for key in ("as_of", "qualitative_as_of"):
        if baseline.get(key) != experiment.get(key):
            critical.append(f"{key} 기준시각이 고정 조건과 다릅니다.")
    old_sources, new_sources = immutable_sources(baseline), immutable_sources(experiment)
    changed_sources = [key for key, value in old_sources.items() if new_sources.get(key) != value]
    if changed_sources:
        critical.append("가격·공시·거시·공식 보강 기초 자료가 변경되거나 누락됐습니다: " + ", ".join(changed_sources[:5]))
    scope = experiment.get("news_integration", {}).get("scope", {})
    fixed = set(scope.get("frozen_candidate_tickers", []))
    if fixed != selected(baseline):
        critical.append("재평가할 고정 후보 집합이 기준 결과와 다릅니다.")
    if selected(experiment) - fixed:
        critical.append("고정 후보 밖의 종목이 새 후보에 포함됐습니다.")
    if scope.get("rerun_preliminary_screen") is not False:
        notices.append("40개 예비 검토를 다시 하지 않았다는 실행 범위 표식이 미확인입니다.")
    actual_model = experiment.get("holdings", {}).get("model_requested")
    if actual_model and actual_model != baseline.get("holdings", {}).get("model_requested"):
        critical.append("비교 모델이 변경됐습니다.")
    for ticker, old in old_sections.items():
        new = new_sections.get(ticker, {})
        if old.get("path") == "blocked" and (new.get("path") != "blocked" or (new.get("final") or {}).get("action") not in {None, "review_required"}):
            critical.append(f"{ticker}: 필수 자료 차단이 뉴스만으로 해제됐습니다.")
    gate_changes = []
    for ticker in old_sections:
        old_gate = baseline.get("coverage", {}).get("holdings", {}).get("tickers", {}).get(ticker, {})
        new_gate = experiment.get("coverage", {}).get("holdings", {}).get("tickers", {}).get(ticker, {})
        before_gate = {"path": old_sections[ticker].get("path"), "status": old_sections[ticker].get("status"),
                       "blocking": old_gate.get("blocking", []), "filing": old_gate.get("filing", {}).get("status")}
        after_gate = {"path": new_sections.get(ticker, {}).get("path"), "status": new_sections.get(ticker, {}).get("status"),
                      "blocking": new_gate.get("blocking", []), "filing": new_gate.get("filing", {}).get("status")}
        if before_gate != after_gate:
            gate_changes.append({"ticker": ticker, "before": before_gate, "after": after_gate,
                                 "requires_source_or_review_reason": True})
        if before_gate["filing"] == "metadata_only" and after_gate["filing"] == "available" and not new_gate.get("filing", {}).get("evidence_ids"):
            critical.append(f"{ticker}: 중요 공시 본문 근거 없이 접수 정보 제한이 해제됐습니다.")
        old_required = baseline.get("holdings_inputs", {}).get(ticker, {}).get("required_evidence_ids")
        new_required = experiment.get("holdings_inputs", {}).get(ticker, {}).get("required_evidence_ids")
        if old_required is not None and old_required != new_required:
            critical.append(f"{ticker}: 원래 필수 근거 ID 조건이 변경됐습니다.")
    cutoff = parse_timestamp(experiment.get("qualitative_as_of") or experiment["as_of"])
    admitted = set(after["admitted_ids"])
    for item in items_of(experiment, True):
        if item.get("id") not in admitted:
            continue
        stamp = item.get("available_at")
        try:
            if not stamp or parse_timestamp(stamp) > cutoff:
                critical.append(f"{item.get('id')}: 실제 역할 입력의 가용시각이 공개 기준을 충족하지 못합니다.")
            if item.get("grade") == "verified_body" and item.get("publisher_updated_at") and parse_timestamp(item["publisher_updated_at"]) > cutoff:
                critical.append(f"{item.get('id')}: 공개 기준 이후 수정된 본문이 검증 본문으로 전달됐습니다.")
        except (TypeError, ValueError):
            critical.append(f"{item.get('id')}: 가용시각을 해석하지 못했습니다.")
    if after_calls is None:
        notices.append("실제 신규 호출 기록이 없어 비용을 계산하지 않았습니다. 기준 호출을 새 호출로 재사용하지 않습니다.")
    elif not experiment_monitor and any(call.get("reused_successful_stage") for call in after_calls):
        notices.append("성공 단계 재사용 기록이 포함돼 실제 유료 호출 수는 복수 모니터로 대조해야 합니다.")
    if not baseline_monitor:
        notices.append("기준 비용은 현재 evidence 호출만 포함하므로 초기 교체 호출을 포함한 전체 비용은 모니터로 보완해야 합니다.")
    changes = []
    ids = set(after["source_ids"])
    for ticker, original in baseline.get("decision_briefs", {}).get("holdings", {}).items():
        old, new = displayed_brief(baseline, ticker), displayed_brief(experiment, ticker)
        for field in ("outlook", "counterargument", "proposal", "reassessment"):
            previous, current = old.get(field) or {}, new.get(field) or {}
            changes.append({"branch": "holdings", "ticker": ticker, "field": field, "before": previous.get("text", ""), "after": current.get("text", ""),
                            "before_raw": (original.get(field) or {}).get("text", ""),
                            "text_changed": previous.get("text") != current.get("text"),
                            "new_source_ids": sorted(set(current.get("evidence_ids", [])).intersection(ids)), "semantic_review_required": True})
    old_analyses = (baseline.get("discovery") or {}).get("business_analyses", {})
    new_analyses = (experiment.get("discovery") or {}).get("business_analyses", {})
    for ticker in fixed:
        for field in ("business", "strategy", "moat", "evidence", "thesis", "valuation", "counterargument"):
            previous, current = old_analyses.get(ticker, {}).get(field) or {}, new_analyses.get(ticker, {}).get(field) or {}
            changes.append({"branch": "discovery", "ticker": ticker, "field": field, "before": previous.get("text", ""),
                            "after": current.get("text", ""), "text_changed": previous.get("text") != current.get("text"),
                            "new_source_ids": sorted(set(current.get("evidence_ids", [])).intersection(ids)), "semantic_review_required": True})
        for horizon in new_analyses.get(ticker, {}).get("horizons", {}):
            for field in ("proposal", "reassessment"):
                previous = (old_analyses.get(ticker, {}).get("horizons", {}).get(horizon, {}).get(field) or {})
                current = new_analyses[ticker]["horizons"][horizon].get(field) or {}
                changes.append({"branch": "discovery", "ticker": ticker, "horizon": horizon, "field": field,
                                "before": previous.get("text", ""), "after": current.get("text", ""),
                                "text_changed": previous.get("text") != current.get("text"),
                                "new_source_ids": sorted(set(current.get("evidence_ids", [])).intersection(ids)), "semantic_review_required": True})
    source_index = {item["id"]: {key: item.get(key) for key in ("id", "ticker", "title", "publisher", "source_url", "published_at",
        "available_at", "collected_at", "publisher_updated_at", "grade", "fact_verified", "body_sha256", "body_path", "event_cluster_id")}
        for item in items_of(experiment, True) if item.get("id")}
    coverage = []
    new_items = items_of(experiment, True)
    for ticker in sorted(set(old_sections) | fixed):
        rows = {item["id"]: item for item in new_items if item.get("ticker") == ticker and item.get("id")}
        coverage.append({"ticker": ticker, "collected_unique": len(rows),
                         "body_available": sum(item.get("content_basis") == "verified_body" for item in rows.values())})
    recovery = baseline.get("business_recovery") or {}
    initial_collection = (recovery.get("original_discovery") or {}).get("business_collection", {})
    return {"baseline_run_id": baseline.get("run_id"), "experiment_run_id": experiment.get("run_id"),
            "before": {"public": before, "quality": judgment_quality(baseline)},
            "after": {"public": after, "quality": judgment_quality(experiment)},
            "cost": {"baseline_full_calls": measurement(before_calls), "experiment_new_calls": measurement(after_calls),
                     "baseline_groups": cost_groups(before_calls, blocked_tickers), "experiment_groups": cost_groups(after_calls, blocked_tickers),
                     "role_comparison": role_costs(before_calls, after_calls, blocked_tickers)},
            "fixed": {"as_of": baseline.get("as_of"), "public_as_of": baseline.get("qualitative_as_of"),
                      "holdings": sorted(old_sections), "candidates": sorted(fixed), "changed_base_sources": changed_sources,
                      "baseline_source_records": len(old_sources), "added_source_paths": sorted(new_sources.keys() - old_sources.keys()),
                      "gate_changes": gate_changes},
            "critical_failures": critical, "measurement_notices": notices, "field_changes": changes,
            "source_index": source_index,
            "news_coverage_by_ticker": coverage,
            "baseline_recovery": {"initial_business_sources": initial_collection.get("available_business_count"),
                                  "recovered_business_sources": (baseline.get("discovery") or {}).get("business_collection", {}).get("available_business_count")},
            "experiment_recovery": recovery_audit(experiment, after_calls),
            "collection_audit": {"summary": (experiment.get("news_context") or {}).get("summary", {}),
                                 "collected_at": (experiment.get("news_context") or {}).get("collected_at")},
            "item_processing": processing_entries(experiment), "rechecks": experiment.get("news_integration", {}).get("recheck", []),
            "presentation_review": experiment.get("presentation_review") or {},
            "collector_exclusion_records": (experiment.get("news_context") or {}).get("excluded", []),
            "interpretation_limits": ["동일 행동이나 인용 0건만으로 실패로 판정하지 않습니다",
                "역할 입력의 ID 존재는 내용의 올바른 이해나 인과 기여를 증명하지 않습니다.",
                "문장 변경만으로 뉴스의 효과를 확정하지 않습니다. 원문과 분석의 의미 대조가 필요합니다.",
                "본문 확보·게시시각 검증은 기사 속 주장의 사실 검증과 구분합니다."]}


def render_comparison(audit):
    lines = ["# 뉴스 분석 통합 실행 비교", "",
             "- 동일한 보유 종목과 고정 후보를 비교했습니다. 기존 결과·원문과 새 호출 기록은 별도 보존합니다.",
             "- 신규 실행은 40개 예비 검토 생략, 기업 심층 입력 8개→고정 후보 재평가로 범위가 줄었습니다. 전체 토큰 차이를 뉴스 통합의 효율 향상으로 해석하지 않습니다.",
             "- 비교 대상의 기초 역할·조건부 검토와 최종 보고 요약은 단계별로 나눠 확인합니다.",
             "- 뉴스 외 입력 전달 오류 수정도 포함됩니다(두 국내 ETF의 세션 메타데이터, 재개 시 기존 섹터 근거 연결). 관련 문장·확신도 변화와 비용 차이를 모두 뉴스 효과로 돌리지 않습니다.",
             "- 시세 차단 종목의 뉴스 전용 검토가 추가됐다면 기존 7종목의 전체 분석과 구분합니다. 거래 판단 차단을 푸는 단계는 아닙니다.",
             f"- 고정 기초 기준 · {safe(audit['fixed']['as_of'])} / 공개 기준 · {safe(audit['fixed']['public_as_of'])}"]
    lines.extend(["", "## 고정 조건과 계측 상태", ""])
    if audit["critical_failures"]:
        lines.extend(f"- **비교 조건 위반** · {safe(item)}" for item in audit["critical_failures"])
    else:
        lines.append("- 구현된 고정 조건 검사에서 차이를 발견하지 못했습니다. 의미·사실 검토 전체의 통과를 의미하지 않습니다.")
    lines.append(f"- 원래 기초 자료 {audit['fixed']['baseline_source_records']}개 경로의 내용 해시를 대조했습니다. 추가 자료 경로 {len(audit['fixed']['added_source_paths'])}개는 기존 자료 변경과 분리했습니다.")
    for change in audit["fixed"]["gate_changes"]:
        labels = {"review_required": "검토 필요", "complete": "분석 완료", "blocked": "판단 보류", "execution_error": "실행 오류"}
        before = labels.get(change["before"].get("status"), "상태 미확인")
        after = labels.get(change["after"].get("status"), "상태 미확인")
        lines.append(f"- **{safe(change['ticker'])}: {before}→{after}** · 기존 자료 재해석과 새 독립 검토도 포함돼 뉴스 효과만으로 설명할 수 없습니다.")
    lines.extend(f"- {safe(item)}" for item in audit["measurement_notices"])
    lines.extend(["", "## 수집에서 실제 분석까지", ""])
    labels = (("collected_records", "종목별 수집 기록"), ("collected_unique_ids", "고유 자료 ID"), ("unique_urls", "고유 URL"),
              ("model_input_unique", "모델 입력에 전달된 ID"), ("analysis_input_unique", "기초·전문 분석 역할 입력 ID"),
              ("analysis_output_unique", "검증 통과 역할 출력에 명시된 ID"), ("failed_output_unique", "실패 원출력의 ID · 성공 집계와 구분"),
              ("nonledger_output_unique", "처리 로그 외 검증 통과 분석 출력의 ID"),
              ("reviewed_unique", "자료별 처리 이유가 있는 ID"),
              ("final_cited_unique", "최종 보유·후보 판단에서 인용한 ID"))
    for key, label in labels:
        lines.append(f"- {label} · 이전 {audit['before']['public'][key]} → 개선 {audit['after']['public'][key]}")
    lines.append(f"- 이전 기사 공유 {audit['before']['public']['article_shares_not_in_model']}개는 모델 입력에서 확인되지 않았습니다. 사전 필터 규칙과 모델의 자료별 채택·제외 판단은 구분해야 합니다.")
    lines.append(f"- 이전 초기 독립 뉴스 조회에서 해당 결과 0건인 종목 · {', '.join(audit['before']['public']['early_zero_lookup_tickers']) or '해당 기록 없음'}")
    lines.append(f"- 이번 수집 단계의 별도 제외 기록 · {number(audit['after']['public']['collector_exclusions'])}건 · 상세 원인과 시점 근거는 비교 JSON에서 확인합니다.")
    lines.append("- 자료별 사유나 검토 ID 목록만 출력한 경우와 분석 본문·최종 판단에 근거를 연결한 경우를 따로 집계했습니다.")
    for row in audit["news_coverage_by_ticker"]:
        if not row["collected_unique"] or not row["body_available"]:
            lines.append(f"- {safe(row['ticker'])} 뉴스 범위 · 고유 {row['collected_unique']}건 / 본문 확보 {row['body_available']}건 · 자료 부재나 제목 한계를 다른 종목의 본문 확보로 대체하지 않습니다.")
    body_labels = {"verified_body": "본문 확보·시점 확인", "headline_only": "제목만 확보", "individual_opinion": "개인 의견", "미기록": "상태 미기록"}
    body_counts = " / ".join(f"{body_labels.get(key, key)} {value}건" for key, value in audit["after"]["public"]["body_status_counts"].items())
    lines.append("- 이번 본문 상태(고유 자료 ID 기준) · " + safe(body_counts, 450))
    lines.append("- 이번 자료 처리 상태 · " + safe(audit["after"]["public"]["processing_status_counts"], 450))
    collection = audit["collection_audit"]["summary"]
    requests = collection.get("cumulative_requests", collection.get("requests"))
    lines.append(f"- 별도 뉴스 수집의 외부 HTTP 요청 · {number(requests)}회(수집 감사 기록 기준). 모델 실행 모니터 이전의 수집으로 유료 모델 호출 수와 구분합니다.")
    lines.append("- 고정 공개 기준 이전의 자료를 나중에 조회한 후향 검증입니다. 수집시각을 공개시각으로 바꾸지 않았으며, 본문 확보는 기사 주장의 독립 사실검증이나 당시 버전의 완전한 재현을 보증하지 않습니다.")
    lines.extend(["", "## 실제 호출·토큰과 실행 범위", ""])
    recovery = audit["baseline_recovery"]
    if recovery["initial_business_sources"] is not None:
        lines.append(f"- 이전 실행에는 초기 사업 본문 확보 {number(recovery['initial_business_sources'])}개에서 {number(recovery['recovered_business_sources'])}개로 복구한 과정이 포함됩니다. 초기 예비 검토 호출도 전체 비용에서 제외하지 않았습니다.")
    for key, label in (("baseline_full_calls", "이전 전체 실행"), ("experiment_new_calls", "이번 신규 유료 실행")):
        ops = audit["cost"][key]
        lines.append(f"- **{label}** · {number(ops['calls'])}회 / 오류 {number(ops['failed_calls'])}회 · 입력 {number(ops['usage']['input_tokens'])} / 출력 {number(ops['usage']['output_tokens'])} / 캐시 {number(ops['usage']['cached_input_tokens'])} 토큰 · 모델 합계 {number(ops['model_seconds'])}초")
    recovery = audit["experiment_recovery"]
    if recovery["initial_failed_calls"]:
        lines.append(f"- **초기 중단과 복구** · 실패 호출 {len(recovery['initial_failed_calls'])}회 보존 / 성공 역할 {recovery['reused_successful_roles']}개 재사용(기존 입력·원응답 일치 {recovery['matching_original_roles']}개) / 대상 {', '.join(recovery['retry_tickers'])}")
        if recovery["final_model_stages_completed"]:
            lines.append("- 실패한 단계만 다시 실행하고 나머지 분석·최종 정리를 완료했습니다. 초기 중단은 최종 실행 실패와 구분하며, 실패 호출의 시간·토큰도 전체에 포함했습니다.")
        for failure in recovery["initial_failed_calls"]:
            reason = str(failure["error"] or "원인 미기록")
            if "News disposition must cover every supplied item" in reason:
                reason = "입력한 기사들의 개별 검토를 누락해 검증 단계에서 중단"
            elif "cited evidence absent from this prompt" in reason:
                reason = "해당 역할 입력에 없는 기존 근거 ID를 인용해 검증 단계에서 중단"
            lines.append(f"  - {safe(failure['ticker'])} / {safe(failure['role'])} · {safe(reason, 180)}")
    for group, label in (("holdings", "보유 분석·독립 검토"), ("blocked_news_only", "시세 차단 종목의 추가 뉴스 검토"), ("report_synthesis", "최종 보고 요약"),
                         ("business_screen", "40개 예비 검토"), ("business_deep", "기업 심층 · 입력 대상 수 다름"), ("other", "그 밖의 호출")):
        old, new = audit["cost"]["baseline_groups"][group], audit["cost"]["experiment_groups"][group]
        lines.append(f"- {label} · 이전 {number(old['calls'])}회 / 입력 {number(old['usage']['input_tokens'])} / 출력 {number(old['usage']['output_tokens'])} → 개선 {number(new['calls'])}회 / 입력 {number(new['usage']['input_tokens'])} / 출력 {number(new['usage']['output_tokens'])}")
    lines.extend(["", "## 역할·단계별 실제 계측", "",
                  "입력·캐시는 프로그램의 토큰 계측값이며 청구 금액이 아닙니다. 안내문·스키마·세션 메타데이터, 임시 실행 컨텍스트와 캐시 조건이 달라 증가분을 기사 본문 탓으로만 설명할 수 없습니다."])
    for row in audit["cost"]["role_comparison"]:
        lines.append(f"- **{safe(row['scope'])} · {safe(row['role'])} / {safe(row['stage'])}**")
        for key, label in (("before", "이전"), ("after", "개선")):
            ops = row[key]
            lines.append(f"  - {label} · {number(ops['calls'])}회 / 프롬프트 {number(ops['prompt_chars'])}자 / 입력 {number(ops['usage']['input_tokens'])} / 출력 {number(ops['usage']['output_tokens'])} / 캐시 {number(ops['usage']['cached_input_tokens'])} / 오류 {number(ops['failed_calls'])}회")
    lines.extend(["", "## 전망·반론·제안·관찰 조건의 변화", "",
                  "새 출처를 연결한 변화 중 기업별 최대 두 가지를 보여줍니다. 모든 필드 차이는 비교 JSON에 보관합니다. 문장 차이만으로 인과 기여를 판정하지 않습니다."])
    fields = {"outlook": "전망", "counterargument": "반론", "proposal": "제안", "reassessment": "관찰 조건",
              "business": "사업 설명", "strategy": "산업 변화", "moat": "차별성", "evidence": "실행 성과", "thesis": "성장과 수익", "valuation": "가격 가정"}
    counts = Counter()
    priorities = {"reassessment": 0, "counterargument": 1, "proposal": 2}
    ordered = sorted(audit["field_changes"], key=lambda row: (-len(row["new_source_ids"]), row["ticker"], priorities.get(row["field"], 3)))
    for change in ordered:
        if change["new_source_ids"] and counts[change["ticker"]] < 2:
            counts[change["ticker"]] += 1
            lines.extend(["", f"### {safe(change['ticker'])} · {fields[change['field']]}",
                          f"- 이전 · {safe(change['before'], 240)}", f"- 개선 · {safe(change['after'], 300)}",
                          f"- 새 공개 출처 인용 · {len(change['new_source_ids'])}개"])
            if links := decision_links(change["new_source_ids"], audit["source_index"], limit=2):
                lines.append("- 원문 연결 · " + links)
    if not counts:
        lines.append("- 최종 필드에 새 공개 출처가 직접 연결된 사례는 없습니다. 이것만으로 내부 고려 여부를 단정하지 않습니다.")
    lines.extend(["", "## 판단 제한의 유지와 변경", ""])
    for branch, label in (("before", "이전"), ("after", "개선")):
        quality = audit[branch]["quality"]
        lines.append(f"- {label} · 입력 차단 {len(quality['blocked_tickers'])}종목 / 공시 본문 미확인 {len(quality['metadata_only_tickers'])}종목 / 독립 검토 미해결 중요 쟁점 {len(quality['unresolved_material'])}건")
        for issue in quality["unresolved_material"]:
            lines.append(f"  - {safe(issue['ticker'])} · {safe(issue['claim'], 260)}")
    lines.extend(["", "## 품질 해석의 한계", "", *(f"- {safe(item)}" for item in audit["interpretation_limits"]),
                  "- 미채택은 사유가 해당 자료의 관련성·신뢰도·시점·기존 근거와 부합하는지 검토합니다. 인용 수나 추천 변경을 목표로 강제하지 않습니다.",
                  "- 전체 자료별 사유·역할 입력 연결·원문 ID·재검토 기록은 비교 JSON에 보관했습니다."])
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--experiment", type=Path, required=True)
    parser.add_argument("--baseline-monitor-dir", type=Path)
    parser.add_argument("--baseline-additional-monitor-dir", type=Path, action="append", default=[])
    parser.add_argument("--experiment-monitor-dir", type=Path)
    parser.add_argument("--experiment-additional-monitor-dir", type=Path, action="append", default=[])
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--pdf", action="store_true")
    args = parser.parse_args()
    old_raw, new_raw = args.baseline.read_bytes(), args.experiment.read_bytes()
    old, new = json.loads(old_raw.decode("utf-8-sig")), json.loads(new_raw.decode("utf-8-sig"))
    old_monitor = load_monitors(args.baseline_monitor_dir, args.baseline_additional_monitor_dir) if args.baseline_monitor_dir else None
    new_monitor = load_monitors(args.experiment_monitor_dir, args.experiment_additional_monitor_dir) if args.experiment_monitor_dir else None
    audit = compare_runs(old, new, old_monitor, new_monitor)
    audit["trace"] = {"baseline": {"path": str(args.baseline.resolve()), "sha256": sha256(old_raw).hexdigest()},
                      "experiment": {"path": str(args.experiment.resolve()), "sha256": sha256(new_raw).hexdigest()}}
    if args.baseline.read_bytes() != old_raw or args.experiment.read_bytes() != new_raw:
        raise ValueError("Source evidence changed during comparison; wait for a completed run")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    stem = args.output_dir / "news-integration-comparison"
    if stem.with_suffix(".json").resolve() in {args.baseline.resolve(), args.experiment.resolve()}:
        raise ValueError("Comparison must not overwrite the source evidence")
    stem.with_suffix(".json").write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    markdown = render_comparison(audit)
    stem.with_suffix(".md").write_text(markdown, encoding="utf-8")
    if args.pdf:
        from .report_pdf import write_pdf
        write_pdf(stem.with_suffix(".pdf"), markdown)
    print(stem.resolve())
    return int(bool(audit["critical_failures"]))


if __name__ == "__main__":
    raise SystemExit(main())
