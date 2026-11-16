from datetime import datetime
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from framework_validation.compact_model import compact_context
from research_prototype.model import PrototypeModel
from research_prototype.report import render_report, source_index
from research_prototype.us_data import collect_us_snapshots, filing_metrics, market_record


COLLECTED = datetime.fromisoformat("2026-11-12T02:00:00+00:00")
OLD_CUTOFF = datetime.fromisoformat("2026-11-11T22:00:00+00:00")
ACCESSION = "0001326801-26-000100"


def quote_payload():
    stamps = [
        datetime.fromisoformat("2026-11-10T14:30:00+00:00"),
        datetime.fromisoformat("2026-11-11T14:30:00+00:00"),
    ]
    return {"chart": {"result": [{
        "meta": {
            "currency": "USD", "exchangeTimezoneName": "America/New_York",
            "regularMarketTime": int(datetime.fromisoformat("2026-11-11T21:00:00+00:00").timestamp()),
        },
        "timestamp": [int(stamp.timestamp()) for stamp in stamps],
        "indicators": {"quote": [{
            "open": [100, 102], "high": [103, 107], "low": [98, 100],
            "close": [101, 105], "volume": [800, 1000],
        }]},
    }]}}


def facts():
    def row(accession, start, end, value):
        return {"accn": accession, "start": start, "end": end, "val": value}

    return {"facts": {"us-gaap": {
        "RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": [
            row("old-accession", "2026-07-01", "2026-09-30", 888),
        ]}},
        "Revenues": {"units": {"USD": [
            row(ACCESSION, "2026-01-01", "2026-09-30", 999),
            row(ACCESSION, "2026-07-01", "2026-09-30", 123),
        ]}},
        "NetCashProvidedByUsedInOperatingActivities": {"units": {"USD": [
            row(ACCESSION, "2026-01-01", "2026-09-30", 49),
        ]}},
    }}}


class FakePublicClient:
    def json(self, url, *, sec=False):
        if "company_tickers" in url:
            return {"0": {"ticker": "META", "cik_str": 1326801}}, COLLECTED
        if "/submissions/" in url:
            return {"cik": "0001326801", "tickers": ["META"], "filings": {"recent": {
                "form": ["8-K", "10-Q"],
                "acceptanceDateTime": ["2026-11-30T19:00:00Z", "2026-10-29T19:00:00Z"],
                "accessionNumber": ["0001326801-26-000200", ACCESSION],
                "filingDate": ["2026-11-30", "2026-10-29"],
                "reportDate": ["2026-11-30", "2026-09-30"],
            }}}, COLLECTED
        if "/companyfacts/" in url:
            return facts(), COLLECTED
        return quote_payload(), COLLECTED

    def text(self, url, *, sec=False):
        series = parse_qs(urlsplit(url).query)["id"][0]
        self.assert_single_series(series)
        return (
            f"observation_date,{series}\n2025-11-01,100\n2026-11-01,103\n2026-11-25,999\n",
            COLLECTED,
        )

    @staticmethod
    def assert_single_series(series):
        if "," in series:
            raise AssertionError("FRED CSV requests must use one series")


class LiveDataTests(unittest.TestCase):
    def collect(self, *, cutoff=None, client=None):
        now = datetime.fromisoformat("2026-11-12T02:00:01+00:00")
        with patch("research_prototype.us_data.utc_now", return_value=now):
            snapshots = collect_us_snapshots(
                ["META"], requested_cutoff=cutoff, client=client or FakePublicClient(),
            )
        return snapshots["META"][1]

    def test_partial_bar_fetched_before_close_never_becomes_eligible_later(self):
        during_session = datetime.fromisoformat("2026-11-11T20:00:00+00:00")
        evidence = market_record("META", "meta-stock-price-window", "stock",
                                 (quote_payload(), during_session), COLLECTED)
        self.assertEqual(evidence["observation_date"], "2026-11-10")
        self.assertEqual(evidence["metrics"]["last_close"], 101)
        self.assertEqual(evidence["collected_at"], during_session.isoformat())
        self.assertIsNone(evidence["published_at"])

    def test_sec_exact_accession_and_income_vs_ytd_duration(self):
        metrics, periods = filing_metrics(facts(), {
            "form": "10-Q", "accession": ACCESSION, "report_date": "2026-09-30",
        })
        self.assertEqual(metrics, {"revenue_usd": 123, "operating_cash_flow_usd": 49})
        self.assertEqual(periods["revenue_usd"]["start"], "2026-07-01")
        self.assertEqual(periods["operating_cash_flow_usd"]["start"], "2026-01-01")

    def test_live_snapshot_admits_only_captured_evidence_and_feeds_market_roles(self):
        fixture = self.collect()
        self.assertEqual(fixture["as_of"], "2026-11-12T02:00:01+00:00")
        evidence = source_index(fixture)
        self.assertNotIn("meta-sec-event-results", evidence)
        self.assertEqual(evidence["meta-sec-periodic-results"]["sec_acceptance_at"], "2026-10-29T19:00:00+00:00")
        self.assertEqual(evidence["us-fred-cpiaucsl"]["metrics"]["same_month_prior_year_change_pct"], 3.0)
        self.assertEqual(evidence["us-fred-dgs10"]["observation_date"], "2026-11-01")
        self.assertEqual(evidence["meta-stock-price-window"]["observation_date"], "2026-11-11")
        context = {**fixture, "evidence": evidence}
        macro = compact_context("macro_market", "analysis", context)["evidence"]
        sector = compact_context("sector", "analysis", context)["evidence"]
        self.assertTrue({"us-fred-dgs10", "us-sp500-price-window"} <= macro.keys())
        self.assertTrue({"us-fred-dgs10", "us-sp500-price-window", "meta-sector-price-window"} <= sector.keys())
        self.assertEqual(source_index(self.collect(cutoff=OLD_CUTOFF)), {})

    def test_macro_and_sector_prompts_distinguish_scope_from_source_gaps(self):
        fixture = self.collect()
        evidence = source_index(fixture)
        model = PrototypeModel.__new__(PrototypeModel)
        context = {**fixture, "evidence": evidence}
        macro = model.build_payload("macro_market", "analysis", context)
        self.assertNotIn("meta-sec-periodic-results", macro["evidence"])
        self.assertNotIn("meta-sector-price-window", macro["evidence"])
        self.assertEqual(macro["snapshot_scope"]["eligible_evidence_count"], len(evidence))
        self.assertTrue({"filing", "sector", "stock"} <= set(macro["snapshot_scope"]["categories_outside_this_view"]))
        prompt = model.build_prompt("macro_market", "analysis", macro)
        self.assertIn("Never infer collection failure", prompt)
        self.assertIn("Start with a macro or broad market finding", prompt)
        sector = model.build_payload("sector", "analysis", context)
        self.assertIn("meta-sector-price-window", sector["evidence"])
        self.assertIn("Leave final issuer actions to synthesis", model.build_prompt("sector", "analysis", sector))

    def test_sec_metadata_does_not_satisfy_missing_financial_data(self):
        class MetadataOnlyClient(FakePublicClient):
            def json(self, url, *, sec=False):
                if "/companyfacts/" in url:
                    return {"facts": {}}, COLLECTED
                return super().json(url, sec=sec)

        fixture = self.collect(client=MetadataOnlyClient())
        evidence = source_index(fixture)
        self.assertIn("meta-sec-periodic-metadata", evidence)
        self.assertIn("meta-sec-periodic-results", fixture["required_evidence_ids"])
        self.assertNotIn("meta-sec-periodic-results", evidence)

    def test_denied_sec_directory_uses_only_a_submissions_verified_sample_cik(self):
        class DirectoryDeniedClient(FakePublicClient):
            def json(self, url, *, sec=False):
                if "company_tickers" in url:
                    raise OSError("HTTP 403")
                return super().json(url, sec=sec)

        source = source_index(self.collect(client=DirectoryDeniedClient()))["meta-sec-periodic-results"]
        self.assertEqual(source["submissions_url"], "https://data.sec.gov/submissions/CIK0001326801.json")
        self.assertEqual(source["facts_url"], "https://data.sec.gov/api/xbrl/companyfacts/CIK0001326801.json")

    def test_report_exposes_role_citations_and_source_timing(self):
        fixture = self.collect()
        sources = source_index(fixture)
        output = {"finding": "관측된 거시 환경", "reasoning": [], "limitations": [],
                  "evidence_ids": ["us-fred-dgs10"]}
        report = render_report({
            "source_mode": "us-live", "run_id": "test-run", "generated_at": fixture["as_of"],
            "model_requested": "test", "sections": [{
                "ticker": "META", "status": "review_required", "final": None,
                "sources": sources, "as_of": fixture["as_of"], "horizon": "medium",
                "trace": [
                    {"role": role, "stage": "analysis", "output": output}
                    for role in ("macro_market", "sector")
                ],
            }],
        })
        self.assertIn("### 거시경제·시장", report)
        self.assertIn("### 섹터", report)
        self.assertIn("공개 시각 미확인", report)
        self.assertIn("공급 지연 여부 미확인", report)
        self.assertIn("[SEC 제출 내역](<https://data.sec.gov/submissions/CIK0001326801.json>)", report)
        self.assertIn("[SEC XBRL 수치](<https://data.sec.gov/api/xbrl/companyfacts/CIK0001326801.json>)", report)
        self.assertIn("[FRED CSV](<https://fred.stlouisfed.org/graph/fredgraph.csv?", report)
        self.assertIn(sources["us-fred-dgs10"]["source_url"], report)
