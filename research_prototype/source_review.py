"""Deterministic presentation review against saved latest-session verification."""

from copy import deepcopy
from hashlib import sha256
import json
import re
from zoneinfo import ZoneInfo

from framework_validation.runner import parse_timestamp


SESSION_DENIAL = re.compile(
    r"(?:정확한\s*(?:관측\s*)?세션|완료\s*세션|관측\s*세션).{0,20}(?:미확정|미확인|확인되지|없어|없다|미검증)"
)


def verified_latest_session(source, cutoff):
    if source.get("session_verification") != "matching_naver_closed_regular_session_latest_only":
        return None
    ticker = source.get("market_symbol")
    if not ticker or source.get("id") != f"{ticker}-stock-price-window":
        return None
    if source.get("session_check_url") != f"https://polling.finance.naver.com/api/realtime/domestic/stock/{ticker}":
        return None
    latest = (source.get("observations") or [{}])[-1]
    if latest.get("session_date") != source.get("observation_date"):
        return None
    if latest.get("session_completion_bound") != source.get("observation_end_at"):
        return None
    close = source.get("metrics", {}).get("last_close")
    if close is None or close != latest.get("close"):
        return None
    try:
        end = parse_timestamp(source["observation_end_at"])
        available = parse_timestamp(source["available_at"])
        if not end <= available <= parse_timestamp(cutoff):
            return None
    except (KeyError, TypeError, ValueError):
        return None
    return {"ticker": ticker, "source_id": source["id"], "last_close": close,
            "closed_at_kst": end.astimezone(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M"),
            "observation_date": source["observation_date"], "price_url": source.get("source_url"),
            "session_check_url": source["session_check_url"],
            "calendar_source_url": latest.get("session_calendar_source_url"),
            "verification_scope": "latest_session_only",
            "source_sha256": sha256(json.dumps(source, ensure_ascii=False, sort_keys=True).encode()).hexdigest()}


def session_review_views(sections, briefs, cutoff):
    """Return reviewed display copies; never rewrite model calls or recommendations."""
    views = deepcopy(briefs)
    notices, audits = {}, {}
    for section in sections:
        ticker = section["ticker"]
        source = section.get("sources", {}).get(f"{ticker}-stock-price-window", {})
        facts = verified_latest_session(source, cutoff)
        if not facts or ticker not in views:
            continue
        changes = []
        for field, value in views[ticker].items():
            if not isinstance(value, dict) or not SESSION_DENIAL.search(value.get("text", "")):
                continue
            before = deepcopy(value)
            sentences = re.split(r"(?<=[.!?])\s+(?=[가-힣A-Z])", value["text"])
            retained = [sentence for sentence in sentences if not SESSION_DENIAL.search(sentence)]
            if field == "issue":
                retained.append(f"최신 종가는 {facts['closed_at_kst']} 서울 정규장 종료 자료로 대조됐습니다.")
            elif "총수익" in value["text"]:
                retained.append("분배금 재투자 총수익은 미확인으로 남아 중기 총수익 성과 판단에 제약이 있습니다.")
            else:
                retained.append(f"최신 종가의 거래 세션은 {facts['closed_at_kst']} 서울 정규장으로 대조됐습니다.")
            value["text"] = " ".join(retained)
            value["evidence_ids"] = list(dict.fromkeys([*value.get("evidence_ids", []), facts["source_id"]]))
            changes.append({"field": field, "before": before, "after": deepcopy(value),
                            "reason": "Saved latest-session verification contradicts the model's absence claim"})
        if not changes:
            continue
        original_status = views[ticker].get("status")
        views[ticker]["status"] = "review_required"
        notices[ticker] = (
            f"수집 기록 대조 · {facts['closed_at_kst']} 서울 정규장 종가 {facts['last_close']:,.0f}원을 "
            "네이버 일봉과 종료된 정규장 응답에서 대조했습니다. 확인 범위는 최신 하루이며 과거 시계열 전체의 "
            "개별 세션 대조는 남아 있습니다. 모델의 세션 미확인 서술을 이 사실에 맞춰 정리했고, "
            "해당 전제로 작성한 행동 제안은 재평가 전까지 잠정으로 표시합니다."
        )
        audits[ticker] = {"verified_facts": facts, "changes": changes, "original_status": original_status,
                         "display_status": "review_required", "action_unchanged": views[ticker].get("action") == briefs[ticker].get("action"),
                         "new_model_calls": 0, "raw_model_output_modified": False}
    return views, notices, audits
