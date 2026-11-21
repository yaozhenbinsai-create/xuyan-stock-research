'use strict';

const noticeStates = {
  not_configured:'站内可见', pending:'外部待确认发送', sending:'正在提交外部渠道',
  accepted:'外部渠道已受理', failed:'外部渠道返回失败', unknown:'外部终态未知'
};

function effectiveDecisions(run) {
  const hidden = new Set((run?.hidden_stocks||[]).map(item=>item.symbol));
  return (run?.report?.decisions||[]).filter(item=>!hidden.has(item.symbol));
}

function shownCandidates(run) {
  const hidden = new Set((run?.hidden_stocks||[]).map(item=>item.symbol));
  return (run?.data?.bundle?.candidates||[]).filter(item=>!hidden.has(item.symbol));
}

function researchText(run, value) {
  let safe=String(value??'');
  for(const {symbol,name} of run?.hidden_stocks||[]){
    for(const key of new Set([name,symbol,symbol.toUpperCase(),symbol.slice(3)]))if(key)safe=safe.replaceAll(key,'该股票');
  }
  return safe;
}

function correctionSummary(run, symbol=null) {
  const hidden = new Set((run?.hidden_stocks||[]).map(item=>item.symbol));
  const items=(run?.corrections||[]).filter(item=>!hidden.has(item.symbol)&&(!symbol||!item.symbol||item.symbol===symbol));
  if(!items.length)return '';
  return `<div class="notice amber"><div><strong>原稿后续更正</strong>
    ${items.map(item=>`<p>${esc(item.at.slice(0,16).replace('T',' '))} · ${esc(item.symbol||'整份报告')} · ${item.action==='withdraw'?'已撤回':'补充说明'}<br>${esc(researchText(run,item.reason))}</p>`).join('')}
    </div></div>`;
}

async function showResearchNotices(before=null) {
  const result=await api('/api/v2/research-notices'+(before?'?before='+encodeURIComponent(before):''));
  const items=result.items.map(row=>`<article class="notice-entry">
    <div class="structure-heading"><strong>${esc(row.data.title)}</strong>${chip(row.read_at?'已读':'未读',row.read_at?'gray':'amber')}</div>
    <p>${esc(row.created.slice(0,16).replace('T',' '))} · ${row.kind==='correction'?'原稿更正':'研究签发'} · ${esc(noticeStates[row.channel_state])}</p>
    ${row.data.reason?`<p>${esc(row.data.reason)}</p>`:''}
    <div class="heading-actions"><a class="btn small" href="#research?run=${esc(row.run_id)}" data-action="close">打开原稿 ↗</a>
      ${row.read_at?'':`<button class="btn small" data-action="read-research-notice" data-id="${row.id}" data-before="${before||''}">标为已读</button>`}
      ${result.external_configured&&['pending','failed','unknown'].includes(row.channel_state)?`<button class="btn small" data-action="send-research-notice" data-id="${row.id}" data-state="${esc(row.channel_state)}" data-before="${before||''}">${row.channel_state==='pending'?'发送至已配置渠道':'核对后重投'}</button>`:''}
    </div></article>`).join('');
  openModal(dialog('研究收件箱',`${num(result.unread)} 条未读 · 原稿和更正均按同一编号查看`,
    items||'<p>完成签发或原稿更正后，通知会出现在这里。</p>',
    `${before?'<button class="btn small" data-action="research-notices">回到最新</button>':''}
      ${result.next?`<button class="btn small" data-action="research-notices" data-before="${result.next}">下一页</button>`:''}`));
}

function editReportCorrection(run) {
  const symbols=[['','整份报告'],...effectiveDecisions(run).map(d=>[d.symbol,d.name+' · '+d.symbol])];
  const fields=`${selectField('影响对象','symbol','',symbols)}
    ${selectField('更正方式','action','note',[['note','补充说明'],['withdraw','撤回所选对象']])}
    <label class="field">更正理由<textarea name="reason" maxlength="800" required></textarea></label>
    <label class="check-label"><input type="checkbox" name="confirm" required>确认追加更正，并保留当时的原稿与评价。</label>`;
  openModal(dialog('追加原稿更正',researchText(run,run.report.title),fields,saveButtons('登记更正'),'report-correction-form'));
}

async function showResearchCoverage(runId, state=null, offset=0) {
  const run=await api('/api/v2/research/runs/'+encodeURIComponent(runId));
  const market=run.data.bundle?.market,coverage=market?.coverage_detail;
  if(!market)throw new Error('本轮覆盖尚未冻结');
  const page=state?await api(`/api/v2/research/runs/${encodeURIComponent(runId)}/coverage?state=${encodeURIComponent(state)}&offset=${offset}`):null;
  const categories=(coverage?.states||[]).filter(x=>x.count>0).map(x=>`<button class="btn small" data-action="research-coverage" data-run="${esc(runId)}" data-id="${esc(x.id)}">${esc(x.id==='covered'?'按偏好筛选后可研究':x.name)} · ${num(x.count)}</button>`).join('');
  const labels={announcements:'公告',financials:'财务',fund_flows:'资金流',actions:'公司行动'};
  const domains=Object.entries(coverage?.domains||{}).map(([key,value])=>`<div class="receipt-stat"><span>${labels[key]} ${value.capped?'· 扫描达上限':''}</span><strong>${num(value.present)} / ${num(value.denominator)}</strong></div>`).join('');
  const members=page?`<h3>${esc(page.label)}</h3><div class="structure-columns">${page.items.map(item=>`<span>${esc(item.symbol)}</span>`).join('')}</div>
    <div class="heading-actions">${offset?`<button class="btn small" data-action="research-coverage" data-run="${esc(runId)}" data-id="${esc(state)}" data-offset="${Math.max(0,offset-25)}">上一页</button>`:''}
    ${page.next!==null?`<button class="btn small" data-action="research-coverage" data-run="${esc(runId)}" data-id="${esc(state)}" data-offset="${page.next}">下一页</button>`:''}</div>`:'';
  openReading(dialog('本轮覆盖与排除',`${market.market_day} · ${sessionLabels[run.data.session]||'收盘'} · 任务 ${runId}`,
    `<p>行情与历史可用 ${num(market.covered)} / ${num(market.scope_size)} 只。按偏好筛选后可研究 ${num(researchParticipating(market))} 只。</p>
    <p class="form-note">证券名单 ${esc(market.directory_day||'待核')}</p><div class="heading-actions">${categories}</div>${members}${domains?'<h3>资料覆盖</h3>'+domains:''}`,
    `<button class="btn primary small" data-action="screening-audit" data-run="${esc(runId)}">查看本轮筛选明细 ↗</button>`));
}

async function showPreparation(symbol) {
  const {items}=await api('/api/v2/research-preparation');
  const fields=items.map(item=>`<label class="check-label"><input type="checkbox" name="tasks" value="${esc(item.id)}" ${item.enabled?'':'disabled'}>
    ${esc(item.label)} <small>${!item.enabled?'来源停用':item.health==='ok'?'来源可用':'需关注来源状态'}</small></label>`).join('');
  openModal(dialog('补齐个股证据',symbol,`<input type="hidden" name="symbol" value="${esc(symbol)}">
    <div class="research-settings-fields">${fields}</div><p class="form-note">先取公告索引，再按已保存的索引取原文。补齐后的事实用于下一轮。</p>
    <div data-prep-result aria-live="polite"></div>
    <label class="check-label"><input type="checkbox" name="confirm" required>确认提交所选来源的个股采集</label>`,
    saveButtons('提交采集'),'research-prep-form'));
}

async function showCalibration(horizon='D3') {
  const data=await api('/api/v2/method-calibration?horizon='+encodeURIComponent(horizon));
  openModal(dialog('同窗样本的概率校准',`${horizon} · ${esc(data.version)}`,
    `<div class="heading-actions">${['D1','D3'].map(h=>`<button class="btn small" data-action="research-calibration" data-id="${h}">${h}</button>`).join('')}</div>
    <p>${num(data.sample_n)} 个可评样本 · ${esc(data.reason)}</p>
    ${(data.groups||[]).map(g=>`<div class="receipt-stat"><span>${esc(controlLabels[g.cohort]||g.cohort)} · 验证 ${num(g.test_n)} 个</span><strong>${percent(g.frequency)} 历史频率</strong></div>`).join('')}`));
}

document.addEventListener('click',async event=>{
  const button=event.target.closest('[data-action]');
  const action=button?.dataset.action;
  if(!['research-notices','read-research-notice','send-research-notice','edit-report-correction','preview-research-plan',
    'research-coverage','research-preparation','research-calibration'].includes(action))return;
  button.disabled=true;
  try {
    const {id,before}=button.dataset;
    if(action==='research-notices')await showResearchNotices(before);
    if(action==='read-research-notice'){
      await send('/api/v2/research-notices/'+id+'/read',{});
      await showResearchNotices(before);
    }
    if(action==='send-research-notice'){
      const unknown=button.dataset.state==='unknown';
      const question=unknown?'上次终态未知。确认核对后按同一通知编号重投？':'确认发送此通知至已配置的外部渠道？';
      if(window.confirm(question)){
        await send('/api/v2/research-notices/'+id+'/send',{confirm:true,acknowledge_unknown:unknown});
        await showResearchNotices(before);
      }
    }
    if(action==='edit-report-correction')editReportCorrection(researchState.selected);
    if(action==='research-coverage')await showResearchCoverage(button.dataset.run,id||null,Number(button.dataset.offset)||0);
    if(action==='research-preparation')await showPreparation(id);
    if(action==='research-calibration')await showCalibration(id);
    if(action==='preview-research-plan'){
      const form=button.closest('form');
      const plan=await send('/api/v2/research/intent',researchRequest(form));
      const target=form.querySelector('[data-intent-preview]');
      target.innerHTML=`<div class="notice"><div><strong>本轮计划</strong>
        <p>${esc(plan.window.label)} · ${esc(plan.window.market_day)} · ${esc(plan.extended_horizon||plan.goal)}
          · ${esc(plan.task==='review'?'选定跟踪周期':plan.symbols.join('、')||'从可用资料发现')}</p>
        ${plan.extended_horizon?'':`<p>方向评价 ${esc(plan.evaluation)} · ${horizonDates(plan.window.target?.horizons)}</p>`}
        <ol>${plan.steps.map(step=>`<li>${esc(step)}</li>`).join('')}</ol>
        ${plan.tool_names.length?`<p>可用取证工具 ${plan.tool_names.map(name=>esc(plan.tools[name])).join('、')}</p>`:''}
        ${plan.extended_horizon?'<p>扩展期限单独研究，使用独立评价口径。</p>':''}</div></div>`;
    }
  }catch(error){toast(error.message,true);}
  finally{if(button.isConnected)button.disabled=false;}
});

document.addEventListener('submit',async event=>{
  const form=event.target;
  if(!['report-correction-form','research-prep-form'].includes(form.id))return;
  event.preventDefault();
  const button=form.querySelector('[type=submit]');
  button.disabled=true;
  try{
    const data=new FormData(form);
    if(form.id==='research-prep-form'){
      const result=await send('/api/v2/research-preparation',{symbol:data.get('symbol'),tasks:data.getAll('tasks'),confirm:data.has('confirm')});
      form.querySelector('[data-prep-result]').innerHTML=result.items.map(item=>`<p>${esc(item.label)} · ${item.job_id?`已登记 ${num(item.total)} 个批次 <a href="#collection" data-action="close">查看采集队列 ↗</a>`:esc(item.detail)}</p>`).join('');
      return;
    }
    const run=researchState.selected;
    await send('/api/v2/research/runs/'+run.id+'/corrections',{
      symbol:data.get('symbol')||null,action:data.get('action'),reason:data.get('reason'),confirm:data.has('confirm')
    });
    closeModal();
    toast('已追加更正');
    await render();
  }catch(error){form.querySelector('#form-error').textContent=error.message;}
  finally{button.disabled=false;}
});
