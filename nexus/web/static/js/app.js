// Moved from templates/base.html (no inline scripts: see the CSP in nexus/web/security.py).
// ---- colour theme toggle ----
function nexusSyncThemeIcon() {
  var light = document.documentElement.classList.contains('light');
  var sun = document.querySelector('.theme-dark-icon');
  var moon = document.querySelector('.theme-light-icon');
  if (sun) sun.style.display = light ? 'none' : 'inline';
  if (moon) moon.style.display = light ? 'inline' : 'none';
}
function nexusToggleTheme() {
  var isLight = document.documentElement.classList.toggle('light');
  try { localStorage.setItem('nexus-theme', isLight ? 'light' : 'dark'); } catch (e) {}
  nexusSyncThemeIcon();
}
document.addEventListener('DOMContentLoaded', nexusSyncThemeIcon);

// ---- failure toasts ----
(function () {
  function toast(message) {
    var host = document.getElementById('toast-host');
    if (!host) return;
    var el = document.createElement('div');
    el.className = 'rounded-lg px-4 py-2.5 text-sm shadow-lg border max-w-xs ' +
      'cursor-pointer transition-opacity duration-300 ' +
      'bg-rose-950 border-rose-800 text-rose-100';
    el.textContent = message;
    el.title = 'Click to dismiss';
    el.addEventListener('click', function () { el.remove(); });
    host.appendChild(el);
    setTimeout(function () {
      el.style.opacity = '0';
      setTimeout(function () { el.remove(); }, 300);
    }, 5000);
  }
  // 4xx/5xx: the server answered but htmx won't swap an error response.
  document.body.addEventListener('htmx:responseError', function () {
    toast('That action did not go through. Please try again.');
  });
  // No answer at all: app stopped, or the connection dropped.
  document.body.addEventListener('htmx:sendError', function () {
    toast('Could not reach the local app — is it still running?');
  });
})();

// ---- analyst assistant (floating chat) ----
(function () {
  var history = [];  // [{role:'user'|'assistant', content:'...'}]
  // What the chat log shows, so it can be rebuilt after a page navigation:
  // [{role:'user', text} | {role:'bot', text, items, sources, actions}].
  var transcript = [];

  function el(id) { return document.getElementById(id); }

  // ---- keep the conversation across page navigations (this tab only) ----
  // sessionStorage lives in THIS browser tab and is gone when it closes; it
  // never leaves the machine. Restored messages are re-rendered through the
  // same text-only functions as live ones (no innerHTML). Every access is
  // guarded: storage can be blocked, full or hold junk.
  var STORE_KEY = 'nexus.sherlock.v1';
  function panelOpen() {
    var p = el('assistant-panel');
    return !!(p && !p.classList.contains('hidden'));
  }
  function persist() {
    try {
      window.sessionStorage.setItem(STORE_KEY, JSON.stringify({
        open: panelOpen(), history: history.slice(-40), log: transcript.slice(-60)
      }));
    } catch (e) {}
  }
  function loadStored() {
    try {
      var d = JSON.parse(window.sessionStorage.getItem(STORE_KEY) || 'null');
      return (d && typeof d === 'object') ? d : null;
    } catch (e) { return null; }
  }
  function clearStored() {
    try { window.sessionStorage.removeItem(STORE_KEY); } catch (e) {}
  }
  // A stored action must never auto-open a tab again when it is replayed.
  function replayableActions(actions) {
    if (!Array.isArray(actions)) return [];
    return actions.filter(function (a) { return a && typeof a === 'object'; })
      .map(function (a) {
        var c = {};
        // A replayed publish card is shown inert (its one-time token is dropped).
        for (var k in a) { if (k !== 'open' && k !== 'token') c[k] = a[k]; }
        return c;
      });
  }

  function setPanel(show, focus) {
    var p = el('assistant-panel');
    var launcher = el('assistant-launcher');
    if (!p) return;
    p.classList.toggle('hidden', !show);
    p.classList.toggle('flex', show);
    if (launcher) launcher.style.display = show ? 'none' : 'flex';
    if (show && focus) { var i = el('assistant-input'); if (i) i.focus(); }
  }

  window.nexusAssistantToggle = function (force) {
    var p = el('assistant-panel');
    if (!p) return;
    var show = (typeof force === 'boolean') ? force : p.classList.contains('hidden');
    setPanel(show, true);
    persist();
  };

  // Safely render a bot message, turning "Item N" references into clickable
  // links. Uses document.createDocumentFragment — no innerHTML, model output
  // can never inject HTML. items is [{n, id, url, title}, ...] from the server.
  function renderBotText(div, text, items) {
    items = items || [];
    var byN = {};
    items.forEach(function (it) { if (it && it.n) byN[it.n] = it; });
    // Split on word-boundary Item N tokens (handles "Item 1", "Items 1, 2", etc.)
    var parts = text.split(/\b(Item\s+\d+)\b/gi);
    var frag = document.createDocumentFragment();
    for (var i = 0; i < parts.length; i++) {
      var m = parts[i].match(/^Item\s+(\d+)$/i);
      if (m) {
        var n = parseInt(m[1], 10);
        var it = byN[n];
        if (it && it.url && /^https?:\/\//i.test(it.url)) {
          var a = document.createElement('a');
          a.href = it.url;
          a.target = '_blank';
          a.rel = 'noopener';
          a.textContent = parts[i];
          a.className = 'text-sky-400 underline hover:text-sky-300';
          a.title = it.title || parts[i];
          frag.appendChild(a);
          continue;
        }
      }
      frag.appendChild(document.createTextNode(parts[i]));
    }
    div.textContent = '';
    div.appendChild(frag);
  }

  function addBubble(role, text, items) {
    var log = el('assistant-log');
    if (!log) return null;
    var div = document.createElement('div');
    div.style.whiteSpace = 'pre-wrap';
    if (role === 'user') {
      div.className = 'assistant-msg-user rounded-lg bg-amber-500/15 text-slate-100 px-3 py-2 ml-6';
      div.textContent = text;  // user input: plain text only
    } else {
      div.className = 'assistant-msg-bot rounded-lg bg-slate-800 text-slate-200 px-3 py-2';
      renderBotText(div, text, items);
    }
    log.appendChild(div);
    log.scrollTop = log.scrollHeight;
    return div;
  }

  // Render the actions Sherlock actually performed (case made, items added,
  // a report to open). Each becomes a clickable row; report rows auto-open.
  function renderActions(actions) {
    var log = el('assistant-log');
    if (!log || !actions || !actions.length) return;
    var wrap = document.createElement('div');
    wrap.className = 'space-y-1';
    actions.forEach(function (a) {
      if (!a || !a.label) return;
      // Only in-app paths and http(s) links are ever used as an href or opened
      // (a stored/replayed action can never smuggle in a javascript: URL).
      var url = (typeof a.url === 'string' && /^(\/(?!\/)|https?:\/\/)/i.test(a.url)) ? a.url : '';
      // A "navigate" action takes the analyst to a page in THIS window — a
      // prominent amber button (not a new tab, never auto-fired so a click
      // is always an explicit choice).
      // A publish Sherlock PREPARED: show exactly what would be sent and let
      // the analyst confirm with a click (the model can never execute it).
      if (a.type === 'confirm_publish') {
        var card = document.createElement('div');
        card.className = 'rounded-md border border-sky-800 bg-slate-900 px-3 py-2 text-xs text-slate-200 space-y-1.5';
        var head = document.createElement('div');
        head.className = 'font-semibold text-sky-300';
        head.textContent = '⇧ ' + a.label;
        card.appendChild(head);
        var pre = document.createElement('pre');
        pre.className = 'max-h-48 overflow-auto rounded border border-slate-800 bg-slate-950 p-2 text-[11px] whitespace-pre-wrap mono';
        pre.textContent = String(a.preview || '');
        card.appendChild(pre);
        var status = document.createElement('div');
        if (typeof a.token === 'string' && a.token) {
          var btn = document.createElement('button');
          btn.type = 'button';
          btn.className = 'px-2 py-1 rounded text-xs bg-amber-500 hover:bg-amber-400 text-slate-950 font-medium';
          btn.textContent = a.confirm_label || 'Confirm & publish';
          btn.addEventListener('click', function () {
            btn.disabled = true;
            fetch('/publish/confirm.json', {method: 'POST', headers: {'Content-Type': 'application/json'},
              body: JSON.stringify({token: a.token})})
              .then(function (r) { return r.json(); })
              .then(function (d) {
                btn.remove();
                status.className = d.ok ? 'text-emerald-300' : 'text-rose-300';
                status.textContent = (d.ok ? '✓ ' : '') + (d.label || '');
              })
              .catch(function () { btn.disabled = false; status.textContent = 'Could not reach the app.'; });
          });
          card.appendChild(btn);
        } else {
          status.className = 'text-slate-500';
          status.textContent = 'This preview is no longer active; ask again to publish.';
        }
        card.appendChild(status);
        wrap.appendChild(card);
        return;
      }
      if (a.type === 'navigate' && url) {
        var nav = document.createElement('a');
        nav.href = url; // same-window navigation, no target=_blank
        nav.className = 'block rounded-md border border-amber-500/70 bg-amber-500/15 ' +
          'hover:bg-amber-500/25 px-3 py-2 text-xs font-semibold text-amber-200 ' +
          'no-underline text-center';
        nav.textContent = '➡️ ' + a.label;
        wrap.appendChild(nav);
        return;
      }
      var icon = a.type === 'report' ? '📄 '
        : a.type === 'case_created' ? '🗂️ '
        : a.type === 'items_added' ? '➕ '
        : a.type === 'scan' ? '🔄 '
        : a.type === 'search' ? '🔎 '
        : a.type === 'watchlist' ? '🔔 '
        : a.type === 'requirement' ? '🎯 '
        : a.type === 'note' ? '📝 '
        : a.type === 'evidence' ? '📸 '
        : a.type === 'error' ? '⚠️ ' : '';
      var row;
      if (url) {
        row = document.createElement('a');
        row.href = url;
        // Pages inside the app open in the same window (the chat panel and
        // its history come along); anything else opens in a new tab.
        if (!/^\/(?!\/)/.test(url)) { row.target = '_blank'; row.rel = 'noopener'; }
        row.className = 'block rounded-md border border-slate-700 bg-slate-800/70 ' +
          'hover:bg-slate-700 px-3 py-2 text-xs text-slate-100 no-underline';
      } else {
        row = document.createElement('div');
        row.className = 'block rounded-md border px-3 py-2 text-xs ' +
          (a.type === 'error'
            ? 'border-rose-800 bg-rose-950/40 text-rose-200'
            : 'border-slate-700 bg-slate-800/70 text-slate-200');
      }
      row.textContent = icon + a.label;
      wrap.appendChild(row);
      // A search_feed action can carry a short list of matching items; render
      // each as its own clickable link (new tab) under the summary row. Only
      // http(s) hrefs are accepted (the server already filtered these).
      if (a.items && a.items.length) {
        a.items.forEach(function (it) {
          if (!it || !it.title) return;
          var safe = (typeof it.url === 'string' &&
                      /^https?:\/\//i.test(it.url)) ? it.url : null;
          var sub;
          if (safe) {
            sub = document.createElement('a');
            sub.href = safe; sub.target = '_blank'; sub.rel = 'noopener';
            sub.className = 'block rounded border border-slate-700/70 bg-slate-900/50 ' +
              'hover:bg-slate-800 px-3 py-1.5 ml-3 text-xs text-slate-200 no-underline';
          } else {
            sub = document.createElement('div');
            sub.className = 'block rounded border border-slate-700/70 bg-slate-900/50 ' +
              'px-3 py-1.5 ml-3 text-xs text-slate-300';
          }
          sub.textContent = '• ' + it.title + (it.meta ? ('  (' + it.meta + ')') : '');
          wrap.appendChild(sub);
        });
      }
      // Honour "open this for me" (e.g. the PDF). Pop-up blockers may stop
      // this; the clickable row above is always the reliable fallback.
      if (a.open && url) { try { window.open(url, '_blank', 'noopener'); } catch (e) {} }
    });
    log.appendChild(wrap);
    log.scrollTop = log.scrollHeight;
  }

  // Show the sources an answer is based on: the collected items Sherlock
  // cited as "(Item N)". Parsed from the answer text so the list reflects
  // what the answer actually used; each becomes a clickable link (http(s)
  // only — item URLs are source-derived and must never run as a scheme).
  // Renders nothing when the answer cited no items (e.g. how-to guidance).
  function renderSources(items, answerText) {
    items = items || [];
    if (!items.length || !answerText) return;
    var byN = {};
    items.forEach(function (it) { if (it && it.n) byN[it.n] = it; });
    var cited = [], seen = {};
    var re = /\bItem\s+(\d+)\b/gi, m;
    while ((m = re.exec(answerText)) !== null) {
      var n = parseInt(m[1], 10);
      if (byN[n] && !seen[n]) { seen[n] = true; cited.push(byN[n]); }
    }
    if (!cited.length) return;
    var log = el('assistant-log');
    if (!log) return;
    var wrap = document.createElement('div');
    wrap.className = 'rounded-lg border border-slate-700 bg-slate-900/60 px-3 py-2 space-y-1';
    var head = document.createElement('div');
    head.className = 'text-[10px] uppercase tracking-wide text-slate-500 font-semibold';
    head.textContent = '🔗 Sources (' + cited.length + ')';
    wrap.appendChild(head);
    cited.forEach(function (it) {
      var meta = [it.source, it.when].filter(Boolean).join(' · ');
      var label = 'Item ' + it.n + ' · ' + (it.title || '(untitled)') + (meta ? ('  — ' + meta) : '');
      var safe = (typeof it.url === 'string' && /^https?:\/\//i.test(it.url)) ? it.url : null;
      var row;
      if (safe) {
        row = document.createElement('a');
        row.href = safe; row.target = '_blank'; row.rel = 'noopener';
        row.className = 'block rounded border border-slate-700/70 bg-slate-800/50 ' +
          'hover:bg-slate-700 px-2 py-1 text-[11px] text-sky-300 no-underline';
      } else {
        row = document.createElement('div');
        row.className = 'block rounded border border-slate-700/70 bg-slate-800/50 ' +
          'px-2 py-1 text-[11px] text-slate-300';
      }
      row.textContent = label;
      wrap.appendChild(row);
    });
    log.appendChild(wrap);
    log.scrollTop = log.scrollHeight;
  }

  function resetChat() {
    history = [];
    transcript = [];
    persist();  // overwrites the saved chat; keeps only open/closed state
    var log = el('assistant-log');
    // No AI configured: keep the "set up AI" card instead of a chat greeting.
    if (el('assistant-needs-ai')) return;
    if (log) {
      log.innerHTML = '';
      var b = document.createElement('div');
      b.className = 'assistant-msg-bot rounded-lg bg-slate-800 text-slate-200 px-3 py-2';
      b.style.whiteSpace = 'pre-wrap';
      b.textContent = "Fresh start. I'm Sherlock — ask me anything, or tell me to build a case, "
        + "fill it with matching items, and make you a report.";
      log.appendChild(b);
    }
  }

  function saveTranscriptLocally() {
    return fetch('/assistant/save-log', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ transcript: history })
    }).catch(function () {});
  }

  // Tear down any open end-chat dialog.
  function closeEndDialog() {
    var ov = el('assistant-end-modal');
    if (ov && ov.parentNode) ov.parentNode.removeChild(ov);
  }

  // End the conversation. Offer an opt-in LOCAL save first via a clean in-app
  // modal (no jarring native browser dialog). Saving keeps a record of the
  // questions on THIS machine only — useful for retracing an investigation —
  // and never sends anything anywhere.
  window.nexusAssistantEnd = function () {
    if (!history.length) { resetChat(); return; }
    closeEndDialog();

    var overlay = document.createElement('div');
    overlay.id = 'assistant-end-modal';
    overlay.className = 'fixed inset-0 z-[60] flex items-center justify-center bg-black/60 p-4';

    var card = document.createElement('div');
    card.className = 'w-[340px] max-w-full rounded-xl border border-slate-700 bg-slate-900 '
      + 'shadow-2xl text-slate-200 overflow-hidden';

    var head = document.createElement('div');
    head.className = 'px-4 py-3 border-b border-slate-800 text-sm font-semibold text-slate-100';
    head.textContent = 'End this conversation?';

    var body = document.createElement('div');
    body.className = 'px-4 py-3 text-[13px] leading-relaxed text-slate-300 space-y-2';
    var p1 = document.createElement('p');
    p1.textContent = 'You can keep a copy of this chat — your questions and my replies — '
      + 'as a local record of your investigation.';
    var p2 = document.createElement('p');
    p2.className = 'text-[12px] text-slate-400';
    p2.textContent = 'It is saved only on this computer and never sent anywhere. '
      + 'Handy for retracing your line of inquiry or handing notes to a colleague.';
    body.appendChild(p1); body.appendChild(p2);

    var actions = document.createElement('div');
    actions.className = 'px-4 py-3 border-t border-slate-800 flex flex-col gap-2';

    var bSave = document.createElement('button');
    bSave.type = 'button';
    bSave.className = 'w-full rounded bg-emerald-600 hover:bg-emerald-500 text-white '
      + 'font-medium px-3 py-2 text-sm';
    bSave.textContent = 'Save a copy & end';
    bSave.onclick = function () { saveTranscriptLocally(); closeEndDialog(); resetChat(); };

    var bPlain = document.createElement('button');
    bPlain.type = 'button';
    bPlain.className = 'w-full rounded border border-slate-600 hover:bg-slate-800 '
      + 'text-slate-200 px-3 py-2 text-sm';
    bPlain.textContent = 'End without saving';
    bPlain.onclick = function () { closeEndDialog(); resetChat(); };

    var bCancel = document.createElement('button');
    bCancel.type = 'button';
    bCancel.className = 'w-full text-slate-400 hover:text-slate-200 px-3 py-1 text-xs';
    bCancel.textContent = 'Keep chatting';
    bCancel.onclick = closeEndDialog;

    actions.appendChild(bSave); actions.appendChild(bPlain); actions.appendChild(bCancel);
    card.appendChild(head); card.appendChild(body); card.appendChild(actions);
    overlay.appendChild(card);
    // Click outside the card dismisses (same as "Keep chatting").
    overlay.addEventListener('click', function (e) { if (e.target === overlay) closeEndDialog(); });
    document.body.appendChild(overlay);
  };

  function autosize(t) {
    t.style.height = 'auto';
    t.style.height = Math.min(t.scrollHeight, 120) + 'px';
  }

  // Rebuild the conversation saved by an earlier page in this tab. Each
  // entry goes through addBubble / renderSources / renderActions — the
  // same text-only path as a live answer.
  function restoreChat() {
    var d = loadStored();
    if (!d) return;
    // No AI model configured: nothing to continue, drop any leftovers.
    if (el('assistant-needs-ai')) { clearStored(); return; }
    if (Array.isArray(d.history)) {
      history = d.history.filter(function (t) {
        return t && (t.role === 'user' || t.role === 'assistant') &&
          typeof t.content === 'string';
      });
    }
    if (Array.isArray(d.log)) {
      d.log.forEach(function (m) {
        if (!m || typeof m.text !== 'string') return;
        if (m.role === 'user') {
          addBubble('user', m.text);
        } else if (m.role === 'bot') {
          var items = Array.isArray(m.items) ? m.items : [];
          addBubble('assistant', m.text, items);
          if (m.sources) renderSources(items, m.text);
          renderActions(replayableActions(m.actions));
        } else {
          return;
        }
        transcript.push(m);
      });
    }
    if (d.open === true) setPanel(true, false);
  }

  document.addEventListener('DOMContentLoaded', function () {
    restoreChat();
    var form = el('assistant-form');
    var input = el('assistant-input');
    if (!form || !input) return;

    input.addEventListener('input', function () { autosize(input); });
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        form.requestSubmit();
      }
    });

    form.addEventListener('submit', function (e) {
      e.preventDefault();
      var q = input.value.trim();
      if (!q) return;
      var sendBtn = el('assistant-send');

      addBubble('user', q);
      history.push({ role: 'user', content: q });
      transcript.push({ role: 'user', text: q });
      persist();
      input.value = '';
      autosize(input);

      var thinking = addBubble('assistant', 'Sherlock is thinking…');
      thinking.classList.add('animate-pulse');
      if (sendBtn) sendBtn.disabled = true;
      // Reassure on a slow model, and never let the bubble hang forever.
      var softTimer = setTimeout(function () {
        if (thinking) thinking.textContent = 'Still working — the AI model is taking a little while…';
      }, 18000);
      var controller = new AbortController();
      var hardTimer = setTimeout(function () { controller.abort(); }, 90000);

      fetch('/assistant/ask', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        signal: controller.signal,
        // Send the current page so Sherlock knows what the analyst is looking
        // at ("this", "here", "the current case" resolve to the open view).
        body: JSON.stringify({
          question: q,
          history: history.slice(0, -1),
          page: window.location.pathname + window.location.search,
          deep: !!(document.getElementById('assistant-deep') || {}).checked,
          cite: !!(document.getElementById('assistant-cite') || {}).checked
        })
      })
        .then(function (r) {
          // Read the body as text first so a non-JSON / error response
          // (e.g. a 404 from an older app version) gives a clear message
          // instead of the vague "could not answer that".
          return r.text().then(function (t) {
            var d = null;
            try { d = JSON.parse(t); } catch (e) { d = null; }
            if (!d || typeof d !== 'object') {
              d = {
                ok: false, actions: [],
                error: (r.status === 404
                  ? 'Sherlock isn’t available on this version of the app — please update or restart it.'
                  : 'Sherlock hit a problem (HTTP ' + r.status + '). Please try again.')
              };
            }
            return d;
          });
        })
        .then(function (data) {
          if (thinking) thinking.classList.remove('animate-pulse');
          var msg = data.ok ? (data.answer || 'Done.')
            : (data.error || 'Sorry, I could not answer that.');
          if (thinking) renderBotText(thinking, msg, data.items || []);
          if (data.ok && data.answer) renderSources(data.items || [], data.answer);
          if (data.ok && data.answer) history.push({ role: 'assistant', content: data.answer });
          if (data.ok && data.actions) renderActions(data.actions);
          transcript.push({
            role: 'bot', text: msg, items: data.items || [],
            sources: !!(data.ok && data.answer),
            actions: data.ok ? replayableActions(data.actions) : []
          });
          persist();
          var log = el('assistant-log');
          if (log) log.scrollTop = log.scrollHeight;
        })
        .catch(function (err) {
          if (thinking) {
            thinking.classList.remove('animate-pulse');
            thinking.textContent = (err && err.name === 'AbortError')
              ? 'The AI took too long to answer — it may be busy. Please try again.'
              : 'Could not reach the local app. Is it still running?';
          }
        })
        .finally(function () {
          clearTimeout(softTimer); clearTimeout(hardTimer);
          if (sendBtn) sendBtn.disabled = false;
          input.focus();
        });
    });
  });
})();
