'use strict';

const structureBias = {
  two_sided:'双向待证', demand:'需求占优', supply:'供给占优',
  demand_test:'需求侧通过复测', supply_test:'供给侧通过复测'
};
const structurePhase = {
  A:'原趋势减速', B:'区间建立', C:'区间探测', D:'离开区间', E:'区间外延续'
};

function structureEvidence(candidate, runId) {
  const value = candidate.metrics.structure;
  if (!value?.version) return '';
  const label = value.phase ? `阶段 ${value.phase} · ${structurePhase[value.phase]}` : '阶段待证';
  return `<div class="detail-layer structure-layer"><span>价量与趋势回放</span>
    <div class="structure-heading"><h3>结构与失效</h3>
      <button class="btn small" data-action="structure-replay" data-id="${esc(candidate.symbol)}" data-run="${esc(runId||'')}">逐柱回放 ↗</button>
    </div>
    <div class="structure-reading">${chip(label,value.gate_eligible?'gray':'amber')} ${chip(structureBias[value.bias]||'待证','gray')}</div>
    <p>${esc(value.counterpoint||'资料不足，等待更多可比行情。')}</p>
    ${value.guard&&value.gate_eligible?`<p>结构参考失效位 ${price(value.guard.below)}，以完整收盘核查。</p>`:''}
  </div>`;
}

async function showStructureReplay(symbol, runId) {
  const preview = researchState.preview;
  const base = runId ? `/api/v2/research/runs/${encodeURIComponent(runId)}/stocks/${encodeURIComponent(symbol)}/structure`
    : `/api/v2/opportunities/${encodeURIComponent(symbol)}/structure`;
  const query = runId ? new URLSearchParams() : new URLSearchParams({
    day:preview.market.market_day, session:preview.market.session, input_hash:preview.input_hash
  });
  const {bars,replay,frozen_at} = await api(`${base}?${query}`);
  const frames = replay.frames;
  if (!frames.length) throw new Error('没有可回放的冻结行情');
  openModal(dialog('结构回放',`${symbol} / 冻结于 ${frozen_at.slice(0,16).replace('T',' ')}`,`
    <div id="structure-lab">
      <div class="structure-replay-toolbar">
        <button type="button" class="btn small" data-replay-step="-1">上一柱</button>
        <button type="button" class="btn small" data-replay-step="1">下一柱</button>
        <button type="button" class="btn small" data-replay-event>下一事件</button>
      </div>
      <label class="field replay-slider">行情揭示位置<input type="range" min="1" max="${frames.length}" value="1" aria-label="行情揭示位置"></label>
      <div data-replay-summary aria-live="polite"></div>
      <div data-replay-chart></div>
      <div data-replay-history></div>
      <details class="structure-projection">
        <summary>点数图情景</summary>
        <p class="muted">固定格值、指定转向格数，按冻结范围生成 X/O 列与计数。按回放位置逐步揭示。</p>
        <form class="structure-pf-form">
          <label class="field">格值（元）<input name="box" type="number" min="0.01" max="10000" step="0.01" value="0.20" required></label>
          <label class="field">转向格数<input name="reversal" type="number" min="1" max="5" value="3" required></label>
          <button type="submit" class="btn small">生成该步点数图</button>
        </form>
        <div data-pf-result aria-live="polite"></div>
      </details>
    </div>`,`<button class="btn small" data-action="stock-research" data-id="${esc(symbol)}" data-run="${esc(runId||'')}">返回逐股依据</button>`));
  const lab = document.getElementById('structure-lab');
  const slider = lab.querySelector('[type=range]');
  const history = lab.querySelector('[data-replay-history]');
  const pf = lab.querySelector('[data-pf-result]');
  let index = 0;
  let generation = 0;

  function draw(next) {
    index = Math.max(0, Math.min(next, frames.length - 1));
    generation++;
    pf.replaceChildren();
    slider.value = index + 1;
    const frame = frames[index];
    const day = frame.as_of.slice(0,10);
    slider.setAttribute('aria-valuetext',`${day}，第${index+1}柱，共${frames.length}柱`);
    for (const button of lab.querySelectorAll('[data-replay-step]')) {
      button.disabled = Number(button.dataset.replayStep) < 0 ? index === 0 : index === frames.length - 1;
    }
    lab.querySelector('[data-replay-event]').disabled = !frames.slice(index+1).some(f=>f.events.length);
    lab.querySelector('[data-replay-summary]').innerHTML = `<div class="structure-snapshot">
      <div><span>行情日期</span><strong>${esc(day)}</strong></div>
      <div><span>资料可得时间</span><strong>${esc(frame.knowledge_at.slice(0,16).replace('T',' '))}</strong></div>
      <div><span>当前阶段</span><strong>${esc(frame.phase?frame.phase+' · '+structurePhase[frame.phase]:'待证')}</strong></div>
      <div><span>已固定区间</span><strong>${frame.range?price(frame.range.low)+' – '+price(frame.range.high):'待形成'}</strong></div>
      </div><p class="form-note">${esc(frame.effort?.description||'量价可比性待核')} · ${esc(frame.counterpoint)}</p>`;
    lab.querySelector('[data-replay-chart]').innerHTML = priceEvidenceChart(bars.slice(0,index+1));
    const events = frames.slice(0,index+1).flatMap(f=>f.events).reverse();
    history.innerHTML = events.length ? `<ol class="structure-events">${events.map(event=>`
      <li><div><strong>${esc(event.label)}</strong> ${chip(event.state==='provisional'?'盘中待证':event.state==='invalidated'?'失效或中止':'已识别',event.state==='confirmed'?'gray':'amber')}</div>
        <small>图上 ${esc(event.plot_day)} · 可识别 ${esc(event.known_at.slice(0,16).replace('T',' '))}</small>
        <p>${esc(event.note)}</p><details><summary>核对事实</summary>${evidenceButtons(event.fact_ids)}</details></li>`).join('')}</ol>`
      : '<p class="muted">截至这一步尚无结构事件。</p>';
  }

  slider.addEventListener('input',()=>draw(Number(slider.value)-1));
  lab.querySelectorAll('[data-replay-step]').forEach(button=>button.addEventListener('click',()=>draw(index+Number(button.dataset.replayStep))));
  lab.querySelector('[data-replay-event]').addEventListener('click',()=>{
    const next=frames.findIndex((frame,i)=>i>index&&frame.events.length);
    if(next>=0)draw(next);
  });
  lab.querySelector('.structure-pf-form').addEventListener('submit', async event=>{
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const params = new URLSearchParams(query);
    params.set('box',form.get('box'));params.set('reversal',form.get('reversal'));params.set('step',index+1);
    const request = ++generation;
    pf.textContent = '正在计算冻结行情…';
    try {
      const result = await api(`${base}?${params}`);
      if (request!==generation) return;
      const point = result.point_figure;
      if(point.state!=='exploratory'){pf.textContent=point.reason;return;}
      pf.innerHTML = `<p>计数 ${point.count} 列 · 计数线 ${price(point.count_line)} · 情景投影 ${point.projection==null?'条件不足':price(point.projection)}</p>
        <div class="structure-columns">${point.columns.map(c=>`<span><b>${esc(c.type)}</b> ${price(c.low)}–${price(c.high)}<small>${esc(c.from.slice(5))} / ${esc(c.to.slice(5))}</small></span>`).join('')}</div>
        <p class="form-note">${esc(point.reason)}</p>`;
    } catch (error) { if(request===generation) pf.textContent=error.message; }
  });
  draw(0);
  lab.querySelector('[data-replay-step="1"]').focus();
}
