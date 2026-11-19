"""Check when saved sources are sufficient for a specific research claim."""

from datetime import timedelta, timezone
from html import escape
import re
from urllib.parse import urlsplit

from framework_validation.runner import parse_timestamp

from .etf_data import SOXL_TERMS
from .events import EVENT_PREFIX, MATERIAL_ITEMS, RECENT_DAYS
from .instruments import holding_label, listing_label, profile, required_terms


STATUS = {
    "available": "확보", "partial": "일부 확보", "missing": "미확보", "stale": "이전 기간만 확보",
    "metadata_only": "접수 정보만 확보", "searched_zero": "검색 완료 · 해당 결과 0건",
    "failed": "수집 실패", "not_run": "조회 미확인", "not_applicable": "해당 없음",
}
GAP_STATUS = {"missing", "stale", "metadata_only", "failed"}
CAUTION_STATUS = {"partial", "not_run"}
APPENDIX_TABLE_HEADER = "| 중요도 | 자료 | 수집 상태 | 기준·참고 |"
SOXL_RISK = "기초지수의 하루 수익률 3배를 목표로 합니다. 공식 투자설명서에 따르면 기초지수가 하루에 크게 하락하면 하루 만에 투자금 전액을 잃을 수 있습니다."
EVENT_GAP = "중요 SEC 8-K의 접수 정보는 확인했으나 본문은 확보하지 못했습니다. 공시의 구체적인 내용과 영향을 이 자료만으로 판단할 수 없습니다."
NEWS_FAILURE = "뉴스 조회 또는 원문 수집에 실패했습니다. 관련 뉴스 유무를 확인할 수 없습니다."


def quarter_coverage(ticker, evidence):
    items = {item["id"]: item for item in evidence}
    stem = f"sec-{ticker.lower()}"
    quarter = items.get(f"{stem}-quarter", {})
    annual = items.get(f"{stem}-annual", {})
    quarter_end = quarter.get("observation_date")
    annual_end = annual.get("observation_date")
    expected = max(filter(None, (quarter_end, annual_end)), default=None)
    if quarter.get("newer_results_filing"):
        status = "stale"
        expected = None  # Filing date is not the fiscal period end.
    elif annual_end and (not quarter_end or annual_end > quarter_end):
        status = "stale" if quarter_end else "missing"
    elif {"revenue_usd", "operating_income_usd"} <= quarter.get("metrics", {}).keys():
        status = "available"
    else:
        status = "partial" if quarter else "missing"
    return {"status": status, "observed_period_end": quarter_end, "expected_period_end": expected,
            "basis": quarter.get("quarter_basis", quarter.get("form")),
            "evidence_ids": [item["id"] for item in (quarter, annual) if item],
            "newer_results_filing": quarter.get("newer_results_filing")}


def event_coverage(ticker, evidence, attempts, cutoff):
    cutoff_time = parse_timestamp(cutoff)
    events = [item for item in evidence if item["id"].startswith(EVENT_PREFIX)]
    disclosed = {
        item.get("accession"): set(re.findall(r"\d\.\d{2}", item.get("sec_event_items", ""))) & MATERIAL_ITEMS.keys()
        for item in evidence if item.get("form") == "8-K" and item.get("sec_acceptance_at")
        and cutoff_time - timedelta(days=RECENT_DAYS) <= parse_timestamp(item["sec_acceptance_at"]) <= cutoff_time
        and set(re.findall(r"\d\.\d{2}", item.get("sec_event_items", ""))) & MATERIAL_ITEMS.keys()
    }
    attempted = [row for row in attempts if row.get("ticker") == ticker]
    for row in attempted:
        if row.get("kind") == "sec_8k" and row.get("item_codes"):
            disclosed.setdefault(row.get("accession"), set()).update(row["item_codes"])
    bodies = [item for item in events if item.get("source_kind") == "sec_8k"]
    unverified = disclosed.keys() - {item.get("accession") for item in bodies}
    filing_lookups = [row for row in attempted if row.get("kind") == "sec_8k_lookup"]
    if unverified:
        filing = "metadata_only"
    elif bodies:
        filing = "available"
    elif any(row.get("error") for row in filing_lookups):
        filing = "failed"
    elif any(row.get("status") == "complete_zero" for row in filing_lookups):
        filing = "searched_zero"
    else:
        filing = "not_run"
    news_bodies = [item for item in events if item.get("source_kind") == "independent_news"]
    headlines = [item for item in events if item.get("source_kind") == "independent_headline"]
    news_attempts = [row for row in attempted if row.get("kind") in {
        "headline_lookup", "independent_news", "independent_headline",
    }]
    failed = any(row.get("error") or row.get("status") in {"failed", "unavailable"} for row in news_attempts)
    if news_bodies:
        news = "partial" if failed else "available"
    elif headlines:
        news = "partial"
    elif failed:
        news = "failed"
    elif any(row.get("kind") == "headline_lookup" and row.get("status") == "complete_zero" for row in news_attempts):
        news = "searched_zero"
    else:
        news = "not_run"
    return {
        "filing": {"status": filing, "unverified_accessions": sorted(filter(None, unverified)),
                   "unverified_categories": sorted({MATERIAL_ITEMS[key] for accession in unverified
                                                    for key in disclosed[accession] if key in MATERIAL_ITEMS}),
                   "evidence_ids": [item["id"] for item in bodies]},
        "news": {"status": news, "lookup_failed": failed, "evidence_ids": [item["id"] for item in news_bodies + headlines]},
        "can_report_no_event": filing == news == "searched_zero",
    }


def event_notices(ticker, status):
    messages = []
    if status["filing"]["status"] == "metadata_only":
        messages.append(f"{ticker} · {EVENT_GAP}")
    if status["news"]["status"] == "failed":
        messages.append(f"{ticker} · {NEWS_FAILURE}")
    return messages


def withhold_unverified_medium(run):
    """Recheck the currency of medium inputs before sending a saved shortlist to a model."""
    if run is None:
        return
    shortlist = run["horizons"]["medium"]["shortlist"]
    evidence = list(run["evidence"].values())
    planned = [pick for pick in shortlist if quarter_coverage(pick["ticker"], evidence)["status"] == "available"]
    if len(planned) == len(shortlist):
        return
    withheld = [pick for pick in shortlist if pick not in planned]
    run["withheld_medium"] = withheld
    run["horizons"] = {**run["horizons"], "medium": {**run["horizons"]["medium"], "shortlist": planned}}
    selected = [pick for horizon in run["horizons"].values() for pick in horizon["shortlist"]]
    run["counts"] = {**run["counts"], "horizon_slots": len(selected),
                     "unique_shortlist": len({pick["ticker"] for pick in selected})}


def event_summary(status):
    if status:
        notices = event_notices("", status)
        if notices:
            return " ".join(message.removeprefix(" · ") for message in notices)
        if status["can_report_no_event"]:
            return "조회한 SEC 공시와 뉴스 범위에서 판단에 반영할 주요 사건은 발견되지 않았습니다."
    return "확보한 범위에서 판단에 반영할 이슈를 확인하지 못했습니다. 전체 검색 결과를 확인한 상태는 아닙니다."


def market_coverage(items):
    if not any(items):
        return "missing"
    if not all(items):
        return "partial"
    if all(item.get("observations") for item in items):
        sessions = [[(row["session_date"], row.get("session_completion_bound")) for row in item["observations"]]
                    for item in items]
        if any(sessions[0] != window for window in sessions[1:]):
            return "partial"
    return "available"


def checklist(record):
    cutoff = record["as_of"]
    diagnostic = record.get("event_collection", [])
    public_context = record.get("public_context", {})
    base_news_label = "독립 뉴스(기존 이슈 조회)" if public_context else "독립 뉴스"
    holdings = {"tickers": {}, "notices": []}
    discovery = {"tickers": {}, "notices": [], "invalid_medium": [], "shared": []}
    sections = {section["ticker"]: section for section in record["holdings"]["sections"]}
    for ticker in record["holdings"]["holdings"]:
        fixture = record.get("holdings_inputs", {}).get(ticker, {})
        evidence = [item for item in fixture.get("evidence", []) if parse_timestamp(item["available_at"]) <= parse_timestamp(cutoff)]
        label = holding_label(ticker, [*evidence, *sections.get(ticker, {}).get("sources", {}).values()])
        available = {item["id"] for item in evidence}
        items = {item["id"]: item for item in evidence}
        status = event_coverage(ticker, evidence, diagnostic, cutoff)
        instrument = profile(ticker)
        korean = bool(instrument and instrument.country == "KR")
        other_fund = bool(instrument and instrument.kind != "equity")
        market_id = "kr-kospi-price-window" if korean else "us-sp500-price-window"
        required_structure = (required_terms(ticker) if instrument else SOXL_TERMS if ticker == "SOXL"
                              else f"{ticker.lower()}-sec-periodic-results")
        stock = items.get(f"{ticker.lower()}-stock-price-window", {})
        market = market_coverage([stock, items.get(market_id), items.get(f"{ticker.lower()}-sector-price-window")])
        required = fixture.get("required_evidence_ids", [])
        blocking = sorted(set(required) - available)
        checks = [
            ("필수", "종목의 거래일 기준 가격·거래량", "available" if stock else "missing", stock.get("observation_date")),
            ("필수", "검증된 운용 구조" if ticker == "SOXL" or other_fund else "검증된 기업 실적·재무",
             "available" if required_structure in available else "missing", None),
            ("비교 시 필수", "같은 거래일의 시장·노출 비교 시세", market, None),
            ("이슈 판단 시 필수", "국내 추가 공시 본문" if korean else "중요 SEC 공시 본문", status["filing"]["status"], None),
            ("보강", base_news_label, status["news"]["status"], None),
            ("보강", "거시 지표", "partial" if korean and any("-kr-macro-" in key for key in available) else
             "available" if sum(key.startswith("us-fred-") for key in available) >= 4 else
             "partial" if any(key.startswith("us-fred-") for key in available) else "missing", None),
        ]
        if reverification := fixture.get("source_reverification"):
            incomplete = [row for row in reverification if row["status"] != "verified_core_text"]
            checks.append(("보강", "기존 공식 자료의 핵심 내용 재확인", "partial" if incomplete else "available",
                           f"재확인 한계 {len(incomplete)}건 / {len(reverification)}건"))
            if incomplete:
                holdings["notices"].append(
                    f"{label} · 이전에 검토한 공식 자료 {len(incomplete)}건을 이번 조회에서 완전히 재확인하지 못했습니다. "
                    "각 자료의 원래 공개일·관측기간을 유지했으며 최신 자료로 대체한 것은 아닙니다."
                )
        if ticker == "SOXL":
            prospectus = items.get("soxl-sec-etf-prospectus")
            checks.append(("필수", "공식 투자설명서의 일일 전액 손실 위험", "available" if prospectus else "missing",
                           prospectus.get("observation_date") if prospectus else None))
            checks.append(("추적 판단 시 필수", "공식 지수·동시점 순자산가치 수익률", "missing", None))
        elif other_fund:
            checks.append(("추적 판단 시 필수", "공식 지수·동시점 순자산가치 수익률", "missing", None))
            if instrument.kind == "covered_call":
                checks.append(("성과 판단 시 필수", "분배금 재투자 총수익", "missing", None))
        else:
            checks.append(("적정 가격 판단 시 필수", "검증된 밸류에이션·실적 전망치", "missing", None))
        if ticker in public_context:
            checks.extend(public_source_checks(public_context[ticker]))
        holdings["tickers"][ticker] = {**status, "checks": checks, "blocking": blocking, "label": label}
        if blocking or not stock:
            holdings["notices"].append(f"{label} · 필수 자료가 부족해 종목 전체 행동 판단을 보류했습니다.")
        holdings["notices"].extend(event_notices(label, status))
        holdings["tickers"][ticker]["review_required"] = status["filing"]["status"] == "metadata_only"
        if holdings["tickers"][ticker]["review_required"] and not blocking:
            holdings["notices"].append(f"{label} · 사건 판단은 보류합니다. 확인한 다른 자료에 기반한 행동 제안은 잠정입니다.")
        if ticker == "SOXL":
            holdings["notices"].append(f"{label} · {SOXL_RISK}")
        elif other_fund and (multiple := items.get(required_structure, {}).get("metrics", {}).get("daily_objective_multiple")):
            holdings["notices"].append(
                f"{label} · 하루 수익률 목표는 {multiple:+g}배입니다. 여러 날 보유했을 때 누적 수익률은 시장 움직임의 순서와 변동성에 따라 달라집니다."
            )
    run = record["discovery"]
    if run is None:
        return {"holdings": holdings, "discovery": discovery}
    by_ticker = {}
    for horizon, selection in run["horizons"].items():
        for pick in selection["shortlist"]:
            by_ticker.setdefault(pick["ticker"], set()).add(horizon)
    evidence = list(run["evidence"].values())
    seats = {row["ticker"]: row for row in run["sector_seats"]}
    for ticker in dict.fromkeys([*by_ticker, *seats]):
        horizons = by_ticker.get(ticker, set())
        status = event_coverage(ticker, [item for item in evidence if item.get("ticker") == ticker], diagnostic, cutoff)
        relevant = quarter_coverage(ticker, evidence)
        metrics = seats.get(ticker, {}).get("metrics", {})
        if relevant["status"] == "available" and not {
            "quarter_revenue_yoy_pct", "quarter_operating_margin_pct",
        } <= metrics.keys():
            relevant["status"] = "partial"
        sources = run.get("qualitative_evidence", {}).get("missing_by_ticker", {}).get(ticker)
        if "medium" in horizons and relevant["status"] != "available":
            discovery["invalid_medium"].append(ticker)
        withheld = any(row["ticker"] == ticker for row in run.get("withheld_medium", []))
        if withheld or ("medium" in horizons and relevant["status"] != "available") or relevant["status"] == "stale":
            if relevant["newer_results_filing"]:
                discovery["notices"].append(f"{ticker} · 새 실적 공시의 접수는 확인했으나 최신 분기 수치를 검증하지 못해 중기 판단에서 제외했습니다.")
            else:
                period = relevant["expected_period_end"] or "미확인"
                discovery["notices"].append(f"{ticker} · 최신 분기({period}) 실적을 확인하지 못해 중기 판단에서 제외했습니다.")
        discovery["notices"].extend(event_notices(ticker, status))
        checks = [
            ("필수", "종목·섹터·시장 시세, 유동성 및 순위 입력", "available", None),
            ("중기 판단 시 필수", "최신 분기 실적과 비교치", relevant["status"],
             ("필요 " + relevant["expected_period_end"] if relevant["expected_period_end"] else "새 실적 접수")
             if relevant["status"] == "stale" else relevant["observed_period_end"]),
            ("장기 판단 시 필수", "비교 연도 현금흐름과 부채", "available" if {
                "minimum_annual_cash_after_capex_margin_pct", "annual_long_term_debt_to_revenue_pct",
            } <= metrics.keys() else "missing", None),
            ("이슈 판단 시 필수", "중요 SEC 공시 본문", status["filing"]["status"], None),
            ("보강", base_news_label, status["news"]["status"], None),
            ("보강", "기업 전략·업황·위험 자료", "not_run" if sources is None else "partial" if sources else "available", None),
        ]
        if ticker in public_context:
            checks.extend(public_source_checks(public_context[ticker]))
        discovery["tickers"][ticker] = {**status, "checks": checks, "quarter": relevant, "horizons": sorted(horizons),
                                         "label": listing_label(ticker, seats.get(ticker, {}).get("listing_security_name"))}
    for label, ids in (("섹터 ETF 구성 종목과 거래소 목록", ["holdings-xlk", "holdings-xli", "holdings-xlv",
                       "holdings-xlp", "listing-nasdaq-trader"]), ("거시 지표", [
                           "us-fred-dgs10", "us-fred-dff", "us-fred-cpiaucsl", "us-fred-unrate"])):
        found = sum(evidence_id in run["evidence"] for evidence_id in ids)
        discovery["shared"].append((label, "available" if found == len(ids) else "partial" if found else "missing"))
    return {"holdings": holdings, "discovery": discovery}


def appendix_lines(section):
    groups = list(section["tickers"].items())
    pages = [groups[index:index + 3] for index in range(0, len(groups), 3)] or [[]]
    lines = []
    for index, page in enumerate(pages, 1):
        lines.extend(["", "<!-- pagebreak -->", "", f"## 부록 · 수집 자료 점검표 ({index}/{len(pages)})", ""])
        if index == 1:
            lines.extend([
                "자료가 필요한 판단별로 확보 상태를 표시했습니다. 보강은 있으면 좋은 자료입니다.",
                "굵은 상태는 확인이 필요한 항목입니다.",
            ])
            if shared := section.get("shared"):
                lines.extend(["", "### 공통 자료", "", *appendix_table_rows(
                    ("공통", label, status, None) for label, status in shared
                )])
        if not page:
            lines.append("점검할 분석 대상이 없습니다.")
        for ticker, result in page:
            lines.extend(["", f"### {result.get('label', ticker)}", "", *appendix_table_rows(result["checks"])])
    return "\n".join(lines) + "\n"


def appendix_table_rows(checks):
    yield APPENDIX_TABLE_HEADER
    yield "| --- | --- | --- | --- |"
    for importance, label, status, reference in checks:
        value = STATUS[status]
        if status in GAP_STATUS or status in CAUTION_STATUS:
            value = f"**{value}**"
        yield f"| {importance} | {label} | {value} | {reference or '—'} |"


PROVIDER_NAMES = {
    "google_news": "구글 뉴스", "google_news_rss": "구글 뉴스", "bluesky": "Bluesky",
    "brave_news": "Brave 뉴스", "finnhub": "Finnhub", "naver_news": "네이버 뉴스",
    "naver_cafe": "네이버 카페", "reddit": "Reddit", "stocktwits": "Stocktwits",
    "yahoo_finance": "Yahoo Finance", "yahoo_finance_rss": "Yahoo Finance RSS", "bing_news_rss": "Bing 뉴스 RSS",
}
FALLBACK_RSS = {"yahoo_finance_rss", "bing_news_rss"}
SEARCH_STATUS = {
    "searched_zero": "조회 범위 내 적격 자료 없음",
    "unavailable": "조회 실패 또는 접근 제한",
    "authentication_required": "인증 정보 미설정",
    "unverified_time": "게시 시각을 확인하지 못해 제외",
}
VIEWS = {"favorable": "긍정적 관점", "unfavorable": "부정적 관점", "mixed": "엇갈린 관점",
         "neutral": "중립적 관점"}
EFFECTS = {
    "unchanged": "기존 판단 유지",
    "watch_condition_added": "추가 확인 조건",
    "review_required": "기존 판단 재검토 필요",
    "insufficient_evidence": "판단에 반영할 근거 부족",
}
FINDING_STATUS = {
    "no_evidence": "판단에 반영할 공개 자료를 확보하지 못했습니다.",
    "unavailable": "공개 자료의 해석을 완료하지 못했습니다.",
    "not_requested": "공개 자료의 판단 영향은 평가하지 않았습니다.",
}
COMMUNITY_BODY_BASES = {"bluesky_created_and_indexed_time", "stocktwits_created_at", "reddit_created_utc"}
KOREA_TIME = timezone(timedelta(hours=9))


def display_text(value):
    """Keep source text on one line and prevent user content from creating links."""
    return (escape(" ".join(str(value).split()), quote=False)
            .replace("[", "&#91;").replace("]", "&#93;").replace("`", "&#96;")
            .replace("*", "&#42;").replace("_", "&#95;"))


def public_url(item):
    url = item.get("source_url", "")
    if not isinstance(url, str) or any(char.isspace() or char in '<>\\' for char in url):
        return None
    try:
        parsed = urlsplit(url)
        if parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username and not parsed.password:
            return url
    except ValueError:
        pass
    return None


def qualifies_public_item(item):
    return bool(public_url(item) and (
        item.get("kind") == "news" or
        (item.get("kind") == "community" and item.get("link_scope") in {"permalink", "direct_item"})
    ))


def public_channel(item):
    if item.get("kind") == "community" and item.get("community_subtype") == "article_share":
        return "article_share"
    return item.get("kind")


def public_source_checks(public_record):
    for kind, label in (("news", "추가 기사"), ("community", "공개 투자자 의견")):
        items = [item for item in public_record.get("items", [])
                 if public_channel(item) == kind and qualifies_public_item(item)]
        shares = sum(public_channel(item) == "article_share" and qualifies_public_item(item)
                     for item in public_record.get("items", [])) if kind == "community" else 0
        statuses = {row.get("status") for row in public_record.get("sources", []) if row.get("kind") == kind}
        limited = bool(statuses & {"unavailable", "authentication_required", "unverified_time"})
        if items:
            bodies = 0
            for item in items:
                excerpt = (item.get("text") or "").strip()
                basis = item.get("availability_basis")
                if excerpt and (basis in COMMUNITY_BODY_BASES or (basis == "reviewed_public_permalink"
                        and excerpt != (item.get("title") or "").strip())):
                    bodies += 1
            reference = []
            if bodies:
                reference.append(f"본문 {bodies}건")
            if others := len(items) - bodies:
                reference.append(f"제목·발췌 {others}건")
            if shares:
                reference.append(f"별도 기사 공유 {shares}건")
            status = "available" if bodies == len(items) and not limited else "partial"
            if limited:
                reference.append("일부 조회 제한")
            yield "보강", label, status, " · ".join(reference)
        elif limited:
            note = "일부 조회 제한 · 전체 범위 미확인" if "searched_zero" in statuses else "조회 실패·제한 또는 게시일 미확인"
            if shares:
                note += f" · 별도 기사 공유 {shares}건"
            yield "보강", label, "failed", note
        elif shares:
            yield "보강", label, "searched_zero", f"작성자 의견 0건 · 기사 공유 {shares}건"
        elif statuses == {"searched_zero"}:
            yield "보강", label, "searched_zero", "조회 범위 내 적격 자료 없음"
        else:
            yield "보강", label, "not_run", "보고서에 연결할 자료 미확인"


def provider_name(item):
    provider = item.get("provider", "")
    return display_text(PROVIDER_NAMES.get(provider, provider) or "출처 미표시")


def publication_label(item):
    published = item.get("published_at")
    if not published:
        return "게시일 미확인"
    if item.get("publication_precision") == "day":
        return f"게시일 {display_text(published[:10])} · 게시일 단위/정확 시각 미확인"
    published_in_korea = parse_timestamp(published).astimezone(KOREA_TIME).date()
    return f"게시일 {published_in_korea} (한국시간)"


def item_card(item, url):
    label = display_text(item.get("title") or ("공개 게시물" if item["kind"] == "community" else "기사"))
    publisher = item.get("publisher")
    fields = [f"[{label}](<{url}>)", display_text(publisher) if publisher else provider_name(item),
              publication_label(item)]
    if author := item.get("author"):
        fields.append(f"작성자 {display_text(author)}")
    title = (item.get("title") or "").strip()
    excerpt = (item.get("text") or "").strip()
    has_excerpt = bool(excerpt and excerpt != title)
    basis = item.get("availability_basis")
    if item["kind"] == "news":
        if basis == "reviewed_public_permalink" and has_excerpt:
            fields.append("공개 원문 확인")
        else:
            fields.append("제목·발췌 확인" if has_excerpt else "제목만 확인")
        if item.get("link_scope") == "aggregator":
            fields.append("뉴스 검색 링크")
        if item.get("provider") in FALLBACK_RSS:
            fields.append(f"대체 RSS {provider_name(item)}")
    elif public_channel(item) == "article_share":
        fields.append("기사 공유 · 작성자 의견 집계에서 제외")
        if article_url := public_url({"source_url": item.get("external_article_url")}):
            fields.append(f"[공유한 기사](<{article_url}>)")
    else:
        body_label = "제목만 확인"
        if excerpt and basis in COMMUNITY_BODY_BASES:
            body_label = "공개 게시글 본문 확인"
        elif has_excerpt:
            body_label = "게시글 요약 확인" if basis == "reviewed_public_permalink" else "게시글 발췌 확인"
        fields.extend(["공개 개인 의견", body_label])
    if basis == "reviewed_public_permalink":
        fields.append("공개 링크 별도 확인")
    return " · ".join(fields)


def render_public_coverage(public_record, analysis, *, blocked=False):
    """Build common Markdown bullets for holdings and candidate reports."""
    public_record = public_record or {}
    analysis = analysis or {}
    items = {}
    for item in public_record.get("items", []):
        if item.get("id") and qualifies_public_item(item):
            items[item["id"]] = (item, item["source_url"])

    findings = []
    for finding in analysis.get("findings", []):
        refs = list(dict.fromkeys(finding.get("evidence_ids", [])))
        kind = finding.get("kind")
        if kind not in {"news", "publisher", "community"}:
            continue
        channel = "community" if kind == "community" else "news"
        if (refs and finding.get("summary") and all(
                ref in items and public_channel(items[ref][0]) == channel for ref in refs)):
            findings.append((finding, refs))

    lines = []
    for kind, label in (("news", "기사"), ("community", "공개 투자자 의견"), ("article_share", "커뮤니티 기사 공유")):
        rows = [row for row in public_record.get("sources", []) if row.get("kind") == kind]
        cards = {key: value for key, value in items.items() if public_channel(value[0]) == kind}
        if not rows and not cards:
            continue
        cited = [key for _, refs in findings for key in refs if key in cards]
        visible = list(dict.fromkeys([*cited, *cards]))[:3]
        for key in visible:
            lines.append(f"- {label} · {item_card(*cards[key])}")
        if len(cards) > 3:
            lines.append(f"- {label} · 보고서에 {len(cards)}건 중 3건 표시. 전체 목록은 근거 기록을 확인하세요.")
        for row in rows:
            status = row.get("status")
            if status == "available":
                if row.get("fallback_for") or row.get("provider") in FALLBACK_RSS:
                    lines.append(f"- {provider_name(row)} · 대체 RSS에서 적격 기사 {row.get('count', 0)}건 확보")
                continue
            if status_text := SEARCH_STATUS.get(status):
                if row.get("http_status") == 429 or row.get("error") == "HTTP 429":
                    status_text = "요청 제한(HTTP 429)"
                lines.append(f"- {provider_name(row)} · {status_text}")
        if cards and kind == "community":
            lines.append("- 공개 의견은 표본이며 작성자의 실제 보유 내역과 투자자 전체의 입장은 확인되지 않았습니다.")

    for finding, refs in findings:
        links = " · ".join(f"[근거 {index}](<{items[ref][1]}>)" for index, ref in enumerate(refs, 1))
        view = VIEWS.get(finding.get("view"), "해석")
        source = "공개 의견 해석" if finding["kind"] == "community" else "보도 해석"
        lines.append(f"- {source} · {view} · {display_text(finding['summary'])} · {links}")

    effect = analysis.get("judgment_effect")
    if findings and analysis.get("status") == "assessed" and effect in EFFECTS:
        reason = analysis.get("effect_reason") if not blocked or effect == "watch_condition_added" else None
        label = "판단 보류 유지 · 추가 확인 참고" if blocked else EFFECTS[effect]
        lines.append(f"- 분석에 미친 영향 · {label}" + (f" · {display_text(reason)}" if reason else ""))
    elif status_text := FINDING_STATUS.get(analysis.get("status")):
        lines.append(f"- {status_text}")
    elif analysis.get("status") == "assessed":
        lines.append("- 판단에 연결할 수 있는 공개 자료 인용을 확인하지 못했습니다.")
    return lines


def public_comparison_lines(comparison):
    if not comparison:
        return []
    before, after = comparison["before"], comparison["after"]

    def numeral(value):
        return f"{value:,}" if value is not None else "미집계"

    def change(key):
        return f"{numeral(before.get(key))} → {numeral(after.get(key))}"

    def rate(period, key):
        numerator, denominator = period.get(key), period.get("ticker_count", comparison["ticker_count"])
        if numerator is None or not denominator:
            return "미집계"
        return f"{numerator}/{denominator}개({numerator / denominator:.1%})".replace(".0%", "%")

    def rates(label, earlier, later):
        coverage = " · ".join(f"{item} {rate(earlier, key)} → {rate(later, key)}"
                              for item, key in (("기사", "news_tickers"), ("작성자 의견", "opinion_tickers")))
        return f"- {label} 확보율 · {coverage}"

    lines = ["", "## 공개 자료 수집 전후 비교", "",
             f"- 비교 범위 · 보유·후보 보고서에 공통으로 적용한 동일 종목 {comparison['ticker_count']}개",
             rates("전체", before, after),
             f"- 확보 기사 · {change('news_items')}건 · 기사 확보 종목 {change('news_tickers')}개",
             f"- 공개 커뮤니티 게시물 전체 · {change('community_items')}건 · 게시물 확보 종목 {change('community_tickers')}개",
             f"- 작성자 투자 의견 · {change('opinion_items')}건 · 기사 공유 · {change('share_items')}건",
             f"- 공개 자료 검토 모델 · 호출 {change('calls')}회 · 입력 {change('input_tokens')} 토큰 · 출력 {change('output_tokens')} 토큰"]
    if before.get("as_of") and after.get("as_of"):
        cutoffs = [parse_timestamp(period["as_of"]).astimezone(KOREA_TIME) for period in (before, after)]
        dates = " → ".join(stamp.strftime("%Y-%m-%d %H:%M") for stamp in cutoffs)
        lines.insert(4, f"- 공개 자료의 조회 기준 시각(한국시간) · {dates}")
        if cutoffs[0] != cutoffs[1]:
            lines.append("- 수집 시점이 달라 게시물과 공급자 응답도 달라질 수 있습니다. 변경 효과만 따로 측정한 수치는 아닙니다.")
    for key, label in (("holdings", "보유 종목"), ("shortlist", "발굴 후보")):
        earlier, later = before.get("groups", {}).get(key), after.get("groups", {}).get(key)
        if earlier is not None and later is not None:
            lines.append(rates(label, earlier, later))
    if before.get("opinion_items") is None and before.get("community_items") is not None:
        lines.append("- 기존 게시물에 의견·공유 구분이 없어 두 항목의 증감은 비교할 수 없습니다.")
    if "google_429_count" in before and "google_429_count" in after:
        recovered = after.get("fallback_recovered_tickers", [])
        recovered_label = f" · 대체 경로로 기사 확보 {len(recovered)}종목" if recovered else ""
        lines.append(f"- 구글 뉴스 요청 제한 · {change('google_429_count')}회{recovered_label}")
    if all(key in before and key in after for key in ("news_zero_lookups", "news_limited_lookups")):
        lines.append(f"- 뉴스 조회 · 적격 0건 {change('news_zero_lookups')}회 · 실패·제한 {change('news_limited_lookups')}회")
    if all(key in before and key in after for key in ("community_zero_lookups", "community_limited_lookups")):
        lines.append(f"- 커뮤니티 조회 · 적격 0건 {change('community_zero_lookups')}회 · 실패·제한 {change('community_limited_lookups')}회")
    if fallback := comparison.get("controlled_fallback"):
        tested = fallback["tested_tickers"]
        recovered = fallback["recovered_tickers"]
        providers = fallback["providers"]
        recovered_sources = ", ".join(
            f"{display_text(ticker)} ({provider_name({'provider': providers[ticker]})})"
            for ticker in recovered if ticker in providers
        )
        lines.extend([
            "- Google에만 429를 재현한 별도 검증 · 대체 피드는 실제 공개 응답",
            f"  - {len(tested)}종목 중 {len(recovered)}종목에서 기사 {fallback['article_count']}건 확보"
            + (f" · {recovered_sources}" if recovered_sources else "") + " · 관측 확보율·토큰 비교에서 제외",
        ])
    return lines
