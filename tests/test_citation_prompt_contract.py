"""The prompt and every actual original-citing statement obey the same validator."""
import copy, json, unittest
from research import contracts

class CitationPromptContractTests(unittest.TestCase):
    def sample(self):
        stmt={'text':'原文对应论点','fact_ids':[2],'anchor_ids':['2:10:30']}
        decision={'symbol':'sh.600000','action':'watch','thesis':'逐股待核',
            'supports':[copy.deepcopy(stmt)],'counterarguments':[copy.deepcopy(stmt)],
            'relations':[{'dimensions':['F04','F07'],'claim':'关系论点','condition':'条件','counterexample':'反例','fact_ids':[2],'anchor_ids':['2:10:30']}],
            'dimensions_used':['F04','F07'],'entry':{'condition':'取得参与报价'},'invalidation':'失效条件','next_checks':['复核风险'],
            'outlook':{'upside':{**stmt,'horizon':'PM','condition':'上行条件'},'downside':{**stmt,'horizon':'PM','condition':'转弱条件'},'chase_risk':{**stmt,'watch_for':'承接变化'}},
            'rating':{**stmt,'stars':3},'counterparty':{**stmt,'lure':'追价表象','watch_for':'承接变化','counterexample':'放量站稳'}}
        report={'title':'隔离报告','summary':'等待证据','market_analysis':'冻结样本','decisions':[decision],'comparison':'同截点比较','limitations':['风险尚待核验']}
        candidate={'symbol':'sh.600000','name':'隔离样本','paths':[],'industry':'隔离行业','evidence_ids':[1,2],
            'originals':{'anchors':[{'id':'2:10:30','fact_id':2}]},'metrics':{},'dimensions':[{'id':'F13','state':'partial'},{'id':'F14','state':'partial'}]}
        return report,{'candidates':[candidate],'mode':'noon_research','stock_library_version':'isolated'}

    def slots(self,decision):
        return [(name,decision[name][0]) for name in ('supports','counterarguments','relations')]+[(f'outlook.{name}',decision['outlook'][name]) for name in ('upside','downside','chase_risk')]+[(name,decision[name]) for name in ('rating','counterparty')]

    def test_every_original_citing_statement_requires_own_anchor(self):
        report,bundle=self.sample()
        contracts.validate_report(json.dumps(report,ensure_ascii=False),bundle)
        for name,_ in self.slots(report['decisions'][0]):
            changed=copy.deepcopy(report)
            target=dict(self.slots(changed['decisions'][0]))[name];target.pop('anchor_ids')
            with self.subTest(statement=name),self.assertRaisesRegex(ValueError,'页段'):
                contracts.validate_report(json.dumps(changed,ensure_ascii=False),bundle)

    def test_all_prompt_examples_show_anchor_field_and_system_names_all_slots(self):
        main=next(line for line in contracts.SYSTEM_PROMPT.splitlines() if line.startswith('{"title":'))
        sample=json.loads(main)['decisions'][0]
        for name in ('supports','counterarguments','relations'):
            self.assertIn('anchor_ids',sample[name][0])
        text=contracts.OUTLOOK_PROMPT.split('outlook 示例 ',1)[1];outlook,_=json.JSONDecoder().raw_decode(text)
        for name in ('upside','downside','chase_risk'):self.assertIn('anchor_ids',outlook[name])
        for label in ('rating 格式 ','counterparty 格式 '):
            text=contracts.LIBRARY_PROMPT.split(label,1)[1];sample,_=json.JSONDecoder().raw_decode(text);self.assertIn('anchor_ids',sample)
        for label,_ in self.slots(self.sample()[0]['decisions'][0]):self.assertIn(label,contracts.SYSTEM_PROMPT)
        self.assertIn('逐字复制',contracts.SYSTEM_PROMPT)
        self.assertIn('不能借用另一个对象',contracts.SYSTEM_PROMPT)

if __name__=='__main__':unittest.main()
