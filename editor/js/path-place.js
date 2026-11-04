// Photos without GPS, placed along the way walked: draw the path on the map, click by click
// (double-click or Enter finishes, Backspace takes the last point back, Esc cancels), and the
// photos are spread along it in the order they were taken - evenly, or by their capture times
// (walking at an even pace). Optionally linked in that order and turned to north from the
// direction of the path where each one stands.
import { bearingDeg, distanceMeters, normalizeHeading } from './geo.js';
import { degToRad, normalizeRad } from './util.js';

let ctx; // from the editor: { map, t, toast, closeMenus, scope(), pushUndo, ensureCaptureTime, link(a, b), tourChanged, refreshConnLines }
const draw = { on: false, pts: [], line: null, ghost: null, nodes: [] };
const el = id => document.getElementById(id);
export const isDrawingPath = () => draw.on;

export function initPathPlace(c) {
  ctx = c;
  const { map } = ctx;
  map.on('click', e => {
    if (!draw.on) return;
    const p = e.latlng.wrap();
    const last = draw.pts[draw.pts.length - 1];
    // The two clicks of a double-click: one point only.
    if (last && map.latLngToContainerPoint(last).distanceTo(map.latLngToContainerPoint(p)) < 4) return;
    draw.pts.push(p);
    redraw();
  });
  map.on('dblclick', () => { if (draw.on) finish(); });
  map.on('mousemove', e => {
    if (!draw.on || !draw.pts.length) return;
    const last = draw.pts[draw.pts.length - 1];
    if (!draw.ghost) draw.ghost = L.polyline([last, e.latlng], { color: '#f6b93b', weight: 2, dashArray: '5 6', interactive: false }).addTo(map);
    else draw.ghost.setLatLngs([last, e.latlng]);
  });
  // While drawing, these keys are the path's (they don't reach the editor).
  addEventListener('keydown', e => {
    if (!draw.on || e.target.closest?.('input, select, textarea')) return;
    if (e.key === 'Escape') cancel();
    else if (e.key === 'Enter') finish();
    else if (e.key === 'Backspace' || e.key === 'Delete') { draw.pts.pop(); redraw(); }
    else return;
    e.preventDefault();
    e.stopPropagation();
  }, true);
  el('path-undo').onclick = () => { draw.pts.pop(); redraw(); };
  el('path-cancel').onclick = cancel;
  el('path-done').onclick = finish;
}

export function openPathPlace() {
  ctx.closeMenus();
  const { nodes, label } = ctx.scope();
  if (nodes.length < 2) {
    alert(ctx.t('Select at least two points first (Ctrl+click), or one point of the tour.'));
    return;
  }
  Object.assign(draw, { on: true, pts: [], nodes });
  el('path-text').textContent = ctx.t('{label}: click along the way you walked, double-click to finish.', { label });
  el('path-bar').hidden = false;
  ctx.map.getContainer().classList.add('drawing');
  redraw();
}

function redraw() {
  const { map } = ctx;
  if (!draw.line) draw.line = L.polyline([], { color: '#f6b93b', weight: 4, opacity: 0.9, interactive: false }).addTo(map);
  draw.line.setLatLngs(draw.pts);
  if (!draw.pts.length) { draw.ghost?.remove(); draw.ghost = null; }
  el('path-done').disabled = draw.pts.length < 2;
  el('path-done').textContent = ctx.t('Place {n} photo(s)', { n: draw.nodes.length });
}

function stop() {
  draw.on = false;
  draw.line?.remove();
  draw.ghost?.remove();
  draw.line = draw.ghost = null;
  el('path-bar').hidden = true;
  ctx.map.getContainer().classList.remove('drawing');
}

function cancel() { stop(); }

// The spot `share` (0-1) of the way along the path, and which way the path goes there.
function along(pts, lengths, share) {
  const total = lengths[lengths.length - 1];
  const want = share * total;
  let i = 1;
  while (i < lengths.length - 1 && lengths[i] < want) i++;
  const a = pts[i - 1], b = pts[i];
  const seg = lengths[i] - lengths[i - 1] || 1;
  const f = Math.max(0, Math.min(1, (want - lengths[i - 1]) / seg));
  return { lat: a.lat + (b.lat - a.lat) * f, lng: a.lng + (b.lng - a.lng) * f, bearing: bearingDeg(a, b) };
}

async function finish() {
  if (!draw.on) return;
  if (draw.pts.length < 2) { ctx.toast(ctx.t('Draw at least two points of the path.')); return; }
  const pts = draw.pts.map(p => ({ lat: p.lat, lng: p.lng }));
  const nodes = draw.nodes;
  const byTime = document.querySelector('input[name="path-space"]:checked').value === 'time';
  const linkThem = el('path-link').checked;
  const north = el('path-north').checked;
  stop();

  // The walk: by capture time (photos without one keep their list order, at the end).
  for (const n of nodes) await ctx.ensureCaptureTime(n);
  const time = n => (n.captureTime ? Date.parse(`${n.captureTime}Z`) : NaN);
  const order = nodes.map((n, i) => ({ n, i, t: time(n) }))
    .sort((a, b) => ((Number.isFinite(a.t) ? a.t : Infinity) - (Number.isFinite(b.t) ? b.t : Infinity)) || a.i - b.i)
    .map(o => o.n);
  const lengths = [0];
  for (let i = 1; i < pts.length; i++) lengths.push(lengths[i - 1] + distanceMeters(pts[i - 1], pts[i]));
  const times = order.map(time);
  const timed = byTime && times.every(Number.isFinite) && times[times.length - 1] > times[0];
  ctx.pushUndo();
  order.forEach((n, i) => {
    const share = timed ? (times[i] - times[0]) / (times[times.length - 1] - times[0]) : i / (order.length - 1);
    const at = along(pts, lengths, share);
    n.lat = at.lat;
    n.lng = at.lng;
    n.marker?.setLatLng([n.lat, n.lng]);
    if (north && n.photoFile && (n.heading == null || n.headingSource === 'auto')) {
      n.heading = normalizeHeading(at.bearing);
      n.headingSource = 'auto';
      n.viewYaw = normalizeRad(-degToRad(n.heading));
    }
  });
  if (linkThem) for (let i = 0; i < order.length - 1; i++) ctx.link(order[i], order[i + 1]);
  ctx.refreshConnLines();
  ctx.tourChanged();
  ctx.toast(ctx.t('{n} photo(s) placed along the path ({d}).', { n: order.length, d: `${Math.round(lengths[lengths.length - 1])} m` }));
}
