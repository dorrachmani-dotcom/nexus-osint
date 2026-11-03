// Moved from templates/intel.html (no inline scripts: see the CSP in nexus/web/security.py).
document.addEventListener('click', function (e) {
  var opt = e.target && e.target.closest && e.target.closest('.intel-export-opt');
  if (!opt) return;
  e.preventDefault();
  var form = document.getElementById('intel-filters');
  var params = new URLSearchParams(new FormData(form));
  params.set('format', opt.getAttribute('data-format') || 'csv');
  var url = '/intel/export?' + params.toString();
  var win = window.open(url, '_blank');
  if (!win || win.closed || typeof win.closed === 'undefined') window.location.href = url;
  var menu = document.getElementById('intel-export-menu');
  if (menu) menu.removeAttribute('open');
});
