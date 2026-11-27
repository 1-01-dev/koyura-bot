/* Koyura admin — application shell.

   Two behaviours that belong to the frame rather than to a page: the mobile
   drawer, and the Admin disclosure group in the sidebar. Kept in their own
   file so no page carries an inline script and the CSP can stay at
   script-src 'self'. */
(function () {
  'use strict';

  var NAV_KEY = 'koyura-nav-admin';

  function readNav() {
    try {
      var v = sessionStorage.getItem(NAV_KEY);
      return v === 'open' || v === 'closed' ? v : null;
    } catch (e) {
      void e;
      return null;
    }
  }

  function writeNav(open) {
    try {
      sessionStorage.setItem(NAV_KEY, open ? 'open' : 'closed');
    } catch (e) {
      void e;
    }
  }

  /* Admin group. The server already renders it open on every /admin/* page
     (the current sub-page must never be hidden), so the only job here is the
     toggle and the remembered state. The class is the single source of truth
     for what is on screen; aria-expanded mirrors it.

     This runs before the first paint — the script sits at the end of <body>,
     after the markup it reads — because collapsing a group after paint is a
     visible jump. */
  function setupNavGroup() {
    var group = document.querySelector('[data-nav-group="admin"]');
    if (!group) return;
    var toggle = group.querySelector('.nav-group-toggle');
    if (!toggle) return;

    function setOpen(open, remember) {
      group.classList.toggle('is-open', open);
      toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
      if (remember) writeNav(open);
    }

    var active = group.getAttribute('data-nav-active') === 'true';
    var remembered = readNav();
    if (active) setOpen(true, true);
    else if (remembered) setOpen(remembered === 'open', false);

    /* A real <button>: Enter and Space are the browser's job, not ours. */
    toggle.addEventListener('click', function () {
      setOpen(toggle.getAttribute('aria-expanded') !== 'true', true);
    });
  }

  setupNavGroup();

  document.addEventListener('DOMContentLoaded', function () {
    var hamburger = document.getElementById('hamburger');
    var sidebar = document.getElementById('sidebar');
    var overlay = document.getElementById('sidebar-overlay');
    if (!hamburger || !sidebar) return;

    function setOpen(open) {
      sidebar.classList.toggle('open', open);
      if (overlay) overlay.classList.toggle('visible', open);
      /* The trigger is fixed to the viewport while the drawer is a sibling, so
         the page root carries the state: that is the only hook CSS has to
         move the button inside the open drawer. */
      document.documentElement.classList.toggle('sidebar-open', open);
      hamburger.setAttribute('aria-expanded', open ? 'true' : 'false');
      hamburger.setAttribute('aria-label', open ? 'Close menu' : 'Open menu');
    }

    hamburger.addEventListener('click', function () {
      setOpen(!sidebar.classList.contains('open'));
    });

    if (overlay) {
      overlay.addEventListener('click', function () {
        setOpen(false);
      });
    }

    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && sidebar.classList.contains('open')) {
        setOpen(false);
        hamburger.focus();
      }
    });
  });
})();
