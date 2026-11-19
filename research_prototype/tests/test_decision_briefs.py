from copy import deepcopy
import unittest

from research_prototype.decision_briefs import FIELDS, assemble, project_source, synthesize, validated_briefs
from framework_validation.runner import parse_timestamp


BASE = "2026-11-18T03:25:00Z"
PUBLIC = "2026-11-18T13:55:00Z"


def record():
    return {"as_of": BASE, "qualitative_as_of": PUBLIC, "holdings": {"sections": [{
        "ticker": "NVDA", "status": "complete", "as_of": BASE, "horizon": "medium", "required_evidence_ids": ["official"],
        "final": {"action": "maintain", "evidence_ids": ["official"]},
        "sources": {"official": {"id": "official", "available_at": BASE, "category": "sec_periodic", "metrics": {"revenue_usd": 90}},
                    "future": {"id": "future", "available_at": PUBLIC, "metrics": {"revenue_usd": 100}}},
    }]}, "discovery": None, "public_context": {"NVDA": {"items": [
        {"id": "headline", "kind": "news", "available_at": PUBLIC, "title": "A headline", "text": ""},
        {"id": "share", "kind": "community", "community_subtype": "article_share", "available_at": PUBLIC},
    ], "sources": []}}}


def output(subject, citation):
    return {"sections": [{"id": subject, "requested_action": "reduce",
                          **{field: {"text": "A bounded finding.", "evidence_ids": [citation]} for field in FIELDS}}],
            "macro": []}


class DecisionBriefTests(unittest.TestCase):
    def test_financial_derivations_and_selected_body_survive_projection(self):
        source = {"available_at": BASE, "metrics": {"revenue_usd": 100},
                  "derived_metrics": {"annual_long_term_debt_to_revenue_pct": 16.31},
                  "long_term_debt_usd": 16.31, "comparative_periods": ["2024", "2025", "2026"],
                  "comparative_cash_after_capex_margins_pct": [23.73, 27.82, 28.41],
                  "quarter_basis": "annual_less_q3_ytd", "text": "Opening. " * 120 + "Material order backlog."}
        result = project_source(source, parse_timestamp(BASE))
        for key, value in source.items():
            self.assertEqual(result[key], value)
        self.assertTrue(result["text"].endswith("Material order backlog."))

    def test_saved_screen_metrics_stay_horizon_scoped_and_gated(self):
        saved = record()
        saved["discovery"] = {"sector_seats": [{"ticker": "TEST", "metrics": {
            "quarter_revenue_yoy_pct": 6.83, "annual_long_term_debt_to_revenue_pct": 84.70}}],
            "evidence": {"sec-test-quarter": {"id": "sec-test-quarter", "available_at": BASE}},
            "horizons": {"medium": {"shortlist": [{"ticker": "TEST", "evidence_ids": ["sec-test-quarter"]}],
                                     "score_fields": ["quarter_revenue_yoy_pct"]}}}
        subject = assemble(saved)["subjects"]["discovery:TEST:medium"]
        self.assertEqual(subject["screen_metrics"]["values"], {"quarter_revenue_yoy_pct": 6.83})
        self.assertEqual(subject["screen_metrics"]["evidence_ids"], ["sec-test-quarter"])
        saved["discovery"]["evidence"] = {}
        subject = assemble(saved)["subjects"]["discovery:TEST:medium"]
        self.assertEqual(subject["status"], "blocked")
        self.assertNotIn("screen_metrics", subject)

    def test_replay_evidence_keeps_separate_cutoffs_and_ignores_shares(self):
        batch = assemble(record())
        subject = next(iter(batch["subjects"].values()))
        self.assertEqual(set(subject["evidence_ids"]), {"official", "headline"})
        self.assertEqual(batch["sources"]["headline"]["grade"], "headline_only")

    def test_public_source_requires_explicit_availability_and_keeps_provenance(self):
        saved = record()
        public = saved["public_context"]["NVDA"]["items"]
        metadata = {"availability_basis": "reviewed_public_permalink", "publication_precision": "day",
                    "link_scope": "direct_item", "source_kind": "reviewed_public_sample"}
        public[0].update(metadata)
        public.append({"id": "observed-only", "kind": "news", "observed_at": PUBLIC, "title": "Unofficial"})
        projected = assemble(saved)["sources"]
        self.assertNotIn("observed-only", projected)
        self.assertEqual({key: projected["headline"].get(key) for key in metadata}, metadata)

    def test_unsupported_action_change_and_blocked_baseline_are_preserved(self):
        source = record()
        batch = assemble(source)
        subject_id = next(iter(batch["subjects"]))
        wrong = output(subject_id, "official")
        wrong["sections"][0]["proposal"]["evidence_ids"].append("headline")
        brief = validated_briefs(wrong, batch)[0]["holdings"]["NVDA"]
        self.assertEqual((brief["action"], brief["proposed_action"]), ("maintain", None))
        blocked = deepcopy(source)
        blocked["holdings"]["sections"][0]["required_evidence_ids"].append("missing-price")
        brief = validated_briefs(wrong, assemble(blocked))[0]["holdings"]["NVDA"]
        self.assertEqual((brief["status"], brief["action"], brief["proposed_action"]),
                         ("blocked", "review_required", None))

    def test_verified_change_still_requires_independent_review(self):
        source = record()
        source["public_context"]["NVDA"]["items"][0].update(
            text="Verified independent original body.", availability_basis="reviewed_public_permalink",
            summary_basis="original_body_verified",
        )
        batch = assemble(source)
        data = output(next(iter(batch["subjects"])), "official")
        data["sections"][0]["proposal"]["evidence_ids"].append("headline")
        brief = validated_briefs(data, batch)[0]["holdings"]["NVDA"]
        self.assertEqual((brief["status"], brief["action"], brief["proposed_action"]),
                         ("review_required", "maintain", "reduce"))

    def test_failed_model_call_is_retained_without_a_decision_brief(self):
        class FailingModel:
            def invoke(self, prompt, schema, call):
                raise RuntimeError("test transport failure")
        briefs, macro, audit = synthesize(record(), FailingModel())
        self.assertEqual((briefs, macro, audit["status"]), ({}, {}, "error"))
        self.assertEqual(audit["calls"][0]["error"], "test transport failure")
        self.assertEqual(audit["operations"]["failed_calls"], 1)
