from copy import deepcopy
from html import unescape
import unittest

from research_prototype.coverage import item_card, public_comparison_lines, public_source_checks, render_public_coverage
from research_prototype.discovery import render as render_discovery
from research_prototype.report import render_report


ITEMS = [
    {"id": "news-source", "ticker": "NVDA", "provider": "google_news",
     "kind": "news", "title": "새 제품 출시 계획", "text": "",
     "publisher": "테스트 뉴스", "source_url": "https://example.com/article",
     "published_at": "2026-11-16T15:00:00+00:00", "link_scope": "aggregator", "source_kind": "title_only"},
    {"id": "community-source", "ticker": "NVDA", "provider": "reddit",
     "kind": "community", "title": "출시 계획 토론", "text": "수요 지표를 더 확인하고 싶습니다.",
     "publisher": "r/stocks", "author": "sample_investor",
     "source_url": "https://www.reddit.com/r/stocks/comments/example/thread/",
     "published_at": "2026-11-16T16:00:00+00:00", "link_scope": "permalink", "source_kind": "text"},
]
PUBLIC_CONTEXT = {"NVDA": {"items": ITEMS, "sources": [
    {"kind": "news", "provider": "google_news", "status": "available", "count": 1},
    {"kind": "news", "provider": "finnhub", "status": "authentication_required", "count": 0},
    {"kind": "community", "provider": "reddit", "status": "available", "count": 1},
    {"kind": "community", "provider": "stocktwits", "status": "searched_zero", "count": 0},
]}}
FINDINGS = {"NVDA": {"status": "assessed", "judgment_effect": "watch_condition_added",
                     "effect_reason": "출시 후 수요 지표를 확인합니다.", "findings": [
    {"kind": "community", "view": "neutral", "summary": "작성자는 수요를 확인하자는 의견을 냈습니다.",
     "evidence_ids": ["community-source"]},
    {"kind": "community", "view": "favorable", "summary": "근거 없는 문장", "evidence_ids": ["unknown"]},
]}}
COMPARISON = {"ticker_count": 1, "compared_tickers": ["NVDA"],
              "before": {"news_items": 0, "news_tickers": 0, "community_items": 2, "community_tickers": 1,
                         "input_tokens": 2400, "output_tokens": 800, "calls": 1},
              "after": {"news_items": 3, "news_tickers": 1, "community_items": 4, "community_tickers": 1,
                        "opinion_items": 1, "opinion_tickers": 1, "share_items": 3, "share_tickers": 1,
                        "input_tokens": 2100, "output_tokens": 600, "calls": 1}}


class PublicCoverageTests(unittest.TestCase):
    def assert_public_material(self, markdown):
        self.assertIn("새 제품 출시 계획", markdown)
        self.assertIn("테스트 뉴스", markdown)
        self.assertIn("게시일 2026-11-17 (한국시간)", markdown)
        self.assertIn("<https://example.com/article>", markdown)
        self.assertIn("제목만 확인", markdown)
        self.assertIn("인증", markdown)
        self.assertIn("조회 범위 내 적격 자료 없음", markdown)
        self.assertIn("sample_investor", unescape(markdown))
        self.assertIn("<https://www.reddit.com/r/stocks/comments/example/thread/>", markdown)
        self.assertIn("작성자는 수요를 확인하자는 의견을 냈습니다.", markdown)
        self.assertIn("출시 후 수요 지표를 확인합니다.", markdown)
        self.assertNotIn("근거 없는 문장", markdown)

    def test_holdings_reports_sources_even_when_action_is_pending(self):
        report = render_report({
            "run_id": "run", "generated_at": "2026-11-16", "model_requested": "offline",
            "source_mode": "us-live",
            "sections": [{"ticker": "NVDA", "status": "review_required", "sources": {}, "final": None}],
            "public_comparison": COMPARISON,
        }, FINDINGS, PUBLIC_CONTEXT)
        self.assert_public_material(report)
        self.assertIn("NVDA (NVIDIA Corporation)", report)
        self.assertIn("판단 보류", report)
        self.assertIn("판단 보류 유지 · 추가 확인 참고", report)
        self.assertIn("## 공개 자료 수집 전후 비교", report)
        self.assertIn("입력 2,400 → 2,100 토큰", report)

    def test_discovery_lists_the_same_sources_once_across_horizons(self):
        from research_prototype.discovery import HORIZONS
        report = render_discovery({
            "run_id": "run", "as_of": "2026-11-16", "held_exclusions": [], "sector_seats": [],
            "universe": [{"ticker": "NVDA", "listing_security_name": "엔비디아", "sector": "XLK"}], "evidence": {},
            "horizons": {key: {"shortlist": [{"ticker": "NVDA", "score": 1, "evidence_ids": []}], "excluded": []}
                         for key in HORIZONS},
            "counts": dict.fromkeys(("holdings_candidates", "eligible", "seats", "sec_with_facts", "horizon_slots",
                                     "unique_shortlist"), 1),
            "operations": {"calls": 0, "usage": dict.fromkeys(("input_tokens", "cached_input_tokens", "output_tokens"), 0)},
            "model_status": "not_requested",
            "public_comparison": COMPARISON,
        }, FINDINGS, PUBLIC_CONTEXT)
        self.assert_public_material(report)
        self.assertIn("NVDA (엔비디아)", report)
        self.assertEqual(report.count("<https://example.com/article>"), 1)
        self.assertIn("## 공개 자료 수집 전후 비교", report)
        self.assertIn("입력 2,400 → 2,100 토큰", report)

    def test_community_index_and_invalid_links_cannot_support_an_interpretation(self):
        context = deepcopy(PUBLIC_CONTEXT["NVDA"])
        context["items"][0]["source_url"] = "javascript:alert(1)"
        context["items"][1]["link_scope"] = "provider_link"
        analysis = FINDINGS["NVDA"]
        markdown = "\n".join(render_public_coverage(context, analysis))
        self.assertNotIn("javascript:", markdown)
        self.assertNotIn("reddit.com/r/stocks/comments/example/thread/", markdown)
        self.assertNotIn("출시 후 수요 지표를 확인합니다", markdown)
        self.assertNotIn("작성자는 수요를 확인하자는 의견", markdown)
        self.assertIn("판단에 연결할 수 있는 공개 자료 인용을 확인하지 못했습니다", markdown)

    def test_short_provider_body_is_distinct_from_title_only(self):
        post = {**ITEMS[1], "provider": "bluesky", "publisher": "Bluesky",
                "availability_basis": "bluesky_created_and_indexed_time",
                "title": "출시 이후 수요를 확인하자", "text": "출시 이후 수요를 확인하자"}
        self.assertIn("공개 게시글 본문 확인", item_card(post, post["source_url"]))
        self.assertNotIn("제목만 확인", item_card(post, post["source_url"]))
        self.assertIn("제목만 확인", item_card({**ITEMS[1], "text": ""}, ITEMS[1]["source_url"]))

    def test_reviewed_publication_day_does_not_claim_availability_bound_as_exact_time(self):
        post = {**ITEMS[1], "published_at": "2026-11-16T23:59:59-05:00",
                "publication_precision": "day", "availability_basis": "reviewed_public_permalink"}
        label = item_card(post, post["source_url"])
        self.assertIn("게시일 2026-11-16 · 게시일 단위/정확 시각 미확인", label)
        self.assertNotIn("23:59", label)
        self.assertNotIn("2026-11-17", label)

    def test_article_share_and_fallback_rss_are_reported_with_specific_labels(self):
        news = {**ITEMS[0], "provider": "yahoo_finance_rss"}
        share = {**ITEMS[1], "provider": "bluesky", "community_subtype": "article_share",
                 "external_article_url": "https://example.com/shared-article"}
        context = {"items": [news, share], "sources": [
            {"provider": "google_news_rss", "kind": "news", "status": "unavailable"},
            {"provider": "yahoo_finance_rss", "kind": "news", "status": "available", "count": 1},
            {"provider": "bluesky", "kind": "community", "status": "available", "commentary_count": 0},
        ]}
        markdown = "\n".join(render_public_coverage(context, FINDINGS["NVDA"]))
        self.assertIn("Yahoo Finance RSS", markdown)
        self.assertIn("대체 RSS", markdown)
        self.assertIn("기사 공유", markdown)
        self.assertIn("<https://example.com/shared-article>", markdown)
        self.assertNotIn("공개 개인 의견", markdown)
        self.assertNotIn("작성자는 수요를 확인하자는 의견", markdown)
        self.assertIn(("보강", "공개 투자자 의견", "searched_zero", "작성자 의견 0건 · 기사 공유 1건"),
                      list(public_source_checks(context)))

    def test_coverage_percentages_preserve_unknown_legacy_opinions(self):
        prior = {"ticker_count": 2, "news_tickers": 0, "opinion_tickers": None, "community_tickers": 1}
        current = {"ticker_count": 2, "news_tickers": 2, "opinion_tickers": 1, "community_tickers": 2}
        summary = "\n".join(public_comparison_lines({"ticker_count": 4,
            "before": {**prior, "ticker_count": 4, "as_of": "2026-11-18T01:00:00Z",
                       "groups": {"holdings": prior, "shortlist": prior}},
            "after": {**current, "ticker_count": 4, "as_of": "2026-11-18T02:00:00Z",
                      "groups": {"holdings": current, "shortlist": current}},
        }))
        self.assertIn("전체 확보율 · 기사 0/4개(0%) → 2/4개(50%)", summary)
        self.assertIn("보유 종목 확보율 · 기사 0/2개(0%) → 2/2개(100%) · 작성자 의견 미집계 → 1/2개(50%)", summary)
        self.assertIn("발굴 후보 확보율", summary)
        self.assertIn("수집 시점이 달라", summary)

    def test_controlled_fallback_is_separate_from_observed_coverage_and_tokens(self):
        comparison = deepcopy(COMPARISON)
        comparison["controlled_fallback"] = {
            "tested_tickers": ["NVDA", "005930"], "recovered_tickers": ["NVDA"],
            "providers": {"NVDA": "yahoo_finance_rss", "005930": "bing_news_rss"}, "article_count": 2,
        }
        summary = "\n".join(public_comparison_lines(comparison))
        self.assertIn("Google에만 429를 재현한 별도 검증 · 대체 피드는 실제 공개 응답", summary)
        self.assertIn("2종목 중 1종목", summary)
        self.assertIn("기사 2건", summary)
        self.assertIn("Yahoo Finance RSS", summary)
        self.assertIn("관측 확보율·토큰 비교에서 제외", summary)
