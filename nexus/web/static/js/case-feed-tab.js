// Moved from templates/_case_feed_tab.html (no inline scripts: see the CSP in nexus/web/security.py).
(function () {
  var ed = document.getElementById('tw-editor');
  if (!ed) return;
  var key = 'nexus-tw-open-' + ed.dataset.case;
  function show(open) {
    ed.classList.toggle('hidden', !open);
    var b = document.getElementById('tw-edit');
    if (b) {
      b.setAttribute('aria-expanded', open ? 'true' : 'false');
      var l = b.querySelector('.tw-edit-label');
      if (l) l.textContent = open ? 'Hide' : 'Edit';
    }
  }
  try {
    var saved = sessionStorage.getItem(key);
    if (saved !== null) show(saved === '1');
  } catch (e) {}
  window.nexusToggleTerms = function () {
    var e = document.getElementById('tw-editor');
    if (!e) return;
    var open = e.classList.contains('hidden');
    show(open);
    try { sessionStorage.setItem('nexus-tw-open-' + e.dataset.case, open ? '1' : '0'); } catch (x) {}
  };
})();
