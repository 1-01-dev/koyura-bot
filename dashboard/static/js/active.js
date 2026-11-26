/* Active downloads card.

   The card stays hidden while the engine is idle: an empty panel on a quiet
   day is noise, and the KPI row already says how much happened today. */

const URL_PREVIEW_CHARS = 50;

function cell(text, className) {
  const td = document.createElement('td');
  if (className) td.className = className;
  td.textContent = text;
  return td;
}

function platformCell(platform) {
  const td = document.createElement('td');
  if (!platform) {
    td.textContent = '\u2014';
    return td;
  }
  const badge = document.createElement('span');
  badge.className = 'badge badge-platform';
  badge.textContent = platform;
  td.append(badge);
  return td;
}

function row(job) {
  const tr = document.createElement('tr');

  const who = job.username || job.user_id || '\u2014';
  tr.append(cell(who));

  const url = String(job.url || '');
  const short = url.length > URL_PREVIEW_CHARS
    ? `${url.slice(0, URL_PREVIEW_CHARS)}\u2026`
    : url;
  tr.append(cell(short, 'url-cell'), platformCell(job.platform));

  /* The event ingest stores `percent`; older rows may still carry `progress`. */
  const value = job.percent ?? job.progress;
  const progress = value === null || value === undefined
    ? '\u2014'
    : `${Number(value).toFixed(0)}%`;
  tr.append(cell(progress, 'mono'));

  return tr;
}

/** Replace the card body and flip the card's visibility. */
export function renderActiveDownloads(jobs = []) {
  const section = document.getElementById('active-downloads');
  if (!section) return;

  const empty = !Array.isArray(jobs) || jobs.length === 0;
  section.style.display = empty ? 'none' : '';
  if (empty) return;

  const badge = document.getElementById('active-downloads-count');
  if (badge) badge.textContent = String(jobs.length);

  const body = document.getElementById('active-downloads-body');
  if (!body) return;
  body.replaceChildren();
  for (const job of jobs) body.append(row(job));
}
