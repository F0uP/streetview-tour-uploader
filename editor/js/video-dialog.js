// 360° video to Street View: the dialog. Google makes the photos of a 360° video
// itself (a "photo sequence"): it picks the frames, places them along the GPS track
// and links them. Here the video is checked first (video.js reads what the file
// says), then sent through the app to Google.
import { escapeHtml } from './util.js';
import { parseGpx } from './gpx.js';
import { gpsTimeline, hourShift, readVideoInfo } from './video.js';

// From the editor (initVideoDialog): translation, messages and its other helpers.
let t, lang, toast, openModal, closeMenus, setAppBusy, storageGet, storageSet, formatBytes, formatEta, formatDistance, editorKey;
export function initVideoDialog(ctx) {
  ({ t, lang, toast, openModal, closeMenus, setAppBusy, storageGet, storageSet, formatBytes, formatEta, formatDistance, editorKey } = ctx);
}

// Google makes the photos of a 360° video itself (a "photo sequence"): it picks the
// frames, places them along the GPS track and links them. Here the video is checked
// first (video.js reads what the file says), then streamed through the app to Google.
const VIDEO_FAILURES = {
  LOW_RESOLUTION: 'the resolution is too low',
  DUPLICATE: 'this video was uploaded before',
  INSUFFICIENT_GPS: 'too few GPS points',
  NO_OVERLAP_GPS: 'the GPS track and the video do not overlap in time',
  INVALID_GPS: 'the GPS data is invalid',
  FAILED_TO_REFINE_POSITIONS: 'Google could not place the photos exactly',
  TAKEDOWN: 'removed for a policy reason',
  CORRUPT_VIDEO: 'the video file is damaged',
  INVALID_VIDEO_FORMAT: 'the video format is not supported',
  INVALID_VIDEO_DIMENSIONS: 'the video is not 2:1 (equirectangular)',
  INVALID_CAPTURE_TIME: 'the capture time is in the future',
  GPS_DATA_GAP: 'the GPS track has a gap of over 5 s',
  JUMPY_GPS: 'the GPS track jumps around too much',
  INSUFFICIENT_MOVEMENT: 'the camera hardly moved',
  NOT_OUTDOORS: 'parts were recorded indoors',
  UNSUPPORTED_CAMERA: 'the camera is not supported',
  CAMERA_COVERED: 'the camera was covered',
  INSUFFICIENT_VIDEO_FRAMES: 'too few video frames',
};
const video = { file: null, info: null, gpx: null, gpxName: '', xhr: null, running: false, stopped: false };
const vdEl = id => document.getElementById(id);

function localInput(ms) { return new Date(ms - new Date(ms).getTimezoneOffset() * 60000).toISOString().slice(0, 19); }
function fromLocalInput(v) { const ms = new Date(v).getTime(); return Number.isFinite(ms) ? ms : null; }
function formatWhen(ms) { return new Date(ms).toLocaleString(lang() === 'de' ? 'de-DE' : 'en-GB'); }
function formatDuration(s) { return s >= 60 ? t('{m} min {s} s', { m: Math.floor(s / 60), s: Math.round(s % 60) }) : t('{s} s', { s: Math.round(s) }); }
function videoGpsMode() { return vdEl('vd-gps-gpx').checked ? 'gpx' : 'camm'; }

export function openVideo() {
  closeMenus();
  openModal('video-back');
  renderVideo();
  listVideos();
}

function factRow(level, text, button) {
  const row = document.createElement('div');
  row.className = `check-row ${level}`;
  row.innerHTML = `<span class="lv"></span><span class="msg">${escapeHtml(text)}</span>`;
  if (button) {
    const b = document.createElement('button');
    b.className = 'btn';
    b.textContent = button.label;
    b.onclick = button.run;
    row.appendChild(b);
  }
  return row;
}

// The GPS points for the upload, or null (no GPX, or no start time).
function videoTimeline() {
  const start = fromLocalInput(vdEl('vd-start').value);
  if (videoGpsMode() !== 'gpx' || !video.gpx || start == null || !video.info?.duration) return null;
  return gpsTimeline(video.gpx, start, video.info.duration);
}

function renderResume() {
  const box = vdEl('vd-resume');
  const state = videoResumeState();
  box.hidden = !state || video.running;
  if (box.hidden) return;
  const pct = Math.floor((state.offset / state.size) * 100);
  box.innerHTML = '';
  const text = document.createElement('span');
  text.textContent = sameVideo(state, video.file)
    ? t('This video is continued where it stopped: {p}% are on Google already.', { p: pct })
    : t('The upload of {name} stopped at {p}% ({when}). Choose the same video to go on from there.', { name: state.name, p: pct, when: formatWhen(state.at) });
  const drop = document.createElement('button');
  drop.className = 'btn';
  drop.textContent = t('Start over');
  drop.onclick = () => { saveVideoResume(null); renderVideo(); };
  box.append(text, drop);
}

function renderVideo() {
  renderResume();
  const { file, info } = video;
  const resuming = sameVideo(videoResumeState(), file);
  const facts = vdEl('vd-facts');
  facts.innerHTML = '';
  vdEl('vd-name').textContent = file ? `${file.name} · ${formatBytes(file.size)}` : '';
  if (file && !info) facts.appendChild(factRow('error', t('This is not an MP4 or MOV video that can be read.')));
  if (info) {
    if (info.duration) facts.appendChild(factRow('ok', t('Length: {d}', { d: formatDuration(info.duration) })));
    if (info.width && info.height) {
      const twoToOne = Math.abs(info.width / info.height - 2) < 0.02;
      facts.appendChild(twoToOne
        ? factRow('ok', t('{w} × {h} pixels (2:1, as a 360° video should be)', { w: info.width, h: info.height }))
        : factRow('error', t('{w} × {h} pixels - not 2:1: Google needs the equirectangular 360° video, as the camera\'s app exports it.', { w: info.width, h: info.height })));
    }
    facts.appendChild(info.spherical
      ? factRow('ok', t('360° metadata found'))
      : factRow('warn', t('No 360° metadata found - export the video as a 360° video from the camera\'s app (or add it with Google\'s "Spatial Media Metadata Injector").')));
    facts.appendChild(info.camm
      ? factRow('ok', t('Camera motion track (CAMM) found - Google reads the GPS from it, if the camera recorded GPS.'))
      : info.gpmd
        ? factRow('warn', t('GPS in GoPro\'s own format (GPMF) - Google does not read it. Use a GPX file of the same walk instead.'))
        : factRow('warn', t('No GPS track in the video - choose a GPX file recorded at the same time.')));
    facts.appendChild(info.created
      ? factRow('ok', t('Recording started {when} (as the file says)', { when: formatWhen(info.created) }))
      : factRow('warn', t('The file does not say when the recording started - set it under GPS.')));
  }
  const gpxMode = videoGpsMode() === 'gpx';
  vdEl('vd-gpx-pick').hidden = !gpxMode;
  vdEl('vd-gpx-box').hidden = !gpxMode;
  vdEl('vd-gpx-name').textContent = gpxMode && video.gpx ? video.gpxName : '';
  if (!vdEl('vd-start').value && info?.created) vdEl('vd-start').value = localInput(info.created);

  const gf = vdEl('vd-gpx-facts');
  gf.innerHTML = '';
  const tl = videoTimeline();
  if (gpxMode && video.gpx) {
    const pts = video.gpx;
    gf.appendChild(factRow('ok', t('{n} track points, {a} to {b}', { n: pts.length, a: formatWhen(pts[0].t), b: formatWhen(pts[pts.length - 1].t) })));
    if (tl) {
      const start = fromLocalInput(vdEl('vd-start').value);
      if (tl.coverage >= 0.95) gf.appendChild(factRow('ok', t('The track covers the whole video ({n} points).', { n: tl.recorded })));
      else if (tl.coverage > 0) gf.appendChild(factRow('warn', t('The track covers only {p}% of the video - Google needs GPS for all of it.', { p: Math.round(tl.coverage * 100) })));
      else gf.appendChild(factRow('error', t('The track and the video do not overlap in time - check the start time.')));
      if (tl.coverage < 0.95) {
        const shift = hourShift(video.gpx, start, video.info.duration);
        if (shift.hours) {
          const h = shift.hours > 0 ? `+${shift.hours}` : `${shift.hours}`;
          gf.appendChild(factRow('warn', t('Shifted by {h} h they match: the camera probably wrote its local time.', { h }),
            { label: t('Shift by {h} h', { h }), run: () => { vdEl('vd-start').value = localInput(start + shift.hours * 3600000); renderVideo(); } }));
        }
      }
      if (tl.filled) gf.appendChild(factRow('ok', t('{n} point(s) filled in along the track where it had gaps of 5 to 30 s (Google refuses gaps over 5 s).', { n: tl.filled })));
      if (tl.gapS) gf.appendChild(factRow('error', t('The track has a gap of {s} s during the video - Google refuses gaps over 5 s.', { s: tl.gapS })));
    }
  }
  // A continued upload sends what was chosen when it started (kept with it).
  vdEl('vd-go').disabled = video.running || !file || !info || (!resuming && gpxMode && !(tl && tl.coverage > 0));
  vdEl('vd-go').textContent = t(resuming ? 'Continue upload' : 'Upload video');
}

vdEl('vd-pick').onclick = () => vdEl('vd-file').click();
export const isVideoFile = f => /^video\//.test(f.type) || /\.(mp4|mov)$/i.test(f.name);

export async function setVideoFile(file) {
  if (video.running) return;
  video.file = file;
  video.info = null;
  vdEl('vd-start').value = '';
  vdEl('vd-status').textContent = t('Reading the video…');
  try { video.info = await readVideoInfo(file); } catch { video.info = null; }
  vdEl('vd-status').textContent = '';
  // Without a motion track in the video, the GPS has to come from a GPX file.
  if (video.info && !video.info.camm) vdEl('vd-gps-gpx').checked = true;
  renderVideo();
}
vdEl('vd-file').onchange = e => {
  const file = e.target.files[0];
  e.target.value = '';
  if (file) setVideoFile(file);
};
const vdBack = document.getElementById('video-back');
vdBack.addEventListener('dragover', e => { e.preventDefault(); vdBack.classList.add('drop'); });
vdBack.addEventListener('dragleave', e => { if (!vdBack.contains(e.relatedTarget)) vdBack.classList.remove('drop'); });
vdBack.addEventListener('drop', e => {
  e.preventDefault();
  e.stopPropagation();
  vdBack.classList.remove('drop');
  const files = [...e.dataTransfer.files];
  const clip = files.find(isVideoFile);
  const track = files.find(f => /\.gpx$/i.test(f.name));
  if (clip) setVideoFile(clip);
  if (track) useGpxFile(track);
});
document.querySelectorAll('input[name=vd-gps]').forEach(r => { r.onchange = renderVideo; });
vdEl('vd-start').oninput = renderVideo;
vdEl('vd-gpx-pick').onclick = () => vdEl('vd-gpx-file').click();
async function useGpxFile(file) {
  vdEl('vd-gps-gpx').checked = true;
  try {
    video.gpx = parseGpx(await file.text());
    video.gpxName = file.name;
  } catch (err) {
    alert(t(err.message || String(err)));
  }
  renderVideo();
}
vdEl('vd-gpx-file').onchange = e => {
  const file = e.target.files[0];
  e.target.value = '';
  if (file) useGpxFile(file);
};

// The video goes up in pieces with Google's resumable protocol: after a broken
// connection the editor asks Google how much arrived and goes on from there. Where
// an upload stands is kept (VIDEO_RESUME), so it can also go on after a restart -
// with the same video chosen again, within the 7 days Google keeps it open.
const VIDEO_CHUNK = 16 * 1024 * 1024; // Google wants multiples of 2 MiB
const VIDEO_RESUME = 'videoUpload';
const VIDEO_KEEP_MS = 7 * 24 * 3600 * 1000;

function videoResumeState() {
  try {
    const s = JSON.parse(storageGet(VIDEO_RESUME) || 'null');
    return s && Date.now() - s.at < VIDEO_KEEP_MS ? s : null;
  } catch { return null; }
}
function saveVideoResume(s) { storageSet(VIDEO_RESUME, s ? JSON.stringify(s) : ''); }
const sameVideo = (s, file) => !!s && !!file && s.name === file.name && s.size === file.size && s.lastModified === file.lastModified;

async function videoApi(path, body = {}) {
  try {
    const r = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    if (r.status === 401) return { ok: false, error: 'not_signed_in' };
    return await r.json();
  } catch {
    return { ok: false, error: t('The editor app is not reachable.'), status: 0 };
  }
}

// One piece: browser -> app (XHR), which streams it on to Google; progress from the app.
function sendVideoChunk(session, file, offset, length, onSent) {
  return new Promise(resolve => {
    const job = `v${Date.now().toString(36)}${Math.random().toString(36).slice(2, 7)}`;
    const final = offset + length >= file.size;
    let finished = false;
    const poll = setInterval(async () => {
      try {
        const p = await (await fetch(`/api/upload-progress?job=${job}`, { cache: 'no-store' })).json();
        if (p.total && !finished) onSent(p.sent);
      } catch {}
    }, 500);
    const xhr = new XMLHttpRequest();
    video.xhr = xhr;
    const params = new URLSearchParams({ url: session, offset, final: final ? '1' : '0', job });
    xhr.open('POST', `/api/video/chunk?${params}`);
    xhr.setRequestHeader('X-Editor-Key', editorKey);
    const done = j => { finished = true; clearInterval(poll); resolve(j); };
    xhr.onload = () => {
      let j = {};
      try { j = JSON.parse(xhr.responseText); } catch {}
      done(xhr.status === 401 ? { ok: false, error: 'not_signed_in' } : j);
    };
    xhr.onerror = () => done({ ok: false, error: t('The editor app is not reachable.'), status: 0 });
    xhr.onabort = () => done({ ok: false, error: 'stopped' });
    xhr.send(file.slice(offset, offset + length));
  });
}

// Send the video from where Google stands; resolves when all of it has arrived.
async function sendVideoFile(file, state, onProgress) {
  let offset = state.offset || 0;
  let failures = 0;
  while (offset < file.size) {
    if (video.stopped) throw new Error(videoError({ error: 'stopped' }));
    const length = Math.min(VIDEO_CHUNK, file.size - offset);
    const j = await sendVideoChunk(state.session, file, offset, length, sent => onProgress(offset + sent));
    if (j.ok) {
      offset += length;
      failures = 0;
      saveVideoResume({ ...state, offset, at: Date.now() });
      onProgress(offset);
      continue;
    }
    if (j.error === 'stopped' || j.error === 'not_signed_in') throw new Error(videoError(j));
    // Refused for good (e.g. not allowed), not just a hiccup on the way: no point in trying again.
    if (j.status >= 400 && j.status < 500 && ![408, 429].includes(j.status)) throw new Error(videoError(j));
    if (++failures > 5) throw new Error(videoError(j));
    vdEl('vd-status').textContent = t('The connection broke - asking Google how much arrived…');
    await new Promise(r => setTimeout(r, 1500 * failures));
    const q = await videoApi('/api/video/query', { session: state.session });
    if (!q.ok) continue;
    if (q.status === 'final') return; // all there; only the answer got lost
    if (q.status !== 'active') throw new Error(t('Google ended this upload - start it again.'));
    offset = q.received;
    saveVideoResume({ ...state, offset, at: Date.now() });
  }
}

function videoError(j) {
  if (j.error === 'not_signed_in') return t('Not signed in (or the sign-in expired) - click "Sign in with Google".');
  if (j.error === 'stopped') return t('Stopped. Choose the same video again later to go on from there (within 7 days).');
  return j.status ? `${t('Google error')} ${j.status}: ${j.error}` : j.error;
}

vdEl('vd-stop').onclick = () => { video.stopped = true; video.xhr?.abort(); };
vdEl('vd-go').onclick = async () => {
  const { file, info } = video;
  const tl = videoGpsMode() === 'gpx' ? videoTimeline() : null;
  const start = fromLocalInput(vdEl('vd-start').value);
  const status = vdEl('vd-status');
  const bar = vdEl('vd-progress');
  const resume = sameVideo(videoResumeState(), file) ? videoResumeState() : null;
  video.running = true;
  video.stopped = false;
  vdEl('vd-stop').hidden = false;
  bar.hidden = false;
  bar.firstElementChild.style.width = '0%';
  renderVideo();
  setAppBusy(true, t('A video is still being uploaded to Google. Quit anyway? The upload is then stopped - it can be continued later.'));
  let startedAt = null;
  try {
    let state = resume;
    if (!state) {
      // What Google gets once the video is there - kept, so a continued upload sends the same.
      const create = {};
      if (tl) {
        create.gps = tl.points.map(p => ({ lat: p.lat, lng: p.lng, time: new Date(p.t).toISOString() }));
        // The start set here only goes along when it differs from what the file says.
        if (start != null && (!info.created || Math.abs(start - info.created) > 1000)) create.captureTime = new Date(start).toISOString();
      }
      const s = await videoApi('/api/video/start', { size: file.size, type: file.type || 'video/mp4' });
      if (!s.ok) throw new Error(videoError(s));
      state = { session: s.session, uploadUrl: s.uploadUrl, name: file.name, size: file.size, lastModified: file.lastModified, offset: 0, create, at: Date.now() };
      saveVideoResume(state);
    } else {
      const q = await videoApi('/api/video/query', { session: state.session });
      if (!q.ok) throw new Error(videoError(q));
      if (q.status !== 'active' && q.status !== 'final') throw new Error(t('Google ended this upload - start it again.'));
      state = { ...state, offset: q.status === 'final' ? file.size : q.received };
    }
    status.textContent = t('Uploading the video to Google…');
    await sendVideoFile(file, state, sent => {
      startedAt ??= { t: Date.now(), sent };
      bar.firstElementChild.style.width = `${(sent / file.size) * 100}%`;
      const rate = (sent - startedAt.sent) / Math.max(1, (Date.now() - startedAt.t) / 1000);
      status.textContent = `${t('Uploading the video to Google…')} ${formatBytes(sent)} / ${formatBytes(file.size)}${rate > 0 ? ` · ${formatEta((file.size - sent) / rate)}` : ''}`;
    });
    status.textContent = t('Asking Google to make photos of it…');
    const j = await videoApi('/api/video/create', { uploadUrl: state.uploadUrl, ...state.create });
    if (!j.ok) throw new Error(videoError(j));
    saveVideoResume(null);
    status.textContent = t('Uploaded. Google now makes photos of the video - that takes hours to days. The list below shows how far it got.');
    toast(t('Video uploaded to Google'));
    listVideos();
  } catch (err) {
    status.textContent = t('The video was not uploaded: {msg}', { msg: err.message || err });
  } finally {
    video.running = false;
    video.xhr = null;
    vdEl('vd-stop').hidden = true;
    bar.hidden = true;
    setAppBusy(false);
    renderVideo();
  }
};

function videoStateText(s) {
  if (s.state === 'PROCESSED') {
    const views = s.views ? ` · ${t('{n} views', { n: s.views.toLocaleString(lang()) })}` : '';
    return t('Done: {n} photo(s) over {d}', { n: s.photos, d: formatDistance(s.distance || 0) }) + views;
  }
  if (s.state === 'FAILED') return t('Failed: {why}', { why: VIDEO_FAILURES[s.failure] ? t(VIDEO_FAILURES[s.failure]) : (s.failure || s.error || '?') });
  if (s.state === 'PENDING') return t('Waiting to be processed');
  return t('Being processed by Google');
}

async function listVideos() {
  const list = vdEl('vd-list');
  list.innerHTML = `<div class="mini">${escapeHtml(t('Asking Google…'))}</div>`;
  let j;
  try {
    j = await (await fetch('/api/video/list', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' })).json();
  } catch {
    j = { ok: false, error: t('The editor app is not reachable.') };
  }
  if (!j.ok) {
    list.innerHTML = `<div class="mini">${escapeHtml(j.error === 'not_signed_in' ? t('Sign in with Google first (in the upload dialog) to see your videos.') : videoError(j))}</div>`;
    return;
  }
  list.innerHTML = j.sequences.length ? '' : `<div class="mini">${escapeHtml(t('No videos on Google yet.'))}</div>`;
  j.sequences.forEach(s => {
    const row = document.createElement('div');
    row.className = `check-row ${s.state === 'PROCESSED' ? 'ok' : s.state === 'FAILED' ? 'error' : 'warn'}`;
    const when = s.uploadTime ? ` · ${t('uploaded {when}', { when: formatWhen(Date.parse(s.uploadTime)) })}` : '';
    row.innerHTML = `<span class="lv"></span><span class="nm">${escapeHtml(s.filename || s.id)}</span><span class="msg">${escapeHtml(videoStateText(s) + when)}</span><button class="btn">${escapeHtml(t('Delete'))}</button>`;
    row.querySelector('button').onclick = async () => {
      if (!confirm(t('Delete this video and all photos Google made of it from Street View? This cannot be undone.'))) return;
      const d = await (await fetch('/api/video/delete', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id: s.id }) })).json();
      if (!d.ok) { alert(videoError(d)); return; }
      listVideos();
    };
    list.appendChild(row);
  });
}
vdEl('vd-refresh').onclick = listVideos;
