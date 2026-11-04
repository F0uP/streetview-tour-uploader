// Floor plans on the map. Indoors there is no GPS: the points are placed on a plan of the
// building instead. A plan (an image) is laid on the map and fitted to the satellite picture -
// moved by its middle, turned and scaled by its corner - and can belong to a floor (then it
// shows with that floor's points). Plans are kept in the project (S.plans), image included.
import { distanceMeters, fromLocal, toLocal } from './geo.js';
import { escapeHtml } from './util.js';

// From the editor (initFloorPlans): { map, S, t, toast, closeMenus, openModal, closeModal, pushUndo, scheduleAutosave, floorView() }
let ctx;
const layers = new Map(); // plan id -> its layer
const urls = new WeakMap(); // image File -> object URL
let adjusting = null; // { id, centre: marker, corner: marker }
const el = id => document.getElementById(id);

const urlOf = file => {
  if (!urls.has(file)) urls.set(file, URL.createObjectURL(file));
  return urls.get(file);
};

// The corners top-left, top-right, bottom-left on the map: the plan is `width` metres wide,
// turned `rotation` degrees clockwise about its centre.
export function planCorners(p) {
  const w = p.width / 2, h = (p.width * p.aspect) / 2;
  const r = (p.rotation * Math.PI) / 180;
  const turn = (x, y) => fromLocal({ x: x * Math.cos(r) + y * Math.sin(r), y: -x * Math.sin(r) + y * Math.cos(r) }, p.center);
  return [turn(-w, h), turn(w, h), turn(-w, -h)];
}

// An image laid on the map by an affine transform (Leaflet's own overlay can't turn).
const PlanLayer = L.Layer.extend({
  options: { pane: 'plans' },
  initialize(plan) { this.plan = plan; },
  onAdd(map) {
    const img = this._img = L.DomUtil.create('img', 'plan-img leaflet-zoom-animated');
    img.alt = '';
    img.draggable = false;
    img.onload = () => this._update();
    img.src = urlOf(this.plan.image);
    this.getPane().appendChild(img);
    map.on('viewreset zoomend moveend', this._update, this);
    if (map._zoomAnimated) map.on('zoomanim', this._animate, this);
    this._update();
  },
  onRemove(map) {
    this._img.remove();
    map.off('viewreset zoomend moveend', this._update, this);
    map.off('zoomanim', this._animate, this);
  },
  setPlan(plan) { this.plan = plan; this._update(); },
  _matrix(toPoint) {
    const img = this._img;
    const w = img.naturalWidth, h = img.naturalHeight;
    if (!w || !h) return null;
    const [tl, tr, bl] = planCorners(this.plan).map(c => toPoint(L.latLng(c.lat, c.lng)));
    return `matrix(${(tr.x - tl.x) / w},${(tr.y - tl.y) / w},${(bl.x - tl.x) / h},${(bl.y - tl.y) / h},${tl.x},${tl.y})`;
  },
  _update() {
    if (!this._map) return;
    const m = this._matrix(ll => this._map.latLngToLayerPoint(ll));
    if (m) this._img.style.transform = m;
    this._img.style.opacity = this.plan.opacity;
  },
  _animate(e) {
    const m = this._matrix(ll => this._map._latLngToNewLayerPoint(ll, e.zoom, e.center));
    if (m) this._img.style.transform = m;
  },
});

export function initFloorPlans(c) {
  ctx = c;
  const pane = ctx.map.createPane('plans');
  pane.style.zIndex = 350; // above the map, below the links and points
  pane.style.pointerEvents = 'none';
  el('plan-file').addEventListener('change', async e => {
    const file = e.target.files[0];
    e.target.value = '';
    if (file) await addPlan(file);
  });
  el('plan-add').onclick = () => el('plan-file').click();
  el('plan-adjust-done').onclick = endAdjust;
}

export function openFloorPlans() {
  ctx.closeMenus();
  endAdjust();
  renderPlanList();
  ctx.openModal('plan-back');
}

async function addPlan(file) {
  const img = new Image();
  img.src = urlOf(file);
  try {
    await img.decode();
  } catch {
    alert(ctx.t('That image could not be read.'));
    return;
  }
  const { map } = ctx;
  const b = map.getBounds();
  const centre = map.getCenter();
  const across = distanceMeters({ lat: centre.lat, lng: b.getWest() }, { lat: centre.lat, lng: b.getEast() });
  ctx.pushUndo();
  const plan = {
    id: `p${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`,
    name: file.name.replace(/\.[^.]+$/, ''),
    image: file,
    center: { lat: centre.lat, lng: centre.lng },
    width: Math.max(5, Math.round(across * 0.6)),
    rotation: 0,
    aspect: img.naturalHeight / img.naturalWidth,
    opacity: 0.75,
    level: ctx.floorView(),
  };
  ctx.S.plans = [...ctx.S.plans, plan];
  renderPlans();
  ctx.scheduleAutosave();
  ctx.closeModal('plan-back');
  startAdjust(plan.id);
}

// The plans on the map as S.plans has them (after undo, opening a project, a floor change).
export function renderPlans() {
  const floor = ctx.floorView();
  const wanted = new Map(ctx.S.plans.map(p => [p.id, p]));
  layers.forEach((layer, id) => {
    if (!wanted.has(id)) { layer.remove(); layers.delete(id); }
  });
  wanted.forEach((plan, id) => {
    const shown = plan.level == null || floor == null || plan.level === floor;
    let layer = layers.get(id);
    if (layer && layer.plan.image !== plan.image) { layer.remove(); layers.delete(id); layer = null; }
    if (!layer) {
      layer = new PlanLayer(plan);
      layers.set(id, layer);
    }
    layer.setPlan(plan);
    if (shown && !ctx.map.hasLayer(layer)) layer.addTo(ctx.map);
    if (!shown && ctx.map.hasLayer(layer)) layer.remove();
  });
  if (adjusting && !wanted.has(adjusting.id)) endAdjust();
}

function planById(id) { return ctx.S.plans.find(p => p.id === id); }

function update(id, changes) {
  ctx.S.plans = ctx.S.plans.map(p => (p.id === id ? { ...p, ...changes } : p));
  renderPlans();
}

function renderPlanList() {
  const list = el('plan-list');
  list.innerHTML = ctx.S.plans.length ? '' : `<div class="mini">${escapeHtml(ctx.t('No floor plans yet.'))}</div>`;
  ctx.S.plans.forEach(p => {
    const row = document.createElement('div');
    row.className = 'plan-row';
    row.innerHTML = `<img src="${urlOf(p.image)}" alt=""><input class="pi plan-name" value="${escapeHtml(p.name)}" spellcheck="false">`
      + `<label class="plan-floor">${escapeHtml(ctx.t('Floor'))} <input class="pi" type="number" step="1" value="${p.level ?? ''}" placeholder="${escapeHtml(ctx.t('all'))}"></label>`
      + `<input class="slider" type="range" min="10" max="100" value="${Math.round(p.opacity * 100)}" title="${escapeHtml(ctx.t('How much the plan covers the map'))}">`
      + `<button class="btn" data-adjust>${escapeHtml(ctx.t('Fit on the map'))}</button><button class="btn danger" data-remove>${escapeHtml(ctx.t('Remove'))}</button>`;
    const [name, floor, opacity] = row.querySelectorAll('input');
    name.onchange = () => { ctx.pushUndo(); update(p.id, { name: name.value.trim() || p.name }); ctx.scheduleAutosave(); };
    floor.onchange = () => {
      ctx.pushUndo();
      update(p.id, { level: floor.value === '' ? null : Math.round(Number(floor.value)) });
      ctx.scheduleAutosave();
    };
    opacity.onpointerdown = () => ctx.pushUndo();
    opacity.oninput = () => update(p.id, { opacity: Number(opacity.value) / 100 });
    opacity.onchange = () => ctx.scheduleAutosave();
    row.querySelector('[data-adjust]').onclick = () => { ctx.closeModal('plan-back'); startAdjust(p.id); };
    row.querySelector('[data-remove]').onclick = () => {
      if (!confirm(ctx.t('Remove the floor plan "{name}" from the map?', { name: p.name }))) return;
      ctx.pushUndo();
      ctx.S.plans = ctx.S.plans.filter(x => x.id !== p.id);
      renderPlans();
      renderPlanList();
      ctx.scheduleAutosave();
    };
    list.appendChild(row);
  });
}

// Fitting on the map: the middle moves the plan, the corner turns and scales it.
function startAdjust(id) {
  endAdjust();
  const plan = planById(id);
  if (!plan) return;
  ctx.pushUndo();
  const handle = cls => L.divIcon({ html: `<div class="plan-handle ${cls}"></div>`, className: '', iconSize: [20, 20], iconAnchor: [10, 10] });
  const centre = L.marker([plan.center.lat, plan.center.lng], { draggable: true, icon: handle('move'), zIndexOffset: 900 }).addTo(ctx.map);
  const [, tr] = planCorners(plan);
  const corner = L.marker([tr.lat, tr.lng], { draggable: true, icon: handle('turn'), zIndexOffset: 900 }).addTo(ctx.map);
  [centre, corner].forEach(m => m.on('click', e => L.DomEvent.stopPropagation(e)));
  centre.on('drag', () => {
    const p = centre.getLatLng();
    update(id, { center: { lat: p.lat, lng: p.lng } });
    const [, c] = planCorners(planById(id));
    corner.setLatLng([c.lat, c.lng]);
  });
  corner.on('drag', () => {
    const p = planById(id);
    const q = toLocal(corner.getLatLng(), p.center);
    // Unturned, the top-right corner lies at atan2(1, aspect) clockwise from north.
    const rotation = ((Math.atan2(q.x, q.y) - Math.atan2(1, p.aspect)) * 180) / Math.PI;
    const width = (2 * Math.hypot(q.x, q.y)) / Math.sqrt(1 + p.aspect * p.aspect);
    update(id, { rotation: ((rotation % 360) + 360) % 360, width: Math.max(1, width) });
  });
  adjusting = { id, centre, corner };
  el('plan-adjust-bar').hidden = false;
  ctx.map.fitBounds(L.latLngBounds(planCorners(plan).map(c => [c.lat, c.lng])).pad(0.2));
}

function endAdjust() {
  if (!adjusting) return;
  adjusting.centre.remove();
  adjusting.corner.remove();
  adjusting = null;
  el('plan-adjust-bar').hidden = true;
  ctx.scheduleAutosave();
}
