from datetime import datetime
import json
from urllib.error import HTTPError
from urllib.parse import urlsplit
import unittest

from research_prototype.qualitative import collect_qualitative_sources
from research_prototype.qualitative import relevance


CUTOFF = "2026-11-18T00:00:00Z"


class FakeClient:
    def __init__(self, feed):
        self.feed = feed
        self.hosts = []

    def get(self, url, *, headers=None):
        host = urlsplit(url).hostname
        self.hosts.append(host)
        payload = self.feed if host == "news.google.com" else (
            '{"posts":[{"uri":"at://did:plc:example/app.bsky.feed.post/abc",'
            '"indexedAt":"2026-11-18T00:00:01Z","author":{"handle":"example.test"},'
            '"record":{"text":"$NVDA trend","createdAt":"2026-11-17T18:00:00Z"}}]}'
        )
        return payload, datetime.fromisoformat("2026-11-18T01:00:00Z")


class QualitativeTests(unittest.TestCase):
    def test_google_429_recovers_from_the_market_fallback(self):
        class RateLimitedClient:
            def __init__(self, fallback):
                self.fallback = fallback
                self.hosts = []

            def get(self, url, *, headers=None):
                host = urlsplit(url).hostname
                self.hosts.append(host)
                if host == "news.google.com":
                    raise HTTPError(url, 429, "Too Many Requests", None, None)
                if host == "api.bsky.app":
                    return '{"posts":[]}', datetime.fromisoformat("2026-11-18T01:00:00Z")
                return self.fallback, datetime.fromisoformat("2026-11-18T01:00:00Z")

        for ticker, region, host, provider, headline, indexed_link in (
            ("NVDA", "US", "feeds.finance.yahoo.com", "yahoo_finance_rss",
             "NVIDIA earnings forecast rises", "https://finance.yahoo.com/news/nvda-earnings.html"),
            ("005930", "KR", "www.bing.com", "bing_news_rss", "삼성전자 실적 발표",
             "http://www.bing.com/news/apiclick.aspx?url=https%3A%2F%2Fwww.yna.co.kr%2Fview%2FAKR123"),
        ):
            with self.subTest(region=region):
                feed = (f"<rss><channel><item><title>{headline}</title><link>{indexed_link}</link>"
                        "<pubDate>Tue, 17 Nov 2026 10:00:00 GMT</pubDate></item></channel></rss>")
                client = RateLimitedClient(feed)
                result = collect_qualitative_sources(ticker, cutoff=CUTOFF, region=region, client=client, environ={})
                self.assertEqual(client.hosts, ["news.google.com", host, "api.bsky.app"])
                self.assertEqual(result["sources"][0]["http_status"], 429)
                self.assertEqual(result["sources"][1]["fallback_for"], "google_news_rss")
                self.assertEqual(result["sources"][1]["fallback_reason"], "http_429")
                self.assertEqual(result["items"][0]["provider"], provider)
                if region == "KR":
                    self.assertEqual(result["items"][0]["source_url"], "https://www.yna.co.kr/view/AKR123")

    def test_bluesky_article_share_and_authored_commentary_remain_distinct(self):
        headline = "NVIDIA reports accelerating data center sales and raises quarterly outlook"
        embed = {"$type": "app.bsky.embed.external", "external": {"title": headline,
                 "uri": "https://publisher.example/nvidia", "description": "Results release."}}
        def post(key, text, author):
            return {"uri": f"at://did:plc:example/app.bsky.feed.post/{key}", "indexedAt": "2026-11-17T20:01:00Z",
                    "author": {"handle": author}, "record": {"text": text, "createdAt": "2026-11-17T20:00:00Z",
                                                            "embed": embed}}
        posts = [post("share", headline, "publisher-news.bsky.social"),
                 post("comment", "$NVDA I think the raised outlook is already priced in given current valuation.",
                      "megan.bsky.social"),
                 post("self", "$NVDA I think the raised outlook is priced in. Read my latest article for details.",
                      "publisher.example")]
        class Client(FakeClient):
            def get(self, url, *, headers=None):
                if urlsplit(url).hostname == "api.bsky.app":
                    return json.dumps({"posts": posts}), datetime.fromisoformat("2026-11-18T01:00:00Z")
                return super().get(url, headers=headers)
        result = collect_qualitative_sources("NVDA", cutoff=CUTOFF, client=Client("<rss><channel/></rss>"), environ={})
        self.assertEqual([row["community_subtype"] for row in result["items"]],
                         ["investor_commentary", "article_share", "article_share"])
        self.assertEqual(result["items"][1]["external_article_url"], "https://publisher.example/nvidia")
        source = result["sources"][1]
        self.assertEqual((source["article_share_count"], source["commentary_count"]), (2, 1))
        limited = collect_qualitative_sources("NVDA", cutoff=CUTOFF, limit=1,
                                              client=Client("<rss><channel/></rss>"), environ={})
        self.assertEqual([row["community_subtype"] for row in limited["items"]], ["investor_commentary"])
        self.assertEqual(limited["sources"][1]["count"], 1)

    def test_post_subject_and_own_opinion_use_text_without_urls(self):
        text = "I think $AMZN looks expensive. https://publisher.example/story?query=AAPL"
        self.assertIsNone(relevance({"title": text, "text": text, "author": "megan.bsky.social"},
                                    "AAPL", "Apple", "community", "bluesky"))
        text = "I’m long $SOXS right now at $59.63. Stop set at $60.85 but will adjust."
        self.assertEqual(relevance({"title": text, "text": text, "author": "megan.bsky.social"},
                                    "SOXS", "SOXS", "community", "bluesky"), "security")

    def test_default_runs_public_requests_even_when_credentials_exist(self):
        env = {
            "BRAVE_SEARCH_API_KEY": "test-brave", "REDDIT_ACCESS_TOKEN": "test-reddit",
            "REDDIT_USER_AGENT": "test-agent", "STOCKTWITS_ACCESS_TOKEN": "test-stocktwits",
            "STOCKTWITS_STREAM_URL": "https://api.stocktwits.com/api/2/streams/symbol/{ticker}.json",
            "NAVER_API_HUB_CLIENT_ID": "test-id", "NAVER_API_HUB_CLIENT_SECRET": "test-naver",
        }
        for ticker, region in (("NVDA", "US"), ("005930", "KR")):
            with self.subTest(region=region):
                client = FakeClient("<rss><channel/></rss>")
                result = collect_qualitative_sources(ticker, cutoff=CUTOFF, region=region,
                                                     client=client, environ=env)
                self.assertEqual(client.hosts, ["news.google.com", "api.bsky.app"])
                self.assertEqual(result["collection_mode"], "public_only")
                self.assertEqual({row["provider"] for row in result["sources"]}, {"google_news_rss", "bluesky"})

    def test_search_deduplicates_and_does_not_admit_after_cutoff_records(self):
        articles = "".join(
            f"<item><title>Nvidia {title}</title><link>https://news.google.com/rss/articles/{url}</link>"
            f"<pubDate>{date}</pubDate><source>Example Publisher</source></item>"
            for title, url, date in (
                ("earnings preview", "first", "Tue, 17 Nov 2026 20:00:00 GMT"),
                ("earnings preview", "another", "Tue, 17 Nov 2026 19:00:00 GMT"),
                ("future earnings", "future", "Wed, 18 Nov 2026 00:00:01 GMT"),
            )
        )
        client = FakeClient(f"<rss><channel>{articles}</channel></rss>")
        result = collect_qualitative_sources("NVDA", cutoff=CUTOFF, company_name="NVIDIA Corporation",
                                             client=client, environ={})
        self.assertEqual(client.hosts, ["news.google.com", "api.bsky.app"])
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["publisher"], "Example Publisher")
        self.assertEqual(result["items"][0]["link_scope"], "aggregator")
        self.assertEqual(result["items"][0]["text"], "")
        statuses = {row["provider"]: row["status"] for row in result["sources"]}
        self.assertEqual(statuses["bluesky"], "searched_zero")
        self.assertEqual(set(statuses), {"google_news_rss", "bluesky"})

    def test_provider_errors_are_not_reported_as_zero_results(self):
        client = FakeClient("<html><body>Consent required</body></html>")
        result = collect_qualitative_sources("005930", cutoff=CUTOFF, region="KR", company_name="삼성전자",
                                             client=client, environ={})
        statuses = {row["provider"]: row["status"] for row in result["sources"]}
        self.assertEqual(statuses["google_news_rss"], "unavailable")
        self.assertNotIn("naver_cafe", statuses)

    def test_public_only_accepts_korean_name_with_particle(self):
        feed = """<rss><channel><item><title>삼성전자가 실적을 발표했다</title>
        <link>https://news.google.com/rss/articles/example</link>
        <pubDate>Tue, 17 Nov 2026 08:00:00 GMT</pubDate><source>예시 언론</source></item></channel></rss>"""
        result = collect_qualitative_sources("005930", cutoff=CUTOFF, region="KR", client=FakeClient(feed), environ={})
        self.assertEqual([item["title"] for item in result["items"]], ["삼성전자가 실적을 발표했다"])

    def test_approved_connectors_remain_explicitly_opt_in(self):
        result = collect_qualitative_sources("NVDA", cutoff=CUTOFF, client=FakeClient("<rss><channel/></rss>"),
                                             environ={}, allow_authenticated=True)
        statuses = {row["provider"]: row["status"] for row in result["sources"]}
        self.assertEqual(result["collection_mode"], "public_and_authenticated")
        self.assertEqual(statuses["brave_news"], "authentication_required")
