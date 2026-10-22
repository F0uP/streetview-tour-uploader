// 360° videos for Google's photo sequences: what the video file says about itself,
// and the GPS track that goes with it. Read in pieces - a video can be many GB.

const MAC_EPOCH = Date.UTC(1904, 0, 1); // MP4 times count seconds from 1904
const CONTAINERS = new Set(['moov', 'trak', 'mdia', 'minf', 'stbl', 'udta', 'edts']);

async function view(file, start, length) {
  return new DataView(await file.slice(start, start + length).arrayBuffer());
}

function fourcc(dv, o) {
  return String.fromCharCode(dv.getUint8(o), dv.getUint8(o + 1), dv.getUint8(o + 2), dv.getUint8(o + 3));
}

// The boxes at the top of the file (ftyp, mdat, moov, ...), found by jumping from header to header.
async function topBoxes(file) {
  const boxes = [];
  let pos = 0;
  while (pos + 8 <= file.size && boxes.length < 500) {
    const dv = await view(file, pos, 16);
    let size = dv.getUint32(0), head = 8;
    if (size === 1 && dv.byteLength >= 16) { size = Number(dv.getBigUint64(8)); head = 16; }
    else if (size === 0) size = file.size - pos;
    if (size < head) break;
    boxes.push({ type: fourcc(dv, 4), start: pos, size, head });
    pos += size;
  }
  return boxes;
}

// The boxes inside a box's bytes, descending into the containers.
function walk(dv, start, end, out) {
  let pos = start;
  while (pos + 8 <= end) {
    let size = dv.getUint32(pos), head = 8;
    if (size === 1) { size = Number(dv.getBigUint64(pos + 8)); head = 16; }
    else if (size === 0) size = end - pos;
    if (size < head || pos + size > end) break;
    const type = fourcc(dv, pos + 4);
    out.push({ type, start: pos + head, end: pos + size });
    if (CONTAINERS.has(type)) walk(dv, pos + head, pos + size, out);
    pos += size;
  }
  return out;
}

// { duration (s), created (ms, as the file says - often the camera's local time
// written as UTC), width, height, spherical, camm, gpmd } - or null for no MP4.
export async function readVideoInfo(file) {
  const moov = (await topBoxes(file)).find(b => b.type === 'moov');
  if (!moov || moov.size > 200 * 1024 * 1024) return null;
  const dv = await view(file, moov.start + moov.head, moov.size - moov.head);
  const boxes = walk(dv, 0, dv.byteLength, []);
  const info = { duration: null, created: null, width: null, height: null, spherical: false, camm: false, gpmd: false };
  const mvhd = boxes.find(b => b.type === 'mvhd');
  if (mvhd) {
    const v1 = dv.getUint8(mvhd.start) === 1;
    const created = v1 ? Number(dv.getBigUint64(mvhd.start + 4)) : dv.getUint32(mvhd.start + 4);
    const scale = dv.getUint32(mvhd.start + (v1 ? 20 : 12));
    const duration = v1 ? Number(dv.getBigUint64(mvhd.start + 24)) : dv.getUint32(mvhd.start + 16);
    if (created) info.created = MAC_EPOCH + created * 1000;
    if (scale) info.duration = duration / scale;
  }
  // The video track's size: tkhd of the trak whose handler is 'vide'.
  let tkhd = null;
  boxes.forEach(b => {
    if (b.type === 'tkhd') tkhd = b;
    if (b.type === 'hdlr' && tkhd && fourcc(dv, b.start + 8) === 'vide' && !info.width) {
      const at = tkhd.start + (dv.getUint8(tkhd.start) === 1 ? 88 : 76);
      info.width = dv.getUint32(at) >>> 16;
      info.height = dv.getUint32(at + 4) >>> 16;
    }
  });
  const text = new TextDecoder('latin1').decode(new Uint8Array(dv.buffer, dv.byteOffset, dv.byteLength));
  info.spherical = text.includes('sv3d') || text.includes('GSpherical');
  info.camm = text.includes('camm');
  info.gpmd = text.includes('gpmd');
  return info;
}

// The GPS points for a video from a GPX track (points sorted by time, t in ms):
// those during the video (and 10 s around it), with points filled in where the
// track has gaps of up to 30 s - Google refuses gaps over 5 s.
export function gpsTimeline(points, startMs, durationS) {
  const endMs = startMs + durationS * 1000;
  const inside = points.filter(p => p.t >= startMs - 10000 && p.t <= endMs + 10000);
  const out = [];
  let filled = 0, maxGap = 0;
  inside.forEach((p, i) => {
    const prev = inside[i - 1];
    if (prev) {
      const gap = p.t - prev.t;
      if (gap > 5000 && gap <= 30000) {
        const steps = Math.ceil(gap / 2000);
        for (let k = 1; k < steps; k++) {
          const f = k / steps;
          out.push({ lat: prev.lat + (p.lat - prev.lat) * f, lng: prev.lng + (p.lng - prev.lng) * f, t: Math.round(prev.t + gap * f) });
          filled++;
        }
      } else if (gap > 30000) {
        maxGap = Math.max(maxGap, gap);
      }
    }
    out.push(p);
  });
  const first = inside[0]?.t, last = inside[inside.length - 1]?.t;
  const covered = inside.length ? Math.max(0, Math.min(last, endMs) - Math.max(first, startMs)) : 0;
  return {
    points: out,
    recorded: inside.length,
    filled,
    gapS: Math.round(maxGap / 1000), // the longest gap left (over 30 s), 0 if none
    coverage: durationS ? Math.min(1, covered / (durationS * 1000)) : 0,
  };
}

// When the track and the video do not overlap, it is usually the camera's clock in
// local time: the whole-hour shift (up to 14 h) that makes them overlap best.
export function hourShift(points, startMs, durationS) {
  let best = { hours: 0, coverage: gpsTimeline(points, startMs, durationS).coverage };
  for (let h = -14; h <= 14; h++) {
    if (!h) continue;
    const coverage = gpsTimeline(points, startMs + h * 3600000, durationS).coverage;
    if (coverage > best.coverage + 0.2) best = { hours: h, coverage };
  }
  return best;
}
