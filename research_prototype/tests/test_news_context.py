import unittest
from unittest.mock import patch
from research_prototype.news_context import collect_news_context, bounded_excerpt

CUTOFF = '2026-11-19T08:50:19.052633+00:00'

def row(ticker='NVDA', **kw):
    return dict({'id': 'seed', 'ticker': ticker, 'kind': 'news', 'title': 'Nvidia earnings demand outlook raises capital spending concerns',
                 'source_url': 'https://publisher.example/story', 'published_at': '2026-11-18T12:00:00+00:00',
                 'available_at': '2026-11-18T12:00:00+00:00'}, **kw)

def html(modified='2026-11-18T13:00:00Z'):
    return ('<html><head><meta property="article:published_time" content="2026-11-18T12:00:00Z">'
            '<meta property="article:modified_time" content="'+modified+'"></head><body><article>'
            + '<p>Nvidia earnings demand and capital spending outlook present a material business risk. '+ 'Detailed industry demand analysis. '*8+'</p>'*2
            + '</article></body></html>')

class Client:
    def __init__(self, body=None, fail=False): self.body, self.fail = body or html(), fail
    def get(self, url):
        if self.fail: raise OSError('HTTP 403 blocked')
        return {'body': self.body.encode(), 'url': url, 'status': 200, 'collected_at':'2026-11-19T12:43:00+00:00'}

class NewsTests(unittest.TestCase):
    def test_default_targets_include_saved_holdings_and_candidates_with_explicit_override(self):
        previous={'qualitative_as_of':CUTOFF,'holdings':{'holdings':['TSLA']},
                  'discovery':{'horizons':{'long':{'shortlist':[{'ticker':'AMZN'}]}}},'public_context':{}}
        result=collect_news_context(previous,client=Client(),search=False)
        self.assertEqual(set(result['items']),{'TSLA','AMZN'})
        result=collect_news_context(previous,client=Client(),search=False,tickers=['META'])
        self.assertEqual(set(result['items']),{'META'})
    def collect(self, rows, client=None):
        return collect_news_context({'public_context': {t: {'items':[r for r in rows if r['ticker']==t]} for t in {r['ticker'] for r in rows}}},
                                    client=client or Client(), cutoff=CUTOFF, search=False)
    def test_body_verified_and_fetch_date_preserved(self):
        out=self.collect([row()]); item=out['items']['NVDA'][0]
        self.assertEqual(item['content_basis'],'verified_body'); self.assertFalse(item['fact_verified'])
        self.assertGreater(item['collected_at'], CUTOFF); self.assertTrue(item['body_sha256'])
    def test_future_excluded(self):
        out=self.collect([row(available_at='2026-11-20T00:00:00Z')]); self.assertFalse(out['items']['NVDA'])
        self.assertIn('after_cutoff',str(out['excluded']))
    def test_gas_local_safety_excluded(self):
        out=self.collect([row('BOIL',title='University dorm evacuated for natural gas odor')]); self.assertFalse(out['items']['BOIL'])
    def test_global_dedup_keeps_ticker_mapping(self):
        rows=[row(),row('SOXL')]; out=self.collect(rows)
        self.assertEqual(out['items']['NVDA'][0]['id'],out['items']['SOXL'][0]['id'])
        self.assertEqual(len(out['sources']),1)
    def test_share_converted_to_news_queue(self):
        out=self.collect([row(kind='community',community_subtype='article_share',external_article_url='https://publisher.example/shared')])
        self.assertEqual(out['items']['NVDA'][0]['source_kind'],'news_article')
        self.assertEqual(out['items']['NVDA'][0]['discovery_basis'],'article_share')
        item=out['items']['NVDA'][0]
        self.assertEqual(item['kind'],'news')
        self.assertEqual(item['original_kind'],'community')
        self.assertEqual(item['community_discovery_subtype'],'article_share')
        self.assertNotIn('community_subtype',item)
        self.assertEqual(item['shared_post_url'],'https://publisher.example/story')
    def test_blocked_not_promoted(self):
        out=self.collect([row()],Client(fail=True)); self.assertEqual(out['items']['NVDA'][0]['content_basis'],'headline_only')
        self.assertIn('fetch_failed', str(out['sources']))
    def test_updated_after_cutoff_no_body_leak(self):
        out=self.collect([row()],Client(html('2026-11-19T10:00:00Z')))
        self.assertEqual(out['items']['NVDA'][0]['text'],'')
        self.assertIn('updated_after_cutoff',str(out['sources']))
    def test_missing_modified_no_historical_body_claim(self):
        out=self.collect([row()],Client(html().replace('article:modified_time','not_modified')))
        self.assertEqual(out['items']['NVDA'][0]['content_basis'],'headline_only')
    def test_publisher_publication_conflict_excludes_even_headline(self):
        body=html().replace('2026-11-18T12:00:00Z','2026-11-20T12:00:00Z')
        out=self.collect([row()],Client(body)); self.assertFalse(out['items']['NVDA'])
    def test_syndicated_identical_title_globally_deduplicated(self):
        out=self.collect([row(),row(source_url='https://second.example/same')])
        self.assertEqual(len(out['items']['NVDA']),1)
    def test_excerpt_preserves_paragraph_boundary(self):
        first='A useful article sentence. '*130
        value=first+'\n'+('Another longer paragraph. '*100)
        self.assertEqual(bounded_excerpt(value), first.rstrip())
        self.assertLessEqual(len(bounded_excerpt(value)),4200)
    def test_missing_parser_fails_before_source_collection(self):
        with patch.dict('sys.modules',{'bs4':None}):
            with self.assertRaisesRegex(RuntimeError, 'beautifulsoup4'):
                self.collect([row('BOIL',title='University dorm evacuated for natural gas odor')])

if __name__=='__main__': unittest.main()
