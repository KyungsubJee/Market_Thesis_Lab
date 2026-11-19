"""Collect bounded recent headlines and public posts through permitted source interfaces."""

from datetime import datetime, timedelta, timezone
from collections import deque
from email.utils import parsedate_to_datetime
from hashlib import sha256
from html import unescape
import json
import os
import re
from urllib.error import HTTPError
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen
from xml.etree import ElementTree

from framework_validation.runner import parse_timestamp

from .instruments import KNOWN_NAMES, profile


LOOKBACK = timedelta(days=7)
MAX_RESPONSE_BYTES = 2_000_000
MAX_PROVIDER_ROWS = 35
TRACKING_KEYS = {"fbclid", "gclid", "cid", "ref", "cmpid"}
UNDERLYING = {
    "BOIL": ("natural gas", r"natural gas|henry hub"),
    "KOLD": ("natural gas", r"natural gas|henry hub"),
    "SOXL": ("semiconductor stocks", r"semiconductor (?:stocks|index|etf)|chip stocks"),
    "SOXS": ("semiconductor stocks", r"semiconductor (?:stocks|index|etf)|chip stocks"),
    "498400": ("코스피200", r"코스피\s*200|kospi\s*200"),
    "486290": ("나스닥100", r"나스닥\s*100|nasdaq[ -]?100"),
}
AUTOMATED_AUTHOR = re.compile(
    r"(?:^|[.\s_-])(?:bot|newsbot|news|alerts?|feed|terminal|rss|liquidations?)(?:$|[.\s_-])|"
    r"(?:^|[.\s_-])[\w-]+bot(?:$|[.\s_-])|(?:^|[.\s_-])(?:ai|agent)[\s_.-]*brief|"
    r"(?:data|finance|market|stock|quant)[\s_.-]*(?:insight|digest)[\s_.-]*(?:lab|hub|feed|news)?|"
    r"(?:insider|stock|market|trade|trading|quant|crypto)[\s_.-]*(?:dashboard|scanner|handbook|signals?|hub|feed)|"
    r"voice[\s_.-]*of[\s_.-]*(?:chain|markets?|stocks?|crypto)", re.I,
)
PROMOTION = re.compile(
    r"🤖|^(?:breaking|stock update|earnings alert|options alert):|(?:join|subscribe) (?:my|our)|"
    r"trade signal|webinar|텔레그램|(?:종목|주식)\s*추천|보유자\s*필독|무료방|가입하기", re.I,
)
OWN_PUBLICATION = re.compile(r"\b(?:our|my) (?:latest|new) (?:article|newsletter|report|video)\b|"
                             r"\bi wrote (?:an|this|about)\b", re.I)
URL_TOKEN = re.compile(r"https?://\S+|(?<![\w@])(?:[\w-]+\.)+[A-Za-z]{2,}/\S*", re.I)
FORMATTED_ALERT = re.compile(
    r"^\W{0,5}(?:liquidation|unusual options|insider trading|price|volume|flow)\s+(?:alerts?|update)\b|"
    r"\b(?:entry|target|stop[- ]loss|exit)\s*:\s*\$?\d.*\b(?:entry|target|stop[- ]loss|exit)\s*:\s*\$?\d|"
    r"\b(?:bullish|bearish|buy|sell)\s+alert\b|\b(?:monitoring|track)\s+\d+\s+(?:tickers|symbols)\b|"
    r"\b\d+\s+(?:updates?|signals?|alerts?)\s+(?:in|over)\s+(?:the last\s+)?\d+\s+(?:hours?|days?|weeks?)\b", re.I,
)
QUOTE_RELAY = re.compile(r"""^[“‘"'「『].+[”’"'」』](?:\s+https?://\S+)?$""")
OPINION_CUE = re.compile(
    r"\b(?:i (?:think|believe|expect|would|prefer|see|worry|hold|own|bought|sold|added|trimmed|went long|went short)|"
    r"my (?:view|thesis|position|take)|i['’](?:m|d) (?:buying|selling|holding|wait|trim|hold|avoid|long|short)|"
    r"looks? (?:overvalued|undervalued|risky)|seems (?:priced|expensive|cheap)|"
    r"priced in|i disagree|would (?:buy|hold|trim|avoid)|don['’]t chase)\b|"
    r"제 생각|내 생각|제가 보기엔|판단합니다|보유 중|매수했|매도했|비중을 (?:늘렸|줄였)|생각합니다|우려됩니다", re.I,
)
FINANCE_WORD = re.compile(r"stock|fund|shares?|price|market|earnings|investment|investor|"
                          r"주가|시장|실적|투자|공시|배당|매출", re.I)


class SourceClient:
    def get(self, url, *, headers=None):
        request = Request(url, headers={"User-Agent": "Market Thesis Lab/0.1", "Accept": "*/*"})
        for key, value in (headers or {}).items():
            # API credentials must not follow HTTP redirects.
            request.add_unredirected_header(key, value)
        with urlopen(request, timeout=8) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            raise ValueError("Response exceeded the source-size limit")
        return body.decode("utf-8-sig"), datetime.now(timezone.utc)


def clean_text(value, max_length):
    if not isinstance(value, str):
        return ""
    return " ".join(re.sub(r"<[^>]+>", " ", unescape(value)).split())[:max_length]


def get_time(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        return parse_timestamp(value).astimezone(timezone.utc)
    except ValueError:
        try:
            stamp = parsedate_to_datetime(value)
            return stamp.astimezone(timezone.utc) if stamp.tzinfo is not None else None
        except (ValueError, OverflowError):
            return None


def valid_url(link):
    if not isinstance(link, str) or any(char.isspace() or char in "<>" for char in link):
        return False
    host = urlsplit(link)
    return host.scheme == "https" and bool(host.hostname) and host.username is None


def result_row(title, link, published, *, text="", publisher=None, author=None, basis="provider_publication_time",
               available=None, observed=None, link_scope="publisher"):
    return {
        "title": clean_text(title, 260), "text": clean_text(text, 500), "source_url": link,
        "published_at": published, "available_at": available or published, "observed_at": observed or published,
        "availability_basis": basis, "publisher": clean_text(publisher, 120) or None,
        "author": clean_text(author, 100) or None, "link_scope": link_scope,
    }


def rss_news(body, provider):
    root = ElementTree.fromstring(body)
    channel = root.find("channel")
    if root.tag != "rss" or channel is None:
        raise ValueError("Expected a news RSS channel")
    results = []
    for item in channel.findall("item")[:MAX_PROVIDER_ROWS]:
        link = item.findtext("link")
        publisher = item.findtext("source")
        scope = "aggregator"
        if provider == "bing_news_rss" and isinstance(link, str):
            index_link = urlsplit(link)
            target = dict(parse_qsl(index_link.query)).get("url") if index_link.hostname == "www.bing.com" else link
            if not valid_url(target):
                continue
            link, scope = target, "publisher"
            publisher = publisher or next(
                (node.text for node in item if node.tag.rsplit("}", 1)[-1].lower() == "source"), None
            ) or urlsplit(link).hostname
        elif provider == "yahoo_finance_rss":
            publisher = publisher or "Yahoo Finance"
        basis = "google_news_feed_timestamp" if provider == "google_news_rss" else f"{provider}_timestamp"
        results.append(result_row(item.findtext("title"), link, get_time(item.findtext("pubDate")),
                                  publisher=publisher, basis=basis, link_scope=scope))
    return results


def google_news(body):
    return rss_news(body, "google_news_rss")


def brave_news(body):
    rows = json.loads(body)["results"]
    return [
        result_row(row["title"], row["url"], None, text=row.get("description"), observed=get_time(row.get("page_age")),
                   available=max(page_date, fetched_at) if (page_date := get_time(row.get("page_age")))
                             and (fetched_at := get_time(row.get("page_fetched"))) else None,
                   publisher=(row.get("profile") or {}).get("name"), basis="brave_page_date_and_fetch_time")
        for row in rows[:MAX_PROVIDER_ROWS]
    ]


def naver_search(body, cafe):
    items = json.loads(body)["items"]
    results = []
    for row in items[:MAX_PROVIDER_ROWS]:
        original = not cafe and valid_url(row.get("originallink"))
        # NAVER's café index normally does not supply a publication date.
        results.append(result_row(
            row["title"], row.get("originallink") if original else row["link"], get_time(row.get("pubDate")),
            text=row.get("description"), publisher=row.get("cafename"),
            basis="naver_search_result_timestamp",
            link_scope="publisher" if original else "permalink" if cafe else "provider_link",
        ))
    return results


def bluesky_url(uri):
    parts = re.fullmatch(r"at://([A-Za-z0-9.:-]+)/app\.bsky\.feed\.post/([A-Za-z0-9_-]+)", uri or "")
    return f"https://bsky.app/profile/{quote(parts[1], safe=':')}/post/{parts[2]}" if parts else None


def bluesky_shared_context(post, record):
    article_url = article_title = quoted_url = quoted_text = None
    for embed in (post.get("embed") or {}, record.get("embed") or {}):
        external = (embed.get("media") or embed).get("external") or {}
        if not article_url and valid_url(external.get("uri")):
            article_url = external["uri"]
            article_title = clean_text(external.get("title"), 260)
        quote_view = embed.get("record") or {}
        quote_record = quote_view.get("record") or quote_view
        if not quoted_url and (link := bluesky_url(quote_record.get("uri"))):
            quoted_url = link
            quoted_text = clean_text((quote_record.get("value") or {}).get("text"), 260)
    if not article_url:
        for facet in record.get("facets", []):
            for feature in facet.get("features", []):
                if valid_url(feature.get("uri")):
                    article_url = feature["uri"]
                    break
            if article_url:
                break
    if not article_url and (link := re.search(r"https://[^\s<>]+", record.get("text") or "")):
        candidate = link[0].rstrip(".,;!?)]")
        if valid_url(candidate):
            article_url = candidate
    return article_url, article_title, quoted_url, quoted_text


def bluesky_posts(body):
    posts = json.loads(body)["posts"]
    results = []
    for post in posts[:MAX_PROVIDER_ROWS]:
        uri = bluesky_url(post.get("uri"))
        if not uri:
            continue
        record = post["record"]
        text = clean_text(record.get("text"), 500)
        created = get_time(record.get("createdAt"))
        indexed = get_time(post.get("indexedAt"))
        if not created or not indexed:
            created = None  # A user-controlled creation timestamp alone does not establish availability.
        article_url, headline, quoted_url, quoted_text = bluesky_shared_context(post, record)
        author = post.get("author") or {}
        handle = (author.get("handle") or "").lower()
        actor = " ".join(filter(None, (handle, author.get("displayName"))))
        article_host = urlsplit(article_url).hostname if article_url else None
        publisher_link = bool(handle and article_host and (article_host == handle or article_host.endswith("." + handle)))
        author_text = URL_TOKEN.sub("", text).strip()
        comparison = re.sub(r"\W+", "", author_text.casefold())
        title_key = re.sub(r"\W+", "", (headline or "").casefold())
        article_reference = bool(article_url and (headline or re.search(
            r"/(?:articles?|stories|news|markets|posts?|p|view)/", urlsplit(article_url).path, re.I,
        )))
        if article_url and not article_reference and not quoted_url and not OPINION_CUE.search(author_text):
            continue  # A bare ticker/chart/profile link is not evidence of an article share.
        is_share = (article_reference or bool(quoted_url)) and (
            not OPINION_CUE.search(author_text) or bool(title_key and title_key == comparison) or AUTOMATED_AUTHOR.search(actor)
            or publisher_link or OWN_PUBLICATION.search(author_text)
        )
        title = (headline or quoted_text or text) if is_share else text
        results.append(result_row(
            title, uri, created, text=text,
            publisher="Bluesky", author=author.get("handle"),
            available=max(created, indexed) if created else None,
            basis="bluesky_created_and_indexed_time", link_scope="permalink",
        ))
        results[-1]["author_display"] = clean_text(author.get("displayName"), 100)
        results[-1].update(community_subtype="article_share" if is_share else "investor_commentary",
                           external_article_url=article_url, external_article_title=headline, quoted_post_url=quoted_url)
    return results


def reddit_posts(body):
    rows = json.loads(body)["data"]["children"]
    results = []
    for row in rows[:MAX_PROVIDER_ROWS]:
        post = row["data"]
        path = post.get("permalink", "")
        if not path.startswith("/r/") or post.get("over_18") or post.get("selftext") in {"[deleted]", "[removed]"}:
            continue
        stamp = post.get("created_utc")
        created = datetime.fromtimestamp(stamp, timezone.utc) if isinstance(stamp, (int, float)) and not isinstance(stamp, bool) else None
        results.append(result_row(
            post.get("title"), "https://www.reddit.com" + path, created, text=post.get("selftext"),
            publisher="Reddit", author=post.get("author"), basis="reddit_created_utc", link_scope="permalink",
        ))
    return results


def stocktwits_posts(body):
    rows = json.loads(body)["messages"]
    results = []
    for row in rows[:MAX_PROVIDER_ROWS]:
        username = (row.get("user") or {}).get("username", "")
        if type(row.get("id")) is not int or not re.fullmatch(r"[A-Za-z0-9_.-]+", username):
            continue
        results.append(result_row(row.get("body"), f"https://stocktwits.com/{username}/message/{row['id']}",
                                  get_time(row.get("created_at")), text=row.get("body"), publisher="Stocktwits",
                                  author=username, basis="stocktwits_created_at", link_scope="permalink"))
    return results


def search_name(ticker, company_name):
    instrument = profile(ticker)
    title = company_name
    if not title or title.casefold() == ticker.casefold():
        title = instrument.name if instrument else KNOWN_NAMES.get(ticker, "")
    title = clean_text(title, 100)
    title = re.sub(r"\s*(?:-\s*)?(?:Class [A-Z] )?Common Stock$", "", title, flags=re.I)
    title = re.sub(r"[, ]+(?:Inc\.?|Corporation|Corp\.?|PLC|Ltd\.?)$", "", title, flags=re.I)
    return title.replace('"', "").strip() or ticker


def request_specs(ticker, region, name, cutoff, limit, env):
    start = cutoff - LOOKBACK
    span = f"{start.date()}to{cutoff.date()}"
    params = {"q": name, "count": limit * 4, "country": region, "freshness": span}
    if env is not None and region == "US":
        brave_key = env.get("BRAVE_SEARCH_API_KEY")
        yield "brave_news", "news", "https://api.search.brave.com/res/v1/news/search?" + urlencode(params), (
            {"X-Subscription-Token": brave_key} if brave_key else None), brave_news
    elif env is not None:
        naver_id = env.get("NAVER_API_HUB_CLIENT_ID") or env.get("NAVER_SEARCH_CLIENT_ID")
        naver_secret = env.get("NAVER_API_HUB_CLIENT_SECRET") or env.get("NAVER_SEARCH_CLIENT_SECRET")
        naver_auth = {"X-NCP-APIGW-API-KEY-ID": naver_id, "X-NCP-APIGW-API-KEY": naver_secret} if naver_id and naver_secret else None
        for kind, category in (("news", "news"), ("cafe", "community")):
            path = "news" if kind == "news" else "cafearticle"
            url = f"https://naverapihub.apigw.ntruss.com/search/v1/{path}?" + urlencode({
                "query": name, "display": limit * 4, "sort": "date",
            })
            yield f"naver_{kind}", category, url, naver_auth, lambda body, cafe=kind == "cafe": naver_search(body, cafe)
    lang = "ko" if region == "KR" else "en"
    query = f'"{name}"'
    if exposure := UNDERLYING.get(ticker):
        query = f'({query} OR "{exposure[0]}")'
    query += f" after:{start.date()} before:{(cutoff + timedelta(days=1)).date()}"
    url = "https://news.google.com/rss/search?" + urlencode({
        "q": query, "hl": f"{lang}-{region}", "gl": region, "ceid": f"{region}:{lang}",
    })
    yield "google_news_rss", "news", url, {}, google_news
    url = "https://api.bsky.app/xrpc/app.bsky.feed.searchPosts?" + urlencode({
        "q": f"${ticker}" if region == "US" else name, "sort": "latest",
        "since": start.isoformat(), "until": cutoff.isoformat(), "limit": max(20, limit * 4),
    })
    yield "bluesky", "community", url, {}, bluesky_posts
    if env is not None and region == "US":
        reddit_token, reddit_agent = env.get("REDDIT_ACCESS_TOKEN"), env.get("REDDIT_USER_AGENT")
        url = "https://oauth.reddit.com/search?" + urlencode({
            "q": f'"{ticker}" OR "{name}"', "sort": "new", "limit": limit * 4, "restrict_sr": "false",
        })
        yield "reddit", "community", url, ({"Authorization": f"Bearer {reddit_token}", "User-Agent": reddit_agent}
                                          if reddit_token and reddit_agent else None), reddit_posts
        stocktwits_key, template = env.get("STOCKTWITS_ACCESS_TOKEN"), env.get("STOCKTWITS_STREAM_URL")
        url = template.replace("{ticker}", quote(ticker)) if template and "{ticker}" in template else ""
        permitted = valid_url(url) and urlsplit(url).netloc == "api.stocktwits.com" and not urlsplit(url).query
        auth = {"Authorization": f"Bearer {stocktwits_key}"} if stocktwits_key and permitted else None
        if permitted:
            url += "?" + urlencode({"limit": limit * 4})
        yield "stocktwits", "community", url, auth, stocktwits_posts


def rss_fallback(ticker, region, name):
    if region == "KR":
        provider = "bing_news_rss"
        # Bing's Korean RSS returned no results for compound OR queries on these ETF names.
        query = "나스닥100" if ticker == "486290" else name
        url = "https://www.bing.com/news/search?" + urlencode({
            "q": query, "format": "rss", "setlang": "ko-kr", "mkt": "ko-KR",
        })
    else:
        provider = "yahoo_finance_rss"
        symbol = "UNG" if ticker in {"BOIL", "KOLD"} else ticker
        url = "https://feeds.finance.yahoo.com/rss/2.0/headline?" + urlencode({
            "s": symbol, "region": "US", "lang": "en-US",
        })
    return provider, "news", url, {}, lambda body: rss_news(body, provider)


def canonical_url(url):
    parsed = urlsplit(url)
    params = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
              if not key.startswith("utm_") and key not in TRACKING_KEYS]
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/"), urlencode(sorted(params)), ""))


def relevance(row, ticker, name, kind, provider):
    content = URL_TOKEN.sub("", f"{row['title']} {row['text']}")
    if PROMOTION.search(content):
        return None
    if kind == "community":
        actor = " ".join(filter(None, (row.get("author"), row.get("author_display"))))
        words = re.sub(r"\$[A-Za-z0-9.-]+", "", URL_TOKEN.sub("", row["text"]))
        is_share = row.get("community_subtype") == "article_share"
        if (FORMATTED_ALERT.search(row["text"]) or not is_share and
                (AUTOMATED_AUTHOR.search(actor) or QUOTE_RELAY.fullmatch(row["text"])
                 or len(re.findall(r"\w+", words)) < 7 or provider == "bluesky" and not OPINION_CUE.search(words))):
            return None
    cashtag = re.search(rf"(?<!\w)\${re.escape(ticker)}(?![\w.-])", content, re.I)
    right_boundary = "" if re.search("[가-힣]", name) else r"(?=\W|$)"
    named = name.casefold() != ticker.casefold() and re.search(
        rf"(?<!\w){re.escape(name)}{right_boundary}", content, re.I,
    )
    ticker_is_distinct = len(ticker) >= 3 and ticker not in {"BOIL", "KOLD"}
    marked = ticker_is_distinct and re.search(rf"(?<!\w){re.escape(ticker)}(?![\w.-])", content)
    if cashtag or named or (marked and FINANCE_WORD.search(content)):
        return "security"
    if (kind == "news" and (theme := UNDERLYING.get(ticker)) and re.search(theme[1], row["title"], re.I)
            and not re.search(r"options? alert|unusual options|options? volume|(?:ETF|펀드).{0,25}(?:출시|상장|순자산)|담아라",
                              row["title"], re.I)):
        return "underlying"
    return None


def collect_qualitative_sources(ticker, *, cutoff, region="US", company_name=None, limit=3, client=None,
                               environ=None, allow_authenticated=False):
    """Return seven days of public results; approved API requests require explicit opt-in."""
    ticker, region = ticker.strip().upper(), region.upper()
    if not re.fullmatch(r"[A-Z0-9.\-]{1,15}", ticker) or region not in {"US", "KR"} or type(limit) is not int or not 1 <= limit <= 5:
        raise ValueError("Expected a ticker, region US/KR and limit 1-5")
    cutoff = parse_timestamp(cutoff) if isinstance(cutoff, str) else cutoff
    if cutoff.utcoffset() is None:
        raise ValueError("Qualitative cutoff must have a timezone")
    cutoff = cutoff.astimezone(timezone.utc)
    client = SourceClient() if client is None else client
    credentials = (os.environ if environ is None else environ) if allow_authenticated else None
    name = search_name(ticker, company_name)
    result = {"items": [], "sources": [],
              "collection_mode": "public_and_authenticated" if allow_authenticated else "public_only"}
    seen_urls, seen_headlines = set(), set()
    requests = deque(request_specs(ticker, region, name, cutoff, limit, credentials))
    while requests:
        provider, kind, url, headers, parser = requests.popleft()
        source = {"provider": provider, "kind": kind, "count": 0, "raw_count": 0, "status": "authentication_required"}
        result["sources"].append(source)
        if provider in {"yahoo_finance_rss", "bing_news_rss"}:
            source.update(fallback_for="google_news_rss", fallback_reason="http_429")
        if headers is None:
            continue
        source.update(query_url=url, window_start=(cutoff - LOOKBACK).isoformat(), cutoff=cutoff.isoformat())
        try:
            body, collected_at = client.get(url, headers=headers)
            rows = parser(body)
        except HTTPError as exc:
            source.update(status="unavailable", error=f"HTTP {exc.code}", http_status=exc.code)
            if provider == "google_news_rss" and exc.code == 429:
                requests.appendleft(rss_fallback(ticker, region, name))
            exc.close()
            continue
        except (OSError, ValueError, TypeError, KeyError, OverflowError, AttributeError, ElementTree.ParseError) as exc:
            source.update(status="unavailable", error=type(exc).__name__)
            continue
        source.update(raw_count=len(rows), unverified_time_count=0, duplicate_count=0, discarded_count=0,
                      collected_at=collected_at.astimezone(timezone.utc).isoformat())
        eligible = []
        for row in rows:
            stamp = row["available_at"]
            if not row["title"] or not valid_url(row["source_url"]):
                source["discarded_count"] += 1
            elif provider == "naver_cafe" and (not stamp or not row["observed_at"]):
                source["unverified_time_count"] += 1
            elif not (scope := relevance(row, ticker, name, kind, provider)):
                source["discarded_count"] += 1
            elif not stamp or not row["observed_at"]:
                source["unverified_time_count"] += 1
            elif not cutoff - LOOKBACK <= row["observed_at"] <= stamp <= min(cutoff, collected_at):
                source["discarded_count"] += 1
            else:
                row["coverage_scope"] = scope
                eligible.append(row)
        source["status"] = "available" if eligible else "unverified_time" if source["unverified_time_count"] else "searched_zero"
        source["eligible_count"] = len(eligible)
        if provider == "bluesky":
            source.update(article_share_count=0, commentary_count=0)
        for row in sorted(eligible, key=lambda item: (
            item.get("community_subtype") == "investor_commentary", item["observed_at"],
        ), reverse=True):
            normalized = canonical_url(row["source_url"])
            title = row["title"]
            if kind == "news" and row["publisher"] and title.endswith(" - " + row["publisher"]):
                title = title.removesuffix(" - " + row["publisher"])
            headline = re.sub(r"\W+", "", title.casefold())
            if normalized in seen_urls or (kind == "news" and headline in seen_headlines):
                source["duplicate_count"] += 1
                continue
            subtype_count = {
                "article_share": "article_share_count", "investor_commentary": "commentary_count",
            }.get(row.get("community_subtype"), "count")
            if source["count"] >= limit:
                break
            seen_urls.add(normalized)
            if kind == "news":
                seen_headlines.add(headline)
            identity = sha256(normalized.encode()).hexdigest()[:14]
            for key in ("published_at", "available_at", "observed_at"):
                row[key] = row[key].isoformat() if row[key] else None
            result["items"].append({
                **row, "id": f"qualitative-{ticker.lower()}-{identity}", "title": title,
                "ticker": ticker, "kind": kind, "provider": provider, "collected_at": source["collected_at"],
                "source_kind": "news_index_result" if kind == "news" else "public_community_post",
            })
            source["count"] += 1
            if subtype_count != "count":
                source[subtype_count] += 1
    return result
