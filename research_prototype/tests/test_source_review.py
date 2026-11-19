from copy import deepcopy
import unittest

from framework_validation.runner import parse_timestamp
from research_prototype.decision_briefs import project_source
from research_prototype.model import PrototypeModel
from research_prototype.source_review import session_review_views, verified_latest_session


STAMP = "2026-11-19T08:43:10+00:00"


def source():
    return {"id": "498400-stock-price-window", "market_symbol": "498400", "currency": "KRW",
            "available_at": "2026-11-19T08:43:06+00:00", "observation_date": "2026-11-19",
            "observation_end_at": "2026-11-19T07:30:00+00:00", "market_timezone": "Asia/Seoul",
            "session_verification": "matching_naver_closed_regular_session_latest_only",
            "session_check_url": "https://polling.finance.naver.com/api/realtime/domestic/stock/498400",
            "metrics": {"last_close": 14640}, "text": "Naver labels alone do not identify the session.",
            "observations": [{"session_date": "2026-11-19", "session_completion_bound": "2026-11-19T07:30:00+00:00",
                              "close": 14640}]}


class SourceReviewTests(unittest.TestCase):
    def test_session_fact_correction_is_display_only_and_retains_total_return_gap(self):
        raw = {"498400": {"status": "complete", "action": "maintain", "proposed_action": None,
                          "issue": {"text": "중기 제안은 유지다. 정확한 관측 세션은 미확정이다.", "evidence_ids": []},
                          "counterargument": {"text": "가격 약세는 축소 근거다. 다만 총수익과 정확한 세션이 없어 판단이 어렵다.", "evidence_ids": []},
                          "proposal": {"text": "현재 보유를 유지하되 NAV 확인 전 증액은 유보한다.", "evidence_ids": []}}}
        frozen = deepcopy(raw)
        sections = [{"ticker": "498400", "sources": {source()["id"]: source()}}]
        views, notes, audit = session_review_views(sections, raw, STAMP)
        self.assertEqual(raw, frozen)
        self.assertEqual(views["498400"]["action"], "maintain")
        self.assertEqual(views["498400"]["proposal"], raw["498400"]["proposal"])
        self.assertEqual(views["498400"]["status"], "review_required")
        self.assertNotIn("세션은 미확정", views["498400"]["issue"]["text"])
        self.assertIn("총수익은 미확인", views["498400"]["counterargument"]["text"])
        self.assertIn("16:30", notes["498400"])
        self.assertIn("최신 하루", notes["498400"])
        self.assertFalse(audit["498400"]["raw_model_output_modified"])

    def test_positive_saved_verification_and_matching_quote_are_required(self):
        valid = source()
        self.assertEqual(verified_latest_session(valid, STAMP)["verification_scope"], "latest_session_only")
        for change in ({"session_verification": None}, {"observation_end_at": "2026-11-19T06:30:00Z"},
                       {"metrics": {"last_close": 1}}):
            self.assertIsNone(verified_latest_session({**valid, **change}, STAMP))
        self.assertIsNone(verified_latest_session(valid, "2026-11-19T07:00:00Z"))

    def test_session_metadata_survives_role_and_final_brief_projections(self):
        item = source()
        context = {"ticker": "498400", "evidence": {item["id"]: item}}
        model = PrototypeModel.__new__(PrototypeModel)
        role = model.build_payload("price_trading", "analysis", context)["evidence"][item["id"]]
        brief = project_source(item, parse_timestamp(STAMP))
        for key in ("observation_date", "observation_end_at", "session_verification", "session_check_url",
                    "currency", "market_timezone"):
            self.assertEqual(role[key], item[key])
            self.assertEqual(brief[key], item[key])


if __name__ == "__main__":
    unittest.main()
