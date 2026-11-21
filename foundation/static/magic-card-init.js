'use strict';
/* Magic Card glow + Border Beam init
   Tracks mouse position on .magic-card elements, writes CSS custom props
   --mouse-x / --mouse-y so the radial gradient follows the cursor.
   Also adds .magic-card to panel/tile elements that benefit from it.
   Runs once on DOMContentLoaded; re-runs after each SPA page render via
   initMagicCards() called from render().
*/

function initMagicCards(root) {
  root = root || document;
  const cards = [...root.querySelectorAll('.panel, .summary-tile, .metric, .connection-row, .plan-card, .agent-card, .rise-tile, .rise-row, .rise-method-card, .tile')].filter(el => !el.matches('.magic-surface, [data-glow]'));
  cards.forEach(card => {
    if (card.dataset.magicInit) return;
    card.dataset.magicInit = '1';
    if (!card.classList.contains('tile')) card.classList.add('magic-card');
    card.addEventListener('mousemove', e => {
      const rect = card.getBoundingClientRect();
      const x = ((e.clientX - rect.left) / rect.width  * 100).toFixed(1) + '%';
      const y = ((e.clientY - rect.top)  / rect.height * 100).toFixed(1) + '%';
      card.style.setProperty('--mouse-x', x);
      card.style.setProperty('--mouse-y', y);
    }, { passive: true });
  });
}

document.addEventListener('DOMContentLoaded', () => initMagicCards());

/* Border Beam: a rotating conic ring injected as a real element, because
   .asset-feature already uses ::before/::after for its dot pattern and orb. */
function initBeams(root) {
  (root || document).querySelectorAll('.beam-target').forEach(el => {
    if (el.dataset.beamInit) return;
    el.dataset.beamInit = '1';
    const ring = document.createElement('span');
    ring.className = 'beam-ring';
    ring.setAttribute('aria-hidden', 'true');
    el.appendChild(ring);
  });
}

/* Number Ticker: counts [data-count] elements up from 0 once per render.
   Skipped when the user prefers reduced motion. */
function initTickers(root) {
  const reduce = window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches;
  (root || document).querySelectorAll('[data-count]').forEach(el => {
    if (el.dataset.tickInit) return;
    el.dataset.tickInit = '1';
    const to = Number(el.dataset.count);
    if (!Number.isFinite(to) || reduce) return;
    tickNumber(el, 0, to, { dur: 900 });
  });
}

/* Single entry point, called by render() in app.js after every page render. */
function initMotion(root) {
  initMagicCards(root);
  initTickers(root);
}
window.initMotion = initMotion;

/* Number ticker — animates a number element from its previous value to the
   new one, 60fps RAF loop, easeOutExpo, 700ms.
   Usage: tickNumber(el, fromValue, toValue, {decimals, suffix})         */
function tickNumber(el, from, to, opts) {
  opts = opts || {};
  const decimals = opts.decimals || 0;
  const suffix   = opts.suffix   || '';
  const dur      = opts.dur      || 700;
  const start    = performance.now();
  function easeOutExpo(t) { return t === 1 ? 1 : 1 - Math.pow(2, -10 * t); }
  function frame(now) {
    const t = Math.min((now - start) / dur, 1);
    const v = from + (to - from) * easeOutExpo(t);
    el.textContent = Number(v.toFixed(decimals)).toLocaleString('zh-CN') + suffix;
    if (t < 1) requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);
}

/* Expose globally so other view scripts can call them */
window.initMagicCards = initMagicCards;
window.tickNumber = tickNumber;
