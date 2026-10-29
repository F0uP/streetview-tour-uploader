// JPEG metadata: what the editor reads from a photo's header, and the EXIF it writes.
// A photo that was edited (blurred, its tripod covered, made smaller) or is exported
// with its position gets a new, small EXIF block - capture time, camera, GPS position
// and compass direction - never the original one, which can hold an unblurred
// thumbnail and the camera's maker notes. The 360° (XMP) and colour (ICC) metadata stay.
import exifr from '../vendor/exifr/full.esm.js';

const XMP_ID = 'http://ns.adobe.com/xap/1.0/\0';
const XMP_EXT_ID = 'http://ns.adobe.com/xmp/extension/\0';

const text = (bytes, start, length) => String.fromCharCode(...bytes.subarray(start, start + length));

// The segments in front of the image data: [{ marker, start, end }] (end exclusive), and where the image data starts.
export function jpegSegments(bytes) {
  const segments = [];
  if (bytes[0] !== 0xFF || bytes[1] !== 0xD8) return { segments, dataStart: -1 };
  let p = 2;
  while (p + 4 <= bytes.length && bytes[p] === 0xFF) {
    const marker = bytes[p + 1];
    if (marker === 0xFF) { p++; continue; } // fill byte
    if (marker === 0xDA || marker === 0xD9) break;
    const end = p + 2 + ((bytes[p + 2] << 8) | bytes[p + 3]);
    if (end > bytes.length) break;
    segments.push({ marker, start: p, end });
    p = end;
  }
  return { segments, dataStart: p };
}

function kindOf(bytes, seg) {
  const at = seg.start + 4;
  if (seg.marker === 0xE0) return 'jfif';
  if (seg.marker === 0xE1) {
    if (text(bytes, at, 6) === 'Exif\0\0') return 'exif';
    if (text(bytes, at, XMP_ID.length) === XMP_ID) return 'xmp';
    if (text(bytes, at, XMP_EXT_ID.length) === XMP_EXT_ID) return 'xmpext';
  }
  // APP2 also carries MPF (preview images, addressed by byte offsets): ICC only.
  if (seg.marker === 0xE2 && text(bytes, at, 12) === 'ICC_PROFILE\0') return 'icc';
  return 'other';
}

// ── Writing EXIF ──────────────────────────────────
const BYTE = 1, ASCII = 2, LONG = 4, RATIONAL = 5;

function entry(tag, type, bytes, count = bytes.length) {
  return { tag, type, count, bytes };
}
function ascii(tag, s) {
  const b = new TextEncoder().encode(`${s}\0`);
  return entry(tag, ASCII, b);
}
function long(tag, v) {
  const b = new Uint8Array(4);
  new DataView(b.buffer).setUint32(0, v, true);
  return entry(tag, LONG, b, 1);
}
function rational(tag, pairs) {
  const b = new Uint8Array(8 * pairs.length);
  const dv = new DataView(b.buffer);
  pairs.forEach(([num, den], i) => { dv.setUint32(i * 8, num, true); dv.setUint32(i * 8 + 4, den, true); });
  return entry(tag, RATIONAL, b, pairs.length);
}
const dms = v => {
  const d = Math.floor(v), m = Math.floor((v - d) * 60);
  return [[d, 1], [m, 1], [Math.round(((v - d) * 60 - m) * 60 * 10000), 10000]];
};
const ifdLength = entries => 2 + 12 * entries.length + 4
  + entries.reduce((sum, e) => sum + (e.bytes.length > 4 ? e.bytes.length + (e.bytes.length & 1) : 0), 0);

function writeIfd(out, dv, at, entries) {
  entries.sort((a, b) => a.tag - b.tag);
  dv.setUint16(at, entries.length, true);
  let data = at + 2 + 12 * entries.length + 4;
  entries.forEach((e, i) => {
    const p = at + 2 + 12 * i;
    dv.setUint16(p, e.tag, true);
    dv.setUint16(p + 2, e.type, true);
    dv.setUint32(p + 4, e.count, true);
    if (e.bytes.length <= 4) {
      out.set(e.bytes, p + 8);
    } else {
      dv.setUint32(p + 8, data, true);
      out.set(e.bytes, data);
      data += e.bytes.length + (e.bytes.length & 1);
    }
  });
  dv.setUint32(at + 2 + 12 * entries.length, 0, true); // no next IFD (no thumbnail)
}

// The APP1 segment of a new EXIF block. taken: the camera's local time ('2026-05-01T14:03:22'),
// offset: its zone ('+02:00'), heading: compass direction of the photo's centre.
export function buildExif({ taken = null, offset = null, lat = null, lng = null, alt = null, heading = null, make = null, model = null } = {}) {
  const ifd0 = [], exif = [], gps = [];
  if (make) ifd0.push(ascii(0x010F, make));
  if (model) ifd0.push(ascii(0x0110, model));
  if (taken && /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d/.test(taken)) {
    exif.push(ascii(0x9003, taken.slice(0, 19).replace('T', ' ').replace(/-/g, ':')));
    if (/^[+-]\d\d:\d\d$/.test(offset || '')) exif.push(ascii(0x9011, offset));
  }
  if (lat != null && lng != null) {
    gps.push(ascii(0x0001, lat < 0 ? 'S' : 'N'), rational(0x0002, dms(Math.abs(lat))),
      ascii(0x0003, lng < 0 ? 'W' : 'E'), rational(0x0004, dms(Math.abs(lng))));
    if (alt != null && Number.isFinite(alt)) {
      gps.push(entry(0x0005, BYTE, new Uint8Array([alt < 0 ? 1 : 0])), rational(0x0006, [[Math.round(Math.abs(alt) * 100), 100]]));
    }
  }
  if (heading != null && Number.isFinite(heading)) {
    gps.push(ascii(0x0010, 'T'), rational(0x0011, [[Math.round((((heading % 360) + 360) % 360) * 100), 100]]));
  }
  if (gps.length) gps.push(entry(0x0000, BYTE, new Uint8Array([2, 3, 0, 0])));
  // The pointers to the EXIF and GPS parts: their places are known once the sizes are.
  const exifPtr = exif.length ? long(0x8769, 0) : null;
  const gpsPtr = gps.length ? long(0x8825, 0) : null;
  [exifPtr, gpsPtr].forEach(e => e && ifd0.push(e));
  const exifAt = 8 + ifdLength(ifd0);
  const gpsAt = exifAt + (exif.length ? ifdLength(exif) : 0);
  const size = gpsAt + (gps.length ? ifdLength(gps) : 0);
  if (exifPtr) new DataView(exifPtr.bytes.buffer).setUint32(0, exifAt, true);
  if (gpsPtr) new DataView(gpsPtr.bytes.buffer).setUint32(0, gpsAt, true);

  const seg = new Uint8Array(10 + size);
  const dv = new DataView(seg.buffer);
  seg.set([0xFF, 0xE1]);
  dv.setUint16(2, 8 + size);
  seg.set(new TextEncoder().encode('Exif'), 4); // + two zero bytes
  const tiff = seg.subarray(10);
  const tv = new DataView(seg.buffer, 10);
  tiff.set([0x49, 0x49, 0x2A, 0x00]); // little-endian TIFF
  tv.setUint32(4, 8, true);
  writeIfd(tiff, tv, 8, ifd0);
  if (exif.length) writeIfd(tiff, tv, exifAt, exif);
  if (gps.length) writeIfd(tiff, tv, gpsAt, gps);
  return seg;
}

// The camera that took a photo (for the new EXIF block).
export async function readCamera(file) {
  try {
    const data = await exifr.parse(file, ['Make', 'Model']);
    return { make: data?.Make ? String(data.Make).trim() : null, model: data?.Model ? String(data.Model).trim() : null };
  } catch {
    return { make: null, model: null };
  }
}

// ── Putting a photo together ──────────────────────
// A newly encoded image (blurred, patched, smaller) with the original's 360° and colour
// metadata and a new EXIF block (or none).
export async function withMetadata(encoded, original, exif = null) {
  const head = new Uint8Array(await original.slice(0, Math.min(original.size, 4 * 1024 * 1024)).arrayBuffer());
  const out = new Uint8Array(await encoded.arrayBuffer());
  const keep = jpegSegments(head).segments
    .filter(s => ['xmp', 'xmpext', 'icc'].includes(kindOf(head, s)))
    .map(s => head.subarray(s.start, s.end));
  // Skip the encoder's own JFIF APP0 so the kept segments follow SOI directly.
  let q = 2;
  if (out[2] === 0xFF && out[3] === 0xE0) q = 4 + ((out[4] << 8) | out[5]);
  return new Blob([out.subarray(0, 2), ...(exif ? [exif] : []), ...keep, out.subarray(q)], { type: 'image/jpeg' });
}

// The XMP segment with the photo's compass direction set (GPano:PoseHeadingDegrees), if it names one.
function xmpWithHeading(seg, heading) {
  const id = XMP_ID.length;
  const xml = new TextDecoder().decode(seg.subarray(4 + id));
  const value = (Math.round(heading * 100) / 100).toString();
  const next = xml
    .replace(/(GPano:PoseHeadingDegrees\s*=\s*")[^"]*(")/, `$1${value}$2`)
    .replace(/(<GPano:PoseHeadingDegrees>)[^<]*(<\/GPano:PoseHeadingDegrees>)/, `$1${value}$2`);
  if (next === xml) return seg;
  const body = new TextEncoder().encode(next);
  const outSeg = new Uint8Array(4 + id + body.length);
  outSeg.set(seg.subarray(0, 4 + id));
  outSeg.set(body, 4 + id);
  if (outSeg.length - 2 > 0xFFFF) return seg;
  new DataView(outSeg.buffer).setUint16(2, outSeg.length - 2);
  return outSeg;
}

// The original photo, its image data untouched, with a new EXIF block in place of its
// own and (if it names one) the new compass direction in its XMP.
export async function replaceExif(original, exif, { heading = null } = {}) {
  const bytes = new Uint8Array(await original.arrayBuffer());
  const { segments, dataStart } = jpegSegments(bytes);
  if (dataStart < 0) throw new Error('Not a JPEG file');
  const parts = [bytes.subarray(0, 2)];
  let placed = false;
  for (const seg of segments) {
    const kind = kindOf(bytes, seg);
    if (kind === 'exif') continue;
    if (!placed && kind !== 'jfif') { parts.push(exif); placed = true; }
    const part = bytes.subarray(seg.start, seg.end);
    parts.push(kind === 'xmp' && heading != null ? xmpWithHeading(part, heading) : part);
  }
  if (!placed) parts.push(exif);
  parts.push(bytes.subarray(dataStart));
  return new Blob(parts, { type: 'image/jpeg' });
}

// ── Reading the header ────────────────────────────
const jpegInfoCache = new WeakMap();

// Size and 360° metadata straight from the JPEG header, without decoding the image.
export async function jpegInfo(file) {
  if (jpegInfoCache.has(file)) return jpegInfoCache.get(file);
  const buf = new Uint8Array(await file.slice(0, Math.min(file.size, 2 * 1024 * 1024)).arrayBuffer());
  const info = { width: null, height: null, gpano: false, jpeg: buf[0] === 0xFF && buf[1] === 0xD8 };
  let p = 2;
  while (p + 9 < buf.length && buf[p] === 0xFF) {
    const m = buf[p + 1];
    const len = (buf[p + 2] << 8) | buf[p + 3];
    if (m >= 0xC0 && m <= 0xCF && ![0xC4, 0xC8, 0xCC].includes(m)) {
      info.height = (buf[p + 5] << 8) | buf[p + 6];
      info.width = (buf[p + 7] << 8) | buf[p + 8];
      break;
    }
    if (m === 0xE1 && !info.gpano) {
      const seg = new TextDecoder('latin1').decode(buf.subarray(p + 4, Math.min(buf.length, p + 2 + len)));
      if (seg.includes('GPano') || seg.includes('equirectangular')) info.gpano = true;
    }
    if (m === 0xDA) break;
    p += 2 + len;
  }
  jpegInfoCache.set(file, info);
  return info;
}
