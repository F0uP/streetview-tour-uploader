// How often a tour is viewed on Google, over time, and a report of a tour for whoever it
// was made for: what is on Google and where, how often it was viewed and how that grew,
// with the link and a QR code to the tour - one HTML page to send, or print as PDF.
import { formatDistance, toLocal } from './geo.js';
import { escapeHtml } from './util.js';
import { qrSvg } from './share.js';

// ── Views over time ───────────────────────────────
// Google only says how often a photo was viewed so far; each time the editor asks, the
// count is noted per day in the point (viewsLog: [[day, count], ...]).
export function logViews(n, count) {
  if (count == null) return false;
  const day = new Date().toISOString().slice(0, 10);
  const log = Array.isArray(n.viewsLog) ? n.viewsLog : [];
  const last = log[log.length - 1];
  if (last && last[0] === day) {
    if (last[1] === count) return false;
    n.viewsLog = [...log.slice(0, -1), [day, count]];
  } else {
    n.viewsLog = [...log, [day, count]].slice(-730);
  }
  return true;
}

// The views of several points added up, per day noted: [[day, total], ...].
export function viewsSeries(nodes) {
  const days = [...new Set(nodes.flatMap(n => (n.viewsLog || []).map(e => e[0])))].sort();
  return days.map(day => [day, nodes.reduce((sum, n) => {
    let count = 0;
    for (const [d, c] of n.viewsLog || []) if (d <= day) count = c;
    return sum + count;
  }, 0)]);
}

// A small line chart of a series, as SVG: the views so far top left (they only grow, so the
// line stays clear of it), the first and the last day below.
export function viewsChart(series, { width = 560, height = 140, label = '', locale = undefined } = {}) {
  if (series.length < 2) return '';
  const max = Math.max(1, ...series.map(s => s[1]));
  const t0 = Date.parse(series[0][0]), t1 = Date.parse(series[series.length - 1][0]) || t0 + 1;
  const x = d => 8 + ((Date.parse(d) - t0) / Math.max(1, t1 - t0)) * (width - 16);
  const y = v => height - 22 - (v / max) * (height - 44); // room above for the figure
  const line = series.map(([d, v], i) => `${i ? 'L' : 'M'}${x(d).toFixed(1)},${y(v).toFixed(1)}`).join(' ');
  return `<svg class="views-chart" viewBox="0 0 ${width} ${height}" role="img" aria-label="${escapeHtml(label)}">`
    + `<path d="${line} L${x(series[series.length - 1][0]).toFixed(1)},${height - 22} L8,${height - 22} Z" fill="currentColor" opacity=".12"/>`
    + `<path d="${line}" fill="none" stroke="currentColor" stroke-width="2"/>`
    + `<text x="8" y="${height - 6}" font-size="11" fill="currentColor" opacity=".7">${escapeHtml(series[0][0])}</text>`
    + `<text x="${width - 8}" y="${height - 6}" font-size="11" text-anchor="end" fill="currentColor" opacity=".7">${escapeHtml(series[series.length - 1][0])}</text>`
    + `<text x="8" y="16" font-size="13" font-weight="700" fill="currentColor">${escapeHtml(series[series.length - 1][1].toLocaleString(locale))}</text></svg>`;
}

// ── The report ────────────────────────────────────
// From the editor (initTourReport): { S, t, lang, gn, links(nodes), statusOf(n), placeOf(n), tourLength(nodes),
// saveTextFile, fileBase, projectBase, setSaveStatus }
let ctx;
export function initTourReport(c) { ctx = c; }

async function thumbData(file) {
  try {
    const bmp = await createImageBitmap(file, { resizeWidth: 240, resizeHeight: 120, resizeQuality: 'medium' });
    const canvas = new OffscreenCanvas(240, 120);
    canvas.getContext('2d').drawImage(bmp, 0, 0);
    bmp.close?.();
    const blob = await canvas.convertToBlob({ type: 'image/jpeg', quality: 0.72 });
    return await new Promise(resolve => {
      const r = new FileReader();
      r.onload = () => resolve(r.result);
      r.onerror = () => resolve('');
      r.readAsDataURL(blob);
    });
  } catch {
    return '';
  }
}

// The points and links as a little map (no tiles: it works offline and prints).
function plan(nodes, links, start) {
  const pts = nodes.filter(n => n.lat != null);
  if (!pts.length) return '';
  const o = { lat: pts.reduce((a, n) => a + n.lat, 0) / pts.length, lng: pts.reduce((a, n) => a + n.lng, 0) / pts.length };
  const local = new Map(pts.map(n => [n.id, toLocal(n, o)]));
  const xs = [...local.values()].map(p => p.x), ys = [...local.values()].map(p => p.y);
  const W = 640, H = 360, pad = 30;
  // Metres to pixels: the tour fills the picture (at least 10 m across).
  const k = Math.min((W - 2 * pad) / Math.max(10, Math.max(...xs) - Math.min(...xs)), (H - 2 * pad) / Math.max(10, Math.max(...ys) - Math.min(...ys)));
  const cx = (Math.max(...xs) + Math.min(...xs)) / 2, cy = (Math.max(...ys) + Math.min(...ys)) / 2;
  const P = n => { const p = local.get(n.id); return [W / 2 + (p.x - cx) * k, H / 2 - (p.y - cy) * k]; };
  const lines = links.filter(([a, b]) => local.has(a.id) && local.has(b.id))
    .map(([a, b]) => { const [x1, y1] = P(a), [x2, y2] = P(b); return `<line x1="${x1.toFixed(1)}" y1="${y1.toFixed(1)}" x2="${x2.toFixed(1)}" y2="${y2.toFixed(1)}"/>`; }).join('');
  const dots = pts.map((n, i) => {
    const [x, y] = P(n);
    return `<g class="${n === start ? 'start' : ''}"><circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="${n === start ? 9 : 7}"/><text x="${x.toFixed(1)}" y="${(y + 3.5).toFixed(1)}">${i + 1}</text></g>`;
  }).join('');
  // A round length for the scale bar (1, 2 or 5 times a power of ten), at most a quarter of the width.
  const quarter = W / 4 / k, unit = Math.pow(10, Math.floor(Math.log10(quarter)));
  const scale = [5, 2, 1].map(f => f * unit).find(v => v <= quarter) || unit;
  return `<svg class="plan" viewBox="0 0 ${W} ${H}" role="img"><g class="links">${lines}</g>${dots}`
    + `<g class="north"><path d="M${W - 24},${pad + 22} l8,-22 l8,22 l-8,-6 z"/><text x="${W - 16}" y="${pad + 36}">N</text></g>`
    + `<g class="scale"><line x1="${pad}" y1="${H - 14}" x2="${(pad + scale * k).toFixed(1)}" y2="${H - 14}"/><text x="${pad}" y="${H - 20}">${formatDistance(scale)}</text></g></svg>`;
}

export async function exportReport(scope = null) {
  const { S, t } = ctx;
  const nodes = scope?.nodes || S.nodes;
  if (!nodes.length) { alert('No nodes found.'); return; }
  ctx.setSaveStatus('saving', 'Packing tour…');
  const name = scope?.name || ctx.projectBase() || t('Tour');
  const locale = ctx.lang() === 'de' ? 'de-DE' : 'en-GB';
  const start = nodes.find(n => S.starts.has(n.id)) || nodes[0];
  const onGoogle = nodes.filter(n => n.gsvPhotoId);
  const published = onGoogle.filter(n => n.gsvStatus === 'published').length;
  const views = nodes.reduce((sum, n) => sum + (n.gsvViews || 0), 0);
  const link = [start, ...nodes].find(n => n?.gsvShareLink)?.gsvShareLink || '';
  const series = viewsSeries(nodes);
  const links = ctx.links(nodes);
  const rows = [];
  for (const [i, n] of nodes.entries()) {
    const thumb = n.photoFile ? await thumbData(n.photoFile) : '';
    rows.push(`<tr><td class="num">${i + 1}</td><td>${thumb ? `<img src="${thumb}" alt="">` : ''}</td><td><b>${escapeHtml(n.name)}</b>`
      + `${ctx.placeOf(n) ? `<div class="sub">${escapeHtml(ctx.placeOf(n))}</div>` : ''}</td>`
      + `<td>${escapeHtml(ctx.statusOf(n))}</td><td class="num">${n.gsvViews != null ? n.gsvViews.toLocaleString(locale) : '–'}</td>`
      + `<td>${n.gsvShareLink ? `<a href="${escapeHtml(n.gsvShareLink)}">${escapeHtml(t('open'))}</a>` : ''}</td></tr>`);
  }
  const figures = [
    [t('Photos'), nodes.length],
    [onGoogle.length ? t('On Google · {n} published', { n: published }) : t('On Google'), onGoogle.length],
    [t('Views'), views.toLocaleString(locale)],
    [t('Length of the links'), formatDistance(ctx.tourLength(nodes))],
  ];
  const html = `<!DOCTYPE html>
<html lang="${ctx.lang()}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>${escapeHtml(name)} – ${escapeHtml(t('Tour report'))}</title>
<style>
body{font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;color:#1b1a20;margin:0;background:#f5f5f8}
main{max-width:860px;margin:0 auto;padding:32px 28px;background:#fff}
h1{font-size:26px;margin:0 0 4px}h2{font-size:16px;margin:28px 0 10px}.sub{color:#6d6a7a;font-size:12.5px}
.figures{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:20px 0}
.figures div{border:1px solid #e7e6ec;border-radius:10px;padding:12px}.figures b{display:block;font-size:22px}
.plan{width:100%;height:auto;border:1px solid #e7e6ec;border-radius:10px;background:#fafafc}
.plan .links line{stroke:#8b5cf6;stroke-width:3}.plan circle{fill:#8b5cf6;stroke:#fff;stroke-width:2}.plan .start circle{fill:#f2a91f}
.plan text{font:700 9px system-ui;fill:#fff;text-anchor:middle}.plan .north path{fill:#1b1a20}.plan .north text,.plan .scale text{fill:#1b1a20;font-size:11px;font-weight:600}
.plan .scale line{stroke:#1b1a20;stroke-width:2}.plan .north text{text-anchor:middle}.plan .scale text{text-anchor:start}
.views-chart{width:100%;height:auto;color:#8b5cf6}
.share{display:flex;gap:18px;align-items:center}.share svg{width:120px;height:120px}
table{width:100%;border-collapse:collapse;font-size:13px}th,td{text-align:left;padding:6px 8px;border-bottom:1px solid #eeedf2;vertical-align:middle}
td img{width:120px;height:60px;object-fit:cover;border-radius:6px;display:block}.num{text-align:right;font-variant-numeric:tabular-nums}
a{color:#6a3be8}footer{margin-top:28px;color:#8c8998;font-size:12px}
@media print{body{background:#fff}main{padding:0}a{color:inherit;text-decoration:none}tr{break-inside:avoid}}
</style></head><body><main>
<h1>${escapeHtml(name)}</h1>
<div class="sub">${escapeHtml(t('Tour report'))} · ${escapeHtml(new Date().toLocaleDateString(locale, { dateStyle: 'long' }))}</div>
<div class="figures">${figures.map(([k, v]) => `<div><b>${escapeHtml(String(v))}</b>${escapeHtml(k)}</div>`).join('')}</div>
<h2>${escapeHtml(t('The tour'))}</h2>
${plan(nodes, links, start)}
${series.length > 1 ? `<h2>${escapeHtml(t('Views on Google over time'))}</h2>${viewsChart(series, { label: t('Views on Google over time'), locale })}` : ''}
${link ? `<h2>${escapeHtml(t('On Google Maps'))}</h2><div class="share">${qrSvg(link, name)}<div><a href="${escapeHtml(link)}">${escapeHtml(link)}</a><div class="sub">${escapeHtml(t('The link opens the tour\'s start photo in Google Maps, where visitors walk on from there.'))}</div></div></div>` : ''}
<h2>${escapeHtml(t('Photos'))}</h2>
<table><thead><tr><th class="num">#</th><th></th><th>${escapeHtml(t('Name'))}</th><th>${escapeHtml(t('On Google'))}</th><th class="num">${escapeHtml(t('Views'))}</th><th></th></tr></thead>
<tbody>${rows.join('\n')}</tbody></table>
<footer>${escapeHtml(t('Made with Streetview Tour Editor.'))}</footer>
</main></body></html>`;
  ctx.setSaveStatus('saved', 'Saved');
  await ctx.saveTextFile(`${ctx.fileBase(name)}-report.html`, new Blob([html], { type: 'text/html' }));
}
