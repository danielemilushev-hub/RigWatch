import { h, setText, fmt, icon, prefs } from './ui.js';
import { pages, buildMini } from './pages.js';

const MAX_POINTS = 900;
const state = {
  static: null,
  snap: null,
  hist: { t: [], s: {} },
  settings: { interval: 1, temp_unit: 'C', theme: 'system', window: 60, always_on_top: false, taskbar_strip: true, privacy: false },
  page: 'overview',
};

const NAV = [
  ['overview', 'Overview', s => (s.status === 'ok' ? '' : String(s.alerts.length))],
  ['cpu', 'Processor', s => fmt.pct(s.cpu.util)],
  ['gpu', 'Graphics', s => (s.gpus[0] && s.gpus[0].util != null ? fmt.pct(s.gpus[0].util) : '')],
  ['memory', 'Memory', s => fmt.pct(s.memory.pct)],
  ['storage', 'Storage', s => { const a = Math.max(0, ...s.drives.map(d => d.active_pct || 0)); return fmt.pct(a); }],
  ['network', 'Network', s => (s.network.primary ? fmt.bits(s.network.rx_bps) : '')],
  ['processes', 'Apps', s => fmt.int(s.cpu.processes)],
  ['sensors', 'All sensors', () => ''],
];
const NAV_FOOT = [['settings', 'Settings', () => '']];
const navRefs = {};
const instances = {};
let current = null;
let mini = null;
const forcedMini = new URLSearchParams(location.search).has('mini');

const $ = id => document.getElementById(id);

// ------------------------------------------------------------------ shell
function buildNav() {
  const make = ([key, label, value]) => {
    const val = h('span.nav-val');
    const a = h('a.nav-item', { href: `#/${key}`, 'data-key': key }, icon(key === 'processes' ? 'processes' : key, 18), h('span.nav-label', label), val);
    navRefs[key] = { a, val, value };
    return h('li', a);
  };
  $('nav').replaceChildren(...NAV.map(make));
  $('nav-foot').replaceChildren(...NAV_FOOT.map(make));
}

function buildRange() {
  const opts = [[60, '1 min'], [300, '5 min'], [900, '15 min']];
  $('range').replaceChildren(...opts.map(([v, label]) => h('button', {
    type: 'button', 'data-v': v, 'aria-pressed': String(state.settings.window === v),
    onclick: () => saveSettings({ window: v }),
  }, label)));
}

function applySettings() {
  const s = state.settings;
  prefs.tempUnit = s.temp_unit;
  if (prefs.privacy !== !!s.privacy) {          // pages bake names into their headers: rebuild them
    prefs.privacy = !!s.privacy;
    for (const key of Object.keys(instances)) { if (instances[key].hide) instances[key].hide(); delete instances[key]; }
    current = null;
    if (state.static) { show(); return; }
  }
  if (s.theme === 'system') document.documentElement.removeAttribute('data-theme');
  else document.documentElement.setAttribute('data-theme', s.theme);
  for (const b of $('range').children) b.setAttribute('aria-pressed', String(Number(b.dataset.v) === s.window));
  render();
}

async function saveSettings(changes) {
  Object.assign(state.settings, changes);
  applySettings();
  try {
    const res = await fetch('/api/settings', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(changes) });
    Object.assign(state.settings, await res.json());
  } catch { /* offline: keep the local choice */ }
}

const ctx = { state, win: () => state.settings.window, saveSettings };

// ----------------------------------------------------------------- routing
function route() {
  const key = (location.hash.replace(/^#\/?/, '') || 'overview').split('/')[0];
  state.page = pages[key] ? key : 'overview';
  show();
}

function show() {
  if (!state.static) return;
  const key = state.page;
  if (current && current.hide) current.hide();
  let inst = instances[key];
  if (!inst) inst = instances[key] = pages[key](ctx);
  current = inst;
  $('view').replaceChildren(inst.el);
  setText($('page-title'), inst.title);
  setText($('page-sub'), inst.sub || '');
  $('range').hidden = !inst.range;
  for (const [k, ref] of Object.entries(navRefs)) {
    if (k === key) ref.a.setAttribute('aria-current', 'page'); else ref.a.removeAttribute('aria-current');
  }
  $('main').scrollTop = 0;
  render();
}

function render() {
  const s = state.snap;
  if (!s || !s.ready || !state.static) return;
  for (const ref of Object.values(navRefs)) setText(ref.val, ref.value(s));
  navRefs.overview.a.dataset.alert = s.status === 'ok' ? '' : s.status;
  if (mini && !$('mini').hidden) mini.update(s);
  if (current && document.body.dataset.mode !== 'mini') {
    try { current.update(s); } catch (err) { console.error(err); }
  }
}

// -------------------------------------------------------------------- data
function pushHistory(s) {
  const H = state.hist;
  if (H.t.length && s.t <= H.t[H.t.length - 1]) return;
  const n = H.t.length;
  H.t.push(s.t);
  for (const [k, v] of Object.entries(s.hist || {})) {
    if (!H.s[k]) H.s[k] = new Array(n).fill(null);
    H.s[k].push(v);
  }
  for (const k in H.s) if (H.s[k].length < H.t.length) H.s[k].push(null);
  if (H.t.length > MAX_POINTS) { H.t.shift(); for (const k in H.s) H.s[k].shift(); }
}

function setLive(stateName, text) {
  $('live').dataset.state = stateName;
  setText($('live-text'), text);
}

function connect() {
  const es = new EventSource('/api/stream');
  es.onopen = () => setLive('on', 'Live');
  es.onmessage = e => {
    const s = JSON.parse(e.data);
    if (!s.ready) return;
    state.snap = s;
    pushHistory(s);
    const every = s.interval >= 1 ? `${s.interval} s` : `${Math.round(s.interval * 1000)} ms`;
    setLive('on', `Live · every ${every}`);
    render();
  };
  es.onerror = () => setLive('off', 'Reconnecting…');
}

async function getJson(url) { return (await fetch(url)).json(); }

async function waitForStatic() {
  for (;;) {
    try {
      const st = await getJson('/api/static');
      if (st.ready) return st;
    } catch { /* service still starting */ }
    await new Promise(r => setTimeout(r, 300));
  }
}

// --------------------------------------------------------------- mini mode
async function setMini(on) {
  if (on && !forcedMini) {
    let native = false;
    try {
      const res = await fetch('/api/window', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ mini: true }) });
      native = (await res.json()).ok;
    } catch { /* ignore */ }
    if (!native) {                      // running in a browser tab: open a small companion window
      window.open('/?mini=1', 'rigwatch-mini', 'width=380,height=440,menubar=no,toolbar=no');
      return;
    }
  }
  if (!on && !forcedMini) {
    fetch('/api/window', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ mini: false }) }).catch(() => {});
  }
  if (!on && forcedMini) { window.close(); return; }
  if (!mini) {
    mini = buildMini(ctx, () => setMini(false));
    $('mini').replaceChildren(mini.el);
  }
  document.body.dataset.mode = on ? 'mini' : '';
  $('mini').hidden = !on;
  render();
}

// -------------------------------------------------------------------- boot
async function boot() {
  buildNav();
  buildRange();
  setLive('off', 'Starting sensors…');
  $('view').replaceChildren(h('div.loading', h('div.loading-mark'), h('p', 'Reading your hardware…')));
  try { Object.assign(state.settings, await getJson('/api/settings')); } catch { /* defaults */ }
  applySettings();
  state.static = await waitForStatic();
  try {
    const hist = await getJson('/api/history');
    state.hist.t = hist.t;
    state.hist.s = hist.series;
  } catch { /* start empty */ }
  window.addEventListener('hashchange', route);
  $('mini-btn').addEventListener('click', () => setMini(true));
  route();
  connect();
  if (forcedMini) setMini(true);
}

boot();
