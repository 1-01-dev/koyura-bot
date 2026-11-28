/* Proxy connections: the editor dialog and the delete confirmation.

   One form does both jobs. "Add" posts to `/admin/proxy/connection`, "Edit" posts
   to `/admin/proxy/connection/<id>`, and the only differences are the action,
   the title and the values filled in — so there is one set of fields, one
   validation path and one place a mistake can appear.

   Three things this module is careful about:

   - **the secret never comes back.** The row's payload carries
     `has_password: true|false`, and the password field is always rendered empty.
     "blank keeps it" is the server's rule; this module only says so.
   - **a refusal keeps the dialog open.** `OnlyOneWireGuard` (a second tunnel) or
     a bad URL arrives as `{ok: false, error, code}`; the message goes into the
     dialog's own alert and nothing typed is lost.
   - **the protocol changes the fields, not the form.** WireGuard hides the proxy
     inputs and shows the config upload; switching back restores them. No second
     form, no markup built here. */

import {
  announce,
  patchRow,
  postBody,
  refreshTable,
  rowById,
} from './proxy_rows.js';
import {
  checkServices,
  initProxyPicker,
  syncPickerVisibility,
} from './proxy_picker.js';

const MODAL = 'proxy-modal';

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

  const set = (id, value) => {
    const input = document.getElementById(id);
    if (input) input.value = value;
  };
  set('proxy-name', editing ? connection.name : '');
  set('proxy-kind', editing ? connection.kind : 'socks5h');
  set('proxy-url', editing ? connection.url || '' : '');
  // The username is not a secret and is not rendered on the page, so an edit
  // starts empty; leaving it empty with the URL intact keeps the stored login
  // only when a password is also blank — which the hint under the field says.
  set('proxy-username', '');
  set('proxy-password', '');
  set('proxy-assignment', editing ? connection.mode : 'all');

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

/** Quick test: probe the stored connection, not the fields being typed. */
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
    const body = new FormData();
    const res = await fetch(`/admin/proxy/connection/${id}/test`, postBody(body));
    const data = await res.json();
    if (!res.ok || !data.ok) {
      result.textContent = data.error || 'The check failed.';
      return;
    }
    patchRow(data.row);
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
    if (patchRow(data.row) === null) await refreshTable();
  } catch (error) {
    console.error('proxy save failed', error);
    form.submit();
  } finally {
    if (save) save.disabled = false;
  }
}

export function openDelete(button) {
  const dialog = document.getElementById('proxy-delete-modal');
  if (!dialog) return;
  const form = document.getElementById('proxy-delete-form');
  if (form) form.action = `/admin/proxy/connection/${button.dataset.connection}/delete`;
  const body = document.getElementById('proxy-delete-body');
  if (body) {
    body.textContent =
      `"${button.dataset.name}" and its stored secret are removed. ` +
      'The engine stops using it on the next job.';
  }
  const confirm = document.getElementById('proxy-delete-confirm');
  if (confirm) {
    confirm.value = '';
    confirm.focus();
  }
  dialog.hidden = false;
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
  bind('proxy-delete-cancel', 'click', () => {
    const dialog = document.getElementById('proxy-delete-modal');
    if (dialog) dialog.hidden = true;
  });

  /* An overlay click closes the dialog it is the backdrop of — the same
     behaviour the other dialogs on the panel have. */
  [MODAL, 'proxy-delete-modal'].forEach((id) => {
    bind(id, 'click', (event) => {
      if (event.target === event.currentTarget) event.currentTarget.hidden = true;
    });
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
