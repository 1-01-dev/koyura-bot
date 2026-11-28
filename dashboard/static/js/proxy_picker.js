/* Proxy connections: the "Apply to" service picker.

   A search box, two bulk buttons ("Select shown" / "Clear shown") and a
   checklist of the same catalogue the Cookies page suggests. It owns three
   interactions and nothing else:

   - the filter is a substring match on the visible name, and `hidden` is the
     only thing it sets, so a filtered-out service is still checked in the form
     (a tick is a decision, not a view);
   - "shown" means "not filtered out", which is why "Clear shown" cannot quietly
     untick the services a search is hiding;
   - the picker appears for the two list modes only. "All services" has no list,
     and a hidden checklist that still submits its boxes would send services the
     operator never chose.

   The boxes are the form's `services` field, so nothing here assembles a
   payload: the browser posts the checklist with the rest of the editor. */

export function initProxyPicker() {
  const assignment = document.getElementById('proxy-assignment');
  if (assignment) assignment.addEventListener('change', syncPickerVisibility);

  const list = document.getElementById('proxy-service-list');
  if (list) list.addEventListener('change', syncCount);

  const search = document.getElementById('proxy-service-search');
  if (search) search.addEventListener('input', () => filter(search.value));

  const selectShown = document.getElementById('proxy-select-shown');
  if (selectShown) selectShown.addEventListener('click', () => markShown(true));

  const clearShown = document.getElementById('proxy-clear-shown');
  if (clearShown) clearShown.addEventListener('click', () => markShown(false));

  syncCount();
  syncPickerVisibility();
}

/** The picker belongs to "Selected" and "All except selected", not to "All". */
export function syncPickerVisibility() {
  const assignment = document.getElementById('proxy-assignment');
  const picker = document.getElementById('proxy-service-picker');
  if (!assignment || !picker) return;
  picker.hidden = assignment.value === 'all';
}

/** Tick the services the row already has; everything else starts clear. */
export function checkServices(names) {
  const wanted = new Set((names || []).map((name) => String(name).toLowerCase()));
  document.querySelectorAll('#proxy-service-list input[type="checkbox"]').forEach((box) => {
    box.checked = wanted.has(box.dataset.service);
  });
  const search = document.getElementById('proxy-service-search');
  if (search) search.value = '';
  filter('');
  syncCount();
}

export function syncCount() {
  const count = document.getElementById('proxy-service-count');
  if (!count) return;
  const chosen = document.querySelectorAll(
    '#proxy-service-list input[type="checkbox"]:checked',
  ).length;
  count.textContent = `${chosen} selected`;
}

function filter(query) {
  const needle = String(query || '').trim().toLowerCase();
  document.querySelectorAll('#proxy-service-list .proxy-service-row').forEach((row) => {
    const name = (row.querySelector('.proxy-service-name') || {}).textContent || '';
    row.hidden = Boolean(needle) && !name.toLowerCase().includes(needle);
  });
}

function markShown(checked) {
  document.querySelectorAll('#proxy-service-list .proxy-service-row').forEach((row) => {
    if (row.hidden) return;
    const box = row.querySelector('input[type="checkbox"]');
    if (box) box.checked = checked;
  });
  syncCount();
}
