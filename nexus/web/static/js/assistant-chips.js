// Moved from templates/base.html (no inline scripts: see the CSP in nexus/web/security.py).
// Clicking a suggestion fills the input so users discover what Sherlock can do.
document.addEventListener('click', function (e) {
  var chip = e.target && e.target.closest && e.target.closest('.asst-chip');
  if (!chip) return;
  var t = document.getElementById('assistant-input');
  if (t) { t.value = chip.getAttribute('data-prompt') || ''; t.focus(); }
});
