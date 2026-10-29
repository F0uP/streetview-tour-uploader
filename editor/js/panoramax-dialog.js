// Publishing a tour on Panoramax, the open street-level photo platform - besides Google.
// The app talks to the instance (panoramax.py) and keeps the token; signing in is a
// token the user claims once in the browser. Each photo goes up with its position,
// north and capture time written into it (photoWithPose); the instance groups them
// into a sequence, blurs faces and plates and publishes them.
import { escapeHtml } from './util.js';

// From the editor (initPanoramax).
let t, toast, openModal, closeMenus, setAppBusy, scopes, photoWithPose, scheduleAutosave, isSample, ensureTimes;
export function initPanoramax(ctx) {
  ({ t, toast, openModal, closeMenus, setAppBusy, scopes, photoWithPose, scheduleAutosave, isSample, ensureTimes } = ctx);
}

const el = id => document.getElementById(id);
const job = { running: false, stop: false };
let info = null;

async function api(path, body = {}) {
  try {
    const r = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    if (r.status === 401) return { ok: false, error: 'not_signed_in' };
    return await r.json();
  } catch {
    return { ok: false, error: t('The editor app is not reachable.') };
  }
}

function errorText(j) {
  if (j.error === 'not_signed_in') return t('Panoramax no longer accepts the sign-in - connect your account again.');
  return j.status ? `${j.status}: ${t(j.error)}` : t(j.error);
}

export async function openPanoramax() {
  closeMenus();
  openModal('pmx-back');
  [info] = await Promise.all([api('/api/panoramax'), ensureTimes()]); // the capture times, read from the photos once
  render();
}

function render() {
  const ready = !!info?.configured;
  el('pmx-signin').hidden = ready;
  el('pmx-ready').hidden = !ready;
  if (!ready) {
    const sel = el('pmx-instance');
    const list = info?.instances || [];
    sel.innerHTML = list.map(u => `<option value="${escapeHtml(u)}">${escapeHtml(u.replace(/^https:\/\//, ''))}</option>`).join('')
      + `<option value="">${escapeHtml(t('Another instance…'))}</option>`;
    sel.onchange = () => { el('pmx-other').hidden = !!sel.value; };
    el('pmx-other').hidden = !!sel.value;
    return;
  }
  el('pmx-account').textContent = t('Connected to {instance} as {user}', { instance: info.instance.replace(/^https?:\/\//, ''), user: info.user || '?' });
  const tours = scopes();
  const sel = el('pmx-tour');
  const keep = sel.value;
  sel.innerHTML = tours.map(sc => `<option value="${sc.key}">${escapeHtml(sc.label)}</option>`).join('');
  if (tours.some(sc => sc.key === keep)) sel.value = keep;
  summary();
}

const chosenInstance = () => el('pmx-instance').value || el('pmx-other').value.trim();

// What goes up: photos with a position and a capture time (Panoramax sorts and places by them).
function plan() {
  const sc = scopes().find(x => x.key === el('pmx-tour').value) || scopes()[0];
  const again = el('pmx-again').checked;
  const nodes = (sc?.nodes || []).filter(n => n.photoFile && n.lat != null && !isSample(n));
  return {
    scope: sc,
    send: nodes.filter(n => n.captureTime && (again || !n.pmx || n.pmx.instance !== info.instance)),
    noTime: nodes.filter(n => !n.captureTime),
    done: nodes.filter(n => n.captureTime && !again && n.pmx?.instance === info.instance),
  };
}

function summary() {
  const { send, noTime, done } = plan();
  el('pmx-sum').textContent = [
    t('{n} photo(s) to publish', { n: send.length }),
    done.length && t('{n} on Panoramax already', { n: done.length }),
    noTime.length && t('{n} without a capture time - Panoramax needs one', { n: noTime.length }),
  ].filter(Boolean).join(' · ');
  el('pmx-go').disabled = job.running || !send.length;
}
['pmx-tour', 'pmx-again'].forEach(id => el(id).addEventListener('change', summary));

el('pmx-connect').onclick = async () => {
  const status = el('pmx-signin-status');
  status.textContent = t('Asking the instance for a sign-in…');
  const j = await api('/api/panoramax/connect', { instance: chosenInstance() });
  if (!j.ok) { status.textContent = errorText(j); return; }
  window.open(j.claim, '_blank', 'noopener');
  status.textContent = t('Log in on the page that opened and confirm - this dialog goes on by itself.');
  const until = Date.now() + 5 * 60000;
  while (Date.now() < until && el('pmx-back').classList.contains('show')) {
    await new Promise(r => setTimeout(r, 3000));
    const c = await api('/api/panoramax/check');
    if (c.ok && c.done) { info = c; status.textContent = ''; toast(t('Connected to Panoramax')); render(); return; }
    if (!c.ok) { status.textContent = errorText(c); return; }
  }
  status.textContent = t('Not confirmed yet - click "Connect my account…" again when you are logged in.');
};

el('pmx-use-token').onclick = async () => {
  const status = el('pmx-signin-status');
  const j = await api('/api/panoramax/token', { instance: chosenInstance(), token: el('pmx-token').value });
  el('pmx-token').value = '';
  if (!j.ok) { status.textContent = errorText(j); return; }
  info = j;
  render();
};

el('pmx-forget').onclick = async () => {
  if (!confirm(t('Disconnect from Panoramax? Your photos there stay.'))) return;
  info = await api('/api/panoramax/forget');
  render();
};

el('pmx-stop').onclick = () => { job.stop = true; };

el('pmx-go').onclick = async () => {
  const { scope, send } = plan();
  if (!send.length || job.running) return;
  const status = el('pmx-status');
  const list = el('pmx-list');
  const bar = el('pmx-progress');
  job.running = true;
  job.stop = false;
  el('pmx-stop').hidden = false;
  bar.hidden = false;
  list.innerHTML = '';
  summary();
  setAppBusy(true, t('Photos are still being sent to Panoramax. Quit anyway? The ones not sent yet can be sent later.'));
  const row = (n, level, text) => {
    const r = document.createElement('div');
    r.className = `check-row ${level}`;
    r.innerHTML = `<span class="lv"></span><span class="nm">${escapeHtml(n.name)}</span><span class="msg">${escapeHtml(text)}</span>`;
    list.appendChild(r);
  };
  let sent = 0;
  try {
    const start = await api('/api/panoramax/start', { title: scope?.name || t('Tour'), count: send.length });
    if (!start.ok) throw new Error(errorText(start));
    for (const [i, n] of send.entries()) {
      if (job.stop) break;
      status.textContent = t('Sending photo {a} of {b}…', { a: i + 1, b: send.length });
      bar.firstElementChild.style.width = `${(i / send.length) * 100}%`;
      let j;
      try {
        const blob = await photoWithPose(n);
        const q = new URLSearchParams({ set: start.set, name: `${n.id}-${n.photoName || 'photo.jpg'}`, blurred: el('pmx-blur').checked ? '0' : '1' });
        const r = await fetch(`/api/panoramax/file?${q}`, { method: 'POST', headers: { 'Content-Type': 'image/jpeg' }, body: blob });
        j = r.status === 401 ? { ok: false, error: 'not_signed_in' } : await r.json();
      } catch (err) {
        j = { ok: false, error: err.message || String(err) };
      }
      if (!j.ok) {
        row(n, 'error', errorText(j));
        if (j.error === 'not_signed_in') break;
        continue;
      }
      if (j.rejected) { row(n, 'error', t('Refused: {why}', { why: j.rejected })); continue; }
      n.pmx = { instance: info.instance, at: Date.now() };
      sent++;
      row(n, 'ok', j.duplicate ? t('was on Panoramax already') : t('sent'));
      scheduleAutosave();
    }
    bar.firstElementChild.style.width = '100%';
    const done = await api('/api/panoramax/complete', { set: start.set });
    if (!done.ok) throw new Error(errorText(done));
    status.textContent = t('{n} photo(s) sent. Panoramax now prepares them (blurring, sequence) - that takes a few minutes.', { n: sent });
    toast(t('{n} photo(s) sent to Panoramax', { n: sent }));
    watch(start.set);
  } catch (err) {
    status.textContent = t('Publishing on Panoramax failed: {msg}', { msg: err.message || err });
  } finally {
    job.running = false;
    el('pmx-stop').hidden = true;
    bar.hidden = true;
    setAppBusy(false);
    summary();
  }
};

// How far the instance got, for a few minutes after the upload.
async function watch(set) {
  const status = el('pmx-status');
  for (let k = 0; k < 40 && el('pmx-back').classList.contains('show'); k++) {
    await new Promise(r => setTimeout(r, k ? 6000 : 2000));
    const s = await api('/api/panoramax/status', { set });
    if (!s.ok) return;
    status.innerHTML = escapeHtml(t('Panoramax: {a} ready, {b} being prepared, {c} refused.', { a: s.prepared, b: s.preparing, c: s.rejected }))
      + ` <a href="${escapeHtml(s.instance)}" target="_blank" rel="noopener">${escapeHtml(t('Open Panoramax'))}</a>`;
    if (s.ready) return;
  }
}
