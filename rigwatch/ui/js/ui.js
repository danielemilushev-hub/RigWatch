// Small DOM + formatting toolkit. No framework: the app is a handful of views
// that are built once and then have their text nodes updated every tick.

export function h(tag, attrs, ...children) {
  const [name, ...classes] = tag.split('.');
  const el = document.createElement(name || 'div');
  if (classes.length) el.className = classes.join(' ');
  if (attrs && (attrs instanceof Node || typeof attrs !== 'object' || Array.isArray(attrs))) {
    children.unshift(attrs);
    attrs = null;
  }
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className += (el.className ? ' ' : '') + v;
    else if (k === 'style' && typeof v === 'object') {
      for (const [prop, val] of Object.entries(v)) {
        if (prop.startsWith('--')) el.style.setProperty(prop, val);   // custom properties need setProperty
        else el.style[prop] = val;
      }
    }
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
    else if (k === 'html') el.innerHTML = v;            // only ever used with our own static SVG
    else el.setAttribute(k, v === true ? '' : v);
  }
  const add = c => {
    if (c == null || c === false) return;
    if (Array.isArray(c)) c.forEach(add);
    else el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  };
  children.forEach(add);
  return el;
}

/** Set text only when it changed, so the DOM is not touched needlessly. */
export function setText(el, text) {
  if (!el) return;
  const s = text == null ? '' : String(text);
  if (el.textContent !== s) el.textContent = s;
}

// ------------------------------------------------------------------ formats
export const prefs = { tempUnit: 'C', privacy: false };
/** Identifying details (computer name, addresses) are hidden while privacy is on. */
export const ident = v => (v == null || v === '' ? v : prefs.privacy ? 'Hidden' : v);
const nf0 = new Intl.NumberFormat('en-US', { maximumFractionDigits: 0 });
const nf1 = new Intl.NumberFormat('en-US', { minimumFractionDigits: 1, maximumFractionDigits: 1 });
const nf2 = new Intl.NumberFormat('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
export const DASH = '—';

export const fmt = {
  int: v => (v == null ? DASH : nf0.format(v)),
  pct: (v, d = 0) => (v == null ? DASH : (d ? nf1.format(v) : nf0.format(v)) + '%'),
  watts: v => (v == null ? DASH : (v >= 100 ? nf0.format(v) : nf1.format(v)) + ' W'),
  temp: v => {
    if (v == null) return DASH;
    return prefs.tempUnit === 'F' ? nf0.format(v * 9 / 5 + 32) + ' °F' : nf0.format(v) + ' °C';
  },
  tempValue: v => (v == null ? null : prefs.tempUnit === 'F' ? v * 9 / 5 + 32 : v),
  tempUnit: () => (prefs.tempUnit === 'F' ? '°F' : '°C'),
  clock: mhz => {
    if (mhz == null) return DASH;
    return mhz >= 1000 ? nf2.format(mhz / 1000) + ' GHz' : nf0.format(mhz) + ' MHz';
  },
  rpm: v => (v == null ? DASH : nf0.format(v) + ' RPM'),
  volts: mv => (mv == null ? DASH : (mv / 1000).toFixed(3) + ' V'),
  bytes: (b, digits) => {
    if (b == null) return DASH;
    const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
    let i = 0, v = Math.abs(b);
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
    const d = digits != null ? digits : (i === 0 ? 0 : v >= 100 ? 0 : 1);
    return new Intl.NumberFormat('en-US', { minimumFractionDigits: d, maximumFractionDigits: d }).format(v) + ' ' + units[i];
  },
  // Decimal units for drive capacity and lifetime totals, as drive vendors quote them.
  bytesDec: b => {
    if (b == null) return DASH;
    const units = ['B', 'kB', 'MB', 'GB', 'TB', 'PB'];
    let i = 0, v = b;
    while (v >= 1000 && i < units.length - 1) { v /= 1000; i++; }
    return (v >= 100 ? nf0.format(v) : nf1.format(v)) + ' ' + units[i];
  },
  rate: bps => (bps == null ? DASH : fmt.bytes(bps) + '/s'),
  bits: bps => {                       // network throughput in bits, like Task Manager
    if (bps == null) return DASH;
    const b = bps * 8;
    if (b >= 1e9) return nf2.format(b / 1e9) + ' Gbps';
    if (b >= 1e6) return nf1.format(b / 1e6) + ' Mbps';
    if (b >= 1e3) return nf0.format(b / 1e3) + ' Kbps';
    return nf0.format(b) + ' bps';
  },
  link: mbps => {
    if (!mbps) return DASH;
    return mbps >= 1000 ? nf1.format(mbps / 1000).replace('.0', '') + ' Gbps' : nf0.format(mbps) + ' Mbps';
  },
  duration: s => {
    if (s == null) return DASH;
    const d = Math.floor(s / 86400), hh = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
    if (d) return `${d} d ${hh} h`;
    if (hh) return `${hh} h ${m} min`;
    return `${m} min`;
  },
  hours: hrs => {
    if (hrs == null) return DASH;
    const days = hrs / 24;
    return `${nf0.format(hrs)} h` + (days >= 2 ? ` (${nf0.format(days)} days)` : '');
  },
  time: t => new Date(t * 1000).toLocaleTimeString('en-GB', { hour12: false }),
};

// ---------------------------------------------------------------- severity
// Shared thresholds so a value is judged the same way wherever it appears.
export function tempTone(kind, v) {
  if (v == null) return null;
  const limits = { cpu: [85, 95], gpu: [85, 95], hotspot: [98, 108], gpumem: [95, 105], drive: [70, 80] }[kind] || [85, 95];
  if (v >= limits[1]) return 'crit';
  if (v >= limits[0]) return 'warn';
  return 'good';
}
export function loadTone(pct, warn = 88, crit = 95) {
  if (pct == null) return null;
  return pct >= crit ? 'crit' : pct >= warn ? 'warn' : null;
}
const TONE_LABEL = { good: 'Normal', warn: 'Hot', crit: 'Critical' };

// ------------------------------------------------------------------- icons
const P = (d, extra = '') => `<path d="${d}" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" ${extra}/>`;
const ICONS = {
  overview: P('M3 10.5 10 4l7 6.5M5 9.5V16h10V9.5'),
  cpu: P('M6.5 6.5h7v7h-7zM8 3v2M12 3v2M8 15v2M12 15v2M3 8h2M3 12h2M15 8h2M15 12h2') + P('M4.5 4.5h11v11h-11z'),
  gpu: P('M2.5 6h13a1.5 1.5 0 0 1 1.5 1.5V13H2.5zM2.5 4v12M5 15.5h4') + '<circle cx="7.3" cy="9.5" r="1.9" fill="none" stroke="currentColor" stroke-width="1.6"/><circle cx="12.6" cy="9.5" r="1.9" fill="none" stroke="currentColor" stroke-width="1.6"/>',
  memory: P('M2.5 6h15v7h-15zM5 13v2.5M8 13v2.5M12 13v2.5M15 13v2.5M6 8.5v2M10 8.5v2M14 8.5v2'),
  storage: P('M3 5.5A1.5 1.5 0 0 1 4.5 4h11A1.5 1.5 0 0 1 17 5.5v9a1.5 1.5 0 0 1-1.5 1.5h-11A1.5 1.5 0 0 1 3 14.5zM3 11.5h14') + '<circle cx="14" cy="13.8" r=".9" fill="currentColor"/>',
  network: P('M10 16v-1M5.2 11.2a6.8 6.8 0 0 1 9.6 0M2.6 8.4a10.4 10.4 0 0 1 14.8 0M7.7 13.8a3.2 3.2 0 0 1 4.6 0'),
  power: P('M11 2.5 4.5 11H10l-1 6.5L15.5 9H10z'),
  processes: P('M3 5h14M3 10h14M3 15h9'),
  sensors: P('M2.5 10h3l2-5 3.5 10 2.5-7 1.5 2h2.5'),
  settings: P('M3.5 6h7M14.5 6h2M3.5 14h2M9.500 14h7') + '<circle cx="12.500" cy="6" r="2" fill="none" stroke="currentColor" stroke-width="1.6"/><circle cx="7.500" cy="14" r="2" fill="none" stroke="currentColor" stroke-width="1.6"/>',
  ok: '<circle cx="10" cy="10" r="7.2" fill="none" stroke="currentColor" stroke-width="1.6"/>' + P('M6.8 10.2 9 12.4l4.2-4.6'),
  warn: P('M10 3.2 17.4 16H2.6zM10 8v3.6') + '<circle cx="10" cy="13.7" r=".95" fill="currentColor"/>',
  crit: '<circle cx="10" cy="10" r="7.2" fill="none" stroke="currentColor" stroke-width="1.6"/>' + P('M10 6v5') + '<circle cx="10" cy="13.6" r=".95" fill="currentColor"/>',
  info: '<circle cx="10" cy="10" r="7.2" fill="none" stroke="currentColor" stroke-width="1.6"/>' + P('M10 9v5') + '<circle cx="10" cy="6.4" r=".95" fill="currentColor"/>',
  off: '<circle cx="10" cy="10" r="7.2" fill="none" stroke="currentColor" stroke-width="1.6"/>' + P('M6 10h8'),
  down: P('M10 4v11M5.5 10.5 10 15l4.5-4.500'),
  up: P('M10 16V5M5.5 9.5 10 5l4.5 4.500'),
  fan: '<circle cx="10" cy="10" r="1.6" fill="currentColor"/>' + P('M10 8.4C9 5.500 10.500 3 13 3.500c1 2.200-.500 4-3 4.900zM11.600 10c2.900-1 5.400.500 4.900 3-2.200 1-4-.500-4.900-3zM10 11.600c1 2.900-.500 5.400-3 4.900-1-2.200.500-4 3-4.900zM8.400 10c-2.900 1-5.400-.500-4.900-3 2.200-1 4 .500 4.900 3z'),
};
export function icon(name, size = 18) {
  const el = h('span.icon', { 'aria-hidden': 'true' });
  el.innerHTML = `<svg viewBox="0 0 20 20" width="${size}" height="${size}">${ICONS[name] || ''}</svg>`;
  return el;
}

// -------------------------------------------------------------- components
/** Status chip: icon + label, never colour alone. */
export function chip(tone, label) {
  const names = { good: 'ok', warn: 'warn', crit: 'crit', info: 'info', off: 'off' };
  return h(`span.chip.chip-${tone}`, icon(names[tone] || 'info', 14), h('span', label));
}
export function setChip(el, tone, label) {
  if (!el) return;
  const key = `${tone}|${label}`;
  if (el.dataset.key === key) return;
  el.dataset.key = key;
  el.replaceChildren(...(tone ? [chip(tone, label || TONE_LABEL[tone])] : []));
}

/** Horizontal meter. The fill carries severity; the track is the same hue, lighter. */
export function meter(color) {
  const fill = h('div.meter-fill');
  const el = h('div.meter', { style: { '--mc': `var(${color})` }, role: 'meter', 'aria-valuemin': 0, 'aria-valuemax': 100 }, fill);
  el.set = (pct, tone) => {
    const v = pct == null ? 0 : Math.max(0, Math.min(100, pct));
    fill.style.width = v + '%';
    el.setAttribute('aria-valuenow', Math.round(v));
    el.dataset.tone = tone || '';
  };
  return el;
}

/** Stat tile: label, value, optional unit/sub line. Returns element with refs. */
export function tile(label, opts = {}) {
  const value = h('div.tile-value', DASH);
  const sub = h('div.tile-sub');
  const status = h('div.tile-status');
  const el = h('div.tile', { class: opts.class }, h('div.tile-label', label), value, sub, status);
  el.value = value; el.sub = sub; el.status = status;
  el.set = (v, s) => { setText(value, v); if (s !== undefined) setText(sub, s); };
  return el;
}

export function card(title, ...children) {
  return h('section.card', title ? h('h2.card-title', title) : null, ...children);
}

/** Definition list rendered as a two-column grid. */
export function facts(pairs) {
  const dl = h('dl.facts');
  dl.refs = {};
  for (const [key, label, value] of pairs) {
    const dd = h('dd', value ?? DASH);
    dl.refs[key] = dd;
    dl.append(h('dt', label), dd);
  }
  return dl;
}

/** 3D hardware illustration; ships a dark-surface and a light-surface cut. */
export function art(name, cls = '') {
  const img = (variant, path) => h(`img.art-${variant}`, { src: path, alt: '', decoding: 'async', onerror: e => e.target.remove() });
  return h('span.art', { class: cls, 'aria-hidden': 'true' },
    img('d', `assets/hw/${name}.webp`), img('l', `assets/hw/light/${name}.webp`));
}

export function showDialog(title, bodyNodes) {
  const dlg = document.getElementById('dialog');
  setText(document.getElementById('dialog-title'), title);
  document.getElementById('dialog-body').replaceChildren(...[].concat(bodyNodes));
  dlg.showModal();
}

export function unavailable(text, onWhy) {
  return h('span.na', text, onWhy ? h('button.linkbtn', { type: 'button', onclick: onWhy }, 'Why?') : null);
}
