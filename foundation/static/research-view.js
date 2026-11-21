'use strict';
const researchState={session:'close',selected:null,poll:null,requestKey:null};
let stockReadTicket = 0;
const adviceNames={buy:'可考虑买入',wait:'先等等',avoid:'不建议买入'};
const adviceForAction={select:'buy',conditional:'wait',watch:'wait',reject:'avoid'};
const researchStatus={queued:'等待研究',running:'正在研究',waiting:'等待自动轮换',completed:'研究已完成',completed_limited:'研究已完成 · 存在证据限制',data_limited:'研究资料不足',failed:'研究未完成',unknown:'远端终态未知',stopped:'已停止',interrupted:'研究已中断',expired:'签发窗口已过'};
const cycleNames={waiting:'等条件',active:'跟踪中',observe:adviceNames.wait,needs_evidence:'待补资料',removed:'已移出',expired:'已到期',rejected:adviceNames.avoid};
const decisionName=d=>d.withdrawn?'已撤回':d.review_event?(cycleNames[d.tracking_state]||'复核待定'):adviceNames[adviceForAction[d.action]];
const dimNames={available:'已有可计算证据',partial:'部分证据',missing:'待补证',verified:'已核验',conditional:'待参与时核价'};
const outcomeNames={observation:'观察样本，不计正式成绩',not_due:'未到期',untriggered:'未登记有效触发',late_trigger:'触发晚于原端点',missing_calendar:'缺交易日历',missing_price:'缺合格终价',not_comparable:'价格可比性待核',missing_reference:'缺剔除参考价',after_endpoint:'剔除晚于原端点',complete:'可评价'};
const percent=v=>v==null?'—':`${v>0?'+':''}${(v*100).toFixed(2)}%`;
const horizonDates=ends=>Object.entries(ends||{}).map(([h,day])=>`${esc(scenarioWindows[h]||h)} ${esc(day||'缺日历')}`).join(' / ')||'待核日历';
const pathName=id=>({O01:'新信息',O02:'板块扩散',O03:'量价突破',O04:'活性重启',O05:'回撤修复',O06:'经营拐点',O07:'供需改善',O08:'风险修复'}[id]||id);
const buyCheckNames={
  '公司风险证据尚未完成核验':'公司风险还没核实',
  '公司行动与跨日价格可比性尚未核验':'还需核对分红送股等是否影响历史股价比较',
  '缺少有效参与核价和组合约束':'参考买价和持仓限制还待核对',
};
function adviceCounts(decisions) {
  const counts={buy:0,wait:0,avoid:0};
  for(const d of decisions){
    const key=adviceForAction[d?.action];
    if(key&&!d.withdrawn&&!d.review_event)counts[key]++;
  }
  return counts;
}
function adviceCountLine(counts, keys) {
  return keys.map(key=>`${adviceNames[key]} ${num(counts[key])} 只`).join(' · ');
}
const buyCheckName=reason=>buyCheckNames[reason]||reason;
const researchStamp=value=>value?.slice(0,16).replace('T',' ')||'';
const researchHref=(session,runId)=>'#research?'+new URLSearchParams(runId?{run:runId,session}:{session});
const methodsHref=(session,runId,stock)=>'#methods?'+new URLSearchParams({
  session,...(runId?{run:runId}:{}),...(stock?{stock}:{}),
});

function researchDateLine(day,session,publishedAt,label='市场日'){
  return `<div class="research-date-line"><strong>${esc(label)} ${esc(day)} · ${esc(sessionLabels[session]||'收盘')}</strong>
    ${publishedAt?`<span>签发 <time datetime="${esc(publishedAt)}">${esc(researchStamp(publishedAt))}</time></span>`:''}</div>`;
}
const missingEvidence = {
  F03:'本轮缺可引用的政策原文或个股事件资料。',
  F07:'本轮缺可比报告期的财务数据或财报原文。',
  F09:'本轮缺可引用的库存、价格、成本或合同资料。',
  F10:'本轮缺有来源、日期和统计口径的资金流或持仓资料。',
  F11:'本轮缺可核对的关注度与传播资料。',
  F13:'本轮缺可核对的风险材料或有效核验记录。',
};
function dimensionReason(dimension) {
  return dimension.reason==='尚无足够且经核验的本轮证据'
    ? missingEvidence[dimension.id]||dimension.reason : dimension.reason;
}
function dimensionTitles(ids, dimensions) {
  const names = new Map(dimensions.map(d=>[d.id,d.name]));
  return ids.map(id=>names.get(id)||'其他证据项');
}
function researchFlow(active,run,day,session,archived=false) {
  const market=run?.data?.bundle?.market;
  const hasEvidence=Boolean(market||run?.has_bundle);
  const runId=run?.id;
  const withdrawn=run?.data?.withdrawn_at||run?.withdrawn_at;
  const signed=Boolean((run?.report?.published_at||run?.published_at)&&!withdrawn);
  const pool=run?.report?.decisions?.some(item=>item.action==='select')?'buy':'watch';
  const steps=[
    {id:'coverage',title:'行情覆盖',note:market?`${num(market.covered)} / ${num(market.scope_size)} 只`:hasEvidence?'范围已冻结':'待冻结',
      action:hasEvidence?'research-coverage':null},
    {id:'screening',title:'筛选线索',note:market?`${num(market.discovered)} 只命中`:hasEvidence?'查看冻结结果':'待筛选',
      action:hasEvidence?'screening-audit':null},
    {id:'methods',title:'逐股核对',note:market?`${num(market.displayed)} 只送研`:hasEvidence?'查看本轮证据':'待有候选',
      href:methodsHref(session,runId)},
    {id:'research',title:'研究报告',note:signed?'已签发':withdrawn?'已撤回':researchStatus[run?.status]||'待发起',
      href:researchHref(session,runId)},
    {id:'tracking',title:'股票跟踪',note:signed?'按本轮原稿':'待签发',
      href:signed?libraryHref({run:runId,pool}):null},
    {id:'review',title:'每日复盘',note:archived?'已归档':'查看当日',
      href:day?reviewHref({day,run:runId}):null},
  ];
  return `<nav class="research-flow" aria-label="本轮投研步骤">${steps.map((step,index)=>{
    const attrs=`class="flow-step ${step.id===active?'current':''}" data-flow-step="${step.id}" ${step.id===active?'aria-current="step"':''}`;
    const content=`<span>${String(index+1).padStart(2,'0')}</span><div><strong>${step.title}</strong><small>${esc(step.note)}</small></div>`;
    if(step.action)return `<button type="button" ${attrs} data-action="${step.action}" data-run="${esc(runId)}">${content}</button>`;
    if(step.href)return `<a ${attrs} href="${esc(step.href)}">${content}</a>`;
    return `<div ${attrs}>${content}</div>`;
  }).join('')}</nav>`;
}
function researchHeroNames(bundle,report,run){
  if(!bundle)return '';
  const rows=report?effectiveDecisions(run).filter(x=>x.action!=='reject'):shownCandidates(run);
  const ready=rows.filter(x=>x.action==='select'&&!x.review_event);
  return `<div class="hero-stock-names">${(ready.length?ready:rows).slice(0,4).map(x=>`<button data-action="stock-research" data-id="${esc(x.symbol)}" data-run="${run.id}">${esc(x.name)} <small>${esc((x.rise||x.metrics?.rise)?.name||'')} ${report?decisionName(x):'待AI研究'}</small> ↗</button>`).join('')}</div>`;
}
function researchTime(run,now){
  const mode=run.report?.mode||run.data.bundle?.mode;
  const report=run.report, bundle=run.data.bundle;
  const expires=report?.action_valid_until;
  const elapsed=expires&&Date.parse(now)>=Date.parse(expires);
  return `<div class="research-time"><span>行情截至 <b>${esc(researchStamp(bundle?.market_data_cutoff)||'尚未封存')}</b></span>
    <span>知识截至 <b>${esc(researchStamp(bundle?.knowledge_cutoff)||'尚未封存')}</b></span>
    ${mode==='retrospective'||mode==='late_review'?chip(mode==='retrospective'?'历史回看':'迟发回看','gray'):''}
    ${expires?`<span>${elapsed?'参与窗口已结束于':'参与窗口截至'} <b>${esc(researchStamp(expires))}</b></span>`:''}
    ${run.report?`<span>${run.report.forecast_target?'原定评价':'原报告窗口'} <b>${horizonDates(run.report.horizons)}</b></span>`:''}</div>`;
}
function researchProgress(run){
  const plan=run.data.research_plan;
  return `<div class="panel research-progress" data-run="${esc(run.id)}"><span class="research-orbit" aria-hidden="true"></span><div>
    <strong id="research-progress-stage">${esc(run.stage)}</strong>
    <p>${esc(run.market_day)} · ${esc(sessionLabels[run.data.session]||'收盘')} · 研究编号 ${esc(run.id)}</p>
    ${plan?`<details><summary>本轮计划与取证</summary><p>${esc(plan.extended_horizon||plan.horizon)} · ${esc(plan.symbols.join('、')||'从可用资料发现')}</p>
      <ol>${plan.steps.map(step=>`<li>${esc(step)}</li>`).join('')}</ol>
      ${(run.data.tool_receipt||[]).map(item=>`<p>${esc(item.question)} · ${item.state==='available'?'已取证':'资料缺口'}</p>`).join('')}</details>`:''}
    </div><button class="btn small" data-action="research-stop" data-id="${esc(run.id)}">停止研究</button></div>`;
}
function startResearchPolling(run,refreshEvidence=false){
  clearTimeout(researchState.poll);
  if(!run||!['running','queued','waiting'].includes(run.status))return;
  const ticket=pageRequest,route=location.hash;
  const isCurrent=()=>ticket===pageRequest&&route===location.hash;
  researchState.poll=setTimeout(async()=>{
    try{
      const next=await api('/api/v2/research/runs/'+run.id+'?progress=true');
      if(!isCurrent())return;
      if(refreshEvidence&&next.has_bundle){
        await render();
        return;
      }
      if(['queued','running','waiting'].includes(next.status)){
        const label=document.getElementById('research-progress-stage');
        if(label)label.textContent=next.stage;
        startResearchPolling(next,refreshEvidence);
      }else{
        toast(researchStatus[next.status]||next.status,['failed','unknown','interrupted'].includes(next.status));
        await render();
      }
    }catch{if(isCurrent())startResearchPolling(run,refreshEvidence);}
  },3000);
}
async function renderResearch(ticket){
  const route=location.hash;
  const q=new URLSearchParams(route.split('?')[1]||'');
  let run=q.get('run')?await api('/api/v2/research/runs/'+encodeURIComponent(q.get('run'))):null;
  const requested=q.get('session');
  const session=run?(run.data.session||'close'):Object.hasOwn(sessionLabels,requested)?requested:'close';
  const d=await api('/api/v2/research?workbench=true&session='+encodeURIComponent(session));
  if(ticket!==pageRequest||route!==location.hash)return null;
  researchState.session=session;
  researchState.dashboard=d;
  const today=d.now.slice(0,10);
  const slot=d.schedule?.recent.find(item=>item.day===today&&item.session===session);
  const missed=slot&&['blocked','missed'].includes(slot.state);
  if(!run&&d.attempt?.market_day===today)run=d.attempt;
  researchState.selected=run;const report=run?.report,bundle=run?.data?.bundle;
  const historical=Boolean(run&&run.market_day!==today);
  const unsigned=run&&!report&&!['queued','running','waiting'].includes(run.status);
  const missedWithoutRun=!run&&Boolean(missed);
  const active=d.active?.id===run?.id?d.active:null;
  const withdrawn=Boolean(run?.data?.withdrawn_at);
  startResearchPolling(active,!bundle);
  const controls=(d.features?.notices?'<button class="btn ghost small" data-action="research-notices">收件箱</button>':'')+(d.schedule?'<button class="btn ghost small" data-action="edit-research-preferences">研究偏好</button><button class="btn ghost small" data-action="edit-research-schedule">时段计划</button>':'');
  let html=researchMast(controls,run,session);
  if(d.test_mode)html+=`<div class="notice amber"><p>隔离界面样本：虚构行情与模型替身，仅用于验证操作流程，不是正式研究或投资结果。</p></div>`;
  html+=`<nav class="research-session" aria-label="研究时段">${[['noon','当日下午上涨','11:30'],['tail','下一交易日上涨','14:35'],['close','1–3 个交易日超短线','15:00']].map(([id,goal,time],index)=>
    `<a ${id===session?'aria-current="page"':''} class="session-tab ${id===session?'selected':''}" data-action="research-session" href="${researchHref(id)}"><small>0${index+1} · ${time} 截点</small><strong>${sessionLabels[id]}</strong><span>${goal}</span></a>`).join('')}</nav>`;
  const latest=d.latest;
  const lastSigned=researchStamp(latest?.published_at||latest?.report?.published_at);
  html+=`<section class="research-context ${historical?'historical':''}" aria-label="当前研究日期">
    ${researchDateLine(run?.market_day||today,session,report?.published_at,historical?(report?'历史原稿':'历史任务'):'今日')}
    ${q.has('run')?`<a class="btn small" href="${researchHref(session)}">返回今日${sessionLabels[session]}</a>`:''}
    ${!historical&&latest&&latest.id!==run?.id?`<a class="btn ghost small" href="${researchHref(session,latest.id)}">
      最近已签发 ${esc(latest.market_day)}${lastSigned?` · 签发 ${esc(lastSigned)}`:''} ↗</a>`:''}</section>`;
  html+=researchFlow('research',run,run?.market_day||today,session);
  if(d.schedule&&!historical)html+=slotSummary(d.schedule,session);
  if(active)html+=researchProgress(active);
  if(unsigned||missedWithoutRun){
    const label=unsigned?researchStatus[run.status]||run.status:slotReceipt(slot);
    const detail=unsigned?run.data.error||run.stage:slot.detail;
    html+=`<div class="notice amber">${icon('info')}<p><strong>${esc(label)}</strong><br>${esc(detail)}。本轮选股判断未签发。${run?.status==='unknown'?`<a href="#ai?tab=usage">查看调用回执 ↗</a> <button class="btn small" data-action="research-reconcile" data-id="${esc(run.id)}">核对原请求</button>`:''}</p></div>`;
  }
  html+=correctionSummary(run);
  const choices=effectiveDecisions(run), candidates=shownCandidates(run);
  const reviewMode=report?.kind==='review';
  if(run)html+=researchTime(run,d.now);
  html+=researchAuditSummary(run,choices);
  html+=researchDecisionBoard(run,choices,missedWithoutRun);
  if(bundle){
    const m=bundle.market;
    if(m.coverage_detail&&run)html+=`<button class="btn small" data-action="research-coverage" data-run="${esc(run.id)}">查看覆盖与排除明细</button>`;
    html+=`<div class="research-coverage"><strong>本轮研究范围</strong><span>行情与历史可用 ${num(m.covered)} / ${num(m.scope_size)} 只</span><span>按偏好筛选后可研究 ${num(researchParticipating(m))} 只</span><span>${num(m.discovered)} 只出现线索 → 本轮送研 ${num(m.displayed)} 只</span><p>${esc(m.scope)}。${m.coverage==null?'':'行情覆盖率 '+(m.coverage*100).toFixed(2)+'%。'}</p></div>`;
    const prep=run.data.evidence_preparation;
    if(prep?.length){
      const labels={cninfo_announcements:'公告索引',cninfo_text:'重点公告正文',bs_actions:'除权除息资料'};
      const states={completed:'已完成',partial:'部分完成',failed:'未取得',blocked:'未启用或受限',queued:'待采集',waiting:'待续传',running:'采集中',paused:'已暂停'};
      html+=`<div class="research-coverage"><strong>签发前补取的资料</strong>${prep.map(item=>`<span>${esc(labels[item.source]||item.source)} · ${esc(states[item.status]||item.status)}${item.total!=null?' '+num(item.done)+'/'+num(item.total)+' 批':''} ${item.job_id?`<button class="btn small" data-action="job-detail" data-id="${esc(item.job_id)}">回执 ↗</button>`:''}</span>`).join('')}<p>按冻结时刻读取已取得的证据，风险和价格可比性仍按核验规则判断。</p></div>`;
    }
    if(!report)html+=`<details class="panel unsigned-candidates"><summary>查看本轮筛选线索 · ${num(candidates.length)} 只</summary>`;
    html+=`<div class="section-heading"><div><h2>${report?'逐股研究结果':'待研究的股票'}</h2><p>${report?'点击股票查看论证、参与条件和失效条件。':'这些是本轮规则筛选的线索。'}</p></div><a class="btn ghost small" href="${esc(methodsHref(session,run.id))}">逐股核对 ${icon('arrow')}</a></div>`;
    if(bundle.rise_policy&&!reviewMode)html+=riseReportGroups(run,choices);
    else {
      if(!bundle.rise_policy)html+=`<p class="form-note">本轮使用签发时的原筛选。<button class="btn ghost small" data-action="rise-screen" data-day="${esc(run.market_day)}" data-session="${esc(session)}">另看同日四类筛选 ↗</button></p>`;
      html+=candidates.length?`<div class="research-stock-grid">${candidates.map(c=>stockResearchCard(c,choices.find(x=>x.symbol===c.symbol),run)).join('')}</div>`
        :empty('当前没有展示中的个股','可以查看股票池里的在跟判断。');
    }
    if(!report)html+='</details>';
  }
  if(report){
    const text=value=>esc(readableReportText(run,value,bundle?.candidates?.[0]?.dimensions));
    html+=`<div class="panel research-summary"><div class="panel-header"><h2>本轮取舍与共同风险</h2></div>
      <div class="panel-inner"><p>${text(report.comparison)}</p><details><summary>展开这轮还缺哪些资料</summary>
      <ul>${report.limitations.map(x=>`<li>${text(x)}</li>`).join('')}</ul></details></div></div>`;
  }
  html+=riseBoard(d.catalog.rise_policy,session);
  html+=`<div class="section-heading"><h2>研究原稿</h2><p>保留当时的日期、方法与结果</p></div>`+archiveLedger(d.runs.items);
  return html;
}
function stockResearchCard(c,d,run){
  const m=c.metrics, paths=c.paths.map(pathName);
  const card=run?.price_cards?.[c.symbol], report=run?.report;
  return `<article class="panel stock-research-card">
    <div class="stock-card-top"><div><small>${esc(c.symbol)} · ${esc(c.board||'A股')}</small><h3>${esc(c.name)}</h3></div>
      ${chip(d?decisionName(d):'待AI研究',d?.action==='reject'?'gray':adviceForAction[d?.action]==='wait'?'amber':'')}</div>
    ${run?researchDateLine(run.market_day,run.data.session,report?.published_at):''}
    <div class="stock-paths">${[m.rise?.name,...paths].filter(Boolean).map(name=>`<span>${esc(name)}</span>`).join('')||'<span>自主点名研究</span>'}${(m.rise?.risk_flags||[]).length?`<span class="risk-flag" title="存在追高风险标记，详见证据">⚠ ${num(m.rise.risk_flags.length)} 项风险</span>`:''}</div>
    <p class="stock-thesis">${esc(readableReportText(run,d?.thesis||m.rise?.reason||'本轮筛选发现了价格、成交量或行业线索，仍需核对风险和相关资料。',c.dimensions))}</p>
    ${card?poolPricePanel(card):`<div class="stock-facts"><div><span>截点价格</span><b>${poolPrice(m.close)}</b></div>
      <div><span>截点均价（20 日）</span><b>${poolPrice(m.ma20)}</b></div>
      <div><span>${m.partial_bar?'同刻相对量':'相对20日量'}</span><b>${m.relative_volume20==null?'—':m.relative_volume20.toFixed(2)+'×'}</b></div>
      </div>`}
    <div class="stock-card-foot"><span>${dimensionCountText(dimensionCounts(c.dimensions))}</span>
      <button class="btn ghost small" data-action="stock-research" data-id="${esc(c.symbol)}" data-run="${esc(run?.id||'')}">${d?'阅读个股报告':'查看筛选依据'} ${icon('arrow')}</button></div></article>`;
}
function evidenceButtons(ids){return [...new Set(ids)].map(id=>`<button class="evidence-ref" data-action="record" data-id="${id}">事实 #${id} ↗</button>`).join('');}
function priceEvidenceChart(bars){
  if(!bars?.length)return '';
  const w=700,h=220,p=12,step=(w-2*p)/bars.length,valid=bars.filter(b=>b.low!=null&&b.high!=null),lo=Math.min(...valid.map(b=>b.low)),hi=Math.max(...valid.map(b=>b.high)),span=hi-lo||1,maxV=Math.max(...bars.map(b=>b.volume||0),1),y=v=>p+(hi-v)/span*140;
  return `<div class="evidence-chart"><svg viewBox="0 0 ${w} ${h}" role="img" aria-label="已存最近60根未复权日线与成交量"><line x1="0" y1="165" x2="700" y2="165" stroke="#d7d8e2"/>${bars.map((b,i)=>{if(b.close==null||b.open==null||b.high==null||b.low==null)return '';const x=p+i*step+step/2,color=b.close>=b.open?'#ba423c':'#303b79',body=Math.max(Math.abs(y(b.close)-y(b.open)),1);return `<g><title>${esc(b.day)} 开${price(b.open)} 高${price(b.high)} 低${price(b.low)} 收${price(b.close)}</title><line x1="${x}" y1="${y(b.high)}" x2="${x}" y2="${y(b.low)}" stroke="${color}"/><rect x="${x-step*.28}" y="${Math.min(y(b.open),y(b.close))}" width="${step*.56}" height="${body}" fill="${color}"/>${b.partial_bar?'':`<rect x="${x-step*.28}" y="${210-(b.volume||0)/maxV*35}" width="${step*.56}" height="${(b.volume||0)/maxV*35}" fill="${color}" opacity=".55"/>`}</g>`;}).join('')}</svg><div><span>${esc(bars[0].day)}</span><span>${bars.at(-1).partial_bar?'末柱为盘中快照 · 未与整日成交量比较':'原始价量 · 结构为待验证线索'}</span><span>${esc(bars.at(-1).day)}</span></div></div>`;
}

function stockReportNext(run,candidate,decision){
  const text=value=>esc(readableReportText(run,value,candidate.dimensions));
  const entry=decision?.original_entry||decision?.entry;
  const hasBuyRange=['select','conditional'].includes(decision?.action)&&entry?.low!=null&&entry.high!=null;
  const conditionTitle=decision?.review_event?'原稿的参与条件':
    {select:'满足什么条件才考虑买入',conditional:'什么情况下再判断',watch:'接下来要等什么',reject:'原稿如何处理'}[decision?.action];
  const fields=[
    [conditionTitle,entry?.condition&&text(entry.condition)],
    ['参考买价范围',hasBuyRange?`${poolPrice(entry.low)} 至 ${poolPrice(entry.high)} 元`:''],
    ['原判断什么时候失效',text(decision?.original_invalidation||decision?.invalidation)],
    ['原条件观察到什么时候',decision&&decision.action!=='reject'&&!decision.review_event&&run.report.action_valid_until?
      esc(researchStamp(run.report.action_valid_until)):''],
    ['原定复盘日期',decision&&horizonDates(decision.original_endpoints||run.report.horizons)],
  ];
  const checks=(decision?.next_checks||candidate.next_checks||[]);
  const blockers=(decision?.formal_blockers||[]).map(buyCheckName);
  return `<section class="report-chapter" data-reader-section="follow" aria-label="后面看什么">
    <h3>后面看什么</h3>
    ${decision?`<dl class="report-next">${fields.filter(([,value])=>value).map(([label,value])=>
      `<div><dt>${label||'原稿条件'}</dt><dd>${value}</dd></div>`).join('')}</dl>`:''}
    ${checks.length?`<h4>还要查哪些资料</h4><ul>${checks.map(item=>`<li>${text(item)}</li>`).join('')}</ul>`:''}
    ${blockers.length?`<details class="report-source"><summary>当时还没核实的买入条件</summary>
      <ul>${blockers.map(item=>`<li>${text(item)}</li>`).join('')}</ul></details>`:''}
    ${decision?.cycle_id?`<button class="btn small" data-action="cycle-detail" data-id="${esc(decision.cycle_id)}">查看跟踪时间线 ↗</button>`:''}
  </section>`;
}

function stockReportOriginal(run,decision){
  const text=value=>esc(researchText(run,value));
  const list=items=>`<ul>${items.map(item=>`<li>${text(item.text||item)}</li>`).join('')}</ul>`;
  const entry=decision.original_entry||decision.entry;
  return `<details class="report-appendix">
    <summary>查看个股签发原文</summary>
    <div class="report-original">
      <h4>当时的判断</h4><p>${text(decision.thesis)}</p>
      <h4>支持理由</h4>${list(decision.supports)}
      <h4>反证与疑点</h4>${list(decision.counterarguments)}
      <h4>条件与待核事项</h4><p>${text(entry?.condition)}</p>
      ${entry?.low!=null&&entry.high!=null?`<p>AI 原稿的价格范围 ${poolPrice(entry.low)} 至 ${poolPrice(entry.high)} 元</p>`:''}
      <p>${text(decision.original_invalidation||decision.invalidation)}</p>${list(decision.next_checks)}
      ${reportRating(decision)}
    </div>
  </details>`;
}

async function showStockResearch(symbol,runId){
  const ticket=++stockReadTicket, route=location.hash, wasOpen=modal.open, reader=modalContent.firstElementChild;
  const current=()=>ticket===stockReadTicket&&route===location.hash&&modal.open===wasOpen&&
    (!wasOpen||reader===modalContent.firstElementChild);
  const stockPath='/api/v2/research/runs/'+encodeURIComponent(runId||'');
  const selected=researchState.selected;
  const [run,savedDetail]=runId?await Promise.all([
    selected?.id===runId?selected:api(stockPath),
    api(stockPath+'/stocks/'+encodeURIComponent(symbol)+'/bars'),
  ]):[null,null];
  if(!current())return;
  const bundle=run?.data?.bundle||researchState.preview;
  const c=(run?shownCandidates(run):bundle?.candidates||[]).find(x=>x.symbol===symbol);if(!c)throw new Error('这只股票已退出展示，或不在当前冻结研究中');
  const d=effectiveDecisions(run).find(x=>x.symbol===symbol);
  const pending=run&&['queued','running','waiting'].includes(run.status);
  const params=new URLSearchParams({session:bundle.market.session,day:bundle.market.market_day});
  const detail=savedDetail||await api('/api/v2/opportunities/'+encodeURIComponent(symbol)+'/analysis?'+params);
  if(!current())return;
  const sections=[['decision','先看结论'],['argument','为什么'],['follow','后面看什么'],['evidence','多维证据']];
  const text=value=>esc(readableReportText(run,value,c.dimensions));
  const status=d?decisionName(d):pending?'AI 研判中':run?'本轮未签发':'筛选线索';
  const buyNotConfirmed=d?.action==='watch'&&['select','conditional'].includes(d.requested_action);
  const quoteTime=bundle.market_data_cutoff;
  const quoteId=c.reference_quote?.id||c.bar_ids?.at(-1);
  const effects=d?.review_effects?.filter(item=>item.state!=='removed')||[];
  const body=`<div class="research-reader"><nav class="reader-jumps" aria-label="个股报告章节">${sections.map(([id,label],index)=>
    `<button type="button" data-action="reader-section" data-id="${id}" aria-pressed="${index===0}">${label}</button>`).join('')}</nav>
    <section data-reader-section="decision" aria-label="先看结论">
      <div class="report-dates">${researchDateLine(bundle.market.market_day,run?.data.session||bundle.market.session,run?.report?.published_at,'研究日期')}
        ${quoteTime?`<p>这份研究使用的行情截至 <time datetime="${esc(quoteTime)}">${esc(researchStamp(quoteTime))}</time>。</p>`:''}
        ${c.metrics.close!=null?`<p>${c.metrics.partial_bar?'当时报价':'当日收盘价'} <strong>${poolPrice(c.metrics.close)} 元</strong>
          ${quoteId?`<button type="button" class="evidence-ref" data-action="record" data-id="${esc(quoteId)}">查看当时的行情 ↗</button>`:''}</p>`:''}
      </div>
      ${correctionSummary(run,symbol)}
      <div class="report-verdict"><span>${d?'报告给出的判断':'本轮状态'}</span><h3>${esc(status)}</h3>
        <p>${buyNotConfirmed?'系统核对后将它列入观察，买入前的条件仍待核实。':
          d?text(d.thesis):pending?'本轮还在研判，可以先查看筛选理由和用到的资料。':'这轮可以查看筛选线索和用到的资料。'}</p>
      </div>
    </section>
    <section class="report-chapter" data-reader-section="argument" aria-label="为什么这样判断">
      <h3>为什么这样判断</h3>
      ${c.metrics.rise?`<p class="report-context-note">系统按股价和成交量把它列入「${esc(c.metrics.rise.name)}」线索。${d?'下面是原稿核对后给出的理由。':text(c.metrics.rise.reason)}</p>`:
        `<p class="report-context-note">${c.paths.length?'本轮筛选线索为 '+c.paths.map(pathName).map(esc).join('、')+'。':'它在本轮指定的研究范围内。'}</p>`}
      ${d?stockDecisionReason(run,c,d):''}
    </section>
    ${stockReportNext(run,c,d)}
    <div class="report-more">
      <h3>按需展开更多资料</h3>
      ${detail.outlook||detail.counterparty_reference||d?.counterparty||d?.counterparty_gap?`<details class="report-appendix">
        <summary>哪些走势值得留意</summary>
        ${priceScenarioDetails(detail.outlook,detail.outlook_source,d,run?.report?.horizons)}
        ${counterpartyDetails(detail.counterparty_reference,d)}
      </details>`:''}
      <details class="report-appendix" data-reader-section="evidence">
        <summary>多维证据与引用</summary>
        ${multidimEvidence(detail.analysis,d,run?'按本轮冻结资料整理 · AI 论证按原稿展示':'同截点本地证据 · 待 AI 研判')}
        ${d?stockAiContribution(run,c,d):''}
      </details>
      <details class="report-appendix">
        <summary>行情、走势图和原始资料</summary>
        ${poolPricePanel(run?.price_cards?.[symbol])}${riseEvidence(c.metrics.rise,bundle.rise_policy)}
        ${priceEvidenceChart(detail.items||c.bars)}
        ${stockEvidenceDetails(c,runId)}
        <div class="detail-layer"><div class="structure-heading"><h3>证据缺口与来源</h3>
          <button class="btn small" data-action="research-preparation" data-id="${esc(symbol)}">补齐个股证据 ↗</button></div></div>
        ${structureEvidence(c,runId)}
        ${run&&state.page!=='methods'?`<a class="btn ghost small" href="${esc(methodsHref(run.data.session||'close',run.id,symbol))}" data-action="close">在逐股核对页查看 ↗</a>`:''}
      </details>
      ${effects.length?`<details class="report-appendix"><summary>复核后的去留记录</summary>
        ${effects.map(item=>`<div class="receipt-stat"><span>${item.rise_name?'原判 '+esc(item.rise_name)+' · ':''}${esc(cycleNames[item.state]||item.state)} · ${text(item.reason)}</span>
          <button class="btn small" data-action="cycle-detail" data-id="${esc(item.cycle_id)}">原周期</button></div>`).join('')}</details>`:''}
      ${d?stockReportOriginal(run,d):''}
    </div></div>`;
  openReading(dialog(c.name+' · 个股报告',`${c.symbol} · ${bundle.market.market_day} · ${sessionLabels[run?.data.session||bundle.market.session]||'收盘'}`,body));
}
async function fullResearchReport(){
  const current=researchState.selected;if(!current?.report)throw new Error('当前没有已签发研究原稿');
  const route=location.hash;
  const run=await api('/api/v2/research/runs/'+encodeURIComponent(current.id)),r=run.report;
  if(route!==location.hash)return;
  const {bundle,session}=run.data;
  const market=bundle?.market;
  const decisions=effectiveDecisions(run);
  const text=value=>esc(readableReportText(run,value,bundle?.candidates?.[0]?.dimensions));
  const raw=value=>esc(researchText(run,value));
  const reviewed=decisions.filter(d=>d.review_event);
  const groups=run.kind==='review'?[['复核结果',decisions]]:Object.entries(adviceNames).map(([key,label])=>
    [label,decisions.filter(d=>!d.review_event&&adviceForAction[d.action]===key)]);
  if(run.kind!=='review'&&reviewed.length)groups.push(['复核结果',reviewed]);
  const actions=`<div class="heading-actions">
    ${researchState.dashboard?.features?.feedback?`<button class="btn small" data-action="edit-research-feedback" data-run="${esc(run.id)}">反馈这次研究</button>`:''}
    ${researchState.dashboard?.features?.notices?'<button class="btn small" data-action="edit-report-correction">追加更正</button>':''}
    </div>`;
  openReading(dialog((sessionLabels[session]||'收盘')+'研究报告',r.market_day,`<div class="research-reader">
    <div class="report-dates">${researchDateLine(r.market_day,session,r.published_at,'研究日期')}</div>
    ${correctionSummary(run)}
    <section class="report-verdict"><h3>这轮的结论</h3><p>${text(r.summary)}</p></section>
    <section class="report-chapter" aria-label="按结论看股票">
      <h3>按结论看股票</h3>
      <p class="report-context-note">点股票名称查看入围理由、风险和后续条件。</p>
      ${run.data.withdrawn_at?'<p>这份原稿已撤回。</p>':groups.map(([label,stocks])=>`
        <div class="report-index-group"><h4>${esc(label)} <small>${num(stocks.length)} 只</small></h4>
        ${stocks.length?`<ul>${stocks.map(d=>`<li><button type="button" class="btn small" data-action="stock-research"
          data-run="${esc(run.id)}" data-id="${esc(d.symbol)}">${esc(d.name)} <small>${esc(d.review_event?decisionName(d):d.symbol.toUpperCase())}</small></button></li>`).join('')}</ul>`:''}</div>`).join('')}
    </section>
    <section class="report-chapter"><h3>这轮看了多大范围</h3>
      ${market?.scope_size!=null&&market?.covered!=null?`<p>初筛范围是 ${num(market.scope_size)} 只股票，其中 ${num(market.covered)} 只有足够的行情可用。${market.displayed!=null?
        `本轮从筛选结果中选出 ${num(market.displayed)} 只做个股研究。`:''}</p>`:''}
      <p>${text(r.market_analysis)}</p>
    </section>
    <section class="report-chapter"><h3>这几只股票怎么比较</h3><p>${text(r.comparison)}</p></section>
    <section class="report-chapter"><h3>哪些地方仍待核实</h3><ul>${r.limitations.map(x=>`<li>${text(x)}</li>`).join('')}</ul></section>
    <details class="report-appendix"><summary>查看本轮签发原文</summary>
      <div class="report-original"><h4>${raw(r.title)}</h4><p>${raw(r.summary)}</p>
        <h4>市场情况与范围</h4><p>${raw(r.market_analysis)}</p>
        <h4>候选比较</h4><p>${raw(r.comparison)}</p>
        <h4>限制</h4><ul>${r.limitations.map(x=>`<li>${raw(x)}</li>`).join('')}</ul>
        <p class="trace-label">任务 ${esc(run.id)} · ${esc(r.model)} · ${esc(r.method_version)}<br>
          行情截至 ${esc(researchStamp(r.market_data_cutoff))} · 资料截至 ${esc(researchStamp(r.knowledge_cutoff))}</p>
      </div>
    </details>${actions}</div>`));
}
async function newResearch(cycleIds=null, scope={}){
  const [p,profile,catalog]=await Promise.all([api('/api/v2/research/plan'),api('/api/v2/research-preferences').catch(()=>null),
    researchState.dashboard?.catalog||api('/api/v2/methods')]);
  researchState.requestKey=crypto.randomUUID();
  researchState.reviewIds=cycleIds;
  const selected=cycleIds?'close':scope.session||researchState.session;
  const intent=cycleIds?'复核原论点，逐只检查新证据并决定保留、降级、剔除或待补证。保留原区间和终点。':researchIntent(selected);
  const body=`${profile?`<div class="research-settings-fields">
    ${selectField('研究时段','session',cycleIds?'close':scope.session||researchState.session,[['','从问题识别时段'],['noon','午盘 · 11:30 截止'],['tail','尾盘 · 14:35 截止'],['close','收盘 · 15:00 截止']])}
    ${inputField('指定市场日（可留空）','market_day',scope.day||'','type="date"')}</div>
    <p class="form-note">午盘和尾盘读取当日截点快照。收盘留空使用最新正式日线。已错过的窗口会标记为回看。</p>`:''}
    <label class="field">这次希望研究什么<textarea name="intent" rows="3" maxlength="2000">${esc(intent)}</textarea></label>
    ${cycleIds?`<p>范围为选中的 ${cycleIds.length} 个跟踪周期。</p>`:`<div class="research-settings-fields">
      ${selectField('研究类别','rise_type',scope.riseType||'',[['','综合发现 · 四类轮流'],...riseOptions(catalog.rise_policy)])}
      ${inputField('指定股票（最多8只，可留空）','symbols','','placeholder="sh.600000，sz.000001"')}</div>
      <p class="form-note">单选一种类别时从该类筛选。指定股票时使用「综合发现」。</p>`}
    ${profile?.confirmed_at?checkLabel('本轮应用已确认的长期研究偏好','use_preferences',true):''}
    <details class="structure-projection"><summary>补证权限</summary>
      ${checkLabel('允许AI请求一轮本地补证，可能多一次模型调用','allow_evidence_round',false)}
    </details>
    <button class="btn small" type="button" data-action="preview-research-plan">核对本轮计划</button><div data-intent-preview aria-live="polite"></div>
    ${p.unresolved_requests?`<label class="check-label"><input type="checkbox" name="acknowledge_unknown">同一市场日和时段有未知请求时，我确认发起独立新请求。</label>`:''}
    <div class="request-budget"><span>保存方案 <b>${esc(p.plan_name)}</b><small>${p.chain.map(x=>esc(x.model.name)).join(' → ')} → 循环</small></span>
    <span>模型设置 <b>${p.budget.timeout_seconds} 秒</b><small>按时段剩余时间执行</small></span></div>
    <p class="form-note">将本轮公开市场证据、意图和选用的研究偏好发送给已配置的模型。终态未知会停止后续派发。</p>`;
  openModal(dialog(cycleIds?'发起去留复核':'开始真实AI研究','提交后使用当前模型方案',body,saveButtons('开始这轮研究'),'research-form'));
}
const researchIntent=session=>`${sessionGoals[session]}。逐只说明支持证据、反证、触发和失效条件。`;
document.addEventListener('change',event=>{
  const field=event.target;
  if(field.form?.id!=='research-form'||field.name!=='session'||!field.value||researchState.reviewIds)return;
  const intent=field.form.elements.intent;
  if(Object.keys(sessionGoals).some(key=>intent.value===researchIntent(key)))intent.value=researchIntent(field.value);
});
async function showCycle(id){const c=await api('/api/v2/cycles/'+id),d=c.data;openReading(dialog(d.name+' · 原判与去留',`${c.symbol} / 周期 ${id}`,`<div class="stock-detail-conclusion">${chip(cycleNames[c.state])}<p>${esc(d.thesis)}</p></div>${riseEvidence(d.rise,researchState.dashboard?.catalog?.rise_policy)}<dl class="detail-pairs"><dt>原始行动</dt><dd>${decisionName(d)}</dd><dt>原参与条件</dt><dd>${esc((d.original_entry||d.entry).condition)}</dd><dt>原失效条件</dt><dd>${esc(d.original_invalidation||d.invalidation)}</dd><dt>原定端点</dt><dd>${horizonDates(d.endpoints)}</dd><dt>参考报价</dt><dd>${d.entry_reference?price(d.entry_reference.price):'尚未登记；未推断成交'}</dd></dl>${c.review_flags?.length?`<div class="notice amber"><div><strong>新证据需要复核</strong>${c.review_flags.map(f=>`<p>${esc(f.detail)} ${evidenceButtons([f.fact_id])}</p>`).join('')}</div></div>`:''}<h3>从原稿到当前的时间线</h3><div class="cycle-timeline">${c.events.map(e=>`<article><span>${esc(e.created.slice(0,19).replace('T',' '))}</span><strong>${{original:'首次研究',review:'去留复核',reaffirm:'再次提及，原契约不变',expiry:'原定到期',trigger:'参考报价触发'}[e.kind]||esc(e.kind)}</strong><p>${esc(e.data.reason||e.data.thesis||e.data.action||'已记录')}</p>${e.run_id?`<a href="#research?run=${e.run_id}" data-action="close">查看当时原稿 ↗</a>`:''}</article>`).join('')}</div><button class="btn small" data-action="record-fill" data-mode="actual" data-id="${esc(c.symbol)}" data-cycle="${esc(id)}">关联实际成交</button><h3>原判与去留评价</h3>${c.outcomes.map(o=>`<div class="receipt-stat"><span>${o.horizon} / ${o.kind==='original'?'原判':'去留'}</span><strong>${outcomeNames[o.status]||esc(o.status)}${o.data.return!=null?' '+percent(o.data.return):''}${o.data.excess_return!=null?' · 同窗超额 '+percent(o.data.excess_return):''}</strong></div>`).join('')||'<p>等待评价登记。</p>'}${c.formal&&c.state==='waiting'&&!d.entry_reference?`<button class="btn primary" data-action="choose-reference" data-id="${esc(id)}" data-kind="entry">核对入池参考报价</button>`:c.formal&&c.state==='removed'&&!d.removal_reference?`<button class="btn primary" data-action="choose-reference" data-id="${esc(id)}" data-kind="removal">核对剔除参考报价</button>`: ''}<p class="trace-label">观察对象不进入正式推荐命中率，后续剔除不会删除原判。成交可在实际持仓或模拟账本中关联原周期。</p>`));}
async function renderOutcomeArchive(){
  const query=new URLSearchParams(location.hash.split('?')[1]||''),riseType=query.get('rise_type')||'';
  const [p,r,experiments,controls,catalog]=await Promise.all([
    api('/api/v2/performance'+(riseType?'?rise_type='+encodeURIComponent(riseType):'')),
    riseType?null:api('/api/v2/research/runs?limit=20'),
    riseType?null:api('/api/v2/method-experiments').catch(()=>null),
    riseType?null:api('/api/v2/review-controls?limit=1').catch(()=>null),
    researchState.dashboard?.catalog||api('/api/v2/methods')
  ]);
  const counts=p.counts;
  return deskPageMast('04 / OUTCOME ARCHIVE','原判评估与方法实验','跟踪参考报价、同窗对照和规则实验。',
    `<button class="btn" data-action="refresh-outcomes">${icon('refresh')}核对到期结果</button><a class="btn small" href="#review?period=month">返回每月复盘 ↗</a>`)
    +riseFilter('review',catalog.rise_policy,riseType)
    +`<div class="review-explainer"><strong>${num(counts.cycles)} 个独立周期</strong>
      <p>${num(counts.reports)} 份报告 · ${num(counts.mentions)} 次提及 · ${num(counts.securities)} 只股票 · ${num(counts.theses)} 个论点。${esc(p.note)}</p></div>
    <div class="review-metric-grid">${p.groups.map(g=>`<article class="panel">
      <span>${g.horizon} · ${g.kind==='original'?'原判':'去留'}</span><strong>${g.hit_rate==null?'暂无可评样本':percent(g.hit_rate)}</strong>
      <p>全部 ${g.total} · 到期 ${g.due} · 可评 ${g.evaluable}</p><small>评价覆盖 ${g.coverage==null?'—':percent(g.coverage)}</small>
      ${g.paired!=null?`<small class="archive-created">同窗配对 ${num(g.paired)} · 平均超额 ${percent(g.mean_excess)}</small>`:''}
      ${Object.entries(g.statuses).map(([k,n])=>`<div class="outcome-row"><span>${esc(outcomeNames[k]||k)}</span><b>${n}</b></div>`).join('')}</article>`).join('')}</div>`
    +(riseType?`<div class="section-heading"><a class="btn small" href="#tracking?rise_type=${encodeURIComponent(riseType)}">查看这一类的股票库记录 ↗</a></div>`
      :controlComparison(controls)+growthOverview(experiments)+`<div class="section-heading"><h2>回到当时的研究</h2><p>失败、停止、受限研究同样保留</p></div>`+archiveLedger(r.items));
}
document.addEventListener('click',async e=>{
  const el=e.target.closest('[data-action]');if(!el||el.disabled)return;const a=el.dataset.action,id=el.dataset.id;
  if(a==='research-session'){
    if(!e.ctrlKey&&!e.metaKey&&!e.shiftKey&&!e.altKey&&el.hash!==location.hash){
      main.querySelector('.session-tab[aria-busy]')?.removeAttribute('aria-busy');
      el.setAttribute('aria-busy','true');
    }
    return;
  }
  try{
    if(a==='new-research')return await newResearch();
    if(a==='usage-page'){location.hash='ai?tab=usage';return;}
    if(a==='review-cycle')return await newResearch([id]);
    if(a==='stock-research'){
      if(el.getAttribute('aria-busy')==='true')return;
      el.setAttribute('aria-busy','true');
      try{await showStockResearch(id,el.dataset.run);}
      finally{el.removeAttribute('aria-busy');}
      return;
    }
    if(a==='structure-replay')return await showStructureReplay(id,el.dataset.run);
    if(a==='full-report')return await fullResearchReport();
    if(a==='cycle-detail')return await showCycle(id);
    if(a==='research-reconcile'){
      el.disabled=true;
      const result=await send('/api/v2/research/runs/'+id+'/reconcile',{});
      toast(result.message||({published:'已取回原结果并核验签发',pending:'原请求仍在处理，稍后自动核对',unknown:'原请求尚未取得终态',failed:'原请求已核实失败，回执已保留',invalid:'原结果未通过签发核验',unqueryable:'原通道没有可查询句柄，未知回执已保留'}[result.state]||'原请求状态已核对'));
      return await render();
    }
    if(a==='research-stop'){el.disabled=true;await send('/api/v2/research/runs/'+id+'/stop',{});toast('停止已登记；已发请求的真实终态会保留');return await render();}
    if(a==='refresh-outcomes'){el.disabled=true;await send('/api/v2/performance/refresh',{});return await render();}
    if(a==='scan-opportunities'){el.disabled=true;el.textContent='正在读取截点证据…';const b=researchState.preview=await api('/api/v2/opportunities?session='+encodeURIComponent(researchState.session));openModal(dialog('实际机会线索',b.market.market_day+' · '+sessionLabels[researchState.session],`<p>${esc(b.market.scope)}</p><p>召回 ${num(b.market.discovered)} 只，本轮显示 ${b.candidates.length} 只。${esc(b.market.ordering)}</p><div class="research-stock-grid">${b.candidates.map(c=>stockResearchCard(c,null,'')).join('')}</div>`));return;}
  }catch(error){toast(error.message,true);}finally{if(el.isConnected)el.disabled=false;}
});
function researchRequest(form){
  const f=new FormData(form),symbols=String(f.get('symbols')||'').split(/[,，\s]+/).filter(Boolean);
  const body={idempotency_key:researchState.requestKey,intent:f.get('intent'),symbols,kind:researchState.reviewIds?'review':'discovery',cycle_ids:researchState.reviewIds||[],session:f.get('session')||null,acknowledge_unknown:f.has('acknowledge_unknown'),allow_evidence_round:f.has('allow_evidence_round')};
  if(f.has('session'))Object.assign(body,{market_day:f.get('market_day')||null,use_preferences:f.has('use_preferences')});
  if(f.get('rise_type'))body.rise_type=f.get('rise_type');
  return body;
}
document.addEventListener('submit',async e=>{
  if(e.target.id!=='research-form')return;e.preventDefault();const form=e.target,button=form.querySelector('[type=submit]');if(button.disabled)return;button.disabled=true;
  const body=researchRequest(form);
  try{const r=await send('/api/v2/research/runs',body);closeModal();researchState.session=r.data.session;if(location.hash!==`#research?run=${r.id}`)location.hash=`research?run=${r.id}`;else await render();}catch(error){form.querySelector('#form-error').textContent=error.message;}finally{button.disabled=false;}
});
