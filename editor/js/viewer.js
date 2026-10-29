// The tour viewer: the page the preview, the website export and the viewer files are made of.
import { degToRad, escapeHtml } from './util.js';

export const VIEWER_LICENSES = [
  'Libraries of this tour viewer:',
  '  Photo Sphere Viewer 5.11.5 - MIT License - https://photo-sphere-viewer.js.org/',
  '  three.js 0.169.0 - MIT License - https://threejs.org/',
  '  Leaflet 1.9.4 - BSD 2-Clause License - https://leafletjs.com/',
  '',
].join('\r\n');

// The tour viewer's libraries, as paths under editor/vendor/ and on the CDN.
// libBase decides where a generated viewer loads them from: null = the CDN
// (viewer files placed next to the photos on a web server), the app's own
// copies (preview), or a folder of the exported website.
export const VIEWER_LIBS = {
  'leaflet/leaflet.css': 'https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.css',
  'leaflet/leaflet.js': 'https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.js',
  'three/three.module.min.js': 'https://cdn.jsdelivr.net/npm/three@0.169.0/build/three.module.min.js',
  'psv/core/index.css': 'https://cdn.jsdelivr.net/npm/@photo-sphere-viewer/core@5.11.5/index.css',
  'psv/core/index.module.js': 'https://cdn.jsdelivr.net/npm/@photo-sphere-viewer/core@5.11.5/index.module.js',
  'psv/markers-plugin/index.css': 'https://cdn.jsdelivr.net/npm/@photo-sphere-viewer/markers-plugin@5.11.5/index.css',
  'psv/markers-plugin/index.module.js': 'https://cdn.jsdelivr.net/npm/@photo-sphere-viewer/markers-plugin@5.11.5/index.module.js',
  'psv/virtual-tour-plugin/index.css': 'https://cdn.jsdelivr.net/npm/@photo-sphere-viewer/virtual-tour-plugin@5.11.5/index.css',
  'psv/virtual-tour-plugin/index.module.js': 'https://cdn.jsdelivr.net/npm/@photo-sphere-viewer/virtual-tour-plugin@5.11.5/index.module.js',
};

// Where the page may load scripts from: its own scripts (with the nonce) and its libraries.
function scriptSources(libBase) {
  if (libBase == null) return 'https://cdn.jsdelivr.net';
  try { return new URL(libBase).origin; } catch { return "'self'"; }
}

// lang and mapLabel: the editor's language, for the page and its one button.
// A point's name can't run anything, even if it held markup: the libraries show names
// as HTML (the arrows' and the minimap's tooltips), so the page gets them escaped
// (name; label is the plain text), and a Content-Security-Policy lets only the page's
// own scripts run.
export function genViewer(startId, nodes, { editable = false, libBase = null, title = null, lang = 'en', mapLabel = 'Map' } = {}) {
  const sn = nodes.find(n => n.id === startId) || nodes[0];
  const closeScript = '<' + '/script>';
  const hasMap = nodes.filter(n => n.lat != null && n.lng != null).length > 1;
  const lib = path => (libBase == null ? VIEWER_LIBS[path] : libBase + path);
  const nonce = Array.from(crypto.getRandomValues(new Uint8Array(16)), b => b.toString(16).padStart(2, '0')).join('');
  const pageNodes = nodes.map(n => ({ ...n, name: escapeHtml(n.name), label: n.name }));
  return `<!DOCTYPE html>
<html lang="${lang}">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<meta http-equiv="Content-Security-Policy" content="script-src 'nonce-${nonce}' ${scriptSources(libBase)}; object-src 'none'; base-uri 'none'">
<title>${escapeHtml(title || `${sn.name} - Tour`)}</title>
<link rel="stylesheet" href="${lib('psv/core/index.css')}">
<link rel="stylesheet" href="${lib('psv/virtual-tour-plugin/index.css')}">
<link rel="stylesheet" href="${lib('psv/markers-plugin/index.css')}">
${hasMap ? `<link rel="stylesheet" href="${lib('leaflet/leaflet.css')}">` : ''}
<style>
*{margin:0;padding:0;box-sizing:border-box}html,body{height:100%;background:#000}#viewer{width:100vw;height:100vh}
#badge{position:fixed;top:12px;left:50%;transform:translateX(-50%);background:rgba(0,0,0,.6);color:#fff;padding:5px 16px;border-radius:999px;font-family:system-ui,sans-serif;font-size:13px;backdrop-filter:blur(6px);pointer-events:none;z-index:10}
.hs-pin{width:26px;height:26px;border-radius:50%;background:rgba(139,92,246,.92);border:2px solid #fff;color:#fff;font:700 14px/22px Georgia,serif;text-align:center;box-shadow:0 2px 10px rgba(0,0,0,.5);cursor:pointer}
.psv-panel-content h2{font-family:system-ui,sans-serif;margin-bottom:10px}.psv-panel-content p{font-family:system-ui,sans-serif;line-height:1.6;margin-bottom:10px}.psv-panel-content a{color:#c4b5fd}
#mm{position:fixed;left:12px;bottom:52px;width:240px;height:170px;border-radius:10px;overflow:hidden;border:2px solid rgba(255,255,255,.7);box-shadow:0 6px 24px rgba(0,0,0,.5);z-index:10;transition:height .2s,width .2s}
#mm.small{width:44px;height:44px}#mm.small .leaflet-control-container,#mm.small .leaflet-pane{display:none}
#mm .leaflet-control-attribution{font:9px/1.4 system-ui,sans-serif;padding:0 4px;background:rgba(255,255,255,.75)}
#mm-t{position:absolute;right:4px;top:4px;z-index:1000;border:none;border-radius:6px;background:rgba(0,0,0,.65);color:#fff;font:600 11px system-ui,sans-serif;padding:3px 7px;cursor:pointer}
#mm.small #mm-t{inset:0;width:100%;height:100%;border-radius:8px;font-size:18px}
@media (prefers-color-scheme:dark){#mm .leaflet-tile-pane{filter:invert(1) hue-rotate(180deg) brightness(.92) contrast(.88) saturate(.7)}#mm{background:#1b1d22}}
</style>
</head>
<body>
<div id="viewer"></div>
<div id="badge">${escapeHtml(sn.name)}</div>
${hasMap ? `<div id="mm"><button id="mm-t" title="${escapeHtml(mapLabel)}">✕</button></div>
<script nonce="${nonce}" src="${lib('leaflet/leaflet.js')}">${closeScript}` : ''}
<script type="importmap" nonce="${nonce}">${JSON.stringify({ imports: {
  three: lib('three/three.module.min.js'),
  '@photo-sphere-viewer/core': lib('psv/core/index.module.js'),
  '@photo-sphere-viewer/virtual-tour-plugin': lib('psv/virtual-tour-plugin/index.module.js'),
  '@photo-sphere-viewer/markers-plugin': lib('psv/markers-plugin/index.module.js'),
} })}${closeScript}
<script type="module" nonce="${nonce}">
import { Viewer } from '@photo-sphere-viewer/core';
import { VirtualTourPlugin } from '@photo-sphere-viewer/virtual-tour-plugin';
import { MarkersPlugin } from '@photo-sphere-viewer/markers-plugin';
const NODES=${JSON.stringify(pageNodes).replace(/</g, '\\u003c')};
const EDIT=${editable ? 'true' : 'false'};
const START=${JSON.stringify(startId)};
const viewer=new Viewer({container:document.getElementById('viewer'),panoData:()=>({poseHeading:0}),panorama:NODES.find(n=>n.id===START)?.panorama??NODES[0].panorama,navbar:['zoom','move','fullscreen'],defaultZoomLvl:0,defaultYaw:${sn.heading != null ? +degToRad(sn.heading).toFixed(4) : 0},plugins:[MarkersPlugin,[VirtualTourPlugin,{nodes:NODES,startNodeId:START,arrowStyle:{scale:[0.5,2]}}]]});
const tour=viewer.getPlugin(VirtualTourPlugin);
const mmEl=document.getElementById('mm');
let mark=()=>{};
if(mmEl&&window.L){
  const mm=L.map(mmEl,{zoomControl:false});
  L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:19,attribution:'&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a>'}).addTo(mm);
  mm.attributionControl.setPrefix(false);
  const pts=NODES.filter(n=>n.lat!=null&&n.lng!=null);const byId=Object.fromEntries(pts.map(n=>[n.id,n]));
  pts.forEach(n=>n.links.forEach(l=>{const o=byId[l.nodeId];if(o&&n.id<o.id)L.polyline([[n.lat,n.lng],[o.lat,o.lng]],{color:'#8b5cf6',weight:3,opacity:.8}).addTo(mm);}));
  const dots=Object.fromEntries(pts.map(n=>[n.id,L.circleMarker([n.lat,n.lng],{radius:5,color:'#fff',weight:1.5,fillColor:'#8b5cf6',fillOpacity:1}).addTo(mm).bindTooltip(n.name).on('click',()=>tour.setCurrentNode(n.id))]));
  mm.fitBounds(L.latLngBounds(pts.map(n=>[n.lat,n.lng])),{padding:[18,18],maxZoom:19});
  mark=id=>{Object.entries(dots).forEach(([k,d])=>{d.setStyle({fillColor:k===id?'#f6b93b':'#8b5cf6',radius:k===id?7:5});if(k===id)d.bringToFront();});};
  mark(START);
  document.getElementById('mm-t').onclick=()=>{const s=mmEl.classList.toggle('small');document.getElementById('mm-t').textContent=s?'🗺':'✕';setTimeout(()=>mm.invalidateSize(),250);};
}
tour.addEventListener('node-changed',({node})=>{document.getElementById('badge').textContent=node.label;mark(node.id);if(EDIT)parent.postMessage({type:'svt-node',id:node.id},'*');});
if(EDIT){
  window.psv={viewer,tour};
  viewer.addEventListener('ready',()=>parent.postMessage({type:'svt-node',id:tour.getCurrentNode()?.id||START},'*'),{once:true});
  window.addEventListener('message',e=>{const m=e.data;if(!m||m.type!=='svt-heading')return;const n=NODES.find(x=>x.id===m.id);if(!n)return;
    n.sphereCorrection=m.sphereCorrection;n.markers=m.markers||[];
    if(tour.getCurrentNode()?.id===m.id){viewer.setOption('sphereCorrection',m.sphereCorrection);viewer.getPlugin(MarkersPlugin)?.setMarkers(n.markers);}});
}
${closeScript}
</body>
</html>`;
}
