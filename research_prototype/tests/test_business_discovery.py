from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from research_prototype.business_discovery import (
    BUSINESS_FIELDS, CutoffClient, REVIEW_SCHEMA, business_batch, dedup_business_deep, deduplicate_source_text,
    digest, document_period, invoke_stage, prepare_deep_dedup, reextract_business, revalidate_business_deep,
    run_business, retry_business_deep,
    replay_business, selected_passages, validate_final, validate_screen,
)


STAMP = "2026-11-18T03:25:31+00:00"


def company(ticker, status="eligible"):
    return {"ticker": ticker, "trade_status": status, "business_source_status": "available",
            "sources": [{"id": f"business-{ticker.lower()}"}]}


def review(ticker, outcome="deep_review"):
    return {"ticker": ticker, "business_summary": "제품과 고객을 확인했습니다.",
            "selection_reason": "제품의 매출 성장과 투자 실행을 추가 확인합니다.",
            "evidence_ids": [f"business-{ticker.lower()}"], "outcome": outcome}


def deep_candidate(ticker):
    return {"ticker": ticker, "evidence_ids": [f"business-{ticker.lower()}", f"price-{ticker.lower()}",
                                                f"sec-{ticker.lower()}-quarter"],
            "business_evidence_ids": [f"business-{ticker.lower()}"],
            "realized_evidence_ids": [f"sec-{ticker.lower()}-quarter"], "eligible_horizons": ["short", "medium"]}


def analysis(ticker, horizon=None):
    item = {"ticker": ticker, "management_claims_status": "partly_verified",
            "selection_reason": "매출 실증과 고객 수요의 지속성을 조사합니다.", "horizons": []}
    for field in BUSINESS_FIELDS:
        ref = f"sec-{ticker.lower()}-quarter" if field == "evidence" else f"business-{ticker.lower()}"
        item[field] = {"text": "공시 근거에 따른 조건부 사업 분석입니다.", "evidence_ids": [ref]}
    if horizon:
        required = f"price-{ticker.lower()}" if horizon == "short" else f"sec-{ticker.lower()}-quarter"
        item["horizons"] = [{"horizon": horizon, "action": "research",
                              "proposal": {"text": "가격과 사업 실증을 추가 조사합니다.", "evidence_ids": [required]},
                              "reassessment": {"text": "다음 분기 매출이 감소하면 성장 가설을 낮춥니다.", "evidence_ids": [required]}}]
    return item


def failed_deep_record():
    from research_prototype.week3 import measurement
    telemetry = {"input_tokens": 100, "cached_input_tokens": 0, "cache_write_input_tokens": 0,
                 "output_tokens": 20, "reasoning_output_tokens": 0}
    screen_input = {"as_of": STAMP, "candidates": [company("T")]}
    screen_call = {"role": "business_screen", "stage": "business_screen", "prompt_chars": 10,
                   "seconds": 1, "usage": telemetry, "output": {"reviews": [review("T")]}}
    deep_input = {"as_of": STAMP, "candidates": [deep_candidate("T")]}
    prompt = "Frozen deep instruction.\n" + json.dumps(deep_input, ensure_ascii=False)
    failed_call = {"role": "business_deep", "stage": "business_deep", "prompt": prompt,
                   "prompt_chars": len(prompt), "usage": None, "error": "Quota startup failure", "seconds": 3.525}
    return {"run_id": "failed", "as_of": STAMP, "holdings": {"saved": True}, "discovery": {
        "run_id": "failed", "as_of": STAMP, "discovery_method": "business_first", "counts": {},
        "business_screen": {"status": "complete", "input": screen_input, "input_sha256": digest(screen_input),
                            "selected_tickers": ["T"], "reviews": [review("T")], "calls": [screen_call],
                            "operations": measurement([screen_call])},
        "business_deep": {"status": "error", "error": "Quota startup failure", "input": deep_input,
                          "input_sha256": digest(deep_input), "calls": [failed_call], "operations": measurement([failed_call])},
        "business_baseline": {"horizons": {key: {"shortlist": []} for key in ("short", "medium", "long")}},
        "sector_seats": [{"ticker": "T"}], "wall_seconds": 8, "model_status": "error",
        "horizons": {key: {"shortlist": []} for key in ("short", "medium", "long")},
        "evidence": {"original-source": {"metrics": {"fixed": 123}}},
        "business_previous_attempts": [{"first_paid_trial": True}],
    }}


class BusinessDiscoveryTests(unittest.TestCase):
    def test_comparative_fields_can_cite_supplied_other_selected_business_but_own_fields_cannot(self):
        candidates = [deep_candidate("A"), deep_candidate("B")]
        for candidate in candidates:
            candidate["sources"] = [{"id": f"business-{candidate['ticker'].lower()}",
                                      "passages": {"business": ["Original supplied business description."]}}]
        payload = {"candidates": candidates, "peer_evidence_ids": ["not-physically-supplied"]}
        rows = [analysis("A", "short"), analysis("B", "short")]
        for field in ("moat", "evidence", "counterargument"):
            rows[0][field]["evidence_ids"].append("business-b")
        rows[1]["moat"]["evidence_ids"].append("business-a")
        self.assertEqual(set(validate_final({"analyses": rows}, payload)), {"A", "B"})
        for field in ("business", "strategy", "thesis", "valuation"):
            invalid = deepcopy(rows)
            invalid[0][field]["evidence_ids"].append("business-b")
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_final({"analyses": invalid}, payload)
        invalid = deepcopy(rows)
        invalid[0]["horizons"][0]["proposal"]["evidence_ids"].append("business-b")
        with self.assertRaises(ValueError):
            validate_final({"analyses": invalid}, payload)
        invalid = deepcopy(rows)
        invalid[0]["moat"]["evidence_ids"].append("not-physically-supplied")
        with self.assertRaises(ValueError):
            validate_final({"analyses": invalid}, payload)

    def test_explicit_revalidation_preserves_original_output_error_usage_and_sources(self):
        saved = failed_deep_record()
        run = saved["discovery"]
        screen, deep = run["business_screen"], run["business_deep"]
        screen["input"]["candidates"].append(company("B"))
        screen["calls"][0]["output"]["reviews"].append(review("B"))
        screen["reviews"].append(review("B"))
        screen["selected_tickers"].append("B")
        screen["input_sha256"] = digest(screen["input"])
        deep["input"]["candidates"].append(deep_candidate("B"))
        deep["input"]["candidates"][1]["sources"] = [{"id": "business-b", "text": "Actual other company source."}]
        deep["input_sha256"] = digest(deep["input"])
        raw = {"analyses": [analysis("T", "short"), analysis("B", "short")]}
        raw["analyses"][0]["moat"]["evidence_ids"].append("business-b")
        deep["error"] = "Invalid business analysis passage T moat"
        call = deep["calls"][-1]
        call.update(error=deep["error"], unvalidated_output=raw,
                    prompt="Exact instructions\n" + json.dumps(deep["input"], ensure_ascii=False))
        frozen = deepcopy(saved)
        with patch("research_prototype.business_discovery.invoke_stage", side_effect=AssertionError("No model call")):
            result = revalidate_business_deep(saved)
        repaired = result["discovery"]
        self.assertEqual(saved, frozen)
        self.assertEqual(repaired["business_deep"]["status"], "revalidated_complete")
        self.assertEqual(repaired["business_deep"]["calls"][-1]["output"], raw)
        self.assertEqual(repaired["business_deep"]["calls"][-1]["unvalidated_output"], raw)
        self.assertEqual(repaired["business_deep"]["calls"][-1]["error"], deep["error"])
        self.assertEqual(repaired["business_deep"]["operations"], deep["operations"])
        self.assertEqual(repaired["business_deep_revalidations"][0]["baseline_deep"], deep)
        self.assertEqual(repaired["business_deep_revalidations"][0]["new_model_calls"], 0)
        self.assertEqual(repaired["evidence"], frozen["discovery"]["evidence"])
        self.assertEqual(repaired["wall_seconds"], frozen["discovery"]["wall_seconds"])
        call["unvalidated_output"]["analyses"][0]["moat"]["evidence_ids"].append("unknown-source")
        with self.assertRaises(ValueError):
            revalidate_business_deep(saved)

    def test_exact_text_projection_preserves_nonidentical_text_metadata_and_all_passages(self):
        exact = {"id": "business-t", "text": "Product paragraph.\nRisk paragraph.",
                 "passages": {"business": ["Product paragraph."], "risk": ["Risk paragraph."]},
                 "available_at": STAMP, "fiscal_period_focus": "FY", "observation_date": "2026-09-30",
                 "metrics": {"revenue_usd": 100}, "unit": "USD"}
        other = {**deepcopy(exact), "id": "not-exact", "text": exact["text"] + " "}
        payload = {"as_of": STAMP, "candidates": [{"ticker": "T", "sources": [exact, other]}],
                   "peer_context": [{"ticker": "PEER", "sources": [{"id": "plain", "text": "Keep original text."}]}]}
        frozen = deepcopy(payload)
        projected, audit = deduplicate_source_text(payload)
        expected = deepcopy(payload)
        del expected["candidates"][0]["sources"][0]["text"]
        self.assertEqual(payload, frozen)
        self.assertEqual(projected, expected)
        self.assertEqual([row["source_id"] for row in audit["removed_texts"]], ["business-t"])
        self.assertEqual(audit["dropped_passages"], 0)
        self.assertEqual(audit["removed_chars"], len(json.dumps(payload, ensure_ascii=False)) - len(json.dumps(expected, ensure_ascii=False)))
        self.assertEqual(deduplicate_source_text(projected)[0], projected)

    def test_normal_stage_uses_projector_but_frozen_retry_keeps_its_exact_input_and_prompt(self):
        source = {"id": "business-t", "text": "Original paragraph.", "passages": {"business": ["Original paragraph."]}}
        payload = {"candidates": [{"ticker": "T", "sources": [source]}]}
        model = Mock()
        model.invoke.return_value = {"reviews": []}
        run = {}
        invoke_stage(run, "business_screen", model, payload, "Original instruction", REVIEW_SCHEMA)
        self.assertNotIn("text", run["business_screen"]["input"]["candidates"][0]["sources"][0])
        self.assertTrue(model.invoke.call_args.args[0].startswith("Original instruction\n"))
        saved = failed_deep_record()
        deep = saved["discovery"]["business_deep"]
        deep["input"]["candidates"][0]["sources"] = [source]
        deep["input_sha256"] = digest(deep["input"])
        deep["calls"][0]["prompt"] = "Frozen original instruction\n" + json.dumps(deep["input"], ensure_ascii=False)
        model.invoke.return_value = {"analyses": [analysis("T", "short")]}
        result = retry_business_deep(saved, model)
        self.assertEqual(result["discovery"]["business_deep"]["input"], deep["input"])
        self.assertEqual(model.invoke.call_args.args[0], deep["calls"][0]["prompt"])

    def test_complete_deep_comparison_preserves_baseline_and_runs_only_one_projected_call(self):
        from research_prototype.week3 import measurement
        saved = failed_deep_record()
        run = saved["discovery"]
        deep = run["business_deep"]
        deep["status"] = "complete"
        deep.pop("error")
        call = deep["calls"][0]
        call.pop("error")
        call["output"] = {"analyses": [analysis("T", "short")]}
        deep["input"]["candidates"][0]["sources"] = [{"id": "business-t", "text": "Products.\nRisk.",
                                                          "passages": {"business": ["Products."], "risk": ["Risk."]},
                                                          "available_at": STAMP, "form": "10-K"}]
        deep["input_sha256"] = digest(deep["input"])
        call["prompt"] = "Exact original instructions\n" + json.dumps(deep["input"], ensure_ascii=False)
        run["business_analyses"] = {}
        run["model_status"] = "complete"
        run["operations"] = measurement([*run["business_screen"]["calls"], *deep["calls"]])
        frozen = deepcopy(saved)
        payload, prompt, projection = prepare_deep_dedup(saved)
        self.assertTrue(prompt.startswith("Exact original instructions\n"))
        self.assertEqual(projection["dropped_passages"], 0)
        model = Mock()
        model.invoke.return_value = call["output"]
        with (patch("research_prototype.business_discovery.prepare_business", side_effect=AssertionError("No collection")),
              patch("research_prototype.business_discovery.get_discovery_filings", side_effect=AssertionError("No SEC request"))):
            result = dedup_business_deep(saved, model)
        self.assertEqual(saved, frozen)
        self.assertEqual(model.invoke.call_count, 1)
        self.assertEqual(model.invoke.call_args.args[0], prompt)
        self.assertEqual(result["discovery"]["business_deep"]["input"], payload)
        self.assertEqual(result["discovery"]["business_screen"], frozen["discovery"]["business_screen"])
        self.assertEqual(result["discovery"]["business_dedup_comparisons"][0]["baseline_deep"], deep)
        self.assertEqual(result["discovery"]["model_status"], "complete")
        self.assertEqual(result["discovery"]["dedup_operations"]["calls"], 1)
        self.assertEqual(result["discovery"]["evidence"], frozen["discovery"]["evidence"])
        model.invoke.return_value = {"analyses": [analysis("T", "short")]}
        model.invoke.return_value["analyses"][0]["horizons"][0]["proposal"]["evidence_ids"] = ["wrong-citation"]
        rejected = dedup_business_deep(saved, model)
        self.assertEqual(rejected["discovery"]["model_status"], "error")
        self.assertEqual(rejected["discovery"]["horizons"]["short"]["shortlist"], [])
        self.assertIn("unvalidated_output", rejected["discovery"]["business_deep"]["calls"][0])

    def test_deep_retry_reuses_exact_prompt_and_preserves_failed_telemetry_separately(self):
        original = failed_deep_record()
        frozen = deepcopy(original)
        model = Mock()
        model.invoke.return_value = {"analyses": [analysis("T", "short")]}

        def invoke(prompt, schema, call):
            call["usage"] = {"input_tokens": 200, "cached_input_tokens": 0, "cache_write_input_tokens": 0,
                             "output_tokens": 30, "reasoning_output_tokens": 0}
            return {"analyses": [analysis("T", "short")]}

        model.invoke.side_effect = invoke
        with (patch("research_prototype.business_discovery.prepare_business", side_effect=AssertionError("No collection")),
              patch("research_prototype.business_discovery.get_discovery_filings", side_effect=AssertionError("No SEC request"))):
            result = retry_business_deep(original, model)
        self.assertEqual(original, frozen)
        self.assertEqual(model.invoke.call_count, 1)
        self.assertEqual(model.invoke.call_args.args[0], frozen["discovery"]["business_deep"]["calls"][0]["prompt"])
        run = result["discovery"]
        self.assertEqual(run["business_screen"], frozen["discovery"]["business_screen"])
        self.assertEqual(run["business_deep"]["input_sha256"], frozen["discovery"]["business_deep"]["input_sha256"])
        self.assertEqual(run["business_deep_failed_attempts"][0], frozen["discovery"]["business_deep"])
        self.assertEqual(run["business_previous_attempts"], frozen["discovery"]["business_previous_attempts"])
        self.assertEqual((run["model_status"], run["operations"]["calls"], run["operations"]["usage"]["input_tokens"]),
                         ("complete", 2, 300))
        self.assertEqual(run["retry_operations"]["calls"], 1)
        self.assertIsNone(run["failed_attempt_operations"]["usage"]["input_tokens"])
        self.assertEqual(run["failed_attempt_operations"]["calls_without_usage"], 1)
        self.assertEqual(run["evidence"], frozen["discovery"]["evidence"])
        self.assertEqual(result["holdings"], frozen["holdings"])

    def test_deep_retry_rejects_tampered_input_before_transport_and_keeps_new_validation_failure(self):
        tampered = failed_deep_record()
        tampered["discovery"]["business_deep"]["input"]["candidates"][0]["ticker"] = "OTHER"
        model = Mock()
        with self.assertRaises(ValueError):
            retry_business_deep(tampered, model)
        model.invoke.assert_not_called()
        model.invoke.return_value = {"analyses": [analysis("T", "short")]}
        model.invoke.return_value["analyses"][0]["horizons"][0]["proposal"]["evidence_ids"] = ["wrong-ticker"]
        result = retry_business_deep(failed_deep_record(), model)
        self.assertEqual(result["discovery"]["model_status"], "error")
        self.assertIn("unvalidated_output", result["discovery"]["business_deep"]["calls"][0])
        self.assertEqual(len(result["discovery"]["business_deep_failed_attempts"]), 1)
    def test_period_metadata_keeps_year_after_nested_inline_span(self):
        body = '''<ix:nonNumeric name="dei:DocumentPeriodEndDate" contextRef="c1"><span>September 26</span>, <span>2026</span></ix:nonNumeric>
        <ix:nonNumeric name="dei:DocumentFiscalYearFocus" contextRef="c1">2026</ix:nonNumeric>
        <ix:nonNumeric name="dei:DocumentFiscalPeriodFocus" contextRef="c1">FY</ix:nonNumeric>'''
        self.assertEqual(document_period(body), {"observation_date": "September 26, 2026",
                                               "fiscal_year_focus": "2026", "fiscal_period_focus": "FY"})

    def test_business_replay_hides_usage_and_keeps_its_last_appendix(self):
        from research_prototype.integrated import save_run
        metrics = {"calls": 1, "usage": {"input_tokens": 123, "cached_input_tokens": 10, "output_tokens": 45}}
        saved = {"run_id": "new", "as_of": STAMP, "business_replayed_from": "old",
                 "holdings": {"operations": metrics},
                 "discovery": {"discovery_method": "business_first", "universe": [{"ticker": "T"}],
                               "model_status": "complete", "business_screen": {"operations": metrics},
                               "business_deep": {"operations": metrics}, "wall_seconds": 1.5},
                 "decision_assessment": {"status": "error", "error": "Historical failure", "operations": metrics}}
        pdf = Mock()
        with TemporaryDirectory() as temporary:
            output = Path(temporary) / "report"
            with (patch("research_prototype.integrated.checklist", return_value={"holdings": {}, "discovery": {}}),
                  patch("research_prototype.integrated.render_report", return_value="Old holdings"),
                  patch("research_prototype.integrated.render", return_value="# Discovery\n<!-- pagebreak -->\n## Last gap table\nGAP LAST\n"),
                  patch("research_prototype.integrated.appendix_lines", return_value="OLD COVERAGE APPENDIX")):
                self.assertEqual(save_run(output, saved, pdf), 0)
            markdown = (output / "discovery.md").read_text(encoding="utf-8")
            self.assertNotIn("전체 기업 예비심사 | 1 | 123 | 10 | 45", markdown)
            self.assertEqual(saved["discovery"]["business_deep"]["operations"], metrics)
            self.assertNotIn("이번 사업 분석 사용량", markdown)
            self.assertTrue(markdown.rstrip().endswith("GAP LAST"))
            self.assertNotIn("공개 정성 자료 검토 사용량", markdown)
            self.assertNotIn("OLD COVERAGE APPENDIX", markdown)
            self.assertFalse((output / "holdings.md").exists())
            self.assertEqual(pdf.call_count, 1)
            self.assertEqual(pdf.call_args.args[0].name, "discovery.pdf")
            self.assertEqual(saved["decision_assessment"]["status"], "error")

    def test_inline_about_heading_is_a_company_overview_without_item_heading(self):
        body = "<p>ABOUT NORTHWIND. The Company operates an aerospace engines and related services business for commercial aircraft customers worldwide.</p>"
        self.assertIn("aerospace engines", " ".join(selected_passages(body)["business"]))

    def test_legal_name_subject_business_fallback_is_actual_source_text(self):
        sentence = "Northwind Railroad Corporation transports freight across its rail network and connects industrial customers with major ports and distribution centers."
        result = selected_passages(f"<h2>Item 1. Business</h2><p>{sentence}</p>")
        self.assertEqual(result["business"], [sentence])

    def test_question_extraction_uses_business_and_excludes_risk_and_accounting(self):
        body = """<h2>Item 1. Business</h2>
        <p>We design and sell industrial power systems and related maintenance services to commercial customers across multiple end markets.</p>
        <p>In 2026 we acquired a modular power business and expanded production capacity for existing data center customers in North America.</p>
        <p>Revenue increased 18 percent to $4 billion in 2026 as product shipments and service renewals increased across our end markets.</p>
        <h2>Item 1A. Risk Factors</h2>
        <p>We may fail to expand capacity and could experience disruptions in manufacturing products and services for our customers.</p>
        <h2>Item 7. Management Discussion</h2>
        <p>Charges in 2026 were included in Cost of products sold, Research and development expense, or Other expense in this period.</p>
        <p>The government launched a program to administer refunds following a court ruling and further regulatory guidance.</p>
        <p>A conference call to discuss our capacity investments and new product strategy will be available via webcast this afternoon.</p>"""
        result = selected_passages(body)
        self.assertIn("power systems", " ".join(result["business"]))
        self.assertIn("acquired a modular", " ".join(result["strategy"]))
        self.assertIn("18 percent", " ".join(result["execution"]))
        for question in ("business", "strategy", "differentiation", "execution"):
            text = " ".join(result[question])
            self.assertNotIn("may fail", text)
            self.assertNotIn("Charges in", text)
            self.assertNotIn("government launched", text)
            self.assertNotIn("conference call", text)

    def test_compact_cards_keep_products_orders_membership_and_product_growth(self):
        cases = [
            ("""<h2>Item 1. Business</h2>
            <p>The Company designs, manufactures and markets phones, personal computers, tablets and accessories, and sells related services to customers.</p>
            <p>The Company sells its products through retail stores and direct sales channels and offers subscription services on its platforms.</p>""", "business", ["personal computers", "subscription services"]),
            ("""<h2>Item 2. Management Discussion</h2>
            <p>Our cash balance increased by $10 billion following share repurchases and dividends during the current reporting period.</p>
            <p>Certain multi-year customer contracts contain firmly committed amounts and remaining performance obligations were approximately $179.2 billion as of August 2, 2026. We expect 25% to be recognized over the next twelve months.</p>""", "execution", ["179.2 billion"]),
            ("""<h2>Item 7. Management Discussion</h2>
            <p>Membership fee revenue increased 11% in fiscal 2026, driven by new member sign-ups, fee increases and upgrades to premium membership.</p>
            <p>Our member renewal rate was 92.3% in our domestic market and 89.8% worldwide at the end of fiscal year 2026.</p>
            <p>Gross margin percentage decreased three basis points during fiscal 2026 due to changes in the merchandise mix and operating costs.</p>""", "execution", ["11%", "92.3%", "89.8%"]),
            ("""<h2>Item 2. Management Discussion</h2>
            <p>Therapy product sales increased 18% to $6.2 billion for the three months ended September 30, 2026, due to higher realized price, higher demand and favorable inventory dynamics.</p>
            <p>Product gross margin increased to 81.0% during the three months ended September 30, 2026, driven by expiration of a royalty obligation.</p>""", "execution", ["18%", "$6.2 billion", "realized price", "inventory"]),
        ]
        for body, question, expected in cases:
            with self.subTest(expected=expected):
                source = {"id": "business-t", "available_at": STAMP, "source_kind": "official_filing_body",
                          "form": "10-K", "fiscal_year_focus": "2026", "fiscal_period_focus": "FY",
                          "passages": selected_passages(body)}
                run = {"as_of": STAMP, "universe": [{"ticker": "T", "name": "Test", "status": "eligible", "sector": "XLK"}],
                       "business_collection": {"packets": {"T": {"status": "available", "evidence": {"business-t": source}}}}}
                candidate = business_batch(run)["candidates"][0]
                text = " ".join(p for item in candidate["sources"] for p in item["passages"].get(question, []))
                for expected_text in expected:
                    self.assertIn(expected_text, text)
                self.assertNotIn("share repurchases", text)
                self.assertNotIn("We expect 25%", text)
                for item in candidate["sources"]:
                    self.assertEqual((item["form"], item["fiscal_year_focus"], item["fiscal_period_focus"]), ("10-K", "2026", "FY"))

    def test_complete_forty_company_review_and_held_exclusion(self):
        candidates = [company(f"T{i}") for i in range(39)] + [company("HELD", "held")]
        rows = [review(row["ticker"], "not_selected") for row in candidates]
        rows[-1] = review("HELD", "excluded_held")
        rows[0]["outcome"] = "deep_review"
        self.assertEqual(validate_screen({"reviews": rows}, {"candidates": candidates}), ["T0"])
        rows[-1]["outcome"] = "deep_review"
        with self.assertRaises(ValueError):
            validate_screen({"reviews": rows}, {"candidates": candidates})
        rows[-1]["outcome"] = "excluded_held"
        rows[0]["outcome"] = "excluded_held"
        with self.assertRaises(ValueError):
            validate_screen({"reviews": rows}, {"candidates": candidates})

    def test_missing_evidence_is_not_business_rejection_and_limit_eight(self):
        candidates = [company(f"T{i}") for i in range(9)]
        with self.assertRaises(ValueError):
            validate_screen({"reviews": [review(row["ticker"]) for row in candidates]}, {"candidates": candidates})
        candidates[0]["business_source_status"] = "insufficient_evidence"
        with self.assertRaises(ValueError):
            validate_screen({"reviews": [review(row["ticker"], "not_selected") for row in candidates]}, {"candidates": candidates})

    def test_final_caps_and_sourced_business_execution_are_required(self):
        payload = {"candidates": [deep_candidate(f"T{i}") for i in range(5)]}
        items = [analysis(f"T{i}", "short" if i < 2 else "medium" if i < 4 else None) for i in range(5)]
        self.assertEqual(len(validate_final({"analyses": items}, payload)), 5)
        items[0]["business"]["evidence_ids"] = []
        with self.assertRaises(ValueError):
            validate_final({"analyses": items}, payload)
        items[0] = analysis("T0", "short")
        items[-1] = analysis("T4", "short")
        with self.assertRaises(ValueError):
            validate_final({"analyses": items}, payload)

    def test_verified_label_requires_realized_evidence_and_horizon_gate(self):
        payload = {"candidates": [deep_candidate("T") ]}
        item = analysis("T", "short")
        item["management_claims_status"] = "verified"
        item["evidence"]["evidence_ids"] = ["business-t"]
        with self.assertRaises(ValueError):
            validate_final({"analyses": [item]}, payload)
        item = analysis("T", "medium")
        payload["candidates"][0]["eligible_horizons"] = ["short"]
        with self.assertRaises(ValueError):
            validate_final({"analyses": [item]}, payload)

    def test_submissions_are_filtered_before_legacy_financial_selection(self):
        class Client:
            def json(self, url, sec=False):
                return {"filings": {"recent": {"acceptanceDateTime": [STAMP, "2026-11-19T00:00:00Z"],
                                                "form": ["10-Q", "10-K"], "accessionNumber": ["old", "future"]}}}, datetime.now(timezone.utc)
        payload, _ = CutoffClient(Client(), datetime.fromisoformat(STAMP)).json("https://data.sec.gov/submissions/CIK1.json")
        self.assertEqual(payload["filings"]["recent"]["accessionNumber"], ["old"])

    def test_collect_only_replay_preserves_original_quotes_financials_and_prior_choices(self):
        quote = {"id": "price-t", "observations": [{"session_date": "2026-11-17", "close": 155.46}]}
        financial = {"id": "sec-t-annual", "metrics": {"revenue_usd": 100}, "long_term_debt_usd": 16.31}
        run = {"as_of": STAMP, "run_id": "old", "universe": [{"ticker": "T", "status": "eligible", "name": "Test", "sector": "XLK"}],
               "evidence": {"price-t": quote, "sec-t-annual": financial}, "counts": {},
               "sector_seats": [{"ticker": "T", "metrics": {"annual_long_term_debt_to_revenue_pct": 16.31}}],
               "horizons": {key: {"shortlist": [{"ticker": "T", "evidence_ids": ["price-t"]}] if key == "long" else []}
                            for key in ("short", "medium", "long")},
               "business_collection": {"packets": {"T": {"status": "insufficient_evidence", "evidence": {}}}}}
        previous = {"run_id": "old", "as_of": STAMP, "discovery": run,
                    "holdings": {"unchanged": True}, "decision_briefs": {"discovery": {"T": {"long": "old"}}}}
        frozen = deepcopy(previous)
        result = replay_business(previous, None, None, "unused")
        self.assertEqual(previous, frozen)
        self.assertEqual(result["discovery"]["evidence"], frozen["discovery"]["evidence"])
        self.assertEqual(result["discovery"]["business_baseline"]["horizons"], frozen["discovery"]["horizons"])
        self.assertEqual(result["holdings"], frozen["holdings"])
        self.assertEqual(result["operations"]["calls"], 0)

    def test_failed_new_screen_does_not_reuse_old_deep_calls_or_selection(self):
        class Model:
            def invoke(self, prompt, schema, call):
                return {"reviews": []}
        run = {"as_of": STAMP, "universe": [{"ticker": "T", "status": "eligible", "name": "Test", "sector": "XLK"}],
               "evidence": {}, "counts": {}, "sector_seats": [], "horizons": {
                   horizon: {"shortlist": [], "excluded": []} for horizon in ("short", "medium", "long")},
               "business_collection": {"packets": {"T": {"status": "available", "evidence": {
                   "business-t": {"id": "business-t", "available_at": STAMP, "source_kind": "official_filing_body",
                                  "form": "10-K", "passages": {"business": ["Original company products and services."]}}}}}},
               "business_screen": {"calls": [{"role": "old_screen"}]},
               "business_deep": {"calls": [{"role": "old_deep"}]}, "selection_changes": [{"ticker": "OLD"}]}
        run_business(run, None, Model(), "unused")
        self.assertEqual(run["model_status"], "error")
        self.assertEqual(len(run["calls"]), 1)
        self.assertNotIn("business_deep", run)
        self.assertNotIn("selection_changes", run)
        self.assertIn("unvalidated_output", run["calls"][0])
        self.assertEqual(len(run["business_previous_attempts"]), 1)

    def test_no_usable_business_sources_blocks_without_a_model_call(self):
        run = {"as_of": STAMP, "universe": [{"ticker": "T", "status": "eligible", "name": "Test", "sector": "XLK"}],
               "evidence": {}, "counts": {}, "sector_seats": [],
               "horizons": {key: {"shortlist": []} for key in ("short", "medium", "long")},
               "business_collection": {"packets": {"T": {"status": "insufficient_evidence", "evidence": {}}}}}
        model = Mock(side_effect=AssertionError("No model for an empty business snapshot"))
        run_business(run, None, model, "unused")
        model.invoke.assert_not_called()
        self.assertEqual(run["business_screen"]["status"], "blocked")
        self.assertEqual(run["calls"], [])
        self.assertEqual(run["model_status"], "error")


if __name__ == "__main__":
    unittest.main()
