from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
import unittest
from unittest.mock import patch

from research_prototype.integrated import SharedClient, collect_inputs, run_reports
from research_prototype.report import render_report


class IntegratedTests(unittest.TestCase):
    def test_identical_requests_share_provider_result_across_workers(self):
        fetched = datetime(2026, 11, 16, 20, tzinfo=timezone.utc)
        client = SharedClient()
        with patch("research_prototype.discovery_data.DiscoveryClient.text", return_value=('{"a":1}', fetched)) as upstream:
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: client.json("https://example.com/data"), range(2)))
        self.assertEqual(results, [({"a": 1}, fetched), ({"a": 1}, fetched)])
        self.assertEqual(upstream.call_count, 1)

    def test_invalid_symbol_does_not_abort_held_and_discovery_collection(self):
        snapshot = {"as_of": "2026-11-16T19:00:00+00:00", "evidence": []}
        discovered = {"as_of": "2026-11-16T20:00:00+00:00", "universe": []}
        client = SharedClient()
        with (patch("research_prototype.integrated.collect_us_snapshots",
                    return_value={"SOXL": (None, dict(snapshot))}) as holdings,
              patch("research_prototype.integrated.collect_discovery", return_value=discovered) as discovery,
              patch("research_prototype.integrated.collect_qualitative"),
              patch("research_prototype.integrated.prepare_business") as business,
              patch("research_prototype.integrated.load_registry")):
            frozen = collect_inputs(["BAD/SYMBOL", "SOXL"], client, business_cache=Path("verified-custom-cache"))
        self.assertEqual(holdings.call_args.args[0], ["SOXL"])
        self.assertEqual(discovery.call_args.args[0], ["SOXL"])
        self.assertIn("BAD/SYMBOL", frozen["holding_errors"])
        self.assertEqual(frozen["as_of"], frozen["snapshots"]["SOXL"][1]["as_of"])
        self.assertEqual(frozen["as_of"], frozen["discovery"]["as_of"])
        business.assert_called_once()
        self.assertEqual(business.call_args.args[2], Path("verified-custom-cache"))

    def test_business_analysis_reuses_the_configured_client_and_cache(self):
        cutoff = "2026-11-19T08:43:10+00:00"
        discovery = {"as_of": cutoff, "discovery_method": "business_first", "calls": []}
        captured = {"as_of": cutoff, "snapshots": {}, "holding_errors": {}, "discovery": discovery}
        client = SharedClient()
        cache = Path("verified-existing-cache")
        with patch("research_prototype.integrated.run_business") as business:
            run_reports([], captured, "test-model", 600, True, client=client, business_cache=cache)
        self.assertIs(business.call_args.args[1], client)
        self.assertIsNone(business.call_args.args[2])
        self.assertEqual(business.call_args.args[3], cache)

    def test_unverified_but_well_formed_symbol_blocks_without_skipping_another_symbol(self):
        cutoff = "2026-11-16T20:00:00+00:00"
        required = "required-source"
        fixtures = {
            ticker: (None, {"ticker": ticker, "case_id": ticker, "as_of": cutoff, "horizon": "short",
                            "question": "Assess", "missing_fields": [], "required_evidence_ids": [required],
                            "evidence": evidence})
            for ticker, evidence in (("AVDA", []), ("SOXL", [
                {"id": required, "source_url": "https://example.com/source", "available_at": cutoff,
                 "collected_at": cutoff},
            ]))
        }

        class Model:
            def __init__(self, *args, **kwargs):
                self.calls = []

            def run(self, role, stage, context):
                self.calls.append({"role": role, "stage": stage, "usage": None})
                if role == "reviewer":
                    return {"issues": [], "resolved_ids": []}
                if role == "synthesis":
                    return {"action": "maintain", "outlook": "유지", "reasoning": ["근거"],
                            "reassessment_conditions": ["다음 거래일"], "downside_and_uncertainty": [],
                            "counterargument": {"action": "none", "rationale": "자료 부족", "evidence_ids": []},
                            "evidence_ids": [required]}
                return {"evidence_ids": [required]}

        captured = {"as_of": cutoff, "snapshots": fixtures, "holding_errors": {}, "discovery": None}
        with patch("research_prototype.integrated.PrototypeModel", Model):
            _, holdings, _, operations = run_reports(["AVDA", "SOXL"], captured, "test-model", 10, False)
        blocked, completed = holdings["sections"]
        self.assertEqual((blocked["ticker"], blocked["path"], blocked["calls"]), ("AVDA", "blocked", []))
        self.assertEqual((completed["ticker"], completed["status"]), ("SOXL", "complete"))
        self.assertEqual(operations["calls"], 7)
        self.assertIn(f"분석 기준 UTC `{cutoff}`", render_report(holdings))
