/* Interactions for the AP run report. No framework, no network.
   All invoice text is already escaped by the generator and lives in inert <template>
   elements; this script only moves, clones and shows/hides that markup. */
(function () {
  'use strict';
  var root = document.querySelector('.ap');
  var wrap = root.querySelector('.ap-wrap');
  var list = root.querySelector('.ap-list');
  var detail = root.querySelector('.ap-detail');
  var live = root.querySelector('[data-live]');
  var LIMIT = 12;
  var state = { owner: 'all', more: false, selected: null, ctx: [], ctxLabel: '' };

  function $all(sel, scope) { return Array.prototype.slice.call((scope || root).querySelectorAll(sel)); }
  function narrow() { return root.clientWidth <= 760; }

  /* ---- theme: system > light > dark ---- */
  var themes = ['system', 'light', 'dark'];
  var labels = { system: 'System', light: 'Light', dark: 'Dark' };
  var themeBtn = root.querySelector('[data-theme-btn]');
  function setTheme(t) {
    root.setAttribute('data-theme', t);
    if (themeBtn) {
      themeBtn.querySelector('span').textContent = labels[t];
      themeBtn.setAttribute('aria-label', 'Theme: ' + labels[t] + '. Switch theme');
    }
  }
  try { var saved = localStorage.getItem('ap-theme'); if (saved && labels[saved]) setTheme(saved); } catch (e) {}
  if (themeBtn) themeBtn.addEventListener('click', function () {
    var next = themes[(themes.indexOf(root.getAttribute('data-theme')) + 1) % themes.length];
    setTheme(next);
    try { localStorage.setItem('ap-theme', next); } catch (e) {}
  });

  /* ---- outcome mix: dollars / count ---- */
  $all('[data-mode-btn]').forEach(function (b) {
    b.addEventListener('click', function () {
      var mode = b.getAttribute('data-mode-btn');
      $all('[data-mode-btn]').forEach(function (x) {
        x.setAttribute('aria-pressed', x === b ? 'true' : 'false');
      });
      $all('[data-mode]').forEach(function (el) { el.hidden = el.getAttribute('data-mode') !== mode; });
      var word = root.querySelector('[data-mode-word]');
      if (word) word.textContent = mode;
    });
  });

  /* ---- collapsible groups ---- */
  function toggleGroup(btn, force) {
    var open = force === undefined ? btn.getAttribute('aria-expanded') !== 'true' : force;
    btn.setAttribute('aria-expanded', open ? 'true' : 'false');
    var body = document.getElementById(btn.getAttribute('aria-controls'));
    if (body) body.classList.toggle('is-collapsed', !open);
  }
  $all('.ap-group-btn').forEach(function (b) { b.addEventListener('click', function () { toggleGroup(b); }); });

  /* ---- owner filter + "show more" on the review list ---- */
  var reviewRows = $all('#ap-review-rows > .ap-row-item');
  var moreWrap = root.querySelector('[data-more]');
  function applyReviewFilter() {
    var visible = 0;
    reviewRows.forEach(function (li) {
      var match = state.owner === 'all' || li.getAttribute('data-owner') === state.owner;
      li.classList.toggle('is-filtered', !match);
      var over = match && !state.more && visible >= LIMIT;
      li.classList.toggle('is-overflow', over);
      if (match) visible++;
    });
    if (moreWrap) {
      var extra = visible - LIMIT;
      moreWrap.hidden = state.more || extra <= 0;
      var btn = moreWrap.querySelector('button');
      if (btn) btn.textContent = 'Show ' + extra + ' more';
    }
  }
  $all('.ap-chip').forEach(function (c) {
    c.addEventListener('click', function () {
      state.owner = c.getAttribute('data-owner');
      $all('.ap-chip').forEach(function (x) { x.setAttribute('aria-pressed', x === c ? 'true' : 'false'); });
      applyReviewFilter();
    });
  });
  if (moreWrap) moreWrap.querySelector('button').addEventListener('click', function () {
    state.more = true; applyReviewFilter();
  });
  applyReviewFilter();

  /* ---- selection and the detail panel ---- */
  function visibleRowsIn(ul) {
    return $all('.ap-row-item', ul).filter(function (li) {
      return !li.classList.contains('is-filtered') && !li.classList.contains('is-overflow');
    });
  }
  function markCurrent(key) {
    $all('.ap-row').forEach(function (b) {
      b.setAttribute('aria-current', b.getAttribute('data-key') === key ? 'true' : 'false');
    });
  }
  function select(key, ul, opts) {
    opts = opts || {};
    var tpl = document.getElementById('d-' + key);
    if (!tpl) return;
    state.selected = key;
    if (ul) {
      state.ctx = visibleRowsIn(ul).map(function (li) { return li.getAttribute('data-key'); });
      state.ctxLabel = ul.getAttribute('data-ctx') || '';
    }
    var pos = state.ctx.indexOf(key);
    detail.textContent = '';
    detail.appendChild(tpl.content.cloneNode(true));
    var posEl = detail.querySelector('[data-pos]');
    if (posEl && pos >= 0) posEl.textContent = (pos + 1) + ' of ' + state.ctx.length + ' · ' + state.ctxLabel;
    var prev = detail.querySelector('[data-prev]'), next = detail.querySelector('[data-next]');
    if (prev) prev.disabled = pos <= 0;
    if (next) next.disabled = pos < 0 || pos >= state.ctx.length - 1;
    wrap.classList.add('has-sel'); wrap.classList.remove('no-sel');
    markCurrent(key);
    var open = detail.querySelector('details[data-open]');
    if (open && !narrow()) open.open = false;
    if (opts.focus || narrow()) {
      var head = detail.querySelector('.ap-dvendor');
      if (head) { head.focus({ preventScroll: false }); }
      if (narrow()) window.scrollTo(0, 0);
    }
  }
  function clearSelection() {
    state.selected = null;
    detail.textContent = '';
    var empty = document.getElementById('d-none');
    if (empty) detail.appendChild(empty.content.cloneNode(true));
    wrap.classList.remove('has-sel'); wrap.classList.add('no-sel');
    markCurrent('');
  }
  function step(delta) {
    var i = state.ctx.indexOf(state.selected) + delta;
    if (i >= 0 && i < state.ctx.length) {
      var key = state.ctx[i];
      var row = root.querySelector('.ap-row[data-key="' + key + '"]');
      select(key, row ? row.closest('.ap-rows') : null);
      if (row && !narrow()) row.scrollIntoView({ block: 'nearest' });
    }
  }
  list.addEventListener('click', function (e) {
    var row = e.target.closest('.ap-row');
    if (row) select(row.getAttribute('data-key'), row.closest('.ap-rows'), { focus: true });
  });
  detail.addEventListener('click', function (e) {
    if (e.target.closest('[data-prev]')) step(-1);
    else if (e.target.closest('[data-next]')) step(1);
    else if (e.target.closest('[data-close]')) clearSelection();
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && state.selected && narrow()) clearSelection();
  });

  /* ---- possible-fraud banner: open the group and show the first one ---- */
  var fraudBtn = root.querySelector('[data-fraud-btn]');
  if (fraudBtn) fraudBtn.addEventListener('click', function () {
    clearSearch();
    var btn = root.querySelector('.ap-group-btn[aria-controls="ap-grp-fraud"]');
    if (btn) toggleGroup(btn, true);
    var ul = document.getElementById('ap-grp-fraud');
    var first = ul && ul.querySelector('.ap-row');
    if (first) { select(first.getAttribute('data-key'), ul, { focus: true }); first.scrollIntoView({ block: 'nearest' }); }
  });

  /* ---- search: results replace the queues ---- */
  var input = root.querySelector('#ap-q');
  var sections = $all('.ap-list > [data-queue]');
  var results = root.querySelector('[data-results]');
  var resultsList = results ? results.querySelector('.ap-rows') : null;
  var rowIndex = $all('.ap-row-item[data-search]').map(function (li) { return li; });
  function clearSearch() { if (input && input.value) { input.value = ''; runSearch(); } }
  function runSearch() {
    var q = (input.value || '').trim().toLowerCase();
    var searching = q.length > 0;
    sections.forEach(function (s) { s.hidden = searching; });
    results.hidden = !searching;
    resultsList.textContent = '';
    if (!searching) { live.textContent = ''; return; }
    var seen = {}, hits = 0;
    rowIndex.forEach(function (li) {
      var k = li.getAttribute('data-key');
      if (seen[k] || li.getAttribute('data-search').indexOf(q) === -1) return;
      seen[k] = true; hits++;
      var clone = li.cloneNode(true);
      clone.classList.remove('is-filtered', 'is-overflow');
      resultsList.appendChild(clone);
    });
    results.querySelector('[data-count]').textContent = hits + (hits === 1 ? ' match' : ' matches');
    var none = results.querySelector('[data-none]');
    none.hidden = hits > 0;
    none.querySelector('[data-q]').textContent = input.value.trim();
    live.textContent = hits + (hits === 1 ? ' matching invoice' : ' matching invoices');
    markCurrent(state.selected || '');
  }
  if (input) input.addEventListener('input', runSearch);

  /* ---- initial state: on a wide screen, open the first thing that needs a person ---- */
  clearSelection();
  var first = reviewRows.filter(function (li) { return !li.classList.contains('is-overflow'); })[0];
  if (first && !narrow()) select(first.getAttribute('data-key'), first.parentNode);
})();
