'use strict';
// Stock board: real data only. Sections without a data source say so instead of showing made-up numbers.
const boardHorizon={PM:'当日下午',D1:'第 1 个交易日',D3:'第 3 个交易日'};
const boardRate=g=>g&&g.hit_rate!=null?Math.round(g.hit_rate*100)+'%':'—';
const boardOriginal=(perf,h)=>perf?.groups.find(g=>g.horizon===h&&g.kind==='original');
const boardShort={PM:'当日',D1:'第 1 日',D3:'第 3 日'};
function boardHitLine(perf,short){
  const hs=[...new Set(perf.groups.map(g=>g.horizon))];
  return hs.map(h=>{const g=boardOriginal(perf,h);
    return `<div><b class='num'>${boardRate(g)}</b><span>${esc((short?boardShort[h]:boardHorizon[h])||h)}</span><small>${short?'':'可评 '}${num(g?.evaluable)} / ${short?'':'到期 '}${num(g?.due)}</small></div>`;}).join('');
}
const boardPoolTotal=(type,pool)=>api(`/api/v2/stock-library?limit=1&rise_type=${encodeURIComponent(type)}&pool=${pool}`).then(r=>r.total).catch(()=>null);
function boardCategory(c,k){
  const inPool=k.buy==null&&k.watch==null?null:(k.buy||0)+(k.watch||0);
  return `<article class='bd-cat'><h3>${esc(c.name)}</h3>
    <div class='n num'>${inPool==null?'—':num(inPool)}<small>只在池</small></div>
    <p class='bd-sub'>买入池 ${num(k.buy)} · 观察池 ${num(k.watch)}</p>
    <div class='bd-days sm'>${k.perf?boardHitLine(k.perf,true):'<small>暂无命中统计</small>'}</div></article>`;
}
/* ==BOARD:market== */
function boardMarket(r){
  const head=`<div class='sec-head'><h2>大盘情况</h2><span>${r?esc(r.day)+' · '+esc(r.scope_label||'已存股票样本'):''}</span></div>`;
  if(!r)return `<section class='bd-market tile'>${head}<p class='bd-empty'>复盘数据暂未返回。</p></section>`;
  const m=r.market;
  const body=m?`<div class='bd-mk'><div><label>样本等权涨跌</label><b class='num ${recapTone(m.move)}'>${percent(m.move)}</b><small>${num(m.sample)} 只有效日线</small></div><div><label>涨跌分布</label>${recapBreadth(m)}</div><div><label>样本成交额</label><b class='num'>${m.amount!=null?num(Math.round(m.amount/1e8)):'—'}<small> 亿元</small></b></div></div>`:`<p class='bd-empty'>待收盘，或正式日线尚未取得。</p>`;
  return `<section class='bd-market tile'>${head}${body}<p class='bd-gap'>暂无数据来源：主要指数点位、北向资金、涨跌停家数。${esc(r.basis||'')}</p></section>`;
}
/* ==BOARD:sectors== */
const boardStrong=row=>row.direction==='strong'&&row.relative_direction==='stronger'&&!row.sample_limited;
function boardTile(row){
  return `<div class='bd-st ${recapTone(row.move)}'>${boardStrong(row)?`<em>相对走强</em>`:''}<b>${esc(row.name)}</b><span class='num'>${percent(row.move)}</span><small>${num(row.up)} / ${num(row.sample)} 上涨 · ${esc(recapDirections[row.direction]||'')}</small></div>`;
}
function boardSectors(r){
  const group=(title,rows)=>rows.length?`<h3>${title}</h3><div class='bd-sectors'>${rows.map(boardTile).join('')}</div>`:'';
  const body=group('行业',r?.industries||[])+group('板块',r?.boards||[]);
  return `<section class='bd-sector tile'><div class='sec-head'><h2>板块强弱</h2><span>按当日样本等权涨跌排序 · 金标为上涨较广且强于样本</span></div>${body||`<p class='bd-empty'>等待可核对的行业和板块归属。</p>`}</section>`;
}
/* ==BOARD:outlook== */
function boardOutlook(r){
  const inf=r?.inference,note=r?.research_note;
  const scenario=inf&&inf.reason?`<div class='bd-ol'><h3>下一交易日宽度情景 · ${esc(inf.title)}</h3><p>${esc(inf.reason)}</p><dl><div><dt>明早确认</dt><dd>${esc(inf.confirm)}</dd></div><div><dt>何时改判</dt><dd>${esc(inf.invalidate)}</dd></div></dl><small>${esc(inf.limitation)}</small></div>`:`<div class='bd-ol'><h3>下一交易日宽度情景</h3><p>取得所选日正式收盘和下一交易日后再给出。</p></div>`;
  const ai=note&&note.text?`<div class='bd-ol bd-ai'><h3>AI 对本轮市场的看法</h3><p>${esc(note.text)}</p><a href='${esc(researchHref('close',note.id))}'>查看收盘研究原稿 ↗</a></div>`:`<div class='bd-ol bd-ai'><h3>AI 对本轮市场的看法</h3><p>当日收盘研究尚未签发，暂无 AI 市场分析。</p></div>`;
  return `<section class='bd-outlook tile'><div class='sec-head'><h2>局势与明日预判</h2><span>${r?esc(r.day):''} → ${esc(r?.next_day||'待交易日历')}</span></div><div class='bd-ol-grid'>${scenario}${ai}</div><p class='bd-gap'>暂无数据来源：国际局势、国内政策与宏观对大盘的影响。AI 看法只引用已签发研究里的市场分析。</p></section>`;
}
/* ==BOARD:ops== */
function boardOps(s){
  const rows=s?.recent||[];
  const body=rows.length?rows.map(row=>{
    const status=slotReceipt(row);
    const detail=row.detail&&row.detail!==status?esc(row.detail):'';
    const link=row.run_id?`<a href='${esc(researchHref(row.session,row.run_id))}'>查看</a>`:'<span></span>';
    return `<div class='bd-op'><span class='num'>${esc(row.day)}</span><span>${esc(sessionLabels[row.session]||row.session)}</span><span class='bd-op-s'>${esc(status)}</span><span class='bd-op-d'>${detail||'—'}</span>${link}</div>`;
  }).join(''):`<p class='bd-empty'>近期没有时段运行记录。</p>`;
  return `<section class='bd-ops tile'><div class='sec-head'><h2>每日运行情况</h2><span>最近 ${num(rows.length)} 个研究时段</span></div>${body}</section>`;
}
async function renderBoard(ticket){
  const [policy,perf,lib,recap,research]=await Promise.all([
    api('/api/v2/methods').then(m=>m.rise_policy),
    api('/api/v2/performance'),
    api('/api/v2/stock-library?limit=1').catch(()=>null),
    api('/api/v2/recap/daily').catch(()=>null),
    api('/api/v2/research?workbench=true').catch(()=>null)]);
  const kinds=await Promise.all(policy.categories.map(async c=>{
    const [p,buy,watch]=await Promise.all([api('/api/v2/performance?rise_type='+encodeURIComponent(c.id)).catch(()=>null),
      boardPoolTotal(c.id,'buy'),boardPoolTotal(c.id,'watch')]);
    return {perf:p,buy,watch};
  }));
  if(ticket!==pageRequest)return null;
  const pools=lib?.pools||{};
  const total=(pools.buy||0)+(pools.watch||0);
  return heading('STOCK BOARD','股票看板','观察池、推荐命中和市场局势，一页看完。',`<button class='btn' data-action='refresh'>${icon('refresh')}刷新</button>`)
    +`<section class='bd-hit tile'><div class='sec-head'><h2>推荐命中率</h2><span>${esc(perf.note)}</span></div>
      <div class='bd-days'>${boardHitLine(perf)}</div>
      <p class='bd-count'>${num(perf.counts.cycles)} 个独立周期 · ${num(perf.counts.reports)} 份报告 · 另有 ${num(perf.observations)} 个观察对象，不计入正式命中率</p></section>`
    +`<section class='bd-pool tile'><div class='bd-pool-top'><b class='num'>${num(total)}</b><span>只股票在池<br>买入池 ${num(pools.buy)} · 观察池 ${num(pools.watch)}<br>各类下方三列：命中率，小字为 可评 / 到期 样本数</span></div>
      <div class='bd-cats'>${policy.categories.map((c,i)=>boardCategory(c,kinds[i])).join('')}</div></section>`
    +boardMarket(recap)+boardSectors(recap)+boardOutlook(recap)+boardOps(research?.schedule);
}
