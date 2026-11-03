// Moved from templates/settings.html (no inline scripts: see the CSP in nexus/web/security.py).
(function () {
  function renderStatus(d) {
    var s = document.getElementById('auto-scan-status');
    if (!s) return;
    var el = document.getElementById('asc-' + d.interval_hours);
    if (el) el.checked = true;
    if (d.interval_hours === 0) {
      s.textContent = 'Auto-scan is off.';
    } else {
      var msg = 'Scanning every ' + d.interval_hours + ' hour(s).';
      if (d.next_at) msg += '  Next run ≈ ' + d.next_at + ' UTC.';
      if (d.last_at) msg += '  Last ran: ' + d.last_at + ' UTC.';
      s.textContent = msg;
    }
  }
  // Load current status on page ready.
  fetch('/settings/auto-scan-status')
    .then(function (r) { return r.json(); })
    .then(renderStatus)
    .catch(function () {
      var s = document.getElementById('auto-scan-status');
      if (s) s.textContent = 'Could not load status.';
    });
  // Save on form submit.
  var form = document.getElementById('auto-scan-form');
  if (form) {
    form.addEventListener('submit', function (e) {
      e.preventDefault();
      var sel = form.querySelector('input[name="interval_hours"]:checked');
      if (!sel) return;
      var body = new URLSearchParams({ interval_hours: sel.value });
      fetch('/settings/auto-scan', { method: 'POST', body: body })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (d.ok) {
            var saved = document.getElementById('asc-saved');
            if (saved) {
              saved.classList.remove('hidden');
              setTimeout(function () { saved.classList.add('hidden'); }, 2000);
            }
          }
          return fetch('/settings/auto-scan-status').then(function (r) { return r.json(); });
        })
        .then(renderStatus)
        .catch(function () {});
    });
  }
})();
