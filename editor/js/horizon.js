// Levelling the horizon. A camera not held quite level gives a wavy horizon on Street
// View. Turn the photo's tilt and roll until the guide line lies on the horizon: the
// values are the camera's pitch and roll (as GPano and Google count them) - Google gets
// them with the photo, the exported tour turns the panorama by them.
// Until the user levels a photo, its own XMP values (if any) apply.
import exifr from '../vendor/exifr/full.esm.js';
import { degToRad } from './util.js';

// The viewer level by our values instead of the photo's XMP ones (that would count them twice).
export const ZERO_POSE = () => ({ poseHeading: 0, posePitch: 0, poseRoll: 0 });
export const hasOwnPose = n => n?.pitch != null || n?.roll != null;
// The viewer's correction for a camera pose (pan 0: the editor shows the photo in its own frame).
export const levelCorrection = n => ({ pan: 0, tilt: -degToRad(n.pitch || 0), roll: -degToRad(n.roll || 0) });

// The photo's own pitch and roll from its XMP (GPano:PosePitchDegrees / PoseRollDegrees).
const xmpCache = new WeakMap();
export async function xmpPose(file) {
  if (!file) return { pitch: 0, roll: 0 };
  if (!xmpCache.has(file)) {
    let pose = { pitch: 0, roll: 0 };
    try {
      const d = await exifr.parse(file, { xmp: true, gps: false, exif: false, ifd0: false, iptc: false });
      pose = { pitch: Number(d?.PosePitchDegrees) || 0, roll: Number(d?.PoseRollDegrees) || 0 };
    } catch {}
    xmpCache.set(file, pose);
  }
  return xmpCache.get(file);
}

// ── 3×3 rotations, for the exported tour ──────────
// The editor levels a photo in its own frame and then turns it to north: R = Ry(-heading) ·
// Rz(-roll) · Rx(-pitch). The viewer takes one correction as Euler angles in the order Z, X, Y
// (R = Rz · Rx · Ry): the same rotation, taken apart (as three.js does for 'ZXY').
const mul = (a, b) => a.map((row, i) => [0, 1, 2].map(j => row[0] * b[0][j] + row[1] * b[1][j] + row[2] * b[2][j]));
const rx = a => [[1, 0, 0], [0, Math.cos(a), -Math.sin(a)], [0, Math.sin(a), Math.cos(a)]];
const ry = a => [[Math.cos(a), 0, Math.sin(a)], [0, 1, 0], [-Math.sin(a), 0, Math.cos(a)]];
const rz = a => [[Math.cos(a), -Math.sin(a), 0], [Math.sin(a), Math.cos(a), 0], [0, 0, 1]];

export function tourCorrection(heading, pitch, roll) {
  const m = mul(mul(ry(-degToRad(heading || 0)), rz(-degToRad(roll || 0))), rx(-degToRad(pitch || 0)));
  const x = Math.asin(Math.max(-1, Math.min(1, m[2][1])));
  let y, z;
  if (Math.abs(m[2][1]) < 0.9999999) {
    y = Math.atan2(-m[2][0], m[2][2]);
    z = Math.atan2(-m[0][1], m[1][1]);
  } else {
    y = 0;
    z = Math.atan2(m[1][0], m[0][0]);
  }
  return { pan: +y.toFixed(5), tilt: +x.toFixed(5), roll: +z.toFixed(5) };
}

// ── The side panel's "Horizon" section ────────────
let ctx; // from the editor: { node(), apply(n), pushUndo, changed(n), t }
const el = id => document.getElementById(id);
// While it is open, the panorama in the side panel shows the guide: a dashed line across its
// middle (editor.css, .pvw.horizon) - that view always looks level, so its middle is the horizon.
export const horizonOpen = () => !!document.querySelector('details[data-sec="horizon"]')?.open;

export function initHorizon(c) {
  ctx = c;
  let undoTaken = false;
  for (const [axis, id] of [['pitch', 'hz-pitch'], ['roll', 'hz-roll']]) {
    const input = el(id);
    input.addEventListener('pointerdown', () => { undoTaken = false; });
    input.addEventListener('input', async () => {
      const n = ctx.node();
      if (!n) return;
      if (!undoTaken) { ctx.pushUndo(); undoTaken = true; }
      if (!hasOwnPose(n)) {
        const camera = await xmpPose(n.photoFile);
        if (!hasOwnPose(n)) Object.assign(n, camera); // (unless an input meanwhile did)
      }
      n[axis] = Number(input.value);
      renderHorizon(n);
      ctx.apply(n);
    });
    input.addEventListener('change', () => { undoTaken = false; const n = ctx.node(); if (n) ctx.changed(n); });
  }
  el('hz-reset').onclick = async () => {
    const n = ctx.node();
    if (!n || !hasOwnPose(n)) return;
    const camera = await xmpPose(n.photoFile);
    if (n.pitch === camera.pitch && n.roll === camera.roll) return;
    ctx.pushUndo();
    // The camera's own values, as the node's: Google may have the ones set by hand from an
    // earlier upload, and gets these with the next one.
    Object.assign(n, camera);
    renderHorizon(n);
    ctx.apply(n);
    ctx.changed(n);
  };
}

export async function renderHorizon(n) {
  if (!n) return;
  const camera = await xmpPose(n.photoFile);
  const pose = hasOwnPose(n) ? { pitch: n.pitch || 0, roll: n.roll || 0 } : camera;
  el('hz-pitch').value = pose.pitch;
  el('hz-roll').value = pose.roll;
  el('hz-pitch-v').textContent = `${(+pose.pitch).toFixed(1)}°`;
  el('hz-roll-v').textContent = `${(+pose.roll).toFixed(1)}°`;
  const byHand = pose.pitch !== camera.pitch || pose.roll !== camera.roll;
  el('hz-note').textContent = byHand ? ctx.t('levelled by hand') : ctx.t('as the camera saved it');
}
