/* Dashboard page controller.

   Owns the four polled resources and the range buttons. The charts are fetched
   once (and on demand), the KPI row and the active-downloads card refresh on a
   fixed cadence: those are the numbers that go stale while you watch them. */

import { count } from './format.js';
import { renderKpis } from './kpi.js';
import { createCharts } from './charts.js';
import { createSubscriptionPanel } from './subscriptions.js';
import { renderActiveDownloads } from './active.js';

const REFRESH_MS = 30_000;
const DEFAULT_RANGE = '1D';

const GLYPHS = '\u30ea\u30f3\u30af \u00b7 \u52d5\u753b \u00b7 \u97f3\u58f0 \u00b7 \u4fdd\u5b58 \u00b7 MP4 \u00b7 MP3 \u00b7 \u901f \u00b7 \u9759 \u00b7 ';

async function getJson(url) {
  const res = await fetch(url, { credentials: 'same-origin' });
  if (!res.ok) return null;
  return res.json();
}

async function refreshKpis() {
  try {
    const payload = await getJson('/api/dashboard-stats');
    if (!payload) return;
    renderKpis(payload.stats || {}, payload.disk || null);
  } catch (err) {
    console.warn('KPI refresh failed', err);
  }
}

async function refreshActive() {
  try {
    const jobs = await getJson('/api/active-downloads');
    if (!jobs) return;
    renderActiveDownloads(jobs);
  } catch (err) {
    console.warn('Active downloads refresh failed', err);
  }
}

/** Top-users panel: built from nodes, never from an HTML string. */
function renderTopUsers(users) {
  const body = document.getElementById('top-users');
  if (!body) return;

  body.replaceChildren();
  if (!Array.isArray(users) || users.length === 0) {
    const tr = document.createElement('tr');
    const td = document.createElement('td');
    td.colSpan = 2;
    td.className = 'empty-state';
    td.textContent = 'No data yet';
    tr.appendChild(td);
    body.appendChild(tr);
    return;
  }

  for (const user of users) {
    const tr = document.createElement('tr');
    const name = document.createElement('td');
    name.textContent = user.username || 'unknown';
    const total = document.createElement('td');
    total.className = 'mono';
    total.textContent = count(user.count);
    tr.append(name, total);
    body.appendChild(tr);
  }
}

function initRangeButtons(charts, subscriptions) {
  const buttons = Array.from(document.querySelectorAll('.range-btn'));

  const load = async (range) => {
    try {
      const payload = await getJson(`/api/chart-data?range=${encodeURIComponent(range)}`);
      if (!payload) return;
      charts.update(payload);
      renderTopUsers(payload.top_users);
      /* The same window, the same request: the subscription cells and their
         series never show a period the downloads line is not on. */
      subscriptions.update(payload.subscriptions);
    } catch (err) {
      console.warn('Chart refresh failed', err);
    }
  };

  for (const button of buttons) {
    button.addEventListener('click', () => {
      for (const other of buttons) {
        other.classList.remove('active');
        other.setAttribute('aria-pressed', 'false');
      }
      button.classList.add('active');
      button.setAttribute('aria-pressed', 'true');
      load(button.dataset.range);
    });
  }

  load(DEFAULT_RANGE);
}

/* Decorative glyph ticker: 10 copies make the -50% keyframe loop seamlessly. */
function initMarquee() {
  const track = document.getElementById('marquee');
  if (track) track.textContent = GLYPHS.repeat(10);
}

/** Entry point for the dashboard page. */
export function initDashboardPage() {
  const charts = createCharts();
  const subscriptions = createSubscriptionPanel();
  initRangeButtons(charts, subscriptions);
  initMarquee();

  document.documentElement.addEventListener('koyura:theme', () => {
    charts.syncTheme();
    subscriptions.syncTheme();
  });

  refreshKpis();
  refreshActive();
  setInterval(() => {
    refreshKpis();
    refreshActive();
  }, REFRESH_MS);
}
