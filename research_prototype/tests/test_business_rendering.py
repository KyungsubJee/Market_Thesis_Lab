from copy import deepcopy
import unittest

from research_prototype.discovery import business_selection_reason, render


def passage(text, *refs):
    return {"text": text, "evidence_ids": list(refs)}


def fixture():
    return {
        "as_of": "2026-11-18T03:25:31+00:00", "universe": [
            {"ticker": "AAPL", "listing_security_name": "Apple Inc. - Common Stock"},
            {"ticker": "MU", "listing_security_name": "Micron Technology, Inc. - Common Stock"}],
        "sector_seats": [], "evidence": {
            "execution": {"id": "execution", "source_url": "https://example.com/results", "form": "10-K"},
            "price": {"id": "price", "source_url": "https://example.com/price", "market_symbol": "AAPL"}},
        "horizons": {"short": {"shortlist": [{"ticker": "AAPL"}]},
                     "long": {"shortlist": [{"ticker": "AAPL"}]}},
        "business_screen": {"reviews": [
            {"ticker": "AAPL", "eligible_for_deep": True, "outcome": "deep_review", "selection_reason": "고객 기반의 지속성 검토"},
            {"ticker": "MU", "eligible_for_deep": False, "outcome": "insufficient_evidence",
             "selection_reason": "제품별 실적 원문 미확보"}], "selected_tickers": ["AAPL"]},
        "business_analyses": {"AAPL": {
            "business": passage("기기와 서비스를 판매해 수익을 얻습니다."),
            "strategy": passage("산업 수요의 변화를 검토합니다."),
            "moat": passage("경쟁사 비교는 미확인입니다."),
            "evidence": passage("유료 이용 실적을 확인했습니다.", "execution"),
            "thesis": passage("판매 증가가 현금 회수로 이어지는 경로를 확인합니다."),
            "valuation": passage("현재 가격에 반영된 기대는 미검증입니다.", "price"),
            "counterargument": passage("수요 둔화가 성장 가설을 무너뜨릴 수 있습니다."),
            "management_claims_status": "partly_verified",
            "horizons": {
                "short": {"action": "monitor", "proposal": passage("단기는 관찰합니다."),
                          "reassessment": passage("다음 5거래일의 수요 지표를 확인합니다.")},
                "long": {"action": "research", "proposal": passage("장기는 가격 가정을 조사합니다."),
                         "reassessment": passage("다음 실적에서 이용 지속률을 확인합니다.")},
                "medium": {"action": "research", "proposal": passage("UNSELECTED HORIZON MUST NOT SHOW")}}}},
        "selection_changes": [{"ticker": "MU", "status": "excluded", "reason": "원문 부족으로 보류합니다."},
                              {"ticker": "AAPL", "status": "retained", "reason": "수요 가설 검증을 지속합니다."}],
        "missing_required": [{"ticker": "AAPL", "field": "현재 가격 평가", "reason": "기대수익 우위를 검증하지 못했습니다.",
                              "source_url": "https://example.com/missing", "has_body": False}],
        # New business output must take precedence over the old rendering path.
        "decision_briefs": {},
    }


class BusinessRenderingTests(unittest.TestCase):
    def test_company_once_and_only_selected_period_proposals(self):
        run = fixture()
        before = deepcopy(run)
        report = render(run)
        self.assertEqual(report.count("## AAPL (Apple Inc.)"), 1)
        self.assertEqual(report.count("기기와 서비스를 판매해 수익을 얻습니다."), 1)
        self.assertIn("단기 · 5~20거래일", report)
        self.assertIn("장기 · 1~3년", report)
        self.assertIn("단기는 관찰합니다.", report)
        self.assertIn("장기는 가격 가정을 조사합니다.", report)
        self.assertNotIn("UNSELECTED HORIZON", report)
        self.assertIn("회사 설명 검증 · 일부 확인", report)
        self.assertIn("<https://example.com/results>", report)
        self.assertEqual(run, before)

    def test_selection_reason_and_material_missing_table_last(self):
        report = render(fixture())
        self.assertIn("이전 후보와 달라진 점", report)
        self.assertIn("선정 제외", report)
        self.assertIn("자료 부족", report)
        self.assertIn("원문 부족으로 보류합니다.", report)
        self.assertIn("전체 사업 검토 결과", report)
        self.assertIn("**미확보**", report)
        self.assertIn("| 중요도 | 자료·분석기간 | 수집 상태 | 판단 영향 |", report)
        self.assertIn("본문 미확보", report)
        self.assertIn("<https://example.com/missing>", report)
        self.assertGreater(report.index("필수 자료 공백"), report.index("전체 사업 검토 결과"))

    def test_empty_finalists_remain_empty_and_missing_analysis_does_not_fabricate(self):
        run = fixture()
        run["horizons"] = {}
        report = render(run)
        self.assertIn("기준을 충족한 최종 후보가 없습니다", report)
        self.assertNotIn("기기와 서비스를 판매", report)
        run["horizons"] = {"short": {"shortlist": [{"ticker": "AAPL"}]}}
        run["business_analyses"] = {}
        report = render(run)
        self.assertIn("기업 분석 자료를 확인하지 못해 제안을 보류", report)
        self.assertNotIn("단기는 관찰", report)

    def test_full_review_table_uses_final_reasons_and_explicit_trade_exclusions(self):
        run = fixture()
        run["business_screen"]["selected_tickers"].append("MU")
        run["business_analyses"]["AAPL"]["selection_reason"] = "실제 반복 매출을 확인해 최종 선정했습니다."
        run["business_analyses"]["MU"] = {"horizons": {}, "selection_reason": "실적 대비 가격 가정을 검증하지 못해 최종 제외했습니다."}
        run["business_screen"]["reviews"].extend([
            {"ticker": "NVDA", "outcome": "excluded_held", "selection_reason": "보유 내역과 일치합니다."},
            {"ticker": "XYZ", "outcome": "ineligible_trade", "selection_reason": "필수 거래 조건을 충족하지 못했습니다."},
        ])
        original = deepcopy(run)
        table = render(run).split("## 부록 · 전체 사업 검토 결과", 1)[1]
        self.assertIn("실제 반복 매출을 확인해 최종 선정했습니다.", table)
        self.assertIn("실적 대비 가격 가정을 검증하지 못해 최종 제외했습니다.", table)
        self.assertIn("최종 선정 제외", table)
        self.assertIn("보유 종목 제외", table)
        self.assertIn("거래 조건 미충족", table)
        self.assertNotIn("고객 기반의 지속성 검토", table)
        self.assertEqual(run, original)

    def test_source_links_group_filing_body_and_index_by_accession(self):
        run = fixture()
        run["evidence"]["execution"].update(accession="0000001-26-000001", source_kind="official_filing_body")
        run["evidence"]["index"] = {"id": "index", "accession": "0000001-26-000001", "form": "10-K",
                                    "source_url": "https://example.com/index"}
        run["business_analyses"]["AAPL"]["evidence"]["evidence_ids"] = ["index", "execution"]
        original = deepcopy(run)
        links = next(line for line in render(run).splitlines() if line.startswith("- 핵심 출처"))
        self.assertIn("<https://example.com/results>", links)
        self.assertIn("<https://example.com/price>", links)
        self.assertNotIn("<https://example.com/index>", links)
        self.assertEqual(run, original)

    def test_required_gaps_translate_metrics_and_limit_impact_to_affected_horizon(self):
        run = fixture()
        run["missing_required"] = [{"ticker": "AAPL", "field": "medium", "reason":
            "해당 기간의 필수 자료를 확인하지 못했습니다 · quarter_operating_margin_pct, latest_verified_quarter"}]
        report = render(run)
        self.assertIn("AAPL · 중기 · 1~2분기", report)
        self.assertIn("분기 영업이익률", report)
        self.assertIn("확인된 최신 분기 실적", report)
        self.assertIn("중기 후보 선정은 보류합니다", report)
        self.assertNotIn("quarter_operating_margin_pct", report)
        self.assertNotIn("latest_verified_quarter", report)
        self.assertIn("단기는 관찰합니다", report)

    def test_valuation_gap_remains_visible_without_required_gap_rows(self):
        run = fixture()
        run["missing_required"] = []
        report = render(run)
        self.assertIn("현재 가격에 반영된 기대는 미검증입니다.", report)
        run["business_analyses"]["AAPL"].pop("valuation")
        report = render(run)
        self.assertIn("가격 평가와 시장 기대의 검증 내용을 확인하지 못했습니다", report)

    def test_only_trailing_internal_source_suffix_is_hidden_and_scope_is_explicit(self):
        run = fixture()
        reason = "실제 판매를 확인해 선정했습니다. 근거: business-aapl-123, sec-aapl-annual, price-aapl."
        run["selection_changes"][0]["reason"] = reason
        run["business_analyses"]["AAPL"]["selection_reason"] = reason
        original = deepcopy(run)
        report = render(run)
        self.assertNotIn("business-aapl-123", report)
        self.assertIn("실제 판매를 확인해 선정했습니다.", report)
        self.assertEqual(report.count("선정·제외는 이번 예비·심층 검토에 전달된 발췌 자료를 기준"), 2)
        self.assertEqual(business_selection_reason("근거: 영업이익 개선을 확인했습니다."), "근거: 영업이익 개선을 확인했습니다.")
        self.assertIn("business-aapl-123", business_selection_reason("근거: business-aapl-123. 그러나 추가 확인이 필요합니다."))
        self.assertEqual(run, original)

    def test_business_header_distinguishes_source_cutoff_from_later_collection(self):
        run = fixture()
        run["qualitative_as_of"] = "2026-11-18T13:55:00+00:00"
        run["business_collection"] = {"collected_at": "2026-11-19T02:10:00+00:00"}
        report = render(run)
        self.assertIn("시세·사업자료 공개 기준 · 2026-11-18 12:25 (한국시간)", report)
        self.assertIn("사업 자료 묶음 생성 시각 · 2026-11-19 11:10 (한국시간)", report)
        self.assertNotIn("뉴스·공개 의견 검토 기준", report)
        self.assertNotIn("2026-11-18 22:55", report)

    def test_news_reassessment_header_separates_reused_selection_and_current_scope(self):
        run = fixture()
        run["qualitative_as_of"] = "2026-11-19T08:50:19+00:00"
        run["news_processing"] = {"target_tickers": ["AAPL"], "news_as_of": run["qualitative_as_of"]}
        original = deepcopy(run)
        report = render(run)
        self.assertIn("후보 선정 이력", report)
        self.assertIn("저장 결과 재사용", report)
        self.assertIn("이번 뉴스 반영 심층 재평가 1개", report)
        self.assertIn("뉴스·공개 의견 검토 기준 · 2026-11-19 17:50 (한국시간)", report)
        self.assertEqual(run, original)

    def test_business_macro_hides_only_known_time_preface_preserving_observation_and_gaps(self):
        run = fixture()
        run["decision_macro"] = {"US": {"text":
            "원자료 기준은 11월 18일 03:25 UTC, 공개 확인 한계는 같은 날 13:55 UTC이며 "
            "시장가격 관측은 11월 17일 미국 정규장까지다. 금리 5.12%는 발표 시점·빈티지가 미확인이다.",
            "evidence_ids": []}}
        original = deepcopy(run)
        report = render(run)
        self.assertNotIn("13:55 UTC", report)
        self.assertNotIn("03:25 UTC", report)
        self.assertIn("시장가격 관측은 11월 17일 미국 정규장까지다", report)
        self.assertIn("금리 5.12%는 발표 시점·최초 발표값이 미확인이다", report)
        self.assertEqual(run, original)


if __name__ == "__main__":
    unittest.main()
