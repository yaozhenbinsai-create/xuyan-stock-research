'use strict';

const ICONS = {
  overview:'<rect x="3" y="3" width="7" height="7" rx="1.4"/><rect x="14" y="3" width="7" height="7" rx="1.4"/><rect x="3" y="14" width="7" height="7" rx="1.4"/><rect x="14" y="14" width="7" height="7" rx="1.4"/>',
  database:'<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v7c0 4 16 4 16 0V5M4 12v7c0 4 16 4 16 0v-7"/>',
  calendar:'<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M7 3v4m10-4v4M3 11h18M7 15h2m4 0h2m2 3h1"/>',
  spark:'<path d="m12 3 2.8 6.2L21 12l-6.2 2.8L12 21l-2.8-6.2L3 12l6.2-2.8L12 3Z"/><path d="m19 2 .8 2.2L22 5l-2.2.8L19 8l-.8-2.2L16 5l2.2-.8L19 2Z"/>',
  layers:'<path d="m12 3 10 5-10 5L2 8l10-5ZM2 12l10 5 10-5M2 16l10 5 10-5"/>',
  search:'<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>',
  refresh:'<path d="M20 8a8 8 0 0 0-14-3L3 8m0-5v5h5m-4 8a8 8 0 0 0 14 3l3-3m0 5v-5h-5"/>',
  arrow:'<path d="M5 12h14m-5-5 5 5-5 5"/>',
  check:'<path d="m5 12 4 4L19 6"/>',
  clock:'<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  file:'<path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9l-6-6Zm0 0v6h6M8 13h8m-8 4h5"/>',
  trend:'<path d="M4 20V4m0 16h17M7 15l4-5 4 3 6-8m-5 0h5v5"/>',
  building:'<path d="M4 21V8l8-5 8 5v13M8 21v-6h8v6M9 9h.01M15 9h.01M9 12h.01M15 12h.01M2 21h20"/>',
  shield:'<path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6l8-3Z"/><path d="m8 12 3 3 5-6"/>',
  key:'<circle cx="8" cy="8" r="5"/><path d="m12 12 9 9m-3-3 3-3m-6 0 3-3"/>',
  plus:'<path d="M12 5v14M5 12h14"/>',
  edit:'<path d="m16 3 5 5L9 20l-6 1 1-6L16 3Zm-2 2 5 5"/>',
  close:'<path d="m6 6 12 12M6 18 18 6"/>',
  download:'<path d="M12 3v12m-5-5 5 5 5-5M4 15v5h16v-5"/>',
  play:'<path d="m8 4 13 8-13 8V4Z"/>',
  pause:'<path d="M8 5v14M16 5v14"/>',
  info:'<circle cx="12" cy="12" r="9"/><path d="M12 11v6m0-10h.01"/>',
  folder:'<path d="M3 7V5h7l2 3h9v12H3V7Z"/>',
  users:'<circle cx="9" cy="8" r="4"/><path d="M2 21v-2a7 7 0 0 1 14 0v2m0-17a4 4 0 0 1 0 8m3 3a7 7 0 0 1 3 6"/>',
};
const icon = name => `<svg class="icon" viewBox="0 0 24 24" aria-hidden="true">${ICONS[name] || ICONS.file}</svg>`;
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const num = value => value == null ? '—' : Number(value).toLocaleString('zh-CN');
const price = value => value == null ? '—' : Number(value).toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2});
const amount = value => value == null ? '—' : price(value / 10000);
const chip = (text,tone='') => `<span class="chip ${tone}">${esc(text)}</span>`;
const dsIcons = {daily:'trend',quotes:'clock',basics:'building',industry:'layers',financials:'file',announcements:'file',minutes:'trend',fund_flows:'layers',calendar:'calendar',actions:'trend',concept:'layers',area:'building',policies:'file'};
const english = {daily:'DAILY BARS',quotes:'MARKET SNAPSHOTS',basics:'SECURITIES',industry:'CLASSIFICATION',financials:'FINANCIALS',announcements:'DISCLOSURES',minutes:'INTRADAY BARS',fund_flows:'FUND FLOW',calendar:'TRADING CALENDAR',actions:'CORPORATE ACTIONS',concept:'CONCEPTS',area:'REGIONS',policies:'POLICY DOCUMENTS'};
const pageGroups = [
  {name:'投研流程',pages:[
    {id:'board',name:'股票看板',icon:'overview',purpose:'观察池、命中率与市场局势'},
    {id:'research',name:'AI研究与选股',icon:'spark',purpose:'看本轮结果和选股理由'},
    {id:'methods',name:'逐股核对',icon:'layers',purpose:'核对入围理由和后续条件'},
    {id:'tracking',name:'股票池',icon:'trend',purpose:'查买入与观察名单'},
    ]},
  {name:'复盘评估',pages:[
    {id:'review',name:'复盘',icon:'clock',purpose:'每日和每月复盘'},
    {id:'archive',name:'历史报告',icon:'file'},
  ]},
  {name:'数据管理',pages:[
    {id:'overview',name:'数据概览',icon:'overview'},
    {id:'collection',name:'每日采集',icon:'calendar'},
    {id:'assets',name:'数据资产',icon:'database'},
    ]},
  {name:'系统',pages:[
    {id:'ai',name:'AI 设置',icon:'key'},
  ]},
];
const pages = pageGroups.flatMap(group=>group.pages);
const state = {page:'research',assetDataset:'daily',assetFilter:{start:'',end:'',symbol:''},assetOffset:0,assetLimit:50,assetCursor:null,assetHistory:[],assetNext:null,cursorMode:false,storage:null,aiTab:'models',collectionTab:'schedules',overview:null,ai:null,collection:null,instance:null,recordTotal:0};
const main = document.getElementById('main');
const modal = document.getElementById('modal');
const modalContent = document.getElementById('modal-content');
const readingTrail = [];
let pageRequest = 0, toastTimer, lastFocus, renderedPage, renderedRoute;

function fillIcons(root=document){root.querySelectorAll('[data-icon]').forEach(el=>el.innerHTML=icon(el.dataset.icon));}
fillIcons();
document.querySelector('.skip-link').addEventListener('click',event=>{event.preventDefault();main.focus();});
function toast(message,error=false){const el=document.getElementById('toast');clearTimeout(toastTimer);el.textContent=message;el.className=`visible${error?' error':''}`;toastTimer=setTimeout(()=>el.className='',4500);}
async function api(path, options={}){
  const r=await fetch(path,{...options,headers:{'Content-Type':'application/json',...options.headers}});
  let result;try{result=await r.json();}catch{throw new Error('服务返回异常，请稍后刷新');}
  if(!r.ok){const error=new Error(typeof result.detail==='string'?result.detail:'操作未完成，请核对输入后重试');error.status=r.status;throw error;}
  return result;
}
const send=(path,data,method='POST')=>api(path,{method,body:JSON.stringify(data)});
function heading(eyebrow,title,description,actions=''){
  return `<div class="page-heading"><div><div class="eyebrow">${eyebrow}</div><h1>${title}</h1><p>${description}</p></div><div>${actions}</div></div>`;
}
function empty(title='这里还没有数据',desc='请调整筛选条件，或到每日采集更新资料。'){return `<div class="empty">${icon('folder')}<h3>${esc(title)}</h3><p>${esc(desc)}</p></div>`;}
function openModal(html){
  if(!modal.open)lastFocus=document.activeElement;
  readingTrail.length=0;
  delete modal.dataset.reading;
  modalContent.innerHTML=html;
  window.initMotion?.(modalContent);
  if(!modal.open)modal.showModal();
  modal.scrollTop=0;
}
function focusReadingTitle(){
  const title=modalContent.querySelector('#modal-title');
  const header=modalContent.querySelector('.dialog-header');
  if(header)modal.style.setProperty('--reader-header',header.offsetHeight+'px');
  if(title){title.tabIndex=-1;title.focus({preventScroll:true});}
}
function openReading(html,{replace=false}={}){
  if(!modal.open)lastFocus=document.activeElement;
  if(modal.open&&modal.dataset.reading&& !replace){
    readingTrail.push({nodes:[...modalContent.childNodes],scroll:modal.scrollTop,focus:document.activeElement});
    if(readingTrail.length>8)readingTrail.shift();
  }
  modal.dataset.reading='true';
  modalContent.innerHTML=html;
  window.initMotion?.(modalContent);
  if(readingTrail.length){
    modalContent.querySelector('.dialog-header')?.insertAdjacentHTML('afterbegin',
      '<button class="reading-back" type="button" data-action="reading-back" aria-label="返回上一层">← <span>上一层</span></button>');
  }
  if(!modal.open)modal.showModal();
  modal.scrollTop=0;
  focusReadingTitle();
}
function backReading(){
  const previous=readingTrail.pop();
  if(!previous)return closeModal();
  modalContent.replaceChildren(...previous.nodes);
  if(!readingTrail.length)modalContent.querySelector('.reading-back')?.remove();
  modal.style.setProperty('--reader-header',(modalContent.querySelector('.dialog-header')?.offsetHeight||70)+'px');
  modal.scrollTop=previous.scroll;
  if(previous.focus?.isConnected)previous.focus.focus({preventScroll:true});
  else focusReadingTitle();
}
function closeModal(){
  readingTrail.length=0;
  modal.close();
  lastFocus?.focus({preventScroll:true});
}
function dialog(title,subtitle,body,footer='',formId=''){
  return `${formId?`<form id="${formId}">`:''}<div class="dialog-header"><div><h2 id="modal-title">${esc(title)}</h2><p>${esc(subtitle)}</p></div><button class="icon-btn" type="button" data-action="close" aria-label="关闭">${icon('close')}</button></div><div class="dialog-body">${body}<p id="form-error" class="error-text" role="alert"></p></div>${footer?`<div class="dialog-footer">${footer}</div>`:''}${formId?'</form>':''}`;
}
const saveFooter=()=>'<button class="btn" type="button" data-action="close">取消</button><button class="btn primary" type="submit">保存设置</button>';
const inputField=(label,name,value='',attrs='')=>`<label class="field">${label}<input name="${name}" value="${esc(value)}" ${attrs}></label>`;
const selectField=(label,name,value,options)=>`<label class="field">${label}<select name="${name}">${options.map(([val,text])=>`<option value="${esc(val)}" ${String(value??'')===String(val)?'selected':''}>${esc(text)}</option>`).join('')}</select></label>`;
const checkLabel=(label,name,on)=>`<label class="check-label"><input type="checkbox" name="${name}" ${on?'checked':''}>${label}</label>`;

function instanceBanner(value=state.instance){
  if(!value)return '';
  const paused=!value.workers_enabled;
  const ready=value.collection_clock&&value.research_clock; if(!paused&&ready)return '';
  const label=paused?`${value.name} · 本版本自动任务已暂停`:ready?`${value.name} · 本版本负责自动执行`:`${value.name} · 自动任务状态待核对`;
  const note=paused?'本实例已暂停后台工作。这里可以保存采集、分时研究和复盘计划，启用后台并重启服务后执行。':
    ready?'按本机保存的计划执行采集、分时研究以及每日和月度复盘。':'本版本已配置自动执行，时钟状态尚未全部就绪，请核对服务状态。';
  return `<div class="notice ${paused||!ready?'amber':''}" role="status" aria-label="本版本自动执行状态" data-instance-status>${icon('info')}<div><strong>${esc(label)}</strong><p>${esc(note)}</p></div></div>`;
}
function sharedScheduleNote(){
  return '<p class="form-note">计划保存在本机数据库。后台工作开启后会执行已启用的计划。</p>';
}

async function render(){
  const ticket=++pageRequest;
  clearTimeout(researchState.poll);
  const [hash,query='']=(location.hash.slice(1)||'board').split('?');
  state.page=pages.some(p=>p.id===hash)?hash:pages[0].id;
  if(state.page!==hash)history.replaceState(null,'',`#${state.page}`);
  const params=new URLSearchParams(query);if(params.has('dataset')&&state.assetDataset!==params.get('dataset')){state.assetDataset=params.get('dataset');resetAssetPages();}
  if(state.page==='ai'){
    const tab=params.get('tab');state.aiTab=['models','plans','agents','usage'].includes(tab)?tab:'models';
  }
  main.dataset.page=state.page;
  const navigation=document.getElementById('navigation');
  if(navigation.dataset.page!==state.page){
    navigation.dataset.page=state.page;
    navigation.innerHTML=pageGroups.map(group=>`
    <div class="nav-group${window.navGroupOpen?.(group.name,group.pages.some(p=>p.id===state.page))===false?' is-collapsed':''}" role="group" aria-label="${esc(group.name)}" data-group="${esc(group.name)}">
      <button type="button" class="nav-group-title" data-nav-toggle aria-expanded="true">${esc(group.name)}</button>
      <div class="nav-group-links"><div class="nav-group-inner">
        ${group.pages.map(p=>`<a class="nav-item ${state.page===p.id?'active':''}" data-nav-page="${p.id}" href="#${p.id}" title="${esc(p.name)}" ${state.page===p.id?'aria-current="page"':''}>${icon(p.icon)}<span>${esc(p.name)}</span></a>`).join('')}</div>
      </div>
    </div>`).join('');
    if(navigation.scrollWidth>navigation.clientWidth){
      const current=navigation.querySelector('[aria-current]');
      const start=current.getBoundingClientRect().left-navigation.getBoundingClientRect().left+navigation.scrollLeft;
      navigation.scrollLeft=start-(navigation.clientWidth-current.offsetWidth)/2;
    }
  }
  const title=pages.find(p=>p.id===state.page).name;document.getElementById('breadcrumb-title').textContent=title;document.title=`${title} · 序研`;
  const route=location.hash;
  const samePage=renderedPage===state.page;
  const restoreSessionFocus=samePage&&document.activeElement?.matches('.session-tab');
  const pagingPool=samePage&&state.page==='tracking'&&document.activeElement?.closest('.pool-pagination');
  main.setAttribute('aria-busy','true');
  if(!samePage){renderedPage=null;main.innerHTML='<div class="loading"><span class="spinner"></span> 正在读取已保存资料…</div>';}
  try{
    const fn={board:renderBoard,research:renderResearch,tracking:renderTracking,review:renderResearchReview,methods:renderMethods,archive:renderResearchArchive,overview:renderOverview,assets:renderAssets,collection:renderCollection,ai:renderAI}[state.page];
    const [html,instance]=await Promise.all([fn(ticket),api('/api/instance')]);
    if(ticket!==pageRequest||route!==location.hash||html==null)return;
    state.instance=instance;main.innerHTML=instanceBanner(instance)+html;renderedPage=state.page;renderedRoute=route;window.initMotion?.(main);
    const flow=main.querySelector('.research-flow');
    for(const link of navigation.querySelectorAll('[data-nav-page]')){
      const page=link.dataset.navPage;
      const sameRun=flow?.querySelector('a[data-flow-step="'+page+'"]');
      link.setAttribute('href',sameRun?.getAttribute('href')||'#'+page);
    }
    if(!samePage)window.scrollTo({top:0,behavior:'instant'});
    if(restoreSessionFocus)main.querySelector('.session-tab[aria-current="page"]')?.focus({preventScroll:true});
    if(pagingPool){
      const heading=main.querySelector('.pool-results-heading h2');
      if(heading){heading.tabIndex=-1;heading.focus({preventScroll:true});heading.scrollIntoView({block:'start'});}
    }
  }catch(e){if(ticket===pageRequest&&route===location.hash){
    if(samePage&&route===renderedRoute)toast(e.message,true);
    else {
      renderedPage=null;
      main.innerHTML=heading('WORKSPACE','暂时无法读取工作台',esc(e.message),'<button class="btn" data-action="refresh">重新读取</button>')+empty('服务未返回资料','已有资料不会因此被清空，请稍后重试。');
    }
  }}finally{if(ticket===pageRequest){
    main.removeAttribute('aria-busy');
    main.querySelector('.session-tab[aria-busy]')?.removeAttribute('aria-busy');
  }}
}

function metric(label,value,unit,note,ic){return `<div class="metric"><div class="metric-label">${icon(ic)}${label}</div><div class="metric-value numeric">${value}<small>${unit}</small></div><div class="metric-note">${note}</div></div>`;}
function coverageChart(days){
  if(!days.length)return empty('日线正在准备','已保存的正式日线会在这里显示。');
  const w=600,h=150,p=12,max=Math.max(...days.map(d=>d.n),1),bw=(w-24)/days.length;
  const lines=[0,.5,1].map(f=>`<line x1="0" y1="${h-12-f*118}" x2="600" y2="${h-12-f*118}" stroke="#e7e8f2" stroke-dasharray="3 4"/>`).join('');
  const bars=days.map((d,i)=>{const bh=d.n/max*118;return `<rect x="${p+i*bw+3}" y="${h-12-bh}" width="${Math.max(bw-8,3)}" height="${bh}" rx="2.5" fill="${i===days.length-1?'#525984':'#a9aecd'}"><title>${esc(d.day)}：${num(d.n)} 条正式日线</title></rect>`;}).join('');
  return `<svg class="coverage-chart" viewBox="0 0 ${w} ${h}" role="img" aria-label="近 ${days.length} 个交易日已存日线条数，最新 ${esc(days.at(-1).day)} 为 ${num(days.at(-1).n)} 条">${lines}${bars}</svg><div class="chart-caption"><span>${esc(days[0].day)}</span><span>每根柱代表一个交易日 · 从 0 开始</span><span>${esc(days.at(-1).day)}</span></div>`;
}
async function renderOverview(){
  const o=state.overview=await api('/api/overview');const stats=o.stats.filter(s=>s.count);const latest=o.daily_coverage.at(-1);
  const sourceCount=o.sources.filter(s=>s.enabled).length;
  const preview=o.schedules.filter(s=>s.enabled&&!s.needs_symbols&&!s.interval&&s.kind==='collect').slice().sort((a,b)=>{const now=o.time.slice(11,16);return Number(a.at<now)-Number(b.at<now)||a.at.localeCompare(b.at);}).slice(0,4);
  const date=new Intl.DateTimeFormat('zh-CN',{year:'numeric',month:'long',day:'numeric',weekday:'long',timeZone:'Asia/Shanghai'}).format(new Date(o.time));
  return heading('YOUR RESEARCH, GROUNDED.','数据工作台','让每一份资料都有来处，让每一次研究都有基础。',`<div class="date-label">${esc(date)}</div><div class="heading-actions">${chip(!o.instance.collection_clock?'本版本自动采集已暂停':o.paused?'共享采集计划已暂停':'本版本自动采集已启用',!o.instance.collection_clock||o.paused?'amber':'')}<button class="btn" data-action="refresh">${icon('refresh')}刷新</button></div>`)
    +(o.test_mode?`<div class="notice amber">${icon('info')}<p>隔离界面验收 · 当前显示测试资料，正式数据未参与写入检查。</p></div>`:'')
    +renderOverviewMetrics(o,stats,sourceCount)
    +`<section class="split"><div class="panel"><div class="panel-header"><div><h2>每日数据沉淀</h2><p>近 ${o.daily_coverage.length} 个交易日 · 已保存的正式日线</p></div><span class="chart-legend">日线条数</span></div><div class="chart-summary"><strong class="numeric">${num(latest?.n)}</strong><span>条 <span class="footer-dot">·</span> ${esc(latest?.day||'尚未采集')}</span></div><div class="chart-wrap">${coverageChart(o.daily_coverage)}</div></div><div class="panel"><div class="panel-header"><div><h2>每日采集</h2><p>沿用已验证的来源与采集节奏</p></div>${icon('clock')}</div><ul class="schedule-preview">${preview.map(s=>`<li><span class="time-block">${esc(s.at)}</span><div class="schedule-line"><strong>${esc(s.name)}</strong><small>${esc(o.sources.find(x=>x.id===s.source)?.provider||'本地')}</small></div>${chip(!o.instance.collection_clock?'本版本已暂停':o.paused?'共享计划已暂停':s.trading_only?'交易日':'每日',!o.instance.collection_clock||o.paused?'gray':'')}</li>`).join('')}</ul><a class="panel-link" href="#collection">查看全部采集计划 ${icon('arrow')}</a></div></section>`
    +`<div class="section-heading"><h2>你的数据资产</h2><p>${sourceCount} 个来源已启用 <span class="footer-dot">·</span> 原始资料可追溯</p></div><div class="panel"><div class="table-wrap"><table><thead><tr><th>数据类型</th><th>有效记录</th><th>已存日期范围</th><th>最新资料</th><th></th></tr></thead><tbody>${stats.map(s=>`<tr><td><div class="data-name"><span class="data-icon">${icon(dsIcons[s.dataset])}</span><div><strong>${esc(s.name)}</strong><small>${english[s.dataset]||s.dataset.toUpperCase()}</small></div></div></td><td class="numeric">${num(s.count)} <span class="muted">条</span></td><td class="muted numeric">${esc(s.first)} <span class="footer-dot">—</span> ${esc(s.last)}</td><td>${chip(s.last==='2026-12-31'&&s.dataset==='calendar'?'全年日历':s.last||'未开始','gray')}</td><td class="right"><a class="btn ghost small" href="#assets?dataset=${s.dataset}">查看 ${icon('arrow')}</a></td></tr>`).join('')}</tbody></table></div><div class="table-foot"><span>重复获取合并记录，真实修订保留版本。</span><a href="#assets">查看全部数据 ↗</a></div></div>`
    +`<div class="mini-overview"><a class="panel mini-box" href="#ai"><div><strong>让 AI 按你的方案协作</strong><p>${o.models} 个文本模型 · ${o.plans} 个调用方案 · 保留原主备顺序</p></div><span class="round-icon">${icon('spark')}</span></a><a class="panel mini-box" href="#collection"><div><strong>管理每日数据采集</strong><p>查看采集计划、运行进度和数据来源</p></div><span class="round-icon">${icon('calendar')}</span></a></div>`;
}

function recordColumns(ds){
  const base=[['日期',r=>esc(r.day)],['股票代码',r=>esc(r.symbol)]];
  const extra={
    daily:[['开盘',r=>price(r.open)],['最高',r=>price(r.high)],['最低',r=>price(r.low)],['收盘',r=>price(r.close)],['成交额 / 万元',r=>amount(r.amount)],['状态',r=>r.suspended?'停牌':'有记录']],
    quotes:[['股票名称',r=>esc(r.name)],['报价',r=>price(r.close)],['涨跌 / %',r=>price(r.pct ?? (r.close!=null&&r.preclose>0?(r.close/r.preclose-1)*100:null))],['来源时点',r=>esc(r.source_time?.slice(11)||'—')]],
    basics:[['股票名称',r=>esc(r.name)],['板块',r=>esc(r.board)],['上市日期',r=>esc(r.listed||'—')],['来源状态',r=>esc(r.status??'—')]],
    industry:[['行业',r=>esc(r.group)],['分类体系',r=>esc(r.taxonomy)],['来源生效日',r=>esc(r.source_effective_date||'未知')]],
    announcements:[['公告标题',r=>`<div class="truncate" title="${esc(r.title)}">${esc(r.title)}</div>`],['原文状态',r=>r.text?'已有正文摘录':'仅有标题 / 链接']],
    financials:[['报告类型',r=>esc({profit:'盈利',balance:'偿债',cashflow:'现金流',growth:'成长'}[r.report_type]||r.report_type)],['报告期',r=>esc(r.report_date)],['实际披露日期',r=>esc(r.published)]],
    minutes:[['周期',r=>`${esc(r.period)} 分钟`],['收盘',r=>price(r.close)],['成交额 / 万元',r=>amount(r.amount)],['来源时点',r=>esc(r.source_time)]],
    fund_flows:[['主力净额 / 万元',r=>amount(r.net_main)],['统计口径',r=>esc(r.basis)],['时间精度',r=>r.time_precision==='day'?'日统计':esc(r.time_precision)]],
    calendar:[['是否交易',r=>r.trading?'交易日':'非交易日'],['覆盖市场',r=>esc((r.markets||[]).join(' / '))]],
    actions:[['记录类型',r=>esc(r.record_kind||'—')],['覆盖起',r=>esc(r.coverage_from||'—')],['覆盖至',r=>esc(r.coverage_through||'—')],['事件数',r=>num(r.event_count)]],
    concept:[['概念',r=>esc(r.group)],['分类体系',r=>esc(r.taxonomy)],['来源生效日',r=>esc(r.source_effective_date||'未知')]],
    area:[['地区',r=>esc(r.group)],['分类体系',r=>esc(r.taxonomy)],['来源生效日',r=>esc(r.source_effective_date||'未知')]],
    policies:[['政策标题',r=>`<div class="truncate" title="${esc(r.title)}">${esc(r.title)}</div>`],['发布日期',r=>esc(r.published)],['生效日',r=>esc(r.effective_day||'—')],['原文范围',r=>esc(r.text_scope||'—')]],
  };
  return [...(['calendar','policies'].includes(ds)?[base[0]]:base),...(extra[ds]||[]),['来源',r=>`<span class="plain-tag">${esc(r._source)}</span>`]];
}
function resetAssetPages(){state.assetOffset=0;state.assetCursor=null;state.assetHistory=[];state.assetNext=null;}
async function renderAssets(){
  const [assetReply,storage]=await Promise.all([api('/api/assets'),api('/api/storage').catch(()=>null)]);
  state.storage=storage;
  const stats=assetReply.items.filter(s=>s.count);
  if(!stats.some(s=>s.dataset===state.assetDataset)&&stats.length){state.assetDataset=stats[0].dataset;resetAssetPages();}
  const ds=state.assetDataset,f=state.assetFilter;
  const params=new URLSearchParams({dataset:ds,limit:state.assetLimit});
  if(state.assetCursor)params.set('cursor',state.assetCursor);else if(state.assetOffset)params.set('offset',state.assetOffset);
  Object.entries(f).forEach(([k,v])=>{if(v)params.set(k,v);});
  let r;
  try{r=await api('/api/records?'+params);}catch(error){
    if(!state.assetCursor||![409,422].includes(error.status))throw error;
    resetAssetPages();params.delete('cursor');params.delete('offset');
    r=await api('/api/records?'+params);toast('资料已更新，已回到第一页。');
  }
  state.recordTotal=r.total;state.assetNext=r.next_cursor||null;state.cursorMode='has_more' in r;
  if(Number.isInteger(r.offset))state.assetOffset=r.offset;
  const selected=stats.find(s=>s.dataset===ds),cols=recordColumns(ds);
  const nextDisabled=state.cursorMode?!r.has_more:state.assetOffset+state.assetLimit>=r.total;
  const table=r.items.length?`<div class="table-wrap"><table><thead><tr>${cols.map(c=>`<th>${c[0]}</th>`).join('')}<th></th></tr></thead><tbody>${r.items.map(row=>`<tr class="record-row" data-action="record" data-id="${row._id}" tabindex="0" role="button" aria-label="查看 ${esc(row.day)} ${esc(row.symbol)} 的资料">${cols.map(c=>`<td class="numeric">${c[1](row)}</td>`).join('')}<td class="muted">↗</td></tr>`).join('')}</tbody></table></div>`:empty();
  return heading('DATA LIBRARY','数据资产','让每一份资料，都有价值和来路。',`<button class="btn" data-action="refresh">${icon('refresh')}刷新资料</button>`)
    +(typeof renderLibraryHero==='function'?renderLibraryHero(storage,stats):'')
    +`<div class="dataset-tabs" role="group" aria-label="数据类型">${stats.map(s=>`<button class="dataset-tab ${ds===s.dataset?'active':''}" data-action="dataset" data-id="${s.dataset}" aria-pressed="${ds===s.dataset}">${esc(s.name)}<small>${num(s.count)}</small></button>`).join('')}</div>`
    +`<form class="filters" id="asset-filters"><label>开始日期<input type="date" name="start" value="${esc(f.start)}"></label><label>结束日期<input type="date" name="end" value="${esc(f.end)}"></label>${!['calendar','policies'].includes(ds)?`<label>股票代码<input name="symbol" class="stock-input" placeholder="例如 sh.600000" value="${esc(f.symbol)}" spellcheck="false"></label>`:''}<button class="btn primary" type="submit">${icon('search')}筛选</button><button class="btn" type="button" data-action="reset-filters">重置</button><button class="btn filter-tail" type="button" data-action="export">${icon('download')}导出当前范围</button></form>`
    +`<div class="panel"><div class="compact-info"><span><strong>${esc(selected?.name||'数据')}</strong></span><span>共 <strong class="numeric">${num(r.total)}</strong> 条有效记录</span><span>价格单位：元 · 成交额展示为万元</span></div>${table}<div class="table-foot"><span>显示 ${r.total?state.assetOffset+1:0}—${Math.min(state.assetOffset+r.items.length,r.total)} / ${num(r.total)} 条</span><label class="page-size-control">每页<select id="asset-page-size" aria-label="每页记录数">${[25,50,100,200].map(n=>`<option value="${n}" ${n===state.assetLimit?'selected':''}>${n} 条</option>`).join('')}</select></label><div class="pagination"><button class="btn small" data-action="prev-page" ${!state.assetOffset?'disabled':''}>上一页</button><span>${Math.floor(state.assetOffset/state.assetLimit)+1}</span><button class="btn small" data-action="next-page" ${nextDisabled?'disabled':''}>下一页</button></div></div></div>`
    +`<div class="notice">${icon('info')}<p>${ds==='industry'?'分类保留首次取得时点；来源没有明确生效日时，不推定为历史成员。':'默认展示每个业务唯一键的当前记录。点击记录可查看必要字段、来源与真实修订。'}</p></div>`;
}

async function renderCollection(){
  const c=state.collection=await api('/api/collection');
  const enabled=c.schedules.filter(s=>s.enabled).length;
  let html=heading('DAILY COLLECTION','每日采集','可靠地积累资料，从每一天开始。',`<div class="heading-actions">${chip(!c.instance.collection_clock?'本版本自动采集已暂停':c.paused?'共享采集计划已暂停':'本版本自动采集已启用',!c.automatic_collection_enabled?'amber':'')}<button class="btn ${c.paused?'primary':''}" data-action="automation">${icon(c.paused?'play':'pause')}${c.paused?'启用共享采集计划':'暂停共享采集计划'}</button><button class="btn" data-action="manual-collect">${icon('plus')}手动采集</button></div>`)
    +`<div class="tabs" role="tablist" aria-label="采集管理">${[['schedules','采集计划',c.schedules.length],['jobs','任务记录',c.jobs.length],['sources','数据来源',c.sources.length]].map(([id,label,n])=>`<button class="tab ${state.collectionTab===id?'active':''}" role="tab" aria-selected="${state.collectionTab===id}" data-action="collection-tab" data-id="${id}">${label}<small>${n}</small></button>`).join('')}</div>`;
  if(state.collectionTab==='schedules'){
    html+=`<div class="panel"><div class="compact-info"><span><strong>${enabled}</strong> 个共享计划已启用</span><span>时区：上海 UTC+8</span><span>本版本自动采集 ${c.automatic_collection_enabled?'运行中':'已暂停'}</span></div><div class="table-wrap"><table class="timeline-table"><thead><tr><th>时间</th><th>采集内容</th><th>执行范围</th><th>共享启用</th><th>操作</th></tr></thead><tbody>${c.schedules.map(s=>`<tr><td class="time-cell">${esc(s.at)}${s.interval?'<div class="schedule-sub">每 '+s.interval+' 秒</div>':''}</td><td><div class="schedule-name">${esc(s.name)}</div><div class="schedule-sub">${esc(c.sources.find(x=>x.id===s.source)?.name||'本地完整备份')}</div></td><td>${s.needs_symbols?chip('等待关注股票','amber'):chip(s.trading_only?'交易日':'每日','gray')}</td><td><button class="switch ${s.enabled?'on':''}" role="switch" aria-checked="${s.enabled}" aria-label="${esc(s.name)}启用开关" data-action="toggle-schedule" data-id="${s.id}"></button></td><td><button class="btn ghost small" data-action="edit-schedule" data-id="${s.id}">设置</button><button class="btn small" data-action="run-schedule" data-id="${s.id}" ${s.needs_symbols?'disabled':''}>${icon('play')}执行一次</button></td></tr>`).join('')}</tbody></table></div></div>`
      +`<div class="mini-overview"><div class="panel mini-box"><div><strong>采集关注股票</strong><p>${c.watchlist.length?esc(c.watchlist.join('、')):'尚未设置；关注股票的分钟与财务计划等待选择。'}</p></div><button class="btn small" data-action="watchlist">设置</button></div><div class="panel mini-box"><div><strong>正式日线与收盘快照分别保存</strong><p>正式日线等待来源发布，未取得时保留等待状态。</p></div><span class="round-icon">${icon('shield')}</span></div></div>`;
  }else if(state.collectionTab==='jobs'){
    html+=`<div class="panel">${c.jobs.length?`<div class="table-wrap"><table><thead><tr><th>采集任务</th><th>范围</th><th>进度</th><th>状态</th><th>操作</th></tr></thead><tbody>${c.jobs.map(j=>`<tr><td><button class="btn ghost small" data-action="job-detail" data-id="${j.id}">${esc(j.name)} ↗</button><div class="schedule-sub">${esc(j.created.slice(0,19).replace('T',' '))}</div></td><td>${esc(j.start)} — ${esc(j.end)}</td><td class="numeric">${j.done} / ${j.total}<div class="job-progress"><i style="width:${j.total?j.done/j.total*100:0}%"></i></div><div class="schedule-sub">新增 ${num(j.added)} · 重复 ${num(j.unchanged)}</div></td><td>${jobChip(j.status)}<div class="schedule-sub">${esc(j.message)}</div></td><td>${['queued','running','waiting'].includes(j.status)?`<button class="btn small" data-action="job-pause" data-id="${j.id}">暂停</button>`:['paused','failed','interrupted'].includes(j.status)?`<button class="btn small" data-action="job-resume" data-id="${j.id}">继续</button>`:''}</td></tr>`).join('')}</tbody></table></div>`:empty('这里将记录新系统的采集','新系统从独立的任务记录开始；旧任务没有迁入。')}</div>`;
  }else{
    html+=`<div class="notice">${icon('info')}<p>这里只保留原系统已经实测通过的免费来源。历史验证时间与本次成功采集时间分别留存，不自动切换至未验证接口。</p></div><div class="sources-grid">${c.sources.map(s=>`<article class="panel source-card"><div class="card-heading"><h3>${esc(s.name)}</h3>${chip(s.enabled?'已启用':'已停用',s.enabled?'':'gray')}</div><p>${esc(s.coverage)}</p><div class="meta"><span>请求间隔 ${s.min_interval} 秒</span><button class="btn small" data-action="edit-source" data-id="${s.id}">设置来源</button></div><div class="trace-label">上次成功 ${esc(s.last_success?.slice(0,10)||'未记录')} · ${s.automatic?'可自动采集':'仅手动使用'}</div></article>`).join('')}</div>`;
  }
  return html;
}
function jobChip(status){const labels={queued:'排队中',running:'正在采集',waiting:'等待重试',paused:'已暂停',completed:'已完成',failed:'未完成',partial:'部分资料未取得',interrupted:'已中断',cancelled:'已停止'};return chip(labels[status]||status,['failed','partial','interrupted'].includes(status)?'amber':['paused','cancelled'].includes(status)?'gray':'');}

async function renderAI(){
  const a=state.ai=await api('/api/ai');
  if(state.aiTab==='usage')return await renderModelUsage(a);
  return renderStudioAI(a,state.aiTab);
}

async function editModel(id){
  const m=state.ai.models.find(m=>m.id===Number(id))||{enabled:true,protocol:'openai',provider:'OpenAI Compatible'};
  const body=`<div class="fields-grid">${inputField('连接名称','name',m.name,'required maxlength="120"')}${inputField('模型标识','model_identifier',m.model_identifier,'required maxlength="160"')}${selectField('接入协议','protocol',m.protocol,[['openai','Chat Completions'],['responses','Responses'],['gemini','Gemini 原生'],['xai','Grok / xAI 原生'],['anthropic','Anthropic Messages'],['codex','本机官方账号']])}${inputField('服务商名称','provider',m.provider)}${selectField('模型系列','model_family',m.model_family||'custom',[['custom','自定义'],['gpt','GPT'],['gemini','Gemini'],['grok','Grok'],['claude','Claude']])}${selectField('接入方式','connection_type',m.protocol==='codex'?'account':m.connection_type||'api',[['api','官方 API'],['relay','中转站 / 聚合服务'],['self_hosted','自建服务'],['account','本机账号']])}
    <label class="field full">接口地址<input name="base_url" value="${esc(m.base_url)}" placeholder="https://…/v1 或 codex://chatgpt" spellcheck="false"><small>官方账号协议自动使用本机账号入口。</small></label>
    <label class="field full" data-api-key-field>API Key<input name="api_key" type="text" autocomplete="off" autocapitalize="off" spellcheck="false" maxlength="10000" placeholder="${m.has_api_key?'正在读取已保存的 Key…':'输入 API Key'}"><small data-key-note>${m.has_api_key?'明文显示，保存时加密。留空保留原 Key。':'填写后加密保存。'}</small></label>
    <p class="field full form-note" data-account-note hidden>本机官方账号沿用当前登录，无需 API Key。</p>
    ${selectField('思考等级','reasoning_effort',m.reasoning_effort,[['','模型默认'],...['none','minimal','low','medium','high','xhigh','max'].map(v=>[v,v])])}${inputField('请求超时 / 秒','timeout_seconds',m.timeout_seconds??'','type="number" min="10" max="600" placeholder="模型默认"')}${inputField('最大输出 token','max_output_tokens',m.max_output_tokens??'','type="number" min="1" placeholder="模型默认"')}${inputField('密钥环境变量（可选）','api_key_env',m.api_key_env)}${checkLabel('启用此模型','enabled',m.enabled)}<div class="field full" data-response-recovery>${checkLabel('支持后台响应与原请求查询','recoverable_responses',m.recoverable_responses)}<small>仅在服务商明确支持 Responses 后台模式时启用。响应将由该服务保存，用于超时后取回原结果。</small></div>
    <div class="field full model-diagnostics"><div class="model-probe-actions"><button type="button" class="btn small" data-action="model-test-editor">测试连接</button><button type="button" class="btn small" data-action="model-catalog">刷新模型目录</button></div>
    <small>测试连接会实际请求一次模型，可能产生少量用量。刷新目录不发起推理。</small>
    <select aria-label="选择目录中的模型" name="catalog_model" hidden></select><p class="model-probe-result" data-model-result role="status"></p></div></div>`;
  openModal(dialog(m.id?'编辑模型连接':'新增模型连接','可以先测试当前填写的配置，再决定是否保存。',body,saveFooter(),'model-form'));
  const form=modal.querySelector('form');form.dataset.id=m.id||'';
  syncModelProtocol(form);
  if(!m.has_api_key||m.protocol==='codex')return;
  const input=form.elements.api_key;
  try{
    const result=await send(`/api/ai/models/${m.id}/key`,{});
    if(modal.open&&form.isConnected&&!form.dataset.keyEdited)input.value=result.api_key;
  }catch(e){
    if(modal.open&&form.isConnected)form.querySelector('[data-key-note]').textContent=e.message;
  }finally{
    input.placeholder='留空保留原 Key';
  }
}
function syncModelProtocol(form){
  const official=form.elements.protocol.value==='codex';
  form.querySelector('[data-api-key-field]').hidden=official;
  form.querySelector('[data-account-note]').hidden=!official;
  form.elements.api_key_env.closest('label').hidden=official;
  const recovery=form.elements.protocol.value==='responses';
  form.querySelector('[data-response-recovery]').hidden=!recovery;
  if(!recovery)form.elements.recoverable_responses.checked=false;
  if(official)form.elements.connection_type.value='account';
  else if(form.elements.connection_type.value==='account')form.elements.connection_type.value='api';
}
function modelPayload(form){
  const f=new FormData(form);
  return {name:f.get('name'),model_identifier:f.get('model_identifier'),provider:f.get('provider'),protocol:f.get('protocol'),model_family:f.get('model_family')||'custom',connection_type:f.get('connection_type')||'api',recoverable_responses:f.get('protocol')==='responses'&&f.has('recoverable_responses'),base_url:f.get('base_url'),api_key:form.dataset.keyEdited?f.get('api_key')||null:null,api_key_env:f.get('api_key_env'),enabled:f.has('enabled'),capability:'文本',reasoning_effort:f.get('reasoning_effort')||null,timeout_seconds:f.get('timeout_seconds')?Number(f.get('timeout_seconds')):null,max_output_tokens:f.get('max_output_tokens')?Number(f.get('max_output_tokens')):null};
}
async function modelProbe(action,form,raw){
  const result=form.querySelector('[data-model-result]');
  const picker=form.querySelector('[name=catalog_model]');
  const buttons=form.querySelectorAll('[data-action="model-test-editor"],[data-action="model-catalog"]');
  if(form.dataset.probing)return;
  form.dataset.probing='1';buttons.forEach(b=>b.disabled=true);
  result.classList.remove('error-text');
  result.textContent=action==='catalog'?'正在读取模型目录…':'正在测试连接，等待模型返回…';
  if(action==='catalog')picker.hidden=true;
  try{
    const data=await send('/api/ai/models/'+action,raw);
    if(!modal.open||!form.isConnected)return;
    if(action==='catalog'){
      if(form.dataset.catalogChanged){result.textContent='连接设置已更改，请重新刷新模型目录。';return;}
      picker.replaceChildren(new Option('选择模型以填入标识',''),...data.models.map(id=>new Option(id,id)));
      picker.hidden=!data.models.length;
      result.textContent=data.models.length?`已读取 ${data.models.length} 个模型${data.more?'（当前返回的部分目录）':''}。实际可用性请点「测试连接」。`:'目录没有返回模型，可以手填模型标识。';
    }else{
      result.classList.toggle('error-text',!data.ok);
      result.textContent=data.message+` · ${(data.elapsed_ms/1000).toFixed(1)} 秒`+(data.call_id?` · 调用编号 ${data.call_id}`:'');
    }
  }catch(e){
    if(modal.open&&form.isConnected){
      if(action==='catalog'&&form.dataset.catalogChanged){result.textContent='连接设置已更改，请重新刷新模型目录。';return;}
      result.classList.add('error-text');result.textContent=e.message;
    }
  }finally{
    delete form.dataset.probing;buttons.forEach(b=>b.disabled=false);
  }
}
function testSavedModel(id){
  const m=state.ai.models.find(m=>m.id===Number(id));if(!m)return;
  openModal(dialog('测试连接',`${m.name} · ${m.model_identifier}`,'<p class="model-probe-result" data-model-result role="status"></p><a href="#ai?tab=usage" data-action="close">查看调用与 TOKEN</a>','<button type="button" class="btn" data-action="close">关闭</button>','model-test-form'));
  return modelProbe('test',modal.querySelector('form'),{model_id:m.id});
}
function editPlan(id){
  const p=state.ai.plans.find(p=>p.id===Number(id))||{enabled:true,model_ids:[]};const order=[...p.model_ids,...state.ai.models.map(m=>m.id).filter(i=>!p.model_ids.includes(i))];
  const body=`<div class="fields-grid">${inputField('方案名称','name',p.name,'required maxlength="120"')}${checkLabel('启用此方案','enabled',p.enabled)}${inputField('研究单次最长等待 / 秒','request_timeout_seconds',p.request_timeout_seconds??'','type="number" min="10" max="600" placeholder="沿用模型配置"')}<p class="field full form-note">股票研究可单独安排较长等待；各通道共用时段签发截止，明确失败后按原顺序使用剩余时间切换。</p><label class="field full">方案说明<textarea name="description" rows="2" maxlength="4000">${esc(p.description)}</textarea></label></div><p class="form-note">勾选参与此方案的模型，用 ↑ ↓ 调整主备顺序。第一个已选模型是首选。</p><div id="plan-models">${order.map((id,i)=>{const m=state.ai.models.find(m=>m.id===id);return `<div class="ordered-model" data-model-id="${id}"><input type="checkbox" aria-label="使用 ${esc(m?.name)}" ${p.model_ids.includes(id)?'checked':''}><span>${esc(m?.name)} ${m?.enabled?'':'（已停用）'}</span><button type="button" class="icon-btn" data-action="model-up" aria-label="上移 ${esc(m?.name)}">↑</button><button type="button" class="icon-btn" data-action="model-down" aria-label="下移 ${esc(m?.name)}">↓</button></div>`;}).join('')}</div>`;
  openModal(dialog(p.id?'编辑调用方案':'新建调用方案','模型顺序与启用状态会保存到新项目。',body,saveFooter(),'plan-form'));modal.querySelector('form').dataset.id=p.id||'';
}
function editAgent(id){
  const a=state.ai.agents.find(a=>a.id===Number(id))||{enabled:true,plan_ids:[]};
  const body=`<div class="fields-grid">${inputField('角色名称','name',a.name,'required maxlength="120"')}${inputField('角色定位','role',a.role,'maxlength="200"')}<label class="field full">研究职责<textarea name="responsibilities" rows="10" maxlength="8000">${esc(a.responsibilities)}</textarea></label><div class="field full"><span>关联调用方案</span>${state.ai.plans.map(p=>checkLabel(esc(p.name),'plan-'+p.id,a.plan_ids.includes(p.id))).join('')}</div>${checkLabel('启用此角色','enabled',a.enabled)}</div>`;
  openModal(dialog(a.id?'编辑研究角色':'新增研究角色','保存角色定义，供下一步新研究流程使用。',body,saveFooter(),'agent-form'));modal.querySelector('form').dataset.id=a.id||'';
}
function editSchedule(id){const s=state.collection.schedules.find(s=>s.id===id);openModal(dialog('设置采集计划',s.name,`<div class="fields-grid">${inputField('每天执行时间（上海）','at',s.at,'type="time" required')}${checkLabel('启用此计划','enabled',s.enabled)}${checkLabel('只在已确认交易日执行','trading_only',s.trading_only)}</div><p class="form-note">本机后台工作开启后将使用此计划的间隔、数据来源与范围。</p>`,saveFooter(),'schedule-form'));modal.querySelector('form').dataset.id=id;}
function editSource(id){const s=state.collection.sources.find(s=>s.id===id);openModal(dialog('数据来源设置',s.name,`<div class="fields-grid">${inputField('请求最小间隔 / 秒','min_interval',s.min_interval,'type="number" min="2" max="600" step="0.5" required')}${inputField('请求超时 / 秒','timeout',s.timeout,'type="number" min="10" max="600" required')}${checkLabel('启用此来源','enabled',s.enabled)}${checkLabel('允许每日计划自动使用','automatic',s.automatic)}</div><p class="trace-label" style="margin-top:20px">${esc(s.validation_origin)}<br>历史最后成功时间：${esc(s.last_success||'未记录')}<br>${esc(s.coverage)}</p>`,saveFooter(),'source-form'));modal.querySelector('form').dataset.id=id;}
function manualCollect(){const c=state.collection;openModal(dialog('手动采集','只采集所选来源与日期范围，不运行 AI。',`<div class="fields-grid"><div class="field full">${selectField('数据来源','source','bs_daily_all',c.sources.filter(s=>s.enabled).map(s=>[s.id,s.name]))}</div>${inputField('开始日期','start',new Date().toLocaleDateString('en-CA',{timeZone:'Asia/Shanghai'}),'type="date" required')}${inputField('结束日期','end',new Date().toLocaleDateString('en-CA',{timeZone:'Asia/Shanghai'}),'type="date" required')}<label class="field full">股票代码（按需填写）<textarea name="symbols" rows="2" placeholder="sh.600000, sz.000001"></textarea><small>全市场日线、名录、行业和公告可留空；个股接口需要指定代码。</small></label></div>`,'<button class="btn" type="button" data-action="close">取消</button><button class="btn primary" type="submit">开始采集</button>','collect-form'));}
const paletteActions=()=>[['rise-screen','四类上涨筛选','spark'],['refresh-page','刷新当前页','refresh']].map(([run,label,ic])=>`<button class="palette-item" data-action="palette-run" data-run="${run}">${icon(ic)}${label}<small>操作</small></button>`).join('');
function command(){openModal(dialog('快速导航','输入页面名称，或使用键盘选择。',`<input class="palette-input" id="command-search" placeholder="前往…" autocomplete="off" autofocus><div class="palette">${pages.map(p=>`<button class="palette-item" data-action="navigate" data-id="${p.id}">${icon(p.icon)}${p.name}<small>↵</small></button>`).join('')}<div class="palette-sep" aria-hidden="true">快捷操作</div>${paletteActions()}</div>`));}
function recordFieldTable(value){
  const labels={day:'日期',symbol:'股票代码',key:'记录分类',trading:'是否交易',markets:'覆盖市场',name:'股票名称',listed:'上市日期',delisted:'退市日期',status:'来源状态',board:'上市板块',group:'分类名称',taxonomy:'分类体系',source_effective_date:'来源生效日',historical_effective_unknown:'历史生效日未知',unclassified:'尚未归类',open:'开盘价',high:'最高价',low:'最低价',close:'收盘 / 最新价',preclose:'前收盘价',volume:'成交量',amount:'成交额',turnover:'换手率',suspended:'是否停牌',is_st:'是否风险警示',adjust:'复权口径',volume_unit:'成交量单位',pe:'市盈率',pb:'市净率',source_time:'来源时点',period:'分钟周期',closed:'周期是否结束',report_type:'报告类型',report_date:'报告期',published:'披露时间',fields:'指标明细',title:'标题',url:'原文地址',text:'已存正文节选',text_extracted_at:'节选保存时间',text_sha256:'节选指纹',text_truncated:'正文已截取',document_sha256:'原件指纹',text_pages:'节选页码',total_pages:'原件页数',text_scope:'节选范围',effective_day:'生效日期',net_main:'主力净额',net_large:'大单净额',net_super:'超大单净额',net_medium:'中单净额',net_small:'小单净额',ratio_main:'主力占比',unit:'金额单位',basis:'统计口径',time_precision:'时间精度',record_kind:'记录类型',coverage_from:'覆盖起日',coverage_through:'覆盖止日',event_count:'事件数'};
  const display=(field,item)=>{
    if(item===null)return '未知';
    if(typeof item==='boolean')return item?'是':'否';
    if(Array.isArray(item))return item.map(part=>display(field,part)).join('、')||'无';
    if(typeof item==='object')return recordFieldTable(item);
    const terms={adjust:{raw:'未复权'},volume_unit:{shares:'股',hands:'手'},time_precision:{day:'日统计',minute:'分钟'},report_type:{profit:'盈利',balance:'偿债',cashflow:'现金流',growth:'成长'}};
    return esc(terms[field]?.[item]??item);
  };
  return `<dl class="detail-pairs">${Object.entries(value).map(([field,item])=>`<dt>${esc(labels[field]||field)}</dt><dd style="white-space:pre-wrap;overflow-wrap:anywhere">${display(field,item)}</dd>`).join('')}</dl>`;
}

async function showRecord(id){const r=await api('/api/records/'+id),v=r.record;openReading(dialog('事实与来源',`${v.symbol} · ${v.day} · ${v.source}`,`<dl class="detail-pairs"><dt>确认来源</dt><dd>${esc(r.origins?.length?r.origins.map(x=>x.source).join(" / "):v.source)}</dd><dt>来源版本</dt><dd>${esc(v.source_version||'新系统采集')}</dd><dt>首次保存</dt><dd>${esc(v.first_seen)}</dd><dt>最近获取</dt><dd>${esc(v.last_seen)}</dd><dt>内容指纹</dt><dd class="mono">${esc(v.fingerprint)}</dd><dt>资料版本</dt><dd>${r.versions.length} 份独立版本；获取 ${v.acquisitions} 次；已合并 ${Math.max(0,v.acquisitions-1)} 次重复</dd></dl><h3>保存的必要字段</h3>${recordFieldTable(v.payload)}${r.versions.length>1?`<h3 style="margin-top:20px">其他已存版本</h3><div class="version-list">${r.versions.filter(x=>x.id!==v.id).map(x=>`<button class="btn small" data-action="record" data-id="${x.id}">${esc(x.first_seen.slice(0,10))} · ${esc(x.source)}</button>`).join('')}</div>`:''}`));}
async function showJob(id){const r=await api('/api/collection/jobs/'+id),j=r.job;openModal(dialog('采集任务记录',j.id,`<dl class="detail-pairs"><dt>来源</dt><dd>${esc(j.name)}</dd><dt>当前状态</dt><dd>${jobChip(j.status)}</dd><dt>任务说明</dt><dd>${esc(j.message)}</dd><dt>已处理批次</dt><dd>${j.done} / ${j.total}</dd><dt>已存结果</dt><dd>新增 ${num(j.added)} · 重复 ${num(j.unchanged)} · 未取得 ${num(j.no_data)}</dd></dl><h3>最近处理记录</h3>${r.events.length?`<pre class="source-json">${esc(JSON.stringify(r.events,null,2))}</pre>`:empty('暂未开始处理','任务真正执行后，会在此留下结果。')}`));}

document.addEventListener('click',async event=>{
  const el=event.target.closest('[data-action]');if(!el||el.disabled)return;
  const action=el.dataset.action,id=el.dataset.id;
  try{
    if(action==='close')return closeModal();
    if(action==='reading-back')return backReading();
    if(action==='reader-section'){
      const target=modalContent.querySelector('[data-reader-section="'+CSS.escape(id)+'"]');
      if(!target)return;
      if(target instanceof HTMLDetailsElement)target.open=true;
      el.closest('nav').querySelectorAll('button').forEach(button=>button.setAttribute('aria-pressed',String(button===el)));
      const offset=modalContent.querySelector('.dialog-header').offsetHeight+el.closest('nav').offsetHeight+14;
      modal.scrollTo({top:modal.scrollTop+target.getBoundingClientRect().top-modal.getBoundingClientRect().top-offset});
      return;
    }
    if(action==='command')return command();
    if(action==='palette-run'){closeModal();const run=el.dataset.run;if(run==='rise-screen')return await showRiseScreen();if(run==='refresh-page')return await render();return;}
    if(action==='navigate'){closeModal();location.hash=id;return;}
    if(action==='refresh'){el.disabled=true;if(state.page==='assets')resetAssetPages();return await render();}
    if(action==='dataset'){state.assetDataset=id;resetAssetPages();history.replaceState(null,'','#assets');return await render();}
    if(action==='reset-filters'){state.assetFilter={start:'',end:'',symbol:''};resetAssetPages();return await render();}
    if(action==='prev-page'||action==='next-page'){
      el.disabled=true;
      if(state.cursorMode){
        if(action==='next-page'){if(!state.assetNext)return;state.assetHistory.push(state.assetCursor);state.assetCursor=state.assetNext;state.assetOffset+=state.assetLimit;}
        else{state.assetCursor=state.assetHistory.pop()||null;state.assetOffset=Math.max(0,state.assetOffset-state.assetLimit);}
      }else state.assetOffset+=action==='prev-page'?-state.assetLimit:state.assetLimit;
      await render();main.querySelector('.compact-info')?.scrollIntoView({block:'start'});return;
    }
    if(action==='storage-policy')return showStoragePolicy();
    if(action==='record')return await showRecord(id);
    if(action==='export'){
      const f=state.assetFilter;if(!f.start||!f.end)throw new Error('请先填写开始与结束日期，再导出当前范围');
      const p=new URLSearchParams({dataset:state.assetDataset,...Object.fromEntries(Object.entries(f).filter(([,v])=>v))});el.disabled=true;
      const r=await fetch('/api/export?'+p);if(!r.ok){const e=await r.json();throw new Error(e.detail);}
      const url=URL.createObjectURL(await r.blob());const a=document.createElement('a');a.href=url;a.download=`${state.assetDataset}-${f.start}-${f.end}.csv`;a.click();setTimeout(()=>URL.revokeObjectURL(url),10000);toast('导出文件已准备好');return;
    }
    if(action==='collection-tab'){state.collectionTab=id;return await render();}
    if(action==='ai-tab'){history.replaceState(null,'',`#ai?tab=${id}`);return await render();}
    if(action==='new-ai')return await ({models:editModel,plans:editPlan,agents:editAgent}[state.aiTab])();
    if(action==='edit-model')return await editModel(id);
    if(action==='test-saved-model')return await testSavedModel(id);
    if(action==='model-test-editor'||action==='model-catalog'){
      const form=el.form;
      if(action==='model-test-editor'&&!form.elements.model_identifier.reportValidity())return;
      delete form.dataset.catalogChanged;
      return await modelProbe(action==='model-catalog'?'catalog':'test',form,{...modelPayload(form),model_id:Number(form.dataset.id)||null});
    }
    if(action==='edit-plan')return editPlan(id);
    if(action==='edit-agent')return editAgent(id);
    if(action==='model-up'||action==='model-down'){const row=el.closest('.ordered-model');const sibling=action==='model-up'?row.previousElementSibling:row.nextElementSibling;if(sibling){if(action==='model-up')sibling.before(row);else sibling.after(row);}return;}
    if(action==='edit-schedule')return editSchedule(id);
    if(action==='edit-source')return editSource(id);
    if(action==='manual-collect')return manualCollect();
    if(action==='watchlist')return openModal(dialog('采集关注股票','用于关注股票的快照、分钟和财务采集。',`<label class="field">股票代码<textarea name="symbols" rows="5" placeholder="sh.600000, sz.000001">${esc(state.collection.watchlist.join(', '))}</textarea><small>用逗号、空格或换行分隔；留空会让这些采集计划等待股票选择。</small></label>`,saveFooter(),'watchlist-form'));
    if(action==='automation'){el.disabled=true;await send('/api/collection/automation',{paused:!state.collection.paused});toast(state.collection.paused?'已启用每日采集计划':'已暂停每日采集计划');return await render();}
    if(action==='toggle-schedule'){el.disabled=true;const s=state.collection.schedules.find(s=>s.id===id);await send('/api/collection/schedules/'+id,{at:s.at,enabled:!s.enabled,trading_only:s.trading_only},'PUT');toast(s.enabled?'已停用本机计划':'已启用本机计划');return await render();}
    if(action==='run-schedule'){el.disabled=true;const j=await send(`/api/collection/schedules/${id}/run`,{});toast(j.integrity==='ok'?'备份已保存并通过校验':'采集已排队，可在任务记录查看进度');state.collectionTab='jobs';return await render();}
    if(action==='job-detail')return await showJob(id);
    if(action==='job-pause'||action==='job-resume'){el.disabled=true;await send(`/api/collection/jobs/${id}/${action==='job-pause'?'pause':'resume'}`,{});toast(action==='job-pause'?'采集已暂停':'已从原进度继续');return await render();}
  }catch(e){toast(e.message,true);}finally{if(el.isConnected)el.disabled=false;}
});

document.addEventListener('submit',async event=>{
  const form=event.target;
  if(!['asset-filters','model-form','plan-form','agent-form','schedule-form','source-form','watchlist-form','collect-form'].includes(form.id))return;
  event.preventDefault();const f=new FormData(form),id=form.dataset.id;
  if(form.id==='asset-filters'){state.assetFilter={start:f.get('start')||'',end:f.get('end')||'',symbol:String(f.get('symbol')||'').trim()};resetAssetPages();return await render();}
  const button=form.querySelector('[type=submit]'),error=form.querySelector('#form-error');if(button.disabled)return;button.disabled=true;if(error)error.textContent='';
  const splitSymbols=()=>String(f.get('symbols')||'').trim().split(/[,，\s]+/).filter(Boolean);
  try{
    let payload,path,method='PUT';
    if(form.id==='model-form'){
      payload=modelPayload(form);path='/api/ai/models'+(id?'/'+id:'');method=id?'PUT':'POST';
    }else if(form.id==='plan-form'){
      payload={name:f.get('name'),enabled:f.has('enabled'),plan_type:'文本分析',description:f.get('description'),request_timeout_seconds:f.get('request_timeout_seconds')?Number(f.get('request_timeout_seconds')):null,model_ids:[...form.querySelectorAll('.ordered-model')].filter(r=>r.querySelector('input').checked).map(r=>Number(r.dataset.modelId))};path='/api/ai/plans'+(id?'/'+id:'');method=id?'PUT':'POST';
    }else if(form.id==='agent-form'){
      payload={name:f.get('name'),role:f.get('role'),responsibilities:f.get('responsibilities'),enabled:f.has('enabled'),plan_ids:state.ai.plans.filter(p=>f.has('plan-'+p.id)).map(p=>p.id)};path='/api/ai/agents'+(id?'/'+id:'');method=id?'PUT':'POST';
    }else if(form.id==='schedule-form'){
      payload={at:f.get('at'),enabled:f.has('enabled'),trading_only:f.has('trading_only')};path='/api/collection/schedules/'+id;
    }else if(form.id==='source-form'){
      payload={min_interval:Number(f.get('min_interval')),timeout:Number(f.get('timeout')),enabled:f.has('enabled'),automatic:f.has('automatic')};path='/api/sources/'+id;
    }else if(form.id==='watchlist-form'){
      payload={symbols:splitSymbols()};path='/api/collection/watchlist';
    }else if(form.id==='collect-form'){
      payload={source:f.get('source'),start:f.get('start'),end:f.get('end'),symbols:splitSymbols()};path='/api/collection/jobs';method='POST';
    }else return;
    await send(path,payload,method);closeModal();toast(form.id==='collect-form'?'采集已排队，可在任务记录查看进度':'已保存');await render();
  }catch(e){if(error)error.textContent=e.message;else toast(e.message,true);}finally{button.disabled=false;}
});
document.addEventListener('change',async event=>{
  const el=event.target,form=el.form;
  if(el.id==='asset-page-size'){state.assetLimit=Number(el.value);resetAssetPages();await render();main.querySelector('.compact-info')?.scrollIntoView({block:'start'});}
  if(form?.id==='model-form'){
    if(el.name==='protocol'){form.dataset.protocolEdited='1';syncModelProtocol(form);}
    if(el.name==='model_family'&&!form.dataset.id&&!form.dataset.protocolEdited){
      const native={gpt:'responses',gemini:'gemini',grok:'xai',claude:'anthropic'}[el.value];
      if(native){
        form.elements.protocol.value=native;
        form.dataset.catalogChanged='1';form.elements.catalog_model.hidden=true;
        syncModelProtocol(form);
      }
    }
    if(el.name==='catalog_model'&&el.value)form.elements.model_identifier.value=el.value;
  }
});

document.addEventListener('input',event=>{
  const el=event.target,form=el.form;
  if(el.id==='command-search'){const q=el.value.toLowerCase();modal.querySelectorAll('.palette-item').forEach(item=>item.hidden=!item.textContent.toLowerCase().includes(q));}
  if(form?.id==='model-form'&&['api_key','base_url','protocol','api_key_env'].includes(el.name)){
    if(el.name==='api_key')form.dataset.keyEdited='1';
    form.dataset.catalogChanged='1';form.elements.catalog_model.hidden=true;
  }
});
document.addEventListener('keydown',event=>{
  if((event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==='k'){event.preventDefault();command();}
  const row=event.target.closest('.record-row');if(row&&(event.key==='Enter'||event.key===' ')){event.preventDefault();row.click();}
  if(event.target.id==='command-search'&&event.key==='Enter'){event.preventDefault();modal.querySelector('.palette-item:not([hidden])')?.click();}
});
modal.addEventListener('click',event=>{if(event.target===modal)closeModal();});
modal.addEventListener('cancel',event=>{event.preventDefault();if(readingTrail.length)backReading();else closeModal();});
modal.addEventListener('close',()=>{if(!modal.open&&modal.querySelector('#model-form'))modalContent.replaceChildren();});
window.addEventListener('hashchange',render);
render();
