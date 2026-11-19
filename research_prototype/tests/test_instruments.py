import unittest

from research_prototype.instruments import canonical_holding, profile
from research_prototype.multi_market import checked_krx_close, naver_bars
from research_prototype.model import PrototypeModel
from framework_validation.runner import parse_timestamp


class InstrumentsTest(unittest.TestCase):
    def test_event_attention_can_cite_a_sector_used_in_a_qualified_reaction(self):
        sector = "nvda-sector-price-window"
        other = "another-sector-price-window"
        context = {"ticker": "NVDA", "evidence": {
            sector: {"id": sector, "metrics": {"return_1_sessions_pct": -0.26}},
            other: {"id": other},
            "event-context-nvda-a": {"id": "event-context-nvda-a", "reaction": {"evidence_ids": [sector]}},
        }}
        model = PrototypeModel.__new__(PrototypeModel)
        projected = model.build_payload("event_attention", "analysis", context)["evidence"]
        self.assertIn(sector, projected)
        self.assertNotIn(other, projected)

    def test_korean_names_and_leading_zeros_are_routed(self):
        self.assertEqual(canonical_holding("TIGER 미국나스닥100타겟데일리커버드콜"), "486290")
        self.assertEqual(canonical_holding("005930"), "005930")
        self.assertEqual(profile("486290").kind, "covered_call")
        self.assertEqual(profile("BOIL").kind, "commodity_leveraged")
        self.assertIsNone(profile("KO"))

    def test_korean_quote_keeps_only_completed_kst_sessions(self):
        xml = "<chartdata><item data='20261113|10|14|9|12|100'/><item data='20261116|11|15|9|13|200'/></chartdata>"
        before_close = naver_bars(xml, parse_timestamp("2026-11-16T06:00:00Z"))
        after_close = naver_bars(xml, parse_timestamp("2026-11-16T06:31:00Z"))
        self.assertEqual([row["session_date"] for row in before_close], ["2026-11-13"])
        self.assertEqual(after_close[-1]["session_completion_bound"], "2026-11-16T06:30:00+00:00")
        self.assertEqual(after_close[-1]["close"], 13)

    def test_krx_exam_day_close_is_1630_with_official_source(self):
        xml = "<chartdata><item data='20261118|10|14|9|12|100'/><item data='20261119|11|15|9|13|200'/></chartdata>"
        before = naver_bars(xml, parse_timestamp("2026-11-19T07:00:00Z"))
        after = naver_bars(xml, parse_timestamp("2026-11-19T07:31:00Z"))
        self.assertEqual(before[-1]["session_date"], "2026-11-18")
        self.assertEqual(after[-1]["session_completion_bound"], "2026-11-19T07:30:00+00:00")
        self.assertIn("kind.krx.co.kr", after[-1]["session_calendar_source_url"])

    def test_korean_quote_rejects_after_hours_even_when_price_matches(self):
        quote = {"itemCode": "005930", "marketSessionType": "afterMarket", "marketStatus": "OPEN",
                 "localTradedAt": "2026-11-16T18:03:00+09:00", "closePriceRaw": "196800"}
        self.assertFalse(checked_krx_close({"datas": [quote]}, "005930", "2026-11-16", 196800))
        regular = {**quote, "marketSessionType": "regularMarket", "marketStatus": "CLOSE",
                   "localTradedAt": "2026-11-16T15:30:00+09:00"}
        self.assertTrue(checked_krx_close({"datas": [regular]}, "005930", "2026-11-16", 196800))
        self.assertFalse(checked_krx_close({"datas": [regular]}, "005930", "2026-11-16", 197200))
