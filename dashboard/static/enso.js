/* Koyura — animated ASCII ensō. Vanilla, no libraries.
   Size is fixed at 78x36 so the animation never reads layout: each frame
   builds one string and writes one text node. Pauses when the tab is hidden
   and renders a single static frame under prefers-reduced-motion. */
(function () {
  'use strict';
  var pre = document.getElementById('enso');
  if (!pre) return;

  var C = 78, R = 36, RAMP = ' .`:-=+*#%@', TAU = Math.PI * 2;
  var reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;
  var mx = 0, my = 0, tx = 0, ty = 0, raf = 0;

  function draw(t) {
    var rot = t * 0.0004, out = '', c, r;
    for (r = 0; r < R; r++) {
      var y = r - R / 2 - my;
      for (c = 0; c < C; c++) {
        var x = (c - C / 2 - mx) * 0.5;
        var d = Math.sqrt(x * x + y * y);
        var a = Math.atan2(y, x);
        var f = ((a - rot) % TAU + TAU) % TAU / TAU;
        var th = 1.2 + 4.2 * (1 - f);
        var rad = 13 + Math.sin(a * 3 + t * 0.0009) * 0.6;
        var v = 0;
        if (f < 0.93) {
          v = 1 - Math.abs(d - rad) / th;
          if (v > 0) v += Math.sin(c * 0.7 + t * 0.002) * 0.12;
        }
        var dx = x - 4.5 - mx, dy = y + 1;
        if (Math.sqrt(dx * dx + dy * dy) < 1.6) v = Math.max(v, 0.9);
        if (v < 0) v = 0;
        else if (v > 0.999) v = 0.999;
        out += RAMP[(v * RAMP.length) | 0];
      }
      out += '\n';
    }
    pre.textContent = out;
  }

  function tick(t) {
    raf = requestAnimationFrame(tick);
    mx += (tx - mx) * 0.06;
    my += (ty - my) * 0.06;
    draw(t);
  }
  function start() {
    if (!raf && !reduced) raf = requestAnimationFrame(tick);
  }
  function stop() {
    if (raf) { cancelAnimationFrame(raf); raf = 0; }
  }

  addEventListener('pointermove', function (e) {
    tx = (e.clientX / innerWidth - 0.5) * 8;
    ty = (e.clientY / innerHeight - 0.5) * 5;
  }, { passive: true });

  document.addEventListener('visibilitychange', function () {
    if (document.hidden) stop(); else start();
  });

  draw(0);
  start();
})();
