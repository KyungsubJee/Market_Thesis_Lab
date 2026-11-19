from copy import deepcopy
import unittest

from research_prototype.news_comparison import compare_runs, recovery_audit, render_comparison


def passage(text, *refs):
    return {"text": text, "evidence_ids": list(refs)}


def fixture():
    price = {"id": "nvda-stock-price-window", "observation_date": "2026-11-18", "metrics": {"last_close": 100}}
    base = {"run_id": "base", "as_of": "2026-11-19T08:43:10Z", "qualitative_as_of": "2026-11-19T08:50:19Z",
        "holdings": {"model_requested": "gpt-6.1-sol", "sections": [
            {"ticker": "NVDA", "status": "review_required", "sources": {price["id"]: price}, "calls": [], "final": {"action": "maintain"}},
            {"ticker": "005930", "status": "review_required", "path": "blocked", "sources": {}, "calls": [], "final": None}]},
        "coverage": {"holdings": {"tickers": {"NVDA": {"filing": {"status": "metadata_only"}}, "005930": {"blocking": ["own-price"]}}}},
        "discovery": {"horizons": {"medium": {"shortlist": [{"ticker": "AVGO"}]}}, "evidence": {}},
        "public_context": {"NVDA": {"items": [{"id": "qualitative-nvda-old", "kind": "news", "source_url": "https://example.com/a"}]}},
        "decision_assessment": {"input": {"sources": {"qualitative-nvda-old": {}}, "subjects": {}}},
        "decision_briefs": {"holdings": {"NVDA": {"outlook": passage("사업 전망 유지"),
            "counterargument": passage("경쟁 위험"), "proposal": passage("유지"), "reassessment": passage("다음 실적 확인")}}}}
    new = deepcopy(base)
    new["run_id"] = "new"
    new["news_context"] = {"items": {"NVDA": [{"id": "news-nvda-a", "ticker": "NVDA", "source_url": "https://example.com/a",
        "available_at": "2026-11-19T08:00:00Z", "body_verification_status": "verified"}]}, "excluded": []}
    new["news_processing"] = [{"item_id": "news-nvda-a", "ticker": "NVDA", "status": "not_adopted", "reason": "공식 발표와 중복입니다."}]
    call = {"ticker": "NVDA", "role": "event_attention", "stage": "analysis", "seconds": 1,
            "usage": {"input_tokens": 100, "output_tokens": 10},
            "input_payload": {"evidence": {"news-nvda-a": {"text": "검증된 기사 문단"}}},
            "output": {"evidence_ids": ["news-nvda-a"]}}
    new["news_integration"] = {"scope": {"frozen_candidate_tickers": ["AVGO"], "rerun_preliminary_screen": False,
        "expected_model": "gpt-6.1-sol"}, "new_calls": [call], "role_inputs": [], "recheck": []}
    return base, new


class NewsComparisonTests(unittest.TestCase):
    def test_same_action_and_zero_final_citations_are_not_automatic_failure(self):
        base, new = fixture()
        before = deepcopy(new)
        audit = compare_runs(base, new)
        self.assertFalse(audit["critical_failures"])
        self.assertEqual(audit["after"]["public"]["final_cited_unique"], 0)
        self.assertEqual(audit["after"]["public"]["analysis_input_unique"], 1)
        self.assertEqual(audit["after"]["public"]["analysis_output_unique"], 1)
        self.assertEqual(audit["after"]["public"]["reviewed_unique"], 1)
        self.assertIn("동일 행동이나 인용 0건만으로 실패로 판정하지 않습니다", render_comparison(audit))
        self.assertEqual(new, before)

    def test_frozen_price_future_admission_and_blocked_gate_are_checked(self):
        base, new = fixture()
        new["holdings"]["sections"][0]["sources"]["nvda-stock-price-window"]["metrics"]["last_close"] = 101
        new["news_context"]["items"]["NVDA"][0]["available_at"] = "2026-11-19T09:00:00Z"
        new["holdings"]["sections"][1].update(path="basic", final={"action": "maintain"})
        audit = compare_runs(base, new)
        self.assertTrue(any("가격" in item for item in audit["critical_failures"]))
        self.assertTrue(any("공개 기준" in item for item in audit["critical_failures"]))
        self.assertTrue(any("차단" in item for item in audit["critical_failures"]))

    def test_collected_uninjected_future_item_is_not_incorrectly_admitted(self):
        base, new = fixture()
        new["news_context"]["items"]["NVDA"].append({"id": "news-later", "available_at": "2026-11-19T09:00:00Z"})
        audit = compare_runs(base, new)
        self.assertFalse(audit["critical_failures"])
        self.assertEqual(audit["after"]["public"]["collected_records"], 2)

    def test_cost_scopes_and_missing_new_calls_do_not_reuse_baseline_totals(self):
        base, new = fixture()
        audit = compare_runs(base, new)
        self.assertEqual(audit["cost"]["experiment_new_calls"]["calls"], 1)
        text = render_comparison(audit)
        self.assertIn("40개 예비 검토 생략", text)
        self.assertIn("8개→고정 후보", text)
        new["news_integration"].pop("new_calls")
        audit = compare_runs(base, new)
        self.assertIsNone(audit["cost"]["experiment_new_calls"]["calls"])
        self.assertTrue(audit["measurement_notices"])

    def test_original_official_and_macro_snapshots_cannot_change_but_added_news_is_separate(self):
        base, new = fixture()
        original = {"id": "nvda-official-results", "metrics": {"revenue": 100}, "text": "회사 실적 원문"}
        base["holdings_inputs"] = {"NVDA": {"evidence": [deepcopy(original)], "required_evidence_ids": [original["id"]]}}
        new["holdings_inputs"] = deepcopy(base["holdings_inputs"])
        new["holdings_inputs"]["NVDA"]["evidence"].append({"id": "news-nvda-a", "text": "새 기사"})
        audit = compare_runs(base, new)
        self.assertFalse(audit["critical_failures"])
        self.assertTrue(audit["fixed"]["added_source_paths"])
        new["holdings_inputs"]["NVDA"]["evidence"][0]["metrics"]["revenue"] = 101
        audit = compare_runs(base, new)
        self.assertTrue(any("공식 보강" in entry for entry in audit["critical_failures"]))

    def test_updated_body_after_cutoff_cannot_be_treated_as_verified_historical_body(self):
        base, new = fixture()
        new["news_context"]["items"]["NVDA"][0].update(grade="verified_body", publisher_updated_at="2026-11-19T09:10:00Z")
        audit = compare_runs(base, new)
        self.assertTrue(any("수정된 본문" in entry for entry in audit["critical_failures"]))

    def test_candidate_review_contract_counts_disposition_and_retains_declared_impacts(self):
        base, new = fixture()
        new["news_processing"] = []
        new["discovery"]["news_processing"] = {"source_reviews": [{"ticker": "AVGO", "source_id": "news-nvda-a",
            "disposition": "context_only", "reason": "업황의 참고 맥락이며 직접 수익 효과는 미확인입니다.",
            "impacts": {"outlook": {"changed": False, "reason": "기존 전망 유지"}}}]}
        audit = compare_runs(base, new)
        self.assertEqual(audit["after"]["public"]["reviewed_unique"], 1)
        self.assertIn("context_only", audit["after"]["public"]["processing_status_counts"])
        self.assertFalse(audit["item_processing"][0]["impacts"]["outlook"]["changed"])

    def test_forced_disposition_ids_alone_are_not_counted_as_substantive_analysis(self):
        base, new = fixture()
        new["news_integration"]["new_calls"][0]["output"] = {"evidence_ids": [], "item_reviews": [
            {"item_id": "news-nvda-a", "disposition": "irrelevant", "reason": "무관"}]}
        audit = compare_runs(base, new)
        self.assertEqual(audit["after"]["public"]["analysis_output_unique"], 1)
        self.assertEqual(audit["after"]["public"]["nonledger_output_unique"], 0)

    def test_invalid_raw_output_is_not_successful_adoption_but_its_input_and_cost_remain(self):
        base, new = fixture()
        new["news_integration"]["new_calls"][0]["error"] = "Disposition validation failed"
        audit = compare_runs(base, new)
        self.assertEqual(audit["after"]["public"]["analysis_input_unique"], 1)
        self.assertEqual(audit["after"]["public"]["analysis_output_unique"], 0)
        self.assertEqual(audit["after"]["public"]["nonledger_output_unique"], 0)
        self.assertEqual(audit["after"]["public"]["failed_output_unique"], 1)
        self.assertEqual(audit["cost"]["experiment_new_calls"]["calls"], 1)
        self.assertEqual(audit["cost"]["experiment_new_calls"]["failed_calls"], 1)

    def test_baseline_diff_uses_reviewed_display_not_superseded_raw_model_sentence(self):
        base, new = fixture()
        raw = passage("완료 세션 미확인")
        corrected = passage("저장된 최신 종료 세션 확인")
        base["decision_briefs"]["holdings"]["NVDA"]["counterargument"] = raw
        base["presentation_review"] = {"tickers": {"NVDA": {"changes": [{"field": "counterargument", "before": raw, "after": corrected}]}}}
        new["decision_briefs"]["holdings"]["NVDA"]["counterargument"] = corrected
        audit = compare_runs(base, new)
        row = next(row for row in audit["field_changes"] if row["ticker"] == "NVDA" and row["field"] == "counterargument")
        self.assertFalse(row["text_changed"])
        self.assertEqual(row["before_raw"], "완료 세션 미확인")
        self.assertEqual(row["before"], "저장된 최신 종료 세션 확인")

    def test_recovery_matches_cached_original_output_without_removing_paid_failure(self):
        base, new = fixture()
        original = deepcopy(new["news_integration"]["new_calls"][0])
        new["news_integration"]["holding_attempt_history"] = {"NVDA": [{"calls": [original]}]}
        new["holdings"]["sections"][0]["calls"] = [{**deepcopy(original), "reused_successful_stage": True}]
        failed = {**deepcopy(original), "error": "News disposition must cover every supplied item exactly once"}
        audit = recovery_audit(new, [failed])
        self.assertEqual(audit["reused_successful_roles"], 1)
        self.assertEqual(audit["matching_original_roles"], 1)
        self.assertEqual(len(audit["initial_failed_calls"]), 1)

    def test_gate_change_displays_korean_status_but_retains_raw_audit(self):
        base, new = fixture()
        new["holdings"]["sections"][0]["status"] = "complete"
        audit = compare_runs(base, new)
        text = render_comparison(audit)
        self.assertIn("NVDA: 검토 필요→분석 완료", text)
        self.assertNotIn("{'path':", text)
        self.assertEqual(audit["fixed"]["gate_changes"][0]["before"]["status"], "review_required")


if __name__ == "__main__":
    unittest.main()
