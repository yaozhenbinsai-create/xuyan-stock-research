'use strict';
const modelUsageState={before:null,history:[],next:null,rows:[]};
const usageModelName=r=>r.model_name||r.data.model_identifier||r.model_id;
const tokenValue=v=>v==null?'未知':num(v);
async function renderModelUsage(a){
  const q=new URLSearchParams({limit:'25'});if(modelUsageState.before)q.set('before',modelUsageState.before);
  const [u,c]=await Promise.all([api('/api/v2/usage'),api('/api/v2/usage/calls?'+q)]);modelUsageState.rows=c.items;modelUsageState.next=c.next;
  return heading('CALLS / ROTATION / MEASURED USAGE','AI调用与TOKEN','看清实际用了哪个模型、如何切换，以及供应商真正返回了多少用量。',`<button class="btn" data-action="refresh">${icon('refresh')}刷新记录</button>`)
    +`<div class="tabs" role="tablist" aria-label="AI 配置">${[['models','模型连接'],['plans','调用方案'],['agents','研究角色'],['usage','调用与TOKEN']].map(([id,n])=>`<button class="tab ${id==='usage'?'active':''}" role="tab" aria-selected="${id==='usage'}" data-action="ai-tab" data-id="${id}">${n}${a[id]?`<small>${a[id].length}</small>`:''}</button>`).join('')}</div>`
    +`<div class="usage-summary"><article class="panel"><span>实际尝试记录</span><strong>${num(u.attempts)}</strong><small>所有成功、失败与未知均保留</small></article><article class="panel"><span>已回报 TOKEN 小计</span><strong>${u.reported.total==null?'暂无回执':num(u.reported.total)}</strong><small>${u.actual_total_known?'现有记录均已取得完整用量':'这不是包含未知请求的实际总消耗'}</small></article><article class="panel"><span>用量完整 / 部分 / 未知</span><strong>${u.usage_complete} / ${u.usage_partial} / ${u.usage_unknown}</strong><small>未收到回执不会记成0 TOKEN</small></article></div>`
    +`<div class="notice ${u.usage_unknown||u.usage_partial?'amber':''}">${icon('info')}<p>${esc(u.basis)}<br>未取得费用回执时金额保持未知，不以TOKEN数量或配置上限编造账单。</p></div>`
    +`<div class="panel"><div class="panel-header"><h2>按模型核对实耗</h2><span class="plain-tag">供应商回执</span></div><div class="table-wrap"><table><thead><tr><th>模型</th><th>尝试</th><th>输入</th><th>缓存输入</th><th>输出</th><th>其中推理</th><th>总量</th><th>未知回执</th></tr></thead><tbody>${u.by_model.map(m=>`<tr><td>${esc(m.model_name)}</td><td>${m.attempts}</td><td>${tokenValue(m.input)}</td><td>${tokenValue(m.cached_input)}</td><td>${tokenValue(m.output)}</td><td>${tokenValue(m.reasoning_output)}</td><td>${tokenValue(m.total)}</td><td>${m.usage_unknown}</td></tr>`).join('')||'<tr><td colspan="8">还没有真实调用记录。</td></tr>'}</tbody></table></div></div>`
    +`<div class="section-heading"><h2>逐次调用与轮换</h2><p>按时间保留真实模型、状态和用量，SQL每页25条</p></div><div class="panel"><div class="table-wrap"><table><thead><tr><th>开始时间</th><th>实际模型</th><th>轮次 / 尝试</th><th>状态</th><th>已回报TOKEN</th><th></th></tr></thead><tbody>${c.items.map(r=>`<tr><td>${esc(r.started.slice(0,19).replace('T',' '))}</td><td>${esc(usageModelName(r))}</td><td>${r.data.origin==='connection_test'?'连接测试':r.data.cycle?`第${r.data.cycle}轮 / 第${r.data.attempt}次`:'历史单次记录'}</td><td>${chip(({completed:'已完成',unknown:'远端未知',sent:'请求中',prepared:'尚未发出',failed:'未完成',invalid:'内容未通过'})[r.status]||r.status,r.status==='unknown'?'amber':'gray')}</td><td>${tokenValue(r.data.usage_normalized?.total)}</td><td><button class="btn small" data-action="usage-call" data-id="${r.id}">详情</button></td></tr>`).join('')||'<tr><td colspan="6">还没有调用记录。</td></tr>'}</tbody></table></div><div class="table-foot"><span>未知值不参与已回报小计，也不代表没有消耗。</span><div><button class="btn small" data-action="usage-prev" ${modelUsageState.history.length?'':'disabled'}>上一页</button><button class="btn small" data-action="usage-next" ${c.next?'':'disabled'}>下一页</button></div></div></div>`;
}
document.addEventListener('click',async e=>{
  const el=e.target.closest('[data-action]');if(!el||el.disabled)return;
  try{
    if(el.dataset.action==='usage-next'){modelUsageState.history.push(modelUsageState.before);modelUsageState.before=modelUsageState.next;return await render();}
    if(el.dataset.action==='usage-prev'){modelUsageState.before=modelUsageState.history.pop()||null;return await render();}
    if(el.dataset.action==='usage-call'){
      const r=modelUsageState.rows.find(x=>x.id===el.dataset.id);if(!r)return;const d=r.data;
      openModal(dialog('实际调用与用量',`${usageModelName(r)} / ${r.started}`,`<dl class="detail-pairs"><dt>请求状态</dt><dd>${esc(r.status)}</dd><dt>实际模型</dt><dd>${esc(usageModelName(r))}</dd><dt>输入TOKEN</dt><dd>${tokenValue(d.usage_normalized?.input)}</dd><dt>输出TOKEN</dt><dd>${tokenValue(d.usage_normalized?.output)}</dd><dt>其中推理</dt><dd>${tokenValue(d.usage_normalized?.reasoning_output)}</dd><dt>总TOKEN</dt><dd>${tokenValue(d.usage_normalized?.total)}</dd><dt>金额</dt><dd>未知</dd><dt>提供方请求身份</dt><dd>${esc(d.provider_request_id||d.remote?.turn_id||'未收到')}</dd><dt>实际原因</dt><dd>${esc(d.message||d.validation_error||'查看请求状态及用量回执')}</dd></dl><p class="form-note">最大输出设置不是实际消耗。缓存输入已包含在输入中，推理已包含在输出中。</p>${d.origin==='connection_test'?'':`<a class="btn" href="#research?run=${r.run_id}" data-action="close">查看所属研究</a>`}`));
    }
  }catch(error){toast(error.message,true);}
});
