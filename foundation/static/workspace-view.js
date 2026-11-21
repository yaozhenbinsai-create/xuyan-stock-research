'use strict';

// Presentation only. Saved models, plan order, and role settings stay in app.js.
function renderStudioAI(a, tab) {
  const plan = a.plans.find(p => p.enabled) || a.plans[0];
  const primaryId = plan?.model_ids[0];
  const ordered = plan ? plan.model_ids.map(id => a.models.find(m => m.id === id)).filter(Boolean) : a.models;
  const protocol = m => ({codex:'本机官方账号',openai:'Chat Completions',responses:'Responses',anthropic:'Anthropic Messages',gemini:'Gemini 原生',xai:'Grok / xAI 原生'}[m.protocol] || m.protocol);
  const keyStatus = m => m.protocol === 'codex' ? '沿用本机账号' : m.has_api_key ? '密钥已加密保存' : m.api_key_env ? '使用环境变量密钥' : '尚未保存密钥';
  const modelSymbol = m => m.protocol === 'codex' ? '◎' : m.model_identifier.toLowerCase().includes('grok') ? 'G' : icon('spark');
  const deck = ordered.slice(0, 3).map((m, i) => `<div class="deck-card ${['front','middle','back'][i]}" style="z-index:${3-i}"><div class="deck-top"><span>${plan ? (i ? 'BACKUP 0'+i : 'PRIMARY MODEL') : 'MODEL 0'+(i+1)}</span><span class="deck-symbol">${m.protocol === 'codex' ? '◎' : '✳'}</span></div><strong>${esc(m.model_identifier)}</strong><small>${esc(m.name)}</small></div>`).reverse().join('');
  let html = heading('INTELLIGENCE, ORCHESTRATED.','AI 工作室','连接模型，安排主备，定义每一个研究角色。',`<button class="btn primary" data-action="new-ai">${icon('plus')}${tab === 'models' ? '新增模型' : tab === 'plans' ? '新建方案' : '新增角色'}</button>`);
  html += `<section class="studio-hero" aria-label="AI 配置概览"><div><div class="studio-kicker">YOUR INTELLIGENCE STACK</div><h2>${num(a.models.length)} 个模型，<br><em>一套协作秩序。</em></h2><p>${num(a.plans.length)} 个调用方案 · ${num(a.agents.length)} 个研究角色<span class="studio-subline"><br>从模型连接，到清晰的研究分工。</span></p><button class="hero-action" data-action="ai-tab" data-id="plans">查看调用顺序 ${icon('arrow')}</button></div><div class="model-deck" aria-hidden="true">${deck}</div></section>`;
  html += `<div class="tabs" role="tablist" aria-label="AI 配置">${[['models','模型连接'],['plans','调用方案'],['agents','研究角色'],['usage','调用与TOKEN']].map(([id,label]) => `<button class="tab ${tab === id ? 'active' : ''}" role="tab" aria-selected="${tab === id}" data-action="ai-tab" data-id="${id}">${label}<small>${a[id]?.length??''}</small></button>`).join('')}</div>`;
  if (tab === 'models') {
    html += a.models.length ? `<section class="connection-library" aria-label="已保存的模型连接"><div class="library-heading"><div><strong>你的模型连接</strong><small class="connection-test-note">测试连接会实际请求一次模型。</small></div><span>${num(a.models.filter(m => m.enabled).length)} 个已启用</span></div>${a.models.map(m => `<article class="connection-row ${m.id === primaryId ? 'is-primary' : ''}"><div class="connection-identity"><span class="model-symbol ${m.protocol === 'codex' ? 'official' : m.model_identifier.toLowerCase().includes('grok') ? 'grok' : ''}" aria-hidden="true">${modelSymbol(m)}</span><div><h3>${esc(m.name)}${m.id === primaryId ? '<span class="model-rank">方案首选</span>' : ''}</h3><p>${esc(m.model_identifier)}</p></div></div><div class="connection-protocol"><span>${esc(protocol(m))} ${!m.enabled ? ' · 已停用' : ''}</span><div class="connection-key">${icon(m.protocol === 'codex' ? 'shield' : 'key')}${esc(keyStatus(m))}</div><div class="connection-bits">思考 ${esc(m.reasoning_effort || '默认')} · 超时 ${m.timeout_seconds ? esc(m.timeout_seconds)+' 秒' : '默认'}</div></div><div class="connection-parameters"><div><span>思考</span>${esc(m.reasoning_effort || '模型默认')}</div><div><span>超时</span>${m.timeout_seconds ? esc(m.timeout_seconds)+' 秒' : '模型默认'}</div><div><span>输出</span>${m.max_output_tokens ? num(m.max_output_tokens) : '模型默认'}</div></div><div class="connection-actions"><button type="button" class="btn small" data-action="test-saved-model" data-id="${m.id}">测试连接</button><button type="button" class="btn small" data-action="edit-model" data-id="${m.id}" aria-label="编辑 ${esc(m.name)} 的连接">${icon('edit')}编辑连接</button></div></article>`).join('')}</section>` : empty('连接你的第一个模型','添加模型后，可以将它加入调用方案。');
  } else if (tab === 'plans') {
    html += a.plans.length ? a.plans.map(p => {
      const roles = a.agents.filter(r => r.plan_ids.includes(p.id));
      return `<article class="panel plan-card"><div class="card-heading"><div><h2>${esc(p.name)}</h2><p class="form-note">${esc(p.description || '按保存的顺序选择首选模型与备用模型。')} ${!p.enabled ? ' · 此方案已停用' : ''}</p></div><button class="btn" data-action="edit-plan" data-id="${p.id}">${icon('edit')}编辑方案</button></div><div class="plan-layout"><div class="plan-sequence">${p.model_ids.map((id,i) => {const m = a.models.find(m => m.id === id); return `<div class="plan-step ${i === 0 ? 'first' : ''}"><span class="step-index">${String(i+1).padStart(2,'0')}</span><div><strong>${esc(m?.name || '关联模型不存在')}</strong><small>${esc(m?.model_identifier || '待检查连接')}${m && !m.enabled ? ' · 已停用' : ''}</small></div>${chip(i ? '备用 '+i : '首选',i ? 'gray' : '')}</div>`;}).join('')}</div><div class="plan-summary"><div class="summary-numeral numeric">${num(roles.length).padStart(2,'0')}</div><p>个研究角色关联此方案</p><small>${esc(roles.length ? roles.slice(0,4).map(r => r.name).join(' · ')+(roles.length > 4 ? '…' : '') : '可在研究角色中关联此方案。')}</small></div></div></article>`;
    }).join('') : empty('还没有调用方案','新建方案，选择模型并设置主备顺序。');
  } else {
    html += a.agents.length ? `<div class="agent-grid">${a.agents.map((r,i) => `<article class="panel agent-card ${r.enabled ? 'active-role' : ''}"><div class="role-index">RESEARCH ROLE / ${String(i+1).padStart(2,'0')}</div><div class="card-heading"><h3>${esc(r.name)}</h3>${chip(r.enabled ? '已启用' : '未启用',r.enabled ? '' : 'gray')}</div><p>${esc(r.responsibilities || '研究职责待配置')}</p><button class="btn small" data-action="edit-agent" data-id="${r.id}" aria-label="查看与编辑 ${esc(r.name)} 的职责">查看与编辑职责 ${icon('arrow')}</button></article>`).join('')}</div>` : empty('定义第一个研究角色','为角色设置研究职责并关联调用方案。');
  }
  return html + `<div class="ai-stage-note">${icon('info')}<span>模型与方案配置</span><i class="stage-divider"></i><span>手动研究按保存的主备顺序持续轮换；临时故障等待后续作，远端未知先核对。实际TOKEN见调用与用量。</span></div>`;
}

function showStoragePolicy() {
  const p=state.storage?.policy;
  if(!p)return toast('保留规则暂未返回，请刷新后重试。',true);
  const rules=[['交易日历、证券名单、行业与日线','长期保存'],['行情快照',p.days.quotes+' 天'],['分钟行情、资金流',p.days.minutes+' 天'],['公告索引与正文节选',p.days.announcements+' 天'],['财务与公司行动',p.days.financials+' 天'],['采集明细',p.events_days+' 天 / '+num(p.events_max_rows)+' 条'],['终态采集任务',p.terminal_jobs_days+' 天 / '+num(p.terminal_jobs_max_rows)+' 条'],['数据备份',p.backups_max_count+' 份 / '+formatStorageBytes(p.backups_max_bytes)]];
  openModal(dialog('让资料轻装积累','真实事实长期积累，临时资料按用途退出。',`<div class="retention-rules">${rules.map(([label,value])=>`<div class="retention-rule"><span>${esc(label)}</span><strong>${esc(value)}</strong></div>`).join('')}</div><p class="form-note">原始接口响应不整份保存；公告正文保留有界节选与原文链接。正式研究已经引用的具体证据受保护，不按普通到期规则删除。容量到达保护线时暂停新增，保留已有基础事实。</p><p class="trace-label">规则版本 ${esc(p.version)} · 当前基础阶段尚未启用研究签发</p>`));
}

function formatStorageBytes(value) {
  if (value == null) return '—';
  return value >= 1024 ** 3 ? (value / 1024 ** 3).toFixed(2) + ' GiB' : (value / 1024 ** 2).toFixed(1) + ' MiB';
}

function renderLibraryHero(storage, stats) {
  if (!storage) return '';
  const policy = storage.policy, usage = Math.min(100, storage.database_bytes / policy.hard_database_bytes * 100);
  const core = stats.filter(s => policy.permanent.includes(s.dataset));
  const total = core.reduce((n,s) => n + s.count, 0);
  return `<section class="library-hero" aria-label="资料与容量概况"><div class="library-statement"><div class="library-eyebrow">MARKET KNOWLEDGE / YOURS</div><h2>每天积累，<br>只留下有用的。</h2><div class="library-core"><strong class="numeric">${num(total)}</strong><span>条长期基础事实</span></div><p>日线、证券名单、行业与日历。<br>同一份资料只存一次，真实修订保留来路。</p></div><div class="library-storage"><div class="storage-topline"><span>资料空间</span><span class="storage-state ${storage.level === 'normal' ? '' : 'attention'}"><i></i>${storage.level === 'normal' ? '容量充裕' : storage.level === 'warning' ? '接近保护线' : '容量保护中'}</span></div><div class="storage-value numeric">${formatStorageBytes(storage.database_bytes).split(' ')[0]}<span>${formatStorageBytes(storage.database_bytes).split(' ')[1]}</span></div><div class="storage-track" role="meter" aria-label="表与索引估计容量" aria-valuemin="0" aria-valuemax="${policy.hard_database_bytes}" aria-valuenow="${storage.database_bytes}"><i style="width:${usage}%"></i><b style="left:75%"></b></div><div class="storage-scale"><span>表与索引估计占用</span><span>${formatStorageBytes(policy.hard_database_bytes)} 保护线</span></div><div class="storage-keeper"><span>${icon('shield')}临时资料按期清理<br><small>正式引用的证据继续保留</small></span><button class="library-policy-link" data-action="storage-policy">保留规则 ↗</button></div></div></section>`;
}

function renderOverviewMetrics(o, stats, sourceCount) {
  const cnt = v => `<span data-count="${v}">${num(v)}</span>`;
  return `<section class="bento" aria-label="数据概况"><div class="tile tile-dark span-7 row-2"><div class="tile-label">${icon('database')}有效数据记录</div><div class="figure">${cnt(o.total_records)}<small>条</small></div><p class="tile-note">按业务唯一键整理，真实修订保留版本。共 ${num(stats.length)} 类数据，${num(sourceCount)} 个来源已启用。</p><div class="tile-foot"><a href="#collection">采集管理 ${icon('arrow')}</a><a href="#assets">浏览数据资产 ${icon('arrow')}</a></div></div><div class="tile span-5"><div class="tile-label">${icon('building')}已收录 A 股</div><div class="figure">${cnt(o.securities)}<small>只</small></div><p class="tile-note">含已退市等历史证券资料。</p></div><a class="tile span-5" href="#ai"><div class="tile-label">${icon('spark')}AI 模型与方案</div><div class="figure">${cnt(o.models)}<small>个模型</small></div><p class="tile-note">${num(o.plans)} 个调用方案，点击进入 AI 工作室调整主备顺序。</p></a></section>`;
}

function renderOverviewMetricsLegacy(o, stats, sourceCount) {
  return `<section class="overview-metrics" aria-label="数据概况"><div class="panel asset-feature"><div class="asset-feature-label">${icon('database')}有效数据记录</div><div class="asset-total"><span data-count="${o.total_records}">${num(o.total_records)}</span><small>条</small></div><div class="asset-feature-foot"><span>按业务唯一键整理 · 真实修订保留</span><a href="#assets">探索数据资产 ${icon('arrow')}</a></div></div><div class="overview-submetrics"><div class="summary-tile"><span>已收录 A 股</span><strong class="numeric"><span data-count="${o.securities}">${num(o.securities)}</span><small>只</small></strong><p>含已退市等历史证券资料</p></div><a class="summary-tile ai-tile" href="#ai"><span>AI 模型 / 方案</span><strong class="numeric"><span data-count="${o.models}">${num(o.models)}</span> <small>/</small> <span data-count="${o.plans}">${num(o.plans)}</span></strong><p>进入你的 AI 工作室 ↗</p></a><div class="summary-strip">${icon('layers')}<strong>${num(stats.length)}</strong>类数据 · ${num(sourceCount)} 个来源已启用<a href="#collection">采集管理 ${icon('arrow')}</a></div></div></section>`;
}
