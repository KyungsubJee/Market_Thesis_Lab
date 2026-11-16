"""Fetch qualitative source text for shortlisted US discovery candidates."""

from datetime import date, datetime, time, timezone
from hashlib import sha256
from html.parser import HTMLParser
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from framework_validation.runner import parse_timestamp

from .discovery_data import fetch_many
from .discovery_qualitative import MAX_TEXT, add_qualitative_evidence
from .us_data import as_utc, utc_now


REGISTRY = Path(__file__).with_name("discovery_sources.json")
STRATEGY_TERMS = ("business", "strategy", "segment", "product", "customer", "demand",
                  "guidance", "outlook", "growth", "backlog", "member", "launch")
RISK_TERMS = ("risk", "could", "may adversely", "depend", "uncertain", "competition", "concentration",
              "export", "regulatory", "contract", "supply", "litigation", "margin")
BLOCKS = {"p", "div", "li", "br", "h1", "h2", "h3", "h4", "td", "tr", "section", "article"}
HIDDEN = {"script", "style", "head", "header", "footer", "nav", "noscript", "svg", "ix:header", "ix:hidden"}
BOILERPLATE = ("will host a conference call", "forward-looking statements", "actual results may differ",
               "photo is also available", "news release distribution", "all rights reserved", "website at")


class SourceText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks = []
        self.parts = []
        self.hidden = []

    def flush(self):
        if self.parts:
            if value := " ".join("".join(self.parts).split()):
                self.blocks.append(value)
            self.parts.clear()

    def handle_starttag(self, tag, attrs):
        if tag in HIDDEN:
            self.flush()
            self.hidden.append(tag)
        elif not self.hidden and tag in BLOCKS:
            self.flush()

    def handle_endtag(self, tag):
        if self.hidden:
            if tag == self.hidden[-1]:
                self.hidden.pop()
        elif tag in BLOCKS:
            self.flush()

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def excerpts(body, identity, terms):
    parser = SourceText()
    parser.feed(body)
    parser.close()
    parser.flush()
    if not re.search(identity, " ".join(parser.blocks), re.I):
        raise ValueError("Expected document identity was not found in visible HTML")
    passages = []
    for block in parser.blocks:
        if len(block) < 90:
            continue
        for passage in re.split(r"(?<=[.!?])\s+(?=[A-Z“])", block):
            if 90 <= len(passage) <= MAX_TEXT:
                lower = passage.lower()
                if any(phrase in lower for phrase in BOILERPLATE):
                    continue
                score = sum(term.lower() in lower for term in terms)
                if score >= 2:
                    passages.append((score, passage))
    if not passages:
        raise ValueError("No relevant narrative passage found in the source")
    selected = []
    for _, passage in sorted(passages, key=lambda row: -row[0]):
        if passage not in selected and len(" … ".join([*selected, passage])) <= MAX_TEXT:
            selected.append(passage)
        if len(selected) == 3:
            break
    return " … ".join(selected)


def dated_source(config, terms):
    stamp = datetime.combine(date.fromisoformat(config["date"]), time.max, timezone.utc)
    return config["urls"], config["identity"], terms, stamp.isoformat(), "registry_publication_date", None


def filing_source(ticker, run, issuer, client):
    filing = max(
        (item for period in ("quarter", "annual") if (item := run["evidence"].get(f"sec-{ticker.lower()}-{period}"))
         and item.get("sec_acceptance_at")),
        key=lambda item: item["sec_acceptance_at"], default=None,
    )
    if not filing:
        return None
    document = filing.get("primary_document")
    if not document:
        submission, _ = client.json(filing["submissions_url"], sec=True)
        recent = submission["filings"]["recent"]
        if filing["accession"] not in recent["accessionNumber"]:
            return None
        index = recent["accessionNumber"].index(filing["accession"])
        document = recent["primaryDocument"][index]
    if not re.fullmatch(r"[\w.-]+\.html?", document) or not re.fullmatch(r"\d{10}-\d{2}-\d{6}", filing["accession"]):
        return None
    canonical = filing["source_url"].rsplit("/", 1)[0] + "/" + document
    urls = [canonical]
    if mirror := issuer.get("sec_mirrors", {}).get(filing["accession"]):
        urls.append(mirror)
    return (urls, r"management.s discussion and analysis|risk factors", RISK_TERMS, filing["sec_acceptance_at"],
            "sec_acceptance_timestamp", {"form": filing["form"], "accession": filing["accession"],
                                         "canonical_url": canonical})


def load_registry(path=REGISTRY):
    registry = json.loads(Path(path).read_text(encoding="utf-8"))
    if registry.get("schema_version") != 1 or not all(
        isinstance(registry.get(key), dict) for key in ("issuers", "industries")
    ):
        raise ValueError("Qualitative source registry requires schema_version 1, issuers and industries")
    for item in (*registry["issuers"].values(), *registry["industries"].values()):
        dated_source(item, ())
        re.compile(item["identity"])
        urls = [*item["urls"], *item.get("sec_mirrors", {}).values()]
        if not urls or any(urlsplit(url).scheme != "https" or not urlsplit(url).hostname
                           or urlsplit(url).username is not None for url in urls):
            raise ValueError("Registry sources require HTTPS URLs without credentials")
    return registry


def collect_qualitative(run, client, registry):
    shortlist = sorted({pick["ticker"] for result in run["horizons"].values() for pick in result["shortlist"]})
    cutoff = parse_timestamp(run.get("qualitative_as_of", run["as_of"]))
    filings = fetch_many(shortlist, lambda ticker: filing_source(ticker, run, registry["issuers"].get(ticker, {}), client))
    plans = {}
    for ticker in shortlist:
        issuer = registry["issuers"].get(ticker, {})
        industry = registry["industries"].get(issuer.get("industry"))
        plans[ticker, "business_strategy"] = dated_source(issuer, (*STRATEGY_TERMS, *issuer.get("terms", []))) if issuer else None
        plans[ticker, "sector_outlook"] = dated_source(industry, industry["terms"]) if industry else None
        plans[ticker, "thesis_risk"] = filings[ticker][0]

    def fetch(plan):
        urls, identity, terms, stamp, basis, filing = plan
        attempts = []
        if parse_timestamp(stamp) > cutoff:
            return None, [{"url": url, "status": "after_cutoff", "available_at": stamp} for url in urls]
        for url in urls:
            attempt = {"url": url, "requested_at": as_utc(utc_now()), "available_at": stamp,
                       "availability_basis": basis, **(filing or {})}
            attempts.append(attempt)
            try:
                body, fetched_at = client.text(url, sec=urlsplit(url).hostname == "www.sec.gov")
                attempt.update(collected_at=as_utc(fetched_at), sha256=sha256(body.encode()).hexdigest())
                excerpt = excerpts(body, identity, terms)
            except (OSError, ValueError) as exc:
                attempt.update(status="unavailable", error=str(exc))
                continue
            attempt["status"] = "available"
            return excerpt, attempts
        return None, attempts

    # Each distinct source is fetched once, even when two candidates share an industry.
    job_keys = {(tuple(plan[0]), plan[1], tuple(plan[2]), plan[3], plan[4]): plan
                for plan in plans.values() if plan}
    results = fetch_many(job_keys, lambda key: fetch(job_keys[key]))
    evidence = []
    metadata = {}
    diagnostics = {}
    for (ticker, category), plan in plans.items():
        ticker_diagnostics = diagnostics.setdefault(ticker, {})
        if plan is None:
            ticker_diagnostics[category] = [{"status": "source_not_configured" if category != "thesis_risk"
                                              else "no_verified_periodic_filing"}]
            if category == "thesis_risk" and filings[ticker][1]:
                ticker_diagnostics[category][0]["error"] = filings[ticker][1]
            continue
        key = (tuple(plan[0]), plan[1], tuple(plan[2]), plan[3], plan[4])
        response, error = results[key]
        if error:
            ticker_diagnostics[category] = [{"status": "unavailable", "urls": plan[0], "error": error}]
            continue
        excerpt, attempts = response
        ticker_diagnostics[category] = attempts
        if excerpt:
            source = attempts[-1]
            evidence_id = f"qual-{ticker.lower()}-{category.replace('_', '-')}"
            evidence.append({"id": evidence_id, "ticker": ticker, "category": category,
                             "source_url": source["url"], "observed_at": source["available_at"], "text": excerpt})
            metadata[evidence_id] = {**source, "provenance": "automatic_primary_source"}
    add_qualitative_evidence(run, {"schema_version": 1, "evidence": evidence})
    for evidence_id, item in metadata.items():
        run["evidence"][evidence_id].update(item)
    run["sources"]["qualitative"] = diagnostics
