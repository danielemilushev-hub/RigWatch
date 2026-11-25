// Time-series chart on canvas: thin lines, recessive hairline grid, a soft area
// wash for a single series, crosshair + tooltip on hover/keyboard, and one
// shared y-axis (two measures of different scale get two charts, never two axes).

import { h, fmt } from './ui.js';

function cssVar(el, name) {
  return getComputedStyle(el).getPropertyValue(name).trim() || '#888';
}

function niceCeil(v) {
  if (!(v > 0)) return 1;
  const exp = Math.pow(10, Math.floor(Math.log10(v)));
  const f = v / exp;
  const nice = f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 4 ? 4 : f <= 5 ? 5 : f <= 8 ? 8 : 10;
  return nice * exp;
}

function lowerBound(arr, x) {
  let lo = 0, hi = arr.length;
  while (lo < hi) { const mid = (lo + hi) >> 1; if (arr[mid] < x) lo = mid + 1; else hi = mid; }
  return lo;
}

export class TimeChart {
  /**
   * opts: { series:[{key,label,color}], format(v), tick(v)?, min=0, max?, floor?, height=180, label }
   * `color` is a CSS custom-property name such as "--c-cpu".
   */
  constructor(host, opts) {
    this.o = Object.assign({ min: 0, max: null, floor: 1, height: 180, format: v => String(v) }, opts);
    this.host = host;
    this.hover = null;                  // sample index under the crosshair
    host.classList.add('chart');
    if (this.o.series.length > 1) {
      this.legend = h('div.legend');
      this.legendVals = {};
      for (const s of this.o.series) {
        const val = h('span.legend-val', '');
        this.legendVals[s.key] = val;
        this.legend.append(h('span.legend-item', h('i.key', { style: { background: `var(${s.color})` } }), h('span', s.label), val));
      }
      host.append(this.legend);
    }
    this.canvas = h('canvas', { tabindex: 0, role: 'img', 'aria-label': this.o.label || 'Chart' });
    this.tip = h('div.chart-tip', { hidden: true });
    this.plot = h('div.chart-plot', { style: { height: this.o.height + 'px' } }, this.canvas, this.tip);
    host.append(this.plot);
    this.ctx = this.canvas.getContext('2d');

    this.canvas.addEventListener('pointermove', e => { this.pointerX = e.offsetX; this.render(); });
    this.canvas.addEventListener('pointerleave', () => { this.pointerX = null; this.hover = null; this.render(); });
    this.canvas.addEventListener('blur', () => { this.keyIdx = null; this.render(); });
    this.canvas.addEventListener('keydown', e => {
      if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
      e.preventDefault();
      const n = this.hist ? this.hist.t.length : 0;
      if (!n) return;
      const cur = this.keyIdx == null ? n - 1 : this.keyIdx;
      this.keyIdx = Math.max(this.first || 0, Math.min(n - 1, cur + (e.key === 'ArrowLeft' ? -1 : 1)));
      this.render();
    });
    this.ro = new ResizeObserver(() => this.render());
    this.ro.observe(this.plot);
  }

  setData(hist, windowSec) {
    this.hist = hist;
    this.win = windowSec;
    this.render();
  }

  destroy() { this.ro.disconnect(); }

  render() {
    const { canvas, ctx, o } = this;
    const w = this.plot.clientWidth, hgt = this.plot.clientHeight;
    if (!w || !hgt) return;
    const dpr = window.devicePixelRatio || 1;
    if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(hgt * dpr)) {
      canvas.width = Math.round(w * dpr); canvas.height = Math.round(hgt * dpr);
      canvas.style.width = w + 'px'; canvas.style.height = hgt + 'px';
    }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, hgt);

    const ink3 = cssVar(this.host, '--ink-3');
    const grid = cssVar(this.host, '--grid');
    const axis = cssVar(this.host, '--axis');
    const surface = cssVar(this.host, '--surface-1');
    const font = `11px ${cssVar(this.host, '--font') || 'sans-serif'}`;
    const padL = 44, padR = 12, padT = 10, padB = 22;
    const pw = w - padL - padR, ph = hgt - padT - padB;

    const t = this.hist ? this.hist.t : [];
    const n = t.length;
    const win = this.win || 60;
    const tEnd = n ? t[n - 1] : 0;
    const tStart = tEnd - win;
    const first = n ? Math.max(0, lowerBound(t, tStart) - 1) : 0;
    this.first = first;

    // y range: pick a round step so every tick label is a clean number
    let yMax = o.max;
    let ticks = 4;
    if (yMax == null) {
      let m = 0;
      for (const s of o.series) {
        const arr = this.hist && this.hist.s[s.key];
        if (!arr) continue;
        for (let i = first; i < n; i++) if (arr[i] != null && arr[i] > m) m = arr[i];
      }
      // Work in display units (o.scale, e.g. bytes -> bits) and, for byte sizes,
      // in the unit the label will use (o.base 1024 -> KB / MB / GB).
      const scale = o.scale || 1;
      let target = Math.max(m * 1.05, o.floor) * scale;
      let unit = 1;
      if (o.base) while (target / unit >= o.base) unit *= o.base;
      target /= unit;
      const exp = Math.pow(10, Math.floor(Math.log10(target)) - 1);
      let step = exp;
      for (const mult of [1, 2, 2.5, 5, 10, 20, 25, 50, 100]) {
        step = mult * exp;
        if (Math.ceil(target / step) <= 5) break;
      }
      ticks = Math.max(2, Math.ceil(target / step));
      yMax = (step * ticks * unit) / scale;
    }
    const yMin = o.min;
    const X = ti => padL + ((ti - tStart) / win) * pw;
    const Y = v => padT + ph - ((v - yMin) / (yMax - yMin)) * ph;

    // grid + y ticks
    ctx.font = font; ctx.textBaseline = 'middle'; ctx.textAlign = 'right';
    ctx.lineWidth = 1;
    for (let i = 0; i <= ticks; i++) {
      const v = yMin + ((yMax - yMin) * i) / ticks;
      const y = Math.round(Y(v)) + 0.5;
      ctx.strokeStyle = i === 0 ? axis : grid;
      ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(padL + pw, y); ctx.stroke();
      ctx.fillStyle = ink3;
      ctx.fillText((o.tick || o.format)(v), padL - 8, y);
    }
    // x labels
    ctx.textBaseline = 'alphabetic';
    const steps = 4;
    for (let i = 0; i <= steps; i++) {
      const secs = win - (win * i) / steps;
      const x = padL + (pw * i) / steps;
      ctx.textAlign = i === 0 ? 'left' : i === steps ? 'right' : 'center';
      const label = i === steps ? 'now' : secs >= 120 ? `${Math.round(secs / 60)} min` : `${Math.round(secs)} s`;
      ctx.fillStyle = ink3;
      ctx.fillText(label, x, hgt - 6);
    }

    if (n < 2) {
      ctx.textAlign = 'center'; ctx.fillStyle = ink3;
      ctx.fillText('Collecting data…', padL + pw / 2, padT + ph / 2);
      this.tip.hidden = true;
      return;
    }

    // series
    ctx.save();
    ctx.beginPath(); ctx.rect(padL, padT - 4, pw + 6, ph + 8); ctx.clip();
    const single = o.series.length === 1;
    const lastPoint = {};
    for (const s of o.series) {
      const arr = this.hist.s[s.key];
      if (!arr) continue;
      const color = cssVar(this.host, s.color);
      // split into runs of consecutive non-null samples
      const runs = [];
      let run = null;
      for (let i = first; i < n; i++) {
        const v = arr[i];
        if (v == null) { run = null; continue; }
        if (!run) { run = []; runs.push(run); }
        run.push([X(t[i]), Y(Math.min(v, yMax)), i]);
      }
      for (const r of runs) {
        if (single && r.length > 1) {
          ctx.beginPath();
          ctx.moveTo(r[0][0], Y(yMin));
          for (const [x, y] of r) ctx.lineTo(x, y);
          ctx.lineTo(r[r.length - 1][0], Y(yMin));
          ctx.closePath();
          ctx.globalAlpha = 0.12; ctx.fillStyle = color; ctx.fill(); ctx.globalAlpha = 1;
        }
        ctx.beginPath();
        r.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
        ctx.lineWidth = 2; ctx.lineJoin = 'round'; ctx.lineCap = 'round'; ctx.strokeStyle = color;
        ctx.stroke();
      }
      const lastRun = runs[runs.length - 1];
      if (lastRun && lastRun[lastRun.length - 1][2] === n - 1) lastPoint[s.key] = { p: lastRun[lastRun.length - 1], color };
    }
    ctx.restore();
    // end markers: r=4 with a 2px surface ring
    for (const { p, color } of Object.values(lastPoint)) {
      ctx.beginPath(); ctx.arc(p[0], p[1], 6, 0, Math.PI * 2); ctx.fillStyle = surface; ctx.fill();
      ctx.beginPath(); ctx.arc(p[0], p[1], 4, 0, Math.PI * 2); ctx.fillStyle = color; ctx.fill();
    }
    if (this.legendVals) {
      for (const s of o.series) {
        const arr = this.hist.s[s.key];
        const v = arr ? arr[n - 1] : null;
        const el = this.legendVals[s.key];
        const text = v == null ? '' : o.format(v);
        if (el.textContent !== text) el.textContent = text;
      }
    }

    // crosshair + tooltip
    let idx = null;
    if (this.pointerX != null && this.pointerX >= padL - 6 && this.pointerX <= padL + pw + 6) {
      const tt = tStart + ((this.pointerX - padL) / pw) * win;
      let i = lowerBound(t, tt);
      if (i >= n) i = n - 1;
      if (i > first && Math.abs(t[i - 1] - tt) < Math.abs(t[i] - tt)) i--;
      idx = Math.max(first, i);
    } else if (this.keyIdx != null) {
      idx = Math.min(n - 1, Math.max(first, this.keyIdx));
    }
    if (idx == null) { this.tip.hidden = true; return; }
    const x = X(t[idx]);
    ctx.strokeStyle = axis; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(Math.round(x) + 0.5, padT); ctx.lineTo(Math.round(x) + 0.5, padT + ph); ctx.stroke();
    const rows = [];
    for (const s of o.series) {
      const arr = this.hist.s[s.key];
      const v = arr ? arr[idx] : null;
      if (v == null) continue;
      const color = cssVar(this.host, s.color);
      const y = Y(Math.min(v, yMax));
      ctx.beginPath(); ctx.arc(x, y, 6, 0, Math.PI * 2); ctx.fillStyle = surface; ctx.fill();
      ctx.beginPath(); ctx.arc(x, y, 4, 0, Math.PI * 2); ctx.fillStyle = color; ctx.fill();
      rows.push(h('div.tip-row', h('i.key', { style: { background: color } }), h('strong', o.format(v)), h('span', s.label)));
    }
    this.tip.replaceChildren(h('div.tip-time', fmt.time(t[idx])), ...rows);
    this.tip.hidden = false;
    const tw = this.tip.offsetWidth;
    const left = x + 14 + tw > w ? x - 14 - tw : x + 14;
    this.tip.style.left = Math.max(4, left) + 'px';
    this.tip.style.top = padT + 'px';
  }
}

/** Tiny axis-less trend line for stat cards. */
export function drawSpark(canvas, values, colorVar, max) {
  const w = canvas.clientWidth, hgt = canvas.clientHeight;
  if (!w || !hgt) return;
  const dpr = window.devicePixelRatio || 1;
  if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(hgt * dpr)) {
    canvas.width = Math.round(w * dpr); canvas.height = Math.round(hgt * dpr);
  }
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, hgt);
  const pts = values.filter(v => v != null);
  if (pts.length < 2) return;
  const color = cssVar(canvas, colorVar);
  const top = max != null ? max : niceCeil(Math.max(...pts, 1e-9) * 1.1);
  const n = values.length;
  const X = i => (i / (n - 1)) * (w - 6) + 1;
  const Y = v => hgt - 3 - (Math.min(v, top) / top) * (hgt - 8);
  ctx.beginPath();
  let started = false, lastX = 0, lastY = 0, firstX = 0;
  values.forEach((v, i) => {
    if (v == null) return;
    const x = X(i), y = Y(v);
    if (!started) { ctx.moveTo(x, y); started = true; firstX = x; } else ctx.lineTo(x, y);
    lastX = x; lastY = y;
  });
  ctx.lineWidth = 1.75; ctx.lineJoin = 'round'; ctx.lineCap = 'round'; ctx.strokeStyle = color; ctx.stroke();
  ctx.lineTo(lastX, hgt); ctx.lineTo(firstX, hgt); ctx.closePath();
  ctx.globalAlpha = 0.12; ctx.fillStyle = color; ctx.fill(); ctx.globalAlpha = 1;
  ctx.beginPath(); ctx.arc(lastX, lastY, 2.6, 0, Math.PI * 2); ctx.fillStyle = color; ctx.fill();
}
