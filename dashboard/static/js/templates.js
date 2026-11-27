/* Message templates: the editor's conveniences.

   The section is a plain form (the server owns validation and saving); this
   module only adds what a textarea cannot do by itself:

   - insert a placeholder at the cursor when its chip is clicked;
   - keep a live character count next to Telegram's limit;
   - render the preview with a debounce, calling the same endpoint the bot's
     renderer is pinned to, and show the error sentence instead of a broken
     render when the body is not valid;
   - ask before a destructive row action (`data-confirm`).

   No inline handlers: the panel's CSP allows no 'unsafe-inline' scripts. */

const PREVIEW_URL = '/api/message-templates/preview';
const DEBOUNCE_MS = 350;

function csrfToken() {
  const meta = document.querySelector('meta[name="csrf-token"]');
  return meta ? meta.getAttribute('content') || '' : '';
}

/** Put `text` where the caret is, keeping the field focused and undoable. */
function insertAtCursor(field, text) {
  const start = field.selectionStart ?? field.value.length;
  const end = field.selectionEnd ?? field.value.length;
  field.setRangeText(text, start, end, 'end');
  field.dispatchEvent(new Event('input', { bubbles: true }));
  field.focus();
}

function updateCount(field, counter, warning, limit) {
  const length = field.value.length;
  if (counter) counter.textContent = `${length} / ${limit}`;
  if (!warning) return;
  if (length > limit) {
    warning.hidden = false;
    warning.textContent = `На ${length - limit} символов больше лимита Telegram (${limit}) — бот обрежет хвост.`;
  } else {
    warning.hidden = true;
    warning.textContent = '';
  }
}

async function renderPreview(card, field) {
  const preview = document.getElementById('template-preview');
  const errorBox = document.getElementById('template-preview-error');
  const meta = document.getElementById('template-preview-meta');
  if (!preview || !errorBox) return;

  let payload;
  try {
    const response = await fetch(PREVIEW_URL, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken() },
      body: JSON.stringify({
        kind: card.dataset.kind,
        lang: card.dataset.lang,
        body: field.value,
      }),
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    payload = await response.json();
  } catch (err) {
    console.error('template preview failed', err);
    errorBox.hidden = false;
    errorBox.textContent = 'Не удалось отрисовать предпросмотр. Попробуйте сохранить — ошибка валидации появится над формой.';
    preview.hidden = true;
    return;
  }

  if (payload.error) {
    errorBox.hidden = false;
    errorBox.textContent = payload.error;
    preview.hidden = true;
  } else {
    errorBox.hidden = true;
    errorBox.textContent = '';
    preview.hidden = false;
    preview.textContent = payload.text || '';
  }
  if (meta) meta.textContent = `${payload.length || 0} символов`;
}

function confirmForms(root) {
  root.querySelectorAll('form[data-confirm]').forEach((form) => {
    form.addEventListener('submit', (event) => {
      const message = form.getAttribute('data-confirm') || 'Продолжить?';
      if (!window.confirm(message)) event.preventDefault();
    });
  });
}

export function initTemplates() {
  const card = document.getElementById('templates-card');
  if (!card) return;

  confirmForms(card);

  const field = document.getElementById('template-body');
  if (!field) return;

  const limit = Number(field.getAttribute('data-limit')) || 4096;
  const counter = document.getElementById('template-count');
  const warning = document.getElementById('template-warning');

  updateCount(field, counter, warning, limit);

  let timer = null;
  field.addEventListener('input', () => {
    updateCount(field, counter, warning, limit);
    if (timer) window.clearTimeout(timer);
    timer = window.setTimeout(() => renderPreview(card, field), DEBOUNCE_MS);
  });

  card.querySelectorAll('[data-insert]').forEach((chip) => {
    chip.addEventListener('click', () => {
      insertAtCursor(field, chip.getAttribute('data-insert') || '');
    });
  });
}
