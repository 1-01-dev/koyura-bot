/* =========================================================
   Koyura — web loader
   =========================================================

   Paste links, look at the card, pick a quality, download. One module, no
   framework and no build step.

   Shape of the file:
     1. engine access   — token from the page, request helpers
     2. formatting      — duration, coarse wait estimates, friendly errors
     3. queue summary   — the read-only limits line under the form
     4. card rendering  — one renderer per state
     5. actions         — fetch info, download, poll, save, download all

   Everything the engine reports (titles, uploader names, error strings) is
   written with textContent: response data is never parsed as markup. */

import { DEFAULT_MAX_MB, pickFormats } from './format_list.js';

/* ---------------------------------------------------------
   1. Engine access
   --------------------------------------------------------- */

/* The loopback operator gets a token injected into the page; anyone else gets
   the empty string and the engine answers 401 on /api/*. */
function injectedToken() {
  const node = document.getElementById('api-token');
  if (!node) return '';
  try {
    const value = JSON.parse(node.textContent || '""');
    return typeof value === 'string' ? value : '';
  } catch {
    return '';
  }
}

const API_TOKEN = injectedToken();

/* Deployment knobs live in their own JSON node so the token element keeps
   exactly one value. Missing or malformed values fall back to the engine's
   default, never to a hard crash. */
function injectedConfig() {
  const node = document.getElementById('api-config');
  if (!node) return {};
  try {
    const value = JSON.parse(node.textContent || '{}');
    return value && typeof value === 'object' ? value : {};
  } catch {
    return {};
  }
}

const API_CONFIG = injectedConfig();

/* The send ceiling in megabytes. Telegram refuses anything above the Bot API
   limit and the engine applies the same number with `--max-filesize`, so the
   list of qualities should not offer what can only fail. */
function injectedMaxMb() {
  const value = Number(API_CONFIG.max_send_mb);
  return Number.isFinite(value) && value >= 0 ? value : DEFAULT_MAX_MB;
}

/* A response may carry its own ceiling (the engine knows `MAX_FILESIZE_MB`),
   and it wins over the page's value: the two are the same number today, and
   reading it from the answer keeps them the same when one of them moves. */
function sendLimitMb(data) {
  const value = Number(data && data.max_file_size_mb);
  return Number.isFinite(value) && value >= 0 ? value : MAX_SEND_MB;
}

const MAX_SEND_MB = injectedMaxMb();

function headers(extra) {
  const merged = Object.assign({}, extra || {});
  if (API_TOKEN) merged['X-Internal-Token'] = API_TOKEN;
  return merged;
}

async function request(path, options) {
  const res = await fetch(path, options || {});
  const data = await res.json().catch(() => ({}));
  return { ok: res.ok, status: res.status, data };
}

function getJson(path) {
  return request(path, { headers: headers() });
}

function postJson(path, body) {
  return request(path, {
    method: 'POST',
    headers: headers({ 'Content-Type': 'application/json' }),
    body: JSON.stringify(body),
  });
}

/* ---------------------------------------------------------
   2. Formatting
   --------------------------------------------------------- */

/* Coarse on purpose: the engine's wait is an estimate, so "~45 s" is honest
   where a precise number would look measured. */
function waitText(seconds) {
  const value = Number(seconds);
  if (!Number.isFinite(value) || value <= 0) return '';
  if (value < 90) return `~${Math.max(1, Math.round(value / 5)) * 5} s`;
  return `~${Math.max(2, Math.ceil(value / 60))} min`;
}

function durationText(seconds) {
  if (!seconds) return '';
  const total = Math.floor(Number(seconds));
  if (!Number.isFinite(total)) return '';
  const minutes = Math.floor(total / 60);
  const rest = String(total % 60).padStart(2, '0');
  return `${minutes}:${rest}`;
}

/* Engine prose is long and technical; these are the causes a user can act on.
   Anything unmatched is truncated rather than paraphrased. */
const ERROR_HINTS = [
  ['Unsupported URL', 'This URL is not supported'],
  ['Video unavailable', 'Video is unavailable or private'],
  ['Private video', 'This video is private'],
  ['HTTP Error 403', 'Access denied by the platform'],
  ['HTTP Error 404', 'Video not found'],
  ['copyright', 'Video blocked due to copyright'],
  ['geo', 'Video not available in your region'],
  ['timed out', 'Request timed out — try again'],
  ['Timed out', 'Request timed out — try again'],
  ['network', 'Network error — check your connection'],
  ['Network', 'Network error — check your connection'],
];

function friendlyError(raw) {
  const message = String(raw || '');
  for (const [needle, hint] of ERROR_HINTS) {
    if (message.includes(needle)) return hint;
  }
  return message.length > 80 ? `${message.slice(0, 80)}...` : message;
}

/* Refusal codes are fixed strings from the engine, never server prose. */
function refusalText(data) {
  const wait = waitText(data.retry_after || data.wait_seconds) || '~2 min';
  if (data.code === 'user_busy') {
    return `You already have a download running — try again in ${wait}`;
  }
  if (data.code === 'queue_full' || data.code === 'too_long' || data.code === 'busy') {
    return `The service is overloaded — try again in ${wait}`;
  }
  return '';
}

function parseUrls(text) {
  const parts = String(text || '').split(/[\s,]+/).map((part) => part.trim());
  return [...new Set(parts.filter((part) => part.startsWith('http')))];
}

/* Only remote images may become a src; anything else falls back to a glyph. */
function safeThumb(url) {
  return /^https?:\/\//i.test(url || '') ? url : '';
}

/* ---------------------------------------------------------
   3. Queue summary
   --------------------------------------------------------- */

async function refreshLimits() {
  const line = document.getElementById('limits-line');
  if (!line) return;

  try {
    const { ok, data } = await getJson('/api/queue');
    if (!ok) return;
    const counts = data.counts || {};
    const parts = [
      `slots ${counts.active || 0}/${counts.max_concurrent || 0}`,
      `queue ${counts.queued || 0}/${counts.max_queue_length || 0}`,
    ];
    const wait = waitText(data.next_wait_seconds);
    if (wait) parts.push(`${wait} wait`);
    line.textContent = parts.join(' \u00b7 ');
    line.classList.toggle('busy', (counts.queued || 0) > 0);
  } catch {
    /* the engine may be restarting; the next tick retries */
  }
}

/* ---------------------------------------------------------
   4. Card rendering
   --------------------------------------------------------- */

const state = {
  format: 'video',
  cards: [],
};

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

function block(className, text) {
  return element('div', className, text);
}

function button(label, className, onClick) {
  const node = element('button', className, label);
  node.type = 'button';
  node.addEventListener('click', onClick);
  return node;
}

function svgIcon(paths, attributes) {
  const ns = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(ns, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('width', '26');
  svg.setAttribute('height', '26');
  for (const [name, value] of Object.entries(attributes || {})) {
    svg.setAttribute(name, value);
  }
  for (const d of paths) {
    const path = document.createElementNS(ns, 'path');
    path.setAttribute('d', d);
    svg.appendChild(path);
  }
  return svg;
}

const ICON_PLAY = ['M5 3 19 12 5 21 5 3'];
const ICON_IMAGE = ['M2 2h20v20H2z', 'M8 8a1.5 1.5 0 1 1 0 3 1.5 1.5 0 0 1 0-3z', 'm21 15-5-5L5 21'];

/* The card shell: reuse the node for this index so a re-render replaces its
   contents without touching its position in the list. */
function cardShell(index) {
  const id = `card-${index}`;
  let node = document.getElementById(id);
  if (!node) {
    node = element('div', 'card');
    node.id = id;
    document.getElementById('cards').appendChild(node);
  }
  return node;
}

function thumbnail(card) {
  const wrap = block('card-thumb');

  if (state.format === 'audio') {
    const inner = block('no-thumb');
    inner.appendChild(svgIcon(ICON_PLAY, { fill: 'currentColor', stroke: 'none' }));
    wrap.appendChild(inner);
    return wrap;
  }

  const src = safeThumb(card.thumbnail);
  if (src) {
    const img = document.createElement('img');
    img.src = src;
    img.alt = '';
    img.loading = 'lazy';
    wrap.appendChild(img);
    return wrap;
  }

  const inner = block('no-thumb');
  inner.appendChild(
    svgIcon(ICON_IMAGE, { fill: 'none', stroke: 'currentColor', 'stroke-width': '1.5' }),
  );
  wrap.appendChild(inner);
  return wrap;
}

function actions(card, index) {
  const row = block('card-actions');
  const audio = state.format === 'audio';

  switch (card.status) {
    case 'ready': {
      row.appendChild(button('Download', 'card-dl-btn', () => downloadCard(index)));
      /* Quality only exists when the extractor listed more than one format,
         and only for video: MP3 has nothing to choose. A chip the engine
         would refuse for its size keeps its place but cannot be picked. */
      if (!audio && card.formats && card.formats.length > 1) {
        for (const format of card.formats) {
          const label = format.label === null || format.label === undefined
            ? ''
            : String(format.label);
          const chip = button(
            label,
            `q-chip${format.id === card.selectedFormatId ? ' active' : ''}`
              + (format.too_large ? ' q-chip--too-large' : ''),
            () => selectFormat(index, format.id),
          );
          if (format.too_large) {
            chip.disabled = true;
            chip.title = `Too large to send \u2014 over the ${card.maxSendMb || MAX_SEND_MB} MB limit`;
            chip.setAttribute('aria-disabled', 'true');
          }
          row.appendChild(chip);
        }
        if (card.filteredFormats) {
          row.appendChild(
            block('card-note', `Qualities over ${card.maxSendMb || MAX_SEND_MB} MB are not shown`),
          );
        }
      }
      break;
    }
    case 'queued': {
      const wait = waitText(card.waitSeconds);
      const where = card.position ? `In queue \u2116${card.position}` : 'Waiting for a slot';
      const status = block('card-status queued');
      status.appendChild(block('spin'));
      status.appendChild(document.createTextNode(wait ? `${where} \u00b7 ${wait}` : where));
      row.appendChild(status);
      break;
    }
    case 'downloading': {
      const status = block('card-status downloading');
      status.appendChild(block('spin'));
      status.appendChild(document.createTextNode('Downloading...'));
      row.appendChild(status);
      break;
    }
    case 'done': {
      /* Rendered from the saved file, not from the pill: after an MP4/MP3
         switch the card still has to point at the file that exists. */
      row.appendChild(button('Save', 'card-dl-btn done', () => saveCard(index)));
      row.appendChild(block('card-status done', `${savedLabel(card)} \u00b7 ${card.filename || ''}`));
      break;
    }
    case 'error': {
      row.appendChild(button('Retry', 'card-dl-btn', () => downloadCard(index)));
      row.appendChild(block('card-status error', friendlyError(card.error || 'Download failed')));
      break;
    }
    case 'busy': {
      /* Refused up front by the engine: the button stays so one tap retries. */
      row.appendChild(button('Retry', 'card-dl-btn', () => downloadCard(index)));
      row.appendChild(
        block('card-status error', card.error || 'The service is overloaded, try again later'),
      );
      break;
    }
    default:
      break;
  }

  return row;
}

/* The engine reports bytes; the card speaks in the same units the quality
   chips do (32.1MB / 1.2GB). Mirrors media_meta.format_filesize so the two
   never disagree. */
function sizeLabel(bytes) {
  const value = Number(bytes);
  if (!Number.isFinite(value) || value <= 0) return '';
  for (const [unit, factor] of [['GB', 1024 ** 3], ['MB', 1024 ** 2], ['KB', 1024]]) {
    if (value >= factor) {
      const amount = value / factor;
      return `${amount.toFixed(amount < 100 ? 1 : 0)}${unit}`;
    }
  }
  return `${Math.round(value)}B`;
}

/* Card caption: who uploaded it, how long it is, and how big the file is —
   the size of the picked quality while the card is open, and of the file on
   disk once it is done. Every part is optional; empty joins drop out so no
   separator dangles at either end. */
function metaText(card) {
  const parts = [];
  if (card.uploader) parts.push(card.uploader);
  const duration = card.duration ? durationText(card.duration) : '';
  if (duration) parts.push(duration);

  const saved = card.status === 'done' ? card.fileSizeLabel : '';
  const size = saved || (state.format === 'audio'
    ? (card.audio && card.audio.size_label)
    : selectedSizeLabel(card));
  if (size) parts.push(size);

  return parts.join(' \u00b7 ');
}

/* A finished card names the container that is actually on disk. Reading it
   from the filename keeps the label truthful when the pill is switched after
   the download: the caption and the size follow the new choice, the file
   behind "Save" does not change. */
function savedLabel(card) {
  const name = String(card.filename || '');
  const dot = name.lastIndexOf('.');
  const ext = dot >= 0 ? name.slice(dot + 1).toLowerCase() : '';
  if (ext) return ext.toUpperCase();
  return card.downloadedFormat === 'audio' ? 'MP3' : 'MP4';
}

/* One renderer per state; each returns the two cells of the card. */
const RENDERERS = {
  loading() {
    const thumb = block('card-thumb loading');
    const body = block('card-body');
    body.append(block('skeleton-line medium'), block('skeleton-line short'));
    return { thumb, body, className: 'card' };
  },

  'info-error'(card) {
    const thumb = block('card-thumb');
    const icon = block('card-error-icon', '\u8aa4');
    icon.setAttribute('aria-hidden', 'true');
    thumb.appendChild(icon);

    const body = block('card-body');
    body.append(
      block('card-title', 'Could not fetch video'),
      block('card-error-msg', friendlyError(card.error || '')),
      block('card-error-url', card.url),
    );
    return { thumb, body, className: 'card card-error' };
  },

  default(card, index) {
    const body = block('card-body');
    body.append(
      block('card-title', card.title || 'Untitled'),
      block('card-meta', metaText(card)),
      actions(card, index),
    );
    return { thumb: thumbnail(card), body, className: 'card' };
  },
};

function renderCard(index) {
  const card = state.cards[index];
  if (!card) return;

  const node = cardShell(index);
  const render = RENDERERS[card.status] || RENDERERS.default;
  const { thumb, body, className } = render(card, index);

  node.className = className;
  node.replaceChildren(thumb, body);
  renderDownloadAll();
}

/* The bulk bar is a function of the cards, never a flag that survives them:
   it is rebuilt after every state change, and it is removed outright when
   fewer than two cards are still waiting for a click. A disabled button on a
   page with nothing to download is a dead control, so it is not rendered at
   all. */
function renderDownloadAll() {
  const existing = document.getElementById('dl-all-bar');
  const ready = state.cards.filter((card) => card.status === 'ready').length;

  if (ready < 2) {
    if (existing) existing.remove();
    return;
  }
  if (existing) {
    /* Re-append moves it: a card created after the bar must not push it up
       the list, or the button would drift between rows. */
    document.getElementById('cards').appendChild(existing);
    return;
  }

  const bar = block('dl-all-bar');
  bar.id = 'dl-all-bar';
  bar.appendChild(button('Download All', 'dl-all-btn', downloadAll));
  document.getElementById('cards').appendChild(bar);
}

/* ---------------------------------------------------------
   5. Actions
   --------------------------------------------------------- */

function setFormat(pill) {
  for (const other of document.querySelectorAll('.pill')) {
    other.classList.remove('active');
    other.setAttribute('aria-pressed', other === pill ? 'true' : 'false');
  }
  pill.classList.add('active');
  state.format = pill.dataset.format;

  /* A format is a property of the whole page, so every card is redrawn: the
     thumbnail, the size in the caption, the quality chips and the "done"
     label all depend on it. Without this a finished card kept the previous
     format's styling and filename. */
  state.cards.forEach((_card, index) => renderCard(index));
}

async function fetchInfo(url) {
  try {
    const { data } = await postJson('/api/info', { url });
    if (data.error) return { status: 'info-error', error: data.error };
    /* The engine offers every stream it found; the chips only offer what can
       be sent. pickFormats keeps the list non-empty and flags the rest. */
    const limitMb = sendLimitMb(data);
    const picked = pickFormats(data.formats || [], limitMb);
    const formats = picked.formats;
    return {
      status: 'ready',
      title: data.title || '',
      thumbnail: data.thumbnail || '',
      duration: data.duration,
      uploader: data.uploader || '',
      formats,
      filteredFormats: picked.filtered,
      maxSendMb: limitMb,
      /* Kept so the MP3 caption can name a size: the pill has no quality list
         to pick from, and the card would otherwise lose the one number the
         engine did report. */
      audio: data.audio || null,
      selectedFormatId: formats[0] ? formats[0].id : null,
    };
  } catch (err) {
    return { status: 'info-error', error: err.message };
  }
}

async function fetchAll() {
  const urls = parseUrls(document.getElementById('urls').value);
  if (urls.length === 0) return;

  const goBtn = document.getElementById('goBtn');
  const container = document.getElementById('cards');
  goBtn.disabled = true;
  goBtn.textContent = 'Loading...';
  container.replaceChildren();
  state.cards = [];

  for (const url of urls) {
    const index = state.cards.length;
    state.cards.push({ url, status: 'loading' });
    renderCard(index);
    state.cards[index] = Object.assign({ url }, await fetchInfo(url));
    renderCard(index);
  }

  renderDownloadAll();
  goBtn.disabled = false;
  goBtn.textContent = 'Fetch';
}

/* The picked format's size sharpens the engine's wait estimate; it is omitted
   when the extractor did not report one. */
function selectedSize(card) {
  const chosen = (card.formats || []).find((format) => format.id === card.selectedFormatId);
  return chosen && chosen.size_bytes ? Number(chosen.size_bytes) : undefined;
}

/** Human size of the picked quality, or of the audio stream for MP3. */
function selectedSizeLabel(card) {
  if (state.format === 'audio') {
    return (card.audio && card.audio.size_label) || '';
  }
  const chosen = (card.formats || []).find((format) => format.id === card.selectedFormatId);
  return (chosen && chosen.size_label) || '';
}

function selectFormat(index, formatId) {
  state.cards[index].selectedFormatId = formatId;
  renderCard(index);
}

async function downloadCard(index) {
  const card = state.cards[index];
  card.status = 'downloading';
  card.error = null;
  card.position = null;
  card.waitSeconds = null;
  card.downloadedFormat = state.format;
  renderCard(index);

  let result;
  try {
    result = await postJson('/api/download', {
      url: card.url,
      format: state.format,
      format_id: card.selectedFormatId,
      title: card.title || '',
      size_bytes: selectedSize(card),
    });
  } catch (err) {
    card.status = 'error';
    card.error = err.message;
    renderCard(index);
    return;
  }

  const data = result.data || {};
  if (!result.ok || data.error) {
    /* 429 is an explicit refusal (busy / queue_full / too_long / user_busy):
       show the reason and keep the button for a retry. */
    card.status = result.status === 429 ? 'busy' : 'error';
    card.error = refusalText(data) || friendlyError(data.error || 'Download failed');
    renderCard(index);
    return;
  }

  card.jobId = data.job_id;
  if (data.status === 'queued') {
    card.status = 'queued';
    card.position = data.queue_position;
    card.waitSeconds = data.wait_seconds;
    renderCard(index);
  } else {
    card.status = 'downloading';
  }

  refreshLimits();
  pollCard(index);
}

function pollCard(index) {
  const card = state.cards[index];
  const tick = setInterval(async () => {
    let data;
    try {
      const res = await getJson(`/api/status/${card.jobId}`);
      data = res.data || {};
    } catch {
      clearInterval(tick);
      card.status = 'error';
      card.error = 'Lost connection to server';
      renderCard(index);
      return;
    }

    if (data.status === 'queued') {
      card.status = 'queued';
      card.position = data.queue_position;
      card.waitSeconds = data.wait_seconds;
      renderCard(index);
      return;
    }

    if (data.status === 'downloading') {
      if (card.status !== 'downloading') {
        card.status = 'downloading';
        renderCard(index);
      }
      return;
    }

    clearInterval(tick);
    if (data.status === 'done') {
      card.status = 'done';
      card.filename = data.filename;
      card.fileSizeLabel = sizeLabel(data.file_size_bytes);
      renderCard(index);
      refreshLimits();
      saveCard(index);
    } else {
      card.status = 'error';
      card.error = data.error;
      renderCard(index);
      refreshLimits();
    }
  }, 1000);
}

/* The file endpoint cannot read a header, so the loopback operator's token
   travels in the query string — nothing else is passed that way. */
function saveCard(index) {
  const card = state.cards[index];
  if (!card.jobId) return;

  const link = document.createElement('a');
  link.href = `/api/file/${card.jobId}`
    + (API_TOKEN ? `?token=${encodeURIComponent(API_TOKEN)}` : '');
  link.download = card.filename || '';
  link.click();
}

async function downloadAll() {
  const btn = document.querySelector('.dl-all-btn');
  if (btn) {
    btn.disabled = true;
    btn.textContent = 'Downloading...';
  }

  for (let index = 0; index < state.cards.length; index += 1) {
    if (state.cards[index].status === 'ready') await downloadCard(index);
  }

  if (btn) {
    btn.disabled = false;
    btn.textContent = 'Download All';
  }
}

/* ---------------------------------------------------------
   Wiring
   --------------------------------------------------------- */

function bind() {
  const field = document.getElementById('urls');
  const goBtn = document.getElementById('goBtn');
  if (!field || !goBtn) return;

  /* Enter fetches; Shift+Enter is a newline, so multi-line paste still works. */
  field.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      fetchAll();
    }
  });

  for (const pill of document.querySelectorAll('.pill')) {
    pill.addEventListener('click', () => setFormat(pill));
  }

  goBtn.addEventListener('click', fetchAll);

  refreshLimits();
  setInterval(refreshLimits, 5000);
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', bind);
} else {
  bind();
}
