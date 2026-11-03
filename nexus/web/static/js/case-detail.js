// Moved from templates/case_detail.html (no inline scripts: see the CSP in nexus/web/security.py).
document.body.addEventListener('htmx:afterSwap', function(e) {
  if (e.detail.target.id !== 'case-feed-tab') return;
  var count = parseInt(e.detail.target.dataset.unread, 10) || 0;
  var badge = document.getElementById('feed-unread-badge');
  if (!badge) return;
  if (count > 0) { badge.textContent = count; badge.classList.remove('hidden'); }
  else { badge.classList.add('hidden'); }
  if (typeof window.nexusBriefRefresh === 'function') window.nexusBriefRefresh();
});
