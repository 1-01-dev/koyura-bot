/* Which quality chips a card may offer.

   Telegram refuses a file above the Bot API ceiling, and the engine enforces
   the same number with `--max-filesize`, so offering a 4K stream that can only
   fail is a dead end. The rule here is deliberately narrow:

     * a format with a *known* size above the ceiling is not offered;
     * a format with no size at all is offered — the extractor only omits it
       for HLS/DASH sources, and the engine still refuses it if it turns out
       too large;
     * when the ceiling would leave nothing to choose from (one 4K source and
       nothing else), every format is offered again, marked as too large.

   The list is therefore never empty and never silently drops the only option.
   No DOM, no fetch: the loader renders what this returns, and a plain Node
   test can call it.
*/

/** Default send ceiling in megabytes; the page can override it. */
export const DEFAULT_MAX_MB = 2000;

const MB = 1024 * 1024;

/** Bytes for a megabyte limit, or `Infinity` for a limit of 0 (unlimited). */
export function limitBytes(maxMb) {
  const value = Number(maxMb);
  if (!Number.isFinite(value) || value <= 0) return Infinity;
  return value * MB;
}

/** True only when the size is known and above the ceiling. */
export function isTooLarge(format, maxMb = DEFAULT_MAX_MB) {
  const limit = limitBytes(maxMb);
  if (limit === Infinity) return false;
  const size = Number(format && format.size_bytes);
  if (!Number.isFinite(size) || size <= 0) return false;
  return size > limit;
}

/**
 * Split the extractor's formats into the chips to show.
 *
 * Returns `{ formats, filtered }`: `formats` keeps the engine's order, each
 * entry copied with a `too_large` flag; `filtered` says whether anything was
 * suppressed, so the card can explain the gap in one line.
 */
export function pickFormats(formats, maxMb = DEFAULT_MAX_MB) {
  const list = Array.isArray(formats) ? formats : [];
  const marked = list.map((format) => ({
    ...format,
    too_large: isTooLarge(format, maxMb),
  }));

  const fitting = marked.filter((format) => !format.too_large);
  if (fitting.length > 0) {
    return { formats: fitting, filtered: fitting.length !== marked.length };
  }
  /* Nothing fits: keep the list usable and mark every chip instead. */
  return { formats: marked, filtered: false };
}
