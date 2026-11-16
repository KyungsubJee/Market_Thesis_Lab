from unittest import TestCase

from framework_validation.compact_model import compact_context
from framework_validation.runner import FIXTURE_DIR, load_fixture, parse_timestamp


class CompactEvidenceTests(TestCase):
    def test_fundamental_analysis_receives_verified_cutoff_price(self):
        cases = (
            ("coca_cola", "ko-regular-session-close", "ko_close_usd"),
            ("meta", "meta-regular-session-close", "meta_close_usd"),
            ("samsung", "samsung_60_session_market", "samsung_cutoff_close_krw"),
        )
        for case_id, price_id, price_key in cases:
            with self.subTest(case_id=case_id):
                fixture = load_fixture(FIXTURE_DIR / f"{case_id}.json")
                as_of = parse_timestamp(fixture["as_of"])
                evidence = {
                    item["id"]: item for item in fixture["evidence"]
                    if parse_timestamp(item["available_at"]) <= as_of
                }
                context = {**fixture, "evidence": evidence}
                first = compact_context("fundamental", "analysis", context)["evidence"]
                self.assertEqual(first[price_id]["metrics"], {price_key: evidence[price_id]["metrics"][price_key]})

                issue = {"evidence_ids": fixture["required_evidence_ids"], "roles": ["fundamental"]}
                followup = compact_context("fundamental", "secondary", {**context, "issue": issue})["evidence"]
                self.assertEqual(followup[price_id]["metrics"], evidence[price_id]["metrics"])
