"""No-network checks for official-policy discovery and durable page snapshots."""
import unittest
from unittest.mock import patch

import httpx

from foundation import policy_feed, sources


GOV = policy_feed.FEEDS['gov']['url']
MEE = policy_feed.FEEDS['mee']['url']
URL = 'https://www.gov.cn/zhengce/content/202611/content_1234567.htm'
MEE_URL = 'https://www.mee.gov.cn/xxgk2018/xxgk/xxgk03/202611/t20261119_1234567.html'


def body(title='关于促进储能和电力发展的通知', published='2026-11-19', root='ucap-content'):
    return ('<html><head><title>'+title+'_站点名称</title>'
            '<meta name="PubDate" content="'+published+'"></head><body><h1>'+title+'</h1>'
            '<div class="'+root+'"><p>'+('加强储能与电力建设，推进节能发展，保存真实公开的原文段落。'*8)+
            '</p><p>第二段明确说明实施范围，不将政策方向直接写成公司已受益。</p></div></body></html>')


def listing(urls, published='2026-11-19'):
    return '<ul>'+''.join('<li><a href="'+url+'">关于政策文件的通知</a><span>'+published+'</span></li>' for url in urls)+'</ul>'


def response(url, html):
    return httpx.Response(200,content=html.encode(),headers={'content-type':'text/html; charset=utf-8'},request=httpx.Request('GET',url))


class PolicyFeedTests(unittest.TestCase):
    def setUp(self):
        today=patch.object(policy_feed.store,'today',return_value='2026-11-20')
        today.start();self.addCleanup(today.stop)
        pacer=patch.object(policy_feed._Pacer,'wait')
        pacer.start();self.addCleanup(pacer.stop)

    def test_discovery_accepts_only_known_official_article_paths_and_real_list_dates(self):
        html=listing([URL,'https://www.gov.cn.evil.test/zhengce/content/202611/content_1.htm',
                      'https://www.gov.cn/search/index.htm',URL+'?tracking=1'])
        self.assertEqual(policy_feed.discover(html,'gov','2026-11-01','2026-11-20'),[URL])
        self.assertEqual(policy_feed.discover(html,'gov','2026-10-01','2026-10-31'),[])

    def test_no_known_links_is_a_visible_schema_failure(self):
        with self.assertRaises(sources.SourceError) as caught:
            policy_feed.discover('<html>登录或脚本页面</html>','gov','2026-11-01','2026-11-20')
        self.assertEqual(caught.exception.kind,'schema')

    def test_original_parser_keeps_paragraphs_date_hash_and_matching_terms(self):
        result=policy_feed.parse_article(body(),URL)
        self.assertEqual(result['day'],'2026-11-19')
        self.assertEqual(result['published'],'2026-11-19')
        self.assertEqual(result['title'],'关于促进储能和电力发展的通知')
        self.assertEqual(result['symbol'],'*')
        self.assertIn('【原文第 1 段】',result['text'])
        self.assertEqual(len(result['document_sha256']),64)
        self.assertTrue(all(term in result['text'] for term in result['_terms']))
        self.assertIn('储能',result['_terms'])

    def test_mee_custom_body_wrapper_uses_the_same_original_parser(self):
        result=policy_feed.parse_article(body(root='Custom_UnionStyle'),MEE_URL)
        self.assertEqual(result['title'],'关于促进储能和电力发展的通知')
        self.assertIn('第二段',result['text'])

    def test_gov_browser_title_suffix_is_excluded_from_document_title(self):
        html=body().replace('<h1>关于促进储能和电力发展的通知</h1>','').replace('_站点名称','_其他_中国政府网')
        self.assertEqual(policy_feed.parse_article(html,URL)['title'],'关于促进储能和电力发展的通知')

    def test_terms_do_not_treat_cultural_work_as_chemical_industry(self):
        html=body().replace('储能与电力建设，推进节能发展','文化工作，优化工作方法')
        self.assertEqual(policy_feed.parse_article(html,URL)['_terms'],[])
        html=body().replace('储能与电力建设，推进节能发展','化工企业管理和化学物质登记')
        self.assertEqual(policy_feed.parse_article(html,URL)['_terms'],['化工企业','化学物质'])

    def test_future_policy_and_nonofficial_page_are_not_accepted(self):
        for html,url in ((body(published='2026-11-21'),URL),(body(),'https://example.invalid/a.html')):
            with self.subTest(url=url):
                with self.assertRaises(sources.SourceError):policy_feed.parse_article(html,url)

    def test_oversized_or_non_html_source_is_not_parsed(self):
        for result in (httpx.Response(200,content=b'x'*(policy_feed.MAX_BYTES+1),headers={'content-type':'text/html'}),
                       httpx.Response(200,content=b'{}',headers={'content-type':'application/json'})):
            with patch.object(sources,'http',return_value=result):
                with self.assertRaises(sources.SourceError):policy_feed._read(URL,10,policy_feed._Pacer())

    def test_next_page_uses_frozen_urls_without_rescanning_columns(self):
        urls=[URL.replace('1234567',str(1234567+i)) for i in range(4)]
        seen=[]
        def get(method,url,**kwargs):
            seen.append(url)
            html=listing(urls) if url==GOV else listing([MEE_URL]) if url==MEE else body()
            return response(url,html)
        with patch.object(sources,'http',side_effect=get):
            first=policy_feed.collect({'start':'2026-11-01','end':'2026-11-20'})
            self.assertEqual(len(first),3)
            self.assertEqual(first.page_info['next'],2)
            second=policy_feed.collect({'start':'2026-11-01','end':'2026-11-20','page':2,
                                        **first.page_info['next_params']})
        self.assertEqual(len(second),2)
        self.assertIsNone(second.page_info['next'])
        self.assertEqual(seen.count(GOV),1)
        self.assertEqual(seen.count(MEE),1)
        self.assertEqual(len({r['url'] for r in first+second}),5)

    def test_invalid_snapshot_cannot_turn_a_page_job_into_arbitrary_http_requests(self):
        with patch.object(sources,'http') as request:
            with self.assertRaises(sources.SourceError):
                policy_feed.collect({'start':'2026-11-01','end':'2026-11-20','policy_snapshot':['https://example.invalid/a.html']})
        request.assert_not_called()

    def test_following_page_requires_the_frozen_snapshot(self):
        with patch.object(sources,'http') as request:
            with self.assertRaises(sources.SourceError):
                policy_feed.collect({'start':'2026-11-01','end':'2026-11-20','page':2})
        request.assert_not_called()

    def test_one_article_error_is_kept_beside_the_valid_original(self):
        bad=URL.replace('1234567','1234568')
        def get(method,url,**kwargs):
            if url==bad:raise sources.SourceError('来源限流','rate_limit')
            return response(url,body())
        with patch.object(sources,'http',side_effect=get):
            result=policy_feed.collect({'start':'2026-11-01','end':'2026-11-20','policy_snapshot':[URL,bad]})
        self.assertEqual(len(result),1)
        self.assertTrue(any(r['status']=='error' and r['kind']=='rate_limit' for r in result.diagnostics))

    def test_all_original_failures_raise_instead_of_empty_success(self):
        with patch.object(sources,'http',side_effect=sources.SourceError('来源暂时失败','network')):
            with self.assertRaises(sources.SourceError) as caught:
                policy_feed.collect({'start':'2026-11-01','end':'2026-11-20','policy_snapshot':[URL]})
        self.assertEqual(caught.exception.kind,'network')
        self.assertIn('来源暂时失败',str(caught.exception))
        self.assertEqual(caught.exception.diagnostics[0]['url'],URL)

    def test_all_column_failures_raise_instead_of_empty_success(self):
        with patch.object(sources,'http',side_effect=sources.SourceError('来源连接失败','network')):
            with self.assertRaises(sources.SourceError) as caught:
                policy_feed.collect({'start':'2026-11-01','end':'2026-11-20'})
        self.assertEqual(caught.exception.kind,'network')

    def test_quiet_range_is_distinguished_from_broken_or_unreadable_columns(self):
        with patch.object(sources,'http',side_effect=lambda method,url,**kw:response(url,listing([URL] if url==GOV else [MEE_URL],published='2026-10-10'))):
            result=policy_feed.collect({'start':'2026-11-01','end':'2026-11-20'})
        self.assertEqual(result,[])
        self.assertTrue(result.checked)
        self.assertTrue(any(r['status']=='checked_no_new' for r in result.diagnostics))

    def test_default_range_is_the_latest_thirty_calendar_days(self):
        with patch.object(sources,'http',side_effect=lambda method,url,**kw:response(url,listing([URL] if url==GOV else [MEE_URL],published='2026-10-20'))):
            result=policy_feed.collect({})
        self.assertEqual(result,[])
        self.assertTrue(result.checked)

    def test_real_article_publication_controls_range_even_when_index_date_is_wrong(self):
        with patch.object(sources,'http',return_value=response(URL,body(published='2026-10-10'))):
            result=policy_feed.collect({'start':'2026-11-01','end':'2026-11-20','policy_snapshot':[URL]})
        self.assertEqual(result,[])
        self.assertTrue(any(r['status']=='outside_range' for r in result.diagnostics))


if __name__=='__main__':
    unittest.main()
