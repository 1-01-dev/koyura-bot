/* Help bubbles: the "?" that hides a long explanation until it is asked for.

   The markup is the contract — a `.help-toggle` button and the `.help-panel`
   whose id it names — and this module only owns the open/closed state:

   - the panel starts with the `hidden` attribute, so with JavaScript off (or
     before this module runs) it is closed and the page does not shift;
   - click toggles it, and a real `<button>` already answers Enter and Space,
     so there is no key handler that could fire the toggle twice;
   - Escape closes the open bubble and puts the focus back on its button, so
     the focus never stays inside something hidden — one listener for the page,
     which covers the button and a link inside the panel alike;
   - opening one closes the others *in the same group* (`data-help-group`), so
     three hints inside one card cannot pile up on a phone.

   `aria-expanded` is the only state a screen reader needs (the panel is named
   by `aria-controls`), and nothing is announced twice.

   No inline handler and no markup from strings — the panel's CSP allows no
   'unsafe-inline' scripts. */

const TOGGLE = '.help-toggle';
const OPEN = 'true';
const CLOSED = 'false';

/** The panel a toggle owns, looked up by the id in `aria-controls`. */
function panelFor(toggle) {
  const id = toggle.getAttribute('aria-controls');
  if (!id) return null;
  return document.getElementById(id);
}

function setOpen(toggle, open) {
  const panel = panelFor(toggle);
  toggle.setAttribute('aria-expanded', open ? OPEN : CLOSED);
  if (panel) panel.hidden = !open;
}

function isOpen(toggle) {
  return toggle.getAttribute('aria-expanded') === OPEN;
}

/** Close every open bubble in the same group, except `keep`. */
function closeSiblings(toggle, keep) {
  const group = toggle.closest('[data-help-group]') || document;
  for (const other of group.querySelectorAll(TOGGLE)) {
    if (other !== keep && isOpen(other)) setOpen(other, false);
  }
}

function bind(toggle) {
  if (toggle.dataset.helpWired === 'true') return;
  toggle.dataset.helpWired = 'true';
  // A toggle whose panel is missing would be a dead button; leave the label
  // alone and let the server-rendered markup be fixed.
  if (!panelFor(toggle)) return;

  toggle.addEventListener('click', () => {
    const open = !isOpen(toggle);
    if (open) closeSiblings(toggle, toggle);
    setOpen(toggle, open);
  });
}

export function initHelpBubbles(root = document) {
  for (const toggle of root.querySelectorAll(TOGGLE)) bind(toggle);
  // A page root that is one section still has to wire the bubbles outside it:
  // the tabs and the header strip are rendered by the layout, not the panel.
  if (root === document) return;
  for (const toggle of document.querySelectorAll(TOGGLE)) bind(toggle);
}

/** Escape anywhere on the page closes the open bubble and restores the focus. */
function onPageKeydown(event) {
  if (event.key !== 'Escape') return;
  const open = document.querySelector(`${TOGGLE}[aria-expanded="${OPEN}"]`);
  if (!open) return;
  event.preventDefault();
  setOpen(open, false);
  open.focus();
}

if (typeof document !== 'undefined') {
  document.addEventListener('keydown', onPageKeydown);
}
