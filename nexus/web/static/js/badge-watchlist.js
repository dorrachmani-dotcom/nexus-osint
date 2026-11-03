// Moved from templates/base.html (no inline scripts: see the CSP in nexus/web/security.py).
// Watchlist hit badge — red dot on the Watchlists nav item for unseen alerts.
(function () {
  var badge = document.getElementById('watchlist-badge');
  if (!badge) return;
  function refresh() {
    fetch('/watchlists/count', { headers: { 'Accept': 'application/json' } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        var n = d && d.total ? d.total : 0;
        if (n > 0) { badge.textContent = n > 99 ? '99+' : n; badge.classList.remove('hidden'); }
        else { badge.classList.add('hidden'); }
      })
      .catch(function () {});
  }
  refresh();
  setInterval(refresh, 180000);
})();
