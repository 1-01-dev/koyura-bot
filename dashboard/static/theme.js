/* Koyura — theme switch. Runs before paint: honours a stored choice, else
   follows the OS. The toggle writes data-theme and remembers it (try/catch:
   private mode must not break the page). */
(function () {
  'use strict';
  var KEY = 'koyura-theme';
  var root = document.documentElement;

  function read() {
    var v = null;
    try { v = localStorage.getItem(KEY); } catch (e) { void e; }
    return v === 'light' || v === 'dark' ? v : null;
  }
  function write(v) {
    try {
      if (v) localStorage.setItem(KEY, v); else localStorage.removeItem(KEY);
    } catch (e) { void e; }
  }
  function isDark() {
    return !!matchMedia && matchMedia('(prefers-color-scheme: dark)').matches;
  }
  function now() {
    return root.getAttribute('data-theme') || (isDark() ? 'dark' : 'light');
  }
  function sync() {
    var b = document.getElementById('theme-toggle');
    if (!b) return;
    var dark = now() === 'dark';
    b.textContent = dark ? 'Invert color · light' : 'Invert color · dark';
    b.setAttribute('aria-pressed', dark ? 'true' : 'false');
  }
  function apply(t) {
    if (t) root.setAttribute('data-theme', t); else root.removeAttribute('data-theme');
    sync();
    root.dispatchEvent(new CustomEvent('koyura:theme', { detail: { theme: now() } }));
  }

  var first = read();
  if (first) root.setAttribute('data-theme', first);

  document.addEventListener('DOMContentLoaded', function () {
    var btn = document.getElementById('theme-toggle');
    sync();
    if (btn) {
      btn.addEventListener('click', function () {
        var next = now() === 'dark' ? 'light' : 'dark';
        write(next);
        apply(next);
      });
    }
    if (!matchMedia) return;
    var mq = matchMedia('(prefers-color-scheme: dark)');
    var onChange = function () { if (!read()) apply(null); };
    if (mq.addEventListener) mq.addEventListener('change', onChange);
    else if (mq.addListener) mq.addListener(onChange);
  });
})();
