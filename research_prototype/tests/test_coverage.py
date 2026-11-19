from datetime import date
import unittest
from unittest.mock import patch

from research_prototype.coverage import appendix_lines, checklist, event_coverage, event_summary, quarter_coverage, withhold_unverified_medium
from research_prototype.discovery_data import fourth_quarter_metrics
from research_prototype.discovery import render
from research_prototype.integrated import run_reports
from research_prototype.report import render_report


class CoverageTests(unittest.TestCase):
    def test_discovery_names_finalists_and_keeps_unidentified_tickers(self):
        cutoff = "2026-11-13T00:00:00Z"
        run = {
            "run_id": "sample", "as_of": cutoff, "held_exclusions": [], "universe": [],
            "counts": dict.fromkeys(("holdings_candidates", "eligible", "seats", "sec_with_facts", "horizon_slots"), 2),
            "sector_seats": [{"ticker": "AVGO", "sector": "XLK", "seat": "liquidity",
                              "listing_security_name": "Broadcom Inc. - Common Stock"},
                             {"ticker": "UNKNOWN", "sector": "XLK", "seat": "sector_excess"}],
            "evidence": {}, "events_requested": True, "model_status": "not_requested",
            "horizons": {"short": {"shortlist": [{"ticker": ticker, "evidence_ids": []}
                                                for ticker in ("AVGO", "UNKNOWN")], "excluded": []},
                         "medium": {"shortlist": [], "excluded": []}, "long": {"shortlist": [], "excluded": []}},
            "operations": {"calls": 0, "usage": dict.fromkeys(("input_tokens", "cached_input_tokens", "output_tokens"), 0)},
        }
        coverage = checklist({"as_of": cutoff, "holdings": {"holdings": [], "sections": []}, "discovery": run})["discovery"]
        markdown = render({**run, "coverage": coverage}) + appendix_lines(coverage)
        self.assertIn("- **AVGO (Broadcom Inc.)**", markdown)
        self.assertIn("### AVGO (Broadcom Inc.)", markdown)
        self.assertIn("- **UNKNOWN**", markdown)
        self.assertIn("### UNKNOWN\n", markdown)

    def test_held_name_on_report_heading_notice_and_appendix(self):
        cutoff = "2026-11-13T00:00:00Z"
        tickers = ["498400", "NVDA", "SOXL", "NEW", "UNKNOWN"]
        issuer = {"id": "new-sec-periodic-results", "issuer_name": "New Issuer Inc.", "available_at": cutoff}
        run = {"run_id": "sample", "generated_at": cutoff, "model_requested": None, "holdings": tickers,
               "sections": [{"ticker": ticker, "status": "not_requested", "final": None,
                             "sources": {issuer["id"]: issuer} if ticker == "NEW" else {}}
                            for ticker in tickers]}
        coverage = checklist({"as_of": cutoff, "holdings": run, "discovery": None,
                              "holdings_inputs": {"NEW": {"evidence": [issuer]}}})["holdings"]
        combined = render_report({**run, "coverage": coverage}) + appendix_lines(coverage)
        for ticker, name in (("498400", "KODEX 200타겟위클리커버드콜"), ("NVDA", "NVIDIA Corporation"),
                             ("SOXL", "Direxion Daily Semiconductor Bull 3X ETF"), ("NEW", "New Issuer Inc.")):
            label = f"{ticker} ({name})"
            self.assertIn(f"## {label}\n", combined)
            self.assertIn(f"### {label}\n", combined)
            self.assertIn(f"- {label} ·", combined)
        self.assertIn("\n## UNKNOWN\n", combined)
    def test_discovered_filing_with_missing_body_cannot_be_a_no_event_result(self):
        evidence = [{"id": "test-8k", "form": "8-K", "sec_event_items": "5.02,9.01",
                     "accession": "0001045810-26-000082", "sec_acceptance_at": "2026-11-12T13:52:41Z"}]
        attempts = [
            {"ticker": "NVDA", "kind": "sec_8k", "status": "unavailable", "accession": "0001045810-26-000082"},
            {"ticker": "NVDA", "kind": "headline_lookup", "status": "complete_zero"},
        ]
        status = event_coverage("NVDA", evidence, attempts, "2026-11-13T00:00:00Z")
        self.assertEqual(status["filing"]["status"], "metadata_only")
        self.assertEqual(status["news"]["status"], "searched_zero")

    def test_failed_lookup_is_distinct_from_valid_zero(self):
        failed = [{"ticker": "ZZZ", "kind": "headline_lookup", "status": "unavailable", "error": "HTTP 503"}]
        self.assertEqual(event_coverage("ZZZ", [], failed, "2026-11-13T00:00:00Z")["news"]["status"], "failed")
        searched = [{"ticker": "ZZZ", "kind": kind, "status": "complete_zero"}
                    for kind in ("headline_lookup", "sec_8k_lookup")]
        result = event_coverage("ZZZ", [], searched, "2026-11-13T00:00:00Z")
        self.assertTrue(result["can_report_no_event"])
        self.assertIn("발견되지 않았습니다", event_summary(result))

    def test_newer_annual_disqualifies_stale_q3_as_latest_quarter(self):
        evidence = [{"id": "sec-mu-quarter", "observation_date": "2026-05-28", "form": "10-Q"},
                    {"id": "sec-mu-annual", "observation_date": "2026-09-03", "form": "10-K"}]
        status = quarter_coverage("MU", evidence)
        self.assertEqual(status["status"], "stale")
        self.assertEqual(status["expected_period_end"], "2026-09-03")
        pick = {"ticker": "MU", "score": 30}
        run = {"horizons": {"medium": {"shortlist": [pick]}, "short": {"shortlist": [pick]}},
               "evidence": {item["id"]: item for item in evidence}, "counts": {"horizon_slots": 2}}
        withhold_unverified_medium(run)
        self.assertFalse(run["horizons"]["medium"]["shortlist"])
        self.assertEqual(run["horizons"]["short"]["shortlist"], [pick])
        self.assertEqual(run["withheld_medium"], [pick])

    def test_q4_numbers_need_matching_reported_full_year_and_ytd(self):
        annual = {"report_date": "2026-09-03", "accession": "annual", "form": "10-K"}
        quarterly = {"report_date": "2026-05-28", "accession": "quarter", "form": "10-Q"}
        rows = [
            {"accn": "annual", "start": "2025-08-29", "end": "2026-09-03", "val": 500},
            {"accn": "quarter", "start": "2025-08-29", "end": "2026-05-28", "val": 340},
            {"accn": "annual", "start": "2024-08-30", "end": "2025-08-28", "val": 400},
            {"accn": "quarter", "start": "2024-08-30", "end": "2025-05-29", "val": 300},
        ]
        facts = {"facts": {"us-gaap": {
            "Revenues": {"units": {"USD": rows}},
            "OperatingIncomeLoss": {"units": {"USD": [{**row, "val": row["val"] / 4} for row in rows]}},
        }}}
        result = fourth_quarter_metrics(facts, annual, quarterly)
        self.assertEqual(result["metrics"]["revenue_usd"], 160)
        self.assertEqual(result["metrics"]["operating_income_usd"], 40)
        self.assertEqual(result["prior_revenue_usd"], 100)
        self.assertEqual(date.fromisoformat(result["start"]), date(2026, 5, 29))
        facts["facts"]["us-gaap"]["Revenues"]["units"]["USD"][1]["start"] = "2025-09-01"
        self.assertIsNone(fourth_quarter_metrics(facts, annual, quarterly))

    def test_missing_material_filing_makes_held_action_tentative_but_keeps_price_rationale(self):
        cutoff = "2026-11-13T00:00:00Z"
        evidence = [{"id": "test-8k", "form": "8-K", "sec_event_items": "5.02,9.01",
                     "accession": "filing", "sec_acceptance_at": "2026-11-12T13:52:41Z"}]
        fixture = {"evidence": evidence, "missing_fields": []}
        captured = {"as_of": cutoff, "snapshots": {"NVDA": (None, fixture)}, "holding_errors": {},
                    "discovery": None, "event_collection": []}
        decision = {"action": "maintain", "reasoning": ["시장 가격을 근거로 잠정 유지"]}
        with patch("research_prototype.integrated.collect",
                   return_value={"sections": [{"ticker": "NVDA", "status": "complete", "final": decision}]}):
            _, holdings, _, _ = run_reports(["NVDA"], captured, "mock", 10, False)
        self.assertEqual(holdings["sections"][0]["status"], "review_required")
        self.assertEqual(holdings["sections"][0]["final"], decision)
        self.assertIn("본문은 확보하지 못했습니다", fixture["missing_fields"][0])

    def test_appendix_continues_after_explicit_page_break(self):
        tickers = {ticker: {"checks": [("필수", "자료", "missing", None)]} for ticker in ("A", "B", "C", "D")}
        appendix = appendix_lines({"tickers": tickers})
        self.assertEqual(appendix.count("<!-- pagebreak -->"), 2)
        self.assertIn("자료 점검표 (2/2)", appendix)
        self.assertIn("| 중요도 | 자료 | 수집 상태 | 기준·참고 |", appendix)
        self.assertIn("| 필수 | 자료 | **미확보** | — |", appendix)

    def test_appendix_distinguishes_unconfirmed_from_completed_zero(self):
        appendix = appendix_lines({"tickers": {"ABC": {"checks": [
            ("보강", "기사", "not_run", None), ("보강", "공시", "searched_zero", None),
            ("보강", "기초 자료", "partial", "2026-11-13"),
        ]}}})
        self.assertIn("| 보강 | 기사 | **조회 미확인** | — |", appendix)
        self.assertIn("| 보강 | 공시 | 검색 완료 · 해당 결과 0건 | — |", appendix)
        self.assertIn("| 보강 | 기초 자료 | **일부 확보** | 2026-11-13 |", appendix)

    def test_appendix_separates_base_news_and_scoped_public_collection(self):
        cut = "2026-11-13T00:00:00Z"
        holdings = {"holdings": ["NVDA"], "sections": [{"ticker": "NVDA"}]}
        pick = {"ticker": "MU", "score": 5}
        discovery = {"horizons": {"short": {"shortlist": [pick]}}, "evidence": {},
                     "sector_seats": [pick, {"ticker": "COST"}]}
        record = {"as_of": cut, "holdings": holdings, "discovery": discovery}
        legacy = checklist(record)
        self.assertIn("| 보강 | 독립 뉴스 |", appendix_lines(legacy["holdings"]))
        self.assertNotIn("추가 기사", appendix_lines(legacy["holdings"]))

        record["public_context"] = {
            "NVDA": {"items": [{"kind": "news", "title": "기사", "text": "",
                                "source_url": "https://example.com/article"}], "sources": [
                {"kind": "community", "status": "searched_zero"},
                {"kind": "community", "status": "authentication_required"},
            ]},
            "MU": {"items": [{"kind": "community", "title": "본문", "text": "본문",
                              "source_url": "https://example.com/post", "link_scope": "permalink",
                              "availability_basis": "bluesky_created_and_indexed_time"}], "sources": [
                {"kind": "news", "status": "searched_zero"},
            ]},
        }
        current = checklist(record)
        holdings_rows = appendix_lines(current["holdings"])
        self.assertIn("| 보강 | 독립 뉴스(기존 이슈 조회) |", holdings_rows)
        self.assertIn("| 보강 | 추가 기사 | **일부 확보** | 제목·발췌 1건", holdings_rows)
        self.assertIn("| 보강 | 공개 투자자 의견 | **수집 실패** | 일부 조회 제한", holdings_rows)
        discovery_rows = appendix_lines(current["discovery"])
        self.assertIn("| 보강 | 추가 기사 | 검색 완료 · 해당 결과 0건 | 조회 범위 내 적격 자료 없음", discovery_rows)
        self.assertIn("| 보강 | 공개 투자자 의견 | 확보 | 본문 1건", discovery_rows)
        self.assertEqual(discovery_rows.count("| 보강 | 추가 기사 |"), 1)
