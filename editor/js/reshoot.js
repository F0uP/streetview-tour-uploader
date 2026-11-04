// New photos for points already on Google - a re-shoot, because the place has changed.
// Each new photo is matched with the nearest photo on Google and takes its place: with
// its links and its place on Google Maps, and (as chosen) its position and north. The
// next upload sends the new photo, links it like the old one and deletes the old one -
// the tour on Google stays as it was, with new pictures.
import { distanceMeters, formatDistance } from './geo.js';
import { escapeHtml } from './util.js';

// From the editor (initReshoot): { S, t, toast, closeMenus, openModal, closeModal, pushUndo, retire(n), moveLinks(from, to),
// removeNode(id), isSample(n), tourChanged, refreshConnLines, resetPanorama }
let ctx;
let pairs = [];
const el = id => document.getElementById(id);

export function initReshoot(c) {
  ctx = c;
  el('rs-dist').addEventListener('input', renderPairs);
  el('rs-go').onclick = apply;
}

export function openReshoot() {
  ctx.closeMenus();
  if (!ctx.S.nodes.some(n => n.gsvPhotoId)) {
    alert(ctx.t('There are no photos on Google in this project. Load them first (Tools → Photos from Google…) or open the project you uploaded them from.'));
    return;
  }
  ctx.openModal('reshoot-back');
  renderPairs();
}

// Each new photo (not on Google) with the nearest photo on Google, closest pairs first, each used once.
function match(maxDist) {
  const fresh = ctx.S.nodes.filter(n => n.photoFile && !n.gsvPhotoId && !ctx.isSample(n));
  const old = ctx.S.nodes.filter(n => n.gsvPhotoId && n.gsvStatus !== 'missing');
  const all = [];
  fresh.forEach(a => old.forEach(b => {
    const d = distanceMeters(a, b);
    if (d <= maxDist) all.push({ fresh: a, old: b, d });
  }));
  all.sort((x, y) => x.d - y.d);
  const usedFresh = new Set(), usedOld = new Set(), out = [];
  all.forEach(p => {
    if (usedFresh.has(p.fresh) || usedOld.has(p.old)) return;
    usedFresh.add(p.fresh);
    usedOld.add(p.old);
    out.push(p);
  });
  return out;
}

function renderPairs() {
  const maxDist = Math.max(0.5, Number(el('rs-dist').value) || 5);
  pairs = match(maxDist);
  const list = el('rs-list');
  list.innerHTML = pairs.length ? '' : `<div class="check-ok" style="color:var(--text-dim)">${escapeHtml(ctx.t('No new photo lies that close to a photo on Google.'))}</div>`;
  pairs.forEach((p, i) => {
    const row = document.createElement('div');
    row.className = 'check-row ok';
    row.innerHTML = `<span class="lv"></span><span class="nm">${escapeHtml(p.fresh.name)}</span>`
      + `<span class="msg">${escapeHtml(ctx.t('replaces {name} on Google ({d} apart)', { name: p.old.name, d: formatDistance(p.d) }))}</span>`
      + `<input type="checkbox" data-pair="${i}" checked>`;
    list.appendChild(row);
  });
  el('rs-go').disabled = !pairs.length;
  el('rs-go').textContent = ctx.t('Replace {n} photo(s)', { n: pairs.length });
}

function apply() {
  const chosen = [...el('rs-list').querySelectorAll('input[data-pair]:checked')].map(box => pairs[Number(box.dataset.pair)]);
  if (!chosen.length) return;
  const keepPosition = el('rs-keep-pos').checked;
  ctx.pushUndo();
  chosen.forEach(({ fresh, old }) => {
    Object.assign(old, {
      photoFile: fresh.photoFile, photoUrl: fresh.photoUrl, photoName: fresh.photoName,
      captureTime: fresh.captureTime, captureOffset: fresh.captureOffset,
      blurred: !!fresh.blurred, privacyChecked: !!fresh.privacyChecked, pitch: fresh.pitch, roll: fresh.roll,
    });
    if (!keepPosition) {
      Object.assign(old, { lat: fresh.lat, lng: fresh.lng, hasGps: fresh.hasGps });
      old.marker?.setLatLng([old.lat, old.lng]);
    }
    // North: the new photo's own, where it has one; else it is taken where the old one faced.
    if (fresh.heading != null) Object.assign(old, { heading: fresh.heading, headingSource: fresh.headingSource, viewYaw: fresh.viewYaw });
    ctx.retire(old); // the old photo becomes an old version: deleted on Google with the next upload
    ctx.moveLinks(fresh.id, old.id);
    ctx.removeNode(fresh.id);
  });
  ctx.resetPanorama();
  ctx.refreshConnLines();
  ctx.closeModal('reshoot-back');
  ctx.tourChanged();
  ctx.toast(ctx.t('{n} photo(s) replaced - the next upload puts them on Google and deletes the old ones.', { n: chosen.length }));
}
