/* Number formatting shared by the panels.
   Every renderer uses these so a missing value always reads as "--" instead
   of "undefined" or "NaN". */

const EMPTY = '--';

/** Counts: locale grouped, or the placeholder when the API sent nothing. */
export function count(value) {
  if (value === null || value === undefined || value === '') return EMPTY;
  const n = Number(value);
  return Number.isFinite(n) ? n.toLocaleString() : EMPTY;
}

/** Percentages with a fixed number of decimals, or the placeholder. */
export function percent(value, digits = 1) {
  if (value === null || value === undefined || value === '') return EMPTY;
  const n = Number(value);
  return Number.isFinite(n) ? `${n.toFixed(digits)}%` : EMPTY;
}
