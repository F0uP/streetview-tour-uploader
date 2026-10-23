// Faces and licence plates in a 360° panorama, found on this computer
// (MediaPipe, shipped in vendor/mediapipe). The detectors expect ordinary photos,
// where faces and cars are fairly large; in a panorama they are small. So the band
// around the horizon is scanned in overlapping square tiles at a few sizes and
// what they find is merged. Results are suggestions for the blur tool - to check.

const MEDIAPIPE = new URL('../vendor/mediapipe', import.meta.url).href;
const FACE_MODELS = [`${MEDIAPIPE}/blaze_face_full_range.tflite`, `${MEDIAPIPE}/blaze_face_short_range.tflite`];
const VEHICLE_MODEL = `${MEDIAPIPE}/efficientdet_lite0.tflite`;
const VEHICLES = ['car', 'truck', 'bus', 'motorcycle'];
// On the graphics card (WebGL) many times faster; on the processor where there is none.
const DELEGATES = ['GPU', 'CPU'];

let vision = null, faces = null, vehicles = null;

export const NO_WEBGL = 'The detection needs WebGL graphics, which this computer does not offer (graphics driver?). Mark the areas by hand.';

function loadVision() {
  vision ??= (async () => {
    // MediaPipe works on WebGL even on the processor; without it, detect() fails later.
    const gl = document.createElement('canvas');
    if (!(gl.getContext('webgl2') || gl.getContext('webgl'))) throw new Error(NO_WEBGL);
    const mod = await import(`${MEDIAPIPE}/vision_bundle.mjs`);
    return { mod, fileset: await mod.FilesetResolver.forVisionTasks(`${MEDIAPIPE}/wasm`) };
  })();
  vision.catch(() => { vision = null; });
  return vision;
}

function loadFaceDetector() {
  faces ??= (async () => {
    const { mod, fileset } = await loadVision();
    let lastErr;
    for (const model of FACE_MODELS) {
      for (const delegate of DELEGATES) {
        try {
          return await mod.FaceDetector.createFromOptions(fileset, {
            baseOptions: { modelAssetPath: model, delegate }, runningMode: 'IMAGE', minDetectionConfidence: 0.6,
          });
        } catch (err) { lastErr = err; }
      }
    }
    throw lastErr;
  })();
  faces.catch(() => { faces = null; });
  return faces;
}

function loadVehicleDetector() {
  vehicles ??= (async () => {
    const { mod, fileset } = await loadVision();
    let lastErr;
    for (const delegate of DELEGATES) {
      try {
        return await mod.ObjectDetector.createFromOptions(fileset, {
          baseOptions: { modelAssetPath: VEHICLE_MODEL, delegate }, runningMode: 'IMAGE',
          scoreThreshold: 0.35, categoryAllowlist: VEHICLES,
        });
      } catch (err) { lastErr = err; }
    }
    throw lastErr;
  })();
  vehicles.catch(() => { vehicles = null; });
  return vehicles;
}

// How much two boxes overlap, relative to the smaller one.
export function overlap(a, b) {
  const x = Math.max(0, Math.min(a.x + a.w, b.x + b.w) - Math.max(a.x, b.x));
  const y = Math.max(0, Math.min(a.y + a.h, b.y + b.h) - Math.max(a.y, b.y));
  return (x * y) / Math.min(a.w * a.h, b.w * b.h);
}

// The same, for boxes in a panorama W wide: one that runs off the right edge goes on at the left.
function overlapRound(a, b, W) {
  return Math.max(overlap(a, b), overlap({ ...a, x: a.x + W }, b), overlap(a, { ...b, x: b.x + W }));
}

// A box in "round" coordinates (x may lie past either edge) as it lies in the
// image: one box, or two where it crosses the seam at the left and right edge
// (the second marked `seam`, so the thing in it is counted once).
export function wrapBox(b, W) {
  const x = ((b.x % W) + W) % W;
  if (x + b.w <= W) return [{ ...b, x }];
  return [{ ...b, x, w: W - x }, { ...b, x: 0, w: x + b.w - W, seam: true }];
}

// A tile at x..x+T; past the right edge it continues with the left one.
function drawTile(ctx, bmp, x, y, T, cs) {
  const W = bmp.width;
  if (x + T <= W) {
    ctx.drawImage(bmp, x, y, T, T, 0, 0, cs, cs);
    return;
  }
  const part = W - x, k = cs / T;
  ctx.drawImage(bmp, x, y, part, T, 0, 0, part * k, cs);
  ctx.drawImage(bmp, 0, y, T - part, T, part * k, 0, cs - part * k, cs);
}

// passes: [tile size, top, bottom of the band to scan (share of the height)]. The
// smaller a face or car, the farther away and the closer to the horizon it is - so
// small tiles only need a narrow band. overlap: of neighbouring tiles.
async function scan(bmp, detector, { passes, overlap: share = 0.35, input = 320 }, onProgress) {
  const W = bmp.width, H = bmp.height;
  const tiles = [];
  passes.forEach(([size, top, bottom]) => {
    const T = Math.round(size), step = Math.round(T * (1 - share));
    if (T < 96) return;
    const y0 = Math.round(H * top), y1 = Math.max(y0 + T, Math.round(H * bottom));
    for (let y = y0; y <= y1 - T + step - 1; y += step) {
      // Round the whole panorama: the last tiles reach over the seam, so a face there is seen whole.
      for (let x = 0; x < W; x += step) tiles.push({ x, y: Math.min(y, H - T), T });
    }
  });
  const tile = document.createElement('canvas');
  // The detector reads every tile back: a canvas kept on the CPU is many times faster here.
  const ctx = tile.getContext('2d', { willReadFrequently: true });
  const found = [];
  for (let i = 0; i < tiles.length; i++) {
    const { x, y, T } = tiles[i];
    const cs = Math.min(T, input);
    if (tile.width !== cs) { tile.width = cs; tile.height = cs; }
    drawTile(ctx, bmp, x, y, T, cs);
    const k = T / cs;
    detector.detect(tile).detections.forEach(d => {
      const b = d.boundingBox;
      found.push({ x: x + b.originX * k, y: y + b.originY * k, w: b.width * k, h: b.height * k, score: d.categories?.[0]?.score ?? 0.5 });
    });
    if (i % 25 === 0) {
      onProgress?.(i / tiles.length);
      await new Promise(r => setTimeout(r));
    }
  }
  const minSide = Math.max(16, W / 400);
  const kept = [];
  found.filter(f => f.w >= minSide && f.h >= minSide).sort((a, b) => b.score - a.score)
    .forEach(f => { if (!kept.some(k => overlapRound(k, f, W) > 0.3)) kept.push(f); });
  return kept;
}

// Into the image: split at the seam, cut at the top and bottom.
function placeBox(r, W, H) {
  const y = Math.max(0, r.y), h = Math.min(H - y, r.h - (y - r.y));
  return wrapBox({ ...r, w: Math.min(r.w, W), y, h }, W);
}

// Faces, each grown a little so hair and chin are covered too.
export async function findFaces(bmp, onProgress) {
  const detector = await loadFaceDetector();
  const H = bmp.height;
  const passes = [[H / 16, 0.4, 0.62], [H / 8, 0.3, 0.72], [H / 4, 0.2, 0.8]];
  return (await scan(bmp, detector, { passes }, onProgress)).flatMap(f =>
    placeBox({ x: f.x - f.w * 0.35, y: f.y - f.h * 0.35, w: f.w * 1.7, h: f.h * 1.7, kind: 'face' }, bmp.width, H));
}

// Where a licence plate usually is on a vehicle seen from the front or back: the
// lower middle. A vehicle seen from the side gets a box too - remove it if there
// is no plate in it.
export function plateZone(v) {
  return { x: v.x + v.w * 0.15, y: v.y + v.h * 0.5, w: v.w * 0.7, h: v.h * 0.45 };
}

export async function findPlates(bmp, onProgress) {
  const detector = await loadVehicleDetector();
  const H = bmp.height;
  // Vehicles stand on the ground and are larger than faces: a lower band, bigger tiles.
  const passes = [[H / 5, 0.44, 0.7], [H / 2.6, 0.4, 0.88]];
  return (await scan(bmp, detector, { passes }, onProgress)).flatMap(v =>
    placeBox({ ...plateZone(v), kind: 'plate' }, bmp.width, H));
}
