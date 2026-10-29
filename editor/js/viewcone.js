// The view on the map: from the selected point, a cone pointing the way the panorama in
// the side panel looks, as wide as it shows. Turning the panorama turns the cone - so
// north can be checked against the satellite map. Without north (or to correct it),
// drag the cone's tip onto what the panorama shows: that sets the photo's north.
import { normalizeHeading } from './geo.js';

const LENGTH = 120; // pixels on the map
let ctx; // from the editor: { map, node(), view(), setHeading(n, heading), t }
let cone = null, tip = null, drag = null;

export function initViewCone(c) {
  ctx = c;
  ctx.map.on('zoomend moveend resize', () => { if (!drag) updateViewCone(); });
}

function pointAt(n, deg, px) {
  const { map } = ctx;
  const p0 = map.latLngToContainerPoint([n.lat, n.lng]);
  const r = (deg * Math.PI) / 180;
  return map.containerPointToLatLng(L.point(p0.x + px * Math.sin(r), p0.y - px * Math.cos(r)));
}

function remove() {
  cone?.remove();
  tip?.remove();
  cone = tip = null;
}

function draw(n, dir, fov, known) {
  const half = Math.min(170, fov) / 2;
  const arc = [];
  for (let k = 0; k <= 10; k++) arc.push(pointAt(n, dir - half + (2 * half * k) / 10, LENGTH));
  const pts = [[n.lat, n.lng], ...arc];
  const cls = `view-cone${known ? '' : ' unknown'}`;
  if (!cone) {
    cone = L.polygon(pts, { className: cls, interactive: false, weight: 1.5 }).addTo(ctx.map);
  } else {
    cone.setLatLngs(pts);
    cone.getElement()?.setAttribute('class', cls);
  }
  const at = pointAt(n, dir, LENGTH);
  if (!tip) {
    tip = L.marker(at, {
      draggable: true, zIndexOffset: 600, keyboard: false,
      icon: L.divIcon({ html: '<div class="view-tip"></div>', className: '', iconSize: [18, 18], iconAnchor: [9, 9] }),
    }).addTo(ctx.map);
    tip.on('click', e => L.DomEvent.stopPropagation(e));
    tip.on('dragstart', () => { drag = { n: ctx.node() }; });
    tip.on('drag', () => {
      const node = drag?.n;
      const view = ctx.view();
      if (!node || !view) return;
      drag.dir = directionTo(node, tip.getLatLng());
      draw(node, drag.dir, view.hFov, true);
    });
    tip.on('dragend', () => {
      const { n: node, dir: d } = drag || {};
      drag = null;
      const view = ctx.view();
      if (node && view && d != null) ctx.setHeading(node, normalizeHeading(d - view.yawDeg));
      updateViewCone();
    });
    tip.bindTooltip(ctx.t('Drag onto what you see in the panorama - that sets north'), { direction: 'top', offset: [0, -8] });
  } else if (!drag) {
    tip.setLatLng(at);
  }
  tip.getElement()?.classList.toggle('unknown', !known);
}

// The direction from a point to a spot on the map, as the screen shows it.
function directionTo(n, latlng) {
  const { map } = ctx;
  const a = map.latLngToContainerPoint([n.lat, n.lng]);
  const b = map.latLngToContainerPoint(latlng);
  return normalizeHeading((Math.atan2(b.x - a.x, a.y - b.y) * 180) / Math.PI);
}

// Draw the cone for the selected point and the panorama's view, or take it away.
export function updateViewCone() {
  if (drag) return;
  const n = ctx.node();
  const view = n && ctx.view();
  if (!n || !view) { remove(); return; }
  const known = n.heading != null;
  draw(n, normalizeHeading((known ? n.heading : 0) + view.yawDeg), view.hFov, known);
}
