"""Public, dated inputs for the bounded US candidate screen."""

from concurrent.futures import ThreadPoolExecutor
import csv
from datetime import date, datetime, timedelta
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
    FACT_TAGS, filing_metrics, filing_record, fred_records, fred_url, lookup_sec_cik, market_record, quote_url,
    sec_filings, utc_now, valid_number,
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
    quarter = next((row for row in filings if row["form"] == "10-Q"), None)
    annual = next((row for row in filings if row["form"] == "10-K"), None)
    q4_expected = bool(annual and (not quarter or annual["report_date"] >= quarter["report_date"]))
    evidence = []
    metrics = {}
    for filing, label in ((quarter, "quarter"), (annual, "annual")):
        if not filing:
            continue
        item = filing_record(ticker, cik, filing, max(submitted, fetched), submissions_url=submissions_url,
                             facts=facts, facts_url=facts_url)
        item["id"] = f"sec-{ticker.lower()}-{label}"
        evidence.append(item)
        values = item["metrics"]
        revenue = values.get("revenue_usd")
        if not revenue or revenue <= 0:
            continue
        if label == "quarter" and not q4_expected:
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
        elif label == "annual":
            metrics.update(long_metrics(facts, filing, item))
    if q4_expected and quarter and (q4 := fourth_quarter_metrics(facts, annual, quarter)):
        annual_item = next(item for item in evidence if item["id"] == f"sec-{ticker.lower()}-annual")
        quarter_item = next(item for item in evidence if item["id"] == f"sec-{ticker.lower()}-quarter")
        derived = {
            "quarter_revenue_yoy_pct": (q4["metrics"]["revenue_usd"] / q4["prior_revenue_usd"] - 1) * 100,
            "quarter_operating_margin_pct": q4["metrics"]["operating_income_usd"] / q4["metrics"]["revenue_usd"] * 100,
        }
        quarter_item.update(
            form="10-K/10-Q", source_url=annual_item["source_url"], observation_date=annual["report_date"],
            sec_acceptance_at=annual["accepted_at"], accession=annual["accession"],
            metrics=q4["metrics"], fact_periods={}, derived_metrics=derived,
            quarter_basis="annual_less_q3_ytd",
            quarter_start=q4["start"], quarter_sources=[
                {"accession": annual["accession"], "url": annual_item["source_url"]},
                {"accession": quarter["accession"], "url": quarter_item["source_url"]},
            ],
            quarter_inputs=q4["inputs"],
            text="Fiscal Q4 derived from matching SEC annual and nine-month same-start cumulative facts.",
        )
        metrics.update(derived)
    latest_periodic = max((item for item in (quarter, annual) if item),
                          key=lambda item: item["accepted_at"], default=None)
    if latest_periodic:
        pending = next((item for item in filings if item["form"] in {"8-K", "8-K/A"}
                        and "2.02" in re.findall(r"\d\.\d{2}", item["items"])
                        and item["accepted_at"] > latest_periodic["accepted_at"]
                        and item["filing_date"] > latest_periodic["filing_date"]), None)
        if pending:
            latest_record = next((item for item in evidence if item["id"] == f"sec-{ticker.lower()}-quarter"), None)
            if latest_record is None:
                latest_record = filing_record(ticker, cik, pending, max(submitted, fetched),
                                              submissions_url=submissions_url)
                latest_record["id"] = f"sec-{ticker.lower()}-quarter"
                evidence.append(latest_record)
            latest_record["newer_results_filing"] = {
                "accession": pending["accession"], "acceptance_at": pending["accepted_at"],
                "filing_date": pending["filing_date"], "item": "2.02",
            }
            metrics.pop("quarter_revenue_yoy_pct", None)
            metrics.pop("quarter_operating_margin_pct", None)
    return evidence, metrics


def fourth_quarter_metrics(facts, annual, quarter):
    """Use FY less nine-month facts with matching US GAAP tag, unit and fiscal start."""
    gaap = facts.get("facts", {}).get("us-gaap", {})
    end, q3 = (date.fromisoformat(filing["report_date"]) for filing in (annual, quarter))
    if not 70 <= (end - q3).days <= 120:
        return None
    inputs = {}
    for metric in ("revenue_usd", "operating_income_usd"):
        options = []
        for tag in FACT_TAGS[metric]:
            rows = [row for row in gaap.get(tag, {}).get("units", {}).get("USD", [])
                    if row.get("start") and row.get("end") and valid_number(row.get("val"))]
            pairs = {}
            for fy in rows:
                if fy.get("accn") != annual["accession"]:
                    continue
                start, fy_end = date.fromisoformat(fy["start"]), date.fromisoformat(fy["end"])
                if not 330 <= (fy_end - start).days <= 390:
                    continue
                for ytd in rows:
                    if ytd.get("accn") != quarter["accession"] or ytd["start"] != fy["start"]:
                        continue
                    ytd_end = date.fromisoformat(ytd["end"])
                    if not (70 <= (fy_end - ytd_end).days <= 120 and 240 <= (ytd_end - start).days <= 310):
                        continue
                    if fy_end == end and ytd_end != q3:
                        continue
                    pair = (fy["start"], ytd["end"], fy["val"], ytd["val"])
                    pairs.setdefault(fy_end, set()).add(pair)
            previous = [day for day in pairs if 350 <= (end - day).days <= 380]
            if len(pairs.get(end, [])) == 1 and (metric != "revenue_usd" or
                    (len(previous) == 1 and len(pairs[previous[0]]) == 1)):
                current = next(iter(pairs[end]))
                prior = next(iter(pairs[previous[0]])) if len(previous) == 1 and len(pairs[previous[0]]) == 1 else None
                options.append({"tag": tag, "current": current, "prior": prior})
        if not options:
            return None
        inputs[metric] = options[0]
    revenue, operating = (inputs[key]["current"] for key in ("revenue_usd", "operating_income_usd"))
    if revenue[:2] != operating[:2]:
        return None
    prior = inputs["revenue_usd"]["prior"]
    values = {key: item["current"][2] - item["current"][3] for key, item in inputs.items()}
    if values["revenue_usd"] <= 0 or prior[2] - prior[3] <= 0:
        return None
    return {"metrics": values, "prior_revenue_usd": prior[2] - prior[3], "inputs": inputs,
            "start": (q3 + timedelta(days=1)).isoformat()}


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
