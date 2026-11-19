"""Collect dated public events and compare frozen, aligned daily prices around them."""

from collections import Counter
from datetime import timedelta
from email.utils import parsedate_to_datetime
from hashlib import sha256
import json
from pathlib import Path
import re
from urllib.parse import urlencode, urlsplit
from xml.etree import ElementTree

from framework_validation.runner import parse_timestamp

from .discovery_collect import SourceText
from .discovery_data import canonical_ticker, fetch_many
from .etf_data import SOXL_TERMS
from .us_data import as_utc, record, sec_filings, utc_now


EVENT_PREFIX = "event-context-"
RECENT_DAYS = 45
MATERIAL_ITEMS = {
    "1.01": "주요 계약", "1.02": "주요 계약 종료", "1.03": "회생·파산",
    "2.01": "인수·자산 처분", "2.02": "실적 발표", "2.03": "채무·재무 의무",
    "2.05": "구조조정", "2.06": "손상차손", "3.01": "상장 관련 변경",
    "3.02": "미등록 증권 발행", "4.01": "감사인 변경", "4.02": "재무제표 신뢰성",
    "5.01": "지배권 변경", "5.02": "경영진·이사회 인사 및 보상", "5.03": "정관 변경",
    "7.01": "선별 공시 규정에 따른 발표", "8.01": "기타 주요 공개 사항",
}
ITEM_HEADING = re.compile(r"^Item\s+(\d\.\d{2})\b", re.I)


def load_manifest(path):
    if path is None:
        return []
    bundle = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if (not isinstance(bundle, dict) or type(bundle.get("schema_version")) is not int
            or bundle["schema_version"] != 1 or not isinstance(bundle.get("sources"), list)):
        raise ValueError("Event source manifest requires schema_version 1 and a sources list")
    for item in bundle["sources"]:
        fields = ("ticker", "url", "published_at", "kind", "title_pattern")
        if not isinstance(item, dict) or any(not isinstance(item.get(key), str) or not item[key].strip() for key in fields):
            raise ValueError("Event source must specify ticker, URL, publish timestamp, kind and title pattern")
        url = urlsplit(item["url"])
        if (url.scheme != "https" or not url.hostname or url.username is not None
                or any(char.isspace() or char in "<>" for char in item["url"])):
            raise ValueError("Event source URL must be HTTPS and credential-free")
        if item["kind"] not in {"issuer", "independent_news"}:
            raise ValueError("Event source kind must be issuer or independent_news")
        if (item.get("availability_basis", "date_upper_bound") not in {"date_upper_bound", "verified_exact_timestamp"}
                or item.get("time_basis", "date_upper_bound") not in {"date_upper_bound", "exact"}):
            raise ValueError("Event source availability_basis must be date_upper_bound or verified_exact_timestamp")
        parse_timestamp(item["published_at"])
        re.compile(item["title_pattern"])
    return bundle["sources"]


def select_manifest(rows, tickers, cutoff):
    scoped = []
    skipped = []
    for item in rows:
        stamp = parse_timestamp(item["published_at"])
        if canonical_ticker(item["ticker"]) not in tickers:
            status = "ticker_out_of_scope"
        elif stamp > cutoff:
            status = "after_cutoff"
        elif stamp < cutoff - timedelta(days=RECENT_DAYS):
            status = "outside_recent_window"
        else:
            scoped.append(item)
            continue
        skipped.append({**item, "status": status})
    accepted = []
    group_counts = Counter()
    for item in sorted(scoped, key=lambda row: parse_timestamp(row["published_at"]), reverse=True):
        group = canonical_ticker(item["ticker"]), item["kind"]
        if group_counts[group] == 2:
            skipped.append({**item, "status": "source_limit"})
        else:
            accepted.append(item)
            group_counts[group] += 1
    return accepted, skipped


def source_blocks(body):
    parser = SourceText()
    parser.feed(body)
    parser.close()
    parser.flush()
    return parser.blocks


def filing_excerpt(body, item_codes):
    blocks = source_blocks(body)
    # Ignore exhibit-only navigation. Include narrative immediately following a substantive item heading.
    passages = []
    for index, block in enumerate(blocks):
        heading = ITEM_HEADING.match(block)
        if heading is None or heading[1] not in item_codes:
            continue
        snippet = [block]
        for next_block in blocks[index + 1:index + 13]:
            if ITEM_HEADING.match(next_block) or next_block.upper() == "SIGNATURES":
                break
            snippet.append(next_block)
        candidate = " ".join(snippet)
        if len(candidate) > 110:
            passages.append(candidate)
    if not passages:
        raise ValueError("Substantive 8-K item body was not found")
    text = " … ".join(dict.fromkeys(passages))
    return text[:2200].rsplit(" ", 1)[0] if len(text) > 2200 else text


def news_excerpt(body, identity):
    blocks = source_blocks(body)
    heading = next((i for i, block in enumerate(blocks) if re.search(identity, block, re.I)), None)
    if heading is None:
        raise ValueError("Expected headline was not found in visible HTML")
    paragraphs = [
        block for block in blocks[heading + 1:heading + 100] if len(block) >= 100
        and not re.search(r"cookie|all rights reserved|sign up|advertisement|forward-looking statements", block, re.I)
    ]
    if not paragraphs:
        raise ValueError("No news article body found after expected headline")
    return blocks[heading][:240], " ".join(dict.fromkeys(paragraphs[:5]))[:1550]


def aligned_reaction(event_at, stock, benchmark, cutoff, sector=None):
    if not stock or not benchmark:
        return None
    event_time, end = map(parse_timestamp, (event_at, cutoff))
    market = {row["session_date"]: row for row in benchmark["observations"]}
    sessions = [
        (row, market[row["session_date"]], bound)
        for row in stock["observations"] if row["session_date"] in market
        if (bound := parse_timestamp(row["session_completion_bound"])) <= end
        and bound == parse_timestamp(market[row["session_date"]]["session_completion_bound"])
    ]
    before = next((pair for pair in reversed(sessions) if pair[2] < event_time), None)
    after = next((pair for pair in sessions if event_time < pair[2]), None)
    if before is None or after is None:
        return None
    if (event_time - before[2] > timedelta(days=7) or after[2] - event_time > timedelta(days=7)):
        return None
    stock_pct, benchmark_pct = ((after[i]["close"] / before[i]["close"] - 1) * 100 for i in (0, 1))
    ratio = after[0]["volume"] / before[0]["volume"] if before[0]["volume"] else None
    result = {
        "before": before[0]["session_date"], "after": after[0]["session_date"],
        "stock_pct": round(stock_pct, 3), "benchmark_pct": round(benchmark_pct, 3),
        "benchmark_ticker": benchmark.get("market_symbol", "SPY"),
        "excess_pp": round(stock_pct - benchmark_pct, 3),
        "volume_vs_pre_event_session": round(ratio, 3) if ratio is not None else None,
        "evidence_ids": [stock["id"], benchmark["id"]],
    }
    if sector:
        sector_bars = {row["session_date"]: row for row in sector["observations"]}
        sector_window = [sector_bars.get(result[key]) for key in ("before", "after")]
        if all(sector_window) and all(parse_timestamp(row["session_completion_bound"]) == pair[2]
                                      for row, pair in zip(sector_window, (before, after))):
            sector_pct = (sector_window[1]["close"] / sector_window[0]["close"] - 1) * 100
            result.update(sector_ticker=sector["market_symbol"], sector_pct=round(sector_pct, 3))
            result["evidence_ids"].append(sector["id"])
    return result


def sec_plans(client, ticker, items, cutoff):
    known = [item for item in items if item.get("form") == "8-K"]
    if known:
        rows = [{"accession": item["accession"], "accepted_at": item["sec_acceptance_at"],
                 "items": item["sec_event_items"], "primary_document": item.get("primary_document"),
                 "source_url": item["source_url"]} for item in known]
    elif source := next((item for item in items if item.get("submissions_url")), None):
        url = source["submissions_url"]
        cik = url.rsplit("CIK", 1)[1].removesuffix(".json")
        submissions, _ = client.json(url, sec=True)
        if ticker not in {canonical_ticker(value) for value in submissions["tickers"]}:
            raise ValueError(f"SEC submission ticker mismatch for {ticker}")
        rows = [{**item, "source_url": f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
                                     f"{item['accession'].replace('-', '')}/index.html"}
                for item in sec_filings(submissions, cutoff) if item["form"] == "8-K"]
    else:
        return None
    plans = []
    for row in rows:
        stamp = parse_timestamp(row["accepted_at"])
        codes = [item for item in re.findall(r"\d\.\d{2}", row["items"]) if item in MATERIAL_ITEMS]
        document = row["primary_document"]
        if not codes:
            continue
        if not cutoff - timedelta(days=RECENT_DAYS) <= stamp <= cutoff:
            continue
        url = (row["source_url"].rsplit("/", 1)[0] + "/" + document
               if document and re.fullmatch(r"[\w.-]+\.html?", document) else None)
        plans.append({"ticker": ticker, "kind": "sec_8k", "url": url, "published_at": row["accepted_at"],
                      "item_codes": codes, "accession": row["accession"]})
        if len(plans) == 2:
            break
    return plans if plans or not known else None


def relevant_headline(title, ticker, issuer_name=None, sector_terms=()):
    if re.search(rf"(?<![\w.-]){re.escape(ticker)}(?![\w.-])", title):
        return True
    aliases = list(sector_terms)
    if issuer_name:
        name = re.sub(r"\s+(?:,?\s*(?:INC|CORP|CORPORATION|CO|COMPANY|PLC|LTD|/THE)\.?)+$", "",
                      issuer_name, flags=re.I)
        if len(name.strip()) >= 4:
            aliases.append(name.strip())
        words = name.split()
        if len(words) == 2 and len(words[0]) <= 3 and len(words[1]) >= 5:
            aliases.append(words[1])
    return any(re.search(rf"(?<!\w){re.escape(alias)}(?!\w)", title, re.I) for alias in aliases)


def discover_headlines(ticker, client, cutoff, issuer_name=None, sector_terms=()):
    feed = "https://feeds.finance.yahoo.com/rss/2.0/headline?" + urlencode({"s": ticker, "region": "US", "lang": "en-US"})
    body, collected = client.text(feed)
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError as exc:
        raise ValueError("Invalid RSS document") from exc
    channel = root.find("./channel")
    if root.tag.lower() != "rss" or channel is None:
        raise ValueError("Expected a valid RSS channel")
    result = []
    for item in channel.findall("./item"):
        title = item.findtext("title", "").strip()
        url = item.findtext("link", "").strip()
        summary = " ".join(source_blocks(item.findtext("description", "")))
        if (urlsplit(url).scheme != "https" or not title or not summary
                or not relevant_headline(title, ticker, issuer_name, sector_terms) or
                not re.search(r"earnings|guidance|appoint|resign|contract|merger|deal|regulator|recall|investigat|product|trial|tariff", title, re.I)):
            continue
        try:
            stamp = parsedate_to_datetime(item.findtext("pubDate", ""))
        except ValueError:
            continue
        if stamp.tzinfo is None or not cutoff - timedelta(days=7) <= stamp <= cutoff:
            continue
        result.append({"ticker": ticker, "kind": "independent_headline", "url": url,
                       "published_at": as_utc(stamp), "title": title[:240], "summary": summary[:550],
                       "feed_url": feed, "collected_at": collected})
        if len(result) == 2:
            break
    return result


def enrich_events(captured, client, manifest, *, discover_news=False):
    cutoff_text = captured["as_of"]
    cutoff = parse_timestamp(cutoff_text)
    snapshots = {ticker: fixture for ticker, (_, fixture) in captured["snapshots"].items()}
    discovery = captured["discovery"]
    candidates = {pick["ticker"] for result in (discovery or {}).get("horizons", {}).values()
                  for pick in result["shortlist"]}
    ticker_set = snapshots.keys() | candidates
    sources = {ticker: list(fixture["evidence"]) for ticker, fixture in snapshots.items()}
    names = {ticker: next((item["issuer_name"] for item in items if item.get("issuer_name")), None)
             for ticker, items in sources.items()}
    for ticker in candidates:
        items = discovery["evidence"]
        seat = next(row for row in discovery["sector_seats"] if row["ticker"] == ticker)
        sector = seat["sector"]
        names[ticker] = seat.get("name")
        ids = (f"price-{ticker.lower()}", "price-spy", f"price-{sector.lower()}",
               f"sec-{ticker.lower()}-quarter", f"sec-{ticker.lower()}-annual")
        sources[ticker] = [items[key] for key in ids if key in items]
    plans, diagnostics = select_manifest(manifest, ticker_set, cutoff)
    company_plans = fetch_many(sorted(ticker_set), lambda ticker: sec_plans(client, ticker, sources[ticker], cutoff))
    for ticker, (rows, error) in company_plans.items():
        if rows:
            for plan in rows:
                if plan["url"]:
                    plans.append(plan)
                else:
                    diagnostics.append({**plan, "status": "unavailable", "error": "Primary document unavailable"})
        status = "failed" if error else "available" if rows else "complete_zero" if rows == [] else "not_run"
        diagnostics.append({"ticker": ticker, "kind": "sec_8k_lookup", "status": status,
                            "error": error})
    if discover_news:
        def lookup(ticker):
            terms = ("semiconductor", "chip stocks", "chipmakers") if any(
                item["id"] == SOXL_TERMS for item in sources[ticker]
            ) else ()
            return discover_headlines(ticker, client, cutoff, names[ticker], terms)

        for ticker, (rows, error) in fetch_many(sorted(ticker for ticker in ticker_set if not ticker.isdigit()), lookup).items():
            if rows:
                plans.extend(rows)
            diagnostics.append({"ticker": ticker, "kind": "headline_lookup",
                                "status": "failed" if error else "available" if rows else "complete_zero",
                                "error": error})

    def fetch_event(index):
        plan = plans[index]
        kind = plan["kind"]
        body_error = None
        if kind == "independent_headline":
            title, excerpt = plan["title"], plan["summary"]
            event_body, collected = excerpt, plan["collected_at"]
            try:
                event_body, collected = client.text(plan["url"])
                title, excerpt = news_excerpt(event_body, re.escape(title))
                kind = "independent_news"
            except (OSError, ValueError) as exc:
                body_error = str(exc)
                event_body, collected = excerpt, plan["collected_at"]
        else:
            event_body, collected = client.text(plan["url"], sec=kind == "sec_8k")
        if kind == "sec_8k":
            excerpt = filing_excerpt(event_body, plan["item_codes"])
            title = ", ".join(MATERIAL_ITEMS[code] for code in plan["item_codes"])
        elif plan["kind"] != "independent_headline":
            title, excerpt = news_excerpt(event_body, plan["title_pattern"])
        ticker = canonical_ticker(plan["ticker"])
        prices = {item.get("market_symbol"): item for item in sources[ticker] if "observations" in item}
        market = "KOSPI" if ticker.isdigit() else "SPY"
        sector = next((item for symbol, item in prices.items() if symbol not in {ticker, market}), None)
        basis = {"sec_8k": "sec_acceptance_timestamp", "independent_headline": "feed_publication_timestamp"}.get(
            plan["kind"], "manifest_exact_publication" if plan.get("availability_basis") == "verified_exact_timestamp"
            or plan.get("time_basis") == "exact"
            else "manifest_date_upper_bound",
        )
        reaction = None
        if basis != "manifest_date_upper_bound" and kind != "independent_headline":
            reaction = aligned_reaction(plan["published_at"], prices.get(ticker), prices.get(market), cutoff_text, sector)
        fingerprint = sha256(plan["url"].encode()).hexdigest()[:12]
        return record(
            f"{EVENT_PREFIX}{ticker.lower()}-{fingerprint}", plan["url"], collected,
            ticker=ticker, category="event_context", title=title, text=excerpt,
            source_kind=kind, observed_at=plan["published_at"],
            content_basis="headline_and_feed_summary_only" if kind == "independent_headline" else "fetched_full_document",
            **({"article_body_error": body_error} if body_error else {}),
            published_at=None if basis == "manifest_date_upper_bound" else plan["published_at"],
            available_at=as_utc(parse_timestamp(plan["published_at"])),
            **({"observation_date": parse_timestamp(plan["published_at"]).date().isoformat()}
               if basis != "manifest_date_upper_bound" else {}),
            availability_basis=basis, body_sha256=sha256(event_body.encode()).hexdigest(), reaction=reaction,
            **{key: value for key, value in plan.items() if key in {"item_codes", "accession", "subject", "feed_url"}},
        )
    seen = set()
    for index, (item, error) in fetch_many(range(len(plans)), fetch_event).items():
        plan = plans[index]
        diagnostics.append({**{key: value for key, value in plan.items() if key != "collected_at"},
                            "status": "available" if item else "unavailable", "error": error,
                            **({"evidence_id": item["id"], "collected_at": item["collected_at"],
                                "content_basis": item["content_basis"],
                                "article_body_error": item.get("article_body_error")} if item else {})})
        if not item or item["id"] in seen:
            continue
        seen.add(item["id"])
        ticker = item["ticker"]
        if ticker in snapshots:
            snapshots[ticker]["evidence"].append(item)
        if ticker in candidates:
            discovery["evidence"][item["id"]] = item
    captured["event_collection"] = diagnostics
    if discovery is not None:
        discovery["events_requested"] = True


def reaction_sentence(reaction):
    if reaction is None:
        return "공개 전후를 비교할 수 있는 같은 날짜의 확정 일봉이 부족했습니다."
    r = reaction
    volume = r["volume_vs_pre_event_session"]
    return (
        f"{r['before']} → {r['after']} 종가 기준 종목 {r['stock_pct']:+.2f}%, "
        f"{r['benchmark_ticker']} {r['benchmark_pct']:+.2f}%, 차이 {r['excess_pp']:+.2f}%p"
        + (f", {r['sector_ticker']} {r['sector_pct']:+.2f}%" if "sector_pct" in r else "")
        + (f", 거래량은 직전 비교일의 {volume:.2f}배" if volume is not None else "")
        + ". 같은 구간의 관측치로, 이벤트의 영향은 분리하지 못했습니다."
    )
