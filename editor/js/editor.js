// The tour editor (editor.html). One module; the parts that stand on their own
// are in the files imported below.
import exifr from '../vendor/exifr/full.esm.js';
import { Viewer } from '@photo-sphere-viewer/core';
import { MarkersPlugin } from '@photo-sphere-viewer/markers-plugin';
import { DE, DE_RX, DE_HTML } from './i18n-de.js';
import { buildZip, readZip } from './zip.js';
import { parseGpx, parseOffsetHours, positionAt } from './gpx.js';
import { degToRad, escapeHtml, normalizeRad, radToDeg } from './util.js';
import { bearingDeg, distanceMeters, formatDistance, fromLocal, headingDiff, normalizeHeading, offsetEast, toLocal } from './geo.js';
import { buildExif, jpegInfo, readCamera, replaceExif, withMetadata } from './exif.js';
import { encodeJpeg, hideArea } from './photo-edit.js';
import { initPhotoTools, openNadir, openSunNorth } from './photo-tools.js';
import { exportGpx, exportKml, exportPhotos, initExports } from './exports.js';
import { initViewCone, updateViewCone } from './viewcone.js';
import { initFilmstrip, renderFilmstrip, stepFilmstrip } from './filmstrip.js';
import { initPanoramax, openPanoramax } from './panoramax-dialog.js';
import { VIEWER_LIBS, VIEWER_LICENSES, genViewer as viewerPage } from './viewer.js';
import { bestShift, headingFromShift, isClearMatch, photoFeatures, walkWeights } from './align.js';
import { findFaces, findPlates, overlap } from './detect.js';
import { gpsTimeline, hourShift, readVideoInfo } from './video.js';
import { embedCode, isWebAddress, qrPng, qrSvg } from './share.js';
import { initVideoDialog, isVideoFile, openVideo, setVideoFile } from './video-dialog.js';
import { initReport, openReport } from './report.js';
import { initNotices, logNotice, notices } from './notices.js';
import { initDialogs } from './a11y.js';

// ── Connection to the local app ───────────────────
// The app opens this page with its secret key in the URL fragment (#k=…). The
// page keeps it in its own storage and sends it with every API call; without it
// the app refuses to do anything, so no other program or user on this computer
// can use the Google sign-in through it.
const EDITOR_KEY = (() => {
  const m = /[#&]k=([\w-]{20,})/.exec(location.hash);
  if (m) {
    storageSet('editorKey', m[1]);
    history.replaceState(null, '', location.pathname + location.search); // don't leave it in the address bar
    return m[1];
  }
  return storageGet('editorKey') || '';
})();

// A notice at the top of the page; × hides it.
function appNotice(text, onClose) {
  const el = document.createElement('div');
  el.className = 'key-banner';
  el.textContent = text;
  const x = document.createElement('button');
  x.className = 'key-banner-x';
  x.title = t('Close');
  x.textContent = '×';
  x.onclick = () => { el.remove(); onClose?.(); };
  el.appendChild(x);
  document.body.appendChild(el);
  return el;
}

let keyProblemShown = false;
function showKeyProblem(appHasWindow) {
  if (keyProblemShown) return;
  keyProblemShown = true;
  appNotice(appHasWindow
    ? t('The Streetview Tour Editor now runs in its own window, so this browser tab is no longer connected and can be closed. Work that was only autosaved in this browser: restore it here, save it as a project (Ctrl+S) and open that file in the app.')
    : t('This page is not connected to the Streetview Tour Editor app. Open the editor from the Start menu (or the app\'s tray icon) to upload and save.'));
}

// The last errors of this page, for a bug report.
const pageErrors = [];
function notePageError(msg, where = '') {
  pageErrors.push({ at: new Date().toISOString(), msg: String(msg).slice(0, 2000), where });
  if (pageErrors.length > 30) pageErrors.shift();
}
window.addEventListener('error', e => notePageError(e.message, `${e.filename || ''}:${e.lineno || ''}`));
window.addEventListener('unhandledrejection', e => notePageError(e.reason?.stack || e.reason));

const nativeFetch = window.fetch.bind(window);
window.fetch = (input, init = {}) => {
  const url = typeof input === 'string' ? input : input.url;
  if (url.startsWith('/api/')) {
    init = { ...init, headers: { ...(init.headers || {}), 'X-Editor-Key': EDITOR_KEY } };
  }
  return nativeFetch(input, init).then(r => {
    if (r.status === 401 && r.headers.get('X-Editor-Auth') === 'bad-key') {
      r.clone().json().then(j => showKeyProblem(!!j.window), () => showKeyProblem(false));
    }
    return r;
  });
};

// ── Native file dialogs in the app window ─────────
// WebView2's own pickers are titled for websites ("Choose a file this site can
// read"). In the app window the app shows Windows' regular dialogs instead and
// reads and writes the chosen file itself; these handles stand in for the File
// System Access API ones the rest of the editor uses.
// The app window keeps photos by reference: for a photo from a file the user
// picked, or from inside an opened project, the autosave notes where it came
// from instead of copying it into the web engine's storage once more. The app
// hands it out again only while that file is unchanged.
const photoRefs = new WeakMap(); // photo File -> { ref, entry (inside a project), type }

async function fileRefs(handles) {
  if (!handles.length || !handles.every(h => h instanceof AppFileHandle)) return null;
  try {
    const j = await (await fetch('/api/files/ref', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ tokens: handles.map(h => h.token) }),
    })).json();
    return j.ok ? j.refs : null;
  } catch { return null; }
}

// Back from references: the photos of a restored session (grouped by file, so a
// project is read once). Photos whose file changed or vanished stay missing.
async function resolvePhotoRefs(nodes) {
  const files = new Map();
  for (const n of nodes) {
    const r = n.photoRef;
    delete n.photoRef;
    if (!r) continue;
    if (!files.has(r.ref)) {
      let got = null;
      try {
        const j = await (await fetch('/api/files/ref/open', {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ref: r.ref }),
        })).json();
        if (j.ok) got = { file: await new AppFileHandle(j.token, j.name).getFile(), zip: null };
      } catch {}
      files.set(r.ref, got);
    }
    const got = files.get(r.ref);
    if (!got) continue;
    let data = got.file;
    if (r.entry) {
      got.zip ??= await readZip(got.file).catch(() => ({}));
      data = got.zip[r.entry];
    }
    if (!data) continue;
    n.photoFile = new File([data], n.photoName || got.file.name, { type: r.type || 'image/jpeg' });
    photoRefs.set(n.photoFile, r);
  }
}

class AppFileHandle {
  constructor(token, name) {
    this.kind = 'file';
    this.token = token;
    this.name = name;
    this.written = null; // what was last saved here - no need to read it back
  }
  async getFile() {
    if (this.written) return new File([this.written], this.name, { type: this.written.type });
    const r = await fetch(`/api/files/read?token=${encodeURIComponent(this.token)}`);
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || `HTTP ${r.status}`);
    return new File([await r.blob()], this.name);
  }
  async createWritable() {
    return appWritable(`/api/files/write?token=${encodeURIComponent(this.token)}`, blob => { this.written = blob; });
  }
  async queryPermission() { return 'granted'; }
  async requestPermission() { return 'granted'; }
}

class AppFolderHandle {
  constructor(token, name, grant) {
    this.kind = 'directory';
    this.token = token;
    this.name = name;
    this.grant = grant;
  }
  async getFileHandle(name) {
    const url = `/api/files/write?token=${encodeURIComponent(this.token)}&name=${encodeURIComponent(name)}`;
    return { kind: 'file', name, createWritable: async () => appWritable(url) };
  }
  async queryPermission() { return 'granted'; }
  async requestPermission() { return 'granted'; }
}

function appWritable(url, onSaved) {
  const parts = [];
  return {
    async write(data) { parts.push(data); },
    async close() {
      const blob = new Blob(parts);
      const r = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/octet-stream' }, body: blob });
      const j = await r.json().catch(() => ({}));
      if (!r.ok || !j.ok) throw new Error(j.error || `HTTP ${r.status}`);
      onSaved?.(blob);
    },
  };
}

async function appPick(kind, opts = {}) {
  const types = (opts.types || []).map(type => ({
    description: type.description || '',
    extensions: Object.values(type.accept || {}).flat(),
  }));
  const r = await fetch('/api/files/pick', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ kind, suggestedName: opts.suggestedName || '', types, multiple: !!opts.multiple, remember: opts.remember !== false }),
  });
  const j = await r.json();
  if (!j.ok) throw new Error(j.error || 'The file dialog failed.');
  if (j.cancelled) throw new DOMException('The user aborted a request.', 'AbortError');
  return j;
}

function useAppFileDialogs() {
  window.showOpenFilePicker = async opts => {
    const j = await appPick('open', opts);
    return (j.files || [j]).map(f => new AppFileHandle(f.token, f.name));
  };
  window.showSaveFilePicker = async opts => { const j = await appPick('save', opts); return new AppFileHandle(j.token, j.name); };
  window.showDirectoryPicker = async opts => { const j = await appPick('folder', opts); return new AppFolderHandle(j.token, j.name, j.grant); };
}

// Tell the app which editor pages are open, so it can quit when the last one
// is closed (a reload says hello again before the app gives up).
const PAGE_ID = crypto.randomUUID ? crypto.randomUUID() : `p${Date.now()}${Math.random()}`;
let appUpdating = false; // the app is down on purpose while an update installs
let appGoneEl = null, appGoneDismissed = false;
function setAppGone(gone) {
  if (gone && !appGoneEl && !appGoneDismissed && !appUpdating) {
    appGoneEl = appNotice(t('The Streetview Tour Editor app is not running any more. Start it again from the Start menu - everything in this tab is kept.'),
      () => { appGoneEl = null; appGoneDismissed = true; });
  } else if (!gone) {
    appGoneEl?.remove();
    appGoneEl = null;
    appGoneDismissed = false;
  }
}
function sayHello() {
  fetch('/api/hello', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ page: PAGE_ID }) })
    .then(() => setAppGone(false), () => setAppGone(true));
}
sayHello();
setInterval(sayHello, 30000);
window.addEventListener('pageshow', e => { if (e.persisted) sayHello(); });
document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') sayHello(); });
document.addEventListener('resume', sayHello); // Chrome unfroze the tab
window.addEventListener('pagehide', () => {
  navigator.sendBeacon('/api/bye', new Blob([JSON.stringify({ page: PAGE_ID, key: EDITOR_KEY })], { type: 'application/json' }));
});

// ── Language (English / Deutsch) ──────────────────
// The page is written in English. In German, t() looks strings up in DE, and
// translateDom() swaps static texts, tooltips and placeholders in place; a
// MutationObserver does the same for anything rendered later, so plain
// textContent / innerHTML updates are translated too. Only strings with
// placeholders ({n}) and alert()/confirm() texts need an explicit t().
// Blocks with inline markup use data-i18n="key" and a whole-HTML entry in DE_HTML.
let currentLang = (storageGet('lang') || (navigator.language || 'en').slice(0, 2)) === 'de' ? 'de' : 'en';

function rxTranslate(s) {
  for (const [rx, rep] of DE_RX) if (rx.test(s)) return s.replace(rx, rep);
  return null;
}

function t(s, vars) {
  let out = currentLang === 'de' ? (DE[s] ?? rxTranslate(s) ?? s) : s;
  if (vars) out = out.replace(/\{(\w+)\}/g, (m, k) => (vars[k] ?? m));
  return out;
}

const i18nOrig = new WeakMap();
const I18N_SKIP = 'script,style,.leaflet-pane,.nl,.nrow-name,#upl-log,.notes,.psv-container,[data-no-i18n],.cil,.walkchip,.chip,.check-row .nm,#cmp-target,#walkh-ref,.hs-row .t,.thumb-tip,.up-name';

function translateTextNode(node) {
  const orig = i18nOrig.get(node) ?? node.nodeValue;
  const key = orig.trim();
  if (!key) return;
  const de = currentLang === 'de' ? (DE[key] ?? rxTranslate(key)) : null;
  if (de != null) {
    i18nOrig.set(node, orig);
    const next = orig.replace(key, de);
    if (node.nodeValue !== next) node.nodeValue = next;
  } else if (i18nOrig.has(node) && node.nodeValue !== orig) {
    node.nodeValue = orig;
  }
}

function translateAttrs(el) {
  for (const attr of ['title', 'placeholder']) {
    if (!el.hasAttribute(attr)) continue;
    const store = `i18n${attr}`;
    if (el.dataset[store] === undefined) {
      if (DE[el.getAttribute(attr)] === undefined) continue;
      el.dataset[store] = el.getAttribute(attr);
    }
    const orig = el.dataset[store];
    el.setAttribute(attr, currentLang === 'de' ? (DE[orig] ?? orig) : orig);
  }
}

function translateDom(root = document.body) {
  if (root.nodeType === Node.TEXT_NODE) {
    if (!root.parentElement?.closest(I18N_SKIP)) translateTextNode(root);
    return;
  }
  if (root.nodeType !== Node.ELEMENT_NODE || root.closest(I18N_SKIP)) return;
  const blocks = [...(root.matches('[data-i18n]') ? [root] : []), ...root.querySelectorAll('[data-i18n]')];
  blocks.forEach(el => {
    if (!i18nOrig.has(el)) i18nOrig.set(el, el.innerHTML);
    const html = currentLang === 'de' ? (DE_HTML[el.dataset.i18n] ?? i18nOrig.get(el)) : i18nOrig.get(el);
    if (el.innerHTML !== html) el.innerHTML = html;
  });
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode: n => (n.parentElement?.closest(`${I18N_SKIP},[data-i18n]`) ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT),
  });
  for (let n = walker.nextNode(); n; n = walker.nextNode()) translateTextNode(n);
  [root, ...root.querySelectorAll('[title],[placeholder]')].forEach(el => { if (el.closest && !el.closest(I18N_SKIP)) translateAttrs(el); });
}

const i18nObserver = new MutationObserver(records => {
  if (currentLang !== 'de') return;
  records.forEach(r => {
    if (r.type === 'characterData') translateDom(r.target);
    else r.addedNodes.forEach(n => translateDom(n));
  });
});

// alert() and confirm() texts go through t() too; alerts are kept under "Messages".
const nativeAlert = window.alert.bind(window);
const nativeConfirm = window.confirm.bind(window);
window.alert = msg => {
  const text = t(String(msg));
  logNotice(text, 'alert');
  nativeAlert(text);
};
window.confirm = msg => nativeConfirm(t(String(msg)));

function setLang(lang) {
  currentLang = lang;
  storageSet('lang', lang);
  document.documentElement.lang = lang;
  translateDom();
  const label = document.getElementById('lang-label');
  if (label) label.textContent = lang === 'de' ? 'English' : 'Deutsch';
  // Re-render the parts built from t() with placeholders.
  updateSb();
  renderNodeList();
  updateStats();
  updateMultiBar();
  refreshAllMarkers();
  updateTitle();
}

const S = {
  nodes: [],
  conns: [],
  selId: null,
  connFrom: null,
  starts: new Set(), // the start point of each tour (see "Tours")
  tourNames: {},     // tour id -> name (a project or an imported folder)
  nxt: 1,
  compareTargetByNode: {},
  multiSel: new Set(),
};

let saveDirHandle = null;
let appHasWindow = false; // the editor runs in the app's own window, not in a browser tab
const uid = () => `n${S.nxt++}`;
const cuid = () => `c${S.nxt++}`;
// Points by id and their place in the list, kept until the list changes.
let nodeIndex = { list: null, length: -1, byId: new Map(), pos: new Map() };
function indexNodes() {
  if (nodeIndex.list !== S.nodes || nodeIndex.length !== S.nodes.length || nodeIndex.dirty) {
    nodeIndex = { list: S.nodes, length: S.nodes.length, byId: new Map(S.nodes.map(n => [n.id, n])), pos: new Map(S.nodes.map((n, i) => [n.id, i])) };
  }
  return nodeIndex;
}
const gn = id => indexNodes().byId.get(id);
const gcbs = (a, b) => S.conns.filter(c => (c.from === a && c.to === b) || (c.from === b && c.to === a));

const map = L.map('map', { zoomControl: true, doubleClickZoom: false }).setView([54.3233, 10.1228], 13);
// CARTO's free basemap tiles (basemaps.cartocdn.com) now require signing up for an
// API key, so the street map uses OpenStreetMap's own tiles, darkened with a CSS
// filter (.osm-dark). Satellite imagery comes from Esri World Imagery, which is
// much better for placing photos and checking north against buildings.
const baseLayers = {
  street: L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    maxZoom: 20,
    maxNativeZoom: 19,
    className: 'osm-dark',
  }),
  satellite: L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', {
    attribution: 'Imagery &copy; Esri, Maxar, Earthstar Geographics',
    maxZoom: 20,
    maxNativeZoom: 19,
  }),
};
const labelLayer = L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}', {
  maxZoom: 20,
  maxNativeZoom: 19,
});
map.setMaxZoom(20);
const savedLayer = storageGet('mapLayer') === 'satellite' ? 'satellite' : 'street';
baseLayers[savedLayer].addTo(map);
if (savedLayer === 'satellite' && storageGet('mapLabels') !== '0') labelLayer.addTo(map);
const layerControl = L.control.layers(
  { 'Street map': baseLayers.street, 'Satellite': baseLayers.satellite },
  { 'Place names': labelLayer },
  { position: 'topright' },
).addTo(map);
map.on('baselayerchange', e => {
  const sat = e.layer === baseLayers.satellite;
  storageSet('mapLayer', sat ? 'satellite' : 'street');
  if (sat && storageGet('mapLabels') !== '0') labelLayer.addTo(map);
  if (!sat) map.removeLayer(labelLayer);
});
map.on('overlayadd overlayremove', e => { if (e.layer === labelLayer) storageSet('mapLabels', e.type === 'overlayadd' ? '1' : '0'); });

const niN = document.getElementById('ni-n');
const niP = document.getElementById('ni-p');
const niH = document.getElementById('ni-h');
const niS = document.getElementById('ni-s');
const niLat = document.getElementById('ni-lat');
const niLng = document.getElementById('ni-lng');
const niLevel = document.getElementById('ni-level');
const niLevelName = document.getElementById('ni-level-name');
const phB = document.getElementById('ph-b');
const phN = document.getElementById('ph-n');
const clL = document.getElementById('cl-l');
const gpsBadge = document.getElementById('gps-b');
const headingMeta = document.getElementById('heading-meta');
const panoWrap = document.getElementById('pano-wrap');
const panoHelp = document.getElementById('pano-help');
const saveHint = document.getElementById('savehint');
const cmpTarget = document.getElementById('cmp-target');
const cmpWrap = document.getElementById('cmp-wrap');
const cmpHelp = document.getElementById('cmp-help');
const cmpOverlayLayer = document.getElementById('cmp-overlay-layer');
const cmpOpacity = document.getElementById('cmp-opacity');
const walkList = document.getElementById('walk-list');
const walkHelp = document.getElementById('walk-help');

let panoViewer = null;
let panoNodeId = null;
let panoFocus = false; // the panorama over the whole window
let cmpBaseViewer = null;
let cmpOverlayViewer = null;
let cmpBaseNodeId = null;
let cmpOverlayNodeId = null;
let cmpSyncing = false;
let cmpDelta = 0;
let cmpSwap = false;
const walkHistory = [];

initSaveDir();

let connectLine = null;

function cancelConnect() {
  if (!S.connFrom) return;
  S.connFrom = null;
  refreshAllMarkers();
  if (connectLine) { map.removeLayer(connectLine); connectLine = null; }
  document.getElementById('map').classList.remove('connecting');
}

// ── Node quick-action flyout ──────────────────────
const qm = document.getElementById('qm');
const qmStar = document.getElementById('qm-star');

function positionQuickMenu() {
  const n = gn(S.selId);
  const el = n?.marker?.getElement();
  if (!el) { qm.classList.remove('show'); return; }
  const rect = el.getBoundingClientRect();
  qm.style.left = `${rect.left + rect.width / 2}px`;
  qm.style.top = `${rect.top}px`;
}

function showQuickMenu() {
  const n = gn(S.selId);
  if (!n || S.connFrom || S.multiSel.size) { qm.classList.remove('show'); return; }
  positionQuickMenu();
  qm.classList.add('show');
  qmStar.classList.toggle('gold', S.starts.has(n.id));
  qmStar.querySelector('svg').style.fill = S.starts.has(n.id) ? 'currentColor' : 'none';
}

function hideQuickMenu() {
  qm.classList.remove('show');
}

map.on('move zoom', () => { if (S.selId) positionQuickMenu(); });
window.addEventListener('resize', () => { if (S.selId) positionQuickMenu(); });

qmStar.onclick = () => {
  const n = gn(S.selId);
  if (!n) return;
  pushUndo();
  if (S.starts.has(n.id)) S.starts.delete(n.id); else setTourStart(n.id);
  refreshAllMarkers();
  updateSb();
  showQuickMenu();
  scheduleAutosave();
};

document.getElementById('qm-link').onclick = () => {
  if (!S.selId) return;
  hideQuickMenu();
  handleNodeConnectClick(S.selId);
};

document.getElementById('qm-copy').onclick = () => {
  const n = gn(S.selId);
  if (!n) return;
  pushUndo();
  const copy = {
    ...n,
    id: uid(),
    marker: null,
    lat: n.lat + 0.00006,
    lng: n.lng + 0.00008,
    name: `${n.name} copy`,
    // The copy is a new photo spot: not on Google yet, and its info points are its own.
    gsvPhotoId: null, gsvStatus: null, gsvShareLink: null, gsvSig: null,
    gsvConfirmed: false, gsvUploadedAt: null, gsvObsolete: [], gsvRemote: null, pmx: null,
    hotspots: (n.hotspots || []).map(h => ({ ...h, id: `h${S.nxt++}` })),
  };
  S.nodes.push(copy);
  createMarker(copy);
  jumpToNode(copy.id, false);
  updateStats();
  scheduleAutosave();
  toast(`Duplicated ${n.name}`);
};

document.getElementById('qm-del').onclick = () => {
  if (!S.selId) return;
  pushUndo();
  removeNodeById(S.selId);
  hideQuickMenu();
  tourChanged();
};

map.on('click', () => {
  if (S.connFrom) { cancelConnect(); return; }
  if (S.multiSel.size) clearMultiSel();
  S.selId = null;
  updateSb();
  renderNodeList();
  hideQuickMenu();
});

map.on('dblclick', e => addEmptyNode(e.latlng.lat, e.latlng.lng));

map.on('mousemove', e => {
  if (!S.connFrom || !connectLine) return;
  const from = gn(S.connFrom);
  if (!from) return;
  connectLine.setLatLngs([[from.lat, from.lng], e.latlng]);
});

map.on('contextmenu', e => {
  e.originalEvent.preventDefault();
  cancelConnect();
});

const mw = document.getElementById('mw');
const doEl = document.getElementById('do');
mw.addEventListener('dragenter', e => { e.preventDefault(); doEl.classList.add('show'); });
mw.addEventListener('dragover', e => { e.preventDefault(); });
mw.addEventListener('dragleave', e => { if (!mw.contains(e.relatedTarget)) doEl.classList.remove('show'); });
mw.addEventListener('drop', async e => {
  e.preventDefault();
  doEl.classList.remove('show');
  const entries = dropEntries(e.dataTransfer); // only readable while the drop is dispatched
  // Project files. getAsFileSystemHandle() only works while the drop is dispatched.
  const projectDrops = [...e.dataTransfer.items]
    .filter(it => it.kind === 'file' && /\.(vrtour|zip)$/i.test(it.getAsFile()?.name || ''))
    .map(it => ({ file: it.getAsFile(), handle: it.getAsFileSystemHandle?.() ?? Promise.resolve(null) }));
  if (projectDrops.length) {
    const items = await Promise.all(projectDrops.map(async d => ({
      file: d.file, handle: await d.handle.then(h => (h?.kind === 'file' ? h : null), () => null),
    })));
    await openOrAddProjects(items);
    return;
  }
  const clip = [...e.dataTransfer.files].find(isVideoFile);
  if (clip) { openVideo(); setVideoFile(clip); return; }
  const files = [...e.dataTransfer.files].filter(isPhotoFile);
  const rect = document.getElementById('map').getBoundingClientRect();
  const fallback = map.containerPointToLatLng(L.point(e.clientX - rect.left, e.clientY - rect.top));
  // Several photos or a folder: the import assistant places, links and aligns them in one go.
  const folders = entries.some(en => en.isDirectory);
  if (files.length > 1 || folders) { openImport({ entries: folders ? entries : null, files, point: fallback }); return; }
  if (!files.length) return;
  for (const file of files) await addPhotoNode(file, fallback);
  if (S.nodes.length >= 2) fitAll();
});

niN.addEventListener('focus', () => pushUndo());
niN.addEventListener('input', () => {
  const n = gn(S.selId);
  if (!n) return;
  n.name = niN.value;
  refreshMarker(n);
  renderConnList();
  refreshCompareUi(n);
  renderNodeList();
  scheduleAutosave();
});

niP.addEventListener('change', async e => {
  const file = e.target.files[0];
  if (!file) return;
  const n = gn(S.selId);
  if (!n) return;
  pushUndo();
  const meta = await readPhotoMeta(file);
  n.photoName = file.name;
  n.photoUrl = URL.createObjectURL(file);
  n.photoFile = file;
  n.captureTime = meta.captureTime;
  n.captureOffset = meta.captureOffset;
  retireGooglePhoto(n);
  n.blurred = false;
  n.privacyChecked = false; // a new photo: not checked yet
  panoNodeId = null; // otherwise the panorama view keeps showing the old photo
  if (meta.hasGps) {
    n.lat = meta.lat;
    n.lng = meta.lng;
    n.hasGps = true;
    n.marker?.setLatLng([n.lat, n.lng]);
    refreshConnLines();
  }
  n.heading = meta.heading;
  n.origHeading = meta.heading;
  n.viewYaw = meta.heading != null ? normalizeRad(-degToRad(meta.heading)) : 0;
  n.origViewYaw = n.viewYaw;
  n.headingSource = meta.heading != null ? 'exif' : 'manual';
  updateSb();
  refreshMarker(n);
  e.target.value = '';
  scheduleAutosave();
});

niH.addEventListener('focus', () => pushUndo());
niH.addEventListener('input', () => {
  const n = gn(S.selId);
  if (!n) return;
  n.heading = parseHeadingInput(niH.value);
  n.headingSource = 'manual';
  if (n.heading != null) {
    n.viewYaw = northYaw(n);
    panoViewer?.rotate({ yaw: northYaw(n), pitch: 0 });
  }
  refreshMarker(n);
  headingMeta.textContent = n.heading == null ? t('not set') : t('set by hand');
  refreshCompareUi(n);
  scheduleAutosave();
});

// Turn a photo's north by a few degrees (or round, if it was taken facing
// backwards); the view above follows so it keeps facing the new north.
function nudgeHeading(n, delta) {
  if (!n) return;
  pushUndo();
  n.heading = normalizeHeading((n.heading ?? 0) + delta);
  n.headingSource = 'manual';
  n.viewYaw = northYaw(n);
  refreshHeadingUi(n);
  refreshMarker(n);
  refreshCompareUi(n);
  panoViewer?.rotate({ yaw: northYaw(n), pitch: 0 });
  scheduleAutosave();
}
document.querySelectorAll('[data-hnudge]').forEach(b => {
  b.onclick = () => nudgeHeading(gn(S.selId), Number(b.dataset.hnudge));
});

niS.addEventListener('change', () => {
  pushUndo();
  if (niS.checked) setTourStart(S.selId); else S.starts.delete(S.selId);
  refreshAllMarkers();
  scheduleAutosave();
});

function commitLatLng() {
  const n = gn(S.selId);
  if (!n) return;
  const lat = parseFloat(niLat.value);
  const lng = parseFloat(niLng.value);
  if (!Number.isFinite(lat) || !Number.isFinite(lng)) { refreshLatLngUi(n); return; }
  if (lat === n.lat && lng === n.lng) return;
  pushUndo();
  n.lat = Math.max(-90, Math.min(90, lat));
  n.lng = lng;
  n.marker?.setLatLng([n.lat, n.lng]);
  refreshConnLines();
  refreshCompareUi(n);
  refreshLatLngUi(n);
  positionQuickMenu();
  scheduleAutosave();
}
niLat.addEventListener('change', commitLatLng);
niLng.addEventListener('change', commitLatLng);
niLat.addEventListener('keydown', e => { if (e.key === 'Enter') niLat.blur(); });
niLng.addEventListener('keydown', e => { if (e.key === 'Enter') niLng.blur(); });

document.getElementById('bh-set').onclick = () => {
  const n = gn(S.selId);
  if (!n || !panoViewer) return;
  pushUndo();
  const pos = lockViewerPitch(panoViewer);
  n.heading = normalizeHeading(-radToDeg(pos.yaw));
  n.viewYaw = pos.yaw;
  n.headingSource = 'manual';
  refreshHeadingUi(n);
  refreshMarker(n);
  refreshCompareUi(n);
  toast(`${n.name}: heading set to ${n.heading.toFixed(1)}°`);
  scheduleAutosave();
};

document.getElementById('bh-reset').onclick = () => {
  const n = gn(S.selId);
  if (!n) return;
  pushUndo();
  n.heading = n.origHeading;
  n.viewYaw = n.origHeading != null ? normalizeRad(-degToRad(n.origHeading)) : 0;
  n.headingSource = n.origHeading != null ? 'exif' : 'manual';
  refreshHeadingUi(n);
  refreshMarker(n);
  if (n.heading != null) panoViewer?.rotate({ yaw: northYaw(n), pitch: 0 });
  refreshCompareUi(n);
  scheduleAutosave();
};

function removeNodeById(id) {
  const n = gn(id);
  if (!n) return;
  if (S.starts.has(n.id)) {
    // The start moves to another point of its tour (or, with none left, of the map).
    const next = tourOf(n.id)?.nodes.find(x => x.id !== n.id);
    S.starts.delete(n.id);
    const fallback = !S.starts.size && S.nodes.find(x => x.id !== n.id);
    if (next || fallback) S.starts.add((next || fallback).id);
  }
  if (n.marker) map.removeLayer(n.marker);
  S.conns.filter(c => c.from === n.id || c.to === n.id).forEach(c => {
    if (c.poly) map.removeLayer(c.poly);
    if (c.xMark) map.removeLayer(c.xMark);
  });
  S.conns = S.conns.filter(c => c.from !== n.id && c.to !== n.id);
  delete S.compareTargetByNode[n.id];
  S.nodes = S.nodes.filter(x => x.id !== n.id);
  if (S.selId === n.id) S.selId = null;
  S.multiSel.delete(n.id);
  if (S.connFrom === n.id) cancelConnect();
  if (panoNodeId === n.id) panoNodeId = null;
  if (cmpBaseNodeId === n.id) cmpBaseNodeId = null;
  if (cmpOverlayNodeId === n.id) cmpOverlayNodeId = null;
}

document.getElementById('bn-d').onclick = () => {
  if (!S.selId) return;
  pushUndo();
  removeNodeById(S.selId);
  tourChanged();
};

cmpTarget.onchange = () => {
  const n = gn(S.selId);
  if (!n) return;
  S.compareTargetByNode[n.id] = cmpTarget.value || null;
  refreshCompareUi(n);
};

document.getElementById('cmp-step').onclick = () => {
  const target = gn(cmpTarget.value);
  if (!target) return;
  jumpToNode(target.id, true);
};

document.getElementById('cmp-center').onclick = () => {
  const n = gn(S.selId);
  const t = gn(cmpTarget.value);
  if (!n || !t) return;
  alignCompareByHeading(n, t);
};

document.getElementById('cmp-swap').onclick = () => {
  cmpSwap = !cmpSwap;
  const n = gn(S.selId);
  if (!n) return;
  refreshCompareUi(n);
};

document.getElementById('walk-back').onclick = () => {
  while (walkHistory.length) {
    const prevId = walkHistory.pop();
    if (gn(prevId)) { jumpToNode(prevId, false); return; }
  }
};

document.getElementById('walk-start').onclick = () => {
  const g = tourOf(S.selId);
  if (!g) return;
  walkHistory.length = 0;
  jumpToNode(tourStart(g).id, false);
};

cmpOpacity.addEventListener('input', () => {
  cmpOverlayLayer.style.opacity = `${Number(cmpOpacity.value) / 100}`;
});
cmpOverlayLayer.style.opacity = `${Number(cmpOpacity.value) / 100}`;

document.getElementById('bdir').onclick = async () => {
  if (!window.showDirectoryPicker) {
    alert('This browser does not support saving directly to a folder.');
    return;
  }
  try {
    saveDirHandle = await window.showDirectoryPicker({ mode: 'readwrite' });
    await persistDirectoryHandle(saveDirHandle);
    updateSaveHint();
    toast(`Save target set to ${saveDirHandle.name}`);
  } catch (err) {
    if (err?.name !== 'AbortError') {
      alert(`Failed to set save target: ${err.message || err}`);
    }
  }
};

// ── Project save / open (.vrtour) ─────────────────
// A .vrtour file is a plain (uncompressed) zip holding project.json plus every
// node's photo under photos/<nodeId>/<filename>, so a whole tour can be moved
// between machines or reopened later without re-dropping the pictures. Photos
// are already JPEG-compressed, so "stored" entries keep saving fast and let
// opening slice each photo straight out of the file without copying it.
const PROJECT_FILE = 'tour-project.vrtour';

function photoPathOf(n) {
  return `photos/${n.id}/${n.photoName || 'photo.jpg'}`;
}

function projectData(nodes = S.nodes) {
  const ids = new Set(nodes.map(n => n.id));
  const starts = [...S.starts].filter(id => ids.has(id));
  const tours = {};
  nodes.forEach(n => { if (n.tourId && S.tourNames[n.tourId]) tours[n.tourId] = S.tourNames[n.tourId]; });
  return {
    format: 'vrtour',
    version: PROJECT_VERSION,
    nodes: nodes.map(n => ({
      id: n.id,
      name: n.name,
      lat: n.lat,
      lng: n.lng,
      heading: n.heading,
      origHeading: n.origHeading,
      viewYaw: n.viewYaw ?? null,
      origViewYaw: n.origViewYaw ?? null,
      headingSource: n.headingSource,
      hasGps: n.hasGps,
      photoName: n.photoName,
      photoPath: n.photoFile ? photoPathOf(n) : null,
      photoType: n.photoFile?.type || null,
      captureTime: n.captureTime ?? null,
      captureOffset: n.captureOffset ?? null,
      blurred: !!n.blurred,
      privacyChecked: !!n.privacyChecked,
      gsvPhotoId: n.gsvPhotoId ?? null,
      gsvStatus: n.gsvStatus ?? null,
      gsvShareLink: n.gsvShareLink ?? null,
      gsvSig: n.gsvSig ?? null,
      gsvConfirmed: !!n.gsvConfirmed,
      gsvUploadedAt: n.gsvUploadedAt ?? null,
      gsvObsolete: n.gsvObsolete || [],
      hotspots: n.hotspots || [],
      tourId: n.tourId ?? null,
      placeId: n.placeId, // missing: not decided yet; null: no place
      placeName: n.placeName ?? null,
      placeAuto: !!n.placeAuto,
      placeKind: n.placeKind ?? null,
      placeDist: n.placeDist ?? null,
      placeFor: n.placeFor ?? null,
      level: n.level, // missing: never set; null: none
      gsvViews: n.gsvViews ?? null,
      pmx: n.pmx ?? null, // on Panoramax: { instance, at }
    })),
    conns: S.conns.filter(c => ids.has(c.from) && ids.has(c.to)).map(c => ({ id: c.id, from: c.from, to: c.to })),
    startId: starts[0] ?? null,
    startIds: starts,
    tours,
    nxt: S.nxt,
  };
}

async function packProject(nodes = S.nodes) {
  const data = projectData(nodes);
  const entries = [{ name: 'project.json', data: new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' }) }];
  data.nodes.forEach(nd => {
    if (nd.photoPath) entries.push({ name: nd.photoPath, data: gn(nd.id).photoFile });
  });
  return buildZip(entries);
}

// ── Current project file (Ctrl+S) ─────────────────
// Where the File System Access API exists (Chrome, Edge), the opened or first
// saved .vrtour is remembered and Ctrl+S writes straight back into it. Other
// browsers fall back to downloading a copy each time.
let projectHandle = null;
let projectName = null;
let projectDirty = false;
const PROJECT_PICKER_TYPES = [{ description: 'Tour project', accept: { 'application/zip': ['.vrtour'] } }];
const PROJECT_OPEN_TYPES = [{ description: 'Tour project', accept: { 'application/zip': ['.vrtour', '.zip'], 'application/json': ['.json'] } }];

function updateTitle() {
  document.title = `${projectDirty ? '• ' : ''}${projectName || t('Untitled')} – ${t('Tour Editor')}`;
  document.getElementById('proj-title').textContent = projectName || t('Untitled');
  document.getElementById('proj-dirty').hidden = !projectDirty;
  if (appHasWindow) {
    fetch('/api/window-title', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ title: document.title }),
    }).catch(() => {});
  }
}
function markDirty() { if (!projectDirty) { projectDirty = true; updateTitle(); } }
function markClean() { projectDirty = false; updateTitle(); }

async function saveProject({ saveAs = false } = {}) {
  if (!S.nodes.length) {
    alert('No nodes found.');
    return;
  }
  let handle = saveAs ? null : projectHandle;
  if (!handle && window.showSaveFilePicker) {
    try {
      handle = await window.showSaveFilePicker({ suggestedName: projectName || PROJECT_FILE, types: PROJECT_PICKER_TYPES });
    } catch (err) {
      if (err?.name === 'AbortError') return;
      handle = null;
    }
  }
  try {
    setSaveStatus('saving', 'Packing project…');
    const zip = await packProject();
    if (handle) {
      const writable = await handle.createWritable();
      await writable.write(zip);
      await writable.close();
      projectHandle = handle;
      projectName = handle.name;
      await rebindPhotos(await handle.getFile(), handle);
      toast(`Saved ${handle.name}`);
      rememberRecent(handle);
    } else {
      await saveTextFile(projectName || PROJECT_FILE, zip);
    }
    markClean();
    setSaveStatus('saved', 'Saved');
  } catch (err) {
    console.error(err);
    setSaveStatus(null, 'Project save failed');
    alert(`Saving the project failed: ${err.message || err}`);
  }
}

// Photos opened from a .vrtour are slices of that file, and the browser refuses
// to read them once the file has been overwritten. After saving in place, point
// every photo (also those in the undo history) at the freshly written file.
async function rebindPhotos(file, handle = null) {
  const entries = await readZip(file);
  const swap = new Map();
  S.nodes.forEach(n => {
    const slice = n.photoFile && entries[photoPathOf(n)];
    if (!slice) return;
    const fresh = new File([slice], n.photoName || 'photo.jpg', { type: n.photoFile.type || 'image/jpeg' });
    notePhotoSource(fresh, handle);
    swap.set(n.photoFile, fresh);
  });
  swapPhotoFiles(swap);
  // In the app window: the autosave refers to the freshly written file from now on.
  const [ref] = (await fileRefs(handle ? [handle] : [])) || [];
  if (ref) S.nodes.forEach(n => { if (n.photoFile && entries[photoPathOf(n)]) photoRefs.set(n.photoFile, { ref, entry: photoPathOf(n), type: n.photoFile.type }); });
}

// Point the tour (and its undo history) at other File objects for the same photos.
function swapPhotoFiles(swap) {
  S.nodes.forEach(n => {
    const fresh = n.photoFile && swap.get(n.photoFile);
    if (!fresh) return;
    const key = photoKeys.get(n.photoFile);
    if (key) {
      photoKeys.set(fresh, key);
      if (unsavedPhotos.has(key)) unsavedPhotos.set(key, fresh);
    }
    const thumb = thumbCache.get(n.photoFile);
    if (typeof thumb === 'string') thumbCache.set(fresh, thumb);
    n.photoFile = fresh;
    if (n.photoUrl) URL.revokeObjectURL(n.photoUrl);
    n.photoUrl = URL.createObjectURL(fresh);
  });
  [...undoStack, ...redoStack].forEach(snap => snap.nodes.forEach(n => {
    if (swap.has(n.photoFile)) n.photoFile = swap.get(n.photoFile);
  }));
}

// In Chrome and Edge, photos of an opened .vrtour are read from that file on
// disk when needed. Before a part of the map is written over such a file, its
// photos are copied into memory: the old file is gone afterwards.
const photoSource = new WeakMap(); // photo File -> the file handle it was read from
const photoSourceHandles = new Set();
function notePhotoSource(file, handle) {
  if (typeof handle?.isSameEntry !== 'function') return; // the app window keeps them in memory anyway
  photoSource.set(file, handle);
  photoSourceHandles.add(handle);
}
async function detachPhotosFrom(target) {
  if (typeof target?.isSameEntry !== 'function') return;
  const same = [];
  for (const h of photoSourceHandles) if (await h.isSameEntry(target).catch(() => false)) same.push(h);
  if (!same.length) return;
  const files = new Set();
  S.nodes.forEach(n => { if (n.photoFile) files.add(n.photoFile); });
  [...undoStack, ...redoStack].forEach(snap => snap.nodes.forEach(n => { if (n.photoFile) files.add(n.photoFile); }));
  const swap = new Map();
  for (const f of files) {
    if (same.includes(photoSource.get(f))) swap.set(f, new File([await f.arrayBuffer()], f.name, { type: f.type }));
  }
  swapPhotoFiles(swap);
}

document.getElementById('bsave').onclick = () => saveProject();

const openInput = document.getElementById('bopen-file');
async function pickAndOpenProject() {
  if (!window.showOpenFilePicker) { openInput.click(); return; }
  let handle;
  try {
    [handle] = await window.showOpenFilePicker({ types: PROJECT_OPEN_TYPES });
  } catch (err) {
    if (err?.name !== 'AbortError') openInput.click();
    return;
  }
  await openProjectFile(await handle.getFile(), handle);
}
document.getElementById('bopen').onclick = pickAndOpenProject;
openInput.addEventListener('change', async () => {
  const file = openInput.files[0];
  openInput.value = '';
  if (file) await openProjectFile(file);
});

// .vrtour versions: 1 - points, links, one start point. 2 - also tours (a name and
// a start each) and places on Google Maps. A file is brought up to date as it is
// opened; one from a newer editor is refused rather than half read.
const PROJECT_VERSION = 2;

function upgradeProject(data, fileName) {
  if ((data.version || 1) < 2) {
    data = { ...data, version: 2, startIds: data.startId ? [data.startId] : [], tours: {} };
  }
  // Points without a tour, in a file without tours (older files, or a single tour
  // never imported): the tour named after the file, so it keeps a name next to others.
  if (!Object.keys(data.tours || {}).length) {
    const id = makeTourId();
    data = { ...data, tours: { [id]: fileName.replace(/\.(vrtour|zip|json)$/i, '') }, nodes: data.nodes.map(nd => ({ ...nd, tourId: nd.tourId || id })) };
  }
  return data;
}

// A .vrtour (or an older tour-project.json) as a snapshot; the map isn't touched.
async function readProjectFile(file, handle = null) {
  let data;
  let photos = {};
  const legacyJson = /\.json$/i.test(file.name);
  if (legacyJson) {
    // Older tour-project.json: positions and links only, photos must be dropped again.
    data = JSON.parse(await file.text());
  } else {
    photos = await readZip(file);
    if (!photos['project.json']) throw new Error('project.json missing - not a .vrtour file');
    data = JSON.parse(await photos['project.json'].text());
  }
  if (!Array.isArray(data.nodes)) throw new Error('No nodes in project');
  if ((data.version || 1) > PROJECT_VERSION) {
    throw new Error(t('It was saved by a newer version of the editor - update the editor to open it.'));
  }
  data = upgradeProject(data, file.name);
  const tourNames = { ...data.tours };
  const tourIdOf = nd => nd.tourId || null;
  const snap = {
      nodes: data.nodes.map(nd => {
        const blob = nd.photoPath && photos[nd.photoPath];
        return {
          id: nd.id, name: nd.name, lat: nd.lat, lng: nd.lng,
          heading: nd.heading ?? null, origHeading: nd.origHeading ?? null,
          viewYaw: nd.viewYaw ?? 0, origViewYaw: nd.origViewYaw ?? 0,
          headingSource: nd.headingSource || 'manual', hasGps: !!nd.hasGps,
          photoName: nd.photoName ?? null,
          photoFile: blob ? new File([blob], nd.photoName || 'photo.jpg', { type: nd.photoType || 'image/jpeg' }) : null,
          captureTime: nd.captureTime ?? undefined,
          captureOffset: nd.captureOffset ?? null,
          blurred: !!nd.blurred,
          privacyChecked: !!nd.privacyChecked,
          gsvPhotoId: nd.gsvPhotoId ?? null,
          gsvStatus: nd.gsvStatus ?? null,
          gsvShareLink: nd.gsvShareLink ?? null,
          gsvSig: nd.gsvSig ?? null,
          gsvConfirmed: !!nd.gsvConfirmed,
          gsvUploadedAt: nd.gsvUploadedAt ?? null,
          gsvObsolete: Array.isArray(nd.gsvObsolete) ? nd.gsvObsolete : [],
          hotspots: Array.isArray(nd.hotspots) ? nd.hotspots : [],
          tourId: tourIdOf(nd),
          placeId: nd.placeId, placeName: nd.placeName ?? null, placeAuto: !!nd.placeAuto,
          placeKind: nd.placeKind ?? null, placeDist: nd.placeDist ?? null, placeFor: nd.placeFor ?? null,
          level: nd.level, gsvViews: nd.gsvViews ?? null, pmx: nd.pmx ?? null,
        };
      }),
      conns: (data.conns || []).map(c => ({ id: c.id, from: c.from, to: c.to })),
      selId: null,
      starts: Array.isArray(data.startIds) ? data.startIds : (data.startId ? [data.startId] : []),
      tourNames,
      nxt: data.nxt ?? 1,
      legacyJson,
  };
  snap.nodes.forEach(n => { if (n.photoFile) notePhotoSource(n.photoFile, handle); });
  const [ref] = (await fileRefs(handle ? [handle] : [])) || [];
  if (ref) {
    snap.nodes.forEach((n, i) => {
      if (n.photoFile) photoRefs.set(n.photoFile, { ref, entry: data.nodes[i].photoPath, type: n.photoFile.type });
    });
  }
  return snap;
}

async function openProjectFile(file, handle = null, { ask = true } = {}) {
  if (ask && S.nodes.length && !confirm('Opening a project replaces the current tour (Ctrl+Z undoes it). Continue?')) return;
  try {
    const snap = await readProjectFile(file, handle);
    pushUndo();
    applySnapshot(snap, { refit: true });
    scheduleAutosave();
    projectHandle = snap.legacyJson ? null : handle;
    projectName = snap.legacyJson ? file.name.replace(/\.json$/i, '.vrtour') : file.name;
    markClean();
    if (projectHandle) rememberRecent(projectHandle);
    const missing = snap.nodes.filter(n => n.photoName && !n.photoFile).length;
    toast(missing
      ? `Opened ${file.name} - ${missing} photo(s) not included, drop them again`
      : `Opened ${file.name} (${snap.nodes.length} nodes)`);
  } catch (err) {
    console.error(err);
    alert(`Could not open ${file.name}: ${err.message || err}`);
  }
}

document.getElementById('bcsv').onclick = () => runExport('csv');

async function exportCsv(scope = null) {
  const nodes = scope?.nodes || S.nodes;
  if (!nodes.length) {
    alert('No nodes found.');
    return;
  }
  const missingPhotos = nodes.filter(n => !n.photoName);
  if (missingPhotos.length) {
    alert(t('These nodes have no photo: {names}', { names: missingPhotos.map(n => n.name).join(', ') }));
    return;
  }
  // The CSV names photos by file name (for one folder): the same name twice can't be told apart.
  const twice = [...new Set(nodes.map(n => n.photoName).filter((name, i, all) => all.indexOf(name) !== i))];
  if (twice.length && !confirm(t('Several points have a photo of the same file name ({names}) - in the CSV their links can\'t be told apart. Rename the files first for a correct CSV. Export anyway?', { names: twice.slice(0, 5).join(', ') }))) return;
  const idToFile = Object.fromEntries(nodes.map(n => [n.id, n.photoName]));
  const rows = nodes.map(n => {
    const connectsTo = S.conns
      .filter(c => c.from === n.id || c.to === n.id)
      .map(c => idToFile[c.from === n.id ? c.to : c.from] || '')
      .filter(Boolean)
      .join(';');
    return {
      filename: n.photoName || '',
      lat: n.lat ?? '',
      lng: n.lng ?? '',
      heading: n.heading == null ? '' : n.heading.toFixed(1),
      connects_to: connectsTo,
      memo: '',
    };
  });
  const csv = [
    ['filename', 'lat', 'lng', 'heading', 'connects_to', 'memo'],
    ...rows.map(row => [row.filename, row.lat, row.lng, row.heading, row.connects_to, row.memo]),
  ].map(r => r.map(csvCell).join(',')).join('\n');
  await saveTextFile(scope ? `${fileBase(scope.name)}.csv` : 'tour.csv', csv);
}

// PSV turns a panorama by -pan, so pan = -heading puts the photo's centre at its heading.
function sphereCorrectionOf(n) {
  return { pan: +normalizeRad(-degToRad(n.heading ?? 0)).toFixed(4) };
}

function buildTourConfig(nodes = S.nodes) {
  const ids = new Set(nodes.map(n => n.id));
  const startId = (nodes.find(n => S.starts.has(n.id)) || nodes[0]).id;
  const nodesConf = nodes.map(n => {
    const linked = getLinkedNodes(n.id).filter(o => ids.has(o.id));
    // Each panorama is turned so that yaw 0 faces north (sphereCorrection), so
    // link arrows sit at their real compass bearing and, after a jump, the viewer
    // keeps looking the way you walked - also between photos taken facing
    // different directions.
    const links = linked.map(o => {
      const yaw = normalizeRad(degToRad(bearingDeg(n, o)));
      return { nodeId: o.id, position: { yaw: +yaw.toFixed(3), pitch: -0.15 } };
    });
    return {
      id: n.id,
      name: n.name,
      panorama: n.photoName || 'placeholder.jpg',
      lat: n.lat,
      lng: n.lng,
      heading: n.heading,
      ...(n.heading != null ? { sphereCorrection: sphereCorrectionOf(n) } : {}),
      links,
      ...(n.hotspots?.length ? { markers: hotspotMarkers(n) } : {}),
    };
  });
  return { startId, nodesConf };
}

const previewBack = document.getElementById('preview-back');
const previewFrame = document.getElementById('preview-frame');
let pendingExport = null;
let previewScope = null; // with several tours: the one shown (and exported from the preview)

function openExportPreview() {
  if (!S.nodes.length) {
    alert('No nodes found.');
    return;
  }
  const cur = tourOf(S.selId);
  const g = manyTours() ? (cur?.index != null ? cur : getTours()[0]) : null;
  previewScope = g ? { name: tourName(g), nodes: g.nodes } : null;
  const nodes = previewScope?.nodes || S.nodes;
  const noPh = nodes.filter(n => !n.photoName);
  if (noPh.length && !confirm(`There are ${noPh.length} nodes without photos. Continue?`)) return;

  pendingExport = buildTourConfig(nodes);
  // The preview iframe has no access to the photo files by filename (they only exist
  // in memory), so swap in the blob URLs for the live preview. The actual export keeps
  // plain filenames in pendingExport.nodesConf, since tour-viewer.html is meant to sit
  // next to the real photo files on disk.
  // Nodes whose photo isn't loaded in this session would point at a bare filename the
  // iframe can't resolve, and one failing panorama breaks the whole viewer, so leave
  // them (and links to them) out of the preview.
  const loaded = new Set(nodes.filter(n => n.photoUrl).map(n => n.id));
  if (!loaded.size) {
    alert('None of the nodes has a photo loaded in this session - drop the photos again (or open a .vrtour project) to preview.');
    return;
  }
  const previewNodes = pendingExport.nodesConf
    .filter(nc => loaded.has(nc.id))
    .map(nc => ({
      ...nc,
      panorama: gn(nc.id).photoUrl,
      links: nc.links.filter(l => loaded.has(l.nodeId)),
    }));
  const previewStart = loaded.has(pendingExport.startId) ? pendingExport.startId : previewNodes[0].id;
  if (previewNodes.length < nodes.length) {
    toast(`${nodes.length - previewNodes.length} node(s) without a loaded photo are left out of the preview`);
  }
  document.getElementById('preview-tour').textContent = previewScope ? `· ${previewScope.name}` : '';
  previewFrame.srcdoc = genViewer(previewStart, previewNodes, { editable: true, libBase: new URL('vendor/', location.href).href });
  setPreviewAlignNode(previewStart);
  previewBack.classList.add('show');
}

let previewAlignId = null;
function setPreviewAlignNode(id) {
  const n = gn(id);
  previewAlignId = n ? id : null;
  document.getElementById('pv-align-name').textContent = n?.name || '';
  document.getElementById('pv-align-val').textContent = n?.heading != null ? `${n.heading.toFixed(1)}°` : '';
}
window.addEventListener('message', e => {
  if (e.source !== previewFrame.contentWindow || e.data?.type !== 'svt-node') return;
  setPreviewAlignNode(e.data.id);
});
document.querySelectorAll('[data-pvnudge]').forEach(b => {
  b.onclick = () => {
    const n = gn(previewAlignId);
    if (!n) return;
    nudgeHeading(n, Number(b.dataset.pvnudge));
    setPreviewAlignNode(n.id);
    if (S.selId === n.id) updateSb();
    previewFrame.contentWindow?.postMessage({ type: 'svt-heading', id: n.id, sphereCorrection: sphereCorrectionOf(n), markers: hotspotMarkers(n) }, '*');
  };
});

document.getElementById('bpreview').onclick = openExportPreview;
document.getElementById('preview-close').onclick = () => previewBack.classList.remove('show');

// tour-viewer.html + tour-config.json, to place next to the photos.
async function exportViewerFiles(scope = null) {
  const nodes = scope?.nodes || S.nodes;
  if (!nodes.length) {
    alert('No nodes found.');
    return;
  }
  // Built now: headings may have been turned in the preview.
  const { startId, nodesConf } = buildTourConfig(nodes);
  await saveTextFile('tour-viewer.html', genViewer(startId, nodesConf, { title: scope?.name }));
  await saveTextFile('tour-config.json', JSON.stringify({ startNodeId: startId, nodes: nodesConf }, null, 2));
  previewBack.classList.remove('show');
  setTimeout(() => toast('Exported tour-viewer.html and tour-config.json'), 200);
}
document.getElementById('preview-export').onclick = () => exportViewerFiles(previewScope);

// ── Command palette ───────────────────────────────
const ICONS = {
  plus: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>',
  eye: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>',
  download: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>',
  upload: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="16 16 12 12 8 16"/><line x1="12" y1="12" x2="12" y2="21"/><path d="M20.39 18.39A5 5 0 0 0 18 9h-1.26A8 8 0 1 0 3 16.3"/></svg>',
  save: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/></svg>',
  grid: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/></svg>',
  folder: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>',
  compass: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polygon points="16.24 7.76 14.12 14.12 7.76 16.24 9.88 9.88 16.24 7.76"/></svg>',
  undo: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="1 4 1 10 7 10"/><path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10"/></svg>',
  redo: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.13-9.36L23 10"/></svg>',
  keyboard: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="5" width="20" height="14" rx="2"/><line x1="6" y1="9" x2="6" y2="9"/><line x1="10" y1="9" x2="10" y2="9"/><line x1="14" y1="9" x2="14" y2="9"/><line x1="18" y1="9" x2="18" y2="9"/><line x1="6" y1="13" x2="18" y2="13"/></svg>',
  link: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 7h3a5 5 0 0 1 0 10h-3m-6 0H6a5 5 0 0 1 0-10h3"/><line x1="8" y1="12" x2="16" y2="12"/></svg>',
  target: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="6"/><circle cx="12" cy="12" r="2"/></svg>',
};

const cmdkBack = document.getElementById('cmdk-back');
const cmdkInput = document.getElementById('cmdk-input');
const cmdkList = document.getElementById('cmdk-list');
const cheatBack = document.getElementById('cheat-back');
let cmdkActive = 0;
let cmdkItems = [];

function staticCommands() {
  const cmds = [
    { icon: ICONS.plus, label: 'Import photos…', hint: 'Ctrl I', run: () => openImport() },
    { icon: ICONS.plus, label: 'Add point at map center', run: () => addEmptyNode(map.getCenter().lat, map.getCenter().lng) },
    { icon: ICONS.eye, label: 'Preview tour', run: openExportPreview },
    { icon: ICONS.download, label: 'Export viewer files (HTML + JSON)', run: () => runExport('viewer') },
    { icon: ICONS.upload, label: 'Upload to Google Street View', run: () => document.getElementById('bupload').click() },
    { icon: ICONS.save, label: 'Save project', hint: 'Ctrl S', run: () => saveProject() },
    { icon: ICONS.save, label: 'Save project as…', hint: 'Ctrl Shift S', run: () => saveProject({ saveAs: true }) },
    { icon: ICONS.folder, label: 'Open project (.vrtour)', hint: 'Ctrl O', run: pickAndOpenProject },
    { icon: ICONS.folder, label: 'Add project to the map…', hint: 'Ctrl Shift O', run: pickAndAddProjects },
    { icon: ICONS.link, label: 'Auto-connect nodes…', run: openAutoConnect },
    { icon: ICONS.target, label: 'Place photos from GPX track…', run: openGpx },
    { icon: ICONS.target, label: 'Move or turn a tour…', run: () => openShift() },
    { icon: ICONS.eye, label: 'Blur faces & plates in the selected photo…', run: () => openBlurTool(S.selId) },
    { icon: ICONS.eye, label: 'Check all photos for faces & plates…', run: () => openPrivacy() },
    { icon: ICONS.download, label: 'Export tour as website (.zip)', run: () => runExport('website') },
    { icon: ICONS.save, label: 'Export a tour as project (.vrtour)…', run: () => runExport('project') },
    { icon: ICONS.redo, label: 'Check for updates', run: () => checkForUpdates({ force: true, quiet: false }) },
    { icon: ICONS.compass, label: 'Headings from walking direction…', run: openWalkHeadings },
    { icon: ICONS.compass, label: 'North from the photos…', run: () => openNorthMatch() },
    { icon: ICONS.compass, label: 'North from the sun…', run: () => openSunNorth() },
    { icon: ICONS.eye, label: 'Cover the tripod…', run: () => openNadir() },
    { icon: ICONS.upload, label: 'Publish on Panoramax…', run: () => openPanoramax() },
    { icon: ICONS.download, label: 'Export photos with position and north (.zip)', run: () => runExport('photos') },
    { icon: ICONS.grid, label: 'Check tour…', run: () => openTourCheck() },
    { icon: ICONS.upload, label: 'Compare with Google…', run: () => openGoogleCompare() },
    { icon: ICONS.eye, label: 'Views on Google…', run: () => openViews() },
    { icon: ICONS.target, label: 'Places on Google Maps…', run: () => openPlacesSetup() },
    { icon: ICONS.keyboard, label: currentLang === 'de' ? 'Switch to English' : 'Auf Deutsch umschalten', run: () => setLang(currentLang === 'de' ? 'en' : 'de') },
    { icon: ICONS.grid, label: 'Export CSV manifest', run: () => document.getElementById('bcsv').click() },
    { icon: ICONS.folder, label: 'Choose export folder…', run: () => document.getElementById('bdir').click() },
    { icon: ICONS.undo, label: 'Undo', hint: 'Ctrl Z', run: doUndo },
    { icon: ICONS.redo, label: 'Redo', hint: 'Ctrl Y', run: doRedo },
    { icon: ICONS.keyboard, label: 'Show keyboard shortcuts', hint: '?', run: () => cheatBack.classList.add('show') },
    { icon: ICONS.eye, label: 'Switch between light and dark mode', run: toggleTheme },
    { icon: ICONS.target, label: 'Introduction', run: startTour },
    { icon: ICONS.target, label: 'Open the sample tour', run: () => openSample() },
  ];
  S.nodes.forEach(n => cmds.push({ icon: ICONS.target, label: n.name, hint: 'Jump to node', run: () => jumpToNode(n.id, false) }));
  return cmds;
}

function renderCmdk() {
  const q = cmdkInput.value.trim().toLowerCase();
  const all = staticCommands();
  cmdkItems = q ? all.filter(c => c.label.toLowerCase().includes(q) || t(c.label).toLowerCase().includes(q)) : all;
  cmdkActive = 0;
  cmdkList.innerHTML = '';
  if (!cmdkItems.length) {
    cmdkList.innerHTML = '<div class="cmdk-empty">No matching commands</div>';
    return;
  }
  cmdkItems.forEach((c, i) => {
    const row = document.createElement('div');
    row.className = `cmdk-item${i === 0 ? ' active' : ''}`;
    row.innerHTML = `${c.icon}<span class="t">${escapeHtml(c.label)}</span>${c.hint ? `<span class="h">${escapeHtml(c.hint)}</span>` : ''}`;
    row.onclick = () => { closeCmdk(); c.run(); };
    cmdkList.appendChild(row);
  });
}

function updateCmdkActive() {
  [...cmdkList.children].forEach((el, i) => el.classList.toggle('active', i === cmdkActive));
  cmdkList.children[cmdkActive]?.scrollIntoView({ block: 'nearest' });
}

function openCmdk() {
  cmdkInput.value = '';
  renderCmdk();
  cmdkBack.classList.add('show');
  setTimeout(() => cmdkInput.focus(), 10);
}
function closeCmdk() { cmdkBack.classList.remove('show'); cmdkInput.blur(); }
function toggleCmdk() { cmdkBack.classList.contains('show') ? closeCmdk() : openCmdk(); }

document.getElementById('bcmdk').onclick = openCmdk;
cmdkInput.addEventListener('input', renderCmdk);
cmdkInput.addEventListener('keydown', e => {
  if (e.key === 'ArrowDown') { e.preventDefault(); cmdkActive = Math.min(cmdkActive + 1, cmdkItems.length - 1); updateCmdkActive(); }
  else if (e.key === 'ArrowUp') { e.preventDefault(); cmdkActive = Math.max(cmdkActive - 1, 0); updateCmdkActive(); }
  else if (e.key === 'Enter') { e.preventDefault(); const c = cmdkItems[cmdkActive]; if (c) { closeCmdk(); c.run(); } }
});
document.getElementById('cheat-close').onclick = () => cheatBack.classList.remove('show');
document.querySelectorAll('.modal-back, .cmdk-back').forEach(back => {
  back.addEventListener('click', e => {
    if (e.target !== back || back.hasAttribute('data-keep-open')) return;
    back.classList.remove('show');
    if (document.activeElement instanceof HTMLElement) document.activeElement.blur();
  });
});

// ── Upload to Google Street View ─────────────────
const uplBack = document.getElementById('upl-back');
const uplCredsSetup = document.getElementById('upl-creds-setup');
const uplSignedInArea = document.getElementById('upl-signed-in-area');
const uplClientId = document.getElementById('upl-client-id');
const uplClientSecret = document.getElementById('upl-client-secret');
const uplCredsError = document.getElementById('upl-creds-error');
const uplSaveCreds = document.getElementById('upl-save-creds');
const uplChangeCreds = document.getElementById('upl-change-creds');
const uplDot = document.getElementById('upl-dot');
const uplStatus = document.getElementById('upl-status');
const uplSignin = document.getElementById('upl-signin');
const uplPlace = document.getElementById('upl-place');
const uplSummary = document.getElementById('upl-summary');
const uplStart = document.getElementById('upl-start');
const uplLog = document.getElementById('upl-log');

function uplLine(msg) {
  msg = t(msg);
  uplLog.textContent += `${msg}\n`;
  uplLog.scrollTop = uplLog.scrollHeight;
}

async function fetchApiStatus() {
  const r = await fetch('/api/status');
  return r.json();
}

async function refreshSignInStatus() {
  try {
    const j = await fetchApiStatus();
    uplDot.classList.toggle('on', j.signedIn);
    uplStatus.textContent = j.signedIn ? 'Signed in to Google' : j.expired ? 'Sign-in expired - please sign in again' : 'Not signed in';
    uplSignin.textContent = j.signedIn ? 'Sign in again' : 'Sign in with Google';
  } catch (err) {
    uplStatus.textContent = 'Could not reach local server. Start it with: python run_editor.py';
  }
}

function showCredsSetup() {
  uplCredsSetup.style.display = '';
  uplSignedInArea.style.display = 'none';
  uplCredsError.style.display = 'none';
  uplClientId.value = '';
  uplClientSecret.value = '';
}

function showSignedInArea() {
  uplCredsSetup.style.display = 'none';
  uplSignedInArea.style.display = '';
  refreshSignInStatus();
}

document.getElementById('bupload').onclick = async () => {
  if (!uploadRunning) showUploadSetup();
  uplBack.classList.add('show');
  try {
    const j = await fetchApiStatus();
    if (j.hasCredentials) showSignedInArea();
    else showCredsSetup();
  } catch (err) {
    showCredsSetup();
  }
};
document.getElementById('upl-close').onclick = () => uplBack.classList.remove('show');

function extractClientFields(text) {
  try {
    const data = JSON.parse(text);
    const block = data.installed || data.web;
    if (block?.client_id && block?.client_secret) {
      return { client_id: block.client_id, client_secret: block.client_secret };
    }
  } catch {}
  return null;
}

uplClientId.addEventListener('paste', e => {
  const text = (e.clipboardData || window.clipboardData).getData('text');
  const fields = extractClientFields(text);
  if (!fields) return;
  e.preventDefault();
  uplClientId.value = fields.client_id;
  uplClientSecret.value = fields.client_secret;
  toast('Detected client_secrets.json — filled both fields');
});

uplSaveCreds.onclick = async () => {
  uplCredsError.style.display = 'none';
  uplSaveCreds.disabled = true;
  try {
    const r = await fetch('/api/set-credentials', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ client_id: uplClientId.value, client_secret: uplClientSecret.value }),
    });
    const j = await r.json();
    if (!j.ok) throw new Error(j.error || 'Could not save credentials');
    showSignedInArea();
    toast('Google API client saved');
  } catch (err) {
    uplCredsError.textContent = err.message;
    uplCredsError.style.display = '';
  } finally {
    uplSaveCreds.disabled = false;
  }
};

uplChangeCreds.onclick = async () => {
  if (!confirm('Remove the saved Google API client and sign-in? You will need to paste a Client ID / Secret again.')) return;
  await fetch('/api/clear-credentials', { method: 'POST' });
  showCredsSetup();
};

uplSignin.onclick = async () => {
  uplSignin.disabled = true;
  uplStatus.textContent = 'Opening Google sign-in in your browser...';
  try {
    const r = await fetch('/api/login', { method: 'POST' });
    const j = await r.json();
    if (!j.ok) throw new Error(j.error || 'Sign-in failed');
    uplLine('Signed in.');
  } catch (err) {
    uplLine(`Sign-in error: ${err.message}`);
  } finally {
    uplSignin.disabled = false;
    refreshSignInStatus();
  }
};

function uploadCandidates() {
  // Points loaded from Google without their photo are in it too: their changes are sent.
  return S.nodes.filter(n => (n.photoFile || n.gsvPhotoId) && n.lat != null && n.lng != null && !isSamplePhoto(n));
}

// This one goes up as a photo (else at most its changes are sent).
const goesUp = (n, reupload = false) => !!n.photoFile && needsUpload(n, reupload);

// Not on Google yet, or Google lost it: these get uploaded.
function needsUpload(n, reupload = false) {
  return reupload || !n.gsvPhotoId || n.gsvStatus === 'missing';
}

function refreshUploadSummary() {
  const ready = uploadCandidates();
  const reupload = document.getElementById('upl-reupload').checked;
  const fresh = ready.filter(n => goesUp(n, reupload)).length;
  const onGoogle = ready.filter(n => !goesUp(n, reupload) && n.gsvPhotoId);
  const changed = onGoogle.filter(n => gsvChanged(n) || !n.gsvConfirmed).length;
  const samples = S.nodes.filter(isSamplePhoto).length;
  const missing = S.nodes.length - ready.length - samples;
  const set = (id, v) => {
    const el = document.getElementById(id);
    el.textContent = v;
    el.parentElement.classList.toggle('zero', !v);
  };
  set('upc-new', fresh);
  set('upc-changed', changed);
  set('upc-done', onGoogle.length);
  set('upc-missing', missing);
  const parts = [];
  if (samples) parts.push(t('The sample tour is not uploaded to Google - its photos are drawn, not real.'));
  if (missing) parts.push(t('{n} without a photo loaded (drop the file again to include it)', { n: missing }));
  if (onGoogle.some(n => !n.gsvConfirmed)) parts.push(t('Photos still being processed by Google get their links as soon as Google is ready.'));
  uplSummary.textContent = parts.join(' ');
  const obsolete = S.nodes.reduce((sum, n) => sum + (n.gsvObsolete?.length || 0), 0);
  document.getElementById('upl-delete-old-row').hidden = !obsolete;
  document.getElementById('upl-delete-old-label').textContent = t('Delete the old versions of replaced or blurred photos on Google ({n})', { n: obsolete });
  uplStart.disabled = !fresh && !changed;
}
document.getElementById('upl-reupload').addEventListener('change', refreshUploadSummary);

// ── Places on Google Maps ─────────────────────────
// Optional, recommended. With an API key of the user's own (kept by the app,
// never by this page), every photo can be tied to its place on Google Maps: the
// building or business it was taken at (Places API), its house number or its
// street (Geocoding API). Nothing about this happens unseen: the upload dialog
// shows for every tour which rule applied and, photo by photo, which place each
// gets - before anything is uploaded - and every choice can be changed, also for
// photos already on Google (pushed like a moved point, without uploading again).
// The side panel shows each point's place, the upload list each photo's.
//
// The automatic rule, in order:
//   1. A tour that lies at one place - all its points within 60 m of its centre -
//      with a building or business within 40 m of that centre: that place.
//   2. Otherwise each photo: its house number when that is within 25 m, else its street.
//   3. Nothing found: position only.
// It decides for photos not on Google yet; photos already there keep their place
// unless the user changes it.
const PLACE_RULE = { spread: 60, near: 40, address: 25 };
const NO_PLACE = { placeId: null, placeName: null, placeKind: null, placeDist: null, placeAuto: false };
// Where a point was when the rule decided for it: it is looked up again only once it moved.
const placeSpot = n => `${n.lat.toFixed(5)},${n.lng.toFixed(5)}`;
let placeLookups = { search: 0, address: 0 }; // really sent to Google in this upload dialog
const PLACE_KINDS = { place: 'building or business', address: 'house number', street: 'street', manual: 'Place ID typed in' };
let placesReady = null; // is a key saved? null: not asked yet
let uplPlaceGroups = [];
let uplPlacesPending = null;

async function placesApi(path, body = {}) {
  const r = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ lang: currentLang, ...body }) });
  return r.json();
}

async function placesConfigured(force = false) {
  if (placesReady == null || force) {
    try { placesReady = !!(await placesApi('/api/places')).configured; } catch { placesReady = false; }
  }
  return placesReady;
}

function placeText(n) {
  const kind = PLACE_KINDS[n.placeKind];
  return [n.placeName || n.placeId, kind && t(kind), n.placeDist != null && formatDistance(n.placeDist)].filter(Boolean).join(' · ');
}

// The side panel: where this point goes on Google Maps, and how that was decided.
function renderPlaceLine(n) {
  const el = document.getElementById('place-line');
  let html = '';
  if (n.placeId) {
    html = `${escapeHtml(t('Place on Google Maps:'))} <b>${escapeHtml(placeText(n))}</b> (${escapeHtml(n.placeAuto ? t('automatic') : t('chosen'))})`;
  } else if (n.placeId === null) {
    html = `${escapeHtml(t('Place on Google Maps:'))} ${escapeHtml(n.placeAuto ? t('none found - position only') : t('none - position only'))}`;
  } else if (placesReady && n.photoFile) {
    html = escapeHtml(t('Place on Google Maps: looked up when you open the upload'));
  }
  el.innerHTML = html;
  el.hidden = !html;
}

function openPlacesSetup() {
  closeMenus();
  const status = document.getElementById('places-status');
  document.getElementById('places-key').value = '';
  status.className = 'mini';
  status.textContent = '';
  placesConfigured(true).then(ok => {
    document.getElementById('places-remove').hidden = !ok;
    if (ok) { status.className = 'mini good'; status.textContent = t('A key is saved - places are looked up when you open the upload.'); }
  });
  openModal('places-back');
}

document.getElementById('places-save').onclick = async () => {
  const key = document.getElementById('places-key').value.trim();
  const status = document.getElementById('places-status');
  const btn = document.getElementById('places-save');
  if (!key) return;
  btn.disabled = true;
  status.className = 'mini';
  status.textContent = t('Checking the key…');
  const at = S.nodes.find(n => n.lat != null);
  try {
    const j = await placesApi('/api/places/key', { key, ...(at ? { lat: at.lat, lng: at.lng } : {}) });
    if (!j.ok) throw new Error(t(j.error || 'failed'));
    placesReady = true;
    const lines = [
      j.places ? t('Places API: works - buildings and businesses are found.') : t('Places API: not working ({msg}) - buildings and businesses are not found.', { msg: j.errors?.places }),
      j.geocoding ? t('Geocoding API: works - house numbers and streets are found.') : t('Geocoding API: not working ({msg}) - house numbers and streets are not found.', { msg: j.errors?.geocoding }),
    ];
    status.className = `mini ${j.places && j.geocoding ? 'good' : ''}`;
    status.textContent = `${t('Key saved.')} ${lines.join(' ')}`;
    document.getElementById('places-remove').hidden = false;
    document.getElementById('places-key').value = '';
    if (uplBack.classList.contains('show')) refreshUploadPlaces();
  } catch (err) {
    status.className = 'mini bad';
    status.textContent = t('Google did not accept the key: {msg}', { msg: err.message });
  } finally {
    btn.disabled = false;
  }
};

document.getElementById('places-remove').onclick = async () => {
  if (!confirm('Remove the saved Places API key?')) return;
  await placesApi('/api/places/key', { key: '' });
  placesReady = false;
  document.getElementById('places-remove').hidden = true;
  document.getElementById('places-status').textContent = '';
  if (uplBack.classList.contains('show')) refreshUploadPlaces();
};
document.getElementById('upl-place-setup').onclick = () => openPlacesSetup();
document.getElementById('upl-place-key').onclick = () => openPlacesSetup();

// The photos to publish, by tour; centre and spread of each.
function uploadPlaceGroups() {
  const byKey = new Map();
  uploadCandidates().forEach(n => {
    const g = tourOf(n.id);
    const key = g?.index != null ? `t${g.index}` : 'loose';
    if (!byKey.has(key)) byKey.set(key, { key, name: g?.index != null ? tourName(g) : t('Other points'), color: manyTours() ? g?.color : null, nodes: [] });
    byKey.get(key).nodes.push(n);
  });
  return [...byKey.values()].map(g => {
    const centre = { lat: g.nodes.reduce((a, n) => a + n.lat, 0) / g.nodes.length, lng: g.nodes.reduce((a, n) => a + n.lng, 0) / g.nodes.length };
    const spread = Math.max(...g.nodes.map(n => distanceMeters(centre, n)));
    return { ...g, centre, spread, radius: Math.min(300, Math.max(50, spread + 30)), pois: null, geo: new Map(), errors: [], busy: true };
  });
}

async function loadPois(g) {
  if (g.pois) return;
  try {
    const j = await placesApi('/api/places/nearby', { lat: g.centre.lat, lng: g.centre.lng, radius: g.radius });
    if (!j.ok) throw new Error(j.error === 'no_key' ? t('No Places API key saved.') : j.error);
    placeLookups.search += j.fetched || 0;
    g.pois = j.places.filter(p => p.lat != null).map(p => ({ ...p, dist: distanceMeters(g.centre, p) })).sort((a, b) => a.dist - b.dist);
  } catch (err) {
    g.pois = [];
    g.errors.push(t('Buildings and businesses: {msg}', { msg: err.message }));
  }
}

// House number and street of each photo (one lookup per photo, cached by the app).
async function loadAddresses(g, nodes) {
  const todo = nodes.filter(n => !g.geo.has(n.id));
  if (!todo.length) return;
  try {
    for (let at = 0; at < todo.length; at += 400) { // the app takes up to 500 points at a time
      const part = todo.slice(at, at + 400);
      const j = await placesApi('/api/places/geocode', { points: part.map(n => ({ lat: n.lat, lng: n.lng })) });
      if (!j.ok) throw new Error(j.error);
      placeLookups.address += j.fetched || 0;
      part.forEach((n, i) => g.geo.set(n.id, j.results[i]));
    }
  } catch (err) {
    g.errors.push(t('House numbers and streets: {msg}', { msg: err.message }));
  }
}

const asPlace = (p, kind, from) => ({
  placeId: p.id, placeName: p.name, placeKind: kind, placeDist: p.lat != null && kind !== 'street' ? Math.round(distanceMeters(from, p)) : null,
});

// Rule 2: house number within 25 m, else the street.
function addressPlace(g, n) {
  const geo = g.geo.get(n.id) || {};
  if (geo.address && distanceMeters(n, geo.address) <= PLACE_RULE.address) return asPlace(geo.address, 'address', n);
  if (geo.street) return asPlace(geo.street, 'street', n);
  return { ...NO_PLACE };
}

// The tour's one place by rule 1, or null.
function tourPlace(g) {
  return g.spread <= PLACE_RULE.spread ? g.pois?.find(p => p.dist <= PLACE_RULE.near) || null : null;
}

async function applyAutomatic(g, nodes) {
  if (!nodes.length) return;
  // The building search (the dearer one) only where rule 1 can apply at all.
  if (g.spread <= PLACE_RULE.spread) await loadPois(g);
  const one = tourPlace(g);
  if (!one) await loadAddresses(g, nodes);
  nodes.filter(n => one || g.geo.has(n.id))
    .forEach(n => Object.assign(n, one ? asPlace(one, 'place', n) : addressPlace(g, n), { placeAuto: true, placeFor: placeSpot(n) }));
}

// The upload waits for this (uplPlacesPending): every photo knows its place first.
function refreshUploadPlaces() {
  const run = (async () => {
    const auto = await placesConfigured();
    document.getElementById('upl-place-auto').hidden = !auto;
    document.getElementById('upl-place-manual').hidden = auto;
    document.getElementById('upl-place-keyrow').hidden = !auto;
    if (!auto) return;
    const groups = uploadPlaceGroups();
    uplPlaceGroups = groups;
    placeLookups = { search: 0, address: 0 };
    renderUploadPlaces();
    for (const g of groups) {
      // The rule decides for photos not on Google yet that nobody chose a place for -
      // once: again only for a point that was moved since.
      await applyAutomatic(g, g.nodes.filter(n => needsUpload(n) && (n.placeId === undefined || (n.placeAuto && n.placeFor !== placeSpot(n)))));
      g.busy = false;
      if (uplPlaceGroups === groups) renderUploadPlaces();
    }
    scheduleAutosave();
    refreshUploadSummary();
    updateSb();
  })();
  uplPlacesPending = run;
  run.finally(() => { if (uplPlacesPending === run) uplPlacesPending = null; });
  return run;
}

// Which choice describes a tour: the rule, one place for all, each photo its
// own house number or street, or no place.
function placeMode(g) {
  const ns = g.nodes;
  if (ns.every(n => n.placeAuto || n.placeId === undefined)) return 'auto';
  if (ns.every(n => n.placeId === null)) return 'none';
  if (ns.every(n => n.placeId && n.placeId === ns[0].placeId)) return 'tour';
  return 'photo';
}

function placeSummary(g) {
  const count = { place: 0, address: 0, street: 0, manual: 0, none: 0 };
  g.nodes.forEach(n => { count[n.placeId ? (n.placeKind || 'manual') : 'none']++; });
  return [
    count.place && t('{n} at a building or business', { n: count.place }),
    count.address && t('{n} at a house number', { n: count.address }),
    count.street && t('{n} at a street', { n: count.street }),
    count.manual && t('{n} with a Place ID typed in', { n: count.manual }),
    count.none && t('{n} with the position only', { n: count.none }),
  ].filter(Boolean).join(', ');
}

function placeWhy(g, mode) {
  if (g.busy) return t('Looking up places…');
  if (mode === 'auto') {
    const first = g.nodes.find(n => n.placeId);
    const one = first?.placeKind === 'place' && g.nodes.every(n => n.placeId === first.placeId) ? first : null;
    return one
      ? t('Automatic: the tour lies at one place (all points within {a} of its centre), with {name} less than {b} away - so all photos get it.', { a: formatDistance(PLACE_RULE.spread), name: one.placeName, b: formatDistance(PLACE_RULE.near) })
      : `${g.spread > PLACE_RULE.spread ? t('Automatic: the tour spreads over {d}, so each photo gets its own place: its house number if that is within {a}, else its street.', { d: formatDistance(2 * g.spread), a: formatDistance(PLACE_RULE.address) })
        : t('Automatic: no building or business within {a} of the tour, so each photo gets its house number if that is within {b}, else its street.', { a: formatDistance(PLACE_RULE.near), b: formatDistance(PLACE_RULE.address) })}`;
  }
  if (mode === 'tour') return t('Chosen by you: one place for all photos of the tour.');
  if (mode === 'none') return t('Chosen by you: the photos are published at their position only.');
  return t('Each photo has its own place - see below.');
}

// Everything found around a tour: its photos' house numbers and streets, buildings and businesses.
function placeCandidates(g) {
  const list = [];
  g.geo.forEach(geo => {
    if (geo.street) list.push({ ...geo.street, kind: 'street' });
    if (geo.address) list.push({ ...geo.address, kind: 'address' });
  });
  (g.pois || []).forEach(p => list.push({ ...p, kind: 'place' }));
  return list;
}

// The street most of the tour's photos are on.
function commonStreet(g) {
  const count = new Map();
  g.geo.forEach(geo => { if (geo.street) count.set(geo.street.id, [geo.street, (count.get(geo.street.id)?.[1] || 0) + 1]); });
  return [...count.values()].sort((a, b) => b[1] - a[1])[0]?.[0] || null;
}

function placeOptions(g, n, current) {
  const geo = g.geo.get(n?.id) || {};
  const seen = new Set();
  const opts = [];
  const add = (id, label) => { if (id && !seen.has(id)) { seen.add(id); opts.push(`<option value="${escapeHtml(id)}">${escapeHtml(label)}</option>`); } };
  if (n) {
    if (geo.address) add(geo.address.id, `${geo.address.name} · ${t('house number')} · ${formatDistance(distanceMeters(n, geo.address))}`);
    if (geo.street) add(geo.street.id, `${geo.street.name} · ${t('street')}`);
  } else {
    placeCandidates(g).filter(c => c.kind === 'street').forEach(c => add(c.id, `${c.name} · ${t('street')}`));
    placeCandidates(g).filter(c => c.kind === 'address').slice(0, 8).forEach(c => add(c.id, `${c.name} · ${t('house number')} · ${formatDistance(distanceMeters(g.centre, c))}`));
  }
  (g.pois || []).slice(0, 8).forEach(p => add(p.id, `${p.name} · ${p.type || t('building or business')} · ${formatDistance(n ? distanceMeters(n, p) : p.dist)}`));
  if (current?.placeId) add(current.placeId, placeText(current));
  opts.push(`<option value="">${escapeHtml(t('No place - position only'))}</option>`);
  opts.push(`<option value="__other">${escapeHtml(t('Other Place ID…'))}</option>`);
  return opts.join('');
}

function placeFromChoice(g, n, value) {
  if (!value) return { ...NO_PLACE };
  const found = placeCandidates(g).find(c => c.id === value);
  if (found) return asPlace(found, found.kind, n);
  const known = g.nodes.find(x => x.placeId === value);
  if (known) return { placeId: value, placeName: known.placeName, placeKind: known.placeKind, placeDist: known.placeKind === 'street' || !known.placeDist ? known.placeDist : null };
  return { placeId: value, placeName: value, placeKind: 'manual', placeDist: null };
}

function askPlaceId(after) {
  const input = document.createElement('input');
  input.className = 'pi';
  input.placeholder = 'ChIJ…';
  after.after(input);
  input.focus();
  return new Promise(resolve => {
    input.addEventListener('change', () => {
      const id = cleanPlaceId(input.value);
      if (/^[A-Za-z0-9_-]{16,}$/.test(id)) resolve(id);
      else { input.value = ''; input.placeholder = t('That does not look like a Place ID (letters, digits, - and _ only, e.g. ChIJ…).'); }
    });
  });
}

function placesChanged() {
  scheduleAutosave();
  renderUploadPlaces();
  refreshUploadSummary();
  updateSb();
}

function renderUploadPlaces() {
  const box = document.getElementById('upl-place-rows');
  box.innerHTML = '';
  if (!uplPlaceGroups.length) { box.innerHTML = `<div class="mini">${escapeHtml(t('Nothing to upload yet.'))}</div>`; return; }
  const cost = document.getElementById('upl-place-cost');
  const { search, address } = placeLookups;
  cost.textContent = search || address
    ? t('Sent to Google for this: {a} place search(es), {b} address lookup(s).', { a: search, b: address })
    : t('No lookups at Google were needed for this.');
  uplPlaceGroups.forEach(g => {
    const mode = placeMode(g);
    const el = document.createElement('div');
    el.className = 'place-tour';
    el.innerHTML = (uplPlaceGroups.length > 1 ? `<span class="pn"${g.color ? ` style="--tc:${g.color}"` : ''}>${escapeHtml(g.name)}</span>` : '')
      + `<select class="pi place-mode"${g.busy ? ' disabled' : ''}>
          <option value="auto">${escapeHtml(t('Automatic (recommended)'))}</option>
          <option value="photo">${escapeHtml(t('Each photo: its house number or street'))}</option>
          <option value="tour">${escapeHtml(t('One place for the whole tour…'))}</option>
          <option value="none">${escapeHtml(t('No place - position only'))}</option>
        </select>`
      + (mode === 'tour' ? `<select class="pi place-one">${placeOptions(g, null, g.nodes[0])}</select>` : '')
      + `<div class="mini why">${escapeHtml(placeWhy(g, mode))}${g.busy ? '' : ` <b>${escapeHtml(placeSummary(g))}.</b>`}</div>`
      + g.errors.map(e => `<div class="mini" style="color:var(--danger)">${escapeHtml(e)}</div>`).join('')
      + (g.busy ? '' : `<details><summary>${escapeHtml(t('Show the place of each photo ({n})', { n: g.nodes.length }))}</summary><div class="place-photos"></div></details>`);
    const modeSel = el.querySelector('.place-mode');
    modeSel.value = mode;
    modeSel.onchange = async () => {
      g.busy = true;
      renderUploadPlaces();
      if (modeSel.value === 'auto') {
        await applyAutomatic(g, g.nodes); // chosen on purpose: also for photos already on Google
      } else if (modeSel.value === 'photo') {
        await loadAddresses(g, g.nodes);
        g.nodes.forEach(n => Object.assign(n, addressPlace(g, n), { placeAuto: false }));
      } else if (modeSel.value === 'none') {
        g.nodes.forEach(n => Object.assign(n, NO_PLACE));
      } else {
        // To start with: the closest building or business, else the street most photos are on.
        await loadPois(g);
        await loadAddresses(g, g.nodes);
        const first = g.pois?.[0] ? { ...g.pois[0], kind: 'place' } : commonStreet(g) && { ...commonStreet(g), kind: 'street' };
        g.nodes.forEach(n => Object.assign(n, first ? asPlace(first, first.kind, n) : NO_PLACE, { placeAuto: false }));
      }
      g.busy = false;
      placesChanged();
    };
    const one = el.querySelector('.place-one');
    if (one) {
      one.value = g.nodes[0].placeId ?? '';
      one.onchange = async () => {
        const value = one.value === '__other' ? await askPlaceId(one) : one.value;
        g.nodes.forEach(n => Object.assign(n, placeFromChoice(g, n, value), { placeAuto: false }));
        placesChanged();
      };
    }
    const details = el.querySelector('details');
    details?.addEventListener('toggle', async () => {
      if (!details.open) return;
      const list = details.querySelector('.place-photos');
      list.innerHTML = `<div class="mini">${escapeHtml(t('Looking up places…'))}</div>`;
      await loadPois(g);
      await loadAddresses(g, g.nodes);
      list.innerHTML = '';
      g.nodes.forEach(n => {
        const row = document.createElement('div');
        row.className = 'place-photo';
        row.innerHTML = `<span class="fn" title="${escapeHtml(n.photoName || n.name)}">${escapeHtml(n.name)}</span><select class="pi">${placeOptions(g, n, n)}</select>`;
        const sel = row.querySelector('select');
        sel.value = n.placeId ?? '';
        sel.onchange = async () => {
          const value = sel.value === '__other' ? await askPlaceId(sel) : sel.value;
          Object.assign(n, placeFromChoice(g, n, value), { placeAuto: false });
          scheduleAutosave();
          refreshUploadSummary();
          updateSb();
          el.querySelector('.why').textContent = `${placeWhy(g, placeMode(g))} ${placeSummary(g)}.`;
          modeSel.value = placeMode(g);
        };
        list.appendChild(row);
      });
    });
    box.appendChild(el);
  });
}

// ── Place ID ──────────────────────────────────────
// Accept a bare ID or a pasted Google URL containing one.
function cleanPlaceId(v) {
  v = (v || '').trim();
  const m = /(?:place_id[:=]|query_place_id=|placeid=)([A-Za-z0-9_-]{10,})/i.exec(v);
  return m ? m[1] : v;
}

function checkPlaceIdInput() {
  const el = document.getElementById('upl-place');
  const msg = document.getElementById('upl-place-msg');
  const v = cleanPlaceId(el.value);
  if (v !== el.value.trim()) el.value = v;
  msg.className = 'mini';
  if (!v) { msg.textContent = ''; return true; }
  if (!/^[A-Za-z0-9_-]{16,}$/.test(v)) {
    msg.className = 'mini bad';
    msg.textContent = t('That does not look like a Place ID (letters, digits, - and _ only, e.g. ChIJ…).');
    return false;
  }
  msg.className = 'mini good';
  msg.textContent = v.startsWith('ChIJ') ? t('Format looks right - Google checks the ID itself during the upload.') : t('Unusual Place ID format - Google will check it during the upload.');
  return true;
}
document.getElementById('upl-place').addEventListener('input', checkPlaceIdInput);

// ── Progress view ─────────────────────────────────
const upRun = document.getElementById('up-run');
const upSetup = document.getElementById('up-setup');
const upList = document.getElementById('up-list');
const upBar = document.getElementById('up-bar');
let uploadRunning = false;
let uploadCancel = false;

function showUploadSetup() {
  upRun.hidden = true;
  upSetup.hidden = false;
  refreshUploadSummary();
  refreshUploadPlaces();
}

function setUploadStep(step) {
  const order = ['upload', 'process', 'link'];
  document.querySelectorAll('.up-step').forEach(el => {
    const i = order.indexOf(el.dataset.step), k = order.indexOf(step);
    el.classList.toggle('active', i === k);
    el.classList.toggle('done', step === 'finished' || i < k);
  });
}

function setOverall({ title, pct = null, sub = '', mode = 'normal' }) {
  if (title != null) document.getElementById('up-title').textContent = title;
  document.getElementById('up-pct').textContent = pct == null ? '' : `${Math.round(pct)}%`;
  document.getElementById('up-bar-fill').style.width = `${pct ?? 0}%`;
  upBar.classList.toggle('indet', mode === 'indet');
  upBar.classList.toggle('success', mode === 'success');
  upBar.classList.toggle('stopped', mode === 'stopped');
  document.getElementById('up-sub').textContent = sub;
}

function showNotice(text, kind = 'warn') {
  const el = document.getElementById('up-notice');
  el.hidden = !text;
  el.className = `up-notice ${kind}`;
  el.textContent = text || '';
}

const CHECK_ICON = '<svg class="up-check" viewBox="0 0 24 24" fill="none" stroke="var(--success)" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>';
const FAIL_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="var(--danger)" stroke-width="2.5" stroke-linecap="round"><path d="M7 7l10 10M17 7L7 17"/></svg>';
const SKIP_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="var(--text-faint)" stroke-width="2.5" stroke-linecap="round"><path d="M5 12h14"/></svg>';

function buildUploadRows(nodes) {
  upList.innerHTML = '';
  nodes.forEach((n, i) => {
    const row = document.createElement('div');
    row.className = 'up-row';
    row.style.setProperty('--i', i);
    row.dataset.node = n.id;
    const thumb = requestThumb(n);
    row.innerHTML = `<img class="up-thumb" data-thumb="${n.id}" alt=""${thumb ? ` src="${thumb}"` : ''}>
      <div class="up-main"><div class="up-name" data-no-i18n>${escapeHtml(n.photoName || n.name)}${n.placeId ? `<span class="up-pl">→ ${escapeHtml(placeText(n))}</span>` : ''}</div><div class="up-rowbar"><div></div></div><div class="up-msg"></div></div>
      <div class="up-icon"></div>`;
    upList.appendChild(row);
    setRow(n, 'waiting', t('Waiting'));
  });
}

function setRow(n, state, msg, pct = null) {
  const row = upList.querySelector(`.up-row[data-node="${n.id}"]`);
  if (!row) return;
  if (row.dataset.state !== state) {
    row.dataset.state = state;
    row.querySelector('.up-icon').innerHTML = state === 'done' ? CHECK_ICON : state === 'failed' ? FAIL_ICON : state === 'skipped' ? SKIP_ICON : '';
    if (state === 'uploading') row.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  }
  if (msg != null) row.querySelector('.up-msg').textContent = msg;
  if (pct != null) row.querySelector('.up-rowbar > div').style.width = `${pct}%`;
}

function formatBytes(b) {
  return b > 1e9 ? `${(b / 1e9).toFixed(2)} GB` : `${(b / 1e6).toFixed(1)} MB`;
}
function formatEta(sec) {
  if (!Number.isFinite(sec) || sec <= 0) return '';
  if (sec < 60) return t('about {n} s left', { n: Math.max(5, Math.round(sec / 5) * 5) });
  return t('about {n} min left', { n: Math.round(sec / 60) });
}

// Google takes photos of up to 75 MB; bigger ones are encoded again (smaller) for the upload.
const GOOGLE_MAX = 75 * 1024 * 1024 - 256 * 1024;
const UPLOAD_PARALLEL = 3;

async function photoForGoogle(n) {
  if (n.photoFile.size <= GOOGLE_MAX) return n.photoFile;
  const smaller = await rewritePhoto(n, null, null);
  uplLine(`${n.photoName}: ${t('made smaller for Google ({a} → {b})', { a: formatBytes(n.photoFile.size), b: formatBytes(smaller.size) })}`);
  return smaller;
}

// Send one photo: browser -> this app (XHR, with progress), and while the app
// passes it on to Google, poll how far that got.
function uploadOne(n, placeId, onBytes, file = n.photoFile) {
  return new Promise(resolve => {
    const job = `j${Date.now().toString(36)}${Math.random().toString(36).slice(2, 7)}`;
    const params = new URLSearchParams({ filename: n.photoName, lat: n.lat, lng: n.lng, job });
    if (n.heading != null) params.set('heading', n.heading);
    if (placeId) params.set('place_id', placeId);
    if (n.level) { params.set('level', n.level.number); params.set('level_name', n.level.name || ''); }
    const total = file.size;
    let local = 0, remote = 0, phase = 'local', finished = false;
    // A progress poll still in flight when the upload ends must not report anymore.
    const report = () => { if (!finished) onBytes(Math.min(total, local * 0.15 + remote * 0.85), phase); };
    const poll = setInterval(async () => {
      try {
        const p = await (await fetch(`/api/upload-progress?job=${job}`, { cache: 'no-store' })).json();
        if (p.total) { remote = p.sent; phase = p.phase; report(); }
      } catch {}
    }, 300);
    const xhr = new XMLHttpRequest();
    xhr.open('POST', `/api/upload-photo?${params}`);
    xhr.setRequestHeader('X-Editor-Key', EDITOR_KEY);
    xhr.upload.onprogress = e => { if (e.lengthComputable) { local = e.loaded; report(); } };
    xhr.onload = () => {
      finished = true;
      clearInterval(poll);
      let j = {};
      try { j = JSON.parse(xhr.responseText); } catch {}
      if (xhr.status === 401) j = { ok: false, error: 'not_signed_in', kind: 'auth' };
      resolve(j);
    };
    xhr.onerror = () => { finished = true; clearInterval(poll); resolve({ ok: false, error: t('The editor app is not reachable.'), kind: 'network' }); };
    xhr.send(file);
  });
}

// Plain-language reasons for what Google answers most often.
function explainUploadError(j) {
  if (j.kind === 'auth') return t('Not signed in (or the sign-in expired) - click "Sign in with Google".');
  if (j.kind === 'place') return t('Google rejected the Place ID.');
  const msg = j.error || '';
  if (j.status === 401 || j.status === 403) return t('Google refused access ({s}): {msg} - sign in again, and check that the Street View Publish API is enabled.', { s: j.status, msg });
  if (j.status === 429) return t('Too many requests to Google - wait a few minutes and try again.');
  if (/too large|size/i.test(msg)) return t('The photo is too large for Google (max 75 MB).');
  return j.status ? `${t('Google error')} ${j.status}: ${msg}` : t(msg);
}

// Find photos that are already on Google at the same spot, so an interrupted or
// confused earlier upload is picked up again instead of uploaded twice.
async function adoptExistingGooglePhotos(nodes) {
  let photos;
  try {
    const j = await (await fetch('/api/list-photos', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' })).json();
    if (!j.ok) return [];
    photos = j.photos.filter(p => p.id && p.lat != null);
  } catch { return []; }
  const used = new Set(S.nodes.filter(n => n.gsvConfirmed && n.gsvPhotoId).map(n => n.gsvPhotoId));
  const adopted = [];
  nodes.forEach(n => {
    let best = null, bestD = 1; // within 1 m
    photos.forEach(p => {
      if (used.has(p.id)) return;
      const d = distanceMeters(n, { lat: p.lat, lng: p.lng });
      if (d < bestD) { best = p; bestD = d; }
    });
    if (!best) return;
    used.add(best.id);
    Object.assign(n, { gsvPhotoId: best.id, gsvConfirmed: true, gsvStatus: best.status, gsvShareLink: best.shareLink || null, gsvSig: null });
    adopted.push(n);
  });
  return adopted;
}

// Push position / heading / links of photos Google already knows. The links go along only
// when they changed; then links on Google to photos this project doesn't have (loaded from
// Google for a part of the map only, or linked with another tool) are kept.
const gsvLinksOf = n => getLinkedNodes(n.id).filter(o => o.gsvConfirmed && o.gsvPhotoId).map(o => o.gsvPhotoId).sort();
function gsvLinksChanged(n) {
  let last = null;
  try { last = JSON.parse(n.gsvSig)?.[3]; } catch {}
  return !Array.isArray(last) || JSON.stringify(last) !== JSON.stringify(gsvLinksOf(n));
}

async function pushGoogleChanges(nodes = S.nodes) {
  let items = nodes.filter(n => n.gsvPhotoId && n.gsvConfirmed && gsvChanged(n));
  if (!items.length) return { ok: 0, failed: 0, items };
  if (items.some(n => gsvLinksChanged(n) && !n.gsvRemote)) await refreshGoogleStatus({ quiet: true });
  // Without knowing Google's links (offline), a photo whose links changed waits for the next try.
  items = items.filter(n => !gsvLinksChanged(n) || n.gsvRemote);
  if (!items.length) return { ok: 0, failed: 0, items };
  const known = new Set(S.nodes.map(n => n.gsvPhotoId).filter(Boolean));
  const sent = new Map(items.map(n => [n, gsvLinksChanged(n)
    ? [...gsvLinksOf(n), ...(n.gsvRemote.connections || []).filter(id => !known.has(id))] : null]));
  const r = await fetch('/api/update-photos', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      items: items.map(n => ({
        id: n.gsvPhotoId, lat: n.lat, lng: n.lng, heading: n.heading,
        ...(sent.get(n) ? { connections: sent.get(n) } : {}),
        ...(n.placeId !== undefined ? { placeId: n.placeId } : {}),
        ...(n.level !== undefined ? { level: n.level } : {}),
      })),
    }),
  });
  const j = await r.json();
  if (!j.ok) throw new Error(j.error || 'failed');
  let ok = 0, failed = 0;
  (j.results || []).forEach(res => {
    const n = items.find(x => x.gsvPhotoId === res.id);
    if (!n) return;
    if (res.placeRejected) {
      Object.assign(n, NO_PLACE);
      uplLine(`${n.photoName}: ${t('Google did not accept its place - kept without one')}`);
    }
    if (res.ok) {
      n.gsvSig = gsvSignature(n);
      if (sent.get(n) && n.gsvRemote) n.gsvRemote.connections = sent.get(n);
      ok++;
    } else failed++;
    uplLine(`${t('update')} ${n.photoName || n.name}: ${res.ok ? 'OK' : `${t('FAILED')} (${res.error})`}`);
  });
  scheduleAutosave({ edit: false });
  return { ok, failed, items };
}

let uplSkipCheck = false;

// Delete the replaced (e.g. unblurred) versions of a photo from Google.
async function deleteObsoleteGooglePhotos(n) {
  try {
    const r = await fetch('/api/delete-photos', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ids: n.gsvObsolete }) });
    const j = await r.json();
    if (!j.ok) throw new Error(j.error || 'failed');
    const done = new Set(j.results.filter(x => x.ok).map(x => x.id));
    n.gsvObsolete = n.gsvObsolete.filter(id => !done.has(id));
    if (done.size) uplLine(`${n.photoName}: ${t('old version deleted on Google')}`);
    j.results.filter(x => !x.ok).forEach(x => uplLine(`${n.photoName}: ${t('could not delete the old version on Google: {msg}', { msg: x.error })}`));
  } catch (err) {
    uplLine(`${n.photoName}: ${t('could not delete the old version on Google: {msg}', { msg: err.message })}`);
  }
}

uplStart.onclick = async () => {
  if (uploadRunning) return;
  try {
    await runUpload();
  } catch (err) {
    console.error(err);
    showNotice(t('Something went wrong: {msg}', { msg: err.message || err }), 'error');
    setOverall({ title: t('Stopped'), pct: null, mode: 'stopped' });
    document.getElementById('up-cancel').hidden = true;
    document.getElementById('up-finish').hidden = false;
  } finally {
    if (uploadRunning) setAppBusy(false);
    uploadRunning = false;
  }
};

// The app window asks before closing while this is set (the question comes from here, translated).
function setAppBusy(busy, question) {
  fetch('/api/busy', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ question: busy ? (question || t('An upload to Google is still running. Quit anyway? Photos that are not uploaded yet stay in your tour and can be uploaded later.')) : null }),
  }).catch(() => {});
}

async function runUpload() {
  const nodes = uploadCandidates();
  if (!nodes.length) {
    alert(S.nodes.some(isSamplePhoto) ? t('The sample tour is not uploaded to Google - its photos are drawn, not real.') : t('No nodes with a loaded photo and GPS position to upload.'));
    return;
  }
  if (!checkPlaceIdInput()) return;
  if (!uplSkipCheck) {
    const issues = await checkTour();
    const ids = new Set(nodes.map(n => n.id));
    if (issues.some(i => i.level === 'error' && ids.has(i.id))) {
      openTourCheck({ onContinue: () => { uplSkipCheck = true; uplStart.click(); } });
      return;
    }
  }
  if (!uplReviewed) {
    uplSkipCheck = false; // "Back" and Start again checks again; "Publish" goes straight on
    openReview(nodes);
    return;
  }
  uplSkipCheck = false;
  uplReviewed = false;

  const reupload = document.getElementById('upl-reupload').checked;
  let placeId = cleanPlaceId(uplPlace.value) || null; // typed by hand: for points without a place of their own
  const rejectedPlaces = new Set();
  const deleteOld = document.getElementById('upl-delete-old').checked;
  if (uplPlacesPending) {
    uplStart.disabled = true;
    await uplPlacesPending; // every photo knows its place before anything goes up
    uplStart.disabled = false;
  }
  uploadRunning = true;
  setAppBusy(true);
  uploadCancel = false;
  upSetup.hidden = true;
  upRun.hidden = false;
  document.getElementById('up-done').hidden = true;
  document.getElementById('up-finish').hidden = true;
  document.getElementById('up-contrib').hidden = true;
  document.getElementById('up-cancel').hidden = false;
  document.getElementById('up-cancel').disabled = false;
  document.getElementById('up-cancel').textContent = t('Cancel');
  uploadPhase = 'upload';
  showNotice('');
  uplLog.textContent = '';
  buildUploadRows(nodes);

  // 1) Pick up photos that are already on Google, then upload the rest.
  setUploadStep('upload');
  setOverall({ title: t('Checking your photos on Google…'), mode: 'indet' });
  let todo = nodes.filter(n => goesUp(n, reupload));
  // Only photos the editor had uploaded before but lost track of are matched
  // to photos on Google; a new photo at a known spot must really be uploaded.
  const lost = todo.filter(n => n.gsvStatus === 'missing');
  if (lost.length && !reupload) {
    const adopted = await adoptExistingGooglePhotos(lost);
    adopted.forEach(n => {
      setRow(n, 'done', t('Already on Google - linked to it'));
      uplLine(`${n.photoName}: ${t('found on Google, not uploaded again')}`);
    });
    todo = todo.filter(n => !adopted.includes(n));
  }
  const fresh = todo.filter(n => !n.gsvPhotoId && n.gsvStatus !== 'missing');
  if (fresh.length && !reupload) {
    let photos = [];
    try {
      const j = await (await fetch('/api/list-photos', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' })).json();
      if (j.ok) photos = j.photos;
    } catch {}
    const used = new Set(S.nodes.filter(n => n.gsvConfirmed && n.gsvPhotoId).map(n => n.gsvPhotoId));
    const matches = findNearbyGooglePhotos(fresh, photos, used);
    if (matches.size && confirm(t('{n} of the photos to upload seem to be on Google already, at exactly the same spot (probably an earlier upload). Link them to those instead of uploading them again?', { n: matches.size }))) {
      matches.forEach((near, n) => {
        const p = near.sort((a, b) => String(b.uploadTime || '').localeCompare(String(a.uploadTime || ''))).find(x => !used.has(x.id));
        if (!p) return;
        used.add(p.id);
        Object.assign(n, { gsvPhotoId: p.id, gsvConfirmed: true, gsvStatus: p.status, gsvShareLink: p.shareLink || null, gsvSig: null });
        setRow(n, 'done', t('Already on Google - linked to it'));
        uplLine(`${n.photoName}: ${t('found on Google, not uploaded again')}`);
      });
      todo = todo.filter(n => !n.gsvConfirmed);
    }
  }
  nodes.filter(n => !todo.includes(n) && !upList.querySelector(`.up-row[data-node="${n.id}"][data-state="done"]`))
    .forEach(n => setRow(n, 'skipped', t('Already on Google')));

  const totalBytes = todo.reduce((sum, n) => sum + Math.min(n.photoFile.size, GOOGLE_MAX), 0);
  const sentBytes = new Map(); // node -> bytes of it sent so far
  const samples = [];
  let uploaded = 0, failed = 0, authFailed = false, begun = 0;
  const sentSoFar = () => [...sentBytes.values()].reduce((a, b) => a + b, 0);

  const sendOne = async n => {
    if (uploadCancel) { setRow(n, 'skipped', t('Cancelled')); return; }
    begun++;
    setRow(n, 'uploading', t('Uploading…'), 0);
    setOverall({ title: t('Uploading photo {a} of {b}', { a: begun, b: todo.length }), pct: totalBytes ? (sentSoFar() / totalBytes) * 100 : 0 });
    let file;
    try {
      file = await photoForGoogle(n);
    } catch (err) {
      failed++;
      setRow(n, 'failed', t('The photo could not be made smaller: {msg}', { msg: err.message || err }), 100);
      return;
    }
    const onBytes = (bytes, phase) => {
      const now = performance.now();
      sentBytes.set(n, bytes);
      const sent = sentSoFar();
      samples.push([now, sent]);
      while (samples.length > 2 && now - samples[0][0] > 6000) samples.shift();
      const [t0, b0] = samples[0];
      const speed = now - t0 > 500 ? (sent - b0) / ((now - t0) / 1000) : 0;
      const pct = totalBytes ? Math.min(100, (sent / totalBytes) * 100) : 100;
      setRow(n, 'uploading', phase === 'creating' ? t('Registering with Google…') : `${formatBytes(bytes)} / ${formatBytes(file.size)}`, (bytes / file.size) * 100);
      setOverall({
        title: t('Uploading photo {a} of {b}', { a: begun, b: todo.length }),
        pct,
        sub: [`${formatBytes(sent)} / ${formatBytes(totalBytes)}`, speed > 0 ? `${(speed / 1e6).toFixed(1)} MB/s` : '', speed > 0 ? formatEta((totalBytes - sent) / speed) : ''].filter(Boolean).join(' · '),
      });
    };
    const ownPlace = n.placeId !== undefined;
    const pid = ownPlace ? n.placeId : placeId;
    let j = await uploadOne(n, pid && !rejectedPlaces.has(pid) ? pid : null, onBytes, file);
    if (!j.ok && j.kind === 'place') {
      // Google refused the Place ID: try this photo once without it.
      uplLine(`${n.photoName}: ${t('Place ID rejected')} (${j.error})`);
      const retry = await uploadOne(n, null, onBytes, file);
      if (retry.ok && ownPlace) {
        rejectedPlaces.add(pid);
        showNotice(t('Google did not accept the place "{name}" ({msg}). Those photos are uploaded with their position only.', { name: n.placeName || pid, msg: j.error }));
        S.nodes.forEach(x => {
          if (x.placeId !== pid) return;
          Object.assign(x, NO_PLACE);
          const label = upList.querySelector(`.up-row[data-node="${x.id}"] .up-pl`);
          if (label) label.textContent = `→ ${t('position only - Google did not take the place')}`;
        });
      } else if (retry.ok) {
        placeId = null;
        showNotice(t('Google did not accept the Place ID "{id}" ({msg}). The photos are uploaded without a place - you can find the right ID with the Place ID Finder (see the help in the upload dialog).', { id: uplPlace.value.trim(), msg: j.error }));
      }
      j = retry;
    }
    sentBytes.set(n, Math.min(n.photoFile.size, GOOGLE_MAX));
    if (j.ok) {
      Object.assign(n, { gsvPhotoId: j.photoId, gsvUploadedAt: Date.now(), gsvConfirmed: false, gsvStatus: 'pending', gsvSig: null, gsvShareLink: null });
      uploaded++;
      setRow(n, 'processing', t('Uploaded - Google is processing it'), 100);
      uplLine(`${n.photoName}: OK (${j.photoId})`);
      if (deleteOld && n.gsvObsolete?.length) await deleteObsoleteGooglePhotos(n);
      scheduleAutosave();
    } else {
      failed++;
      const reason = explainUploadError(j);
      setRow(n, 'failed', reason, 100);
      uplLine(`${n.photoName}: ${t('FAILED')} (${j.error})`);
      if (j.kind === 'auth') { authFailed = true; uploadCancel = true; }
    }
  };
  // The first photo alone (a Place ID or sign-in Google refuses shows up before the others go),
  // then a few at a time.
  let next = 0;
  if (todo.length) await sendOne(todo[next++]);
  await Promise.all(Array.from({ length: Math.min(UPLOAD_PARALLEL, todo.length - next) }, async () => {
    while (next < todo.length) await sendOne(todo[next++]);
  }));
  // While waiting for Google, the button skips the wait instead (links follow in the background).
  // (If the upload itself was cancelled or sign-in failed, there is nothing to wait for.)
  uploadPhase = 'wait';
  document.getElementById('up-cancel').disabled = uploadCancel;
  document.getElementById('up-cancel').textContent = t("Don't wait");
  if (authFailed) showNotice(t('Not signed in - click "Sign in with Google" first.'), 'error');
  refreshAllMarkers();

  // 2) Wait for Google to accept the new photos, 3) then set links and positions.
  await finishGoogleSync(nodes, { uploaded, failed, waitMs: uploaded ? 150000 : 20000 });
}

async function finishGoogleSync(nodes, { uploaded = 0, failed = 0, waitMs = 20000 } = {}) {
  setUploadStep('process');
  const deadline = Date.now() + waitMs;
  const pendingNodes = () => nodes.filter(n => n.gsvPhotoId && !n.gsvConfirmed);
  while (true) {
    await refreshGoogleStatus({ quiet: true });
    nodes.filter(n => n.gsvPhotoId && n.gsvConfirmed).forEach(n => {
      const row = upList.querySelector(`.up-row[data-node="${n.id}"]`);
      if (row?.dataset.state === 'processing') setRow(n, 'processing', t('Google has it - setting links next'), 100);
    });
    const pending = pendingNodes().length;
    if (!pending || Date.now() > deadline || uploadCancel) break;
    const known = nodes.filter(n => n.gsvPhotoId).length - pending;
    setOverall({
      title: t('Google is processing the photos…'),
      pct: null,
      mode: 'indet',
      sub: t('{a} of {b} ready - this usually takes a few minutes', { a: known, b: known + pending }),
    });
    await new Promise(r => setTimeout(r, 8000));
  }

  setUploadStep('link');
  setOverall({ title: t('Setting links, position and heading…'), mode: 'indet' });
  let linkResult = { ok: 0, failed: 0, items: [] };
  try {
    linkResult = await pushGoogleChanges(nodes);
  } catch (err) {
    uplLine(t('Updating on Google failed: {msg}', { msg: err.message }));
  }
  linkResult.items.forEach(n => {
    const row = upList.querySelector(`.up-row[data-node="${n.id}"]`);
    if (!row || row.dataset.state === 'failed') return;
    if (gsvChanged(n)) setRow(n, 'failed', t('Links could not be set - tried again automatically later'), 100);
    else setRow(n, 'done', t('On Google, linked'), 100);
  });
  const pending = pendingNodes();
  pending.forEach(n => setRow(n, 'processing', t('Still being processed by Google - links follow automatically'), 100));
  nodes.filter(n => n.gsvConfirmed && !gsvChanged(n)).forEach(n => {
    const row = upList.querySelector(`.up-row[data-node="${n.id}"]`);
    if (row && ['processing', 'skipped'].includes(row.dataset.state)) setRow(n, 'done', t('On Google, up to date'), 100);
  });
  refreshAllMarkers();
  updateSb();
  refreshUploadSummary();

  setUploadStep('finished');
  const doneEl = document.getElementById('up-done');
  doneEl.hidden = false;
  const allGood = !failed && !pending.length && !linkResult.failed;
  doneEl.classList.toggle('partial', !allGood);
  doneEl.style.animation = 'none';
  void doneEl.offsetWidth;
  doneEl.style.animation = '';
  doneEl.querySelector('svg').outerHTML = doneEl.querySelector('svg').outerHTML; // restart the check animation
  document.getElementById('up-done-title').textContent = failed
    ? t('{a} uploaded, {f} failed', { a: uploaded, f: failed })
    : uploaded ? t('{a} photo(s) uploaded', { a: uploaded }) : t('Tour on Google is up to date');
  document.getElementById('up-done-text').textContent = pending.length
    ? t('Google is still processing {n} photo(s). Their links and positions are set automatically as soon as Google is ready - keep the editor open, or press Upload again later. It can take a few hours until they show on Google Maps.', { n: pending.length })
    : failed ? t('The failed photos are listed above with the reason. Fix it and press Upload again - only the missing ones are sent.')
      : t('It can take a few hours until new photos show on Google Maps.');
  setOverall({ title: allGood ? t('Done') : t('Finished with notes'), pct: 100, mode: allGood ? 'success' : 'stopped' });
  document.getElementById('up-cancel').hidden = true;
  document.getElementById('up-finish').hidden = false;
  document.getElementById('up-contrib').hidden = !uploaded && !nodes.some(n => n.gsvPhotoId);
  if (pending.length) startGoogleBackgroundSync();
}

let uploadPhase = 'upload';
document.getElementById('up-cancel').onclick = () => {
  uploadCancel = true;
  document.getElementById('up-cancel').disabled = true;
  showNotice(uploadPhase === 'wait'
    ? t('Not waiting for Google - links and positions are set in the background as soon as Google is ready.')
    : t('Stopping after the photos being sent now…'), 'info');
};
document.getElementById('up-finish').onclick = () => {
  if (uploadRunning) return;
  showUploadSetup();
};

document.getElementById('upl-refresh-status').onclick = () => openGoogleCompare();

// While photos are still being processed by Google, keep checking in the
// background and set their links as soon as Google accepts them.
let gsvSyncTimer = null;
function startGoogleBackgroundSync() {
  if (gsvSyncTimer) return;
  gsvSyncTimer = setInterval(async () => {
    if (uploadRunning) return;
    const waiting = S.nodes.filter(n => n.gsvPhotoId && (!n.gsvConfirmed || gsvChanged(n)) && n.gsvStatus !== 'missing');
    if (!waiting.length) { clearInterval(gsvSyncTimer); gsvSyncTimer = null; return; }
    const counts = await refreshGoogleStatus({ quiet: true });
    if (!counts) return;
    try {
      const res = await pushGoogleChanges();
      if (res.ok) toast(t('Google is ready: links set for {n} photo(s)', { n: res.ok }));
    } catch {}
    refreshUploadSummary();
  }, 60000);
}

function parseHeadingInput(v) {
  if (v == null || `${v}`.trim() === '') return null;
  const n = Number(v);
  return Number.isFinite(n) ? normalizeHeading(n) : null;
}

// heading = compass direction of the photo's centre (Google's pose.heading), so in
// the photo itself north lies at yaw -heading. The editor view opens facing north.
// Photo Sphere Viewer reads GPano:PoseHeadingDegrees from a photo's XMP and turns
// the panorama by it on its own. Headings are handled explicitly here (node.heading),
// so that extra turn would count twice and leave such photos rotated - keep XMP
// pitch/roll levelling, but never its heading. As a function it applies to every
// panorama the viewer loads, not just the first one.
const IGNORE_XMP_HEADING = () => ({ poseHeading: 0 });

function northYaw(n) {
  return normalizeRad(-degToRad(n.heading));
}
function getNodeEditorYaw(n) {
  if (n?.heading != null) return northYaw(n);
  return n?.viewYaw ?? 0;
}

function lockViewerPitch(viewer) {
  const pos = viewer.getPosition();
  if (Math.abs(pos.pitch) < 0.0001) return pos;
  viewer.rotate({ yaw: pos.yaw, pitch: 0 });
  return { yaw: pos.yaw, pitch: 0 };
}

function csvCell(v) {
  const s = `${v ?? ''}`;
  return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

async function readPhotoMeta(file) {
  let lat = null;
  let lng = null;
  let heading = null;
  let hasGps = false;
  let captureTime = null;
  let captureOffset = null;
  try {
    const data = await exifr.parse(file, { gps: true, xmp: true, exif: true, ifd0: false, iptc: false });
    const taken = data?.DateTimeOriginal ?? data?.CreateDate;
    if (taken instanceof Date && !Number.isNaN(taken.getTime())) captureTime = naiveIso(taken);
    captureOffset = data?.OffsetTimeOriginal ?? data?.OffsetTime ?? null;
    if (data?.latitude && data?.longitude) {
      lat = data.latitude;
      lng = data.longitude;
      hasGps = true;
    }
    heading = normalizeHeading(data?.PoseHeadingDegrees ?? data?.GPSImgDirection ?? null);
  } catch {}
  return { lat, lng, heading, hasGps, captureTime, captureOffset };
}

async function addPhotoNode(file, fallback) {
  pushUndo();
  const meta = await readPhotoMeta(file);
  const node = meta.hasGps ? newPhotoNode(file, meta, meta, true) : newPhotoNode(file, meta, fallback, false);
  if (!meta.hasGps) toast(`${file.name}: no GPS found, placed at the drop point`);
  S.nodes.push(node);
  if (!S.starts.size) S.starts.add(node.id);
  S.selId = node.id;
  createMarker(node);
  tourChanged();
}

function newPhotoNode(file, meta, pos, hasGps) {
  return {
    id: uid(),
    name: file.name.replace(/\.[^.]+$/, ''),
    lat: pos.lat,
    lng: pos.lng,
    heading: meta.heading,
    origHeading: meta.heading,
    viewYaw: meta.heading != null ? normalizeRad(-degToRad(meta.heading)) : 0,
    origViewYaw: meta.heading != null ? normalizeRad(-degToRad(meta.heading)) : 0,
    headingSource: meta.heading != null ? 'exif' : 'manual',
    hasGps,
    photoUrl: URL.createObjectURL(file),
    photoName: file.name,
    photoFile: file,
    captureTime: meta.captureTime,
    captureOffset: meta.captureOffset,
    marker: null,
  };
}

function addEmptyNode(lat, lng) {
  pushUndo();
  const node = {
    id: uid(),
    name: t('Point {n}', { n: S.nodes.length + 1 }),
    lat,
    lng,
    heading: null,
    origHeading: null,
    viewYaw: 0,
    origViewYaw: 0,
    headingSource: 'manual',
    hasGps: false,
    photoUrl: null,
    photoName: null,
    photoFile: null,
    marker: null,
  };
  S.nodes.push(node);
  if (!S.starts.size) S.starts.add(node.id);
  S.selId = node.id;
  createMarker(node);
  tourChanged();
}

// ── Tours ─────────────────────────────────────────
// One map can hold several tours: points that are linked to each other, or
// that came from the same project or imported folder (their tourId). Each tour
// has a start and a name (its project's or folder's, else its start point's);
// with more than one, each gets its own colour. Single points that are neither
// linked nor from a project or import belong to no tour.
const TOUR_COLORS = ['#8b5cf6', '#0ea5e9', '#f97316', '#10b981', '#ec4899', '#84cc16'];
let toursVersion = 0, toursKey = '', toursCache = null, paintedTours = '';
const makeTourId = () => `t${Date.now().toString(36)}${Math.random().toString(36).slice(2, 7)}`;
const fileBase = name => (name || 'tour').replace(/[<>:"/\\|?*\x00-\x1f]+/g, '-').replace(/^[\s.]+|[\s.]+$/g, '').slice(0, 80) || 'tour';

function tourData() {
  const key = `${S.nodes.length}:${S.conns.length}:${S.nxt}:${toursVersion}`;
  if (toursCache && key === toursKey) return toursCache;
  const parent = new Map(S.nodes.map(n => [n.id, n.id]));
  const find = id => { while (parent.get(id) !== id) id = parent.get(id); return id; };
  const join = (a, b) => { const ra = find(a), rb = find(b); if (ra !== rb) parent.set(ra, rb); };
  S.conns.forEach(c => { if (parent.has(c.from) && parent.has(c.to)) join(c.from, c.to); });
  const firstOfTour = new Map();
  S.nodes.forEach(n => {
    if (!n.tourId) return;
    if (firstOfTour.has(n.tourId)) join(n.id, firstOfTour.get(n.tourId));
    else firstOfTour.set(n.tourId, n.id);
  });
  const groups = new Map();
  S.nodes.forEach(n => {
    const root = find(n.id);
    if (!groups.has(root)) groups.set(root, { nodes: [] });
    groups.get(root).nodes.push(n);
  });
  const all = [...groups.values()];
  const tours = all.filter(g => g.nodes.length > 1 || g.nodes[0].tourId);
  tours.forEach((g, i) => { g.index = i; g.color = TOUR_COLORS[i % TOUR_COLORS.length]; });
  const byNode = new Map();
  all.forEach(g => g.nodes.forEach(n => byNode.set(n.id, g)));
  toursCache = { all, tours, byNode };
  toursKey = key;
  return toursCache;
}
const getTours = () => tourData().tours;
const tourOf = id => tourData().byNode.get(id) || null; // a loose point: a group of its own, not a tour
const manyTours = () => getTours().length > 1;
const tourColorOf = n => (manyTours() && tourOf(n.id)?.color) || null;
const tourStart = g => g.nodes.find(n => S.starts.has(n.id)) || g.nodes[0];

function tourName(g) {
  const count = new Map();
  g.nodes.forEach(n => {
    const name = S.tourNames[n.tourId];
    if (name) count.set(name, (count.get(name) || 0) + 1);
  });
  return [...count].sort((a, b) => b[1] - a[1])[0]?.[0] || tourStart(g).name;
}

function setTourStart(id) {
  tourOf(id)?.nodes.forEach(n => S.starts.delete(n.id));
  S.starts.add(id);
}

// Linking two tours leaves two starts in one: the first stays.
function tidyStarts() {
  const live = new Set(S.nodes.map(n => n.id));
  [...S.starts].forEach(id => { if (!live.has(id)) S.starts.delete(id); });
  tourData().all.forEach(g => g.nodes.filter(n => S.starts.has(n.id)).slice(1).forEach(n => S.starts.delete(n.id)));
}

// Repaint markers and links when the tours (or their colours) changed.
function paintTourColors() {
  const sig = manyTours() ? getTours().map(g => `${g.color}:${g.nodes.map(n => n.id).join(',')}`).join('|') : '';
  if (sig === paintedTours) return;
  paintedTours = sig;
  refreshAllMarkers();
  S.conns.forEach(c => c.poly?.setStyle({ color: tourColorOf(gn(c.from)) || '#8b5cf6' }));
}

function renderTours() {
  const box = document.getElementById('tours');
  const tours = getTours();
  box.hidden = tours.length < 2;
  if (box.hidden) return;
  document.getElementById('tour-list').innerHTML = tours.map(g => `<div class="tour-row" data-tour="${g.index}" style="--tc:${g.color}">`
    + `<span class="dot"></span><span class="tn" title="${escapeHtml(t('Double-click to rename'))}">${escapeHtml(tourName(g))}</span>`
    + `<span class="tprog" data-tprog></span>`
    + `<span class="tc" title="${escapeHtml(t('{n} point(s), {d} of links', { n: g.nodes.length, d: formatDistance(tourLength(g.nodes)) }))}">${g.nodes.length}</span>`
    + `<button class="iconbtn sm" data-export title="${escapeHtml(t('Export this tour…'))}">${ICONS.download}</button></div>`).join('');
}

// How much of a tour's checklist is done, in its row of the tour list.
function updateTourProgress() {
  const tours = getTours();
  document.querySelectorAll('#tour-list .tour-row').forEach(row => {
    const g = tours[Number(row.dataset.tour)];
    const el = row.querySelector('[data-tprog]');
    if (!g || !el) return;
    const chips = checklistChips(tourFacts(g.nodes));
    const open = chips.filter(c => c.level === 'todo');
    el.textContent = `${chips.length - open.length}/${chips.length}`;
    el.classList.toggle('done', !open.length);
    el.title = open.length ? `${t('Still to do:')} ${open.map(c => c.label).join(', ')}` : t('Everything on the checklist is done');
  });
}

const tourListEl = document.getElementById('tour-list');
tourListEl.addEventListener('click', e => {
  const row = e.target.closest('.tour-row');
  const g = row && getTours()[Number(row.dataset.tour)];
  if (!g || e.target.closest('input')) return;
  if (e.target.closest('[data-export]')) { runExport(document.querySelector('input[name="exp-format"]:checked')?.value || 'website', `t${g.index}`); return; }
  jumpToNode(tourStart(g).id, false);
  map.fitBounds(L.latLngBounds(g.nodes.map(n => [n.lat, n.lng])), { padding: [60, 60], maxZoom: 19 });
});
tourListEl.addEventListener('dblclick', e => {
  const name = e.target.closest('.tn');
  const g = name && getTours()[Number(name.closest('.tour-row').dataset.tour)];
  if (!g) return;
  const input = document.createElement('input');
  input.className = 'pi';
  input.value = tourName(g);
  name.replaceWith(input);
  input.focus();
  input.select();
  let done = false;
  const finish = save => {
    if (done) return;
    done = true;
    const value = input.value.trim();
    if (save && value && value !== tourName(g)) {
      pushUndo();
      // One name for the whole tour, also where linking joined several.
      const id = g.nodes.find(n => n.tourId)?.tourId || makeTourId();
      g.nodes.forEach(n => { n.tourId = id; });
      S.tourNames[id] = value;
      toursVersion++;
      scheduleAutosave();
    }
    renderTours();
  };
  input.addEventListener('keydown', ev => {
    if (ev.key === 'Enter') finish(true);
    else if (ev.key === 'Escape') { ev.stopPropagation(); finish(false); }
  });
  input.addEventListener('blur', () => finish(true));
});

function markerHtml(n) {
  const i = indexNodes().pos.get(n.id) ?? S.nodes.indexOf(n);
  const cls = ['nd'];
  const tc = tourColorOf(n);
  if (n.id === S.selId) cls.push('sel');
  if (S.starts.has(n.id)) cls.push('start');
  if (n.id === S.connFrom) cls.push('csrc');
  if (n.photoUrl) cls.push('hp');
  if (S.multiSel.has(n.id)) cls.push('multi');
  if (onOtherFloor(n)) cls.push('dim');
  const wedge = n.heading != null
    ? `<svg class="nd-wedge" viewBox="0 0 44 44" style="transform:rotate(${n.heading}deg)"><path d="M22 3 L28 16 L16 16 Z" fill="rgba(255,255,255,.85)"/></svg>`
    : '';
  const gs = n.gsvPhotoId ? `<span class="gs ${n.gsvStatus || ''}${gsvChanged(n) ? ' changed' : ''}"></span>` : '';
  return `<div class="${cls.join(' ')}"${tc ? ` style="--tc:${tc}"` : ''}>${wedge}${gs}${i + 1}<div class="nl">${escapeHtml(n.name)}${S.starts.has(n.id) ? ' start' : ''}</div></div>`;
}

let suppressNextNodeClick = false;

function createMarker(n, { animate = true } = {}) {
  const html = markerHtml(n);
  const mk = L.marker([n.lat, n.lng], {
    icon: L.divIcon({ html, className: '', iconAnchor: [17, 17] }),
    draggable: true,
  }).addTo(map);
  mk.svtHtml = html;
  const el = mk.getElement();
  if (el) {
    if (animate) {
      el.classList.add('nd-pop');
      el.addEventListener('animationend', () => el.classList.remove('nd-pop'), { once: true });
    }
    attachLongPress(el, n.id);
  }
  mk.on('click', e => {
    L.DomEvent.stopPropagation(e);
    if (suppressNextNodeClick) { suppressNextNodeClick = false; return; }
    const native = e.originalEvent;
    if (native && (native.ctrlKey || native.metaKey || native.shiftKey)) {
      toggleMultiSelect(n.id);
    } else {
      handleNodeClick(n.id);
    }
  });
  mk.on('contextmenu', e => {
    L.DomEvent.stopPropagation(e);
    e.originalEvent.preventDefault();
    handleNodeConnectClick(n.id);
  });
  mk.on('dblclick', e => L.DomEvent.stopPropagation(e));
  mk.on('dragend', () => {
    pushUndo();
    const p = mk.getLatLng();
    n.lat = p.lat;
    n.lng = p.lng;
    refreshConnLines();
    refreshCompareUi(gn(S.selId));
    if (S.selId === n.id) { refreshLatLngUi(n); positionQuickMenu(); }
    scheduleAutosave();
  });
  n.marker = mk;
  bindMarkerTip(n);
}

function attachLongPress(el, id) {
  if (el.dataset.longPress) return; // Leaflet keeps the element when the icon changes
  el.dataset.longPress = '1';
  let timer = null;
  let moved = false;
  el.addEventListener('touchstart', () => {
    moved = false;
    clearTimeout(timer);
    timer = setTimeout(() => {
      if (moved) return;
      suppressNextNodeClick = true;
      handleNodeConnectClick(id);
      if (navigator.vibrate) navigator.vibrate(25);
    }, 500);
  }, { passive: true });
  el.addEventListener('touchmove', () => { moved = true; clearTimeout(timer); }, { passive: true });
  el.addEventListener('touchend', () => clearTimeout(timer));
  el.addEventListener('touchcancel', () => clearTimeout(timer));
}

function refreshMarker(n) {
  if (!n.marker) return;
  const html = markerHtml(n);
  if (n.marker.svtHtml === html) return; // nothing changed: the icon stays
  n.marker.svtHtml = html;
  n.marker.setIcon(L.divIcon({ html, className: '', iconAnchor: [17, 17] }));
  const el = n.marker.getElement();
  if (el) attachLongPress(el, n.id);
}

function refreshAllMarkers() {
  S.nodes.forEach(refreshMarker);
  refreshConnMarks();
}

// The × that removes a link sits only on the selected node's links: on every
// link at once it cluttered the whole map.
function refreshConnMarks() {
  S.conns.forEach(c => c.xMark?.getElement()?.classList.toggle('hide', c.from !== S.selId && c.to !== S.selId));
}

function handleNodeClick(id) {
  if (S.connFrom) { cancelConnect(); return; }
  if (S.multiSel.size) clearMultiSel();
  jumpToNode(id, false);
}

function toggleMultiSelect(id) {
  if (S.connFrom) cancelConnect();
  hideQuickMenu();
  if (S.multiSel.has(id)) S.multiSel.delete(id);
  else S.multiSel.add(id);
  refreshAllMarkers();
  updateMultiBar();
  renderNodeList();
}

function clearMultiSel() {
  if (!S.multiSel.size) return;
  S.multiSel.clear();
  refreshAllMarkers();
  updateMultiBar();
  renderNodeList();
}

function handleNodeConnectClick(id) {
  if (S.multiSel.size) clearMultiSel();
  if (!S.connFrom) {
    S.connFrom = id;
    refreshAllMarkers();
    const from = gn(id);
    connectLine = L.polyline([[from.lat, from.lng], [from.lat, from.lng]], {
      color: '#8b5cf6', weight: 2, opacity: 0.8, dashArray: '6 6',
    }).addTo(map);
    document.getElementById('map').classList.add('connecting');
    return;
  }
  if (S.connFrom !== id && gcbs(S.connFrom, id).length === 0) {
    pushUndo();
    S.conns.push({ id: cuid(), from: S.connFrom, to: id, poly: null, xMark: null });
    cancelConnect();
    drawLastConn();
    tourChanged();
    return;
  }
  cancelConnect();
}

let walkArrivalYaw = null;

function jumpToNode(id, recordHistory = true) {
  const from = gn(S.selId), to = gn(id);
  walkArrivalYaw = recordHistory && from && to && from !== to && to.heading != null && distanceMeters(from, to) > 0.3
    ? normalizeRad(degToRad(bearingDeg(from, to) - to.heading))
    : null;
  if (recordHistory && S.selId && S.selId !== id) walkHistory.push(S.selId);
  S.selId = id;
  refreshAllMarkers();
  updateSb();
  renderNodeList();
  const node = gn(id);
  if (node?.marker) {
    map.panTo(node.marker.getLatLng(), { animate: true });
  }
  showQuickMenu();
  renderFilmstrip();
}

function drawLastConn() {
  drawConn(S.conns[S.conns.length - 1]);
}

function drawConn(c, { animate = true } = {}) {
  const a = gn(c.from);
  const b = gn(c.to);
  if (!a || !b) return;
  c.poly = L.polyline([[a.lat, a.lng], [b.lat, b.lng]], { color: tourColorOf(a) || '#8b5cf6', weight: 3, opacity: 0.9 }).addTo(map);
  c.poly.bindTooltip(formatDistance(distanceMeters(a, b)), { className: 'conn-dist-wrap', sticky: true, direction: 'top', opacity: 1 });
  const pathEl = animate ? c.poly.getElement?.() : null;
  if (pathEl) {
    const len = pathEl.getTotalLength();
    pathEl.style.strokeDasharray = `${len}`;
    pathEl.style.strokeDashoffset = `${len}`;
    pathEl.getBoundingClientRect();
    pathEl.style.transition = 'stroke-dashoffset .45s ease';
    requestAnimationFrame(() => { pathEl.style.strokeDashoffset = '0'; });
  }
  const mid = [(a.lat + b.lat) / 2, (a.lng + b.lng) / 2];
  c.xMark = L.marker(mid, {
    icon: L.divIcon({ html: '<div class="conn-x">×</div>', className: '', iconAnchor: [9, 9] }),
    zIndexOffset: 200,
  }).addTo(map);
  c.xMark.on('click', e => {
    L.DomEvent.stopPropagation(e);
    deleteConn(c.id);
  });
  c.xMark.getElement()?.classList.toggle('hide', c.from !== S.selId && c.to !== S.selId);
}

function deleteConn(id) {
  const c = S.conns.find(x => x.id === id);
  if (!c) return;
  pushUndo();
  if (c.poly) map.removeLayer(c.poly);
  if (c.xMark) map.removeLayer(c.xMark);
  S.conns = S.conns.filter(x => x.id !== id);
  tourChanged();
}

function refreshConnLines() {
  S.conns.forEach(c => {
    if (c.poly) map.removeLayer(c.poly);
    if (c.xMark) map.removeLayer(c.xMark);
    drawConn(c, { animate: false });
  });
  updateViewCone(); // a point may have moved
}

function getLinkedNodes(id) {
  return S.conns
    .filter(c => c.from === id || c.to === id)
    .map(c => gn(c.from === id ? c.to : c.from))
    .filter(Boolean);
}

function updateSb() {
  renderChecklist();
  const n = gn(S.selId);
  document.getElementById('no-sel').style.display = n ? 'none' : '';
  document.getElementById('n-pr').style.display = n ? '' : 'none';
  if (!n) { updateViewCone(); return; }
  niN.value = n.name;
  niS.checked = S.starts.has(n.id);
  refreshLatLngUi(n);
  gpsBadge.innerHTML = n.hasGps ? '<span class="gok">GPS</span>' : '<span class="gno">No GPS</span>';
  phB.style.display = n.photoUrl ? 'none' : '';
  renderPhotoLine(n);
  renderHotspots(n);
  document.getElementById('bn-blur').style.display = n.photoFile ? '' : 'none';
  document.querySelector('.np-photo-bar').style.display = n.photoUrl ? '' : 'none';
  refreshHeadingUi(n);
  renderPlaceLine(n);
  ensurePanoViewer(n);
  renderWalkList(n);
  renderConnList();
  refreshCompareUi(n);
}

function refreshLatLngUi(n) {
  niLat.value = n.lat.toFixed(6);
  niLng.value = n.lng.toFixed(6);
  niLevel.value = n.level ? n.level.number : '';
  niLevelName.value = n.level?.name || '';
}

// ── Floors ────────────────────────────────────────
// Inside buildings each point can have a floor (Street View: pose.level - a number
// and a label of up to 3 characters, like a lift button). With floors on the
// map, a switch shows one floor at a time; the others are faded.
let floorView = null; // the floor shown, null: all

function setLevel(n, number, name) {
  n.level = number === null ? null : { number, name: (name ?? '').slice(0, 3) };
}

function commitLevel() {
  const n = gn(S.selId);
  if (!n) return;
  const raw = niLevel.value.trim();
  const number = raw === '' ? null : Math.round(Number(raw));
  if (raw !== '' && !Number.isFinite(number)) { refreshLatLngUi(n); return; }
  const name = niLevelName.value.trim().slice(0, 3);
  if ((n.level?.number ?? null) === number && (n.level?.name || '') === (number === null ? '' : name)) return;
  pushUndo();
  setLevel(n, number, name);
  tourChanged();
}
niLevel.addEventListener('change', commitLevel);
niLevelName.addEventListener('change', commitLevel);

document.getElementById('multibar-level').addEventListener('change', e => {
  const v = e.target.value;
  e.target.value = '';
  if (!v || !S.multiSel.size) return;
  pushUndo();
  S.nodes.filter(n => S.multiSel.has(n.id)).forEach(n => {
    if (v === 'none') setLevel(n, null);
    else setLevel(n, Number(v), n.level?.number === Number(v) ? n.level.name : '');
  });
  tourChanged();
});

const floorControl = L.control({ position: 'topleft' });
floorControl.onAdd = () => {
  const el = L.DomUtil.create('div', 'floor-filter');
  L.DomEvent.disableClickPropagation(el);
  el.addEventListener('click', e => {
    const b = e.target.closest('button[data-floor]');
    if (!b) return;
    floorView = b.dataset.floor === 'all' ? null : Number(b.dataset.floor);
    renderFloors();
    refreshAllMarkers();
    paintFloorLinks();
  });
  return el;
};
let floorControlShown = false;

function renderFloors() {
  const floors = [...new Map(S.nodes.filter(n => n.level).map(n => [n.level.number, n.level])).values()].sort((a, b) => b.number - a.number);
  if (!floors.length) {
    if (floorControlShown) { floorControl.remove(); floorControlShown = false; }
    if (floorView !== null) { floorView = null; refreshAllMarkers(); paintFloorLinks(); }
    return;
  }
  if (!floorControlShown) { floorControl.addTo(map); floorControlShown = true; }
  if (floorView !== null && !floors.some(f => f.number === floorView)) floorView = null;
  floorControl.getContainer().innerHTML = `<button data-floor="all" class="${floorView === null ? 'on' : ''}" title="${escapeHtml(t('All floors'))}">${escapeHtml(t('All'))}</button>`
    + floors.map(f => `<button data-floor="${f.number}" class="${floorView === f.number ? 'on' : ''}" title="${escapeHtml(t('Floor {n}', { n: f.number }))}">${escapeHtml(f.name || String(f.number))}</button>`).join('');
}

const onOtherFloor = n => floorView !== null && (n.level?.number ?? null) !== floorView;
function paintFloorLinks() {
  S.conns.forEach(c => c.poly?.setStyle({ opacity: onOtherFloor(gn(c.from)) && onOtherFloor(gn(c.to)) ? 0.15 : 0.9 }));
}

function refreshHeadingUi(n) {
  niH.value = n.heading == null ? '' : n.heading.toFixed(1);
  headingMeta.textContent = n.heading == null ? t('not set')
    : n.headingSource === 'exif' ? t('from the photo (EXIF)')
    : n.headingSource === 'auto' ? t('from the walking direction')
    : n.headingSource === 'match' ? t('matched to the neighbouring photos')
    : n.headingSource === 'sun' ? t('from the sun')
    : n.headingSource === 'google' ? t('as on Google') : t('set by hand');
  updateViewCone();
}

async function ensurePanoViewer(n) {
  if (!n?.photoUrl) {
    if (panoFocus) setPanoFocus(false);
    panoWrap.style.display = 'none';
    panoHelp.textContent = 'Choose a photo to check north alignment here.';
    updateViewCone();
    return;
  }
  panoWrap.style.display = '';
  panoHelp.textContent = 'Drag to look around. Turn until you face north, then press "Set This Direction as North".';
  if (!panoViewer) {
    panoViewer = new Viewer({
      container: document.getElementById('pano-view'),
      panoData: IGNORE_XMP_HEADING,
      plugins: [MarkersPlugin],
      panorama: n.photoUrl,
      navbar: ['zoom', 'move'],
      defaultYaw: getNodeEditorYaw(n),
      defaultPitch: 0,
      touchmoveTwoFingers: true,
    });
    panoViewer.addEventListener('position-updated', () => {
      if (S.selId !== panoNodeId) return;
      const pos = lockViewerPitch(panoViewer);
      const yaw = normalizeHeading(radToDeg(pos.yaw));
      panoHelp.textContent = `View yaw ${yaw.toFixed(1)}°. Rotate horizontally only and face north.`;
      updateViewCone();
    });
    panoViewer.addEventListener('zoom-updated', () => updateViewCone());
  }
  // Only a new photo sets the view; redrawing the side panel leaves it where the user turned it.
  if (panoNodeId !== n.id) {
    panoNodeId = n.id;
    const yaw = walkArrivalYaw ?? getNodeEditorYaw(n);
    walkArrivalYaw = null;
    await panoViewer.setPanorama(n.photoUrl, {
      transition: false,
      position: { yaw, pitch: 0 },
    });
  }
  if (S.selId === n.id) refreshPanoMarkers(n);
  updateViewCone();
  if (panoFocus) { renderFocusBar(n); updateFocusMap(n); }
}

function renderConnList() {
  const n = gn(S.selId);
  if (!n) return;
  const linked = getLinkedNodes(n.id);
  clL.innerHTML = '';
  document.getElementById('cl-count').textContent = linked.length || '';
  // Walking back only once you have walked somewhere.
  const back = document.getElementById('walk-back');
  const start = document.getElementById('walk-start');
  back.hidden = !walkHistory.some(id => gn(id));
  const ownStart = tourOf(n.id) && tourStart(tourOf(n.id));
  start.hidden = !ownStart || ownStart === n || !walkHistory.length;
  document.getElementById('walk-row').hidden = back.hidden && start.hidden;
  if (!linked.length) {
    clL.innerHTML = `<span class="chips-empty">${escapeHtml(t('Right-click this node, then another one, to link them.'))}</span>`;
    return;
  }
  linked.forEach(o => {
    const c = gcbs(n.id, o.id)[0];
    const chip = document.createElement('span');
    chip.className = 'chip';
    chip.innerHTML = `<button class="go" title="${escapeHtml(t('Walk there'))}">${escapeHtml(o.name)}</button><button class="x" title="${escapeHtml(t('Remove link'))}">×</button>`;
    chip.querySelector('.go').onclick = () => jumpToNode(o.id, true);
    chip.querySelector('.x').onclick = () => deleteConn(c.id);
    clL.appendChild(chip);
  });
}

function renderWalkList(n) {
  // The connection chips walk there now; this list stays empty and hidden.
  walkList.innerHTML = '';
  return;
  const linked = getLinkedNodes(n.id).filter(x => x.photoUrl);
  if (!linked.length) {
    walkHelp.textContent = 'Select a node with links and you can walk from here inside the editor.';
    return;
  }
  walkHelp.textContent = `Current: ${n.name}. Click a linked node to move there.`;
  linked.forEach(o => {
    const btn = document.createElement('button');
    btn.className = 'walkchip';
    btn.textContent = o.name;
    btn.onclick = () => jumpToNode(o.id, true);
    walkList.appendChild(btn);
  });
}

function refreshCompareUi(n) {
  if (n && !document.querySelector('details[data-sec="compare"]').open) {
    cmpWrap.style.display = 'none';
    return;
  }
  if (!n) {
    cmpWrap.style.display = 'none';
    cmpTarget.innerHTML = '';
    return;
  }
  const linked = getLinkedNodes(n.id).filter(x => x.photoUrl);
  cmpTarget.innerHTML = '';
  if (!linked.length || !n.photoUrl) {
    cmpWrap.style.display = 'none';
    cmpHelp.textContent = 'If a linked node has a photo, you can overlay-compare it here.';
    const opt = document.createElement('option');
    opt.value = '';
    opt.textContent = linked.length ? 'Choose linked node' : 'No comparable linked nodes';
    cmpTarget.appendChild(opt);
    return;
  }
  let targetId = S.compareTargetByNode[n.id];
  if (!targetId || !linked.some(x => x.id === targetId)) {
    targetId = linked[0].id;
    S.compareTargetByNode[n.id] = targetId;
  }
  linked.forEach(o => {
    const opt = document.createElement('option');
    opt.value = o.id;
    opt.textContent = o.name;
    opt.selected = o.id === targetId;
    cmpTarget.appendChild(opt);
  });
  cmpWrap.style.display = '';
  cmpHelp.textContent = cmpSwap
    ? 'Swapped comparison order. Use whichever view makes the overlap easier to read.'
    : 'Use the translucent overlap and align it by eye.';
  ensureCompareViewers(n, gn(targetId));
}

async function ensureCompareViewers(baseNode, targetNode) {
  if (!baseNode?.photoUrl || !targetNode?.photoUrl) {
    cmpWrap.style.display = 'none';
    return;
  }
  const mainNode = cmpSwap ? targetNode : baseNode;
  const overNode = cmpSwap ? baseNode : targetNode;
  if (!cmpBaseViewer) {
    cmpBaseViewer = new Viewer({
      container: document.getElementById('cmp-base'),
      panoData: IGNORE_XMP_HEADING,
      panorama: mainNode.photoUrl,
      navbar: ['zoom', 'move'],
      defaultPitch: 0,
      touchmoveTwoFingers: true,
    });
    cmpBaseViewer.addEventListener('position-updated', () => {
      if (!cmpOverlayViewer || cmpSyncing) return;
      const pos = cmpBaseViewer.getPosition();
      cmpSyncing = true;
      cmpOverlayViewer.rotate({
        yaw: normalizeRad(pos.yaw + cmpDelta),
        pitch: pos.pitch,
      });
      cmpSyncing = false;
    });
  }
  if (!cmpOverlayViewer) {
    cmpOverlayViewer = new Viewer({
      container: document.getElementById('cmp-overlay'),
      panoData: IGNORE_XMP_HEADING,
      panorama: overNode.photoUrl,
      navbar: [],
      defaultPitch: 0,
      mousewheel: false,
      touchmoveTwoFingers: false,
    });
  }
  if (cmpBaseNodeId !== mainNode.id) {
    cmpBaseNodeId = mainNode.id;
    await cmpBaseViewer.setPanorama(mainNode.photoUrl, { transition: false, position: { yaw: 0, pitch: 0 } });
  }
  if (cmpOverlayNodeId !== overNode.id) {
    cmpOverlayNodeId = overNode.id;
    await cmpOverlayViewer.setPanorama(overNode.photoUrl, { transition: false, position: { yaw: 0, pitch: 0 } });
  }
  const key = `${mainNode.id}|${overNode.id}|${baseNode.heading}|${targetNode.heading}`;
  if (cmpAligned !== key) {
    cmpAligned = key;
    alignCompareByHeading(baseNode, targetNode);
  }
}
let cmpAligned = '';

function alignCompareByHeading(baseNode, targetNode) {
  if (!cmpBaseViewer || !cmpOverlayViewer) return;
  // Both views face north, i.e. the same real-world direction.
  const baseYaw = baseNode.heading != null ? northYaw(baseNode) : 0;
  const overlayYaw = targetNode.heading != null ? northYaw(targetNode) : 0;
  const mainYaw = cmpSwap ? overlayYaw : baseYaw;
  const overYaw = cmpSwap ? baseYaw : overlayYaw;
  cmpDelta = normalizeRad(overYaw - mainYaw);
  cmpSyncing = true;
  cmpBaseViewer.rotate({ yaw: mainYaw, pitch: 0 });
  cmpOverlayViewer.rotate({ yaw: overYaw, pitch: 0 });
  cmpSyncing = false;
  cmpHelp.textContent = cmpSwap
    ? 'Comparing with front/back swapped. Align where the overlap looks natural.'
    : 'Comparing with the same view heading. Align where the overlap looks natural.';
}

// After any change to points, links or tours: everything that shows them is
// redrawn and the change is autosaved. (Undo: pushUndo() before the change.)
function tourChanged() {
  updateSb();
  refreshAllMarkers();
  updateStats();
  scheduleAutosave();
}

function updateStats() {
  tidyStarts();
  const tourCount = getTours().length;
  const length = tourLength(S.nodes);
  document.getElementById('st').innerHTML = `Nodes <b>${S.nodes.length}</b> &middot; Links <b>${S.conns.length}</b>`
    + (tourCount > 1 ? ` &middot; Tours <b>${tourCount}</b>` : '')
    + (length ? ` &middot; <b>${formatDistance(length)}</b>` : '');
  renderTours();
  paintTourColors();
  renderFloors();
  renderChecklist();
  updateTourProgress();
  document.getElementById('no-sel').classList.toggle('has-nodes', S.nodes.length > 0);
  // No list without nodes; a search box only once the list gets long.
  document.getElementById('node-list-panel').hidden = !S.nodes.length;
  const search = document.getElementById('node-search');
  search.hidden = S.nodes.length < 8 && !search.value;
  renderNodeList();
  renderFilmstrip();
  showLabels();
}

// The names under the points: all of them only close up or with few points (else they
// cover each other), the selected one always, any other on hover.
function showLabels() {
  document.getElementById('map').classList.toggle('labels', map.getZoom() >= 19 || S.nodes.length <= 12);
}
map.on('zoomend', showLabels);

function fitAll() {
  const b = L.latLngBounds(S.nodes.map(n => [n.lat, n.lng]));
  map.fitBounds(b, { padding: [60, 60] });
}

// ── Undo / Redo ───────────────────────────────────
let undoStack = [];
let redoStack = [];
const bUndo = document.getElementById('bundo');
const bRedo = document.getElementById('bredo');

function snapshotState() {
  return {
    nodes: S.nodes.map(n => ({
      id: n.id, name: n.name, lat: n.lat, lng: n.lng,
      heading: n.heading, origHeading: n.origHeading,
      viewYaw: n.viewYaw, origViewYaw: n.origViewYaw,
      headingSource: n.headingSource, hasGps: n.hasGps,
      photoName: n.photoName, photoFile: n.photoFile || null,
      captureTime: n.captureTime, captureOffset: n.captureOffset ?? null,
      blurred: !!n.blurred, privacyChecked: !!n.privacyChecked, gsvPhotoId: n.gsvPhotoId ?? null,
      gsvStatus: n.gsvStatus ?? null, gsvShareLink: n.gsvShareLink ?? null, gsvSig: n.gsvSig ?? null,
      gsvConfirmed: !!n.gsvConfirmed, gsvUploadedAt: n.gsvUploadedAt ?? null, gsvObsolete: [...(n.gsvObsolete || [])],
      hotspots: (n.hotspots || []).map(h => ({ ...h })), tourId: n.tourId ?? null,
      placeId: n.placeId, placeName: n.placeName ?? null, placeAuto: !!n.placeAuto, placeKind: n.placeKind ?? null, placeDist: n.placeDist ?? null, placeFor: n.placeFor ?? null,
      level: n.level, gsvViews: n.gsvViews ?? null, pmx: n.pmx ?? null,
    })),
    conns: S.conns.map(c => ({ id: c.id, from: c.from, to: c.to })),
    selId: S.selId, starts: [...S.starts], startId: [...S.starts][0] ?? null, tourNames: { ...S.tourNames }, nxt: S.nxt,
  };
}

function pushUndo(snap = snapshotState()) {
  undoStack.push(snap);
  if (undoStack.length > 60) undoStack.shift();
  redoStack = [];
  updateUndoRedoButtons();
}

function applySnapshot(snap, { refit = false, keepGoogle = false } = {}) {
  // Undo/redo restores edits, not what happened on Google: an upload after the
  // snapshot was taken must survive, or the photo would be uploaded twice.
  if (keepGoogle) {
    const live = new Map(S.nodes.map(n => [n.id, n]));
    snap = { ...snap, nodes: snap.nodes.map(n => {
      const cur = live.get(n.id);
      if (!cur || cur.photoFile !== n.photoFile) return n;
      const keep = {};
      GSV_FIELDS.forEach(k => { keep[k] = cur[k]; });
      return { ...n, ...keep };
    }) };
  }
  S.nodes.forEach(n => { if (n.marker) map.removeLayer(n.marker); });
  S.conns.forEach(c => {
    if (c.poly) map.removeLayer(c.poly);
    if (c.xMark) map.removeLayer(c.xMark);
  });
  if (connectLine) { map.removeLayer(connectLine); connectLine = null; }
  document.getElementById('map').classList.remove('connecting');

  S.nodes = snap.nodes.map(n => ({
    ...n,
    photoUrl: n.photoFile ? URL.createObjectURL(n.photoFile) : null,
    marker: null,
  }));
  S.conns = snap.conns.map(c => ({ ...c, poly: null, xMark: null }));
  S.selId = snap.selId ?? null;
  S.starts = new Set(snap.starts ?? (snap.startId ? [snap.startId] : []));
  S.tourNames = { ...(snap.tourNames || {}) };
  toursVersion++;
  S.nxt = snap.nxt ?? S.nxt;
  // Never hand out an ID that is already in use (older projects had no counter).
  const used = [...S.nodes.map(n => n.id), ...S.conns.map(c => c.id), ...S.nodes.flatMap(n => (n.hotspots || []).map(h => h.id))]
    .map(id => Number(String(id).slice(1)) || 0);
  const maxUsed = Math.max(0, ...used);
  if (S.nxt <= maxUsed) S.nxt = maxUsed + 1;
  S.connFrom = null;
  S.multiSel = new Set();
  panoNodeId = null;
  cmpBaseNodeId = null;
  cmpOverlayNodeId = null;
  cmpAligned = '';

  S.nodes.forEach(n => createMarker(n, { animate: false }));
  S.conns.forEach(c => drawConn(c, { animate: false }));
  updateSb();
  updateMultiBar();
  updateStats();
  if (refit && S.nodes.length) fitAll();
  if (S.nodes.some(n => n.gsvPhotoId && n.gsvStatus !== 'missing' && (!n.gsvConfirmed || n.gsvSig !== gsvSignature(n)))) {
    startGoogleBackgroundSync();
  }
}

function doUndo() {
  if (!undoStack.length) return;
  redoStack.push(snapshotState());
  const snap = undoStack.pop();
  applySnapshot(snap, { keepGoogle: true });
  updateUndoRedoButtons();
  scheduleAutosave();
}

function doRedo() {
  if (!redoStack.length) return;
  undoStack.push(snapshotState());
  const snap = redoStack.pop();
  applySnapshot(snap, { keepGoogle: true });
  updateUndoRedoButtons();
  scheduleAutosave();
}

function updateUndoRedoButtons() {
  bUndo.disabled = !undoStack.length;
  bRedo.disabled = !redoStack.length;
}

bUndo.onclick = doUndo;
bRedo.onclick = doRedo;

function closeAnyOverlay() {
  let closed = false;
  if (cmdkBack.classList.contains('show')) { closeCmdk(); closed = true; }
  if (cheatBack.classList.contains('show')) { cheatBack.classList.remove('show'); closed = true; }
  if (previewBack.classList.contains('show')) { previewBack.classList.remove('show'); closed = true; }
  if (uplBack.classList.contains('show')) { uplBack.classList.remove('show'); closed = true; }
  document.querySelectorAll('.modal-back.show').forEach(el => {
    if (el.id === 'update-back' && document.getElementById('update-close').style.display === 'none') return;
    if (el.id === 'blur-back' && !mayCloseBlur()) { closed = true; return; } // kept open: Esc is used up
    el.classList.remove('show');
    closed = true;
  });
  if (document.querySelector('#tb .menu.show')) { closeMenus(); closed = true; }
  if (closed && document.activeElement instanceof HTMLElement) document.activeElement.blur();
  return closed;
}

document.addEventListener('keydown', e => {
  const tag = document.activeElement?.tagName;
  const typing = tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT';

  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
    e.preventDefault();
    toggleCmdk();
    return;
  }

  if ((e.ctrlKey || e.metaKey) && !e.altKey && ['s', 'o', 'i'].includes(e.key.toLowerCase())) {
    e.preventDefault();
    if (document.querySelector('.modal-back.show')) return;
    if (e.key.toLowerCase() === 's') saveProject({ saveAs: e.shiftKey });
    else if (e.key.toLowerCase() === 'i') openImport();
    else if (e.shiftKey) pickAndAddProjects();
    else pickAndOpenProject();
    return;
  }

  if (e.key === 'Escape') {
    if (panoFocus && !document.querySelector('.modal-back.show')) { setPanoFocus(false); return; }
    if (closeAnyOverlay()) return;
    if (S.connFrom) cancelConnect();
    else if (S.multiSel.size) clearMultiSel();
    else { S.selId = null; updateSb(); renderNodeList(); hideQuickMenu(); }
    return;
  }

  const anyModalOpen = cmdkBack.classList.contains('show') || !!document.querySelector('.modal-back.show');
  if (anyModalOpen) return;

  if (!typing && e.key === '?') {
    e.preventDefault();
    cheatBack.classList.add('show');
    return;
  }

  if ((e.ctrlKey || e.metaKey) && !typing) {
    const k = e.key.toLowerCase();
    if (k === 'z' && !e.shiftKey) { e.preventDefault(); doUndo(); return; }
    if (k === 'y' || (k === 'z' && e.shiftKey)) { e.preventDefault(); doRedo(); return; }
  }
  if (typing) return;

  if (panoFocus && ['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight'].includes(e.key)) {
    e.preventDefault();
    focusKey(e.key);
    return;
  }

  if (['j', 'k', 'n'].includes(e.key.toLowerCase()) && !e.ctrlKey && !e.metaKey && !e.altKey) {
    const key = e.key.toLowerCase();
    const id = key === 'n' ? nextWithoutNorth() : stepFilmstrip(key === 'j' ? 1 : -1);
    if (id && id !== S.selId) jumpToNode(id, key !== 'n');
    return;
  }

  if (e.key.toLowerCase() === 'f' && !e.ctrlKey && !e.metaKey && !e.altKey) {
    setPanoFocus(!panoFocus);
    return;
  }

  if (e.key === 'Delete' || e.key === 'Backspace') {
    if (S.multiSel.size) {
      pushUndo();
      [...S.multiSel].forEach(removeNodeById);
      clearMultiSel();
      tourChanged();
    } else if (S.selId) {
      pushUndo();
      removeNodeById(S.selId);
      tourChanged();
      hideQuickMenu();
    }
  }
});

// ── Multi-select bar ──────────────────────────────
const multibar = document.getElementById('multibar');
const multibarCount = document.getElementById('multibar-count');
const multibarConnect = document.getElementById('multibar-connect');

function updateMultiBar() {
  if (!S.multiSel.size) { multibar.classList.remove('show'); return; }
  multibar.classList.add('show');
  multibarCount.textContent = `${S.multiSel.size} selected`;
  multibarConnect.disabled = S.multiSel.size < 2;
}

multibarConnect.onclick = () => {
  const ids = [...S.multiSel];
  if (ids.length < 2) return;
  pushUndo();
  const before = S.conns.length;
  for (let i = 0; i < ids.length - 1; i++) {
    const a = ids[i], b = ids[i + 1];
    if (gcbs(a, b).length === 0) {
      S.conns.push({ id: cuid(), from: a, to: b, poly: null, xMark: null });
    }
  }
  S.conns.slice(before).forEach(c => drawConn(c));
  const added = S.conns.length - before;
  clearMultiSel();
  tourChanged();
  toast(added ? `Connected ${ids.length} nodes in sequence` : 'Those nodes are already connected');
};

document.getElementById('multibar-delete').onclick = () => {
  const ids = [...S.multiSel];
  if (!ids.length) return;
  pushUndo();
  ids.forEach(removeNodeById);
  clearMultiSel();
  tourChanged();
};

document.getElementById('multibar-clear').onclick = () => clearMultiSel();

// ── Searchable / reorderable node list ────────────
const nodeSearchEl = document.getElementById('node-search');
const nodeListEl = document.getElementById('node-list');

function renderNodeList() {
  const q = nodeSearchEl.value.trim().toLowerCase();
  nodeListEl.innerHTML = '';
  const filtered = S.nodes
    .map((n, i) => ({ n, i }))
    .filter(({ n }) => !q || n.name.toLowerCase().includes(q));

  if (!filtered.length) {
    const empty = document.createElement('div');
    empty.className = 'nrow-empty';
    empty.textContent = S.nodes.length ? 'No matches' : 'No nodes yet';
    nodeListEl.appendChild(empty);
    return;
  }

  filtered.forEach(({ n, i }) => {
    const row = document.createElement('div');
    row.className = `nrow${n.id === S.selId ? ' sel' : ''}${S.multiSel.has(n.id) ? ' multi' : ''}`;
    const tc = tourColorOf(n);
    if (tc) row.style.setProperty('--tc', tc);
    row.draggable = true;
    row.innerHTML = `<span class="nrow-drag">⠿</span><span class="nrow-idx">${i + 1}</span>${thumbImg(n)}<span class="nrow-name">${escapeHtml(n.name)}</span><span class="cx" title="Delete">×</span>`;
    row.addEventListener('click', e => {
      if (e.target.classList.contains('cx')) return;
      if (e.ctrlKey || e.metaKey || e.shiftKey) toggleMultiSelect(n.id);
      else jumpToNode(n.id, false);
    });
    row.querySelector('.cx').addEventListener('click', e => {
      e.stopPropagation();
      pushUndo();
      removeNodeById(n.id);
      tourChanged();
    });
    row.addEventListener('dragstart', e => {
      e.dataTransfer.effectAllowed = 'move';
      e.dataTransfer.setData('text/plain', n.id);
    });
    row.addEventListener('dragover', e => {
      e.preventDefault();
      row.classList.add('drag-over');
    });
    row.addEventListener('dragleave', () => row.classList.remove('drag-over'));
    row.addEventListener('drop', e => {
      e.preventDefault();
      row.classList.remove('drag-over');
      const draggedId = e.dataTransfer.getData('text/plain');
      if (!draggedId || draggedId === n.id) return;
      reorderNode(draggedId, n.id);
    });
    nodeListEl.appendChild(row);
  });
}

function reorderNode(draggedId, targetId) {
  const from = S.nodes.findIndex(x => x.id === draggedId);
  const to = S.nodes.findIndex(x => x.id === targetId);
  if (from === -1 || to === -1 || from === to) return;
  pushUndo();
  const [moved] = S.nodes.splice(from, 1);
  S.nodes.splice(to, 0, moved);
  nodeIndex.dirty = true; // same list, new order
  toursVersion++;
  refreshAllMarkers();
  renderNodeList();
  scheduleAutosave();
}

nodeSearchEl.addEventListener('input', renderNodeList);

// ── Session autosave / restore (IndexedDB) ────────
let autosaveTimer = null;
const saveStatusEl = document.getElementById('save-status');
const saveStatusText = document.getElementById('save-status-text');

function setSaveStatus(mode, text) {
  saveStatusEl.classList.remove('saving', 'saved');
  if (mode) saveStatusEl.classList.add(mode);
  saveStatusText.textContent = text;
}

let autosaveDue = false;
// edit: false for what isn't the user's change (news from Google): saved, but the project isn't "unsaved".
function scheduleAutosave({ edit = true } = {}) {
  if (edit) markDirty();
  queueChecklist(); // every change passes here: the checklist follows
  clearTimeout(autosaveTimer);
  autosaveDue = true;
  setSaveStatus('saving', 'Saving…');
  autosaveTimer = setTimeout(saveSessionNow, 1200);
}

// The app window calls this before it closes. Only a pending autosave is
// written: saving an untouched, empty editor would throw away a previous
// session the user hasn't restored yet.
window.__svtBeforeClose = async () => {
  if (autosaveDue) {
    clearTimeout(autosaveTimer);
    saveSessionNow();
  }
  await Promise.race([sessionSaveChain, new Promise(resolve => setTimeout(resolve, 8000))]);
  return true;
};

// Photos are stored once each, under their own 'photo:…' key, and the session
// record only refers to those keys. Rewriting every photo on each autosave was
// slow for big tours, and in Firefox it broke restored sessions: replacing the
// record deletes the stored photo data that the restored photos still point to,
// so the preview could no longer load them ("The panorama cannot be loaded").
const photoKeys = new WeakMap(); // photo File -> its IndexedDB key
const unsavedPhotos = new Map(); // key -> File, not yet written
let photoKeySeq = 0;

function photoKeyOf(file) {
  let key = photoKeys.get(file);
  if (!key) {
    key = `photo:${Date.now().toString(36)}-${(photoKeySeq++).toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
    photoKeys.set(file, key);
    unsavedPhotos.set(key, file);
  }
  return key;
}

// Every photo still reachable from the tour or its undo history.
function referencedPhotoKeys() {
  const keys = new Set();
  const add = f => { const k = f && photoKeys.get(f); if (k) keys.add(k); };
  S.nodes.forEach(n => add(n.photoFile));
  [...undoStack, ...redoStack].forEach(snap => snap.nodes.forEach(n => add(n.photoFile)));
  restorePendingKeys.forEach(k => keys.add(k));
  return keys;
}

function idbRequest(req) {
  return new Promise((resolve, reject) => {
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

// Saves run one after another: a big first save can take longer than the
// autosave delay, and two overlapping ones would write the same photos twice.
let sessionSaveChain = Promise.resolve();
function saveSessionNow() {
  autosaveDue = false;
  sessionSaveChain = sessionSaveChain.then(saveSessionOnce, saveSessionOnce);
  return sessionSaveChain;
}

// Photos of a saved session the user has not restored or discarded yet: an
// autosave in the meantime must not delete them.
const restorePendingKeys = new Set();

async function saveSessionOnce() {
  try {
    const snap = snapshotState();
    const stored = {
      ...snap,
      format: 2,
      // A photo with a reference (app window) is not stored again, just named.
      nodes: snap.nodes.map(n => ({
        ...n, photoFile: null,
        photoRef: (n.photoFile && photoRefs.get(n.photoFile)) || null,
        photoKey: n.photoFile && !photoRefs.has(n.photoFile) ? photoKeyOf(n.photoFile) : null,
      })),
    };
    const writes = [...unsavedPhotos];
    const db = await openSaveDb();
    await new Promise((resolve, reject) => {
      const tx = db.transaction('kv', 'readwrite');
      const store = tx.objectStore('kv');
      writes.forEach(([key, file]) => store.put(file, key));
      if (!S.nodes.length) store.delete('session');
      else store.put(stored, 'session');
      tx.oncomplete = resolve;
      tx.onerror = () => reject(tx.error);
      tx.onabort = () => reject(tx.error);
    });
    writes.forEach(([key]) => unsavedPhotos.delete(key));
    await removeUnusedPhotos(db, referencedPhotoKeys());
    db.close();
    setSaveStatus('saved', 'Saved');
  } catch (err) {
    console.warn('autosave failed', err);
    setSaveStatus(null, 'Autosave failed');
  }
}

async function removeUnusedPhotos(db, keep) {
  const tx = db.transaction('kv', 'readwrite');
  const store = tx.objectStore('kv');
  const keys = await idbRequest(store.getAllKeys());
  keys.filter(k => typeof k === 'string' && k.startsWith('photo:') && !keep.has(k)).forEach(k => store.delete(k));
  await new Promise(resolve => { tx.oncomplete = resolve; tx.onerror = resolve; tx.onabort = resolve; });
}

async function clearSavedSession() {
  try {
    const db = await openSaveDb();
    const tx = db.transaction('kv', 'readwrite');
    const store = tx.objectStore('kv');
    store.delete('session');
    const keys = await idbRequest(store.getAllKeys());
    keys.filter(k => typeof k === 'string' && k.startsWith('photo:')).forEach(k => store.delete(k));
    await new Promise(resolve => { tx.oncomplete = resolve; tx.onerror = resolve; });
    db.close();
  } catch (err) { /* ignore */ }
}

async function loadSavedSession() {
  try {
    const db = await openSaveDb();
    const tx = db.transaction('kv', 'readonly');
    const store = tx.objectStore('kv');
    const snap = await idbRequest(store.get('session'));
    if (snap) {
      for (const n of snap.nodes) {
        if (!n.photoKey) {
          // Sessions saved before format 2 kept the photos inline in the record.
          if (n.photoFile) n.legacyInlinePhoto = true;
          continue;
        }
        n.photoFile = (await idbRequest(store.get(n.photoKey))) || null;
        if (n.photoFile) photoKeys.set(n.photoFile, n.photoKey);
        restorePendingKeys.add(n.photoKey);
        delete n.photoKey;
      }
    }
    db.close();
    return snap || null;
  } catch (err) {
    return null;
  }
}

const restoreBanner = document.getElementById('restore-banner');

async function checkSavedSession() {
  const snap = await loadSavedSession();
  if (!snap || !snap.nodes?.length) return;
  document.getElementById('restore-text').textContent =
    `Previous session found: ${snap.nodes.length} node(s), ${snap.conns.length} connection(s).`;
  restoreBanner.classList.add('show');

  document.getElementById('restore-yes').onclick = async () => {
    await resolvePhotoRefs(snap.nodes);
    // A photo stored in IndexedDB can come back unreadable (browser storage cleanup,
    // or the original file changed before it was copied). Drop those up front so the
    // node shows "No photo" instead of silently breaking the viewers and preview.
    for (const n of snap.nodes) {
      if (!n.photoFile) continue;
      try {
        if (n.legacyInlinePhoto) {
          // Old sessions kept photos inside the session record, which the next
          // autosave replaces (and Firefox then deletes the data) - copy them first.
          n.photoFile = new File([await n.photoFile.arrayBuffer()], n.photoFile.name || n.photoName || 'photo.jpg', { type: n.photoFile.type || 'image/jpeg' });
        } else {
          await n.photoFile.slice(0, 1).arrayBuffer();
        }
      } catch {
        n.photoFile = null;
      }
      delete n.legacyInlinePhoto;
    }
    if (S.nodes.length) pushUndo(); // work done before restoring stays reachable with Ctrl+Z
    applySnapshot(snap, { refit: true });
    restorePendingKeys.clear();
    restoreBanner.classList.remove('show');
    const lost = snap.nodes.filter(n => n.photoName && !n.photoFile).length;
    toast(lost
      ? `Session restored - ${lost} photo(s) could not be recovered, drop them again`
      : 'Session restored');
    scheduleAutosave();
  };
  document.getElementById('restore-no').onclick = () => {
    restorePendingKeys.clear();
    clearSavedSession();
    restoreBanner.classList.remove('show');
  };
}

function toast(msg) {
  logNotice(msg);
  const t = document.createElement('div');
  t.className = 'toast';
  t.textContent = msg;
  document.body.appendChild(t);
  requestAnimationFrame(() => t.classList.add('show'));
  setTimeout(() => {
    t.classList.remove('show');
    setTimeout(() => t.remove(), 250);
  }, 4000);
}

async function initSaveDir() {
  saveDirHandle = await loadSavedDirectoryHandle();
  updateSaveHint();
}

function updateSaveHint() {
  // In the app window every download asks where to save it.
  saveHint.textContent = saveDirHandle?.name || (appHasWindow ? t('ask each time') : 'Downloads');
}

async function saveTextFile(name, content) {
  if (saveDirHandle) {
    try {
      const perm = await ensureDirectoryPermission(saveDirHandle);
      if (perm === 'granted') {
        const fileHandle = await saveDirHandle.getFileHandle(name, { create: true });
        const writable = await fileHandle.createWritable();
        await writable.write(content);
        await writable.close();
        toast(`${name} saved to ${saveDirHandle.name}`);
        return true;
      }
    } catch (err) {
      console.warn('direct save failed, fallback to download', err);
      toast(`${name}: folder save failed, downloading instead`);
    }
  }
  if (appHasWindow) return saveWithAppDialog(name, content);
  dl(content, name);
  return false;
}

// The app window has no downloads bar (and each platform's web engine treats
// downloads differently), so a download is a regular "Save as" there.
async function saveWithAppDialog(name, content) {
  const ext = (/\.(\w+)$/.exec(name) || [])[1];
  try {
    const handle = await window.showSaveFilePicker({
      suggestedName: name,
      types: ext ? [{ description: ext.toUpperCase(), accept: { 'application/octet-stream': [`.${ext}`] } }] : [],
    });
    const writable = await handle.createWritable();
    await writable.write(content instanceof Blob ? content : new Blob([content], { type: 'text/plain' }));
    await writable.close();
    toast(`Saved ${handle.name}`);
    return true;
  } catch (err) {
    if (err?.name !== 'AbortError') alert(t('Saving {name} failed: {msg}', { name, msg: err.message || err }));
    return false;
  }
}

async function ensureDirectoryPermission(handle) {
  const opts = { mode: 'readwrite' };
  if ((await handle.queryPermission(opts)) === 'granted') return 'granted';
  return handle.requestPermission(opts);
}

function openSaveDb() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open('vr-tour-editor', 1);
    req.onupgradeneeded = () => req.result.createObjectStore('kv');
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

async function persistDirectoryHandle(handle) {
  try {
    const db = await openSaveDb();
    const stored = handle instanceof AppFolderHandle ? { appFolder: handle.grant, name: handle.name } : handle;
    await new Promise((resolve, reject) => {
      const tx = db.transaction('kv', 'readwrite');
      tx.objectStore('kv').put(stored, 'saveDirHandle');
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(tx.error);
    });
    db.close();
  } catch (err) {
    console.warn('persistDirectoryHandle failed', err);
  }
}

async function loadSavedDirectoryHandle() {
  try {
    const db = await openSaveDb();
    const handle = await new Promise((resolve, reject) => {
      const tx = db.transaction('kv', 'readonly');
      const req = tx.objectStore('kv').get('saveDirHandle');
      req.onsuccess = () => resolve(req.result || null);
      req.onerror = () => reject(req.error);
    });
    db.close();
    if (handle?.appFolder) {
      const j = await (await fetch('/api/files/folder', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ grant: handle.appFolder }),
      })).json();
      return j.ok ? new AppFolderHandle(j.token, j.name, handle.appFolder) : null;
    }
    return handle;
  } catch (err) {
    console.warn('loadSavedDirectoryHandle failed', err);
    return null;
  }
}

// ── Small modal helpers ───────────────────────────
function openModal(id) { document.getElementById(id).classList.add('show'); }
function closeModal(id) { document.getElementById(id).classList.remove('show'); }
document.querySelectorAll('[data-close]').forEach(el => {
  el.onclick = () => { if (el.dataset.close !== 'blur-back' || mayCloseBlur()) closeModal(el.dataset.close); };
});

function storageGet(key) { try { return localStorage.getItem(key); } catch { return null; } }
function storageSet(key, value) { try { localStorage.setItem(key, value); } catch {} }

// ── Collapsible sections of the side panel ───────
document.querySelectorAll('details.sec').forEach(sec => {
  const key = `sec:${sec.dataset.sec}`;
  const saved = storageGet(key);
  if (saved != null) sec.open = saved === '1';
  sec.addEventListener('toggle', () => {
    storageSet(key, sec.open ? '1' : '0');
    if (sec.dataset.sec === 'compare' && sec.open) refreshCompareUi(gn(S.selId));
  });
});

// ── Light / dark ──────────────────────────────────
// The choice is remembered; without one the editor follows the system. The
// theme attribute is already set in <head>, before the first paint.
const themeQuery = matchMedia('(prefers-color-scheme: light)');
function currentTheme() {
  const chosen = storageGet('theme');
  return chosen === 'light' || chosen === 'dark' ? chosen : themeQuery.matches ? 'light' : 'dark';
}
function applyTheme() {
  const theme = currentTheme();
  document.documentElement.dataset.theme = theme;
  document.getElementById('btheme').title = theme === 'dark' ? t('Switch to light mode') : t('Switch to dark mode');
  if (appHasWindow) {  // the window frame follows (title bar colour)
    fetch('/api/window-theme', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ theme }),
    }).catch(() => {});
  }
}
function toggleTheme() {
  storageSet('theme', currentTheme() === 'dark' ? 'light' : 'dark');
  applyTheme();
}
themeQuery.addEventListener('change', () => { if (!storageGet('theme')) applyTheme(); });
document.getElementById('btheme').onclick = toggleTheme;
applyTheme();

// ── First-run tour ────────────────────────────────
// A few steps that point at the real controls. Shown once (also after an update
// that brings it), and again from Tools → Introduction.
const TOUR_STEPS = [
  { title: 'Welcome to Streetview Tour Editor',
    text: 'Turn the 360° photos of a walk into a connected tour on Google Street View. This short tour shows you around - it takes a minute.' },
  { target: '#mw', title: 'Bring in your photos',
    text: 'Drag your 360° photos - or a whole folder - onto the map, or use Project → Import photos. They are placed by GPS, linked in the order you walked and turned to north in one go.' },
  { target: '#sb', title: 'Edit a node',
    text: 'Click a node - on the map or in this list - to edit it here: its name, the photo, the north direction, links and info points.' },
  { target: '#mw', title: 'Link the nodes',
    text: 'Right-click a node, then a second one, to link them - that is how visitors walk from photo to photo. Tools → Auto-connect links everything in the order you walked.' },
  { target: '#sb', title: 'Get north right',
    text: 'Turn the photo in the side panel until you face north, then click "Current view is north". Tools → North from the photos then turns all the others to match it.' },
  { target: '#btools', title: 'Tools',
    text: 'Auto-connect, placing photos from a GPX track, blurring faces and licence plates, a tour check before uploading, and comparing your tour with Google.' },
  { target: '#bpreview', title: 'Preview',
    text: 'Walk through the tour exactly as visitors will see it. If a photo looks turned after a jump, fix it right there.' },
  { target: '#bproject', title: 'Save and export',
    text: 'Ctrl+S saves the project with all photos as one .vrtour file - everything is autosaved as well. Export the tour as a website or as files for your own server.' },
  { target: '#bupload', title: 'Publish to Google',
    text: 'Uploads the photos with their position, direction and links to Google Street View. The first time, a short guide connects your Google account. Recommended: with a Places key, every photo is also tied to its building, house number or street on Google Maps.',
    action: { label: 'Set up places…', run: () => openPlacesSetup() } },
  { target: '#btheme', title: 'Make it yours',
    text: 'Switch between light and dark here. Ctrl+K lists every command, ? all shortcuts - and Tools → Introduction shows this tour again.' },
];
const tourEl = document.getElementById('tour');
const tourSpot = tourEl.querySelector('.tour-spot');
const tourCard = tourEl.querySelector('.tour-card');
let tourIndex = -1;

function startTour() {
  closeMenus();
  tourIndex = 0;
  tourEl.hidden = false;
  showTourStep();
}
function endTour() {
  tourEl.hidden = true;
  tourIndex = -1;
  storageSet('tourDone', '1');
}
function showTourStep() {
  const step = TOUR_STEPS[tourIndex];
  const last = tourIndex === TOUR_STEPS.length - 1;
  document.getElementById('tour-step').textContent = `${tourIndex + 1} / ${TOUR_STEPS.length}`;
  document.getElementById('tour-title').textContent = t(step.title);
  document.getElementById('tour-text').textContent = t(step.text);
  document.getElementById('tour-back').hidden = tourIndex === 0;
  document.getElementById('tour-skip').hidden = last;
  const action = document.getElementById('tour-action');
  action.hidden = !step.action;
  if (step.action) action.textContent = t(step.action.label);
  document.getElementById('tour-next').textContent = tourIndex === 0 ? t('Start the tour') : last ? t('Done') : t('Next');
  placeTour();
  document.getElementById('tour-next').focus();
}
function placeTour() {
  if (tourIndex < 0) return;
  const step = TOUR_STEPS[tourIndex];
  const target = step.target && document.querySelector(step.target);
  const vw = innerWidth, vh = innerHeight, gap = 14;
  const card = tourCard.getBoundingClientRect();
  if (!target) {  // welcome: no spotlight, card in the middle
    Object.assign(tourSpot.style, { left: `${vw / 2}px`, top: `${vh / 2}px`, width: '0px', height: '0px' });
    Object.assign(tourCard.style, { left: `${(vw - card.width) / 2}px`, top: `${(vh - card.height) / 2}px` });
    return;
  }
  const r = target.getBoundingClientRect();
  const pad = r.width > 200 ? 0 : 6;  // big areas (map, side panel) are shown as they are
  Object.assign(tourSpot.style, {
    left: `${r.left - pad}px`, top: `${r.top - pad}px`, width: `${r.width + 2 * pad}px`, height: `${r.height + 2 * pad}px`,
  });
  let left, top;
  if (r.width > vw / 2) {           // the map: card inside it, lower middle
    left = r.left + (r.width - card.width) / 2;
    top = r.bottom - card.height - 40;
  } else if (r.height > vh / 2) {   // the side panel: card right of it
    left = r.right + gap;
    top = r.top + 80;
  } else {                          // a toolbar button: card below it
    left = r.left + r.width / 2 - card.width / 2;
    top = r.bottom + gap;
  }
  left = Math.max(16, Math.min(left, vw - card.width - 16));
  top = Math.max(16, Math.min(top, vh - card.height - 16));
  Object.assign(tourCard.style, { left: `${left}px`, top: `${top}px` });
}
document.getElementById('tour-next').onclick = () => {
  if (tourIndex >= TOUR_STEPS.length - 1) endTour();
  else { tourIndex++; showTourStep(); }
};
document.getElementById('tour-back').onclick = () => { if (tourIndex > 0) { tourIndex--; showTourStep(); } };
document.getElementById('tour-skip').onclick = endTour;
document.getElementById('tour-action').onclick = () => {
  const step = TOUR_STEPS[tourIndex];
  endTour();
  step?.action?.run();
};
addEventListener('resize', placeTour);
// While the tour runs it owns the keyboard (no shortcuts reach the editor).
addEventListener('keydown', e => {
  if (tourIndex < 0) return;
  e.stopPropagation();
  if (e.key === 'Escape') { e.preventDefault(); endTour(); }
  else if (e.key === 'ArrowRight' || e.key === 'Enter') { e.preventDefault(); document.getElementById('tour-next').click(); }
  else if (e.key === 'ArrowLeft') { e.preventDefault(); document.getElementById('tour-back').click(); }
}, true);

// ── Toolbar menus (Project, Tools) ────────────────
function closeMenus() { document.querySelectorAll('#tb .menu.show').forEach(m => m.classList.remove('show')); }
document.querySelectorAll('#tb .menu-wrap').forEach(wrap => {
  const menu = wrap.querySelector('.menu');
  wrap.querySelector(':scope > .btn').onclick = e => {
    e.stopPropagation();
    const open = !menu.classList.contains('show');
    closeMenus();
    menu.classList.toggle('show', open);
  };
  // Every entry closes the menu; entries with an id have their own handler elsewhere.
  menu.addEventListener('click', e => { if (e.target.closest('button')) closeMenus(); });
  menu.querySelectorAll('button[data-run]').forEach(b => { b.onclick = () => TOOL_ACTIONS[b.dataset.run](); });
});
document.addEventListener('click', e => { if (!e.target.closest('#tb .menu')) closeMenus(); });
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeMenus(); });

const TOOL_ACTIONS = {
  autoconnect: openAutoConnect,
  gpxplace: openGpx,
  shift: () => openShift(),
  blur: () => openBlurTool(S.selId),
  privacy: () => openPrivacy(),
  video: () => openVideo(),
  sample: () => openSample(),
  fromgoogle: () => openFromGoogle(),
  rename: () => openRename(),
  kml: () => runExport('kml'),
  gpx: () => runExport('gpx'),
  share: () => openShare(),
  report: () => openReport(),
  website: () => runExport('website'),
  viewer: () => runExport('viewer'),
  exportproject: () => runExport('project'),
  tour: startTour,
  shortcuts: () => cheatBack.classList.add('show'),
  updates: () => checkForUpdates({ force: true, quiet: false }),
  saveas: () => saveProject({ saveAs: true }),
  walkheadings: () => openWalkHeadings(),
  northmatch: () => openNorthMatch(),
  check: () => openTourCheck(),
  gstatus: () => openGoogleCompare(),
  views: () => openViews(),
  places: () => openPlacesSetup(),
  lang: () => setLang(currentLang === 'de' ? 'en' : 'de'),
  nadir: () => openNadir(),
  sunnorth: () => openSunNorth(),
  panoramax: () => openPanoramax(),
  photos: () => runExport('photos'),
};

// ── Capture time (EXIF) ───────────────────────────
// Stored as the camera's local wall-clock time ("2026-05-01T14:03:22") plus its
// zone offset when the camera recorded one, since EXIF has no time zone itself.
function naiveIso(d) {
  const p = v => String(v).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

async function ensureCaptureTime(n) {
  if (n.captureTime !== undefined || !n.photoFile) return n.captureTime ?? null;
  const meta = await readPhotoMeta(n.photoFile);
  n.captureTime = meta.captureTime;
  n.captureOffset = meta.captureOffset;
  return n.captureTime;
}

function scopeNodes() {
  const ids = S.multiSel.size >= 2 ? S.multiSel : null;
  return ids ? S.nodes.filter(n => ids.has(n.id)) : S.nodes.slice();
}

function scopeLabel(list) {
  return S.multiSel.size >= 2 ? `Applies to the ${list.length} selected nodes.` : `Applies to all ${list.length} nodes (select nodes first to limit it).`;
}

// ── Auto-connect ──────────────────────────────────
function openAutoConnect() {
  const list = scopeNodes();
  if (list.length < 2) { alert('Add at least two nodes first.'); return; }
  document.getElementById('autoc-scope').textContent = scopeLabel(list);
  openModal('autoc-back');
}

function removeConnsWithin(ids) {
  S.conns.filter(c => ids.has(c.from) && ids.has(c.to)).forEach(c => {
    if (c.poly) map.removeLayer(c.poly);
    if (c.xMark) map.removeLayer(c.xMark);
  });
  S.conns = S.conns.filter(c => !(ids.has(c.from) && ids.has(c.to)));
}

document.getElementById('autoc-go').onclick = async () => {
  const list = scopeNodes();
  const mode = document.querySelector('input[name="autoc-mode"]:checked').value;
  const maxDist = Math.max(1, Number(document.getElementById('autoc-dist').value) || 25);
  const replace = document.getElementById('autoc-replace').checked;

  const pairs = [];
  let untimed = 0;
  if (mode === 'time') {
    for (const n of list) await ensureCaptureTime(n);
    const order = list.map((n, i) => ({ n, i, t: n.captureTime ? Date.parse(`${n.captureTime}Z`) : null }));
    untimed = order.filter(o => o.t == null).length;
    order.sort((a, b) => (a.t ?? Infinity) - (b.t ?? Infinity) || a.i - b.i);
    for (let i = 0; i < order.length - 1; i++) pairs.push([order[i].n, order[i + 1].n]);
  } else {
    for (const n of list) {
      list.filter(o => o !== n)
        .map(o => ({ o, d: distanceMeters(n, o) }))
        .sort((a, b) => a.d - b.d)
        .slice(0, 3)
        .forEach(({ o }) => pairs.push([n, o]));
    }
  }

  pushUndo();
  if (replace) removeConnsWithin(new Set(list.map(n => n.id)));
  const before = S.conns.length;
  let tooFar = 0;
  for (const [a, b] of pairs) {
    if (distanceMeters(a, b) > maxDist) { tooFar++; continue; }
    if (gcbs(a.id, b.id).length) continue;
    S.conns.push({ id: cuid(), from: a.id, to: b.id, poly: null, xMark: null });
  }
  S.conns.slice(before).forEach(c => drawConn(c));
  tourChanged();
  closeModal('autoc-back');
  const added = S.conns.length - before;
  const notes = [];
  if (mode === 'time' && untimed) notes.push(`${untimed} without capture time`);
  if (mode === 'time' && tooFar) notes.push(`${tooFar} gap(s) over ${maxDist} m skipped`);
  toast(`Added ${added} connection(s)${notes.length ? ` - ${notes.join(', ')}` : ''}`);
};

// ── Move or turn a tour ───────────────────────────
// GPS a few metres off: the whole tour moved or turned about its centre, or each
// point snapped onto the nearest path from OpenStreetMap. Live on the map; the
// whole session is one undo step.
const OVERPASS = 'https://overpass-api.de/api/interpreter';
const shift = { before: null, start: null, nodes: [], east: 0, north: 0, turn: 0, snapped: 0 };

function openShift() {
  closeMenus();
  if (!S.nodes.length) { alert('No nodes found.'); return; }
  const sel = document.getElementById('sh-tour');
  const options = getTours().map(g => ({ key: `t${g.index}`, label: tourName(g), nodes: g.nodes }));
  options.push({ key: 'all', label: t('All points'), nodes: S.nodes });
  sel.innerHTML = options.map(o => `<option value="${o.key}">${escapeHtml(o.label)} · ${escapeHtml(t('{n} point(s)', { n: o.nodes.length }))}</option>`).join('');
  sel.onchange = () => { shiftFinish(); shiftBegin(options.find(o => o.key === sel.value).nodes); };
  const cur = tourOf(S.selId);
  sel.value = cur?.index != null ? `t${cur.index}` : options[0].key;
  shiftBegin(options.find(o => o.key === sel.value).nodes);
  openModal('shift-back');
}

function shiftBegin(nodes) {
  Object.assign(shift, {
    before: snapshotState(), nodes,
    start: new Map(nodes.map(n => [n.id, { lat: n.lat, lng: n.lng, heading: n.heading }])),
    east: 0, north: 0, turn: 0, snapped: 0,
  });
  shiftSummary();
  map.fitBounds(L.latLngBounds(nodes.map(n => [n.lat, n.lng])), { padding: [60, 60], maxZoom: 19 });
}

// The start positions, moved and turned; snapping comes on top of that.
function shiftApply() {
  const pts = shift.nodes.map(n => shift.start.get(n.id));
  const centre = { lat: pts.reduce((a, p) => a + p.lat, 0) / pts.length, lng: pts.reduce((a, p) => a + p.lng, 0) / pts.length };
  const rad = (shift.turn * Math.PI) / 180;
  shift.nodes.forEach(n => {
    const s = shift.start.get(n.id);
    const q = toLocal(s, centre);
    // clockwise on the map, as a compass turns
    const r = { x: q.x * Math.cos(rad) + q.y * Math.sin(rad) + shift.east, y: -q.x * Math.sin(rad) + q.y * Math.cos(rad) + shift.north };
    Object.assign(n, fromLocal(r, centre));
    if (n.headingSource === 'auto' && s.heading != null) {
      n.heading = normalizeHeading(s.heading + shift.turn);
      n.viewYaw = normalizeRad(-degToRad(n.heading));
    }
    n.marker?.setLatLng([n.lat, n.lng]);
  });
  shift.snapped = 0;
  refreshConnLines();
  shiftSummary();
}

function shiftSummary() {
  const parts = [];
  const dist = Math.hypot(shift.east, shift.north);
  if (dist) {
    const dir = ['north', 'north-east', 'east', 'south-east', 'south', 'south-west', 'west', 'north-west'][Math.round(((Math.atan2(shift.east, shift.north) * 180) / Math.PI + 360) % 360 / 45) % 8];
    parts.push(t('moved {d} to the {dir}', { d: formatDistance(dist), dir: t(dir) }));
  }
  if (shift.turn) parts.push(t('turned {a}° {way}', { a: Math.abs(shift.turn), way: t(shift.turn > 0 ? 'clockwise' : 'anticlockwise') }));
  if (shift.snapped) parts.push(t('{n} point(s) snapped to paths', { n: shift.snapped }));
  document.getElementById('sh-sum').textContent = parts.length ? `${t('So far:')} ${parts.join(', ')}.` : t('Not changed yet.');
}

document.querySelectorAll('#shift-back [data-move]').forEach(b => {
  b.onclick = () => {
    const step = Number(document.getElementById('sh-step').value);
    const [dx, dy] = b.dataset.move.split(',').map(Number);
    shift.east += dx * step;
    shift.north += dy * step;
    shiftApply();
  };
});
document.querySelectorAll('#shift-back [data-turn]').forEach(b => {
  b.onclick = () => { shift.turn += Number(b.dataset.turn); shiftApply(); };
});
document.getElementById('sh-reset').onclick = () => { shift.east = shift.north = shift.turn = 0; shiftApply(); };

document.getElementById('sh-snap').onclick = async () => {
  const max = Math.max(1, Math.min(30, Number(document.getElementById('sh-snap-dist').value) || 8));
  const btn = document.getElementById('sh-snap');
  const sum = document.getElementById('sh-sum');
  const lats = shift.nodes.map(n => n.lat), lngs = shift.nodes.map(n => n.lng);
  const pad = 0.0006;
  const bbox = [Math.min(...lats) - pad, Math.min(...lngs) - pad, Math.max(...lats) + pad, Math.max(...lngs) + pad].map(v => v.toFixed(6)).join(',');
  btn.disabled = true;
  sum.textContent = t('Asking OpenStreetMap for the paths here…');
  try {
    const r = await fetch(OVERPASS, {
      method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: `data=${encodeURIComponent(`[out:json][timeout:25];way["highway"]["area"!="yes"](${bbox});out geom;`)}`,
    });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const ways = ((await r.json()).elements || []).filter(e => e.geometry?.length > 1).map(e => e.geometry);
    let moved = 0, most = 0;
    shift.nodes.forEach(n => {
      let best = null;
      ways.forEach(g => {
        for (let i = 0; i < g.length - 1; i++) {
          const a = toLocal({ lat: g[i].lat, lng: g[i].lon }, n), b = toLocal({ lat: g[i + 1].lat, lng: g[i + 1].lon }, n);
          const dx = b.x - a.x, dy = b.y - a.y;
          const k = Math.max(0, Math.min(1, -(a.x * dx + a.y * dy) / (dx * dx + dy * dy || 1)));
          const q = { x: a.x + k * dx, y: a.y + k * dy };
          const d = Math.hypot(q.x, q.y);
          if (!best || d < best.d) best = { d, q };
        }
      });
      if (best && best.d > 0.2 && best.d <= max) {
        Object.assign(n, fromLocal(best.q, n));
        n.marker?.setLatLng([n.lat, n.lng]);
        moved++;
        most = Math.max(most, best.d);
      }
    });
    refreshConnLines();
    shift.snapped += moved;
    shiftSummary();
    toast(ways.length
      ? t('{n} of {m} point(s) moved onto a path, by {d} at most.', { n: moved, m: shift.nodes.length, d: formatDistance(most) })
      : t('OpenStreetMap has no paths here.'));
  } catch (err) {
    sum.textContent = t('Could not get the paths from OpenStreetMap: {msg}', { msg: err.message || err });
  } finally {
    btn.disabled = false;
  }
};

// Closing (Done, Close or Esc) keeps what was done, as one undo step.
function shiftFinish() {
  if (!shift.before) return;
  const changed = shift.nodes.some(n => {
    const s = shift.start.get(n.id);
    return Math.abs(n.lat - s.lat) > 1e-9 || Math.abs(n.lng - s.lng) > 1e-9;
  });
  if (changed) {
    pushUndo(shift.before);
    tourChanged();
  }
  shift.before = null;
}
document.getElementById('sh-done').onclick = () => closeModal('shift-back');
new MutationObserver(() => {
  if (!document.getElementById('shift-back').classList.contains('show')) shiftFinish();
}).observe(document.getElementById('shift-back'), { attributes: true, attributeFilter: ['class'] });

// ── GPX import ────────────────────────────────────
function openGpx() {
  if (!S.nodes.length) { alert('Add photos first.'); return; }
  const withOffset = S.nodes.find(n => n.captureOffset);
  const offset = withOffset ? parseOffsetHours(withOffset.captureOffset) : -new Date().getTimezoneOffset() / 60;
  document.getElementById('gpx-offset').value = offset;
  document.getElementById('gpx-result').textContent = '';
  openModal('gpx-back');
}

document.getElementById('gpx-go').onclick = async () => {
  const file = document.getElementById('gpx-file').files[0];
  const out = document.getElementById('gpx-result');
  if (!file) { out.textContent = 'Choose a GPX file first.'; return; }
  let pts;
  try { pts = parseGpx(await file.text()); } catch (err) { out.textContent = err.message; return; }
  const offsetMs = Number(document.getElementById('gpx-offset').value || 0) * 3600000;
  const moveAll = document.getElementById('gpx-all').checked;
  const list = scopeNodes().filter(n => moveAll || !n.hasGps);
  if (!list.length) { out.textContent = 'All photos already have GPS - tick "Also move photos that already have GPS" to override.'; return; }

  pushUndo();
  let placed = 0, noTime = 0, outside = 0;
  for (const n of list) {
    const local = await ensureCaptureTime(n);
    if (!local) { noTime++; continue; }
    const pos = positionAt(pts, Date.parse(`${local}Z`) - offsetMs);
    if (!pos) { outside++; continue; }
    n.lat = pos.lat;
    n.lng = pos.lng;
    n.hasGps = true;
    n.marker?.setLatLng([n.lat, n.lng]);
    placed++;
  }
  refreshConnLines();
  tourChanged();
  if (placed) fitAll();
  const notes = [];
  if (noTime) notes.push(`${noTime} without capture time`);
  if (outside) notes.push(`${outside} outside the track's time range - check the time zone`);
  out.textContent = `Placed ${placed} of ${list.length} photo(s).${notes.length ? ` ${notes.join('; ')}.` : ''}`;
  if (placed && !notes.length) { closeModal('gpx-back'); toast(`Placed ${placed} photo(s) from ${file.name}`); }
};

// ── Blur tool ─────────────────────────────────────
const blurStage = document.getElementById('blur-stage');
const blurCanvas = document.getElementById('blur-canvas');
const blurCtx = blurCanvas.getContext('2d');
const blur = { node: null, bmp: null, rects: [], scale: 1, ox: 0, oy: 0, drag: null, style: 'pixelate' };
const blurStyle = document.getElementById('blur-style');
blur.style = blurStyle.value = storageGet('blurStyle') === 'blur' ? 'blur' : 'pixelate';
blurStyle.onchange = () => { blur.style = blurStyle.value; storageSet('blurStyle', blur.style); drawBlur(); };

// presets: suggestions to start with; batch: opened from the check of all photos.
async function openBlurTool(id, { presets = null, batch = false } = {}) {
  const n = gn(id);
  if (!n?.photoFile) { alert('Select a node with a photo first.'); return; }
  toast('Loading photo…');
  try {
    blur.bmp?.close?.(); // free the previous photo (a decoded 8K panorama is ~130 MB)
    blur.bmp = await createImageBitmap(n.photoFile);
  } catch (err) {
    alert(`Could not load the photo: ${err.message || err}`);
    return;
  }
  blur.node = n;
  blur.rects = (presets || []).map(r => ({ ...r, auto: true }));
  blur.batch = batch;
  openModal('blur-back');
  requestAnimationFrame(() => { fitBlurView(); drawBlur(); });
}

function fitBlurView() {
  const r = blurStage.getBoundingClientRect();
  blurCanvas.width = Math.round(r.width * devicePixelRatio);
  blurCanvas.height = Math.round(r.height * devicePixelRatio);
  blur.fit = blurCanvas.width / blur.bmp.width;
  blur.scale = blur.fit;
  blur.ox = 0;
  blur.oy = 0;
}

function clampBlurView() {
  const vw = blurCanvas.width / blur.scale, vh = blurCanvas.height / blur.scale;
  blur.ox = Math.min(Math.max(blur.ox, 0), Math.max(0, blur.bmp.width - vw));
  blur.oy = Math.min(Math.max(blur.oy, 0), Math.max(0, blur.bmp.height - vh));
}

function drawBlur() {
  const { bmp, scale, ox, oy } = blur;
  blurCtx.fillStyle = '#000';
  blurCtx.fillRect(0, 0, blurCanvas.width, blurCanvas.height);
  blurCtx.drawImage(bmp, 0, 0, bmp.width, bmp.height, -ox * scale, -oy * scale, bmp.width * scale, bmp.height * scale);
  const all = blur.drag?.rect ? [...blur.rects, blur.drag.rect] : blur.rects;
  all.forEach(r => {
    const x = (r.x - ox) * scale, y = (r.y - oy) * scale, w = r.w * scale, h = r.h * scale;
    hideArea(blurCtx, { ...r, x: r.x - ox, y: r.y - oy }, blur.style, scale);
    blurCtx.strokeStyle = !r.auto ? '#c4b5fd' : r.kind === 'plate' ? '#38bdf8' : '#f6b93b';
    blurCtx.lineWidth = 2;
    blurCtx.setLineDash(r.auto ? [8, 5] : []);
    blurCtx.strokeRect(x, y, w, h);
    blurCtx.setLineDash([]);
  });
  document.getElementById('blur-count').textContent = blur.rects.length
    ? `${blur.rects.length} area(s) marked.` : 'Drag boxes over faces and plates.';
}

function blurImagePoint(e) {
  const r = blurCanvas.getBoundingClientRect();
  const cx = (e.clientX - r.left) * devicePixelRatio, cy = (e.clientY - r.top) * devicePixelRatio;
  return { x: blur.ox + cx / blur.scale, y: blur.oy + cy / blur.scale, cx, cy };
}

blurCanvas.addEventListener('contextmenu', e => e.preventDefault());
blurCanvas.addEventListener('pointerdown', e => {
  if (!blur.bmp) return;
  blurCanvas.setPointerCapture(e.pointerId);
  const p = blurImagePoint(e);
  if (e.button === 0 && !e.altKey) blur.drag = { kind: 'rect', x0: p.x, y0: p.y, rect: null };
  else { blur.drag = { kind: 'pan', cx: p.cx, cy: p.cy, ox: blur.ox, oy: blur.oy }; blurStage.classList.add('panning'); }
});
blurCanvas.addEventListener('pointermove', e => {
  const d = blur.drag;
  if (!d) return;
  const p = blurImagePoint(e);
  if (d.kind === 'rect') {
    const x = Math.max(0, Math.min(d.x0, p.x)), y = Math.max(0, Math.min(d.y0, p.y));
    d.rect = { x, y, w: Math.min(blur.bmp.width, Math.max(d.x0, p.x)) - x, h: Math.min(blur.bmp.height, Math.max(d.y0, p.y)) - y };
  } else {
    blur.ox = d.ox - (p.cx - d.cx) / blur.scale;
    blur.oy = d.oy - (p.cy - d.cy) / blur.scale;
    clampBlurView();
  }
  drawBlur();
});
blurCanvas.addEventListener('pointerup', () => {
  const d = blur.drag;
  blur.drag = null;
  blurStage.classList.remove('panning');
  if (d?.kind === 'rect' && d.rect && d.rect.w * blur.scale > 4 && d.rect.h * blur.scale > 4) {
    blur.rects.push(d.rect);
  } else if (d?.kind === 'rect') {
    // A click without dragging removes the box under the pointer.
    const i = blur.rects.findLastIndex(r => d.x0 >= r.x && d.x0 <= r.x + r.w && d.y0 >= r.y && d.y0 <= r.y + r.h);
    if (i >= 0) blur.rects.splice(i, 1);
  }
  drawBlur();
});
blurCanvas.addEventListener('wheel', e => {
  e.preventDefault();
  const p = blurImagePoint(e);
  blur.scale = Math.min(Math.max(blur.scale * (e.deltaY < 0 ? 1.25 : 0.8), blur.fit), 4);
  blur.ox = p.x - p.cx / blur.scale;
  blur.oy = p.y - p.cy / blur.scale;
  clampBlurView();
  drawBlur();
}, { passive: false });

document.getElementById('blur-undo').onclick = () => { blur.rects.pop(); drawBlur(); };
// Looked at, nothing to hide: the photo counts as checked.
document.getElementById('blur-none').onclick = () => {
  const n = blur.node;
  if (blur.rects.length && !confirm(t('Close without blurring? The {n} marked area(s) are not applied.', { n: blur.rects.length }))) return;
  blur.rects = [];
  if (n && !n.privacyChecked) { n.privacyChecked = true; scheduleAutosave(); updateSb(); }
  closeModal('blur-back');
};
// Closing with marked areas that are not applied asks first (they would be lost).
function mayCloseBlur() {
  return !blur.rects.length || confirm(t('Close without blurring? The {n} marked area(s) are not applied.', { n: blur.rects.length }));
}
document.getElementById('blur-clear').onclick = () => { blur.rects = []; drawBlur(); };

document.getElementById('blur-apply').onclick = async () => {
  const n = blur.node;
  if (!n || !blur.rects.length) { closeModal('blur-back'); return; }
  const btn = document.getElementById('blur-apply');
  btn.disabled = true;
  btn.textContent = 'Applying…';
  try {
    const jpeg = await rewritePhoto(n, blur.bmp, ctx => blur.rects.forEach(r => hideArea(ctx, r, blur.style)));
    pushUndo();
    replacePhoto(n, jpeg);
    n.blurred = true;
    n.privacyChecked = true;
    updateSb();
    refreshMarker(n);
    scheduleAutosave();
    if (blur.batch && privacy.results.has(n.id)) privacy.results.get(n.id).done = true;
    blur.rects = [];
    closeModal('blur-back');
    toast(`Blurred ${blur.rects.length} area(s) in ${n.name}`);
  } catch (err) {
    alert(`Blurring failed: ${err.message || err}`);
  } finally {
    btn.disabled = false;
    btn.textContent = 'Apply to photo';
  }
};

// A photo with changed pixels (draw changes them), as JPEG with the original's 360° metadata
// and a new EXIF block: capture time, camera, position and north - never the original EXIF,
// which can hold an unblurred thumbnail. source: an ImageBitmap already decoded, or null.
async function rewritePhoto(n, source, draw, { maxBytes = GOOGLE_MAX } = {}) {
  const bmp = source || await createImageBitmap(n.photoFile);
  try {
    const canvas = new OffscreenCanvas(bmp.width, bmp.height);
    const ctx = canvas.getContext('2d', { willReadFrequently: true });
    ctx.drawImage(bmp, 0, 0);
    draw?.(ctx, bmp.width, bmp.height);
    const encoded = await encodeJpeg(canvas, { quality: 0.92, maxBytes });
    return withMetadata(encoded, n.photoFile, await poseExif(n));
  } finally {
    if (!source) bmp.close?.();
  }
}

// The EXIF block for a photo as the tour has it: where it is, which way it faces, when it was taken.
async function poseExif(n, file = n.photoFile) {
  await ensureCaptureTime(n);
  return buildExif({
    taken: n.captureTime, offset: n.captureOffset, lat: n.lat, lng: n.lng, heading: n.heading,
    ...(await readCamera(file)),
  });
}

// The node gets the new photo; the one on Google is out of date (uploaded again, the old one deleted).
function replacePhoto(n, blob) {
  n.photoFile = new File([blob], n.photoName || 'photo.jpg', { type: 'image/jpeg' });
  if (n.photoUrl) URL.revokeObjectURL(n.photoUrl);
  n.photoUrl = URL.createObjectURL(n.photoFile);
  retireGooglePhoto(n);
  panoNodeId = null;
}

// ── Export as website (.zip) ──────────────────────
function safeFileName(name) {
  return (name || 'photo.jpg').replace(/[^\w.\-]+/g, '_');
}

async function exportTourWebsite(scope = null) {
  const all = scope?.nodes || S.nodes;
  const withPhoto = all.filter(n => n.photoFile);
  if (!withPhoto.length) { alert('None of the nodes has a photo loaded.'); return; }
  const ids = new Set(withPhoto.map(n => n.id));
  const { startId, nodesConf } = buildTourConfig(all);
  const nodes = nodesConf
    .filter(nc => ids.has(nc.id))
    .map(nc => ({
      ...nc,
      panorama: `photos/${nc.id}-${safeFileName(gn(nc.id).photoName)}`,
      links: nc.links.filter(l => ids.has(l.nodeId)),
    }));
  const start = ids.has(startId) ? startId : nodes[0].id;
  const readme = [
    'Streetview Tour Editor - exported tour',
    '',
    'Upload this whole folder to any web space and open index.html there.',
    '',
    'Opening index.html straight from disk does not work in most browsers (they block',
    'loading the photos from file://). To look at it locally, run in this folder:',
    '  python -m http.server 8000',
    'and open http://localhost:8000',
    '',
  ].join('\r\n');
  try {
    setSaveStatus('saving', 'Packing tour…');
    // The viewer's libraries go into the zip: the website needs no CDN.
    const libs = await Promise.all(Object.keys(VIEWER_LIBS).map(async path => {
      const r = await fetch(`vendor/${path}`);
      if (!r.ok) throw new Error(`vendor/${path}: HTTP ${r.status}`);
      return { name: `lib/${path}`, data: await r.blob() };
    }));
    const entries = [
      { name: 'index.html', data: new Blob([genViewer(start, nodes, { libBase: './lib/', title: scope?.name })], { type: 'text/html' }) },
      { name: 'tour-config.json', data: new Blob([JSON.stringify({ startNodeId: start, nodes }, null, 2)], { type: 'application/json' }) },
      { name: 'README.txt', data: new Blob([readme], { type: 'text/plain' }) },
      ...libs,
      { name: 'lib/LICENSES.txt', data: new Blob([VIEWER_LICENSES], { type: 'text/plain' }) },
      ...nodes.map(nc => ({ name: nc.panorama, data: gn(nc.id).photoFile })),
    ];
    const zip = await buildZip(entries);
    setSaveStatus('saved', 'Saved');
    await saveTextFile(scope ? `${fileBase(scope.name)}-website.zip` : 'tour-website.zip', zip);
    const skipped = all.length - withPhoto.length;
    if (skipped) setTimeout(() => toast(`${skipped} node(s) without a photo were left out`), 400);
  } catch (err) {
    setSaveStatus(null, 'Export failed');
    alert(`Export failed: ${err.message || err}`);
  }
}
document.getElementById('preview-website').onclick = () => { previewBack.classList.remove('show'); exportTourWebsite(previewScope); };

// ── Google setup wizard ───────────────────────────
const wizSteps = [...document.querySelectorAll('#wiz .wiz-step')];
function showWizStep(i) {
  wizSteps.forEach((el, k) => {
    el.classList.toggle('open', k === i);
    el.classList.toggle('done', k < Number(storageGet('gsvWizardDone') || 0));
  });
}
wizSteps.forEach((el, i) => {
  el.querySelector('.wiz-h').onclick = () => showWizStep(el.classList.contains('open') ? -1 : i);
  el.querySelector('.wiz-next')?.addEventListener('click', () => {
    storageSet('gsvWizardDone', String(Math.max(i + 1, Number(storageGet('gsvWizardDone') || 0))));
    showWizStep(i + 1);
  });
});
showWizStep(Math.min(Number(storageGet('gsvWizardDone') || 0), wizSteps.length - 1));

document.getElementById('upl-json').addEventListener('change', async e => {
  const file = e.target.files[0];
  e.target.value = '';
  if (!file) return;
  const fields = extractClientFields(await file.text());
  if (!fields) { alert('That file does not contain an OAuth client ID and secret.'); return; }
  uplClientId.value = fields.client_id;
  uplClientSecret.value = fields.client_secret;
  toast('Filled Client ID and Client Secret from the JSON file');
});

// ── App version, updates, files opened from Explorer ─
const updateBanner = document.getElementById('update-banner');
const updateGo = document.getElementById('update-go');
let updateInfo = null;

async function initAppInfo() {
  try {
    const j = await (await fetch('/api/app-info')).json();
    document.getElementById('app-version').textContent = `Streetview Tour Editor ${j.version === 'dev' ? 'dev' : j.version}`;
    appHasWindow = !!j.window;
    if (appHasWindow) {
      useAppFileDialogs();
      updateSaveHint();
      updateTitle();
      applyTheme();
    }
    return j;
  } catch { return null; }
}

async function checkForUpdates({ force = false, quiet = true } = {}) {
  try {
    const j = await (await fetch(`/api/update/check${force ? '?force=1' : ''}`)).json();
    if (j.dev) { if (!quiet) toast('Running from source - update with git pull'); return; }
    if (!j.ok) { if (!quiet) toast(`Update check failed: ${j.error}`); return; }
    updateInfo = j;
    if (!j.newer) { if (!quiet) toast(`You're on the latest version (${j.current})`); return; }
    if (quiet && storageGet('dismissedUpdate') === j.latest) return;
    document.getElementById('update-text').innerHTML =
      `Version <b>${escapeHtml(j.latest)}</b> is available <span>(you have ${escapeHtml(j.current)})</span>`;
    updateGo.textContent = j.canInstall ? 'Update now' : j.howTo?.kind === 'apt' ? 'How to update' : 'Download';
    updateBanner.classList.add('show');
  } catch {
    if (!quiet) toast('Update check failed - is the app still running?');
  }
}

document.getElementById('update-later').onclick = () => {
  if (updateInfo) storageSet('dismissedUpdate', updateInfo.latest);
  updateBanner.classList.remove('show');
};

document.getElementById('update-notes').onclick = () => {
  if (!updateInfo) return;
  document.getElementById('update-title').textContent = `What's new in ${updateInfo.latest}`;
  document.getElementById('update-body').innerHTML =
    `<div class="notes">${escapeHtml(updateInfo.notes || 'No release notes.')}</div>` +
    `<div class="mini"><a href="${escapeHtml(updateInfo.pageUrl || '#')}" target="_blank" rel="noopener" style="color:var(--accent-2)">Open the release page</a></div>`;
  document.getElementById('update-progress').style.display = 'none';
  document.getElementById('update-status').textContent = '';
  document.getElementById('update-close').style.display = '';
  openModal('update-back');
};
document.getElementById('update-close').onclick = () => closeModal('update-back');

// A copy that can't install the update itself: how it gets it, for this computer.
function showUpdateHowTo(info) {
  const how = info.howTo || { kind: 'page' };
  const link = how.url ? `<p><a class="btn g" href="${escapeHtml(how.url)}" target="_blank" rel="noopener">${escapeHtml(t('Download {name}', { name: how.name }))}</a></p>` : '';
  const code = cmd => `<pre class="update-cmd" data-no-i18n>${escapeHtml(cmd)}</pre>`;
  const text = {
    macos: `<p>${escapeHtml(t('Download the disk image, open it and drag Streetview Tour Editor into Applications, replacing the old one. Your sign-in, settings and work are kept.'))}</p>${link}`,
    apt: `<p>${escapeHtml(t("Installed from the apt repository: the update comes with your system's updates, or in a terminal:"))}</p>${code('sudo apt update && sudo apt upgrade')}`,
    deb: `<p>${escapeHtml(t('Download the new package and install it over the old one - your settings are kept:'))}</p>${link}${code(`sudo apt install ./${how.name}`)}`
      + `<p class="mini">${escapeHtml(t("With the apt repository (see the README), updates come with the system's own."))}</p>`,
    portable: `<p>${escapeHtml(t('Close the editor, download the new portable zip and unpack it over the old folder. Your settings are kept.'))}</p>${link}`,
    page: '',
  }[how.kind] || '';
  document.getElementById('update-title').textContent = t('Version {v} is available', { v: info.latest });
  document.getElementById('update-body').innerHTML = `<div class="update-howto">${text}</div>`
    + `<div class="mini"><a href="${escapeHtml(info.pageUrl || '#')}" target="_blank" rel="noopener" style="color:var(--accent-2)">${escapeHtml(t('Open the release page'))}</a></div>`;
  document.getElementById('update-progress').style.display = 'none';
  document.getElementById('update-status').textContent = '';
  document.getElementById('update-close').style.display = '';
  openModal('update-back');
}

updateGo.onclick = async () => {
  if (!updateInfo) return;
  if (!updateInfo.canInstall) {
    if (updateInfo.howTo && updateInfo.howTo.kind !== 'page') showUpdateHowTo(updateInfo);
    else window.open(updateInfo.pageUrl, '_blank', 'noopener');
    return;
  }
  updateBanner.classList.remove('show');
  appUpdating = true;
  const bar = document.getElementById('update-progress');
  const statusEl = document.getElementById('update-status');
  document.getElementById('update-title').textContent = `Updating to ${updateInfo.latest}`;
  document.getElementById('update-body').innerHTML = `<div class="mini" style="margin-top:0">${appHasWindow
    ? t('Your work is saved first. The window closes while the new version installs, then the editor opens again by itself.')
    : t('Your work is saved first. The editor closes briefly while the new version installs, then this page reloads by itself.')}</div>`;
  document.getElementById('update-close').style.display = 'none';
  bar.style.display = '';
  bar.classList.remove('indet');
  bar.firstElementChild.style.width = '0%';
  statusEl.textContent = 'Saving your work…';
  openModal('update-back');

  clearTimeout(autosaveTimer);
  await saveSessionNow();
  const fail = msg => {
    appUpdating = false;
    statusEl.textContent = msg;
    bar.style.display = 'none';
    document.getElementById('update-close').style.display = '';
  };
  try {
    const j = await (await fetch('/api/update/install', { method: 'POST' })).json();
    if (!j.ok) return fail(j.error || 'Could not start the update.');
  } catch (err) {
    return fail(`Could not start the update: ${err.message}`);
  }

  const oldVersion = updateInfo.current;
  const started = Date.now();
  const poll = async () => {
    if (Date.now() - started > 5 * 60000) return fail('This is taking unusually long. Start the editor again from the Start menu, then reload this page.');
    try {
      const s = await (await fetch('/api/update/status', { cache: 'no-store' })).json();
      if (s.version !== oldVersion) {
        statusEl.textContent = `Updated to ${s.version} - reloading…`;
        setTimeout(() => location.reload(), 800);
        return;
      }
      if (s.state === 'error') return fail(s.error || 'The update failed.');
      if (s.state === 'downloading') {
        statusEl.textContent = `Downloading… ${Math.round(s.progress * 100)}%`;
        bar.firstElementChild.style.width = `${Math.round(s.progress * 100)}%`;
      } else if (s.state === 'installing') {
        statusEl.textContent = 'Installing… the editor restarts in a moment.';
        bar.classList.add('indet');
      }
    } catch {
      // The app closed for the install - keep polling until the new version answers.
      statusEl.textContent = 'Installing… the editor restarts in a moment.';
      bar.classList.add('indet');
    }
    setTimeout(poll, 700);
  };
  poll();
};

// A .vrtour double-clicked in Explorer (or passed to the .exe) waits on the
// server until this page fetches it.
window.__svtOpenPending = () => openPendingProject();
async function openPendingProject() {
  try {
    const j = await (await fetch('/api/pending-project', { cache: 'no-store' })).json();
    if (!j.name) return;
    toast(`Opening ${j.name}…`);
    const r = await fetch('/api/pending-project/data', { cache: 'no-store' });
    if (!r.ok) return;
    await openProjectFile(new File([await r.blob()], j.name), j.token ? new AppFileHandle(j.token, j.name) : null);
  } catch (err) {
    console.warn('openPendingProject failed', err);
  }
}

// ── Box select (Shift + drag on the map) ──────────
// Leaflet's own Shift+drag zooms to a box; here it selects nodes instead.
map.boxZoom.disable();
let boxSel = null;
// Pointer events throughout: cancelling pointerdown also suppresses the mouse events.
map.getContainer().addEventListener('pointerdown', e => {
  if (!e.shiftKey || e.button !== 0 || e.target.closest('.leaflet-marker-icon, .leaflet-control')) return;
  e.preventDefault();
  e.stopPropagation();
  map.dragging.disable();
  const start = map.mouseEventToLatLng(e);
  boxSel = { start, rect: L.rectangle([start, start], { color: '#c4b5fd', weight: 1.5, dashArray: '4 4', fillOpacity: 0.12, interactive: false }).addTo(map) };
}, true);
document.addEventListener('pointermove', e => {
  if (boxSel) boxSel.rect.setBounds(L.latLngBounds(boxSel.start, map.mouseEventToLatLng(e)));
});
document.addEventListener('pointerup', () => {
  if (!boxSel) return;
  const bounds = boxSel.rect.getBounds();
  map.removeLayer(boxSel.rect);
  boxSel = null;
  map.dragging.enable();
  const hit = S.nodes.filter(n => bounds.contains([n.lat, n.lng]));
  if (!hit.length) return;
  if (S.connFrom) cancelConnect();
  hideQuickMenu();
  hit.forEach(n => S.multiSel.add(n.id));
  refreshAllMarkers();
  updateMultiBar();
  renderNodeList();
  toast(t('{n} node(s) selected', { n: S.multiSel.size }));
});

// ── Thumbnails (node list, marker hover) ──────────
const thumbCache = new WeakMap(); // photoFile -> object URL ('' = failed, PENDING = queued)
const PENDING = Symbol('pending');
const thumbQueue = [];
let thumbBusy = false;

function requestThumb(n) {
  const f = n?.photoFile;
  if (!f) return null;
  const v = thumbCache.get(f);
  if (typeof v === 'string') return v || null;
  if (v !== PENDING) {
    thumbCache.set(f, PENDING);
    thumbQueue.push(f);
    pumpThumbs();
  }
  return null;
}

// Decode one photo at a time: 8K panoramas are big, and a whole tour dropped
// at once would otherwise decode dozens in parallel.
async function pumpThumbs() {
  if (thumbBusy) return;
  thumbBusy = true;
  while (thumbQueue.length) {
    const f = thumbQueue.shift();
    try {
      const bmp = await createImageBitmap(f, { resizeWidth: 192, resizeHeight: 96, resizeQuality: 'medium' });
      const c = new OffscreenCanvas(192, 96);
      c.getContext('2d').drawImage(bmp, 0, 0);
      bmp.close();
      const url = URL.createObjectURL(await c.convertToBlob({ type: 'image/jpeg', quality: 0.75 }));
      thumbCache.set(f, url);
      S.nodes.filter(n => n.photoFile === f).forEach(n => {
        document.querySelectorAll(`img[data-thumb="${n.id}"]`).forEach(img => { img.src = url; img.hidden = false; });
        if (n.marker?.isTooltipOpen()) n.marker.setTooltipContent(markerTipHtml(n));
      });
    } catch {
      thumbCache.set(f, '');
    }
  }
  thumbBusy = false;
}

function thumbImg(n) {
  const src = requestThumb(n);
  if (!n.photoFile) return '';
  return `<img class="nrow-thumb" data-thumb="${n.id}" alt=""${src ? ` src="${src}"` : ' hidden'}>`;
}

function markerTipHtml(n) {
  const src = requestThumb(n);
  const status = gsvLabel(n);
  return `${src ? `<img src="${src}" alt="">` : ''}<div>${escapeHtml(n.name)}</div>${status ? `<div class="sub">${escapeHtml(status)}</div>` : ''}`;
}

function bindMarkerTip(n) {
  n.marker?.bindTooltip(() => markerTipHtml(n), { direction: 'top', offset: [0, -20], className: 'thumb-tip', opacity: 1 });
}

// ── Google status (published / processing / rejected) ─
// What happened on Google (and Panoramax): undo and redo leave it as it is.
const GSV_FIELDS = ['gsvPhotoId', 'gsvStatus', 'gsvShareLink', 'gsvSig', 'gsvConfirmed', 'gsvUploadedAt', 'gsvObsolete', 'pmx'];

// The photo of a node was replaced (new file, blurred): it has to be uploaded
// again, and the version already on Google becomes obsolete - remember it so the
// next upload can delete it (otherwise e.g. the unblurred photo stays public).
function retireGooglePhoto(n) {
  const obsolete = [...(n.gsvObsolete || [])];
  if (n.gsvPhotoId) obsolete.push(n.gsvPhotoId);
  Object.assign(n, { gsvPhotoId: null, gsvStatus: null, gsvShareLink: null, gsvSig: null, gsvConfirmed: false, gsvUploadedAt: null, gsvObsolete: obsolete, gsvRemote: null, pmx: null });
}

const GSV_LABELS = {
  pending: 'Uploaded - Google is still processing it',
  published: 'Published on Google Maps',
  processing: 'On Google, not yet shown on Maps',
  rejected: 'Rejected by Google',
  missing: 'Not found on Google (deleted?) - will be uploaded again',
};

// What Google should know about a node: when this changes after an upload, it
// is pushed to Google. Links only count once the target photo exists on Google.
function gsvSignature(n) {
  const links = getLinkedNodes(n.id).filter(o => o.gsvConfirmed && o.gsvPhotoId).map(o => o.gsvPhotoId).sort();
  return JSON.stringify([n.lat.toFixed(7), n.lng.toFixed(7), n.heading == null ? null : n.heading.toFixed(2), links, ...(n.placeId ? [n.placeId] : []), ...(n.level ? [`level ${n.level.number} ${n.level.name || ''}`] : [])]);
}

function gsvChanged(n) {
  return !!n.gsvPhotoId && !!n.gsvConfirmed && n.gsvSig !== gsvSignature(n);
}

function gsvLabel(n) {
  if (!n.gsvPhotoId) return '';
  const base = t(GSV_LABELS[n.gsvStatus] || 'Uploaded to Google');
  return gsvChanged(n) ? `${base} · ${t('changes not on Google yet')}` : base;
}

// After news from Google: the markers, the lists and the photo's status line - not the whole
// side panel, which would undo what the user is typing or how the panorama is turned.
function refreshGoogleUi() {
  refreshAllMarkers();
  updateStats();
  const n = gn(S.selId);
  if (n) renderPhotoLine(n);
}

function renderPhotoLine(n) {
  phN.innerHTML = [escapeHtml(n.photoName || ''), n.blurred && t('blurred')].filter(Boolean).join(' · ') +
    (n.gsvPhotoId ? `<div style="margin-top:4px">${gsvStatusHtml(n)}</div>` : '');
}

function gsvStatusHtml(n) {
  if (!n.gsvPhotoId) return '';
  const link = n.gsvShareLink ? ` <a href="${escapeHtml(n.gsvShareLink)}" target="_blank" rel="noopener">${t('open')}</a>` : '';
  const views = n.gsvViews != null ? ` · ${t('{n} views', { n: n.gsvViews.toLocaleString(currentLang) })}` : '';
  return `<span class="gstat"><span class="dot ${n.gsvStatus || ''}"></span>${escapeHtml(gsvLabel(n) + views)}${link}</span>`;
}

// A freshly uploaded photo is invisible to Google's API for a while; only call
// it missing if Google never confirmed it and that window is long over.
const GSV_PROCESSING_GRACE = 3 * 24 * 3600 * 1000;

async function refreshGoogleStatus({ quiet = false } = {}) {
  const nodes = S.nodes.filter(n => n.gsvPhotoId);
  if (!nodes.length) { if (!quiet) toast(t('No photos uploaded yet')); return null; }
  try {
    const r = await fetch('/api/photo-status', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ids: nodes.map(n => n.gsvPhotoId) }),
    });
    const j = await r.json();
    if (r.status === 401) { if (!quiet) toast(t('Sign in with Google first (Upload to Google)')); return null; }
    if (!j.ok) throw new Error(j.error || 'failed');
    const counts = {};
    let changed = false;
    const facts = n => `${n.gsvPhotoId}|${n.gsvStatus}|${n.gsvConfirmed}|${n.gsvShareLink}|${n.gsvViews}`;
    nodes.forEach(n => {
      const s = j.photos[n.gsvPhotoId];
      if (!s) return;
      const before = facts(n);
      if (s.status === 'notfound') {
        const young = Date.now() - (n.gsvUploadedAt || 0) < GSV_PROCESSING_GRACE;
        n.gsvStatus = !n.gsvConfirmed && young ? 'pending' : 'missing';
      } else {
        n.gsvStatus = s.status;
        n.gsvConfirmed = true;
        n.gsvShareLink = s.shareLink || null;
        n.gsvRemote = { lat: s.lat, lng: s.lng, heading: s.heading, connections: s.connections || [] };
        if (s.viewCount != null) n.gsvViews = s.viewCount;
        // Keep the form of the ID Google actually answers to.
        if (s.resolvedId && s.resolvedId !== n.gsvPhotoId) n.gsvPhotoId = s.resolvedId;
      }
      if (facts(n) !== before) changed = true;
      counts[n.gsvStatus] = (counts[n.gsvStatus] || 0) + 1;
    });
    refreshGoogleUi();
    if (changed) scheduleAutosave({ edit: false });
    if (S.nodes.some(n => n.gsvPhotoId && (!n.gsvConfirmed || gsvChanged(n)) && n.gsvStatus !== 'missing')) startGoogleBackgroundSync();
    const summary = Object.entries(counts).map(([k, v]) => `${v} ${t(k)}`).join(' · ');
    if (!quiet) toast(t('Google status: {summary}', { summary }));
    return counts;
  } catch (err) {
    if (!quiet) toast(t('Could not get the status from Google: {msg}', { msg: err.message }));
    return null;
  }
}

// ── Compare with Google ───────────────────────────
// Reads what Google has stored for every photo of the tour (links, position,
// heading) and compares it with the editor; also finds photos on the account
// at the same spot that the editor doesn't know about (earlier uploads whose
// ID got lost, or duplicates). "Repair" links those and pushes whatever differs.
const GCMP_NEAR = 1; // metres: a Google photo this close to a node is "the same spot"

function findNearbyGooglePhotos(nodes, photos, used) {
  const out = new Map();
  nodes.forEach(n => {
    const near = photos
      .filter(p => p.id && p.lat != null && !used.has(p.id))
      .map(p => ({ p, d: distanceMeters(n, { lat: p.lat, lng: p.lng }) }))
      .filter(x => x.d < GCMP_NEAR)
      .sort((a, b) => a.d - b.d)
      .map(x => x.p);
    if (near.length) out.set(n, near);
  });
  return out;
}

let gcmpPlan = null;

async function openGoogleCompare() {
  openModal('gcmp-back');
  const list = document.getElementById('gcmp-list');
  const summary = document.getElementById('gcmp-summary');
  document.getElementById('gcmp-repair').disabled = true;
  document.getElementById('gcmp-dupes-row').hidden = true;
  list.innerHTML = `<div class="check-ok" style="color:var(--text-dim)">${t('Asking Google…')}</div>`;
  summary.textContent = '';

  const counts = await refreshGoogleStatus({ quiet: true });
  let photos = [];
  try {
    const r = await fetch('/api/list-photos', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
    const j = await r.json();
    if (r.status === 401) { list.innerHTML = `<div class="check-ok bad">${t('Sign in with Google first (Upload to Google)')}</div>`; return; }
    if (!j.ok) throw new Error(j.error || 'failed');
    photos = j.photos;
  } catch (err) {
    list.innerHTML = `<div class="check-ok bad">${escapeHtml(t('Could not get the status from Google: {msg}', { msg: err.message }))}</div>`;
    return;
  }
  if (counts === null && S.nodes.some(n => n.gsvPhotoId)) {
    list.innerHTML = `<div class="check-ok bad">${t('Could not get the status from Google: {msg}', { msg: '?' })}</div>`;
    return;
  }

  const nodes = S.nodes.filter(n => n.photoFile || n.gsvPhotoId);
  const used = new Set(S.nodes.filter(n => n.gsvPhotoId && n.gsvConfirmed).map(n => n.gsvPhotoId));
  const nearby = findNearbyGooglePhotos(nodes, photos, used);
  const plan = { adopt: new Map(), push: new Set(), dupes: [] };
  const rows = [];

  nodes.forEach(n => {
    const issues = [];
    const notes = [];
    let near = [...(nearby.get(n) || [])];
    if (n.gsvConfirmed && n.gsvRemote) {
      const r = n.gsvRemote;
      const expected = new Set(getLinkedNodes(n.id).filter(o => o.gsvConfirmed && o.gsvPhotoId).map(o => o.gsvPhotoId));
      const remote = new Set(r.connections || []);
      const known = new Set(S.nodes.map(x => x.gsvPhotoId).filter(Boolean));
      const missing = [...expected].filter(id => !remote.has(id)).length;
      const extra = [...remote].filter(id => !expected.has(id) && known.has(id)).length;
      const outside = [...remote].filter(id => !known.has(id)).length;
      if (outside) notes.push(t('{n} link(s) to photos outside this project - they stay', { n: outside }));
      const moved = r.lat != null && distanceMeters(n, { lat: r.lat, lng: r.lng }) > 1;
      const turned = n.heading != null && (r.heading == null || headingDiff(n.heading, r.heading) > 2);
      if (missing) issues.push(t('{n} link(s) missing on Google', { n: missing }));
      if (extra) issues.push(t("{n} link(s) on Google that the editor doesn't have", { n: extra }));
      if (moved) issues.push(t('position differs by {d}', { d: formatDistance(distanceMeters(n, { lat: r.lat, lng: r.lng })) }));
      if (turned) issues.push(r.heading == null ? t('no heading on Google') : t('heading on Google {g}° instead of {e}°', { g: Math.round(r.heading), e: Math.round(n.heading) }));
      if (missing || extra || moved || turned) plan.push.add(n);
      const notYet = getLinkedNodes(n.id).filter(o => !o.gsvConfirmed).length;
      if (notYet) issues.push(t('{n} linked photo(s) not on Google yet', { n: notYet }));
    } else if (n.gsvConfirmed) {
      issues.push(t('Google status unknown'));
      near = [];
    } else if (n.gsvPhotoId && n.gsvStatus === 'pending') {
      // Just uploaded: only a photo uploaded at the same time can be this one
      // (listed under another form of its ID). Never touch older photos here.
      const mine = near.find(p => Date.parse(p.uploadTime || '') >= (n.gsvUploadedAt || 0) - 5 * 60000);
      if (mine) {
        plan.adopt.set(n, mine);
        used.add(mine.id);
        issues.push(t('found on Google, but the editor had lost track of it - will be linked'));
      } else {
        issues.push(t('Uploaded - Google is still processing it'));
      }
      near = [];
    } else if (near.length) {
      // Google has a photo here the editor can't match by ID: an upload whose ID
      // got lost, or one still being processed under another form of its ID.
      // Take the most recently uploaded one.
      near.sort((a, b) => String(b.uploadTime || '').localeCompare(String(a.uploadTime || '')));
      const pick = near.shift();
      plan.adopt.set(n, pick);
      used.add(pick.id);
      issues.push(t('found on Google, but the editor had lost track of it - will be linked'));
    } else if (n.photoFile) {
      issues.push(n.gsvStatus === 'missing' ? t('Not found on Google (deleted?) - will be uploaded again') : t('not uploaded yet'));
    }
    if (near.length) {
      plan.dupes.push(...near.map(p => p.id));
      near.forEach(p => used.add(p.id));
      issues.push(t('{n} more photo(s) on Google at this spot (duplicates)', { n: near.length }));
    }
    rows.push({ n, issues, notes, ok: !issues.length });
  });
  gcmpPlan = plan;

  const okCount = rows.filter(r => r.ok).length;
  const fixable = plan.adopt.size + plan.push.size;
  summary.textContent = t('{ok} of {all} photo(s) match Google exactly.', { ok: okCount, all: rows.length }) +
    (fixable ? ` ${t('{n} can be repaired.', { n: fixable })}` : '');
  list.innerHTML = '';
  rows.sort((a, b) => a.ok - b.ok).forEach(({ n, issues, notes, ok }) => {
    const row = document.createElement('div');
    row.className = `check-row ${ok ? 'ok' : 'warn'}`;
    const msg = [ok ? t('matches Google (links, position, heading)') : issues.join(' · '), ...notes].join(' · ');
    row.innerHTML = `<span class="lv"></span><span class="nm">${escapeHtml(n.name)}</span><span class="msg">${escapeHtml(msg)}</span><button class="btn">${t('Show')}</button>`;
    row.querySelector('button').onclick = () => { closeModal('gcmp-back'); jumpToNode(n.id, false); };
    list.appendChild(row);
  });
  document.getElementById('gcmp-repair').disabled = !fixable && !plan.dupes.length;
  document.getElementById('gcmp-dupes-row').hidden = !plan.dupes.length;
  document.getElementById('gcmp-dupes').checked = false;
  document.getElementById('gcmp-dupes-label').textContent = t('Also delete the {n} duplicate photo(s) from Google', { n: plan.dupes.length });
}

document.getElementById('gcmp-repair').onclick = async () => {
  const plan = gcmpPlan;
  if (!plan) return;
  const btn = document.getElementById('gcmp-repair');
  btn.disabled = true;
  try {
    plan.adopt.forEach((p, n) => {
      Object.assign(n, { gsvPhotoId: p.id, gsvConfirmed: true, gsvStatus: p.status, gsvShareLink: p.shareLink || null, gsvSig: null });
    });
    // Linking a lost photo changes what its neighbours should link to as well.
    plan.adopt.forEach((p, n) => getLinkedNodes(n.id).forEach(o => { if (o.gsvConfirmed) o.gsvSig = null; }));
    plan.push.forEach(n => { n.gsvSig = null; });
    const res = await pushGoogleChanges();
    let deleted = 0;
    if (plan.dupes.length && document.getElementById('gcmp-dupes').checked
        && confirm(t('Delete {n} duplicate photo(s) from Google? This cannot be undone.', { n: plan.dupes.length }))) {
      const r = await fetch('/api/delete-photos', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ids: plan.dupes }) });
      const j = await r.json();
      deleted = (j.results || []).filter(x => x.ok).length;
    }
    tourChanged();
    toast(t('Repaired: {a} photo(s) updated on Google, {b} linked again, {c} duplicate(s) deleted', { a: res.ok, b: plan.adopt.size, c: deleted }));
    if (res.failed) toast(t('{n} photo(s) could not be updated yet - Google may still be processing them; this is retried automatically.', { n: res.failed }));
    startGoogleBackgroundSync();
  } catch (err) {
    toast(t('Updating on Google failed: {msg}', { msg: err.message }));
  }
  await openGoogleCompare();
};
document.getElementById('gcmp-rerun').onclick = () => openGoogleCompare();

// ── Headings from walking direction ───────────────
function openWalkHeadings() {
  const list = scopeNodes();
  if (list.length < 2) { alert(t('Add at least two nodes first.')); return; }
  document.getElementById('walkh-scope').textContent = scopeLabel(list);
  const sel = document.getElementById('walkh-ref');
  const withHeading = list.filter(n => n.heading != null && n.headingSource !== 'auto');
  sel.innerHTML = `<option value="">${escapeHtml(t('None - the camera front faced the walking direction'))}</option>` +
    withHeading.map(n => `<option value="${n.id}">${escapeHtml(n.name)} (${n.heading.toFixed(1)}°)</option>`).join('');
  const cur = withHeading.find(n => n.id === S.selId);
  sel.value = cur ? cur.id : (withHeading[0]?.id || '');
  openModal('walkh-back');
}

function walkBearing(order, i) {
  const cur = order[i], prev = order[i - 1], next = order[i + 1];
  const far = (a, b) => a && b && distanceMeters(a, b) > 0.3;
  if (far(prev, next)) return bearingDeg(prev, next);
  if (far(cur, next)) return bearingDeg(cur, next);
  if (far(prev, cur)) return bearingDeg(prev, cur);
  return null;
}

document.getElementById('walkh-go').onclick = async () => {
  const list = scopeNodes();
  const byTime = document.querySelector('input[name="walkh-order"]:checked').value === 'time';
  const overwrite = document.getElementById('walkh-overwrite').checked;
  const ref = gn(document.getElementById('walkh-ref').value);
  let order = list;
  if (byTime) {
    for (const n of list) await ensureCaptureTime(n);
    order = list.map((n, i) => ({ n, i, t: n.captureTime ? Date.parse(`${n.captureTime}Z`) : Infinity }))
      .sort((a, b) => a.t - b.t || a.i - b.i).map(o => o.n);
  }
  let offset = 0;
  if (ref) {
    const wb = walkBearing(order, order.indexOf(ref));
    if (wb == null) { alert(t('The reference photo has no neighbour to walk from or to.')); return; }
    offset = ref.heading - wb;
  }
  pushUndo();
  let changed = 0;
  order.forEach((n, i) => {
    if (n === ref) return;
    if (!overwrite && n.heading != null && n.headingSource !== 'auto') return;
    const wb = walkBearing(order, i);
    if (wb == null) return;
    n.heading = normalizeHeading(wb + offset);
    n.headingSource = 'auto';
    n.viewYaw = normalizeRad(-degToRad(n.heading));
    changed++;
  });
  refreshAllMarkers();
  panoNodeId = null;
  updateSb();
  scheduleAutosave();
  closeModal('walkh-back');
  toast(t('Set the heading of {n} photo(s)', { n: changed }));
};

// ── Export: one tour or all ───────────────────────
// With several tours on the map (or points selected), each export asks which
// tour it is for; with a single tour it goes straight ahead as before.
const EXPORTERS = {
  website: scope => exportTourWebsite(scope),
  viewer: scope => exportViewerFiles(scope),
  csv: scope => exportCsv(scope),
  project: scope => exportTourProject(scope),
  kml: scope => exportKml(scope),
  gpx: scope => exportGpx(scope),
  photos: scope => exportPhotos(scope),
};

// What a tour action can be for: each tour, everything, the selected points.
function tourScopes() {
  const scopes = getTours().map(g => ({ key: `t${g.index}`, label: tourName(g), name: tourName(g), nodes: g.nodes }));
  scopes.push({ key: 'all', label: t('Everything on the map'), name: null, nodes: S.nodes.slice() });
  const selected = S.nodes.filter(n => S.multiSel.has(n.id));
  if (selected.length) scopes.push({ key: 'sel', label: t('The selected points'), name: t('Selection'), nodes: selected });
  return scopes;
}
const defaultScope = () => (S.multiSel.size >= 2 ? 'sel' : tourOf(S.selId)?.index != null ? `t${tourOf(S.selId).index}` : null);
let expScopes = [];

function runExport(format, tourKey = null) {
  if (!S.nodes.length) { alert('No nodes found.'); return; }
  if (!tourKey && !manyTours() && S.multiSel.size < 2) { EXPORTERS[format](null); return; }
  closeMenus();
  expScopes = tourScopes();
  const select = document.getElementById('exp-tour');
  select.innerHTML = expScopes.map(sc => `<option value="${sc.key}">${escapeHtml(sc.label)} · ${escapeHtml(t('{n} point(s)', { n: sc.nodes.length }))}</option>`).join('');
  select.value = tourKey || defaultScope() || expScopes[0].key;
  document.querySelector(`input[name="exp-format"][value="${format}"]`).checked = true;
  openModal('exp-back');
  showExportScope();
}

function showExportScope() {
  const sc = expScopes.find(x => x.key === document.getElementById('exp-tour').value);
  if (sc?.nodes.length) map.fitBounds(L.latLngBounds(sc.nodes.map(n => [n.lat, n.lng])), { padding: [60, 60], maxZoom: 19 });
}
document.getElementById('exp-tour').addEventListener('change', showExportScope);
document.getElementById('exp-go').onclick = () => {
  const sc = expScopes.find(x => x.key === document.getElementById('exp-tour').value);
  const format = document.querySelector('input[name="exp-format"]:checked').value;
  closeModal('exp-back');
  if (sc) EXPORTERS[format](sc.key === 'all' ? null : { name: sc.name, nodes: sc.nodes });
};

// A tour (or the whole map) as a project file of its own. The open project
// stays the open project; Ctrl+S still saves everything there.
async function exportTourProject(scope = null) {
  const nodes = scope?.nodes || S.nodes;
  if (!nodes.length) { alert('No nodes found.'); return; }
  const name = `${fileBase(scope?.name || (projectName || PROJECT_FILE).replace(/\.vrtour$/i, ''))}.vrtour`;
  let handle = null;
  if (window.showSaveFilePicker) {
    try {
      handle = await window.showSaveFilePicker({ suggestedName: name, types: PROJECT_PICKER_TYPES });
    } catch (err) {
      if (err?.name === 'AbortError') return;
      handle = null;
    }
  }
  try {
    setSaveStatus('saving', 'Packing project…');
    if (handle) await detachPhotosFrom(handle);
    const zip = await packProject(nodes);
    if (handle) {
      const writable = await handle.createWritable();
      await writable.write(zip);
      await writable.close();
      toast(`Saved ${handle.name}`);
      if (await projectHandle?.isSameEntry?.(handle).catch(() => false)) markDirty(); // the project file holds less than the map now
    } else {
      await saveTextFile(name, zip);
    }
    setSaveStatus('saved', 'Saved');
  } catch (err) {
    console.error(err);
    setSaveStatus(null, 'Export failed');
    alert(`Export failed: ${err.message || err}`);
  }
}

// A question with more answers than confirm()'s OK and Cancel.
let choiceDone = null;
function choose(title, text, buttons) {
  document.getElementById('choice-title').textContent = title;
  document.getElementById('choice-text').textContent = text;
  const box = document.getElementById('choice-buttons');
  box.innerHTML = '';
  return new Promise(resolve => {
    choiceDone = value => { choiceDone = null; closeModal('choice-back'); resolve(value); };
    buttons.forEach(b => {
      const el = document.createElement('button');
      el.className = `btn${b.primary ? ' g' : ''}`;
      el.textContent = b.label;
      el.onclick = () => choiceDone?.(b.value);
      box.appendChild(el);
    });
    openModal('choice-back');
    box.querySelector('.btn.g')?.focus();
  });
}
// Closed with Esc: no answer.
new MutationObserver(() => {
  if (!document.getElementById('choice-back').classList.contains('show')) choiceDone?.(null);
}).observe(document.getElementById('choice-back'), { attributes: true, attributeFilter: ['class'] });

// ── Several projects on one map ───────────────────
// Adding a project keeps what is on the map: the project's points, links,
// starts and info points come in under new ids, its tours stay tours of their
// own. The map is then more than any one file, so the next Ctrl+S asks for a
// new one and the opened files stay as they are.
async function openOrAddProjects(items) {
  if (!items.length) return;
  if (!S.nodes.length) { await addProjectsToMap(items); return; }
  const one = items.length === 1;
  const choice = await choose(
    one ? items[0].file.name : t('{n} projects', { n: items.length }),
    t('Add to the tours on the map, or open instead of them?'),
    [{ value: 'add', label: t('Add to the map'), primary: true }, { value: 'open', label: t('Open instead') }, { value: null, label: t('Cancel') }],
  );
  if (choice === 'add') await addProjectsToMap(items);
  else if (choice === 'open') {
    await openProjectFile(items[0].file, items[0].handle, { ask: false });
    if (!one) await addProjectsToMap(items.slice(1));
  }
}

const addInput = document.getElementById('badd-file');
async function pickAndAddProjects() {
  closeMenus();
  if (!window.showOpenFilePicker) { addInput.click(); return; }
  let handles;
  try {
    handles = await window.showOpenFilePicker({ multiple: true, types: PROJECT_OPEN_TYPES });
  } catch (err) {
    if (err?.name !== 'AbortError') addInput.click();
    return;
  }
  const items = [];
  for (const handle of handles) items.push({ file: await handle.getFile(), handle });
  await addProjectsToMap(items);
}
document.getElementById('badd').onclick = pickAndAddProjects;
addInput.addEventListener('change', async () => {
  const files = [...addInput.files];
  addInput.value = '';
  await addProjectsToMap(files.map(file => ({ file, handle: null })));
});

async function addProjectsToMap(items) {
  if (!S.nodes.length && items.length === 1) { await openProjectFile(items[0].file, items[0].handle, { ask: false }); return; }
  const read = [];
  for (const it of items) {
    try {
      read.push({ ...it, snap: await readProjectFile(it.file, it.handle) });
    } catch (err) {
      console.error(err);
      alert(`Could not open ${it.file.name}: ${err.message || err}`);
    }
  }
  // The same project twice is most likely a mistake.
  const onGoogle = new Set(S.nodes.map(n => n.gsvPhotoId).filter(Boolean));
  const spot = n => `${n.photoName}|${Number(n.lat).toFixed(6)}|${Number(n.lng).toFixed(6)}`;
  const spots = new Set(S.nodes.map(spot));
  const accepted = read.filter(({ file, snap }) => {
    const same = snap.nodes.filter(nd => (nd.gsvPhotoId && onGoogle.has(nd.gsvPhotoId)) || spots.has(spot(nd))).length;
    return !same || confirm(t('{name}: {n} of its points are already on the map. Add it anyway?', { name: file.name, n: same }));
  });
  if (!accepted.length) return;

  const wasEmpty = !S.nodes.length;
  pushUndo();
  const added = [];
  const before = S.conns.length;
  for (const { snap } of accepted) {
    const ids = new Map(snap.nodes.map(nd => [nd.id, uid()]));
    const tours = new Map();
    const newTour = old => {
      if (!old) return null;
      if (!tours.has(old)) {
        const id = makeTourId();
        tours.set(old, id);
        if (snap.tourNames[old]) S.tourNames[id] = snap.tourNames[old];
      }
      return tours.get(old);
    };
    snap.nodes.forEach(nd => {
      const n = {
        ...nd,
        id: ids.get(nd.id),
        tourId: newTour(nd.tourId),
        hotspots: nd.hotspots.map(h => ({ ...h, id: `h${S.nxt++}` })),
        photoUrl: nd.photoFile ? URL.createObjectURL(nd.photoFile) : null,
        marker: null,
      };
      // One Google photo can't belong to two points: the second counts as new.
      if (n.gsvPhotoId && onGoogle.has(n.gsvPhotoId)) {
        Object.assign(n, { gsvPhotoId: null, gsvStatus: null, gsvShareLink: null, gsvSig: null, gsvConfirmed: false, gsvUploadedAt: null, gsvObsolete: [] });
      }
      if (n.gsvPhotoId) onGoogle.add(n.gsvPhotoId);
      S.nodes.push(n);
      added.push(n);
    });
    snap.conns.forEach(c => {
      if (ids.has(c.from) && ids.has(c.to)) S.conns.push({ id: cuid(), from: ids.get(c.from), to: ids.get(c.to), poly: null, xMark: null });
    });
    snap.starts.forEach(id => { if (ids.has(id)) S.starts.add(ids.get(id)); });
  }
  toursVersion++;
  added.forEach(n => createMarker(n, { animate: false }));
  S.conns.slice(before).forEach(c => drawConn(c, { animate: false }));
  updateSb();
  updateStats();
  fitAll();
  scheduleAutosave();

  const base = name => name.replace(/\.(vrtour|zip|json)$/i, '');
  const names = accepted.map(r => base(r.file.name));
  projectHandle = null;
  projectName = `${[...(wasEmpty ? [] : [base(projectName || t('Untitled'))]), ...names].join(' + ')}.vrtour`;
  updateTitle();
  accepted.forEach(({ handle }) => { if (handle) rememberRecent(handle); });
  toast(t('Added {names} to the map ({n} tours now). Saving asks for a new file - the opened files stay as they are.',
    { names: names.join(', '), n: getTours().length }));
  if (added.some(n => n.gsvPhotoId && n.gsvStatus !== 'missing' && (!n.gsvConfirmed || n.gsvSig !== gsvSignature(n)))) {
    startGoogleBackgroundSync();
  }
}

// ── Import assistant ──────────────────────────────
// Photos or whole folders in one go: placed by GPS (or a GPX track), linked in
// the order they were taken, turned to north from the walking direction, then
// the tour check. Project → Import photos / Ctrl+I, and dropping several photos
// or a folder on the map.
const IMPORT_TYPES = [{ description: 'Photos', accept: { 'image/*': ['.jpg', '.jpeg', '.png', '.webp'] } }];
const imp = { files: [], metas: new Map(), skipped: 0, point: null, busy: false, gen: 0 };
const impBack = document.getElementById('imp-back');
const impDrop = document.getElementById('imp-drop');

function isPhotoFile(f) {
  return !!f && !f.name.startsWith('.') && (f.type.startsWith('image/') || /\.(jpe?g|png|webp)$/i.test(f.name));
}
const photoKey = f => `${f.name}|${f.size}|${f.lastModified}`;
const byName = (a, b) => a.name.localeCompare(b.name, undefined, { numeric: true });

// Dropped files and folders. webkitGetAsEntry() works only while the drop event
// is dispatched, so the entries are taken right away and read afterwards.
function dropEntries(dt) {
  return [...(dt?.items || [])].filter(it => it.kind === 'file').map(it => it.webkitGetAsEntry?.()).filter(Boolean);
}

async function filesFromEntries(entries, depth = 0) {
  const out = [];
  for (const entry of entries) {
    if (entry.name.startsWith('.')) continue;
    if (entry.isFile) {
      const file = await new Promise(resolve => entry.file(resolve, () => resolve(null)));
      if (isPhotoFile(file)) out.push(file);
    } else if (entry.isDirectory && depth < 4) {
      const reader = entry.createReader();
      const children = [];
      for (;;) { // handed out in batches
        const batch = await new Promise(resolve => reader.readEntries(resolve, () => resolve([])));
        if (!batch.length) break;
        children.push(...batch);
      }
      out.push(...await filesFromEntries(children.sort(byName), depth + 1));
    }
  }
  return out;
}

async function filesFromDirHandle(dir, depth = 0) {
  const handles = [];
  for await (const h of dir.values()) handles.push(h);
  const out = [];
  for (const h of handles.sort(byName)) {
    if (h.name.startsWith('.')) continue;
    if (h.kind === 'file') {
      const file = await h.getFile();
      if (isPhotoFile(file)) out.push(file);
    } else if (depth < 3) {
      out.push(...await filesFromDirHandle(h, depth + 1));
    }
  }
  return out;
}

// Files from the app window's dialogs come through the app, one by one.
async function readAppFiles(handles) {
  const out = [];
  for (let i = 0; i < handles.length; i++) {
    if (i % 5 === 0) setImpStatus(t('Reading photos… {i} of {n}', { i: i + 1, n: handles.length }));
    const f = await handles[i].getFile();
    const type = /\.png$/i.test(f.name) ? 'image/png' : /\.webp$/i.test(f.name) ? 'image/webp' : 'image/jpeg';
    out.push(new File([f], f.name, { type }));
  }
  const refs = await fileRefs(handles); // the autosave refers to these files instead of copying them
  if (refs) out.forEach((f, i) => photoRefs.set(f, { ref: refs[i], entry: null, type: f.type }));
  return out;
}

function setImpStatus(msg) { document.getElementById('imp-status').textContent = msg; }

function openImport({ entries = null, files = null, point = null } = {}) {
  closeMenus();
  imp.gen++;
  Object.assign(imp, { files: [], metas: new Map(), skipped: 0, point, busy: false, source: null });
  let o = {};
  try { o = JSON.parse(storageGet('importOptions') || '{}'); } catch {}
  document.getElementById('imp-connect').checked = o.connect ?? true;
  document.getElementById('imp-north').checked = o.north ?? true;
  document.getElementById('imp-check').checked = o.check ?? true;
  document.getElementById('imp-dist').value = o.dist ?? 25;
  document.getElementById('imp-gpx').value = '';
  setImpStatus('');
  renderImport();
  openModal('imp-back');
  if (entries?.length) collectImport(() => filesFromEntries(entries), folderName(entries));
  else if (files?.length) addImportFiles(files);
}

// The folder a drop consists of, if it is one: the imported tour gets its name.
function folderName(entries) {
  const dirs = entries.filter(en => en.isDirectory);
  return dirs.length === 1 ? dirs[0].name : null;
}

async function collectImport(getFiles, source = null) {
  const gen = imp.gen;
  if (source) imp.source = source;
  imp.busy = true;
  renderImport();
  setImpStatus(t('Looking for photos…'));
  let files;
  try {
    files = await getFiles();
  } catch (err) {
    if (gen !== imp.gen) return;
    imp.busy = false;
    renderImport();
    setImpStatus(t('Could not read the photos: {msg}', { msg: err.message || err }));
    return;
  }
  if (gen !== imp.gen) return;
  imp.busy = false;
  await addImportFiles(files);
}

async function addImportFiles(files) {
  const gen = imp.gen;
  const known = new Set(imp.files.map(photoKey));
  const inTour = new Set(S.nodes.filter(n => n.photoFile).map(n => `${n.photoName}|${n.photoFile.size}`));
  const fresh = [];
  for (const f of files) {
    if (!isPhotoFile(f) || known.has(photoKey(f))) continue;
    known.add(photoKey(f));
    if (inTour.has(`${f.name}|${f.size}`)) imp.skipped++;
    else fresh.push(f);
  }
  if (!fresh.length) {
    renderImport();
    setImpStatus(imp.files.length ? '' : t('No photos found.'));
    return;
  }
  imp.busy = true;
  renderImport();
  for (let i = 0; i < fresh.length; i++) {
    if (gen !== imp.gen || !impBack.classList.contains('show')) { imp.gen++; return; } // closed or started over
    if (i % 5 === 0) setImpStatus(t('Reading photos… {i} of {n}', { i: i + 1, n: fresh.length }));
    imp.metas.set(fresh[i], await readPhotoMeta(fresh[i]));
    imp.files.push(fresh[i]);
  }
  imp.busy = false;
  setImpStatus('');
  renderImport();
}

function renderImport() {
  const n = imp.files.length;
  const metas = imp.files.map(f => imp.metas.get(f));
  const gps = metas.filter(m => m.hasGps).length;
  const heading = metas.filter(m => m.heading != null).length;
  const sum = document.getElementById('imp-sum');
  const parts = [`<b>${escapeHtml(t('{n} photo(s)', { n }))}</b>`];
  if (n) parts.push(escapeHtml(t('{n} with GPS', { n: gps })), escapeHtml(t('{n} with capture time', { n: metas.filter(m => m.captureTime).length })));
  if (heading) parts.push(escapeHtml(t('{n} with compass heading', { n: heading })));
  if (imp.skipped) parts.push(escapeHtml(t('{n} already in the tour - skipped', { n: imp.skipped })));
  sum.innerHTML = parts.join(' · ') + (n && !imp.busy ? ` <span class="x" id="imp-clear">${escapeHtml(t('Clear'))}</span>` : '');
  sum.hidden = !n && !imp.skipped;
  document.getElementById('imp-gpx-row').hidden = !n || gps === n;
  const go = document.getElementById('imp-go');
  go.disabled = !n || imp.busy;
  go.textContent = n ? t('Import {n} photo(s)', { n }) : t('Import');
  document.getElementById('imp-pick-files').disabled = document.getElementById('imp-pick-folder').disabled = imp.busy;
}

document.getElementById('imp-sum').addEventListener('click', e => {
  if (e.target.id !== 'imp-clear') return;
  imp.gen++;
  Object.assign(imp, { files: [], metas: new Map(), skipped: 0, busy: false });
  setImpStatus('');
  renderImport();
});

document.getElementById('imp-pick-files').onclick = async () => {
  if (!appHasWindow) { document.getElementById('imp-files-input').click(); return; }
  let handles;
  try {
    handles = await window.showOpenFilePicker({ multiple: true, types: IMPORT_TYPES });
  } catch (err) {
    if (err?.name !== 'AbortError') setImpStatus(err.message || String(err));
    return;
  }
  collectImport(() => readAppFiles(handles));
};

document.getElementById('imp-pick-folder').onclick = async () => {
  if (appHasWindow) {
    let j;
    try {
      j = await appPick('folder', { remember: false });
    } catch (err) {
      if (err?.name !== 'AbortError') setImpStatus(err.message || String(err));
      return;
    }
    collectImport(async () => {
      const r = await (await fetch('/api/files/photos', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ token: j.token }),
      })).json();
      if (!r.ok) throw new Error(r.error || 'The folder could not be read.');
      return readAppFiles(r.files.map(f => new AppFileHandle(f.token, f.name)));
    }, j.name);
    return;
  }
  if (!window.showDirectoryPicker) { document.getElementById('imp-folder-input').click(); return; }
  let dir;
  try {
    dir = await window.showDirectoryPicker({ mode: 'read' });
  } catch (err) {
    if (err?.name !== 'AbortError') setImpStatus(err.message || String(err));
    return;
  }
  collectImport(() => filesFromDirHandle(dir), dir.name);
};

document.getElementById('imp-files-input').addEventListener('change', e => {
  const files = [...e.target.files];
  e.target.value = '';
  addImportFiles(files);
});
document.getElementById('imp-folder-input').addEventListener('change', e => {
  const files = [...e.target.files].sort((a, b) => a.webkitRelativePath.localeCompare(b.webkitRelativePath, undefined, { numeric: true }));
  e.target.value = '';
  imp.source = files[0]?.webkitRelativePath.split('/')[0] || imp.source;
  addImportFiles(files);
});

impBack.addEventListener('dragover', e => { e.preventDefault(); impDrop.classList.add('over'); });
impBack.addEventListener('dragleave', e => { if (!impBack.contains(e.relatedTarget)) impDrop.classList.remove('over'); });
impBack.addEventListener('drop', e => {
  e.preventDefault();
  e.stopPropagation();
  impDrop.classList.remove('over');
  if (imp.busy) return;
  const entries = dropEntries(e.dataTransfer);
  const files = [...e.dataTransfer.files];
  if (entries.some(en => en.isDirectory)) collectImport(() => filesFromEntries(entries), folderName(entries));
  else addImportFiles(files);
});

// A file dropped anywhere else must not replace the editor with that file
// (file inputs still take a dropped file).
const strayFileDrag = e => e.dataTransfer?.types?.includes('Files') && !e.target.closest?.('input[type=file]');
document.addEventListener('dragover', e => {
  if (e.defaultPrevented || !strayFileDrag(e)) return;
  e.preventDefault();
  e.dataTransfer.dropEffect = 'none';
});
document.addEventListener('drop', e => { if (strayFileDrag(e)) e.preventDefault(); });

document.getElementById('imp-go').onclick = async () => {
  if (!imp.files.length || imp.busy) return;
  const opts = {
    connect: document.getElementById('imp-connect').checked,
    north: document.getElementById('imp-north').checked,
    check: document.getElementById('imp-check').checked,
    dist: Math.max(1, Number(document.getElementById('imp-dist').value) || 25),
  };
  storageSet('importOptions', JSON.stringify(opts));

  // Photos without GPS: from the GPX track, if there is one.
  let track = null, offsetMs = 0;
  const gpxFile = document.getElementById('imp-gpx-row').hidden ? null : document.getElementById('imp-gpx').files[0];
  if (gpxFile) {
    try { track = parseGpx(await gpxFile.text()); } catch (err) { setImpStatus(err.message); return; }
    const withOffset = imp.files.map(f => imp.metas.get(f)).find(m => m.captureOffset);
    offsetMs = (withOffset ? parseOffsetHours(withOffset.captureOffset) : -new Date().getTimezoneOffset() / 60) * 3600000;
  }

  // The walk: capture time, then file name (photos without a time come last).
  const time = m => { const v = m.captureTime ? Date.parse(`${m.captureTime}Z`) : NaN; return Number.isFinite(v) ? v : Infinity; };
  const walk = imp.files.map(f => ({ f, m: imp.metas.get(f) }))
    .sort((a, b) => (time(a.m) - time(b.m)) || byName(a.f, b.f));

  pushUndo();
  const base = imp.point || map.getCenter();
  const created = [];
  const tourId = makeTourId();
  if (imp.source) S.tourNames[tourId] = imp.source;
  let fromTrack = 0, unplaced = 0;
  for (const { f, m } of walk) {
    let pos = m.hasGps ? m : null;
    if (!pos && track && m.captureTime) {
      pos = positionAt(track, Date.parse(`${m.captureTime}Z`) - offsetMs);
      if (pos) fromTrack++;
    }
    const node = pos ? newPhotoNode(f, m, pos, true) : newPhotoNode(f, m, offsetEast(base, 5 * unplaced++), false);
    node.tourId = tourId;
    S.nodes.push(node);
    created.push(node);
  }

  // Photos without a position sit in a makeshift row: they say nothing about the
  // way walked, so the chain and the headings skip them (unless none has one).
  const placed = created.filter(n => n.hasGps);
  const before = S.conns.length;
  if (opts.connect) {
    const chain = placed.length ? placed : created;
    for (let i = 0; i < chain.length - 1; i++) {
      const a = chain[i], b = chain[i + 1];
      if (distanceMeters(a, b) <= opts.dist) S.conns.push({ id: cuid(), from: a.id, to: b.id, poly: null, xMark: null });
    }
  }
  let headings = 0;
  if (opts.north) {
    placed.forEach((n, i) => {
      if (n.heading != null) return;
      const wb = walkBearing(placed, i);
      if (wb == null) return;
      n.heading = normalizeHeading(wb);
      n.headingSource = 'auto';
      n.viewYaw = normalizeRad(-degToRad(n.heading));
      headings++;
    });
  }

  S.starts.add(created[0].id); // the start of the new tour
  S.selId = created[0].id;
  created.forEach(n => createMarker(n, { animate: false }));
  S.conns.slice(before).forEach(c => drawConn(c, { animate: false }));
  refreshAllMarkers();
  updateSb();
  updateStats();
  if (S.nodes.length >= 2) fitAll(); else map.setView([created[0].lat, created[0].lng], 19);
  scheduleAutosave();
  closeModal('imp-back');

  const notes = [t('Imported {n} photo(s): {l} link(s), {h} heading(s) from the walking direction.', { n: created.length, l: S.conns.length - before, h: headings })];
  if (fromTrack) notes.push(t('{n} placed from the GPX track.', { n: fromTrack }));
  if (unplaced) notes.push(t('{n} without a position, placed in a row - drag them to the right spot.', { n: unplaced }));
  toast(notes.join(' '));
  imp.gen++;
  Object.assign(imp, { files: [], metas: new Map(), skipped: 0 });
  if (opts.check) setTimeout(() => openTourCheck(), 400);
};

document.getElementById('bimport').onclick = () => openImport();
document.getElementById('es-import').onclick = () => openImport();
document.getElementById('es-open').onclick = () => pickAndOpenProject();

// ── Recent projects ───────────────────────────────
// In the app window the app keeps the list (the paths stay on its side). In
// Chrome and Edge the file handles are kept in this browser's storage and need
// the user's OK again to be read; other browsers can't reopen a file at all.
const RECENT_MAX = 8;
const CLOCK_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><polyline points="12 7 12 12 15 14"/></svg>';

async function recentDb(list) {
  const db = await openSaveDb();
  try {
    return await new Promise((resolve, reject) => {
      const tx = db.transaction('kv', list ? 'readwrite' : 'readonly');
      const req = list ? tx.objectStore('kv').put(list, 'recentProjects') : tx.objectStore('kv').get('recentProjects');
      tx.oncomplete = () => resolve(list || (Array.isArray(req.result) ? req.result : []));
      tx.onerror = () => reject(tx.error);
    });
  } finally {
    db.close();
  }
}

async function rememberRecent(handle) {
  try {
    if (handle instanceof AppFileHandle) {
      await fetch('/api/recent/add', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ token: handle.token }) });
    } else if (typeof handle?.isSameEntry === 'function') {
      const keep = [];
      for (const r of await recentDb()) {
        if (!(await r.handle.isSameEntry(handle).catch(() => false))) keep.push(r);
      }
      const id = crypto.randomUUID ? crypto.randomUUID() : `r${Date.now()}`;
      await recentDb([{ id, name: handle.name, handle }, ...keep].slice(0, RECENT_MAX));
    }
  } catch (err) {
    console.warn('recent projects', err);
  }
  renderRecent();
}

async function recentItems() {
  if (appHasWindow) {
    const j = await (await fetch('/api/recent', { cache: 'no-store' })).json();
    return (j.items || []).map(it => ({ id: it.id, name: it.name, where: it.folder }));
  }
  if (!window.showOpenFilePicker) return [];
  return (await recentDb()).map(r => ({ id: r.id, name: r.name, where: '' }));
}

async function renderRecent() {
  let items = [];
  try { items = await recentItems(); } catch {}
  document.getElementById('menu-recent').hidden = !items.length;
  document.getElementById('menu-recent-list').innerHTML = items.slice(0, 5).map(it =>
    `<button data-recent="${escapeHtml(it.id)}" title="${escapeHtml(it.where || it.name)}">${CLOCK_ICON}<span class="rn">${escapeHtml(it.name)}</span></button>`).join('');
  document.getElementById('es-recent').hidden = !items.length;
  document.getElementById('es-recent-list').innerHTML = items.map(it =>
    `<button class="recent-item" data-recent="${escapeHtml(it.id)}" title="${escapeHtml(it.where || it.name)}"><span class="rn">${escapeHtml(it.name)}</span>${it.where ? `<span class="rw">${escapeHtml(it.where)}</span>` : ''}</button>`).join('');
}

async function forgetRecent(id, name) {
  try {
    if (appHasWindow) {
      await fetch('/api/recent/remove', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id }) });
    } else {
      await recentDb((await recentDb()).filter(r => r.id !== id));
    }
  } catch {}
  toast(t('{name} is no longer there - removed from the list.', { name }));
  renderRecent();
}

async function openRecent(id, name) {
  try {
    if (appHasWindow) {
      const j = await (await fetch('/api/recent/open', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id }),
      })).json();
      if (!j.ok) { forgetRecent(id, name); return; }
      const handle = new AppFileHandle(j.token, j.name);
      await openProjectFile(await handle.getFile(), handle);
      return;
    }
    const r = (await recentDb()).find(x => x.id === id);
    if (!r) return;
    const opts = { mode: 'read' };
    if ((await r.handle.queryPermission(opts)) !== 'granted' && (await r.handle.requestPermission(opts)) !== 'granted') return;
    let file;
    try {
      file = await r.handle.getFile();
    } catch (err) {
      if (err?.name === 'NotFoundError') { forgetRecent(id, name); return; }
      throw err;
    }
    await openProjectFile(file, r.handle);
  } catch (err) {
    alert(`Could not open ${name}: ${err.message || err}`);
  }
}

['menu-recent-list', 'es-recent-list'].forEach(id => document.getElementById(id).addEventListener('click', e => {
  const b = e.target.closest('[data-recent]');
  if (b) openRecent(b.dataset.recent, b.querySelector('.rn')?.textContent || '');
}));
document.getElementById('bproject').addEventListener('click', () => renderRecent());

// The tour viewer in the editor's language.
function genViewer(startId, nodes, opts = {}) {
  return viewerPage(startId, nodes, { lang: currentLang, mapLabel: t('Map'), ...opts });
}

// ── Views on Google ───────────────────────────────
// How often the photos were viewed, as Google counts it: per tour and per photo.
async function openViews() {
  closeMenus();
  const sum = document.getElementById('views-sum');
  const list = document.getElementById('views-list');
  list.innerHTML = '';
  openModal('views-back');
  if (!S.nodes.some(n => n.gsvPhotoId)) { sum.textContent = t('No photos uploaded yet'); return; }
  sum.textContent = t('Asking Google…');
  await refreshGoogleStatus({ quiet: true });
  const fmt = v => v.toLocaleString(currentLang);
  const groups = manyTours() ? getTours().map(g => ({ name: tourName(g), nodes: g.nodes })) : [{ name: null, nodes: S.nodes }];
  const total = S.nodes.reduce((a, n) => a + (n.gsvViews || 0), 0);
  sum.textContent = t('{n} views in total.', { n: fmt(total) });
  groups.forEach(g => {
    const nodes = g.nodes.filter(n => n.gsvPhotoId).sort((a, b) => (b.gsvViews || 0) - (a.gsvViews || 0));
    if (!nodes.length) return;
    if (g.name) {
      const head = document.createElement('div');
      head.className = 'views-tour';
      head.innerHTML = `<span>${escapeHtml(g.name)}</span><span>${escapeHtml(t('{n} views', { n: fmt(nodes.reduce((a, n) => a + (n.gsvViews || 0), 0)) }))}</span>`;
      list.appendChild(head);
    }
    nodes.forEach(n => {
      const row = document.createElement('div');
      row.className = 'check-row ok';
      row.innerHTML = `<span class="lv"></span><span class="nm">${escapeHtml(n.name)}</span><span class="msg">${escapeHtml(n.gsvViews != null ? t('{n} views', { n: fmt(n.gsvViews) }) : t('not counted yet'))}</span><button class="btn">${escapeHtml(t('Show'))}</button>`;
      row.querySelector('button').onclick = () => { closeModal('views-back'); jumpToNode(n.id, false); };
      list.appendChild(row);
    });
  });
}

// ── North from the photos ─────────────────────────
// Turns the photos of a tour to match one whose north is right, by comparing each
// photo's horizon with its linked neighbours' (align.js). It works outwards along
// the links; a photo matched only vaguely is shown but not used to go on from, so
// an error doesn't spread. Nothing changes until the ticked results are applied.
let northResults = [];
const northFeatures = new WeakMap(); // photo File -> its horizon

async function featuresOf(n) {
  if (!northFeatures.has(n.photoFile)) northFeatures.set(n.photoFile, await photoFeatures(n.photoFile));
  return northFeatures.get(n.photoFile);
}

// Headings set by hand or read from the camera are kept (unless asked) and start new matches.
const fixedNorth = n => n.heading != null && ['manual', 'exif', 'sun'].includes(n.headingSource);

function openNorthMatch() {
  closeMenus();
  const refs = S.nodes.filter(n => n.photoFile && fixedNorth(n));
  if (!refs.length) {
    alert(t('Set north by hand on one photo first (side panel: "Current view is north") - the others are turned to match it.'));
    return;
  }
  const sel = document.getElementById('mn-ref');
  sel.innerHTML = refs.map(n => `<option value="${n.id}">${escapeHtml(n.name)} (${n.heading.toFixed(1)}°)</option>`).join('');
  sel.value = (refs.find(n => n.id === S.selId) || refs[0]).id;
  document.getElementById('mn-list').innerHTML = '';
  document.getElementById('mn-status').textContent = '';
  document.getElementById('mn-actions').hidden = true;
  openModal('mnorth-back');
}

document.getElementById('mn-go').onclick = async () => {
  const ref = gn(document.getElementById('mn-ref').value);
  if (!ref) return;
  const overwrite = document.getElementById('mn-overwrite').checked;
  const btn = document.getElementById('mn-go');
  const status = document.getElementById('mn-status');
  const list = document.getElementById('mn-list');
  btn.disabled = true;
  list.innerHTML = '';
  document.getElementById('mn-actions').hidden = true;
  const known = new Map([[ref.id, ref.heading]]);
  const queue = [ref];
  const best = new Map();
  const tried = new Set();
  let compared = 0;
  try {
    while (queue.length) {
      const a = queue.shift();
      for (const b of getLinkedNodes(a.id)) {
        if (known.has(b.id) || !b.photoFile || tried.has(`${a.id}>${b.id}`)) continue;
        tried.add(`${a.id}>${b.id}`);
        if (!overwrite && fixedNorth(b)) { known.set(b.id, b.heading); queue.push(b); continue; }
        status.textContent = t('Comparing photos… {i}', { i: ++compared });
        await new Promise(r => setTimeout(r));
        const m = bestShift(await featuresOf(a), await featuresOf(b), walkWeights(known.get(a.id), bearingDeg(a, b)));
        const r = { node: b, from: a, heading: headingFromShift(known.get(a.id), m.shift), clear: isClearMatch(m), score: m.score };
        if (!best.has(b.id) || r.score > best.get(b.id).score) best.set(b.id, r);
        if (r.clear) { known.set(b.id, r.heading); queue.push(b); }
      }
    }
  } catch (err) {
    status.textContent = t('Comparing failed: {msg}', { msg: err.message || err });
    btn.disabled = false;
    return;
  }
  btn.disabled = false;
  northResults = [...best.values()].sort((x, y) => S.nodes.indexOf(x.node) - S.nodes.indexOf(y.node));
  if (!northResults.length) { status.textContent = t('No linked photos to compare - link the photos first.'); return; }
  const clear = northResults.filter(r => r.clear).length;
  status.textContent = t('{a} photo(s) matched clearly, {b} uncertain (not ticked).', { a: clear, b: northResults.length - clear });
  northResults.forEach(r => {
    const row = document.createElement('div');
    row.className = `check-row ${r.clear ? 'ok' : 'warn'}`;
    const before = r.node.heading == null ? t('none so far') : t('so far {h}°', { h: r.node.heading.toFixed(1) });
    row.innerHTML = `<span class="lv"></span><span class="nm">${escapeHtml(r.node.name)}</span>`
      + `<span class="msg">${escapeHtml(`${r.heading.toFixed(1)}° (${before}) · ${r.clear ? t('clear match') : t('uncertain')} · ${t('from {name}', { name: r.from.name })}`)}</span>`
      + `<input type="checkbox"${r.clear ? ' checked' : ''}>`;
    list.appendChild(row);
  });
  document.getElementById('mn-actions').hidden = false;
};

document.getElementById('mn-apply').onclick = () => {
  const boxes = [...document.querySelectorAll('#mn-list input[type=checkbox]')];
  const chosen = northResults.filter((r, i) => boxes[i]?.checked);
  if (!chosen.length) return;
  pushUndo();
  chosen.forEach(r => {
    r.node.heading = normalizeHeading(r.heading);
    r.node.headingSource = 'match';
    r.node.viewYaw = normalizeRad(-degToRad(r.node.heading));
  });
  panoNodeId = null;
  closeModal('mnorth-back');
  tourChanged();
  toast(t('Set the heading of {n} photo(s)', { n: chosen.length }));
};

// ── Tour check ────────────────────────────────────
async function checkTour() {
  const issues = [];
  const add = (n, level, msg) => issues.push({ id: n.id, level, msg });
  for (const n of S.nodes) {
    if (!n.photoFile) {
      // Loaded from Google without the photo: only its changes are sent - nothing to check.
      if (!n.gsvPhotoId) add(n, 'warn', t('No photo loaded - this node is not uploaded'));
    } else {
      let info;
      try {
        info = await jpegInfo(n.photoFile);
      } catch {
        add(n, 'error', t('The photo file can no longer be read - drop it again'));
        continue;
      }
      if (!info.jpeg) add(n, 'error', t('Not a JPEG file - Google Street View only accepts JPEG photos'));
      else if (!info.width) add(n, 'warn', t('Could not read the image size'));
      else if (Math.abs(info.width / info.height - 2) > 0.02) add(n, 'error', t('Not a 360° panorama: {w}×{h}, Google needs a 2:1 image', { w: info.width, h: info.height }));
      else if (info.width < 4096) add(n, 'warn', t('Only {w}×{h} - Google recommends at least 4096×2048', { w: info.width, h: info.height }));
      if (!info.gpano) add(n, 'warn', t('No 360° (GPano) metadata - Google may not show it as a panorama'));
      if (n.photoFile.size > GOOGLE_MAX) add(n, 'warn', t('Larger than Google\'s 75 MB limit - it is made smaller for the upload'));
    }
    if (!n.hasGps) add(n, 'warn', t('Position was placed by hand, not from GPS - check that it is right'));
    if (n.heading == null) add(n, 'warn', t('No heading (north) set'));
    if (S.nodes.length > 1 && !getLinkedNodes(n.id).length) add(n, 'warn', t('Not connected to any other node'));
    if (n.gsvStatus === 'rejected') add(n, 'error', t('Rejected by Google'));
  }
  S.conns.forEach(c => {
    const a = gn(c.from), b = gn(c.to);
    if (a && b && distanceMeters(a, b) > 50) add(a, 'warn', t('Link to {name} is {d} long', { name: b.name, d: formatDistance(distanceMeters(a, b)) }));
  });
  // The same photo at two points (duplicated, or imported twice): it would be on Google twice.
  const firstWith = new Map();
  S.nodes.forEach(n => {
    if (!n.photoFile) return;
    const key = `${n.photoName}|${n.photoFile.size}`;
    const first = firstWith.get(key);
    if (first) add(n, 'warn', t('The same photo as {name} - it would be on Google twice', { name: first.name }));
    else firstWith.set(key, n);
  });
  for (let i = 0; i < S.nodes.length; i++) {
    for (let k = i + 1; k < S.nodes.length; k++) {
      if (distanceMeters(S.nodes[i], S.nodes[k]) < 0.5) add(S.nodes[i], 'warn', t('Same position as {name}', { name: S.nodes[k].name }));
    }
  }
  return issues.sort((a, b) => (a.level === 'error' ? 0 : 1) - (b.level === 'error' ? 0 : 1));
}

// North of linked photos, compared by their horizons (as "North from the photos" does): a clear
// match that disagrees with the headings set means one of the two is turned.
async function checkNorthAgainstNeighbours(onProgress) {
  const issues = [];
  const pairs = S.conns.map(c => [gn(c.from), gn(c.to)]).filter(([a, b]) => a?.photoFile && b?.photoFile && a.heading != null && b.heading != null);
  for (const [i, [a, b]] of pairs.entries()) {
    onProgress?.(i + 1, pairs.length);
    await new Promise(r => setTimeout(r));
    const m = bestShift(await featuresOf(a), await featuresOf(b), walkWeights(a.heading, bearingDeg(a, b)));
    if (!isClearMatch(m)) continue;
    const off = headingDiff(headingFromShift(a.heading, m.shift), b.heading);
    if (off > 15) issues.push({ id: b.id, level: 'warn', msg: t('North disagrees with {name} by {d}° (compared by their horizons) - check both', { name: a.name, d: Math.round(off) }) });
  }
  return issues;
}

let checkContinue = null;
async function openTourCheck({ onContinue = null } = {}) {
  checkContinue = onContinue;
  const list = document.getElementById('check-list');
  list.innerHTML = `<div class="check-ok" style="color:var(--text-dim)">${t('Checking…')}</div>`;
  openModal('check-back');
  const issues = await checkTour();
  const errors = issues.filter(i => i.level === 'error').length;
  document.getElementById('check-summary').textContent = issues.length
    ? t('{e} problem(s) that stop the upload, {w} warning(s).', { e: errors, w: issues.length - errors })
    : '';
  list.innerHTML = issues.length ? '' : `<div class="check-ok">${t('Everything looks good.')}</div>`;
  document.getElementById('check-north-status').textContent = '';
  renderCheckRows(issues);
  document.getElementById('check-continue').style.display = onContinue ? '' : 'none';
  return issues;
}

function renderCheckRows(issues) {
  const list = document.getElementById('check-list');
  issues.forEach(i => {
    const n = gn(i.id);
    const row = document.createElement('div');
    row.className = `check-row ${i.level}`;
    row.innerHTML = `<span class="lv"></span><span class="nm">${escapeHtml(n?.name)}</span><span class="msg">${escapeHtml(i.msg)}</span><button class="btn">${t('Show')}</button>`;
    row.querySelector('button').onclick = () => {
      document.querySelectorAll('.modal-back.show').forEach(el => el.classList.remove('show'));
      jumpToNode(i.id, false);
    };
    list.appendChild(row);
  });
}
document.getElementById('check-rerun').onclick = () => openTourCheck({ onContinue: checkContinue });
document.getElementById('check-north').onclick = async () => {
  const btn = document.getElementById('check-north');
  const status = document.getElementById('check-north-status');
  btn.disabled = true;
  try {
    const found = await checkNorthAgainstNeighbours((i, n) => { status.textContent = t('Comparing photos… {i}', { i: `${i}/${n}` }); });
    status.textContent = found.length ? t('{n} photo(s) whose north disagrees with a neighbour.', { n: found.length }) : t('North agrees with the neighbouring photos wherever they could be compared.');
    if (found.length) {
      document.querySelector('#check-list .check-ok')?.remove();
      renderCheckRows(found);
    }
  } catch (err) {
    status.textContent = t('Comparing failed: {msg}', { msg: err.message || err });
  } finally {
    btn.disabled = false;
  }
};
document.getElementById('check-continue').onclick = () => {
  closeModal('check-back');
  const fn = checkContinue;
  checkContinue = null;
  fn?.();
};

// ── Faces and plates in the blur tool ─────────────
// Suggestions (dashed) from detect.js, to check: a click removes a box.
async function suggestIn(kind) {
  const face = kind === 'face';
  const btn = document.getElementById(face ? 'blur-detect' : 'blur-plates');
  const label = document.getElementById('blur-count');
  btn.disabled = true;
  try {
    label.textContent = t(face ? 'Loading face detection…' : 'Loading vehicle detection…');
    const boxes = await (face ? findFaces : findPlates)(blur.bmp, p => {
      label.textContent = t(face ? 'Looking for faces… {p}%' : 'Looking for vehicles… {p}%', { p: Math.round(p * 100) });
    });
    const fresh = boxes.filter(b => !blur.rects.some(r => overlap(r, b) > 0.3));
    fresh.forEach(b => blur.rects.push({ ...b, auto: true }));
    drawBlur();
    const count = fresh.filter(b => !b.seam).length; // one across the seam is two boxes
    label.textContent = face
      ? (fresh.length ? t('{n} face(s) suggested (dashed) - click a box to remove it, drag to add more.', { n: count }) : t('No faces found - mark them by hand if there are any.'))
      : (fresh.length ? t('{n} vehicle(s) found - the lower middle of each, where plates usually are, is suggested (blue). Remove boxes without a plate.', { n: count }) : t('No vehicles found - mark plates by hand if there are any.'));
  } catch (err) {
    label.textContent = t(face ? 'Face detection failed: {msg}' : 'Vehicle detection failed: {msg}', { msg: t(err.message || String(err)) });
  } finally {
    btn.disabled = false;
  }
}
document.getElementById('blur-detect').onclick = () => suggestIn('face');
document.getElementById('blur-plates').onclick = () => suggestIn('plate');

// ── Faces and plates in all photos ────────────────
// Every photo is checked here, on this computer; then the photos with findings are
// gone through one by one in the blur tool, which opens with the suggestions.
const privacy = { results: new Map(), running: false, stop: false };

function openPrivacy() {
  closeMenus();
  renderPrivacy();
  openModal('privacy-back');
}

function renderPrivacy() {
  const list = document.getElementById('pv-list');
  list.innerHTML = '';
  S.nodes.forEach(n => {
    const r = privacy.results.get(n.id);
    if (!r || !r.boxes.length) return;
    const row = document.createElement('div');
    const seen = r.done || n.privacyChecked;
    row.className = `check-row ${seen ? 'ok' : 'warn'}`;
    const what = [r.faces && t('{n} face(s)', { n: r.faces }), r.plates && t('{n} vehicle(s)', { n: r.plates })].filter(Boolean).join(', ');
    const state = r.done ? ` · ${t('blurred')}` : seen ? ` · ${t('reviewed')}` : '';
    row.innerHTML = `<span class="lv"></span><span class="nm">${escapeHtml(n.name)}</span><span class="msg">${escapeHtml(what + state)}</span><button class="btn">${escapeHtml(t(seen ? 'Again' : 'Review'))}</button>`;
    row.querySelector('button').onclick = () => {
      closeModal('privacy-back');
      openBlurTool(n.id, { presets: r.boxes, batch: true });
    };
    list.appendChild(row);
  });
}

// Back to the list after a photo was looked at (applied, nothing to blur, or just closed).
new MutationObserver(() => {
  if (!document.getElementById('blur-back').classList.contains('show') && blur.batch) {
    blur.batch = false;
    // Checked only when blurred or confirmed as nothing to hide - just closing leaves it open.
    renderPrivacy();
    openModal('privacy-back');
  }
}).observe(document.getElementById('blur-back'), { attributes: true, attributeFilter: ['class'] });

document.getElementById('pv-cancel').onclick = () => { privacy.stop = true; };
document.getElementById('pv-go').onclick = async () => {
  const faces = document.getElementById('pv-faces').checked;
  const plates = document.getElementById('pv-plates').checked;
  const skip = document.getElementById('pv-skip').checked;
  const status = document.getElementById('pv-status');
  const bar = document.getElementById('pv-progress');
  const nodes = S.nodes.filter(n => n.photoFile && !(skip && (n.blurred || n.privacyChecked)));
  if (!nodes.length || (!faces && !plates)) { status.textContent = t('Nothing to check.'); return; }
  privacy.results.clear();
  privacy.stop = false;
  document.getElementById('pv-go').disabled = true;
  document.getElementById('pv-cancel').hidden = false;
  bar.hidden = false;
  renderPrivacy();
  let checked = 0;
  try {
    for (const n of nodes) {
      if (privacy.stop) break;
      const part = (step, p) => { bar.firstElementChild.style.width = `${((checked + (step + p) / (faces && plates ? 2 : 1)) / nodes.length) * 100}%`; };
      status.textContent = t('Checking {name} ({i} of {n})…', { name: n.name, i: checked + 1, n: nodes.length });
      const bmp = await createImageBitmap(n.photoFile);
      try {
        const found = [...(faces ? await findFaces(bmp, p => part(0, p)) : []), ...(plates ? await findPlates(bmp, p => part(faces ? 1 : 0, p)) : [])];
        privacy.results.set(n.id, { boxes: found, faces: found.filter(b => b.kind === 'face' && !b.seam).length, plates: found.filter(b => b.kind === 'plate' && !b.seam).length, done: false });
        // Nothing found: checked. Findings: checked once they were looked at in the blur tool.
        n.privacyChecked = !found.length;
      } finally {
        bmp.close?.();
      }
      checked++;
      renderPrivacy();
      scheduleAutosave();
    }
    const withFindings = [...privacy.results.values()].filter(r => r.boxes.length);
    status.textContent = t('Checked {a} of {b} photo(s): {c} with faces or vehicles ({f} face(s), {v} vehicle(s)). Go through them with "Review".', {
      a: checked, b: nodes.length, c: withFindings.length,
      f: withFindings.reduce((s, r) => s + r.faces, 0), v: withFindings.reduce((s, r) => s + r.plates, 0),
    });
  } catch (err) {
    status.textContent = t('Checking failed: {msg}', { msg: t(err.message || String(err)) });
  } finally {
    document.getElementById('pv-go').disabled = false;
    document.getElementById('pv-cancel').hidden = true;
    bar.hidden = true;
  }
};

// ── Photos from Google ────────────────────────────
// Photos already on Street View come into the editor with what Google keeps of
// them - position, direction, floor, place and links - to be changed here; the next
// upload sends only the changes, the photos are not uploaded again. The photos
// themselves can be downloaded too (for the panorama, north and blurring).
const fromGoogle = { photos: [], running: false, stop: false };

async function postJson(path, body = {}) {
  try {
    const r = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    if (r.status === 401) return { ok: false, error: 'not_signed_in' };
    return await r.json();
  } catch {
    return { ok: false, error: t('The editor app is not reachable.') };
  }
}

async function openFromGoogle() {
  closeMenus();
  fromGoogle.photos = [];
  document.getElementById('fg-status').textContent = '';
  openModal('fromg-back');
  renderFromGoogle(t('Asking Google…'));
  const j = await postJson('/api/list-photos');
  if (!j.ok) {
    renderFromGoogle(j.error === 'not_signed_in' ? t('Sign in with Google first (in the upload dialog).') : `${t('Google error')}: ${j.error}`);
    return;
  }
  const known = new Set(S.nodes.map(n => n.gsvPhotoId).filter(Boolean));
  fromGoogle.photos = j.photos.filter(p => p.id && p.lat != null && p.lng != null && !known.has(p.id));
  renderFromGoogle();
}

function fromGoogleSelection() {
  if (!document.getElementById('fg-view').checked) return fromGoogle.photos;
  const b = map.getBounds();
  return fromGoogle.photos.filter(p => b.contains([p.lat, p.lng]));
}

function renderFromGoogle(message = null) {
  const all = fromGoogle.photos;
  const b = map.getBounds();
  document.getElementById('fg-n-view').textContent = `(${all.filter(p => b.contains([p.lat, p.lng])).length})`;
  document.getElementById('fg-n-all').textContent = `(${all.length})`;
  document.getElementById('fg-sum').textContent = message
    ?? (all.length ? t('{n} photo(s) on Google that are not in this project yet.', { n: all.length }) : t('All your photos on Google are in this project already.'));
  document.getElementById('fg-go').disabled = fromGoogle.running || !fromGoogleSelection().length;
}
document.querySelectorAll('input[name=fg-scope]').forEach(r => { r.onchange = () => renderFromGoogle(); });
document.getElementById('fg-stop').onclick = () => { fromGoogle.stop = true; };

async function downloadGooglePhoto(p) {
  try {
    const r = await fetch('/api/google-photo', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id: p.id }) });
    if (!r.ok || !(r.headers.get('Content-Type') || '').startsWith('image/')) return null;
    return new File([await r.blob()], `google-${p.id.replace(/[^\w-]/g, '').slice(-12)}.jpg`, { type: 'image/jpeg' });
  } catch {
    return null;
  }
}

document.getElementById('fg-go').onclick = async () => {
  const photos = fromGoogleSelection().slice().sort((a, b) => String(a.uploadTime || '').localeCompare(String(b.uploadTime || '')));
  if (!photos.length || fromGoogle.running) return;
  const download = document.getElementById('fg-download').checked;
  const status = document.getElementById('fg-status');
  const bar = document.getElementById('fg-progress');
  fromGoogle.running = true;
  fromGoogle.stop = false;
  document.getElementById('fg-stop').hidden = false;
  bar.hidden = false;
  renderFromGoogle();
  pushUndo();
  const tourId = makeTourId();
  S.tourNames[tourId] = t('Photos from Google');
  const byGoogleId = new Map(S.nodes.filter(n => n.gsvPhotoId).map(n => [n.gsvPhotoId, n]));
  const added = [];
  let withoutPhoto = 0;
  try {
    for (const [i, p] of photos.entries()) {
      if (fromGoogle.stop) break;
      status.textContent = t('Loading photo {a} of {b}…', { a: i + 1, b: photos.length });
      bar.firstElementChild.style.width = `${(i / photos.length) * 100}%`;
      const file = download ? await downloadGooglePhoto(p) : null;
      if (download && !file) withoutPhoto++;
      const heading = p.heading ?? null;
      const yaw = heading != null ? normalizeRad(-degToRad(heading)) : 0;
      const n = {
        id: uid(), name: t('Google photo {n}', { n: i + 1 }), lat: p.lat, lng: p.lng,
        heading, origHeading: heading, viewYaw: yaw, origViewYaw: yaw,
        headingSource: heading != null ? 'google' : 'manual', hasGps: true,
        photoUrl: file ? URL.createObjectURL(file) : null, photoName: file?.name ?? null, photoFile: file,
        marker: null, tourId, hotspots: [],
        gsvPhotoId: p.id, gsvConfirmed: true, gsvStatus: p.status, gsvShareLink: p.shareLink || null,
        gsvUploadedAt: Date.parse(p.uploadTime) || null, gsvViews: p.viewCount ?? null, gsvObsolete: [],
        gsvRemote: { lat: p.lat, lng: p.lng, heading: p.heading ?? null, connections: p.connections || [] },
        ...(p.level ? { level: { number: Number(p.level.number) || 0, name: p.level.name || '' } } : {}),
        ...(p.placeId ? { placeId: p.placeId, placeName: p.placeName || p.placeId, placeAuto: false } : {}),
      };
      S.nodes.push(n);
      createMarker(n);
      byGoogleId.set(p.id, n);
      added.push([n, p]);
    }
    // The links as Google has them - also to photos that were in the project before.
    const have = new Set(S.conns.map(c => [c.from, c.to].sort().join('|')));
    added.forEach(([n, p]) => (p.connections || []).forEach(gid => {
      const o = byGoogleId.get(gid);
      const key = o && [n.id, o.id].sort().join('|');
      if (!o || o === n || have.has(key)) return;
      have.add(key);
      const c = { id: cuid(), from: n.id, to: o.id, poly: null, xMark: null };
      S.conns.push(c);
      drawConn(c, { animate: false });
    }));
    // What Google has now is what later changes are compared with.
    added.forEach(([n]) => { n.gsvSig = gsvSignature(n); });
    toursVersion++;
    tourChanged();
    if (added.length) map.fitBounds(L.latLngBounds(added.map(([n]) => [n.lat, n.lng])), { padding: [60, 60], maxZoom: 19 });
    status.textContent = [
      t('{n} photo(s) loaded from Google.', { n: added.length }),
      withoutPhoto ? t('{n} of them without the photo itself (Google offered no download) - their position, direction and links can still be changed.', { n: withoutPhoto }) : '',
      fromGoogle.stop ? t('Stopped.') : '',
    ].filter(Boolean).join(' ');
    toast(t('{n} photo(s) loaded from Google', { n: added.length }));
  } finally {
    fromGoogle.running = false;
    document.getElementById('fg-stop').hidden = true;
    bar.hidden = true;
    const known = new Set(S.nodes.map(n => n.gsvPhotoId).filter(Boolean));
    fromGoogle.photos = fromGoogle.photos.filter(p => !known.has(p.id));
    renderFromGoogle();
  }
};

// ── Rename points ─────────────────────────────────
// Many points at once, by a pattern: {n} the number, {place} the point's place on
// Google Maps (else its name), {tour} the tour's name, {name} the name it has.
// Numbered in walking order from the tour's start, or as in the list.
function renameScopeNodes() {
  const scope = document.querySelector('input[name=rn-scope]:checked')?.value;
  if (scope === 'sel') return S.nodes.filter(n => S.multiSel.has(n.id) || (!S.multiSel.size && n.id === S.selId));
  if (scope === 'tour') {
    const g = S.selId && tourOf(S.selId);
    return g ? g.nodes.slice() : S.nodes.slice();
  }
  return S.nodes.slice();
}

// The points in walking order: from each tour's start along the links, nearest first.
function walkingOrder(nodes) {
  const ids = new Set(nodes.map(n => n.id));
  const seen = new Set();
  const out = [];
  const visit = first => {
    const queue = [first];
    seen.add(first.id);
    while (queue.length) {
      const n = queue.shift();
      out.push(n);
      getLinkedNodes(n.id).filter(o => ids.has(o.id) && !seen.has(o.id))
        .sort((a, b) => distanceMeters(n, a) - distanceMeters(n, b))
        .forEach(o => { seen.add(o.id); queue.push(o); });
    }
  };
  nodes.filter(n => S.starts.has(n.id)).forEach(n => { if (!seen.has(n.id)) visit(n); });
  nodes.forEach(n => { if (!seen.has(n.id)) visit(n); });
  return out;
}

function renamePlan() {
  let nodes = renameScopeNodes();
  if (document.getElementById('rn-order').value === 'walk') nodes = walkingOrder(nodes);
  else nodes = S.nodes.filter(n => nodes.includes(n));
  const pattern = document.getElementById('rn-pattern').value.trim() || '{name}';
  const first = Number(document.getElementById('rn-start').value) || 1;
  const digits = String(first + nodes.length - 1).length;
  const names = nodes.map((n, i) => pattern
    .replace(/\{n\}/g, String(first + i).padStart(pattern.includes('{n}') && nodes.length >= 10 ? digits : 1, '0'))
    .replace(/\{place\}/g, n.placeName ? String(n.placeName).split(',')[0] : n.name)
    .replace(/\{tour\}/g, tourOf(n.id) ? tourName(tourOf(n.id)) : '')
    .replace(/\{name\}/g, n.name)
    .replace(/\s+/g, ' ').trim() || n.name);
  // The same name twice (e.g. several photos at one place): numbered.
  const count = new Map();
  names.forEach(x => count.set(x, (count.get(x) || 0) + 1));
  const used = new Map();
  return nodes.map((n, i) => {
    const base = names[i];
    if (count.get(base) < 2) return [n, base];
    used.set(base, (used.get(base) || 0) + 1);
    return [n, `${base} ${used.get(base)}`];
  });
}

function renderRename() {
  const plan = renamePlan();
  const list = document.getElementById('rn-preview');
  list.innerHTML = '';
  plan.slice(0, 8).forEach(([n, name]) => {
    const row = document.createElement('div');
    row.className = 'rn-row';
    row.innerHTML = `<span>${escapeHtml(n.name)}</span><span>→</span><b>${escapeHtml(name)}</b>`;
    list.appendChild(row);
  });
  if (plan.length > 8) list.insertAdjacentHTML('beforeend', `<div class="mini">${escapeHtml(t('… and {n} more', { n: plan.length - 8 }))}</div>`);
  document.getElementById('rn-go').disabled = !plan.length;
  document.getElementById('rn-go').textContent = t('Rename {n} point(s)', { n: plan.length });
}

function openRename() {
  closeMenus();
  if (!S.nodes.length) return;
  const many = S.multiSel.size >= 2;
  document.getElementById('rn-sel-label').hidden = !many && !S.selId;
  document.querySelector(`input[name=rn-scope][value="${many ? 'sel' : manyTours() && S.selId ? 'tour' : 'all'}"]`).checked = true;
  openModal('rename-back');
  renderRename();
}
['rn-pattern', 'rn-start', 'rn-order'].forEach(id => document.getElementById(id).addEventListener('input', renderRename));
document.querySelectorAll('input[name=rn-scope]').forEach(r => { r.onchange = renderRename; });
document.querySelectorAll('[data-rn-pattern]').forEach(b => {
  b.onclick = () => { document.getElementById('rn-pattern').value = b.dataset.rnPattern; renderRename(); };
});
document.getElementById('rn-go').onclick = () => {
  const plan = renamePlan().filter(([n, name]) => n.name !== name);
  closeModal('rename-back');
  if (!plan.length) return;
  pushUndo();
  plan.forEach(([n, name]) => { n.name = name; refreshMarker(n); });
  tourChanged();
  toast(t('{n} point(s) renamed', { n: plan.length }));
};

// ── Photos with position and north ────────────────
// A photo with where it is, which way it faces and when it was taken written into it
// (a new EXIF block; the image itself is not touched) - for the photos export
// (exports.js) and Panoramax, which take these from the photos.
async function photoWithPose(n) {
  return replaceExif(n.photoFile, await poseExif(n), { heading: n.heading });
}

// The next photo (after the selected one) without north - N.
function nextWithoutNorth() {
  const open = S.nodes.filter(n => n.photoFile && n.heading == null);
  if (!open.length) { toast(t('Every photo has its north')); return null; }
  const pos = indexNodes().pos;
  const at = pos.get(S.selId) ?? -1;
  return (open.find(n => pos.get(n.id) > at) || open[0]).id;
}

// ── Side panel width ──────────────────────────────
// Drag the edge to make the side panel - and with it the panorama - wider.
const sidebar = document.getElementById('sb');
function setSidebarWidth(w) {
  const width = Math.round(Math.max(280, Math.min(w, Math.min(760, innerWidth - 420))));
  sidebar.style.width = `${width}px`;
  return width;
}
function afterSidebarResize() {
  map.invalidateSize();
  panoViewer?.autoSize?.();
  cmpBaseViewer?.autoSize?.();
  cmpOverlayViewer?.autoSize?.();
  updateViewCone();
}
(() => {
  const handle = document.getElementById('sb-resize');
  const saved = Number(storageGet('sbWidth'));
  if (saved) setSidebarWidth(saved);
  let drag = null;
  handle.addEventListener('pointerdown', e => {
    drag = { x: e.clientX, w: sidebar.getBoundingClientRect().width };
    handle.setPointerCapture(e.pointerId);
    document.body.classList.add('resizing');
  });
  handle.addEventListener('pointermove', e => {
    if (!drag) return;
    setSidebarWidth(drag.w + e.clientX - drag.x);
    requestAnimationFrame(afterSidebarResize);
  });
  handle.addEventListener('pointerup', () => {
    if (!drag) return;
    drag = null;
    document.body.classList.remove('resizing');
    storageSet('sbWidth', String(setSidebarWidth(sidebar.getBoundingClientRect().width)));
    afterSidebarResize();
  });
  handle.addEventListener('dblclick', () => {
    sidebar.style.width = '';
    storageSet('sbWidth', '');
    afterSidebarResize();
  });
})();

// ── Tour as KML / GPX, tour length ────────────────
// For Google Earth, My Maps or a GPS app: the points (with their Street View link
// once published) and the links between them as lines.
function tourLength(nodes) {
  const ids = new Set(nodes.map(n => n.id));
  return S.conns.filter(c => ids.has(c.from) && ids.has(c.to))
    .reduce((sum, c) => sum + distanceMeters(gn(c.from), gn(c.to)), 0);
}

// ── Share the tour ────────────────────────────────
// The tour's start on Google Maps, and the exported website: as a link, a QR code
// (share.js) and, for the website, the code to embed it in another page.
let shareKey = null; // which tour

function shareScopes() {
  const tours = getTours();
  return tours.length ? tours.map(g => ({ key: `t${g.index}`, name: tourName(g), nodes: g.nodes, start: tourStart(g) }))
    : [{ key: 'all', name: projectName?.replace(/\.vrtour$/i, '') || t('Tour'), nodes: S.nodes, start: S.nodes.find(n => S.starts.has(n.id)) || S.nodes[0] }];
}

function openShare() {
  closeMenus();
  if (!S.nodes.length) return;
  const scopes = shareScopes();
  const cur = tourOf(S.selId);
  shareKey = cur?.index != null ? `t${cur.index}` : scopes[0].key;
  const select = document.getElementById('share-tour');
  select.innerHTML = scopes.map(sc => `<option value="${sc.key}">${escapeHtml(sc.name)}</option>`).join('');
  select.value = shareKey;
  document.getElementById('share-tour-row').hidden = scopes.length < 2;
  openModal('share-back');
  renderShare();
}

function shareSiteKey(scope) { return `shareUrl:${projectName || ''}:${scope.name}`; }

function renderShare() {
  const scope = shareScopes().find(sc => sc.key === shareKey) || shareScopes()[0];
  // On Google Maps: the start photo's link (any photo of the tour, if the start isn't there yet)
  const onMaps = [scope.start, ...scope.nodes].find(n => n?.gsvShareLink);
  const maps = document.getElementById('share-maps');
  maps.hidden = !onMaps;
  document.getElementById('share-maps-none').hidden = !!onMaps;
  if (onMaps) {
    document.getElementById('share-maps-link').value = onMaps.gsvShareLink;
    document.getElementById('share-maps-qr').innerHTML = qrSvg(onMaps.gsvShareLink, scope.name);
  }
  // The website: its address once it is online
  const input = document.getElementById('share-site-url');
  if (document.activeElement !== input) input.value = storageGet(shareSiteKey(scope)) || '';
  const url = input.value.trim();
  const ok = isWebAddress(url);
  document.getElementById('share-site').hidden = !ok;
  input.classList.toggle('bad', !!url && !ok);
  if (ok) {
    document.getElementById('share-site-embed').value = embedCode(url, scope.name);
    document.getElementById('share-site-qr').innerHTML = qrSvg(url, scope.name);
  }
}

document.getElementById('share-tour').onchange = e => { shareKey = e.target.value; renderShare(); };
document.getElementById('share-site-url').addEventListener('input', e => {
  const scope = shareScopes().find(sc => sc.key === shareKey) || shareScopes()[0];
  storageSet(shareSiteKey(scope), e.target.value.trim());
  renderShare();
});

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast(t('Copied'));
  } catch {
    prompt(t('Copy this:'), text);
  }
}
document.getElementById('share-maps-copy').onclick = () => copyText(document.getElementById('share-maps-link').value);
document.getElementById('share-site-copy').onclick = () => copyText(document.getElementById('share-site-embed').value);

async function saveQr(text, kind, format) {
  const scope = shareScopes().find(sc => sc.key === shareKey) || shareScopes()[0];
  const name = `${fileBase(scope.name)}-${kind}-qr.${format}`;
  if (format === 'svg') await saveTextFile(name, new Blob([qrSvg(text, scope.name)], { type: 'image/svg+xml' }));
  else await saveTextFile(name, await qrPng(text));
}
document.querySelectorAll('[data-qr]').forEach(b => {
  b.onclick = () => {
    const [kind, format] = b.dataset.qr.split(':');
    const text = kind === 'maps' ? document.getElementById('share-maps-link').value : document.getElementById('share-site-url').value.trim();
    saveQr(text, kind, format);
  };
});

// ── Sample tour ───────────────────────────────────
// A small tour with drawn photos to try the editor with (packaging/make_sample.py).
// Its photos are named svt-sample-NN.jpg and are never uploaded to Google.
const isSamplePhoto = n => /^svt-sample-\d+\.jpg$/.test(n.photoName || '');

async function openSample() {
  closeMenus();
  let blob;
  try {
    const r = await fetch('sample/sample.vrtour', { cache: 'no-store' });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    blob = await r.blob();
  } catch (err) {
    alert(t('The sample tour could not be loaded: {msg}', { msg: err.message || err }));
    return;
  }
  await openProjectFile(new File([blob], `${t('Sample tour')}.vrtour`, { type: 'application/zip' }));
  if (!S.nodes.some(isSamplePhoto)) return; // not opened (declined)
  // Names and the info text in the editor's language.
  S.nodes.filter(isSamplePhoto).forEach(n => {
    n.name = t(n.name);
    (n.hotspots || []).forEach(h => { h.title = t(h.title); h.text = t(h.text); });
    if (S.tourNames[n.tourId]) S.tourNames[n.tourId] = t(S.tourNames[n.tourId]);
  });
  tourChanged();
  markClean();
  if (S.starts.size) jumpToNode([...S.starts][0], false);
}
document.getElementById('es-sample').onclick = openSample;

// ── Checklist ─────────────────────────────────────
// What is done and what is left for a tour, as chips at the top of the side panel:
// for the tour of the selected point (or the only one). A chip that is not done yet
// leads to where it gets done. The review before publishing spells the same out.
function tourFacts(nodes) {
  const ids = new Set(nodes.map(n => n.id));
  const withPhoto = nodes.filter(n => n.photoFile);
  return {
    nodes,
    withPhoto,
    noPhoto: nodes.filter(n => !n.photoFile && !n.gsvPhotoId),
    noNorth: withPhoto.filter(n => n.heading == null),
    unchecked: withPhoto.filter(n => !n.blurred && !n.privacyChecked),
    withPlace: nodes.filter(n => n.placeId),
    levels: nodes.filter(n => n.level).length,
    links: S.conns.filter(c => ids.has(c.from)).length,
    unlinked: nodes.length > 1 ? (linked => nodes.filter(n => !linked.has(n.id)))(new Set(S.conns.flatMap(c => [c.from, c.to]))) : [],
    onGoogle: nodes.filter(n => n.gsvPhotoId && n.gsvStatus !== 'missing'),
    processing: nodes.filter(n => n.gsvPhotoId && !n.gsvConfirmed && n.gsvStatus !== 'missing'),
    toUpload: nodes.filter(n => !isSamplePhoto(n) && ((n.photoFile && needsUpload(n)) || gsvChanged(n))),
    sample: nodes.some(isSamplePhoto),
  };
}

function checklistGroup() {
  const tours = getTours();
  const g = (S.selId && tourOf(S.selId)) || (tours.length === 1 ? tours[0] : null);
  if (g) return { name: tours.length > 1 ? tourName(g) : '', nodes: g.nodes };
  return { name: tours.length > 1 ? t('all tours') : '', nodes: S.nodes };
}

function checklistChips(f) {
  const jump = list => () => jumpToNode(list[0].id, false);
  const upload = () => document.getElementById('bupload').click();
  return [
    { key: 'photos', ...(f.noPhoto.length
      ? { level: 'todo', label: t('{n} without photo', { n: f.noPhoto.length }), tip: t('Show the first one'), act: jump(f.noPhoto) }
      : { level: 'ok', label: t('{n} photo(s)', { n: f.withPhoto.length }) }) },
    { key: 'links', ...(f.unlinked.length
      ? { level: 'todo', label: t('{n} not linked', { n: f.unlinked.length }), tip: t('Show the first one'), act: jump(f.unlinked) }
      : { level: 'ok', label: t('{n} link(s)', { n: f.links }) }) },
    { key: 'north', ...(f.noNorth.length
      ? { level: 'todo', label: t('{n} without north', { n: f.noNorth.length }), tip: t('Show the first one'), act: jump(f.noNorth) }
      : { level: 'ok', label: t('North set') }) },
    { key: 'privacy', ...(f.unchecked.length
      ? { level: 'todo', label: t('Faces & plates: {n} not checked', { n: f.unchecked.length }), tip: t('Check all photos for faces & plates…'), act: openPrivacy }
      : { level: 'ok', label: t('Faces & plates checked'), act: openPrivacy }) },
    { key: 'places', level: f.withPlace.length === f.nodes.length ? 'ok' : 'info', label: t('Places {a}/{b}', { a: f.withPlace.length, b: f.nodes.length }), tip: t('Places on Google Maps…'), act: () => TOOL_ACTIONS.places() },
    { key: 'publish', ...(f.sample
      ? { level: 'info', label: t('Sample - never uploaded') }
      : f.toUpload.length
        ? { level: 'todo', label: f.onGoogle.length ? t('{n} change(s) for Google', { n: f.toUpload.length }) : t('Not on Google yet'), tip: t('Upload to Google Street View'), act: upload }
        : f.processing.length
          ? { level: 'info', label: t('Google is processing {n}', { n: f.processing.length }), tip: t('Links and positions follow by themselves once Google is ready.'), act: upload }
          : f.onGoogle.length
            ? { level: 'ok', label: t('On Google'), act: upload }
            : { level: 'todo', label: t('Not on Google yet'), tip: t('Upload to Google Street View'), act: upload }) },
  ];
}

// The tools of each step, under the steps for the one chosen (or the first still open).
const STEP_TOOLS = {
  photos: [['Import photos…', () => openImport()], ['Place photos from GPX track…', () => openGpx()], ['Move or turn a tour…', () => openShift()]],
  links: [['Auto-connect nodes…', () => openAutoConnect()]],
  north: [['Headings from walking direction…', () => openWalkHeadings()], ['North from the photos…', () => openNorthMatch()], ['North from the sun…', () => openSunNorth()]],
  privacy: [['Check all photos for faces & plates…', () => openPrivacy()], ['Cover the tripod…', () => openNadir()]],
  places: [['Places on Google Maps…', () => openPlacesSetup()]],
  publish: [['Check tour…', () => openTourCheck()], ['Preview', () => openExportPreview()], ['Upload', () => document.getElementById('bupload').click()], ['Publish on Panoramax…', () => openPanoramax()]],
};
let checklistStep = null;

let checklistQueued = false;
function queueChecklist() {
  if (checklistQueued) return;
  checklistQueued = true;
  requestAnimationFrame(() => { checklistQueued = false; renderChecklist(); updateTourProgress(); });
}

function renderChecklist() {
  const box = document.getElementById('checklist');
  box.hidden = !S.nodes.length;
  if (box.hidden) return;
  const group = checklistGroup();
  document.getElementById('checklist-tour').textContent = group.name ? `· ${group.name}` : '';
  const chips = checklistChips(tourFacts(group.nodes));
  const active = chips.find(c => c.key === checklistStep) || chips.find(c => c.level === 'todo') || chips[chips.length - 1];
  const el = document.getElementById('checklist-chips');
  el.innerHTML = '';
  chips.forEach(c => {
    const b = document.createElement('button');
    b.className = `st-chip ${c.level}${c === active ? ' active' : ''}`;
    b.innerHTML = `<i></i>${escapeHtml(c.label)}`;
    b.dataset.step = c.key;
    if (c.tip) b.title = c.tip;
    b.onclick = () => { checklistStep = c.key; renderChecklist(); c.act?.(); };
    el.appendChild(b);
  });
  const tools = document.getElementById('checklist-tools');
  tools.innerHTML = '';
  STEP_TOOLS[active.key].forEach(([label, run]) => {
    const b = document.createElement('button');
    b.className = 'btn';
    b.textContent = t(label);
    b.onclick = run;
    tools.appendChild(b);
  });
}

// ── Review before publishing ──────────────────────
// Comes before every upload: what goes to Google, tour by tour, and what is still
// open - in plain words, with a way to fix each open point first.
let uplReviewed = false;

function reviewRow(level, text, button) {
  const row = document.createElement('div');
  row.className = `check-row ${level}`;
  row.innerHTML = `<span class="lv"></span><span class="msg">${escapeHtml(text)}</span>`;
  if (button) {
    const b = document.createElement('button');
    b.className = 'btn';
    b.textContent = button.label;
    b.onclick = button.run;
    row.appendChild(b);
  }
  return row;
}

async function openReview(nodes) {
  const reupload = document.getElementById('upl-reupload').checked;
  const fresh = nodes.filter(n => goesUp(n, reupload)).length;
  const changed = nodes.filter(n => !goesUp(n, reupload) && n.gsvPhotoId && (gsvChanged(n) || !n.gsvConfirmed)).length;
  const old = document.getElementById('upl-delete-old').checked ? S.nodes.reduce((sum, n) => sum + (n.gsvObsolete?.length || 0), 0) : 0;
  const parts = [t('{n} new photo(s)', { n: fresh }), t('{n} update(s) of photos on Google', { n: changed })];
  if (old) parts.push(t('{n} old version(s) deleted', { n: old }));
  document.getElementById('review-sum').textContent = parts.join(' · ');
  const strip = document.getElementById('review-thumbs');
  const going = nodes.filter(n => goesUp(n, reupload));
  strip.innerHTML = going.slice(0, 14).map(n => {
    const src = requestThumb(n);
    return `<img class="rv-thumb" data-thumb="${escapeHtml(n.id)}" alt="" title="${escapeHtml(n.name)}"${src ? ` src="${src}"` : ''}>`;
  }).join('') + (going.length > 14 ? `<span class="rv-more">+${going.length - 14}</span>` : '');
  strip.hidden = !going.length;

  const list = document.getElementById('review-list');
  list.innerHTML = '';
  const ids = new Set(nodes.map(n => n.id));
  const groups = manyTours()
    ? getTours().map(g => ({ name: tourName(g), nodes: g.nodes.filter(n => ids.has(n.id)) })).filter(g => g.nodes.length)
    : [{ name: null, nodes }];
  const leave = run => () => { closeModal('review-back'); run(); };
  groups.forEach(g => {
    if (g.name) {
      const head = document.createElement('div');
      head.className = 'views-tour';
      head.innerHTML = `<span>${escapeHtml(g.name)}</span><span>${escapeHtml(t('{n} photo(s)', { n: g.nodes.length }))}</span>`;
      list.appendChild(head);
    }
    const f = tourFacts(g.nodes);
    list.appendChild(reviewRow('ok', t('{n} photo(s), each at its position', { n: f.withPhoto.length })));
    list.appendChild(f.noNorth.length
      ? reviewRow('warn', t('{n} photo(s) without north - Google then decides the direction itself.', { n: f.noNorth.length }), { label: t('Show'), run: leave(() => { closeModal('upl-back'); jumpToNode(f.noNorth[0].id, false); }) })
      : reviewRow('ok', t('North is set for every photo.')));
    if (g.nodes.length > 1) {
      list.appendChild(f.links
        ? reviewRow('ok', t('{n} link(s): Google shows them as arrows between the photos.', { n: f.links }))
        : reviewRow('warn', t('No links - on Google the photos are not connected.')));
    }
    list.appendChild(f.withPlace.length
      ? reviewRow('ok', t('{a} of {b} with a place on Google Maps (see the list above).', { a: f.withPlace.length, b: g.nodes.length }))
      : reviewRow('info', t('No places - the photos go up with their positions only (optional).')));
    if (f.levels) list.appendChild(reviewRow('ok', t('{n} photo(s) with a floor', { n: f.levels })));
    list.appendChild(f.unchecked.length
      ? reviewRow('warn', t('{n} photo(s) not checked for faces and licence plates - photos published this way are not blurred by Google.', { n: f.unchecked.length }), { label: t('Check now'), run: leave(() => { closeModal('upl-back'); openPrivacy(); }) })
      : reviewRow('ok', t('Faces and licence plates: checked or blurred in every photo.')));
  });
  const warnings = (await checkTour()).filter(i => i.level !== 'error' && ids.has(i.id)).length;
  if (warnings) list.appendChild(reviewRow('warn', t('{n} warning(s) from the tour check.', { n: warnings }), { label: t('Show'), run: leave(() => openTourCheck()) }));
  openModal('review-back');
}

document.getElementById('review-cancel').onclick = () => closeModal('review-back');
document.getElementById('review-go').onclick = () => {
  closeModal('review-back');
  uplSkipCheck = true; // the check ran before the review
  uplReviewed = true;
  uplStart.click();
};

// ── Panorama focus mode ───────────────────────────
// The panorama over the whole window, to look at a photo closely or walk the
// tour from point to point; F switches it on and off, Esc leaves it.
function setPanoFocus(on) {
  if (on && (panoWrap.style.display === 'none' || !gn(S.selId))) return;
  panoFocus = on;
  panoWrap.classList.toggle('focus', on);
  document.body.classList.toggle('pano-focus', on);
  document.getElementById('pano-focus-bar').hidden = !on;
  if (on) renderFocusBar(gn(S.selId));
  requestAnimationFrame(() => { panoViewer?.autoSize?.(); if (on) updateFocusMap(gn(S.selId)); });
}

// Up walks to the linked point closest to where you look, down to the one behind
// you; left and right turn the view.
function focusKey(key) {
  const n = gn(S.selId);
  if (!n || !panoViewer) return;
  const pos = panoViewer.getPosition();
  if (key === 'ArrowLeft' || key === 'ArrowRight') {
    panoViewer.rotate({ yaw: pos.yaw + (key === 'ArrowLeft' ? -1 : 1) * Math.PI / 6, pitch: pos.pitch });
    return;
  }
  if (n.heading == null) { toast(t("Set this photo's north first - then the arrows know where you look.")); return; }
  const look = normalizeHeading(n.heading + radToDeg(pos.yaw) + (key === 'ArrowDown' ? 180 : 0));
  let best = null, bestDiff = 70;
  getLinkedNodes(n.id).forEach(o => {
    const diff = Math.abs(((bearingDeg(n, o) - look + 540) % 360) - 180);
    if (diff < bestDiff) { best = o; bestDiff = diff; }
  });
  if (best) jumpToNode(best.id, true);
  else toast(t('No linked point in that direction'));
}

let focusMap = null, focusLayer = null;
function updateFocusMap(n) {
  if (!n || !panoFocus) return;
  if (!focusMap) {
    focusMap = L.map('focus-map', { zoomControl: false, keyboard: false });
    focusMap.attributionControl.setPrefix(false);
    L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
      maxZoom: 20, maxNativeZoom: 19, className: 'osm-dark', attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
    }).addTo(focusMap);
    focusLayer = L.layerGroup().addTo(focusMap);
  }
  focusMap.invalidateSize();
  focusLayer.clearLayers();
  const nodes = tourOf(n.id)?.nodes || [n];
  const ids = new Set(nodes.map(x => x.id));
  S.conns.filter(c => ids.has(c.from) && ids.has(c.to)).forEach(c => {
    const a = gn(c.from), b = gn(c.to);
    L.polyline([[a.lat, a.lng], [b.lat, b.lng]], { color: '#8b5cf6', weight: 3, opacity: 0.85 }).addTo(focusLayer);
  });
  nodes.forEach(x => {
    L.circleMarker([x.lat, x.lng], { radius: x === n ? 7 : 5, color: '#fff', weight: 1.5, fillColor: x === n ? '#f6b93b' : '#8b5cf6', fillOpacity: 1 })
      .bindTooltip(escapeHtml(x.name)).on('click', () => jumpToNode(x.id, true)).addTo(focusLayer);
  });
  focusMap.setView([n.lat, n.lng], focusMap.getZoom() || 18);
}

function renderFocusBar(n) {
  const bar = document.getElementById('pano-focus-bar');
  bar.innerHTML = `<b>${escapeHtml(n.name)}</b>`
    + getLinkedNodes(n.id).map(o => `<button class="btn" data-go="${escapeHtml(o.id)}" title="${escapeHtml(t('Walk there'))}">→ ${escapeHtml(o.name)}</button>`).join('')
    + `<button class="btn" data-close title="Esc">${escapeHtml(t('Close'))}</button>`
    + `<span class="pano-focus-keys">${escapeHtml(t('↑ ↓ walk · ← → turn'))}</span>`;
}

document.getElementById('pano-focus-bar').addEventListener('click', e => {
  const b = e.target.closest('button');
  if (!b) return;
  if (b.dataset.go) jumpToNode(b.dataset.go, true);
  else setPanoFocus(false);
});
document.getElementById('bn-focus').onclick = () => setPanoFocus(!panoFocus);

// What the page adds to a bug report (report.js).
function reportPageInfo() {
  const tours = getTours();
  return {
    page: { userAgent: navigator.userAgent, language: navigator.language, editorLanguage: currentLang,
      screen: `${screen.width}x${screen.height}@${devicePixelRatio}`, theme: document.documentElement.dataset.theme || '', window: appHasWindow },
    tour: { points: S.nodes.length, withPhoto: S.nodes.filter(n => n.photoFile).length, links: S.conns.length, tours: tours.length,
      onGoogle: S.nodes.filter(n => n.gsvPhotoId).length, floors: S.nodes.filter(n => n.level).length,
      photoMB: Math.round(S.nodes.reduce((sum, n) => sum + (n.photoFile?.size || 0), 0) / 1e6) },
    messages: notices.slice(0, 50).map(m => ({ at: m.at.toISOString(), kind: m.kind, text: m.msg })),
    errors: pageErrors.slice(),
  };
}

// The parts in modules of their own get what they need from here.
initNotices({ t, lang: () => currentLang });
initReport({ t, closeMenus, openModal, saveTextFile, pageInfo: reportPageInfo });
initVideoDialog({
  t, lang: () => currentLang, toast, openModal, closeMenus, setAppBusy, storageGet, storageSet,
  formatBytes, formatEta, formatDistance, editorKey: EDITOR_KEY,
});
initViewCone({
  map, t,
  node: () => { const n = gn(S.selId); return n && n.photoUrl && panoViewer && panoNodeId === n.id && !panoFocus ? n : null; },
  view: () => ({ yawDeg: radToDeg(panoViewer.getPosition().yaw), hFov: panoViewer.state.hFov || 90 }),
  setHeading: (n, heading) => {
    pushUndo();
    n.heading = heading;
    n.headingSource = 'manual';
    n.viewYaw = northYaw(n);
    refreshHeadingUi(n);
    refreshMarker(n);
    refreshCompareUi(n);
    scheduleAutosave();
    toast(`${n.name}: heading set to ${n.heading.toFixed(1)}°`);
  },
});
initFilmstrip({
  t, storageGet, storageSet, thumb: requestThumb, selId: () => S.selId, jump: id => jumpToNode(id, false), order: walkingOrder,
  group: () => { const tours = getTours(); const g = (S.selId && tourOf(S.selId)) || (tours.length === 1 ? tours[0] : null); return g?.index != null ? g : null; },
});
initPanoramax({
  t, toast, openModal, closeMenus, setAppBusy, scheduleAutosave, photoWithPose, isSample: isSamplePhoto, scopes: tourScopes,
  ensureTimes: async () => { for (const n of S.nodes) if (n.photoFile) await ensureCaptureTime(n); },
});
initPhotoTools({
  S, t, toast, closeMenus, openModal, closeModal, storageGet, storageSet, tourScopes, defaultScope, isSample: isSamplePhoto, pushUndo,
  tourChanged, rewritePhoto, replacePhoto, scopeNodes, scopeLabel, ensureCaptureTime, fixedNorth, resetPanorama: () => { panoNodeId = null; },
});
initExports({
  S, t, toast, gn, saveTextFile, fileBase, walkingOrder, getLinkedNodes, isSample: isSamplePhoto, safeFileName, photoWithPose, setSaveStatus,
  projectBase: () => projectName?.replace(/\.vrtour$/i, ''),
});
initDialogs();

// ── Info points (hotspots) ────────────────────────
let hsEditing = null; // { nodeId, id } of the point in the dialog

function renderHotspots(n) {
  const list = document.getElementById('hs-list');
  const items = n.hotspots || [];
  document.getElementById('hs-count').textContent = items.length || '';
  list.innerHTML = items.length ? '' : `<div class="mini" style="margin:0 0 6px">${t('No info points yet.')}</div>`;
  items.forEach(h => {
    const row = document.createElement('div');
    row.className = 'hs-row';
    row.innerHTML = `<span class="t" title="${escapeHtml(t('Look at it'))}">ⓘ ${escapeHtml(h.title || t('(no title)'))}</span><button class="e" title="${escapeHtml(t('Edit'))}">✎</button><button class="x" title="${escapeHtml(t('Delete'))}">×</button>`;
    row.querySelector('.t').onclick = () => panoViewer?.rotate({ yaw: h.yaw, pitch: 0 });
    row.querySelector('.e').onclick = () => openHotspot(n, h);
    row.querySelector('.x').onclick = () => deleteHotspot(n, h.id);
    list.appendChild(row);
  });
  document.getElementById('hs-add').disabled = !n.photoUrl;
}

function refreshPanoMarkers(n) {
  const plugin = panoViewer?.getPlugin(MarkersPlugin);
  if (!plugin) return;
  plugin.setMarkers((n?.hotspots || []).map(h => ({
    id: h.id,
    position: { yaw: h.yaw, pitch: h.pitch },
    html: '<div class="hs-pin">i</div>',
    anchor: 'center center',
    tooltip: escapeHtml(h.title || ''),
  })));
}

function openHotspot(n, h = null) {
  if (!h && !panoViewer) return;
  const yaw = h ? h.yaw : lockViewerPitch(panoViewer).yaw;
  hsEditing = { nodeId: n.id, id: h?.id || null, yaw };
  document.getElementById('hs-title-h').textContent = h ? t('Edit info point') : t('New info point');
  document.getElementById('hs-title').value = h?.title || '';
  document.getElementById('hs-text').value = h?.text || '';
  document.getElementById('hs-url').value = h?.url || '';
  const pitch = document.getElementById('hs-pitch');
  pitch.value = h ? Math.round(radToDeg(h.pitch)) : 0;
  pitch.dispatchEvent(new Event('input'));
  document.getElementById('hs-delete').style.display = h ? '' : 'none';
  openModal('hs-back');
  document.getElementById('hs-title').focus();
}

document.getElementById('hs-pitch').addEventListener('input', e => {
  document.getElementById('hs-pitch-v').textContent = `${e.target.value}°`;
});
document.getElementById('hs-add').onclick = () => { const n = gn(S.selId); if (n) openHotspot(n); };

document.getElementById('hs-save').onclick = () => {
  const n = gn(hsEditing?.nodeId);
  if (!n) return;
  pushUndo();
  const data = {
    title: document.getElementById('hs-title').value.trim(),
    text: document.getElementById('hs-text').value.trim(),
    url: document.getElementById('hs-url').value.trim(),
    pitch: degToRad(Number(document.getElementById('hs-pitch').value)),
  };
  n.hotspots = n.hotspots || [];
  const existing = n.hotspots.find(h => h.id === hsEditing.id);
  if (existing) Object.assign(existing, data);
  else n.hotspots.push({ id: `h${S.nxt++}`, yaw: hsEditing.yaw, ...data });
  closeModal('hs-back');
  renderHotspots(n);
  refreshPanoMarkers(n);
  scheduleAutosave();
};

function deleteHotspot(n, id) {
  pushUndo();
  n.hotspots = (n.hotspots || []).filter(h => h.id !== id);
  renderHotspots(n);
  refreshPanoMarkers(n);
  scheduleAutosave();
}
document.getElementById('hs-delete').onclick = () => {
  const n = gn(hsEditing?.nodeId);
  closeModal('hs-back');
  if (n) deleteHotspot(n, hsEditing.id);
};

// Markers for the exported viewer (PSV MarkersPlugin config).
function hotspotMarkers(n) {
  return (n.hotspots || []).map(h => {
    const link = /^https?:\/\//i.test(h.url || '')
      ? `<p><a href="${escapeHtml(h.url)}" target="_blank" rel="noopener">${escapeHtml(h.url)}</a></p>` : '';
    return {
      id: h.id,
      position: { yaw: +normalizeRad(h.yaw + degToRad(n.heading ?? 0)).toFixed(4), pitch: +h.pitch.toFixed(4) },
      html: '<div class="hs-pin">i</div>',
      anchor: 'center center',
      tooltip: escapeHtml(h.title || ''),
      content: `<h2>${escapeHtml(h.title || '')}</h2><p>${escapeHtml(h.text || '').replace(/\n/g, '<br>')}</p>${link}`,
    };
  });
}

document.getElementById('bn-blur').onclick = () => openBlurTool(S.selId);

function dl(content, name) {
  const a = document.createElement('a');
  const url = URL.createObjectURL(content instanceof Blob ? content : new Blob([content], { type: 'text/plain' }));
  a.href = url;
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 60000);
}

document.documentElement.lang = currentLang;
document.getElementById('lang-label').textContent = currentLang === 'de' ? 'English' : 'Deutsch';
translateDom();
i18nObserver.observe(document.body, { childList: true, subtree: true, characterData: true });
updateStats();
updateTitle();
checkSavedSession();
initAppInfo().then(renderRecent);
placesConfigured();
openPendingProject();
setTimeout(() => checkForUpdates(), 2500);
// First start: the tour, once nothing else (restore prompt, a dialog) asks for
// attention - unless an automated browser (tests, screenshots) drives the editor.
if (!storageGet('tourDone') && !navigator.webdriver) {
  const startWhenCalm = () => {
    if (storageGet('tourDone')) return;
    if (document.querySelector('.modal-back.show, .restore-banner.show, .update-banner.show')) setTimeout(startWhenCalm, 1000);
    else startTour();
  };
  setTimeout(startWhenCalm, 900);
}
