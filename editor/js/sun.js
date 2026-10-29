// North from the sun: where the sun stood (from the capture time and the position)
// and where it is in the panorama give the photo's heading - without any other
// photo to compare with. Works on sunny photos, where the sun is a bright spot in
// the sky; the result says how sure it is.
import { normalizeHeading } from './geo.js';

// The sun's position (after V. Agafonkin's SunCalc): azimuth as a compass direction, altitude above the horizon, in degrees.
const RAD = Math.PI / 180;
const OBLIQUITY = RAD * 23.4397;

export function sunPosition(date, lat, lng) {
  const d = date.valueOf() / 86400000 - 0.5 + 2440588 - 2451545; // days since J2000
  const M = RAD * (357.5291 + 0.98560028 * d); // mean anomaly
  const C = RAD * (1.9148 * Math.sin(M) + 0.02 * Math.sin(2 * M) + 0.0003 * Math.sin(3 * M));
  const L = M + C + RAD * 102.9372 + Math.PI; // ecliptic longitude
  const dec = Math.asin(Math.sin(OBLIQUITY) * Math.sin(L));
  const ra = Math.atan2(Math.sin(L) * Math.cos(OBLIQUITY), Math.cos(L));
  const H = RAD * (280.16 + 360.9856235 * d) - RAD * -lng - ra; // hour angle
  const phi = RAD * lat;
  const az = Math.atan2(Math.sin(H), Math.cos(H) * Math.sin(phi) - Math.tan(dec) * Math.cos(phi)); // from south, westwards
  const alt = Math.asin(Math.sin(phi) * Math.sin(dec) + Math.cos(phi) * Math.cos(dec) * Math.cos(H));
  return { azimuth: normalizeHeading(az / RAD + 180), altitude: alt / RAD };
}

// The brightest spot in the sky part of a panorama: { x, y } (shares of width and
// height), its size (share of the sky) and how much else is that bright (relative to it).
// null when nothing is near white (an overcast sky).
export async function findSun(file, W = 1440) {
  const H = W / 2;
  const bmp = await createImageBitmap(file, { resizeWidth: W, resizeHeight: H, resizeQuality: 'medium' });
  const canvas = new OffscreenCanvas(W, H);
  const ctx = canvas.getContext('2d', { willReadFrequently: true });
  ctx.drawImage(bmp, 0, 0);
  bmp.close?.();
  const rows = Math.round(H * 0.56); // the sky, and a little below the horizon (a camera not held quite level)
  const px = ctx.getImageData(0, 0, W, rows).data;
  const lum = new Float32Array(W * rows);
  let max = 0, at = 0;
  for (let i = 0; i < lum.length; i++) {
    const l = 0.299 * px[i * 4] + 0.587 * px[i * 4 + 1] + 0.114 * px[i * 4 + 2];
    lum[i] = l;
    if (l > max) { max = l; at = i; }
  }
  if (max < 235) return null;
  const limit = max - 10;
  // The bright blob around the brightest pixel (round the panorama, over its seam).
  const seen = new Uint8Array(lum.length);
  const stack = [at];
  seen[at] = 1;
  let count = 0, sx = 0, sy = 0, sum = 0;
  const cap = Math.round(lum.length * 0.05);
  while (stack.length && count < cap) {
    const i = stack.pop();
    const x = i % W, y = (i - x) / W;
    count++;
    sx += Math.cos((x / W) * 2 * Math.PI);
    sy += Math.sin((x / W) * 2 * Math.PI);
    sum += y;
    for (const [dx, dy] of [[1, 0], [-1, 0], [0, 1], [0, -1]]) {
      const ny = y + dy;
      if (ny < 0 || ny >= rows) continue;
      const j = ny * W + ((x + dx + W) % W);
      if (!seen[j] && lum[j] >= limit) { seen[j] = 1; stack.push(j); }
    }
  }
  let elsewhere = 0;
  for (let i = 0; i < lum.length; i++) if (lum[i] >= limit && !seen[i]) elsewhere++;
  const angle = Math.atan2(sy, sx);
  return {
    x: (((angle / (2 * Math.PI)) % 1) + 1) % 1,
    y: (sum / count + 0.5) / H,
    size: count / lum.length,
    elsewhere: elsewhere / count,
  };
}

// The photo's heading from the sun, or a reason why not: { heading, sunAz, sunAlt, photoAlt, clear } | { reason }.
export async function northFromSun(file, when, lat, lng) {
  const sun = sunPosition(when, lat, lng);
  if (sun.altitude < 2) return { reason: 'low', sunAlt: sun.altitude };
  const spot = await findSun(file);
  if (!spot) return { reason: 'none', sunAlt: sun.altitude };
  const yaw = spot.x * 360 - 180; // from the photo's centre, clockwise
  const photoAlt = 90 - spot.y * 180;
  const heading = normalizeHeading(sun.azimuth - yaw);
  // A small, single bright spot at the height where the sun stood: that is the sun.
  const clear = spot.size <= 0.012 && spot.elsewhere <= 0.6 && Math.abs(photoAlt - sun.altitude) <= 10;
  return { heading, sunAz: sun.azimuth, sunAlt: sun.altitude, photoAlt, clear };
}
