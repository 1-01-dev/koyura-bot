/* Proxy connections: the editor's fields.

   One form does both jobs — "Add" and "Edit" — so every value the dialog shows
   is written here, from the payload the server rendered, and every "has this
   changed?" question is answered here too. The dialog's behaviour (what Save
   and Quick test do) lives in `proxy_editor.js`; this module only knows which
   input holds which fact.

   Two rules shape it:

   - **the address is two fields.** The protocol select decides the scheme, so
     the host and the port are separate inputs and the server assembles the
     canonical URL. Filling the dialog means splitting the stored URL, which
     `connection_json` has already done.
   - **the password never comes back.** `has_password` is a bool; that field is
     always written empty and the hint says a blank one keeps the stored secret.
     The username is a name, not a secret, so it is filled in. */

import { postBody, rowById } from './proxy_rows.js';

/** The stored payload of one row, from its `data-payload` attribute. */
export function payloadFor(id) {
  const row = rowById(id);
  if (!row) return null;
  try {
    return JSON.parse(row.dataset.payload || '{}');
  } catch (error) {
    console.error('proxy row payload is not JSON', error);
    return null;
  }
}

/** The dialog's own alert: a refusal about one field, everything else intact. */
export function setError(text) {
  const box = document.getElementById('proxy-modal-error');
  if (!box) return;
  box.textContent = text || '';
  box.hidden = !text;
}

/** One field's value, by id — the dialog has a dozen and none is worth three
 *  lines each. */
function setValue(id, value) {
  const input = document.getElementById(id);
  if (input) input.value = value;
}

/** Proxy fields or the tunnel's, driven by the protocol select. */
export function syncFields() {
  const kind = document.getElementById('proxy-kind');
  const wireguard = Boolean(kind && kind.value === 'wireguard');
  const proxyFields = document.getElementById('proxy-fields');
  const wgFields = document.getElementById('proxy-wg-fields');
  if (proxyFields) proxyFields.hidden = wireguard;
  if (wgFields) wgFields.hidden = !wireguard;
  /* The address is required for a proxy and refused for a tunnel: the server's
     rule, mirrored here so the browser stops the submit before a round trip. */
  const host = document.getElementById('proxy-host');
  if (host) host.required = !wireguard;
  const port = document.getElementById('proxy-port');
  if (port) port.required = !wireguard;
  const wgFile = document.getElementById('proxy-wg-file');
  if (wgFile) wgFile.disabled = !wireguard;
}

/*: The payload the dialog was opened with, or `null` for "Add". Kept so a Quick
  test can tell whether the address fields still describe the stored row. */
let opened = null;

/** Whether the address fields no longer describe the stored row.
 *
 *  Compared field by field rather than by rebuilding the URL: the scheme is the
 *  protocol select's, and a row whose port is blank reads as unchanged when both
 *  sides are blank. */
export function addressChanged() {
  if (!opened) return false;
  const value = (id) => {
    const element = document.getElementById(id);
    return element ? element.value.trim() : '';
  };
  return (
    value('proxy-host') !== String(opened.host || '').trim() ||
    value('proxy-port') !== String(opened.port || '').trim()
  );
}

/** The dialog's title, its hidden id, its stale result line and its action. */
function resetDialog(editing, connection) {
  const title = document.getElementById('proxy-modal-title');
  const idField = document.getElementById('proxy-connection-id');
  const result = document.getElementById('proxy-test-result');
  const form = document.getElementById('proxy-form');
  if (title) title.textContent = editing ? 'Edit connection' : 'Add connection';
  if (idField) idField.value = editing ? String(connection.id) : '';
  if (result) {
    result.textContent = '';
    result.hidden = true;
  }
  if (form) {
    form.action = editing
      ? `/admin/proxy/connection/${connection.id}`
      : '/admin/proxy/connection';
  }
}

/** Fill the dialog from a payload; `null` means "Add". */
export function fillForm(connection) {
  const form = document.getElementById('proxy-form');
  if (!form) return;
  form.reset();
  setError('');

  const editing = Boolean(connection && connection.id);
  opened = editing ? connection : null;
  resetDialog(editing, connection);

  setValue('proxy-name', editing ? connection.name : '');
  setValue('proxy-kind', editing ? connection.kind : 'socks5h');
  setValue('proxy-host', editing ? connection.host || '' : '');
  setValue('proxy-port', editing ? connection.port || '' : '');
  setValue('proxy-assignment', editing ? connection.mode : 'all');
  setValue('proxy-username', editing ? connection.username || '' : '');
  setValue('proxy-password', '');

  const state = document.getElementById('proxy-password-state');
  if (state) {
    state.textContent =
      editing && connection.has_password
        ? 'set — blank keeps it, type to replace'
        : 'never shown back';
  }
}

/** The form as a fetch POST, carrying the dashboard's CSRF header. */
export function formBody(form) {
  return postBody(new FormData(form));
}
