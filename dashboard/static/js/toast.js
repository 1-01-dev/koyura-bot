/* Toasts: one container, one close affordance, no inline styles.

   The container already exists in base.html (#toast-container, aria-live
   polite), so a toast is just a node appended to it. Leaving is a CSS class
   the stylesheet animates; the node is removed once that transition ends. */

const AUTO_DISMISS_MS = 5000;
const LEAVE_MS = 200;

let host = null;

function container() {
  if (!host || !host.isConnected) host = document.getElementById('toast-container');
  return host;
}

function dismiss(toast) {
  if (!toast || !toast.isConnected || toast.classList.contains('is-leaving')) return;
  toast.classList.add('is-leaving');
  setTimeout(() => toast.remove(), LEAVE_MS);
}

function build(message, kind) {
  const toast = document.createElement('div');
  toast.className = `toast toast-${kind}`;

  const text = document.createElement('span');
  text.textContent = String(message);

  const close = document.createElement('button');
  close.type = 'button';
  close.className = 'toast-close';
  close.dataset.toastClose = '';
  close.setAttribute('aria-label', 'Dismiss');
  close.textContent = '\u00d7';

  toast.append(text, close);
  return toast;
}

/** Show a transient message. `kind` is info | success | warning | danger. */
export function showToast(message, kind = 'info') {
  const parent = container();
  if (!parent) return;

  const toast = build(message, kind);
  parent.appendChild(toast);

  setTimeout(() => dismiss(toast), AUTO_DISMISS_MS);
}

/* One delegated listener: toasts created later need no extra wiring. */
document.addEventListener('click', (event) => {
  const close = event.target.closest('[data-toast-close]');
  if (close) dismiss(close.closest('.toast'));
});
