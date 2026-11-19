from copy import deepcopy
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from framework_validation.optimized_runner import run_optimized
from research_prototype.news_reanalysis import integrate_news_fixture


BASE = "2026-11-19T08:43:10+00:00"
PUBLIC = "2026-11-19T08:50:19+00:00"


def fixture():
    return {"case_id": "news-test", "ticker": "T", "as_of": BASE, "horizon": "medium", "question": "Assess",
            "missing_fields": [], "required_evidence_ids": ["price-t"],
            "evidence": [{"id": "price-t", "available_at": BASE, "metrics": {"last_close": 100}}]}


def news():
    return {"id": "news-context-test", "ticker": "T", "available_at": "2026-11-19T08:48:00+00:00",
            "published_at": "2026-11-19T08:48:00+00:00", "collected_at": "2026-11-19T12:50:00+00:00",
            "content_basis": "verified_body", "extraction_verified": True, "fact_verified": False,
            "text": "Original attributed company statement, not independently corroborated.",
            "source_url": "https://example.com/article", "publisher_updated_at": "2026-11-19T08:48:00+00:00"}


class NewsFixtureTests(unittest.TestCase):
    def test_prepared_scope_is_derived_from_saved_shortlist(self):
        from research_prototype.news_reanalysis import prepare_record
        previous={'run_id':'old','as_of':BASE,'qualitative_as_of':PUBLIC,
                  'holdings':{'holdings':['T'],'sections':[]},'holdings_inputs':{'T':fixture()},
                  'discovery':{'horizons':{'long':{'shortlist':[{'ticker':'AMZN'}]}}}}
        result=prepare_record(previous,{'as_of':PUBLIC,'items':{}})
        self.assertEqual(result['news_integration']['scope']['frozen_candidate_tickers'],['AMZN'])
    def test_saved_render_passes_both_cutoffs_without_operation_metrics(self):
        from research_prototype.integrated import save_run
        from research_prototype.week3 import measurement
        record = {"run_id": "render-only", "as_of": BASE, "qualitative_as_of": PUBLIC,
                  "holdings": {"sections": [], "holdings": [], "operations": measurement([])},
                  "discovery": None, "discovery_error": "Not part of this fixture", "news_integration": {"test": True}}
        coverage = {"holdings": {"tickers": {}}, "discovery": {"tickers": {}}}
        with tempfile.TemporaryDirectory() as folder, patch("research_prototype.integrated.checklist", return_value=coverage), patch("research_prototype.integrated.render_report", return_value="# 보고서") as renderer, patch("research_prototype.integrated.appendix_lines", return_value=""):
            target = Path(folder) / "rendered"
            save_run(target, record, None)
            self.assertEqual(renderer.call_args.args[0]["qualitative_as_of"], PUBLIC)
            self.assertEqual(renderer.call_args.args[0]["as_of"], BASE)
            self.assertNotIn("최종 유효 분석 체인", (target / "holdings.md").read_text(encoding="utf-8"))
            self.assertNotIn("보유 분석 사용량", (target / "holdings.md").read_text(encoding="utf-8"))
    def test_event_attention_receives_existing_base_sources_cited_by_upstream_summaries(self):
        from research_prototype.model import NewsPrototypeModel
        model = NewsPrototypeModel("gpt-6.1-sol", 600, 20, audit_inputs=True)
        sid = "486290-sector-price-window"
        source = {"id": sid, "available_at": BASE, "text": "기간이 다른 국내 대용 ETF 가격", "metrics": {"last_close": 100},
                  "observation_end_at": "2026-11-19T07:30:00Z"}
        context = {**fixture(), "evidence": {sid: source}, "sector": {"finding": "업종 관측", "evidence_ids": [sid, "unknown-id"]}}
        event = model.build_payload("event_attention", "analysis", context)
        self.assertIn(sid, event["evidence"])
        self.assertEqual(event["evidence"][sid]["metrics"], source["metrics"])
        self.assertNotIn("unknown-id", event["evidence"])
        self.assertEqual(event["evidence"][sid]["observation_end_at"], source["observation_end_at"])
    def test_event_schema_requires_each_of_four_known_news_ids(self):
        from research_prototype.model import NewsPrototypeModel
        model = NewsPrototypeModel("gpt-6.1-sol", 600, 20, audit_inputs=True)
        rows = [{**news(), "id": f"news-context-{index}", "category": "news_context"} for index in range(4)]
        context = {**fixture(), "news_as_of": PUBLIC, "evidence": {row["id"]: row for row in rows}}
        captured = {}
        def transport(prompt, schema, record):
            captured.update(schema=schema, prompt=prompt)
            return {"finding": "최소 응답", "reasoning": [], "limitations": [], "evidence_ids": [], "item_reviews": []}
        with patch.object(model, "invoke", side_effect=transport), self.assertRaises(ValueError):
            model.run("event_attention", "analysis", context)
        reviews = captured["schema"]["properties"]["item_reviews"]
        self.assertEqual((reviews["minItems"], reviews["maxItems"]), (4, 4))
        self.assertEqual(reviews["items"]["properties"]["item_id"]["enum"], [row["id"] for row in rows])
        for row in rows:
            self.assertIn(row["id"], captured["prompt"])
        self.assertIn("unvalidated_output", model.calls[-1])
    def test_candidate_failure_resume_reuses_holdings_and_identical_frozen_candidate_input(self):
        from research_prototype.news_reanalysis import run_experiment
        original = {"business_deep": {"input": {"candidates": [{"ticker": "AVGO", "sources": []}]}}}
        record = {"as_of": BASE, "holdings": {"sections": [{"ticker": "T", "status": "complete", "calls": []}]},
                  "discovery": deepcopy(original), "news_context": {"as_of": PUBLIC, "items": {}},
                  "news_integration": {"original_discovery": deepcopy(original)}}
        inputs = []
        def candidate(previous, bundle, model):
            inputs.append(deepcopy(previous))
            return {**deepcopy(previous), "news_candidate_baseline": {"saved": True},
                    "model_status": "error" if len(inputs) == 1 else "complete", "calls": [{"role": "business_deep", "stage": "business_deep", "usage": None}]}
        def decision(view, model):
            view.update(decision_assessment={"status": "complete", "calls": [], "operations": {}}, decision_briefs={}, decision_macro={})
        with patch("research_prototype.news_reanalysis.reanalyze_holdings") as holding, patch("research_prototype.news_candidates.reanalyze_candidates", side_effect=candidate), patch("research_prototype.integrated.apply_decision_briefs", side_effect=decision):
            with self.assertRaisesRegex(RuntimeError, "Candidate batch failed"):
                run_experiment(record, "gpt-6.1-sol", 600)
            run_experiment(record, "gpt-6.1-sol", 600)
        self.assertEqual(holding.call_count, 1)
        self.assertEqual(inputs, [original, original])
        self.assertEqual(len(record["news_integration"]["new_calls"]), 2)
        self.assertEqual(record["news_integration"]["completed_phases"], ["holdings", "candidates", "decision"])
    def test_item_decisions_reach_draft_and_review_without_replaying_analyses(self):
        from research_prototype.model import NewsPrototypeModel
        model = NewsPrototypeModel("gpt-6.1-sol", 600, 20, audit_inputs=True)
        item = {**news(), "category": "news_context"}
        context = {**fixture(), "news_as_of": PUBLIC, "evidence": {item["id"]: item}, "analyses": {}}
        review = {"item_id": item["id"], "disposition": "incorporated", "reason": "공시와 대조할 전망 주장입니다.",
                  "material": True, "impact": "outlook", "claim": "기사 저자는 성장 기회를 주장합니다.",
                  "additional_confirmation": "실제 계약 공시를 확인합니다.", "evidence_ids": [item["id"]]}
        output = {"finding": "기사의 주장을 검토합니다.", "reasoning": ["공식 확인이 필요합니다."],
                  "limitations": [], "evidence_ids": [item["id"]], "item_reviews": [review]}
        with patch.object(model, "invoke", return_value=output):
            model.run("event_attention", "analysis", context)
        draft = model.build_payload("synthesis", "draft", context)
        reviewer = model.build_payload("reviewer", "review", {**context, "draft": {}})
        self.assertEqual(draft["news_item_reviews"], [review])
        self.assertEqual(reviewer["news_item_reviews"], [review])
        self.assertNotIn("analyses", reviewer)
        self.assertFalse(reviewer["evidence"][item["id"]]["fact_verified"])
        headline = {**item, "content_basis": "headline_only", "text": ""}
        with patch.object(model, "invoke", return_value=output), self.assertRaisesRegex(ValueError, "Headline"):
            model.run("event_attention", "analysis", {**context, "evidence": {item["id"]: headline}})

    def test_frozen_successful_role_reuse_never_calls_transport(self):
        from research_prototype.model import NewsPrototypeModel
        model = NewsPrototypeModel("gpt-6.1-sol", 600, 20, audit_inputs=True)
        context = {**fixture(), "evidence": {row["id"]: row for row in fixture()["evidence"]}}
        payload = model.build_payload("fundamental", "analysis", context)
        output = {"finding": "동결 자료", "reasoning": [], "limitations": [], "evidence_ids": ["price-t"]}
        model.successful_stage_cache = [{"role": "fundamental", "stage": "analysis", "input_payload": payload,
            "prompt": model.build_prompt("fundamental", "analysis", payload), "output": output}]
        with patch.object(model, "invoke", side_effect=AssertionError("No model allowed")):
            self.assertEqual(model.run("fundamental", "analysis", context), output)
        self.assertTrue(model.calls[0]["reused_successful_stage"])
    def test_related_role_projection_and_review_keep_attributed_body_and_cutoffs(self):
        from research_prototype import model as module
        self.assertTrue(hasattr(module, "NewsPrototypeModel"))
        instance = module.NewsPrototypeModel.__new__(module.NewsPrototypeModel)
        instance.news_reviews = []
        item = {**news(), "category": "news_context", "analysis_roles": ["event_attention", "fundamental"]}
        context = {**fixture(), "news_as_of": PUBLIC,
                   "evidence": {row["id"]: row for row in [*fixture()["evidence"], item]}}
        self.assertNotIn(item["id"], instance.build_payload("macro_market", "analysis", context)["evidence"])
        fundamental = instance.build_payload("fundamental", "analysis", context)
        self.assertEqual(fundamental["news_as_of"], PUBLIC)
        self.assertFalse(fundamental["evidence"][item["id"]]["fact_verified"])
        self.assertEqual(fundamental["evidence"][item["id"]]["text"], item["text"])
        reviewer = instance.build_payload("reviewer", "review", context)
        self.assertIn(item["id"], reviewer["evidence"])
        self.assertIn("item_reviews", instance.response_schema("event_attention")["properties"])
    def test_news_uses_public_cutoff_while_later_market_source_is_excluded(self):
        class Capture:
            def run(self, role, stage, context):
                self.context = context
                raise RuntimeError("Capture only, no model")
        base = fixture()
        frozen = deepcopy(base)
        enriched = integrate_news_fixture(base, [news()], PUBLIC)
        enriched["evidence"].append({"id": "future-price", "available_at": "2026-11-19T08:45:00Z"})
        model = Capture()
        result = run_optimized(enriched, model)
        self.assertIn("news-context-test", model.context["evidence"])
        self.assertNotIn("future-price", model.context["evidence"])
        self.assertEqual(model.context["as_of"], BASE)
        self.assertEqual(model.context["news_as_of"], PUBLIC)
        self.assertEqual(base, frozen)
        self.assertIn("future-price", result["excluded_evidence"])

    def test_future_updated_article_rejected_and_required_price_gate_survives(self):
        item = {**news(), "publisher_updated_at": "2026-11-19T10:00:00Z"}
        with self.assertRaises(ValueError):
            integrate_news_fixture(fixture(), [item], PUBLIC)
        base = fixture()
        base["evidence"] = []
        enriched = integrate_news_fixture(base, [news()], PUBLIC)
        self.assertEqual(run_optimized(enriched, None)["path"], "blocked")
