'use strict';

const riseCaptions = {
  strong:'涨势与趋势同步',
  setup:'收窄缩量，等待过线',
  early:'启动后确认站稳',
  breakout:'越过区间并放量',
};
const riseStates = {awaiting_trigger:'等待触发',intraday:'盘中状态',observed:'收盘形态'};
const riseOptions = policy => (policy?.categories||[]).map(({id,name})=>[id,name]);
const riseName = (id, policy) => policy?.categories.find(item=>item.id===id)?.name||'';
const risePrice = value => value==null?'—':Number(value).toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:4});

function riseBoard(policy, session) {
  if(!policy)return '';
  return `<section class="rise-board tile tile-dark" aria-label="四类上涨机会">
    <header><div><span class="eyebrow">OPPORTUNITY SCREENER</span><h2>四类上涨机会</h2>
      <p>${esc(sessionLabels[session])}截点 · 按价量证据筛选 · 0 次模型调用</p></div>
      <button class="btn light" data-action="rise-screen" data-session="${esc(session)}">打开四类筛选 ↗</button></header>
    <div class="rise-tile-grid">${policy.categories.map(({id,name},index)=>`
      <button class="rise-tile" data-action="rise-screen" data-rise-type="${esc(id)}" data-session="${esc(session)}">
        <span class="rise-tile-index">${String(index+1).padStart(2,'0')} <span aria-hidden="true">↗</span></span>
        <strong>${esc(name)}</strong><small>${esc(riseCaptions[id])}</small>
      </button>`).join('')}</div>
  </section>`;
}

function riseMethods(policy) {
  if(!policy)return '';
  const order=policy.priority.map(id=>riseName(id,policy)).map(esc).join(' → ');
  return `<section class="rise-methods" aria-label="四类上涨的判定">
    <div class="section-heading"><div><h2>四类上涨的判定</h2><p>同股同截点归入一个主类，逐股依据保留其他命中类别。</p></div>
      <button class="btn small" data-action="rise-screen">查看真实筛选 ↗</button></div>
    <div class="rise-tile-grid">${policy.categories.map(({name,rule,invalidation})=>`
      <article class="panel rise-method-card"><h3>${esc(name)}</h3><p>${esc(rule)}</p>
        <small><strong>失效</strong> ${esc(invalidation)}</small></article>`).join('')}</div>
    <p class="form-note">重叠时优先归类为 ${order}。四类基于截止时点的数据筛选，入选还需 AI 研究与风险、核价校验。</p>
  </section>`;
}

function riseFilter(page, policy, selected) {
  const options=[['','全部周期'],...riseOptions(policy)];
  return `<nav class="rise-filter-links" aria-label="按原判上涨类别查看">
    ${options.map(([id,label])=>`<a class="btn small ${selected===id?'primary':''}" href="#${page}${id?'?rise_type='+encodeURIComponent(id):''}"
      ${selected===id?'aria-current="page"':''}>${esc(label)}</a>`).join('')}</nav>`;
}

function riseEvidence(signal, policy, references=true) {
  if(!signal)return '';
  const overlaps=(signal.matches||[]).filter(id=>id!==signal.type).map(id=>riseName(id,policy)).filter(Boolean);
  const riskLabels={
    extended_above_ma20:'偏离均线过远',
    blowoff_velocity:'五日涨幅过快',
    volume_climax:'成交量异常放大',
  };
  const riskDescs={
    extended_above_ma20:'收盘已超二十日均线 12% 以上，均值回归概率偏高，注意追高风险。',
    blowoff_velocity:'近五日涨幅超 10%，动能可能进入耗竭阶段，须有其他维度支撑。',
    volume_climax:'成交量超均量三倍以上，须区分确认性放量与潜在派发。',
  };
  const flags=(signal.risk_flags||[]);
  return `<div class="rise-evidence"><div class="rise-evidence-heading"><strong>${esc(signal.name)}</strong>
    ${chip(riseStates[signal.state]||'上涨形态',signal.state==='awaiting_trigger'?'amber':'gray')}</div>
    <p>${esc(signal.reason)}</p>
    <dl class="rise-levels">
      ${signal.trigger_above==null?'':`<div><dt>触发条件</dt><dd>价格高于 ¥${risePrice(signal.trigger_above)}</dd></div>`}
      <div><dt>失效条件</dt><dd>收盘低于 ¥${risePrice(signal.invalidation_price)}</dd></div>
    </dl>
    ${flags.length?`<div class="rise-risk-flags">${flags.map(f=>`<span class="risk-flag" title="${esc(riskDescs[f]||f)}">⚠ ${esc(riskLabels[f]||f)}</span>`).join('')}</div>`:''}
    ${overlaps.length?`<p class="form-note">同时命中 ${overlaps.map(esc).join('、')}</p>`:''}
    ${references&&signal.facts?.length?`<details><summary>查看对应行情事实</summary>${evidenceButtons(signal.facts)}</details>`:''}
  </div>`;
}

function riseReportGroups(run, choices) {
  const bundle=run.data.bundle, policy=bundle.rise_policy, counts=bundle.market.rise_counts;
  const decisions=new Map(choices.map(d=>[d.symbol,d]));
  const groups=new Map([...policy.categories.map(({id})=>[id,[]]),['other',[]]]);
  for(const candidate of shownCandidates(run))groups.get(candidate.metrics.rise?.type||'other').push(candidate);
  const cards=items=>items.map(c=>stockResearchCard(c,decisions.get(c.symbol),run)).join('');
  return policy.categories.map(({id,name})=>{
    const items=groups.get(id);
    const advice=adviceCounts(items.map(c=>decisions.get(c.symbol)));
    const populated=Object.keys(advice).filter(key=>advice[key]>0);
    const scope=bundle.market.rise_scope, source=`行情线索 ${num(counts[id])} 只`;
    const summary=scope&&scope!==id?`${source} · 本轮未选此类别`
      :`${source} · 送研 ${num(items.length)} 只`;
    return `<section class="rise-result-group"><div class="section-heading"><div><h3>${esc(name)}</h3><p>${summary}</p></div>
      ${run.report&&populated.length?`<small>${adviceCountLine(advice,populated)}</small>`:''}</div>
      ${items.length?`<div class="research-stock-grid">${cards(items)}</div>`:''}</section>`;
  }).join('')+(groups.get('other').length?`<section class="rise-result-group">
    <div class="section-heading"><h3>其他研究线索</h3></div>
    <div class="research-stock-grid">${cards(groups.get('other'))}</div></section>`:'');
}

function riseScreenBody(filters,result,error='') {
  const policy=result?.rise_policy||researchState.dashboard?.catalog?.rise_policy;
  const {session,day,riseType,q,after}=filters;
  const nav=(label,cursor)=>`<button class="btn small" data-action="rise-screen" data-session="${esc(session)}"
    data-day="${esc(result.market_day)}" data-rise-type="${esc(riseType)}" data-q="${esc(q)}"
    data-after="${cursor}" data-input-hash="${esc(result.input_hash)}">${label}</button>`;
  const results=result?`
    <div class="rise-tile-grid rise-counts" aria-label="按类别筛选">
      ${policy.categories.map(({id,name})=>`<button data-action="rise-screen" data-session="${esc(session)}"
        data-day="${esc(result.market_day)}" data-rise-type="${esc(id)}" data-q="${esc(q)}"
        aria-pressed="${riseType===id}"><span>${esc(name)}</span><strong>${num(result.counts[id])}<small>只</small></strong></button>`).join('')}
    </div>
    <p class="form-note">${esc(result.market_day)} · ${esc(sessionLabels[session])} ${esc(result.cutoff.slice(11,16))} 截点
      · ${num(result.covered)} / ${num(result.scope_size)} 只有研究行情 · ${num(result.advancing)} 只上涨
      ${result.mode==='retrospective'?' · 历史回看':''}</p>
    <p class="form-note">以上为${result.checked!=null&&result.checked!==result.covered?`确认范围内 ${num(result.checked)} 只股票的`:'覆盖范围内的'}分类总数，不随名称或代码筛选缩小。</p>
    <p class="form-note rise-active-filter" role="status">当前名单：${esc(riseName(riseType,policy)||'四类全部')}${q?' · 名称或代码“'+esc(q)+'”':''} · 本页 ${num(result.items.length)} 只，每页最多 12 只。</p>`:'';
  const list=result?.items.length?`<div class="rise-rows">${result.items.map(row=>`
    <article class="rise-row"><header><div><strong>${esc(row.name)}</strong><small>${esc(row.symbol)}</small></div>
      <span class="numeric" aria-label="截点涨跌">${percent(row.move)}</span></header>
      ${riseEvidence(row.signal,policy)}
      <div class="rise-row-actions"><button class="btn small" data-action="dimension-preview" data-id="${esc(row.symbol)}"
        data-back="screen" data-session="${esc(session)}" data-day="${esc(result.market_day)}"
        data-rise-type="${esc(riseType)}" data-q="${esc(q)}" data-after="${after}" data-input-hash="${esc(result.input_hash)}">看多维分析 ↗</button></div></article>`).join('')}</div>`
    :result?empty('当前筛选条件下没有线索','可更换类别、日期或名称与代码。'):'';
  return dialog('四类上涨筛选','行情线索 · 0 次模型调用',
    `<form id="rise-screen-form" class="rise-filters">
      ${selectField('研究时段','session',session,Object.entries(sessionLabels))}
      ${inputField('市场日','day',result?.market_day||day,'type="date"')}
      ${selectField('上涨类别','riseType',riseType,[['','四类全部'],...riseOptions(policy)])}
      ${inputField('股票名称或代码','q',q,'maxlength="40" placeholder="例如 600011"')}
      <button class="btn primary small" type="submit">筛选</button>
    </form>${error?`<div class="notice amber" role="alert"><p>${esc(error)}</p></div>`:''}${results}${list}`,
    result?`<div class="rise-footer"><div>${after?nav('回到第一页',0):''}${result.next!=null?nav('下一页',result.next):''}</div>
      <button class="btn primary small" data-action="research-rise-type" data-rise-type="${esc(riseType)}"
        data-session="${esc(session)}" data-day="${esc(result.market_day)}">${riseType?'研究这一类':'研究四类线索'}</button></div>`:'');
}

async function showRiseScreen(options={}) {
  const filters={session:options.session||researchState.session,day:options.day||'',
    riseType:options.riseType||'',q:String(options.q||'').trim(),after:Number(options.after)||0};
  const params=new URLSearchParams({session:filters.session,limit:12});
  for(const key of ['day','q','after'])if(filters[key])params.set(key,filters[key]);
  if(filters.riseType)params.set('rise_type',filters.riseType);
  if(filters.after)params.set('input_hash',options.inputHash);
  const content=document.getElementById('modal-content');
  const pending=dialog('四类上涨筛选','从本机读取指定截点的行情',
    '<div id="rise-screen-loading" class="rise-loading" role="status"><span class="spinner"></span>正在计算筛选结果</div>');
  openReading(pending,{replace:Boolean(modal.open&&content.querySelector('#rise-screen-form'))});
  const loading=document.getElementById('rise-screen-loading');
  let result,error='';
  try{result=await api('/api/v2/opportunities/rises?'+params);}
  catch(failure){error=failure.message;}
  if(modal.open&&loading.isConnected){
    openReading(riseScreenBody(filters,result,error),{replace:true});
  }
}

document.addEventListener('click',async event=>{
  const button=event.target.closest('[data-action="rise-screen"],[data-action="research-rise-type"]');
  if(!button||button.disabled)return;
  try{
    if(button.dataset.action==='rise-screen'){
      const form=button.closest('#modal-content')?.querySelector('#rise-screen-form');
      const options=form&&button.closest('.rise-counts')
        ?{...Object.fromEntries(new FormData(form)),riseType:button.dataset.riseType,after:0}
        :button.dataset;
      await showRiseScreen(options);
    }else await newResearch(null,button.dataset);
  }catch(error){toast(error.message,true);}
});

document.addEventListener('submit',async event=>{
  if(event.target.id!=='rise-screen-form')return;
  event.preventDefault();
  await showRiseScreen(Object.fromEntries(new FormData(event.target)));
});
