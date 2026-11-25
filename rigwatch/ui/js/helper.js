// The sensor helper's user interface: the dialog that explains CPU temperature
// and turns it on, and the matching switch on the Settings page.

import { h, setText, chip, showDialog } from './ui.js';

const CHIPS = {
  on: ['good', 'On'], off: ['off', 'Off'], starting: ['info', 'Starting'], enabling: ['info', 'Waiting'],
  disabling: ['info', 'Waiting'], no_driver: ['warn', 'Driver missing'], not_elevated: ['warn', 'No permission'],
  error: ['warn', 'Problem'], not_responding: ['warn', 'Not responding'],
};
const WAITING = { enable: 'enabling', disable: 'disabling' };
const WAITING_STATES = new Set(Object.values(WAITING));

export const HELPER_SOURCE = 'Processor thermal sensor';

async function post(action) {
  const res = await fetch(`/api/helper/${action}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
  return res.json();
}

function driverLink(st) {
  return h('a', { href: st.driver_url || 'https://pawnio.eu', target: '_blank', rel: 'noopener' }, 'pawnio.eu');
}

/** One-line description of where the helper stands, for lists and hints. */
export function helperSummary(st) {
  if (!st) return '';
  if (st.state === 'on') return `On. ${st.sensors} readings from the processor and the motherboard.`;
  if (st.state === 'off') {
    if (!st.engine_available) return 'This copy of RigWatch was built without the sensor engine.';
    return 'Off. Turn it on to read the processor’s temperature, motherboard temperatures, case fans and voltages.';
  }
  return st.message || '';
}

/** Explains why CPU temperature needs the helper and lets the user switch it on or off. */
export function cpuTempDialog() {
  const body = h('div.helper-dialog');
  const dlg = document.getElementById('dialog');
  let busy = false, last = null;

  const act = async action => {
    busy = true;
    render({ ...(last || {}), state: WAITING[action] });
    let result = null;
    try { result = await post(action); } catch { /* handled below */ }
    busy = false;
    if (result) render(result, result.note);
    else render(last || { state: 'off', engine_available: true, driver_installed: true }, 'RigWatch did not answer. Try again.');
  };
  const button = (label, action, primary) =>
    h(primary ? 'button.btn.btn-primary' : 'button.btn', { type: 'button', onclick: () => act(action) }, label);

  function render(st, note) {
    if (!WAITING_STATES.has(st.state)) last = st;
    const [tone, label] = CHIPS[st.state] || CHIPS.off;
    const parts = [h('div.helper-state', chip(tone, label))];
    const actions = [];
    if (st.state === 'off') {
      parts.push(
        h('p', 'Windows keeps the processor’s temperature sensor behind administrator rights. ',
          'That is why Task Manager does not show it either.'),
        h('p', 'RigWatch can read it with a small sensor helper. Turning it on asks Windows for permission once. ',
          'After that the helper starts with RigWatch and stops when you close it; RigWatch itself keeps running without those rights.'),
        h('p', 'It also adds motherboard temperatures, case fans and voltages. Everything shown is still measured, never estimated.'));
      if (!st.engine_available) {
        parts.push(h('p', h('strong', 'Not available in this copy. '), 'It was built without the sensor engine.'));
      } else {
        if (!st.driver_installed) {
          parts.push(h('p', h('strong', 'One more thing is needed: '), 'the free, open-source PawnIO driver. Install it from ',
            driverLink(st), ', then turn the helper on.'));
        }
        actions.push(button('Turn on CPU temperature', 'enable', true));
      }
    } else if (st.state === 'enabling') {
      parts.push(h('p', 'Waiting for your answer to the Windows permission prompt. If you do not see it, look for a flashing shield on the taskbar.'));
    } else if (st.state === 'disabling') {
      parts.push(h('p', 'Turning the sensor helper off…'));
    } else if (st.state === 'starting') {
      parts.push(h('p', 'The sensor helper is starting. Readings appear in a few seconds.'));
      actions.push(button('Turn off', 'disable'));
    } else if (st.state === 'on') {
      parts.push(
        h('p', `The sensor helper is running: ${st.sensors} readings from the processor and the motherboard. `,
          'They are listed under All sensors.'),
        h('p', 'It stops when you close RigWatch. Turning it off removes it again; Windows may ask for permission once more.'));
      actions.push(button('Turn off', 'disable'));
    } else if (st.state === 'no_driver') {
      parts.push(h('p', 'The sensor helper is running, but the free, open-source PawnIO driver it reads through is not installed. Get it from ',
        driverLink(st), '. The readings appear by themselves afterwards; nothing needs restarting.'));
      actions.push(button('Turn off', 'disable'));
    } else {
      parts.push(h('p', st.message || 'The sensor helper is not working.'));
      actions.push(button('Try again', 'enable', true), button('Turn off', 'disable'));
    }
    if (note) parts.push(h('p.helper-note', note));
    if (actions.length) parts.push(h('div.helper-actions', actions));
    body.replaceChildren(...parts);
  }

  const refresh = async () => {
    if (busy) return;
    try { const st = await (await fetch('/api/helper')).json(); if (!busy) render(st); } catch { /* keep what is shown */ }
  };
  body.replaceChildren(h('p', 'Checking…'));
  showDialog('CPU temperature', [body]);
  refresh();
  const timer = setInterval(refresh, 1500);
  dlg.addEventListener('close', () => clearInterval(timer), { once: true });
}

/** Settings row with a switch. Call ``update(snapshot)`` on every sample. */
export function helperSetting() {
  const sw = h('input', { type: 'checkbox', class: 'switch', 'aria-label': 'CPU temperature, fans and voltages' });
  const hint = h('div.muted');
  const more = h('button.linkbtn', { type: 'button', onclick: () => cpuTempDialog() }, 'How this works');
  const el = h('div.setting', h('div', h('div.setting-l', 'CPU temperature, fans and voltages'), hint, more), sw);
  let busy = false, noteUntil = 0;

  function paint(st, note) {
    sw.checked = !!(st.installed || st.state === 'on' || st.state === 'enabling');
    sw.disabled = busy || (!st.engine_available && !st.installed);
    if (note) { setText(hint, note); noteUntil = Date.now() + 10000; }
    else if (Date.now() > noteUntil) setText(hint, helperSummary(st));
  }
  sw.addEventListener('change', async () => {
    const action = sw.checked ? 'enable' : 'disable';
    busy = true; sw.disabled = true; noteUntil = 0;
    setText(hint, action === 'enable' ? 'Waiting for your answer to the Windows permission prompt…' : 'Turning the sensor helper off…');
    let result = null;
    try { result = await post(action); } catch { /* handled below */ }
    busy = false;
    if (result) paint(result, result.note);
    else { sw.checked = !sw.checked; sw.disabled = false; setText(hint, 'RigWatch did not answer. Try again.'); noteUntil = Date.now() + 10000; }
  });
  el.update = s => { if (!busy && s.helper) paint(s.helper); };
  return el;
}
