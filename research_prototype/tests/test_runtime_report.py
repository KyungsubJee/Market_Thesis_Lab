from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from research_prototype.runtime_report import build_audit, exit_classification, judgment_quality, load_monitor, load_monitors, render_audit, safe


def fixture():
    return {"run_id": "fresh", "as_of": "2026-11-19T07:00:00+00:00",
            "holdings": {"sections": [{"ticker": "005930", "status": "review_required", "path": "blocked",
                "sources": {}, "final": {"action": "maintain"}, "calls": [{"role": "old_duplicate", "stage": "draft"}]}]},
            "coverage": {"holdings": {"tickers": {"005930": {"blocking": ["own-price"], "checks": [
                ["필수", "자체 가격", "missing", None]]}}}},
            "discovery": {"model_status": "complete", "missing_required": [], "sources": {},
                          "business_screen": {"status": "complete", "calls": []},
                          "business_deep": {"status": "complete", "calls": []}}}


class RuntimeReportTests(unittest.TestCase):
    def test_monitor_keeps_distinct_attempts_and_unknown_usage_without_double_counting_evidence(self):
        record = fixture()
        monitor = {"provided": True, "summary": {"call_count": 2, "finished_at": "2026-11-19T08:00:00Z", "status": "complete"},
                   "files": [], "events": [], "calls": [
                       {"call_id": "1", "role": "analyst", "stage": "analysis", "seconds": 3,
                        "usage": {"input_tokens": 100, "output_tokens": 20, "cached_input_tokens": 0}},
                       {"call_id": "2", "role": "analyst", "stage": "analysis", "seconds": 4, "usage": None,
                        "error": "quota error"}]}
        original = deepcopy(record)
        audit = build_audit(record, monitor, expected_kr="2026-11-19")
        self.assertEqual(audit["operations"]["calls"], 2)
        self.assertEqual(audit["operations"]["failed_calls"], 1)
        self.assertIsNone(audit["operations"]["usage"]["input_tokens"])
        self.assertEqual(audit["operations"]["calls_without_usage"], 1)
        self.assertEqual(record, original)
        report = render_audit(audit, "evidence.json", "abc")
        self.assertIn("판단 보류", report)
        self.assertIn("실제 관측 미확인 / 기대 2026-11-19", report)
        self.assertIn("입력 미집계", report)
        self.assertNotIn("old_duplicate", report)

    def test_monitor_checks_file_count_ids_and_unfinished_phases(self):
        monitor = {"provided": True, "summary": {"call_count": 2}, "files": [], "calls": [],
                   "events": [{"event": "phase_started", "phase": "수집", "elapsed_seconds": 0},
                              {"event": "phase_completed", "phase": "수집", "elapsed_seconds": 9},
                              {"event": "phase_started", "phase": "분석", "elapsed_seconds": 9}]}
        audit = build_audit(fixture(), monitor)
        self.assertEqual(audit["phases"][0]["seconds"], 9)
        self.assertEqual(audit["phases"][1]["status"], "not_finished")
        self.assertTrue(any("호출 수" in note for note in audit["integrity_notices"]))
        self.assertTrue(any("종료시각" in note for note in audit["integrity_notices"]))

    def test_monitor_loads_call_records_and_traces_without_printing_prompt(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "calls").mkdir()
            (root / "summary.json").write_text(json.dumps({"call_count": 1, "finished_at": "2026-11-19T08:00:00Z"}), encoding="utf-8")
            (root / "events.jsonl").write_text('{"event":"run_finished"}\n', encoding="utf-8")
            (root / "calls/0001.json").write_text(json.dumps({"call_id": "0001", "role": "analyst", "stage": "analysis",
                "prompt": "SECRET PROMPT", "record": {"role": "analyst", "stage": "analysis", "seconds": 1,
                "usage": {"input_tokens": 10, "output_tokens": 2}}}), encoding="utf-8")
            monitor = load_monitor(root)
        audit = build_audit(fixture(), monitor)
        self.assertEqual(len(audit["monitor_files"]), 3)
        self.assertEqual(audit["operations"]["usage"]["input_tokens"], 10)
        self.assertNotIn("SECRET PROMPT", json.dumps(audit))
        self.assertNotIn("SECRET PROMPT", render_audit(audit, "evidence.json", "abc"))

    def test_source_errors_redact_keys_and_projection_is_not_reported_as_measured_token_savings(self):
        record = fixture()
        record["shared_requests"] = {"requests": [{"status": "unavailable", "error": "https://x.test/?api_key=secret&x=1"}]}
        record["discovery"]["business_deep"]["input_projection"] = {
            "before_chars": 100, "after_chars": 50, "removed_texts": [{"source_id": "x"}], "dropped_passages": 0}
        audit = build_audit(record)
        report = render_audit(audit, "evidence.json", "abc")
        self.assertNotIn("secret", report)
        self.assertIn("토큰·금액 절감률로 환산하지 않습니다", report)
        self.assertEqual(safe(False), "False")
        self.assertTrue(audit["integrity_notices"])

    def test_official_http_success_does_not_promote_dated_source_to_today_verified(self):
        supplement = {"sources": [{"status": "verified_core_text"}, {"status": "dated_unrevalidated"}],
            "records": [{"ticker": "BOIL", "id": "boil-outlook", "status": "dated_unrevalidated", "http_status": 200,
                         "extracted_characters": 0, "published_at": "2026-11-10"},
                        {"ticker": "KOLD", "id": "kold-outlook", "status": "dated_unrevalidated", "http_status": 200,
                         "extracted_characters": 0, "published_at": "2026-11-10"},
                        {"ticker": "NVDA", "status": "verified_core_text"}]}
        original = deepcopy(supplement)
        audit = build_audit(fixture(), supplement_audit=supplement)
        report = render_audit(audit, "evidence.json", "abc")
        self.assertIn("고유 출처 2개 / 종목별 기록 3개", report)
        self.assertIn("핵심 본문 확인 1개 / 일부 확인 0개 / 이전 자료·오늘 본문 미확인 1개", report)
        self.assertIn("입력 품질 확인 필요 · BOIL, KOLD", report)
        self.assertIn("HTTP 200 / 추출 0자", report)
        self.assertIn("오늘 새로 검증한 사실로 취급하지 않았습니다", report)
        self.assertEqual(supplement, original)

    def test_original_and_recovery_monitors_count_both_and_preserve_initial_failures(self):
        old = {"role": "business_screen", "stage": "business_screen", "seconds": 5,
               "usage": {"input_tokens": 8, "output_tokens": 2}, "output": {"reviews": []}}
        new = {"role": "business_screen", "stage": "business_screen", "seconds": 8,
               "usage": {"input_tokens": 10, "output_tokens": 4}, "output": {"reviews": ["recovered"]}}
        with TemporaryDirectory() as directory:
            paths = []
            for name, record, start, end in [("original", old, "2026-11-19T07:00:00Z", "2026-11-19T07:01:00Z"),
                                              ("recovery", new, "2026-11-19T07:05:00Z", "2026-11-19T07:06:00Z")]:
                path = Path(directory) / name
                (path / "calls").mkdir(parents=True)
                (path / "summary.json").write_text(json.dumps({"started_at": start, "finished_at": end,
                    "status": "complete", "wall_seconds": 60, "call_count": 1}), encoding="utf-8")
                (path / "calls/0001.json").write_text(json.dumps({"call_id": 1, "record": record}), encoding="utf-8")
                events = [{"event": "phase_started", "phase": "business", "elapsed_seconds": 0},
                          {"event": "phase_completed", "phase": "business", "elapsed_seconds": record["seconds"]}]
                if name == "original":
                    events.append({"event": "source_request", "status": "unavailable", "error": "HTTP 403",
                                   "url": "https://sec.test/document"})
                (path / "events.jsonl").write_text("\n".join(json.dumps(event) for event in events), encoding="utf-8")
                paths.append(path)
            monitor = load_monitors(paths[0], paths[1:])
            with self.assertRaises(ValueError):
                load_monitors(paths[0], [paths[0]])
        record = fixture()
        record["holdings"]["sections"][0]["calls"] = []
        record["discovery"]["business_screen"]["calls"] = [new]
        audit = build_audit(record, monitor)
        self.assertEqual(audit["operations"]["calls"], 2)
        self.assertEqual(audit["operations"]["usage"]["input_tokens"], 18)
        self.assertEqual([call["evidence_link"] for call in audit["calls"]], ["earlier_or_unmatched", "final_evidence"])
        self.assertEqual([row["seconds"] for row in audit["phases"]], [5, 8])
        self.assertEqual(audit["monitor_summary"]["wall_seconds"], 360)
        self.assertEqual(audit["monitor_summary"]["summed_program_seconds"], 120)
        self.assertFalse(audit["integrity_notices"])
        report = render_audit(audit, "evidence.json", "sha")
        self.assertIn("HTTP 403", report)
        self.assertIn("https://sec.test/document", report)
        self.assertIn("original", report)
        self.assertIn("recovery", report)

    def test_public_collection_is_not_counted_as_cited_integration_and_open_review_remains_visible(self):
        record = fixture()
        record["holdings"]["sections"] = [{"ticker": "SOXS", "status": "review_required", "sources": {},
            "final": {"action": "maintain"}, "calls": [
                {"role": "synthesis", "stage": "draft", "output": {"action": "reduce"}},
                {"role": "synthesis", "stage": "reconcile", "output": {"action": "maintain"}},
                {"role": "reviewer", "stage": "rereview", "output": {"issues": [
                    {"id": "maintain-risk", "material": True, "claim": "유지 우위는 아직 검증하지 못했습니다.", "impact": "action"}]}}]}]
        record["public_context"] = {"SOXS": {"items": [{"id": "qualitative-soxs-new"}]}}
        record["decision_briefs"] = {"holdings": {"SOXS": {"proposal": {"text": "잠정 유지", "evidence_ids": ["soxs-fund-terms"]}}}}
        quality = judgment_quality(record)
        self.assertEqual(quality["public_adoption"]["collected_unique_items"], 1)
        self.assertEqual(quality["public_adoption"]["cited_unique_items"], 0)
        self.assertTrue(quality["action_changes"][0]["material_issues_remain"])
        report = render_audit(build_audit(record), "evidence.json", "sha")
        self.assertIn("실제 채택된 정보는 0건", report)
        self.assertIn("SOXS 미해결 중요 쟁점", report)
        self.assertIn("축소→유지", report)
        record["decision_briefs"]["holdings"]["SOXS"]["proposal"]["evidence_ids"].append("qualitative-soxs-new")
        self.assertEqual(judgment_quality(record)["public_adoption"]["cited_unique_items"], 1)

    def test_exit_one_is_quality_limitation_only_when_saved_models_complete_and_inputs_blocked(self):
        monitor = {"summary": {"exit_code": 1, "status": "error", "error": None}, "events": []}
        ops = {"calls": 1, "failed_calls": 0}
        rows = [{"blocked": True, "status": "review_required", "observed_session": None}]
        artifacts = {"holdings.pdf": {"exists": True}, "discovery.pdf": {"exists": True}}
        result = exit_classification(fixture(), monitor, ops, rows, artifacts)
        self.assertIn("필수 시세 검증 미충족", result["summary"])
        self.assertEqual(result["runs"][0]["status"], "error")
        self.assertEqual(result["runs"][0]["exit_code"], 1)
        rows[0]["blocked"] = False
        self.assertIn("원인 미확정", exit_classification(fixture(), monitor, ops, rows, artifacts)["runs"][0]["interpretation"])
        monitor["summary"]["error"] = "Python exception"
        self.assertTrue(exit_classification(fixture(), monitor, ops, rows, artifacts)["runs"][0]["exception_recorded"])


if __name__ == "__main__":
    unittest.main()
