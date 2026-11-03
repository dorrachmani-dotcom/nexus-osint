// Moved from templates/topics.html (no inline scripts: see the CSP in nexus/web/security.py).
// Update placeholder + hint to match the selected source.
(function () {
  var sel = document.getElementById("topic-source");
  var val = document.getElementById("topic-value");
  var hint = document.getElementById("topic-hint");
  if (!sel) return;
  sel.addEventListener("change", function () {
    var opt = sel.options[sel.selectedIndex];
    val.placeholder = opt.getAttribute("data-placeholder") || "";
    hint.textContent = opt.getAttribute("data-hint") || "";
  });
})();
