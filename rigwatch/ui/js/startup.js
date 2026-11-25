// The "Start with Windows" switch on the Settings page.
// The switch shows what Windows will really do: it reads the entry in the
// user's start-up list and Windows' own on/off choice for it.

import { h, setText, prefs } from './ui.js';

const REFRESH_MS = 4000;      // picks up a change made in Task Manager while Settings is open

async function request(body) {
  const init = body === undefined ? undefined
    : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
  const res = await fetch('/api/startup', init);
  return res.json();
}

/** What the hint under the switch says for a given state. */
export function startupSummary(st, stripOn = true) {
  if (!st) return '';
  if (st.state === 'blocked') {
    return 'Switched off in Windows’ own list (Task Manager → Startup apps). Turn it on here to allow it again.';
  }
  if (st.state !== 'on') {
    return st.available
      ? 'Open RigWatch when you sign in. It starts minimised, with the numbers on the taskbar.'
      : 'Not available from this folder: its path is too long for Windows’ start-up list.';
  }
  const parts = [];
  if (st.this_copy) parts.push('On. RigWatch opens minimised when you sign in.');
  else if (prefs.privacy || !st.target) parts.push('On. Windows starts another copy of RigWatch when you sign in.');
  else parts.push(`On. Windows starts the copy at ${st.target} when you sign in.`);
  if (st.target_missing) parts.push('That copy is not there right now, so nothing will start.');
  else if (st.external) parts.push('It is on an external drive: if the drive is not plugged in when you sign in, Windows skips it without a message.');
  if (!stripOn) parts.push('The taskbar strip is switched off, so only the taskbar button shows.');
  return parts.join(' ');
}

/** Settings row with a switch. Call ``update(snapshot)`` on every sample. */
export function startupSetting(stripOn = () => true) {
  const sw = h('input', { type: 'checkbox', class: 'switch', 'aria-label': 'Start with Windows' });
  const hint = h('div.muted', 'Checking…');
  const el = h('div.setting', h('div', h('div.setting-l', 'Start with Windows'), hint), sw);
  let busy = false, last = null, fetchedAt = 0, noteUntil = 0;
  sw.disabled = true;

  function paint(st, note) {
    last = st;
    sw.checked = st.state === 'on';
    sw.disabled = busy || (!st.available && st.state === 'off');
    if (note) { setText(hint, note); noteUntil = Date.now() + 10000; }
    else if (Date.now() > noteUntil) setText(hint, startupSummary(st, stripOn()));
  }

  async function refresh() {
    fetchedAt = Date.now();
    try { const st = await request(); if (!busy) paint(st); } catch { /* keep what is shown */ }
  }

  sw.addEventListener('change', async () => {
    const want = sw.checked;
    busy = true; sw.disabled = true; noteUntil = 0;
    let result = null;
    try { result = await request({ enabled: want }); } catch { /* handled below */ }
    busy = false;
    fetchedAt = Date.now();
    if (result && result.state) paint(result, result.note);
    else {
      sw.checked = !want; sw.disabled = false;
      setText(hint, 'RigWatch did not answer. Try again.'); noteUntil = Date.now() + 10000;
    }
  });

  el.update = () => {
    if (busy) return;
    if (Date.now() - fetchedAt > REFRESH_MS) refresh();
    else if (last && Date.now() > noteUntil) setText(hint, startupSummary(last, stripOn()));
  };
  refresh();
  return el;
}
