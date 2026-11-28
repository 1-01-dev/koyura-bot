/* The dashboard's subscription panel: three KPI cells, a two-series line and
   the per-channel table.

   Everything arrives with the charts request (`/api/chart-data?range=…`), so
   the page never issues a second call for the same window, and the range
   buttons above the downloads chart drive both. The API decides visibility
   (`visible`): the gate is on *and* the window holds at least one transition.
   When it is false this module removes the block from the layout instead of
   leaving three cells reading "0" — an empty panel claims nobody subscribes,
   which is a different fact from "nothing recorded".

   Two series, one axis: subscriptions in ink, unsubscriptions in the accent,
   no fills. The accent is already on the errors cell and on the first of these
   cells, so the chart's second series reuses it deliberately: it is the same
   "this one needs attention" meaning, not a second colour vocabulary. */

import { count } from './format.js';
import {
  animation,
  applyDefaults,
  axis,
  countsAxis,
  palette,
  tooltip,
} from './chartkit.js';

/** The style the KPI labels use for the period: 1D, 7D, 1M, 1Y. */
function periodLabel(period) {
  return String(period || '').toUpperCase();
}

function renderKpis(block) {
  const period = periodLabel(block.period);
  const periods = document.querySelectorAll('#kpi-subscribed-period, #kpi-left-period');
  for (const node of periods) node.textContent = period;

  const values = {
    'kpi-subscribed': block.subscribed,
    'kpi-left': block.left,
    'kpi-active-subscribers': block.active,
  };
  for (const [id, value] of Object.entries(values)) {
    const node = document.getElementById(id);
    if (node) node.textContent = count(value);
  }
}

/** One row per channel; a single channel would only repeat the KPI cells. */
function renderBreakdown(block) {
  const card = document.getElementById('subscriptions-breakdown');
  const body = document.getElementById('subscriptions-by-channel');
  if (!body) return;

  const channels = Array.isArray(block.channels) ? block.channels : [];
  if (card) card.hidden = channels.length < 2;

  body.replaceChildren();
  if (channels.length < 2) return;

  for (const channel of channels) {
    const tr = document.createElement('tr');

    const nameCell = document.createElement('td');
    const label = channel.title || channel.username || 'channel';
    if (channel.link) {
      const link = document.createElement('a');
      link.href = channel.link;
      link.rel = 'noopener noreferrer';
      link.target = '_blank';
      link.textContent = label;
      nameCell.append(link);
    } else {
      nameCell.textContent = label;
    }

    const numbers = [channel.subscribed, channel.left, channel.active].map((value) => {
      const cell = document.createElement('td');
      cell.className = 'num mono';
      cell.textContent = count(value);
      return cell;
    });

    tr.append(nameCell, ...numbers);
    body.append(tr);
  }
}

function build(canvas, theme, animate) {
  return new window.Chart(canvas, {
    type: 'line',
    data: {
      labels: [],
      datasets: [
        {
          label: 'Subscriptions',
          data: [],
          borderColor: theme.ink,
          backgroundColor: theme.ink,
          borderWidth: 2,
          pointRadius: 2,
          pointHoverRadius: 4,
          pointBackgroundColor: theme.ink,
          pointBorderColor: theme.ink,
          tension: 0,
          fill: false,
        },
        {
          label: 'Unsubscriptions',
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
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: animate ? animation() : false,
      interaction: { mode: 'index', intersect: false },
      /* Two series need a legend; the one-series panels above do not. */
      plugins: {
        legend: {
          display: true,
          position: 'bottom',
          labels: {
            boxWidth: 10,
            boxHeight: 10,
            color: theme.dim,
            font: { family: "'JetBrains Mono', ui-monospace, monospace", size: 10 },
          },
        },
        tooltip: tooltip(theme),
      },
      scales: { x: axis(theme), y: countsAxis(theme) },
    },
  });
}

function destroyCharts(state) {
  if (state.chart) state.chart.destroy();
  state.chart = null;
}

function paint(state, block) {
  if (!state.chart) return;
  const series = block.chart || {};
  state.chart.data.labels = Array.isArray(series.labels) ? series.labels : [];
  state.chart.data.datasets[0].data = Array.isArray(series.subscribed)
    ? series.subscribed : [];
  state.chart.data.datasets[1].data = Array.isArray(series.left) ? series.left : [];
  state.chart.update('none');
}

/**
 * Wire the subscription panel. Returns a controller the page drives with the
 * `subscriptions` block of `/api/chart-data`.
 */
export function createSubscriptionPanel() {
  const root = document.getElementById('subscriptions-block');
  const canvas = document.getElementById('subscriptions-chart');
  if (!root || !canvas) {
    return { update: () => {}, syncTheme: () => {} };
  }

  let state = { chart: null, block: null };

  /** Chart.js is absent when the CDN is unreachable: numbers still render. */
  function ensureChart(animate) {
    if (state.chart || !window.Chart) return;
    const theme = palette();
    applyDefaults(window.Chart, theme);
    state.chart = build(canvas, theme, animate);
  }

  return {
    update(block = {}) {
      const visible = Boolean(block && block.visible);
      state.block = visible ? block : null;
      root.hidden = !visible;
      if (!visible) {
        /* Dropping the canvas means the next period asks for a fresh instance
           rather than a chart inside a hidden container, which measures its
           own height as zero. */
        destroyCharts(state);
        return;
      }

      renderKpis(block);
      renderBreakdown(block);
      ensureChart(true);
      paint(state, block);
    },

    /* Theme switch: same numbers, new ink, no growth animation (see charts.js). */
    syncTheme() {
      destroyCharts(state);
      if (!state.block) return;
      ensureChart(false);
      paint(state, state.block);
    },
  };
}
