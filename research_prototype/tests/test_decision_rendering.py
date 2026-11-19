import unittest
from copy import deepcopy

from research_prototype.coverage import APPENDIX_TABLE_HEADER, appendix_lines
from research_prototype.discovery import render
from research_prototype.report import decision_brief_lines, decision_links, render_report


AS_OF = "2026-11-18T03:25:31+00:00"
SOURCE = {"id": "nvda-stock-price-window", "source_url": "https://example.com/nvda", "market_symbol": "NVDA"}
FIELDS = {
    "issue": {"text": "수급 변화", "evidence_ids": []},
    "current": {"text": "정규장 상대수익률이 약했습니다.", "evidence_ids": [SOURCE["id"]]},
    "outlook": {"text": "수요가 늘면 수익성이 개선될 수 있습니다.", "evidence_ids": []},
    "counterargument": {"text": "이미 높은 이익률은 유지할 근거입니다.", "evidence_ids": []},
    "proposal": {"text": "시장과 섹터 대비 약세를 고려해 비중을 축소합니다.", "evidence_ids": []},
    "reassessment": {"text": "20거래일 수익률이 업종을 웃돌면 다시 판단합니다.", "evidence_ids": []},
}


class DecisionRenderingTests(unittest.TestCase):
    def test_issuer_release_link_is_not_mislabeled_as_sec_filing(self):
        sources = {"release": {"form": "issuer_release", "source_url": "https://example.com/results"},
                   "filing": {"form": "10-K", "source_url": "https://example.com/annual"}}
        links = decision_links(["release", "filing"], sources)
        self.assertIn("[기업 실적 발표](<https://example.com/results>)", links)
        self.assertIn("[SEC 10-K](<https://example.com/annual>)", links)
        self.assertNotIn("SEC issuer_release", links)

    def test_holding_brief_respects_gate_and_hides_raw_public_lists(self):
        tickers = ("NVDA", "SOXS")
        run = {"run_id": "saved", "generated_at": AS_OF, "as_of": AS_OF,
               "model_requested": "test", "source_mode": "us-live",
               "sections": [{"ticker": ticker, "as_of": AS_OF, "horizon": "medium", "status": "complete",
                             "sources": {SOURCE['id']: SOURCE}, "final": {"action": "reduce"}} for ticker in tickers],
               "coverage": {"tickers": {"SOXS": {"blocking": ["required"], "checks": [
                   ("필수", "필수 가격 자료", "missing", None),
               ]}}},
               "decision_macro": {"text": "미국 전반 수급이 약합니다.", "evidence_ids": []},
               "decision_briefs": {ticker: {**FIELDS, "status": "complete", "action": "reduce"} for ticker in tickers}}
        report = render_report(run, public_context={"NVDA": {"items": [{"title": "RAW HEADLINE"}]}})
        self.assertEqual(report.count("## 공통 거시·시장"), 1)
        self.assertIn("분석 기간 중기", report)
        self.assertIn("> **행동 제안 · 축소**", report)
        self.assertIn("> **행동 제안 · 판단 보류**", report)
        self.assertIn("필수 가격 자료", report)
        self.assertIn("<https://example.com/nvda>", report)
        self.assertIn("20거래일 수익률이 업종을 웃돌면", report)
        self.assertNotIn("RAW HEADLINE", report)
        self.assertEqual(report.count(FIELDS["proposal"]["text"]), 1)
        self.assertIn(APPENDIX_TABLE_HEADER, appendix_lines(run["coverage"]))

    def test_discovery_brief_keeps_horizon_and_proposes_research_only(self):
        run = {"run_id": "saved", "as_of": AS_OF, "universe": [], "sector_seats": [],
               "horizons": {"short": {"shortlist": [{"ticker": "NVDA"}]}}, "evidence": {SOURCE["id"]: SOURCE},
               "decision_briefs": {"NVDA": {"short": {**FIELDS, "action": "research", "status": "complete",
                                                        "proposal": {"text": "업종 흐름을 더 확인합니다.", "evidence_ids": []}}}}}
        report = render(run)
        self.assertIn("단기 · 5~20거래일", report)
        self.assertIn("> **후보 제안 · 추가 조사**", report)
        self.assertIn("업종 흐름을 더 확인합니다.", report)
        self.assertIn("<https://example.com/nvda>", report)
        self.assertNotIn("행동 제안 · 축소", report)

    def test_tentative_holding_keeps_action_without_definitive_proposal_copy(self):
        run = {"as_of": AS_OF, "qualitative_as_of": "2026-11-18T13:55:45Z",
               "sections": [{"ticker": "NVDA", "as_of": AS_OF, "horizon": "medium", "status": "review_required",
                             "sources": {SOURCE["id"]: SOURCE}, "final": {"action": "reduce"}}],
               "decision_briefs": {"NVDA": {**FIELDS, "status": "complete", "action": "reduce",
                                            "proposed_action": "sell"}}}
        report = render_report(run)
        self.assertIn("## 현재 행동 제안\n\n- **잠정 축소** · NVDA (NVIDIA Corporation)", report)
        self.assertIn("> **잠정 행동 제안 · 축소**", report)
        self.assertNotIn(FIELDS["proposal"]["text"], report)
        self.assertIn("독립 검토 전 대안** · 매도", report)
        self.assertIn("기초 분석 기준 · 2026-11-18 12:25", report)
        self.assertIn("공개 의견 검토 기준 · 2026-11-18 22:55", report)

    def test_discovery_audit_summarizes_saved_screen_without_changing_it(self):
        run = {"as_of": AS_OF, "evidence": {}, "decision_briefs": {}, "sector_seats": [],
               "held_exclusions": ["NVDA", "SOXS"], "universe": [{"ticker": "NVDA", "status": "held"}],
               "horizons": {"short": {"shortlist": [], "excluded": [
                   {"ticker": "MU", "reason": "failed_positive_gate", "fields": ["sector_excess_20d_pp"]},
                   {"ticker": "GE", "reason": "rank_below_top_two"},
                   {"ticker": "LLY", "reason": "rank_below_top_two"},
               ]}}}
        original = deepcopy(run)
        report = render(run)
        self.assertIn("## 후보 선별과 제외 기준", report)
        self.assertIn("2,000만 달러", report)
        self.assertIn("보유 내역과 일치해 제외한 종목 · NVDA", report)
        self.assertIn("기준 미달 1종목 (예: MU, 20거래일 섹터 대비 수익률)", report)
        self.assertIn("상위 2개 밖 2종목 (예: GE)", report)
        self.assertNotIn("LLY", report)
        self.assertEqual(run, original)

    def test_report_normalizes_previous_action_and_keeps_original_reversal(self):
        brief = {**FIELDS, "status": "complete", "action": "reduce",
                 "proposal": {"text": "가격 약세로 기존 축소 판단을 유지한다.", "evidence_ids": []},
                 "reassessment": {"text": "SOXX가 SPY를 앞서면 재검토한다.", "evidence_ids": []}}
        record = {"as_of": AS_OF, "sections": [{"ticker": "SOXL", "status": "complete", "horizon": "short",
                  "sources": {}, "final": {"action": "reduce", "reassessment_conditions": [
                      "SOXX가 SPY를 앞서면 보유 유지를 재검토한다.",
                  ]}}], "decision_briefs": {"SOXL": brief}}
        report = render_report(record)
        self.assertIn("가격 약세로 비중을 축소한다.", report)
        self.assertNotIn("기존 축소 판단", report)
        self.assertIn("보유 유지를 재검토한다.", report)
        self.assertIn("SOXX(미국 반도체)", report)
        self.assertIn("SPY(S&amp;P 500)", report)

    def test_discovery_flags_unverified_valuation_before_medium_and_long(self):
        run = {"as_of": AS_OF, "evidence": {}, "decision_briefs": {}, "sector_seats": [], "universe": [],
               "horizons": {"medium": {"shortlist": [{"ticker": "MU"}]},
                            "long": {"shortlist": [{"ticker": "AAPL"}]}}}
        report = render(run)
        statement = "현재 주가의 적정성과 시장의 실적 기대는 확인하지 못했습니다."
        self.assertEqual(report.count(statement), 2)
        for section in ("## 중기 · 1~2분기", "## 장기 · 1~3년"):
            self.assertIn(section + "\n\n" + statement, report)

    def test_core_links_include_product_risk_and_a_quantitative_driver(self):
        sources = {
            "stock": {"source_url": "https://example.com/price", "market_symbol": "SOXL"},
            "peer": {"source_url": "https://example.com/peer", "market_symbol": "SOXX"},
            "terms": {"source_url": "https://www.direxion.com/product/fund", "category": "etf_product"},
            "prospectus": {"source_url": "https://www.sec.gov/fund", "category": "etf_prospectus", "form": "497K"},
        }
        brief = {**FIELDS, "proposal": {"text": "日次 목표와 수익률로 축소합니다.", "evidence_ids": ["stock", "peer"]},
                 "counterargument": {"text": "일일 레버리지 위험이 있습니다.", "evidence_ids": ["terms", "prospectus"]}}
        lines = decision_brief_lines(brief, sources, "행동 제안", "축소")
        links = lines[-1]
        self.assertIn("https://www.sec.gov/fund", links)
        self.assertIn("https://example.com/price", links)
        self.assertNotIn("https://example.com/peer", links)

    def test_core_links_combine_filing_and_event_instead_of_two_prices(self):
        sources = {
            "stock": {"source_url": "https://example.com/price", "market_symbol": "NVDA"},
            "peer": {"source_url": "https://example.com/peer", "market_symbol": "SOXX"},
            "filing": {"source_url": "https://www.sec.gov/earnings", "form": "10-Q"},
            "event": {"source_url": "https://nvidianews.nvidia.com/transition", "category": "supplemental_official_source"},
        }
        brief = {**FIELDS, "current": {"text": "실적과 상대수익률", "evidence_ids": ["stock", "peer", "filing"]},
                 "issue": {"text": "임원 변동", "evidence_ids": ["event"]}}
        links = decision_brief_lines(brief, sources, "행동 제안", "유지")[-1]
        self.assertIn("https://www.sec.gov/earnings", links)
        self.assertIn("https://nvidianews.nvidia.com/transition", links)
