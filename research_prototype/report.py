"""Load holdings and fixed evidence, run the workflow, and write a report pair."""

import csv
from datetime import datetime, timezone
from html import escape
import json
from pathlib import Path
import re
from urllib.parse import urlsplit
from uuid import uuid4

from framework_validation.optimized_runner import evidence_cutoff, run_optimized, summarize_calls
from framework_validation.runner import load_fixture, parse_timestamp

from .events import EVENT_PREFIX, reaction_sentence
from .coverage import KOREA_TIME, STATUS, event_summary, public_comparison_lines, public_url, render_public_coverage
from .instruments import holding_label
from .source_review import session_review_views

ACTIONS = {
    "maintain": "유지",
    "increase": "확대",
    "reduce": "축소",
    "sell": "매도",
}
STATUSES = {"complete": "검토 완료", "review_required": "검토 필요", "execution_error": "실행 실패",
            "not_requested": "모델 미호출"}
HORIZONS = {"short": "단기", "medium": "중기", "long": "장기"}
SNAPSHOT_FIELDS = (
    "case_id", "ticker", "as_of", "horizon", "question", "evidence", "missing_fields",
)


def load_holdings(path):
    """Read an ordered ticker list. Explicit held=false rows are excluded."""
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if not reader.fieldnames or "ticker" not in reader.fieldnames:
            raise ValueError("Holdings CSV requires a ticker column")
        if len(set(reader.fieldnames)) != len(reader.fieldnames):
            raise ValueError("Holdings CSV has duplicate headers")
        selected = []
        seen = set()
        for row in reader:
            ticker = (row["ticker"] or "").strip().upper()
            if not ticker or ticker in seen:
                raise ValueError(f"Missing or duplicate ticker on CSV row {reader.line_num}")
            seen.add(ticker)
            held = (row.get("held") or "true").strip().casefold()
            if held not in {"true", "false"}:
                raise ValueError(f"held must be true or false on CSV row {reader.line_num}")
            if held == "true":
                selected.append(ticker)
    if not selected:
        raise ValueError("Holdings CSV has no selected tickers")
    return selected


def load_snapshots(directory):
    snapshots = {}
    for path in sorted(Path(directory).glob("*.json")):
        raw = load_fixture(path)
        try:
            fixture = {key: raw[key] for key in SNAPSHOT_FIELDS}
            ticker = fixture["ticker"].strip().upper()
            parse_timestamp(fixture["as_of"])
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"Invalid fixed snapshot {path.name}: {exc}") from exc
        if not ticker or ticker in snapshots:
            raise ValueError(f"Empty or duplicate snapshot ticker in {path.name}")
        if "required_evidence_ids" in raw:
            fixture["required_evidence_ids"] = raw["required_evidence_ids"]
        snapshots[ticker] = (path.resolve(), fixture)
    if not snapshots:
        raise ValueError(f"No fixed snapshots found in {directory}")
    return snapshots


def source_index(fixture):
    cutoff = parse_timestamp(fixture["as_of"])
    result = {}
    seen = set()
    for source in fixture["evidence"]:
        evidence_id = source["id"]
        if not isinstance(evidence_id, str) or not evidence_id or evidence_id in seen:
            raise ValueError(f"Invalid or duplicate evidence ID: {evidence_id}")
        seen.add(evidence_id)
        if parse_timestamp(source["available_at"]) > evidence_cutoff(fixture, source):
            continue
        url = source["source_url"]
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"} or not parsed.netloc
            or any(ch.isspace() or ch in "<>" for ch in url)
        ):
            raise ValueError(f"Evidence must have an HTTP source URL: {evidence_id}")
        result[evidence_id] = source
    return result


def collect(tickers, snapshots, model_factory, model_name, *, source_mode="fixed"):
    run = {
        "run_id": uuid4().hex,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model_requested": model_name,
        "source_mode": source_mode,
        "holdings": list(tickers),
        "sections": [],
    }
    for ticker in tickers:
        section = {"ticker": ticker, "status": "execution_error", "sources": {}, "final": None}
        run["sections"].append(section)
        if ticker not in snapshots:
            section["error"] = "해당 종목의 고정 분석 자료가 없습니다."
            continue
        path, fixture = snapshots[ticker]
        section.update(
            as_of=fixture["as_of"], horizon=fixture["horizon"], question=fixture["question"],
            required_evidence_ids=list(fixture.get("required_evidence_ids", [])),
        )
        if path is not None:
            section["fixture_file"] = str(path)
        model = None
        try:
            section["sources"] = source_index(fixture)
            model = model_factory()
            result = run_optimized(fixture, model)
            section.update(result, **summarize_calls(model.calls), calls=model.calls)
        except (KeyError, OSError, TypeError, ValueError, RuntimeError) as exc:
            section["error"] = str(exc)
            if model is not None:
                section.update(summarize_calls(model.calls), calls=model.calls)
    return run


def one_line(value):
    return escape(" ".join(str(value).splitlines()), quote=False)


def reader_line(value):
    value = re.sub(r"\[([^\]]+)\]\(<https?://[^>]+>\)", r"\1", str(value))
    value = re.sub(r"https?://\S+|\b(?:event-context|price|sec|qual|us-fred)-[a-z0-9]+(?:-[a-z0-9]+)+\b", "", value)
    value = re.sub(r"[（(]\s*[）)]", "", value)
    return one_line(value.strip())


def source_links(ids, sources):
    return " · ".join(
        f"[{one_line(evidence_id)}](<{sources[evidence_id]['source_url']}>)"
        for evidence_id in dict.fromkeys(ids) if not evidence_id.startswith(EVENT_PREFIX)
    )


def render_week3(run, sources):
    week3 = run["week3"]
    coverage = week3["coverage"]
    lines = ["", "### 근거 충족도", ""]
    roles = coverage["analysis_sections"]
    for role, label in (("macro_market", "거시경제·시장"), ("sector", "섹터")):
        citations = source_links(roles[role]["evidence_ids"], sources)
        status = {"with_citations": "분석 및 인용 근거 확보", "no_citations": "분석에 인용 근거 없음",
                  "unavailable": "분석 미완료"}[roles[role]["status"]]
        lines.append(f"- {label} · {status}" + (f" · {citations}" if citations else ""))
    for role, label in (("stock", "종목"), ("market", "시장 대용 ETF"), ("sector", "섹터 대용 ETF")):
        bar = coverage["daily_bars"][role]
        if not bar["observation_date"]:
            lines.append(f"- {label} 일봉 · 확보하지 못함")
            continue
        evidence = source_links([bar["evidence_id"]], sources)
        line = f"- {label} 최종 확보 일봉 · `{bar['observation_date']}`"
        if bar["status"] == "newer_session_unconfirmed":
            line += f" · 공급자는 `{bar['provider_latest_session']}` 호가 세션을 표시했으나 확정 일봉은 미확인"
        else:
            line += " · 공급자의 최종 관측 범위 기준"
        lines.append(f"{line} · {evidence}")
    same_session = {True: "같은 관측일", False: "관측일이 다름", None: "일부 일봉 미확보"}
    lines.append(f"- 종목·시장·섹터 비교 기준 · {same_session[coverage['same_session']]}")
    alignment = {True: "일치", False: "불일치", None: "자료 부족"}
    windows = " · ".join(
        f"{window}거래일 {alignment[aligned]}" for window, aligned in coverage["lookback_session_alignment"].items()
    )
    lines.append(f"- 수익률 비교용 거래일 배열 · {windows}")
    filing = coverage["sec_8k_body"]
    fund = coverage["fund_structure"]
    if fund:
        ids = [fund[key] for key in ("terms_id", "sec_prospectus_id", "holdings_id") if fund[key]]
        lines.append("- 펀드 상품 구조·보유내역 · " + (source_links(ids, sources) if ids else "확보하지 못함"))
        lines.append("- 공식 일일 추적 · 동일 기준일의 공식 지수와 펀드 NAV 수익률 미확보")
    elif filing["evidence_ids"]:
        lines.append(f"- SEC 8-K · 접수 메타데이터 확보, 본문·첨부 미수집 · {source_links(filing['evidence_ids'], sources)}")
    else:
        lines.append("- SEC 8-K · 분석 스냅샷에 접수 내역과 본문이 없음")
    if not fund:
        lines.append("- 발표 전 컨센서스 · 미수집. 실적 서프라이즈는 확인할 수 없음")

    valuation = coverage["valuation"]
    lines.extend(["", "### 밸류에이션과 판단", ""])
    if valuation["observed_close_usd"] is not None:
        price_link = source_links([valuation["price_evidence_id"]], sources)
        lines.append(f"- 관측 종가 · USD {valuation['observed_close_usd']:,.2f} · {price_link}")
    if fund:
        nav, price = fund["issuer_nav_usd"], fund["issuer_market_price_usd"]
        if nav is not None and price is not None:
            terms = source_links([fund["terms_id"]], sources)
            lines.append(
                f"- 발행사 동일일 표시 · `{fund['issuer_nav_date']}` NAV USD {nav:,.2f} "
                f"· 시장가격 USD {price:,.2f} · {terms}"
            )
        lines.append("- 종목 가치평가 대신 일일 레버리지·경로·비용과 동일일 NAV의 범위를 평가")
    else:
        lines.append("- 적정가 · 미산정. 검증된 추정치와 평가 가정이 확보되지 않음")
    if valuation["judgment_action"]:
        action = ACTIONS.get(valuation["judgment_action"], "판단 보류")
        citations = source_links(valuation["judgment_evidence_ids"], sources)
        lines.append(f"- 연구 판단 · {action}. 판단 근거는 보고서의 종목 분석에서 확인" +
                     (f" · {citations}" if citations else ""))

    operations = week3["operations"]
    lines.extend(["", "### 실행 및 사용량", ""])
    modes = {
        "fresh": "신규 수집 및 분석",
        "render_replay": "저장 결과 재렌더",
        "frozen_reanalysis": "동결 근거 재분석",
    }
    lines.append(f"- 실행 방식 · {modes[week3['mode']]}")
    if week3["original_run_id"]:
        lines.append(f"- 원본 실행 ID · `{week3['original_run_id']}`")
    if week3["snapshot_sha256"]:
        lines.append(f"- 입력 SHA-256 · `{week3['snapshot_sha256']}`")
    if operations["wall_seconds"] is not None:
        lines.append(f"- 이번 실행 경과 · {operations['wall_seconds']:,.3f}초")
    for label, metrics in (("이번 실행", operations["this_run"]), ("원본 분석", operations["original_capture"])):
        if metrics is None:
            continue
        usage = metrics["usage"]
        counts = (metrics["calls"], usage["input_tokens"], usage["cached_input_tokens"], usage["output_tokens"])
        calls, inputs, cached, outputs = ("미기록" if value is None else f"{value:,}" for value in counts)
        lines.append(f"- {label} · 모델 호출 {calls}회 · 입력 {inputs} · 입력 중 캐시 {cached} · 출력 {outputs} 토큰")
        activity = (
            ("추가 검토", metrics["extra_calls"]), ("실패", metrics["failed_calls"]),
            ("사용량 미기록", metrics["calls_without_usage"]),
        )
        lines.append(
            f"  - {label} 호출 내역 · " + " · ".join(
                f"{key} {value:,}회" if value is not None else f"{key} 미기록" for key, value in activity
            )
        )
        if largest := metrics["largest_input_call"]:
            lines.append(
                f"  - 기록된 최대 입력 · `{largest['role']}/{largest['stage']}` "
                f"· {largest['input_tokens']:,} 토큰"
            )
    lines.append("- 청구 금액 · Codex CLI에서 제공하지 않음")
    return lines


def render_reassessment(checks, sources):
    rows = [
        "| 확인 시점 | 확인 기준 | 기준 충족·확인 후 행동 | 확인에 사용할 근거 |",
        "| --- | --- | --- | --- |",
    ]
    for check in checks:
        fields = [one_line(check[key]).replace("|", "&#124;") for key in ("when", "criterion", "action")]
        refs = source_links(check["evidence_ids"], sources)
        rows.append("| " + " | ".join([*fields, refs]) + " |")
    return rows


DECISION_FIELDS = (
    ("issue", "왜 지금 봐야 하나"), ("current", "현재 상태"), ("outlook", "앞으로의 전망"),
    ("counterargument", "가장 강한 반론"), ("reassessment", "다시 판단할 기준"),
)
BENCHMARK_NAMES = {
    "SPY": "S&P 500", "SOXX": "미국 반도체", "UNG": "미국 천연가스 선물",
    "XLK": "미국 기술주", "XLI": "미국 산업재", "XLV": "미국 헬스케어", "XLP": "미국 필수소비재",
    "091160": "KODEX 반도체", "069500": "KODEX 200", "133690": "TIGER 미국나스닥100",
}
PAST_ACTION = re.compile(r"\b기존\s+(축소|확대|매도|유지)\s+판단을\s+유지(한다|합니다)")
ACTION_IN_RECHECK = re.compile(r"(?<!\w)(유지|확대|축소|매도)(?:\s+판단)?(?:를|을|로)?\s*(?:재검토|검토)")


def normalize_decision_text(value):
    verbs = {"축소": "비중을 축소", "확대": "비중을 확대", "매도": "매도", "유지": "보유를 유지"}
    value = PAST_ACTION.sub(lambda match: verbs[match[1]] + match[2], value)
    return re.sub(r"\b기존\s+보유를\s+유지(한다|합니다)", r"보유를 유지\1", value)


def decision_benchmarks(lines):
    body = "\n".join(lines)
    names = [f"{ticker}({one_line(name)})" for ticker, name in BENCHMARK_NAMES.items()
             if re.search(rf"(?<![A-Za-z0-9]){ticker}(?![A-Za-z0-9])", body)]
    return f"- 비교에 쓰는 ETF · {' · '.join(names)}" if names else None


def decision_time(value):
    return parse_timestamp(value).astimezone(KOREA_TIME).strftime("%Y-%m-%d %H:%M") if value else "미확인"


def decision_heading(run, title):
    lines = [f"# {title}", "", f"- 가격·공시 등 기초 분석 기준 · {decision_time(run.get('as_of'))} (한국시간)"]
    if public_cutoff := run.get("qualitative_as_of"):
        lines.append(f"- 뉴스·공개 의견 검토 기준 · {decision_time(public_cutoff)} (한국시간)")
    lines.append("- [원문·수집 상태·검토 기록](evidence.json)")
    return lines


def decision_links(evidence_ids, sources, *, limit=None):
    links = {}
    for key in evidence_ids:
        source = sources.get(key, {})
        if not (url := public_url(source)) or url in links:
            continue
        publisher = source.get("publisher") or source.get("source_name")
        if symbol := source.get("market_symbol"):
            label = f"{symbol} 시세"
        elif source.get("form") == "issuer_release":
            label = "기업 실적 발표"
        elif form := source.get("form"):
            label = f"SEC {form}"
        elif source.get("category") == "etf_product" or key.endswith("fund-terms"):
            label = "운용사 상품 설명"
        elif publisher:
            label = publisher
        else:
            label = urlsplit(url).hostname.removeprefix("www.")
        # Source labels come from external text. Keep the Markdown link syntax intact.
        label = one_line(label).replace("[", "&#91;").replace("]", "&#93;")
        links[url] = f"[{label}](<{url}>)"
        if limit is not None and len(links) >= limit:
            break
    return " · ".join(links.values())


def decision_statement(field, sources):
    value = reader_line(field.get("text", ""))
    if value and (links := decision_links(field.get("evidence_ids", []), sources, limit=2)):
        return value + " · " + links
    return value


def decision_macro_lines(run, sources, *, markets=("US", "KR")):
    macro = run.get("decision_macro") or {}
    rows = []
    if "text" in macro:
        rows = [decision_statement(macro, sources)]
    else:
        names = {"US": "미국", "KR": "한국"}
        for country in markets:
            if statement := decision_statement(macro.get(country, {}), sources):
                rows.append(f"{names[country]} · {statement}")
    rows = [row for row in rows if row]
    return ["", "## 공통 거시·시장", "", *(f"- {row}" for row in rows)] if rows else []


def decision_core_sources(evidence_ids, sources):
    candidates = list(dict.fromkeys(key for key in evidence_ids if public_url(sources.get(key, {}))))

    def group(key):
        source = sources[key]
        if source.get("category") in {"etf_product", "etf_prospectus"} or key.endswith("fund-terms"):
            return "structure"
        if source.get("form") in {"10-Q", "10-K", "10-Q/A", "10-K/A"}:
            return "financial"
        if source.get("category") == "sec_periodic" or key.endswith(("-quarter", "-annual")):
            return "financial"
        if source.get("market_symbol") or source.get("category") == "etf_comparison":
            return "price"
        return "qualitative"

    def best(keys):
        # Prefer the legal risk document, then issuer disclosure, over headline-only sources.
        return min(keys, key=lambda key: (
            0 if sources[key].get("form") == "497K" else
            1 if sources[key].get("form") or sources[key].get("category") in {"etf_product", "supplemental_official_source"}
            or key.endswith("fund-terms") else 2,
            candidates.index(key),
        ))

    groups = {name: [key for key in candidates if group(key) == name]
              for name in ("structure", "financial", "price", "qualitative")}
    if groups["structure"]:
        selected = [best(groups["structure"])]
        other = groups["price"] or groups["financial"] or groups["qualitative"]
    else:
        selected = [best(groups["financial"] or groups["price"])] if groups["financial"] or groups["price"] else []
        other = groups["qualitative"]
    if other:
        selected.append(best(other))
    if not selected:
        selected = [best(candidates)] if candidates else []
    return selected


def decision_brief_lines(brief, sources, action_label, action, *, blocked=False, tentative=False):
    lines = [f"> **{action_label} · {action}**"]
    proposal = reader_line(normalize_decision_text(brief.get("proposal", {}).get("text", "")))
    if not blocked and proposal and not tentative:
        lines.append(f"> {proposal}")
    if not blocked and (alternative := brief.get("proposed_action")) and alternative != brief.get("action"):
        alternative = ACTIONS.get(alternative, {"research": "추가 조사", "monitor": "관찰", "defer": "보류"}.get(alternative))
        if alternative:
            lines.append(f"- **독립 검토 전 대안** · {alternative}")
    for key, label in DECISION_FIELDS:
        if statement := reader_line(normalize_decision_text(brief.get(key, {}).get("text", ""))):
            lines.append(f"- **{label}** · {statement}")
    if not brief:
        lines.append("- 통합 판단을 작성할 수 없었습니다. 근거 기록에서 저장된 결과를 확인할 수 있습니다.")
    fields = ["current", "issue", "counterargument", "outlook", "reassessment"]
    if not blocked and not tentative and proposal:
        fields.insert(0, "proposal")
    evidence_ids = [ref for field in fields for ref in brief.get(field, {}).get("evidence_ids", [])
                    if brief.get(field, {}).get("text")]
    if refs := decision_links(decision_core_sources(evidence_ids, sources), sources, limit=2):
        lines.append(f"- 핵심 출처 · {refs}")
    return lines


def decision_gap_lines(ticker_coverage, brief, *, horizon=None):
    relevant = {"필수", "비교 시 필수", "이슈 판단 시 필수"}
    if horizon == "medium":
        relevant.add("중기 판단 시 필수")
    elif horizon == "long":
        relevant.add("장기 판단 시 필수")
    required_gaps = [
        f"{one_line(label)} {STATUS[status]}"
        for importance, label, status, _ in ticker_coverage.get("checks", [])
        if (importance in relevant
            and status in {"missing", "partial", "stale", "failed", "metadata_only"})
    ]
    if brief.get("missing_evidence_ids") and not required_gaps:
        required_gaps.append("판단에 필요한 일부 근거를 확인하지 못했습니다.")
    return ["- **판단을 제한한 자료 공백** · " + " · ".join(required_gaps)] if required_gaps else []


def decision_source_index(sources, public_context):
    return {**sources, **{item["id"]: item for item in public_context.get("items", []) if item.get("id")}}


def holding_action(section, status, brief):
    blocked = bool(status.get("blocking") or section.get("path") == "blocked" or brief.get("status") == "blocked")
    original = (section.get("final") or {}).get("action")
    action = ACTIONS.get(original) if brief and not blocked and brief.get("action") == original else None
    tentative = action and (status.get("review_required") or section.get("status") != "complete"
                            or brief.get("status") != "complete")
    return action, tentative


def restore_action_conditions(brief, final):
    current = brief.get("reassessment", {}).get("text", "")
    used = set(ACTION_IN_RECHECK.findall(current))
    retained = []
    for condition in final.get("reassessment_conditions", []):
        actions = set(ACTION_IN_RECHECK.findall(condition))
        if actions - used:
            retained.append(reader_line(condition))
            used.update(actions)
    return [f"- **행동을 바꿀 조건** · {condition}" for condition in retained]


def render_decision_holdings(run, public_context):
    if "source_verification_notes" not in run:
        briefs, notes, audits = session_review_views(run["sections"], run["decision_briefs"], run["as_of"])
        run = {**run, "decision_briefs": briefs, "source_verification_notes": notes, "session_review_audits": audits}
    coverage = run.get("coverage", {}).get("tickers", {})
    labels = {section["ticker"]: coverage.get(section["ticker"], {}).get("label")
              or holding_label(section["ticker"], section["sources"].values()) for section in run["sections"]}
    lines = decision_heading(run, "보유 종목 연구 보고서")
    lines.append(f"- 분석 대상 · 보유 종목 {len(run['sections'])}개")
    overview = {}
    for section in run["sections"]:
        ticker = section["ticker"]
        action, tentative = holding_action(section, coverage.get(ticker, {}), run["decision_briefs"].get(ticker, {}))
        label = f"잠정 {action}" if tentative else action or "판단 보류"
        overview.setdefault(label, []).append(one_line(labels[ticker]))
    if overview:
        lines.extend(["", "## 현재 행동 제안", "", *(f"- **{label}** · {', '.join(tickers)}" for label, tickers in overview.items())])
    limits = []
    groups = {"price": [], "filing": [], "issues": [], "session_review": [], "dated": []}
    for section in run["sections"]:
        ticker = section["ticker"]
        status = coverage.get(ticker, {})
        if any(key.endswith("stock-price-window") for key in status.get("blocking", [])):
            groups["price"].append(labels[ticker])
        if status.get("filing", {}).get("status") == "metadata_only":
            groups["filing"].append(labels[ticker])
        elif ticker in run.get("source_verification_notes", {}):
            groups["session_review"].append(labels[ticker])
        elif section.get("status") == "review_required" and not status.get("blocking") and section.get("path") != "blocked":
            groups["issues"].append(labels[ticker])
        if any("기존 공식 자료" in label and check_status == "partial"
               for _, label, check_status, _ in status.get("checks", [])):
            groups["dated"].append(labels[ticker])
    descriptions = {
        "price": "정규장 종가 검증 자료가 부족해 행동 판단을 보류했습니다.",
        "filing": "중요 공시의 접수 정보만 확보해 사건 판단과 행동 제안이 잠정입니다.",
        "issues": "중요 쟁점이 남아 행동 제안이 잠정입니다.",
        "session_review": "최신 정규장 종가는 대조됐습니다. 모델의 세션 미확인 전제를 바로잡고 기존 행동 제안은 잠정으로 표시했습니다.",
        "dated": "일부 공식 자료를 이번 조회에서 완전히 재확인하지 못해 원래 공개일과 관측기간을 유지했습니다.",
    }
    for key, tickers in groups.items():
        if tickers:
            limits.append(f"- **{', '.join(map(one_line, tickers))}** · {descriptions[key]}")
    if limits:
        lines.extend(["", "## 판단 제한", "", *limits])
    shared_sources = {key: item for section in run["sections"] for key, item in section["sources"].items()}
    legend_position = len(lines)
    lines.extend(decision_macro_lines(run, shared_sources))
    for section in run["sections"]:
        ticker = section["ticker"]
        status = coverage.get(ticker, {})
        brief = run["decision_briefs"].get(ticker, {})
        lines.extend(["", f"## {one_line(labels[ticker])}", "",
                      f"- 분석 기간 {HORIZONS.get(section.get('horizon'), '미확인')}"])
        if section.get("as_of") and section["as_of"] != run.get("as_of"):
            lines.append(f"- 종목 기준 · {decision_time(section['as_of'])} (한국시간)")
        action, tentative = holding_action(section, status, brief)
        sources = decision_source_index(section["sources"], public_context.get(ticker, {}))
        if notice := run.get("source_verification_notes", {}).get(ticker):
            lines.append("- **수집 근거 확인** · " + notice)
        lines.extend(decision_brief_lines(brief, sources, "잠정 행동 제안" if tentative else "행동 제안",
                                          action or "판단 보류", blocked=not action, tentative=tentative))
        if action:
            lines.extend(restore_action_conditions(brief, section["final"]))
        lines.extend(decision_gap_lines(status, brief))
    if legend := decision_benchmarks(lines):
        lines.insert(legend_position, legend)
    return "\n".join(lines) + "\n"


def render_report(run, qualitative_findings=None, public_context=None):
    if "decision_briefs" in run:
        return render_decision_holdings(run, public_context or {})
    live = run.get("source_mode") == "us-live"
    plain = run.get("events_requested", False)
    text = reader_line if plain else one_line
    presentation = run.get("presentation")
    coverage = run.get("coverage", {})
    qualitative_findings = qualitative_findings or {}
    public_context = public_context or {}
    labels = {
        section["ticker"]: coverage.get("tickers", {}).get(section["ticker"], {}).get("label")
        or holding_label(section["ticker"], section["sources"].values())
        for section in run["sections"]
    }
    lines = [
        "# 보유 종목 연구 보고서",
        "",
    ]
    if presentation:
        section = run["sections"][0]
        action = ACTIONS.get(section["final"]["action"], "판단 보류")
        label = "잠정 행동 제안" if section["status"] != "complete" and action != "판단 보류" else "행동 제안"
        lines.extend([
            f"## {one_line(labels[section['ticker']])} 판단 요약",
            "",
            f"- {label} **{action}**",
            f"- 적용 범위 {one_line(presentation['applicability'])}",
            f"- 분석 기준 시각 `{section['as_of']}`",
            f"- 다음 확인 {one_line(presentation['checks'][0]['when'])}",
            "",
            "저장된 모델 결론과 근거를 보고서 편집용으로 재구성했습니다.",
            "",
        ])
    lines.extend([
        f"- 실행 ID `{run['run_id']}`",
        *([f"- 분석 기준 UTC `{run['as_of']}`"] if run.get("as_of") else []),
        f"- 생성 시각 `{run['generated_at']}`",
        *([f"- 공개 자료 검토 기준 UTC `{run['qualitative_as_of']}`"] if run.get("qualitative_as_of") else []),
        f"- 모델 `{run['model_requested']}`",
        *(["- 보유 종목 · " + ", ".join(map(one_line, labels.values()))] if coverage and labels else []),
        "- [근거·검토·호출 기록](evidence.json)",
        "",
        (
            "미국 공개 자료에 하나의 UTC 분석 기준 시각을 적용했습니다. "
            "공개 시각을 확인할 수 없는 자료는 수집 시각부터 사용 가능하다고 처리했습니다."
            if live else "종목별로 표시한 과거 분석 기준 시각까지의 고정 자료를 사용했습니다."
        ),
    ])
    if coverage.get("notices"):
        lines.extend(["", "## 판단 전에 확인할 사항", "", *(f"- {one_line(notice)}" for notice in coverage["notices"])])
    lines.extend(public_comparison_lines(run.get("public_comparison")))
    for section in run["sections"]:
        status = section["status"]
        sources = section["sources"]
        final = section["final"]
        ticker_coverage = coverage.get("tickers", {}).get(section["ticker"], {})
        if ticker_coverage.get("blocking"):
            status, final = "review_required", None
        elif status == "complete" and ticker_coverage.get("review_required"):
            status = "review_required"
        lines.extend(["", f"## {one_line(labels[section['ticker']])}", ""])
        if "as_of" in section:
            lines.extend([
                f"- 분석 기준 시각 `{section['as_of']}`",
                f"- 분석 기간 {HORIZONS.get(section['horizon'], one_line(section['horizon']))}",
            ])
        lines.append(f"- 검토 상태 **{STATUSES[status]}**")
        if section.get("error"):
            lines.extend(["", f"실행 오류 — {text(section['error'])}"])
        if final:
            action = ACTIONS.get(final["action"])
            if action is None:
                lines.append("- 행동 제안 **판단 보류**")
            elif status == "complete":
                lines.append(f"- 행동 제안 **{action}**")
            else:
                lines.append(f"- 잠정 행동 제안 **{action}**")
            lines.extend(["", text(final["outlook"]), "", "### 판단 근거", ""])
            lines.extend(f"- {text(reason)}" for reason in final["reasoning"])
            counter = final["counterargument"]
            lines.extend(["", "### 핵심 반론", ""])
            alternative = ACTIONS.get(counter["action"], "근거가 충분한 대안 없음")
            refs = "" if plain else source_links(counter["evidence_ids"], sources)
            statement = f"- 대안 행동 **{alternative}** — {text(counter['rationale'])}"
            lines.append(f"{statement} · {refs}" if refs else statement)
            if presentation:
                weighing = presentation["weighing"]
                refs = source_links(weighing["evidence_ids"], sources)
                lines.append(f"- 최종 판단에서 더 무게를 둔 근거 — {one_line(weighing['text'])} · {refs}")
            for heading, field in (("하방 위험과 불확실성", "downside_and_uncertainty"),
                                   ("재검토 조건", "reassessment_conditions")):
                if final[field]:
                    lines.extend(["", f"### {heading}", ""])
                    if presentation and field == "reassessment_conditions":
                        lines.extend(render_reassessment(presentation["checks"], sources))
                    else:
                        lines.extend(f"- {text(point)}" for point in final[field])
        elif not section.get("error"):
            lines.append("- 행동 제안 **판단 보류**")

        ticker = section["ticker"]
        public_lines = render_public_coverage(
            public_context.get(ticker), qualitative_findings.get(ticker),
            blocked=section.get("path") == "blocked" or not final or final.get("action") == "review_required",
        )
        if public_lines:
            lines.extend(["", "### 최근 보도와 공개 투자자 의견", "", *public_lines])

        role_outputs = {
            entry["role"]: entry["output"] for entry in section.get("trace", [])
            if entry["stage"] == "analysis" and "output" in entry
        }
        if run.get("events_requested"):
            lines.extend(["", "### 주요 이슈와 시장 반응", ""])
            events = [item for key, item in sources.items() if key.startswith(EVENT_PREFIX)]
            finding = role_outputs.get("event_attention", {})
            event_ids = set(finding.get("evidence_ids", []))
            if events and event_ids.intersection(item["id"] for item in events) and finding.get("finding"):
                lines.append(text(finding["finding"]))
                lines.extend(f"- {text(reason)}" for reason in finding.get("reasoning", []))
            elif events:
                lines.append("확인한 공시와 보도 가운데 현재 판단에 반영할 내용을 제시하지 못했습니다.")
            else:
                lines.append(event_summary(ticker_coverage))
            reactions = dict.fromkeys(reaction_sentence(item["reaction"]) for item in events
                                      if item["id"] in event_ids and item["reaction"])
            lines.extend(f"- {reaction}" for reaction in reactions)

        for role, heading in (("macro_market", "거시경제·시장"), ("sector", "섹터")):
            finding = role_outputs.get(role, {})
            if "finding" not in finding:
                continue
            lines.extend(["", f"### {heading}", "", text(finding["finding"])])
            lines.extend(f"- {text(reason)}" for reason in finding["reasoning"])
            lines.extend(f"- 한계 — {text(limit)}" for limit in finding["limitations"])
            if not plain and (refs := source_links(finding["evidence_ids"], sources)):
                lines.extend(["", refs])

        if section.get("unresolved_issues"):
            lines.extend(["", "### 미해결 쟁점", ""])
            for issue in section["unresolved_issues"]:
                refs = "" if plain else source_links(issue["evidence_ids"], sources)
                statement = f"- {text(issue['claim'])}" if plain else f"- `{one_line(issue['id'])}` — {one_line(issue['claim'])}"
                lines.append(f"{statement} · {refs}" if refs else statement)
        if section.get("path") == "blocked":
            lines.extend(["", "자료 확인 단계에서 분석을 보류했습니다. 상세 원인은 근거 기록을 확인하세요."])
        if section.get("missing_fields"):
            lines.extend(["", "### 데이터 공백", ""])
            lines.extend(f"- {text(field)}" for field in section["missing_fields"])
        if run.get("week3"):
            lines.extend(render_week3(run, sources))
        if final and not plain:
            lines.extend(["", "### 인용 근거", ""])
            for evidence_id in dict.fromkeys(final["evidence_ids"]):
                if evidence_id.startswith(EVENT_PREFIX):
                    continue
                ref = source_links([evidence_id], sources)
                lines.append(f"- {ref} · 사용 가능 기준 `{sources[evidence_id]['available_at']}`")
        if live and sources and not plain:
            lines.extend(["", "### 수집 자료와 시점", ""])
            for evidence_id, source in sources.items():
                if evidence_id.startswith(EVENT_PREFIX):
                    continue
                observation = source.get("observation_date", "미확인")
                publication = source.get("published_at") or "미확인"
                collected = source["collected_at"]
                accepted = source.get("sec_acceptance_at")
                timing = (
                    f"관측·대상일 `{observation}` · 공개 시각 {publication} · "
                    f"사용 가능 기준 `{source['available_at']}` · 수집 `{collected}`"
                )
                if accepted:
                    timing += f" · SEC 접수 `{accepted}`"
                availability = source.get("availability_basis", "")
                if availability.endswith("_manually_verified_via_web"):
                    timing += " · 공식 웹 확인본"
                if source.get("nav_market_price_origin") == "dated_manual_official_capture":
                    timing += " · NAV·시장가격은 해당 날짜의 공식 웹 확인본"
                if source.get("supplier_delay") == "provider_reports_later_quote_session":
                    timing += " · 공급자가 표시한 더 최근 호가 세션의 확정 일봉은 미확인"
                elif "supplier_delay" in source:
                    timing += " · 공급 지연 여부 미확인"
                if session_end := source.get("observation_end_at"):
                    timing += f" · 정규장 종료 기준 `{session_end}`"
                links = [source_links([evidence_id], sources)]
                for key, label in (("submissions_url", "SEC 제출 내역"), ("facts_url", "SEC XBRL 수치"),
                                   ("download_url", "FRED CSV")):
                    if url := source.get(key):
                        links.append(f"[{label}](<{url}>)")
                lines.append(f"- {timing} · " + " · ".join(links))
    return "\n".join(lines) + "\n"


def write_report(directory, run):
    # Render first so a rendering failure cannot leave a report linked to mismatched evidence.
    report = render_report(run)
    with (directory / "evidence.json").open("x", encoding="utf-8") as stream:
        json.dump(run, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    with (directory / "report.md").open("x", encoding="utf-8") as stream:
        stream.write(report)
