/* Proxy connections: the editor dialog and the delete confirmation.

   One form does both jobs: "Add" posts to `/admin/proxy/connection`, "Edit" to
   `/admin/proxy/connection/<id>`, and the only differences are the action, the
   title and the values filled in — one set of fields, one validation path, one
   place a mistake can appear.

   The secret never comes back: the payload carries `has_password`, the field is
   always empty, and a blank field keeps the stored credential (the server's
   rule). A refusal — `OnlyOneWireGuard`, a bad URL — arrives as
   `{ok: false, error, code}` and lands in the dialog's own alert with everything
   typed still in place. Switching the protocol hides the proxy inputs and shows
   the tunnel upload; the form itself never changes. */

import {
  MODAL,
  announce,
  patchRow,
  postBody,
  refreshTable,
  rowById,
  storePayload,
} from './proxy_rows.js';
import {
  checkServices,
  initProxyPicker,
  syncPickerVisibility,
} from './proxy_picker.js';

/** The stored payload of one row, from its `data-payload` attribute. */
function payloadFor(id) {
  const row = rowById(id);
  if (!row) return null;
  try {
    return JSON.parse(row.dataset.payload || '{}');
  } catch (error) {
    console.error('proxy row payload is not JSON', error);
    return null;
  }
}

function setError(text) {
  const box = document.getElementById('proxy-modal-error');
  if (!box) return;
  box.textContent = text || '';
  box.hidden = !text;
}

/** One field's value, by id — the dialog has a dozen and none of them is worth
 *  three lines each. */
function setValue(id, value) {
  const input = document.getElementById(id);
  if (input) input.value = value;
}

/** Proxy fields or the tunnel's, driven by the protocol select. */
function syncFields() {
  const kind = document.getElementById('proxy-kind');
  const wireguard = Boolean(kind && kind.value === 'wireguard');
  const proxyFields = document.getElementById('proxy-fields');
  const wgFields = document.getElementById('proxy-wg-fields');
  if (proxyFields) proxyFields.hidden = wireguard;
  if (wgFields) wgFields.hidden = !wireguard;
  /* The URL is required for a proxy and refused for a tunnel: the server's rule,
     mirrored here so the browser stops the submit before a round trip. */
  const url = document.getElementById('proxy-url');
  if (url) url.required = !wireguard;
  const wgFile = document.getElementById('proxy-wg-file');
  if (wgFile) wgFile.disabled = !wireguard;
  syncPickerVisibility();
}

/** Fill the dialog from a payload; `null` means "Add". */
function fill(connection) {
  const form = document.getElementById('proxy-form');
  if (!form) return;
  form.reset();
  setError('');

  const editing = Boolean(connection && connection.id);
  const title = document.getElementById('proxy-modal-title');
  const idField = document.getElementById('proxy-connection-id');
  const result = document.getElementById('proxy-test-result');
  if (title) title.textContent = editing ? 'Edit connection' : 'Add connection';
  if (idField) idField.value = editing ? String(connection.id) : '';
  if (result) {
    result.textContent = '';
    result.hidden = true;
  }
  form.action = editing
    ? `/admin/proxy/connection/${connection.id}`
    : '/admin/proxy/connection';

  setValue('proxy-name', editing ? connection.name : '');
  setValue('proxy-kind', editing ? connection.kind : 'socks5h');
  setValue('proxy-url', editing ? connection.url || '' : '');
  setValue('proxy-assignment', editing ? connection.mode : 'all');
  // Neither credential field is ever rendered back into the page, so both start
  // empty. Blank means "keep the stored secret"; the hint under the field says so.
  setValue('proxy-username', '');
  setValue('proxy-password', '');

  const state = document.getElementById('proxy-password-state');
  if (state) {
    state.textContent =
      editing && connection.has_password
        ? 'set — blank keeps it, type to replace'
        : 'never shown back';
  }

  checkServices(editing ? connection.services : []);
  syncFields();
}

export function openEditor(id) {
  const dialog = document.getElementById(MODAL);
  if (!dialog) return;
  fill(id ? payloadFor(id) : null);
  dialog.hidden = false;
  const name = document.getElementById('proxy-name');
  if (name) name.focus();
}

function closeEditor() {
  const dialog = document.getElementById(MODAL);
  if (dialog) dialog.hidden = true;
}

/** Quick test: probe the connection the dialog is editing, not the typed fields. */
async function quickTest() {
  const button = document.getElementById('proxy-quick-test');
  const result = document.getElementById('proxy-test-result');
  const idField = document.getElementById('proxy-connection-id');
  if (!button || !result) return;
  const id = idField ? idField.value : '';

  result.hidden = false;
  if (!id) {
    /* Nothing is stored, so there is nothing for the server to dial: the honest
       answer is "save it first", not a probe of a URL that does not exist yet. */
    result.textContent = 'Save the connection first, then test it.';
    return;
  }

  button.disabled = true;
  result.textContent = 'Testing…';
  try {
    const res = await fetch(`/admin/proxy/connection/${id}/test`, postBody(new FormData()));
    const data = await res.json();
    if (!res.ok || !data.ok) {
      result.textContent = data.error || 'The check failed.';
      return;
    }
    patchRow(data.row);
    storePayload(data.payload);
    result.textContent =
      data.row.status === 'ok'
        ? `External IP: ${data.row.last_test_result}`
        : `Check failed: ${data.row.last_test_result}`;
  } catch (error) {
    console.error('proxy quick test failed', error);
    result.textContent = 'The check could not run. See the console.';
  } finally {
    button.disabled = false;
  }
}

async function submitEditor(event) {
  event.preventDefault();
  const form = document.getElementById('proxy-form');
  if (!form) return;
  const save = document.getElementById('proxy-save');
  if (save) save.disabled = true;
  setError('');

  try {
    const res = await fetch(form.action, postBody(new FormData(form)));
    const data = await res.json();
    if (!res.ok || !data.ok) {
      /* The dialog stays open with every field as it was: a refusal about one
         field must not throw away the rest of the form. */
      setError(data.error || 'Could not save the connection.');
      return;
    }
    closeEditor();
    announce(`Saved ${data.row.name}.`);
    /* Editing patches the row in place. A new row's id is only known after the
       insert (every form action in it names that id), so the body is re-fetched
       and re-rendered by the server rather than assembled here. */
    if (patchRow(data.row) === null) {
      await refreshTable();
    } else {
      storePayload(data.payload);
    }
  } catch (error) {
    console.error('proxy save failed', error);
    form.submit();
  } finally {
    if (save) save.disabled = false;
  }
}

function bind(id, event, handler) {
  const element = document.getElementById(id);
  if (element) element.addEventListener(event, handler);
}

export function initProxyEditor() {
  if (!document.getElementById(MODAL)) return;

  bind('proxy-form', 'submit', submitEditor);
  bind('proxy-modal-close', 'click', closeEditor);
  bind('proxy-cancel', 'click', closeEditor);
  bind('proxy-kind', 'change', syncFields);
  bind('proxy-quick-test', 'click', quickTest);
  /* An overlay click closes the dialog it is the backdrop of — the same
     behaviour the other dialogs on the panel have. */
  bind(MODAL, 'click', (event) => {
    if (event.target === event.currentTarget) closeEditor();
  });

  /* Escape closes whichever dialog is open. Bound here (not in proxy.js) because
     this module owns both of them, and the listener runs before the table's. */
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape') return;
    const dialog = document.getElementById(MODAL);
    if (!dialog || dialog.hidden) return;
    event.preventDefault();
    event.stopPropagation();
    closeEditor();
  });

  initProxyPicker();
}
