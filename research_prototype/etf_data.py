"""Verified official references and live holdings for the scoped SOXL route."""

import csv
from datetime import datetime, timezone
from html import unescape
from io import StringIO
import json
from pathlib import Path
import re
from zoneinfo import ZoneInfo


SOXL_HOLDINGS = "https://www.direxion.com/holdings/SOXL.csv"
SOXL_DOCUMENTS = Path(__file__).with_name("data") / "soxl_references.json"
SOXL_TERMS = "soxl-etf-issuer-terms"
SOXL_COMPARISON = "soxl-etf-comparison"


def soxl_issuer_terms(downloaded, reference):
    body, collected_at = downloaded
    body = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ", body, flags=re.I | re.S)
    text = " ".join(unescape(re.sub(r"<[^>]+>", " ", body)).split())
    product = re.search(r"SOXL\s+Direxion Daily Semiconductor Bull 3X ETF", text, re.I)
    if not product or "NYSE Semiconductor Index" not in text:
        raise ValueError("Direxion product terms could not be identified")
    block = text[product.end():product.end() + 1500].split("SOXS Direxion", 1)[0]
    if not re.search(r"Daily Target\s*\+?300%", block, re.I):
        raise ValueError("Direxion daily leverage objective could not be confirmed")
    price_date = re.search(r"NAV and Market Price information as of\s+(\d{1,2}/\d{1,2}/\d{4})", text, re.I)
    pricing = text[price_date.end():price_date.end() + 1000].split("SOXS Direxion", 1)[0] if price_date else ""
    nav = (
        re.search(r"Daily NAV\s*\$\s*([\d.]+)", block, re.I)
        or re.search(r"Net Asset Value\s*\(NAV\)\s*\$\s*([\d.]+)", pricing, re.I)
    )
    market_price = (
        re.search(r"Daily Market Price\s*\$\s*([\d.]+)", block, re.I)
        or re.search(r"Market Price Closing\s*\$\s*([\d.]+)", pricing, re.I)
    )
    expenses = re.search(r"Expense Ratio\s*\(\s*gross\s*/\s*net\s*%\s*\)\s*([\d.]+)\s*/\s*([\d.]+)", block, re.I)
    metrics = {"daily_objective_multiple": 3}
    if expenses:
        metrics.update(gross_expense_ratio_pct=float(expenses[1]), net_expense_ratio_pct=float(expenses[2]))
    observed = datetime.strptime(price_date[1], "%m/%d/%Y").date().isoformat() if price_date else None
    fallback_price = False
    if observed and nav and market_price:
        metrics.update(issuer_reported_nav_usd=float(nav[1]), issuer_reported_market_price_usd=float(market_price[1]))
    elif not observed or observed == reference["observation_date"]:
        metrics.update({
            key: reference["metrics"][key]
            for key in ("issuer_reported_nav_usd", "issuer_reported_market_price_usd")
        })
        observed = reference["observation_date"]
        fallback_price = True
    collected = collected_at.astimezone(timezone.utc).isoformat()
    return {
        **reference, "available_at": collected, "collected_at": collected,
        "availability_basis": "collection_time_issuer_page_publication_unverified",
        "observation_date": observed,
        "nav_market_price_origin": "dated_manual_official_capture" if fallback_price else "live_product_page",
        "metrics": metrics,
        "text": (
            "Direxion live product page confirms SOXL seeks +300% of the NYSE Semiconductor Index before fees "
            "and expenses for one trading day. Daily reset makes longer holding returns path-dependent. "
            "Swap financing and transaction costs may be outside the stated operating expense limit. "
            "Use only the extracted fee and price/NAV metrics provided. Match issuer NAV and market prices "
            "on the same dated observation. Index constituents are not the fund's holdings."
            + (" The dated NAV and issuer market price came from the separate manual official page capture."
               if fallback_price else "")
        ),
    }


def soxl_holdings(downloaded, cutoff):
    body, collected_at = downloaded
    lines = body.splitlines()
    header = next(
        (index for index, row in enumerate(csv.reader(lines)) if row and row[0].lstrip("\ufeff") == "TradeDate"),
        None,
    )
    if header is None:
        raise ValueError("Direxion holdings CSV header was not found")
    rows = list(csv.DictReader(StringIO("\n".join(lines[header:]))))
    cutoff_date = min(collected_at, cutoff).astimezone(ZoneInfo("America/New_York")).date()
    positions = []
    for row in rows:
        if row.get("AccountTicker") != "SOXL" or not row.get("TradeDate"):
            continue
        trade_date = datetime.strptime(row["TradeDate"].split()[0], "%m/%d/%Y").date()
        if trade_date <= cutoff_date:
            positions.append({
                "date": trade_date.isoformat(), "ticker": row.get("StockTicker"),
                "description": row.get("SecurityDescription"), "shares_raw": row.get("Shares"),
                "price_raw": row.get("Price"), "market_value_raw": row.get("MarketValue"),
                "holdings_percent_raw": row.get("HoldingsPercent"),
            })
    if not positions:
        raise ValueError("No dated SOXL holdings were available")
    latest = max(position["date"] for position in positions)
    positions = [position for position in positions if position["date"] == latest]
    example_positions = [
        f"{position['description']} ({position['ticker'] or 'unlabeled'})"
        for position in positions[:5] if position["description"]
    ]
    collected = collected_at.astimezone(timezone.utc).isoformat()
    return {
        "id": "soxl-etf-holdings", "source_url": SOXL_HOLDINGS,
        "available_at": collected, "collected_at": collected, "published_at": None,
        "availability_basis": "collection_time_holdings_publication_unverified",
        "category": "etf_holdings", "observation_date": latest,
        "positions": positions, "metrics": {"reported_position_rows": len(positions)},
        "text": (
            f"Direxion SOXL holdings CSV TradeDate {latest}, with {len(positions)} rows. "
            f"First listed positions, not ranked: {', '.join(example_positions)}. "
            "TradeDate is the issuer's date field, not verified publication or end-of-day timing. "
            "Portfolio holdings and derivatives differ from index weights; rows alone do not establish effective daily exposure."
        ),
    }


def soxl_sources(client, cutoff):
    evidence = json.loads(SOXL_DOCUMENTS.read_text(encoding="utf-8"))
    gaps = [
        "공식 지수 수익률과 SOXL 동시점 NAV 수익률이 없어 일일 목표 추적 오차를 검증하지 못했습니다.",
        "SEC 497K는 날짜가 명시된 공식 웹 확인본입니다. 최신 보충 공시는 별도로 확인하지 않았습니다.",
        "공식 NAV는 표시된 발행사 관측일 기준입니다. 다른 거래일의 시세와 혼용하지 않습니다.",
    ]
    try:
        evidence[0] = soxl_issuer_terms(client.text(evidence[0]["source_url"]), evidence[0])
        if "net_expense_ratio_pct" not in evidence[0]["metrics"]:
            gaps.append("발행사 페이지에서 최신 총·순비용률을 구조화하지 못했습니다. SEC 보고서 비용률은 해당 날짜 기준입니다.")
        if evidence[0]["nav_market_price_origin"] == "dated_manual_official_capture":
            gaps.append("발행사 NAV·시장가격은 자동 추출 실패로 해당 관측일의 공식 웹 확인본을 사용했습니다.")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        gaps.append(f"Direxion 상품 페이지 자동 추출 실패 — 날짜가 표시된 공식 웹 확인본 사용 ({exc})")
    try:
        evidence.append(soxl_holdings(client.text(SOXL_HOLDINGS), cutoff))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        gaps.append(f"Direxion 보유내역 CSV 수집 실패 — {exc}")
    return evidence, gaps


def soxl_comparison(sources):
    by_id = {item["id"]: item for item in sources}
    ids = ("soxl-stock-price-window", "us-sp500-price-window", "soxl-sector-price-window")
    if not all(evidence_id in by_id for evidence_id in ids):
        return None
    market = [by_id[evidence_id] for evidence_id in ids]
    dates = [[row["session_date"] for row in item["observations"]] for item in market]
    aligned = {}
    for window in (1, 5, 20, 59):
        count = window + 1
        aligned[f"aligned_{window}_sessions"] = (
            all(len(series) >= count for series in dates)
            and len({tuple(series[-count:]) for series in dates}) == 1
        )
    available_at = max(item["available_at"] for item in market)
    return {
        "id": SOXL_COMPARISON, "source_url": market[0]["source_url"],
        "available_at": available_at, "collected_at": available_at, "published_at": None,
        "availability_basis": "computed_from_captured_market_sources", "category": "etf_comparison",
        "observation_date": market[0]["observation_date"], "metrics": aligned,
        "source_evidence_ids": list(ids),
        "text": (
            "Python compared the full N+1 daily session-date arrays of SOXL, SOXX and SPY for each return window. "
            "Do not draw comparisons where alignment is false. SOXX is an unlevered ETF price proxy, "
            "so neither 3× its cumulative return nor a difference from its return measures official tracking."
        ),
    }
