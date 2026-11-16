from datetime import datetime
import json
import unittest
from unittest.mock import patch

from framework_validation.compact_model import CompactCodexModel
from research_prototype.etf_data import (
    SOXL_COMPARISON, SOXL_DOCUMENTS, SOXL_HOLDINGS, SOXL_TERMS, soxl_holdings, soxl_issuer_terms,
)
from research_prototype.model import PrototypeModel
from research_prototype.report import source_index
from research_prototype.us_data import collect_us_snapshots
from research_prototype.week3 import validate_coverage

from research_prototype.tests.test_us_data import FakePublicClient


NOW = datetime.fromisoformat("2026-11-12T07:00:00+00:00")
CSV = (
    "Direxion holdings\nSOXL\nShares Outstanding:302800060\n\n"
    '"TradeDate","AccountTicker","StockTicker","SecurityDescription","Shares","Price","MarketValue","Cusip","HoldingsPercent"\n'
    '"11/11/2026 12:00:00 AM","SOXL","","ICESEMI SWAP ASSET LEG","1","99","99","","12.50"\n'
    '"11/13/2026 12:00:00 AM","SOXL","NVDA","Future Row","1","25","25","","7"\n'
)
PRODUCT = (
    "<h2>SOXL Direxion Daily Semiconductor Bull 3X ETF</h2>"
    "<div>Daily Target +300% Daily NAV $ <span>55.93</span> Daily Market Price $ <span>56.06</span></div>"
    "<p>Expense Ratio (gross/net %)0.91 / 0.75*</p><h2>SOXS Direxion Daily Semiconductor Bear 3X ETF</h2>"
    "<p>NAV and Market Price information as of 11/10/2026. NYSE Semiconductor Index</p>"
)


class EtfTests(unittest.TestCase):
    def test_issuer_parses_spaced_prices_and_fee_terms(self):
        reference = json.loads(SOXL_DOCUMENTS.read_text(encoding="utf-8"))[0]
        item = soxl_issuer_terms((PRODUCT, NOW), reference)
        self.assertEqual(item["metrics"]["issuer_reported_nav_usd"], 55.93)
        self.assertEqual(item["metrics"]["issuer_reported_market_price_usd"], 56.06)
        self.assertEqual(item["metrics"]["net_expense_ratio_pct"], 0.75)
        self.assertEqual(item["observation_date"], "2026-11-10")
        self.assertEqual(item["nav_market_price_origin"], "live_product_page")

    def test_quoted_holdings_header_and_swap_row_without_ticker(self):
        item = soxl_holdings((CSV, NOW), NOW)
        self.assertEqual(item["observation_date"], "2026-11-11")
        self.assertEqual(item["metrics"]["reported_position_rows"], 1)
        self.assertEqual(item["positions"][0]["description"], "ICESEMI SWAP ASSET LEG")
        self.assertEqual(item["positions"][0]["holdings_percent_raw"], "12.50")

    def test_etf_route_avoids_corporate_gate_and_feeds_alignment_to_models(self):
        class FundClient(FakePublicClient):
            def text(self, url, *, sec=False):
                if url == SOXL_HOLDINGS:
                    return CSV, NOW
                if "direxion.com" in url:
                    return PRODUCT, NOW
                return super().text(url, sec=sec)

            def json(self, url, *, sec=False):
                if sec:
                    raise AssertionError("SOXL must not fetch operating-company SEC facts")
                return super().json(url, sec=sec)

        with patch("research_prototype.us_data.utc_now", return_value=NOW):
            fixture = collect_us_snapshots(["SOXL"], client=FundClient())["SOXL"][1]
        self.assertEqual(fixture["required_evidence_ids"], ["soxl-stock-price-window", SOXL_TERMS])
        self.assertEqual(fixture["horizon"], "short")
        sources = source_index(fixture)
        self.assertEqual(sources["soxl-sector-price-window"]["market_symbol"], "SOXX")
        self.assertEqual(sources[SOXL_COMPARISON]["metrics"]["aligned_1_sessions"], True)
        model = PrototypeModel.__new__(PrototypeModel)
        payload = model.build_payload("fundamental", "analysis", {**fixture, "evidence": sources})
        self.assertEqual(payload["instrument_profile"], "daily_leveraged_etf")
        self.assertIn(SOXL_COMPARISON, payload["evidence"])
        self.assertIn("path dependence", model.build_prompt("fundamental", "analysis", payload))
        report = validate_coverage({"ticker": "SOXL", "sources": sources})
        self.assertEqual(report["fund_structure"]["issuer_nav_date"], "2026-11-10")
        self.assertEqual(report["consensus"]["status"], "not_applicable")

    def test_opt_in_audit_saves_payload_prompt_and_output(self):
        model = CompactCodexModel.__new__(CompactCodexModel)
        model.audit_inputs = True
        model.max_calls = 1
        model.calls = []
        response = {"finding": "sample", "reasoning": [], "limitations": [], "evidence_ids": []}
        with patch.object(model, "invoke", return_value=response) as invoke:
            model.run("macro_market", "analysis", {"evidence": {}})
        call = model.calls[0]
        self.assertEqual(call["prompt"], invoke.call_args.args[0])
        self.assertEqual(call["input_payload"]["evidence"], {})
        self.assertEqual(call["output"], response)

    def test_audit_keeps_prior_payload_when_new_specialist_work_is_added(self):
        model = CompactCodexModel.__new__(CompactCodexModel)
        model.audit_inputs = True
        model.max_calls = 1
        model.calls = []
        additional = [{"finding": "first analysis"}]
        with patch.object(model, "invoke", return_value={"issues": [], "resolved_ids": []}):
            model.run("reviewer", "rereview", {"evidence": {}, "additional": additional})
        additional.append({"finding": "later analysis"})
        saved = model.calls[0]
        self.assertEqual(saved["input_payload"]["additional"], [{"finding": "first analysis"}])
        self.assertEqual(saved["input_payload"], json.loads(saved["prompt"].rsplit("\n", 1)[-1]))
