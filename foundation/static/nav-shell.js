'use strict';
// Sidebar shell: collapsible groups and a collapsible icon rail.
// Preferences live in localStorage; app.js asks navGroupOpen() while it draws the menu.
(() => {
  const KEY = 'xy-nav-v1';
  const root = document.documentElement;
  const read = () => { try { return JSON.parse(localStorage.getItem(KEY)) || {}; } catch { return {}; } };
  const prefs = read();
  const save = () => { try { localStorage.setItem(KEY, JSON.stringify(prefs)); } catch {} };
  const collapsed = () => new Set(Array.isArray(prefs.collapsed) ? prefs.collapsed : []);

  // The group that holds the current page always stays open.
  window.navGroupOpen = (name, hasCurrent) => hasCurrent || !collapsed().has(name);

  function setRail(mini) {
    if (mini) root.dataset.rail = 'mini'; else delete root.dataset.rail;
    const button = document.querySelector('[data-rail-toggle]');
    if (!button) return;
    const label = mini ? '展开侧栏' : '收起侧栏';
    button.setAttribute('aria-pressed', String(mini));
    button.setAttribute('aria-label', label);
    button.title = label;
    const text = button.querySelector('span');
    if (text) text.textContent = mini ? '展开' : '收起';
  }

  function syncExpanded() {
    document.querySelectorAll('#navigation .nav-group').forEach(group => {
      group.querySelector('[data-nav-toggle]')?.setAttribute('aria-expanded', String(!group.classList.contains('is-collapsed')));
    });
  }

  document.addEventListener('click', event => {
    const toggle = event.target.closest('[data-nav-toggle]');
    if (toggle) {
      const group = toggle.closest('.nav-group');
      const isCollapsed = group.classList.toggle('is-collapsed');
      toggle.setAttribute('aria-expanded', String(!isCollapsed));
      const names = collapsed();
      if (isCollapsed) names.add(group.dataset.group); else names.delete(group.dataset.group);
      prefs.collapsed = [...names];
      save();
      return;
    }
    if (event.target.closest('[data-rail-toggle]')) {
      const mini = root.dataset.rail !== 'mini';
      prefs.mini = mini;
      save();
      setRail(mini);
    }
  });

  const nav = document.getElementById('navigation');
  if (nav) new MutationObserver(syncExpanded).observe(nav, {childList: true});
  setRail(Boolean(prefs.mini));
  syncExpanded();
})();
