// Moved from templates/base.html (no inline scripts: see the CSP in nexus/web/security.py).
var _drawerReturnFocus = null;
function openItemDrawer() {
  _drawerReturnFocus = document.activeElement;
  var d = document.getElementById('item-drawer');
  d.classList.remove('translate-x-full');
  document.getElementById('item-drawer-overlay').classList.remove('hidden');
  d.focus();  // move focus into the dialog for keyboard/screen-reader users
}
function closeItemDrawer() {
  document.getElementById('item-drawer').classList.add('translate-x-full');
  document.getElementById('item-drawer-overlay').classList.add('hidden');
  // Return focus to whatever opened the drawer (e.g. the Details button).
  if (_drawerReturnFocus && _drawerReturnFocus.focus) {
    try { _drawerReturnFocus.focus(); } catch (e) {}
  }
  _drawerReturnFocus = null;
}
document.addEventListener('keydown', function (e) {
  if (e.key === 'Escape') closeItemDrawer();
});
// Open drawer automatically after htmx loads content into it.
document.body.addEventListener('htmx:afterSwap', function (e) {
  if (e.target && e.target.id === 'item-drawer-content') openItemDrawer();
});
