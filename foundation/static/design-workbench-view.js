'use strict';

const atlasShortNames={
  F01:'大盘',F02:'板块',F03:'事件',F04:'量价',F05:'活性',F06:'结构',F07:'财务',
  F08:'估值',F09:'供需',F10:'资金',F11:'情绪',F12:'流动',F13:'风险',F14:'执行',
};
const atlasToneLabels={supported:'可核',partial:'部分有据',missing:'待补证',unknown:'待有样本'};
const atlasCountText=counts=>`${counts.supported} 只可核 · ${counts.partial} 只部分有据 · ${counts.missing} 只待补`;
let atlasContext;

function atlasDimension(dimension,candidates){
  const rows=candidates.map(candidate=>candidate.dimensions.find(row=>row.id===dimension.id)).filter(Boolean);
  const counts=dimensionCounts(rows);
  const tone=!rows.length?'unknown':counts.supported===rows.length?'supported':
    counts.missing===rows.length?'missing':'partial';
  return {dimension,rows,counts,tone};
}

function atlasCoverage({counts,rows}){
  return `<div class="atlas-coverage" role="img" aria-label="${rows.length?atlasCountText(counts):'待有研究样本'}">${rows.length?
    ['supported','partial','missing'].filter(key=>counts[key]).map(key=>`<i class="${key}" style="flex:${counts[key]}"></i>`).join('')
    :'<i class="unknown"></i>'}</div>`;
}

function atlasReadings(detail,id){
  if(!detail)return '<small>正在读取该股的冻结证据…</small>';
  if(detail.error)return `<small>${esc(detail.error)}</small>`;
  const row=detail.analysis.items.find(item=>item.id===id);
  if(!row)return '';
  return `${row.readings.length?`<dl>${row.readings.slice(0,3).map(item=>`
    <div><dt>${esc(item.label)}</dt><dd>${formatDimensionReading(item)}</dd></div>`).join('')}</dl>`:''}
    ${row.facts.length?`<details><summary>核对原始事实</summary>${evidenceButtons(row.facts)}</details>`:''}`;
}

async function atlasStockDetail(context,symbol){
  if(context.readings.has(symbol))return context.readings.get(symbol);
  const request=api(`/api/v2/research/runs/${encodeURIComponent(context.run.id)}/stocks/${encodeURIComponent(symbol)}/bars`)
    .catch(error=>({error:error.message}));
  context.readings.set(symbol,request);
  const detail=await request;
  context.readings.set(symbol,detail);
  return detail;
}

function atlasPane(context,params){
  const {catalog,candidates,run}=context;
  const selectedPath=catalog.paths.find(path=>path.id===params.get('path'))||catalog.paths[2]||catalog.paths[0];
  const selectedStock=candidates.find(candidate=>candidate.symbol===params.get('stock'));
  const scope=selectedStock?[selectedStock]:candidates;
  const evidence=new Map(catalog.dimensions.map(dimension=>[dimension.id,atlasDimension(dimension,scope)]));
  const selectedId=selectedPath.dimensions.includes(params.get('dim'))?params.get('dim'):selectedPath.dimensions[0];
  const selected=evidence.get(selectedId);
  const matches=scope.filter(candidate=>candidate.paths.includes(selectedPath.id));
  const pathCount=run?.data?.bundle?.market?.path_counts;
  const groupHeads=dimensionGroups.map(([title,codes])=>`<span style="--span:${codes.length}">${esc(title)}</span>`).join('');
  const dimensionOrder=dimensionGroups.flatMap(([,codes])=>codes).map(id=>catalog.dimensions.find(dimension=>dimension.id===id));
  const rows=catalog.paths.map((path,index)=>`<div class="atlas-row ${path.id===selectedPath.id?'current':''}">
    <button type="button" class="atlas-path" data-action="atlas-path" data-id="${esc(path.id)}" aria-pressed="${path.id===selectedPath.id}">
      <span>${String(index+1).padStart(2,'0')}</span><strong>${esc(pathName(path.id))}</strong></button>
    ${dimensionOrder.map(dimension=>{
      if(!path.dimensions.includes(dimension.id))return '<span class="atlas-dot inactive" aria-hidden="true"></span>';
      const {tone}=evidence.get(dimension.id);
      const selectedCell=path.id===selectedPath.id&&dimension.id===selectedId;
      const label=`${path.name} · ${dimension.name} · ${atlasToneLabels[tone]}`;
      return `<button type="button" class="atlas-dot ${tone}" data-action="atlas-cell" data-path="${esc(path.id)}" data-id="${esc(dimension.id)}"
        aria-label="${esc(label)}" title="${esc(label)}" aria-pressed="${selectedCell}"><span></span></button>`;
    }).join('')}</div>`).join('');
  const columns=dimensionOrder.map(dimension=>`<span title="${esc(dimension.name)}">${esc(atlasShortNames[dimension.id]||dimension.name)}</span>`).join('');
  const dimensionButtons=selectedPath.dimensions.map(id=>{
    const value=evidence.get(id);
    return `<button type="button" data-action="atlas-cell" data-path="${esc(selectedPath.id)}" data-id="${esc(id)}"
      class="atlas-dimension-button" aria-pressed="${id===selectedId}"><span>${esc(value.dimension.name)}</span>
      ${atlasCoverage(value)}<small>${esc(atlasToneLabels[value.tone])}</small></button>`;
  }).join('');
  const selectedRow=selected.rows[0];
  const listed=selectedStock?selectedRow?.reason:'';
  const detail=context.readings.get(selectedStock?.symbol);
  const ready=detail?.analysis||detail?.error?detail:null;
  return `<section class="atlas-map" aria-label="路径与证据关系矩阵" id="atlas-map">
    <header class="atlas-map-head"><div><span class="desk-index">01 / PATH × EVIDENCE</span><h2>证据地图</h2></div>
    ${selectField('查看范围','atlas-stock',selectedStock?.symbol||'',
      [['','本轮全部股票'],...candidates.map(item=>[item.symbol,item.name+' · '+item.symbol.slice(3)])])}</header>
    <div class="atlas-mobile-selection"><span>${esc(selected.dimension.name)} · ${atlasToneLabels[selected.tone]}</span>
      <button type="button" data-action="atlas-jump" data-id="atlas-detail">查看证据 ↓</button></div>
    <div class="atlas-map-scroll" tabindex="0" aria-label="关系矩阵，可横向滚动">
      <div class="atlas-grid"><div class="atlas-axis"><span>发现路径</span>${groupHeads}</div>
      <div class="atlas-column-labels"><span></span>${columns}</div>${rows}</div></div>
    <div class="atlas-legend"><span>点选有色节点看证据</span>
      ${Object.entries(atlasToneLabels).map(([key,label])=>`<span><i class="${key}"></i>${label}</span>`).join('')}</div></section>
    <aside class="atlas-detail" aria-label="所选路径与维度" id="atlas-detail" tabindex="-1">
      <div class="atlas-detail-kicker"><span class="desk-index">02 / INSPECT</span>
      <span>${pathCount?`原截点 ${num(pathCount[selectedPath.id]||0)} 条`:'待有研究截点'}</span></div>
      <h2>${esc(selectedPath.name)}</h2><p class="atlas-path-rule">${esc(selectedPath.description)}</p>
      <div class="atlas-dimensions">${dimensionButtons}</div>
      <section class="atlas-fact" aria-label="所选证据">
        <div><h3>${esc(selected.dimension.name)}</h3><span class="atlas-fact-badge ${selected.tone}">${atlasToneLabels[selected.tone]}</span></div>
        <p>${esc(selected.dimension.question)}</p>
        <small>${esc(listed?dimensionReason(selectedRow):selected.rows.length?atlasCountText(selected.counts):'等待研究记录')}</small>
        ${selectedStock?`<div class="atlas-readings" aria-live="polite" data-stock="${esc(selectedStock.symbol)}" data-dim="${esc(selectedId)}">
          ${atlasReadings(ready,selectedId)}</div>`:''}
      </section>
      <div class="atlas-matches"><strong>${selectedStock?'当前股票':'本轮送研中命中'}${selectedStock?'<span></span>':`<span>${matches.length} 只</span>`}</strong>
        ${(selectedStock?[selectedStock]:matches).map(item=>`<button type="button" class="atlas-stock-link" data-action="stock-research"
          data-id="${esc(item.symbol)}" data-run="${esc(run?.id)}">${esc(item.name)} <span>查看个股 ↗</span></button>`).join('')
          ||'<p>本轮送研名单中无匹配股票。</p>'}
        ${selectedStock&&!matches.length?'<p>这只股票本轮未命中当前路径。上方显示该股证据状态。</p>':''}</div>
      <button type="button" class="atlas-jump-back" data-action="atlas-jump" data-id="atlas-map">返回证据地图 ↑</button>
    </aside>`;
}

function stockCheckLabel(decision){
  if(!decision)return '筛选线索';
  if(decision.review_event||decision.withdrawn)return decisionName(decision);
  return {watch:'观察',conditional:'等条件',reject:'不选'}[decision.action]||decisionName(decision);
}

function stockCheckPane(context,params){
  const {run,candidates,decisions}=context;
  const stock=candidates.find(item=>item.symbol===params.get('stock'))||candidates[0];
  const decision=decisions.get(stock.symbol);
  const {rise}=stock.metrics;
  const signals=[rise?.name,...stock.paths.map(pathName)].filter(Boolean);
  const quote=stock.reference_quote?.id||stock.bar_ids?.at(-1);
  const status=stockCheckLabel(decision);
  const entry=decision?.original_entry||decision?.entry;
  const conditionLabel={buy:'参与条件',wait:'等待条件',avoid:'原稿处理条件'}[adviceForAction[decision?.action]];
  const checks=(decision?.next_checks||stock.next_checks||[]).slice(0,2);
  const blockers=decision?.formal_blockers||[];
  const text=value=>esc(researchText(run,value));
  const stockAction=`data-action="stock-research" data-run="${esc(run.id)}" data-id="${esc(stock.symbol)}"`;
  const reasons=[['支持理由',decision?.supports?.[0]],['反证与风险',decision?.counterarguments?.[0]]];
  return `<div class="stock-check-layout">
    <aside class="panel stock-check-picker" aria-label="选择候选股票">
      <h2>1 选一只候选股</h2>
      ${selectField('1 选一只候选股','stock-check-stock',stock.symbol,candidates.map(item=>
        [item.symbol,`${item.name} · ${item.symbol.slice(3)} · ${stockCheckLabel(decisions.get(item.symbol))}`]))}
      <div class="stock-check-list">${candidates.map(item=>{
        const selected=item.symbol===stock.symbol;
        return `<button type="button" data-action="stock-check-select" data-id="${esc(item.symbol)}" aria-pressed="${selected}">
          <strong>${esc(item.name)}</strong><small>${esc(item.symbol.slice(3))} · ${esc(stockCheckLabel(decisions.get(item.symbol)))}</small>
        </button>`;
      }).join('')}</div>
    </aside>
    <article class="panel stock-check-detail" aria-label="所选股票的依据与后续动作">
      <header class="stock-check-title"><div><span>2 看依据</span><h2>${esc(stock.name)} <small>${esc(stock.symbol.toUpperCase())}</small></h2></div>
        ${chip(status,decision?.action==='watch'||decision?.action==='conditional'?'amber':'gray')}</header>
      <p class="stock-check-thesis">${decision?text(decision.thesis):'当前可核对入围线索和截点事实。'}</p>
      <div class="stock-check-actions">
        <button type="button" class="btn primary small" ${stockAction}>${decision?'查看完整论证':'查看冻结证据'} ↗</button>
        ${decision?.cycle_id&&decision.action!=='reject'?`<button type="button" class="btn small" data-action="cycle-detail"
          data-id="${esc(decision.cycle_id)}">查看跟踪去留 ↗</button>`:''}
      </div>
      <div class="stock-check-section">
        <h3>为什么进入候选</h3>
        <p>${signals.length?`<strong>${signals.map(esc).join(' · ')}</strong>`:'本轮研究范围'}
          ${rise?.reason?`<span>${text(rise.reason)}</span>`:''}</p>
        ${quote?`<button type="button" class="btn small" data-action="record" data-id="${esc(quote)}">核对截点行情 ↗</button>`:''}
      </div>
      ${decision?`<div class="stock-check-evidence">${reasons.filter(([,item])=>item).map(([label,item])=>`
        <section><h3>${label}</h3><p>${text(item.text)}</p>${reportStatementSources(item)}</section>`).join('')}</div>`:''}
      <div class="stock-check-section stock-check-next">
        <h3>3 接下来核对什么</h3>
        ${entry?.condition?`<p><strong>${conditionLabel||'原稿条件'}</strong> ${text(entry.condition)}</p>`:''}
        ${decision?.invalidation?`<p><strong>判断何时失效</strong> ${text(decision.original_invalidation||decision.invalidation)}</p>`:''}
        ${checks.length?`<ul>${checks.map(item=>`<li>${text(item)}</li>`).join('')}</ul>`:''}
        ${blockers.length?`<details><summary>签发时待核对 ${num(blockers.length)} 项</summary>
          <ul>${blockers.map(item=>`<li>${text(buyCheckName(item))}</li>`).join('')}</ul></details>`:''}
      </div>
    </article>
  </div>`;
}

async function renderMethods(ticket){
  const route=location.hash;
  const params=new URLSearchParams(route.split('?')[1]||'');
  const sessionFilter=params.get('session');
  const query=new URLSearchParams({workbench:'true'});
  if(Object.hasOwn(sessionLabels,sessionFilter))query.set('session',sessionFilter);
  const data=await api('/api/v2/research?'+query);
  const runId=params.get('run');
  const run=runId&&runId!==data.attempt?.id?
    await api('/api/v2/research/runs/'+encodeURIComponent(runId)):data.attempt;
  const session=run?.data?.session||query.get('session')||'close';
  const today=data.now.slice(0,10);
  const day=run?.market_day||today;
  const historical=day!==today;
  const bundle=run?.data?.bundle;
  const otherAttempt=!bundle&&data.runs.items.some(item=>item.market_day===day&&item.session===session&&item.id!==run?.id);
  const candidates=shownCandidates(run);
  const decisions=new Map(effectiveDecisions(run).map(item=>[item.symbol,item]));
  const context={catalog:data.catalog,run,candidates,decisions,readings:new Map()};
  const mapOpen=params.has('path')||params.has('dim');
  const symbol=params.get('stock');
  if(mapOpen&&candidates.some(candidate=>candidate.symbol===symbol))await atlasStockDetail(context,symbol);
  if(ticket!==pageRequest||route!==location.hash)return null;
  atlasContext=context;
  const running=['queued','running','waiting'].includes(run?.status);
  if(running)startResearchPolling(run,!bundle);
  const deck=`<header class="desk-mast stock-check-mast">
    <div><h1>逐股核对</h1>
    <p>选一只股票，查看它为什么入围、判断有何依据、接下来等什么条件。</p></div>
  </header>`;
  return deck+`<section class="research-context ${historical?'historical':''}" aria-label="本轮证据日期">
    ${researchDateLine(day,session,run?.report?.published_at,historical?(run?.report?'历史原稿':'历史任务'):'今日')}
    ${run?`<span class="research-flow-run">任务 ${esc(run.id)} · ${esc(researchStatus[run.status]||run.status)}</span>`:''}
    ${historical?`<a class="btn small" href="${esc(methodsHref(session))}">查看今日${sessionLabels[session]}</a>`:''}
    ${otherAttempt?`<a class="btn small" href="${esc(archiveHref({start:day,end:day,session,outcome:'all'}))}">查看同日任务</a>`:''}</section>
    ${researchFlow('methods',run,day,session)}
    ${running?researchProgress(run):''}
    ${candidates.length?`<div id="stock-check-workspace">${stockCheckPane(context,params)}</div>`:
      bundle?empty('本轮没有可查看的候选股','可打开筛选线索，核对这轮的范围与结果。'):
      empty('这轮筛选资料尚未冻结',run?'冻结后，候选股和证据会显示在这里。':'可打开今日研究，查看时段状态和可用的行情线索。')}
    <details class="atlas-rules stock-check-advanced" id="stock-check-methods" ${mapOpen?'open':''}>
      <summary>查看证据地图和筛选规则</summary>
      <p>可查看发现路径用了哪些证据，也可按维度核对事实和资料缺口。</p>
      ${bundle?`<div class="atlas-layout" id="atlas-layout">${atlasPane(context,params)}</div>`:''}
      ${riseMethods(data.catalog.rise_policy)}
      ${data.features?.originals?'<button type="button" class="btn small" data-action="policy-originals">政策原文库</button>':''}
    </details>`;
}

async function loadAtlasSelection(layout,context){
  const target=layout.querySelector('.atlas-readings');
  if(!target)return;
  const detail=await atlasStockDetail(context,target.dataset.stock);
  if(context===atlasContext&&target.isConnected){
    target.innerHTML=atlasReadings(detail,target.dataset.dim);
  }
}

async function updateAtlas(changes,focusAction,focusId,focusPath){
  const layout=document.getElementById('atlas-layout');
  if(!layout||!atlasContext)return;
  const context=atlasContext;
  const params=new URLSearchParams(location.hash.split('?')[1]||'');
  params.set('run',context.run.id);
  params.set('session',context.run.data.session||'close');
  for(const [key,value] of Object.entries(changes))if(value)params.set(key,value);else params.delete(key);
  const previousScroll=layout.querySelector('.atlas-map-scroll')?.scrollLeft||0;
  history.replaceState(null,'','#methods'+(params.size?'?'+params:''));
  startResearchPolling(context.run);
  const workspace=document.getElementById('stock-check-workspace');
  if(workspace&&Object.hasOwn(changes,'stock'))workspace.innerHTML=stockCheckPane(context,params);
  layout.innerHTML=atlasPane(context,params);
  layout.querySelector('.atlas-map-scroll').scrollLeft=previousScroll;
  const buttons=[...layout.querySelectorAll('[data-action]')];
  const clicked=buttons.find(button=>button.dataset.action===focusAction&&button.dataset.id===focusId
    &&(focusAction!=='atlas-cell'||button.dataset.path===focusPath));
  const focus=focusAction==='stock-check-select'?workspace?.querySelector('[aria-pressed="true"]'):
    focusAction==='stock-check-stock'?workspace?.querySelector('[name="stock-check-stock"]'):
    focusAction?clicked:layout.querySelector('[name="atlas-stock"]');
  focus?.focus({preventScroll:true});
  if(layout.closest('details')?.open)await loadAtlasSelection(layout,context);
}

document.addEventListener('click',event=>{
  const button=event.target.closest('[data-action="stock-check-select"],[data-action="atlas-path"],[data-action="atlas-cell"],[data-action="atlas-jump"]');
  if(!button)return;
  const {action,id,path}=button.dataset;
  if(action==='stock-check-select'){
    if(button.getAttribute('aria-pressed')!=='true')updateAtlas({stock:id},action,id);
    return;
  }
  if(action==='atlas-jump'){
    const section=document.getElementById(id);
    section?.scrollIntoView({block:'start'});
    (section?.querySelector('.atlas-path[aria-pressed=true]')||section)?.focus({preventScroll:true});
    return;
  }
  updateAtlas(action==='atlas-path'?{path:id,dim:''}:{path,dim:id},action,id,path);
});

document.addEventListener('change',event=>{
  const {name,value}=event.target;
  if(name==='atlas-stock')updateAtlas({stock:value});
  if(name==='stock-check-stock')updateAtlas({stock:value},name);
});

document.addEventListener('toggle',event=>{
  const details=event.target;
  const layout=details.id==='stock-check-methods'&&details.open&&details.querySelector('#atlas-layout');
  if(layout&&atlasContext)loadAtlasSelection(layout,atlasContext);
},true);

function researchMast(controls,run,session){
  const date=run?.data.bundle?.market_data_cutoff?.slice(0,16).replace('T',' ');
  return `<header class="desk-mast research-mast"><div><div class="desk-index"><i></i> 01 / MARKET RESEARCH</div>
    <h1>AI 研究与选股</h1><p>${date?`${run.report?'原稿截点':'行情截点'} ${esc(date)} · ${esc(sessionLabels[session])}`:'按午盘、尾盘、收盘查看研究'}</p></div>
    <div class="desk-actions"><button class="btn primary" data-action="new-research">${icon('spark')}开始一次研究</button>
    <div>${controls}<button class="btn ghost small" data-action="usage-page">调用与 TOKEN</button></div></div></header>`;
}

function deskPageMast(index,title,detail,actions=''){
  return `<header class="desk-mast"><div><div class="desk-index"><i></i> ${index}</div>
    <h1>${title}</h1><p>${detail}</p></div><div class="desk-actions">${actions}</div></header>`;
}

function researchDecisionBoard(run,choices,missedWithoutRun){
  const report=run?.report,bundle=run?.data?.bundle,market=bundle?.market;
  const withdrawn=Boolean(run?.data?.withdrawn_at);
  const reviewMode=report?.kind==='review';
  const totals=adviceCounts(choices);
  const reviewed=report?.decisions||[];
  const labels=reviewMode?[
    ['移出',reviewed.filter(item=>item.tracking_state==='removed').length,'out'],
    ['待补证',reviewed.filter(item=>item.tracking_state==='needs_evidence').length,'watch'],
    ['本轮复核',reviewed.length,'neutral'],
  ]:[['可考虑买入',totals.buy,'buy'],['先等等',totals.wait,'watch'],['不建议买入',totals.avoid,'neutral']];
  const steps=[['初筛范围',market?.scope_size],['行情与历史可用',market?.covered],['按偏好筛选后可研究',researchParticipating(market)],['出现机会线索',market?.discovered],['送入研究',market?.displayed]];
  const unsigned=run&&!report&&!['queued','running','waiting'].includes(run.status);
  const verdict=withdrawn?'报告已撤回':reviewMode?'本轮复核结果':report?'本轮选股结果':unsigned||missedWithoutRun?'本轮未签发':bundle?'等待 AI 研判':'等待研究结果';
  return `<section class="desk-decision" aria-label="本轮判断和筛选范围"><div class="desk-decision-main">
    <div class="desk-index"><i></i> ${reviewMode?'REASSESSMENT':'SIGNED RESEARCH'}</div><h2>${verdict}</h2>
    ${report&&!withdrawn?`<div class="desk-verdicts">${labels.map(([label,value,tone])=>`<div class="${tone}"><b>${num(value)}</b><span>${label}</span></div>`).join('')}</div>`
      :`<p class="desk-awaiting">${withdrawn?'可以查阅报告摘要。':bundle?'已冻结候选和行情。':missedWithoutRun?'可在时段计划核对本轮记录。':'研究签发后会在这里显示判断。'}</p>`}
    ${report&&!withdrawn?researchHeroNames(bundle,report,run):''}
    <div class="desk-decision-actions">
    ${bundle?`<a class="btn small" href="${esc(methodsHref(run.data.session||'close',run.id))}">逐股核对本轮依据 ↗</a>`:''}
    ${report?'<button class="btn ghost small" data-action="full-report">阅读本轮报告 ↗</button>'
      :'<button class="btn ghost small" data-action="scan-opportunities">查看机会线索 ↗</button>'}</div></div>
    <div class="desk-funnel"><div class="desk-funnel-head"><span class="desk-index">MARKET → SIGNAL → REVIEW</span><h3>从初筛到送研</h3></div>
    <ol>${steps.map(([label,count],index)=>{const share=market?.scope_size&&count!=null?Math.min(100,count/market.scope_size*100):0;
      return `<li><div><span>0${index+1} / ${label}</span><b>${num(count)}</b></div><div class="desk-meter" aria-hidden="true">
      ${share>0?`<i style="--share:${share}%"></i>`:''}</div></li>`;}).join('')}</ol>
    <div class="desk-funnel-foot"><span>${market?'条长按初筛范围换算':'待有可核对的筛选记录'}</span>
    ${bundle&&run?`<button class="desk-link" data-action="screening-audit" data-run="${esc(run.id)}">筛选明细 ↗</button>`:''}</div></div></section>`;
}
