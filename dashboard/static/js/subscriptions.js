/* The Channels page's chart: subscriptions and unsubscriptions per day.

   A second, independent Chart.js panel — the dashboard's `charts.js` owns the
   downloads line and the platform bars, and this one must not fight it for the
   same canvas. Both read the same palette tokens, so a theme switch paints the
   same ink in both places.

   The numbers arrive in a JSON island (`#subscription-chart-data`) rather than
   from an API call: the page is server-rendered, the period is a link, and a
   round trip would only be a second way to read the same rows. */

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

function readData() {
  const island = document.getElementById('subscription-chart-data');
  if (!island) return { labels: [], subscribed: [], left: [] };
  try {
    const parsed = JSON.parse(island.textContent || '{}');
    return {
      labels: Array.isArray(parsed.labels) ? parsed.labels : [],
      subscribed: Array.isArray(parsed.subscribed) ? parsed.subscribed : [],
      left: Array.isArray(parsed.left) ? parsed.left : [],
    };
  } catch (err) {
    console.warn('Subscription chart data is not JSON', err);
    return { labels: [], subscribed: [], left: [] };
  }
}

function build(canvas, theme) {
  return new window.Chart(canvas, {
    type: 'bar',
    data: {
      labels: [],
      datasets: [
        {
          label: 'Subscribed',
          data: [],
          backgroundColor: theme.ink,
          borderColor: theme.ink,
          borderWidth: 0,
          borderRadius: 0,
        },
        {
          label: 'Unsubscribed',
          data: [],
          backgroundColor: theme.red,
          borderColor: theme.red,
          borderWidth: 0,
          borderRadius: 0,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: prefersReducedMotion ? false : { duration: 400, easing: 'easeOut' },
      interaction: { mode: 'index', intersect: false },
      /* Two series need a legend: the dashboard's single-series panels do not. */
      plugins: {
        legend: {
          display: true,
          position: 'bottom',
          labels: {
            boxWidth: 10,
            boxHeight: 10,
            color: theme.dim,
            font: { family: MONO, size: 10 },
          },
        },
        tooltip: tooltip(theme),
      },
      scales: { x: axis(theme), y: countsAxis(theme) },
    },
  });
}

function paint(chart, data) {
  chart.data.labels = data.labels;
  chart.data.datasets[0].data = data.subscribed;
  chart.data.datasets[1].data = data.left;
  chart.update('none');
}

export function initSubscriptionChart() {
  const canvas = document.getElementById('subscriptions-chart');
  if (!canvas || !window.Chart) return;

  window.Chart.defaults.font.family = MONO;
  window.Chart.defaults.font.size = 10;

  let chart = build(canvas, palette());
  paint(chart, readData());

  /* A theme switch repaints every live chart; rebuilding is the only reliable
     way to drop the previous ink from the canvas (see charts.js). */
  document.documentElement.addEventListener('koyura:theme', () => {
    const data = readData();
    chart.destroy();
    chart = build(canvas, palette());
    paint(chart, data);
  });
}
