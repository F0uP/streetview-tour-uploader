// Small helpers shared by the editor and the tour viewer it generates.

export function radToDeg(v) { return v * 180 / Math.PI; }

export function degToRad(v) { return v * Math.PI / 180; }

export function escapeHtml(s) {
  return `${s ?? ''}`.replace(/[&<>"']/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));
}

export function normalizeRad(v) {
  const twoPi = Math.PI * 2;
  return ((v + Math.PI) % twoPi + twoPi) % twoPi - Math.PI;
}
