// The photos of a tour as a strip along the bottom of the map, in walking order from
// its start: click one to go there; J / K step forward and back. Each shows what is
// still open for it (no north, not checked for faces and plates).
import { escapeHtml } from './util.js';

let ctx; // from the editor: { group(), order(nodes), selId(), jump(id), thumb(n), storageGet, storageSet, t }
let shown = []; // the nodes in the strip, in order
const strip = () => document.getElementById('filmstrip');
const list = () => document.getElementById('fs-list');

export function initFilmstrip(c) {
  ctx = c;
  strip().classList.toggle('closed', ctx.storageGet('filmstrip') === '0');
  document.getElementById('fs-toggle').onclick = () => {
    const closed = strip().classList.toggle('closed');
    ctx.storageSet('filmstrip', closed ? '0' : '1');
    renderFilmstrip();
  };
  list().addEventListener('click', e => {
    const item = e.target.closest('[data-fs]');
    if (item) ctx.jump(item.dataset.fs);
  });
}

export function renderFilmstrip() {
  const group = ctx.group();
  const el = strip();
  el.hidden = !group || group.nodes.length < 2;
  if (el.hidden) { shown = []; return; }
  shown = ctx.order(group.nodes);
  document.getElementById('fs-count').textContent = `${shown.length}`;
  if (el.classList.contains('closed')) return;
  const sel = ctx.selId();
  list().innerHTML = shown.map((n, i) => {
    const src = ctx.thumb(n);
    const open = [n.photoFile && n.heading == null && ctx.t('no north'), n.photoFile && !n.blurred && !n.privacyChecked && ctx.t('faces & plates not checked')].filter(Boolean);
    return `<button class="fs-item${n.id === sel ? ' sel' : ''}${open.length ? ' open' : ''}" data-fs="${escapeHtml(n.id)}" title="${escapeHtml([n.name, ...open].join(' · '))}">`
      + (n.photoFile ? `<img data-thumb="${escapeHtml(n.id)}" alt=""${src ? ` src="${src}"` : ' hidden'}>` : '<span class="fs-none"></span>')
      + `<span class="fs-i">${i + 1}</span></button>`;
  }).join('');
  list().querySelector('.fs-item.sel')?.scrollIntoView({ block: 'nearest', inline: 'center' });
}

// The next (delta 1) or previous (-1) point of the strip; the first one if none is selected.
export function stepFilmstrip(delta) {
  if (!shown.length) return null;
  const i = shown.findIndex(n => n.id === ctx.selId());
  const next = i < 0 ? shown[0] : shown[Math.max(0, Math.min(shown.length - 1, i + delta))];
  return next.id;
}
