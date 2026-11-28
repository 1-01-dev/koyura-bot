/* Koyura admin — page entry.

   base.html loads this one module on every page; it decides what to wire from
   the DOM that the current template rendered, so a page without charts or a
   file table costs nothing.

   The action table stays here, next to the markup contract: every value is the
   exact `data-action` name used in a template. */

import { initDashboardPage } from './js/report.js';
import { initLinkModal } from './js/linkmodal.js';
import { initTemplates } from './js/templates.js';
import { initHelpBubbles } from './js/help.js';
import { initProxyPage } from './js/proxy.js';
import {
  deleteSelected,
  openPurgeModal,
  closePurgeModal,
  confirmPurge,
  fillLimitsPreset,
  initAdminActions,
  initSelectAll,
} from './js/admin.js';

const ADMIN_ACTIONS = {
  'delete-selected': deleteSelected,
  'purge-open': openPurgeModal,
  'purge-close': closePurgeModal,
  'purge-confirm': confirmPurge,
  'limit-preset': fillLimitsPreset,
};

function bootDashboard() {
  if (!document.getElementById('downloads-chart')) return;
  initDashboardPage();
}

function bootAdmin() {
  /* The admin page is five pages now: the file table and the purge dialog live
     on /admin/system, the presets on /admin/limits, the editor on /admin/bot,
     the channel table on /admin/channels. Each wiring starts on its own hook,
     so a page without that section costs nothing and no section is skipped
     because one id is missing. The subscription panel is the dashboard's now
     (report.js wires it), so there is nothing to boot for it here. */
  initAdminActions(ADMIN_ACTIONS);
  initSelectAll();
  if (document.getElementById('templates-card')) initTemplates();
  /* The Proxy page owns a table, two dialogs and a menu of its own; every hook
     it reads is an id that page renders, so another section costs one lookup. */
  if (document.getElementById('proxy-card')) initProxyPage();
}

function boot() {
  bootDashboard();
  bootAdmin();
  initLinkModal();
  // Every page may carry a "?": the help bubbles are pure markup, so they are
  // wired once here instead of per page.
  initHelpBubbles();
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', boot);
} else {
  boot();
}
