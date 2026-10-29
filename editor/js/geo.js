// Distances and directions on the map, and a small flat map in metres around a point
// (east = x, north = y) for moving, turning and snapping points.
import { radToDeg } from './util.js';

export const M_PER_DEG = 111320;

export function distanceMeters(a, b) {
  const R = 6371000;
  const lat1 = a.lat * Math.PI / 180;
  const lat2 = b.lat * Math.PI / 180;
  const dLat = (b.lat - a.lat) * Math.PI / 180;
  const dLng = (b.lng - a.lng) * Math.PI / 180;
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(lat1) * Math.cos(lat2) * Math.sin(dLng / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(h));
}

export function formatDistance(m) {
  return m < 1000 ? `${Math.round(m)} m` : `${(m / 1000).toFixed(2)} km`;
}

export function normalizeHeading(v) {
  if (v == null || Number.isNaN(v)) return null;
  return ((v % 360) + 360) % 360;
}

export function bearingDeg(from, to) {
  const lat1 = from.lat * Math.PI / 180;
  const lat2 = to.lat * Math.PI / 180;
  const dLng = (to.lng - from.lng) * Math.PI / 180;
  const y = Math.sin(dLng) * Math.cos(lat2);
  const x = Math.cos(lat1) * Math.sin(lat2) - Math.sin(lat1) * Math.cos(lat2) * Math.cos(dLng);
  return normalizeHeading(radToDeg(Math.atan2(y, x)));
}

// How far apart two compass directions are (0-180°).
export function headingDiff(a, b) {
  return Math.abs(((a - b) % 360 + 540) % 360 - 180);
}

export const toLocal = (p, o) => ({ x: (p.lng - o.lng) * M_PER_DEG * Math.cos((o.lat * Math.PI) / 180), y: (p.lat - o.lat) * M_PER_DEG });
export const fromLocal = (q, o) => ({ lat: o.lat + q.y / M_PER_DEG, lng: o.lng + q.x / (M_PER_DEG * Math.cos((o.lat * Math.PI) / 180)) });

export function offsetEast(p, meters) {
  return { lat: p.lat, lng: p.lng + meters / (M_PER_DEG * Math.cos(p.lat * Math.PI / 180)) };
}
