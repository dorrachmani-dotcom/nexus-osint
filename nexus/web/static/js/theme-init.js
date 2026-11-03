// Moved from templates/base.html (no inline scripts: see the CSP in nexus/web/security.py).
// Apply the saved colour theme before first paint so there is no flash of
// the wrong theme. Default is dark; "light" is opt-in and remembered.
try { if (localStorage.getItem('nexus-theme') === 'light') document.documentElement.classList.add('light'); } catch (e) {}
