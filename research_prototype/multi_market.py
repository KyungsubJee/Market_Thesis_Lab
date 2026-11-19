"""Collect live exchange-specific price evidence for nonstandard holdings."""

from datetime import datetime, time
import json
from math import isfinite
from pathlib import Path
import re
from statistics import mean
from urllib.parse import urlencode, urlsplit
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

from framework_validation.runner import parse_timestamp

from .discovery_data import fetch_macro, fetch_many
from .instruments import canonical_holding, profile, required_terms
from .us_data import as_utc, market_record, quote_url, record, utc_now


KRX_CLOSE_EXCEPTIONS = {
    "2026-11-19": (time(16, 30), "https://kind.krx.co.kr/external/2026/11/05/000141/20261105000216/99303.htm"),
}


def korean_quote_url(symbol):
    return "https://fchart.stock.naver.com/sise.nhn?" + urlencode({
        "symbol": symbol, "timeframe": "day", "count": 90, "requestType": 0,
    })


def naver_session_url(symbol):
    return f"https://polling.finance.naver.com/api/realtime/domestic/stock/{symbol}"


def checked_krx_close(payload, symbol, day, close):
    for row in payload.get("datas", []):
        if (row.get("itemCode") != symbol or row.get("marketSessionType") != "regularMarket"
                or row.get("marketStatus") != "CLOSE"):
            continue
        traded = parse_timestamp(row["localTradedAt"]).astimezone(ZoneInfo("Asia/Seoul"))
        if traded.date().isoformat() == day and float(row["closePriceRaw"]) == close:
            return True
    return False


def naver_bars(body, cutoff, symbol=None):
    try:
        tree = ElementTree.fromstring(re.sub(r"^\s*<\?xml[^>]*\?>", "", body, count=1))
    except ElementTree.ParseError as exc:
        raise ValueError("Unexpected Naver chart response") from exc
    chart = tree if tree.tag == "chartdata" else tree.find("./chartdata")
    if chart is None or (symbol and chart.get("symbol") != symbol):
        raise ValueError(f"Naver chart did not confirm the requested identifier {symbol}")
    seoul = ZoneInfo("Asia/Seoul")
    rows = {}
    for item in chart.findall("./item"):
        stamp, open_, high, low, close, volume = item.attrib["data"].split("|")
        day = datetime.strptime(stamp, "%Y%m%d").date()
        close_time, calendar_url = KRX_CLOSE_EXCEPTIONS.get(day.isoformat(), (time(15, 30), None))
        bound = datetime.combine(day, close_time, seoul)
        values = tuple(map(float, (open_, high, low, close, volume)))
        if cutoff < bound or values[3] <= 0 or any(not isfinite(value) or value < 0 for value in values):
            continue
        rows[day] = {"session_date": day.isoformat(), "session_completion_bound": as_utc(bound),
                     **dict(zip(("open", "high", "low", "close", "volume"), values))}
        if calendar_url:
            rows[day]["session_calendar_source_url"] = calendar_url
    return [rows[day] for day in sorted(rows)[-60:]]


def korean_quote(symbol, source_id, scope, downloaded, cutoff):
    body, collected = downloaded
    bars = naver_bars(body, min(cutoff, collected), symbol)
    if not bars:
        raise ValueError(f"No completed Korean daily bars for {symbol}")
    latest = bars[-1]
    metrics = {"session_count_including_cutoff": len(bars), "last_close": latest["close"],
               "cutoff_volume_shares": latest["volume"]}
    for sessions in (1, 5, 20, 59):
        if len(bars) > sessions:
            metrics[f"return_{sessions}_sessions_pct"] = round(
                (latest["close"] / bars[-sessions - 1]["close"] - 1) * 100, 4,
            )
    if len(bars) >= 21 and (average := mean(bar["volume"] for bar in bars[-21:-1])):
        metrics["cutoff_volume_vs_preceding_20_sessions_mean_ratio"] = round(latest["volume"] / average, 4)
    return record(source_id, korean_quote_url(symbol), collected, category=scope, market_symbol=symbol,
                  observation_date=latest["session_date"], observation_end_at=latest["session_completion_bound"],
                  market_timezone="Asia/Seoul", currency=None if symbol == "KOSPI" else "KRW", metrics=metrics,
                  observations=bars, availability_basis="naver_collection_time",
                  text=f"{symbol} {scope}. Naver daily chart prices through {latest['session_date']}; "
                       "KRW quotes or KOSPI index points. Returns exclude distributions and use the Seoul trading "
                       "calendar. Naver daily chart labels alone do not identify the trading session. "
                       "A Korean-listed foreign exposure proxy does not synchronously measure its US market.")


def multi_market_snapshots(tickers, client, started):
    markets = {ticker: profile(ticker) for ticker in tickers}
    keys = {(item.country, symbol) for ticker, item in markets.items()
            for symbol in (ticker, item.market_proxy, item.exposure_proxy)}
    downloads = fetch_many(sorted(keys), lambda key: client.text(korean_quote_url(key[1]))
                           if key[0] == "KR" else client.json(quote_url(key[1])))
    sessions = fetch_many((ticker for ticker, item in markets.items() if item.country == "KR"),
                          lambda ticker: client.json(naver_session_url(ticker)))
    macro = fetch_macro(client, started) if any(item.country == "US" for item in markets.values()) else {}
    cutoff = utc_now()
    snapshots = {}
    for ticker, item in markets.items():
        stock_id = f"{ticker.lower()}-stock-price-window"
        benchmark_id = "us-sp500-price-window" if item.country == "US" else "kr-kospi-price-window"
        market_rows = ((ticker, stock_id, "held instrument"),
                       (item.market_proxy, benchmark_id, "broad market"),
                       (item.exposure_proxy, f"{ticker.lower()}-sector-price-window", item.comparison))
        evidence = []
        missing = []
        unverified_quotes = []
        for symbol, evidence_id, scope in market_rows:
            downloaded, error = downloads[item.country, symbol]
            if downloaded:
                try:
                    collector = korean_quote if item.country == "KR" else market_record
                    quote = collector(symbol, evidence_id, scope, downloaded, cutoff)
                    if item.country == "KR" and symbol == ticker:
                        checked, check_error = sessions[ticker]
                        if not checked or not checked_krx_close(checked[0], symbol, quote["observation_date"],
                                                                quote["metrics"]["last_close"]):
                            quote.update(session_check_url=naver_session_url(ticker),
                                         session_check_error=check_error or "No matching closed regular session")
                            unverified_quotes.append(quote)
                            missing.append("한국 정규장 종가 미검증 · 공급자가 표시한 거래 세션과 일봉을 대조하지 못했습니다.")
                            continue
                        quote.update(session_check_url=naver_session_url(ticker),
                                     session_verification="matching_naver_closed_regular_session_latest_only")
                    evidence.append(quote)
                    continue
                except (KeyError, TypeError, ValueError, IndexError) as exc:
                    error = str(exc)
            missing.append(f"{symbol} 확정 일봉 수집 실패 · {error}")
        if item.country == "US":
            evidence.extend(source for records, _ in macro.values() for source in records or [])
            missing.extend(f"{series} 거시 자료 미확보 · {error}" for series, (records, error) in macro.items() if not records)
        if item.kind == "equity":
            missing.extend(["검증된 적정 가격 산정에 필요한 전망치", "DART 공시와 국내 거시 지표의 전수 자동 조회"])
        else:
            missing.extend(["동일 시점의 공식 기초지수·순자산가치 수익률", "포트폴리오 비중과 매매 기준"])
            if item.kind == "covered_call":
                missing.append("장기 성과 비교에 필요한 분배금 재투자 총수익과 검증된 최신 옵션 매도 비중")
        snapshots[ticker] = (None, {
            "case_id": f"{item.country.lower()}-live-{ticker.lower()}", "ticker": ticker,
            "as_of": as_utc(cutoff), "horizon": "medium" if item.kind in {"equity", "covered_call"} else "short",
            "question": f"Assess held {item.name} ({ticker}) under the {item.country} listing calendar. "
                        f"Focus on {item.macro_scope} {item.comparison} Compare only matching completed sessions. "
                        "Propose a concrete research action, strongest alternative and reassessment conditions.",
            "required_evidence_ids": [stock_id, required_terms(ticker)], "evidence": evidence, "missing_fields": missing,
            "unverified_market_data": unverified_quotes,
        })
    return snapshots


def load_supplement(path):
    if path is None:
        return {}
    bundle = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if type(bundle.get("schema_version")) is not int or bundle["schema_version"] != 1 or not isinstance(bundle.get("evidence"), dict):
        raise ValueError("Evidence supplement requires schema_version 1 and evidence by ticker")
    collected = utc_now()
    result = {}
    for label, rows in bundle["evidence"].items():
        ticker = canonical_holding(label)
        records = result.setdefault(ticker, [])
        for row in rows:
            if any(not isinstance(row.get(key), str) or not row[key].strip()
                   for key in ("id", "source_url", "published_at", "text")):
                raise ValueError(f"Supplement {ticker} requires id, source_url, published_at and text")
            url = urlsplit(row["source_url"])
            if url.scheme != "https" or not url.hostname or url.username or any(char.isspace() or char in "<>" for char in row["source_url"]):
                raise ValueError(f"Supplement {ticker} URL must be credential-free HTTPS")
            if len(row["text"]) > 5000 or not isinstance(row.get("metrics", {}), dict):
                raise ValueError(f"Supplement {ticker} has invalid text or metrics")
            published = as_utc(parse_timestamp(row["published_at"]))
            item = record(row["id"], row["source_url"], collected, text=row["text"], metrics=row.get("metrics", {}),
                          published_at=published, available_at=published, category="supplemental_official_source",
                          availability_basis=row.get("availability_basis", "provided_official_source_date"),
                          observation_date=row.get("observation_date"))
            item.update({key: value for key, value in row.items() if key.startswith("refresh_")})
            if row.get("refresh_status"):
                item["collection_method"] = "dated_previously_reviewed_source_with_reverification_audit"
                item["supplement_loaded_at"] = as_utc(collected)
            if item["id"] in {entry["id"] for entry in records}:
                raise ValueError(f"Duplicate supplementary source {item['id']}")
            records.append(item)
    return result


def apply_supplement(snapshots, supplements):
    for ticker, (_, fixture) in snapshots.items():
        cutoff = parse_timestamp(fixture["as_of"])
        seen = {item["id"] for item in fixture["evidence"]}
        for item in supplements.get(ticker, []):
            if parse_timestamp(item["available_at"]) > cutoff:
                fixture["missing_fields"].append(f"{item['id']} · 기준 시각 이후 자료 제외")
                continue
            if item["id"] in seen:
                raise ValueError(f"Supplement evidence ID collides with captured data: {item['id']}")
            fixture["evidence"].append(item)
            seen.add(item["id"])
            if status := item.get("refresh_status"):
                fixture.setdefault("source_reverification", []).append({
                    "id": item["id"], "status": status, "published_at": item.get("published_at"),
                    "observation_date": item.get("observation_date"),
                    **{key: value for key, value in item.items() if key.startswith("refresh_")},
                })
                if status != "verified_core_text":
                    fixture["missing_fields"].append(
                        f"공식 자료 재확인 한계 · {item['id']} · 원래 공개일 {item.get('published_at')}의 검토 자료이며 "
                        "이번 조회에서 핵심 내용 전체를 재검증하지 못했습니다. 최신 운용·실적 자료로 해석하지 마세요."
                    )
            if item["id"] == f"{ticker.lower()}-stock-price-window":
                fixture["missing_fields"] = [gap for gap in fixture["missing_fields"]
                                             if not gap.startswith("한국 정규장 종가 미검증")]
