/* Koyura — theme switch.
   Applies a stored theme before first paint, then keeps the toggle in sync.
   Default is the system preference: no attribute means "follow the OS".
   localStorage is wrapped in try/catch — private mode must not break the page. */
(function () {
  'use strict';

  var KEY = 'koyura-theme';
  var root = document.documentElement;

  function read() {
    var v = null;
    try {
      v = window.localStorage.getItem(KEY);
    } catch (err) {
      void err;
    }
    return v === 'light' || v === 'dark' ? v : null;
  }

  function write(v) {
    try {
      if (v) window.localStorage.setItem(KEY, v);
      else window.localStorage.removeItem(KEY);
    } catch (err) {
      /* storage unavailable: the chosen theme still holds for this session */
      void err;
    }
  }

  function systemIsDark() {
    return !!(window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches);
  }

  function effective() {
    return root.getAttribute('data-theme') || (systemIsDark() ? 'dark' : 'light');
  }

  function apply(theme) {
    if (theme) root.setAttribute('data-theme', theme);
    else root.removeAttribute('data-theme');
    sync();
    root.dispatchEvent(new CustomEvent('koyura:theme', { detail: { theme: effective() } }));
  }

  function sync() {
    var btn = document.getElementById('theme-toggle');
    if (!btn) return;
    /* The button states the colour it will switch TO, in mono caps. */
    btn.textContent = effective() === 'dark' ? 'Invert color · light' : 'Invert color · dark';
    btn.setAttribute('aria-pressed', effective() === 'dark' ? 'true' : 'false');
  }

  /* Pre-paint: honour a stored choice before the first frame. */
  var initial = read();
  if (initial) root.setAttribute('data-theme', initial);

  document.addEventListener('DOMContentLoaded', function () {
    var btn = document.getElementById('theme-toggle');
    sync();
    if (btn) {
      btn.addEventListener('click', function () {
        var next = effective() === 'dark' ? 'light' : 'dark';
        write(next);
        apply(next);
      });
    }

    /* A system change only matters while no explicit choice is stored. */
    if (!window.matchMedia) return;
    var mq = window.matchMedia('(prefers-color-scheme: dark)');
    var onChange = function () {
      if (!read()) apply(null);
    };
    if (mq.addEventListener) mq.addEventListener('change', onChange);
    else if (mq.addListener) mq.addListener(onChange);
  });
})();
