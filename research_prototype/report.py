"""Load holdings and fixed evidence, run the workflow, and write a report pair."""

import csv
from datetime import datetime, timezone
from html import escape
import json
from pathlib import Path
import re
from urllib.parse import urlsplit
from uuid import uuid4

from framework_validation.optimized_runner import run_optimized, summarize_calls
from framework_validation.runner import load_fixture, parse_timestamp

from .events import EVENT_PREFIX, reaction_sentence

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
        if parse_timestamp(source["available_at"]) > cutoff:
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


def render_report(run):
    live = run.get("source_mode") == "us-live"
    plain = run.get("events_requested", False)
    text = reader_line if plain else one_line
    presentation = run.get("presentation")
    lines = [
        "# 보유 종목 연구 보고서",
        "",
    ]
    if presentation:
        section = run["sections"][0]
        action = ACTIONS.get(section["final"]["action"], "판단 보류")
        label = "잠정 행동 제안" if section["status"] != "complete" and action != "판단 보류" else "행동 제안"
        lines.extend([
            f"## {one_line(section['ticker'])} 판단 요약",
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
        f"- 모델 `{run['model_requested']}`",
        "- [근거·검토·호출 기록](evidence.json)",
        "",
        (
            "미국 공개 자료에 하나의 UTC 분석 기준 시각을 적용했습니다. "
            "공개 시각을 확인할 수 없는 자료는 수집 시각부터 사용 가능하다고 처리했습니다."
            if live else "종목별로 표시한 과거 분석 기준 시각까지의 고정 자료를 사용했습니다."
        ),
    ])
    for section in run["sections"]:
        status = section["status"]
        sources = section["sources"]
        final = section["final"]
        lines.extend(["", f"## {one_line(section['ticker'])}", ""])
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
                lines.append("이 분석에서 확인한 주요 공시 본문이나 보도가 없습니다.")
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
