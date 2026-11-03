// Moved from templates/feed.html (no inline scripts: see the CSP in nexus/web/security.py).
(function () {
  function selected() {
    return document.querySelectorAll('.bulk-select:checked');
  }
  function refresh() {
    var n = selected().length;
    var bar = document.getElementById('bulk-bar');
    var cnt = document.getElementById('bulk-count');
    if (cnt) cnt.textContent = n;
    if (bar) bar.classList.toggle('hidden', n === 0);
    // Keep any "select all" box in sync with the visible rows.
    var all = document.getElementById('bulk-all');
    if (all) {
      var boxes = document.querySelectorAll('.bulk-select');
      all.checked = boxes.length > 0 && n === boxes.length;
    }
  }
  // Delegated so it keeps working after htmx swaps the feed list.
  document.addEventListener('change', function (e) {
    var t = e.target;
    if (!t) return;
    if (t.id === 'bulk-all') {
      document.querySelectorAll('.bulk-select').forEach(function (c) { c.checked = t.checked; });
      refresh();
    } else if (t.classList && t.classList.contains('bulk-select')) {
      refresh();
    }
  });
  document.addEventListener('click', function (e) {
    if (e.target && e.target.id === 'bulk-clear') {
      document.querySelectorAll('.bulk-select').forEach(function (c) { c.checked = false; });
      refresh();
    }
    // Export: open /export with the feed's current filter values so the file
    // reflects exactly what the analyst is looking at, in the chosen format
    // (PDF / HTML report, or CSV / JSON data). If the browser's popup blocker
    // swallows the new tab, fall back to the same tab so the click always works.
    var opt = e.target && e.target.closest && e.target.closest('.export-opt');
    if (opt) {
      e.preventDefault();
      var form = document.getElementById('feed-filter');
      var params = new URLSearchParams(new FormData(form));
      params.set('format', opt.getAttribute('data-format') || 'pdf');
      var url = '/export?' + params.toString();
      var win = window.open(url, '_blank');
      if (!win || win.closed || typeof win.closed === 'undefined') {
        window.location.href = url;  // popup blocked -> use the current tab
      }
      var menu = document.getElementById('export-menu');
      if (menu) menu.removeAttribute('open');  // close the dropdown
    }
  });
  // After a feed swap (filter / scan / bulk action) or a Load-more append,
  // re-sync the bulk bar with whatever rows are now on screen.
  document.body.addEventListener('htmx:afterSwap', function (evt) {
    var t = evt.target;
    if (t && (t.id === 'feed' || t.id === 'feed-items' ||
              (t.closest && t.closest('#feed')))) refresh();
  });
})();
