// Two people working on one project file - in a OneDrive, Dropbox, Google Drive or
// Nextcloud folder - each change their own copy. When the file changes under the editor
// (the other one saved), both are put together, compared with the version both started
// from (base): what only one side changed is taken from that side, what both changed
// alike stays, and what both changed differently keeps the version here and is reported.
// A point the other one deleted goes, unless it was changed here (and the other way
// round); a point added on one side comes in. Without a base (e.g. a copy the cloud kept
// of a version that started from one this editor never saw) nothing is deleted and,
// where both sides differ, the version here wins.
//
// Works on project.json data (projectData in editor.js). Related fields of a point count
// as one change - a new position is latitude and longitude together - so a merged point
// never mixes halves of two edits.

const GROUPS = new Map([
  ['position', ['lat', 'lng', 'hasGps']],
  ['north', ['heading', 'origHeading', 'viewYaw', 'origViewYaw', 'headingSource']],
  ['photo', ['photoName', 'photoPath', 'photoType', 'photoSize', 'blurred']],
  ['capture', ['captureTime', 'captureOffset']],
  ['google', ['gsvPhotoId', 'gsvStatus', 'gsvShareLink', 'gsvSig', 'gsvConfirmed', 'gsvUploadedAt']],
  ['place', ['placeId', 'placeName', 'placeAuto', 'placeKind', 'placeDist', 'placeFor']],
  ['horizon', ['pitch', 'roll']],
]);
const GROUP_OF = new Map([...GROUPS].flatMap(([g, keys]) => keys.map(k => [k, g])));
const OWN_RULES = new Set(['id', 'gsvObsolete', 'viewsLog', 'gsvViews']);
// A key missing from a point means something for this one (no place decided yet); for every
// other key it means the file was written by an editor that doesn't know the key yet.
const MISSING_MEANS = new Set(['placeId']);
// Always written by editors that know them: compared only where every version has them.
const DERIVED = new Set(['photoSize']);
// What Google reports and what is read from the photo: records, not edits of a point.
const RECORDS = new Set(['gsvStatus', 'gsvShareLink', 'gsvSig', 'gsvConfirmed', 'gsvUploadedAt', 'gsvViews', 'viewsLog', 'captureTime', 'captureOffset', 'photoSize']);
const NO_UPLOAD = { gsvPhotoId: null, gsvStatus: null, gsvShareLink: null, gsvSig: null, gsvConfirmed: false, gsvUploadedAt: null };

// A value as text: the same text for the same content (object keys in order); a missing key: '~'.
function canon(v) {
  if (v === undefined) return '~';
  return JSON.stringify(v, (_, x) => (x && typeof x === 'object' && !Array.isArray(x)
    ? Object.fromEntries(Object.keys(x).sort().map(k => [k, x[k]])) : x));
}

// A side's value of a key. A key an older editor didn't write: unchanged (the base's value),
// or nothing (null) where the base doesn't have it either.
function val(side, key, base) {
  if (key in side || MISSING_MEANS.has(key)) return side[key];
  return base && key in base ? base[key] : null;
}

// Which side to take: 'same' (both alike), 'ours', 'theirs' or 'conflict'.
function choose(b, o, t) {
  if (o === t) return 'same';
  if (o === b) return 'theirs';
  if (t === b) return 'ours';
  return 'conflict';
}

// Items kept from a list both sides changed: in both, or added by one (not in base).
function set3(b = [], o = [], t = []) {
  const inB = new Set(b.map(canon)), inO = new Set(o.map(canon)), inT = new Set(t.map(canon));
  const out = new Map();
  [...o, ...t].forEach(x => {
    const k = canon(x);
    if ((inO.has(k) && inT.has(k)) || (!inB.has(k) && (inO.has(k) || inT.has(k)))) out.set(k, x);
  });
  return [...out.values()];
}

// Views noted per day on either side: the higher count of a day.
function mergeLogs(...logs) {
  const days = new Map();
  logs.flat().forEach(e => { if (Array.isArray(e)) days.set(e[0], Math.max(days.get(e[0]) ?? 0, e[1])); });
  return [...days].sort((a, b) => (a[0] < b[0] ? -1 : 1));
}

function setKey(obj, key, value) {
  if (value === undefined) delete obj[key];
  else obj[key] = value;
}

// Did someone change this point since the base (more than the records)?
function edited(n, b) {
  return [...new Set([...Object.keys(n), ...Object.keys(b)])].some(k => !RECORDS.has(k) && canon(val(n, k, b)) !== canon(val(b, k, b)));
}

// One point changed on both sides (base: the point before, or null).
function mergeNode(b, o, t) {
  const out = { ...o };
  const sides = {};
  const conflicts = [];
  const done = new Set();
  for (const key of new Set([...Object.keys(b || {}), ...Object.keys(o), ...Object.keys(t)])) {
    if (OWN_RULES.has(key) || done.has(key)) continue;
    const group = GROUP_OF.get(key);
    const keys = (group ? GROUPS.get(group) : [key]).filter(k => !DERIVED.has(k) || [b, o, t].every(s => !s || k in s));
    (group ? GROUPS.get(group) : [key]).forEach(k => done.add(k));
    const part = side => canon(keys.map(k => val(side, k, b)));
    const side = b ? choose(part(b), part(o), part(t)) : (part(o) === part(t) ? 'same' : 'conflict');
    sides[group || key] = side;
    if (side === 'theirs') (group ? GROUPS.get(group) : [key]).forEach(k => setKey(out, k, val(t, k, b)));
    if (side === 'conflict') conflicts.push(group || key);
  }
  // Changed on both sides: a photo with its faces and plates hidden is never given up for one without.
  const blurKept = sides.photo === 'conflict' && !!t.blurred && !o.blurred;
  if (blurKept) {
    GROUPS.get('photo').forEach(k => setKey(out, k, val(t, k, b)));
    sides.photo = 'theirs';
  }
  out.gsvObsolete = b ? set3(b.gsvObsolete, o.gsvObsolete, t.gsvObsolete) : set3([], o.gsvObsolete, t.gsvObsolete);
  out.viewsLog = mergeLogs(o.viewsLog || [], t.viewsLog || []);
  const views = [o.gsvViews, t.gsvViews].filter(v => v != null);
  out.gsvViews = views.length ? Math.max(...views) : null;

  // Uploads since the base: the one that counts is that of the photo kept. One of the other
  // side's photo, or the same photo twice, is out of date - deleted on Google with the next upload.
  const photo = sides.photo === 'theirs' ? 'theirs' : sides.photo === 'same' || sides.photo === undefined ? 'same' : 'ours';
  // What belongs to the photo goes with it.
  if (photo !== 'same') [...GROUPS.get('capture'), 'privacyChecked'].forEach(k => setKey(out, k, val(photo === 'theirs' ? t : o, k, b)));
  else if (!b) out.privacyChecked = !!(o.privacyChecked || t.privacyChecked);
  const sameUpload = o.gsvPhotoId === t.gsvPhotoId;
  let retired = false;
  for (const [side, n] of [['ours', o], ['theirs', t]]) {
    const id = n.gsvPhotoId;
    if (!id || id === b?.gsvPhotoId || sameUpload) continue;
    if (id === out.gsvPhotoId && (photo === 'same' || photo === side)) continue;
    if (!out.gsvObsolete.includes(id)) out.gsvObsolete.push(id);
    if (id === out.gsvPhotoId) Object.assign(out, NO_UPLOAD);
    retired = true;
  }
  // Not for the user to decide: what follows the photo, one upload that only reads differently on
  // the two sides, and an upload out of date or a blurred photo kept, which are reported as that.
  const said = conflicts.filter(c => !['capture', 'privacyChecked'].includes(c) && !(c === 'google' && (retired || sameUpload)) && !(c === 'photo' && blurKept));
  return { node: out, photo, conflicts: said, retired, blurKept, theirsChanged: Object.values(sides).includes('theirs') };
}

// Floor plans: each as a whole (its image is its own: kept or replaced, never mixed).
function mergeWhole(b, o, t) {
  const side = b ? choose(canon(b), canon(o), canon(t)) : (canon(o) === canon(t) ? 'same' : 'conflict');
  return { item: side === 'theirs' ? t : o, from: side === 'theirs' ? 'theirs' : side === 'same' ? 'same' : 'ours', conflict: side === 'conflict' };
}

const idNumber = id => Number(String(id).slice(1)) || 0;

// base: the version both started from, or null; ours: the editor's; theirs: the file's.
// Returns the merged data and what happened:
//   photos: node id -> 'ours' | 'theirs' | 'same' (where the merged point's photo comes from;
//     'same': both have the same photo), plans: plan id -> the same,
//   renamed: our node id -> its new id (added on both sides under the same id),
//   added, changed, removed, restored: names of points the other side added, changed,
//     deleted, or deleted/changed where this side did the other,
//   conflicts: [{ name, what: ['north', ...] }] (this side's version kept),
//   retired: names of points whose upload on Google is out of date now,
//   blurKept: names of points where the other side's blurred photo beat a change here.
export function mergeProjects(base, ours, theirs) {
  const report = { photos: new Map(), plans: new Map(), renamed: new Map(), added: [], changed: [], removed: [], restored: [], conflicts: [], retired: [], blurKept: [] };
  const byId = list => new Map((list || []).map(x => [x.id, x]));
  const B = byId(base?.nodes), O = byId(ours.nodes), T = byId(theirs.nodes);
  let nxt = Math.max(ours.nxt || 1, theirs.nxt || 1,
    ...[...O.keys(), ...T.keys(), ...(ours.conns || []).map(c => c.id), ...(theirs.conns || []).map(c => c.id)].map(id => idNumber(id) + 1));
  const name = n => n.name || n.id;

  // Points.
  const merged = new Map(); // id -> point
  const restored = new Set(); // deleted on one side, changed on the other: kept, with their links
  for (const [id, o] of O) {
    const t = T.get(id), b = B.get(id);
    if (!t) {
      if (!b) { merged.set(id, o); report.photos.set(id, 'ours'); continue; } // added here
      if (!edited(o, b)) { report.removed.push(name(b)); continue; } // deleted there
      merged.set(id, o); report.photos.set(id, 'ours'); report.restored.push(name(o)); restored.add(id); // deleted there, changed here
      continue;
    }
    if (!b && canon(o) !== canon(t) && (base || o.photoName !== t.photoName)) {
      // Added on both sides under the same id: two points - ours gets a new id.
      const fresh = `n${nxt++}`;
      report.renamed.set(id, fresh);
      merged.set(fresh, { ...o, id: fresh, photoPath: o.photoPath ? o.photoPath.replace(`photos/${id}/`, `photos/${fresh}/`) : o.photoPath });
      report.photos.set(fresh, 'ours');
      merged.set(id, t);
      report.photos.set(id, 'theirs');
      report.added.push(name(t));
      continue;
    }
    const m = mergeNode(b || null, o, t);
    merged.set(id, m.node);
    report.photos.set(id, m.photo);
    if (m.theirsChanged) report.changed.push(name(m.node));
    if (m.conflicts.length) report.conflicts.push({ name: name(m.node), what: m.conflicts });
    if (m.retired) report.retired.push(name(m.node));
    if (m.blurKept) report.blurKept.push(name(m.node));
  }
  for (const [id, t] of T) {
    if (O.has(id)) continue;
    const b = B.get(id);
    if (b && !edited(t, b)) continue; // deleted here
    merged.set(id, t);
    report.photos.set(id, 'theirs');
    (b ? report.restored : report.added).push(name(t)); // changed there after it was deleted here / added there
    if (b) restored.add(id);
  }
  // In this side's order; points added there after the point before them there.
  const order = [...(ours.nodes || []).map(n => report.renamed.get(n.id) || n.id).filter(id => merged.has(id))];
  (theirs.nodes || []).forEach((n, i) => {
    if (order.includes(n.id) || !merged.has(n.id)) return;
    const before = (theirs.nodes || []).slice(0, i).reverse().find(p => order.includes(p.id));
    order.splice(before ? order.indexOf(before.id) + 1 : order.length, 0, n.id);
  });
  const nodes = order.map(id => merged.get(id));

  // Links: by the two points they join.
  const ren = id => report.renamed.get(id) || id;
  const pair = c => [c.from, c.to].sort().join('|');
  const pairsOf = (list, rename = x => x) => new Map((list || []).map(c => [pair({ from: rename(c.from), to: rename(c.to) }), { ...c, from: rename(c.from), to: rename(c.to) }]));
  const cB = pairsOf(base?.conns), cO = pairsOf(ours.conns, ren), cT = pairsOf(theirs.conns);
  const usedConnIds = new Set();
  const conns = [];
  for (const key of new Set([...cO.keys(), ...cT.keys()])) {
    const c = { ...(cO.get(key) || cT.get(key)) };
    // Kept: on both sides, or added on one; a link only gone with a point that is kept stays too.
    const keep = !base || (cO.has(key) && cT.has(key)) || !cB.has(key) || restored.has(c.from) || restored.has(c.to);
    if (!keep) continue;
    if (!merged.has(c.from) || !merged.has(c.to)) continue;
    if (usedConnIds.has(c.id)) c.id = `c${nxt++}`;
    usedConnIds.add(c.id);
    conns.push(c);
  }

  // Start points: one per tour - this side's where both chose another.
  const startsB = base?.startIds || [], startsO = (ours.startIds || []).map(ren), startsT = theirs.startIds || [];
  const starts = (base ? set3(startsB, startsO, startsT) : [...new Set([...startsO, ...startsT])]).filter(id => merged.has(id));
  const startIds = [];
  const tourOf = id => merged.get(id).tourId ?? null;
  [...starts].sort((a, b) => startsO.includes(b) - startsO.includes(a)).forEach(id => {
    if (!startIds.some(s => tourOf(s) === tourOf(id))) startIds.push(id);
  });

  // Tour names: per tour.
  const tours = {};
  for (const id of new Set([...Object.keys(ours.tours || {}), ...Object.keys(theirs.tours || {})])) {
    const o = ours.tours?.[id], t = theirs.tours?.[id], b = base?.tours?.[id];
    tours[id] = o === undefined ? t : t === undefined ? o : (b !== undefined && o === b ? t : o);
  }

  // Floor plans: each as a whole.
  const pB = byId(base?.plans), pO = byId(ours.plans), pT = byId(theirs.plans);
  const plans = [];
  for (const id of new Set([...pO.keys(), ...pT.keys()])) {
    const o = pO.get(id), t = pT.get(id), b = pB.get(id);
    if (o && t) {
      const m = mergeWhole(b, o, t);
      plans.push(m.item);
      report.plans.set(id, m.from);
      if (m.conflict) report.conflicts.push({ name: o.name || id, what: ['floor plan'] });
    } else if (o) {
      if (b && canon(o) === canon(b)) continue; // deleted there
      plans.push(o); report.plans.set(id, 'ours');
    } else {
      if (b && canon(t) === canon(b)) continue; // deleted here
      plans.push(t); report.plans.set(id, 'theirs');
    }
  }

  // Photos of removed points, to delete on Google - never that of a point kept after all.
  const inUse = new Set(nodes.map(n => n.gsvPhotoId).filter(Boolean));
  const gsvRemoved = (base ? set3(base.gsvRemoved, ours.gsvRemoved, theirs.gsvRemoved) : set3([], ours.gsvRemoved, theirs.gsvRemoved)).filter(id => !inUse.has(id));
  const data = {
    ...theirs, ...ours, // the file's settings this editor doesn't know stay
    nodes, conns, startIds, startId: startIds[0] ?? null, tours, plans, gsvRemoved,
    version: Math.max(ours.version || 1, theirs.version || 1),
    nxt: Math.max(nxt, ...nodes.map(n => idNumber(n.id) + 1)),
  };
  return { data, report };
}

// Did the other side change anything that shows? (A file only touched, or saved unchanged, doesn't.)
export function reportIsEmpty(r) {
  return !r.added.length && !r.changed.length && !r.removed.length && !r.restored.length && !r.renamed.size
    && ![...r.photos.values(), ...r.plans.values()].includes('theirs');
}
