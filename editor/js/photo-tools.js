// Tools that work on the photos themselves: covering the tripod at the bottom of every
// photo (nadir), and north from where the sun stood when each photo was taken.
import { degToRad, escapeHtml, normalizeRad } from './util.js';
import { normalizeHeading } from './geo.js';
import { parseOffsetHours } from './gpx.js';
import { NADIR_SIZES, coverNadir, lookDown } from './photo-edit.js';
import { northFromSun } from './sun.js';

// From the editor (initPhotoTools).
let S, t, toast, closeMenus, openModal, closeModal, storageGet, storageSet, tourScopes, defaultScope, isSample, pushUndo,
  tourChanged, rewritePhoto, replacePhoto, scopeNodes, scopeLabel, ensureCaptureTime, fixedNorth, resetPanorama;
export function initPhotoTools(ctx) {
  ({ S, t, toast, closeMenus, openModal, closeModal, storageGet, storageSet, tourScopes, defaultScope, isSample, pushUndo,
    tourChanged, rewritePhoto, replacePhoto, scopeNodes, scopeLabel, ensureCaptureTime, fixedNorth, resetPanorama } = ctx);
}

// ── Cover the tripod (nadir) ──────────────────────
const nadir = { logo: null, gen: 0 };
const NADIR_LABELS = ['Small', 'Medium', 'Large', 'Very large'];
const nadirOptions = () => ({
  size: Number(document.getElementById('nadir-size').value) || NADIR_SIZES[1],
  style: document.querySelector('input[name="nadir-style"]:checked').value,
  logo: nadir.logo,
});
let nadirScopes = [];

export function openNadir() {
  closeMenus();
  if (!S.nodes.some(n => n.photoFile)) { alert(t('Add photos first.')); return; }
  const size = document.getElementById('nadir-size');
  const keep = size.value || storageGet('nadirSize') || String(NADIR_SIZES[1]);
  size.innerHTML = NADIR_SIZES.map((v, i) => `<option value="${v}">${escapeHtml(t(NADIR_LABELS[i]))} (${v}°)</option>`).join('');
  size.value = keep;
  nadirScopes = tourScopes().map(sc => ({ ...sc, nodes: sc.nodes.filter(n => n.photoFile && !isSample(n)) })).filter(sc => sc.nodes.length);
  const sel = document.getElementById('nadir-scope');
  sel.innerHTML = nadirScopes.map(sc => `<option value="${sc.key}">${escapeHtml(sc.label)} · ${escapeHtml(t('{n} photo(s)', { n: sc.nodes.length }))}</option>`).join('');
  sel.value = defaultScope() && nadirScopes.some(sc => sc.key === defaultScope()) ? defaultScope() : nadirScopes[0]?.key;
  document.getElementById('nadir-status').textContent = '';
  openModal('nadir-back');
  renderNadirPreview();
}

// The selected photo (or the first one it is for), looking straight down, with the cover.
async function renderNadirPreview() {
  const gen = ++nadir.gen;
  const scope = nadirScopes.find(sc => sc.key === document.getElementById('nadir-scope').value);
  const n = scope?.nodes.find(x => x.id === S.selId) || scope?.nodes[0];
  const out = document.getElementById('nadir-canvas');
  if (!n) return;
  try {
    const bmp = await createImageBitmap(n.photoFile, { resizeWidth: 2048, resizeHeight: 1024, resizeQuality: 'medium' });
    if (gen !== nadir.gen) { bmp.close?.(); return; }
    const canvas = new OffscreenCanvas(2048, 1024);
    const ctx = canvas.getContext('2d', { willReadFrequently: true });
    ctx.drawImage(bmp, 0, 0);
    bmp.close?.();
    coverNadir(ctx, 2048, 1024, nadirOptions());
    lookDown(ctx, 2048, 1024, out);
    document.getElementById('nadir-view-note').textContent = t('Looking straight down: {name}', { name: n.name });
  } catch (err) {
    document.getElementById('nadir-status').textContent = t('Could not load the photo: {msg}', { msg: err.message || err });
  }
}
['nadir-size', 'nadir-scope'].forEach(id => document.getElementById(id).addEventListener('change', () => {
  storageSet('nadirSize', document.getElementById('nadir-size').value);
  renderNadirPreview();
}));
document.querySelectorAll('input[name="nadir-style"]').forEach(r => r.addEventListener('change', renderNadirPreview));
document.getElementById('nadir-logo').addEventListener('change', async e => {
  const file = e.target.files[0];
  if (!file) return;
  try {
    const img = new Image();
    img.src = URL.createObjectURL(file);
    await img.decode();
    nadir.logo = await createImageBitmap(img);
    URL.revokeObjectURL(img.src);
    document.querySelector('input[name="nadir-style"][value="logo"]').checked = true;
    renderNadirPreview();
  } catch {
    alert(t('That image could not be read.'));
  }
});

document.getElementById('nadir-go').onclick = async () => {
  const opts = nadirOptions();
  if (opts.style === 'logo' && !opts.logo) { alert(t('Choose the logo first.')); return; }
  const scope = nadirScopes.find(sc => sc.key === document.getElementById('nadir-scope').value);
  const nodes = scope?.nodes || [];
  if (!nodes.length) return;
  const btn = document.getElementById('nadir-go');
  const bar = document.getElementById('nadir-progress');
  const status = document.getElementById('nadir-status');
  btn.disabled = true;
  bar.hidden = false;
  pushUndo();
  let done = 0;
  try {
    for (const [i, n] of nodes.entries()) {
      status.textContent = t('Photo {a} of {b}…', { a: i + 1, b: nodes.length });
      bar.firstElementChild.style.width = `${(i / nodes.length) * 100}%`;
      replacePhoto(n, await rewritePhoto(n, null, (ctx, W, H) => coverNadir(ctx, W, H, opts)));
      done++;
    }
    status.textContent = t('The tripod is covered in {n} photo(s).', { n: done });
    toast(t('The tripod is covered in {n} photo(s).', { n: done }));
  } catch (err) {
    status.textContent = t('Covering failed: {msg}', { msg: err.message || err });
  } finally {
    btn.disabled = false;
    bar.hidden = true;
    tourChanged();
  }
};

// ── North from the sun ────────────────────────────
let sunResults = [];

export function openSunNorth() {
  closeMenus();
  const list = scopeNodes().filter(n => n.photoFile);
  if (!list.length) { alert(t('Add photos first.')); return; }
  document.getElementById('sun-scope').textContent = scopeLabel(list);
  const withOffset = list.find(n => n.captureOffset);
  document.getElementById('sun-offset').value = withOffset ? parseOffsetHours(withOffset.captureOffset) : -new Date().getTimezoneOffset() / 60;
  document.getElementById('sun-list').innerHTML = '';
  document.getElementById('sun-status').textContent = '';
  document.getElementById('sun-actions').hidden = true;
  openModal('sun-back');
}

document.getElementById('sun-go').onclick = async () => {
  const list = scopeNodes().filter(n => n.photoFile);
  const overwrite = document.getElementById('sun-overwrite').checked;
  const zone = Number(document.getElementById('sun-offset').value || 0);
  const btn = document.getElementById('sun-go');
  const status = document.getElementById('sun-status');
  const box = document.getElementById('sun-list');
  btn.disabled = true;
  box.innerHTML = '';
  sunResults = [];
  try {
    for (const [i, n] of list.entries()) {
      status.textContent = t('Looking for the sun… {i}', { i: `${i + 1}/${list.length}` });
      const local = await ensureCaptureTime(n);
      let r;
      if (!local) r = { reason: 'time' };
      else {
        const hours = n.captureOffset ? parseOffsetHours(n.captureOffset) : zone;
        r = await northFromSun(n.photoFile, new Date(Date.parse(`${local}Z`) - hours * 3600000), n.lat, n.lng);
      }
      sunResults.push({ node: n, ...r });
    }
  } catch (err) {
    status.textContent = t('Comparing failed: {msg}', { msg: err.message || err });
    btn.disabled = false;
    return;
  }
  btn.disabled = false;
  const found = sunResults.filter(r => r.heading != null);
  status.textContent = t('{a} photo(s) with a clear sun, {b} uncertain, {c} without a usable sun.', {
    a: found.filter(r => r.clear).length, b: found.filter(r => !r.clear).length, c: sunResults.length - found.length,
  });
  const reasons = { time: 'no capture time', low: 'the sun was below the horizon', none: 'no sun in the photo (overcast?)' };
  sunResults.forEach(r => {
    const row = document.createElement('div');
    const n = r.node;
    const tick = r.heading != null && r.clear && (overwrite || !fixedNorth(n));
    row.className = `check-row ${r.heading == null ? 'info' : r.clear ? 'ok' : 'warn'}`;
    const before = n.heading == null ? t('none so far') : t('so far {h}°', { h: n.heading.toFixed(1) });
    const msg = r.heading == null ? t(reasons[r.reason])
      : `${r.heading.toFixed(1)}° (${before}) · ${r.clear ? t('clear match') : t('uncertain')} · ${t('sun at {az}°, {alt}° high', { az: Math.round(r.sunAz), alt: Math.round(r.sunAlt) })}`;
    row.innerHTML = `<span class="lv"></span><span class="nm">${escapeHtml(n.name)}</span><span class="msg">${escapeHtml(msg)}</span>`
      + (r.heading != null ? `<input type="checkbox"${tick ? ' checked' : ''}>` : '');
    row.dataset.node = n.id;
    box.appendChild(row);
  });
  document.getElementById('sun-actions').hidden = !found.length;
};

document.getElementById('sun-apply').onclick = () => {
  const chosen = [...document.querySelectorAll('#sun-list .check-row')]
    .filter(row => row.querySelector('input')?.checked)
    .map(row => sunResults.find(r => r.node.id === row.dataset.node))
    .filter(r => r?.heading != null);
  if (!chosen.length) return;
  pushUndo();
  chosen.forEach(({ node: n, heading }) => {
    n.heading = normalizeHeading(heading);
    n.headingSource = 'sun';
    n.viewYaw = normalizeRad(-degToRad(n.heading));
  });
  resetPanorama();
  closeModal('sun-back');
  tourChanged();
  toast(t('Set the heading of {n} photo(s)', { n: chosen.length }));
};
