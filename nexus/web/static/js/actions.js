// Declarative UI actions, so no template needs inline JavaScript (the
// Content-Security-Policy allows scripts from this origin only).
//
//   data-action="name[:arg]"   on click   (several, separated by ";")
//   data-change="name[:arg]"   on change
//   data-toggled="name[:arg]"  on a <details> toggle
//   data-done="name[:arg]"     after the element's htmx request finishes
//                              (replaces hx-on::after-request)
//
// Text and classes for "done" states come from data-done-text and
// data-done-class. Only the named actions below exist: attribute values are
// never evaluated as code.
(function () {
  'use strict';

  function $(sel) { try { return document.querySelector(sel); } catch (e) { return null; } }

  function parse(spec) {
    return String(spec || '').split(';').map(function (s) { return s.trim(); })
      .filter(Boolean).map(function (s) {
        var i = s.indexOf(':');
        return i < 0 ? [s, ''] : [s.slice(0, i), s.slice(i + 1)];
      });
  }

  // Global functions defined by the app's own scripts that markup may call.
  var CALLABLE = {
    nexusToggleTheme: 1, nexusAssistantEnd: 1, nexusAssistantToggle: 1,
    closeItemDrawer: 1, nexusToggleTerms: 1, toggleKind: 1, resetKinds: 1
  };

  // Destinations for the 'goto' action.
  var GOTO = { brief: '/brief' };

  function briefRefresh() {
    if (typeof window.nexusBriefRefresh === 'function') window.nexusBriefRefresh();
  }

  var ACTIONS = {
    'history-back': function () { history.back(); },
    'history-forward': function () { history.forward(); },
    'nav-toggle': function (el) {
      var n = document.getElementById('main-nav');
      if (!n) return;
      var hidden = n.classList.toggle('max-md:hidden');
      el.setAttribute('aria-expanded', hidden ? 'false' : 'true');
    },
    'call': function (el, fn) {
      if (!CALLABLE[fn] || typeof window[fn] !== 'function') return;
      // data-arg: "this" passes the element, "true"/"false" a boolean,
      // absent calls the function with no arguments.
      var arg = el.getAttribute('data-arg');
      if (arg === 'this') window[fn](el);
      else if (arg === 'false') window[fn](false);
      else if (arg === 'true') window[fn](true);
      else window[fn]();
    },
    'remove': function (el, sel) { var t = $(sel); if (t) t.remove(); },
    'remove-closest': function (el, sel) { var t = el.closest(sel); if (t) t.remove(); },
    'copy': function (el) {
      if (!navigator.clipboard) return;
      var original = el.textContent;
      navigator.clipboard.writeText(el.getAttribute('data-copy') || '').then(function () {
        el.textContent = '✓';
        setTimeout(function () { el.textContent = original; }, 1500);
      });
    },
    'toggle-details': function (el, sel) {
      var d = $(sel);
      if (!d) return;
      d.open = !d.open;
      if (d.open) {
        var focus = d.querySelector('[data-autofocus]');
        if (focus) focus.focus(); else d.scrollIntoView({ block: 'nearest' });
      }
    },
    'show-if': function (el, arg) {
      // show-if:<value>:<selector>
      var i = arg.indexOf(':');
      var t = $(arg.slice(i + 1));
      if (t) t.style.display = el.value === arg.slice(0, i) ? 'block' : 'none';
    },
    'toggle-label': function (el, sel) {
      // A <details> toggled: update the button that opens it.
      var b = $(sel);
      if (!b) return;
      b.textContent = el.open ? (b.getAttribute('data-open-label') || '') :
                                (b.getAttribute('data-closed-label') || '');
      b.setAttribute('aria-expanded', el.open ? 'true' : 'false');
    },
    // --- after an htmx request ---------------------------------------------
    'reset': function (el) { if (el.reset) el.reset(); },
    'reset-if-ok': function (el, arg, evt) { if (evt && evt.detail.successful && el.reset) el.reset(); },
    'close-if-ok': function (el, sel, evt) {
      var d = $(sel);
      if (d && evt && evt.detail.successful) d.open = false;
    },
    'done': function (el) {
      // Mark a button as done: new text, optional class, disabled.
      if (el.hasAttribute('data-done-text')) el.textContent = el.getAttribute('data-done-text');
      if (el.hasAttribute('data-done-class')) el.className = el.getAttribute('data-done-class');
      if (el.hasAttribute('data-done-disable')) el.disabled = true;
    },
    'done-button': function (el) {
      var b = el.querySelector('button');
      if (!b) return;
      b.textContent = el.getAttribute('data-done-text') || b.textContent;
      b.disabled = true;
    },
    'check-prefix': function (el) {
      el.classList.add('text-emerald-300');
      el.textContent = '✓ ' + el.textContent;
    },
    'dim-closest': function (el, sel) {
      var t = el.closest(sel);
      if (t) t.style.opacity = '0.4';
      el.disabled = true;
    },
    'fade-closest': function (el, sel) { var t = el.closest(sel); if (t) t.classList.add('opacity-60'); },
    'hide-closest': function (el, sel) { var t = el.closest(sel); if (t) t.style.display = 'none'; },
    'close-closest': function (el, sel) { var t = el.closest(sel); if (t) t.open = false; },
    // Navigate to one of a fixed set of app pages (never a URL from markup).
    'goto': function (el, name) { if (GOTO.hasOwnProperty(name)) window.location.assign(GOTO[name]); },
    'case-rename': function (el) {
      var v = (el.querySelector('[name=name]') || {}).value;
      v = (v || '').trim();
      if (v) {
        var t = document.getElementById('case-name-text');
        if (t) t.textContent = v;
        document.title = v + ' \u2014 Nexus';
      }
      var d = document.getElementById('case-rename-details');
      if (d) d.open = false;
    },
    'case-describe': function (el) {
      var v = ((el.querySelector('[name=description]') || {}).value || '').trim();
      var p = document.getElementById('case-desc-text');
      if (p) {
        p.textContent = v || 'No description \u2014 click \u270e to add one.';
        p.className = 'text-sm ' + (v ? 'text-slate-400' : 'text-slate-600 italic');
      }
      var d = document.getElementById('case-desc-details');
      if (d) d.open = false;
    },
    'brief-refresh': briefRefresh,
    'brief-reload': function () {
      briefRefresh();
      if (window.htmx) {
        window.htmx.ajax('GET', '/brief', { target: '#brief-body', select: '#brief-body', swap: 'outerHTML' });
      }
    }
  };

  function run(el, spec, evt) {
    parse(spec).forEach(function (pair) {
      var fn = ACTIONS[pair[0]];
      if (fn) fn(el, pair[1], evt);
    });
  }

  document.addEventListener('click', function (e) {
    var el = e.target && e.target.closest && e.target.closest('[data-action]');
    if (el) run(el, el.getAttribute('data-action'), e);
  });
  document.addEventListener('change', function (e) {
    var el = e.target && e.target.closest && e.target.closest('[data-change]');
    if (el) run(el, el.getAttribute('data-change'), e);
  });
  // "toggle" does not bubble: listen in the capture phase.
  document.addEventListener('toggle', function (e) {
    var el = e.target;
    if (el && el.getAttribute && el.getAttribute('data-toggled')) run(el, el.getAttribute('data-toggled'), e);
  }, true);
  // Same semantics as hx-on::after-request: the handler on an element runs
  // for its own requests and those of its descendants. Note: when a request's
  // swap removes the element itself, its htmx:afterRequest no longer reaches
  // this document listener, so data-done must not be relied on for an
  // element that replaces itself (none does today).
  document.addEventListener('htmx:afterRequest', function (e) {
    var el = e.target;
    while (el && el.closest) {
      el = el.closest('[data-done]');
      if (!el) break;
      run(el, el.getAttribute('data-done'), e);
      el = el.parentElement;
    }
  });
})();
