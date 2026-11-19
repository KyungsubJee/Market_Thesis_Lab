"""Bounded public article collection with explicit historical-version limitations.

Extraction verification is not fact verification. Retrospective bodies require both
publisher publication and last-modification metadata within the requested cutoff.
No authentication, paywall circumvention, or model-generated article text is used.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from hashlib import sha256
from html import unescape
import json
from pathlib import Path
import re
import threading
from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET

NAMES = {'NVDA':'Nvidia', 'SOXL':'US semiconductor stocks', 'SOXS':'US semiconductor stocks',
         'BOIL':'natural gas futures LNG storage weather Henry Hub', 'KOLD':'natural gas futures LNG storage weather Henry Hub',
         '005930':'삼성전자', '000660':'SK하이닉스', '498400':'코스피200 정기변경',
         '486290':'나스닥100', 'AVGO':'Broadcom', 'PLTR':'Palantir', 'MSFT':'Microsoft', 'COST':'Costco'}
FINANCIAL = re.compile(r'earnings|revenue|demand|forecast|outlook|guidance|profit|margin|investigat|chief|CEO|capital|'
                       r'export|tariff|sanction|valuation|supply|futures|storage|LNG|weather|Henry Hub|pipeline|'
                       r'매출|실적|전망|수요|공급|규제|경영|반도체|네오클라우드|정기변경|편입|금리|지수|로봇',re.I)
LOCAL_GAS = re.compile(r'gas odor|gas smell|dorm|stadium|evacuat|gas leak|carbon monoxide|기숙사|가스냄새',re.I)
GAS_MARKET = re.compile(r'futures|Henry Hub|LNG|storage|EIA|production|export|weather|heating|supply|demand|'
                        r'천연가스.{0,20}(?:선물|재고|수급)|gas prices',re.I)
PROMO = re.compile(r'join (?:my|our)|subscribe (?:to|now)|guaranteed return|무료방|가입하기|텔레그램|보유자 필독',re.I)
TRACKING = {'fbclid','gclid','oc','ref'}

class NewsContextDependencyError(RuntimeError):
    """Local setup failure, never an unavailable publisher or empty news result."""

def require_html_parser():
    try:
        from bs4 import BeautifulSoup
    except ImportError as error:
        raise NewsContextDependencyError(
            'News collection requires the beautifulsoup4 package in the active Python environment. '
            'Install this project requirements.txt with that Python interpreter before retrying; '
            'no source collection was attempted. This is a local dependency issue, not a publisher access failure.'
        ) from error
    return BeautifulSoup

def stamp(value):
    if isinstance(value,datetime): return value.astimezone(timezone.utc) if value.tzinfo else None
    if not isinstance(value,str): return None
    try: result=datetime.fromisoformat(value.replace('Z','+00:00'))
    except ValueError:
        try: result=parsedate_to_datetime(value)
        except (ValueError,TypeError): return None
    return result.astimezone(timezone.utc) if result.tzinfo else None

def iso(value): return value.astimezone(timezone.utc).isoformat()
def tidy(value): return ' '.join(unescape(re.sub('<[^>]+>',' ',value or '')).split())
def bounded_excerpt(text,limit=4200):
    if len(text)<=limit: return text
    prefix=text[:limit]
    boundary=prefix.rfind('\n')
    if boundary>=int(limit*.6): return prefix[:boundary].rstrip()
    matches=list(re.finditer(r'[.!?。](?:\s|$)|다\.(?:\s|$)',prefix))
    if matches and matches[-1].end()>=int(limit*.6): return prefix[:matches[-1].end()].rstrip()
    return prefix.rsplit(' ',1)[0].rstrip() if ' ' in prefix else prefix
def canonical(url):
    p=urlsplit(url or '')
    return urlunsplit((p.scheme.lower(),p.netloc.lower(),p.path.rstrip('/') or '/',
                       urlencode([(k,v) for k,v in parse_qsl(p.query) if not k.startswith('utm_') and k not in TRACKING]),''))

def normalize_article_share(row):
    """Separate article evidence classification from its social discovery route."""
    if row.get('community_subtype')=='article_share' or (row.get('kind')=='community' and row.get('discovery_basis')=='article_share'):
        row={**row,'original_kind':row.get('original_kind',row.get('kind','community')),
             'kind':'news','community_discovery_subtype':'article_share','discovery_basis':'article_share'}
        row.pop('community_subtype',None)
    return row

class PublicClient:
    def __init__(self,max_requests=64): self.max_requests=max_requests; self.requests=0; self.lock=threading.Lock()
    def get(self,url):
        p=urlsplit(url)
        if p.scheme!='https' or not p.hostname or p.username: raise ValueError('Not a public HTTPS URL')
        with self.lock:
            if self.requests>=self.max_requests: raise RuntimeError('request_budget_exhausted')
            self.requests+=1
        request=Request(url,headers={'User-Agent':'MarketThesisLab/0.1 public research contact-free client','Accept':'text/html,application/rss+xml,application/xml;q=0.9,*/*;q=0.5'})
        with urlopen(request,timeout=9) as response:
            body=response.read(2_000_001)
            if len(body)>2_000_000: raise ValueError('response_size_limit')
            return {'body':body,'url':response.url,'status':response.status,'content_type':response.headers.get('Content-Type',''),
                    'collected_at':iso(datetime.now(timezone.utc))}

def response(client,url):
    value=client.get(url)
    if isinstance(value,tuple):
        body,when=value
        return {'body':body.encode() if isinstance(body,str) else body,'url':url,'status':200,'collected_at':iso(when)}
    return value

def parse_article(body,url):
    BeautifulSoup=require_html_parser()
    soup=BeautifulSoup(body,'html.parser')
    published=[]; updated=[]; authors=[]; publishers=[]
    for node in soup.find_all('meta'):
        key=(node.get('property') or node.get('name') or node.get('itemprop') or '').lower()
        val=node.get('content')
        if key in {'article:published_time','datepublished','pubdate','date','parsely-pub-date','sailthru.date'}: published.append(val)
        if key in {'article:modified_time','datemodified','lastmod','last-modified','og:updated_time'}: updated.append(val)
        if key in {'author','article:author','parsely-author'} and val: authors.append(val)
    def walk(obj):
        if isinstance(obj,list):
            for entry in obj: walk(entry)
        elif isinstance(obj,dict):
            if obj.get('datePublished'): published.append(obj['datePublished'])
            if obj.get('dateModified'): updated.append(obj['dateModified'])
            for field,target in (('author',authors),('publisher',publishers)):
                value=obj.get(field)
                for entry in value if isinstance(value,list) else [value]:
                    name=entry.get('name') if isinstance(entry,dict) else entry
                    if isinstance(name,str): target.append(name)
            for value in obj.values():
                if isinstance(value,(dict,list)): walk(value)
    for node in soup.find_all('script',type='application/ld+json'):
        try: walk(json.loads(node.string or node.get_text()))
        except (ValueError,TypeError): pass
    pubs=[stamp(x) for x in published if stamp(x)]; mods=[stamp(x) for x in updated if stamp(x)]
    for node in soup(['script','style','nav','header','footer','aside','form','noscript']): node.decompose()
    root=soup.find('article') or soup.find('main') or soup
    paragraphs=[tidy(p.get_text(' ',strip=True)) for p in root.find_all('p')]
    text='\n'.join(dict.fromkeys(p for p in paragraphs if len(p)>=45 and not re.search(
        r'want the latest recommendations|download .* free report|subscribe to our newsletter|click to get this free report',p,re.I)))
    title=tidy(soup.title.get_text()) if soup.title else ''
    return {'text':text,'publisher_published_at':iso(max(pubs)) if pubs else None,
            'publisher_updated_at':iso(max(mods)) if mods else None,'title':title,
            'article_authors':list(dict.fromkeys(authors)),'article_publishers':list(dict.fromkeys(publishers)),
            'date_metadata_raw':{'published':published,'updated':updated}}

def relevance(ticker,title,text=''):
    value=title+' '+text[:500]
    if PROMO.search(value): return 0,'promotion'
    if ticker in {'BOIL','KOLD'}:
        if LOCAL_GAS.search(title): return 0,'local_gas_safety_not_market_supply_demand'
        if not GAS_MARKET.search(value): return 0,'gas_market_link_not_established'
    if ticker in {'SOXL','SOXS'} and not re.search(r'semiconductor|Nvidia|chip|반도체|엔비디아',value,re.I):
        return 0,'semiconductor_link_not_established'
    named=NAMES.get(ticker,ticker)
    if ticker not in {'BOIL','KOLD','SOXL','SOXS','498400','486290'}:
        if not re.search(re.escape(named)+r'|\$?'+re.escape(ticker)+r'\b',value,re.I): return 0,'issuer_link_not_established'
    score=2+min(4,len(FINANCIAL.findall(value)))
    if re.search(r'earnings|guidance|CEO|export|investigat|실적|정기변경|네오클라우드',title,re.I): score+=3
    if ticker in {'SOXL','SOXS'} and re.search(r'Japan|Nikkei|일본',title,re.I) and not re.search(r'US |U.S.|Nvidia|미국',title,re.I): score-=2
    return score,'materiality_heuristic_not_investment_verdict'

def rss_candidates(body,ticker,provider):
    rows=[]
    root=ET.fromstring(body)
    for node in root.findall('.//item')[:35]:
        link=node.findtext('link') or ''; title=tidy(node.findtext('title'))
        if 'bing.com' in urlsplit(link).netloc:
            link=dict(parse_qsl(urlsplit(link).query)).get('url',link)
        when=stamp(node.findtext('pubDate'))
        rows.append({'ticker':ticker,'kind':'news','title':title,'source_url':link,
                     'published_at':iso(when) if when else None,'available_at':iso(when) if when else None,
                     'availability_basis':provider+'_feed_timestamp','provider':provider,'discovery_basis':'rss_search'})
    return rows

def collect_news_context(previous,client=None,cutoff=None,tickers=None,output_dir=None,max_requests=64,search=True,search_tickers=None,search_queries=None):
    require_html_parser()
    cutoff=stamp(cutoff or previous.get('qualitative_as_of'))
    if cutoff is None: raise ValueError('An offset-aware cutoff is required')
    started=datetime.now(timezone.utc); client=client or PublicClient(max_requests)
    is_run='holdings' in previous or 'discovery' in previous
    mapping=previous.get('public_context',{} if is_run else previous)
    if tickers is None:
        held=(previous.get('holdings') or {}).get('holdings', []) if is_run else []
        candidates=[row['ticker'] for period in (previous.get('discovery') or {}).get('horizons',{}).values()
                    for row in period.get('shortlist',[])] if is_run else []
        tickers=list(dict.fromkeys([*held,*candidates,*mapping]))
    else:
        tickers=list(dict.fromkeys(tickers))
    items={t:[] for t in tickers}; excluded=[]; sources=[]; candidates=[]
    directory=Path(output_dir) if output_dir else None
    if directory: (directory/'bodies').mkdir(parents=True,exist_ok=True)
    def progress(value):
        if directory:
            with (directory/'collection-progress.jsonl').open('a',encoding='utf-8') as stream:
                stream.write(json.dumps(value,ensure_ascii=False)+'\n')
    for ticker in tickers:
        value=mapping.get(ticker,{})
        for row in value.get('items',[]) if isinstance(value,dict) else value:
            candidates.append({**row,'ticker':ticker,'discovery_basis':'baseline'})
    search_audit=[]
    if search:
        tasks=[]
        for ticker in search_tickers if search_tickers is not None else tickers:
            query=(search_queries or {}).get(ticker,NAMES.get(ticker,ticker))+' after:'+(cutoff-timedelta(days=7)).date().isoformat()+' before:'+(cutoff+timedelta(days=1)).date().isoformat()
            tasks.append((ticker,'bing_news_rss','https://www.bing.com/news/search?'+urlencode({'q':query,'format':'rss'})))
            if re.fullmatch(r'[A-Z][A-Z0-9.\-]{0,9}',ticker):
                tasks.append((ticker,'yahoo_finance_rss','https://finance.yahoo.com/rss/headline?'+urlencode({'s':ticker})))
        def fetch_search(task):
            ticker,provider,url=task; audit={'ticker':ticker,'provider':provider,'query_url':url}
            try:
                result=response(client,url); rows=rss_candidates(result['body'],ticker,provider)
                if directory:
                    rawpath=directory/'bodies'/('rss-'+sha256(url.encode()).hexdigest()[:16]+'.xml')
                    rawbody=result['body']; rawbody=rawbody.encode() if isinstance(rawbody,str) else rawbody
                    rawpath.write_bytes(rawbody)
                    audit.update(body_path=str(rawpath.resolve()),body_sha256=sha256(rawbody).hexdigest())
                audit.update(status='available' if rows else 'searched_zero',raw_count=len(rows),collected_at=result['collected_at'])
                return rows,audit
            except Exception as err:
                audit.update(status='fetch_failed',error=str(err)[:250],raw_count=0)
                return [],audit
        with ThreadPoolExecutor(max_workers=4) as pool:
            for rows,audit in pool.map(fetch_search,tasks):
                candidates.extend(rows); search_audit.append(audit); progress({'phase':'search',**audit})
    queue={}; opinions=[]; title_keys={}
    for row in candidates:
        ticker=row['ticker']; reason=None
        when=stamp(row.get('available_at') or row.get('published_at'))
        if when is None: reason='unverified_publication_time'
        elif when>cutoff: reason='after_cutoff'
        elif when<cutoff-timedelta(days=7): reason='outside_lookback'
        subtype=row.get('community_subtype'); link=row.get('source_url','')
        if row.get('kind')=='community' and subtype=='article_share':
            link=row.get('external_article_url') or ''
            row={**row,'discovery_basis':'article_share','shared_post_url':row.get('source_url'),'shared_author':row.get('author')}
            if not link: reason=reason or 'article_share_without_original_article_url'
            if re.search(r'gta-terminal|trading.?signals?',row.get('author') or '',re.I):
                reason=reason or 'automated_promotional_trading_signal_share'
            row=normalize_article_share(row)
        elif row.get('kind')=='community':
            if subtype!='investor_commentary': reason=reason or 'not_individual_opinion'
            if not reason:
                opinion={**row,'content_basis':'individual_opinion','fact_verified':False,'extraction_verified':False,
                         'grade':'individual_opinion','source_kind':'public_individual_opinion','usage_scope':'anecdotal_not_representative'}
                opinions.append(opinion)
            else: excluded.append({'id':row.get('id'),'ticker':ticker,'source_url':link,'reason':reason})
            continue
        score,why=relevance(ticker,row.get('title',''))
        if score<=0: reason=reason or why
        if not link.startswith('https://'): reason=reason or 'invalid_original_url'
        if reason:
            excluded.append({'id':row.get('id'),'ticker':ticker,'title':row.get('title'),'source_url':link,'reason':reason}); continue
        key=canonical(link)
        titlekey=re.sub(r'\W+','',row.get('title','').lower())
        if titlekey in title_keys and key!=title_keys[titlekey]:
            key=title_keys[titlekey]
            excluded.append({'id':row.get('id'),'ticker':ticker,'source_url':link,'reason':'syndicated_duplicate_title','retained_url':key})
        title_keys[titlekey]=key
        if key not in queue: queue[key]={'row':{**row,'source_url':link},'tickers':[],'score':score,'seed_ids':[]}
        queue[key]['tickers']=list(dict.fromkeys(queue[key]['tickers']+[ticker]))
        queue[key]['seed_ids'].append(row.get('id')); queue[key]['score']=max(score,queue[key]['score'])
    ranked=sorted(queue.items(),key=lambda x:x[1]['score'],reverse=True)
    selected=[]; counts={t:0 for t in tickers}
    for key,entry in ranked:
        affected=[t for t in entry['tickers'] if counts[t]<4]
        if not affected:
            excluded.append({'source_url':key,'tickers':entry['tickers'],'reason':'bounded_materiality_rank'}); continue
        entry['tickers']=affected; selected.append((key,entry))
        for ticker in affected: counts[ticker]+=1
    # One direct publisher attempt per ticker before spending on second/third articles.
    ordered=[]; remaining=selected[:]
    for ticker in tickers:
        options=[pair for pair in remaining if ticker in pair[1]['tickers']]
        options.sort(key=lambda pair: ('news.google.com' in pair[0] or 'msn.com' in pair[0],-pair[1]['score']))
        if options:
            ordered.append(options[0]); remaining.remove(options[0])
    selected=ordered+remaining
    def fetch_article(pair):
        key,entry=pair; row=entry['row']; article_id='news-context-'+sha256(key.encode()).hexdigest()[:16]
        audit={'id':article_id,'source_url':row['source_url'],'tickers':entry['tickers'],'title':row.get('title'),
               'seed_ids':entry['seed_ids'],'discovery_basis':row.get('discovery_basis'),'importance_score':entry['score'],
               'cutoff':iso(cutoff),'status':'headline_only','reason':None}
        parsed={}; text=''; collected=iso(datetime.now(timezone.utc)); final_url=row['source_url']
        try:
            result=response(client,row['source_url']); collected=result['collected_at']; final_url=result['url']; body=result['body']
            if isinstance(body,str): body=body.encode()
            body_hash=sha256(body).hexdigest(); path=None
            if directory:
                path=directory/'bodies'/(article_id+'.html'); path.write_bytes(body)
            audit.update(http_status=result.get('status'),collected_at=collected,final_url=final_url,body_sha256=body_hash,body_path=str(path.resolve()) if path else None)
            # Google RSS URLs are indexes, never publisher bodies even if HTML has paragraphs.
            if urlsplit(final_url).hostname in {'news.google.com','www.bing.com','bing.com'}:
                from bs4 import BeautifulSoup
                soup=BeautifulSoup(body,'html.parser')
                links=[a.get('href') for a in soup.select('a[href]') if a.get('href','').startswith('https://')
                       and not any(h in urlsplit(a.get('href')).netloc for h in ('google.','bing.com','microsoft.com','gstatic.'))]
                links=list(dict.fromkeys(links))
                if len(links)==1:
                    result=response(client,links[0]); final_url=result['url']; body=result['body']; collected=result['collected_at']
                    if isinstance(body,str): body=body.encode()
                    body_hash=sha256(body).hexdigest()
                    if directory: path=directory/'bodies'/(article_id+'-publisher.html'); path.write_bytes(body)
                    audit.update(final_url=final_url,collected_at=collected,body_sha256=body_hash,body_path=str(path.resolve()) if path else None)
                else: raise ValueError('aggregator_original_url_unresolved')
            parsed=parse_article(body,final_url)
            pub=stamp(parsed['publisher_published_at']); mod=stamp(parsed['publisher_updated_at'])
            if pub and pub>cutoff: audit['reason']='publisher_published_after_cutoff'
            elif len(parsed['text'])<240: audit['reason']='body_missing_short_or_access_wall'
            elif pub is None: audit['reason']='publisher_publication_time_unverified'
            elif mod and mod>cutoff: audit['reason']='updated_after_cutoff'
            elif stamp(collected)>cutoff and mod is None: audit['reason']='historical_body_version_unverified'
            else: text=bounded_excerpt(parsed['text']); audit.update(status='verified_body',reason='publisher_body_and_temporal_metadata_extracted')
        except Exception as err: audit['reason']='fetch_failed: '+str(err)[:200]
        audit.update({k:v for k,v in parsed.items() if k!='text'})
        publisher_type='public_analysis_author' if any(host in urlsplit(final_url).netloc for host in ('note.com','tikr.com','fool.com','coin24.news','247wallst.com')) else 'news_publisher_unverified'
        evidence={**row,'id':article_id,'tickers':entry['tickers'],'source_url':final_url,'original_feed_url':row['source_url'],
                  'title':row.get('title'),'text':text,'published_at':row.get('published_at'),'available_at':row.get('available_at'),
                  'collected_at':collected,'source_kind':'news_article' if text else 'news_index_result',
                  'content_basis':audit['status'],'grade':audit['status'],'extraction_verified':bool(text),'fact_verified':False,
                  'availability_basis':row.get('availability_basis','feed_publication_timestamp'),
                  'publisher_published_at':parsed.get('publisher_published_at'),'publisher_updated_at':parsed.get('publisher_updated_at'),
                  'body_sha256':audit.get('body_sha256'),'body_path':audit.get('body_path'),'body_status_reason':audit['reason'],
                  'publisher_type':publisher_type,'independence_verified':False,
                  'article_authors':parsed.get('article_authors',[]),'article_publishers':parsed.get('article_publishers',[]),
                  'publisher_domain':urlsplit(final_url).hostname,
                  'body_full_character_count':len(parsed.get('text','')),'text_truncated':bool(text and len(text)<len(parsed.get('text',''))),
                  'excerpt_limit_characters':4200,'excerpt_caution':'Bounded opening paragraphs may omit later qualifications; raw body is retained.',
                  'discovery_basis':row.get('discovery_basis'),'importance_score':entry['score'],'event_at':None,
                  'event_at_status':'not_independently_verified','retrospective_fetch':stamp(collected)>cutoff,
                  'usage_scope':'article_claim_not_independently_fact_checked' if text else 'verification_lead_only'}
        return evidence,audit
    with ThreadPoolExecutor(max_workers=4) as pool:
        for evidence,audit in pool.map(fetch_article,selected):
            sources.append(audit); progress({'phase':'article',**audit})
            if audit['reason']=='publisher_published_after_cutoff':
                excluded.append({'id':evidence['id'],'tickers':evidence['tickers'],'source_url':evidence['source_url'],'reason':'publisher_published_after_cutoff'})
                audit['admitted']=False
                continue
            audit['admitted']=True
            for ticker in evidence['tickers']: items[ticker].append({**evidence,'ticker':ticker})
    for row in opinions:
        if len([x for x in items[row['ticker']] if x['content_basis']=='individual_opinion'])<2:
            items[row['ticker']].append(row)
    unique={row['id']:row for rows in items.values() for row in rows}
    bundle={'as_of':iso(cutoff),'collected_at':iso(datetime.now(timezone.utc)), 'items':items,'sources':sources,'raw_candidates':candidates,
            'excluded':excluded,'searches':search_audit,'summary':{'raw_candidates':len(candidates),'unique_article_urls':len(queue),
            'selected_unique_articles':len(selected),'verified_bodies':sum(s['status']=='verified_body' for s in sources),
            'headline_only':sum(s['status']=='headline_only' for s in sources),'individual_opinions':sum(x['content_basis']=='individual_opinion' for x in unique.values()),
            'excluded_count':len(excluded),'requests':getattr(client,'requests',None),'request_budget':max_requests},
            'limitations':['Body extraction does not verify article claims.','Retrospective collection times are not reset to the analysis cutoff.',
                          'Unknown event times and publisher modifications are explicit gaps.','Article shares are discovery leads, not independent personal opinions.']}
    if directory:
        (directory/'news-context.json').write_text(json.dumps(bundle,ensure_ascii=False,indent=2),encoding='utf-8')
    return bundle

def resume_news_context(path,max_new_requests=24):
    """Targeted second pass: cached HTTP bodies keep original collection times."""
    require_html_parser()
    path=Path(path); prior=json.loads(path.read_text(encoding='utf-8'))
    archive=path.with_name('news-context-first-pass.json')
    if not archive.exists(): archive.write_text(json.dumps(prior,ensure_ascii=False,indent=2),encoding='utf-8')
    cached={}
    for source in prior['sources']:
        if source.get('body_path') and Path(source['body_path']).exists():
            for url in (source.get('source_url'),source.get('final_url')):
                if url: cached[canonical(url)]=source
    class CacheClient(PublicClient):
        def get(self,url):
            source=cached.get(canonical(url))
            if source:
                body=Path(source['body_path']).read_bytes()
                if sha256(body).hexdigest()!=source['body_sha256']: raise ValueError('cache_hash_mismatch')
                return {'body':body,'url':source.get('final_url') or url,'status':source.get('http_status',200),'collected_at':source['collected_at']}
            return super().get(url)
    client=CacheClient(max_new_requests)
    mapping={ticker:{'items':[row for row in rows if row.get('body_status_reason')!='publisher_published_after_cutoff']}
             for ticker,rows in prior['items'].items()}
    result=collect_news_context({'public_context':mapping},client=client,cutoff=prior['as_of'],tickers=list(mapping),
                                output_dir=path.parent,max_requests=max_new_requests,search_tickers=list(mapping))
    # The first pass remains a complete immutable audit, including conflicting dates.
    result['previous_collection_audit']=str(archive.resolve())
    result['summary']['cumulative_requests']=prior['summary']['requests']+client.requests
    result['summary']['previous_pass_requests']=prior['summary']['requests']
    result['summary']['new_requests']=client.requests
    result['summary']['new_request_budget']=max_new_requests
    result['limitations'].append('Second-pass candidates cover the retained first-pass set plus targeted new feeds; first-pass exclusions remain in the separate audit.')
    path.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    return result

def finalize_stored_context(path):
    """Re-extract stored bytes only; no network and no publication date changes."""
    require_html_parser()
    path=Path(path); bundle=json.loads(path.read_text(encoding='utf-8'))
    for rows in bundle['items'].values():
        for index,row in enumerate(rows):
            row=normalize_article_share(row)
            rows[index]=row
            row['publisher_domain']=urlsplit(row.get('source_url','')).hostname
            row['fact_verified']=False
            if row.get('body_path'):
                body=Path(row['body_path']).read_bytes()
                if sha256(body).hexdigest()!=row['body_sha256']: raise ValueError('cache_hash_mismatch')
                parsed=parse_article(body,row['source_url'])
                row['article_authors']=parsed['article_authors']; row['article_publishers']=parsed['article_publishers']
                if row.get('content_basis')=='verified_body':
                    row['text']=bounded_excerpt(parsed['text'])
                    row['body_full_character_count']=len(parsed['text']); row['text_truncated']=len(row['text'])<len(parsed['text'])
                    row['excerpt_limit_characters']=4200
                    row['excerpt_caution']='Bounded opening paragraphs may omit later qualifications; raw body is retained.'
            title=row.get('title','').lower()
            topic=re.sub(r'\W+',' ',title).strip()
            if topic:
                row['event_cluster_id']='news-event-'+sha256(topic.encode()).hexdigest()[:16]
                row['event_cluster_basis']='normalized_title_only_not_independent_event_confirmation'
                row['weighting_caution']='Related reports may describe the same event; article count does not establish independent corroboration.'
    bundle['coverage']={ticker:{'admitted_records':len(rows),'verified_body_count':sum(r.get('content_basis')=='verified_body' for r in rows),
                               'headline_only_count':sum(r.get('content_basis')=='headline_only' for r in rows),
                               'status':'body_available' if any(r.get('content_basis')=='verified_body' for r in rows) else ('headline_only' if rows else 'no_cutoff_eligible_sources'),
                               'gap':'No cutoff-eligible article body verified' if not any(r.get('content_basis')=='verified_body' for r in rows) else None}
                        for ticker,rows in bundle['items'].items()}
    bundle['collection_summary_note']='verified_body counts successful extraction and timestamp checks only; author claims and numerical accuracy remain unverified.'
    unique={row['id']:row for rows in bundle['items'].values() for row in rows}
    bundle['summary']['admitted_unique_records']=len(unique)
    bundle['summary']['admitted_verified_bodies']=sum(r.get('content_basis')=='verified_body' for r in unique.values())
    bundle['summary']['admitted_headline_only']=sum(r.get('content_basis')=='headline_only' for r in unique.values())
    bundle['summary']['admitted_individual_opinions']=sum(r.get('content_basis')=='individual_opinion' for r in unique.values())
    bundle['summary']['admitted_ticker_records']=sum(len(rows) for rows in bundle['items'].values())
    bundle['summary']['rejected_after_body_inspection']=sum(not s.get('admitted',True) for s in bundle['sources'])
    path.write_text(json.dumps(bundle,ensure_ascii=False,indent=2),encoding='utf-8')
    return bundle

if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(); parser.add_argument('--baseline'); parser.add_argument('--output'); parser.add_argument('--resume'); parser.add_argument('--finalize')
    parser.add_argument('--cutoff'); parser.add_argument('--tickers',nargs='+'); parser.add_argument('--max-requests',type=int,default=64)
    args=parser.parse_args()
    if args.finalize:
        result=finalize_stored_context(args.finalize)
    elif args.resume:
        result=resume_news_context(args.resume,args.max_requests)
    else:
        if not args.baseline or not args.output: parser.error('--baseline and --output are required without --resume')
        baseline=json.loads(Path(args.baseline).read_text(encoding='utf-8'))
        result=collect_news_context(baseline,cutoff=args.cutoff,tickers=args.tickers,output_dir=args.output,max_requests=args.max_requests)
    print(json.dumps(result['summary'],ensure_ascii=False))
