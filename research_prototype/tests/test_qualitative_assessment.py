from datetime import datetime
from unittest import TestCase

from research_prototype.qualitative_assessment import assess, collection_scope, merge_supplement


CUTOFF = '2026-11-18T04:00:00+00:00'


class QualitativeAssessmentTests(TestCase):
    def test_scope_excludes_names_that_did_not_make_the_shortlist(self):
        holdings = {'holdings': ['NVDA', '000660'], 'sections': []}
        discovery = {'horizons': {'short': {'shortlist': [{'ticker': 'MU'}]}},
                     'universe': [{'ticker': 'MU'}, {'ticker': 'META'}]}
        self.assertEqual(list(collection_scope(holdings, discovery)), ['NVDA', '000660', 'MU'])

    def test_manual_sample_leaves_provider_status_and_enforces_cutoff(self):
        context = {'NVDA': {'items': [], 'sources': [{'provider': 'Reddit', 'status': 'authentication_required'}]}}
        item = {'ticker': 'NVDA', 'provider': 'Reddit', 'kind': 'community', 'title': 'Debate',
                'text': 'A concern was voiced.', 'source_url': 'https://www.reddit.com/r/stocks/comments/abc/title/',
                'published_at': '2026-11-18T03:00:00+00:00', 'publisher': 'r/stocks'}
        now = datetime.fromisoformat(CUTOFF)
        merge_supplement(context, {'items': [item]}, cutoff=now, collected_at=now)
        self.assertEqual(context['NVDA']['sources'][0]['status'], 'authentication_required')
        self.assertEqual(context['NVDA']['items'][0]['availability_basis'], 'reviewed_public_permalink')
        future = {**item, 'published_at': '2026-11-18T04:01:00+00:00'}
        with self.assertRaisesRegex(ValueError, 'cutoff'):
            merge_supplement(context, {'items': [future]}, cutoff=now, collected_at=now)

    def test_cross_ticker_citation_cannot_change_judgment(self):
        class CrossTickerModel:
            calls = []

            def invoke(self, prompt, schema, call):
                return {'results': [{'ticker': 'NVDA', 'judgment_effect': 'review_required',
                                     'effect_reason': '추가 검토가 필요하다.', 'effect_evidence_ids': ['other-id'], 'findings': [
                                         {'kind': 'community', 'view': 'unfavorable', 'summary': '경쟁 우려',
                                          'evidence_ids': ['other-id']}]}]}

        data = {'NVDA': {'items': [{'id': 'nvda-id', 'kind': 'community', 'title': 'Test', 'text': 'Test'}],
                         'sources': []}}
        holdings = {'holdings': ['NVDA'], 'sections': [{'ticker': 'NVDA', 'status': 'complete',
                                                       'final': {'action': 'maintain'}}]}
        findings, audit = assess(holdings, None, data, CUTOFF, CrossTickerModel())
        self.assertEqual(findings['NVDA']['status'], 'unavailable')
        self.assertIn('citation', audit['error'])
        self.assertEqual(holdings['sections'][0]['final']['action'], 'maintain')
