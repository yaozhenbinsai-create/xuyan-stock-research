"""Versioned research questions and explicit, experimental discovery rules."""
from research import learning, rises, sessions

METHOD_VERSION = learning.BASE['version']
DIMENSIONS = [
 ('F01','市场环境','市场宽度、成交与相对强弱','daily'),
 ('F02','板块与产业链','行业扩散；真实业务敞口需另证','industry'),
 ('F03','政策消息与事件','主体、生效、增量和兑现条件','announcements'),
 ('F04','量能与价格','同口径相对量、价格推进和收盘位置','daily'),
 ('F05','历史活性','独立正负异动簇、沉寂与修复','daily'),
 ('F06','趋势结构与威科夫','区间、供需与相反解释；公司行动校核','daily'),
 ('F07','经营与财务','报告期、披露日、利润与现金流','financials'),
 ('F08','预期与估值','可比口径及已经定价的部分','financials'),
 ('F09','供需与成本','库存、价格、合同与传导时滞',None),
 ('F10','资金与持仓','字段定义、持续性与价量印证','fund_flows'),
 ('F11','情绪与关注','原始信息、传播去重和拥挤',None),
 ('F12','流动性与拥挤','成交容量、报价、停牌和可参与性','quotes'),
 ('F13','公司风险','监管、审计、偿债与减持等反证','announcements'),
 ('F14','执行与组合','参与条件、失效、期限与共同敞口','quotes'),
]
PATHS = [
 ('O01','新信息与预期变化','已公开公告正文中的新增事件，另核生效条件',True,['F03','F07','F08']),
 ('O02','板块扩散与同行差异','行业宽度与个股相对表现；不代表真实业务受益',True,['F01','F02','F04']),
 ('O03','量能与趋势确认','20日区间、相对成交量与收盘位置',True,['F04','F06','F12']),
 ('O04','活性重新启动','正负独立异动簇与沉寂；新催化仍须补证',True,['F03','F04','F05']),
 ('O05','趋势回撤修复','均线位置、回撤缩量及当前需求',True,['F04','F06','F13']),
 ('O06','经营或业绩拐点','同季营收、利润和经营现金流交叉召回',True,['F07','F08','F13']),
 ('O07','供需或成本改善','成本供需公告线索与同期经营数据交叉，传导仍须验证',True,['F02','F07','F09']),
 ('O08','风险变化与估值修复','风险解除公告与同截点可比同行估值，另核生效与新风险',True,['F08','F13','F14']),
]

def catalog():
    method = learning.active()
    auto = sessions.config()['enabled']
    return {'version':method['version'],'status':'experimental','label':'探索性候选发现，尚未证明投资有效',
      'principle':'路径产生线索，维度提供证据，AI综合支持与反证，签发校验决定行动；不做十四维固定等权打分。',
      'dimensions':[{'id':i,'name':n,'question':q,'dataset':ds} for i,n,q,ds in DIMENSIONS],
      'paths':[{'id':i,'name':n,'description':d,'implemented':ready,'dimensions':dims} for i,n,d,ready,dims in PATHS],
      'rise_policy':rises.catalog(method),
      'rules':{'minimum_bars':60,'history_bars':65,'candidate_limit':8,'breakout_lookback':20,'relative_volume_min':method['relative_volume_min'],
        'close_location_min':method['close_location_min'],'sector_breadth_min':method['sector_breadth_min'],
        'reactivation_return_min':0.03,'independent_cluster_gap_sessions':3,'sector_min_members':5},
      'evaluation':{'horizons':['D1','D3'],'entry':'签发后首个经核价的参考报价；不推断真实成交',
        'endpoint':'原市场日后第1和第3个交易日，不随触发或去留顺延','benchmark':'未接入同窗基准时不报超额',
        'price_basis':'仅可比口径可评，缺公司行动或参考价时明确不可评'},
      'automation':{'enabled':bool(auto),'sessions':auto},
      'growth':{'status':'collecting_evidence','label':'积累真实原判与复核样本','automatic_promotion':False}}
