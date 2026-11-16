from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from framework_validation.optimized_runner import snapshot_digest
from research_prototype.__main__ import main
from research_prototype.presentation import validate_presentation
from research_prototype.week3 import measurement, restore_snapshot, validate_coverage


AS_OF = "2026-11-12T01:13:54+00:00"
STOCK = "meta-stock-price-window"
FILING = "meta-sec-periodic-results"


def evidence(evidence_id, days):
    return {
        "id": evidence_id, "source_url": "https://example.com/evidence",
        "available_at": AS_OF, "collected_at": AS_OF,
        "observation_date": days[-1],
        "observations": [{"session_date": day} for day in days],
        "metrics": {"last_close": 641.39},
    }


def capture(*, omit_filing=False):
    days = ["2026-11-09", "2026-11-10", "2026-11-11"]
    sources = {
        STOCK: evidence(STOCK, days),
        "us-sp500-price-window": evidence("us-sp500-price-window", days),
        "meta-sector-price-window": evidence("meta-sector-price-window", days),
    }
    if not omit_filing:
        sources[FILING] = {
            "id": FILING, "source_url": "https://example.com/filing",
            "available_at": AS_OF, "collected_at": AS_OF, "form": "10-Q",
        }
    fixture = {
        "ticker": "META", "as_of": AS_OF, "horizon": "medium", "question": "Assess the outlook",
        "evidence": list(sources.values()), "missing_fields": [],
    }
    section = {
        **{key: value for key, value in fixture.items() if key != "evidence"},
        "case_id": "us-live-meta", "sources": sources,
        "snapshot_evidence_ids": list(sources), "excluded_evidence": [],
        "snapshot_sha256": snapshot_digest(fixture),
        "status": "review_required", "final": None,
        "path": "blocked" if omit_filing else "basic",
        "calls": [
            {"role": "reviewer", "stage": "review", "usage": {"input_tokens": 100}, "prompt_chars": 75},
        ],
    }
    return {
        "run_id": "original-run", "generated_at": AS_OF, "model_requested": "gpt-6.1-sol",
        "source_mode": "us-live", "holdings": ["META"], "sections": [section],
    }


class NoCallsModel:
    def __init__(self, *args):
        self.calls = []

    def run(self, *args):
        raise AssertionError("A blocked capture must not call the model")


class Week3Tests(unittest.TestCase):
    def test_restore_rejects_snapshot_changes_and_missing_exclusions(self):
        original = capture()
        restored = restore_snapshot(original)
        self.assertEqual(restored["as_of"], AS_OF)
        self.assertEqual(restored["required_evidence_ids"], [STOCK, FILING])
        self.assertEqual(snapshot_digest(restored), original["sections"][0]["snapshot_sha256"])
        changed = deepcopy(original)
        changed["sections"][0]["sources"][STOCK]["metrics"]["last_close"] = 999
        with self.assertRaisesRegex(ValueError, "hash does not match"):
            restore_snapshot(changed)
        changed = deepcopy(original)
        changed["sections"][0]["excluded_evidence"] = ["later-source"]
        with self.assertRaisesRegex(ValueError, "cannot be reconstructed"):
            restore_snapshot(changed)

    def test_partial_usage_is_unknown_and_review_cost_is_visible(self):
        metrics = measurement([
            {"role": "reviewer", "stage": "review", "usage": {"input_tokens": 50}},
            {"role": "synthesis", "stage": "reconcile", "usage": None, "error": "timeout"},
        ])
        self.assertEqual(metrics["calls"], 2)
        self.assertEqual(metrics["extra_calls"], 1)
        self.assertEqual(metrics["failed_calls"], 1)
        self.assertEqual(metrics["calls_without_usage"], 1)
        self.assertEqual(metrics["largest_input_call"]["role"], "reviewer")
        self.assertIsNone(metrics["usage"]["input_tokens"])
        self.assertIsNone(metrics["usage"]["output_tokens"])
        self.assertEqual(measurement([])["usage"]["input_tokens"], 0)

    def test_alignment_checks_more_than_the_last_observation_date(self):
        section = capture()["sections"][0]
        section["sources"]["us-sp500-price-window"]["observations"][-2]["session_date"] = "2026-11-08"
        section["sources"][STOCK]["provider_latest_regular_quote_session"] = "2026-11-12"
        report = validate_coverage(section)
        self.assertTrue(report["same_session"])
        self.assertFalse(report["lookback_session_alignment"]["1"])
        self.assertIsNone(report["lookback_session_alignment"]["5"])
        self.assertEqual(report["daily_bars"]["stock"]["status"], "newer_session_unconfirmed")
        self.assertEqual(report["valuation"]["observed_close_usd"], 641.39)
        self.assertIsNone(report["valuation"]["estimated_fair_value"])

    def run_cli(self, capture_data, *, reanalyze=False, presentation=None):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            saved = root / "original.json"
            output = root / "out"
            saved.write_text(json.dumps(capture_data), encoding="utf-8")
            arguments = ["research_prototype", "--week3", "--replay", str(saved), "--output-dir", str(output)]
            if reanalyze:
                arguments.append("--reanalyze")
            if presentation is not None:
                layout = root / "layout.json"
                layout.write_text(json.dumps(presentation), encoding="utf-8")
                arguments.extend(["--presentation", str(layout)])
            with (
                patch.object(sys, "argv", arguments),
                patch("research_prototype.__main__.collect_us_snapshots",
                      side_effect=AssertionError("Replay must not collect live data")),
                patch("research_prototype.__main__.PrototypeModel", NoCallsModel),
                redirect_stdout(StringIO()),
            ):
                exit_code = main()
            return (
                exit_code,
                json.loads((output / "evidence.json").read_text(encoding="utf-8")),
                (output / "report.md").read_text(encoding="utf-8"),
            )

    def test_render_replay_does_not_charge_original_call_to_new_run(self):
        exit_code, output, report = self.run_cli(capture())
        self.assertEqual(exit_code, 0)
        self.assertEqual(output["run_id"], "original-run")
        operations = output["week3"]["operations"]
        self.assertEqual(operations["this_run"]["calls"], 0)
        self.assertEqual(operations["original_capture"]["calls"], 1)
        self.assertIsNone(operations["original_capture"]["usage"]["output_tokens"])
        self.assertIn("원본 분석 · 모델 호출 1회", report)
        self.assertIn("이번 실행 · 모델 호출 0회", report)

    def test_reanalysis_stays_blocked_without_required_filing(self):
        exit_code, output, _ = self.run_cli(capture(omit_filing=True), reanalyze=True)
        self.assertEqual(exit_code, 1)
        self.assertEqual(output["week3"]["mode"], "frozen_reanalysis")
        self.assertEqual(output["sections"][0]["as_of"], AS_OF)
        self.assertEqual(output["sections"][0]["gate_decisions"][0]["evidence_ids"], [FILING])
        self.assertEqual(output["week3"]["operations"]["this_run"]["calls"], 0)

    def test_editorial_presentation_keeps_original_decision_and_separates_counterargument(self):
        original = capture()
        final = {
            "action": "reduce", "outlook": "Review exposure.", "reasoning": ["Price risk."],
            "counterargument": {"action": "maintain", "rationale": "Short rebound.", "evidence_ids": [STOCK]},
            "downside_and_uncertainty": [], "reassessment_conditions": ["Review if momentum improves."],
            "evidence_ids": [STOCK],
        }
        original["sections"][0]["final"] = final
        presentation = {
            "source_run_id": original["run_id"], "ticker": "META", "applicability": "Short horizon.",
            "weighing": {"text": "Rebound is outweighed by price risk.", "evidence_ids": [STOCK]},
            "checks": [{"when": "Next close", "criterion": "Momentum improves",
                        "action": "Review", "evidence_ids": [STOCK]}],
        }
        exit_code, output, report = self.run_cli(original, presentation=presentation)
        self.assertEqual(exit_code, 0)
        self.assertEqual(output["sections"][0]["final"], final)
        self.assertEqual(output["week3"]["operations"]["this_run"]["calls"], 0)
        self.assertIn("잠정 행동 제안 **축소**", report.split("## META 판단 요약")[1].split("## META\n")[0])
        self.assertIn("대안 행동 **유지**", report)
        self.assertIn("최종 판단에서 더 무게를 둔 근거", report)
        self.assertIn("| Next close | Momentum improves | Review |", report)
        presentation["weighing"]["evidence_ids"] = ["unknown"]
        with self.assertRaisesRegex(ValueError, "cite saved evidence"):
            validate_presentation(original, presentation)
