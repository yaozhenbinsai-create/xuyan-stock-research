'use strict';

const dimensionGroups = [
  ['市场与交易',['F01','F02','F04','F05','F06']],
  ['事件与公司',['F03','F07','F08','F09']],
  ['资金与交易约束',['F10','F11','F12','F13','F14']],
];
const combinationStates = {compared:'已交叉核对',partial:'条件待核',missing:'资料不足',conflict:'存在证据差异'};
const transmissionNames = {benefit:'可能受益',supply:'供需传导',cost_pressure:'成本压力',
  dependency:'业务依赖',conflict:'证据冲突',counterevidence:'反证'};

function dimensionCounts(items) {
  return {supported:items.filter(x=>['available','verified'].includes(x.state)).length,
    partial:items.filter(x=>['partial','conditional'].includes(x.state)).length,
    missing:items.filter(x=>x.state==='missing').length};
}

function dimensionCountText(counts) {
  return `${counts.supported} 项可核 · ${counts.partial} 项部分有据 · ${counts.missing} 项待补`;
}

function formatDimensionReading({value,unit}) {
  if(unit==='text')return esc(value);
  if(unit==='ratio')return esc((Number(value)*100).toFixed(2)+'%');
  if(unit==='pp')return esc((value>0?'+':'')+(Number(value)*100).toFixed(2)+' 个百分点');
  if(unit==='times')return esc(Number(value).toFixed(2)+' 倍');
  if(unit==='price')return '¥'+risePrice(value);
  if(unit==='stocks')return num(value)+' 只';
  if(unit==='items')return num(value)+' 项';
  if(unit==='cny'){
    const size=Math.abs(value);
    if(size>=1e8)return num(Math.round(value/1e6)/100)+' 亿元';
    if(size>=1e4)return num(Math.round(value/100)/100)+' 万元';
    return num(value)+' 元';
  }
  return num(value);
}

function aiDimensionRelations(decision, dimensions) {
  const byId=new Map(dimensions.map(item=>[item.id,item]));
  return (decision.relations||[]).map(relation=>{
    const linked=relation.dimensions.map(id=>byId.get(id)).filter(Boolean);
    const gaps=linked.filter(row=>!['available','verified'].includes(row.state));
    const edge=relation.edge;
    return `<article class="relation-card"><b>${linked.map(row=>esc(row.name)).join(' × ')}</b>
      <p>${esc(relation.claim)}</p>
      ${edge?`<small>${esc(transmissionNames[edge.kind])} · ${esc(edge.subject)} → ${esc(edge.target)}
        ${edge.lag_sessions==null?'':` · ${edge.lag_sessions} 个交易日`}
        ${edge.valid_through?` · 截至 ${esc(edge.valid_through)}`:''}</small>`:''}
      <dl><dt>成立条件</dt><dd>${esc(relation.condition)}</dd>
        <dt>反例</dt><dd>${esc(relation.counterexample)}</dd></dl>
      ${gaps.length?`<small>组合中仍待补证 ${gaps.map(row=>esc(row.name)).join('、')}</small>`:''}
      <details><summary>核对原稿引用</summary>${evidenceButtons(relation.fact_ids)}</details>
    </article>`;
  }).join('');
}

function multidimEvidence(analysis, decision, basis) {
  const {items,coverage,combinations}=analysis;
  const byId=new Map(items.map(row=>[row.id,row]));
  const used=new Set(decision?.dimensions_used||[]);
  const metrics=Object.entries({supported:'可核',partial:'部分有据',missing:'待补证'});
  const factsSection=dimensionGroups.map(([title,codes])=>`<section class="dimension-cluster"><h4>${title}</h4>
    ${codes.map(id=>{
      const row=byId.get(id);
      const readings=row.readings.map(item=>`<div><dt>${esc(item.label)}</dt><dd>${formatDimensionReading(item)}</dd></div>`).join('');
      return `<details class="dimension-row" data-state="${esc(row.state)}" data-used="${used.has(id)}">
        <summary><span><strong>${esc(row.name)}</strong>${used.has(id)?'<small>原稿列入核查</small>':''}</span>
          ${chip(dimNames[row.state]||row.state,row.state==='missing'?'amber':'gray')}</summary>
        <p>${esc(dimensionReason(row))}</p>
        ${readings?`<dl class="dimension-readings">${readings}</dl>`:''}
        <p class="dimension-note">${esc(row.note)}</p>
        ${row.documents.map(doc=>`<button type="button" class="btn small" data-action="record" data-id="${doc.id}">
          ${esc(doc.day)} ${esc(doc.title)} ↗</button>`).join('')}
        ${row.facts.length?evidenceButtons(row.facts):''}
      </details>`;
    }).join('')}</section>`).join('');
  return `<section class="multidim-deck" aria-label="逐股多维分析">
    <header class="dimension-hero"><div><div class="eyebrow">MULTI-FACTOR REVIEW</div>
      <h3>十四维证据</h3><p>${esc(basis)}</p></div>
      <div class="dimension-metrics">${metrics.map(([key,name])=>`
        <div><strong>${num(coverage[key])}</strong><span>${name}</span></div>`).join('')}</div></header>
    ${decision?`<section class="dimension-cross"><div class="section-heading"><div><h3>AI 组合论证</h3>
      <p>原稿列入核查 ${used.size} 项。每条论证标出它实际连接的维度。</p></div></div>
      ${aiDimensionRelations(decision,items)}</section>`:''}
    <section class="dimension-cross"><div class="section-heading"><div><h3>多维交叉核查</h3>
      <p>同截点可复算的比较与待补条件。</p></div></div>
      <div class="dimension-cross-grid">${combinations.map(row=>`<article><div>${chip(combinationStates[row.state],row.state==='conflict'?'amber':'gray')}</div>
        <h4>${esc(row.title)}</h4><small>${row.names.map(esc).join(' × ')}</small><p>${esc(row.summary)}</p>
        ${row.facts.length?`<details><summary>核对关联事实</summary>${evidenceButtons(row.facts)}</details>`:''}</article>`).join('')}</div>
    </section>
    <section class="dimension-ledger" aria-label="十四维事实明细"><div class="section-heading"><h3>十四维事实明细</h3>
      <div class="dimension-toggles" aria-label="筛选事实维度">
        <button class="btn small" data-action="dimension-filter" data-id="all" aria-pressed="true">全部 ${items.length} 项</button>
        ${decision?`<button class="btn small" data-action="dimension-filter" data-id="used" aria-pressed="false">原稿列入核查</button>`:''}
        <button class="btn small" data-action="dimension-filter" data-id="missing" aria-pressed="false">待补证</button>
      </div></div>
      <p data-empty-dimensions hidden>当前筛选无对应维度。</p>${factsSection}</section>
  </section>`;
}

async function showDimensionPreview(options) {
  const session=options.session||researchState.session;
  const p=new URLSearchParams({session});
  if(options.day)p.set('day',options.day);
  const view=await api('/api/v2/opportunities/'+encodeURIComponent(options.id)+'/analysis?'+p);
  const back=options.back==='screen'?`<button class="btn small" data-action="rise-screen" data-session="${esc(session)}"
    data-day="${esc(view.market_day)}" data-rise-type="${esc(options.riseType)}" data-q="${esc(options.q)}"
    data-after="${esc(options.after||'0')}" data-input-hash="${esc(options.inputHash)}">返回筛选结果</button>`:'';
  openReading(dialog(view.name+' · 多维分析',view.symbol+' · '+view.cutoff.slice(0,16).replace('T',' '),
    riseEvidence(view.rise,view.rise_policy,false)+priceScenarioDetails(view.outlook,view.outlook_source,null)+
      counterpartyDetails(view.counterparty_reference,null)+multidimEvidence(view.analysis,null,
      view.mode==='retrospective'?'同截点历史回看 · 本地证据 · 待 AI 研判':'同截点本地证据 · 待 AI 研判'),
    back+`<button class="btn small" data-action="research-preparation" data-id="${esc(view.symbol)}">补齐个股证据</button>`));
}

document.addEventListener('click',async event=>{
  const button=event.target.closest('[data-action="dimension-preview"],[data-action="dimension-filter"]');
  if(!button||button.disabled)return;
  const {action,id}=button.dataset;
  if(action==='dimension-filter'){
    const ledger=button.closest('.dimension-ledger');
    ledger.querySelectorAll('.dimension-toggles button').forEach(item=>item.setAttribute('aria-pressed',item===button));
    const rows=[...ledger.querySelectorAll('.dimension-row')];
    for(const row of rows)row.hidden=id==='used'?row.dataset.used!=='true':id==='missing'&&row.dataset.state!=='missing';
    for(const group of ledger.querySelectorAll('.dimension-cluster'))group.hidden=!group.querySelector('.dimension-row:not([hidden])');
    ledger.querySelector('[data-empty-dimensions]').hidden=rows.some(row=>!row.hidden);
    return;
  }
  button.disabled=true;
  try{
    await showDimensionPreview(button.dataset);
  }catch(error){toast(error.message,true);}
  finally{if(button.isConnected)button.disabled=false;}
});
