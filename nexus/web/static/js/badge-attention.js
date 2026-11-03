// Moved from templates/base.html (no inline scripts: see the CSP in nexus/web/security.py).
// Live "Needs your eyes" count on the Focus nav tab. Fail-silent.
(function () {
  var badge = document.getElementById('attention-badge');
  if (!badge) return;
  function refresh() {
    fetch('/attention/count', { headers: { 'Accept': 'application/json' } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        var n = d && d.total ? d.total : 0;
        if (n > 0) { badge.textContent = n > 99 ? '99+' : n; badge.classList.remove('hidden'); }
        else { badge.classList.add('hidden'); }
      })
      .catch(function () {});
  }
  window.nexusAttentionRefresh = refresh;
  refresh();
  setInterval(refresh, 180000);  // every 3 minutes
})();
