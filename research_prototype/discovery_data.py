"""Public, dated inputs for the bounded US candidate screen."""

from concurrent.futures import ThreadPoolExecutor
import csv
from datetime import date, datetime
from hashlib import sha256
from io import BytesIO, StringIO
import re
from threading import Lock
from time import monotonic, sleep
from urllib.request import Request, urlopen
from xml.etree import ElementTree
from zipfile import ZipFile
from zoneinfo import ZoneInfo

from .us_data import (
    FRED_SERIES, PublicClient, SEC_SAMPLE_CIKS, SEC_TICKERS, capture,
    filing_metrics, filing_record, fred_records, fred_url, lookup_sec_cik, market_record, quote_url, sec_filings, utc_now,
)


SECTORS = ("XLK", "XLI", "XLV", "XLP")
NASDAQ_DIRECTORY = (
    "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt",
    "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt",
)
NON_COMMON = re.compile(
    r"\b(?:ETF|ETN|ADR|ADS|DEPOSITARY|PREFERRED|PREFERENCE|WARRANT|RIGHTS|CASH|SWAP|FUTURES|MONEY MARKET)\b", re.I,
)
XLSX = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def holdings_url(sector):
    return f"https://www.ssga.com/library-content/products/fund-data/etfs/us/holdings-daily-us-en-{sector.lower()}.xlsx"


def canonical_ticker(value):
    return value.strip().upper().replace(".", "-")


def parse_holdings(data, sector, collected_at):
    with ZipFile(BytesIO(data)) as workbook:
        strings = []
        if "xl/sharedStrings.xml" in workbook.namelist():
            tree = ElementTree.fromstring(workbook.read("xl/sharedStrings.xml"))
            strings = ["".join(t.text or "" for t in item.iter(XLSX + "t")) for item in tree]
        tree = ElementTree.fromstring(workbook.read("xl/worksheets/sheet1.xml"))
    columns = None
    observed = None
    holdings = []
    for row in tree.iter(XLSX + "row"):
        cells = {}
        for cell in row:
            value = cell.findtext(XLSX + "v")
            if cell.get("t") == "s" and value is not None:
                value = strings[int(value)]
            if cell.get("t") == "inlineStr":
                value = "".join(node.text or "" for node in cell.iter(XLSX + "t"))
            if value is not None:
                cells[re.sub(r"\d+", "", cell.attrib["r"])] = value.strip()
        by_value = {value.casefold(): col for col, value in cells.items()}
        if any(value.startswith("as of ") for value in by_value):
            for value in cells.values():
                if not value.lower().startswith("as of "):
                    continue
                for pattern in ("%d-%b-%Y", "%m/%d/%Y", "%Y-%m-%d", "%B %d, %Y", "%b %d, %Y"):
                    try:
                        observed = datetime.strptime(value[6:], pattern).date()
                        break
                    except ValueError:
                        continue
        if {"ticker", "weight", "identifier", "local currency"} <= by_value.keys():
            columns = by_value
            continue
        if not columns:
            continue
        ticker = canonical_ticker(cells.get(columns["ticker"], ""))
        identifier = cells.get(columns["identifier"], "")
        name = cells.get(columns.get("name", "A"), "")
        weight = cells.get(columns["weight"], "")
        if ticker and identifier and name and weight:
            holdings.append({
                "ticker": ticker, "name": name, "identifier": identifier,
                "currency": cells.get(columns["local currency"]), "weight_pct": float(weight.rstrip("%")),
            })
    if not holdings or observed is None:
        raise ValueError(f"State Street {sector} XLSX lacks dated holdings")
    if observed > collected_at.astimezone(ZoneInfo("America/New_York")).date():
        raise ValueError(f"State Street {sector} holdings date is after collection")
    return {
        "id": f"holdings-{sector.lower()}", "source_url": holdings_url(sector),
        "observation_date": observed.isoformat(), "collected_at": collected_at.isoformat(),
        "available_at": collected_at.isoformat(), "published_at": None,
        "workbook_sha256": sha256(data).hexdigest(),
        "holdings": sorted(holdings, key=lambda row: (-row["weight_pct"], row["ticker"])),
    }


def fetch_holdings(sector):
    request = Request(holdings_url(sector), headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(request, timeout=15) as response:
        return parse_holdings(response.read(), sector, utc_now())


def fetch_listing(url, client):
    body, collected_at = client.text(url)
    directory = {}
    for row in csv.DictReader(StringIO(body), delimiter="|"):
        ticker = row.get("Symbol") or row.get("ACT Symbol")
        if not ticker or row.get("ETF") != "N" or row.get("Test Issue") != "N":
            continue
        name = row["Security Name"]
        if not NON_COMMON.search(name):
            directory[canonical_ticker(ticker)] = name
    if not directory:
        raise ValueError("No qualifying security names in exchange directory")
    return {"source_url": url, "collected_at": collected_at.isoformat(), "listings": directory}


def quote_source(symbol, client):
    downloaded = client.json(quote_url(symbol))
    source = market_record(symbol, f"price-{symbol.lower()}", "US market price", downloaded, downloaded[1])
    metadata = downloaded[0]["chart"]["result"][0]["meta"]
    source["quote_type"] = metadata.get("instrumentType")
    source["exchange"] = metadata.get("exchangeName")
    return source


class DiscoveryClient(PublicClient):
    """Pace SEC requests across the discovery worker pool."""

    def __init__(self, *args):
        super().__init__(*args)
        self._sec_lock = Lock()
        self._last_request = 0

    def text(self, url, *, sec=False):
        if sec:
            with self._sec_lock:
                sleep(max(0, 0.15 - (monotonic() - self._last_request)))
                self._last_request = monotonic()
        return super().text(url, sec=sec)


def fetch_many(keys, callback):
    with ThreadPoolExecutor(max_workers=8) as pool:
        jobs = {key: pool.submit(capture, lambda key=key: callback(key)) for key in keys}
        return {key: job.result() for key, job in jobs.items()}


def fetch_macro(client, collection_started=None):
    cutoff = collection_started or utc_now()

    def load(series):
        url = fred_url(series, cutoff)
        body = client.text(url)
        return fred_records(body, body[1], url=url, series_ids=(series,))

    return fetch_many(FRED_SERIES, load)


def fetch_sec(selected, client):
    if not selected:
        return {}
    directory, error = capture(lambda: client.json(SEC_TICKERS, sec=True))
    if directory:
        rows = directory[0].values()
        ciks = {row["ticker"].upper().replace(".", "-"): str(row["cik_str"]).zfill(10) for row in rows}
    else:
        ciks = {}
    ciks.update(SEC_SAMPLE_CIKS)

    names = {row["ticker"]: row["name"] for row in selected}

    def load(ticker):
        cik = ciks.get(ticker)
        if not cik:
            clean_name = re.sub(r"\s+(?:/\s*THE|[A-C])$", "", names[ticker]).replace("+", " ")
            cik = lookup_sec_cik(ticker, client, (clean_name,))
        if not cik:
            raise ValueError(f"SEC ticker mapping unavailable for {ticker}: {error or 'no exact EFTS match'}")
        return get_discovery_filings(ticker, cik, client)

    return fetch_many(names, load)


def get_discovery_filings(ticker, cik, client):
    submissions_url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    facts_url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
    submission, submitted = client.json(submissions_url, sec=True)
    if (str(submission.get("cik")).zfill(10) != cik
            or ticker not in {canonical_ticker(symbol) for symbol in submission.get("tickers", [])}):
        raise ValueError("SEC Submissions did not confirm ticker and issuer")
    facts, fetched = client.json(facts_url, sec=True)
    filings = sec_filings(submission, max(submitted, fetched))
    evidence = []
    metrics = {}
    for form, label in (("10-Q", "quarter"), ("10-K", "annual")):
        filing = next((row for row in filings if row["form"] == form), None)
        if not filing:
            continue
        item = filing_record(ticker, cik, filing, max(submitted, fetched), submissions_url=submissions_url,
                             facts=facts, facts_url=facts_url)
        if not item["metrics"]:
            continue
        item["id"] = f"sec-{ticker.lower()}-{label}"
        evidence.append(item)
        values = item["metrics"]
        revenue = values.get("revenue_usd")
        if not revenue or revenue <= 0:
            continue
        if form == "10-Q":
            end = date.fromisoformat(filing["report_date"])
            tag = item["fact_periods"]["revenue_usd"]["tag"]
            rows = facts["facts"]["us-gaap"][tag]["units"]["USD"]
            prior_dates = {
                row["end"] for row in rows if row.get("accn") == filing["accession"]
                and row.get("end") and 350 <= (end - date.fromisoformat(row["end"])).days <= 380
            }
            if len(prior_dates) == 1:
                prior, _ = filing_metrics(facts, {**filing, "report_date": next(iter(prior_dates))})
                if prior.get("revenue_usd", 0) > 0:
                    metrics["quarter_revenue_yoy_pct"] = (revenue / prior["revenue_usd"] - 1) * 100
            if "operating_income_usd" in values:
                metrics["quarter_operating_margin_pct"] = values["operating_income_usd"] / revenue * 100
        else:
            metrics.update(long_metrics(facts, filing, item))
    return evidence, metrics


def long_metrics(facts, filing, item):
    """Use only fiscal-year comparatives explicitly filed in the same accession."""
    gaap = facts.get("facts", {}).get("us-gaap", {})
    current_end = date.fromisoformat(filing["report_date"])
    flow_fields = ("revenue_usd", "operating_cash_flow_usd", "capital_expenditures_usd")
    periods = {}
    for field in flow_fields:
        if field not in item["fact_periods"]:
            return {}
        tag = item["fact_periods"][field]["tag"]
        periods[field] = {
            row["end"]: row["val"] for row in gaap.get(tag, {}).get("units", {}).get("USD", [])
            if row.get("accn") == filing["accession"] and row.get("start") and row.get("end")
            and 330 <= (date.fromisoformat(row["end"]) - date.fromisoformat(row["start"])).days <= 390
        }
    years = sorted(set.intersection(*(set(series) for series in periods.values())))
    years = [end for end in years if 0 <= (current_end - date.fromisoformat(end)).days <= 780][-3:]
    if len(years) < 2 or years[-1] != current_end.isoformat():
        return {}
    if not all(periods["revenue_usd"][end] > 0 for end in years):
        return {}
    margins = [
        (periods["operating_cash_flow_usd"][end] - periods["capital_expenditures_usd"][end])
        / periods["revenue_usd"][end] * 100 for end in years
    ]

    def instant(*tags):
        for tag in tags:
            rows = gaap.get(tag, {}).get("units", {}).get("USD", [])
            match = {
                row["val"] for row in rows if row.get("accn") == filing["accession"]
                and row.get("end") == current_end.isoformat() and not row.get("start")
            }
            if len(match) == 1:
                return match.pop()
        return None

    debt = instant("LongTermDebtAndFinanceLeaseObligations", "LongTermDebt")
    if debt is None:
        current = instant("LongTermDebtAndFinanceLeaseObligationsCurrent", "LongTermDebtCurrent")
        noncurrent = instant("LongTermDebtAndFinanceLeaseObligationsNoncurrent", "LongTermDebtNoncurrent")
        if current is not None and noncurrent is not None:
            debt = current + noncurrent
    derived = {"minimum_annual_cash_after_capex_margin_pct": min(margins)}
    item["comparative_periods"] = years
    item["comparative_cash_after_capex_margins_pct"] = margins
    if debt is not None and debt >= 0:
        current_revenue = periods["revenue_usd"][years[-1]]
        derived["annual_long_term_debt_to_revenue_pct"] = debt / current_revenue * 100
        item["long_term_debt_usd"] = debt
    item["derived_metrics"] = derived
    return derived
