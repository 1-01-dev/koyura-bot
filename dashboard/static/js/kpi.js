/* KPI cells: four hairline cells fed by /api/dashboard-stats.

   Each cell has a value node and (except disk) a trend node. The trend text
   is deliberately terse: an arrow, the comparison value, and the word
   "yesterday" so the number never floats without a reference point. */

import { count, percent } from './format.js';

/* The disk KPI compares the downloads folder against a fixed ceiling rather
   than the host filesystem: the panel cares about what the cleanup loop will
   delete, and that ceiling is a policy number, not a disk statistic. */
const DISK_CEILING_BYTES = 100 * 1024 ** 3;

function node(id) {
  return document.getElementById(id);
}

function setText(id, text) {
  const el = node(id);
  if (el) el.textContent = text;
}

function setTrend(id, direction, text) {
  const el = node(id);
  if (!el) return;
  el.textContent = text;
  el.className = direction ? `kpi-trend ${direction}` : 'kpi-trend';
}

/* Downloads rise with the day; that is the good direction. */
function renderDownloadsTrend(stats) {
  const today = Number(stats.downloads_today ?? 0);
  const yesterday = Number(stats.downloads_yesterday ?? 0);

  if (today === 0 && yesterday === 0) {
    setTrend('kpi-downloads-trend', '', '');
    return;
  }
  const up = today >= yesterday;
  const arrow = up ? '\u2191' : '\u2193';
  setTrend('kpi-downloads-trend', up ? 'up' : 'down', `${arrow} vs ${count(yesterday)} yesterday`);
}

/* Errors fall with the day; that is the good direction, so the classes invert. */
function renderErrorsTrend(stats) {
  const today = Number(stats.error_rate ?? 0);
  const yesterday = Number(stats.error_rate_yesterday ?? 0);

  if (today === 0 && yesterday === 0) {
    setTrend('kpi-errors-trend', '', '');
    return;
  }
  const better = today <= yesterday;
  const arrow = better ? '\u2193' : '\u2191';
  setTrend(
    'kpi-errors-trend',
    better ? 'up' : 'down',
    `${arrow} vs ${yesterday.toFixed(1)}% yesterday`,
  );
}

function renderDisk(disk) {
  const value = node('kpi-disk');
  const bar = node('disk-bar');
  if (!disk || !value) return;

  const used = Number(disk.total_bytes) || 0;
  const usedPct = used > 0 ? Math.min((used / DISK_CEILING_BYTES) * 100, 100) : 0;

  value.textContent = percent(usedPct);
  if (bar) {
    /* Width is the whole signal: the fill is the cell's ink, so a percentage
       colour would add a second accent to a page that only has one. */
    bar.style.width = `${usedPct}%`;
    bar.className = 'disk-bar';
  }
}

/** Paint all four cells from one stats/disk payload. */
export function renderKpis(stats = {}, disk = null) {
  setText('kpi-downloads', count(stats.downloads_today));
  renderDownloadsTrend(stats);

  setText('kpi-users', count(stats.active_users_24h));

  setText('kpi-errors', stats.error_rate === null || stats.error_rate === undefined
    ? '--'
    : percent(stats.error_rate));
  renderErrorsTrend(stats);

  renderDisk(disk);
}
