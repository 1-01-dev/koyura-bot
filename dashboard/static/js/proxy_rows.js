/* Proxy connections: the row view model.

   Everything that reads or writes *one table row*, plus the two helpers every
   other proxy module needs (the CSRF token and a POST that carries it). Kept
   apart from `proxy.js` because the editor needs `patchRow` after a save, and a
   page module that imports the page module would be a cycle.

   Nothing here builds markup: a row is patched field by field, and a table that
   needs a new row is re-fetched from the server, which renders it through
   `_proxy_row.html` — the same template that produced the first one. */

/*: The element ids the page and the editor both name. They are constants rather
  than string literals in three files, because a typo in one of them is a dialog
  that never opens and no error anywhere. */
export const MODAL = 'proxy-modal';
export const DELETE_MODAL = 'proxy-delete-modal';

/** The dashboard's CSRF token, from the meta tag every page carries. */
export function csrfToken() {
  const meta = document.querySelector('meta[name="csrf-token"]');
  return meta ? meta.getAttribute('content') || '' : '';
}

/** A form POST as a `fetch`, with the header the dashboard expects for it.
 *
 * The proxy routes read the token from *the submitted form* (`_proxy_request`),
 * not from the header — so a body that carries no `csrf_token` field (the Quick
 * test posts an empty one) is refused with a redirect even though the header is
 * right. Adding it here keeps every caller on one shape and leaves a caller
 * that already rendered its own hidden field alone.
 */
export function postBody(body) {
  const payload = body instanceof FormData ? body : new FormData();
  const token = csrfToken();
  if (token && !payload.has('csrf_token')) payload.set('csrf_token', token);
  return {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'X-Requested-With': 'fetch', 'X-CSRF-Token': token },
    body: payload,
  };
}

/** One line for the live region: a test result, a saved row, a refusal. */
export function announce(text) {
  const live = document.getElementById('proxy-live');
  if (live) live.textContent = text;
}

export function rowById(id) {
  return document.querySelector(`tr[data-connection-id="${CSS.escape(String(id))}"]`);
}

/** One cell of a row, by its `data-field` name. */
export function field(row, name) {
  return row ? row.querySelector(`[data-field="${name}"]`) : null;
}

/** The verb a row's toggle button carries: what the click will do next. */
export function toggleVerb(enabled) {
  return enabled ? 'Disable' : 'Enable';
}

/** The mode word in the page header, derived from the enabled rows. */
export function refreshMode() {
  const rows = Array.from(document.querySelectorAll('tr[data-connection-id]'));
  const live = rows.filter((row) => row.dataset.enabled === 'true');
  let mode = 'Off';
  if (live.length > 0) {
    const kinds = live.map((row) => (field(row, 'kind') || {}).textContent || '');
    const tunnel = kinds.some((kind) => kind.trim().toLowerCase() === 'wireguard');
    mode = tunnel ? 'WireGuard' : 'Manual';
  }
  const target = document.getElementById('proxy-mode');
  if (target) target.textContent = mode;
}

/** The footer count, the empty state and the header line, after any change. */
export function refreshCount() {
  const rows = document.querySelectorAll('tr[data-connection-id]');
  const count = document.getElementById('proxy-count');
  if (count) {
    count.textContent = `${rows.length} connection${rows.length === 1 ? '' : 's'}`;
  }
  const summary = document.getElementById('proxy-summary-count');
  if (summary) {
    summary.textContent = `${rows.length} connection${rows.length === 1 ? '' : 's'}`;
  }
  const empty = document.getElementById('proxy-empty');
  if (empty) empty.hidden = rows.length > 0;
  const line = document.getElementById('proxy-mode-line');
  if (line) line.hidden = rows.length === 0;
  refreshMode();
}

/**
 * Remember the editor's copy of a row, from a save or a toggle.
 *
 * The row's `data-payload` is what "Edit" fills the dialog from, so it has to
 * follow a write: a second edit without a reload must start from what was just
 * saved, not from what the page was rendered with.
 */
export function storePayload(payload) {
  if (!payload || payload.id === undefined) return;
  const row = rowById(payload.id);
  if (row) row.dataset.payload = JSON.stringify(payload);
}

/**
 * Apply one row payload from the server to the row already on the page.
 *
 * Everything written here is text or an attribute the row's own forms read: the
 * verdict, the status badge's class, the toggle's verb and its hidden `enabled`
 * field, and the aria-labels that name the row. The URL cell is left alone on
 * purpose — that string is the server's, and a patched row must not invent one.
 */
export function patchRow(payload) {
  const row = rowById(payload.id);
  if (!row) return null;

  row.dataset.enabled = payload.enabled ? 'true' : 'false';
  const set = (name, value) => {
    const cell = field(row, name);
    if (cell && value !== undefined) cell.textContent = value;
  };
  set('name', payload.name);
  set('kind', payload.kind_label);
  set('assignment', payload.assignment_label);
  set('last-test', payload.last_test_at);

  const status = field(row, 'status');
  if (status) {
    status.textContent = payload.status;
    status.className = payload.status_badge;
  }
  const result = field(row, 'result');
  if (result) {
    result.textContent = payload.last_test_result || '';
    result.title = payload.last_test_result || '';
  }

  const verb = toggleVerb(payload.enabled);
  const toggle = row.querySelector('[data-proxy-action="toggle"]');
  if (toggle) {
    toggle.textContent = verb;
    toggle.setAttribute('aria-label', `${verb} ${payload.name}`);
    const hidden = toggle.form && toggle.form.querySelector('[name="enabled"]');
    if (hidden) hidden.value = payload.enabled ? '0' : '1';
  }
  const edit = row.querySelector('[data-proxy-action="edit"]');
  if (edit) {
    edit.setAttribute('aria-label', `Edit ${payload.name}`);
    edit.dataset.connection = String(payload.id);
  }
  const remove = row.querySelector('[data-proxy-action="delete"]');
  if (remove) {
    remove.setAttribute('aria-label', `Delete ${payload.name}`);
    remove.dataset.connection = String(payload.id);
    remove.dataset.name = payload.name;
  }
  const test = row.querySelector('[data-proxy-action="test"]');
  if (test) test.setAttribute('aria-label', `Test ${payload.name} now`);
  refreshCount();
  return row;
}

/** Replace the table body with a freshly rendered one from the server. */
export function renderRows(body) {
  const current = document.getElementById('proxy-rows');
  if (!current || !body) return;
  current.replaceChildren(...body.children);
  refreshCount();
}

/** Re-fetch the page and take only its table body: one round trip, no stale row. */
export async function refreshTable() {
  try {
    const res = await fetch('/admin/proxy', { credentials: 'same-origin' });
    if (!res.ok) return;
    const html = new DOMParser().parseFromString(await res.text(), 'text/html');
    renderRows(html.getElementById('proxy-rows'));
  } catch (error) {
    console.error('proxy table refresh failed', error);
  }
}

/* ------------------------------------------------------------ the menu */

export function closeMenu() {
  const menu = document.getElementById('proxy-menu');
  const button = document.getElementById('proxy-menu-toggle');
  if (menu) menu.hidden = true;
  if (button) button.setAttribute('aria-expanded', 'false');
}

export function toggleMenu() {
  const menu = document.getElementById('proxy-menu');
  const button = document.getElementById('proxy-menu-toggle');
  if (!menu || !button) return;
  const open = menu.hidden;
  menu.hidden = !open;
  button.setAttribute('aria-expanded', open ? 'true' : 'false');
}
