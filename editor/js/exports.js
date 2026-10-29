// Exports for other apps: the photos with their position and north written into them
// (Mapillary, Panoramax, ...), and the tour as KML (Google Earth, My Maps) or GPX (GPS apps).
import { buildZip } from './zip.js';

// From the editor (initExports).
let S, t, toast, gn, projectBase, saveTextFile, fileBase, walkingOrder, getLinkedNodes, isSample, safeFileName, photoWithPose, setSaveStatus;
export function initExports(ctx) {
  ({ S, t, toast, gn, projectBase, saveTextFile, fileBase, walkingOrder, getLinkedNodes, isSample, safeFileName, photoWithPose, setSaveStatus } = ctx);
}

export async function exportPhotos(scope = null) {
  const nodes = (scope?.nodes || S.nodes).filter(n => n.photoFile && !isSample(n));
  if (!nodes.length) { alert('None of the nodes has a photo loaded.'); return; }
  setSaveStatus('saving', 'Packing photos…');
  const used = new Set();
  const entries = [];
  let skipped = 0;
  for (const n of nodes) {
    let name = safeFileName(n.photoName || `${n.id}.jpg`);
    if (used.has(name.toLowerCase())) name = `${n.id}-${name}`;
    used.add(name.toLowerCase());
    try {
      entries.push({ name: `photos/${name}`, data: await photoWithPose(n) });
    } catch {
      skipped++; // not a JPEG
    }
  }
  const readme = [
    'Streetview Tour Editor - photos with position and north',
    '',
    'Every photo in photos/ carries its position (GPS), north (GPSImgDirection and',
    'GPano:PoseHeadingDegrees) and capture time in its metadata, as set in the editor.',
    '',
    'Mapillary:  mapillary_tools process_and_upload photos/',
    'Panoramax:  panoramax_cli upload --api-url https://panoramax.openstreetmap.fr photos/',
    '',
  ].join('\r\n');
  try {
    const zip = await buildZip([{ name: 'README.txt', data: new Blob([readme], { type: 'text/plain' }) }, ...entries]);
    setSaveStatus('saved', 'Saved');
    await saveTextFile(`${fileBase(scope?.name || projectBase() || 'tour')}-photos.zip`, zip);
    if (skipped) setTimeout(() => toast(t('{n} photo(s) are not JPEG files and were left out', { n: skipped })), 400);
  } catch (err) {
    setSaveStatus(null, 'Export failed');
    alert(`Export failed: ${err.message || err}`);
  }
}


const xmlText = s => String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

function scopeLinks(nodes) {
  const ids = new Set(nodes.map(n => n.id));
  return S.conns.filter(c => ids.has(c.from) && ids.has(c.to)).map(c => [gn(c.from), gn(c.to)]);
}

export async function exportKml(scope) {
  const nodes = (scope?.nodes || S.nodes).filter(n => n.lat != null);
  const name = scope?.name || projectBase() || t('Tour');
  const place = n => [
    `<Placemark><name>${xmlText(n.name)}</name>`,
    n.gsvShareLink ? `<description>${xmlText(n.gsvShareLink)}</description>` : '',
    `<styleUrl>#${S.starts.has(n.id) ? 'start' : 'point'}</styleUrl>`,
    `<Point><coordinates>${n.lng.toFixed(7)},${n.lat.toFixed(7)},0</coordinates></Point></Placemark>`,
  ].join('');
  const line = ([a, b]) => `<Placemark><styleUrl>#link</styleUrl><LineString><tessellate>1</tessellate><coordinates>${a.lng.toFixed(7)},${a.lat.toFixed(7)},0 ${b.lng.toFixed(7)},${b.lat.toFixed(7)},0</coordinates></LineString></Placemark>`;
  const kml = `<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2"><Document><name>${xmlText(name)}</name>
<Style id="point"><IconStyle><color>fff65c8b</color><scale>0.8</scale></IconStyle></Style>
<Style id="start"><IconStyle><color>ff3bb9f6</color><scale>1.1</scale></IconStyle></Style>
<Style id="link"><LineStyle><color>fff65c8b</color><width>3</width></LineStyle></Style>
<Folder><name>${xmlText(t('Points'))}</name>${nodes.map(place).join('\n')}</Folder>
<Folder><name>${xmlText(t('Links'))}</name>${scopeLinks(nodes).map(line).join('\n')}</Folder>
</Document></kml>
`;
  await saveTextFile(`${fileBase(name)}.kml`, kml);
}

export async function exportGpx(scope) {
  const nodes = (scope?.nodes || S.nodes).filter(n => n.lat != null);
  const name = scope?.name || projectBase() || t('Tour');
  const wpt = n => `<wpt lat="${n.lat.toFixed(7)}" lon="${n.lng.toFixed(7)}"><name>${xmlText(n.name)}</name>${n.gsvShareLink ? `<link href="${xmlText(n.gsvShareLink)}"/>` : ''}</wpt>`;
  // The walk itself: in walking order from the start, a new segment where it jumps.
  const order = walkingOrder(nodes);
  const segs = [];
  order.forEach((n, i) => {
    const prev = order[i - 1];
    if (!prev || !getLinkedNodes(n.id).includes(prev)) segs.push([]);
    segs[segs.length - 1].push(n);
  });
  const trk = segs.filter(sg => sg.length > 1)
    .map(sg => `<trkseg>${sg.map(n => `<trkpt lat="${n.lat.toFixed(7)}" lon="${n.lng.toFixed(7)}"/>`).join('')}</trkseg>`).join('\n');
  const gpx = `<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" creator="Streetview Tour Editor" xmlns="http://www.topografix.com/GPX/1/1">
<metadata><name>${xmlText(name)}</name></metadata>
${nodes.map(wpt).join('\n')}
<trk><name>${xmlText(name)}</name>
${trk}
</trk>
</gpx>
`;
  await saveTextFile(`${fileBase(name)}.gpx`, gpx);
}
