'use strict';

const bookState = {actual:{after:null,before:null},paper:{after:null,before:null}};
const controlLabels = {selected:'入选',watch:'观察',rejected:'不选',unreviewed:'有线索未研究',outside:'池外对照'};
const controlStates = {awaiting_quote:'等待窗口报价',awaiting_close:'等待终点',missing_start:'缺同窗起点',missing_end:'缺终点',not_comparable:'价格不可比',complete:'可评'};
const bookName = mode => mode==='paper'?'模拟账本':'实际持仓';
const cashValue = value => value==null?'未知':`${esc(value)} 元`;

function bookNav(book='cycles') {
  const options=[['cycles','研究股票池','#tracking'],['actual','实际持仓','#tracking?book=actual'],['paper','模拟账本','#tracking?book=paper']];
  return `<nav class="book-switch" aria-label="跟踪账本">${options.map(([id,label,href])=>`<a class="btn ${book===id?'primary':''}" href="${href}" ${book===id?'aria-current="page"':''}>${label}</a>`).join('')}</nav>`;
}

async function renderPortfolio(mode) {
  const view=bookState[mode], root='/api/v2/portfolio/'+mode;
  const posQuery=new URLSearchParams({limit:'25'}), fillQuery=new URLSearchParams({limit:'25'});
  if(view.after)posQuery.set('after',view.after);
  if(view.before)fillQuery.set('before',view.before);
  const [p,f]=await Promise.all([api(root+'/positions?'+posQuery),api(root+'/fills?'+fillQuery)]);
  const header=heading('RESEARCH / POSITIONS',bookName(mode),mode==='actual'?'录入已发生的成交，核对实际股数、费用和成本。':'保存明确登记的模拟成交，独立复算费用和结果。',
    `<div class="heading-actions"><button class="btn" data-action="import-fills" data-mode="${mode}">批量导入</button><button class="btn primary" data-action="record-fill" data-mode="${mode}">登记成交</button></div>`);
  const portfolio=p.items.length?`<div class="portfolio-cards">${p.items.map(row=>`<article class="panel portfolio-card"><div class="stock-card-top"><h3>${esc(row.symbol)}</h3>${chip(bookName(mode),'gray')}</div>
    <strong>${num(row.quantity)}<small> 股</small></strong><p>已交收 ${num(row.available)} 股</p>
    <dl class="detail-pairs"><dt>含费用成本</dt><dd>${cashValue(row.net_basis)}</dd><dt>参考市值</dt><dd>${cashValue(row.reference_value)}</dd>
    <dt>参考浮动盈亏</dt><dd>${cashValue(row.unrealized_net)}</dd><dt>核价日</dt><dd>${esc(row.reference_day||'待核价')}</dd></dl>
    <button class="btn small" data-action="portfolio-summary" data-mode="${mode}" data-id="${esc(row.symbol)}">查看已实现损益</button></article>`).join('')}</div>`:
    empty('当前没有持仓','登记买入成交后，工作台按股数和已知费用核算。');
  const records=f.items.length?`<div class="panel"><div class="table-wrap"><table><thead><tr><th>成交时间</th><th>代码</th><th>方向</th><th>股数</th><th>价格</th><th>费用</th><th>操作</th></tr></thead>
    <tbody>${f.items.map(row=>`<tr><td>${esc(row.filled_at.slice(0,19).replace('T',' '))}</td><td>${esc(row.symbol)}<small class="archive-created">${esc(row.external_id)}</small></td>
      <td>${row.side==='buy'?'买入':'卖出'}${row.voided_at?` ${chip('已撤销','gray')}`:''}</td><td>${num(row.quantity)}</td><td>${cashValue(row.price)}</td><td>${cashValue(row.fees)}</td>
      <td><button class="btn small" data-action="portfolio-summary" data-mode="${mode}" data-id="${esc(row.symbol)}">损益</button>
      ${row.voided_at?'':`<button class="btn ghost small" data-action="void-fill" data-mode="${mode}" data-id="${row.id}">撤销记录</button>`}</td></tr>`).join('')}</tbody></table></div></div>`:
      empty('还没有成交记录','可以登记一笔，或粘贴成交导出内容批量录入。');
  return header+bookNav(mode)+(p.test_mode?'<div class="notice amber"><p>隔离账本 · 验收示例成交</p></div>':'')+`<section class="portfolio-intro"><span class="eyebrow">${mode==='actual'?'ACTUAL FILLS':'PAPER FILLS'}</span><h2>${mode==='actual'?'股数、成本和费用，都能对账。':'每次模拟，都有自己的记录。'}</h2>
    <p>按成交顺序复算。参考市值取最近已存日线，未填费用的净损益保持未知。</p></section>`+portfolio+
    bookPager(mode,'after',view.after,p.next)+`<div class="section-heading"><h2>成交账本</h2><p>每页最多25笔，按成交时间分页</p></div>`+records+bookPager(mode,'before',view.before,f.next);
}

function bookPager(mode,key,current,next) {
  if(!current&&!next)return '';
  return `<div class="table-foot"><span>${key==='after'?'持仓':'成交'} · 每页25条</span><div>
    <button class="btn small" data-action="book-page" data-mode="${mode}" data-key="${key}" data-id="" ${current?'':'disabled'}>回到首页</button>
    <button class="btn small" data-action="book-page" data-mode="${mode}" data-key="${key}" data-id="${esc(next||'')}" ${next?'':'disabled'}>下一页</button></div></div>`;
}

function fillForm(mode, symbol='', cycle='') {
  const body=`<input type="hidden" name="mode" value="${esc(mode)}"><input type="hidden" name="cycle_id" value="${esc(cycle)}">
    ${inputField('成交编号','external_id','','required maxlength="80" placeholder="成交单上的编号或你为本笔设定的编号"')}
    <div class="research-settings-fields">${inputField('股票代码','symbol',symbol,'required placeholder="sh.600000"')}
    ${selectField('成交方向','side','buy',[['buy','买入'],['sell','卖出']])}
    ${inputField('成交时间','filled_at','','type="datetime-local" required')}</div>
    <div class="research-settings-fields">${inputField('成交股数','quantity','','type="number" min="1" step="1" required')}
    ${inputField('成交价（元）','price','','type="number" min="0.0001" step="0.0001" required')}
    ${inputField('本笔总费用（元）','fees','','type="number" min="0" step="0.01" placeholder="留空为未知"')}</div>
    ${inputField('备注','note','','maxlength="300"')}${cycle?`<p>关联原研究周期 ${esc(cycle)}</p>`:''}`;
  openModal(dialog('登记成交',bookName(mode)+' · 上海时间',body,saveButtons('保存成交'),'portfolio-fill-form'));
}

function importFillForm(mode) {
  const body=`<input type="hidden" name="mode" value="${esc(mode)}"><p>每批最多 100 笔。编号用于识别同一笔成交，费用留空时保持未知。</p>
    <label class="field">成交 CSV<textarea name="csv" required rows="7" maxlength="48000" placeholder="编号,代码,方向,成交时间,股数,成交价,费用"></textarea></label>`;
  openModal(dialog('批量导入成交',bookName(mode),body,saveButtons('核对并导入'),'portfolio-import-form'));
}

async function showPortfolioSummary(mode,symbol) {
  const row=await api('/api/v2/portfolio/'+mode+'/symbols/'+encodeURIComponent(symbol));
  openModal(dialog('已实现损益',`${symbol} · ${bookName(mode)}`,
    `<dl class="detail-pairs"><dt>卖出成交</dt><dd>${num(row.sells)}</dd><dt>毛损益</dt><dd>${cashValue(row.realized_gross)}</dd>
    <dt>净损益</dt><dd>${cashValue(row.realized_net)}</dd><dt>费用齐全的卖出</dt><dd>${num(row.known_net_sells)} / ${num(row.sells)}</dd></dl>`));
}

async function openPolicyOriginals(before=null) {
  const values=await api('/api/v2/policy-originals?limit=15'+(before?'&before='+encodeURIComponent(before):''));
  const list=values.items.map(row=>`<article class="policy-row"><div><b>${esc(row.title)}</b><small>${esc(row.published)} · ${esc(row.terms.join('、'))}</small></div>${evidenceButtons([row.id])}</article>`).join('');
  const body=`<p>给出可定位的官方正文链接和产业术语。之后的研究会对照政策段落与公司业务披露。</p>
    ${inputField('官方政策原文链接','url','','type="url" maxlength="400" required')}
    ${inputField('产业或产品术语','terms','','required placeholder="例如储能，锂电池"')}
    <div class="policy-list">${list||'<p>尚未登记政策原件。</p>'}</div>
    ${values.next?`<button type="button" class="btn small" data-action="policy-originals" data-id="${values.next}">下一页原件</button>`:''}`;
  openModal(dialog('政策原文库','核对官方正文与公开日期',body,saveButtons('获取并登记原文'),'policy-original-form'));
}

function originalSource(anchor) {
  try {
    const url=new URL(anchor.url);
    if(url.protocol!=='https:'||!['static.cninfo.com.cn','www.gov.cn','www.mee.gov.cn','www.most.gov.cn','www.miit.gov.cn','www.mofcom.gov.cn'].includes(url.hostname))return '';
    const page=anchor.location.match(/PDF 第 (\d+) 页/);
    if(page)url.hash='page='+page[1];
    return `<a href="${esc(url.toString())}" target="_blank" rel="noopener noreferrer">打开原件 ↗</a>`;
  } catch {return '';}
}

async function showOriginalPacket(run,symbol) {
  const value=await api(`/api/v2/research/runs/${encodeURIComponent(run)}/stocks/${encodeURIComponent(symbol)}/originals`);
  const rows=(value.financial_fields||[]).map(row=>`<tr><td>${esc(row.period)}</td><td>${esc(row.label)}</td><td>${cashValue(row.amount_yuan)}</td><td>${row.basis==='cumulative'?'期间累计':'待核口径'}</td></tr>`).join('');
  const comparisons=(value.comparisons||[]).map(row=>`<p>${esc(row.current.period)} · ${esc(row.current.label)} ${chip(row.state==='matches'?'两个原件一致':'原件数字有差异',row.state==='matches'?'':'amber')} · 差额 ${cashValue(row.delta_yuan)}</p>`).join('');
  const links=(value.industry_links||[]).map(row=>`<article class="policy-row"><div><b>${esc(row.term)}</b><small>${esc(row.policy_title)} · 生效 ${esc(row.effective_day||'待核')}</small></div>${evidenceButtons([row.policy_id,row.filing_id])}</article>`).join('');
  const quotes=(value.anchors||[]).map(a=>`<details><summary>${esc(a.label)} · ${esc(a.location)} · #${a.fact_id}</summary><p>${esc(a.quote||'原事实当前不可读')}</p>${originalSource(a)} ${evidenceButtons([a.fact_id])}</details>`).join('');
  openReading(dialog('政策、财报与业务敞口',symbol+' · 本轮冻结原件',
    `<div class="evidence-layers"><h3>同口径财报数字</h3>${rows?`<div class="table-wrap"><table><thead><tr><th>报告期</th><th>科目</th><th>原件金额</th><th>口径</th></tr></thead><tbody>${rows}</tbody></table></div>`:'<p>未读到可定位的财报原件数字。</p>'}${comparisons}
    <h3>政策与公司业务交集</h3>${links||'<p>尚无两边都能定位的产业术语。</p>'}<h3>已读取的原文页段</h3>${quotes||'<p>本轮没有冻结原文片段。</p>'}
    <p>${esc(value.basis||'')}</p></div>`));
}

function controlComparison(value) {
  if(!value)return '';
  const rows=value.summary.groups.map(row=>`<tr><th>${esc(controlLabels[row.cohort])}</th><td>${esc(row.horizon)}</td><td>${num(row.evaluable)} / ${num(row.total)}</td>
    <td>${num(row.positive)}</td><td>${percent(row.returns)}</td></tr>`).join('');
  return `<section class="panel comparison-panel"><div class="panel-header"><div><h2>同一窗口，选与没选</h2><p>${esc(value.summary.basis)}</p></div>
    <button class="btn small" data-action="control-details">逐笔核对</button></div>
    <div class="table-wrap"><table><thead><tr><th>当时归类</th><th>终点</th><th>可评 / 全部</th><th>上涨样本</th><th>平均参考收益</th></tr></thead><tbody>${rows}</tbody></table></div></section>`;
}

async function controlDetails(before=null) {
  const page=await api('/api/v2/review-controls?limit=25'+(before?'&before='+encodeURIComponent(before):''));
  const rows=page.items.map(row=>`<tr><td>${esc(row.symbol)}</td><td>${esc(controlLabels[row.cohort])}</td><td>${esc(row.start_at.slice(0,16).replace('T',' '))}</td>
    <td>${esc(row.endpoint)} · ${row.horizon}</td><td>${esc(controlStates[row.status])} ${percent(row.data.return_)}</td><td><a href="#research?run=${esc(row.run_id)}" data-action="close">原稿</a></td></tr>`).join('');
  openModal(dialog('同窗复盘样本','按当时冻结的入选、未选和池外范围',rows?`<div class="table-wrap"><table><thead><tr><th>股票</th><th>范围</th><th>起点</th><th>终点</th><th>结果</th><th>依据</th></tr></thead><tbody>${rows}</tbody></table></div>`:'<p>暂无冻结的同窗样本。</p>',
    page.next?`<button class="btn" data-action="control-details" data-id="${page.next}">下一页</button>`:''));
}

async function editFeedback(run='') {
  const value=await api('/api/v2/research-feedback');
  const settingLabels={note:'研究重点',risk:'风险倾向',horizon:'优先周期',boards:'关注板块',candidate_limit:'候选上限'};
  const displayValue=value=>Array.isArray(value)?value.join('、'):({conservative:'稳健',balanced:'均衡',opportunistic:'机会型'})[value]||value;
  const items=value.items.map(row=>`<article class="policy-row"><div><b>${esc(row.label)}</b><small>${row.stale&&row.state==='pending'?'基于较早的偏好版本':({pending:'待确认',accepted:'已应用',dismissed:'已略过'})[row.state]}</small>
    ${Object.entries(row.patch).map(([key,v])=>`<small>${esc(settingLabels[key]||key)} ${esc(displayValue(v))}</small>`).join('')}${row.source_note?`<p>${esc(row.source_note)}</p>`:''}</div>
    ${row.state==='pending'?`<div><button type="button" class="btn small" data-action="feedback-decision" data-id="${esc(row.id)}" data-choice="accept" ${row.stale?'disabled':''}>应用偏好</button><button type="button" class="btn ghost small" data-action="feedback-decision" data-id="${esc(row.id)}" data-choice="dismiss">略过</button></div>`:''}</article>`).join('');
  const body=`<input type="hidden" name="run_id" value="${esc(run)}">${selectField('这次想调整什么','topic','risk',Object.entries(value.topics))}
    ${inputField('补充说明（可选）','note','','maxlength="250"')}
    <div class="policy-list"><h3>待确认和已处理的建议</h3>${items||'<p>还没有偏好建议。</p>'}</div>`;
  openModal(dialog('研究反馈','建议需要确认才会影响后续研究',body,saveButtons('生成偏好建议'),'research-feedback-form'));
}

document.addEventListener('click',async event=>{
  const el=event.target.closest('[data-action]');if(!el||el.disabled)return;
  const {action,id,mode,key,run,choice}=el.dataset;
  try {
    if(action==='book-page'){bookState[mode][key]=id||null;return await render();}
    if(action==='record-fill')return fillForm(mode,id||'',el.dataset.cycle||'');
    if(action==='import-fills')return importFillForm(mode);
    if(action==='portfolio-summary')return await showPortfolioSummary(mode,id);
    if(action==='void-fill'){
      const body=`<input type="hidden" name="mode" value="${esc(mode)}"><input type="hidden" name="id" value="${esc(id)}">${inputField('撤销原因','note','','required maxlength="160"')}`;
      return openModal(dialog('撤销成交记录','保留原成交和撤销原因，持仓重新核算',body,saveButtons('登记撤销'),'portfolio-void-form'));
    }
    if(action==='policy-originals')return await openPolicyOriginals(id||null);
    if(action==='show-originals')return await showOriginalPacket(run,id);
    if(action==='control-details')return await controlDetails(id||null);
    if(action==='edit-research-feedback')return await editFeedback(run||'');
    if(action==='feedback-decision'){
      await send('/api/v2/research-feedback/'+encodeURIComponent(id),{action:choice,confirm:true});
      await editFeedback();toast(choice==='accept'?'已更新长期研究偏好':'已略过');
    }
    if(action==='filing-collection'){
      const b=researchState.selected?.data?.bundle;
      const end=b?.market?.market_day||new Date().toISOString().slice(0,10);
      const start=new Date(Date.parse(end)-120*86400000).toISOString().slice(0,10);
      const body=`<input type="hidden" name="symbol" value="${esc(id)}">${selectField('获取内容','source','cninfo_text',[['cninfo_text','读取已存公告的原文'],['cninfo_announcements','更新这只股票的公告索引']])}
        <div class="research-settings-fields">${inputField('起始日期','start',start,'type="date" required')}${inputField('结束日期','end',end,'type="date" required')}</div>`;
      return openModal(dialog('补取公司原件',id+' · 使用现有巨潮来源',body,saveButtons('登记采集任务'),'filing-collection-form'));
    }
  } catch(error){toast(error.message,true);}
});

document.addEventListener('submit',async event=>{
  const form=event.target;
  if(!['portfolio-fill-form','portfolio-import-form','portfolio-void-form','policy-original-form','research-feedback-form','filing-collection-form'].includes(form.id))return;
  event.preventDefault();const button=form.querySelector('[type=submit]');if(button.disabled)return;button.disabled=true;
  const v=Object.fromEntries(new FormData(form));
  try {
    if(form.id==='policy-original-form'){
      const result=await send('/api/v2/policy-originals',{url:v.url,terms:splitValues(v.terms)});
      await openPolicyOriginals();toast(result.added?'已登记官方原文':'已复用原文，术语已保存');return;
    }
    if(form.id==='research-feedback-form'){
      await send('/api/v2/research-feedback',v);await editFeedback(v.run_id);toast('已生成待确认建议');return;
    }
    if(form.id==='filing-collection-form'){
      await send('/api/collection/jobs',{source:v.source,start:v.start,end:v.end,symbols:[v.symbol]});
      closeModal();toast('巨潮采集任务已登记');return;
    }
    const mode=v.mode;delete v.mode;
    const path='/api/v2/portfolio/'+mode;
    if(form.id==='portfolio-fill-form'){
      await send(path+'/fills',{items:[{...v,quantity:Number(v.quantity)}]});
    }else if(form.id==='portfolio-import-form'){
      const result=await send(path+'/import',{csv:v.csv});toast(`新增 ${result.inserted.length} 笔，重复 ${result.repeated.length} 笔`);
    }else{
      await send(path+'/fills/'+encodeURIComponent(v.id)+'/void',{note:v.note});
    }
    bookState[mode]={after:null,before:null};closeModal();
    if(location.hash!==`#tracking?book=${mode}`)location.hash=`tracking?book=${mode}`;
    else await render();
    if(form.id!=='portfolio-import-form')toast('账本已更新');
  }catch(error){form.querySelector('#form-error').textContent=error.message;}
  finally{button.disabled=false;}
});
