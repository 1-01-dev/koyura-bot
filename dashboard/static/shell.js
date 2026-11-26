/* Koyura admin — application shell.
   Mobile sidebar toggle. Kept in its own file so the pages carry no
   inline script and the CSP can stay at script-src 'self'. */
(function () {
  'use strict';

  document.addEventListener('DOMContentLoaded', function () {
    var hamburger = document.getElementById('hamburger');
    var sidebar = document.getElementById('sidebar');
    var overlay = document.getElementById('sidebar-overlay');
    if (!hamburger || !sidebar) return;

    function setOpen(open) {
      sidebar.classList.toggle('open', open);
      if (overlay) overlay.classList.toggle('visible', open);
      hamburger.setAttribute('aria-expanded', open ? 'true' : 'false');
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
