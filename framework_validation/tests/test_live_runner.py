import unittest

from framework_validation.diagnostic_runner import run_diagnostic
from framework_validation.live_runner import run_comparison


class RecordingModel:
    def __init__(self, issues):
        self.issues = issues
        self.calls = []

    def run(self, role, stage, context):
        self.calls.append((role, stage, context))
        if stage == "review":
            return {"issues": self.issues}
        if stage == "rereview":
            return {"issues": []}
        return {"evidence_ids": list(context["evidence"]), "stage": stage}


class LiveComparisonTests(unittest.TestCase):
    def setUp(self):
        self.fixture = {
            "case_id": "test",
            "ticker": "TEST",
            "horizon": "medium",
            "question": "Assess the outlook.",
            "as_of": "2024-01-02T00:00:00+00:00",
            "missing_fields": [],
            "evidence": [
                {"id": "valid", "available_at": "2024-01-01T00:00:00+00:00"},
                {"id": "future", "available_at": "2024-01-03T00:00:00+00:00"},
            ],
            "mock_reviews": "fixture-only expectation",
            "expected_path": "fixture-only route",
        }

    def test_no_escalation_reuses_final(self):
        model = RecordingModel([])
        result = run_comparison(self.fixture, model)
        self.assertFalse(result["comparison"]["treatment_applied"])
        self.assertEqual(result["branches"]["baseline"]["final"], result["branches"]["escalated"]["final"])
        self.assertEqual(sum(stage == "final" for _, stage, _ in model.calls), 1)

    def test_material_issue_uses_shared_inputs_but_distinct_finals(self):
        issue = {
            "id": "needs_retest",
            "kind": "uncertain_assumption",
            "material": True,
            "claim": "Retest the decisive assumption.",
            "roles": ["fundamental"],
            "evidence_ids": ["valid"],
        }
        model = RecordingModel([issue])
        result = run_comparison(self.fixture, model)
        self.assertEqual(result["branches"]["baseline"]["path"], "basic")
        self.assertEqual(result["branches"]["baseline"]["unresolved_issues"], ["needs_retest"])
        self.assertEqual(result["branches"]["escalated"]["path"], "secondary")
        self.assertTrue(result["comparison"]["treatment_applied"])
        self.assertEqual(sum(stage == "review" for _, stage, _ in model.calls), 1)
        baseline, escalated = [context for _, stage, context in model.calls if stage == "final"]
        for key in ("analyses", "draft", "initial_review", "evidence"):
            self.assertEqual(baseline[key], escalated[key])
        self.assertEqual(baseline["additional"], [])
        self.assertTrue(escalated["additional"])
        for _, _, context in model.calls:
            self.assertNotIn("mock_reviews", context)
            self.assertNotIn("expected_path", context)
            self.assertEqual(set(context["evidence"]), {"valid"})

    def test_outside_snapshot_citation_is_an_error(self):
        issue = {
            "id": "bad_citation",
            "kind": "data_error",
            "material": True,
            "claim": "An ineligible evidence ID was cited.",
            "roles": [],
            "evidence_ids": ["future"],
        }
        result = run_comparison(self.fixture, RecordingModel([issue]))
        self.assertEqual(result["branches"], {})
        self.assertIn("outside the snapshot", result["errors"]["baseline"])

    def test_forced_diagnostic_reuses_natural_analysis_and_preserves_review(self):
        natural_model = RecordingModel([])
        natural = run_comparison(self.fixture, natural_model)
        natural["calls"] = [
            {"role": role, "stage": stage, "output": {"evidence_ids": ["valid"], "stage": stage}}
            for role, stage, _ in natural_model.calls
        ]
        diagnostic_model = RecordingModel([])
        issue = {
            "id": "diagnostic",
            "kind": "interpretation",
            "material": True,
            "claim": "Compare two readings of the same evidence.",
            "roles": [],
            "evidence_ids": ["valid"],
        }

        result = run_diagnostic(self.fixture, diagnostic_model, natural, issue)

        self.assertEqual(result["mode"], "forced_diagnostic")
        self.assertEqual(result["natural_initial_review"], {"issues": []})
        self.assertEqual(result["baseline"], natural["branches"]["baseline"])
        self.assertEqual(result["diagnostic"]["path"], "debate")
        self.assertEqual(
            [stage for _, stage, _ in diagnostic_model.calls],
            ["debate_open", "debate_open", "debate_reply", "debate_reply", "rereview", "final"],
        )
