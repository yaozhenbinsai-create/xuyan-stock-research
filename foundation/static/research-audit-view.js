'use strict';

const archiveStamp = value => value ? value.slice(0,16).replace('T',' ') : '—';
const researchParticipating = market => market?.participating ?? market?.coverage_detail?.states.find(item=>item.id==='covered')?.count ?? market?.covered;

function archiveLedger(items){
  if(!items.length)return empty('当前没有研究记录','可以调整日期或时段。');
  const dates=new Map();
  for(const row of items){
    if(!dates.has(row.market_day))dates.set(row.market_day,[]);
    dates.get(row.market_day).push(row);
  }
  return `<section class="archive-ledger" aria-label="按市场日排列的研究记录">${[...dates].map(([day,rows])=>`
    <section class="archive-day"><header><span class="desk-index">MARKET DATE</span><h3>${esc(day?.slice(5).replace('-','.')||'待定')}</h3>
      <span>${esc(day||'')}</span></header><div class="archive-entry-list">${rows.map(row=>`
      <article class="archive-entry"><div><small>${esc(sessionLabels[row.session]||'收盘')} · ${row.kind==='review'?'增量复核':'选股研究'}</small>
      <h3>${esc(row.title||row.intent?.slice(0,80)||'研究任务')}</h3></div>
      <div class="archive-when"><span>${row.published_at?'签发':'创建'}</span><time>${esc(archiveStamp(row.published_at||row.created))}</time></div>
      ${chip(row.withdrawn_at?'已撤回':researchStatus[row.status]||row.status,row.withdrawn_at||!row.published_at?'amber':'gray')}
      <a class="btn small" href="#research?run=${esc(row.id)}">${row.published_at?'打开原稿':'查看任务'} ↗</a></article>`).join('')}</div></section>`).join('')}</section>`;
}

function auditStages(market, pointed) {
  const stages = [
    ['进入初筛',market.scope_size],
    ['按偏好筛选后可研究',researchParticipating(market)],
    [pointed?'指定范围中可研究':'触发机会线索',market.discovered],
    ['进入本轮候选',market.displayed],
  ];
  return `<div class="audit-stages">${stages.map(([label,value])=>`<div><span>${label}</span><strong>${num(value)}</strong></div>`).join('')}</div>`;
}

function researchAuditSummary(run, choices) {
  const market = run?.data?.bundle?.market;
  if (!market) return '';
  const issued = run.report;
  const waiting = choices.filter(d=>adviceForAction[d.action]==='wait'&&!d.withdrawn&&!d.review_event);
  const blocks = [...new Set(waiting.flatMap(d=>d.formal_blockers||[]).map(buyCheckName))];
  const pointed = Boolean(run.data.symbols?.length);
  const waitingForReport=['queued','running','waiting'].includes(run.status);
  return `<section class="panel audit-summary" aria-label="本轮筛选过程">
    <div class="structure-heading"><div><h2>本轮筛选过程</h2><p>${esc(market.market_day)} · 覆盖样本中 ${num(market.advancing)} 只上涨、${num(market.declining)} 只下跌</p></div>
      <button class="btn small" data-action="screening-audit" data-run="${esc(run.id)}">查看筛选明细 ↗</button></div>
    ${auditStages(market,pointed)}
    ${run.data.withdrawn_at?'<p class="form-note">原稿已撤回。筛选记录供历史核对。</p>':issued?blocks.length?`
      <div class="audit-verdict"><strong>为什么先等等</strong>
      <ul class="audit-blocks">${blocks.map(reason=>`<li>${esc(reason)}</li>`).join('')}</ul></div>`:''
      :`<p class="form-note">${waitingForReport?'筛选记录已冻结，等待 AI 研判。':'本轮已结束，冻结的筛选记录可继续核对。'}</p>`}
    <div class="heading-actions"><a class="btn ghost small" href="#archive">历史报告 ↗</a></div>
  </section>`;
}

async function showScreeningAudit(runId, filters={}) {
  const route=location.hash;
  const stage = filters.stage||'all';
  const q = filters.q||'';
  const path = filters.path||'';
  const riseType = filters.riseType||'';
  const after = Number(filters.after)||0;
  const params = new URLSearchParams({stage,q,after});
  if (path) params.set('path',path);
  if (riseType) params.set('rise_type',riseType);
  const result = await api('/api/v2/research/runs/'+encodeURIComponent(runId)+'/screening?'+params);
  if(route!==location.hash)return;
  const market = result.market;
  if (!market) {
    openModal(dialog('筛选明细','本轮尚未冻结候选',empty('还没有可查看的筛选结果','可在任务记录查看停在哪个阶段。')));
    return;
  }
  const basis = {
    frozen:'读取本轮已冻结的筛选记录。',
    matched_replay:'按原稿参数复算。原稿的覆盖、规则统计和送审名单指纹核对一致。',
    selected_only:'这份早期原稿保留了本轮候选和汇总。可核实的逐股明细以已送研究的名单为准。'
  };
  const session=market.session||'close';
  const controls = `<form id="screening-audit-form" class="audit-filters" data-run="${esc(runId)}">
    ${inputField('股票名称或代码','q',q,'maxlength="40" placeholder="例如 中直股份 或 600038"')}
    ${selectField('筛选阶段','stage',stage,[['all','全部可查线索'],['selected','进入本轮候选'],['not_selected','命中但未进入候选']])}
    ${selectField('命中线索','path',path,[['','不限'],...Object.keys(market.path_counts||{}).map(id=>[id,pathName(id)])])}
    ${result.rise_policy?selectField('上涨类别','riseType',riseType,[['','不限'],...riseOptions(result.rise_policy)]):''}
    <button class="btn primary small" type="submit">筛选</button></form>`;
  const table = result.items.length?`<div class="table-wrap"><table>
    <thead><tr><th>股票</th><th>命中线索</th><th>本截点涨跌</th><th>相对量</th><th>筛选结果</th></tr></thead>
    <tbody>${result.items.map(row=>`<tr><td><strong>${esc(row.name)}</strong><small class="archive-created">${esc(row.symbol)}</small></td>
      <td>${row.rise_type?`<strong>${esc(riseName(row.rise_type,result.rise_policy))}</strong>`:''}
        <small class="archive-created">${row.paths.map(id=>esc(pathName(id))).join(' · ')||(!row.rise_type?'自主指定':'')}</small></td>
      <td class="numeric">${percent(row.move)}</td><td class="numeric">${row.relative_volume==null?'—':row.relative_volume.toFixed(2)+'×'}</td>
      <td>${row.selected?`<button class="btn small" data-action="stock-research" data-id="${esc(row.symbol)}" data-run="${esc(runId)}">查看逐股依据</button>`
      :'<span>触发规则 · 超出本轮候选数</span>'}</td></tr>`).join('')}</tbody></table></div>`
    :result.match?`<div class="notice"><p>${esc(result.match.symbol)} · ${esc(result.match.detail)}</p></div>`
    :empty('当前条件下没有明细','可调整筛选条件，或在覆盖与排除中核对范围。');
  const button=(label,cursor)=>`<button class="btn small" data-action="screening-audit" data-run="${esc(runId)}"
    data-q="${esc(q)}" data-stage="${esc(stage)}" data-path="${esc(path)}" data-rise-type="${esc(riseType)}" data-after="${cursor}">${label}</button>`;
  const footer=`<button class="btn small" data-action="research-coverage" data-run="${esc(runId)}">覆盖与排除</button>
    <a class="btn primary small" href="${esc(methodsHref(session,runId))}" data-action="close">进入逐股核对 ↗</a>
    ${after?button('回到第一页',0):''}${result.next!=null?button('下一页',result.next):''}`;
  openReading(dialog('筛选明细',market.market_day+' · '+sessionLabels[session]+' · 任务 '+runId,
    auditStages(market,result.pointed)+`<p class="form-note">${esc(basis[result.basis])} ${esc(market.ordering||'')}</p>`+controls+table,footer),
    {replace:Boolean(modal.open&&modalContent.querySelector('#screening-audit-form'))});
}

const archiveHref = (params, cursor='') => {
  const query = new URLSearchParams(params);
  query.delete('before');
  if (cursor) query.set('before',cursor);
  return '#archive'+(query.size?'?'+query:'');
};

async function renderResearchArchive() {
  const current = new URLSearchParams(location.hash.split('?')[1]||'');
  const outcome = current.get('outcome')||'published';
  const session = current.get('session')||'';
  const start = current.get('start')||'';
  const end = current.get('end')||'';
  const before = current.get('before')||'';
  const args = new URLSearchParams({limit:25});
  if (outcome!=='all') args.set('outcome',outcome);
  for (const [key,value] of Object.entries({session,start,end,before})) if (value) args.set(key,value);
  const data = await api('/api/v2/research/runs?'+args);
  const fields=`<form id="research-archive-filter" class="panel audit-filters">
    ${selectField('记录类型','outcome',outcome,[['published','已签发报告'],['unpublished','未出报告的任务'],['all','全部记录']])}
    ${selectField('研究时段','session',session,[['','全部时段'],['noon','午盘'],['tail','尾盘'],['close','收盘']])}
    ${inputField('市场日开始','start',start,'type="date"')}${inputField('市场日结束','end',end,'type="date"')}
    <button class="btn primary small" type="submit">查找</button></form>`;
  return deskPageMast('05 / RESEARCH ARCHIVE','历史报告','沿市场日回看原稿、筛选过程与逐股证据。')
    +fields+archiveLedger(data.items)+`<div class="table-foot"><span>匹配 ${num(data.total)} 条 · 每页 25 条</span><div>
    ${before?`<a class="btn small" href="${esc(archiveHref(current))}">回到第一页</a>`:''}
    ${data.next?`<a class="btn small" href="${esc(archiveHref(current,data.next))}">下一页</a>`:''}</div></div>`;
}

document.addEventListener('click',async event=>{
  const button=event.target.closest('[data-action="screening-audit"]');
  if(!button||button.disabled)return;
  button.disabled=true;
  try { await showScreeningAudit(button.dataset.run,button.dataset); }
  catch(error){toast(error.message,true);}
  finally {if(button.isConnected)button.disabled=false;}
});

document.addEventListener('submit',async event=>{
  const form=event.target;
  if(!['research-archive-filter','screening-audit-form'].includes(form.id))return;
  event.preventDefault();
  const fields=Object.fromEntries(new FormData(form));
  if(form.id==='research-archive-filter'){
    const params=new URLSearchParams(Object.entries(fields).filter(([,value])=>value));
    const target=archiveHref(params);
    if(location.hash===target)await render();
    else location.hash=target;
    return;
  }
  const controls=[...form.elements];
  controls.forEach(control=>control.disabled=true);
  try{await showScreeningAudit(form.dataset.run,fields);}
  catch(error){toast(error.message,true);}
  finally{if(form.isConnected)controls.forEach(control=>control.disabled=false);}
});
