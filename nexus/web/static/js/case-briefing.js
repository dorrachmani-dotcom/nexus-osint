// Moved from templates/_case_briefing.html (no inline scripts: see the CSP in nexus/web/security.py).
// Turns "Item N" references in the AI briefing into buttons that open that item.
(function() {
  const src = document.getElementById('briefing-items');
  let items = [];
  try { items = JSON.parse(src ? src.textContent : '[]') || []; } catch (e) { items = []; }
  const el = document.getElementById('briefing-text');
  if (!el || !items.length) return;
  const text = el.textContent;
  const parts = text.split(/\b(Item\s+\d+)\b/gi);
  if (parts.length <= 1) return;
  const frag = document.createDocumentFragment();
  for (let i = 0; i < parts.length; i++) {
    const m = parts[i].match(/^Item\s+(\d+)$/i);
    if (m) {
      const n = parseInt(m[1], 10);
      const item = items[n - 1];
      if (item && item.id) {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.textContent = parts[i];
        btn.title = (item.title || '') + ' — click to open details';
        btn.className = 'text-violet-300 hover:text-violet-100 underline underline-offset-2 cursor-pointer';
        btn.onclick = (function(id) {
          return function() {
            htmx.ajax('GET', '/items/' + id + '/detail', {target: '#item-drawer-content', swap: 'innerHTML'});
          };
        })(item.id);
        frag.appendChild(btn);
        continue;
      }
    }
    frag.appendChild(document.createTextNode(parts[i]));
  }
  el.textContent = '';
  el.appendChild(frag);
})();
