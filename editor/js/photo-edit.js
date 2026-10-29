// Changing the pixels of a panorama: hiding faces and plates (pixelated or blurred),
// covering the tripod at the bottom (the nadir) and encoding the result as JPEG.

// Pixelate a rectangle of `ctx` in place: shrink it to a few pixels and stretch it back.
export function pixelate(ctx, x, y, w, h, block) {
  x = Math.round(x); y = Math.round(y); w = Math.round(w); h = Math.round(h);
  if (w < 2 || h < 2) return;
  const tw = Math.max(1, Math.round(w / block)), th = Math.max(1, Math.round(h / block));
  const tmp = new OffscreenCanvas(tw, th);
  const tctx = tmp.getContext('2d');
  tctx.imageSmoothingQuality = 'high';
  tctx.drawImage(ctx.canvas, x, y, w, h, 0, 0, tw, th);
  ctx.save();
  ctx.imageSmoothingEnabled = true;
  ctx.drawImage(tmp, 0, 0, tw, th, x, y, w, h);
  ctx.restore();
}

// Blur a rectangle so that nothing in it can be recognised: shrunk far and stretched back
// smoothly (twice, so no blocks show), with a little noise on top - a blur alone can
// sometimes be partly undone, noise can't.
export function softBlur(ctx, x, y, w, h, strength) {
  x = Math.round(x); y = Math.round(y); w = Math.round(w); h = Math.round(h);
  if (w < 2 || h < 2) return;
  const tw = Math.max(2, Math.round(w / strength)), th = Math.max(2, Math.round(h / strength));
  const small = new OffscreenCanvas(tw, th);
  const sctx = small.getContext('2d');
  sctx.imageSmoothingQuality = 'high';
  sctx.drawImage(ctx.canvas, x, y, w, h, 0, 0, tw, th);
  const mid = new OffscreenCanvas(Math.max(2, tw * 4), Math.max(2, th * 4));
  const mctx = mid.getContext('2d');
  mctx.imageSmoothingQuality = 'high';
  mctx.drawImage(small, 0, 0, mid.width, mid.height);
  ctx.save();
  ctx.imageSmoothingEnabled = true;
  ctx.imageSmoothingQuality = 'high';
  ctx.drawImage(mid, 0, 0, mid.width, mid.height, x, y, w, h);
  ctx.restore();
  const img = ctx.getImageData(x, y, w, h);
  const d = img.data;
  for (let i = 0; i < d.length; i += 4) {
    const n = (Math.random() - 0.5) * 18;
    d[i] += n; d[i + 1] += n; d[i + 2] += n;
  }
  ctx.putImageData(img, x, y);
}

export function blurBlockSize(r) { return Math.max(6, Math.min(r.w, r.h) / 7); }

// One marked area, the way the user chose: 'pixelate' or 'blur'.
export function hideArea(ctx, r, style, scale = 1) {
  const x = r.x * scale, y = r.y * scale, w = r.w * scale, h = r.h * scale;
  if (style === 'blur') softBlur(ctx, x, y, w, h, Math.max(3, blurBlockSize(r) * scale * 1.4));
  else pixelate(ctx, x, y, w, h, Math.max(3, blurBlockSize(r) * scale));
}

// ── The tripod at the bottom (nadir) ──────────────
// In a 360° photo the ground right below the camera is the bottom band of the image.
// size: the angle around the nadir to cover (degrees). style: 'blur' - the ground
// around it, smeared inwards; 'color' - its average colour; 'logo' - an image of the
// user's, laid flat on the ground (upright as seen when looking down facing forward).
export const NADIR_SIZES = [12, 18, 25, 32];

export function nadirBand(H, size) {
  return Math.round(H * (size / 180));
}

export function coverNadir(ctx, W, H, { size = 18, style = 'blur', logo = null } = {}) {
  const band = nadirBand(H, size);
  if (band < 2) return;
  const y0 = H - band;
  // The ring just above the band: what the ground around the tripod looks like.
  const ring = Math.max(2, Math.round(band * 0.25));
  const fill = new OffscreenCanvas(64, 1);
  const fctx = fill.getContext('2d');
  fctx.imageSmoothingQuality = 'high';
  fctx.drawImage(ctx.canvas, 0, y0 - ring, W, ring, 0, 0, 64, 1);
  const patch = new OffscreenCanvas(W, band + ring);
  const pctx = patch.getContext('2d');
  if (style === 'color') {
    const avg = new OffscreenCanvas(1, 1).getContext('2d');
    avg.imageSmoothingQuality = 'high';
    avg.drawImage(fill, 0, 0, 1, 1);
    const [r, g, b] = avg.getImageData(0, 0, 1, 1).data;
    pctx.fillStyle = `rgb(${r},${g},${b})`;
    pctx.fillRect(0, 0, W, band + ring);
  } else {
    // Each column: the colour of the ground above it, smeared down - the band is a
    // disc around the nadir, so this fades the surroundings in from all sides.
    pctx.imageSmoothingEnabled = true;
    pctx.imageSmoothingQuality = 'high';
    pctx.drawImage(fill, 0, 0, 64, 1, 0, 0, W, band + ring);
  }
  // Soft edge: the patch fades in over the ring above the band.
  const fade = pctx.createLinearGradient(0, 0, 0, ring * 2);
  fade.addColorStop(0, 'rgba(0,0,0,0)');
  fade.addColorStop(1, 'rgba(0,0,0,1)');
  pctx.globalCompositeOperation = 'destination-in';
  pctx.fillStyle = fade;
  pctx.fillRect(0, 0, W, band + ring);
  pctx.globalCompositeOperation = 'source-over';
  ctx.drawImage(patch, 0, y0 - ring);
  if (style === 'logo' && logo) drawNadirLogo(ctx, W, H, size, logo);
}

// The logo as seen looking straight down: each pixel of the band is a direction, which
// is a point on the logo (azimuthal projection around the nadir).
function drawNadirLogo(ctx, W, H, size, logo) {
  const band = nadirBand(H, size);
  const y0 = H - band;
  const L = 512;
  const lc = new OffscreenCanvas(L, L).getContext('2d', { willReadFrequently: true });
  lc.drawImage(logo, 0, 0, L, L);
  const src = lc.getImageData(0, 0, L, L).data;
  const img = ctx.getImageData(0, y0, W, band);
  const d = img.data;
  const sin = new Float32Array(W), cos = new Float32Array(W);
  for (let x = 0; x < W; x++) {
    const lambda = ((x + 0.5) / W) * 2 * Math.PI - Math.PI; // 0: the photo's front
    sin[x] = Math.sin(lambda);
    cos[x] = Math.cos(lambda);
  }
  for (let y = 0; y < band; y++) {
    const fromNadir = ((H - (y0 + y + 0.5)) / H) * 180; // degrees
    const rho = fromNadir / size; // 0 at the nadir, 1 at the edge of the logo
    if (rho > 1) continue;
    const edge = Math.min(1, (1 - rho) * band / 2); // a smooth rim
    for (let x = 0; x < W; x++) {
      const u = Math.min(L - 1, Math.max(0, Math.floor((0.5 + 0.5 * rho * sin[x]) * L)));
      const v = Math.min(L - 1, Math.max(0, Math.floor((0.5 - 0.5 * rho * cos[x]) * L)));
      const s = (v * L + u) * 4;
      const a = (src[s + 3] / 255) * edge;
      if (!a) continue;
      const i = (y * W + x) * 4;
      d[i] += (src[s] - d[i]) * a;
      d[i + 1] += (src[s + 1] - d[i + 1]) * a;
      d[i + 2] += (src[s + 2] - d[i + 2]) * a;
    }
  }
  ctx.putImageData(img, 0, y0);
}

// What the camera sees looking straight down (for the preview): `fov` degrees around the
// nadir of the panorama in ctx, drawn into the square canvas `out`, the photo's front up.
export function lookDown(ctx, W, H, out, fov = 80) {
  const size = out.width;
  const band = Math.min(H, Math.ceil(H * (fov / 2 / 180)) + 1);
  const src = ctx.getImageData(0, H - band, W, band).data;
  const octx = out.getContext('2d');
  const img = octx.createImageData(size, size);
  const d = img.data;
  for (let v = 0; v < size; v++) {
    for (let u = 0; u < size; u++) {
      const dx = ((u + 0.5) / size) * 2 - 1, dy = ((v + 0.5) / size) * 2 - 1;
      const rho = Math.hypot(dx, dy);
      if (rho > 1) continue;
      const fromNadir = (rho * fov) / 2;
      const lambda = Math.atan2(dx, -dy);
      const x = Math.min(W - 1, Math.floor(((lambda + Math.PI) / (2 * Math.PI)) * W));
      const y = Math.max(0, Math.min(band - 1, Math.floor(band - 1 - (fromNadir / 180) * H)));
      const s = (y * W + x) * 4, o = (v * size + u) * 4;
      d[o] = src[s]; d[o + 1] = src[s + 1]; d[o + 2] = src[s + 2]; d[o + 3] = 255;
    }
  }
  octx.putImageData(img, 0, 0);
}

// ── Encoding ──────────────────────────────────────
// JPEG at the given quality; with maxBytes, lower quality (and at last smaller) until it fits.
export async function encodeJpeg(canvas, { quality = 0.92, maxBytes = null } = {}) {
  let blob = await canvas.convertToBlob({ type: 'image/jpeg', quality });
  if (!maxBytes || blob.size <= maxBytes) return blob;
  for (const q of [0.86, 0.8, 0.74, 0.68]) {
    if (q >= quality) continue;
    blob = await canvas.convertToBlob({ type: 'image/jpeg', quality: q });
    if (blob.size <= maxBytes) return blob;
  }
  let w = canvas.width, h = canvas.height;
  while (blob.size > maxBytes && w > 2048) {
    w = Math.round(w * 0.85 / 2) * 2;
    h = w / 2;
    const smaller = new OffscreenCanvas(w, h);
    const sctx = smaller.getContext('2d');
    sctx.imageSmoothingQuality = 'high';
    sctx.drawImage(canvas, 0, 0, w, h);
    blob = await smaller.convertToBlob({ type: 'image/jpeg', quality: 0.8 });
  }
  return blob;
}
