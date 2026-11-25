// Every page is built once (DOM + references) and then only updated in place.
// Single-series charts use the component's identity colour; charts with several
// series use the palette slots --s1/--s2/--s3 in fixed order.

import { h, setText, fmt, DASH, icon, chip, setChip, meter, tile, card, facts, art, showDialog,
         tempTone, loadTone, prefs, ident } from './ui.js';
import { TimeChart, drawSpark } from './chart.js';
import { cpuTempDialog, helperSetting, helperSummary, HELPER_SOURCE } from './helper.js';

const GB = 1024 ** 3;
const tail = (arr, n) => (arr ? arr.slice(Math.max(0, arr.length - n)) : []);

function shortCpu(name) {
  return (name || 'Processor').replace(/\(R\)|\(TM\)/gi, '').replace(/\s+\d+-Core Processor/i, '')
    .replace(/\s+CPU\s+@.*$/i, '').replace(/\s+/g, ' ').trim();
}
function shortGpu(name) { return (name || 'Graphics').replace(/^(AMD|NVIDIA|Intel\(R\)|Intel)\s+/i, '').trim(); }
const pctUnit = v => fmt.pct(v);

function stat(label) {
  const v = h('span.stat-v', DASH);
  const el = h('div.stat', h('span.stat-l', label), v);
  el.v = v;
  return el;
}

// Explains why the reading is missing and offers to switch the sensor helper on.
function whyNoCpuTemp() { cpuTempDialog(); }
const tempFrom = source => (source === HELPER_SOURCE ? 'Read from the processor’s own sensor' : `From ${source}`);

function chartCard(title, opts, note) {
  const host = h('div');
  const el = card(title, host, note ? h('p.card-note', note) : null);
  el.chart = new TimeChart(host, { label: title, ...opts });
  return el;
}

function pageHead({ artName, color, title, lines }) {
  const sub = h('p.ph-sub');
  const el = h('section.page-head', { style: { '--accent': `var(${color})` } },
    art(artName, 'ph-art'),
    h('div.ph-text', h('div.ph-kicker', h('i.dot'), lines.kicker), h('h2.ph-title', title), sub));
  setText(sub, lines.sub);
  el.sub = sub;
  return el;
}

function procList(emptyText) {
  const el = h('div.plist');
  el.set = rows => {
    if (!rows.length) { el.replaceChildren(h('p.muted', emptyText)); return; }
    el.replaceChildren(...rows.map(r => h('div.prow',
      h('span.prow-name', r.name, r.count > 1 ? h('span.count', '×' + r.count) : null),
      h('span.prow-bar', h('i', { style: { width: Math.max(1, Math.min(100, r.share)) + '%' } })),
      h('span.prow-val', r.value), h('span.prow-sub', r.sub || ''))));
  };
  return el;
}

// ================================================================ OVERVIEW
function overview(ctx) {
  const st = ctx.state.static;
  const heroIcon = h('span.hero-icon');
  const heroTitle = h('h2.hero-title', 'Reading sensors…');
  const heroSub = h('p.hero-sub');
  const alerts = h('ul.hero-alerts');
  const hsUptime = stat('Uptime'), hsPower = stat('Measured power'), hsProcs = stat('Processes');
  const hero = h('section.hero', { role: 'status' },
    h('div.hero-text', h('div.hero-head', heroIcon, heroTitle), heroSub, alerts),
    h('div.hero-stats', hsUptime, hsPower, hsProcs));
  const parts = [shortCpu(st.cpu.name), ...st.gpus.map(g => shortGpu(g.name)),
                 `${Math.round(st.memory.total_bytes / GB)} GB memory`, st.os.name];
  setText(heroSub, parts.filter(Boolean).join('  ·  '));

  const grid = h('div.grid.cc-grid');
  function cc({ label, name, artName, color, page }) {
    const value = h('span.cc-value', DASH), unit = h('span.cc-unit'), sub = h('div.cc-sub');
    const spark = h('canvas.cc-spark'), stats = h('div.cc-stats'), extra = h('div.cc-extra');
    const el = h('a.card.cc', { href: `#/${page}`, style: { '--accent': `var(${color})` }, 'aria-label': `${label}: open details` },
      art(artName, 'cc-art'),
      h('div.cc-head', h('i.dot'), h('span.cc-label', label)),
      h('div.cc-name', name || ''),
      h('div.cc-main', value, unit), sub, extra, spark, stats);
    grid.append(el);
    return { el, value, unit, sub, spark, stats, extra, color };
  }

  // CPU
  const cCpu = cc({ label: 'Processor', name: shortCpu(st.cpu.name), artName: 'cpu', color: '--c-cpu', page: 'cpu' });
  const cpuPower = stat('Power'), cpuClock = stat('Clock'), cpuTemp = stat('Temperature');
  cCpu.stats.append(cpuPower, cpuClock, cpuTemp);
  setText(cCpu.unit, '%'); setText(cCpu.sub, 'utilisation');

  // GPUs
  const cGpus = st.gpus.map(g => {
    const c = cc({ label: st.gpus.length > 1 ? `Graphics ${g.index + 1}` : 'Graphics', name: shortGpu(g.name), artName: 'gpu', color: '--c-gpu', page: 'gpu' });
    c.temp = stat('Temperature'); c.power = stat('Power'); c.vram = stat('Video memory'); c.fan = stat('Fan');
    c.stats.append(c.temp, c.power, c.vram, c.fan);
    setText(c.unit, '%'); setText(c.sub, 'utilisation');
    return c;
  });

  // Memory
  const cMem = cc({ label: 'Memory', name: memoryName(st), artName: 'ram', color: '--c-mem', page: 'memory' });
  const memMeter = meter('--c-mem');
  cMem.extra.append(memMeter);
  const memAvail = stat('Available'), memCommit = stat('Committed');
  cMem.stats.append(memAvail, memCommit);
  setText(cMem.unit, '%');

  // Storage
  const cDisk = cc({ label: 'Storage', name: '', artName: 'ssd', color: '--c-disk', page: 'storage' });
  const volRows = h('div.vols');
  cDisk.extra.append(volRows);
  const dTemp = stat('Temperature'), dHealth = stat('Drive health'), dAct = stat('Activity');
  cDisk.stats.append(dTemp, dHealth, dAct);
  setText(cDisk.unit, '%');

  // Network
  const cNet = cc({ label: 'Network', name: '', artName: 'network', color: '--c-net', page: 'network' });
  const nLink = stat('Link speed'), nIp = stat('Address'), nUp = stat('Upload');
  cNet.stats.append(nUp, nLink, nIp);

  // Power
  const cPwr = cc({ label: 'Power', name: 'Measured draw', artName: 'power', color: '--c-pwr', page: 'sensors' });
  const pCpu = stat('Processor'), pGpu = stat('Graphics');
  cPwr.stats.append(pCpu, pGpu);
  setText(cPwr.unit, 'W');

  // lower row
  const temps = h('div.tlist');
  const procs = procList('No process data yet.');
  const lower = h('div.grid.two',
    card('Temperatures', temps),
    card('Busiest apps', procs, h('p.card-note', 'Share of total processor time, grouped by app.')));

  const el = h('div.page', hero, grid, lower);

  function tempRow(label, value, kind, extra) {
    const tone = tempTone(kind, value);
    return h('div.trow', h('span.trow-l', label),
      value == null ? (extra || h('span.na', 'Not available')) : h('span.trow-v', fmt.temp(value)),
      tone ? chip(tone, { good: 'Normal', warn: 'Hot', crit: 'Critical' }[tone]) : h('span'));
  }

  function update(s) {
    const H = ctx.state.hist;
    // hero
    const level = s.status;
    hero.dataset.level = level;
    heroIcon.replaceChildren(icon(level === 'ok' ? 'ok' : level === 'warning' ? 'warn' : 'crit', 26));
    setText(heroTitle, level === 'ok' ? 'Everything looks normal'
      : s.alerts.length === 1 ? s.alerts[0].message : `${s.alerts.length} things need attention`);
    const key = s.alerts.map(a => a.message).join('|');
    if (alerts.dataset.key !== key) {
      alerts.dataset.key = key;
      alerts.replaceChildren(...(s.alerts.length > 1 ? s.alerts.map(a =>
        h('li', icon(a.level === 'critical' ? 'crit' : 'warn', 14), a.message)) : []));
    }
    setText(hsUptime.v, fmt.duration(s.cpu.uptime_s));
    setText(hsPower.v, fmt.watts(s.power.total_w));
    setText(hsProcs.v, fmt.int(s.cpu.processes));

    // cpu
    setText(cCpu.value, fmt.int(s.cpu.util));
    setText(cpuPower.v, fmt.watts(s.cpu.power_w));
    setText(cpuClock.v, fmt.clock(s.cpu.freq_mhz));
    if (s.cpu.temp_c == null) {
      if (!cpuTemp.v.dataset.na) {
        cpuTemp.v.dataset.na = '1';
        cpuTemp.v.replaceChildren(h('button.linkbtn', { type: 'button',
          onclick: e => { e.preventDefault(); e.stopPropagation(); whyNoCpuTemp(s.cpu.temp_reason); } }, 'Not available'));
      }
    } else { delete cpuTemp.v.dataset.na; setText(cpuTemp.v, fmt.temp(s.cpu.temp_c)); }
    drawSpark(cCpu.spark, tail(H.s['cpu.util'], 60), '--c-cpu', 100);

    s.gpus.forEach((g, i) => {
      const c = cGpus[i]; if (!c) return;
      setText(c.value, g.util == null ? DASH : fmt.int(g.util));
      setText(c.temp.v, fmt.temp(g.temp_c));
      setText(c.power.v, fmt.watts(g.power_w));
      setText(c.vram.v, g.vram_used == null ? DASH : `${fmt.bytes(g.vram_used)} of ${fmt.bytes(g.vram_total, 0)}`);
      setText(c.fan.v, g.fan_rpm == null ? DASH : g.fan_rpm === 0 ? 'Stopped' : fmt.rpm(g.fan_rpm));
      drawSpark(c.spark, tail(H.s[`gpu${g.index}.util`], 60), '--c-gpu', 100);
    });

    const m = s.memory;
    setText(cMem.value, fmt.int(m.pct));
    setText(cMem.sub, `${fmt.bytes(m.used)} of ${fmt.bytes(m.total)} in use`);
    memMeter.set(m.pct, loadTone(m.pct));
    setText(memAvail.v, fmt.bytes(m.available));
    setText(memCommit.v, m.commit_used == null ? DASH : `${fmt.bytes(m.commit_used)} of ${fmt.bytes(m.commit_limit)}`);
    drawSpark(cMem.spark, tail(H.s['mem.pct'], 60), '--c-mem', 100);

    // storage: headline is the system volume; every volume gets a row
    const vols = s.volumes;
    const sys = vols.find(v => v.mount.toUpperCase().startsWith('C')) || vols[0];
    const sysDrive = s.drives.find(d => sys && d.volumes.includes(sys.mount)) || s.drives[0];
    setText(cDisk.value, sys ? fmt.int(sys.pct) : DASH);
    setText(cDisk.sub, sys ? `of ${sys.mount} used · ${fmt.bytes(sys.free)} free` : '');
    setText(cDisk.el.querySelector('.cc-name'), sysDrive ? sysDrive.model : '');
    const vkey = vols.map(v => `${v.mount}${v.pct}`).join('|');
    if (volRows.dataset.key !== vkey) {
      volRows.dataset.key = vkey;
      volRows.replaceChildren(...vols.slice(0, 4).map(v => {
        const mt = meter('--c-disk'); mt.set(v.pct, loadTone(v.pct, 90, 95));
        return h('div.vol', h('span.vol-name', v.mount), mt, h('span.vol-val', `${fmt.bytes(v.used)} / ${fmt.bytes(v.total)}`));
      }));
    }
    if (sysDrive) {
      setText(dTemp.v, fmt.temp(sysDrive.temp_c));
      setText(dHealth.v, sysDrive.health_pct == null ? DASH : fmt.pct(sysDrive.health_pct));
      setText(dAct.v, fmt.pct(sysDrive.active_pct));
      drawSpark(cDisk.spark, tail(H.s[`disk${sysDrive.index}.active`], 60), '--c-disk', 100);
    }

    // network
    const n = s.network;
    const prim = n.adapters.find(a => a.name === n.primary);
    const [rv, ru] = splitUnit(fmt.bits(n.rx_bps));
    setText(cNet.value, rv); setText(cNet.unit, ru);
    setText(cNet.sub, 'download');
    setText(cNet.el.querySelector('.cc-name'), prim ? prim.name : 'No active connection');
    setText(nUp.v, fmt.bits(n.tx_bps));
    setText(nLink.v, prim ? fmt.link(prim.speed_mbps) : DASH);
    setText(nIp.v, prim && prim.ipv4[0] ? ident(prim.ipv4[0]) : DASH);
    drawSpark(cNet.spark, tail(H.s['net.rx'], 60), '--c-net');

    // power
    const p = s.power;
    setText(cPwr.value, p.total_w == null ? DASH : fmt.int(p.total_w));
    setText(cPwr.sub, p.scope ? `${p.scope}, measured` : 'No power sensors on this system');
    setText(pCpu.v, fmt.watts(p.cpu_w)); setText(pGpu.v, fmt.watts(p.gpu_w));
    drawSpark(cPwr.spark, tail(H.s['power.total'], 60), '--c-pwr');

    // temperatures
    const rows = [];
    rows.push(tempRow('Processor', s.cpu.temp_c, 'cpu',
      h('button.linkbtn', { type: 'button', onclick: () => whyNoCpuTemp(s.cpu.temp_reason) }, 'Not available — turn it on')));
    for (const g of s.gpus) {
      const nm = s.gpus.length > 1 ? shortGpu(g.name) : 'Graphics card';
      rows.push(tempRow(nm, g.temp_c, 'gpu'));
      if (g.temp_hotspot_c != null) rows.push(tempRow(`${nm} hotspot`, g.temp_hotspot_c, 'hotspot'));
      if (g.temp_mem_c != null) rows.push(tempRow(`${nm} memory`, g.temp_mem_c, 'gpumem'));
    }
    for (const d of s.drives) if (d.temp_c != null) rows.push(tempRow(d.model, d.temp_c, 'drive'));
    temps.replaceChildren(...rows);

    const top = [...s.processes].sort((a, b) => b.cpu - a.cpu).slice(0, 6);
    const maxCpu = Math.max(5, ...top.map(r => r.cpu));
    procs.set(top.map(r => ({ name: r.name.replace(/\.exe$/i, ''), count: r.count, share: (r.cpu / maxCpu) * 100,
      value: fmt.pct(r.cpu, 1), sub: fmt.bytes(r.mem) })));
  }
  return { el, update, title: 'Overview', sub: `${ident(st.os.hostname)} · ${st.os.name}` };
}

function splitUnit(text) {
  const i = text.lastIndexOf(' ');
  return i === -1 ? [text, ''] : [text.slice(0, i), text.slice(i + 1)];
}
function memoryName(st) {
  const mods = st.memory.modules || [];
  if (!mods.length) return `${Math.round(st.memory.total_bytes / GB)} GB`;
  const type = mods[0].type || '';
  const speed = mods[0].configured_speed || mods[0].rated_speed;
  return `${Math.round(st.memory.total_bytes / GB)} GB ${type}${speed ? `-${speed}` : ''}`.trim();
}

// ===================================================================== CPU
function cpu(ctx) {
  const st = ctx.state.static;
  const c = st.cpu;
  const head = pageHead({ artName: 'cpu', color: '--c-cpu', title: shortCpu(c.name), lines: { kicker: 'Processor',
    sub: [`${c.cores} cores`, `${c.threads} threads`, c.socket ? `Socket ${c.socket}` : null,
          c.base_mhz ? `Base ${fmt.clock(c.base_mhz)}` : null].filter(Boolean).join('  ·  ') } });

  const tUtil = tile('Utilisation', { class: 'tile-hero' });
  const tClock = tile('Clock'), tPower = tile('Package power'), tTemp = tile('Temperature');
  const tProc = tile('Processes'), tThreads = tile('Threads'), tHandles = tile('Handles'), tUp = tile('Uptime');
  const tiles = h('div.tiles', tUtil, tClock, tPower, tTemp, tProc, tThreads, tHandles, tUp);

  const cUtil = chartCard('Utilisation', { series: [{ key: 'cpu.util', label: 'Utilisation', color: '--c-cpu' }], max: 100, format: pctUnit, height: 200 });
  const cPower = chartCard('Package power', { series: [{ key: 'cpu.power', label: 'Package power', color: '--c-cpu' }], format: fmt.watts, tick: v => fmt.int(v) + ' W', floor: 20 },
    'Measured by the processor’s own energy counters (RAPL).');
  const cClock = chartCard('Clock speed', { series: [{ key: 'cpu.freq', label: 'Average clock', color: '--c-cpu' }], format: fmt.clock, tick: v => (v / 1000).toFixed(1) + ' GHz', floor: 1000 },
    'Average effective frequency across all threads.');
  // Shown once a temperature has been read (needs the sensor helper on most desktops).
  const cTemp = chartCard('Temperature', { series: [{ key: 'cpu.temp', label: 'Temperature', color: '--c-cpu' }],
    format: v => fmt.temp(v), tick: v => fmt.int(fmt.tempValue(v)) + '°', floor: 80 });
  cTemp.hidden = true;

  // per-thread bars
  const bars = h('div.bars', { role: 'img', 'aria-label': 'Utilisation per thread' });
  const fills = [];
  const perCore = c.smt ? Math.round(c.threads / c.cores) : 1;
  for (let i = 0; i < c.threads; i++) {
    const f = h('i.bar-fill');
    fills.push(f);
    const showLabel = c.threads <= 24 || i % perCore === 0;
    bars.append(h('div.bar', { class: perCore > 1 && i % perCore === 0 && i ? 'bar-gap' : '' },
      h('div.bar-track', f), h('div.bar-label', showLabel ? String(i) : '')));
  }
  const coreTable = h('table.table');
  const coreBody = h('tbody');
  coreTable.append(h('thead', h('tr', h('th', 'Core'), h('th.num', 'Load'), h('th.num', 'Clock'), h('th.num', 'Power'))), coreBody);
  const threadsCard = card('Load per thread', bars,
    h('p.card-note', perCore > 1 ? `Each group of ${perCore} bars is one physical core.` : 'One bar per thread.'));
  const coresCard = card('Cores', h('div.table-wrap', coreTable));

  const el = h('div.page', head, tiles, cUtil, cTemp, h('div.grid.two', cPower, cClock), h('div.grid.two', threadsCard, coresCard));

  function update(s) {
    const d = s.cpu;
    tUtil.set(fmt.pct(d.util), d.util_source === 'psutil' ? '' : 'Same measure as Task Manager');
    tClock.set(fmt.clock(d.freq_mhz), d.freq_max_mhz ? `Fastest thread ${fmt.clock(d.freq_max_mhz)}` : '');
    tPower.set(fmt.watts(d.power_w), d.power_w == null ? 'No energy meter on this processor' : 'Measured');
    if (d.temp_c == null) {
      if (!tTemp.dataset.na) {
        tTemp.dataset.na = '1';
        tTemp.value.replaceChildren(h('span.na', 'Not available'));
        tTemp.sub.replaceChildren(h('button.linkbtn', { type: 'button', onclick: () => whyNoCpuTemp(d.temp_reason) }, 'Why, and how to turn it on'));
        setChip(tTemp.status, null);
      }
    } else {
      delete tTemp.dataset.na;
      tTemp.set(fmt.temp(d.temp_c), tempFrom(d.temp_source));
      setChip(tTemp.status, tempTone('cpu', d.temp_c));
    }
    cTemp.hidden = d.temp_c == null && !(ctx.state.hist.s['cpu.temp'] || []).some(v => v != null);
    tProc.set(fmt.int(d.processes)); tThreads.set(fmt.int(d.threads)); tHandles.set(fmt.int(d.handles));
    tUp.set(fmt.duration(d.uptime_s));

    d.per_thread.forEach((v, i) => {
      const f = fills[i]; if (!f) return;
      f.style.height = Math.max(1.5, v) + '%';
      f.parentNode.parentNode.title = `Thread ${i}: ${fmt.pct(v)}`;
    });
    const rows = [];
    for (let k = 0; k < c.cores; k++) {
      const th = d.per_thread.slice(k * perCore, (k + 1) * perCore);
      const fr = d.per_thread_freq.slice(k * perCore, (k + 1) * perCore);
      const load = th.length ? th.reduce((a, b) => a + b, 0) / th.length : null;
      const clock = fr.length ? Math.max(...fr) : null;
      const pw = d.per_core_power_w[k];
      rows.push(h('tr', h('td', `Core ${k}`), h('td.num', fmt.pct(load)), h('td.num', fmt.clock(clock)),
        h('td.num', pw == null ? DASH : pw.toFixed(1) + ' W')));
    }
    coreBody.replaceChildren(...rows);

    const H = ctx.state.hist, w = ctx.win();
    cUtil.chart.setData(H, w); cPower.chart.setData(H, w); cClock.chart.setData(H, w);
    if (!cTemp.hidden) cTemp.chart.setData(H, w);
  }
  return { el, update, title: 'Processor', sub: shortCpu(c.name), range: true };
}

// ===================================================================== GPU
function gpu(ctx) {
  const st = ctx.state.static;
  if (!st.gpus.length) {
    return { el: h('div.page', card(null, h('p.muted', 'No graphics adapter was found on this system.'))), update() {}, title: 'Graphics', sub: '' };
  }
  const sections = st.gpus.map(g => {
    const p = `gpu${g.index}`;
    const head = pageHead({ artName: 'gpu', color: '--c-gpu', title: g.name, lines: { kicker: 'Graphics',
      sub: [`${fmt.bytes(g.vram_bytes, 0)} video memory`, g.driver && g.driver.version ? `Driver ${g.driver.version}` : null,
            `Telemetry: ${g.telemetry}`].filter(Boolean).join('  ·  ') } });
    const tUtil = tile('Utilisation', { class: 'tile-hero' });
    const tTemp = tile('Temperature'), tHot = tile('Hotspot'), tPower = tile('Power draw'), tFan = tile('Fan');
    const tVram = tile('Video memory'), tCore = tile('Core clock'), tMem = tile('Memory clock'), tVolt = tile('Core voltage');
    const vramMeter = meter('--c-gpu');
    tVram.append(vramMeter);
    const tiles = h('div.tiles', tUtil, tTemp, tHot, tPower, tFan, tVram, tCore, tMem, tVolt);

    const cUtil = chartCard('Utilisation', { series: [{ key: `${p}.util`, label: 'Utilisation', color: '--c-gpu' }], max: 100, format: pctUnit, height: 200 });
    const cTemp = chartCard('Temperatures', { series: [
      { key: `${p}.temp`, label: 'GPU', color: '--s1' }, { key: `${p}.hotspot`, label: 'Hotspot', color: '--s2' },
      { key: `${p}.temp_mem`, label: 'Memory', color: '--s3' }],
      format: v => fmt.temp(v), tick: v => fmt.int(fmt.tempValue(v)) + '°', floor: 80 });
    const cPower = chartCard('Power draw', { series: [{ key: `${p}.power`, label: 'Power draw', color: '--c-gpu' }], format: fmt.watts, tick: v => fmt.int(v) + ' W', floor: 50 });
    const cVram = chartCard('Video memory in use', { series: [{ key: `${p}.vram_pct`, label: 'In use', color: '--c-gpu' }], max: 100, format: pctUnit });

    const engines = h('div.hbars');
    const gprocs = procList('Nothing is using the graphics card right now.');
    const el = h('div.gpu-section', head, tiles, cUtil, h('div.grid.two', cTemp, cPower),
      h('div.grid.two', cVram, card('Workload by engine', engines, h('p.card-note', 'Which part of the card is busy: rendering, compute, video or copy.'))),
      card('Apps using this card', gprocs));

    function update(s) {
      const d = s.gpus.find(x => x.index === g.index);
      if (!d) return;
      tUtil.set(fmt.pct(d.util), 'Busiest engine, as in Task Manager');
      tTemp.set(fmt.temp(d.temp_c), d.temp_source ? `From ${d.temp_source}` : '');
      setChip(tTemp.status, tempTone('gpu', d.temp_c));
      tHot.set(fmt.temp(d.temp_hotspot_c), d.temp_mem_c != null ? `Memory ${fmt.temp(d.temp_mem_c)}` : '');
      setChip(tHot.status, tempTone('hotspot', d.temp_hotspot_c));
      tPower.set(fmt.watts(d.power_w), d.power_w == null ? 'Not reported by this card' : d.power_kind === 'board' ? 'Whole card, measured' : 'Graphics chip only, measured');
      tFan.set(d.fan_rpm == null ? (d.fan_pct == null ? DASH : fmt.pct(d.fan_pct)) : d.fan_rpm === 0 ? 'Stopped' : fmt.rpm(d.fan_rpm),
        d.fan_rpm === 0 ? 'Zero-RPM mode while cool' : d.fan_pct != null && d.fan_rpm != null ? `${fmt.pct(d.fan_pct)} duty` : '');
      tVram.set(d.vram_used == null ? DASH : fmt.bytes(d.vram_used), d.vram_total ? `of ${fmt.bytes(d.vram_total, 0)} · ${fmt.pct(d.vram_pct)}` : '');
      vramMeter.set(d.vram_pct, loadTone(d.vram_pct, 90, 97));
      tCore.set(fmt.clock(d.core_clock_mhz)); tMem.set(fmt.clock(d.mem_clock_mhz));
      tVolt.set(fmt.volts(d.voltage_mv), d.pcie_lanes ? `PCIe ×${d.pcie_lanes}` : '');

      engines.replaceChildren(...(d.engines.length ? d.engines : [{ name: 'No engine data', util: 0 }]).map(e => h('div.hbar',
        h('span.hbar-l', e.name), h('span.hbar-t', h('i', { style: { width: Math.max(0.5, e.util) + '%' } })), h('span.hbar-v', fmt.pct(e.util)))));

      const rows = s.processes.filter(r => r.gpu > 0 || r.vram > 32 * 1024 * 1024)
        .sort((a, b) => b.gpu - a.gpu || b.vram - a.vram).slice(0, 8);
      const top = Math.max(5, ...rows.map(r => r.gpu));
      gprocs.set(rows.map(r => ({ name: r.name.replace(/\.exe$/i, ''), count: r.count, share: (r.gpu / top) * 100,
        value: fmt.pct(r.gpu, 1), sub: r.vram ? fmt.bytes(r.vram) : '' })));

      const H = ctx.state.hist, w = ctx.win();
      for (const c of [cUtil, cTemp, cPower, cVram]) c.chart.setData(H, w);
    }
    return { el, update };
  });
  const el = h('div.page', ...sections.map(s => s.el));
  return { el, update: s => sections.forEach(x => x.update(s)), title: 'Graphics', sub: st.gpus.map(g => g.name).join(' · '), range: true };
}

// ================================================================== MEMORY
function memory(ctx) {
  const st = ctx.state.static;
  const mods = st.memory.modules || [];
  const head = pageHead({ artName: 'ram', color: '--c-mem', title: memoryName(st), lines: { kicker: 'Memory',
    sub: mods.length ? `${mods.length} of ${st.memory.slots || mods.length} slots in use` : 'Module details not reported by firmware' } });
  const tUse = tile('In use', { class: 'tile-hero' });
  const tAvail = tile('Available'), tCommit = tile('Committed'), tCache = tile('Cached');
  const tPaged = tile('Paged pool'), tNon = tile('Non-paged pool');
  const useMeter = meter('--c-mem');
  tUse.append(useMeter);
  const tiles = h('div.tiles', tUse, tAvail, tCommit, tCache, tPaged, tNon);
  const cUse = chartCard('Memory in use', { series: [{ key: 'mem.pct', label: 'In use', color: '--c-mem' }], max: 100, format: pctUnit, height: 200 });

  const table = h('table.table');
  table.append(h('thead', h('tr', h('th', 'Slot'), h('th.num', 'Size'), h('th', 'Type'), h('th.num', 'Speed'), h('th', 'Module'))),
    h('tbody', mods.map(m => h('tr', h('td', m.slot), h('td.num', fmt.bytes(m.size_bytes, 0)), h('td', [m.type, m.form_factor].filter(Boolean).join(' ') || DASH),
      h('td.num', m.configured_speed ? `${m.configured_speed} MT/s` : m.rated_speed ? `${m.rated_speed} MT/s` : DASH),
      h('td', [m.manufacturer, m.part_number].filter(Boolean).join(' ') || DASH)))));
  const procs = procList('No process data yet.');
  const el = h('div.page', head, tiles, cUse,
    h('div.grid.two', card('Largest apps', procs, h('p.card-note', 'Private memory, grouped by app — the figure Task Manager shows.')),
      card('Installed modules', mods.length ? h('div.table-wrap', table) : h('p.muted', 'The firmware did not report module details.'))));

  function update(s) {
    const m = s.memory;
    tUse.set(fmt.pct(m.pct), `${fmt.bytes(m.used)} of ${fmt.bytes(m.total)}`);
    useMeter.set(m.pct, loadTone(m.pct));
    tAvail.set(fmt.bytes(m.available), 'Free for apps right now');
    tCommit.set(fmt.bytes(m.commit_used), m.commit_limit ? `of ${fmt.bytes(m.commit_limit)} limit · ${fmt.pct(m.commit_pct)}` : '');
    tCache.set(fmt.bytes(m.cached), 'Reusable file cache');
    tPaged.set(fmt.bytes(m.kernel_paged)); tNon.set(fmt.bytes(m.kernel_nonpaged));
    const top = [...s.processes].sort((a, b) => b.mem - a.mem).slice(0, 8);
    const max = Math.max(1, ...top.map(r => r.mem));
    procs.set(top.map(r => ({ name: r.name.replace(/\.exe$/i, ''), count: r.count, share: (r.mem / max) * 100,
      value: fmt.bytes(r.mem), sub: fmt.pct((r.mem / m.total) * 100, 1) })));
    cUse.chart.setData(ctx.state.hist, ctx.win());
  }
  return { el, update, title: 'Memory', sub: memoryName(st), range: true };
}

// ================================================================= STORAGE
function storagePage(ctx) {
  const st = ctx.state.static;
  const sections = st.drives.map(d => {
    const p = `disk${d.index}`;
    const head = pageHead({ artName: 'ssd', color: '--c-disk', title: d.model || `Drive ${d.index}`, lines: { kicker: d.kind || 'Drive',
      sub: [d.size_bytes ? fmt.bytesDec(d.size_bytes) : null, d.bus, d.firmware ? `Firmware ${d.firmware}` : null].filter(Boolean).join('  ·  ') } });
    const tTemp = tile('Temperature'), tHealth = tile('Drive health'), tAct = tile('Active time');
    const tRead = tile('Reading'), tWrite = tile('Writing');
    const tHours = tile('Powered on'), tWritten = tile('Written in total'), tCycles = tile('Power cycles');
    const tiles = h('div.tiles', tTemp, tHealth, tAct, tRead, tWrite, tHours, tWritten, tCycles);
    const vols = h('div.vols.vols-lg');
    const cIo = chartCard('Read and write speed', { series: [{ key: `${p}.read`, label: 'Read', color: '--s1' }, { key: `${p}.write`, label: 'Write', color: '--s2' }],
      format: fmt.rate, tick: v => fmt.bytes(v), floor: 1024 * 1024, base: 1024 });
    const cAct = chartCard('Active time', { series: [{ key: `${p}.active`, label: 'Active time', color: '--c-disk' }], max: 100, format: pctUnit });
    const el = h('div.gpu-section', head, tiles, card('Volumes on this drive', vols), h('div.grid.two', cIo, cAct));
    function update(s) {
      const x = s.drives.find(v => v.index === d.index);
      if (!x) return;
      tTemp.set(fmt.temp(x.temp_c), x.temp_c == null ? 'Not reported by this drive' : 'Read from the drive');
      setChip(tTemp.status, tempTone('drive', x.temp_c));
      tHealth.set(x.health_pct == null ? DASH : fmt.pct(x.health_pct), x.health_pct == null ? 'Wear data not available' : `Rated life remaining · spare ${fmt.pct(x.spare_pct)}`);
      setChip(tHealth.status, x.health_pct == null ? null : x.health_pct <= 10 ? 'crit' : x.health_pct <= 30 ? 'warn' : 'good',
        x.health_pct == null ? '' : x.health_pct <= 10 ? 'Replace soon' : x.health_pct <= 30 ? 'Worn' : 'Healthy');
      tAct.set(fmt.pct(x.active_pct), 'Time spent handling requests');
      tRead.set(fmt.rate(x.read_bps)); tWrite.set(fmt.rate(x.write_bps));
      tHours.set(x.power_on_hours == null ? DASH : fmt.int(x.power_on_hours) + ' h', x.power_on_hours == null ? '' : `${fmt.int(x.power_on_hours / 24)} days`);
      tWritten.set(fmt.bytesDec(x.data_written_bytes), x.data_read_bytes != null ? `Read ${fmt.bytesDec(x.data_read_bytes)}` : '');
      tCycles.set(fmt.int(x.power_cycles), x.unsafe_shutdowns != null ? `${fmt.int(x.unsafe_shutdowns)} unsafe shutdowns` : '');
      const mine = s.volumes.filter(v => v.disks.includes(d.index));
      const key = mine.map(v => v.mount + v.used).join('|');
      if (vols.dataset.key !== key) {
        vols.dataset.key = key;
        vols.replaceChildren(...(mine.length ? mine.map(v => {
          const mt = meter('--c-disk'); mt.set(v.pct, loadTone(v.pct, 90, 95));
          return h('div.vol', h('span.vol-name', `${v.mount}`, h('span.muted', ` ${v.fs}`)), mt,
            h('span.vol-val', `${fmt.bytes(v.used)} used of ${fmt.bytes(v.total)} · ${fmt.bytes(v.free)} free`));
        }) : [h('p.muted', 'No mounted volumes.')]));
      }
      cIo.chart.setData(ctx.state.hist, ctx.win()); cAct.chart.setData(ctx.state.hist, ctx.win());
    }
    return { el, update };
  });
  const el = h('div.page', ...(sections.length ? sections.map(s => s.el) : [card(null, h('p.muted', 'No drives found.'))]));
  return { el, update: s => sections.forEach(x => x.update(s)), title: 'Storage', sub: `${st.drives.length} drive${st.drives.length === 1 ? '' : 's'}`,
    range: true, sig: st.drives.map(d => d.index).join(',') };
}

// ================================================================= NETWORK
function network(ctx) {
  const head = pageHead({ artName: 'network', color: '--c-net', title: 'Network', lines: { kicker: 'Network', sub: '' } });
  const tDown = tile('Download', { class: 'tile-hero' }), tUp = tile('Upload');
  const tRecv = tile('Received'), tSent = tile('Sent'), tLink = tile('Link speed'), tIp = tile('IPv4 address');
  const tiles = h('div.tiles', tDown, tUp, tRecv, tSent, tLink, tIp);
  const cNet = chartCard('Throughput', { series: [{ key: 'net.rx', label: 'Download', color: '--s1' }, { key: 'net.tx', label: 'Upload', color: '--s2' }],
    format: fmt.bits, tick: fmt.bits, floor: 125000, scale: 8, base: 1000, height: 220 });
  const table = h('table.table');
  const body = h('tbody');
  table.append(h('thead', h('tr', h('th', 'Adapter'), h('th.num', 'Download'), h('th.num', 'Upload'), h('th.num', 'Link'), h('th', 'Address'), h('th', 'MAC'))), body);
  const el = h('div.page', head, tiles, cNet, card('All active adapters', h('div.table-wrap', table)));

  function update(s) {
    const n = s.network;
    const a = n.adapters.find(x => x.name === n.primary);
    setText(head.querySelector('.ph-title'), a ? a.name : 'No active connection');
    setText(head.sub, a ? 'Default route for internet traffic' : 'Connect a network to see activity');
    tDown.set(fmt.bits(n.rx_bps), a ? fmt.rate(n.rx_bps) : '');
    tUp.set(fmt.bits(n.tx_bps), a ? fmt.rate(n.tx_bps) : '');
    tRecv.set(a ? fmt.bytes(a.rx_session) : DASH, a ? `${fmt.bytes(a.rx_total)} since startup` : '');
    tSent.set(a ? fmt.bytes(a.tx_session) : DASH, a ? `${fmt.bytes(a.tx_total)} since startup` : '');
    tLink.set(a ? fmt.link(a.speed_mbps) : DASH, 'Negotiated with the router or switch');
    tIp.set(a && a.ipv4[0] ? ident(a.ipv4[0]) : DASH, a && a.mac ? ident(a.mac) : '');
    body.replaceChildren(...n.adapters.map(x => h('tr',
      h('td', x.name, x.primary ? h('span.count', 'default') : null), h('td.num', fmt.bits(x.rx_bps)), h('td.num', fmt.bits(x.tx_bps)),
      h('td.num', fmt.link(x.speed_mbps)), h('td', ident(x.ipv4[0]) || DASH), h('td', ident(x.mac) || DASH))));
    cNet.chart.setData(ctx.state.hist, ctx.win());
  }
  return { el, update, title: 'Network', sub: '', range: true };
}

// =============================================================== PROCESSES
function processes(ctx) {
  let sortKey = 'cpu', query = '';
  const search = h('input.input', { type: 'search', placeholder: 'Filter apps', 'aria-label': 'Filter apps' });
  const count = h('span.muted');
  const cols = [['name', 'App', false], ['cpu', 'Processor', true], ['mem', 'Memory', true], ['gpu', 'Graphics', true], ['vram', 'Video memory', true], ['threads', 'Threads', true]];
  const headRow = h('tr');
  const ths = {};
  for (const [key, label, num] of cols) {
    const btn = h('button.th-btn', { type: 'button', onclick: () => { sortKey = key; paint(); } }, label);
    const th = h('th', { class: num ? 'num' : '' }, btn);
    ths[key] = th; headRow.append(th);
  }
  const body = h('tbody');
  const table = h('table.table.table-proc', h('thead', headRow), body);
  const el = h('div.page', card(null, h('div.toolbar', search, count), h('div.table-wrap', table),
    h('p.card-note', 'Processes with the same name are grouped. Memory is private memory, as in Task Manager; graphics is the busiest engine each app is using.')));
  let last = null;
  search.addEventListener('input', () => { query = search.value.trim().toLowerCase(); paint(); });

  function paint() {
    if (!last) return;
    for (const [key] of cols) ths[key].setAttribute('aria-sort', key === sortKey ? (key === 'name' ? 'ascending' : 'descending') : 'none');
    let rows = last.processes;
    if (query) rows = rows.filter(r => r.name.toLowerCase().includes(query));
    rows = [...rows].sort((a, b) => sortKey === 'name' ? a.name.localeCompare(b.name) : b[sortKey] - a[sortKey]);
    setText(count, `${rows.length} app${rows.length === 1 ? '' : 's'} shown · ${fmt.int(last.cpu.processes)} processes running`);
    const total = last.memory.total || 1;
    body.replaceChildren(...rows.map(r => h('tr',
      h('td', h('span.pname', r.name.replace(/\.exe$/i, '')), r.count > 1 ? h('span.count', '×' + r.count) : null),
      h('td.num', fmt.pct(r.cpu, 1)),
      h('td.num', fmt.bytes(r.mem), h('span.share', fmt.pct((r.mem / total) * 100, 1))),
      h('td.num', r.gpu ? fmt.pct(r.gpu, 1) : h('span.muted', DASH)),
      h('td.num', r.vram ? fmt.bytes(r.vram) : h('span.muted', DASH)),
      h('td.num', fmt.int(r.threads)))));
  }
  function update(s) { last = s; if (document.activeElement !== search || !body.children.length || s.seq % 2 === 0) paint(); }
  return { el, update, title: 'Apps & processes', sub: '' };
}

// ================================================================= SENSORS
function sensors(ctx) {
  const st = ctx.state.static;
  let query = '', rows = [], since = null, timer = null;
  const search = h('input.input', { type: 'search', placeholder: 'Filter sensors', 'aria-label': 'Filter sensors' });
  const sinceEl = h('span.muted');
  const reset = h('button.btn', { type: 'button', onclick: async () => { await fetch('/api/sensors/reset', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' }); load(); } }, 'Reset min / max');
  const exportBtn = h('a.btn', { href: '/api/sensors.csv', download: 'rigwatch-sensors.csv' }, 'Export CSV');
  const histBtn = h('a.btn', { href: '/api/history.csv', download: 'rigwatch-history.csv' }, 'Export history');
  const body = h('tbody');
  const table = h('table.table.table-sensors', h('thead', h('tr', h('th', 'Sensor'), h('th.num', 'Now'), h('th.num', 'Min'), h('th.num', 'Max'), h('th.num', 'Average'), h('th', 'Source'))), body);
  search.addEventListener('input', () => { query = search.value.trim().toLowerCase(); paint(); });

  const sources = h('div.sources');
  for (const src of st.sources || []) {
    sources.append(h('div.source', chip(src.ok ? 'good' : 'off', src.ok ? 'Active' : 'Unavailable'),
      h('div', h('div.source-name', src.name), h('div.muted', src.detail))));
  }
  const cpuTempSrc = h('div.source');
  sources.append(cpuTempSrc);

  const maker = { 'Micro-Star International Co., Ltd.': 'MSI', 'ASUSTeK COMPUTER INC.': 'ASUS', 'Gigabyte Technology Co., Ltd.': 'Gigabyte' };
  const boardName = [maker[st.board.manufacturer] || st.board.manufacturer, st.board.product].filter(Boolean).join(' ') || ident(st.os.hostname);
  const head = pageHead({ artName: 'board', color: '--c-pwr', title: boardName, lines: { kicker: 'System',
    sub: [st.bios.version ? `BIOS ${st.bios.version}${st.bios.date ? ` (${st.bios.date})` : ''}` : null,
          `${st.os.name}${st.os.version ? ' ' + st.os.version : ''}`, `Build ${st.os.build}`].filter(Boolean).join('  ·  ') } });
  head.classList.add('ph-wide');
  const el = h('div.page', head,
    card(null, h('div.toolbar', search, sinceEl, h('span.spacer'), reset, exportBtn, histBtn), h('div.table-wrap', table)),
    card('Where these numbers come from', h('p.card-note.lead', 'Every value is read from the hardware or from Windows. Nothing is estimated: when a sensor cannot be read it is shown as not available.'), sources));

  const val = (r, v) => {
    if (v == null) return DASH;
    if (r.kind === 'temp') return fmt.temp(v);
    if (r.unit === '%') return v.toFixed(1) + '%';
    const digits = { W: 1, MHz: 0, RPM: 0, mV: 0, lanes: 0, MB: 0, GB: 1, 'MB/s': 2, 'Mbit/s': 2 }[r.unit];
    const d = digits != null ? digits : Math.abs(v) >= 100 ? 0 : 1;
    return new Intl.NumberFormat('en-US', { minimumFractionDigits: d, maximumFractionDigits: d }).format(v) + (r.unit ? ' ' + r.unit : '');
  };
  function paint() {
    const out = [];
    let group = null;
    for (const r of rows) {
      if (query && !(r.label + ' ' + r.group).toLowerCase().includes(query)) continue;
      if (r.value == null && r.min == null) continue;       // never produced a reading
      if (r.group !== group) { group = r.group; out.push(h('tr.group', h('th', { colspan: 6, scope: 'colgroup' }, group))); }
      out.push(h('tr', h('td', r.label), h('td.num.strong', val(r, r.value)), h('td.num', val(r, r.min)), h('td.num', val(r, r.max)),
        h('td.num', val(r, r.avg)), h('td.muted', r.source || '')));
    }
    body.replaceChildren(...out);
    setText(sinceEl, since ? `Min / max since ${fmt.time(since)}` : '');
  }
  async function load() {
    try {
      const data = await (await fetch('/api/sensors')).json();
      rows = data.rows; since = data.since; paint();
    } catch { /* keep the previous table */ }
  }
  function update(s) {
    if (!timer) { load(); timer = setInterval(load, 2000); }
    const hs = s.helper || { state: 'off' };
    const key = [s.cpu.temp_source || '', s.bridge.connected, s.bridge.source || '', hs.state, hs.sensors || 0].join('|');
    if (cpuTempSrc.dataset.key !== key) {
      cpuTempSrc.dataset.key = key;
      const has = s.bridge.connected || s.cpu.temp_c != null;
      const detail = hs.state === 'on' ? 'Read by the RigWatch sensor helper from the processor and the motherboard (LibreHardwareMonitor’s sensor engine, PawnIO driver).'
        : s.bridge.connected ? `Provided by ${s.bridge.source}.`
        : s.cpu.temp_c != null ? `Provided by ${s.cpu.temp_source}.`
        : hs.state === 'off' ? 'Windows keeps these behind administrator rights. The sensor helper reads them once you turn it on.'
        : helperSummary(hs);
      cpuTempSrc.replaceChildren(chip(has ? 'good' : 'off', has ? 'Active' : 'Unavailable'),
        h('div', h('div.source-name', 'CPU core temperature, case fans, voltages'), h('div.muted', detail),
          hs.state === 'on' ? null : h('button.linkbtn', { type: 'button', onclick: () => whyNoCpuTemp() }, hs.state === 'off' ? 'Turn it on' : 'Details')));
    }
  }
  function destroy() { clearInterval(timer); timer = null; }
  return { el, update, hide: destroy, title: 'All sensors', sub: 'Live values with minimum, maximum and average' };
}

// ================================================================ SETTINGS
function settingsPage(ctx) {
  const st = ctx.state.static;
  const S = () => ctx.state.settings;
  function select(key, options, parse = v => v) {
    const el = h('select.input', { 'aria-label': key }, options.map(([v, label]) => h('option', { value: v }, label)));
    el.value = String(S()[key]);
    el.addEventListener('change', () => ctx.saveSettings({ [key]: parse(el.value) }));
    return el;
  }
  const row = (label, hint, control) => h('div.setting', h('div', h('div.setting-l', label), h('div.muted', hint)), control);
  const onTop = h('input', { type: 'checkbox', class: 'switch', 'aria-label': 'Keep window on top' });
  onTop.checked = !!S().always_on_top;
  onTop.addEventListener('change', () => ctx.saveSettings({ always_on_top: onTop.checked }));
  const strip = h('input', { type: 'checkbox', class: 'switch', 'aria-label': 'Show the taskbar strip when minimised' });
  strip.checked = S().taskbar_strip !== false;
  strip.addEventListener('change', () => ctx.saveSettings({ taskbar_strip: strip.checked }));
  const privacy = h('input', { type: 'checkbox', class: 'switch', 'aria-label': 'Hide identifying details' });
  privacy.checked = !!S().privacy;
  privacy.addEventListener('change', () => ctx.saveSettings({ privacy: privacy.checked }));
  const sensorHelper = helperSetting();
  const base = location.origin;
  const api = [['/api/snapshot', 'Latest reading of every sensor'], ['/api/static', 'Hardware inventory'], ['/api/history', 'Recent history for the charts'],
    ['/api/sensors', 'Flat sensor list with min / max / average'], ['/api/stream', 'Live updates (server-sent events)'], ['/api/sensors.csv', 'Sensor table as CSV']];
  const el = h('div.page.narrow',
    card('Display',
      row('Refresh rate', 'How often sensors are read. Faster costs a little more processor time.',
        select('interval', [[0.5, 'Twice a second'], [1, 'Every second'], [2, 'Every 2 seconds'], [5, 'Every 5 seconds']], Number)),
      row('Temperature unit', 'Used everywhere temperatures are shown.', select('temp_unit', [['C', 'Celsius (°C)'], ['F', 'Fahrenheit (°F)']])),
      row('Theme', 'Follow Windows, or always use one.', select('theme', [['system', 'Match Windows'], ['dark', 'Dark'], ['light', 'Light']])),
      row('Keep on top', 'Keep the RigWatch window above other windows.', onTop),
      row('Taskbar strip', 'While RigWatch is minimised, show live numbers on the taskbar. Click the strip to reopen, drag it to move it, right-click for more.', strip),
      row('Hide identifying details', 'Hide the computer name and network addresses, for screenshots and streaming.', privacy)),
    card('Sensors', h('p.card-note.lead', 'Windows keeps a few sensors behind administrator rights. A small helper reads them for RigWatch; turning it on asks Windows for permission once.'),
      sensorHelper),
    card('For developers', h('p.card-note.lead', 'RigWatch serves its readings on this computer only (127.0.0.1). Use them in your own scripts, overlays or dashboards.'),
      h('div.table-wrap', h('table.table', h('tbody', api.map(([p, d]) => h('tr', h('td', h('code', base + p)), h('td.muted', d))))))),
    card('About', h('div.about-banner', { role: 'presentation' }), h('div.about', h('img.about-mark', { src: 'assets/icon.png', alt: '', onerror: e => e.target.remove() }),
      h('div', h('div.setting-l', `RigWatch ${st.version}`),
        h('div.muted', `${st.os.name}${st.os.version ? ' ' + st.os.version : ''} · build ${st.os.build}`),
        h('div.muted', [st.board.manufacturer, st.board.product].filter(Boolean).join(' ') + (st.bios.version ? ` · BIOS ${st.bios.version}` : '')),
        h('div.muted', 'Hardware illustrations rendered in Blender. Backdrop photography generated with cyberninjas.')))));
  return { el, update(s) { sensorHelper.update(s); }, title: 'Settings', sub: '' };
}

export const pages = { overview, cpu, gpu, memory, storage: storagePage, network, processes, sensors, settings: settingsPage };

// ==================================================================== MINI
export function buildMini(ctx, onClose) {
  const rows = {};
  const list = h('div.mini-list');
  for (const [key, label, color] of [['cpu', 'CPU', '--c-cpu'], ['gpu', 'GPU', '--c-gpu'], ['vram', 'VRAM', '--c-gpu'], ['mem', 'RAM', '--c-mem'], ['net', 'NET', '--c-net']]) {
    const m = meter(color);
    const v = h('span.mini-v', DASH), sub = h('span.mini-sub', '');
    rows[key] = { m, v, sub };
    list.append(h('div.mini-row', h('span.mini-l', h('i.dot', { style: { background: `var(${color})` } }), label), v, sub, m));
  }
  const status = h('div.mini-status');
  const el = h('div.mini-inner', h('div.mini-head', h('span.brand-name', 'RigWatch'), status,
    h('button.btn.btn-sm', { type: 'button', onclick: onClose }, 'Expand')), list);
  function update(s) {
    rows.cpu.m.set(s.cpu.util); setText(rows.cpu.v, fmt.pct(s.cpu.util));
    setText(rows.cpu.sub, [s.cpu.temp_c != null ? fmt.temp(s.cpu.temp_c) : null, fmt.watts(s.cpu.power_w)].filter(Boolean).join(' · '));
    const g = s.gpus[0];
    if (g) {
      rows.gpu.m.set(g.util); setText(rows.gpu.v, fmt.pct(g.util));
      setText(rows.gpu.sub, [fmt.temp(g.temp_c), fmt.watts(g.power_w)].join(' · '));
      rows.vram.m.set(g.vram_pct, loadTone(g.vram_pct, 90, 97)); setText(rows.vram.v, fmt.pct(g.vram_pct));
      setText(rows.vram.sub, g.vram_used == null ? '' : `${fmt.bytes(g.vram_used)} of ${fmt.bytes(g.vram_total, 0)}`);
    }
    rows.mem.m.set(s.memory.pct, loadTone(s.memory.pct)); setText(rows.mem.v, fmt.pct(s.memory.pct));
    setText(rows.mem.sub, `${fmt.bytes(s.memory.used)} of ${fmt.bytes(s.memory.total)}`);
    const link = (s.network.adapters.find(a => a.name === s.network.primary) || {}).speed_mbps;
    rows.net.m.set(link ? Math.min(100, ((s.network.rx_bps * 8) / (link * 1e6)) * 100) : 0);
    setText(rows.net.v, fmt.bits(s.network.rx_bps)); setText(rows.net.sub, `up ${fmt.bits(s.network.tx_bps)}`);
    const lvl = s.status;
    setChip(status, lvl === 'ok' ? 'good' : lvl === 'warning' ? 'warn' : 'crit', lvl === 'ok' ? 'Normal' : s.alerts[0].message);
  }
  return { el, update };
}
