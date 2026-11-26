/* Chart.js panels: downloads over time (line) and downloads by platform (bar).

   Chart.js is loaded as a UMD bundle before this module runs, so it arrives as
   `window.Chart`. The palette is read from the CSS custom properties instead of
   being repeated here: one token source, and a theme switch is a repaint, not a
   second colour table. */

const prefersReducedMotion = Boolean(
  window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches,
);

const MONO = "'JetBrains Mono', ui-monospace, monospace";

function token(name, fallback) {
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return value || fallback;
}

function palette() {
  return {
    ink: token('--ink', '#17140f'),
    dim: token('--dim', '#6a6355'),
    red: token('--red', '#d4361c'),
    bg: token('--bg', '#ece6d8'),
    line: token('--line', 'rgba(23,20,15,.28)'),
  };
}

function axis(theme) {
  return {
    grid: { color: theme.line, drawTicks: false, lineWidth: 1 },
    border: { color: theme.ink, width: 1 },
    ticks: {
      color: theme.dim,
      maxRotation: 0,
      autoSkipPadding: 12,
      font: { family: MONO, size: 10 },
    },
  };
}

function countsAxis(theme) {
  const base = axis(theme);
  return {
    ...base,
    beginAtZero: true,
    ticks: { ...base.ticks, precision: 0 },
  };
}

function tooltip(theme) {
  return {
    backgroundColor: theme.bg,
    borderColor: theme.ink,
    borderWidth: 1,
    cornerRadius: 0,
    displayColors: false,
    titleColor: theme.ink,
    bodyColor: theme.dim,
    titleFont: { family: MONO, size: 11, weight: '500' },
    bodyFont: { family: MONO, size: 11 },
    padding: 10,
  };
}

function animation() {
  return prefersReducedMotion ? false : { duration: 400, easing: 'easeOut' };
}

function lineChart(canvas, theme, animate) {
  return new window.Chart(canvas, {
    type: 'line',
    data: {
      labels: [],
      datasets: [{
        label: 'Downloads',
        data: [],
        borderColor: theme.red,
        backgroundColor: theme.red,
        borderWidth: 2,
        pointRadius: 2,
        pointHoverRadius: 4,
        pointBackgroundColor: theme.red,
        pointBorderColor: theme.red,
        tension: 0,
        fill: false,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: animate ? animation() : false,
      interaction: { mode: 'index', intersect: false },
      plugins: { legend: { display: false }, tooltip: tooltip(theme) },
      scales: { x: axis(theme), y: countsAxis(theme) },
    },
  });
}

function barChart(canvas, theme, animate) {
  return new window.Chart(canvas, {
    type: 'bar',
    data: {
      labels: [],
      datasets: [{
        label: 'Downloads',
        data: [],
        /* Sumi bars: the accent belongs to the error cell, not to a ranking. */
        backgroundColor: theme.ink,
        borderColor: theme.ink,
        borderWidth: 0,
        borderRadius: 0,
        barPercentage: 0.62,
      }],
    },
    options: {
      indexAxis: 'y',
      responsive: true,
      maintainAspectRatio: false,
      animation: animate ? animation() : false,
      plugins: { legend: { display: false }, tooltip: tooltip(theme) },
      scales: { x: countsAxis(theme), y: axis(theme) },
    },
  });
}

/* Chart.js keeps the last animated colour on every element, so repainting a
   live chart after a theme switch can leave the previous ink on the canvas.
   Rebuilding is the reliable path — two canvases, and only on a theme change. */

function build(canvases, animate) {
  const theme = palette();

  window.Chart.defaults.font.family = MONO;
  window.Chart.defaults.font.size = 10;
  window.Chart.defaults.color = theme.dim;
  window.Chart.defaults.borderColor = theme.line;

  return {
    downloads: canvases.line ? lineChart(canvases.line, theme, animate) : null,
    platforms: canvases.bar ? barChart(canvases.bar, theme, animate) : null,
  };
}

function destroy(charts) {
  if (charts.downloads) charts.downloads.destroy();
  if (charts.platforms) charts.platforms.destroy();
}

function paint(charts, data, animate) {
  const { downloads, platforms } = charts;

  if (downloads) {
    downloads.data.labels = Array.isArray(data.labels) ? data.labels : [];
    downloads.data.datasets[0].data = Array.isArray(data.values) ? data.values : [];
    downloads.update(animate ? undefined : 'none');
  }

  if (platforms) {
    const rows = Array.isArray(data.platforms) ? data.platforms : [];
    platforms.data.labels = rows.map((row) => row.platform);
    platforms.data.datasets[0].data = rows.map((row) => row.count);
    platforms.update(animate ? undefined : 'none');
  }
}

/**
 * Create both panels if their canvases exist.
 * Returns a controller the page drives; an empty controller when the page has
 * no charts (history, errors, admin), so callers need no null checks.
 */
export function createCharts() {
  const canvases = {
    line: document.getElementById('downloads-chart'),
    bar: document.getElementById('platform-chart'),
  };
  if (!canvases.line && !canvases.bar) {
    return { update: () => {}, syncTheme: () => {} };
  }

  let data = { labels: [], values: [], platforms: [] };
  let charts = build(canvases, true);

  return {
    /* New numbers from the API: keep the theme, animate the transition. */
    update(payload = {}) {
      data = {
        labels: payload.labels || [],
        values: payload.values || [],
        platforms: payload.platforms || [],
      };
      paint(charts, data, true);
    },

    /* Theme switch: same numbers, new ink, no growth animation. */
    syncTheme() {
      destroy(charts);
      charts = build(canvases, false);
      paint(charts, data, false);
    },
  };
}
