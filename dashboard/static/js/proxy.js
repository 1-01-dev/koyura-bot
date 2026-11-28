/* Proxy connections: the table, the "⋯" menu and the row actions.

   The markup is the contract. Every row carries real forms for "Test" and
   "Enable/Disable" (`_proxy_row.html`), and the page renders the table on the
   server; this module only upgrades those actions to `fetch` so the row is
   patched in place instead of the page reloading.

   Two rules shape it:

   - **the form is the floor.** If a `fetch` fails, is refused, or this module
     never loads, the plain form POST does the same thing and redirects. Nothing
     here is the only way to test, toggle or save a connection.
   - **no markup from strings.** A row is patched field by field
     (`proxy_rows.js`); a row that does not exist yet is fetched from the
     server, which renders it through the same template as the first render.

   The editor and the delete confirmation live in `proxy_editor.js`. */

import {
  DELETE_MODAL,
  announce,
  closeMenu,
  field,
  patchRow,
  postBody,
  refreshCount,
  rowById,
  toggleMenu,
} from './proxy_rows.js';
import { initProxyEditor, openDelete, openEditor } from './proxy_editor.js';
import { showToast } from './toast.js';

/** What the live region says after one row action. */
function resultLine(action, row) {
  if (action !== 'test') {
    return `${row.name}: ${row.enabled ? 'enabled' : 'disabled'}`;
  }
  if (row.status === 'ok') {
    return `${row.name}: external IP ${row.last_test_result}`;
  }
  return `${row.name}: check failed — ${row.last_test_result}`;
}

/** Test, or flip enabled: both are a form POST that answers with the new row. */
async function runRowAction(button) {
  const row = button.closest('tr[data-connection-id]');
  const form = button.form;
  if (!row || !form) return;
  const action = button.dataset.proxyAction;

  if (action === 'test') {
    button.setAttribute('aria-busy', 'true');
    button.disabled = true;
    announce(`Testing ${(field(row, 'name') || {}).textContent || 'connection'}…`);
  }

  try {
    const res = await fetch(form.action, postBody(new FormData(form)));
    const data = await res.json();
    if (!res.ok || !data.ok) throw new Error(data.error || `HTTP ${res.status}`);
    if (!data.row) return;
    patchRow(data.row);
    announce(resultLine(action, data.row));
  } catch (error) {
    /* One fallback for both cases: the form does exactly this action, it just
       answers with a redirect instead of a row. */
    console.error('proxy row action failed', error);
    form.submit();
  } finally {
    if (action === 'test') {
      button.disabled = false;
      button.setAttribute('aria-busy', 'false');
    }
  }
}

/** Delete: the confirmation's own form, so the word is checked server-side too. */
async function submitDelete(event) {
  event.preventDefault();
  const form = document.getElementById('proxy-delete-form');
  const dialog = document.getElementById(DELETE_MODAL);
  if (!form) return;

  try {
    const res = await fetch(form.action, postBody(new FormData(form)));
    const data = await res.json();
    if (!res.ok || !data.ok) {
      showToast(data.error || 'Could not delete the connection.', 'danger');
      return;
    }
    if (dialog) dialog.hidden = true;
    const row = rowById(data.id);
    if (row) row.remove();
    refreshCount();
    announce('Connection deleted.');
  } catch (error) {
    console.error('proxy delete failed', error);
    form.submit();
  }
}

function onRowClick(event) {
  const button = event.target.closest('[data-proxy-action]');
  if (!button) return;
  const action = button.dataset.proxyAction;
  if (action === 'edit') {
    event.preventDefault();
    openEditor(button.dataset.connection);
    return;
  }
  if (action === 'delete') {
    event.preventDefault();
    openDelete(button);
    return;
  }
  if (action === 'test' || action === 'toggle') {
    event.preventDefault();
    runRowAction(button);
  }
}

function onKeydown(event) {
  if (event.key !== 'Escape') return;
  const deleteDialog = document.getElementById(DELETE_MODAL);
  if (deleteDialog && !deleteDialog.hidden) {
    event.preventDefault();
    deleteDialog.hidden = true;
    return;
  }
  if (document.getElementById('proxy-menu')?.hidden === false) {
    event.preventDefault();
    closeMenu();
  }
}

function onDocumentClick(event) {
  const menu = document.getElementById('proxy-menu');
  const wrap = document.getElementById('proxy-menu-wrap');
  if (menu && !menu.hidden && wrap && !wrap.contains(event.target)) closeMenu();
}

export function initProxyPage() {
  if (!document.getElementById('proxy-card')) return;

  document.addEventListener('click', onDocumentClick);
  document.addEventListener('keydown', onKeydown);

  const menuToggle = document.getElementById('proxy-menu-toggle');
  if (menuToggle) menuToggle.addEventListener('click', toggleMenu);

  const body = document.getElementById('proxy-rows');
  if (body) body.addEventListener('click', onRowClick);

  const deleteForm = document.getElementById('proxy-delete-form');
  if (deleteForm) deleteForm.addEventListener('submit', submitDelete);

  // The "Add connection" buttons are the editor's, but they belong to this card.
  ['proxy-add', 'proxy-empty-add'].forEach((id) => {
    const button = document.getElementById(id);
    if (button) {
      button.addEventListener('click', () => openEditor(null));
    }
  });

  // The interval form is a plain POST: its CSRF token is rendered with it, and
  // the background loop reads the stored value on every sweep, so reloading the
  // page is exactly the feedback it needs. Nothing to wire.

  initProxyEditor();
  refreshCount();
}
