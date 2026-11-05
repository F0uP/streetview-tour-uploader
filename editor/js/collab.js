// Working on one project together: the project file in a folder a cloud service shares
// (OneDrive, Dropbox, Google Drive, Nextcloud), open on several computers. Here: looking
// at the file every few seconds for what the others saved (the editor merges it in),
// saving into it automatically if wanted and - in the app - who else has it open and the
// copies a cloud folder keeps when two saved at the same time.
import { escapeHtml } from './util.js';

// From the editor (initCollab): { t, toast, openModal, closeModal, closeMenus, storageGet, storageSet, choose,
// isApp(), handle(), fileName(), pull(), save(), dirty(), blocked(), mergeCopy(copy), defaultName(), knownVersion(rev) }
let ctx;
const el = id => document.getElementById(id);
const LOOK_EVERY = 4000; // ms: has the file changed?
const PRESENCE_EVERY = 60000; // ms: "still here" (others count an editor as gone after 3 minutes)
const VERSIONS_EVERY = 5 * 60000; // ms: copies the cloud folder kept
const SAVE_AFTER = 8000; // ms after the last change, when saving automatically

let others = []; // [{ name, since, client }]
let presenceFor = null; // the file token presence was last noted for
let lastPresence = 0, lastVersions = 0, hinted = false;
let saveTimer = null;
const asked = new Set(); // copies already asked about (name and time)

function collabClient() {
  let id = ctx.storageGet('collabClient');
  if (!id || !/^[A-Za-z0-9_-]{6,40}$/.test(id)) {
    id = `e${Math.random().toString(36).slice(2, 10)}${Date.now().toString(36)}`;
    ctx.storageSet('collabClient', id);
  }
  return id;
}
// The name set in the dialog: the only one that goes into a project file.
export const collabOwnName = () => (ctx.storageGet('collabName') || '').trim();
// The name the others see while the project is open here: else the computer's user name.
const collabName = () => collabOwnName() || (ctx.defaultName() || '').trim();
const autoSaving = () => ctx.storageGet('collabAutosave') === '1';
const appToken = () => (ctx.isApp() ? ctx.handle()?.token || null : null);

async function post(path, body) {
  try {
    const r = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), keepalive: !!body.leave });
    return await r.json();
  } catch { return null; }
}

export function initCollab(c) {
  ctx = c;
  setInterval(look, LOOK_EVERY);
  window.addEventListener('focus', look); // back from another window: at once
  document.addEventListener('visibilitychange', look);
  window.addEventListener('pagehide', () => { if (presenceFor) post('/api/files/presence', { token: presenceFor, client: collabClient(), leave: true }); });
  el('collab-people').onclick = openCollab;
  el('collab-name').addEventListener('change', () => {
    ctx.storageSet('collabName', el('collab-name').value.trim());
    lastPresence = 0; // the others see the new name with the next sign of life
    look();
  });
  el('collab-autosave').addEventListener('change', () => {
    ctx.storageSet('collabAutosave', el('collab-autosave').checked ? '1' : '0');
    if (el('collab-autosave').checked) collabEdited();
  });
}

// The project file changed (opened, saved under another name, closed by adding projects).
export function collabProjectChanged() {
  const token = appToken();
  if (presenceFor && presenceFor !== token) post('/api/files/presence', { token: presenceFor, client: collabClient(), leave: true });
  if (presenceFor !== token) { presenceFor = null; others = []; hinted = false; }
  lastVersions = 0;
  render();
  setTimeout(look, 300);
}

let looking = false;
async function look() {
  if (looking || !ctx.handle()) return;
  looking = true;
  // In a minimized window the changes of others wait, but it is still there for them.
  const visible = document.visibilityState === 'visible';
  try {
    if (visible) await ctx.pull();
    const token = appToken();
    if (!token) return;
    const now = Date.now();
    if (token !== presenceFor || now - lastPresence > PRESENCE_EVERY) await notePresence(token);
    if (visible && now - lastVersions > VERSIONS_EVERY && !ctx.blocked()) checkVersions(token);
  } catch (err) {
    console.warn('shared project', err);
  } finally {
    looking = false;
  }
}

async function notePresence(token) {
  const j = await post('/api/files/presence', { token, client: collabClient(), name: collabName() });
  if (!j?.ok || token !== appToken()) return;
  presenceFor = token;
  lastPresence = Date.now();
  others = j.others || [];
  render();
  if (others.length && !hinted && !autoSaving()) {
    hinted = true;
    ctx.toast(ctx.t('{names} also has this project open. Your changes reach them when you save (Ctrl+S) - or let the editor save by itself: Project → Working together…',
      { names: others.map(o => o.name || ctx.t('Someone')).join(', ') }));
  }
}

async function checkVersions(token) {
  lastVersions = Date.now();
  const j = await post('/api/files/versions', { token });
  for (const copy of j?.versions || []) {
    const key = `${copy.name}|${copy.mtime}`;
    // A version this project went through (a backup, say) has nothing new in it.
    if (asked.has(key) || token !== appToken() || ctx.knownVersion(copy.rev)) continue;
    asked.add(key);
    const who = copy.savedBy?.name;
    const answer = await ctx.choose(ctx.t('A second version of this project'),
      ctx.t('{copy} is another version of {file}{who}. A cloud folder keeps one like it when two people saved at the same time. Merge its changes into {file}? The copy then goes to the recycle bin.',
        { copy: copy.name, file: ctx.fileName(), who: who ? ` (${ctx.t('saved by {name}', { name: who })})` : '' }),
      [{ label: ctx.t('Merge it in'), value: 'merge', primary: true }, { label: ctx.t('Not now'), value: null }]);
    if (answer === 'merge') await ctx.mergeCopy(copy);
  }
}

// A change in the tour: saved into the file shortly after, if saving automatically.
export function collabEdited() {
  if (!autoSaving() || !ctx.handle()) return;
  clearTimeout(saveTimer);
  saveTimer = setTimeout(saveNow, SAVE_AFTER);
}
async function saveNow() {
  if (!ctx.handle() || !ctx.dirty()) return;
  if (ctx.blocked()) { saveTimer = setTimeout(saveNow, 4000); return; } // in the middle of something
  await ctx.save();
}

const initials = name => (name || '?').split(/\s+/).filter(Boolean).slice(0, 2).map(w => w[0].toUpperCase()).join('') || '?';
const sinceText = ms => new Date(ms || Date.now()).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });

function render() {
  const box = el('collab-people');
  box.hidden = !others.length;
  box.innerHTML = others.slice(0, 3).map(o => `<span class="av">${escapeHtml(initials(o.name))}</span>`).join('')
    + (others.length > 3 ? `<span class="av more">+${others.length - 3}</span>` : '');
  box.title = ctx.t('Also working on this project: {names}', { names: others.map(o => o.name || ctx.t('Someone')).join(', ') });
  if (el('collab-back').classList.contains('show')) renderDialog();
}

export function openCollab() {
  ctx.closeMenus();
  el('collab-name').value = collabName();
  el('collab-autosave').checked = autoSaving();
  renderDialog();
  ctx.openModal('collab-back');
  look();
}

function renderDialog() {
  const handle = ctx.handle();
  el('collab-file').textContent = handle
    ? ctx.t('{file}: what the others save comes in here as soon as the cloud service has brought it.', { file: ctx.fileName() })
    : window.showSaveFilePicker
      ? ctx.t('Save the project as a file first (Ctrl+S), in a folder the cloud service shares - then open that file on the other computers.')
      : ctx.t('This browser can only download a copy of the project. Working on one file together needs the app (or Chrome or Edge).');
  el('collab-who').innerHTML = others.length
    ? others.map(o => `<div class="check-row ok"><span class="lv"></span><span class="nm">${escapeHtml(o.name || ctx.t('Someone'))}</span>`
      + `<span class="msg">${escapeHtml(ctx.t('since {time}', { time: sinceText(o.since) }))}</span></div>`).join('')
    : `<div class="mini">${escapeHtml(ctx.isApp() ? ctx.t('Nobody else has it open just now.') : ctx.t('Who else has it open shows in the app.'))}</div>`;
}
