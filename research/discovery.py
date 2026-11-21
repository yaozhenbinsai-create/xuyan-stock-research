"""Bounded streaming discovery from actual stored facts; no scoring masquerading as AI."""
from __future__ import annotations
from collections import defaultdict, deque
from datetime import datetime, timedelta
import hashlib
import json
import statistics
import threading
import time
from fastapi import HTTPException
from foundation import database as store, record_store
from foundation import directory as security_directory
from research import coverage, evidence, learning, market, methods, originals, outlook, rises, screening, sessions, structure as wyckoff
from research import history_fill

_cache = {}
_lock = threading.Lock()

def pct(a,b):
    return (a/b-1) if a is not None and b and b>0 else None

def mean(values):
    values=[v for v in values if v is not None]
    return statistics.fmean(values) if values else None

def describe_bars(bars, *, volume_refs=None, relative_volume_min=1.3, close_location_min=.65, classify=False):
    """All windows end at the visible final bar; cluster directions stay separate."""
    valid=[b for b in bars if b.get('close') and not b.get('suspended')]
    if len(valid)<60:return None
    b=valid[-1];prior=valid[-21:-1]
    if any(not x.get('high') or not x.get('low') for x in valid[-21:]):return None
    returns=[pct(x['close'],x.get('preclose')) for x in valid]
    # An unexplained raw-price discontinuity invalidates multi-day comparability.
    gaps=[x['day'] for p,x in zip(valid,valid[1:]) if x.get('preclose') and abs(x['preclose']/p['close']-1)>.001]
    pos,neg=[],[]
    for i,r in enumerate(returns):
        if r is None or abs(r)<.04:continue
        clusters=pos if r>0 else neg
        if not clusters or i-clusters[-1]['last']>3:clusters.append({'first':i,'last':i,'day':valid[i]['day']})
        else:clusters[-1]['last']=i
    intraday = bool(b.get('partial_bar'))
    volume_refs = volume_refs or []
    if intraday:
        prior_volume = mean([v for v, _ in volume_refs]) if len(volume_refs) >= 3 and b.get('volume_unit') == 'shares' else None
    else:
        prior_volume=mean([x.get('volume') for x in prior])
    volume_ratio=b.get('volume')/prior_volume if b.get('volume') is not None and prior_volume else None
    high=max(x['high'] for x in prior);low=min(x['low'] for x in prior)
    spread=b['high']-b['low']
    result={'day':b['day'],'close':b['close'],'return_1':returns[-1],
      'return_5':pct(b['close'],valid[-6]['close']),'return_20':pct(b['close'],valid[-21]['close']),
      'ma5':mean([x['close'] for x in valid[-5:]]),'ma20':mean([x['close'] for x in valid[-20:]]),
      'ma60':mean([x['close'] for x in valid[-60:]]),'prior_high20':high,'prior_low20':low,
      'relative_volume20':volume_ratio,'close_location':(b['close']-b['low'])/spread if spread>0 else None,
      'amount':b.get('amount'),'turnover':b.get('turnover'),'positive_clusters':len(pos),'negative_clusters':len(neg),
      'last_positive_cluster':pos[-1]['day'] if pos else None,'raw_discontinuities':gaps,
      'comparable':not gaps,'action_adjustment_verified':False,'bars':len(valid),'partial_bar':intraday,
      'pe':evidence.numeric(b.get('pe')),'pb':evidence.numeric(b.get('pb')),
      'quote_time':b.get('source_time'),'volume_samples':len(volume_refs) if intraday else len(prior),
      'volume_basis':'同刻累计快照' if intraday else '完整交易日均量'}
    paths=[]
    if not gaps and volume_ratio is not None:
        if b['close']>high and volume_ratio>=relative_volume_min and result['close_location'] is not None and result['close_location']>=close_location_min:paths.append('O03')
        prior_positive=[x for x in pos if x['last']<len(valid)-5]
        quiet=all(r is not None and abs(r)<.03 for r in returns[-6:-1])
        if result['return_1'] is not None and result['return_1']>=.03 and prior_positive and quiet and volume_ratio>=1.3:paths.append('O04')
    pullback_volume = mean([x.get('volume') for x in valid[-4:-1]])
    completed_mean = mean([x.get('volume') for x in prior])
    if not gaps and pullback_volume is not None and completed_mean and pullback_volume < completed_mean:
        if b['close'] > result['ma20'] > result['ma60'] and valid[-2]['close'] <= mean([x['close'] for x in valid[-6:-1]]) and (result['return_1'] or 0) > 0:
            paths.append('O05')
    clusters=[]
    if not gaps:
        for direction,group in (('up',pos),('down',neg)):
            for cluster in group[-3:]:
                start,end=cluster['first'],cluster['last']
                terminal=valid[end+3] if end+3<len(valid) and not valid[end+3].get('partial_bar') else None
                indices = [start,end]
                if terminal:
                    indices.append(end+3)
                clusters.append({'direction':direction,'start':valid[start]['day'],'end':valid[end]['day'],
                    'move':pct(valid[end]['close'],valid[start].get('preclose')),
                    'follow_through_3':pct(terminal['close'],valid[end]['close']) if terminal else None,
                    'fact_ids':[valid[i]['_id'] for i in dict.fromkeys(indices) if '_id' in valid[i]]})
    result['activity_clusters'] = sorted(clusters,key=lambda row:row['start'])
    result['paths']=paths
    if classify:
        rise = rises.classify(valid,result)
        if rise:
            if rise['type']=='breakout' and intraday:
                rise['facts']=list(dict.fromkeys(rise['facts']+[fid for _,fid in volume_refs]))
            result['rise']=rise
    return result


def _latest(c,dataset,day):
    return c.execute('SELECT max(day) FROM heads WHERE dataset=%s AND day<=%s',(dataset,day)).fetchone()[0]

def _map(c,dataset,day):
    if not day:return {}
    rows=c.execute('SELECT h.symbol,f.id FROM heads h JOIN facts f ON f.id=h.fact_id WHERE h.dataset=%s AND h.day=%s ORDER BY h.symbol LIMIT 10001',(dataset,day)).fetchall()
    if len(rows)>10000:raise HTTPException(409,'证券基础资料超出本批处理边界，未静默截断')
    return {r['symbol']:{'fact_id':r['id'],**r['payload']} for r in record_store.hydrate(c,rows)}


_HISTORY_BATCH_SIZE = 5000


def _history_rows(c, history_symbols, first_day, last_day, *, pointed):
    """Read each typed daily bar once, in the existing bounded cursor order."""
    scope = ','.join(['%s']*len(history_symbols))
    index = 'head_symbol' if pointed else 'PRIMARY'
    ordering = 'h.symbol,h.day,h.record_key' if pointed else 'h.day,h.symbol,h.record_key'
    positions = ('symbol','day','record_key') if pointed else ('day','symbol','record_key')
    after = None
    while True:
        continuation = '';continuation_values = []
        if after is not None:
            terms = []
            for offset,position in enumerate(positions):
                terms.append('('+' AND '.join([f'h.{key}=%s' for key in positions[:offset]]+
                                             [f'h.{position}>%s'])+')')
                continuation_values.extend(after[:offset+1])
            continuation = ' AND ('+' OR '.join(terms)+')'
        batch = c.execute(
            f"SELECT h.symbol,h.day,h.record_key,f.id,f.first_seen,d.* FROM heads h FORCE INDEX ({index}) "
            "JOIN facts f ON f.id=h.fact_id LEFT JOIN daily_bars d ON d.fact_id=f.id "
            f"WHERE h.dataset='daily' AND h.day BETWEEN %s AND %s AND h.symbol IN ({scope})"
            + continuation + f" ORDER BY {ordering} LIMIT {_HISTORY_BATCH_SIZE}",
            (first_day,last_day,*history_symbols,*continuation_values),
        ).fetchall()
        if not batch:
            break
        for row in batch:
            if row['fact_id'] is None:
                raise RuntimeError(f"Typed daily record is missing for fact {row['id']}")
            payload = {'day':row['day'],'symbol':row['symbol'],'key':row['record_key'],
                       **record_store._decode('daily_bars',dict(row))}
            yield {'symbol':row['symbol'],'id':row['id'],'first_seen':row['first_seen'],'payload':payload}
        after = tuple(batch[-1][key] for key in positions)
        if len(batch) < _HISTORY_BATCH_SIZE:
            break


def snapshot(market_day=None, symbols=None, *, slot=None, profile=None, method=None, review_symbols=(), prepared_symbols=(),
             rise_version=rises.VERSION, rise_type=None, market_context=False, outlook_version=None):
    """Current-knowledge retrospective inputs are never represented as historical forecasts."""
    if rise_version not in (None,rises.VERSION) or rise_type and (not rise_version or rise_type not in rises.TYPES):
        raise HTTPException(422,'上涨分类或规则版本不可用')
    if outlook_version not in (None,outlook.VERSION):
        raise HTTPException(422,'涨跌情景规则版本不可用')
    with _lock:
        with store.db(readonly=True,isolation='REPEATABLE READ',timeout=120) as c:
            profile = profile or {}
            method = {**learning.BASE,**(method or {})}
            latest=_latest(c,'daily',market_day or store.today())
            if not latest:raise HTTPException(409,'尚无正式日线，不能生成研究')
            slot = slot or sessions.window('close', market_day or latest, c)
            day = slot['market_day']
            intraday = slot['session'] != 'close'
            if not intraday and latest != day:
                raise HTTPException(409, f'{day} 的正式日线尚未就绪')
            history_end = (datetime.fromisoformat(day).date() - timedelta(days=1)).isoformat() if intraday else day
            knowledge = store.now()
            basics_day,directory=security_directory.market_roster(c,day,known_at=knowledge)
            # A same-value universe observation can add an origin without
            # changing the head generation. Its eligibility still changes.
            directory_key=(basics_day,tuple(sorted(v['fact_id'] for v in directory.values())))
            generation=[store.setting('generation_'+ds,0,conn=c) for ds in ('daily','basics','industry','quotes','policies','policy_terms',*evidence.DOMAINS)]
            profile_key = {k:v for k,v in profile.items() if k!='note'}
            key=(store.description()['name'],day,store.dumps(slot),tuple(generation),tuple(symbols or []),tuple(review_symbols),
                 store.dumps(profile_key),store.dumps(method),rise_version,rise_type,market_context,outlook_version,tuple(prepared_symbols),
                 store.setting('generation_research_check',0,conn=c),directory_key)
            cached=_cache.get(key)
            if cached and time.monotonic()-cached[0]<120:return json.loads(store.dumps(cached[1]))
            days=[r[0] for r in c.execute("SELECT day FROM heads WHERE dataset='daily' AND day<=%s GROUP BY day ORDER BY day DESC LIMIT 65",(history_end,))]
            if not days:raise HTTPException(409,'缺少截点之前的正式日线')
            if intraday:
                previous = sessions.previous_trading_day(c, day)
                if not previous or days[0] != previous:
                    raise HTTPException(409,'盘中研究缺少上一交易日的正式日线或已确认的交易日历')
            industry_day=_latest(c,'industry',day)
            industries=_map(c,'industry',industry_day)
            quotes = market.latest_quotes(c, day, slot['market_data_cutoff'], knowledge, slot['quote_age_seconds'],
                                         None if market_context else symbols)
            if intraday and not quotes:
                raise HTTPException(409, f"{slot['label']}缺少截点前的有效快照，截止 {slot['market_data_cutoff'][11:16]}")
            quote_scope = {s:quotes[s] for s in symbols if s in quotes} if symbols and market_context else quotes
            volume_refs = market.comparable_volume(c, quote_scope, days, slot['market_data_cutoff'], knowledge, slot['quote_age_seconds']) if intraday else {}
            current_bars = _map(c,'daily',days[0])
            active_directory = {s for s, info in directory.items() if info.get('status') == '1'}
            eligible_symbols = {
                s for s, bar in current_bars.items() if s in active_directory and not bar.get('is_st') and not bar.get('suspended')
                and (not intraday or s in quotes and (quotes[s].get('volume') or 0)>0)
            }
            history_symbols = sorted(eligible_symbols.intersection(symbols) if symbols else eligible_symbols)
            history=defaultdict(lambda:deque(maxlen=65));n=0
            if history_symbols:
                for row in _history_rows(c,history_symbols,days[-1],history_end,pointed=bool(symbols)):
                    n+=1
                    if n>450000:raise HTTPException(409,'本次事实窗口超过45万条处理上限，未静默缩小范围')
                    b=row['payload'];b.update(_id=row['id'],_known_at=row['first_seen'])
                    history[row['symbol']].append(b)
            history_recovery=history_fill.expand(c,history,history_end,knowledge)
            eligible=history
            if intraday:
                for s, bars in eligible.items():
                    bars.append(quotes[s])
            all_metrics={s:describe_bars(bs,volume_refs=volume_refs.get(s),relative_volume_min=method['relative_volume_min'],
                                         close_location_min=method['close_location_min'],classify=bool(rise_version)) for s,bs in eligible.items()}
            all_metrics={s:m for s,m in all_metrics.items() if m}
            for symbol,details in history_recovery['symbols'].items():
                if symbol in all_metrics:all_metrics[symbol]['history_sample']=details
            documents, evidence_limits = evidence.collect(c, day, knowledge, intraday, symbols, historical_cutoff=slot['market_data_cutoff'] if slot['retrospective'] else None)
            if prepared_symbols:
                focused, _ = evidence.collect(c,day,knowledge,intraday,prepared_symbols)
                documents.update(focused)
            sectors=defaultdict(list)
            for symbol in eligible_symbols:
                bar = quotes[symbol] if intraday else current_bars[symbol]
                group=industries.get(symbol,{}).get('group')
                if group and not industries[symbol].get('unclassified'):
                    sectors[group].append({'return_1':pct(bar.get('close'),bar.get('preclose')),'pe':evidence.numeric(bar.get('pe'))})
            sector_metrics={g:{'members':len(ms),'advancing':sum((m['return_1'] or 0)>0 for m in ms),'mean_return':mean([m['return_1'] for m in ms])} for g,ms in sectors.items()}
            peers=defaultdict(list)
            for symbol in sorted(eligible_symbols):
                industry=industries.get(symbol,{})
                if industry.get('group') and not industry.get('unclassified'):
                    peers[industry['group']].append(symbol)
            valuations = {}
            for group, ms in sectors.items():
                values = [m['pe'] for m in ms if m['pe'] is not None and m['pe'] > 0]
                if len(values) >= 5:
                    valuations[group] = {'median_pe': statistics.median(values), 'members': len(values), 'basis': '同一截点已存正PE同行'}
            assessments = {}
            for symbol,m in all_metrics.items():
                group=industries.get(symbol,{}).get('group');sector=sector_metrics.get(group,{})
                if learning.experiment_signal({'metrics':m,'sector':sector},method,'sector_breadth_min'):m['paths'].append('O02')
                assessment = evidence.assess(documents.get(symbol, {}), m, valuations.get(group))
                m['paths'].extend(assessment['triggers'])
                assessments[symbol] = assessment
            path_counts={p[0]:sum(p[0] in m['paths'] for m in all_metrics.values()) for p in methods.PATHS}
            excluded = set(profile.get('excluded_symbols', []))
            boards = set(profile.get('boards', []))
            allowed = {s for s in all_metrics if s not in excluded and (not boards or directory[s].get('board') in boards)}
            available=[(s,m) for s,m in all_metrics.items() if (m['paths'] or m.get('rise')) and s in allowed
                       and (not rise_type or m.get('rise',{}).get('type')==rise_type)]

            def _candidate_score(symbol, m):
                """Lower score = higher priority. Penalise risk flags and exhaustion."""
                risk_flags = (m.get('rise') or {}).get('risk_flags') or []
                # Each risk flag costs 1 rank position worth of penalty
                risk_penalty = len(risk_flags)
                # Stocks that have been rising >=4 consecutive days without a down day
                # are likely near mean-reversion; count from the negative_clusters gap
                consecutive_up = 0
                pos_clusters = m.get('positive_clusters', 0)
                neg_clusters = m.get('negative_clusters', 0)
                last_pos = m.get('last_positive_cluster')
                if last_pos and m.get('comparable'):
                    # If positive cluster is recent and there were few negatives lately, penalise
                    if pos_clusters > 0 and neg_clusters == 0:
                        consecutive_up = 2   # steep penalty: no negative cluster in 20 days
                    elif pos_clusters > neg_clusters + 1:
                        consecutive_up = 1
                # O03 breakout and O05 pullback are more reliable than O02 sector breadth alone
                path_bonus = -int('O03' in m['paths']) - int('O05' in m['paths'])
                # Prefer larger float / more liquid; use amount as tiebreaker
                amount_rank = -(m.get('amount') or 0)
                return (risk_penalty + consecutive_up, path_bonus, amount_rank, symbol)

            # Lexicographic recall priority: quality-adjusted, never a probability score.
            available.sort(key=lambda x: _candidate_score(x[0], x[1]))
            if symbols:
                available=[(s,all_metrics[s]) for s in symbols if s in allowed]
            else:
                # Reserve the first place from each available discovery path, then fill in priority order.
                distinct = {s: all_metrics[s] for s in review_symbols if s in all_metrics and not rise_type}
                if rise_version:
                    for s,m in rises.balanced(available):
                        if len(distinct)>=profile.get('candidate_limit',8):
                            break
                        distinct.setdefault(s,m)
                for path, *_ in methods.PATHS:
                    candidate = next(((s, m) for s, m in available if path in m['paths'] and s not in distinct), None)
                    if candidate:
                        distinct[candidate[0]] = candidate[1]
                for s, m in available:
                    distinct.setdefault(s, m)
            selected=(available if symbols else list(distinct.items()))[:profile.get('candidate_limit', 8)]
            # Sector saturation guard: at most 2 candidates from the same industry group,
            # dropping the lowest-ranked excess. This prevents recommending an entire
            # sector that is already chasing — when one stock breaks out, peers often
            # follow mechanically without independent catalysts.
            if not symbols and len(selected) > 2:
                industry_counts = defaultdict(int)
                filtered = []
                for symbol, m in selected:
                    group = industries.get(symbol, {}).get('group')
                    if group and industry_counts[group] >= 2:
                        continue
                    industry_counts[group] += 1
                    filtered.append((symbol, m))
                # If we dropped any, backfill from the remaining pool to keep the limit
                if len(filtered) < len(selected) and len(filtered) < profile.get('candidate_limit', 8):
                    in_set = {s for s, _ in filtered}
                    for symbol, m in available:
                        if len(filtered) >= profile.get('candidate_limit', 8):
                            break
                        if symbol in in_set:
                            continue
                        group = industries.get(symbol, {}).get('group')
                        if group and industry_counts[group] >= 2:
                            continue
                        industry_counts[group] += 1
                        filtered.append((symbol, m))
                selected = filtered
            screening_rows=screening.snapshot_rows(available,selected,directory)
            comparisons=[]
            if not symbols:
                chosen={symbol for symbol,_ in selected}
                scope_seed=f"{day}/{slot['session']}/{method['version']}"
                remaining=sorted(allowed-chosen,key=lambda symbol:hashlib.sha256(f'{scope_seed}/{symbol}'.encode()).hexdigest())
                hits={s for s,_ in available}
                for cohort,has_path in (('unreviewed',True),('outside',False)):
                    group=[symbol for symbol in remaining if (symbol in hits)==has_path][:2]
                    comparisons.extend({'symbol':symbol,'cohort':cohort,**learning.observation_features({
                        'metrics':all_metrics[symbol],'sector':sector_metrics.get(industries.get(symbol,{}).get('group'),{})})} for symbol in group)
            scope = symbols or active_directory
            scope_summary, scope_pack = coverage.freeze(scope,directory,current_bars,quotes,all_metrics,allowed,
                                                         intraday,documents,evidence_limits)
            scope_size = len(scope)
            covered = sum(s in all_metrics for s in symbols) if symbols else len(all_metrics)
            market_summary={'market_day':day,'directory_day':basics_day,'industry_day':industry_day,
              'directory_active':len(active_directory),'daily_symbols':len(current_bars),
              'eligible':len(eligible),'with_60_bars':len(all_metrics),'discovered':len(available),'displayed':len(selected),
              'advancing':sum((m['return_1'] or 0)>0 for m in all_metrics.values()),'declining':sum((m['return_1'] or 0)<0 for m in all_metrics.values()),
              'mean_return':mean([m['return_1'] for m in all_metrics.values()]),'path_counts':path_counts,
              'exclusions':{'inactive_st_suspended_or_no_current_bar':len(current_bars)-len(eligible_symbols),'insufficient_or_invalid_history':len(eligible)-len(all_metrics)},
              'session':slot['session'],'quote_count':len(quotes),'scope_size':scope_size,'covered':covered,
              'coverage':covered / scope_size if scope_size else None,'participating':sum(s in allowed for s in scope),'evidence_scan_limits':evidence_limits,
              'coverage_detail':scope_summary,
              'scope':('同截点快照及其前置历史日线' if intraday else '同日正式日线及其前置历史')+'；缺资料的证券单列',
              'ordering':'按不同机会路径保留线索，再按区间突破、趋势修复和成交额补充'}
            if market_context:
                source = quotes if intraday else current_bars
                moves = [pct(source[s].get('close'),source[s].get('preclose')) for s in sorted(eligible_symbols)]
                moves = [value for value in moves if value is not None]
                market_summary['breadth_context'] = {
                    'basis':'同截点有有效涨跌数据的在市样本','sample':len(moves),
                    'advancing':sum(value>0 for value in moves),'declining':sum(value<0 for value in moves),
                    'mean_return':mean(moves),
                } if moves else {}
            if rise_version:
                market_summary['rise_counts'] = {key:0 for key in rises.TYPES}
                market_summary['rise_universe'] = len(allowed)
                for symbol in allowed:
                    if signal := all_metrics[symbol].get('rise'):
                        market_summary['rise_counts'][signal['type']] += 1
                market_summary['ordering'] = '先按四类上涨线索轮流安排研究，每类比较自身证据，余位再按发现路径补充'
                market_summary['rise_scope'] = rise_type
            candidates=[]
            publication=slot['market_data_cutoff'] if slot['retrospective'] else knowledge
            policies=originals.policies(c,knowledge,published_through=publication[:10],limit=12)['items'] if selected else []
            calendar_start=min([days[-1]]+[history[symbol][0]['day'] for symbol,_ in selected if history[symbol]])
            calendar = outlook.trading_dates(c,calendar_start,days[0]) if selected and outlook_version else []
            for symbol,m in selected:
                bs=list(history[symbol]);info=directory[symbol];ind=industries.get(symbol,{})
                checks = evidence.read_checks(c, symbol, knowledge)
                docs = evidence.compact(documents.get(symbol, {}), assessments[symbol], checks)
                original_packet=originals.packet(c,symbol,knowledge,publication,policies)
                if 'O04' in m['paths']:
                    m['reactivation_catalyst_ids']=assessments[symbol]['triggers'].get('O01',[])
                refs=[b['_id'] for b in bs]
                other_refs = [row['id'] for row in docs] + [fid for _, fid in volume_refs.get(symbol, [])]
                quote = quotes.get(symbol)
                if quote:
                    other_refs.append(quote['_id'])
                required_checks = [fid for check in checks.values() for fid in check['fact_ids']]
                source_ids=[anchor['fact_id'] for anchor in original_packet['anchors']]
                m['action_adjustment_verified'] = m['comparable'] and checks.get('actions', {}).get('state') == 'verified'
                structure_basis = market.structure_basis(c, symbol, bs[0]['day'], day, knowledge, checks.get('actions', {}))
                m['structure'] = wyckoff.analyze(bs, partial_ratio=m['relative_volume20'], **structure_basis)
                dimensions=dimension_states(m,ind,docs,checks,profile,quote,close_anchor=not intraday and bool(bs[-1].get('close')))
                for dimension in dimensions:
                    if dimension['id']=='F07' and original_packet['financial_fields']:
                        dimension.update(state='partial',reason='已有财报页级数字与同期间交叉核对，差异和口径缺口单列')
                    if dimension['id'] in ('F02','F03','F09') and original_packet['industry_links']:
                        dimension.update(state='partial',reason='政策条文与公司业务披露存在可引用交集，适用和经济影响待论证')
                cid={'symbol':symbol,'name':info.get('name',symbol),'board':info.get('board'),'industry':ind.get('group'),'metrics':m,
                  'paths':m['paths'],'industry_fact_id':ind.get('fact_id'),
                  'evidence_ids':list(dict.fromkeys(refs+[info['fact_id']]+([ind['fact_id']] if ind else [])+other_refs+required_checks+source_ids+structure_basis.get('action_ids',[]))),
                  'bar_ids':refs,'documents':docs,'checks':checks,'research_evidence':assessments[symbol],
                  'structure_basis':structure_basis,
                  'originals':original_packet,'benchmark_symbols':[s for s in peers[ind.get('group')] if s!=symbol][:3],
                  'reference_quote': {'id':quote['_id'],'at':quote['source_time'],'price':quote['close']} if quote else None,
                  'bars':[{'day':b['day'],'open':b.get('open'),'high':b.get('high'),'low':b.get('low'),'close':b.get('close'),'volume':b.get('volume'),'id':b['_id'],'partial_bar':b.get('partial_bar',False)} for b in bs[-60:]],
                  'sector':sector_metrics.get(ind.get('group'),{}),'dimensions':dimensions,
                  'counterpoints':['未核对近期公司行动，原始价格跨日形态仅作线索','缺少完整公司风险核验，不能据此直接签发买入建议'],
                  'next_checks':['取得近期公告原件与风险核验','补齐财务、现金流与真实业务敞口','参与前取得有效报价并复核原条件']}
                if outlook_version:
                    cid['outlook_reference']=outlook.reference(bs,calendar,m,structure_basis.get('action_days',()),target=slot.get('target'))
                candidates.append(cid)
        input_parts = [market_summary,[(c['symbol'],c['evidence_ids'],c['metrics']) for c in candidates]]
        if outlook_version:
            input_parts.extend((outlook_version,[c['outlook_reference'] for c in candidates]))
        result={'market':market_summary,'candidates':candidates,'scope_pack':scope_pack,'screening_rows':screening_rows,
          'comparison_samples':comparisons,'knowledge_cutoff':knowledge,'market_data_cutoff':slot['market_data_cutoff'],
          'method_version':method['version'],'mode':'retrospective' if slot['retrospective'] else slot['session']+'_research','session_policy':slot,
          'input_hash':hashlib.sha256(store.dumps(input_parts).encode()).hexdigest(),
          'note':'机会发现只产生待研究对象；财务、事件、公司风险和参与核价不足时，正式入选会被阻止。'}
        if rise_version:
            result['rise_policy']=rises.catalog(method)
            result['rise_preview']={symbol:metrics['rise'] for symbol,metrics in available if metrics.get('rise')}
        if key not in _cache and len(_cache)>=4:
            _cache.pop(next(iter(_cache)))
        _cache[key]=(time.monotonic(),result)
        return json.loads(store.dumps(result))

def dimension_states(metrics,industry,documents=None,checks=None,profile=None,quote=None,*,close_anchor=False):
    documents = documents or []
    checks = checks or {}
    profile = profile or {}
    domains = {r['dataset'] for r in documents}
    has_text = any(r.get('excerpt') for r in documents)
    ready={'F01','F04','F05'}
    partial={'F02','F06','F12','F14'}
    rows=[]
    for i,name,question,dataset in methods.DIMENSIONS:
        state='available' if i in ready else 'partial' if i in partial else 'missing'
        reason={'F01':'仅已存日线样本的市场宽度，非指数基准',
          'F02':'有历史行业标签与广度；业务及产业链敞口未核验',
          'F04':'以完整日线比较前20日成交量和收盘位置',
          'F05':'正负异动按相邻3个交易日归为独立簇，不等同催化事件',
          'F06':'仅原始价量形态；公司行动与威科夫阶段尚未确认',
          'F12':'有日成交额，尚无参与时段报价或盘口',
          'F14':'缺个人组合约束、合格核价与正式触发条件'}.get(i,'尚无足够且经核验的本轮证据')
        if i=='F02' and not industry:state='missing';reason='当前截点缺行业归属'
        if i=='F06':
            study = metrics.get('structure', {})
            if study.get('state') == 'incomparable':
                state='missing';reason='最近可比价量不足，等跨日断点后重新累计'
            elif study.get('gate_eligible'):
                state='available';reason='已核公司行动并逐柱评估结构，支持、反证及失效可回放'
            else:
                reason='结构仅为假设，需完成公司行动覆盖或等待完整日线确认'
        if i=='F04' and metrics.get('partial_bar'):
            state='available' if metrics['relative_volume20'] is not None else 'partial'
            reason=f"盘中量能以 {metrics['volume_samples']} 个同刻历史快照比较；不足三个时量能保持未知"
        if i=='F04' and any(metrics.get(key) is None for key in ('return_1','relative_volume20','close_location')):
            state='partial'
            if not metrics.get('partial_bar'):
                reason='已读取正式日线，涨跌、相对量或区间位置的可比字段有缺口'
        if i=='F05' and not metrics.get('comparable'):
            state='missing';reason='原始价格存在跨日断点，暂停比较正负异动簇'
        if i in ('F03','F11','F13') and 'announcements' in domains:
            state='partial';reason='有同截点公告' + ('正文摘录，可核原文与独立事件' if has_text else '索引，正文待核')
        if i=='F07' and 'financials' in domains:state='partial';reason='按实际披露时点读取财务，列出同季利润、营收与经营现金流线索'
        if i=='F08' and metrics.get('pe'):state='partial';reason='有同截点估值字段，预期历史仍待核'
        if i=='F09' and has_text:state='partial';reason='已纳入公告节选，按原文核对价格、库存、成本和公司敞口'
        if i=='F10' and 'fund_flows' in domains:state='partial';reason='有历史资金流，保留来源定义和时间精度'
        if i=='F12' and quote:state='partial';reason='有截点前参考报价与成交量，参与时需要重新核价'
        if i=='F13' and checks.get('risk',{}).get('state')=='verified':state='verified';reason='已登记有效期内的公告风险核验'
        if i=='F14' and quote and (quote.get('volume') or 0)>0 and profile.get('confirmed_at'):
            state='verified';reason='已冻结参考报价与用户确认的参与范围，触发时按原窗口重新核价'
        elif i=='F14' and close_anchor and profile.get('confirmed_at'):
            state='conditional';reason='正式收盘价可作条件研究锚点，次日参与需要独立有效核价'
        rows.append({'id':i,'name':name,'state':state,'question':question,'reason':reason})
    return rows
