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
const genreLabel = (g) => String(g ?? "").replace(/[-_]/g, " ");
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

// Only touch the DOM when the markup actually changed (keeps hover/menus/inputs alive).
function setHTML(el, html) {
  if (!el || el._html === html) return false;
  el._html = html;
  el.innerHTML = html;
  return true;
}

// ---------- audio output: Web Audio (gapless hand-over) ----------
// Each song plays once. The next song starts exactly when the current song's last
// bar ends (music_seconds); the old song's reverb ring-out overlaps the new start.
class WebAudioOut {
  constructor() {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    this.gapless = true;
    this.ctx = new Ctx();
    this.master = this.ctx.createGain();
    this.analyser = this.ctx.createAnalyser();
    this.analyser.fftSize = 128;
    this.master.connect(this.analyser);
    this.analyser.connect(this.ctx.destination);
    this.loading = new Map(); // id -> Promise<AudioBuffer>
    this.ready = new Map();   // id -> AudioBuffer
    this.cur = null;          // { id, src, gain, start, music, track }
    this.pending = null;      // the next song, scheduled to start when the current one's music ends
  }

  static sinkSupported() {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    return !!Ctx && typeof Ctx.prototype.setSinkId === "function";
  }

  setSink(id) { return this.ctx.setSinkId(id || ""); }

  load(track) {
    if (!this.loading.has(track.id)) {
      const p = fetch(track.mp3)
        .then((r) => { if (!r.ok) throw new Error(`audio ${r.status}`); return r.arrayBuffer(); })
        .then((b) => this.ctx.decodeAudioData(b))
        .then((buf) => { this.ready.set(track.id, buf); return buf; });
      p.catch(() => this.loading.delete(track.id));
      this.loading.set(track.id, p);
      this._trim(track.id);
    }
    return this.loading.get(track.id);
  }

  buffer(id) { return this.ready.get(id) || null; }

  // A decoded 5-minute song is ~100 MB; keep only what is playing or about to.
  _trim(keepId) {
    const keep = new Set([keepId, this.cur?.id, this.pending?.id]);
    for (const id of [...this.loading.keys()]) {
      if (this.loading.size <= 3) break;
      if (!keep.has(id)) { this.loading.delete(id); this.ready.delete(id); }
    }
  }

  _voice(track, buf, when) {
    const src = this.ctx.createBufferSource();
    src.buffer = buf;
    const gain = this.ctx.createGain();
    src.connect(gain);
    gain.connect(this.master);
    src.start(when);
    const music = Math.min(track.music_seconds || buf.duration, buf.duration);
    return { id: track.id, src, gain, start: when, music, track };
  }

  // Immediate switch (user action): short fade-out of the old song.
  playNow(track, buf) {
    const t = this.ctx.currentTime + 0.03;
    this.cancelPending();
    if (this.cur) {
      const g = this.cur.gain.gain;
      g.cancelScheduledValues(t);
      g.setValueAtTime(g.value, t);
      g.linearRampToValueAtTime(0, t + 0.4);
      this.cur.src.stop(t + 0.45);
    }
    const v = this._voice(track, buf, t);
    v.gain.gain.setValueAtTime(0, t);
    v.gain.gain.linearRampToValueAtTime(1, t + 0.01);
    this.cur = v;
  }

  // Sample-accurate start of the next song; the current one keeps ringing out on its own.
  scheduleAt(track, buf, when) {
    this.pending = this._voice(track, buf, when);
  }

  // Called every tick: promote the pending voice once its start time has passed.
  promote() {
    if (this.pending && this.ctx.currentTime >= this.pending.start) {
      this.cur = this.pending;
      this.pending = null;
      return true;
    }
    return false;
  }

  cancelPending() {
    if (this.pending) {
      try { this.pending.src.stop(); } catch { /* not started */ }
      this.pending = null;
    }
  }

  position() {
    if (!this.cur) return null;
    return { t: Math.max(0, this.ctx.currentTime - this.cur.start), music: this.cur.music, track: this.cur.track };
  }

  musicEnd() { return this.cur.start + this.cur.music; }
  now() { return this.ctx.currentTime; }
  setVolume(v) { this.master.gain.setTargetAtTime(v, this.ctx.currentTime, 0.02); }
  pause() { return this.ctx.suspend(); }
  resume() { return this.ctx.resume(); }
  get paused() { return this.ctx.state !== "running"; }
}

// ---------- audio output: <audio> fallback (small gap between songs) ----------
class ElementOut {
  constructor() {
    this.gapless = false;
    this.el = new Audio();
    this.ready = new Map();
    this.cur = null;
    this.pending = null;
  }
  static sinkSupported() { return typeof HTMLMediaElement !== "undefined" && typeof HTMLMediaElement.prototype.setSinkId === "function"; }
  setSink(id) { return this.el.setSinkId(id || ""); }
  load(track) { this.ready.set(track.id, track.mp3); return Promise.resolve(track.mp3); }
  buffer(id) { return this.ready.get(id) || null; }
  playNow(track, url) {
    this.el.src = url;
    this.el.play().catch(() => {});
    this.cur = { id: track.id, music: track.music_seconds, start: 0, track };
  }
  promote() { return false; }
  cancelPending() {}
  position() {
    if (!this.cur) return null;
    return { t: this.el.currentTime, music: this.cur.music, track: this.cur.track };
  }
  setVolume(v) { this.el.volume = v; }
  pause() { this.el.pause(); return Promise.resolve(); }
  resume() { return this.el.play(); }
  get paused() { return this.el.paused; }
}

const OutClass = (window.AudioContext || window.webkitAudioContext) ? WebAudioOut : ElementOut;

// ---------- app state ----------
let S = null;               // last server state
let out = null;             // audio output
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
let audioFail = { id: null, at: 0 };
const liked = { q: "", genre: "", sort: "newest" };
const models = new Map();   // row key -> row model (for event delegation)
let route = { name: "home" };
let mountedRoute = "";

function ensureOut() {
  if (!out) {
    out = new OutClass();
    out.setVolume(volume);
    if (sinkId && OutClass.sinkSupported()) out.setSink(sinkId).catch(() => { sinkId = ""; store.set("sinkId", ""); });
  }
  if (!wantPaused && out.paused && out.cur) out.resume();
  if (out.ctx && out.ctx.state === "suspended" && !wantPaused) out.ctx.resume();
  return out;
}

async function refresh() {
  try {
    S = await api("/state");
  } catch {
    setStatus("Server unreachable", true);
    return;
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
  const nowId = S?.now?.id ?? null;
  if (nowId !== lastNowId) {
    lastNowId = nowId;
    loadHistory();
  }
}

function reconcile() {
  if (!out || !S) return;
  const now = S.now, next = S.up_next;
  if (next && next.status === "ready") out.load(next).catch(() => {});
  if (!now || now.status !== "ready") return;
  if (out.cur?.id === now.id || out.pending?.id === now.id || awaitingId) return;
  const buf = out.buffer(now.id);
  if (buf) { out.playNow(now, buf); return; }
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
function tick() {
  if (!out || !out.cur) return renderProgress(null);
  if (out.promote()) return;
  const pos = out.position();
  renderProgress(pos);
  if (!S || !S.now || S.now.id !== out.cur.id || out.pending || awaitingId) return;
  const next = S.up_next;
  const buf = next && next.status === "ready" ? out.buffer(next.id) : null;
  if (!buf) return; // not ready yet: the current song rings out, the next starts as soon as it is
  const fromId = out.cur.id;
  if (out.gapless) {
    const t = out.now();
    const end = out.musicEnd();
    if (end - t > 2) return;
    out.scheduleAt(next, buf, Math.max(end, t + 0.05));
    // The pending voice is promoted in tick(); tell the server right when it starts.
    const wait = setInterval(() => {
      if (out.cur?.id === next.id) { clearInterval(wait); confirmAdvance(fromId, next.id); }
      else if (out.pending?.id !== next.id) clearInterval(wait); // cancelled by a user action
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
    recent = (await api("/history")).history || [];
    recent.sort((a, b) => (b.played_at || 0) - (a.played_at || 0));
  } catch { /* keep previous */ }
  historyBusy = false;
  if (route.name === "home") { renderView(); highlight(); }
}

async function loadSettings() {
  try {
    settings = await api("/settings");
  } catch (e) {
    settings = { error: e.message };
  }
  backups = null;
  if (settings?.storage?.s3) loadBackups();
  if (route.name === "settings") renderView();
}

async function loadBackups() {
  try {
    backups = await api("/admin/backups");
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
  const tag = m.source && m.source !== "procedural" ? `<span class="tag">${esc(m.source)}</span>` : "";
  const keyCell = m.tempo != null ? `${esc(m.key)} ${esc(scaleLabel(m.scale))} · ${Math.round(m.tempo)} bpm` : "";
  const sub = [m.genre ? esc(genreLabel(m.genre)) : "", m.showStatus ? statusBadge(m.status) : ""].filter(Boolean).join(" ");
  const ready = !m.tid || m.status === "ready";
  const moves = m.ctx === "pl"
    ? `<button class="icon-btn" data-a="row-up" ${k} title="Move up" aria-label="Move up" ${i === 0 ? "disabled" : ""}>↑</button>`
      + `<button class="icon-btn" data-a="row-down" ${k} title="Move down" aria-label="Move down" ${m.last ? "disabled" : ""}>↓</button>`
    : "";
  return `<div class="row" ${k} data-hash="${esc(m.hash || "")}" data-tid="${esc(m.tid || "")}" data-pl="${esc(m.plId || "")}">
  <div class="num"><span class="n">${i + 1}</span><span class="eq" aria-hidden="true">♪</span><button class="p" data-a="row-play" ${k} title="Play" aria-label="Play" ${ready ? "" : "disabled"}>▶</button></div>
  <div class="song"><div class="art" ${artStyle(m.hash || m.id)}></div><div class="meta"><div class="title">${esc(m.name)}${tag}</div><div class="sub">${sub}</div></div></div>
  <div class="cell">${keyCell}</div>
  <div class="cell">${m.seconds ? fmtTime(m.seconds) : ""}</div>
  <div class="acts">${moves}<button class="icon-btn like${isLiked ? " on" : ""}" data-a="row-like" ${k} title="${isLiked ? "Remove from Liked songs" : "Like"}" aria-label="Like" ${ready ? "" : "disabled"}>${isLiked ? "♥" : "♡"}</button><button class="icon-btn add" data-a="row-add" ${k} title="Add to playlist" aria-label="Add to playlist" ${ready ? "" : "disabled"}>＋</button><button class="icon-btn" data-a="row-more" ${k} title="More" aria-label="More">⋯</button></div>
</div>`;
}

function rowsHtml(list, cls = "") {
  if (!list.length) return "";
  return `<div class="rows ${cls}"><div class="row-head"><span>#</span><span>Title</span><span>Key · tempo</span><span>⏱</span><span></span></div>${list.map(rowHtml).join("")}</div>`;
}

// ---------- routing / views ----------
function parseRoute() {
  const h = location.hash.replace(/^#\/?/, "");
  const [name, ...rest] = h.split("/");
  if (name === "playlist" && rest[0]) return { name: "playlist", id: decodeURIComponent(rest.join("/")) };
  if (["home", "queue", "settings", "liked"].includes(name)) return { name };
  return { name: "home" };
}

function onRoute() {
  route = parseRoute();
  closeMenu();
  for (const a of document.querySelectorAll("[data-route]")) a.classList.toggle("active", a.dataset.route === route.name);
  if (route.name === "settings") loadSettings();
  if (route.name === "home") loadHistory();
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
  ({ home: fillHome, queue: fillQueue, settings: fillSettings, liked: fillLiked, playlist: fillPlaylist })[route.name]();
}

function skeleton(r) {
  switch (r.name) {
    case "home":
      return `<h1>${greeting()}</h1>
        <div class="section-title"><h2>Radios</h2><span class="muted">Endless songs in one genre</span></div>
        <div id="v-radios" class="tiles"></div>
        <div class="section-title"><h2>Your playlists</h2></div>
        <div id="v-playlists"></div>
        <div class="section-title"><h2>Recently played</h2></div>
        <div id="v-recent"></div>`;
    case "queue":
      return `<h1>Queue</h1><h2>Now playing</h2><div id="v-now" class="queue-block"></div>
        <h2>Up next</h2><div id="v-next" class="queue-block"></div>
        <h2>This session</h2><div id="v-session"></div>`;
    case "liked":
      return `<div class="hero"><div class="art liked-art">♥</div><div><div class="kind">PLAYLIST</div><h1>Liked songs</h1><div id="v-liked-meta" class="muted"></div></div></div>
        <div class="toolbar">
          <input id="liked-q" type="search" placeholder="Search liked songs" aria-label="Search liked songs" value="${esc(liked.q)}">
          <select id="liked-sort" aria-label="Sort">
            ${[["newest", "Recently added"], ["name", "Name"], ["genre", "Genre"], ["tempo", "Tempo"], ["length", "Length"]]
              .map(([v, l]) => `<option value="${v}" ${liked.sort === v ? "selected" : ""}>${l}</option>`).join("")}
          </select>
        </div>
        <div id="v-chips" class="chips"></div>
        <div id="v-liked" style="margin-top:12px"></div>`;
    case "playlist":
      return `<div id="v-pl-hero"></div><div id="v-pl-actions" class="actions"></div><div id="v-pl-rows"></div>`;
    case "settings":
      return `<h1>Settings</h1><div id="v-ai"></div><div id="v-storage"></div>`;
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
      <span class="hover-play" aria-hidden="true">▶</span></div>`).join("")
    + `<div class="tile" role="button" tabindex="0" data-a="shuffle-radios" data-genre="*shuffle" title="Shuffle radios">
      <div class="art shuffle-art"><span>⤨</span></div>
      <div class="tile-name">Shuffle radios</div><div class="tile-sub">A new genre every song</div>
      <span class="hover-play" aria-hidden="true">▶</span></div>`;
  setHTML($("v-radios"), genres.length ? radios : '<div class="empty">Loading radios…</div>');

  setHTML($("v-playlists"), playlists.length
    ? `<div class="hscroll">${playlists.map((p) => `<div class="tile" role="button" tabindex="0" data-a="go" data-href="#/playlist/${enc(p.id)}" data-plid="${esc(p.id)}">
        ${playlistArt("art", p)}
        <div class="tile-name">${esc(p.name)}</div><div class="tile-sub">${p.count} song${p.count === 1 ? "" : "s"}</div>
        <button class="hover-play" data-a="pl-play" data-pl="${esc(p.id)}" title="Play" aria-label="Play ${esc(p.name)}" ${p.count ? "" : "disabled"}>▶</button></div>`).join("")}</div>`
    : '<div class="empty">No playlists yet. Use ＋ next to “Your library” or on any song.</div>');

  const rows = recent.slice(0, 30).map((h) => entryRow(h, "recent", { hash: h.hash || h.id, libraryId: null }));
  setHTML($("v-recent"), rows.length ? rowsHtml(rows) : '<div class="empty">Songs you play show up here.</div>');
}

function fillQueue() {
  const now = S?.now, next = S?.up_next;
  setHTML($("v-now"), now ? rowsHtml([{ ...trackRow(now, "q-now"), showStatus: now.status !== "ready" }]) : '<div class="empty">Nothing playing. Pick a radio on Home.</div>');
  setHTML($("v-next"), next ? rowsHtml([{ ...trackRow(next, "q-next"), showStatus: true }]) : '<div class="empty">Nothing queued yet.</div>');
  const hist = (S?.history || []).slice(0, 50).map((t) => trackRow(t, "q-hist"));
  setHTML($("v-session"), hist.length ? rowsHtml(hist) : '<div class="empty">No songs played yet this session.</div>');
}

function fillLiked() {
  const total = library.reduce((a, e) => a + (e.music_seconds || 0), 0);
  setHTML($("v-liked-meta"), `${library.length} song${library.length === 1 ? "" : "s"}${library.length ? ` · ${fmtLong(total)}` : ""}`);
  const counts = new Map();
  for (const e of library) counts.set(e.genre, (counts.get(e.genre) || 0) + 1);
  if (liked.genre && !counts.has(liked.genre)) liked.genre = "";
  setHTML($("v-chips"), counts.size > 1
    ? [`<button class="chip${liked.genre ? "" : " on"}" data-a="chip" data-genre="">All ${library.length}</button>`]
      .concat([...counts].sort((a, b) => b[1] - a[1]).map(([g, n]) => `<button class="chip${liked.genre === g ? " on" : ""}" data-a="chip" data-genre="${esc(g)}">${esc(genreLabel(g))} ${n}</button>`)).join("")
    : "");
  const q = liked.q.trim().toLowerCase();
  let list = library.filter((e) => (!liked.genre || e.genre === liked.genre)
    && (!q || `${e.name} ${e.genre} ${e.key} ${e.scale} ${e.source}`.toLowerCase().includes(q)));
  const by = {
    newest: (a, b) => (b.liked_at || 0) - (a.liked_at || 0),
    name: (a, b) => String(a.name).localeCompare(String(b.name)),
    genre: (a, b) => String(a.genre).localeCompare(String(b.genre)) || String(a.name).localeCompare(String(b.name)),
    tempo: (a, b) => (a.tempo || 0) - (b.tempo || 0),
    length: (a, b) => (a.music_seconds || 0) - (b.music_seconds || 0),
  }[liked.sort];
  list = [...list].sort(by);
  setHTML($("v-liked"), list.length
    ? rowsHtml(list.map((e) => entryRow(e, "lib")))
    : `<div class="empty">${library.length ? "No songs match." : "Songs you like (♥) show up here."}</div>`);
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
  setHTML($("v-pl-hero"), `<div class="hero">${playlistArt("art", p)}<div><div class="kind">PLAYLIST</div><h1>${esc(p.name)}</h1>
    <div class="muted">${count} song${count === 1 ? "" : "s"}${count ? ` · ${fmtLong(secs)}` : ""}${gs.length ? ` · ${gs.map((g) => esc(genreLabel(g))).join(", ")}` : ""}</div></div></div>`);
  setHTML($("v-pl-actions"), `<button class="big-play" id="pl-big-play" data-a="pl-play" data-pl="${esc(id)}" title="Play" aria-label="Play" ${count ? "" : "disabled"}>▶</button>
    <button class="pill" data-a="pl-shuffle" data-pl="${esc(id)}" ${count ? "" : "disabled"}>⤨ Shuffle play</button>
    <button class="pill" data-a="pl-rename" data-pl="${esc(id)}">Rename</button>
    <button class="pill" data-a="pl-delete" data-pl="${esc(id)}">Delete</button>`);
  if (!pl) { setHTML($("v-pl-rows"), '<div class="empty">Loading songs…</div>'); return; }
  setHTML($("v-pl-rows"), songs.length
    ? rowsHtml(songs.map((e, i) => entryRow(e, "pl", { plId: id, index: i, last: i === songs.length - 1 })), "pl")
    : '<div class="empty">This playlist is empty. Add songs with ＋ on any song.</div>');
}

const BACKENDS = {
  off: "Procedural only. No AI calls; every song comes from the built-in generator.",
  "claude-cli": "Uses the local `claude` command line tool and its own login. No API key needed here.",
  "claude-api": "Calls the Anthropic API directly. Needs ANTHROPIC_API_KEY in the server environment.",
  openai: "Any OpenAI-compatible endpoint (OpenAI, a local server, …) at the base URL below. Uses OPENAI_API_KEY if the endpoint needs one.",
};

function fillSettings() {
  if (!settings) { setHTML($("v-ai"), '<div class="empty">Loading settings…</div>'); return; }
  if (settings.error) { setHTML($("v-ai"), `<div class="empty">Could not load settings: ${esc(settings.error)}</div>`); return; }
  const ai = settings.ai || {};
  const keys = ai.keys || {};
  const backendsList = ai.backends || Object.keys(BACKENDS);
  // Built from the loaded snapshot only, so polling never resets what is being edited.
  setHTML($("v-ai"), `<section class="card" id="ai-card"><h2>AI generation</h2>
    <p class="muted">AI picks a short brief (genre, key, tempo, instruments, name); the procedural engine still writes every note.</p>
    <div class="field"><label for="ai-backend">Backend</label><select id="ai-backend">${backendsList.map((b) => `<option value="${esc(b)}" ${b === ai.backend ? "selected" : ""}>${esc(b)}</option>`).join("")}</select>
      <div class="hint" id="ai-backend-hint">${esc(BACKENDS[ai.backend] || "")}</div></div>
    <div class="field"><label for="ai-model">Model</label><input id="ai-model" type="text" value="${esc(ai.model)}" spellcheck="false">
      <div class="hint">Used by claude-api and openai.</div></div>
    <div class="field"><label for="ai-cli-model">CLI model</label><input id="ai-cli-model" type="text" value="${esc(ai.cli_model)}" spellcheck="false">
      <div class="hint">Used by claude-cli.</div></div>
    <div class="field"><label for="ai-base-url">OpenAI-compatible base URL</label><input id="ai-base-url" type="text" value="${esc(ai.base_url)}" spellcheck="false"></div>
    <div class="field"><label for="ai-ratio">AI share of radio songs</label><input id="ai-ratio" type="range" min="0" max="1" step="0.05" value="${Number(ai.ratio) || 0}">
      <div class="hint"><span id="ai-ratio-val">${Math.round((Number(ai.ratio) || 0) * 100)}%</span> of radio songs ask the AI for a brief. Prompts always use AI when a backend is on.</div></div>
    <div class="field"><label for="ai-timeout">Timeout (seconds)</label><input id="ai-timeout" type="number" min="1" max="300" step="1" value="${esc(ai.timeout)}"></div>
    <dl class="kv"><dt>Anthropic API key</dt><dd class="${keys.anthropic ? "ok" : "bad"}">${keys.anthropic ? "configured" : "not set"}</dd>
      <dt>OpenAI API key</dt><dd class="${keys.openai ? "ok" : "bad"}">${keys.openai ? "configured" : "not set"}</dd></dl>
    <p class="muted">API keys are only read from environment variables on the server (ANTHROPIC_API_KEY, OPENAI_API_KEY). They are never entered or shown here.</p>
    <div class="actions"><button class="pill accent" data-a="ai-save">Save</button><button class="pill" data-a="ai-test">Test</button></div>
    <div id="ai-result" class="result"></div></section>`);

  const st = settings.storage || {};
  let storage = `<section class="card"><h2>Storage</h2><dl class="kv">
    <dt>S3</dt><dd class="${st.s3 ? "ok" : ""}">${st.s3 ? "enabled" : "off (local SQLite only)"}</dd>`;
  if (st.s3) {
    storage += `<dt>Bucket</dt><dd>${esc(st.bucket)}</dd><dt>Prefix</dt><dd>${esc(st.prefix)}</dd>
      <dt>Endpoint</dt><dd>${esc(st.endpoint || "AWS default")}</dd>
      <dt>Song audio in S3</dt><dd>${st.media ? "on" : "off"}</dd>
      <dt>Automatic backup</dt><dd>${st.backup_interval_hours ? `every ${esc(st.backup_interval_hours)} h` : "manual only"}</dd>`;
  }
  storage += "</dl>";
  if (st.s3) {
    storage += `<div class="actions"><button class="pill accent" data-a="backup-now">Back up now</button></div><h2>Backups</h2>`;
    if (!backups) storage += '<div class="empty">Loading backups…</div>';
    else if (backups.error) storage += `<div class="empty">Could not list backups: ${esc(backups.error)}</div>`;
    else if (!backups.backups?.length) storage += '<div class="empty">No backups yet.</div>';
    else storage += backups.backups.map((b) => `<div class="backup"><div class="meta"><div>${esc(fmtDate(b.created))}</div><div class="muted">${esc(b.key)} · ${fmtSize(b.size)}</div></div>
      <button class="pill small" data-a="restore" data-key="${esc(b.key)}">Restore</button></div>`).join("");
  } else {
    storage += '<p class="muted">Set S3_BUCKET (and credentials) in the server environment to enable backups and remote song storage.</p>';
  }
  setHTML($("v-storage"), storage + "</section>");
}

// ---------- cheap updates (every poll) ----------
function highlight() {
  for (const r of document.querySelectorAll("#view .row")) {
    const m = models.get(r.dataset.m);
    r.classList.toggle("playing", !!m && isPlaying(m));
  }
  const mode = S?.mode || {};
  const radio = mode.playlist || mode.prompt ? null : mode.shuffle ? "*shuffle" : mode.genre;
  for (const t of document.querySelectorAll("#view .tile[data-genre]")) t.classList.toggle("active", !!S?.now && t.dataset.genre === radio);
  for (const t of document.querySelectorAll("#view .tile[data-plid]")) t.classList.toggle("active", mode.playlist?.id === t.dataset.plid);
  const big = $("pl-big-play");
  if (big) {
    const mine = mode.playlist?.id === route.id && out?.cur && !wantPaused;
    big.textContent = mine ? "❚❚" : "▶";
  }
}

function renderSidebar() {
  $("liked-count").textContent = `Playlist · ${library.length} song${library.length === 1 ? "" : "s"}`;
  const playing = S?.mode?.playlist?.id;
  setHTML($("sidebar-playlists"), playlists.map((p) => `<a href="#/playlist/${enc(p.id)}" class="lib-item" data-plid="${esc(p.id)}">
      ${playlistArt("lib-art", p)}
      <span class="lib-meta"><span class="lib-name">${esc(p.name)}</span><span class="lib-sub">Playlist · ${p.count} song${p.count === 1 ? "" : "s"}</span></span>
      <span class="spk" aria-label="Playing">🔊</span></a>`).join(""));
  for (const a of $("sidebar-playlists").children) {
    a.classList.toggle("playing", a.dataset.plid === playing);
    a.classList.toggle("active", route.name === "playlist" && a.dataset.plid === route.id);
  }
}

function trackSub(t) {
  const mode = S?.mode || {};
  if (mode.playlist) {
    const pos = (t.playlist_pos ?? mode.playlist.position ?? 0) + 1;
    return `Playlist · ${mode.playlist.name} ${pos}/${mode.playlist.count}`;
  }
  if (mode.prompt && mode.mood) return `Prompt: ${mode.mood}`;
  if (t.tempo == null) return t.genre ? genreLabel(t.genre) : "AI is choosing…";
  return `${genreLabel(t.genre)} · ${Math.round(t.tempo)} bpm · ${t.key} ${scaleLabel(t.scale)}`;
}

function statusText(t) {
  if (!t) return "";
  if (t.status === "generating") return t.source && t.source !== "procedural" ? `Composing (asking ${t.source})…` : "Composing…";
  if (t.status === "rendering") return "Rendering…";
  if (t.status === "error") return t.error || "Something went wrong";
  if (!out) return "Press ▶ to listen";
  if (out.cur?.id !== t.id && out.pending?.id !== t.id) return "Loading audio…";
  return "";
}

function setStatus(text, error, busy) {
  const el = $("np-status");
  el.className = "np-status" + (error ? " error" : "");
  setHTML(el, (busy ? '<span class="spin"></span>' : "") + esc(text));
}

const REPEAT_NEXT = { off: "all", all: "one", one: "off" };
const REPEAT_TIP = { off: "Repeat: off", all: "Repeat: all (playlist / radio keeps going)", one: "Repeat: this song" };

function renderPlayer() {
  const now = S?.now;
  $("np-name").textContent = now ? (now.name || (now.genre ? genreLabel(now.genre) : "AI is choosing…")) : "Nothing playing";
  $("np-sub").textContent = now ? trackSub(now) : "Pick a radio or a playlist";
  $("np-art").style.background = now ? gradient(now.hash || now.id) : "";
  const st = statusText(now);
  const busy = !!now && (now.status === "generating" || now.status === "rendering" || st === "Loading audio…");
  setStatus(st, now?.status === "error", busy);
  $("play").textContent = out && out.cur && !wantPaused ? "❚❚" : "▶";
  const ready = !!now && now.status === "ready";
  const isLiked = !!now && (libIds.has(now.hash) || libIds.has(now.library_id));
  $("like").classList.toggle("on", isLiked);
  $("like").textContent = isLiked ? "♥" : "♡";
  $("like").disabled = !ready;
  $("add-now").disabled = !ready;
  $("download").disabled = !ready;
  $("skip").disabled = !now;
  const rep = S?.mode?.repeat || "off";
  const rb = $("repeat");
  rb.textContent = rep === "one" ? "🔂" : "🔁";
  rb.classList.toggle("on", rep !== "off");
  rb.title = REPEAT_TIP[rep];
  rb.setAttribute("aria-label", REPEAT_TIP[rep]);
  $("device").classList.toggle("on", !!sinkId);
}

function currentSection(sections, t) {
  let cur = null;
  for (const sec of sections || []) if (sec.start <= t) cur = sec;
  return cur;
}

let marksFor = null;
function renderProgress(pos) {
  if (!pos) {
    $("song-bar").style.width = "0";
    $("section").textContent = "";
    $("t-cur").textContent = "0:00";
    $("t-total").textContent = S?.now?.music_seconds ? fmtTime(S.now.music_seconds) : "0:00";
    if (marksFor !== null) { $("section-marks").innerHTML = ""; marksFor = null; }
    return;
  }
  const { t, music, track } = pos;
  $("song-bar").style.width = `${Math.min(100, (100 * t) / music)}%`;
  const sec = t < music ? currentSection(track.sections, t) : null;
  $("section").textContent = sec ? sec.kind : t >= music ? "ending" : "";
  $("t-cur").textContent = fmtTime(Math.min(t, music));
  $("t-total").textContent = fmtTime(music);
  if (marksFor !== track.id) {
    marksFor = track.id;
    $("section-marks").innerHTML = (track.sections || []).slice(1)
      .map((s) => `<span style="left:${(100 * Number(s.start)) / music}%"></span>`).join("");
  }
}

// ---------- visualiser ----------
function drawViz() {
  const c = $("viz");
  const ctx2d = c.getContext("2d");
  const w = (c.width = c.clientWidth * devicePixelRatio);
  const h = (c.height = c.clientHeight * devicePixelRatio);
  ctx2d.clearRect(0, 0, w, h);
  if (out && out.analyser && out.cur) {
    const data = new Uint8Array(out.analyser.frequencyBinCount);
    out.analyser.getByteFrequencyData(data);
    const n = data.length, bw = w / n;
    ctx2d.fillStyle = "#7ee0b5";
    for (let i = 0; i < n; i++) {
      const v = (data[i] / 255) * h * 0.9;
      ctx2d.fillRect(i * bw, h - v, bw - 1, v);
    }
  }
  requestAnimationFrame(drawViz);
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
      el.textContent = it.label;
      if (it.checked) el.classList.add("checked");
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
function ask({ title, text = "", value, ok = "OK", danger = false }) {
  const d = $("dialog");
  $("dialog-title").textContent = title;
  $("dialog-text").textContent = text;
  $("dialog-text").hidden = !text;
  const input = $("dialog-input");
  const isPrompt = value !== undefined;
  input.hidden = !isPrompt;
  input.value = isPrompt ? value : "";
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
async function act(fn) {
  wantPaused = false; // choosing music means "play"
  ensureOut();
  try {
    S = await fn();
    afterState();
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

async function togglePause() {
  ensureOut();
  if (!S?.now) return act(() => api("/random", {}));
  if (out.cur && !wantPaused) {
    wantPaused = true;
    await out.pause();
  } else {
    wantPaused = false;
    await out.resume();
    reconcile();
  }
  renderPlayer();
  highlight();
}

function playRow(m) {
  if (isPlaying(m) && out?.cur) return togglePause();
  if (m.ctx === "pl") return act(() => api(`/playlists/${enc(m.plId)}/play`, { start: m.id }));
  // Persisted history may outlive its track id; prefer the saved song when there is one.
  const id = (m.ctx === "recent" && savedId(m)) || m.id;
  return act(() => api("/play", { id }));
}

async function toggleLike(m) {
  const sid = savedId(m);
  if (!sid) {
    return mutate(() => api("/like", { id: m.id }), (r) => (r.already ? "Already in Liked songs" : `Saved “${r.entry?.name ?? m.name}”`));
  }
  const yes = await ask({ title: "Remove from Liked songs?", text: `“${m.name}” will also be removed from every playlist.`, ok: "Remove", danger: true });
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
    { label: "Download MP3", run: () => download(m.id, "mp3"), disabled: !ready },
    { label: "Download WAV", run: () => download(m.id, "wav"), disabled: !ready },
  ];
  if (sid) items.push({ sep: true }, { label: "Rename…", run: () => renameSong(sid, m.name) });
  if (m.ctx === "pl") items.push({ sep: true }, { label: "Remove from playlist", run: () => removeFromPlaylist(m) });
  if (sid) {
    if (m.ctx !== "pl") items.push({ sep: true });
    items.push({ label: "Remove from library…", danger: true, run: () => toggleLike(m) });
  }
  openMenu(anchor, items);
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
  const yes = await ask({ title: "Delete playlist?", text: `“${p?.name ?? "This playlist"}” will be deleted. The songs stay in Liked songs.`, ok: "Delete", danger: true });
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

async function sendPrompt(text) {
  if (!text) return;
  wantPaused = false;
  ensureOut();
  try {
    const r = await api("/prompt", { text });
    S = r;
    toast(r.genre ? `Playing ${genreLabel(r.genre)} radio for “${text}”` : "AI is choosing…");
    afterState();
  } catch (e) {
    toast(e.message);
  }
}

// ---------- output devices ----------
async function deviceMenu(anchor) {
  if (!OutClass.sinkSupported() || !navigator.mediaDevices?.enumerateDevices) {
    return openMenu(anchor, [{ heading: "Output device" }, { note: "This browser can't pick an output here. Switch output in system settings." }]);
  }
  let devs = [];
  try {
    devs = (await navigator.mediaDevices.enumerateDevices()).filter((d) => d.kind === "audiooutput");
  } catch { /* none */ }
  const items = [{ heading: "Output device" }];
  const unnamed = devs.some((d) => !d.label);
  items.push({ label: "System default", checked: !sinkId || sinkId === "default", run: () => pickSink("") });
  devs.filter((d) => d.deviceId && d.deviceId !== "default").forEach((d, i) => {
    items.push({ label: d.label || `Output ${i + 1}`, checked: d.deviceId === sinkId, run: () => pickSink(d.deviceId) });
  });
  if (unnamed || !devs.length) {
    items.push({ sep: true }, {
      label: "Show device names…",
      run: async () => {
        try {
          const s = await navigator.mediaDevices.getUserMedia({ audio: true });
          s.getTracks().forEach((t) => t.stop());
        } catch { toast("Permission denied: device names stay hidden"); }
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
async function saveAI() {
  const body = {
    backend: $("ai-backend").value,
    model: $("ai-model").value.trim(),
    cli_model: $("ai-cli-model").value.trim(),
    base_url: $("ai-base-url").value.trim(),
    ratio: parseFloat($("ai-ratio").value),
    timeout: parseFloat($("ai-timeout").value),
  };
  try {
    const r = await api("/settings/ai", body, "PUT");
    toast("AI settings saved");
    if (r && r.ai) settings = r;
    else await loadSettings();
    await loadGenres();
    renderView();
  } catch (e) {
    toast(e.message);
  }
}

async function testAI() {
  const res = $("ai-result");
  res.className = "result muted";
  res.textContent = "Testing… (uses the saved settings)";
  try {
    const r = await api("/settings/ai/test", {});
    const secs = r.seconds != null ? ` in ${Number(r.seconds).toFixed(1)} s` : "";
    if (r.ok) {
      const b = r.brief || {};
      const brief = [b.name, b.genre && genreLabel(b.genre), b.key && `${b.key} ${scaleLabel(b.scale || "")}`.trim(), b.tempo && `${Math.round(b.tempo)} bpm`].filter(Boolean).join(" · ");
      res.className = "result ok";
      res.textContent = `OK · ${r.source}${secs}${brief ? ` · ${brief}` : ""}`;
    } else {
      res.className = "result bad";
      res.textContent = `Failed${secs}: ${r.error || "unknown error"}`;
    }
  } catch (e) {
    res.className = "result bad";
    res.textContent = e.message;
  }
}

async function backupNow(btn) {
  btn.disabled = true;
  try {
    const { backup } = await api("/admin/backup", {});
    toast(`Backed up (${fmtSize(backup?.size)})`);
    await loadBackups();
  } catch (e) {
    toast(e.message);
  } finally {
    btn.disabled = false;
  }
}

async function restore(key) {
  const yes = await ask({ title: "Restore this backup?", text: `Your library, playlists and history will be replaced with ${key}. This can't be undone.`, ok: "Restore", danger: true });
  if (!yes) return;
  try {
    await api("/admin/restore", { key });
    toast("Backup restored");
    plDetail.clear();
    await Promise.all([loadLib(), loadHistory()]);
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
    case "row-up": moveRow(m, -1); break;
    case "row-down": moveRow(m, 1); break;
    case "radio": act(() => api("/genre", { genre: el.dataset.genre })); break;
    case "shuffle-radios": act(() => api("/random", {})); break;
    case "go": location.hash = el.dataset.href; break;
    case "pl-play": {
      const id = el.dataset.pl;
      if (S?.mode?.playlist?.id === id && out?.cur) togglePause();
      else act(() => api(`/playlists/${enc(id)}/play`, {}));
      break;
    }
    case "pl-shuffle": act(() => api(`/playlists/${enc(el.dataset.pl)}/play`, { shuffle: true })); break;
    case "pl-rename": renamePlaylist(el.dataset.pl); break;
    case "pl-delete": deletePlaylist(el.dataset.pl); break;
    case "chip": liked.genre = el.dataset.genre; fillLiked(); highlight(); break;
    case "ai-save": saveAI(); break;
    case "ai-test": testAI(); break;
    case "backup-now": backupNow(el); break;
    case "restore": restore(el.dataset.key); break;
  }
}

function toggleMenuFor(anchor, open) {
  if (menuAnchor === anchor && !$("menu").hidden) closeMenu();
  else open();
}

function bind() {
  document.addEventListener("click", onClick);
  window.addEventListener("hashchange", onRoute);
  window.addEventListener("resize", closeMenu);
  $("main").addEventListener("scroll", closeMenu, { passive: true });

  $("play").onclick = togglePause;
  $("skip").onclick = () => S?.now && act(() => api("/skip", { from_id: S.now.id }));
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
  $("device").onclick = (ev) => { ev.stopPropagation(); if (menuAnchor === $("device") && !$("menu").hidden) closeMenu(); else deviceMenu($("device")); };
  $("repeat").onclick = () => {
    const next = REPEAT_NEXT[S?.mode?.repeat || "off"];
    api("/repeat", { mode: next }).then((r) => { S = r; afterState(); toast(REPEAT_TIP[next]); }).catch((e) => toast(e.message));
  };
  $("new-playlist").onclick = () => newPlaylist(null);
  $("prompt-form").onsubmit = (ev) => {
    ev.preventDefault();
    const text = $("prompt").value.trim();
    if (text) { sendPrompt(text); $("prompt").blur(); }
  };
  $("volume").value = volume;
  $("volume").oninput = (e) => {
    volume = parseFloat(e.target.value);
    store.set("volume", volume);
    if (out) out.setVolume(volume);
  };

  // Liked view toolbar lives in the skeleton; only its results re-render.
  $("view").addEventListener("input", (ev) => {
    if (ev.target.id === "liked-q") { liked.q = ev.target.value; fillLiked(); highlight(); }
    if (ev.target.id === "ai-ratio") $("ai-ratio-val").textContent = `${Math.round(parseFloat(ev.target.value) * 100)}%`;
  });
  $("view").addEventListener("change", (ev) => {
    if (ev.target.id === "liked-sort") { liked.sort = ev.target.value; fillLiked(); highlight(); }
    if (ev.target.id === "ai-backend") $("ai-backend-hint").textContent = BACKENDS[ev.target.value] || "";
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
    else if (ev.key === "l") $("like").click();
    else if (ev.key === "/") { ev.preventDefault(); $("prompt").focus(); }
  });
}

async function init() {
  bind();
  route = parseRoute();
  for (const a of document.querySelectorAll("[data-route]")) a.classList.toggle("active", a.dataset.route === route.name);
  renderView();
  await loadGenres();
  await refresh();
  await loadLib();
  onRoute();
  setInterval(refresh, 1000);
  setInterval(tick, 100);
  setInterval(loadLib, 15000);
  requestAnimationFrame(drawViz);
}

init();
