'use strict';

const sessionLabels = {noon:'午盘', tail:'尾盘', close:'收盘'};
const sessionGoals = {noon:'寻找当日下午可能上涨的股票',tail:'寻找下一交易日可能上涨的股票',close:'寻找未来 1–3 个交易日看好的超短线股票'};
const slotStates = {capturing:'快照采集中', capture_unavailable:'快照采集不可用', submitted:'已登记研究', missed:'已错过', blocked:'暂未执行'};
const boardChoices = ['主板','创业板','科创板','北交所'];
const splitValues = value => String(value || '').split(/[,，\s]+/).filter(Boolean);

function slotReceipt(item){
  const result=item.result, capture=item.capture;
  const label=result?researchStatus[result.status]||result.stage:
    capture?.status==='failed'?'快照采集失败':
    item.state==='capturing'&&capture?.status==='completed'?'快照已采集，等待研究发起':slotStates[item.state]||item.state;
  const coverage=result?.coverage==null?'':` · 行情覆盖 ${(result.coverage*100).toFixed(2)}%`;
  const batches=capture?.total?` · 快照 ${capture.done}/${capture.total} 批`:'';
  return `${label}${coverage}${batches}`;
}

function slotSummary(schedule, session) {
  const window = schedule.windows.find(item => item.session === session);
  if (!window) return '';
  const recent = schedule.recent.find(item => item.session === session && item.day === window.market_day);
  const saved = schedule.config.enabled.includes(session);
  const enabled = schedule.effective_enabled.includes(session);
  return `<div class="slot-strip"><div class="slot-goal"><span>研判目标</span><b>${esc(window.target?.goal||sessionGoals[session])}</b></div>
    <div><span>${esc(window.label)}行情截止</span><b>${esc(window.market_data_cutoff.slice(11,16))}</b></div>
    <div><span>建议发起</span><b>${esc(window.opens_at.slice(11,16))}</b></div>
    <div><span>签发期限</span><b>${esc(window.publish_until ? (session === 'close' ? '下一交易日 ' : '') + window.publish_until.slice(11,16) : '待交易日历')}</b></div>
    <div>${chip(enabled?'本版本自动研究已启用':saved?'本版本自动研究已暂停':'共享计划未启用',enabled?'':'gray')} ${saved&&!enabled?'<small>已保存时段计划，等待本机后台启用。</small>':''} ${window.trading_day!==true?chip(window.trading_day===false?'非交易日':'待交易日历','amber'):''}
    ${recent?`<small>${esc(slotReceipt(recent))}${recent.run_id?` · <a href="#research?run=${esc(recent.run_id)}">查看研究</a>`:''}</small>`:''}</div></div>`;
}

async function editResearchSchedule() {
  const schedule = await api('/api/v2/research-schedule');
  const c = schedule.config;
  const windows = schedule.windows.map(w => `<label class="check-label"><input type="checkbox" name="enabled" value="${esc(w.session)}" ${c.enabled.includes(w.session)?'checked':''}>
    ${esc(w.label)} <small>${esc(w.target?.goal||sessionGoals[w.session])} · 行情截止 ${esc(w.market_data_cutoff.slice(11,16))}</small></label>`).join('');
  const body = `${sharedScheduleNote()}<div class="research-settings-fields">${windows}</div>
    ${inputField('指定股票（最多8只，可留空）','symbols',c.symbols.join('，'),'placeholder="sh.600000，sz.000001"')}
    <p class="form-note">三个时段都按已启用的腾讯来源和限速采样。留空时使用最新已存有效名录。截点前未取得的股票按缺口统计。</p>
    <div class="research-settings-fields">${inputField('快照最长间隔（秒）','quote_age_seconds',c.quote_age_seconds,'type="number" min="30" max="900" required')}
    ${inputField('最低数据覆盖率（%）','min_coverage',Math.round(c.min_coverage*100),'type="number" min="50" max="100" required')}</div>
    ${checkLabel('将所选时段保存到本机计划，后台按模型方案运行','confirm',false)}
    ${schedule.recent.length?`<h3>最近的自动时段</h3><div class="cycle-timeline">${schedule.recent.slice(0,5).map(r=>`<article><span>${esc(r.day)}</span><strong>${esc(sessionLabels[r.session])} · ${esc(slotReceipt(r))}</strong><p>${esc(r.result?.stage||r.detail)}</p>${r.run_id?`<a href="#research?run=${esc(r.run_id)}" data-action="close">查看该次研究 ↗</a>`:''}${r.capture?` <button class="btn small" data-action="job-detail" data-id="${esc(r.capture.id)}" type="button">采集回执 ↗</button>`:''}</article>`).join('')}</div>`:''}`;
  openModal(dialog('三时段研究计划','上海时间 · 以交易日历和截点证据执行',body,saveButtons('保存时段计划'),'session-policy-form'));
}

function saveButtons(label) {
  return `<button class="btn" type="button" data-action="close">取消</button><button class="btn primary" type="submit">${esc(label)}</button>`;
}

async function editResearchPreferences() {
  const [p,feedback] = await Promise.all([api('/api/v2/research-preferences'),api('/api/v2/research-feedback').catch(()=>null)]);
  const body = `<p>${p.confirmed_at?`已启用第 ${num(p.revision)} 版研究偏好。`:'你还没有确认长期研究偏好。'}</p>
    <label class="field">我希望 AI 记住的研究偏好<textarea name="note" rows="3" maxlength="600" placeholder="例如更重视现金流和下行反证">${esc(p.note||'')}</textarea></label>
    <div class="research-settings-fields">${selectField('优先研究周期','horizon',p.horizon||'D3',[['D1','下一交易日'],['D3','未来三个交易日']])}
    ${selectField('研究风险倾向','risk',p.risk||'balanced',[['conservative','稳健'],['balanced','均衡'],['opportunistic','机会型']])}
    ${inputField('自动发现最多候选','candidate_limit',p.candidate_limit||8,'type="number" min="1" max="8" required')}
    ${inputField('同一行业最多入选（可留空）','max_industry_recommendations',p.max_industry_recommendations??'','type="number" min="1" max="8"')}</div>
    <p class="form-note">选择自动发现时关注的板块，留空按可用范围研究。自主点名优先。</p>
    <div class="research-settings-fields">${boardChoices.map(b=>`<label class="check-label"><input type="checkbox" name="boards" value="${b}" ${p.boards?.includes(b)?'checked':''}>${b}</label>`).join('')}</div>
    ${inputField('自动发现时排除的股票','excluded_symbols',(p.excluded_symbols||[]).join('，'))}
    ${checkLabel('确认将这些偏好用于后续研究','confirm',false)}
    ${feedback?'<button class="btn" type="button" data-action="edit-research-feedback">反馈与待确认偏好</button>':''}
    ${p.confirmed_at?'<button class="btn ghost" type="button" data-action="revoke-research-preferences">撤销长期偏好</button>':''}`;
  openModal(dialog('让研究更贴近你','你确认后生效，可以随时编辑或撤销',body,saveButtons('保存研究偏好'),'research-preferences-form'));
}

function growthOverview(value) {
  if (!value) return '';
  researchState.methodFactors=value.factors||null;
  researchState.methodProposals=value.diagnostics?.proposals||[];
  const params=value.factors?Object.entries(value.factors).map(([key,spec])=>`${esc(spec.label)} ${num(value.active[key])}`).join(' · '):`突破相对量 ${num(value.active.relative_volume_min)} 倍`;
  const diagnosis=value.diagnostics;
  const suggestions=researchState.methodProposals.map((draft,index)=>`<article class="diagnosis-card"><b>${esc(draft.title)}</b>
    <p>训练段 ${draft.training_days} 个交易日出现 ${draft.losses} 个亏损样本，候选参数 ${num(draft.threshold)}。</p>
    <button class="btn small" data-action="draft-method-experiment" data-id="${index}">登记这项验证</button></article>`).join('');
  return `<section class="panel growth-proof"><div><span class="eyebrow">METHOD EXPERIMENTS</span><h2>从复盘找出反复失效的条件。</h2>
    <p>当前 ${esc(value.active.version)} · ${params}</p>
    <div class="heading-actions"><button class="btn primary" data-action="create-method-experiment">登记实验</button><button class="btn" data-action="research-calibration" data-id="D3">概率校准</button>${value.previous?'<button class="btn" data-action="rollback-method">回退到上一版本</button>':''}</div>
    ${diagnosis?`<div class="diagnosis-card"><b>复盘诊断</b><p>${diagnosis.evaluable} 个可评样本 · ${diagnosis.sample_days} 个交易日</p>
    ${suggestions||(diagnosis.state==='insufficient'?'<p>积累到足够的独立交易日后生成建议。</p>':'<p>训练段尚未发现符合阈值的重复失效。</p>')}</div>`:''}</div>
    <div>${value.items.length?value.items.map(e=>`<button class="experiment-row" data-action="method-experiment" data-id="${esc(e.id)}"><span><b>${esc(e.title)}</b><small>${esc(value.factors?.[e.factor]?.label||'突破相对量')} ${num(e.threshold)}</small></span>${chip(({shadow:'影子观察',paused:'已暂停',published:'已发布',rejected:'已关闭'})[e.state])}</button>`).join(''):'<b>等待登记实验</b><p>时间外和前瞻影子样本分别检验。</p>'}</div></section>`;
}
function newMethodExperiment(draft={}) {
  const factors=researchState.methodFactors;
  const body=`${inputField('实验名称','title',draft.title||'','required maxlength="100"')}
    <div class="research-settings-fields">${factors?selectField('实验参数','factor',draft.factor||'relative_volume_min',Object.entries(factors).map(([id,s])=>[id,s.label])):''}
    ${inputField('候选阈值','threshold',draft.threshold||1.5,'type="number" min="0.5" max="3" step="0.01" required')}
    ${inputField('最少可评周期','min_samples',20,'type="number" min="20" max="300" required')}${inputField('最少独立交易日','min_days',6,'type="number" min="6" max="60" required')}</div>
    <div class="research-settings-fields">${[['训练开始','train_start'],['训练结束','train_end'],['验证结束','validation_end'],['时间外测试结束','test_end']].map(([label,key])=>inputField(label,key,draft[key]||'','type="date" required')).join('')}</div>
    ${inputField('往返成本情景（基点，可留空）','cost_bps','','type="number" min="0" max="1000" step="0.01"')}
    ${checkLabel('本实验通过预登记门槛后允许自动发布','auto_publish',false)}
    <p class="form-note">相对量填倍数，收盘位置和同行广度填 0 到 1 之间的比例。新研究先冻结影子信号，等 D3 结果出现后再比较。</p>`;
  openModal(dialog('登记方法实验','先确定参数和观察窗口',body,saveButtons('登记影子实验'),'method-experiment-form'));
}

async function showMethodExperiment(id) {
  const {experiment:e,evaluation:v} = await api('/api/v2/method-experiments/'+encodeURIComponent(id));
  const rows = Object.entries(v.stages).map(([name,s])=>`<tr><th>${({train:'训练',validation:'验证',test:'时间外',shadow:'前瞻影子'})[name]}</th>
    <td>${s.evaluable} / ${s.total}</td><td>${s.days}</td><td>${s.different}</td><td>${percent(s.baseline.mean_per_sample)}</td><td>${percent(s.candidate.mean_per_sample)}</td></tr>`).join('');
  const body = `${chip(v.qualified?'已达到发布门槛':'等待足够证据',v.qualified?'':'amber')}
    <p>${esc(researchState.methodFactors?.[e.factor]?.label||'突破相对量')} · 基线 ${num(e.baseline[e.factor||'relative_volume_min'])} · 候选 ${num(e.threshold)}</p>
    <p>${e.cost_bps==null?'收益为费用前口径':`每次往返成本情景 ${num(e.cost_bps)} 基点`} · ${e.auto_publish?'已授权门槛达标后自动发布':'需手动发布'}</p>
    <div class="table-wrap"><table><thead><tr><th>观察段</th><th>可评 / 全部</th><th>交易日</th><th>不同信号</th><th>基线每样本收益</th><th>候选每样本收益</th></tr></thead><tbody>${rows}</tbody></table></div>
    <p class="form-note">${esc(v.basis)}${v.truncated?' 超出本轮计算上限，暂不可发布。':''}</p>`;
  const change=(action,label)=>`<button class="btn" data-action="method-decision" data-id="${esc(id)}" data-decision="${action}">${label}</button>`;
  const footer = e.state==='shadow'?`${change('reject','关闭实验')} ${change('pause','暂停观察')}
    <button class="btn primary" data-action="method-decision" data-id="${esc(id)}" data-decision="publish" ${v.qualified?'':'disabled'}>发布到后续研究</button>`
    :e.state==='paused'?change('reject','关闭实验')+change('resume','继续观察'):'';
  openModal(dialog(e.title,'同一批已冻结的前瞻样本',body,footer));
}

function stockEvidenceDetails(candidate,runId) {
  const docs = candidate.documents || [];
  const s = candidate.metrics.structure;
  return `<div class="evidence-layers"><h3>实际读取的证据</h3>${docs.length?docs.map(row=>`<details><summary>${esc(row.title||row.report_type||row.dataset)} · ${esc(row.day)}</summary><p>${esc(row.excerpt||row.basis||row.source)}</p>${evidenceButtons([row.id])}</details>`).join(''):'<p>这轮没有读到对应的公告、财务或资金材料。</p>'}
    ${s?`<h3>结构线索</h3><p>${esc([...(s.signals||[]),s.counterpoint].filter(Boolean).join('；'))}</p>`:''}
    <div class="heading-actions">${runId&&candidate.originals?`<button class="btn primary small" data-action="show-originals" data-run="${esc(runId)}" data-id="${esc(candidate.symbol)}">核对政策、财报与业务</button>`:''}
    <button class="btn small" data-action="filing-collection" data-id="${esc(candidate.symbol)}">补取公司原件</button>
    <button class="btn small" data-action="edit-research-check" data-id="${esc(candidate.symbol)}" data-kind="risk">登记风险核验</button>
    <button class="btn small" data-action="edit-research-check" data-id="${esc(candidate.symbol)}" data-kind="actions">登记公司行动核验</button></div></div>`;
}

async function editResearchCheck(symbol,kind) {
  const checks = await api('/api/v2/research-checks/'+encodeURIComponent(symbol));
  const c = checks[kind];
  const selected = researchState.selected?.data?.bundle?.candidates?.find(row=>row.symbol===symbol);
  const ids = (selected?.documents||[]).filter(row=>kind==='risk'?row.dataset==='announcements'&&row.excerpt_limited:row.dataset==='actions').map(row=>row.id);
  const body = `<input type="hidden" name="symbol" value="${esc(symbol)}"><input type="hidden" name="kind" value="${esc(kind)}">
    ${selectField('核验结论','state',c?.state||'verified',[['verified','核验已完成'],['blocked','存在未解问题'],['revoke','撤销核验记录']])}
    ${inputField('事实编号','fact_ids',(c?.fact_ids||ids).join('，'))}${inputField('有效期至','valid_until',c?.valid_until||'','type="date"')}
    <label class="field">核查范围和依据<textarea name="note" maxlength="700" rows="3">${esc(c?.note||'')}</textarea></label>`;
  openModal(dialog(kind==='risk'?'公司风险核验':'公司行动核验',symbol+' · 用于之后的研究',body,saveButtons('登记核验'),'research-check-form'));
}

async function chooseReference(id, kind) {
  const {items} = await api('/api/v2/cycles/'+encodeURIComponent(id)+'/references?kind='+encodeURIComponent(kind));
  const q = items[0];
  if (!q) {
    openModal(dialog('等待合格参考报价','原条件和窗口保持', '<p>当前还没有符合原窗口的已存报价。可在数据资产核对采集状态。</p>'));
    return;
  }
  const body = `<input type="hidden" name="cycle_id" value="${esc(id)}"><input type="hidden" name="kind" value="${esc(kind)}"><input type="hidden" name="fact_id" value="${q.fact_id}">
    <p>${esc(q.at.replace('T',' ').slice(0,19))} · 参考价 <b>${price(q.price)}</b></p>${evidenceButtons([q.fact_id])}
    ${checkLabel('确认此时原有非价格条件已经满足','conditions_confirmed',false)}`;
  openModal(dialog(kind==='entry'?'登记原窗口参考价':'登记剔除后参考价','使用原窗口内最早的合格报价',body,saveButtons('登记参考报价'),'reference-form'));
}

document.addEventListener('click', async e => {
  const el = e.target.closest('[data-action]');
  if (!el || el.disabled) return;
  const {action,id,kind,decision} = el.dataset;
  try {
    if (action==='edit-research-schedule') await editResearchSchedule();
    if (action==='edit-research-preferences') await editResearchPreferences();
    if (action==='revoke-research-preferences') {
      await send('/api/v2/research-preferences',{confirm:true,revoke:true},'PUT');
      await editResearchPreferences();toast('长期偏好已撤销');
    }
    if (action==='create-method-experiment') newMethodExperiment();
    if (action==='draft-method-experiment') newMethodExperiment(researchState.methodProposals[Number(id)]);
    if (action==='method-experiment') await showMethodExperiment(id);
    if (action==='method-decision') {
      await send('/api/v2/method-experiments/'+encodeURIComponent(id)+'/decision',{action:decision,confirm:true});
      closeModal();await render();toast({publish:'后续研究已切换到新方法',reject:'实验已关闭，记录已保存',pause:'实验已暂停',resume:'实验已恢复影子观察'}[decision]);
    }
    if (action==='rollback-method') {
      await send('/api/v2/method-rollback',{confirm:true});await render();toast('已回退方法版本');
    }
    if (action==='edit-research-check') await editResearchCheck(id,kind);
    if (action==='choose-reference') await chooseReference(id,kind);
  } catch (error) {toast(error.message,true);}
});

document.addEventListener('submit', async e => {
  const form=e.target;
  if (!['session-policy-form','research-preferences-form','method-experiment-form','research-check-form','reference-form'].includes(form.id)) return;
  e.preventDefault();
  const button=form.querySelector('[type=submit]');
  if (button.disabled) return;
  button.disabled=true;
  const f=new FormData(form), v=Object.fromEntries(f);
  try {
    if (form.id==='session-policy-form') {
      await send('/api/v2/research-schedule',{enabled:f.getAll('enabled'),symbols:splitValues(v.symbols),quote_age_seconds:Number(v.quote_age_seconds),min_coverage:Number(v.min_coverage)/100,confirm:f.has('confirm')},'PUT');
    } else if (form.id==='research-preferences-form') {
      await send('/api/v2/research-preferences',{...v,boards:f.getAll('boards'),excluded_symbols:splitValues(v.excluded_symbols),
        candidate_limit:Number(v.candidate_limit),max_industry_recommendations:v.max_industry_recommendations?Number(v.max_industry_recommendations):null,confirm:f.has('confirm')},'PUT');
    } else if (form.id==='method-experiment-form') {
      await send('/api/v2/method-experiments',{...v,threshold:Number(v.threshold),min_samples:Number(v.min_samples),min_days:Number(v.min_days),
        cost_bps:v.cost_bps===''?null:Number(v.cost_bps),auto_publish:f.has('auto_publish'),confirm_auto:f.has('auto_publish')});
    } else if (form.id==='research-check-form') {
      await send('/api/v2/research-checks',{...v,fact_ids:splitValues(v.fact_ids).map(Number)});
    } else {
      await send('/api/v2/cycles/'+encodeURIComponent(v.cycle_id)+'/reference',{kind:v.kind,fact_id:Number(v.fact_id),conditions_confirmed:f.has('conditions_confirmed')});
      await showCycle(v.cycle_id);toast('参考价已登记');return;
    }
    closeModal();await render();toast('已保存');
  } catch(error) {form.querySelector('#form-error').textContent=error.message;}
  finally {button.disabled=false;}
});
