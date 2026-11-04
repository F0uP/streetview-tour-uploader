// Is a photo sharp and well exposed? A panorama taken while walking can be blurred by
// motion, one against the sun can be mostly white. Measured on a small copy: the
// sharpness is how strongly neighbouring pixels differ (the variance of the Laplacian)
// in the band around the horizon, where the scene is; the exposure is how much of the
// photo is near white or near black.

const W = 1024, H = 512;
const cache = new WeakMap(); // photo File -> its result

// Below these a photo is reported (on a 1024 px wide copy, 0-255 grey levels).
export const LIMITS = { sharpness: 25, bright: 0.3, dark: 0.5 };

export async function photoQuality(file) {
  if (cache.has(file)) return cache.get(file);
  const bmp = await createImageBitmap(file, { resizeWidth: W, resizeHeight: H, resizeQuality: 'medium' });
  const ctx = new OffscreenCanvas(W, H).getContext('2d', { willReadFrequently: true });
  ctx.drawImage(bmp, 0, 0);
  bmp.close?.();
  const px = ctx.getImageData(0, 0, W, H).data;
  const grey = new Float32Array(W * H);
  let bright = 0, dark = 0;
  for (let i = 0; i < grey.length; i++) {
    const l = 0.299 * px[i * 4] + 0.587 * px[i * 4 + 1] + 0.114 * px[i * 4 + 2];
    grey[i] = l;
    if (l >= 250) bright++;
    else if (l <= 6) dark++;
  }
  // The Laplacian in the middle band (25-75 % of the height): no sky, no tripod.
  let sum = 0, sq = 0, count = 0;
  for (let y = Math.round(H * 0.25); y < Math.round(H * 0.75); y++) {
    for (let x = 1; x < W - 1; x++) {
      const i = y * W + x;
      const lap = grey[i - 1] + grey[i + 1] + grey[i - W] + grey[i + W] - 4 * grey[i];
      sum += lap;
      sq += lap * lap;
      count++;
    }
  }
  const mean = sum / count;
  const result = { sharpness: sq / count - mean * mean, bright: bright / grey.length, dark: dark / grey.length };
  cache.set(file, result);
  return result;
}
