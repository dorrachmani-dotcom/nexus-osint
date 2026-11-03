// Moved from templates/feed.html (no inline scripts: see the CSP in nexus/web/security.py).
// Keyboard triage: move through cards with j/k and act without the mouse.
//   j / k  : next / previous item      o : open the source
//   b      : bookmark (star)           r : mark read       x : select (bulk)
// Inert while typing in a field, so search etc. are unaffected.
(function () {
  var idx = -1;
  function cards() { return Array.prototype.slice.call(document.querySelectorAll('#feed-items [id^="item-"]')); }
  function focusCard(i) {
    var list = cards();
    if (!list.length) return;
    idx = Math.max(0, Math.min(i, list.length - 1));
    list.forEach(function (c, n) { c.classList.toggle('ring-2', n === idx); c.classList.toggle('ring-amber-500', n === idx); });
    list[idx].scrollIntoView({ block: 'nearest' });
  }
  function clickIn(card, selector) { var el = card && card.querySelector(selector); if (el) el.click(); }
  document.addEventListener('keydown', function (e) {
    var tag = (e.target && e.target.tagName) || '';
    if (/^(INPUT|TEXTAREA|SELECT)$/.test(tag) || e.target.isContentEditable) return;
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    var list = cards();
    if (!list.length) return;
    var card = list[idx];
    switch (e.key) {
      case 'j': e.preventDefault(); focusCard(idx + 1); break;
      case 'k': e.preventDefault(); focusCard(idx <= 0 ? 0 : idx - 1); break;
      case 'o': if (card) { e.preventDefault(); var a = card.querySelector('a[target="_blank"]'); if (a) window.open(a.href, '_blank', 'noopener'); } break;
      case 'b': if (card) { e.preventDefault(); clickIn(card, 'button[hx-post$="/bookmark"], button[hx-post$="/unbookmark"]'); } break;
      case 'r': if (card) { e.preventDefault(); clickIn(card, 'button[hx-post*="/read"]'); } break;
      case 'd': if (card) { e.preventDefault(); clickIn(card, 'button[hx-post*="/dismiss"]'); } break;
      case 'v': if (card) { e.preventDefault(); clickIn(card, 'button[hx-get*="/detail"]'); } break;
      case 'p': if (card) { e.preventDefault(); clickIn(card, 'button[title^="Pin to active case"]'); } break;
      case 'x': if (card) { e.preventDefault(); var cb = card.querySelector('.bulk-select'); if (cb) { cb.checked = !cb.checked; cb.dispatchEvent(new Event('change', { bubbles: true })); } } break;
    }
  });
})();
