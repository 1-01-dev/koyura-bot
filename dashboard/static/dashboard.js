/* Koyura admin — page entry.

   base.html loads this one module on every page; it decides what to wire from
   the DOM that the current template rendered, so a page without charts or a
   file table costs nothing.

   The action table stays here, next to the markup contract: every value is the
   exact `data-action` name used in a template. */

import { initDashboardPage } from './js/report.js';
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
  if (!document.getElementById('file-table')) return;
  initSelectAll();
  initAdminActions(ADMIN_ACTIONS);
}

function boot() {
  bootDashboard();
  bootAdmin();
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', boot);
} else {
  boot();
}
