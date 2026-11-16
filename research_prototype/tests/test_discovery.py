from copy import deepcopy
from datetime import date, datetime, timedelta
from io import BytesIO
import unittest
from zipfile import ZipFile

from research_prototype.discovery import (
    allocate_seats, annotate, compact_batch, score_candidates, screen_metrics, validate_notes,
)
from research_prototype.discovery_collect import collect_qualitative, excerpts
from research_prototype.discovery_data import parse_holdings
from research_prototype.discovery_qualitative import add_qualitative_evidence
from research_prototype.integrated import retry_discovery


def candidate(ticker, liquidity, excess, sector="XLK", **metrics):
    return {
        "ticker": ticker, "sector": sector, "status": "eligible",
        "metrics": {"median_dollar_volume_20d_usd": liquidity, "sector_excess_20d_pp": excess, **metrics},
    }


class DiscoveryTests(unittest.TestCase):
    def test_official_xlsx_format_and_weights(self):
        def row(number, fields):
            return "<row>" + "".join(
                f'<c r="{column}{number}" t="inlineStr"><is><t>{value}</t></is></c>'
                for column, value in fields.items()
            ) + "</row>"

        rows = [
            row(3, {"A": "Holdings:", "B": "As of 11-Nov-2026"}),
            row(5, {"A": "Name", "B": "Ticker", "C": "Identifier", "E": "Weight", "H": "Local Currency"}),
            row(6, {"A": "Sample", "B": "BF.B", "C": "001234567", "E": "5.25", "H": "USD"}),
        ]
        data = BytesIO()
        with ZipFile(data, "w") as workbook:
            workbook.writestr(
                "xl/worksheets/sheet1.xml",
                '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
                + "".join(rows) + "</sheetData></worksheet>",
            )
        parsed = parse_holdings(data.getvalue(), "XLP", datetime.fromisoformat("2026-11-12T20:00:00+00:00"))
        self.assertEqual(parsed["observation_date"], "2026-11-11")
        self.assertEqual(parsed["holdings"][0]["ticker"], "BF-B")
        self.assertEqual(parsed["holdings"][0]["weight_pct"], 5.25)
        self.assertEqual(len(parsed["workbook_sha256"]), 64)

    def test_all_60_dates_must_match_and_liquidity_uses_last_20(self):
        series = [
            {"session_date": (date(2026, 1, 1) + timedelta(days=i)).isoformat(),
             "close": 100 + i, "volume": 250_000}
            for i in range(60)
        ]
        stock = {"observations": series, "quote_type": "EQUITY"}
        benchmark = {"observations": deepcopy(series)}
        scores = screen_metrics(stock, benchmark, benchmark)
        self.assertEqual(scores["median_dollar_volume_20d_usd"], 149.5 * 250_000)
        self.assertEqual(scores["sector_excess_20d_pp"], 0)
        benchmark["observations"][0]["session_date"] = "2025-12-31"
        with self.assertRaisesRegex(ValueError, "session_dates_do_not_align"):
            screen_metrics(stock, benchmark, benchmark)

    def test_exact_seat_ties_holding_exclusions_and_determinism(self):
        rows = [
            candidate("ZZZ", 20, 12), candidate("BBB", 25, 4), candidate("AAA", 25, 1),
            candidate("CCC", 24, 12), {**candidate("HELD", 100, 100), "status": "held"},
            candidate("AAA", 100, 100, sector="XLI"), candidate("X", 20, 2, sector="XLI"),
        ]
        result = allocate_seats(rows)
        self.assertEqual([(row["ticker"], row["seat"]) for row in result], [
            ("AAA", "liquidity"), ("CCC", "sector_excess"), ("X", "liquidity"),
        ])
        self.assertEqual(result, allocate_seats(list(reversed(rows))))

    def test_horizon_gaps_do_not_use_other_periods_or_fill_slots(self):
        rows = [
            candidate("A", 100, -5, quarter_revenue_yoy_pct=10, quarter_operating_margin_pct=15,
                      minimum_annual_cash_after_capex_margin_pct=8),
            candidate("B", 100, 1, minimum_annual_cash_after_capex_margin_pct=4,
                      annual_long_term_debt_to_revenue_pct=0),
        ]
        horizons = score_candidates(rows)
        self.assertEqual([row["ticker"] for row in horizons["short"]["shortlist"]], ["B"])
        self.assertEqual(horizons["short"]["excluded"][0]["reason"], "failed_positive_gate")
        self.assertEqual([row["ticker"] for row in horizons["medium"]["shortlist"]], ["A"])
        self.assertEqual([row["ticker"] for row in horizons["long"]["shortlist"]], ["B"])
        self.assertEqual(horizons["long"]["excluded"][0]["fields"], ["annual_long_term_debt_to_revenue_pct"])

    def test_compact_batch_cannot_add_unranked_ticker_or_unknown_citation(self):
        selected = [candidate("A", 100, 2)]
        sources = {
            key: {"id": key, "source_url": "https://example.com", "observation_date": "2026-11-11"}
            for key in ("price-a", "price-xlk")
        }
        run = {"as_of": "2026-11-12", "sector_seats": selected, "horizons": score_candidates(selected), "evidence": sources}
        batch = compact_batch(run)
        note = {"ticker": "A", "horizon": "short", "rationale": "r", "upside_hypothesis": "u",
                "risk": "r", "reassessment": "r", "evidence_ids": ["price-a"]}
        validate_notes({"notes": [note]}, batch)
        with self.assertRaisesRegex(ValueError, "unselected"):
            validate_notes({"notes": [{**note, "ticker": "B"}]}, batch)
        with self.assertRaisesRegex(ValueError, "admissible citations"):
            validate_notes({"notes": [{**note, "evidence_ids": ["fabricated"]}]}, batch)
        annotate(run, None)
        self.assertEqual(run["model_status"], "not_requested")
        self.assertEqual(run["calls"], [])
        self.assertEqual(len(run["model_input_sha256"]), 64)

    def test_batch_and_citations_follow_assigned_horizon(self):
        stock = candidate(
            "A", 100, -2, quarter_revenue_yoy_pct=10, quarter_operating_margin_pct=-3,
            minimum_annual_cash_after_capex_margin_pct=8, annual_long_term_debt_to_revenue_pct=30,
        )
        sources = {
            key: {"id": key, "source_url": "https://example.com", "observation_date": "2026-11-11"}
            for key in ("price-a", "price-xlk", "sec-a-quarter", "sec-a-annual")
        }
        batch = compact_batch({
            "as_of": "2026-11-12", "sector_seats": [stock], "horizons": score_candidates([stock]), "evidence": sources,
        })
        row = batch["candidates"][0]
        self.assertNotIn("quarter_operating_margin_pct", row["metrics"])
        self.assertNotIn("sec-a-quarter", {item["id"] for item in row["evidence"]})
        note = {"ticker": "A", "horizon": "long", "rationale": "r", "upside_hypothesis": "u",
                "risk": "r", "reassessment": "r", "evidence_ids": ["sec-a-annual"]}
        validate_notes({"notes": [note]}, batch)
        with self.assertRaisesRegex(ValueError, "admissible citations"):
            validate_notes({"notes": [{**note, "evidence_ids": ["price-a"]}]}, batch)

    def test_qualitative_evidence_is_bounded_by_cutoff_shortlist_and_ticker(self):
        selected = [candidate("A", 100, 2), candidate("B", 101, 1)]
        sources = {
            key: {"id": key, "source_url": "https://example.com", "observation_date": "2026-11-11"}
            for key in ("price-a", "price-b", "price-xlk")
        }
        horizons = score_candidates(selected)
        run = {"as_of": "2026-11-12T02:00:00+00:00", "sector_seats": selected, "horizons": horizons,
               "evidence": sources}
        first = {"id": "qual-a-strategy", "ticker": "A", "category": "business_strategy",
                 "source_url": "https://example.com/strategy", "observed_at": "2026-11-12T01:00:00+00:00",
                 "text": "An attributed strategy excerpt."}
        late = {**first, "observed_at": "2026-11-12T02:00:01+00:00"}
        with self.assertRaisesRegex(ValueError, "after the analysis cutoff"):
            add_qualitative_evidence(run, {"schema_version": 1, "evidence": [late]})
        add_qualitative_evidence(run, {"schema_version": 1, "evidence": [
            first, {**first, "id": "qual-unknown-strategy", "ticker": "UNKNOWN"},
        ]})
        self.assertEqual(run["horizons"], horizons)
        self.assertEqual(run["qualitative_evidence"]["skipped"][0]["reason"], "not_shortlisted")
        self.assertEqual(run["qualitative_evidence"]["missing_by_ticker"]["A"], ["sector_outlook", "thesis_risk"])
        batch = compact_batch(run)
        self.assertEqual(batch["candidates"][0]["qualitative_evidence"][0]["id"], first["id"])
        self.assertEqual(batch["candidates"][1]["qualitative_evidence"], [])
        note = {"horizon": "short", "rationale": "r", "upside_hypothesis": "u", "risk": "r", "reassessment": "r"}
        notes = [
            {**note, "ticker": "A", "evidence_ids": ["price-a"], "qualitative_judgment": "Conditionally positive",
             "qualitative_evidence_ids": [first["id"]]},
            {**note, "ticker": "B", "evidence_ids": ["price-b"], "qualitative_judgment": "",
             "qualitative_evidence_ids": []},
        ]
        validate_notes({"notes": notes}, batch)
        notes[1]["qualitative_evidence_ids"] = [first["id"]]
        with self.assertRaisesRegex(ValueError, "same ticker"):
            validate_notes({"notes": notes}, batch)

    def test_automatic_qualitative_collection_respects_scope_and_cutoff(self):
        strategy = "The product strategy includes new customer products and growth across three separate business segments."
        risk = "Dependence on contract suppliers and uncertain regulatory requirements could adversely impact competition and margin."
        industry = "Industry outlook expects growth from customers, while this forecast depends on changing regional market demand."

        class Client:
            def __init__(self):
                self.urls = []

            def text(self, url, *, sec=False):
                self.urls.append((url, sec))
                body = risk if sec else industry if url.endswith("industry") else strategy
                return f"<h1>Management’s Discussion and Analysis</h1><p>{body}</p>", datetime.fromisoformat(
                    "2026-11-12T12:00:00+00:00"
                )

        client = Client()
        issuer = {"date": "2026-11-10", "urls": ["https://example.com/issuer"], "identity": "analysis",
                  "industry": "shared"}
        registry = {"issuers": {"A": issuer, "B": {**issuer, "date": "2026-11-13"},
                               "NOTSELECTED": {**issuer, "urls": ["https://example.com/unselected"]}},
                    "industries": {"shared": {**issuer, "urls": ["https://example.com/industry"],
                                              "terms": ["forecast", "growth"]}}}
        selected = [candidate("A", 100, 2), candidate("B", 101, 1)]
        evidence = {key: {"id": key, "source_url": "https://example.com", "observation_date": "2026-11-11"}
                    for key in ("price-a", "price-b", "price-xlk")}
        evidence["sec-a-quarter"] = {
            "accession": "0000000001-26-000001", "form": "10-Q", "primary_document": "a-20261030.htm",
            "source_url": "https://www.sec.gov/Archives/edgar/data/1/000000000126000001/index.htm",
            "sec_acceptance_at": "2026-11-09T15:00:00+00:00",
        }
        run = {"as_of": "2026-11-12T10:00:00+00:00", "sector_seats": selected, "evidence": evidence,
               "sources": {}, "horizons": score_candidates(selected)}
        collect_qualitative(run, client, registry)
        self.assertCountEqual(client.urls, [
            ("https://example.com/issuer", False), ("https://example.com/industry", False),
            ("https://www.sec.gov/Archives/edgar/data/1/000000000126000001/a-20261030.htm", True),
        ])
        self.assertEqual(run["sources"]["qualitative"]["B"]["business_strategy"][0]["status"], "after_cutoff")
        self.assertEqual(run["qualitative_evidence"]["missing_by_ticker"]["B"], ["business_strategy", "thesis_risk"])
        self.assertEqual(run["evidence"]["qual-a-thesis-risk"]["text"], risk)
        batch = compact_batch(run)
        notes = []
        for row in batch["candidates"]:
            ticker = row["ticker"]
            ids = {item["category"]: item["id"] for item in row["qualitative_evidence"]}
            note = {"ticker": ticker, "horizon": "short", "rationale": "r", "upside_hypothesis": "u",
                    "risk": "r", "reassessment": "r", "evidence_ids": [f"price-{ticker.lower()}"]}
            for field in ("business_strategy", "sector_outlook", "investment_thesis", "counterargument"):
                ref = ids.get(field, ids["sector_outlook"] if field in ("investment_thesis", "counterargument") else None)
                note[field] = {"text": "Conditional judgment" if ref else "", "evidence_ids": [ref] if ref else []}
            notes.append(note)
        validate_notes({"notes": notes}, batch)
        notes[0]["business_strategy"]["evidence_ids"] = ["qual-a-sector-outlook"]
        with self.assertRaisesRegex(ValueError, "business_strategy must cite"):
            validate_notes({"notes": notes}, batch)
        failed = {"role": "discovery_annotation", "stage": "analysis", "error": "invalid citation", "usage": None}
        run.update(calls=[failed], model_status="error", model_input=batch, model_input_sha256="old", wall_seconds=8)
        holdings = {"sections": [{"calls": [{"role": "synthesis", "stage": "draft", "usage": None}]}]}
        record = {"holdings": deepcopy(holdings), "discovery": run, "wall_seconds": 30}

        class Model:
            def invoke(self, prompt, schema, call):
                return {"notes": deepcopy(notes)}

        retry_discovery(record, Model())
        self.assertEqual(record["holdings"], holdings)
        self.assertEqual(run["model_status"], "complete")
        self.assertEqual(run["calls"][0], failed)
        self.assertEqual(run["calls"][1]["rejected_output"]["notes"][0]["business_strategy"]["evidence_ids"],
                         ["qual-a-sector-outlook"])
        self.assertEqual(run["notes"][0]["business_strategy"], {"text": "", "evidence_ids": []})
        self.assertTrue(run["notes"][0]["sector_outlook"]["text"])
        self.assertEqual(run["calls"][1]["omitted_optional_fields"][0]["field"], "business_strategy")
        self.assertEqual(record["operations"]["calls"], 3)
        self.assertEqual(record["operations"]["failed_calls"], 1)
        with self.assertRaisesRegex(ValueError, "identity"):
            excerpts(f"<script>Management Discussion</script><p>{strategy}</p>", "Management Discussion", ["strategy"])
