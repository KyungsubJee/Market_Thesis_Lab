from copy import deepcopy
import json
import unittest

from research_prototype.news_candidates import reanalyze_candidates
from research_prototype.business_discovery import BUSINESS_FIELDS


def fixture():
    candidates, analyses, horizons = [], {}, {h: {'shortlist': [], 'excluded': [], 'score_fields': []} for h in ('short','medium','long')}
    for ticker, horizon in [('AVGO','medium'),('PLTR','medium'),('MSFT','long'),('COST','long')]:
        sid = 'sec-' + ticker.lower() + ('-quarter' if horizon == 'medium' else '-annual')
        business = 'business-' + ticker.lower()
        sources = [{'id': business, 'text': 'Business products'}, {'id': sid, 'text': 'Realized results'}]
        candidates.append({'ticker':ticker,'evidence_ids':[business,sid], 'business_evidence_ids':[business], 'realized_evidence_ids':[sid], 'sources':sources,'eligible_horizons':[horizon]})
        item = {f: {'text':'공시 기반 분석', 'evidence_ids':[sid if f == 'evidence' else business]} for f in BUSINESS_FIELDS}
        item.update(ticker=ticker,management_claims_status='partly_verified',selection_reason='조건 확인',horizons={horizon:{'action':'monitor','proposal':{'text':'추가 조사','evidence_ids':[sid]},'reassessment':{'text':'다음 실적 확인','evidence_ids':[sid]}}})
        analyses[ticker]=item
        horizons[horizon]['shortlist'].append({'ticker':ticker,'evidence_ids':[sid]})
    previous={'as_of':'2026-11-19T08:43:10Z','business_deep':{'input':{'candidates':candidates,'peer_context':[]},'calls':[{'role':'old'}]},'business_analyses':analyses,'horizons':horizons,'evidence':{},'calls':[{'role':'old'}]}
    source={'id':'news-context-avgo','ticker':'AVGO','text':'Article body','content_basis':'verified_body','publisher_type':'public_analysis_author','fact_verified':False,'extraction_verified':True,'publisher_published_at':'2026-11-19T07:00:00Z','published_at':'2026-11-19T07:00:00Z','publisher_updated_at':None,'collected_at':'2026-11-19T09:00:00Z'}
    return previous, {'as_of':'2026-11-19T08:50:19Z','items':{'AVGO':[source]}}


class Model:
    def __init__(self, previous, mutate=None): self.previous=previous; self.mutate=mutate; self.payload=None
    def invoke(self,prompt,schema,call):
        self.payload=json.loads(prompt.split('\n',1)[1])
        analyses=[]
        for item in self.previous['business_analyses'].values():
            item=deepcopy(item); item['horizons']=[{'horizon':h,**d} for h,d in item['horizons'].items()]; analyses.append(item)
        result={'analyses':analyses,'news_reviews':[{'ticker':c['ticker'],'source_id':s['id'],'disposition':'context_only','reason':'주장 검증 필요', 'impacts':{f:{'changed':False,'reason':'공시 기준 유지'} for f in ('outlook','counterargument','proposal','reassessment')}} for c in self.payload['candidates'] for s in c['sources'] if s['id'].startswith('news-context-')]}
        if self.mutate: self.mutate(result)
        return result


class CandidateTests(unittest.TestCase):
    def test_arbitrary_single_saved_candidate_and_zero_candidates(self):
        previous,bundle=fixture()
        previous=json.loads(json.dumps(previous).replace('AVGO','AMZN').replace('avgo','amzn'))
        bundle=json.loads(json.dumps(bundle).replace('AVGO','AMZN').replace('avgo','amzn'))
        previous['business_analyses']={k:v for k,v in previous['business_analyses'].items() if k=='AMZN'}
        previous['business_deep']['input']['candidates']=[row for row in previous['business_deep']['input']['candidates'] if row['ticker']=='AMZN']
        for period in previous['horizons'].values():
            period['shortlist']=[row for row in period['shortlist'] if row['ticker']=='AMZN']
        result=reanalyze_candidates(previous,bundle,Model(previous))
        self.assertEqual(result['model_status'],'complete')
        self.assertEqual(result['news_processing']['target_tickers'],['AMZN'])
        previous['business_analyses']={}
        for period in previous['horizons'].values(): period['shortlist']=[]
        class NoModel:
            def invoke(self,*args): raise AssertionError('No candidate requires no model')
        result=reanalyze_candidates(previous,bundle,NoModel())
        self.assertEqual(result['calls'],[])
        self.assertEqual(result['model_status'],'complete')
    def test_one_batch_preserves_baseline_financials_and_periods(self):
        previous, bundle=fixture(); original=deepcopy(previous); model=Model(previous)
        result=reanalyze_candidates(previous,bundle,model)
        self.assertEqual(previous,original)
        self.assertEqual(len(result['calls']),1)
        self.assertEqual(result['news_processing']['screen_model_calls'],0)
        self.assertEqual(set(result['business_analyses']),{'AVGO','PLTR','MSFT','COST'})
        self.assertEqual(result['horizons'],previous['horizons'])
        source=model.payload['candidates'][0]['sources'][-1]
        self.assertFalse(source['fact_verified'])
        self.assertEqual(source['publisher_type'],'public_analysis_author')
        self.assertEqual(model.payload['candidates'][0]['sources'][:2],original['business_deep']['input']['candidates'][0]['sources'])
        self.assertEqual(result['model_status'],'complete')

    def test_future_modified_body_is_excluded_but_late_collection_allowed(self):
        previous,bundle=fixture(); future=deepcopy(bundle['items']['AVGO'][0]);future.update(id='news-context-future',publisher_updated_at='2026-11-19T09:01:00Z')
        bundle['items']['AVGO'].append(future); model=Model(previous)
        result=reanalyze_candidates(previous,bundle,model)
        self.assertEqual([r['source_id'] for r in result['news_processing']['source_reviews']],['news-context-avgo'])
        self.assertEqual(result['news_processing']['excluded_sources'][0]['id'],'news-context-future')

    def test_missing_source_review_rejects_output_and_keeps_raw(self):
        previous,bundle=fixture();model=Model(previous,lambda output:output.update(news_reviews=[]))
        result=reanalyze_candidates(previous,bundle,model)
        self.assertEqual(result['model_status'],'error')
        self.assertIn('unvalidated_output',result['calls'][0])
        self.assertEqual(result['business_analyses'],previous['business_analyses'])

    def test_scope_audit_records_unchanged_business_and_next_actions(self):
        previous,bundle=fixture();result=reanalyze_candidates(previous,bundle,Model(previous))
        audit=result['news_processing']
        self.assertTrue(audit['frozen_business_sources_unchanged'])
        self.assertEqual(audit['admitted_source_ids'],['news-context-avgo'])
        self.assertEqual(audit['next_steps'][0]['action'],'monitor')
        self.assertTrue(audit['next_steps'][0]['checkpoint_required'])
        self.assertNotIn('review_required',audit['next_steps'][0])
        self.assertEqual(audit['peer_context_sha256_before'],audit['peer_context_sha256_after'])

    def test_forged_verified_body_is_excluded_before_model_input(self):
        for field,value in [('text','  '),('extraction_verified',False),('publisher_published_at',None)]:
            with self.subTest(field=field):
                previous,bundle=fixture();bundle['items']['AVGO'][0][field]=value
                result=reanalyze_candidates(previous,bundle,Model(previous))
                self.assertEqual(result['news_processing']['admitted_source_ids'],[])
                self.assertEqual(result['news_processing']['excluded_sources'][0]['reason'],'verified_body_requires_verified_extraction_text_and_publisher_timestamp')
                self.assertNotIn('news-context-avgo',result['business_deep']['input']['candidates'][0]['evidence_ids'])

    def test_changed_period_is_rejected(self):
        previous,bundle=fixture();model=Model(previous,lambda output:output['analyses'][0].update(horizons=[]))
        result=reanalyze_candidates(previous,bundle,model)
        self.assertEqual(result['model_status'],'error')

    def test_headline_cannot_be_promoted_into_realized_evidence(self):
        previous,bundle=fixture();bundle['items']['AVGO'][0]['content_basis']='headline_only'
        def promote(output): output['analyses'][0]['evidence']['evidence_ids'].append('news-context-avgo')
        result=reanalyze_candidates(previous,bundle,Model(previous,promote))
        self.assertEqual(result['model_status'],'error')

if __name__=='__main__': unittest.main()
