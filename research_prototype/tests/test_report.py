from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from framework_validation.runner import FIXTURE_DIR
from research_prototype.model import PrototypeModel
from research_prototype.report import collect, load_holdings, load_snapshots, write_report


EVIDENCE_ID = "samsung_q3_results"
SYNTHESIS = {
    "action": "maintain",
    "outlook": "중기 회복은 확인이 필요하다.",
    "reasoning": ["확정 실적은 잠정치를 웃돌았다."],
    "counterargument": {
        "action": "reduce",
        "rationale": "DS 영업이익 하락은 지금 비중을 축소할 근거다.",
        "evidence_ids": [EVIDENCE_ID],
    },
    "reassessment_conditions": ["다음 분기 DS 이익률이 더 하락하면 다시 판단한다."],
    "downside_and_uncertainty": ["일회성 비용의 이익 영향은 수치화할 수 없다."],
    "evidence_ids": [EVIDENCE_ID],
}


class LocalModel:
    def __init__(self):
        self.calls = []
        self.contexts = []

    def run(self, role, stage, context):
        self.calls.append({"role": role, "stage": stage, "usage": None})
        self.contexts.append(context)
        if role == "reviewer":
            return {
                "issues": [{
                    "id": "ds-cost-attribution",
                    "kind": "uncertain_assumption",
                    "horizon": "medium",
                    "subject_key": "ds-cost-attribution",
                    "impact": "action",
                    "handling": "reconcile",
                    "material": True,
                    "claim": "일회성 비용의 기여는 분리되지 않았다.",
                    "evidence_ids": [EVIDENCE_ID],
                }],
                "resolved_ids": [],
            }
        if role == "synthesis":
            return deepcopy(SYNTHESIS)
        return {"evidence_ids": [EVIDENCE_ID]}


class ReportTests(unittest.TestCase):
    def test_csv_preserves_leading_zero_and_filters_unheld_rows(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "holdings.csv"
            path.write_text("ticker,held\n005930,true\nKO,false\nMETA,\n", encoding="utf-8-sig")
            self.assertEqual(load_holdings(path), ["005930", "META"])

    def test_evidence_pair_preserves_provisional_action_counterargument_and_cutoff(self):
        model = LocalModel()
        run = collect(["005930", "UNKNOWN"], load_snapshots(FIXTURE_DIR), lambda: model, "test-model")
        active, missing = run["sections"]
        self.assertEqual(active["status"], "review_required")
        self.assertEqual(missing["status"], "execution_error")
        self.assertTrue(all("mock_reviews" not in context for context in model.contexts))
        with TemporaryDirectory() as directory:
            directory = Path(directory)
            write_report(directory, run)
            markdown = (directory / "report.md").read_text(encoding="utf-8")
            evidence = json.loads((directory / "evidence.json").read_text(encoding="utf-8"))
        self.assertIn(evidence["run_id"], markdown)
        self.assertIn("## 005930", markdown)
        self.assertIn("2024-11-01T15:30:00+09:00", markdown)
        self.assertIn("검토 상태 **검토 필요**", markdown)
        self.assertIn("잠정 행동 제안 **유지**", markdown)
        self.assertIn("### 핵심 반론\n\n- 대안 행동 **축소**", markdown)
        self.assertIn("### 재검토 조건", markdown)
        self.assertIn("### 미해결 쟁점", markdown)
        source = evidence["sections"][0]["sources"][EVIDENCE_ID]
        self.assertIn(source["source_url"], markdown)
        self.assertIn(source["available_at"], markdown)

    def test_synthesis_rejects_unlinked_counterargument_and_absent_trigger(self):
        model = PrototypeModel.__new__(PrototypeModel)
        result = deepcopy(SYNTHESIS)
        self.assertEqual(model.cited_evidence("synthesis", result), {EVIDENCE_ID})
        result["counterargument"]["evidence_ids"] = ["unknown"]
        with self.assertRaisesRegex(ValueError, "missing from the synthesis"):
            model.cited_evidence("synthesis", result)
        result = deepcopy(SYNTHESIS)
        result["reassessment_conditions"] = []
        with self.assertRaisesRegex(ValueError, "missing"):
            model.cited_evidence("synthesis", result)
