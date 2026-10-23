// Sharing a tour: QR codes (to print, or put on a sign at the place) and the code
// that embeds an exported tour website in another web page.
import qrcode from '../vendor/qrcode/qrcode.mjs';

// UTF-8, so addresses with umlauts work too (the library takes 8-bit text).
qrcode.stringToBytes = s => [...new TextEncoder().encode(s)];

function make(text) {
  const qr = qrcode(0, 'M'); // the smallest size that fits; medium error correction
  qr.addData(text);
  qr.make();
  return qr;
}

export function qrSvg(text, title = '') {
  return make(text).createSvgTag({ cellSize: 8, margin: 32, scalable: true, title });
}

// A PNG of the code, `size` pixels wide (with the white margin a scanner needs).
export function qrPng(text, size = 1024) {
  const qr = make(text);
  const n = qr.getModuleCount();
  const cell = Math.floor(size / (n + 8));
  const margin = Math.floor((size - cell * n) / 2);
  const canvas = new OffscreenCanvas(size, size);
  const ctx = canvas.getContext('2d');
  ctx.fillStyle = '#fff';
  ctx.fillRect(0, 0, size, size);
  ctx.fillStyle = '#000';
  for (let r = 0; r < n; r++) {
    for (let c = 0; c < n; c++) if (qr.isDark(r, c)) ctx.fillRect(margin + c * cell, margin + r * cell, cell, cell);
  }
  return canvas.convertToBlob({ type: 'image/png' });
}

const attr = s => String(s).replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;');

export function embedCode(url, title) {
  return `<iframe src="${attr(url)}" title="${attr(title)}" width="100%" height="520" style="border:0;border-radius:12px" allow="fullscreen" allowfullscreen loading="lazy"></iframe>`;
}

export function isWebAddress(s) {
  try {
    const u = new URL(s);
    return (u.protocol === 'https:' || u.protocol === 'http:') && !!u.hostname;
  } catch {
    return false;
  }
}
