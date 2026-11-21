'use strict';

const recapWindows={PM:'当日下午',D1:'第 1 个交易日',D3:'第 3 个交易日'};
const recapDirections={strong:'上涨较广',mixed:'涨跌方向不一致',weak:'下跌较广'};
const recapTone=value=>value==null?'flat':value>0?'up':value<0?'down':'flat';
const recapRatio=value=>value==null?'—':`${(value*100).toFixed(2)}%`;

function reviewHref(entries={}){
  const params=new URLSearchParams(Object.entries(entries).filter(([,value])=>value!=null&&value!==''));
  return '#review'+(params.size?'?'+params:'');
}

function recapMast(period,value){
  const monthly=period==='month',caption=monthly?'检验三种研究窗口、复核本月行情、列出下月行动。':'收盘样本统计、下一交易日宽度情景、公告线索和股票去留。';
  const label=monthly?'复盘月份':'市场日期';
  return deskPageMast('04 / MARKET REVIEW','复盘',caption)+
    `<div class="recap-nav"><nav aria-label="复盘周期">
      <a href="#review" ${monthly?'':'aria-current="page"'}><span>01</span> 每日复盘</a>
      <a href="#review?period=month" ${monthly?'aria-current="page"':''}><span>02</span> 每月复盘</a>
      </nav><form id="recap-date-form" data-period="${period}">${inputField(label,'value',value,`type="${monthly?'month':'date'}" required`)}
      <button type="submit" class="btn small">查看</button></form></div>`;
}

function recapScheduleLine(schedule,kind,data){
  const plan=schedule[kind], archive=data.archive, monthly=kind==='monthly';
  const active=plan.effective_enabled;
  const when=monthly?`下月第 3 个交易日 ${plan.at} 起`:`交易日 ${plan.at} 起`;
  const saved=archive?(archive.late?'迟发回看':'定时已归档')+` · ${archiveStamp(archive.at)}`:'当前为即时复盘';
  const href=monthly?reviewHref({period:'month',month:plan.latest}):reviewHref({day:plan.latest});
  const latest=plan.latest&&plan.latest!==(monthly?data.month:data.day)?`<a href="${esc(href)}">上次归档 ${esc(plan.latest)} ↗</a>`:'';
  return `<div class="recap-schedule"><div>${chip(active?'本版本自动复盘已启用':plan.enabled?'本版本自动复盘已暂停':'共享计划未启用',active?'':'gray')} ${plan.enabled&&!active?'<small>已保存复盘计划，等待本机后台启用。</small>':''}
    <span>${esc(when)} · ${esc(saved)}</span>${latest}</div>
    <button class="btn small" type="button" data-action="recap-schedule">自动复盘设置</button></div>`;
}

function recapBreadth(current){
  if(!current)return '';
  const {up,down,flat,sample}=current;
  return `<div class="recap-breadth" role="img" aria-label="上涨 ${up} 只，下跌 ${down} 只，平盘 ${flat} 只">
    <div class="recap-breadth-bar">${[['up',up],['flat',flat],['down',down]].filter(([,n])=>n).map(([tone,n])=>
      `<i class="${tone}" style="flex:${n/sample}"></i>`).join('')}</div>
    <div class="recap-breadth-labels"><span>涨 <b>${num(up)}</b></span><span>平 <b>${num(flat)}</b></span><span>跌 <b>${num(down)}</b></span></div></div>`;
}

function dailyPanels(data){
  const {market,inference}=data;
  return `<div class="recap-split"><section class="recap-market magic-surface" data-glow>
    <div class="desk-index">MARKET CLOSE / ${esc(data.day)}</div><h2>${esc(data.scope_label||'已存股票样本')}</h2>
    <div class="recap-market-value ${recapTone(market?.move)}">${market?percent(market.move):'待收盘'}</div>
    <p>${market?`样本等权涨跌 · ${num(market.sample)} 只有效日线${market.amount!=null?` · 样本成交额 ${num(Math.round(market.amount/1e8))} 亿元`:''}`
      :data.latest_day?`已有日线截至 ${esc(data.latest_day)}`:'等待正式日线资料'}</p>
    ${recapBreadth(market)}
    <small>${esc(data.basis)}${data.index_data?'':' 主要指数行情待接入。'}</small>
    </section><section class="recap-next magic-surface" data-glow>
    <div class="desk-index">NEXT SESSION / ${esc(data.next_day||'待交易日历')}</div><h2>下一交易日宽度情景</h2>
    <div class="recap-next-verdict"><strong>${esc(inference.title)}</strong><span>样本宽度规则</span></div>
    ${inference.reason?`<p>${esc(inference.reason)}</p><dl><div><dt>明早确认</dt><dd>${esc(inference.confirm)}</dd></div>
      <div><dt>何时改判</dt><dd>${esc(inference.invalidate)}</dd></div></dl>`:
      '<p>取得所选日正式收盘和下一交易日后，再给出情景及确认条件。</p>'}
    <small>${esc(inference.limitation||'仅按样本上涨比例和等权涨跌生成规则情景；未综合主要指数、资金和事件，不代表经验证的次日预测。')}</small></section></div>`;
}

function groupRecap(title,rows,caption){
  return `<section class="panel recap-groups"><header><h3>${title}</h3><p>${caption}</p></header>
    ${rows.length?`<div class="table-wrap"><table><thead><tr><th scope="col">名称</th><th scope="col">等权涨跌</th><th scope="col">上涨 / 样本</th><th scope="col">相对样本</th><th scope="col">当日表现与观察</th></tr></thead>
    <tbody>${rows.map(row=>`<tr><th scope="row">${esc(row.name)}</th><td class="recap-number ${recapTone(row.move)}">${percent(row.move)}</td>
      <td>${num(row.up)} / ${num(row.sample)}</td><td>${row.relative==null?'未记录':`${row.relative>=0?'强':'弱'} ${Math.abs(row.relative*100).toFixed(2)} 个百分点`}</td>
      <td><span class="recap-direction ${row.direction}">${esc(recapDirections[row.direction])}</span>
      <small>${esc(row.observation||'历史记录未保存观察说明。')}</small></td></tr>`).join('')}</tbody></table></div>`:
      '<p class="recap-empty-note">等待可核对的归属和行情。</p>'}</section>`;
}

function recapNews(page,day,before,archive,runId){
  const href=cursor=>reviewHref({day,run:runId,news:cursor});
  const note=archive?`归档时 ${num(archive.news_total)} 条${archive.news_status==='completed'?'':'，晚间公告回执未完整'}。下方可查看当前已存公告。`
    :'按已存公告标题筛选，点开核对原始来源。';
  return `<section class="panel recap-news"><header><div><h2>潜在利好线索</h2><p>${esc(note)}</p></div>
    <span>${num(page.total)} 条</span></header>
    ${page.items.length?`<div class="recap-news-list">${page.items.map(item=>`<article>
      <span>${esc(item.kind)}</span><div><h3>${esc(item.title)}</h3><small>${esc(item.symbol)} · ${esc(archiveStamp(item.published))}</small></div>
      <button type="button" class="btn small" data-action="record" data-id="${item.id}">看来源 ↗</button></article>`).join('')}</div>`:
      '<p class="recap-empty-note">当前已存公告中无符合筛选的标题。</p>'}
    ${page.policies.length?`<details class="recap-policy-list"><summary>当日政策原文</summary>${page.policies.map(item=>
      `<button class="btn small" data-action="record" data-id="${item.id}">${esc(item.title)} ↗</button>`).join('')}</details>`:''}
    ${before||page.next?`<nav class="table-foot" aria-label="利好线索分页"><span>每页 8 条</span><div>
      ${before?`<a class="btn small" href="${esc(href(''))}">第一页</a>`:''}
      ${page.next?`<a class="btn small" href="${esc(href(page.next))}">下一页</a>`:''}</div></nav>`:''}</section>`;
}

function monthChart(days){
  if(!days.length)return '<p class="recap-empty-note">本月暂无已存正式日线。</p>';
  const width=720,height=125,baseline=55,max=Math.max(.005,...days.map(row=>Math.abs(row.move))),step=width/days.length;
  return `<svg class="recap-month-chart" viewBox="0 0 ${width} ${height}" role="img"
    aria-label="本月 ${days.length} 个已存交易日的股票样本等权涨跌">
    <line x1="0" y1="${baseline}" x2="${width}" y2="${baseline}"></line>
    ${days.map((row,i)=>{const size=Math.max(1,Math.abs(row.move)/max*45),x=i*step+step*.19;
      return `<rect x="${x}" y="${row.move>=0?baseline-size:baseline}" width="${step*.62}" height="${size}" rx="2" class="${recapTone(row.move)}">
        <title>${esc(row.day)} · ${percent(row.move)} · 上涨 ${num(row.up)} 只</title></rect>`;}).join('')}
    </svg><div class="recap-chart-caption"><span>${esc(days[0].day)}</span><span>日等权涨跌 · 中线 0%</span><span>${esc(days.at(-1).day)}</span></div>`;
}

function monthSession(item,index){
  const keys=item.session==='noon'?['PM']:item.session==='tail'?['D1']:['D1','D3'];
  const panels=keys.map(horizon=>{
    const selected=item.outcomes.find(row=>row.horizon===horizon&&row.cohort==='selected');
    const watched=item.outcomes.find(row=>row.horizon===horizon&&row.cohort==='watch');
    const show=row=>row?`${num(row.up)} / ${num(row.scored)} 可评上涨，${num(row.total)} 个记录${row.missing?` · ${num(row.missing)} 个缺口`:''}`:'无新口径样本';
    return `<div class="recap-window"><strong>${recapWindows[horizon]}</strong><span>入选 · ${show(selected)}</span>
      <span>观察 · ${show(watched)}</span></div>`;
  }).join('');
  return `<article class="panel recap-session-card" data-glow><div class="desk-index">0${index+1} / ${esc(item.session)}</div>
    <h3>${esc(sessionLabels[item.session])}</h3><p>${esc(item.goal)}</p>
    <dl><div><dt>提交</dt><dd>${num(item.tasks)}</dd></div><div><dt>有效报告</dt><dd>${num(item.issued)}</dd></div>
      <div><dt>新口径前瞻</dt><dd>${num(item.targeted)}</dd></div></dl>
    <p class="recap-session-meta">可考虑买入 ${num(item.buy)} · 观察 ${num(item.watch)} · 资料不足 ${num(item.data_limited)} · 运行问题 ${num(item.failures)}${item.legacy?` · 旧口径 ${num(item.legacy)} 份`:''}</p>
    <p class="recap-eval-note">${esc(item.evaluation)}</p>${panels}
    <div class="recap-fix"><strong>改进重点</strong><p>${esc(item.improvement)}</p></div></article>`;
}

async function renderMonthlyRecap(params){
  const month=params.get('month');
  const [data,schedule]=await Promise.all([api('/api/v2/recap/monthly'+(month?'?month='+encodeURIComponent(month):'')),api('/api/v2/recap/schedule')]);
  return recapMast('month',data.month)+recapScheduleLine(schedule,'monthly',data)+
    `<section class="recap-month-market magic-surface" data-glow><header><div class="desk-index">MARKET MONTH / ${esc(data.month)}</div>
      <h2>本月已存样本行情</h2><p>${data.partial?'本月至今':'已结束月份'}${data.through?' · 行情截至 '+esc(data.through):''} · 等权序列不代表指数或策略收益</p></header>
      <div class="recap-month-metrics"><div><span>等权序列累积</span><strong>${percent(data.equal_weight_path)}</strong></div>
      <div><span>日均上涨比例</span><strong>${recapRatio(data.mean_breadth)}</strong></div>
      <div><span>覆盖交易日 · 等权上涨 ${num(data.positive_days)} 日</span><strong>${num(data.market_days.length)} / ${num(data.expected_days)}</strong></div></div>
      ${monthChart(data.market_days)}</section>
      <div class="section-heading"><div><h2>三种分析分别复盘</h2><p>方向评价只使用新窗口可评样本，入选与观察分别展示。${data.archive?.pending?`归档时还有 ${num(data.archive.pending)} 个样本待评。`:''}</p></div>
        <a class="btn small" href="#review?period=audit">原判评估与方法实验 ↗</a></div>
      <div class="recap-session-grid">${data.sessions.map(monthSession).join('')}</div>
      <section class="recap-next-month"><div class="desk-index">NEXT MONTH / ${esc(data.next_month)}</div><h2>下月怎么做</h2>
        <p>${esc(data.next_step)}</p><p>研究改进按上方三个窗口分别验证，先补齐资料和同窗口样本。</p></section>`;
}

async function renderResearchReview(ticket){
  const route=location.hash;
  const params=new URLSearchParams(route.split('?')[1]||'');
  if(params.get('period')==='audit'||params.has('rise_type'))return renderOutcomeArchive();
  if(params.get('period')==='month')return renderMonthlyRecap(params);
  const day=params.get('day'),before=params.get('news');
  const linked=params.get('run');
  const [data,schedule,requestedRun]=await Promise.all([
    api('/api/v2/recap/daily'+(day?'?day='+encodeURIComponent(day):'')),api('/api/v2/recap/schedule'),
    linked?api('/api/v2/research/runs/'+encodeURIComponent(linked)):null]);
  const selected=requestedRun?.market_day===data.day?requestedRun:null;
  const run=selected||data.research_attempt||(data.research_note?.id?
    await api('/api/v2/research/runs/'+encodeURIComponent(data.research_note.id)+'?progress=true'):null);
  const session=run?.data?.session||run?.session||'close';
  const note=selected?.report&&session==='close'&&selected.kind==='discovery'&&!selected.data.withdrawn_at?
    {id:selected.id,text:selected.report.market_analysis}:data.research_note;
  const isLatest=data.day===data.latest_day;
  if(!selected)params.delete('run');
  params.set('day',data.day);
  const newsParams=new URLSearchParams({day:data.day});
  if(before)newsParams.set('before',before);
  const [news,pool,queue]=await Promise.all([api('/api/v2/recap/daily/news?'+newsParams),
    isLatest?api('/api/v2/pool-review?session=close'):null,isLatest?poolReviewQueue(params):null]);
  if(ticket!==pageRequest||route!==location.hash)return null;
  if(run&&(!data.archive||selected))startResearchPolling(run);
  const prior=data.archive?.pool;
  const poolReceipt=prior?`<p class="recap-history-note">归档时收盘去留 · 核对 ${num(prior.checked)} 只 · 待核 ${num(prior.pending)} 只 · 转观察待补证 ${num(prior.needs_evidence)} 只 · 移出 ${num(prior.removed)} 只</p>`:'';
  const attempt=data.research_attempt;
  const attemptState=attempt?.withdrawn_at?'已撤回':attempt?.published_at?'已签发':attempt?(researchStatus[attempt.status]||attempt.status):'没有研究任务记录';
  const contextLabel=data.archive&&data.research_context_frozen?'归档时':'当前';
  const researchReceipt=`<p class="recap-history-note">市场统计摘要${data.archive?'已归档':'为即时计算'}；${contextLabel} AI 收盘研究：${esc(attemptState)}${attempt?.stage?' · '+esc(attempt.stage):''}。
    ${data.research_context_note?esc(data.research_context_note):''}</p>`;
  const updated=data.current_research_context;
  const updatedAttempt=updated?.attempt;
  const updatedNote=updated?.note;
  const updateReceipt=updated?`<details class="panel recap-research-note"><summary>归档后的研究补充</summary>
    <p>${updatedAttempt?`${esc(researchStatus[updatedAttempt.status]||updatedAttempt.status)} · ${esc(updatedAttempt.stage)}`:'当前没有关联任务。'}</p>
    ${updatedNote?.text?`<p>${esc(updatedNote.text)}</p>`:''}
    ${updatedAttempt?.id?`<a class="btn small" href="${esc(researchHref('close',updatedAttempt.id))}">查看当前任务 ↗</a>`:''}</details>`:'';
  return recapMast('day',data.day)+recapScheduleLine(schedule,'daily',data)+
    (run?`<section class="research-context" aria-label="复盘关联的研究日期">
      ${researchDateLine(data.day,session,run.report?.published_at||run.published_at,selected?'所选研究':`${contextLabel}关联研究`)}
      <span class="research-flow-run">任务 ${esc(run.id)}</span></section>`:'')+
    researchFlow('review',run,data.day,session,Boolean(data.archive))+researchReceipt+dailyPanels(data)+
    `<div class="section-heading"><div><h2>板块与行业</h2><p>当日涨跌宽度、相对已存样本的表现与样本规模分别展示，后续方向等待确认。</p></div></div>
      <div class="recap-group-grid">${groupRecap('上市板块',data.boards,'按已存证券名录分组。')}
      ${groupRecap('行业强弱',data.industries,'按证监会行业分类展示强弱两端。')}</div>`+
    recapNews(news,data.day,before,data.archive,selected?.id)+
    (note?.text?`<details class="panel recap-research-note"><summary>${selected?'所选':data.archive&&data.research_context_frozen?'归档关联':'当前补充'}收盘原稿的市场分析</summary>
      <p>${esc(note.text)}</p><a class="btn small" href="${esc(researchHref('close',note.id))}">打开收盘原稿 ↗</a></details>`:'')+updateReceipt+poolReceipt+
    (pool?poolReviewStrip(pool)+queue:
      '<p class="recap-history-note">正在查看所选日期的市场复盘。 <a class="btn small" href="#review">查看最新收盘去留 ↗</a></p>');
}

document.addEventListener('click',async event=>{
  const button=event.target.closest('[data-action="recap-schedule"]');
  if(!button)return;
  button.disabled=true;
  try{
    const {daily,monthly}=await api('/api/v2/recap/schedule');
    const body=sharedScheduleNote()+checkLabel(`交易日 ${daily.at} 起归档每日复盘`,'daily',daily.enabled)+
      checkLabel(`下月第 3 个交易日 ${monthly.at} 起归档每月复盘`,'monthly',monthly.enabled)+
      '<p class="form-note">上海时间。每日等待当日正式日线，晚间公告或收盘研究仍在处理时可等到 21:00。月报留出第 3 个交易日核对月末选股。复盘用已存资料生成，不另发模型请求。</p>';
    openModal(dialog('自动复盘设置','归档保留计算摘要和实际出报时间',body,saveButtons('保存复盘计划'),'recap-schedule-form'));
  }catch(error){toast(error.message,true);}finally{button.disabled=false;}
});

document.addEventListener('submit',async event=>{
  const form=event.target;
  if(form.id==='recap-schedule-form'){
    event.preventDefault();
    const fields=new FormData(form),button=form.querySelector('[type=submit]');
    if(button.disabled)return;
    button.disabled=true;
    try{
      await send('/api/v2/recap/schedule',{daily:fields.has('daily'),monthly:fields.has('monthly')},'PUT');
      closeModal();toast('已保存本机复盘计划');await render();
    }catch(error){form.querySelector('#form-error').textContent=error.message;}finally{button.disabled=false;}
    return;
  }
  if(form.id!=='recap-date-form')return;
  event.preventDefault();
  const value=String(new FormData(form).get('value')||'');
  if(!value)return;
  const target=reviewHref(form.dataset.period==='month'?{period:'month',month:value}:{day:value});
  if(location.hash===target)render();else location.hash=target;
});

const allowGlow=window.matchMedia('(hover:hover) and (pointer:fine) and (prefers-reduced-motion:no-preference)');
document.addEventListener('pointermove',event=>{
  if(!allowGlow.matches)return;
  const surface=event.target.closest('[data-glow]');
  if(!surface)return;
  const rect=surface.getBoundingClientRect();
  surface.style.setProperty('--glow-x',`${event.clientX-rect.left}px`);
  surface.style.setProperty('--glow-y',`${event.clientY-rect.top}px`);
},{passive:true});
