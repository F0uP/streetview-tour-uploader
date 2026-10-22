// GPX tracks: where the photographer was at a photo's capture time.

export function parseOffsetHours(s) {
  const m = /^([+-])(\d{1,2}):?(\d{2})?$/.exec(s || '');
  if (!m) return 0;
  return (m[1] === '-' ? -1 : 1) * (Number(m[2]) + Number(m[3] || 0) / 60);
}

export function parseGpx(text) {
  const doc = new DOMParser().parseFromString(text, 'application/xml');
  if (doc.querySelector('parsererror')) throw new Error('Not a valid GPX file');
  const pts = [...doc.querySelectorAll('trkpt, rtept, wpt')]
    .map(el => ({
      lat: Number(el.getAttribute('lat')),
      lng: Number(el.getAttribute('lon')),
      t: Date.parse(el.querySelector('time')?.textContent || ''),
    }))
    .filter(p => Number.isFinite(p.lat) && Number.isFinite(p.lng) && Number.isFinite(p.t))
    .sort((a, b) => a.t - b.t);
  if (pts.length < 2) throw new Error('The GPX file has no timed track points');
  return pts;
}

export function positionAt(pts, t, toleranceMs = 120000) {
  if (t < pts[0].t - toleranceMs || t > pts[pts.length - 1].t + toleranceMs) return null;
  if (t <= pts[0].t) return pts[0];
  if (t >= pts[pts.length - 1].t) return pts[pts.length - 1];
  let lo = 0, hi = pts.length - 1;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (pts[mid].t <= t) lo = mid; else hi = mid;
  }
  const a = pts[lo], b = pts[hi];
  if (b.t - a.t > 10 * 60000) return null; // signal lost for over 10 minutes: don't guess
  const f = b.t === a.t ? 0 : (t - a.t) / (b.t - a.t);
  return { lat: a.lat + (b.lat - a.lat) * f, lng: a.lng + (b.lng - a.lng) * f };
}
