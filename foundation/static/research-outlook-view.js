'use strict';

const scenarioWindows={PM:'当日下午',D1:'后 1 个交易日',D3:'后 3 个交易日'};
const measuredNames={return_1:'当日涨跌',return_5:'近五日涨跌',relative_volume20:'相对量',
  close:'截点价格',amount:'成交额',financial_original:'原文金额'};
const measuredUnits={ratio:'ratio',times:'times',CNY:'cny',CNY_per_share:'price'};

const reportPhrases=[
  ['未给独立触发价','原稿没有给出单独的确认价位'],['无独立触发价','原稿没有给出单独的确认价位'],
  ['主类为','按筛选规则归入'],['的突破承接','突破后是否仍有买盘支撑'],['突破承接','突破后有没有买盘支撑'],
  ['上方承接','之上有没有买盘支撑'],
  ['放量站上','成交量增加且价格站上'],['放量越过','成交量增加且价格超过'],['放量滞涨','成交量增加但股价涨不动'],
  ['缩量上涨','股价上涨但成交量偏少'],['量价推进较强','上涨时的成交表现也偏强'],
  ['量价突破条件未齐','股价和成交量还没有同时达到确认突破的条件'],
  ['量能','成交量'],['量价','股价与成交量'],['跑赢样本市场与行业','股价表现好于本轮市场样本和所属行业'],
  ['守住短中期均线','股价没有跌破短期和中期均价线'],['跑输市场与行业','表现比本轮市场样本和所属行业都弱'],
  ['尚未触发','还没达到原稿的确认条件'],
  ['原参与区间','原稿给出的参考买价范围'],['参与核价','确认买入前的可用报价'],
  ['核验、核价后再评','核实资料和可用报价后再判断'],['核价','确认可用报价'],
  ['准入未齐','买入前的条件还没核实齐'],['未闭环','还没核实完'],['补核后再评','补齐资料后再判断'],
  ['公司行动','分红、送股等公司事项'],['参与区间','参考买价范围'],['有效窗口','原定观察期限'],
  ['三季报营收单值的期间口径仍待核','还要查清三季报里的这项营业收入是单季度的，还是年初到三季度末累计的'],
  ['营收单值','单项营业收入数字'],['营收口径','营业收入的统计方式'],
  ['估值溢价缺兑现佐证','估值比同行高，经营结果还不足以支持这个差距'],
  ['估值溢价缺现金流佐证','估值比同行高，但还缺现金流证据'],
  ['经营兑现','实际经营成果'],['盈利兑现','实际赚到更多利润'],
  ['负PE不代表便宜','市盈率为负时，不能用它来判断股价是否便宜'],
  ['补财报、异动公告锚点及风险核验','补查财报和股价异常波动公告里的相关原文，再核实公司风险'],
  ['异动公告锚点','股价异常波动公告的原文段落'],['异动公告缺锚','股价异常波动公告还缺可核对的原文段落'],
  ['公告缺锚','公告还缺可核对的原文段落'],['财报缺锚','财报还缺可核对的原文段落'],
  ['公告锚点','可核对的公告原文段落'],['财报锚点','可核对的财报原文段落'],['营收锚点','可核对的营收原文段落'],
  ['结构上沿','原稿识别的股价区间上边界'],['旧结构区间','此前识别的股价区间'],
  ['结构区间','原稿识别的股价区间'],['上探回落反证','股价冲高后回落的信号'],
  ['完整日线收盘','当天正式收盘'],['正式买入入选','正式买入结果'],
  ['偏好内可研究','符合当时筛选偏好的有'],['冻结候选','本轮确定的候选股'],['深读','逐只研究'],
  ['known_at','当时能知道这条信息的时间'],['plot_day','信号在图上对应的日期'],
  ['；','。'],
];

function readableReportText(run,value,dimensions=[]) {
  let text=researchText(run,value)
    .replace(/主类为(突破上涨|强势上涨|即将上涨|刚开始上涨（持续性）)/g,'筛选时被列为「$1」线索')
    .replace(/仅观察\s*(\d+(?:\.\d+)?)\s*(?:元)?上方承接/g,'先看股价在 $1 元之上时有没有买盘支撑')
    .replace(/收盘(?:价)?(?:低于|失守)\s*(\d+(?:\.\d+)?)(?![\d.])(?:\s*元)?/g,'收盘价低于 $1 元');
  for(const [phrase,plain] of reportPhrases)text=text.replaceAll(phrase,plain);
  const names=new Map(dimensions.map(item=>[item.id,item.name]));
  for(const candidate of run?.data?.bundle?.candidates||[]){
    const industry=candidate.industry?.match(/^([A-Z]\d{2})\s*(.+)$/);
    if(industry)names.set(industry[1],industry[2]);
  }
  return text.replace(/\b(?:PM|D1|D3)\b/g,key=>scenarioWindows[key])
    .replace(/\b[A-Z]\d{2}\b/g,id=>names.get(id)||id)
    .replace(/\bPE\b/gi,'市盈率')
    .replace(/^核(?=[\p{Script=Han}])(?![对验实查])/u,'核对');
}

function reportStatementSources(statement) {
  const facts=[...new Set(statement.fact_ids||[])];
  if(!facts.length)return '';
  const numbers=(statement.measurements||[]).map(item=>`<div><dt>${esc(measuredNames[item.metric]||'核对数值')}</dt>
    <dd>${formatDimensionReading({value:item.value,unit:measuredUnits[item.unit]})}</dd></div>`).join('');
  return `<details class="reason-sources"><summary>核对 ${facts.length} 条引用${numbers?'与数值':''}</summary>
    ${numbers?`<dl class="dimension-readings">${numbers}</dl>`:''}${evidenceButtons(facts)}</details>`;
}

function reportMeasurementNotes(statement,candidate) {
  const intraday=candidate.metrics.partial_bar;
  const volumeBasis=intraday?'过去同一时刻的平均成交量':'此前 20 个交易日的平均成交量';
  const explain={
    return_1:intraday?'截至研究时点，股价当天':'研究日收盘价',
    return_5:'和五个交易日前相比，股价',
  };
  const metrics=new Map((statement.measurements||[]).filter(item=>item.symbol===candidate.symbol)
    .map(item=>[item.metric,item]));
  return ['return_1','return_5','relative_volume20'].map(metric=>{
    const item=metrics.get(metric);
    if(!item)return '';
    if(metric==='relative_volume20')return `<li>${intraday?'当时累计':'当天'}成交量是${volumeBasis}的
      <strong>${formatDimensionReading({value:item.value,unit:'times'})}</strong>。</li>`;
    const change=item.value===0?'持平':`${item.value>0?'上涨':'下跌'} <strong>${percent(Math.abs(item.value)).slice(1)}</strong>`;
    return `<li>${explain[metric]}${change}。</li>`;
  }).join('');
}

function stockDecisionReason(run,candidate,decision) {
  const reason=(title,items)=>`<div class="report-reason"><h4>${title}</h4>${items.map(item=>{
    const numbers=reportMeasurementNotes(item,candidate);
    return `<div><p>${esc(readableReportText(run,item.text,candidate.dimensions))}</p>
      ${numbers?`<ul class="report-numbers">${numbers}</ul>`:''}${reportStatementSources(item)}</div>`;
  }).join('')}</div>`;
  return reason('报告看到了哪些有利迹象',decision.supports)+
    reason(decision.action==='reject'?'为什么本轮不选':'为什么还要谨慎',decision.counterarguments);
}

function scenarioRange(range, count) {
  if(!range)return `<span class="muted">样本不足 · ${num(count)} 个</span>`;
  return `<strong>${percent(range.return_low)} ～ ${percent(range.return_high)}</strong>
    <small>¥${risePrice(range.price_low)} ～ ¥${risePrice(range.price_high)} · ${num(range.samples)} 个</small>`;
}

function priceScenarioDetails(reference, source, decision, dates={}) {
  if(!reference)return '';
  const view=decision?.outlook;
  const direction=(key,title)=>{
    const item=view?.[key];
    if(!item)return '';
    const date=dates[item.horizon];
    const range=reference.horizons[item.horizon]?.[key];
    return `<article class="reason-card"><h4>${title}</h4>
      <small>${esc(scenarioWindows[item.horizon])} ${date?'· '+esc(date):''}</small>
      <p><b>观察信号</b> ${esc(item.condition)}</p><p><b>判断依据</b> ${esc(item.text)}</p>
      ${item.use_history&&range?`<p><b>AI 参考的历史幅度</b> ${percent(range.return_low)} ～ ${percent(range.return_high)}</p>`:''}
      ${reportStatementSources(item)}</article>`;
  };
  const prior=!view&&decision?.relations?.[0];
  const history=Object.entries(scenarioWindows).filter(([key])=>reference.horizons[key]).map(([key,label])=>{
    const row=reference.horizons[key];
    return `<tr><th scope="row">${label}<small>${dates[key]?esc(dates[key])+' · ':''}${num(row.samples)} 个可比样本</small></th>
      <td>${scenarioRange(row.upside,row.rising)}</td><td>${scenarioRange(row.downside,row.falling)}</td></tr>`;
  }).join('');
  const basis=source==='archive_calculation'?'按原稿冻结行情复算':source==='sent'?'本轮研究冻结的幅度参考':'从所选截点的本地行情计算';
  return `<section class="detail-layer stock-outlook" aria-label="涨跌空间和转弱信号">
    <h3>涨跌空间和转弱信号</h3>
    <p class="scenario-basis">${esc(basis)}${reference.reference_price!=null?` · ${esc(reference.as_of?.slice(0,16).replace('T',' '))} 参考价 ¥${risePrice(reference.reference_price)}`:''}</p>
    ${view?`<div class="argument-columns">${direction('upside','什么情况支持上涨')}${direction('downside','什么情况开始转弱')}</div>
      ${view.gap?`<p class="scenario-gap">${esc(view.gap)}</p>`:''}`
      :prior?`<div class="argument-columns scenario-archive"><article class="reason-card"><h4>原稿的上涨条件</h4><p>${esc(prior.condition)}</p></article>
        <article class="reason-card"><h4>原稿的反证或转弱信号</h4><p>${esc(prior.counterexample)}</p></article></div>`:''}
    ${view?.chase_risk&&!decision?.counterparty?`<article class="reason-card scenario-risk"><h4>这种走势要留意</h4><p>${esc(view.chase_risk.text)}</p>
      <p><b>怎么识别</b> ${esc(view.chase_risk.watch_for)}</p>${reportStatementSources(view.chase_risk)}</article>`:''}
    <div class="scenario-table"><table><thead><tr><th>观察窗口</th><th>上行样本参考</th><th>下行样本参考</th></tr></thead><tbody>${history}</tbody></table></div>
    <p class="form-note">${reference.horizons.PM?'午后同口径历史样本不足，按本轮条件观察。':
      (reference.history_start?`样本日期 ${esc(reference.history_start)} 至 ${esc(reference.history_end)}。`:'缺可用的完整日线样本。')+
      '幅度取各方向历史样本中间一半。区间用于条件情景对照。'}</p>
  </section>`;
}

function counterpartyDetails(reference, decision) {
  if(!reference&&!decision?.counterparty&&!decision?.counterparty_gap)return '';
  const fromAi=Boolean(decision?.counterparty);
  const view=decision?.counterparty||reference;
  const fields=view?.text?[['什么表象会吸引追涨',view.lure],['追价者在哪一步容易被套',view.text],
    ['用什么信号识别',view.watch_for],['什么会推翻这个假设',view.counterexample]]:[];
  return `<section class="detail-layer counterparty-report" aria-label="大资金视角的反向推演">
    <h3>大资金视角的反向推演</h3>
    <p class="form-note">${fromAi?'AI 原稿的假设情景':'按本轮冻结形态推演的假设情景'}</p>
    ${fields.length?`<div class="counterparty-grid">${fields.map(([label,value])=>`
      <article class="reason-card"><h4>${label}</h4><p>${esc(value)}</p></article>`).join('')}</div>${reportStatementSources(view)}`
      :`<p>${esc(view?.gap||decision?.counterparty_gap||'本轮缺少可核对的反向情景。')}</p>`}
    ${fields.length&&decision?.counterparty_gap?`<p class="form-note">AI 原稿列出的资料缺口 ${esc(decision.counterparty_gap)}</p>`:''}
  </section>`;
}

function stockAiContribution(run, candidate, decision) {
  const names=dimensionTitles([...new Set(decision.relations.flatMap(item=>item.dimensions))],candidate.dimensions);
  const sources=[...decision.supports,...decision.counterarguments,...decision.relations,
    ...[...Object.values(decision.outlook||{}),decision.rating,decision.counterparty].filter(item=>Array.isArray(item?.fact_ids))];
  const cited=new Set(sources.flatMap(item=>item.fact_ids));
  const missing=candidate.dimensions.filter(item=>item.state==='missing').map(item=>item.name);
  const documents=candidate.documents?.length||0;
  const passages=candidate.originals?.anchors?.length||0;
  const proposal=adviceNames[adviceForAction[decision.requested_action||decision.action]];
  const final=decisionName(decision);
  return `<section class="detail-layer"><h3>这轮 AI 参与了什么</h3><div class="research-responsibility">
    <article><h4>系统先筛股票</h4><p>${esc(candidate.metrics.rise?.name||'本轮研究范围')}${candidate.paths.length?' · '+candidate.paths.map(pathName).map(esc).join('、'):''}。</p>
      <p>程序按本轮截点冻结行情、候选名单和证据。</p></article>
    <article><h4>AI 逐股分析</h4><p>送研资料包括行情、市场与行业摘要，最近 ${Math.min(5,candidate.bar_ids?.length||0)} 根行情，
      ${documents} 条公告／财务等资料摘要，${passages} 个原文段落。${run.data.dimension_evidence_version?'另含多维交叉核查摘要。':''}
      ${candidate.outlook_reference?'另含本轮涨跌幅度参考。':''}</p>
      <p>原稿给出 ${decision.supports.length} 条支持、${decision.counterarguments.length} 条反证、${decision.relations.length} 条组合论证，共引用 ${cited.size} 条不同事实。</p>
      ${names.length?`<p>组合实际涉及 ${names.map(esc).join('、')}。</p>`:''}
      ${decision.rating?'<p>原稿同时记录了 AI 星级与评分理由。</p>':''}
      ${decision.counterparty?'<p>反向推演来自这次 AI 研究，并带有可核对的事实。</p>':''}
      ${missing.length?`<p>本轮缺资料 ${missing.map(esc).join('、')}。</p>`:''}</article>
    <article><h4>系统核对结论</h4><p>AI 原判断 ${esc(proposal)} · ${decision.withdrawn?'当前状态':'本轮结果'} ${esc(final)}。</p>
      ${decision.formal_blockers.length?`<p>待核 ${decision.formal_blockers.map(reason=>esc(buyCheckName(reason))).join('、')}。</p>`
        :'<p>页面展示本轮买入判断和原定条件。</p>'}</article>
  </div></section>`;
}
