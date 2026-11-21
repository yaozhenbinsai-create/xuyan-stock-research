'use strict';

const libraryKeys=['q','start','end','session','state','stars','rise_type','symbol','run'];
const poolStateNames={wait:'观察中',buy:'待参与',active:'跟踪中',evidence:'待补证',expired:'已到期',archive:'历史回看'};
const poolPageSize=6;

function stockStars(stars) {
  if(!Number.isInteger(stars)||stars<1||stars>5)return chip('待评分','gray');
  return `<span class="library-stars"><span aria-hidden="true">${'★'.repeat(stars)+'☆'.repeat(5-stars)}</span>
    <small>${stars} / 5 星</small></span>`;
}

function reportRating(decision) {
  const rating=decision.rating;
  return `<section class="detail-layer rating-report"><div class="structure-heading"><h3>AI 星级</h3>${stockStars(rating?.stars)}</div>
    ${rating?`<p>${esc(rating.text)}</p>${reportStatementSources(rating)}`
      :'<p>这份原稿尚未登记 AI 评分。</p>'}</section>`;
}

function libraryHref(values, navigation={}) {
  const query = new URLSearchParams();
  for(const key of ['pool',...libraryKeys])if(values[key])query.set(key,values[key]);
  for(const [key,value] of Object.entries(navigation))if(value)query.set(key,value);
  return '#tracking'+(query.size?'?'+query:'');
}

function poolPriceChart(quote) {
  const bars=quote.trend||[];
  if(bars.length<2)return '<div class="pool-graph-gap">20 日走势待核</div>';
  const values=bars.map(([,value])=>value),width=184,height=49;
  const low=Math.min(...values),high=Math.max(...values);
  const y=value=>height-5-(value-low)/(high-low||1)*(height-10);
  const coordinates=values.map((value,i)=>[(i*width/(values.length-1)).toFixed(1),y(value).toFixed(1)]);
  const line=coordinates.map(point=>point.join(',')).join(' ');
  const last=coordinates.at(-1);
  const tone=values.at(-1)>=values[0]?'up':'down';
  return `<svg class="pool-sparkline ${tone}" viewBox="0 0 ${width+1} ${height}" preserveAspectRatio="none" role="img"
    aria-label="${esc(bars[0][0])} 至 ${esc(bars.at(-1)[0])} 收盘从 ${poolPrice(values[0])} 元到 ${poolPrice(values.at(-1))} 元">
    <polygon points="0,${height} ${line} ${width},${height}" class="pool-spark-fill"/>
    <line x1="0" y1="${y(quote.ma20)}" x2="${width}" y2="${y(quote.ma20)}" class="pool-spark-ma"/>
    <polyline points="${line}" class="pool-spark-path"/>
    <circle cx="${last[0]}" cy="${last[1]}" r="2.7" class="pool-spark-dot"/></svg>`;
}

const poolPrice = value => value==null?'—':Number(value).toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:4});
const poolStamp = value => value?.slice(0,16).replace('T',' ')||'待有行情';

function poolPricePanel(card) {
  if(!card)return '';
  const {quote,levels,counts}=card;
  const target=levels.target;
  const tone=quote.change>0?'up':quote.change<0?'down':'flat';
  const quoteLabel=quote.kind==='quote'?'最近采集报价':quote.suspended?'最近停牌记录':'最近正式收盘';
  return `<div class="pool-instrument">
    <div class="pool-market-line">
      <div class="pool-quote"><span>${quote.price!=null?quoteLabel:'最近价格'}</span>
        <strong><small>¥</small> ${poolPrice(quote.price)}</strong>
        ${quote.change!=null?`<b class="pool-move ${tone}">${percent(quote.change)}</b>`:''}
        <div class="pool-stamp">${esc(poolStamp(quote.at))}
          ${quote.fact_id?`<button class="pool-source" data-action="record" data-id="${quote.fact_id}" title="核对行情事实">原始行情 ↗</button>`:''}</div>
      </div>
      <div class="pool-trend"><div><span>20 日均价</span><b>${poolPrice(quote.ma20)}</b></div>
        ${poolPriceChart(quote)}
        <small>${quote.ma_day?esc(quote.ma_day)+' · 正式日线':'等待正式日线'}</small></div>
    </div>
    <div class="pool-price-lanes">
      <div><span>${levels.trigger!=null?'触发价':'研究价'}</span><strong>${poolPrice(levels.trigger??levels.anchor)}</strong></div>
      <div><span>${target?esc(target.kind):'上行参考'}</span>
        <strong>${target?`${poolPrice(target.price_low)} – ${poolPrice(target.price_high)}`:'待证'}</strong></div>
      <div><span>${esc(levels.floor_kind||'失效位')}</span><strong>${poolPrice(levels.floor)}</strong></div>
    </div>
    ${target?`<p class="pool-target-basis" title="${esc(target.condition||'')}">${target.horizon==='D3'?'后 3 个交易日':'后 1 个交易日'} ·
      研究价 ${poolPrice(levels.anchor)} · ${target.samples} 个可比上行样本${levels.as_of?' · '+esc(poolStamp(levels.as_of)):''}</p>`:''}
    ${target?.condition?`<details class="pool-target-basis"><summary>展开上行条件</summary><p>${esc(target.condition)}</p></details>`:''}
    <div class="pool-ledger-tallies" title="有效报告每份计一次。入选含观察和可考虑买入，推荐只计签发为可考虑买入。">
      <div><b>${num(counts.selected_count)}</b><span>次入选</span></div><div><b>${num(counts.recommended_count)}</b><span>次推荐</span></div>
    </div></div>`;
}

function poolReviewStrip(review) {
  const counts=review.pools;
  const day=review.day;
  const status=day===review.today?'已取得今日收盘资料':day?'等待新的正式收盘':'等待正式收盘';
  return `<section class="pool-review-strip" aria-label="收盘去留状态">
    <div><span class="eyebrow">DAILY CLOSE</span><h2>收盘去留状态</h2><p>${status}${day?` · 资料截至 ${esc(day)}`:''}</p></div>
    <div class="pool-review-numbers">
      ${[['买入池',counts.buy],['观察池',counts.watch],['已核对',review.checked],['待行情',review.pending],['待补证',review.needs_evidence],['已剔除',review.removed]].map(([label,value])=>
        `<div><b>${num(value)}</b><span>${label}</span></div>`).join('')}
    </div><div class="pool-review-actions"><button class="btn small" data-action="pool-refresh" data-session="close">核对最新收盘 ↗</button></div>
  </section>`;
}

async function poolReviewQueue(query) {
  const pool=query.get('pool')==='buy'?'buy':'watch';
  const params=new URLSearchParams({pool,limit:poolPageSize,cycle_session:'close'});
  for(const key of ['before','after'])if(query.has(key))params.set(key,query.get(key));
  const result=await api('/api/v2/stock-library?'+params);
  const href=(id,cursor='',direction='')=>reviewHref({day:query.get('day'),run:query.get('run'),news:query.get('news'),pool:id,
    ...(cursor?{[direction]:cursor}:{})});
  const ids=result.items.map(row=>row.cycle_id).filter(Boolean);
  return `<section class="panel pool-close-queue" aria-label="收盘研究去留名单"><div class="section-heading"><div><h2>收盘研究去留</h2>
    <p>核对收盘研究原判，逐股登记保留、转观察或移出。</p></div>
    ${ids.length?`<button class="btn small" data-action="review-pool-page" data-ids="${esc(ids.join(','))}">AI 复核本页</button>`:''}</div>
    <div class="pool-close-tabs">${[['buy','买入池'],['watch','观察池']].map(([id,label])=>
      `<a class="btn small ${pool===id?'primary':''}" href="${esc(href(id))}" ${pool===id?'aria-current="page"':''}>${label} ${num(result.pools[id])}</a>`).join('')}</div>
    ${result.items.length?`<div class="pool-close-list">${result.items.map(row=>`<article aria-label="${esc(row.name)}">
      <div><button class="pool-stock-title" data-action="stock-research" data-run="${esc(row.run_id)}" data-id="${esc(row.symbol)}">${esc(row.name)}</button>
      <small>${esc(row.symbol.toUpperCase())}</small>
      ${researchDateLine(row.market_day,row.session,row.published_at,row.kind==='review'?'复核市场日':'研究市场日')}</div>
      <div><span>最近已存价</span><b>${poolPrice(row.card.quote.price)}</b></div>
      <div><span>原失效位</span><b>${poolPrice(row.card.levels.floor)}</b></div>
      <div><b>${row.checked_day?'复盘 '+esc(row.checked_day):'待复盘'}</b><p>${esc(row.check_reason||'等待新收盘资料核对原判断。')}</p></div>
      <div class="pool-card-actions"><button class="btn small" data-action="review-cycle" data-id="${esc(row.cycle_id)}">AI 复核</button>
        <button class="btn ghost small pool-remove" data-action="remove-pool-stock" data-session="close" data-id="${esc(row.symbol)}" data-name="${esc(row.name)}">移出</button></div>
    </article>`).join('')}</div>`:empty('当前池没有在跟股票','可切换到另一个池查看。')}
    <nav class="table-foot pool-pagination" aria-label="复盘名单分页"><span>${num(result.total)} 只 · 每页 ${poolPageSize} 条</span><div>
      ${params.has('after')||params.has('before')?`<a class="btn small" href="${esc(href(pool))}">第一页</a>`:''}
      ${result.previous?`<a class="btn small" href="${esc(href(pool,result.previous,'after'))}">上一页</a>`:''}
      ${result.next?`<a class="btn small" href="${esc(href(pool,result.next,'before'))}">下一页</a>`:''}</div></nav></section>`;
}

function libraryFilters(filters, policy, today) {
  const options=filters.pool==='buy'?[['buy','待参与'],['active','跟踪中']]:
    filters.symbol?Object.entries(poolStateNames):[['wait','观察中'],['evidence','待补证']];
  const active=['start','end','session','stars','rise_type','state'].filter(key=>filters[key]).length;
  const todayOnly=filters.start===today&&filters.end===today;
  const allDates=!filters.start&&!filters.end;
  return `<form class="panel pool-filters" id="stock-library-filters">
    ${filters.symbol?`<input type="hidden" name="symbol" value="${esc(filters.symbol)}">`:`<input type="hidden" name="pool" value="${filters.pool}">`}
    ${filters.run?`<input type="hidden" name="run" value="${esc(filters.run)}">`:''}
    <div class="pool-search-line">${inputField('股票名称或代码','q',filters.q,'maxlength="40" placeholder="例如 兆威机电"')}
      <button class="btn primary small" type="submit">筛选</button>
      <a class="btn ghost small" href="${esc(libraryHref({pool:filters.pool,symbol:filters.symbol,run:filters.run}))}">清空条件</a></div>
    ${today&&!filters.run?`<nav class="pool-date-tabs" aria-label="按研究市场日筛选">
      <a class="btn small ${allDates?'primary':''}" ${allDates?'aria-current="page"':''}
        href="${esc(libraryHref({...filters,start:'',end:''}))}">全部日期</a>
      <a class="btn small ${todayOnly?'primary':''}" ${todayOnly?'aria-current="page"':''}
        href="${esc(libraryHref({...filters,start:today,end:today}))}">仅今日 ${esc(today)}</a></nav>`:''}
    <details class="pool-filter-details" ${active?'open':''}><summary>${filters.run?'星级、类别和状态':'日期、时段、星级和状态'}${active?` · 已应用 ${active} 项`:''}</summary>
      <div class="pool-filter-grid">
      ${filters.run?'':`${inputField('市场日从','start',filters.start,'type="date"')}
      ${inputField('市场日至','end',filters.end,'type="date"')}
      ${selectField('时段','session',filters.session,[['','全部时段'],...Object.entries(sessionLabels)])}`}
      ${selectField('AI 星级','stars',filters.stars,[['','全部星级'],['5','5 星'],['4','4 星'],['3','3 星'],['2','2 星'],['1','1 星'],['unrated','待评分']])}
      ${selectField('上涨类别','rise_type',filters.rise_type,[['','全部类别'],...riseOptions(policy)])}
      ${options.length?selectField('状态','state',filters.state,[['','全部状态'],...options]):''}
      </div></details></form>`;
}

function libraryRow(row, history) {
  const action=poolStateNames[row.state]||'原判断';
  const isLive=Boolean(row.pool&&row.cycle_id&&!['expired','archive'].includes(row.state));
  return `<article class="pool-stock-card panel" aria-label="${esc(row.name)}">
    <header class="pool-stock-header"><div><small>${esc(row.symbol.toUpperCase())} · ${esc(row.rise_name||'研究记录')}</small>
      <button class="pool-stock-title" data-action="stock-research" data-run="${esc(row.run_id)}" data-id="${esc(row.symbol)}">${esc(row.name)}</button></div>
      <div class="pool-stock-tags"><span class="pool-state ${row.pool||'watch'}">${esc(action)}</span>${stockStars(row.stars)}</div></header>
    ${researchDateLine(row.market_day,row.session,row.published_at,row.kind==='review'?'复核市场日':'研究市场日')}
    ${row.original_day&&row.original_published_at!==row.published_at?
      researchDateLine(row.original_day,row.original_session,row.original_published_at,'原判市场日'):''}
    ${poolPricePanel(row.card)}
    ${row.thesis?`<p class="pool-thesis">${esc(readableReportText(null,row.thesis))}</p>`:''}
    ${row.check_reason?`<p class="pool-card-check"><b>${esc(row.checked_day)} 复盘</b> ${esc(row.check_reason)}</p>`:''}
    <footer class="pool-card-actions">
      <button class="btn small" data-action="stock-research" data-run="${esc(row.run_id)}" data-id="${esc(row.symbol)}">阅读个股报告 ↗</button>
      <a class="btn ghost small" href="${esc(researchHref(row.session,row.run_id))}">查看原稿</a>
      ${history?'':`<a class="btn ghost small" href="${esc(libraryHref({symbol:row.symbol}))}">历史记录</a>`}
      ${row.cycle_id?`<button class="btn ghost small" data-action="cycle-detail" data-id="${esc(row.cycle_id)}">跟踪时间线</button>`:''}
      ${isLive&&!history?`<button class="btn ghost small" data-action="review-cycle" data-id="${esc(row.cycle_id)}">${row.stars?'AI 复核':'AI 评分与复核'}</button>
        <button class="btn ghost small pool-remove" data-action="remove-pool-stock" data-id="${esc(row.symbol)}" data-name="${esc(row.name)}">移出</button>`:''}
    </footer></article>`;
}

function poolHeader(filters, counts, review, run) {
  return `<header class="desk-mast pool-mast" aria-label="股票池总览"><div>
    <div class="desk-index"><i></i> 03 / EQUITY BOOK</div><h1>股票池</h1>
    <p>按市场日查看每只股票的判断来源、签发时间和后续去留。</p></div>
    <nav class="pool-switch" aria-label="选择股票池">${[['buy','买入池','已有买入判断，跟踪参与条件'],['watch','观察池','等触发或补证']].map(([id,label,caption])=>
      `<a class="pool-switch-item ${id}" href="${esc(libraryHref({pool:id,...Object.fromEntries(libraryKeys.filter(key=>key!=='state').map(key=>[key,filters[key]]))}))}"
        ${filters.pool===id?'aria-current="page"':''}><div><small>${id==='buy'?'01 / BUY':'02 / WATCH'}</small><strong>${label}</strong><span>${caption}</span></div>
        <b>${num(counts[id])}<small>${run?'只（总池）':'只'}</small></b></a>`).join('')}</nav></header>
    <div class="desk-subnav pool-subnav"><span><i></i> ${review.day?`正式收盘截至 ${esc(review.day)}`:'等待正式收盘'}</span>
    <div><a class="btn ghost small" href="${esc(reviewHref(run?{day:run.market_day,run:run.id}:{}))}">每日复盘 ↗</a></div></div>`;
}

async function renderTracking(ticket) {
  const route=location.hash;
  const query = new URLSearchParams(route.split('?')[1]||'');
  const book = query.get('book');
  if(['actual','paper'].includes(book))return renderPortfolio(book);
  const filters = Object.fromEntries(libraryKeys.map(key=>[key,query.get(key)||'']));
  const history = Boolean(filters.symbol);
  if(!history)filters.pool=query.get('pool')==='buy'?'buy':'watch';
  const params = new URLSearchParams(Object.entries(filters).filter(([,value])=>value));
  if(filters.run){params.delete('run');params.set('run_id',filters.run);}
  params.set('limit',poolPageSize);
  const before = query.get('before'),after = query.get('after');
  if(before)params.set('before',before);
  if(after)params.set('after',after);
  const [library,review,catalog,run]=await Promise.all([api('/api/v2/stock-library?'+params),api('/api/v2/pool-review'),
    researchState.dashboard?.catalog||api('/api/v2/methods'),
    filters.run?api('/api/v2/research/runs/'+encodeURIComponent(filters.run)+'?progress=true'):null]);
  if(ticket!==pageRequest||route!==location.hash)return null;
  if(run&&(!run.market_day||library.items.some(row=>row.run_id!==run.id)))throw new Error('暂时无法核对本轮跟踪范围');
  const active=!run||review.active?.id===run.id?review.active:null;
  startResearchPolling(active);
  const title=(library.items[0]?.name||'股票')+' · 历史记录';
  let html=history?heading('RESEARCH HISTORY',esc(title),'按市场日回看每次判断、评分和参与条件。',
    '<a class="btn small" href="#tracking">返回股票池 ↗</a>'):poolHeader(filters,library.pools,review,run)+bookNav();
  if(run){
    html+=`<section class="research-context" aria-label="跟踪的研究日期">
      ${researchDateLine(run.market_day,run.session,run.published_at,'跟踪原稿')}
      <span class="research-flow-run">任务 ${esc(run.id)}</span>
      <a class="btn small" href="${esc(libraryHref({pool:filters.pool}))}">查看全部股票池</a></section>`
      +researchFlow('tracking',run,run.market_day,run.session);
  }
  if(active)html+=researchProgress(active);
  html+=libraryFilters(filters,catalog.rise_policy,review.today);
  const reviewIds=history?[]:[...new Set(library.items.map(row=>row.cycle_id).filter(Boolean))];
  html+=`<div class="section-heading pool-results-heading"><div><h2>${run?'本轮':''}${history?'原稿记录':filters.pool==='buy'?'买入池名单':'观察池名单'}</h2>
    <p>${num(library.total)} ${history?'条原稿':'只股票 · 每股一条'}</p></div>
    ${reviewIds.length?`<button class="btn small" data-action="review-pool-page" data-ids="${esc(reviewIds.join(','))}">AI 复核本页</button>`:''}</div>`;
  html+=library.items.length?`<section class="pool-stock-grid" aria-label="股票池结果">
    ${library.items.map(row=>libraryRow(row,history)).join('')}</section>`
    :run?empty('本轮在这个池中没有跟踪记录','可以切换买入池和观察池，或回到原稿查看全部判断。'):
    empty(!history&&filters.pool==='buy'?'当前没有符合条件的买入股票':'当前条件下没有记录',
      !history&&filters.pool==='buy'?(library.pools.buy?'可调整筛选条件。':
        `当前没有生效的买入判断。观察池有 ${num(library.pools.watch)} 只在跟股票，可查看各自等待的条件。`):'可清空条件或更换日期和状态。');
  const pageNo=Math.max(1,Math.min(Math.ceil(library.total/poolPageSize)||1,Number(query.get('page'))||1));
  html+=`<nav class="table-foot pool-pagination" aria-label="股票池分页"><span>第 ${pageNo} / ${Math.ceil(library.total/poolPageSize)||1} 页 · 每页 ${poolPageSize} 条</span><div>
    ${before||after?`<a class="btn small" href="${esc(libraryHref(filters))}">第一页</a>`:''}
    ${library.previous?`<a class="btn small" href="${esc(libraryHref(filters,{after:library.previous,page:pageNo-1}))}">上一页</a>`:''}
    ${library.next?`<a class="btn small" href="${esc(libraryHref(filters,{before:library.next,page:pageNo+1}))}">下一页</a>`:''}</div></nav>`;
  return html;
}

document.addEventListener('click',async event=>{
  const button=event.target.closest('[data-action="remove-pool-stock"],[data-action="pool-refresh"],[data-action="review-pool-page"]');
  if(!button||button.disabled)return;
  const {action,id,name,ids,session}=button.dataset;
  if(action==='remove-pool-stock'){
    const fields=`<input type="hidden" name="symbol" value="${esc(id)}"><input type="hidden" name="session" value="${esc(session)}">
      <label class="field">移除理由<textarea name="reason" maxlength="500" rows="3" required></textarea></label>
      <p class="form-note">${session==='close'?'确认后移出这只股票的收盘跟踪。':'确认后移出这只股票的全部跟踪。'}研究账本会记录本次去留。</p>`;
    openModal(dialog('移出 '+name,id,fields,saveButtons('确认移出'),'pool-remove-form'));
    return;
  }
  button.disabled=true;
  try{
    if(action==='review-pool-page')await newResearch(ids.split(','));
    else{
      const result=await send('/api/v2/pool-review/refresh'+(session?'?session='+encodeURIComponent(session):''),{});
      toast(result.day?`收盘资料截至 ${result.day} · 已核对 ${result.checked} 只`:'等待正式收盘资料');
      await render();
    }
  }catch(error){toast(error.message,true);}
  finally{if(button.isConnected)button.disabled=false;}
});

document.addEventListener('submit',async event=>{
  const form=event.target;
  if(form.id==='stock-library-filters'){
    event.preventDefault();
    const target=libraryHref(Object.fromEntries(new FormData(form)));
    if(location.hash===target)await render();
    else location.hash=target;
  }
  if(form.id==='pool-remove-form'){
    event.preventDefault();
    const values=Object.fromEntries(new FormData(form));
    const button=form.querySelector('[type=submit]');
    if(button.disabled)return;
    button.disabled=true;
    try{
      await send('/api/v2/stock-library/'+encodeURIComponent(values.symbol)+'/remove',{
        reason:values.reason,confirm:true,...(values.session?{session:values.session}:{})});
      closeModal();
      toast('股票已移出');
      await render();
    }catch(error){form.querySelector('#form-error').textContent=error.message;}
    finally{button.disabled=false;}
  }
});
