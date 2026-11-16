from datetime import datetime
import unittest

from research_prototype.events import aligned_reaction, enrich_events, filing_excerpt, relevant_headline, select_manifest
from research_prototype.integrated import restore_run


class EventTests(unittest.TestCase):
    def test_news_title_requires_a_subject_and_ignores_passive_feed_mentions(self):
        self.assertFalse(relevant_headline("Walmart Earnings Shipment Breaks Records", "COST", "Costco Wholesale Corp"))
        self.assertTrue(relevant_headline("Costco Wholesale Reports October Revenue", "COST", "Costco Wholesale Corp"))
        self.assertTrue(relevant_headline("Chip stocks decline after export restrictions", "SOXL",
                                          sector_terms=("chip stocks",)))

    def test_item_502_reads_document_body(self):
        body = (
            "<div>Item 5.02. Departure of Directors or Certain Officers</div>"
            "<div>On November 11 the Board appointed Alex Lee to serve as Chief Financial Officer. "
            "The appointment will become effective on December 1.</div>"
            "<div>Item 9.01. Financial Statements and Exhibits</div><div>Unrelated exhibit details</div>"
        )
        excerpt = filing_excerpt(body, ["5.02"])
        self.assertIn("Alex Lee", excerpt)
        self.assertNotIn("Unrelated exhibit", excerpt)

    def test_intraday_event_uses_aligned_completed_before_and_after_sessions(self):
        def row(date, close, volume=100):
            return {"session_date": date, "session_completion_bound": date + "T21:00:00Z",
                    "close": close, "volume": volume}

        stock = {"id": "price-x", "observations": [row("2026-11-11", 100), row("2026-11-12", 104), row("2026-11-13", 110)]}
        benchmark = {"id": "price-spy", "observations": [row("2026-11-11", 50), row("2026-11-12", 51)]}
        reaction = aligned_reaction("2026-11-12T13:52:41Z", stock, benchmark, "2026-11-12T21:01:00Z")
        self.assertEqual((reaction["before"], reaction["after"]), ("2026-11-11", "2026-11-12"))
        self.assertEqual((reaction["stock_pct"], reaction["benchmark_pct"]), (4.0, 2.0))
        self.assertIsNone(aligned_reaction("2026-11-12T21:05:00Z", stock, benchmark, "2026-11-13T05:00:00Z"))

    def test_headline_registry_admits_only_scoped_pre_cutoff_source(self):
        source = {"ticker": "OTHER", "url": "https://example.org/news", "published_at": "2026-11-12T13:00:00Z",
                  "kind": "independent_news", "title_pattern": "Company update"}
        plan, diagnostics = select_manifest([
            source, {**source, "published_at": "2026-11-13T12:00:00Z"},
            {**source, "ticker": "OUTSIDE"},
        ], {"OTHER"}, datetime.fromisoformat("2026-11-12T20:00:00Z"))
        self.assertEqual(plan, [source])
        self.assertEqual([item["status"] for item in diagnostics], ["after_cutoff", "ticker_out_of_scope"])

    def test_replay_enriches_specific_disclosed_event_without_refreshing_price_bars(self):
        cutoff = "2026-11-13T00:53:56Z"
        original_evidence = [
            {"id": "sec-nvda", "form": "8-K", "accession": "0001045810-26-000082",
             "sec_acceptance_at": "2026-11-12T13:52:41Z", "sec_event_items": "5.02,9.01",
             "primary_document": "nvda-20261111.htm",
             "source_url": "https://www.sec.gov/Archives/edgar/data/1045810/000104581026000082/index.html"},
        ]
        for symbol, closes in (("NVDA", [100, 95.66]), ("SPY", [100, 99.646]), ("SOXX", [100, 99.736])):
            original_evidence.append({"id": f"price-{symbol.lower()}", "market_symbol": symbol, "observations": [
                {"session_date": f"2026-11-{day}", "session_completion_bound": f"2026-11-{day}T21:00:00Z",
                 "close": close, "volume": 100} for day, close in zip((11, 12), closes)
            ]})
        original = {"run_id": "original", "as_of": cutoff, "holdings": {"sections": []}, "discovery": None,
                    "holdings_inputs": {"NVDA": {"ticker": "NVDA", "as_of": cutoff, "evidence": original_evidence}}}

        class Client:
            def __init__(self):
                self.calls = []

            def text(self, url, *, sec=False):
                self.calls.append((url, sec))
                if sec:
                    text = ("<p>Item 5.02. Departure of Directors and Officers</p>"
                            "<p>The Board accepted the resignation and appointed an interim chief executive officer "
                            "while a permanent successor is considered.</p>")
                else:
                    text = ("<h1>Management Transition</h1><p>Independent coverage described the reported change in "
                            "leadership, together with the market decline and uncertain timing of a successor.</p>")
                return text, datetime.fromisoformat("2026-11-16T03:00:00Z")

        client = Client()
        replay = restore_run(original)
        enrich_events(replay, client, [
            {"ticker": "NVDA", "url": "https://example.com/report", "kind": "independent_news",
             "title_pattern": "Management Transition", "published_at": "2026-11-12T23:59:59Z"},
            {"ticker": "NVDA", "url": "https://example.com/future", "kind": "independent_news",
             "title_pattern": "Management Transition", "published_at": "2026-11-13T01:00:00Z"},
        ])
        enriched = replay["snapshots"]["NVDA"][1]["evidence"]
        self.assertEqual(enriched[:4], original_evidence)
        self.assertEqual(len(original_evidence), 4)
        self.assertEqual(replay["as_of"], cutoff)
        self.assertEqual(client.calls, [
            ("https://example.com/report", False),
            ("https://www.sec.gov/Archives/edgar/data/1045810/000104581026000082/nvda-20261111.htm", True),
        ])
        news, sec = enriched[4:]
        self.assertIsNone(news["reaction"])
        self.assertIsNone(news["published_at"])
        self.assertEqual(sec["accession"], "0001045810-26-000082")
        self.assertIn("interim chief executive officer", sec["text"])
        self.assertEqual((sec["reaction"]["stock_pct"], sec["reaction"]["sector_pct"],
                          sec["reaction"]["benchmark_pct"]), (-4.34, -0.264, -0.354))
