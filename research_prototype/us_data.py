"""Build a current US-equity evidence snapshot from public data endpoints."""

import csv
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta, timezone
from io import StringIO
import json
from math import isfinite
import re
from statistics import mean
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from framework_validation.runner import parse_timestamp

from .etf_data import SOXL_TERMS, soxl_comparison, soxl_sources


MARKET = "SPY"
SECTOR_ETFS = {"META": "XLC", "KO": "XLP", "NVDA": "SOXX", "SOXL": "SOXX"}
SEC_TICKERS = "https://www.sec.gov/files/company_tickers.json"
SEC_AGENT = "Tauric Research Base local research prototype"
# Public directory may reject a request even when EDGAR data endpoints respond.
# Confirm these fallback mappings against the returned issuer's tickers and CIK.
SEC_SAMPLE_CIKS = {"META": "0001326801", "KO": "0000021344", "NVDA": "0001045810"}
FRED_SERIES = {
    "DGS10": ("미국 국채 10년 만기 금리", "percent"),
    "DFF": ("미국 실효 연방기금금리", "percent"),
    "CPIAUCSL": ("미국 CPI (계절조정)", "index_1982_84_100"),
    "UNRATE": ("미국 실업률 (계절조정)", "percent"),
}
PERIODIC_FORMS = {"10-Q", "10-K"}
EVENT_FORMS = {"8-K", "8-K/A", "10-Q/A", "10-K/A"}
FACT_TAGS = {
    "revenue_usd": (
        "RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
    ),
    "operating_income_usd": ("OperatingIncomeLoss",),
    "net_income_usd": ("NetIncomeLoss", "ProfitLoss"),
    "operating_cash_flow_usd": ("NetCashProvidedByUsedInOperatingActivities",),
    "capital_expenditures_usd": ("PaymentsToAcquirePropertyPlantAndEquipment",),
}


def utc_now():
    return datetime.now(timezone.utc)


def as_utc(value):
    return value.astimezone(timezone.utc).isoformat()


class PublicClient:
    def __init__(self, sec_agent=SEC_AGENT, timeout=15):
        self.sec_agent = sec_agent
        self.timeout = timeout

    def text(self, url, *, sec=False):
        headers = {}
        if sec:
            headers["User-Agent"] = self.sec_agent
            headers["Accept"] = "*/*"
        elif "finance.yahoo.com/" in url:
            headers["User-Agent"] = "Mozilla/5.0"
        elif "direxion.com/" in url:
            headers["User-Agent"] = "Tauric Research Base public fund data probe"
        elif urlsplit(url).hostname in {
            "ca.marketscreener.com", "investors.micron.com", "www.investor.jnj.com", "www.prnewswire.com",
        }:
            headers.update({"User-Agent": "Mozilla/5.0", "Accept": "*/*"})
        elif urlsplit(url).hostname in {
            "investor.nvidia.com", "investor.costco.com", "news.lockheedmartin.com", "investor.jnj.com",
            "investors.broadcom.com", "www.jnj.com",
        }:
            headers.update({"User-Agent": "Tauric Research Base public research", "Accept": "*/*"})
        with urlopen(Request(url, headers=headers), timeout=self.timeout) as response:
            body = response.read().decode("utf-8-sig")
        return body, utc_now()

    def json(self, url, *, sec=False):
        body, collected_at = self.text(url, sec=sec)
        return json.loads(body), collected_at


def record(evidence_id, url, collected_at, **fields):
    """An archive response proves availability by collection, without proving its first release."""
    collected = as_utc(collected_at)
    return {
        "id": evidence_id, "source_url": url, "available_at": collected,
        "collected_at": collected, "published_at": None, **fields,
    }


def quote_url(symbol):
    return (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        + quote(symbol, safe="")
        + "?"
        + urlencode({"range": "6mo", "interval": "1d", "includePrePost": "false"})
    )


def valid_number(value):
    return isinstance(value, (float, int)) and not isinstance(value, bool) and isfinite(value)


def daily_bars(payload, cutoff):
    """Discard partial regular sessions. Historical early closes use a conservative 16:00 ET bound."""
    eastern = ZoneInfo("America/New_York")
    data = payload["chart"]["result"][0]
    meta = data["meta"]
    if meta.get("currency") != "USD" or meta.get("exchangeTimezoneName") != eastern.key:
        raise ValueError("Unexpected quote currency or exchange time zone")
    regular_end = meta.get("currentTradingPeriod", {}).get("regular", {}).get("end")
    vendor_close = datetime.fromtimestamp(regular_end, eastern) if regular_end else None
    quote_rows = data["indicators"]["quote"][0]
    bars = {}
    for index, stamp in enumerate(data["timestamp"]):
        bar_stamp = datetime.fromtimestamp(stamp, eastern)
        session = bar_stamp.date()
        if session.weekday() >= 5:
            continue
        regular_close = datetime.combine(session, time(16), eastern)
        if vendor_close and vendor_close.date() == session:
            regular_close = vendor_close
        if regular_close > cutoff:
            continue
        values = {key: quote_rows[key][index] for key in ("open", "high", "low", "close", "volume")}
        if not all(valid_number(value) and value >= 0 for value in values.values()) or not values["close"]:
            continue
        bars[session] = {
            "session_date": session.isoformat(), "bar_timestamp": as_utc(bar_stamp),
            "session_completion_bound": as_utc(regular_close), **values,
        }
    return [bars[key] for key in sorted(bars)][-60:]


def market_record(symbol, evidence_id, scope, downloaded, cutoff):
    payload, collected_at = downloaded
    bars = daily_bars(payload, min(cutoff, collected_at))
    if not bars:
        raise ValueError("No complete usable regular-session daily bar")
    meta = payload["chart"]["result"][0]["meta"]
    latest = bars[-1]
    metrics = {
        "session_count_including_cutoff": len(bars),
        "last_close": latest["close"],
        "cutoff_volume_shares": latest["volume"],
    }
    for sessions in (1, 5, 20, 59):
        if len(bars) > sessions:
            metrics[f"return_{sessions}_sessions_pct"] = round(
                (latest["close"] / bars[-sessions - 1]["close"] - 1) * 100, 4,
            )
    if len(bars) >= 21:
        volume_average = mean(bar["volume"] for bar in bars[-21:-1])
        if volume_average:
            metrics["cutoff_volume_vs_preceding_20_sessions_mean_ratio"] = round(
                latest["volume"] / volume_average, 4,
            )
    vendor_time = meta.get("regularMarketTime")
    vendor_date = (
        datetime.fromtimestamp(vendor_time, ZoneInfo("America/New_York")).date().isoformat()
        if vendor_time else None
    )
    delay = "unknown"
    if vendor_date and vendor_date > latest["session_date"]:
        delay = "provider_reports_later_quote_session"
    return record(
        evidence_id, quote_url(symbol), collected_at,
        category=scope, observation_date=latest["session_date"],
        observed_at=latest["bar_timestamp"],
        observation_end_at=latest["session_completion_bound"],
        availability_basis="collection_time_first_publication_unverified",
        supplier_delay=delay, provider_latest_regular_quote_session=vendor_date,
        market_symbol=symbol, metrics=metrics, observations=bars,
        text=(
            f"{symbol} {scope}, Yahoo US regular-session USD daily bars through {latest['session_date']}. "
            "Price returns exclude cash distributions; do not present them as total returns. "
            "Provider archive may contain split adjustments or corrections. Publication time and delivery lag "
            "are not established; compare the observation date with the analysis cutoff."
        ),
    )


def fred_url(series, cutoff):
    start = cutoff.date() - timedelta(days=430)
    return "https://fred.stlouisfed.org/graph/fredgraph.csv?" + urlencode({
        "id": series, "cosd": start.isoformat(),
    })


def fred_records(downloaded, cutoff, *, url, series_ids):
    body, collected_at = downloaded
    rows = list(csv.DictReader(StringIO(body)))
    cutoff_date = min(cutoff, collected_at).astimezone(ZoneInfo("America/New_York")).date()
    records = []
    for series in series_ids:
        observations = {}
        for row in rows:
            raw_date = row.get("observation_date") or row.get("DATE")
            raw_value = row.get(series)
            if not raw_date or not raw_value or raw_value == ".":
                continue
            observed = date.fromisoformat(raw_date)
            value = float(raw_value)
            if observed <= cutoff_date and isfinite(value):
                observations[observed] = value
        if not observations:
            continue
        observed = max(observations)
        metrics = {"value": observations[observed]}
        if series == "CPIAUCSL":
            previous = observed.replace(year=observed.year - 1)
            if observations.get(previous):
                metrics["same_month_prior_year_change_pct"] = round(
                    (observations[observed] / observations[previous] - 1) * 100, 4,
                )
        label, unit = FRED_SERIES[series]
        records.append(record(
            f"us-fred-{series.lower()}", f"https://fred.stlouisfed.org/series/{series}", collected_at,
            category="macro_indicator", observation_date=observed.isoformat(),
            availability_basis="collection_time_release_and_vintage_unverified",
            supplier_delay="unknown", download_url=url, unit=unit, metrics=metrics,
            text=(
                f"FRED {series} {label}; observation date {observed.isoformat()}, unit {unit}. "
                "FRED current series may have been revised; release time, historical vintage and delay are unknown."
            ),
        ))
    return records


def sec_filings(submission, cutoff):
    recent = submission["filings"]["recent"]
    filings = []
    for index, form in enumerate(recent["form"]):
        if form not in PERIODIC_FORMS | EVENT_FORMS:
            continue
        accepted = recent["acceptanceDateTime"][index]
        if not accepted:
            continue
        stamp = parse_timestamp(accepted)
        if stamp > cutoff:
            continue
        filings.append({
            "form": form, "accepted_at": as_utc(stamp),
            "accession": recent["accessionNumber"][index],
            "filing_date": recent["filingDate"][index],
            "report_date": recent["reportDate"][index],
            "items": recent.get("items", [""] * len(recent["form"]))[index],
            "primary_document": recent.get("primaryDocument", [""] * len(recent["form"]))[index],
        })
    return sorted(filings, key=lambda row: row["accepted_at"], reverse=True)


def filing_metrics(facts, filing):
    gaap = facts.get("facts", {}).get("us-gaap", {})
    annual = filing["form"] == "10-K"
    metrics = {}
    periods = {}
    for key, tags in FACT_TAGS.items():
        # Quarterly income fields and year-to-date cash flows are separate periods.
        cash_flow = key in {"operating_cash_flow_usd", "capital_expenditures_usd"}
        for tag in tags:
            rows = gaap.get(tag, {}).get("units", {}).get("USD", [])
            matches = []
            for row in rows:
                if (
                    row.get("accn") != filing["accession"] or row.get("end") != filing["report_date"]
                    or not row.get("start") or not valid_number(row.get("val"))
                ):
                    continue
                days = (date.fromisoformat(row["end"]) - date.fromisoformat(row["start"])).days
                if (annual and 330 <= days <= 390) or (not annual and 70 <= days <= (300 if cash_flow else 110)):
                    matches.append((days, row))
            if matches:
                _, row = max(matches, key=lambda match: match[0])
                metrics[key] = row["val"]
                periods[key] = {"start": row["start"], "end": row["end"], "tag": tag}
                break
    return metrics, periods


def filing_record(ticker, cik, filing, collected_at, *, submissions_url, facts=None, facts_url=None):
    accession = filing["accession"]
    url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/{accession}-index.html"
    periodic = filing["form"] in PERIODIC_FORMS
    metrics, periods = filing_metrics(facts, filing) if facts else ({}, {})
    label = "periodic-results" if metrics else "periodic-metadata" if periodic else "event-results"
    evidence_id = f"{ticker.lower()}-sec-{label}"
    period_text = ", ".join(f"{key} {p['start']}–{p['end']}" for key, p in periods.items())
    return record(
        evidence_id, url, collected_at,
        category="sec_periodic" if periodic else "sec_event",
        observation_date=filing["report_date"] or filing["filing_date"],
        availability_basis="collection_time_sec_acceptance_is_not_publication",
        sec_acceptance_at=filing["accepted_at"], accession=accession, form=filing["form"],
        primary_document=filing.get("primary_document"),
        sec_filing_date=filing["filing_date"], sec_event_items=filing["items"],
        fact_periods=periods, submissions_url=submissions_url, facts_url=facts_url, metrics=metrics,
        text=(
            f"SEC {ticker} {filing['form']} filing {accession}, period end {filing['report_date']}, "
            f"accepted {filing['accepted_at']}; the acceptance is not a measured publication time. "
            f"Form items {filing['items'] or 'not listed'}. "
            + (f"As-filed standard US-GAAP USD metrics; period by metric: {period_text}."
               " Later amendments or XBRL processing corrections may exist."
               if metrics else "Filing metadata only; this does not establish company results or event content.")
        ),
    )


def capture(function):
    try:
        return function(), None
    except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        return None, f"{type(exc).__name__}: {exc}"


def sec_records(ticker, cik, client, cutoff):
    submissions_url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    facts_url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
    submission, submitted_at = client.json(submissions_url, sec=True)
    if (
        str(submission.get("cik")).zfill(10) != cik
        or ticker not in {name.upper().replace(".", "-") for name in submission.get("tickers", [])}
    ):
        raise ValueError(f"SEC Submissions did not confirm issuer mapping for {ticker}")
    rows = sec_filings(submission, cutoff)
    periodic = next((row for row in rows if row["form"] in PERIODIC_FORMS), None)
    event = next((row for row in rows if row["form"] in EVENT_FORMS), None)
    evidence = []
    gaps = []
    if periodic:
        downloaded, error = capture(lambda: client.json(facts_url, sec=True))
        if downloaded:
            facts, facts_at = downloaded
            evidence.append(filing_record(ticker, cik, periodic, max(submitted_at, facts_at), submissions_url=submissions_url,
                                          facts=facts, facts_url=facts_url))
            if not evidence[-1]["metrics"]:
                gaps.append("최신 SEC 정기 공시 접수번호에 대응하는 표준 XBRL 수치가 없습니다.")
        else:
            gaps.append(f"SEC XBRL 수집 실패 — {error}")
            evidence.append(filing_record(ticker, cik, periodic, submitted_at, submissions_url=submissions_url))
    else:
        gaps.append("기준 시점 이내 SEC 정기 공시를 확인하지 못했습니다.")
    if event:
        evidence.append(filing_record(ticker, cik, event, submitted_at, submissions_url=submissions_url))
    for item in evidence:
        item["issuer_name"] = submission.get("name")
    return evidence, gaps


def lookup_sec_cik(ticker, client, names=()):
    for entity in dict.fromkeys((ticker, ticker.split("-")[0], *names)):
        url = "https://efts.sec.gov/LATEST/search-index?" + urlencode({
            "entityName": entity, "forms": "10-K,10-Q", "size": 25,
        })
        response, _ = capture(lambda: client.json(url, sec=True))
        if not response:
            continue
        matches = {
            str(cik).zfill(10)
            for hit in response[0]["hits"]["hits"]
            for cik, display_name in zip(hit["_source"]["ciks"], hit["_source"]["display_names"])
            if ticker in {
                symbol.strip().upper().replace(".", "-")
                for group in re.findall(r"\(([^()]*)\)\s*\(CIK", display_name)
                for symbol in group.split(",")
            }
        }
        if len(matches) == 1:
            return next(iter(matches))
    return None


def collect_us_snapshots(tickers, *, requested_cutoff=None, client=None, collection_started=None):
    client = client or PublicClient()
    started_at = utc_now()
    if requested_cutoff is not None and requested_cutoff > started_at:
        raise ValueError("Analysis cutoff cannot be in the future")
    symbols = dict.fromkeys([MARKET, *tickers, *(SECTOR_ETFS[t] for t in tickers if t in SECTOR_ETFS)])
    with ThreadPoolExecutor(max_workers=6) as pool:
        quotes = {
            symbol: pool.submit(capture, lambda symbol=symbol: client.json(quote_url(symbol)))
            for symbol in symbols
        }
        fred_urls = {series: fred_url(series, requested_cutoff or collection_started or started_at) for series in FRED_SERIES}
        fred = {
            series: pool.submit(capture, lambda url=url: client.text(url))
            for series, url in fred_urls.items()
        }
        fund = pool.submit(soxl_sources, client, requested_cutoff or started_at) if "SOXL" in tickers else None
        companies, ticker_error = (None, None)
        if any(ticker != "SOXL" for ticker in tickers):
            ticker_file = pool.submit(capture, lambda: client.json(SEC_TICKERS, sec=True))
            companies, ticker_error = ticker_file.result()
        ciks = (
            {item["ticker"].upper().replace(".", "-"): str(item["cik_str"]).zfill(10)
             for item in companies[0].values()}
            if companies else {}
        )
        for ticker, cik in SEC_SAMPLE_CIKS.items():
            ciks.setdefault(ticker, cik)
        def issuer_filings(ticker):
            cik = ciks.get(ticker) or lookup_sec_cik(ticker, client)
            if not cik:
                raise ValueError(f"SEC ticker not verified for {ticker}" + (f" ({ticker_error})" if ticker_error else ""))
            return sec_records(ticker, cik, client, requested_cutoff or utc_now())

        filings = {ticker: pool.submit(capture, lambda ticker=ticker: issuer_filings(ticker))
                   for ticker in tickers if ticker != "SOXL"}
        quote_data = {symbol: job.result() for symbol, job in quotes.items()}
        fred_data = {series: job.result() for series, job in fred.items()}
        filing_data = {ticker: job.result() for ticker, job in filings.items()}
        fund_data = fund.result() if fund else None

    cutoff = requested_cutoff or utc_now()
    macro = {}
    for series, (downloaded, error) in fred_data.items():
        items, error = (
            capture(lambda: fred_records(downloaded, cutoff, url=fred_urls[series], series_ids=(series,)))
            if downloaded else (None, error)
        )
        macro[series] = (items or [], error)
    snapshots = {}
    for ticker in tickers:
        stock_id = f"{ticker.lower()}-stock-price-window"
        periodic_id = f"{ticker.lower()}-sec-periodic-results"
        is_fund = ticker == "SOXL"
        missing = [] if is_fund else ["발표 전 컨센서스", "매수·확대에 필요한 검증된 밸류에이션"]
        evidence = []
        for series, (items, error) in macro.items():
            evidence.extend(items)
            if not items:
                missing.append(f"FRED {series} 수집 자료 없음" + (f" — {error}" if error else ""))
        market_inputs = [
            (ticker, stock_id, "stock"),
            (MARKET, "us-sp500-price-window", "S&P 500 시장 대용 ETF"),
        ]
        if sector := SECTOR_ETFS.get(ticker):
            scope = "비레버리지 반도체 섹터 대용 ETF (공식 지수 아님)" if is_fund else "섹터 대용 ETF"
            market_inputs.append((sector, f"{ticker.lower()}-sector-price-window", scope))
        else:
            missing.append("이 종목의 섹터 벤치마크가 매핑되지 않았습니다.")
        for symbol, evidence_id, scope in market_inputs:
            downloaded, error = quote_data[symbol]
            item, error = (
                capture(lambda: market_record(symbol, evidence_id, scope, downloaded, cutoff))
                if downloaded else (None, error)
            )
            if item:
                evidence.append(item)
            else:
                missing.append(f"{symbol} 확정 일봉 수집 실패 — {error}")
        if is_fund:
            items, gaps = fund_data
            evidence.extend(items)
            missing.extend(gaps)
            if comparison := soxl_comparison(evidence):
                evidence.append(comparison)
        else:
            filed, error = filing_data.get(ticker, (None, ticker_error or "SEC 종목 코드 미확인"))
            if filed:
                items, gaps = filed
                evidence.extend(items)
                missing.extend(gaps)
            else:
                missing.append(f"SEC 공시 수집 실패 — {error}")
        if any(parse_timestamp(item["available_at"]) > cutoff for item in evidence):
            missing.append("과거 시점 공개·수정 이력이 검증되지 않아 기준 시각 후에 수집된 자료를 제외했습니다.")
        snapshots[ticker] = (None, {
            "case_id": f"us-live-{ticker.lower()}", "ticker": ticker, "as_of": as_utc(cutoff),
            "horizon": "short" if is_fund else "medium",
            "question": (
                "Evaluate held SOXL as a daily +3x leveraged ETF over a short tactical review horizon. "
                "Distinguish that horizon from the one-day target. Use verified fund terms, holdings, price "
                "and macro context. State the strongest opposing action and reassessment triggers."
                if is_fund else
                "Evaluate the held US equity using the captured US macro context, market and sector ETFs, "
                "reported SEC facts and completed daily bars. Compare daily returns only for aligned "
                "sessions and identify ETF proxies. Explain current opposing actions and triggers."
            ),
            "required_evidence_ids": [stock_id, SOXL_TERMS if is_fund else periodic_id],
            "evidence": evidence, "missing_fields": missing,
        })
    return snapshots
