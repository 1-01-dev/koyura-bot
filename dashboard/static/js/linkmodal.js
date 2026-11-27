/* Full-URL dialog for /history and /errors.

   A query string with `?si=…&list=…` is wider than the whole table, so every
   row shows one compact "Link" button and the URL itself rides in a data-*
   attribute. The dialog is one node in base.html; this module only fills it,
   so a page that keeps a hundred rows still owns a single overlay.

   Opening is delegated from the document: rows are rendered server-side and
   the table is long, so one listener beats one per button. Closing returns
   focus to the button that opened the dialog — the row is exactly where the
   operator left it. */

const OPEN_SELECTOR = '[data-link-url]';
const FOCUSABLE = 'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])';

/* The clipboard API needs a secure context; the panel also runs on plain
   http behind a loopback proxy, so a textarea + execCommand keeps the button
   honest there. When both refuse, the URL is selected for a manual copy. */
function legacyCopy(text, target) {
  const buffer = document.createElement('textarea');
  buffer.className = 'copy-buffer';
  buffer.value = text;
  buffer.setAttribute('readonly', '');
  document.body.appendChild(buffer);
  buffer.select();

  let copied = false;
  try {
    copied = document.execCommand('copy');
  } catch {
    copied = false;
  }
  buffer.remove();
  if (copied) return true;

  if (!target) return false;
  const range = document.createRange();
  range.selectNodeContents(target);
  const selection = window.getSelection();
  if (!selection) return false;
  selection.removeAllRanges();
  selection.addRange(range);
  return false;
}

async function copyText(text, target) {
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      /* The write can stall on a permission prompt that never appears (a
         headless or embedded browser does this); a short race keeps the
         button from waiting forever, and the fallback below still copies. */
      const written = await Promise.race([
        navigator.clipboard.writeText(text).then(() => true, () => false),
        new Promise((resolve) => { setTimeout(() => resolve(false), 1200); }),
      ]);
      if (written) return true;
    }
  } catch {
    /* fall through to the selection path */
  }
  return legacyCopy(text, target);
}

/** Wire the dialog. Returns silently on pages that do not render it. */
export function initLinkModal() {
  const modal = document.getElementById('link-modal');
  if (!modal) return;

  const urlText = modal.querySelector('[data-link-text]');
  const status = modal.querySelector('[data-link-status]');
  const copyBtn = modal.querySelector('[data-link-copy]');
  const closeBtn = modal.querySelector('[data-link-close]');
  let opener = null;

  const url = () => (urlText ? urlText.textContent || '' : '');

  function setStatus(message) {
    if (status) status.textContent = message;
  }

  function open(trigger) {
    opener = trigger;
    if (urlText) urlText.textContent = trigger.dataset.linkUrl || '';
    setStatus('');
    modal.hidden = false;
    /* Focus lands on Close: it is the safe default and Enter then dismisses. */
    (closeBtn || modal).focus();
  }

  function close() {
    if (modal.hidden) return;
    modal.hidden = true;
    setStatus('');
    const back = opener;
    opener = null;
    if (back && typeof back.focus === 'function') back.focus();
  }

  async function copy() {
    const text = url();
    if (!text) return;
    const ok = await copyText(text, urlText);
    setStatus(ok ? 'Copied to clipboard' : 'Selected \u2014 press Ctrl/Cmd+C');
    if (copyBtn) copyBtn.textContent = ok ? 'Copied' : 'Copy';
  }

  document.addEventListener('click', (event) => {
    const target = event.target;
    if (!(target instanceof Element)) return;

    const trigger = target.closest(OPEN_SELECTOR);
    if (trigger) {
      event.preventDefault();
      open(trigger);
      return;
    }
    /* A click on the scrim — and only on the scrim — dismisses. */
    if (!modal.hidden && target === modal) close();
  });

  if (closeBtn) closeBtn.addEventListener('click', close);
  if (copyBtn) copyBtn.addEventListener('click', copy);

  document.addEventListener('keydown', (event) => {
    if (modal.hidden) return;
    if (event.key === 'Escape') {
      event.preventDefault();
      close();
    }
  });

  /* Keep Tab inside the dialog while it is open: behind it the page is inert
     for the reader but still focusable by the browser. */
  modal.addEventListener('keydown', (event) => {
    if (event.key !== 'Tab') return;
    const items = Array.from(modal.querySelectorAll(FOCUSABLE));
    if (items.length === 0) return;

    const first = items[0];
    const last = items[items.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  });
}
