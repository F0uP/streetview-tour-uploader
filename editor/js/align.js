// North from the photos: the turn between two panoramas taken a few metres apart,
// found by comparing their horizons. Things far away - and things straight ahead
// or behind, along the way walked - appear at the same compass bearing in both
// photos, so the shift that lines up the two horizons is the difference of the
// photos' headings, whichever way the camera was held.

export const ALIGN_W = 720; // columns compared: half a degree each
const SLICES = 6;

function normalise(f) {
  let mean = 0;
  for (let i = 0; i < f.length; i++) mean += f[i];
  mean /= f.length;
  let sd = 0;
  for (let i = 0; i < f.length; i++) sd += (f[i] - mean) ** 2;
  sd = Math.sqrt(sd / f.length) || 1;
  for (let i = 0; i < f.length; i++) f[i] = (f[i] - mean) / sd;
}

// Per column of the horizon band (38-62 % of the height): the brightness in six
// slices and the change to the next column, each normalised over the circle.
// rgba: the pixels of the panorama scaled to width x height (2:1).
export function horizonFeatures(rgba, width, height) {
  const y0 = Math.round(height * 0.38);
  const y1 = Math.round(height * 0.62);
  const feats = Array.from({ length: SLICES + 1 }, () => new Float32Array(width));
  for (let k = 0; k < SLICES; k++) {
    const a = y0 + Math.floor(((y1 - y0) * k) / SLICES);
    const b = y0 + Math.floor(((y1 - y0) * (k + 1)) / SLICES);
    for (let x = 0; x < width; x++) {
      let sum = 0;
      for (let y = a; y < b; y++) {
        const i = (y * width + x) * 4;
        sum += 0.299 * rgba[i] + 0.587 * rgba[i + 1] + 0.114 * rgba[i + 2];
      }
      feats[k][x] = sum / Math.max(1, b - a);
    }
  }
  for (let x = 0; x < width; x++) {
    let here = 0, next = 0;
    for (let k = 0; k < SLICES; k++) { here += feats[k][x]; next += feats[k][(x + 1) % width]; }
    feats[SLICES][x] = (next - here) / SLICES;
  }
  feats.forEach(normalise);
  return feats;
}

// The panorama's horizon, from an image file (scaled down by the browser).
export async function photoFeatures(file) {
  const bmp = await createImageBitmap(file, { resizeWidth: ALIGN_W, resizeHeight: ALIGN_W / 2, resizeQuality: 'medium' });
  const canvas = typeof OffscreenCanvas === 'function' ? new OffscreenCanvas(ALIGN_W, ALIGN_W / 2) : Object.assign(document.createElement('canvas'), { width: ALIGN_W, height: ALIGN_W / 2 });
  const ctx = canvas.getContext('2d', { willReadFrequently: true });
  ctx.drawImage(bmp, 0, 0);
  bmp.close?.();
  return horizonFeatures(ctx.getImageData(0, 0, ALIGN_W, ALIGN_W / 2).data, ALIGN_W, ALIGN_W / 2);
}

// The shift of B against A (columns; a feature at column x of A is at x + shift in
// B) with the best match, found with the columns weighted (weight: per column of
// A). How sure it is comes from the plain correlation: score there (1 = the same
// horizon) and margin - how far it stands out from the best match more than 10°
// away. Two different places can correlate fairly well somewhere by chance, but
// then nothing stands out.
export function bestShift(fa, fb, weight = null) {
  const W = fa[0].length;
  const K = fa.length;
  const plain = new Float32Array(W);
  const weighted = new Float32Array(W);
  let wsum = 0;
  for (let x = 0; x < W; x++) wsum += weight ? weight[x] : 1;
  for (let s = 0; s < W; s++) {
    let sum = 0, wsumc = 0;
    for (let x = 0; x < W; x++) {
      const xb = (x + s) % W;
      let c = 0;
      for (let k = 0; k < K; k++) c += fa[k][x] * fb[k][xb];
      sum += c;
      wsumc += (weight ? weight[x] : 1) * c;
    }
    plain[s] = sum / (W * K);
    weighted[s] = wsumc / (wsum * K);
  }
  let best = 0;
  for (let s = 1; s < W; s++) if (weighted[s] > weighted[best]) best = s;
  const guard = Math.round(W / 36);
  let second = -Infinity;
  for (let s = 0; s < W; s++) {
    const d = Math.min(Math.abs(s - best), W - Math.abs(s - best));
    if (d > guard && plain[s] > second) second = plain[s];
  }
  const l = weighted[(best - 1 + W) % W], c = weighted[best], r = weighted[(best + 1) % W];
  const frac = (l - r) / (2 * (l - 2 * c + r));
  return { shift: best + (Number.isFinite(frac) ? Math.max(-0.5, Math.min(0.5, frac)) : 0), score: plain[best], margin: plain[best] - second };
}

// Columns of A along the way walked (bearing from A to B) count most: there the
// two photos see the same things at the same bearing, nearby or not.
export function walkWeights(headingA, bearing, width = ALIGN_W) {
  const w = new Float32Array(width);
  for (let x = 0; x < width; x++) {
    const world = headingA + (x / width) * 360 - 180;
    w[x] = 0.3 + Math.cos(((world - bearing) * Math.PI) / 180) ** 2;
  }
  return w;
}

// B's heading from A's and the shift between them.
export function headingFromShift(headingA, shift, width = ALIGN_W) {
  return (((headingA - (shift * 360) / width) % 360) + 360) % 360;
}

// How sure: good enough to apply without asking, or only to show.
export const isClearMatch = m => m.score >= 0.45 && m.margin >= 0.2;
