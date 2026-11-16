import unittest

from framework_validation.optimized_runner import run_optimized


def issue(issue_id, *, kind="interpretation", material=True, impact="action", subject=None,
          horizon="medium", handling="specialist"):
    return {
        "id": issue_id,
        "kind": kind,
        "material": material,
        "impact": impact,
        "subject_key": subject or issue_id,
        "horizon": horizon,
        "handling": handling,
        "claim": "Test a change in the assessed action.",
        "roles": ["fundamental"],
        "evidence_ids": ["released"],
    }


def review(*issues, resolved=()):
    return {"issues": list(issues), "resolved_ids": list(resolved)}


class ScriptedModel:
    def __init__(self, reviews):
        self.reviews = iter(reviews)
        self.calls = []

    def run(self, role, stage, context):
        self.calls.append((role, stage, context))
        if role == "reviewer":
            return next(self.reviews)
        return {"action": "maintain", "evidence_ids": ["released"]}


class OptimizedRunnerTests(unittest.TestCase):
    def setUp(self):
        self.fixture = {
            "case_id": "fixture",
            "ticker": "TEST",
            "as_of": "2024-01-02T00:00:00Z",
            "horizon": "medium",
            "question": "Assess the outlook.",
            "evidence": [
                {"id": "released", "available_at": "2024-01-01T00:00:00Z"},
                {"id": "future", "available_at": "2024-01-03T00:00:00Z"},
            ],
            "missing_fields": [],
            "mock_reviews": "Never expose this to the model",
        }

    def test_disclosed_limitation_stops_after_independent_review(self):
        model = ScriptedModel([review(issue("ko_fx_scope", material=False, impact="none"))])
        result = run_optimized(self.fixture, model)
        self.assertEqual((result["path"], result["status"]), ("basic", "complete"))
        self.assertEqual(len(model.calls), 7)
        self.assertEqual(result["excluded_evidence"], ["future"])
        for _, _, context in model.calls:
            self.assertNotIn("mock_reviews", context)
            self.assertEqual(list(context["evidence"]), ["released"])

    def test_reconciliation_resolves_condition_without_specialist(self):
        concern = issue(
            "pricing_vs_volume", kind="uncertain_assumption", impact="reassessment", handling="reconcile",
        )
        model = ScriptedModel([review(concern), review(resolved=[concern["id"]])])
        result = run_optimized(self.fixture, model)
        self.assertEqual((result["path"], result["status"]), ("basic", "complete"))
        self.assertEqual([stage for _, stage, _ in model.calls][-2:], ["reconcile", "rereview"])

    def test_distinct_action_issue_routes_directly_and_leaves_unresolved_downside(self):
        mild = issue("separate_downside", impact="downside")
        decisive = issue("maintain_vs_reduce")
        duplicate = issue("duplicate_id", subject=decisive["subject_key"])
        model = ScriptedModel([
            review(mild, decisive, duplicate),
            review(resolved=[decisive["id"]]),
        ])
        result = run_optimized(self.fixture, model)
        self.assertEqual((result["path"], result["status"]), ("debate", "review_required"))
        self.assertEqual([item["id"] for item in result["unresolved_issues"]], [mild["id"]])
        debate = [(role, context["issue"]["id"]) for role, stage, context in model.calls if stage == "debate_open"]
        self.assertEqual(debate, [("bull", decisive["id"]), ("bear", decisive["id"])])

    def test_material_data_error_blocks_reconciliation(self):
        model = ScriptedModel([review(issue("wrong_report", kind="data_error"), issue("dispute"))])
        result = run_optimized(self.fixture, model)
        self.assertEqual((result["path"], result["status"]), ("blocked", "review_required"))
        self.assertIsNone(result["final"])
        self.assertEqual(len(model.calls), 7)

    def test_replay_keeps_four_roles_but_reruns_fundamental_and_review(self):
        original_model = ScriptedModel([review()])
        original = run_optimized(self.fixture, original_model)
        original["calls"] = [
            {**entry, "usage": None} for entry in original["trace"]
        ]
        rerun = ScriptedModel([review()])
        result = run_optimized(self.fixture, rerun, replay=original)
        self.assertEqual([role for role, _, _ in rerun.calls], ["fundamental", "synthesis", "reviewer"])
        self.assertEqual(len(result["trace"]), 7)
        self.assertEqual(sum(entry.get("reused", False) for entry in result["trace"]), 4)
