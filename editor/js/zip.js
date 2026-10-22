// Minimal zip writer/reader for "stored" (uncompressed) entries, which is all .vrtour uses.
const CRC_TABLE = (() => {
  const t = new Uint32Array(256);
  for (let i = 0; i < 256; i++) {
    let c = i;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xEDB88320 ^ (c >>> 1) : c >>> 1;
    t[i] = c >>> 0;
  }
  return t;
})();

async function crc32(blob) {
  let crc = 0xFFFFFFFF;
  const CHUNK = 8 * 1024 * 1024;
  for (let off = 0; off < blob.size; off += CHUNK) {
    const buf = new Uint8Array(await blob.slice(off, off + CHUNK).arrayBuffer());
    for (let i = 0; i < buf.length; i++) crc = CRC_TABLE[(crc ^ buf[i]) & 0xFF] ^ (crc >>> 8);
  }
  return (crc ^ 0xFFFFFFFF) >>> 0;
}

export async function buildZip(entries) {
  const enc = new TextEncoder();
  const parts = [];
  const central = [];
  let offset = 0;
  for (const e of entries) {
    const name = enc.encode(e.name);
    const size = e.data.size;
    if (offset + size >= 0xFFFFFFFF) throw new Error('Project too large (over 4 GB)');
    const crc = await crc32(e.data);
    const local = new DataView(new ArrayBuffer(30));
    local.setUint32(0, 0x04034b50, true);
    local.setUint16(4, 20, true);
    local.setUint16(6, 0x0800, true); // UTF-8 file names
    local.setUint32(14, crc, true);
    local.setUint32(18, size, true);
    local.setUint32(22, size, true);
    local.setUint16(26, name.length, true);
    parts.push(local.buffer, name, e.data);

    const cen = new DataView(new ArrayBuffer(46));
    cen.setUint32(0, 0x02014b50, true);
    cen.setUint16(4, 20, true);
    cen.setUint16(6, 20, true);
    cen.setUint16(8, 0x0800, true);
    cen.setUint32(16, crc, true);
    cen.setUint32(20, size, true);
    cen.setUint32(24, size, true);
    cen.setUint16(28, name.length, true);
    cen.setUint32(42, offset, true);
    central.push(cen.buffer, name);
    offset += 30 + name.length + size;
  }
  const cenSize = central.reduce((sum, b) => sum + b.byteLength, 0);
  const end = new DataView(new ArrayBuffer(22));
  end.setUint32(0, 0x06054b50, true);
  end.setUint16(8, entries.length, true);
  end.setUint16(10, entries.length, true);
  end.setUint32(12, cenSize, true);
  end.setUint32(16, offset, true);
  return new Blob([...parts, ...central, end.buffer], { type: 'application/zip' });
}

export async function readZip(file) {
  const tailLen = Math.min(file.size, 22 + 0xFFFF);
  const tail = new DataView(await file.slice(file.size - tailLen).arrayBuffer());
  let eocd = -1;
  for (let i = tailLen - 22; i >= 0; i--) {
    if (tail.getUint32(i, true) === 0x06054b50) { eocd = i; break; }
  }
  if (eocd < 0) throw new Error('Not a .vrtour / zip file');
  const count = tail.getUint16(eocd + 10, true);
  const cenSize = tail.getUint32(eocd + 12, true);
  const cenOff = tail.getUint32(eocd + 16, true);
  const cen = new DataView(await file.slice(cenOff, cenOff + cenSize).arrayBuffer());
  const dec = new TextDecoder();
  const out = {};
  let p = 0;
  for (let i = 0; i < count; i++) {
    if (cen.getUint32(p, true) !== 0x02014b50) throw new Error('Corrupt zip directory');
    const method = cen.getUint16(p + 10, true);
    const size = cen.getUint32(p + 20, true);
    const nameLen = cen.getUint16(p + 28, true);
    const extraLen = cen.getUint16(p + 30, true);
    const commentLen = cen.getUint16(p + 32, true);
    const localOff = cen.getUint32(p + 42, true);
    const name = dec.decode(new Uint8Array(cen.buffer, p + 46, nameLen));
    p += 46 + nameLen + extraLen + commentLen;
    if (method !== 0) throw new Error(`${name} is compressed - only .vrtour files saved by this editor can be opened`);
    const lh = new DataView(await file.slice(localOff, localOff + 30).arrayBuffer());
    const dataStart = localOff + 30 + lh.getUint16(26, true) + lh.getUint16(28, true);
    out[name] = file.slice(dataStart, dataStart + size);
  }
  return out;
}
