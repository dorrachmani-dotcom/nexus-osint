// Moved from templates/base.html (no inline scripts: see the CSP in nexus/web/security.py).
// Live "new since last visit" badge on the Brief nav tab — refreshed on load
// and every few minutes. Fail-silent (never blocks the page).
(function () {
  var badge = document.getElementById('brief-badge');
  function refresh() {
    fetch('/brief/count', { headers: { 'Accept': 'application/json' } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        var n = d && d.total ? d.total : 0;
        if (badge) {
          if (n > 0) { badge.textContent = n > 99 ? '99+' : n; badge.classList.remove('hidden'); }
          else { badge.classList.add('hidden'); }
        }
        document.title = n > 0 ? '(' + n + ') ' + (window._nexusBaseTitle || 'Nexus') : (window._nexusBaseTitle || 'Nexus');
      })
      .catch(function () {});
  }
  window.nexusBriefRefresh = refresh;
  if (badge) refresh();
  setInterval(refresh, 180000);  // every 3 minutes
})();
