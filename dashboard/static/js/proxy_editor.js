/* Proxy connections: the editor dialog's behaviour.

   The dialog is one form for both jobs: "Add" posts to
   `/admin/proxy/connection`, "Edit" to `/admin/proxy/connection/<id>`, and only
   the action, the title and the values differ — one set of fields, one
   validation path, one place a mistake can appear. The fields themselves (and
   the host/port pair the address became) are `proxy_form.js`; this module is
   what happens on Save, on Quick test and on Escape.

   A refusal — `OnlyOneWireGuard`, a bad host, a port out of range — arrives as
   `{ok: false, error, code}` and lands in the dialog's own alert with everything
   typed still in place. The password never comes back, and switching the
   protocol hides the proxy inputs and shows the tunnel upload. */

import {
  MODAL,
  announce,
  patchRow,
  postBody,
  refreshTable,
  storePayload,
} from './proxy_rows.js';
import {
  addressChanged,
  fillForm,
  formBody,
  payloadFor,
  setError,
  syncFields,
} from './proxy_form.js';
import {
  checkServices,
  initProxyPicker,
  syncPickerVisibility,
} from './proxy_picker.js';

/** Show the editor: a row's stored values, or an empty form for a new one. */
export function openEditor(id) {
  const dialog = document.getElementById(MODAL);
  if (!dialog) return;
  const connection = id ? payloadFor(id) : null;
  fillForm(connection);
  checkServices(connection ? connection.services : []);
  syncPickerVisibility();
  syncFields();
  dialog.hidden = false;
  const name = document.getElementById('proxy-name');
  if (name) name.focus();
}

function closeEditor() {
  const dialog = document.getElementById(MODAL);
  if (dialog) dialog.hidden = true;
}

/**
 * Post the dialog's form and apply the answer; true when the row was stored.
 *
 * Shared by Save and by a Quick test that had to persist a new address first,
 * so both go through one validation path and one refusal surface.
 */
async function saveEditor() {
  const form = document.getElementById('proxy-form');
  if (!form) return false;
  setError('');

  try {
    const res = await fetch(form.action, formBody(form));
    const data = await res.json();
    if (!res.ok || !data.ok) {
      /* The dialog stays open with every field as it was: a refusal about one
         field must not throw away the rest of the form. */
      setError(data.error || 'Could not save the connection.');
      return false;
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
    return true;
  } catch (error) {
    console.error('proxy save failed', error);
    setError('The connection could not be saved. Check the console for the reason.');
    return false;
  }
}

async function submitEditor(event) {
  event.preventDefault();
  const save = document.getElementById('proxy-save');
  if (save) save.disabled = true;
  try {
    await saveEditor();
  } finally {
    if (save) save.disabled = false;
  }
}

/**
 * Quick test: probe the stored connection, saving the address first if it moved.
 *
 * Without the save the button would dial the *old* host and port and report a
 * verdict about a proxy the operator no longer has. A refused save keeps the
 * dialog open with its own message, and no probe runs.
 */
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
    if (addressChanged()) {
      const saved = await saveEditor();
      if (!saved) {
        result.hidden = true;
        return;
      }
    }
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
    /* A save above closes the dialog, so the same answer also goes to the
       page's live region: the verdict must not be invisible because the panel
       it was asked from has gone. */
    announce(`${data.row.name}: ${result.textContent}`);
  } catch (error) {
    console.error('proxy quick test failed', error);
    result.textContent = 'The check could not run. See the console.';
  } finally {
    button.disabled = false;
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
