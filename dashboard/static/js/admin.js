/* Admin page: file deletion, the purge dialog, and the limits presets.

   No button in the templates carries an inline handler — the panel's CSP has
   no 'unsafe-inline' for scripts — so everything is wired here through the
   `data-action` hooks. Destructive calls also send the CSRF token from the
   page meta tag. */

import { showToast } from './toast.js';

const PURGE_WORD = 'PURGE';

function csrfToken() {
  const meta = document.querySelector('meta[name="csrf-token"]');
  return meta ? meta.getAttribute('content') || '' : '';
}

function jsonRequest(method, body) {
  return {
    method,
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken() },
    body: JSON.stringify(body || {}),
  };
}

function plural(count, singular) {
  return count === 1 ? singular : `${singular}s`;
}

function selectedPaths() {
  return Array.from(document.querySelectorAll('.file-check:checked'), (box) => box.value);
}

function dropRow(path) {
  const box = document.querySelector(`.file-check[value="${CSS.escape(path)}"]`);
  const row = box && box.closest('tr');
  if (row) row.remove();
}

/** Delete every checked file; the row disappears only after the server agrees. */
export async function deleteSelected() {
  const paths = selectedPaths();
  if (paths.length === 0) {
    showToast('No files selected.', 'warning');
    return;
  }

  try {
    const res = await fetch('/api/files', jsonRequest('DELETE', { paths }));
    const data = await res.json();
    const deleted = data.deleted || [];
    const errors = data.errors || [];

    if (deleted.length > 0) {
      showToast(`Deleted ${deleted.length} ${plural(deleted.length, 'file')}.`, 'success');
      deleted.forEach(dropRow);
    }
    if (errors.length > 0) {
      showToast(
        `${errors.length} ${plural(errors.length, 'file')} could not be deleted.`,
        'danger',
      );
    }
  } catch (err) {
    console.error('deleteSelected failed', err);
    showToast('Delete failed. See console.', 'danger');
  }
}

/** Open the purge dialog and put the cursor in the confirmation field. */
export function openPurgeModal() {
  const modal = document.getElementById('purge-modal');
  if (!modal) return;
  modal.style.display = 'flex';

  const field = document.getElementById('purge-confirm');
  if (field) {
    field.value = '';
    field.focus();
  }
}

export function closePurgeModal() {
  const modal = document.getElementById('purge-modal');
  if (modal) modal.style.display = 'none';
}

/** Purge everything, but only after the operator typed the confirmation word. */
export async function confirmPurge() {
  const field = document.getElementById('purge-confirm');
  if (!field || field.value !== PURGE_WORD) {
    showToast(`Type ${PURGE_WORD} to confirm.`, 'warning');
    return;
  }

  try {
    const res = await fetch('/api/files/all', jsonRequest('DELETE', { confirm: PURGE_WORD }));
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    await res.json();
    showToast('All files purged.', 'success');
    closePurgeModal();
    /* The table is stale by now; a reload is cheaper than a diff of the disk. */
    setTimeout(() => location.reload(), 1200);
  } catch (err) {
    console.error('confirmPurge failed', err);
    showToast('Purge failed. See console.', 'danger');
  }
}

/**
 * A preset only *fills* the limits form; the form itself is a plain POST, so
 * the server stays the authority and a failed save cannot desync the inputs.
 */
export function fillLimitsPreset(button) {
  const form = document.getElementById('limits-form');
  if (!form || !button) return;

  let values = {};
  try {
    values = JSON.parse(button.dataset.preset || '{}');
  } catch (err) {
    console.error('fillLimitsPreset: bad preset payload', err);
    return;
  }

  Object.keys(values).forEach((name) => {
    const input = form.querySelector(`[name="${CSS.escape(name)}"]`);
    if (input) input.value = values[name];
  });

  document.querySelectorAll('#limits-card .preset-btn').forEach((el) => {
    const chosen = el === button;
    el.classList.toggle('active', chosen);
    el.setAttribute('aria-pressed', chosen ? 'true' : 'false');
  });

  const label = button.dataset.presetLabel || 'Preset';
  const state = document.getElementById('limits-state');
  if (state) state.textContent = `${label} preset filled in \u2014 save to apply`;
  showToast(`${label} preset filled in. Save to apply.`, 'info');
}

/** Keep the header checkbox in sync with the rows below it. */
export function initSelectAll() {
  const all = document.getElementById('select-all');
  if (!all) return;

  all.addEventListener('change', () => {
    document.querySelectorAll('.file-check').forEach((box) => {
      box.checked = all.checked;
    });
  });

  document.addEventListener('change', (event) => {
    const target = event.target;
    if (!target || !target.classList || !target.classList.contains('file-check')) return;
    const boxes = document.querySelectorAll('.file-check');
    const checked = document.querySelectorAll('.file-check:checked');
    all.indeterminate = checked.length > 0 && checked.length < boxes.length;
    all.checked = boxes.length > 0 && checked.length === boxes.length;
  });
}

/**
 * Attach one click listener per `[data-action]` element.
 * The handler always receives the element, so parameterless actions ignore it
 * and the preset button can read its own payload.
 */
export function initAdminActions(actions) {
  document.querySelectorAll('[data-action]').forEach((el) => {
    const handler = actions[el.dataset.action];
    if (typeof handler !== 'function') return;
    el.addEventListener('click', () => handler(el));
  });
}
