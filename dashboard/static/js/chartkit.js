/* Shared Chart.js furniture: palette, hairline axes, square tooltip, motion.

   The dashboard draws two panels and the subscriptions panel a third, all in
   the same ink. Keeping the palette and the axis rules in one module means a
   theme switch cannot leave one chart in the previous theme's colours, and
   there is exactly one place that knows a chart has no rounded corners.

   The palette is read from the CSS custom properties rather than repeated
   here: one token source, and a theme switch is a repaint. */

export const MONO = "'JetBrains Mono', ui-monospace, monospace";

const prefersReducedMotion = Boolean(
  window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches,
);

/** One design token, or the fallback colour when the sheet has not loaded. */
export function token(name, fallback) {
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return value || fallback;
}

/** The five colours every chart on the page uses. */
export function palette() {
  return {
    ink: token('--ink', '#17140f'),
    dim: token('--dim', '#6a6355'),
    red: token('--red', '#d4361c'),
    bg: token('--bg', '#ece6d8'),
    line: token('--line', 'rgba(23,20,15,.28)'),
  };
}

/** A 1px grid and a mono tick row — the hairlines of the rest of the panel. */
export function axis(theme) {
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

/** The same axis for a counted quantity: zero-based, integers only. */
export function countsAxis(theme) {
  const base = axis(theme);
  return {
    ...base,
    beginAtZero: true,
    ticks: { ...base.ticks, precision: 0 },
  };
}

/** Paper-coloured box, ink border, no radius: the tooltip of a printed chart. */
export function tooltip(theme) {
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

/** The growth animation, or nothing when the visitor asked for less motion. */
export function animation() {
  return prefersReducedMotion ? false : { duration: 400, easing: 'easeOut' };
}

/** Chart.js globals: one font, one tick colour, one border colour. */
export function applyDefaults(Chart, theme) {
  Chart.defaults.font.family = MONO;
  Chart.defaults.font.size = 10;
  Chart.defaults.color = theme.dim;
  Chart.defaults.borderColor = theme.line;
}
