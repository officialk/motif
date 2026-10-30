"use strict";

// ---------- helpers ----------
const $ = (id) => document.getElementById(id);
const store = {
  get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* storage unavailable */ } },
};
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtTime = (s) => `${Math.floor((s || 0) / 60)}:${String(Math.floor((s || 0) % 60)).padStart(2, "0")}`;
const fmtLong = (s) => {
  const m = Math.round((s || 0) / 60);
  return m >= 60 ? `${Math.floor(m / 60)} h ${m % 60} min` : `${m} min`;
};
const fmtSize = (b) => (b >= 1048576 ? `${(b / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round((b || 0) / 1024))} KB`);
const fmtDate = (t) => (t ? new Date(t * 1000).toLocaleString() : "");
const genreLabel = (g) => { const s = String(g ?? "").replace(/[-_]/g, " "); return s.charAt(0).toUpperCase() + s.slice(1); };
const scaleLabel = (s) => String(s ?? "").replace(/_/g, " ");
const enc = encodeURIComponent;

async function api(path, body, method) {
  const opts = {};
  if (method || body !== undefined) opts.method = method || "POST";
  if (body !== undefined) {
    opts.headers = { "Content-Type": "application/json" };
    opts.body = JSON.stringify(body);
  }
  const r = await fetch(path, opts);
  if (r.status === 401 && booted) {
    lockOut(); // session ended: show the login view; the empty message keeps callers from toasting
    throw new Error("");
  }
  if (!r.ok) throw new Error(await errorText(r));
  return r.json();
}

async function errorText(r) {
  try {
    const j = await r.json();
    if (typeof j.detail === "string") return j.detail;
  } catch { /* not json */ }
  return `${r.status} ${r.statusText}`.trim();
}

let toastTimer;
function toast(msg) {
  if (!msg) return;
  const t = $("toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove("show"), 2600);
}

// Deterministic gradient art from any string (song hash, id, genre).
function hashNum(str) {
  let h = 2166136261;
  for (const c of String(str ?? "")) { h ^= c.codePointAt(0); h = Math.imul(h, 16777619); }
  return h >>> 0;
}
function gradient(key) {
  const h = hashNum(key);
  const a = h % 360, b = (a + 40 + ((h >>> 9) % 120)) % 360, ang = (h >>> 17) % 360;
  return `linear-gradient(${ang}deg, hsl(${a} 62% 52%), hsl(${b} 58% 28%))`;
}
const artStyle = (key) => `style="background:${gradient(key)}"`;
function mosaicHtml(cls, keys, fallback) {
  const ks = keys.slice(0, 4);
  if (ks.length < 4) return `<div class="${cls}" ${artStyle(ks[0] || fallback)}></div>`;
  return `<div class="${cls} mosaic">${ks.map((k) => `<i ${artStyle(k)}></i>`).join("")}</div>`;
}

// Static icons in index.html are <el data-icon="name">; dynamic ones swap via setIcon.
function fillIcons(root = document) {
  for (const el of root.querySelectorAll("[data-icon]")) setIcon(el, el.dataset.icon);
}
function setIcon(el, name, cls = "") {
  if (!el || el._icon === name) return;
  el._icon = name;
  el.innerHTML = icon(name, cls);
}

// Only touch the DOM when the markup actually changed (keeps hover/menus/inputs alive).
function setHTML(el, html) {
  if (!el || el._html === html) return false;
  el._html = html;
  el.innerHTML = html;
  return true;
}

// Audio engines (WebAudioOut / ElementOut / OutClass), clientId() and streamEvents() live in audio.js.

// ---------- app state ----------
let S = null;               // last server state
let out = null;             // audio output (only while this tab is, or is becoming, the speaker)
let awaitingId = null;      // local auto-advance waiting for the server to confirm
let wantPaused = false;
let volume = store.get("volume", 0.8);
let sinkId = store.get("sinkId", "");
let genres = [];
let aiInfo = null;
let library = [];           // saved songs (Entry[])
let libIds = new Set();
let playlists = [];
const plDetail = new Map(); // playlist id -> {playlist with songs, updated}
let recent = [];            // GET /history
let settings = null;
let backups = null;         // {enabled, backups}
let apiKeys = null;         // GET /settings/keys: {keys} | {error}
let audioFail = { id: null, at: 0 };
const models = new Map();   // row key -> row model (for event delegation)
let route = { name: "home" };
let mountedRoute = "";
let me = null;              // GET /auth/me: {user: {id, email, role}, login}
let booted = false;         // the app (player, stream) is running for a signed-in user
let locked = false;         // signed out; the login view is up
let streamH = null;         // SSE handle
const timers = [];
let members = null;         // GET /users: {users, invites, smtp} | {error}
let memLink = null;         // {link, email} until dismissed
const isAdmin = () => me?.user?.role === "admin";
const FEATURES = [["download", "Download"], ["ai", "AI"], ["api", "API"], ["backup", "Backups"], ["overlay", "Overlays"]];
const hasFeature = (k) => (me?.features ?? me?.user?.features)?.[k] !== false; // missing = allowed

// ---------- live sync: speaker / remote ----------
// Exactly one client plays audio: the speaker. This tab is the speaker, a remote (another
// client plays; we mirror its progress and send /control), or idle (nobody plays).
const CLIENT = clientId();
const live = {
  up: false,         // SSE stream connected
  version: null,     // newest event version taken (reset per connection)
  speaker: null,     // {client, kind, since}
  playback: null,    // speaker report {client, track_id, state, position, at, volume}
  offset: 0,         // server clock − local clock, seconds
  lastSeq: null,     // highest command seq seen (null = adopt on next event)
  claiming: 0,       // claims in flight: don't let older events overrule them
  claimSince: 0,     // `since` of our last successful claim
  clients: [],       // connected pages/overlays [{client, kind, name, audio, since}]
};
let autoClaimAt = 0;
let reportedId = null, lastReportAt = 0, reportSoon = null;
let resumeAt = null;        // {id, t}: "Play here" continues where the other speaker was
let fallbackTimer = null;   // slow /state poll while the stream is down

const isSpeaker = () => !!live.speaker && live.speaker.client === CLIENT;
const isRemote = () => !!live.speaker && live.speaker.client !== CLIENT;
const serverNow = () => Date.now() / 1000 + live.offset;
// The speaker's report, if it belongs to the current speaker.
const remotePlayback = () => (live.playback && live.speaker && live.playback.client === live.speaker.client ? live.playback : null);
// Something is playing (or paused mid-song) somewhere: here or on the speaker.
const hasPlayback = () => !!(out?.cur || (isRemote() && remotePlayback()));
function playingNow() {
  if (isRemote()) return remotePlayback()?.state === "playing";
  return !!(out && out.cur && !wantPaused);
}

function ensureOut() {
  if (!out) {
    out = new SiteOut();
    out.setVolume(volume);
    if (sinkId && SiteOut.sinkSupported()) out.setSink(sinkId).catch(() => { sinkId = ""; store.set("sinkId", ""); });
    if (typeof mediaAttach === "function") mediaAttach(out); // media.js: system controls, background play
  }
  out.unlock?.(); // <audio> output: let it start later without a click (iOS)
  if (!wantPaused && out.paused && out.cur) out.resume();
  if (out.ctx && out.ctx.state === "suspended" && !wantPaused) out.ctx.resume();
  return out;
}

// The user asked for music in this tab. Plays here unless another client is the speaker.
// Audio is created synchronously so the browser counts the click as the autoplay gesture.
function wantLocal() {
  if (isRemote()) return false;
  ensureOut();
  if (!isSpeaker()) claimSpeaker(false);
  return true;
}

async function claimSpeaker(force) {
  live.claiming++;
  try {
    const r = await api("/speaker/claim", { client: CLIENT, kind: "site", force: !!force });
    live.speaker = r.speaker || null;
    if (isSpeaker()) { live.claimSince = r.speaker.since || serverNow(); reportedId = null; }
  } catch (e) {
    toast(e.message);
  } finally {
    live.claiming--;
  }
  applyRole();
  return isSpeaker();
}

// React to the current speaker: a remote never holds audio.
function applyRole() {
  if (live.claiming) return;
  if (isRemote()) {
    if (out) stopLocal();
  } else if (!live.speaker && out?.cur && live.up && Date.now() - autoClaimAt > 5000) {
    // Our stream dropped and the server released us, but this tab still has the song: take it back.
    autoClaimAt = Date.now();
    claimSpeaker(false);
  }
}

function stopLocal() {
  if (!out) return;
  out.destroy();
  out = null;
  awaitingId = null;
  reportedId = null;
  resumeAt = null;
  renderProgress(null);
  renderPlayer();
  highlight();
}

async function playHere() {
  const pos = remotePos();
  resumeAt = pos ? { id: pos.track.id, t: pos.t } : null;
  wantPaused = false;
  ensureOut();
  if (await claimSpeaker(true)) reconcile();
  else stopLocal();
  renderPlayer();
}

// Role or features changed on the server (every event carries `account`): follow without a reload.
function applyAccount(a) {
  if (!a || !me || (a.id && a.id !== me.user.id)) return;
  const feats = a.features || me.features || me.user.features;
  if (a.role === me.user.role && JSON.stringify(feats) === JSON.stringify(me.features ?? me.user.features)) return;
  if (a.role) me.user.role = a.role;
  if (a.features) { me.features = a.features; if (me.user.features) me.user.features = a.features; }
  $("download").hidden = !hasFeature("download");
  renderAccount();
  if (!hasFeature("download")) closeMenu();
  settings = null;
  if (route.name === "members") {
    if (!canManage()) { location.hash = "#/home"; return; }
    members = null;
    loadMembers();
    renderView();
  }
  if (route.name === "settings") {
    loadSettings();
    renderView();
  }
}

function onLive(type, d) {
  if (type !== "state" || !d || typeof d !== "object") return;
  applyAccount(d.account);
  if (live.version !== null && typeof d.version === "number" && d.version <= live.version) return;
  if (typeof d.version === "number") live.version = d.version;
  if (typeof d.library_rev === "number" && d.library_rev !== live.libraryRev) {
    if (live.libraryRev !== undefined) loadLib();  // e.g. a chat request added songs to a playlist
    live.libraryRev = d.library_rev;
  }
  if (typeof d.server_time === "number") live.offset = d.server_time - Date.now() / 1000;
  const sp = d.speaker || null;
  // An event older than our own claim can still say someone else is the speaker; ignore that.
  const stale = !!out && !!sp && sp.client !== CLIENT && (sp.since || 0) < live.claimSince;
  if (!live.claiming && !stale) live.speaker = sp;
  live.playback = d.playback || null;
  live.clients = Array.isArray(d.clients) ? d.clients : [];
  const cmd = d.command;
  const seq = cmd && typeof cmd.seq === "number" ? cmd.seq : 0;
  if (live.lastSeq === null) live.lastSeq = seq; // first event on this connection: never replay old commands
  else if (seq > live.lastSeq) {
    live.lastSeq = seq;
    if (isSpeaker()) runCommand(cmd);
  }
  applyRole();
  if (d.state) { S = d.state; afterState(); } else renderPlayer();
}

function onLiveStatus(status, info) {
  if (status === "down" && info?.status === 401) { lockOut(); return; }
  if (status === "open") {
    live.up = true;
    live.version = null;
    live.lastSeq = null;
    clearInterval(fallbackTimer);
    fallbackTimer = null;
  } else if (status === "down") {
    live.up = false;
    if (!fallbackTimer) fallbackTimer = setInterval(refresh, 5000);
  }
}

// Commands from remotes, executed by the speaker only.
async function runCommand(c) {
  if (!out) return;
  switch (c.action) {
    case "pause": if (!wantPaused) await setPaused(true); break;
    case "resume": if (wantPaused || out.paused) await setPaused(false); break;
    case "toggle": await setPaused(!!(out.cur && !wantPaused)); break;
    case "seek": if (typeof c.t === "number") seekTo(c.t); break;
    case "volume": if (typeof c.volume === "number") setVolume(c.volume); break;
  }
}

async function control(body) {
  try {
    await api("/control", body);
  } catch (e) {
    toast(e.message);
  }
}

// Tell remotes what the speaker is doing. tick() also calls this on song change and every 10 s.
function report() {
  clearTimeout(reportSoon);
  if (!out?.cur || !isSpeaker()) return;
  const pos = out.position();
  reportedId = out.cur.id;
  lastReportAt = Date.now();
  api("/playback", {
    client: CLIENT, track_id: out.cur.id, state: wantPaused ? "paused" : "playing",
    position: pos ? Math.min(pos.t, pos.music) : 0, volume,
  }).catch(() => {});
}

// Progress of the remote speaker, extrapolated with the server clock.
function remotePos() {
  const pb = remotePlayback();
  if (!pb) return null;
  const track = [S?.now, S?.up_next].find((x) => x && x.id === pb.track_id);
  const music = track?.music_seconds;
  if (!music) return null;
  const since = pb.state === "playing" && typeof pb.at === "number" ? Math.max(0, serverNow() - pb.at) : 0;
  return { t: Math.max(0, (pb.position || 0) + since), music, track };
}

// What the progress bar shows: local audio, or the remote speaker.
function progressPos() {
  if (out?.cur) return out.position();
  return isRemote() ? remotePos() : null;
}

async function refresh() {
  try {
    S = await api("/state");
  } catch {
    return; // the stream's reconnect handles a server that's down
  }
  afterState();
}

let lastNowId = null;
function afterState() {
  renderPlayer();
  renderSidebar();
  renderView();
  highlight();
  reconcile();
  queueMaybeChanged();
  const nowId = S?.now?.id ?? null;
  if (nowId !== lastNowId) {
    lastNowId = nowId;
    loadHistory();
  }
}

function reconcile() {
  if (!out || !S) return;
  const now = S.now, next = S.up_next;
  // Fetch the next song ahead: it must start from memory when this one ends (maybe locked).
  if (next && next.status === "ready" && !out.buffer(next.id)) out.load(next).then(() => tick()).catch(() => {});
  if (!now || now.status !== "ready") return;
  if (out.cur?.id === now.id || out.pending?.id === now.id || awaitingId) return;
  const buf = out.buffer(now.id);
  if (buf && S.stopped) {
    // The playlist ended: its first song is cued, paused, until play is pressed.
    wantPaused = true;
    out.playNow(now, buf);
    out.pause().catch(() => {});
    report();
    renderPlayer();
    return;
  }
  if (buf) {
    out.playNow(now, buf);
    if (resumeAt?.id === now.id && resumeAt.t > 1) out.seek(Math.min(resumeAt.t, (now.music_seconds || resumeAt.t) - 1));
    resumeAt = null;
    return;
  }
  // Only chain when the buffer is not ready yet; chaining unconditionally loops forever.
  if (audioFail.id === now.id && Date.now() - audioFail.at < 5000) return;
  out.load(now).then(() => reconcile()).catch((e) => {
    audioFail = { id: now.id, at: Date.now() };
    toast(`Audio failed: ${e.message}`);
  });
}

async function confirmAdvance(fromId, newId) {
  awaitingId = newId;
  try {
    S = await api("/skip", { from_id: fromId });
  } catch (e) {
    toast(e.message);
  } finally {
    awaitingId = null;
  }
  afterState();
}

// Start the prefetched song exactly when the current song's last bar ends.
// Only the speaker has `out`, so only the speaker hands over and calls /skip.
function tick() {
  if (!out || !out.cur) return renderProgress(isRemote() ? remotePos() : null);
  if (out.promote()) return;
  if (isSpeaker() && (out.cur.id !== reportedId || (!wantPaused && Date.now() - lastReportAt > 10000))) report();
  const pos = out.position();
  renderProgress(pos);
  if (!S || !S.now || S.now.id !== out.cur.id || out.pending || awaitingId) return;
  const next = S.up_next;
  if (!next && S.at_end) {
    // Last song of a playlist (repeat off): when it ends, the server cues the first one, paused.
    if (pos && pos.t >= pos.music) confirmAdvance(out.cur.id, "end");
    return;
  }
  const buf = next && next.status === "ready" ? out.buffer(next.id) : null;
  if (!buf) return; // not ready yet: the current song rings out, the next starts as soon as it is
  const fromId = out.cur.id;
  if (out.gapless) {
    const t = out.now();
    const end = out.musicEnd();
    if (end - t > 2) return;
    out.scheduleAt(next, buf, Math.max(end, t + 0.05));
    // The pending voice is promoted in tick(); tell the server right when it starts.
    const o = out;
    const wait = setInterval(() => {
      if (out !== o) clearInterval(wait); // audio was stopped (lost the speaker role)
      else if (o.cur?.id === next.id) { clearInterval(wait); confirmAdvance(fromId, next.id); }
      else if (o.pending?.id !== next.id) clearInterval(wait); // cancelled by a user action
    }, 50);
  } else if (pos.t >= pos.music) {
    out.playNow(next, buf);
    confirmAdvance(fromId, next.id);
  }
}

// ---------- data loading ----------
async function loadLib() {
  const [l, p] = await Promise.allSettled([api("/library"), api("/playlists")]);
  if (l.status === "fulfilled") library = l.value.library || [];
  if (p.status === "fulfilled") playlists = p.value.playlists || [];
  libIds = new Set(library.map((e) => e.id));
  renderSidebar();
  renderPlayer();
  if (route.name === "playlist") loadPlaylist(route.id);
  renderView();
  highlight();
}

// Shuffle (player bar, like Spotify): off = in order, on = random order,
// auto = Shuffle+ (AI playlists): random order with a new song mixed in now and then.
// It switches the playing playlist in place and is remembered per playlist for the next Play.
const SHUFFLE_TIP = {
  off: "Enable shuffle", on: "Shuffle: on",
  auto: "Shuffle+: random order, with new songs mixed in now and then",
};
function playPlaylist(id, start) {
  const sh = shuffleOf(id);
  return act(() => api(`/playlists/${enc(id)}/play`, { start, shuffle: sh !== "off", plus: sh === "auto" }));
}
function shuffleOf(id) {
  const mp = S?.mode?.playlist;
  if (mp?.id === id) return mp.plus ? "auto" : mp.shuffle ? "on" : "off";
  return store.get(`shuffle:${id}`, "off");
}
async function cycleShuffle(id, ai) {
  const next = { off: "on", on: ai ? "auto" : "off", auto: "off" }[shuffleOf(id)];
  store.set(`shuffle:${id}`, next);
  if (S?.mode?.playlist?.id === id) await act(() => api(`/playlists/${enc(id)}/shuffle`, { mode: next }));
  toast({ off: "Shuffle off", on: "Shuffle on", auto: "Shuffle+ on" }[next]);
}

// "Generate new" on an AI playlist: the AI adds songs in the playlist's vibe (server picks the seeds).
const plGenerating = new Set();
function generateMenu(el) {
  const id = el.dataset.pl;
  openMenu(el, [{ heading: "Add new songs" }, ...[1, 3, 5].map((n) => ({
    label: `${n} song${n === 1 ? "" : "s"}`, run: () => generateInto(id, n),
  }))]);
}

async function generateInto(id, count) {
  if (plGenerating.has(id)) return;
  plGenerating.add(id);
  if (route.name === "playlist" && route.id === id) fillPlaylist();
  try {
    const r = await api(`/playlists/${enc(id)}/generate`, { count });
    if (r.playlist) plDetail.set(id, r.playlist);
    toast(`Added ${r.added.length} song${r.added.length === 1 ? "" : "s"}`);
    await loadLib();
  } catch (e) {
    toast(e.message);
  } finally {
    plGenerating.delete(id);
    if (route.name === "playlist" && route.id === id) { renderView(); highlight(); }
  }
}

async function loadPlaylist(id) {
  const summary = playlists.find((p) => p.id === id);
  const have = plDetail.get(id);
  if (have && summary && have.updated === summary.updated && have.count === summary.count) return;
  try {
    const { playlist } = await api(`/playlists/${enc(id)}`);
    plDetail.set(id, playlist);
  } catch (e) {
    plDetail.set(id, { error: e.message });
  }
  if (route.name === "playlist" && route.id === id) { renderView(); highlight(); }
}

let historyBusy = false;
async function loadHistory() {
  if (historyBusy) return;
  historyBusy = true;
  try {
    recent = (await api("/history?limit=200")).history || [];
    recent.sort((a, b) => (b.played_at || 0) - (a.played_at || 0));
  } catch { /* keep previous */ }
  historyBusy = false;
  if (route.name === "history") { renderView(); highlight(); }
}

async function loadSettings() {
  try {
    settings = await api("/settings");
  } catch (e) {
    settings = { error: e.message };
  }
  backups = null;
  if (settings?.backup?.enabled) loadBackups();
  loadKeys();
  if (route.name === "settings") renderView();
}

async function loadKeys() {
  try {
    apiKeys = await api("/settings/keys");
  } catch (e) {
    apiKeys = { error: e.message };
  }
  if (route.name === "settings") renderView();
}

async function loadBackups() {
  try {
    backups = await api("/backups");
  } catch (e) {
    backups = { enabled: false, error: e.message, backups: [] };
  }
  if (route.name === "settings") renderView();
}

async function loadGenres() {
  try {
    const g = await api("/genres");
    genres = g.genres || [];
    aiInfo = g.ai || null;
  } catch {
    genres = [];
  }
  $("engine-badge").textContent = !aiInfo || aiInfo.backend === "off" ? "procedural" : aiInfo.backend;
}

// ---------- row models ----------
// A row is a song from any source: saved Entry, persisted history item, or session Track.
function trackRow(t, ctx) {
  return {
    ctx, id: t.id, tid: t.id, hash: t.hash || t.library_id || null, libraryId: t.library_id || null,
    name: t.name || (t.genre ? genreLabel(t.genre) : "AI is choosing…"), genre: t.genre, tempo: t.tempo, key: t.key, scale: t.scale,
    seconds: t.music_seconds, source: t.source, status: t.status, error: t.error,
  };
}
function entryRow(e, ctx, extra = {}) {
  return {
    ctx, id: e.id, tid: null, hash: e.id, libraryId: e.id, name: e.name, genre: e.genre, tempo: e.tempo, key: e.key,
    scale: e.scale, seconds: e.music_seconds, source: e.source, ...extra,
  };
}
// The saved-song id for a row, if it is in the library.
function savedId(m) {
  if (m.hash && libIds.has(m.hash)) return m.hash;
  if (m.libraryId && libIds.has(m.libraryId)) return m.libraryId;
  return null;
}
function isPlaying(m) {
  const now = S?.now;
  if (!now) return false;
  if (m.tid) return m.tid === now.id;
  if (m.ctx === "pl" && S.mode?.playlist && S.mode.playlist.id !== m.plId) return false;
  return !!m.hash && (m.hash === now.hash || m.hash === now.library_id);
}

function statusBadge(st) {
  if (!st) return "";
  const cls = st === "ready" ? "ready" : st === "error" ? "error" : "busy";
  const label = st === "generating" ? "generating…" : st === "rendering" ? "rendering…" : st;
  return `<span class="status ${cls}">${esc(label)}</span>`;
}

function rowHtml(m, i) {
  const key = `${m.ctx}:${i}:${m.id}`;
  models.set(key, m);
  const k = `data-m="${esc(key)}"`;
  const isLiked = !!savedId(m);
  const tag = m.source && m.source !== "procedural" ? `<span class="tag" title="Made with ${esc(m.source)}">AI</span>` : "";
  const sub = [m.genre ? esc(genreLabel(m.genre)) : "", m.showStatus ? statusBadge(m.status) : ""].filter(Boolean).join(" ");
  const ready = !m.tid || m.status === "ready";
  const moves = m.ctx === "pl"
    ? `<button class="icon-btn" data-a="row-up" ${k} title="Move up" aria-label="Move up" ${i === 0 ? "disabled" : ""}>${icon("arrow_upward")}</button>`
      + `<button class="icon-btn" data-a="row-down" ${k} title="Move down" aria-label="Move down" ${m.last ? "disabled" : ""}>${icon("arrow_downward")}</button>`
    : "";
  return `<div class="row" ${k} data-hash="${esc(m.hash || "")}" data-tid="${esc(m.tid || "")}" data-pl="${esc(m.plId || "")}">
  <div class="num"><span class="n">${i + 1}</span><span class="eq" aria-hidden="true">${icon("equalizer")}</span><button class="p" data-a="row-play" ${k} title="Play" aria-label="Play ${esc(m.name)}" ${ready ? "" : "disabled"}>${icon("play_arrow")}</button></div>
  <div class="song"><div class="art" ${artStyle(m.hash || m.id)}></div><div class="meta"><div class="title">${esc(m.name)}${tag}</div><div class="sub">${sub}</div></div></div>
  <div class="cell">${m.seconds ? fmtTime(m.seconds) : ""}</div>
  <div class="acts">${moves}<button class="icon-btn like${isLiked ? " on" : ""}" data-a="row-like" ${k} title="${isLiked ? "Remove from Liked songs" : "Like"}" aria-label="${isLiked ? "Remove from Liked songs" : "Like"}" aria-pressed="${isLiked}" ${ready ? "" : "disabled"}>${icon(isLiked ? "favorite" : "favorite_border")}</button><button class="icon-btn add" data-a="row-add" ${k} title="Add to playlist" aria-label="Add to playlist" ${ready ? "" : "disabled"}>${icon("add")}</button><button class="icon-btn del" data-a="row-delete" ${k} title="Delete song" aria-label="Delete ${esc(m.name)}" ${ready ? "" : "disabled"}>${icon("delete")}</button><button class="icon-btn" data-a="row-more" ${k} title="More" aria-label="More">${icon("more_horiz")}</button></div>
</div>`;
}

function rowsHtml(list, cls = "") {
  if (!list.length) return "";
  return `<div class="rows ${cls}"><div class="row-head"><span>#</span><span>Title</span><span title="Length" aria-label="Length">${icon("schedule")}</span><span></span></div>${list.map(rowHtml).join("")}</div>`;
}

// ---------- routing / views ----------
function parseRoute() {
  const h = location.hash.replace(/^#\/?/, "");
  const [name, ...rest] = h.split("/");
  if (name === "playlist" && rest[0]) return { name: "playlist", id: decodeURIComponent(rest.join("/")) };
  if (["home", "history", "queue", "settings", "liked", "library", "members"].includes(name)) return { name };
  return { name: "home" };
}

const canManage = () => isAdmin() && !!me?.login;
// Highlight the current page in the sidebar and tab bar (Your Library covers liked songs, playlists and history).
function markNav() {
  const lib = ["library", "liked", "playlist", "history"].includes(route.name);
  for (const a of document.querySelectorAll("[data-route]")) {
    const on = a.dataset.route === route.name || (a.dataset.route === "library" && lib);
    a.classList.toggle("active", on);
    if (on) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  }
}

let leaveOk = false;
async function onRoute() {
  const next = parseRoute();
  if (next.name === "members" && !canManage()) { location.hash = "#/home"; return; }
  // Leaving Settings with unsaved edits: put the hash back (no event) and ask first.
  if (route.name === "settings" && next.name !== "settings" && !leaveOk && settingsDirty()) {
    const target = location.hash;
    history.replaceState(null, "", "#/settings");
    const yes = await ask({ title: "Leave without saving?", text: "Your unsaved settings changes will be lost.", ok: "Discard changes", danger: true });
    if (!yes) return;
    leaveOk = true;
    location.hash = target;
    return;
  }
  leaveOk = false;
  const entering = next.name === "settings" && route.name !== "settings";
  route = next;
  closeMenu();
  markNav();
  $("prompt-form").hidden = route.name !== "home"; // prompts live on Home only
  if (route.name === "settings") {
    if (entering) settings = null; // never show a stale form
    loadSettings();
  }
  if (route.name === "members") {
    if (mountedRoute !== "members") { members = null; memLink = null; }
    loadMembers();
  }
  if (route.name === "history") loadHistory();
  if (route.name === "queue") loadQueue();
  if (route.name === "playlist") loadPlaylist(route.id);
  renderSidebar();
  renderView();
  highlight();
  $("main").scrollTop = 0;
}

function renderView() {
  const key = route.name + (route.id ? `/${route.id}` : "");
  const view = $("view");
  if (mountedRoute !== key) {
    mountedRoute = key;
    models.clear();
    view._html = null;
    view.innerHTML = skeleton(route);
  }
  ({ home: fillHome, history: fillHistory, queue: fillQueue, settings: fillSettings, liked: fillLiked, playlist: fillPlaylist, library: fillLibrary, members: fillMembers })[route.name]();
}

function skeleton(r) {
  switch (r.name) {
    case "home":
      return `<h1>${greeting()}</h1>
        <div class="section-title"><h2>Radios</h2><span class="muted">Endless songs in one genre</span></div>
        <div id="v-radios" class="tiles"></div>
        <div class="section-title"><h2>Your playlists</h2></div>
        <div id="v-playlists"></div>`;
    case "queue":
      return `<h1>Queue</h1><div id="v-queue"></div>`;
    case "history":
      return `<h1>Recently played</h1><div id="v-history-meta" class="muted"></div><div id="v-history" style="margin-top:12px"></div>`;
    case "liked":
      return `<div class="hero"><div class="art liked-art">${icon("favorite")}</div><div><div class="kind">PLAYLIST</div><h1>Liked songs</h1><div id="v-liked-meta" class="muted"></div></div></div>
        <div id="v-liked-actions" class="actions"></div>
        <div id="v-liked"></div>`;
    case "playlist":
      return `<div id="v-pl-hero"></div><div id="v-pl-actions" class="actions"></div><div id="v-pl-rows"></div>`;
    case "members":
      return `<h1>Members</h1><div id="v-members"></div>`;
    case "library":
      return `<h1>Your Library</h1><div id="v-library"></div>`;
    case "settings":
      return `<h1>Settings</h1>
        <div id="v-account"></div><div id="v-app"></div><div id="v-ai"></div><div id="v-storage"></div><div id="v-backups"></div><div id="v-keys"></div>`;
  }
  return "";
}

function greeting() {
  const h = new Date().getHours();
  return h < 5 ? "Good night" : h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening";
}

function playlistArt(cls, pl) {
  return mosaicHtml(cls, pl.song_ids || (pl.songs || []).map((s) => s.id), pl.id);
}

function fillHome() {
  const radios = genres.map((g) => `<div class="tile" role="button" tabindex="0" data-a="radio" data-genre="${esc(g)}" title="Play ${esc(genreLabel(g))} radio">
      <div class="art genre-art" ${artStyle(`genre:${g}`)}><span>${esc(genreLabel(g))}</span></div>
      <div class="tile-name">${esc(genreLabel(g))} radio</div><div class="tile-sub">Procedural · endless</div>
      <span class="hover-play" aria-hidden="true">${icon("play_arrow")}</span></div>`).join("")
    + `<div class="tile" role="button" tabindex="0" data-a="shuffle-radios" data-genre="*shuffle" title="Shuffle radios">
      <div class="art shuffle-art">${icon("shuffle")}</div>
      <div class="tile-name">Shuffle radios</div><div class="tile-sub">A new genre every song</div>
      <span class="hover-play" aria-hidden="true">${icon("play_arrow")}</span></div>`;
  setHTML($("v-radios"), genres.length ? radios : '<div class="empty">Loading radios…</div>');

  setHTML($("v-playlists"), playlists.length
    ? `<div class="hscroll">${playlists.map((p) => `<div class="tile" role="button" tabindex="0" data-a="go" data-href="#/playlist/${enc(p.id)}" data-plid="${esc(p.id)}">
        ${playlistArt("art", p)}
        <div class="tile-name">${esc(p.name)}${p.source === "ai" ? '<span class="tag">AI</span>' : ""}</div><div class="tile-sub">${p.count} song${p.count === 1 ? "" : "s"}</div>
        <button class="hover-play" data-a="pl-play" data-pl="${esc(p.id)}" title="Play" aria-label="Play ${esc(p.name)}" ${p.count ? "" : "disabled"}>${icon("play_arrow")}</button></div>`).join("")}</div>`
    : `<div class="empty">No playlists yet. Describe a vibe above, or use ${icon("add", "inline")} next to “Your library” or on any song.</div>`);
}

// ---------- your library (phones have no sidebar) ----------
function fillLibrary() {
  const playing = S?.mode?.playlist?.id;
  const item = (href, art, name, sub, plid) => `<a href="${href}" class="lib-row${plid && plid === playing ? " playing" : ""}">
    ${art}<span class="lib-meta"><span class="lib-name">${name}</span><span class="lib-sub">${sub}</span></span>
    <span class="lib-go" aria-hidden="true">${icon("chevron_right")}</span></a>`;
  const n = (c) => `${c} song${c === 1 ? "" : "s"}`;
  setHTML($("v-library"), `<div class="actions"><button type="button" class="pill" id="lib-new">${icon("add")}New playlist</button></div>
    <div class="lib-page">
      ${item("#/liked", `<span class="lib-art liked-art">${icon("favorite")}</span>`, "Liked songs", `Playlist · ${n(library.length)}`)}
      ${item("#/history", `<span class="lib-art hist-art">${icon("history")}</span>`, "Recently played", "Your last songs")}
      ${playlists.map((p) => item(`#/playlist/${enc(p.id)}`, playlistArt("lib-art", p),
        `${esc(p.name)}${p.source === "ai" ? '<span class="tag">AI</span>' : ""}`, `Playlist · ${n(p.count)}`, p.id)).join("")}
    </div>
    ${playlists.length ? "" : '<div class="empty">No playlists yet. Create one, or describe a vibe on Home to have one made for you.</div>'}`);
  const b = $("lib-new");
  if (b) b.onclick = () => newPlaylist(null);
}

// ---------- queue ----------
let queueData = null, queueKey = "";
async function loadQueue() {
  try { queueData = await api("/queue"); } catch (e) { queueData = { error: e.message }; }
  if (route.name === "queue") { renderView(); highlight(); }
}
// Reload the queue when what plays (or the order) changes.
function queueMaybeChanged() {
  if (route.name !== "queue" || !S) return;
  const key = JSON.stringify([S.now?.id, S.up_next?.id, S.up_next?.status, S.mode, S.stopped]);
  if (key !== queueKey) { queueKey = key; loadQueue(); }
}
let queueScrolledTo = null;
function fillQueue() {
  const q = queueData;
  if (!q) return setHTML($("v-queue"), '<div class="empty">Loading…</div>');
  if (q.error) return setHTML($("v-queue"), `<div class="empty">Could not load the queue: ${esc(q.error)}</div>`);
  // One list: played above, the current song highlighted, what's next below.
  const rows = q.items.map((t, n) => (q.radio
    ? trackRow(t, n < (q.current ?? q.items.length) ? "recent" : n === q.current ? "now" : "next")
    : entryRow(t, "queue", { pos: t.pos })));
  setHTML($("v-queue"), rows.length
    ? rowsHtml(rows) + (q.repeats ? `<div class="muted queue-note">${icon("repeat", "inline")} Then it repeats</div>` : "")
    : '<div class="empty">Nothing playing.</div>');
  const key = `${S?.now?.id}:${q.current}`;
  if (q.current !== null && queueScrolledTo !== key) {
    queueScrolledTo = key;
    requestAnimationFrame(() => document.querySelectorAll("#v-queue .row")[q.current]?.scrollIntoView({ block: "center" }));
  }
}

function fillHistory() {
  setHTML($("v-history-meta"), recent.length ? `${recent.length} song${recent.length === 1 ? "" : "s"}` : "");
  const rows = recent.map((h) => entryRow(h, "recent", { hash: h.hash || h.id, libraryId: null }));
  setHTML($("v-history"), rows.length ? rowsHtml(rows) : '<div class="empty">Songs you play show up here.</div>');
}

function fillLiked() {
  const total = library.reduce((a, e) => a + (e.music_seconds || 0), 0);
  setHTML($("v-liked-meta"), `${library.length} song${library.length === 1 ? "" : "s"}${library.length ? ` · ${fmtLong(total)}` : ""}`);
  setHTML($("v-liked-actions"), `<button class="big-play" id="pl-big-play" data-a="pl-play" data-pl="liked" title="Play" aria-label="Play" ${library.length ? "" : "disabled"}>${icon("play_arrow")}</button>`);
  const list = [...library].sort((a, b) => (b.liked_at || 0) - (a.liked_at || 0));
  setHTML($("v-liked"), list.length
    ? rowsHtml(list.map((e) => entryRow(e, "lib")))
    : `<div class="empty">Songs you like (${icon("favorite", "inline")}) show up here.</div>`);
}

function fillPlaylist() {
  const id = route.id;
  const summary = playlists.find((p) => p.id === id);
  const pl = plDetail.get(id);
  if (!summary && !pl) {
    setHTML($("v-pl-hero"), '<div class="empty">Loading playlist…</div>');
    return;
  }
  if (pl?.error && !summary) {
    setHTML($("v-pl-hero"), `<div class="empty">Could not load playlist: ${esc(pl.error)}</div>`);
    return;
  }
  const p = pl && !pl.error ? pl : summary;
  const songs = pl?.songs || [];
  const count = p.count ?? songs.length;
  const secs = p.seconds ?? songs.reduce((a, s) => a + (s.music_seconds || 0), 0);
  const gs = p.genres || [...new Set(songs.map((s) => s.genre))];
  setHTML($("v-pl-hero"), `<div class="hero">${playlistArt("art", p)}<div><div class="kind">${p.source === "ai" ? "AI PLAYLIST" : "PLAYLIST"}</div><h1>${esc(p.name)}</h1>
    ${p.source === "ai" && p.prompt ? `<div class="pl-prompt">${icon("auto_awesome", "inline")}“${esc(p.prompt)}”</div>` : ""}
    <div class="muted">${count} song${count === 1 ? "" : "s"}${count ? ` · ${fmtLong(secs)}` : ""}${gs.length ? ` · ${gs.map((g) => esc(genreLabel(g))).join(", ")}` : ""}</div></div></div>`);
  setHTML($("v-pl-actions"), `<button class="big-play" id="pl-big-play" data-a="pl-play" data-pl="${esc(id)}" title="Play" aria-label="Play" ${count ? "" : "disabled"}>${icon("play_arrow")}</button>
    ${p.source === "ai" ? `<button class="pill" data-a="pl-generate" data-pl="${esc(id)}" ${plGenerating.has(id) ? "disabled" : ""}>${icon("auto_awesome")}${plGenerating.has(id) ? "Generating…" : "Generate new"}</button>` : ""}
    <button class="pill" data-a="pl-rename" data-pl="${esc(id)}">Rename</button>
    <button class="pill" data-a="pl-delete" data-pl="${esc(id)}">Delete</button>`);
  if (!pl) { setHTML($("v-pl-rows"), '<div class="empty">Loading songs…</div>'); return; }
  setHTML($("v-pl-rows"), songs.length
    ? rowsHtml(songs.map((e, i) => entryRow(e, "pl", { plId: id, index: i, last: i === songs.length - 1 })), "pl")
    : `<div class="empty">This playlist is empty. Add songs with ${icon("add", "inline")} on any song.</div>`);
}

// ---------- settings view ----------
// The forms are built from the last loaded `settings` snapshot only. Polling re-runs
// fillSettings, but setHTML skips identical markup, so typing is never wiped. A card
// re-renders only when its saved data changes (save, clear) or when forced.
const BACKENDS = [
  ["off", "Off"],
  ["claude-cli", "Claude CLI"],
  ["claude-api", "Claude API"],
  ["openai", "OpenAI-compatible"],
];
const URL_PRESETS = [
  ["OpenAI", "https://api.openai.com/v1"],
  ["Ollama", "http://localhost:11434/v1"],
  ["OpenRouter", "https://openrouter.ai/api/v1"],
  ["LM Studio", "http://localhost:1234/v1"],
  ["Groq", "https://api.groq.com/openai/v1"],
];
const AI_ON = "claude-cli claude-api openai";
const pendingSnap = {}; // card id -> form snapshot to restore after a forced re-render

function cardHead(id, title) {
  return `<div class="card-head"><h2 id="${id}">${esc(title)}</h2><span class="dirty" role="status" hidden>${icon("edit", "inline")}Unsaved changes</span></div>`;
}

// `hint` is trusted markup written here, never server data.
function textField(id, label, value, placeholder, hint, type = "text", extra = "") {
  return `<div class="field"><label for="${id}">${esc(label)}</label>
    <input id="${id}" type="${type}" value="${esc(value)}" placeholder="${esc(placeholder || "")}" spellcheck="false" autocomplete="off" autocapitalize="off"${hint ? ` aria-describedby="${id}-hint"` : ""} ${extra}>
    ${hint ? `<div class="hint" id="${id}-hint">${hint}</div>` : ""}</div>`;
}

function keyStatus(k) {
  if (k?.source === "env") return ["From environment", "env"];
  if (k?.set) return ["Saved", "saved"];
  return ["Not set", "none"];
}

// Write-only secret: never prefilled; only sent when typed, or cleared with the Clear button.
function keyField(id, label, k, scope, field, hint) {
  const [txt, cls] = keyStatus(k);
  const ph = cls === "saved" ? "Leave blank to keep the saved key" : cls === "env" ? "Leave blank to use the environment value" : "Paste the key";
  return `<div class="field"><label for="${id}">${esc(label)}</label>
    <div class="key-row">
      <div class="secret"><input id="${id}" type="password" data-secret autocomplete="new-password" spellcheck="false" autocapitalize="off" placeholder="${ph}" aria-describedby="${id}-status${hint ? ` ${id}-hint` : ""}">
        <button type="button" class="icon-btn" data-a="reveal" data-for="${id}" title="Show" aria-label="Show ${esc(label)}" aria-pressed="false">${icon("visibility")}</button></div>
      <span class="kpill ${cls}" id="${id}-status">${txt}</span>
      ${k?.source === "settings" ? `<button type="button" class="pill small" data-a="key-clear" data-scope="${scope}" data-field="${field}" data-label="${esc(label)}">Clear</button>` : ""}
    </div>
    ${hint ? `<div class="hint" id="${id}-hint">${hint}</div>` : ""}</div>`;
}

function switchHtml(id, label, on) {
  return `<label class="switch" for="${id}"><input type="checkbox" role="switch" id="${id}" ${on ? "checked" : ""}><span class="track" aria-hidden="true"></span><span>${esc(label)}</span></label>`;
}

// App defaults (stored on the server, so every device gets them).
function appCardHtml(a) {
  const opt = (id, label, opts, val) => `<div class="field"><label for="${id}">${esc(label)}</label><select id="${id}">${
    opts.map(([v, l]) => `<option value="${esc(v)}" ${v === val ? "selected" : ""}>${esc(l)}</option>`).join("")}</select></div>`;
  const startup = [["last", "Last played song"], ["random", "Random radio"],
    ...genres.map((g) => [`genre:${g}`, `${genreLabel(g)} radio`]), ["none", "Nothing"]];
  return `<section class="card settings-card" id="app-card" aria-labelledby="app-h">
    ${cardHead("app-h", "App")}
    ${opt("app-startup", "On startup", startup, a.startup || "last")}
    ${opt("app-repeat", "Repeat", [["off", "Off"], ["all", "All"], ["one", "One"]], a.repeat || "off")}
    ${textField("app-prompt-count", "Songs per prompt", a.prompt_count ?? 8, "8", "", "number", 'min="2" max="20" step="1" inputmode="numeric"')}
    <div class="field"><span class="flabel">Visualizer</span>${switchHtml("app-visualizer", "Show above the player", a.visualizer !== false)}</div>
    <div class="actions"><button type="button" class="pill accent" data-a="app-save">Save</button></div>
  </section>`;
}

function aiCardHtml(ai) {
  const d = ai.defaults || {};
  const keys = ai.keys || {};
  // The Claude CLI runs on the server's own login: admin only.
  const list = (ai.backends?.length ? ai.backends : BACKENDS.map((b) => b[0])).filter((b) => b !== "claude-cli" || isAdmin());
  const own = (v, def) => (v && v !== def ? v : ""); // empty = server default
  const opts = list.map((b) => {
    const [, title] = BACKENDS.find((x) => x[0] === b) || [b, b];
    return `<label class="opt" for="ai-b-${esc(b)}"><input type="radio" name="ai-backend" id="ai-b-${esc(b)}" value="${esc(b)}" ${b === ai.backend ? "checked" : ""}>
      <span class="opt-title">${esc(title)}</span></label>`;
  }).join("");
  const presets = URL_PRESETS.map(([n, u]) => `<button type="button" class="chip" data-a="url-preset" data-url="${esc(u)}" title="${esc(u)}">${esc(n)}</button>`).join("");
  const ratio = Number(ai.ratio) || 0;
  return `<section class="card settings-card" id="ai-card" aria-labelledby="ai-h">
    ${cardHead("ai-h", me?.login && !isAdmin() ? "Your AI provider" : "AI generation")}
    ${me?.login && !isAdmin() ? '<p class="note">Your key is private to you; nobody else can use or see it.</p>' : ""}
    <fieldset class="backends"><legend class="sr-only">Backend</legend>${opts}</fieldset>
    <div class="group" data-backends="claude-cli">
      ${textField("ai-cli-model", "CLI model", own(ai.cli_model, d.cli_model), d.cli_model)}
    </div>
    <div class="group" data-backends="claude-api">
      ${textField("ai-model", "Model", own(ai.model, d.model), d.model)}
      ${keyField("ai-key-anthropic", "Anthropic API key", keys.anthropic, "ai", "anthropic_api_key")}
    </div>
    <div class="group" data-backends="openai">
      <div class="field"><label for="ai-base-url">Base URL</label>
        <input id="ai-base-url" type="text" inputmode="url" value="${esc(ai.base_url || "")}" placeholder="${esc(d.base_url || "")}" spellcheck="false" autocomplete="off" autocapitalize="off">
        <div class="hint presets" role="group" aria-label="Base URL presets">${presets}</div></div>
      ${textField("ai-openai-model", "Model", own(ai.openai_model, d.openai_model), d.openai_model)}
      ${keyField("ai-key-openai", "OpenAI API key", keys.openai, "ai", "openai_api_key")}
    </div>
    <div class="group" data-backends="${AI_ON}">
      <div class="field"><label for="ai-ratio">AI share of radio</label>
        <input id="ai-ratio" type="range" min="0" max="1" step="0.05" value="${ratio}" aria-describedby="ai-ratio-hint">
        <div class="hint" id="ai-ratio-hint"><b id="ai-ratio-val">${Math.round(ratio * 100)}%</b></div></div>
      ${textField("ai-timeout", "Timeout (seconds)", ai.timeout ?? "", "20", "", "number", 'min="1" max="120" step="1" inputmode="numeric"')}
    </div>
    <div class="actions"><button type="button" class="pill accent" data-a="ai-save">Save</button><button type="button" class="pill" data-a="ai-test" data-test>Test</button></div>
    <div id="ai-result" class="result" role="status" aria-live="polite"></div>
  </section>`;
}

function storageCardHtml(st) {
  const keys = st.keys || {};
  return `<section class="card settings-card" id="st-card" aria-labelledby="st-h">
    ${cardHead("st-h", "Backups (S3)")}
    ${switchHtml("st-enabled", "Back up to S3", st.enabled)}
    <div class="group" data-s3>
      ${textField("st-bucket", "Bucket", st.bucket || "", "my-bucket")}
      ${textField("st-prefix", "Prefix", st.prefix || "", "motif/")}
      ${textField("st-endpoint", "Endpoint URL", st.endpoint || "", "https://…", "", "text", 'inputmode="url"')}
      ${textField("st-region", "Region", st.region || "", "us-east-1")}
      ${keyField("st-key-id", "Access key ID", keys.aws_access_key_id, "backup", "aws_access_key_id")}
      ${keyField("st-key-secret", "Secret access key", keys.aws_secret_access_key, "backup", "aws_secret_access_key")}
      ${textField("st-backup-hours", "Backup every (hours)", st.backup_interval_hours ?? 0, "0", "", "number", 'min="0" step="1" inputmode="decimal"')}
    </div>
    <div class="actions"><button type="button" class="pill accent" data-a="st-save">Save</button><button type="button" class="pill" data-a="st-test" data-test>Test connection</button></div>
    <div id="st-result" class="result" role="status" aria-live="polite"></div>
  </section>`;
}

function backupsHtml(st) {
  if (!st.enabled) return "";
  let h = `<section class="card" aria-labelledby="bk-h"><div class="card-head"><h2 id="bk-h">Backups</h2></div>
    <div class="actions"><button type="button" class="pill accent" data-a="backup-now">Back up now</button></div>`;
  if (!backups) h += '<div class="empty">Loading backups…</div>';
  else if (backups.error) h += `<div class="empty">Could not list backups: ${esc(backups.error)}</div>`;
  else if (!backups.backups?.length) h += '<div class="empty">No backups yet.</div>';
  else h += backups.backups.map((b) => `<div class="backup"><div class="meta"><div>${esc(fmtDate(b.created))}</div><div class="muted">${esc(b.key)} · ${fmtSize(b.size)}</div></div>
      <button type="button" class="pill small" data-a="restore" data-key="${esc(b.key)}">Restore</button></div>`).join("");
  return h + "</section>";
}

function fillSettings() {
  setHTML($("v-account"), accountCardHtml());
  setHTML($("v-keys"), keysCardHtml());
  setHTML($("overlay-list"), keysListHtml("overlay"));
  setHTML($("api-list"), keysListHtml("api"));
  if (!settings || settings.error) {
    setHTML($("v-app"), "");
    setHTML($("v-ai"), settings ? `<div class="empty">Could not load settings: ${esc(settings.error)}</div>` : '<div class="empty">Loading settings…</div>');
    setHTML($("v-storage"), "");
    setHTML($("v-backups"), "");
    return;
  }
  if (setHTML($("v-app"), appCardHtml(settings.app || {}))) initCard($("app-card"));
  if (setHTML($("v-ai"), hasFeature("ai") ? aiCardHtml(settings.ai || {}) : "")) initCard($("ai-card"));
  if (setHTML($("v-storage"), settings.backup && hasFeature("backup") ? storageCardHtml(settings.backup) : "")) initCard($("st-card"));
  setHTML($("v-backups"), settings.backup && hasFeature("backup") ? backupsHtml(settings.backup) : "");
}

// ---------- overlays and API keys ----------
// Two kinds of key: overlay keys (mo_…) open an overlay page and play its audio; API keys
// (mk_…) are for tools and bots. A token is shown once, right after creating; the server keeps a hash.
const KEY_SCOPES = [
  ["read", "See what's playing"],
  ["control", "Play, pause, skip, pick music"],
  ["request", "Make playlists from prompts"],
];
let keyForm = null; // "overlay" | "api" while its New form is open
let newKey = null;  // {kind, token} until "Done"

function keysCardHtml() {
  const card = (kind, title, btn) => `<section class="card" id="${kind}-keys-card" aria-labelledby="${kind}-keys-h">
    <div class="card-head"><h2 id="${kind}-keys-h">${title}</h2>
      <button type="button" class="pill small" data-a="key-new" data-kind="${kind}">${icon("add")}${btn}</button></div>
    <div id="${kind}-form"></div>
    <div id="${kind}-new"></div>
    <div id="${kind}-list" class="keylist"></div>
  </section>`;
  return (hasFeature("overlay") ? card("overlay", "Overlays", "New overlay") : "") + (hasFeature("api") ? card("api", "API keys", "New API key") : "");
}

function ago(ts) {
  const s = Date.now() / 1000 - ts;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  if (s < 86400 * 30) return `${Math.floor(s / 86400)} d ago`;
  return fmtDate(ts);
}

function keysListHtml(kind) {
  if (!apiKeys) return '<div class="empty">Loading…</div>';
  if (apiKeys.error) return `<div class="empty">Could not load keys: ${esc(apiKeys.error)}</div>`;
  const list = (apiKeys.keys || []).filter((k) => (k.kind || "api") === kind);
  if (!list.length) return `<div class="empty small">${kind === "overlay" ? "No overlays yet." : "No API keys yet."}</div>`;
  return list.map((k) => `<div class="keyrow">
      <span class="key-ico">${icon(kind === "overlay" ? "cast" : "key")}</span>
      <div class="key-meta">
        <div class="key-name">${esc(k.name)}</div>
        <div class="key-sub"><code>${esc(k.prefix)}…</code>${kind === "api" ? (k.scopes || []).map((s) => `<span class="kpill">${esc(s)}</span>`).join("") : ""}
          <span title="Created ${esc(fmtDate(k.created))}">${k.last_used ? `Used ${esc(ago(k.last_used))}` : "Never used"}</span></div>
      </div>
      <button type="button" class="pill small danger-text" data-a="key-revoke" data-id="${esc(k.id)}" data-name="${esc(k.name)}">Revoke</button>
    </div>`).join("");
}

function keyFormHtml(kind) {
  const scopes = kind === "api"
    ? `<div class="scope-chips" role="group" aria-label="Scopes">${KEY_SCOPES.map(([s, label]) => `<label class="scope-chip" title="${esc(label)}">
        <input type="checkbox" id="ks-${s}" value="${s}" ${s === "request" ? "" : "checked"}><span><b>${s}</b> ${esc(label)}</span></label>`).join("")}</div>`
    : "";
  return `<div class="keyform">
    <input id="key-name" maxlength="60" placeholder="${kind === "overlay" ? "Name, e.g. OBS stream" : "Name, e.g. Streamer.bot"}" spellcheck="false" autocomplete="off" aria-label="Name">
    ${scopes}
    <div class="keyform-actions"><button type="button" class="pill accent" data-a="key-create" data-kind="${kind}">Create</button>
      <button type="button" class="pill" data-a="key-cancel">Cancel</button></div>
  </div>`;
}

function newKeyHtml(kind) {
  const tokenRow = `<div class="copy-row"><code id="nk-token" class="copy-text"></code><button type="button" class="pill small" data-a="copy" data-for="nk-token">${icon("content_copy")}Copy</button></div>`;
  if (kind === "api") {
    return `<div class="newkey" role="region" aria-label="New API key">
      <div class="newkey-head"><b>${icon("key", "inline")} Copy this key now</b><span class="muted small">It won't be shown again</span></div>
      ${tokenRow}
      <div class="actions"><button type="button" class="pill" data-a="key-done">Done</button></div></div>`;
  }
  const sel = (id, label, opts) => `<label class="ov-opt" for="${id}"><span>${label}</span><select id="${id}">${opts.map(([v, l]) => `<option value="${v}">${l}</option>`).join("")}</select></label>`;
  return `<div class="newkey" role="region" aria-label="New overlay">
    <div class="newkey-head"><b>${icon("cast", "inline")} Add this URL as a browser source</b><span class="muted small">The key in it won't be shown again</span></div>
    <div class="ov-opts">
      ${sel("ov-layout", "Layout", [["card", "Card"], ["bar", "Bar"], ["mini", "Mini"]])}
      ${sel("ov-pos", "Position", [["bl", "Bottom left"], ["br", "Bottom right"], ["tl", "Top left"], ["tr", "Top right"]])}
      ${sel("ov-progress", "Progress", [["1", "Show"], ["0", "Hide"]])}
      ${sel("ov-audio", "Audio", [["1", "Play here"], ["0", "Display only"]])}
      ${sel("ov-viz", "Visualizer", [["0", "Off"], ["1", "On"]])}
    </div>
    <div class="copy-row"><code id="nk-url" class="copy-text"></code><button type="button" class="pill small accent" data-a="copy" data-for="nk-url">${icon("content_copy")}Copy URL</button></div>
    <div class="actions"><button type="button" class="pill" data-a="key-done">Done</button></div>
  </div>`;
}

function updateOverlayURL() {
  if (newKey?.kind !== "overlay" || !$("nk-url")) return;
  const q = new URLSearchParams({ layout: $("ov-layout").value, pos: $("ov-pos").value });
  if ($("ov-progress").value === "0") q.set("progress", "0");
  if ($("ov-audio").value === "0") q.set("audio", "0");
  if ($("ov-viz").value === "1") q.set("viz", "1");
  if (newKey.name) q.set("name", newKey.name);
  q.set("key", newKey.token);
  $("nk-url").textContent = `${location.origin}/overlay?${q}`;
}

function openKeyForm(kind) {
  dismissNewKey();
  keyForm = keyForm === kind ? null : kind;
  for (const k of ["overlay", "api"]) setHTML($(`${k}-form`), keyForm === k ? keyFormHtml(k) : "");
  if (keyForm) $("key-name").focus();
}

async function createKey(btn) {
  const kind = btn.dataset.kind;
  const name = $("key-name").value.trim();
  const scopes = kind === "api" ? KEY_SCOPES.map(([s]) => s).filter((s) => $(`ks-${s}`).checked) : [];
  if (!name) { toast("Give it a name"); $("key-name").focus(); return; }
  if (kind === "api" && !scopes.length) { toast("Pick at least one scope"); return; }
  busyBtn(btn, true);
  try {
    const r = await api("/settings/keys", { name, kind, scopes });
    keyForm = null;
    setHTML($(`${kind}-form`), "");
    newKey = { kind, token: r.token, name };
    $(`${kind}-new`).innerHTML = newKeyHtml(kind);
    if ($("nk-token")) $("nk-token").textContent = r.token;
    updateOverlayURL();
    $(`${kind}-new`).scrollIntoView({ block: "nearest", behavior: "smooth" });
    loadKeys();
  } catch (e) {
    toast(e.message);
    busyBtn(btn, false);
  }
}

async function revokeKey(btn) {
  const { id, name } = btn.dataset;
  const yes = await ask({ title: `Revoke “${name}”?`, text: "Anything using it stops working right away.", ok: "Revoke", danger: true });
  if (!yes) return;
  try {
    await api(`/settings/keys/${enc(id)}`, undefined, "DELETE");
    toast("Revoked");
  } catch (e) {
    toast(e.message);
  }
  loadKeys();
}

function dismissNewKey() {
  if (newKey) setHTML($(`${newKey.kind}-new`), "");
  newKey = null;
}

// navigator.clipboard needs a secure context; plain http on a LAN address falls back to execCommand.
async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.cssText = "position:fixed;opacity:0;top:0;left:0";
    document.body.appendChild(ta);
    ta.select();
    let ok = false;
    try { ok = document.execCommand("copy"); } catch { /* unsupported */ }
    ta.remove();
    return ok;
  }
}

// ---------- settings form state ----------
function formValues(card) {
  const v = {};
  for (const el of card.querySelectorAll("input, select")) {
    if (el.hasAttribute("data-secret")) continue;
    if (el.type === "radio") { if (el.checked) v[el.name] = el.value; } else if (el.type === "checkbox") v[el.id] = el.checked;
    else v[el.id] = el.value;
  }
  return JSON.stringify(v);
}
const secretsTyped = (card) => [...card.querySelectorAll("[data-secret]")].some((i) => i.value !== "");
const isDirty = (card) => !!card && (formValues(card) !== card._base || secretsTyped(card));
const settingsDirty = () => isDirty($("app-card")) || isDirty($("ai-card")) || isDirty($("st-card"));

function snapshotForm(card) {
  const s = {};
  for (const el of card.querySelectorAll("input[id], select[id]")) s[el.id] = el.type === "radio" || el.type === "checkbox" ? el.checked : el.value;
  return s;
}
function restoreForm(snap) {
  for (const [id, v] of Object.entries(snap)) {
    const el = $(id);
    if (!el) continue;
    if (el.type === "radio" || el.type === "checkbox") el.checked = v; else el.value = v;
  }
}

function initCard(card) {
  if (!card) return;  // the card was just removed (feature turned off)
  card._base = formValues(card);
  if (pendingSnap[card.id]) { restoreForm(pendingSnap[card.id]); delete pendingSnap[card.id]; }
  syncCard(card);
}

// Show the fields for the current (unsaved) selection and the dirty state. Pure DOM, no re-render.
function syncCard(card) {
  if (card.id === "ai-card") {
    const b = card.querySelector('input[name="ai-backend"]:checked')?.value || "off";
    for (const g of card.querySelectorAll("[data-backends]")) g.hidden = !g.dataset.backends.split(" ").includes(b);
    for (const o of card.querySelectorAll(".opt")) o.classList.toggle("on", o.querySelector("input").checked);
    $("ai-ratio-val").textContent = `${Math.round(parseFloat($("ai-ratio").value) * 100)}%`;
    const url = $("ai-base-url").value.trim().replace(/\/+$/, "");
    for (const c of card.querySelectorAll("[data-a=url-preset]")) {
      const on = c.dataset.url === url;
      c.classList.toggle("on", on);
      c.setAttribute("aria-pressed", String(on));
    }
  } else if (card.id === "st-card") {
    card.querySelector("[data-s3]").hidden = !$("st-enabled").checked;
  }
  const dirty = isDirty(card);
  card.classList.toggle("is-dirty", dirty);
  card.querySelector(".dirty").hidden = !dirty;
  const test = card.querySelector("[data-test]");
  if (!test) return;
  const off = card.id === "st-card" && !settings?.backup?.enabled;
  test.disabled = dirty || off;
  test.title = dirty ? "Save first" : off ? "Turn backups on and save first" : "";
}

// Replace the snapshot with a server response and re-render the given card from it.
function applySettings(r, cardHost) {
  if (!r || !r.ai) return loadSettings();
  settings = r;
  if (cardHost) $(cardHost)._html = null;
  if (route.name === "settings") renderView();
}

// ---------- cheap updates (every poll) ----------
function highlight() {
  for (const r of document.querySelectorAll("#view .row")) {
    const m = models.get(r.dataset.m);
    r.classList.toggle("playing", !!m && isPlaying(m));
  }
  const mode = S?.mode || {};
  const radio = mode.playlist ? null : mode.shuffle ? "*shuffle" : mode.genre;
  for (const t of document.querySelectorAll("#view .tile[data-genre]")) t.classList.toggle("active", !!S?.now && t.dataset.genre === radio);
  for (const t of document.querySelectorAll("#view .tile[data-plid]")) t.classList.toggle("active", mode.playlist?.id === t.dataset.plid);
  const big = $("pl-big-play");
  if (big) {
    const mine = mode.playlist?.id === (route.name === "liked" ? "liked" : route.id) && playingNow();
    setIcon(big, mine ? "pause" : "play_arrow");
    big.title = mine ? "Pause" : "Play";
    big.setAttribute("aria-label", big.title);
  }
}

function renderSidebar() {
  $("liked-count").textContent = `Playlist · ${library.length} song${library.length === 1 ? "" : "s"}`;
  const playing = S?.mode?.playlist?.id;
  setHTML($("sidebar-playlists"), playlists.map((p) => `<a href="#/playlist/${enc(p.id)}" class="lib-item" data-plid="${esc(p.id)}">
      ${playlistArt("lib-art", p)}
      <span class="lib-meta"><span class="lib-name">${esc(p.name)}${p.source === "ai" ? '<span class="tag">AI</span>' : ""}</span><span class="lib-sub">Playlist · ${p.count} song${p.count === 1 ? "" : "s"}</span></span>
      <span class="spk" title="Playing" aria-label="Playing">${icon("volume_up")}</span></a>`).join(""));
  for (const a of $("sidebar-playlists").children) {
    a.classList.toggle("playing", a.dataset.plid === playing);
    a.classList.toggle("active", route.name === "playlist" && a.dataset.plid === route.id);
  }
}

function trackSub(t) {
  const mode = S?.mode || {};
  if (mode.playlist?.plus) return `Shuffle+ · ${mode.playlist.name}${t.library_id ? "" : " · new"}`;
  if (mode.playlist) {
    const pos = (t.playlist_pos ?? mode.playlist.position ?? 0) + 1;
    return `${mode.playlist.source === "ai" ? "AI playlist" : "Playlist"} · ${mode.playlist.name} ${pos}/${mode.playlist.count}`;
  }
  return t.genre ? genreLabel(t.genre) : "AI is choosing…";
}

// No status line under the player; a failed song is reported once as a toast.
let erroredId = null;
function reportTrackError(t) {
  if (t?.status === "error" && t.id !== erroredId) {
    erroredId = t.id;
    toast(t.error || "This song couldn't be made");
  }
}

const REPEAT_NEXT = { off: "all", all: "one", one: "off" };
const REPEAT_TIP = { off: "Repeat: off", all: "Repeat: all (playlist / radio keeps going)", one: "Repeat: this song" };

// ---------- visualizer above the player ----------
let appPrefs = { visualizer: true };
async function loadAppPrefs() {
  try { appPrefs = (await api("/settings")).app || appPrefs; } catch { /* keep defaults */ }
}

// Live spectrum from this tab's own audio. When an overlay plays, there's nothing here to analyse.
const PVIZ_BARS = 96;
const PVIZ_HALF = PVIZ_BARS / 2; // mirrored around the centre: bass in the middle, highs to the sides
let pvizLevels = new Array(PVIZ_HALF).fill(0);
function drawPlayerViz() {
  requestAnimationFrame(drawPlayerViz);
  const c = $("player-viz");
  const live = appPrefs.visualizer !== false && out && out.analyser && out.cur && !wantPaused && !out.paused;
  const target = live ? out.levels(PVIZ_HALF) : null;
  if (!out) pvizLevels.fill(0); // audio moved elsewhere (overlay) or stopped: no fade-out needed
  if (!target && pvizLevels.every((v) => v < 0.005)) { if (!c.hidden) c.hidden = true; return; }
  c.hidden = false;
  const w = c.clientWidth, h = c.clientHeight, dpr = window.devicePixelRatio || 1;
  if (c.width !== Math.round(w * dpr)) { c.width = Math.round(w * dpr); c.height = Math.round(h * dpr); }
  const g = c.getContext("2d");
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, w, h);
  pvizLevels = pvizLevels.map((v, i) => { const t = target ? target[i] : 0; return v + (t - v) * (t > v ? 0.55 : 0.12); });
  // Bars stand on the top edge of the player area and grow upward only, mirrored from the centre.
  const gap = 3, bw = (w - gap * (PVIZ_BARS - 1)) / PVIZ_BARS, center = PVIZ_HALF;
  const grad = g.createLinearGradient(0, h, 0, 0);
  grad.addColorStop(0, "rgba(126, 224, 181, .75)");
  grad.addColorStop(1, "rgba(138, 180, 255, .15)");
  g.fillStyle = grad;
  const r = Math.min(3, bw / 2);
  for (let i = 0; i < PVIZ_HALF; i++) {
    const bh = pvizLevels[i] * (h - 2);
    if (bh < 0.5) continue;
    for (const slot of [center + i, center - 1 - i]) { // right and left of the centre
      g.beginPath();
      g.roundRect ? g.roundRect(slot * (bw + gap), h - bh, bw, bh, [r, r, 0, 0]) : g.rect(slot * (bw + gap), h - bh, bw, bh);
      g.fill();
    }
  }
}

function renderPlayer() {
  const now = S?.now;
  $("np-name").textContent = now ? (now.name || (now.genre ? genreLabel(now.genre) : "AI is choosing…")) : "Nothing playing";
  $("np-sub").textContent = now ? trackSub(now) : "Pick a radio or a playlist";
  $("np-art").style.background = now ? gradient(now.hash || now.id) : "";
  reportTrackError(now);
  const playing = playingNow();
  setIcon($("play"), playing ? "pause" : "play_arrow");
  $("play").title = playing ? "Pause (space)" : "Play (space)";
  $("play").setAttribute("aria-label", playing ? "Pause" : "Play");
  const ready = !!now && now.status === "ready";
  const isLiked = !!now && (libIds.has(now.hash) || libIds.has(now.library_id));
  $("like").classList.toggle("on", isLiked);
  setIcon($("like"), isLiked ? "favorite" : "favorite_border");
  $("like").title = isLiked ? "Remove from Liked songs (l)" : "Like (l)";
  $("like").setAttribute("aria-label", isLiked ? "Remove from Liked songs" : "Like");
  $("like").setAttribute("aria-pressed", String(isLiked));
  $("like").disabled = !ready;
  $("add-now").disabled = !ready;
  $("download").disabled = !ready;
  $("skip").disabled = !now;
  const rep = S?.mode?.repeat || "off";
  // Shuffle and repeat only mean something for a playlist (a radio never ends).
  const mp = S?.mode?.playlist;
  const sb = $("shuffle");
  sb.classList.toggle("gone", !mp);
  // Radios and Shuffle+ (a radio on the playlist) have no loop, previous or queue.
  const radio = !mp || mp.plus;
  $("repeat").classList.toggle("gone", radio);
  $("prev").classList.toggle("gone", radio);
  $("queue-link").hidden = radio;
  if (mp) {
    const sh = shuffleOf(mp.id);
    setIcon(sb, sh === "auto" ? "auto_awesome" : "shuffle");
    sb.classList.toggle("on", sh !== "off");
    sb.title = SHUFFLE_TIP[sh];
    sb.setAttribute("aria-label", SHUFFLE_TIP[sh]);
  }
  const rb = $("repeat");
  setIcon(rb, rep === "one" ? "repeat_one" : "repeat");
  rb.classList.toggle("on", rep !== "off");
  rb.title = REPEAT_TIP[rep];
  rb.setAttribute("aria-label", REPEAT_TIP[rep]);
  renderDevice();
  renderVolume();
  if (typeof mediaSync === "function") mediaSync(); // media.js: lock screen / notification
}

// The speaker button shows where the music plays: this tab (speaker icon), or another
// player (cast icon + its name). Its menu moves playback ("Play on").
function renderDevice() {
  const rem = isRemote();
  const sp = live.speaker;
  const who = rem ? (live.clients.find((c) => c.client === sp.client)?.name || (sp.kind === "overlay" ? "Overlay" : "Another tab")) : "";
  setIcon($("device-ico"), rem ? "cast" : "speaker");
  $("device-label").hidden = !rem;
  $("device-label").textContent = who;
  $("device").classList.toggle("on", rem || !!sinkId);
  $("device").classList.toggle("remote", rem);
  $("device").title = rem ? `Playing on ${who}` : "Output device";
  $("device").setAttribute("aria-label", rem ? `Playing on ${who}. Change output` : "Output device");
}

// As a remote the slider shows (and sets) the speaker's volume; this tab's own volume is kept.
const remoteVol = { v: null, at: 0 }; // last value this remote sent
function shownVolume() {
  if (!isRemote()) return volume;
  const pb = remotePlayback();
  if (remoteVol.v !== null && (Date.now() - remoteVol.at < 1500 || typeof pb?.volume !== "number")) return remoteVol.v;
  return typeof pb?.volume === "number" ? pb.volume : volume;
}

function renderVolume() {
  const v = shownVolume();
  const muted = v <= 0;
  if (Number($("volume").value) !== v) $("volume").value = v;
  setIcon($("mute"), muted ? "volume_off" : "volume_up");
  $("mute").title = muted ? "Unmute" : "Mute";
  $("mute").setAttribute("aria-label", muted ? "Unmute" : "Mute");
}

let unmuteTo = volume > 0 ? volume : 0.8;
let volTimer = null, volQueued = null;
function sendVolume(v) {
  if (volTimer) { volQueued = v; return; }
  control({ action: "volume", volume: v });
  volTimer = setTimeout(() => {
    volTimer = null;
    if (volQueued !== null) { const q = volQueued; volQueued = null; sendVolume(q); }
  }, 200);
}

function setVolume(v) {
  v = Math.max(0, Math.min(1, v));
  if (v > 0) unmuteTo = v;
  if (isRemote()) {
    remoteVol.v = v;
    remoteVol.at = Date.now();
    sendVolume(v);
    renderVolume();
    return;
  }
  volume = v;
  store.set("volume", volume);
  if (out) out.setVolume(volume);
  renderVolume();
  if (isSpeaker()) { clearTimeout(reportSoon); reportSoon = setTimeout(report, 300); }
}

// ---------- seeking ----------
let seekDrag = null; // fraction 0..1 while the pointer is down on the progress bar

function seekFraction(ev) {
  const r = $("seek").getBoundingClientRect();
  return Math.max(0, Math.min(1, (ev.clientX - r.left) / r.width));
}

function seekTo(t) {
  if (!out?.cur && isRemote()) {
    const pos = remotePos();
    if (!pos) return;
    t = Math.max(0, Math.min(t, pos.music));
    live.playback = { ...live.playback, position: t, at: serverNow() }; // shown until the speaker reports
    control({ action: "seek", t });
    renderProgress(remotePos());
    return;
  }
  if (!out?.cur) return;
  out.seek(Math.max(0, Math.min(t, out.cur.music)));
  renderProgress(out.position());
  report();
}

function bindSeek() {
  const bar = $("seek");
  bar.onpointerdown = (ev) => {
    if (!progressPos() || ev.button !== 0) return;
    ev.preventDefault();
    bar.setPointerCapture(ev.pointerId);
    bar.classList.add("dragging");
    seekDrag = seekFraction(ev);
    renderProgress(progressPos());
  };
  bar.onpointermove = (ev) => {
    if (seekDrag === null) return;
    seekDrag = seekFraction(ev);
    renderProgress(progressPos());
  };
  const finish = (ev, apply) => {
    if (seekDrag === null) return;
    const f = apply ? seekFraction(ev) : null;
    seekDrag = null;
    bar.classList.remove("dragging");
    const pos = progressPos();
    if (f !== null && pos) seekTo(f * pos.music);
  };
  bar.onpointerup = (ev) => finish(ev, true);
  bar.onpointercancel = (ev) => finish(ev, false);
  bar.onkeydown = (ev) => {
    const pos = progressPos();
    if (!pos) return;
    const step = { ArrowLeft: -5, ArrowRight: 5, PageDown: -30, PageUp: 30 }[ev.key];
    if (step !== undefined) seekTo(pos.t + step);
    else if (ev.key === "Home") seekTo(0);
    else if (ev.key === "End") seekTo(pos.music - 1);
    else return;
    ev.preventDefault();
    ev.stopPropagation();
  };
}

function renderProgress(pos) {
  if (!pos) {
    $("song-bar").style.width = "0";
    $("seek-knob").style.left = "0";
    $("seek").setAttribute("aria-valuemax", "0");
    $("seek").setAttribute("aria-valuenow", "0");
    $("t-cur").textContent = "0:00";
    $("t-total").textContent = S?.now?.music_seconds ? fmtTime(S.now.music_seconds) : "0:00";
    return;
  }
  const { music, track } = pos;
  const t = seekDrag !== null ? seekDrag * music : pos.t; // while dragging, show where it will land
  const pct = `${Math.min(100, (100 * t) / music)}%`;
  $("song-bar").style.width = pct;
  $("seek-knob").style.left = pct;
  $("seek").setAttribute("aria-valuemax", String(Math.round(music)));
  $("seek").setAttribute("aria-valuenow", String(Math.round(Math.min(t, music))));
  $("seek").setAttribute("aria-valuetext", `${fmtTime(Math.min(t, music))} of ${fmtTime(music)}`);
  $("t-cur").textContent = fmtTime(Math.min(t, music));
  $("t-total").textContent = fmtTime(music);
}


// ---------- menu & dialog ----------
let menuAnchor = null;
// items: {label, run, checked, disabled, danger} | {sep: true} | {heading} | {note}
function openMenu(anchor, items) {
  const m = $("menu");
  m.innerHTML = "";
  for (const it of items) {
    let el;
    if (it.sep) el = document.createElement("hr");
    else if (it.heading) { el = document.createElement("div"); el.className = "label"; el.textContent = it.heading; }
    else if (it.note) { el = document.createElement("div"); el.className = "note"; el.textContent = it.note; }
    else {
      el = document.createElement("button");
      el.type = "button";
      el.setAttribute("role", "menuitem");
      if (it.checked) {
        el.classList.add("checked");
        el.innerHTML = icon("check", "chk") + esc(it.label);
      } else {
        el.textContent = it.label;
      }
      if (it.danger) el.style.color = "var(--danger)";
      el.disabled = !!it.disabled;
      el.onclick = (ev) => { ev.stopPropagation(); closeMenu(); it.run?.(); };
    }
    m.appendChild(el);
  }
  m.hidden = false;
  menuAnchor = anchor;
  const r = anchor.getBoundingClientRect();
  const mw = m.offsetWidth, mh = m.offsetHeight;
  let left = Math.min(r.right - mw, window.innerWidth - mw - 8);
  left = Math.max(8, left);
  let top = r.bottom + 4;
  if (top + mh > window.innerHeight - 8) top = Math.max(8, r.top - mh - 4);
  m.style.left = `${left}px`;
  m.style.top = `${top}px`;
  m.querySelector("button:not(:disabled)")?.focus({ preventScroll: true });
}

function closeMenu() {
  $("menu").hidden = true;
  menuAnchor = null;
}

// Resolves to the input text (prompt), true (confirm) or null (cancelled).
function ask({ title, text = "", value, ok = "OK", danger = false, type = "text" }) {
  const d = $("dialog");
  $("dialog-title").textContent = title;
  $("dialog-text").textContent = text;
  $("dialog-text").hidden = !text;
  const input = $("dialog-input");
  const isPrompt = value !== undefined;
  input.hidden = !isPrompt;
  input.value = isPrompt ? value : "";
  input.type = type;
  input.autocomplete = type === "password" ? "new-password" : "off";
  const okBtn = $("dialog-ok");
  okBtn.textContent = ok;
  okBtn.className = `pill ${danger ? "danger" : "accent"}`;
  d.returnValue = "";
  closeMenu();
  return new Promise((resolve) => {
    d.addEventListener("close", () => {
      if (d.returnValue !== "ok") return resolve(null);
      if (!isPrompt) return resolve(true);
      const v = input.value.trim();
      resolve(v || null);
    }, { once: true });
    d.showModal();
    if (isPrompt) { input.focus(); input.select(); }
  });
}

// ---------- actions ----------
// Choosing music means "play": here if this tab is (or can become) the speaker, else on the speaker.
async function act(fn) {
  wantPaused = false;
  const local = wantLocal();
  try {
    S = await fn();
    afterState();
    if (!local && remotePlayback()?.state === "paused") control({ action: "resume" });
  } catch (e) {
    toast(e.message);
  }
  return S;
}

async function mutate(fn, okMsg) {
  try {
    const r = await fn();
    if (okMsg) toast(typeof okMsg === "function" ? okMsg(r) : okMsg);
    await loadLib();
    return r;
  } catch (e) {
    toast(e.message);
    return null;
  }
}

// Like Spotify: more than 3 s into a song, previous restarts it; otherwise it goes back one song.
async function previous() {
  const mp = S?.mode?.playlist;
  if (!S?.now || !mp || mp.plus) return; // radios have no previous
  const pos = out?.cur ? out.position() : isRemote() ? remotePos() : null;
  if (pos && pos.t > 3) return seekTo(0);
  try {
    const r = await api("/previous", {});
    if (!r.moved) return seekTo(0);
    S = r;
    afterState();
  } catch (e) {
    toast(e.message);
  }
}

async function togglePause() {
  if (!S?.now) return act(() => api("/random", {}));
  if (isRemote()) return control({ action: "toggle" });
  wantLocal();
  await setPaused(!!(out.cur && !wantPaused));
}

async function setPaused(paused) {
  if (!out) return;
  wantPaused = paused;
  try {
    await (paused ? out.pause() : out.resume());
  } catch { /* blocked by the browser until a click */ }
  if (!paused) reconcile();
  report();
  renderPlayer();
  highlight();
}

function playRow(m) {
  if (isPlaying(m) && hasPlayback()) return togglePause();
  if (m.ctx === "pl") return playPlaylist(m.plId, m.id);
  if (m.ctx === "queue") return act(() => api("/queue/jump", { pos: m.pos }));
  if (m.ctx === "next") return act(() => api("/skip", { from_id: S?.now?.id, manual: true }));
  if (m.ctx === "lib") return playPlaylist("liked", m.id);  // a liked song plays on through Liked songs
  // Persisted history may outlive its track id; prefer the saved song when there is one.
  const id = (m.ctx === "recent" && savedId(m)) || m.id;
  return act(() => api("/play", { id }));
}

async function toggleLike(m) {
  const sid = savedId(m);
  if (!sid) {
    return mutate(() => api("/like", { id: m.id }), (r) => (r.already ? "Already in Liked songs" : `Saved “${r.entry?.name ?? m.name}”`));
  }
  const yes = await ask({ title: "Remove from Liked songs?", text: `“${m.name}” will no longer be in Liked songs. Playlists that have it keep it.`, ok: "Remove", danger: true });
  if (yes) mutate(() => api(`/library/${enc(sid)}`, undefined, "DELETE"), "Removed from Liked songs");
}

function addMenu(anchor, m) {
  const items = [{ heading: "Add to playlist" }];
  for (const p of playlists) {
    const has = !!m.hash && (p.song_ids || []).includes(m.hash);
    items.push({
      label: p.name, checked: has,
      run: () => (has ? toast(`Already in ${p.name}`) : mutate(() => api(`/playlists/${enc(p.id)}/songs`, { id: m.id }), `Added to ${p.name}`)),
    });
  }
  if (playlists.length) items.push({ sep: true });
  items.push({ label: "New playlist…", run: () => newPlaylist(m) });
  openMenu(anchor, items);
}

async function newPlaylist(m) {
  const name = await ask({ title: "New playlist", value: `My playlist #${playlists.length + 1}`, ok: "Create" });
  if (!name) return;
  const sid = m ? savedId(m) : null;
  const r = await mutate(async () => {
    const res = await api("/playlists", sid ? { name, song_id: sid } : { name });
    // Unsaved songs go through /songs, which saves them first.
    if (m && !sid) await api(`/playlists/${enc(res.playlist.id)}/songs`, { id: m.id });
    return res;
  }, `Created “${name}”`);
  if (r?.playlist && !m) location.hash = `#/playlist/${enc(r.playlist.id)}`;
}

function moreMenu(anchor, m) {
  const sid = savedId(m);
  const ready = !m.tid || m.status === "ready";
  const items = [
    { label: isPlaying(m) ? "Play / pause" : "Play", run: () => playRow(m), disabled: !ready },
    { sep: true },
    ...(hasFeature("download") ? [
      { label: "Download MP3", run: () => download(m.id, "mp3"), disabled: !ready },
      { label: "Download WAV", run: () => download(m.id, "wav"), disabled: !ready },
    ] : []),
  ];
  if (sid) items.push({ sep: true }, { label: "Rename…", run: () => renameSong(sid, m.name) });
  if (m.ctx === "pl") items.push({ sep: true }, { label: "Move up", run: () => moveRow(m, -1), disabled: m.index === 0 }, { label: "Move down", run: () => moveRow(m, 1), disabled: !!m.last }, { sep: true }, { label: "Remove from playlist", run: () => removeFromPlaylist(m) });
  if (sid) {
    if (m.ctx !== "pl") items.push({ sep: true });
    items.push({ label: "Remove from Liked songs…", danger: true, run: () => toggleLike(m) });
  }
  items.push({ sep: true }, { label: "Delete song…", danger: true, run: () => deleteSong(m), disabled: !ready });
  openMenu(anchor, items);
}

// Removes the song everywhere: saved seed/spec, playlist entries, history, cached audio.
async function deleteSong(m) {
  const yes = await ask({
    title: "Delete song?",
    text: `“${m.name}” will be removed from Liked songs, every playlist and your history. This can't be undone.`,
    ok: "Delete", danger: true,
  });
  if (!yes) return;
  const r = await mutate(() => api(`/songs/${enc(m.hash || m.id)}`, undefined, "DELETE"), "Song deleted");
  if (!r) return;
  plDetail.clear();
  if (route.name === "playlist") loadPlaylist(route.id);
  loadHistory();
  refresh();
}

async function renameSong(id, current) {
  const name = await ask({ title: "Rename song", value: current || "", ok: "Save" });
  if (name && name !== current) {
    await mutate(() => api(`/library/${enc(id)}`, { name }, "PATCH"), "Renamed");
    plDetail.clear();
    if (route.name === "playlist") loadPlaylist(route.id);
  }
}

async function removeFromPlaylist(m) {
  await mutate(() => api(`/playlists/${enc(m.plId)}/songs/${enc(m.id)}`, undefined, "DELETE"), "Removed from playlist");
}

async function moveRow(m, delta) {
  const pl = plDetail.get(m.plId);
  if (!pl?.songs) return;
  const ids = pl.songs.map((s) => s.id);
  const j = m.index + delta;
  if (j < 0 || j >= ids.length) return;
  [ids[m.index], ids[j]] = [ids[j], ids[m.index]];
  try {
    const { playlist } = await api(`/playlists/${enc(m.plId)}/order`, { song_ids: ids }, "PUT");
    if (playlist?.songs) plDetail.set(m.plId, playlist);
    else plDetail.delete(m.plId);
  } catch (e) {
    toast(e.message);
  }
  await loadLib();
}

async function renamePlaylist(id) {
  const p = playlists.find((x) => x.id === id);
  const name = await ask({ title: "Rename playlist", value: p?.name || "", ok: "Save" });
  if (name) { plDetail.delete(id); await mutate(() => api(`/playlists/${enc(id)}`, { name }, "PATCH"), "Renamed"); }
}

async function deletePlaylist(id) {
  const p = playlists.find((x) => x.id === id);
  const yes = await ask({ title: "Delete playlist?", text: `“${p?.name ?? "This playlist"}” will be deleted. Your Liked songs are not affected.`, ok: "Delete", danger: true });
  if (!yes) return;
  const r = await mutate(() => api(`/playlists/${enc(id)}`, undefined, "DELETE"), "Playlist deleted");
  if (r) { plDetail.delete(id); location.hash = "#/home"; }
}

let downloading = 0;
async function download(id, format) {
  downloading++;
  $("download").classList.add("busy");
  toast(format === "wav" ? "Rendering WAV… this can take a few seconds" : "Preparing MP3…");
  try {
    const r = await fetch(`/download/${enc(id)}`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ format }),
    });
    if (!r.ok) throw new Error(`Download failed: ${await errorText(r)}`);
    const blob = await r.blob();
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = filenameFrom(r.headers.get("content-disposition") || "") || `${id}.${format}`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 5000);
  } catch (e) {
    toast(e.message);
  } finally {
    if (--downloading === 0) $("download").classList.remove("busy");
  }
}

function filenameFrom(cd) {
  const star = /filename\*\s*=\s*([\w-]+)''([^;]+)/i.exec(cd);
  if (star) { try { return decodeURIComponent(star[2].trim().replace(/^"|"$/g, "")); } catch { /* fall through */ } }
  const plain = /filename\s*=\s*"([^"]+)"/i.exec(cd) || /filename\s*=\s*([^;]+)/i.exec(cd);
  return plain ? plain[1].trim() : "";
}

// A prompt creates a whole playlist; with an AI backend on this can take 10–50 s.
let prompting = false;
function setPromptBusy(busy) {
  prompting = busy;
  const btn = $("prompt-btn");
  $("prompt-form").classList.toggle("busy", busy);
  $("prompt-form").setAttribute("aria-busy", String(busy));
  $("prompt").disabled = busy;
  btn.disabled = busy;
  btn.innerHTML = busy ? '<span class="spin" aria-hidden="true"></span>Composing your playlist…' : "Create playlist";
}

async function sendPrompt(text) {
  if (!text || prompting) return;
  setPromptBusy(true);
  wantPaused = false;
  const local = wantLocal();
  try {
    const r = await api("/prompt", { text });
    const pl = r.playlist;
    S = r;
    if (!local && remotePlayback()?.state === "paused") control({ action: "resume" });
    $("prompt").value = "";
    if (pl?.songs) plDetail.set(pl.id, pl);
    afterState();
    await loadLib();
    if (pl?.id) location.hash = `#/playlist/${enc(pl.id)}`;
    const n = pl?.count ?? pl?.song_ids?.length ?? 0;
    toast(pl ? `Created “${pl.name}” · ${n} song${n === 1 ? "" : "s"}` : "Playlist created");
  } catch (e) {
    toast(e.message);
  } finally {
    setPromptBusy(false);
  }
}

// ---------- output devices ----------
// Browsers hide the speaker list for privacy. Firefox has a proper speaker picker
// (selectAudioOutput); Chrome only lists speakers once microphone access was granted,
// so that is the fallback (the stream is stopped at once; nothing is recorded).
async function listOutputs() {
  try {
    return (await navigator.mediaDevices.enumerateDevices())
      .filter((d) => d.kind === "audiooutput" && d.deviceId && d.deviceId !== "default");
  } catch {
    return [];
  }
}

// Where the music plays: this tab or a connected overlay (only one plays at a time).
function playOnItems() {
  const overlays = live.clients.filter((c) => c.kind === "overlay" && c.audio !== false && c.client !== CLIENT);
  const items = [{ heading: "Play on" },
    { label: "This tab", checked: live.speaker?.client === CLIENT, run: () => { if (live.speaker?.client !== CLIENT) playHere(); } }];
  overlays.forEach((c, i) => items.push({
    label: c.name || (overlays.filter((o) => !o.name).length > 1 ? `Overlay ${i + 1}` : "Overlay"),
    checked: live.speaker?.client === c.client,
    run: () => mutate(() => api("/speaker/assign", { id: c.client }), `Playing on ${c.name || "overlay"}`),
  }));
  return items;
}

async function deviceMenu(anchor) {
  const items = [...playOnItems(), { sep: true }];
  if (!SiteOut.sinkSupported() || !navigator.mediaDevices?.enumerateDevices) {
    return openMenu(anchor, [...items, { heading: "Output device" }, { note: "Switch output in system settings." }]);
  }
  const devs = await listOutputs();
  const named = devs.filter((d) => d.label);
  items.push({ heading: "Output device" });
  items.push({ label: "System default", checked: !sinkId || sinkId === "default", run: () => pickSink("") });
  named.forEach((d) => items.push({ label: d.label, checked: d.deviceId === sinkId, run: () => pickSink(d.deviceId) }));
  if (navigator.mediaDevices.selectAudioOutput) {
    items.push({ sep: true }, {
      label: "Choose speakers…",
      run: async () => {
        try {
          const d = await navigator.mediaDevices.selectAudioOutput();
          await pickSink(d.deviceId);
        } catch { /* picker closed */ }
      },
    });
  } else if (!named.length) {
    items.push({ sep: true },
      { note: "Speaker names need microphone permission; nothing is recorded." },
      {
        label: "Show speakers…",
        run: async () => {
          try {
            const s = await navigator.mediaDevices.getUserMedia({ audio: true });
            s.getTracks().forEach((t) => t.stop());
          } catch {
            return toast("Permission denied: only the system default output is available");
          }
          if (!(await listOutputs()).some((d) => d.label)) toast("No other speakers found");
          deviceMenu(anchor);
        },
      });
  }
  openMenu(anchor, items);
}

async function pickSink(id) {
  try {
    if (out) await out.setSink(id);
    sinkId = id;
    store.set("sinkId", id);
    toast(id ? "Output switched" : "Using system default output");
  } catch (e) {
    toast(`Could not switch output: ${e.message}`);
  }
  renderPlayer();
}

// ---------- settings actions ----------
function busyBtn(btn, busy) {
  if (btn && btn.isConnected) btn.disabled = busy;
}

function showResult(id, r, okText) {
  const res = $(id);
  if (!res) return;
  const secs = r.seconds != null ? ` in ${Number(r.seconds).toFixed(1)} s` : "";
  if (r.ok) {
    res.className = "result ok";
    res.textContent = `OK${secs}${okText ? ` · ${okText}` : ""}`;
  } else {
    res.className = "result bad";
    res.textContent = `Failed${secs}: ${r.error || "unknown error"}`;
  }
}

async function saveApp(btn) {
  const count = parseInt($("app-prompt-count").value, 10);
  if (!(count >= 2 && count <= 20)) { toast("Songs per prompt must be between 2 and 20"); return; }
  const body = { startup: $("app-startup").value, repeat: $("app-repeat").value, prompt_count: count,
    visualizer: $("app-visualizer").checked };
  busyBtn(btn, true);
  try {
    const r = await api("/settings/app", body, "PUT");
    appPrefs = r.app || appPrefs;
    applySettings(r, "v-app");
    toast("App settings saved");
  } catch (e) {
    toast(e.message);
    busyBtn(btn, false);
  }
}

async function saveAI(btn) {
  const card = $("ai-card");
  const backend = card.querySelector('input[name="ai-backend"]:checked')?.value || settings.ai?.backend || "off";
  const body = { backend, ratio: parseFloat($("ai-ratio").value), timeout: parseFloat($("ai-timeout").value) };
  if (!(body.timeout >= 1 && body.timeout <= 120)) { toast("Timeout must be between 1 and 120 seconds"); return; }
  if (backend === "claude-cli") body.cli_model = $("ai-cli-model").value.trim();
  if (backend === "claude-api") body.model = $("ai-model").value.trim();
  if (backend === "openai") {
    body.base_url = $("ai-base-url").value.trim();
    body.openai_model = $("ai-openai-model").value.trim();
  }
  const ak = $("ai-key-anthropic").value.trim(), ok = $("ai-key-openai").value.trim();
  if (ak) body.anthropic_api_key = ak;
  if (ok) body.openai_api_key = ok;
  busyBtn(btn, true);
  try {
    const r = await api("/settings/ai", body, "PUT");
    applySettings(r, "v-ai");
    toast("AI settings saved");
    loadGenres();
  } catch (e) {
    toast(e.message);
    busyBtn(btn, false);
  }
}

async function testAI(btn) {
  const res = $("ai-result");
  res.className = "result muted";
  res.textContent = "Testing the saved settings…";
  busyBtn(btn, true);
  try {
    const r = await api("/settings/ai/test", {});
    const b = r.brief || {};
    const brief = [b.name && `“${b.name}”`, b.genre && genreLabel(b.genre), b.key && `${b.key} ${scaleLabel(b.scale || "")}`.trim(), b.tempo && `${Math.round(b.tempo)} bpm`].filter(Boolean).join(" · ");
    showResult("ai-result", r, [r.source, brief].filter(Boolean).join(" · "));
  } catch (e) {
    res.className = "result bad";
    res.textContent = e.message;
  }
  busyBtn(btn, false);
  syncCard($("ai-card"));
}

async function saveStorage(btn) {
  let body;
  if (!$("st-enabled").checked) {
    body = { bucket: "" };
  } else {
    const bucket = $("st-bucket").value.trim();
    if (!bucket) { toast("Enter a bucket name, or turn backups off"); $("st-bucket").focus(); return; }
    const hours = parseFloat($("st-backup-hours").value || "0");
    if (!(hours >= 0)) { toast("Backup interval must be 0 or more hours"); return; }
    body = {
      bucket, prefix: $("st-prefix").value.trim() || "motif/", endpoint: $("st-endpoint").value.trim(),
      region: $("st-region").value.trim(), backup_interval_hours: hours,
    };
    const id = $("st-key-id").value.trim(), secret = $("st-key-secret").value.trim();
    if (id) body.aws_access_key_id = id;
    if (secret) body.aws_secret_access_key = secret;
  }
  busyBtn(btn, true);
  try {
    const r = await api("/settings/backup", body, "PUT");
    backups = null;
    applySettings(r, "v-storage");
    toast(body.bucket ? "Backup settings saved" : "Backups turned off");
    if (settings?.backup?.enabled) loadBackups();
  } catch (e) {
    toast(e.message);
    busyBtn(btn, false);
  }
}

async function testStorage(btn) {
  const res = $("st-result");
  res.className = "result muted";
  res.textContent = "Connecting to the saved bucket…";
  busyBtn(btn, true);
  try {
    const r = await api("/settings/backup/test", {});
    showResult("st-result", r, r.ok ? "bucket reachable" : "");
  } catch (e) {
    res.className = "result bad";
    res.textContent = e.message;
  }
  busyBtn(btn, false);
  syncCard($("st-card"));
}

function toggleReveal(btn) {
  const input = $(btn.dataset.for);
  if (!input) return;
  const show = input.type === "password";
  input.type = show ? "text" : "password";
  setIcon(btn, show ? "visibility_off" : "visibility");
  btn.title = show ? "Hide" : "Show";
  btn.setAttribute("aria-pressed", String(show));
  btn.setAttribute("aria-label", btn.getAttribute("aria-label").replace(/^(Show|Hide)/, show ? "Hide" : "Show"));
}

async function clearKey(btn) {
  const { scope, field, label } = btn.dataset;
  const yes = await ask({
    title: `Clear the saved ${label}?`,
    text: "It is removed from this server. Other unsaved changes on this card are kept.",
    ok: "Clear", danger: true,
  });
  if (!yes) return;
  const host = scope === "ai" ? "v-ai" : "v-storage";
  const card = scope === "ai" ? $("ai-card") : $("st-card");
  const snap = snapshotForm(card);
  try {
    const r = await api(`/settings/${scope}`, { [field]: "" }, "PUT");
    pendingSnap[card.id] = snap;
    applySettings(r, host);
    toast(`${label} cleared`);
  } catch (e) {
    toast(e.message);
  }
}

function presetURL(btn) {
  $("ai-base-url").value = btn.dataset.url;
  syncCard($("ai-card"));
}

async function backupNow(btn) {
  btn.disabled = true;
  try {
    const { backup } = await api("/backups", {});
    toast(`Backed up (${fmtSize(backup?.size)})`);
    await loadBackups();
  } catch (e) {
    toast(e.message);
  } finally {
    btn.disabled = false;
  }
}

async function restore(key) {
  const yes = await ask({ title: "Restore this backup?", text: `This replaces your likes, playlists, history and settings with ${key}. This can't be undone.`, ok: "Restore", danger: true });
  if (!yes) return;
  try {
    await api("/backups/restore", { key });
    toast("Backup restored");
    plDetail.clear();
    await Promise.all([loadLib(), loadHistory(), loadAppPrefs(), refresh()]);
    loadSettings();
  } catch (e) {
    toast(e.message);
  }
}

// ---------- binding ----------
function onClick(ev) {
  const menu = $("menu");
  if (!menu.hidden && !menu.contains(ev.target) && !(menuAnchor && menuAnchor.contains(ev.target))) closeMenu();
  const el = ev.target.closest("[data-a]");
  if (!el || el.disabled) return;
  const a = el.dataset.a;
  const m = el.dataset.m ? models.get(el.dataset.m) : null;
  if (a.startsWith("row-") && !m) return;
  ev.preventDefault();
  ev.stopPropagation();
  switch (a) {
    case "row-play": playRow(m); break;
    case "row-like": toggleLike(m); break;
    case "row-add": toggleMenuFor(el, () => addMenu(el, m)); break;
    case "row-more": toggleMenuFor(el, () => moreMenu(el, m)); break;
    case "row-delete": deleteSong(m); break;
    case "row-up": moveRow(m, -1); break;
    case "row-down": moveRow(m, 1); break;
    case "radio": act(() => api("/genre", { genre: el.dataset.genre })); break;
    case "shuffle-radios": act(() => api("/random", {})); break;
    case "go": location.hash = el.dataset.href; break;
    case "pl-play": {
      const id = el.dataset.pl;
      if (S?.mode?.playlist?.id === id && hasPlayback()) togglePause();
      else playPlaylist(id);
      break;
    }
    case "pl-rename": renamePlaylist(el.dataset.pl); break;
    case "pl-delete": deletePlaylist(el.dataset.pl); break;
    case "pl-generate": generateMenu(el); break;
    case "app-save": saveApp(el); break;
    case "ai-save": saveAI(el); break;
    case "ai-test": testAI(el); break;
    case "st-save": saveStorage(el); break;
    case "st-test": testStorage(el); break;
    case "reveal": toggleReveal(el); break;
    case "key-clear": clearKey(el); break;
    case "url-preset": presetURL(el); break;
    case "backup-now": backupNow(el); break;
    case "restore": restore(el.dataset.key); break;
    case "key-create": createKey(el); break;
    case "key-revoke": revokeKey(el); break;
    case "key-done": dismissNewKey(); break;
    case "key-new": openKeyForm(el.dataset.kind); break;
    case "acct-pw": changePassword(el); break;
    case "mem-invite": inviteMember(el); break;
    case "mem-remove": removeMember(el); break;
    case "mem-reset": resetMember(el); break;
    case "mem-setpw": setMemberPassword(el); break;
    case "inv-resend": resendInvite(el); break;
    case "inv-revoke": revokeInvite(el); break;
    case "mem-link-done": memLink = null; fillMembers(); break;
    case "key-cancel": openKeyForm(keyForm); break;
    case "copy": {
      const src = $(el.dataset.for);
      if (src) copyText(src.textContent).then((ok) => toast(ok ? "Copied" : "Copy failed: select and copy it manually"));
      break;
    }
  }
}

function toggleMenuFor(anchor, open) {
  if (menuAnchor === anchor && !$("menu").hidden) closeMenu();
  else open();
}

function bind() {
  bindSeek();
  document.addEventListener("click", onClick);
  window.addEventListener("hashchange", onRoute);
  window.addEventListener("resize", closeMenu);
  $("main").addEventListener("scroll", closeMenu, { passive: true });

  $("play").onclick = togglePause;
  $("skip").onclick = () => S?.now && act(() => api("/skip", { from_id: S.now.id, manual: true }));
  $("prev").onclick = previous;
  $("like").onclick = () => S?.now && toggleLike(trackRow(S.now, "now"));
  $("add-now").onclick = (ev) => { ev.stopPropagation(); S?.now && toggleMenuFor($("add-now"), () => addMenu($("add-now"), trackRow(S.now, "now"))); };
  $("download").onclick = (ev) => {
    ev.stopPropagation();
    const now = S?.now;
    if (!now) return;
    toggleMenuFor($("download"), () => openMenu($("download"), [
      { heading: "Download" },
      { label: "MP3", run: () => download(now.id, "mp3") },
      { label: "WAV (lossless, slower)", run: () => download(now.id, "wav") },
    ]));
  };
  $("shuffle").onclick = () => { const mp = S?.mode?.playlist; if (mp) cycleShuffle(mp.id, mp.source === "ai"); };
  $("device").onclick = (ev) => { ev.stopPropagation(); if (menuAnchor === $("device") && !$("menu").hidden) closeMenu(); else deviceMenu($("device")); };
  $("repeat").onclick = () => {
    const next = REPEAT_NEXT[S?.mode?.repeat || "off"];
    api("/repeat", { mode: next }).then((r) => { S = r; afterState(); toast(REPEAT_TIP[next]); }).catch((e) => toast(e.message));
  };
  $("new-playlist").onclick = () => newPlaylist(null);
  $("logout").onclick = logout;
  $("download").hidden = !hasFeature("download");
  $("prompt-form").onsubmit = (ev) => {
    ev.preventDefault();
    const text = $("prompt").value.trim();
    if (text && !prompting) { $("prompt").blur(); sendPrompt(text); }
  };
  $("volume").value = volume;
  $("volume").oninput = (e) => setVolume(parseFloat(e.target.value));
  $("mute").onclick = () => setVolume(shownVolume() > 0 ? 0 : unmuteTo);

  // Liked view toolbar lives in the skeleton; only its results re-render.
  $("view").addEventListener("input", (ev) => {
    const card = ev.target.closest?.(".settings-card");
    if (card) syncCard(card);
  });
  $("view").addEventListener("change", (ev) => {
    if (ev.target.id?.startsWith("ov-")) updateOverlayURL();
    if (ev.target.dataset?.feat) { setFeature(ev.target); return; }
    const card = ev.target.closest?.(".settings-card");
    if (card) syncCard(card);
  });
  window.addEventListener("beforeunload", (ev) => {
    if (route.name === "settings" && settingsDirty()) { ev.preventDefault(); ev.returnValue = ""; }
  });
  // Tiles are focusable divs: Enter/Space activate them.
  $("view").addEventListener("keydown", (ev) => {
    if ((ev.key === "Enter" || ev.key === " ") && ev.target.classList?.contains("tile")) {
      ev.preventDefault();
      ev.stopPropagation();
      ev.target.click();
    }
  });

  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") {
      if (!$("menu").hidden) { closeMenu(); ev.preventDefault(); return; }
      if (document.activeElement === $("prompt")) $("prompt").blur();
      return;
    }
    if ($("dialog").open || ev.metaKey || ev.ctrlKey || ev.altKey) return;
    const tag = ev.target.tagName;
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || ev.target.isContentEditable) return;
    if (ev.code === "Space") {
      if (tag === "BUTTON" || tag === "A") return; // let focused controls activate
      ev.preventDefault();
      togglePause();
    } else if (ev.key === "n") $("skip").click();
    else if (ev.key === "p") $("prev").click();
    else if (ev.key === "l") $("like").click();
    else if (ev.key === "/") {
      ev.preventDefault();
      if (route.name !== "home") location.hash = "#/home";
      requestAnimationFrame(() => $("prompt").focus());
    }
  });
}

// ---------- accounts ----------
function until(ts) {
  const s = ts - Date.now() / 1000;
  if (s <= 0) return "expired";
  if (s < 3600) return `in ${Math.max(1, Math.floor(s / 60))} min`;
  if (s < 86400) return `in ${Math.floor(s / 3600)} h`;
  return `in ${Math.floor(s / 86400)} d`;
}

function accountCardHtml() {
  if (!me?.login) return "";
  const u = me.user;
  return `<section class="card" id="account-card" aria-labelledby="acct-h">
    <div class="card-head"><h2 id="acct-h">Account</h2></div>
    <div class="kv-row"><span>Email</span><span>${esc(u.email)}</span></div>
    <div class="kv-row"><span>Role</span><span>${esc(u.role)}</span></div>
    <h3 class="sub-h">Change password</h3>
    <div class="field"><label for="acct-cur">Current password</label><input id="acct-cur" type="password" autocomplete="current-password"></div>
    <div class="field"><label for="acct-new">New password</label><input id="acct-new" type="password" autocomplete="new-password" aria-describedby="acct-new-hint"><div class="hint" id="acct-new-hint">At least 8 characters.</div></div>
    <div class="field"><label for="acct-new2">Repeat new password</label><input id="acct-new2" type="password" autocomplete="new-password"></div>
    <div class="actions"><button type="button" class="pill accent" data-a="acct-pw">Change password</button></div>
  </section>`;
}

async function changePassword(btn) {
  const cur = $("acct-cur").value, next = $("acct-new").value;
  if (!cur) { toast("Enter your current password"); $("acct-cur").focus(); return; }
  if (next.length < 8) { toast("New password must be at least 8 characters"); $("acct-new").focus(); return; }
  if (next !== $("acct-new2").value) { toast("The new passwords don't match"); $("acct-new2").focus(); return; }
  busyBtn(btn, true);
  try {
    await api("/auth/password", { current: cur, new: next });
    for (const id of ["acct-cur", "acct-new", "acct-new2"]) $(id).value = "";
    toast("Password changed");
  } catch (e) {
    toast(e.message);
  }
  busyBtn(btn, false);
}

async function logout() {
  try { await api("/auth/logout", {}); } catch { /* already signed out */ }
  route = { name: "home" }; // nothing to confirm on the way out
  location.hash = "#/home";
  location.reload();
}

function renderAccount() {
  for (const el of document.querySelectorAll(".admin-only")) el.hidden = !canManage();
  $("account").hidden = !me?.login;
  if (!me?.login) return;
  $("account-email").textContent = me.user.email;
  $("account-email").title = me.user.email;
}

// ---------- members (admin) ----------
async function loadMembers() {
  try {
    members = await api("/users");
  } catch (e) {
    members = { error: e.message };
  }
  if (route.name === "members") fillMembers();
}

function membersShell() {
  return `<section class="card" id="members-card" aria-labelledby="mem-inv-h">
      <div class="card-head"><h2 id="mem-inv-h">Invite someone</h2></div>
      <div id="mem-smtp"></div>
      <div class="invite-row"><input id="invite-email" type="email" inputmode="email" placeholder="Email to invite" aria-label="Email to invite" autocomplete="off" spellcheck="false" autocapitalize="off">
        <button type="button" class="pill accent" data-a="mem-invite">Invite</button></div>
      <div class="invite-feats"><span class="muted small">They can use</span>
        <div class="feat-row" role="group" aria-label="Features for the new member">${FEATURES.map(([k, l]) => `<label class="scope-chip"><input type="checkbox" data-invfeat="${k}" checked><span>${l}</span></label>`).join("")}</div></div>
      <div id="mem-link"></div>
    </section>
    <section class="card" aria-labelledby="mem-h">
      <div class="card-head"><h2 id="mem-h">Members</h2></div>
      <div id="mem-users" class="keylist"></div>
    </section>
    <section class="card" aria-labelledby="mem-pend-h">
      <div class="card-head"><h2 id="mem-pend-h">Pending invites</h2></div>
      <div id="mem-invites" class="keylist"></div>
    </section>`;
}

function memLinkHtml() {
  if (!memLink) return "";
  return `<div class="newkey" role="region" aria-label="Link for ${esc(memLink.email)}">
    <div class="newkey-head"><b>${icon("key", "inline")} Email isn't set up — send this link yourself.</b><span class="muted small">For ${esc(memLink.email)}</span></div>
    <div class="copy-row"><code id="mem-link-text" class="copy-text">${esc(memLink.link)}</code><button type="button" class="pill small accent" data-a="copy" data-for="mem-link-text">${icon("content_copy")}Copy</button></div>
    <div class="actions"><button type="button" class="pill" data-a="mem-link-done">Done</button></div></div>`;
}

function fillMembers() {
  const host = $("v-members");
  if (!isAdmin() || !me.login) { setHTML(host, ""); return; }
  if (!$("members-card")) { host._html = null; setHTML(host, membersShell()); }
  setHTML($("mem-link"), memLinkHtml());
  if (!members || members.error) {
    const msg = members ? `Could not load members: ${esc(members.error)}` : "Loading…";
    setHTML($("mem-smtp"), "");
    setHTML($("mem-users"), `<div class="empty small">${msg}</div>`);
    setHTML($("mem-invites"), "");
    return;
  }
  setHTML($("mem-smtp"), members.smtp === false ? '<p class="note">Email isn\'t set up on this server, so invite and reset links are shown here for you to send yourself.</p>' : "");
  const users = members.users || [], invites = members.invites || [];
  setHTML($("mem-users"), users.map((u) => {
    const self = u.id === me.user.id;
    return `<div class="keyrow">
      <span class="key-ico">${icon("key")}</span>
      <div class="key-meta">
        <div class="key-name">${esc(u.email)}${self ? ' <span class="muted small">(you)</span>' : ""}</div>
        <div class="key-sub"><span class="kpill ${u.role === "admin" ? "admin" : ""}">${esc(u.role)}</span>
          <span title="${u.last_login ? esc(fmtDate(u.last_login)) : ""}">${u.last_login ? `Last login ${esc(ago(u.last_login))}` : "Never logged in"}</span></div>
      </div>
      ${self ? "" : `<div class="feat-row" role="group" aria-label="Features for ${esc(u.email)}">${FEATURES.map(([k, l]) => `<label class="scope-chip"><input type="checkbox" data-feat="${k}" data-id="${esc(u.id)}" ${u.features?.[k] === false ? "" : "checked"}><span>${l}</span></label>`).join("")}</div>
      <div class="row-btns"><button type="button" class="pill small" data-a="mem-reset" data-id="${esc(u.id)}" data-email="${esc(u.email)}">Send reset link</button>
        <button type="button" class="pill small" data-a="mem-setpw" data-id="${esc(u.id)}" data-email="${esc(u.email)}">Set password</button>
        <button type="button" class="pill small danger-text" data-a="mem-remove" data-id="${esc(u.id)}" data-email="${esc(u.email)}">Remove</button></div>`}
    </div>`;
  }).join("") || '<div class="empty small">No members yet.</div>');
  setHTML($("mem-invites"), invites.map((v) => `<div class="keyrow">
      <span class="key-ico">${icon("schedule")}</span>
      <div class="key-meta">
        <div class="key-name">${esc(v.email)}</div>
        <div class="key-sub"><span title="${esc(fmtDate(v.expires))}">Expires ${esc(until(v.expires))}</span>
          <span>${esc(featSummary(v.features))}</span></div>
      </div>
      <div class="row-btns"><button type="button" class="pill small" data-a="inv-resend" data-id="${esc(v.id)}" data-email="${esc(v.email)}">Resend</button>
        <button type="button" class="pill small danger-text" data-a="inv-revoke" data-id="${esc(v.id)}" data-email="${esc(v.email)}">Revoke</button></div>
    </div>`).join("") || '<div class="empty small">No pending invites.</div>');
}

// An email-less server returns the link instead; show it so the admin can pass it on.
function memResult(r, email, what) {
  memLink = r.link ? { link: r.link, email } : null;
  fillMembers();
  if (!r.link) toast(r.sent ? `${what} sent to ${email}` : `${what} created`);
}

// "All features", or the ones the member gets.
function featSummary(f) {
  const on = FEATURES.filter(([k]) => f?.[k] !== false).map(([, l]) => l);
  return on.length === FEATURES.length ? "All features" : on.length ? on.join(", ") : "No extra features";
}

async function inviteMember(btn) {
  const email = $("invite-email").value.trim();
  if (!/^\S+@\S+$/.test(email)) { toast("Enter an email address"); $("invite-email").focus(); return; }
  busyBtn(btn, true);
  try {
    const features = Object.fromEntries([...document.querySelectorAll("[data-invfeat]")].map((b) => [b.dataset.invfeat, b.checked]));
    const r = await api("/users/invite", { email, features });
    $("invite-email").value = "";
    for (const b of document.querySelectorAll("[data-invfeat]")) b.checked = true;
    memResult(r, email, "Invite");
    await loadMembers();
  } catch (e) {
    toast(e.message);
  }
  busyBtn(btn, false);
}

async function resendInvite(btn) {
  const { id, email } = btn.dataset;
  busyBtn(btn, true);
  try {
    memResult(await api(`/invites/${enc(id)}/resend`, {}), email, "Invite");
    await loadMembers();
  } catch (e) {
    toast(e.message);
  }
  busyBtn(btn, false);
}

async function revokeInvite(btn) {
  const { id, email } = btn.dataset;
  const yes = await ask({ title: `Revoke invite for ${email}?`, text: "The link in their email stops working.", ok: "Revoke", danger: true });
  if (!yes) return;
  try {
    await api(`/invites/${enc(id)}`, undefined, "DELETE");
    toast("Invite revoked");
  } catch (e) {
    toast(e.message);
  }
  loadMembers();
}

async function resetMember(btn) {
  const { id, email } = btn.dataset;
  busyBtn(btn, true);
  try {
    memResult(await api(`/users/${enc(id)}/reset`, {}), email, "Reset link");
  } catch (e) {
    toast(e.message);
  }
  busyBtn(btn, false);
}

async function setMemberPassword(btn) {
  const { id, email } = btn.dataset;
  const pw = await ask({ title: `Set a password for ${email}`, text: "At least 8 characters. They are signed out everywhere and log in with this one.",
    value: "", ok: "Set password", type: "password" });
  if (pw === null) return;
  if (pw.length < 8) { toast("Use at least 8 characters"); return; }
  try {
    await api(`/users/${enc(id)}/password`, { password: pw }, "PUT");
    toast(`Password set for ${email}`);
  } catch (e) {
    toast(e.message);
  }
}

// Toggle saves at once; a failure puts the switch back.
async function setFeature(box) {
  const { feat, id } = box.dataset, on = box.checked;
  box.disabled = true;
  try {
    const r = await api(`/users/${enc(id)}`, { features: { [feat]: on } }, "PATCH");
    const u = members?.users?.find((x) => x.id === id);
    if (u) u.features = r.user?.features || { ...u.features, [feat]: on };
  } catch (e) {
    box.checked = !on;
    toast(e.message);
  }
  box.disabled = false;
}

async function removeMember(btn) {
  const { id, email } = btn.dataset;
  const yes = await ask({ title: `Remove ${email}?`, text: "Their playlists, likes, history, settings and keys are deleted for good.", ok: "Remove", danger: true });
  if (!yes) return;
  try {
    await api(`/users/${enc(id)}`, undefined, "DELETE");
    toast("Member removed");
  } catch (e) {
    toast(e.message);
  }
  loadMembers();
}

// ---------- sign-in views ----------
function showAuth(html) {
  document.body.classList.add("locked");
  const a = $("auth");
  a.hidden = false;
  a.innerHTML = `<div class="auth-card"><div class="auth-brand"><span class="logo" aria-hidden="true"></span>Motif</div>${html}</div>`;
}

const authField = (id, label, type, extra = "") => `<div class="afield"><label for="${id}">${label}</label><input id="${id}" type="${type}" ${extra}></div>`;

function showLogin(msg) {
  showAuth(`<form id="login-form">
    ${authField("login-email", "Email", "email", 'autocomplete="username" spellcheck="false" autocapitalize="off" required')}
    ${authField("login-pass", "Password", "password", 'autocomplete="current-password" required')}
    <button type="submit" class="pill accent">Log in</button>
    <div id="login-error" class="auth-error" role="alert">${esc(msg || "")}</div>
  </form>`);
  const err = $("login-error");
  $("login-email").focus();
  $("login-form").onsubmit = async (ev) => {
    ev.preventDefault();
    const btn = ev.target.querySelector("button");
    btn.disabled = true;
    err.textContent = "";
    try {
      await api("/auth/login", { email: $("login-email").value.trim(), password: $("login-pass").value });
      location.reload();
    } catch (e) {
      err.textContent = e.message;
      btn.disabled = false;
    }
  };
}

// Link from an invite or reset email: #/welcome?token=…
function welcomeToken() {
  const m = location.hash.match(/^#\/welcome(?:\?(.*))?$/);
  return m ? new URLSearchParams(m[1] || "").get("token") || "" : null;
}

async function showWelcome(token) {
  showAuth('<p class="sub">Checking your link…</p>');
  let info;
  try {
    info = await api(`/auth/token/${enc(token)}`);
  } catch {
    showAuth('<h1>Link not valid</h1><p class="sub">This link has expired or was already used.</p><a class="pill" href="#/home">Go to log in</a>');
    return;
  }
  const invite = info.kind === "invite";
  showAuth(`<h1>${invite ? "You've been invited to Motif" : "Reset your password"}</h1>
    <p class="sub">Set your password to ${invite ? "get started" : "log back in"}.</p>
    <form id="welcome-form">
      ${authField("w-email", "Email", "email", `value="${esc(info.email)}" readonly autocomplete="username"`)}
      ${authField("w-pass", "Password", "password", 'autocomplete="new-password" minlength="8" required')}
      ${authField("w-pass2", "Confirm password", "password", 'autocomplete="new-password" minlength="8" required')}
      <button type="submit" class="pill accent">${invite ? "Create account" : "Set password"}</button>
      <div id="welcome-error" class="auth-error" role="alert"></div>
    </form>`);
  const err = $("welcome-error");
  $("w-pass").focus();
  $("welcome-form").onsubmit = async (ev) => {
    ev.preventDefault();
    const pw = $("w-pass").value;
    if (pw.length < 8) { err.textContent = "Password must be at least 8 characters."; return; }
    if (pw !== $("w-pass2").value) { err.textContent = "The passwords don't match."; return; }
    const btn = ev.target.querySelector("button[type=submit]");
    btn.disabled = true;
    err.textContent = "";
    try {
      await api("/auth/token", { token, password: pw });
      location.hash = "#/home";
      location.reload();
    } catch (e) {
      err.textContent = e.message;
      btn.disabled = false;
    }
  };
}

// Signed out while running: stop audio and the stream, then ask for a login.
function lockOut() {
  if (locked) return;
  locked = true;
  stopLocal();
  streamH?.close();
  for (const t of timers) clearInterval(t);
  clearInterval(fallbackTimer);
  fallbackTimer = null;
  closeMenu();
  showLogin();
}

async function start() {
  const token = welcomeToken();
  if (token !== null) { showWelcome(token); return; }
  let r;
  try {
    r = await fetch("/auth/me");
  } catch {
    showLogin("Can't reach the server.");
    return;
  }
  if (r.status === 401) { showLogin(); return; }
  if (!r.ok) { showLogin(await errorText(r)); return; }
  me = await r.json();
  document.body.classList.remove("locked");
  $("auth").hidden = true;
  init();
}

async function init() {
  booted = true;
  fillIcons();
  renderAccount();
  renderVolume();
  bind();
  route = parseRoute();
  markNav();
  renderView();
  await loadGenres();
  await refresh();
  await loadLib();
  loadAppPrefs();
  onRoute();
  requestAnimationFrame(drawPlayerViz);
  // Live state over SSE (same origin, no key); a slow /state poll runs only while it is down.
  streamH = streamEvents(`/events?client=${enc(CLIENT)}&kind=site`, {}, onLive, onLiveStatus);
  timers.push(setInterval(tick, 100), setInterval(loadLib, 15000));
}

window.addEventListener("hashchange", () => { if (!booted) start(); });
start();
