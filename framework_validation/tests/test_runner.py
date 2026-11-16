from copy import deepcopy
from pathlib import Path
import unittest

from framework_validation.runner import MockModel, load_fixture, run_case


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


class RunnerTests(unittest.TestCase):
    def load_case(self, name):
        return load_fixture(FIXTURES / f"{name}.json")

    def run_fixture(self, fixture):
        return run_case(fixture, MockModel(fixture))

    def test_expected_paths(self):
        cases = (
            ("coca_cola", "basic", set()),
            ("samsung", "secondary", {"secondary", "rereview"}),
            ("meta", "debate", {"debate_open", "debate_reply", "rereview"}),
        )
        escalations = {"secondary", "debate_open", "debate_reply", "rereview"}
        for name, path, expected_escalations in cases:
            with self.subTest(fixture=name):
                result = self.run_fixture(self.load_case(name))
                stages = [entry["stage"] for entry in result["trace"]]
                self.assertEqual(result["path"], path)
                self.assertEqual(result["status"], "complete")
                self.assertEqual(set(stages) & escalations, expected_escalations)
                self.assertEqual(stages[-1], "final")
                self.assertEqual(result["unresolved_issues"], [])
                self.assertEqual(
                    [entry["role"] for entry in result["trace"][:7]],
                    ["macro_market", "sector", "fundamental", "price_trading",
                     "event_attention", "synthesis", "reviewer"],
                )

    def test_nonmaterial_issue_does_not_escalate(self):
        fixture = self.load_case("meta")
        issue = deepcopy(fixture["mock_reviews"][0]["issues"][0])
        issue["material"] = False
        fixture["mock_reviews"] = [{"issues": [issue]}]

        result = self.run_fixture(fixture)

        self.assertEqual(result["path"], "basic")
        self.assertEqual(result["status"], "complete")
        self.assertNotIn("debate_open", [entry["stage"] for entry in result["trace"]])

    def test_persistent_material_issue_does_not_repeat_debate(self):
        fixture = self.load_case("meta")
        first_review = fixture["mock_reviews"][0]
        issue_id = first_review["issues"][0]["id"]
        fixture["mock_reviews"] = [first_review, deepcopy(first_review)]

        result = self.run_fixture(fixture)

        self.assertEqual(result["path"], "debate")
        self.assertEqual(result["status"], "review_required")
        self.assertIn(issue_id, result["unresolved_issues"])
        trace = result["trace"]
        for stage in ("debate_open", "debate_reply"):
            self.assertCountEqual(
                [entry["role"] for entry in trace if entry["stage"] == stage],
                ["bull", "bear"],
            )
        self.assertEqual(sum(entry["stage"] == "rereview" for entry in trace), 1)

    def test_future_evidence_is_excluded_from_every_stage(self):
        fixture = self.load_case("coca_cola")
        future = deepcopy(fixture["evidence"][0])
        future["id"] = "future-evidence"
        future["available_at"] = "2099-01-01T00:00:00Z"
        fixture["evidence"].append(future)

        result = self.run_fixture(fixture)

        self.assertEqual(result["status"], "complete")
        self.assertIn(future["id"], result["excluded_evidence"])
        for entry in result["trace"]:
            with self.subTest(role=entry["role"], stage=entry["stage"]):
                self.assertNotIn(future["id"], entry["evidence_ids"])

    def test_missing_required_evidence_blocks_model_calls(self):
        fixture = self.load_case("coca_cola")
        required_id = fixture["required_evidence_ids"][0]
        fixture["evidence"] = [item for item in fixture["evidence"] if item["id"] != required_id]

        result = self.run_fixture(fixture)

        self.assertEqual(result["path"], "blocked")
        self.assertEqual(result["status"], "review_required")
        self.assertEqual(result["trace"], [])
        self.assertEqual(result["unresolved_issues"], [required_id])

    def test_material_data_error_takes_precedence_over_debate(self):
        fixture = self.load_case("meta")
        dispute = fixture["mock_reviews"][0]["issues"][0]
        error = {**dispute, "id": "data-error", "kind": "data_error"}
        fixture["mock_reviews"] = [{"issues": [dispute, error]}]

        result = self.run_fixture(fixture)

        self.assertEqual(result["path"], "blocked")
        self.assertEqual(result["status"], "review_required")
        self.assertIsNone(result["final"])
        self.assertEqual(result["trace"][-1]["stage"], "review")

    def test_secondary_review_can_open_a_new_interpretation_debate(self):
        fixture = self.load_case("samsung")
        uncertain = fixture["mock_reviews"][0]["issues"][0]
        dispute = {**uncertain, "id": "remaining-dispute", "kind": "interpretation"}
        fixture["mock_reviews"] = [{"issues": [uncertain]}, {"issues": [dispute]}, {"issues": []}]

        result = self.run_fixture(fixture)

        self.assertEqual(result["path"], "secondary+debate")
        self.assertEqual(result["status"], "complete")
        stages = [entry["stage"] for entry in result["trace"]]
        self.assertLess(stages.index("secondary"), stages.index("debate_open"))
        self.assertEqual(stages.count("rereview"), 2)
