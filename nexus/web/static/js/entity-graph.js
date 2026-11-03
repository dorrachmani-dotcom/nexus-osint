// Moved from templates/_entity_graph.html (no inline scripts: see the CSP in nexus/web/security.py).
(function () {
  var hidden = new Set();
  var ON  = ['bg-amber-500','text-slate-950','border-amber-500','font-semibold'];
  var OFF = ['bg-slate-800','text-slate-400','border-slate-700'];
  window.toggleKind = function (btn) {
    var kind = btn.getAttribute('data-kind');
    if (hidden.has(kind)) {
      hidden.delete(kind);
      OFF.forEach(function(c){btn.classList.remove(c);}); ON.forEach(function(c){btn.classList.add(c);});
    } else {
      hidden.add(kind);
      ON.forEach(function(c){btn.classList.remove(c);}); OFF.forEach(function(c){btn.classList.add(c);});
    }
    applyFilter();
  };
  window.resetKinds = function () {
    hidden.clear();
    document.querySelectorAll('.kind-pill').forEach(function (b) {
      OFF.forEach(function(c){b.classList.remove(c);}); ON.forEach(function(c){b.classList.add(c);});
    });
    applyFilter();
  };
  function applyFilter() {
    document.querySelectorAll('[data-entity-kind]').forEach(function (el) {
      el.style.display = hidden.has(el.getAttribute('data-entity-kind')) ? 'none' : '';
    });
  }
})();
