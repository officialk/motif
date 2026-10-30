"use strict";

// Motif overlay for OBS / a browser tab. Shows what plays and, with audio=1, is the speaker:
// it plays the songs (gapless, like the site), runs remote commands and reports playback.

const $ = (id) => document.getElementById(id);
const fmtTime = (s) => `${Math.floor((s || 0) / 60)}:${String(Math.floor((s || 0) % 60)).padStart(2, "0")}`;
const genreLabel = (g) => { const s = String(g ?? "").replace(/[-_]/g, " "); return s.charAt(0).toUpperCase() + s.slice(1); };

// Same deterministic gradient as the site (app.js).
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

// ---------- options ----------
const Q = new URLSearchParams(location.search);
const pick = (v, list, d) => (list.includes(v) ? v : d);
const OPT = {
  layout: pick(Q.get("layout"), ["card", "bar", "mini"], "card"),
  pos: pick(Q.get("pos"), ["bl", "br", "tl", "tr"], "bl"),
  progress: Q.get("progress") !== "0",
  audio: Q.get("audio") !== "0",
  name: (Q.get("name") || "").trim().slice(0, 40), // label in the site's "Play on" menu
  viz: ["1", "on", "bars"].includes(Q.get("viz")), // live audio bars inside the art
};

// The key comes in the fragment (never sent to the server); hide it from the address bar but keep it
// for this tab's session, so reloads (OBS refreshes sources) still have it. An overlay needs a key:
// without one it doesn't connect, so revoking the key always stops it.
const KEY_STORE = "motif-overlay-key";
const KEY = (() => {
  const given = (Q.get("key") || new URLSearchParams(location.hash.replace(/^#/, "")).get("key") || "").trim();
  try {
    if (given) sessionStorage.setItem(KEY_STORE, given);
    return given || sessionStorage.getItem(KEY_STORE) || "";
  } catch {
    return given;
  }
})();
if (Q.has("key") || location.hash) {
  const clean = new URLSearchParams(location.search);
  clean.delete("key");
  history.replaceState(null, "", location.pathname + (clean.toString() ? `?${clean}` : ""));
}
const AUTH = KEY ? { Authorization: `Bearer ${KEY}` } : {};
const CLIENT = clientId();

class HttpError extends Error {
  constructor(status, msg) { super(msg); this.status = status; }
}

async function api(path, body) {
  const opts = { headers: { ...AUTH } };
  if (body !== undefined) {
    opts.method = "POST";
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const r = await fetch(path, opts);
  if (!r.ok) {
    let msg = `${r.status} ${r.statusText}`.trim();
    try { const j = await r.json(); if (typeof j.detail === "string") msg = j.detail; } catch { /* not json */ }
    if (r.status === 401) keyRejected();
    throw new HttpError(r.status, msg);
  }
  return r.json();
}

// ---------- state ----------
let S = null;
let out = null;
let wantPaused = false;
let awaitingId = null;
let audioFail = { id: null, at: 0 };
let volume = 1;
try { const v = parseFloat(localStorage.getItem("motif-overlay-volume")); if (v >= 0 && v <= 1) volume = v; } catch { /* no storage */ }
let problem = KEY ? null : "nokey"; // null | "nokey" | "auth" | "down" | "scope" | "scope-read"

// The key was revoked or is wrong: stop playing and show it, instead of the last song.
function keyRejected() {
  problem = "auth";
  if (out) stopAudio();
  S = null;
  render();
}
let everOpen = false;
const live = { version: null, speaker: null, playback: null, offset: 0, lastSeq: null, claiming: 0, claimSince: 0 };
let reportedId = null, lastReportAt = 0, reportSoon = null;

const isSpeaker = () => !!live.speaker && live.speaker.client === CLIENT;
const serverNow = () => Date.now() / 1000 + live.offset;
const remotePlayback = () => (live.playback && live.speaker && live.playback.client === live.speaker.client ? live.playback : null);

// ---------- speaker ----------
async function claim() {
  live.claiming++;
  try {
    const r = await api("/speaker/claim", { client: CLIENT, kind: "overlay" });
    live.speaker = r.speaker || null;
    if (isSpeaker()) {
      live.claimSince = r.speaker.since || serverNow();
      reportedId = null;
      if (problem === "scope") problem = null;
      ensureOut();
      reconcile();
    }
  } catch (e) {
    if (e.status === 403) problem = "scope"; // key can't be the speaker: display only
  } finally {
    live.claiming--;
  }
  applyRole();
  render();
}

function applyRole() {
  if (!live.claiming && !isSpeaker() && out) stopAudio();
  // Handed the speaker role again (e.g. picked in the site's "Play on" menu): start playing.
  if (isSpeaker() && OPT.audio && !out) ensureOut();
}

function ensureOut() {
  if (!out) {
    out = new OutClass(AUTH);
    out.setVolume(volume);
    if (out.ctx) out.ctx.onstatechange = () => report();
  }
  if (!wantPaused && out.ctx?.state === "suspended") out.ctx.resume().catch(() => {});
  return out;
}

function stopAudio() {
  if (!out) return;
  if (out.ctx) out.ctx.onstatechange = null;
  out.destroy();
  out = null;
  awaitingId = null;
  reportedId = null;
}

// OBS plays audio without a gesture. A normal browser tab blocks it until the page is
// clicked once; any click or key on the page quietly unlocks it (no button is shown).
function unlockAudio() {
  if (!out || wantPaused || !out.blocked) return;
  (out.ctx ? out.ctx.resume() : out.resume()).catch(() => {}).finally(() => { reconcile(); report(); });
}

function reconcile() {
  if (!out || !S) return;
  const now = S.now, next = S.up_next;
  if (next && next.status === "ready") out.load(next).catch(() => {});
  if (!now || now.status !== "ready") return;
  if (out.cur?.id === now.id || out.pending?.id === now.id || awaitingId) return;
  const buf = out.buffer(now.id);
  if (buf) { out.playNow(now, buf); return; }
  if (audioFail.id === now.id && Date.now() - audioFail.at < 5000) return;
  out.load(now).then(() => reconcile()).catch(() => { audioFail = { id: now.id, at: Date.now() }; });
}

async function confirmAdvance(fromId, newId) {
  awaitingId = newId;
  try {
    S = await api("/skip", { from_id: fromId });
  } catch { /* the next event brings the truth */ }
  awaitingId = null;
  render();
  reconcile();
}

// Gapless hand-over at the current song's music end, then POST /skip {from_id}.
function handOver() {
  if (!out || !out.cur) return;
  if (out.promote()) return;
  if (isSpeaker() && (out.cur.id !== reportedId || (!wantPaused && Date.now() - lastReportAt > 10000))) report();
  if (!S || !S.now || S.now.id !== out.cur.id || out.pending || awaitingId) return;
  const next = S.up_next;
  const buf = next && next.status === "ready" ? out.buffer(next.id) : null;
  if (!buf) return;
  const fromId = out.cur.id;
  if (out.gapless) {
    const t = out.now();
    const end = out.musicEnd();
    if (end - t > 2) return;
    out.scheduleAt(next, buf, Math.max(end, t + 0.05));
    const o = out;
    const wait = setInterval(() => {
      if (out !== o) clearInterval(wait);
      else if (o.cur?.id === next.id) { clearInterval(wait); confirmAdvance(fromId, next.id); }
      else if (o.pending?.id !== next.id) clearInterval(wait);
    }, 50);
  } else {
    const pos = out.position();
    if (pos.t >= pos.music) { out.playNow(next, buf); confirmAdvance(fromId, next.id); }
  }
}

function report() {
  clearTimeout(reportSoon);
  if (!out?.cur || !isSpeaker()) return;
  const pos = out.position();
  reportedId = out.cur.id;
  lastReportAt = Date.now();
  api("/playback", {
    client: CLIENT, track_id: out.cur.id, state: wantPaused || out.paused ? "paused" : "playing",
    position: pos ? Math.min(pos.t, pos.music) : 0, volume,
  }).catch(() => {});
}

async function setPaused(paused) {
  if (!out) return;
  wantPaused = paused;
  try { await (paused ? out.pause() : out.resume()); } catch { /* blocked */ }
  if (!paused) reconcile();
  report();
  render();
}

function seekTo(t) {
  if (!out?.cur) return;
  out.seek(Math.max(0, Math.min(t, out.cur.music)));
  report();
}

function setVolume(v) {
  volume = Math.max(0, Math.min(1, v));
  try { localStorage.setItem("motif-overlay-volume", String(volume)); } catch { /* no storage */ }
  if (out) out.setVolume(volume);
  clearTimeout(reportSoon);
  reportSoon = setTimeout(report, 300);
}

function runCommand(c) {
  if (!out) return;
  switch (c.action) {
    case "pause": if (!wantPaused) setPaused(true); break;
    case "resume": if (wantPaused || out.paused) setPaused(false); break;
    case "toggle": setPaused(!!(out.cur && !wantPaused)); break; // playing -> pause, paused -> play
    case "seek": if (typeof c.t === "number") seekTo(c.t); break;
    case "volume": if (typeof c.volume === "number") setVolume(c.volume); break;
  }
}

// ---------- live stream ----------
function onLive(type, d) {
  if (type !== "state" || !d || typeof d !== "object") return;
  if (live.version !== null && typeof d.version === "number" && d.version <= live.version) return;
  if (typeof d.version === "number") live.version = d.version;
  if (typeof d.server_time === "number") live.offset = d.server_time - Date.now() / 1000;
  const sp = d.speaker || null;
  const stale = !!out && !!sp && sp.client !== CLIENT && (sp.since || 0) < live.claimSince;
  if (!live.claiming && !stale) live.speaker = sp;
  live.playback = d.playback || null;
  const seq = d.command && typeof d.command.seq === "number" ? d.command.seq : 0;
  if (live.lastSeq === null) live.lastSeq = seq; // first event on this connection: don't replay
  else if (seq > live.lastSeq) {
    live.lastSeq = seq;
    if (isSpeaker()) runCommand(d.command);
  }
  if (problem === "auth" || problem === "down") problem = null;
  applyRole();
  if (d.state) S = d.state;
  render();
  reconcile();
}

function onStatus(status, info) {
  if (status === "open") {
    everOpen = true;
    live.version = null;
    live.lastSeq = null;
    if (problem !== "scope") problem = null;
    // The server released us when the old stream closed; an overlay always takes the speaker back.
    if (OPT.audio) claim();
  } else if (status === "down") {
    if (info?.status === 401) return keyRejected();
    problem = info?.status === 403 ? "scope-read" : "down";
  }
  render();
}

// ---------- display ----------
function lineText(now) {
  const mode = S?.mode || {};
  if (mode.playlist) return `Playlist · ${mode.playlist.name}`;
  if (mode.shuffle) return `Shuffle radio · ${genreLabel(now.genre)}`;
  if (mode.genre) return `Radio · ${genreLabel(mode.genre)}`;
  return genreLabel(now.genre);
}

function remotePos() {
  const pb = remotePlayback();
  if (!pb) return null;
  const track = [S?.now, S?.up_next].find((x) => x && x.id === pb.track_id);
  const music = track?.music_seconds;
  if (!music) return null;
  const since = pb.state === "playing" && typeof pb.at === "number" ? Math.max(0, serverNow() - pb.at) : 0;
  return { t: Math.max(0, (pb.position || 0) + since), music, track };
}

function position() {
  if (out?.cur) return out.position();
  return remotePos();
}

function isPaused() {
  if (out?.cur) return wantPaused || out.paused;
  const pb = remotePlayback();
  return !!pb && pb.state === "paused";
}

let shownId, swapTimer = null;
function render() {
  const ov = $("ov");
  const now = S?.now || null;
  const badge = problem === "nokey" ? "Overlay key required"
    : problem === "auth" ? "Invalid overlay key"
    : problem === "scope-read" ? "Key needs the read scope"
    : problem === "down" ? (everOpen ? "Reconnecting…" : "Connecting…")
    : problem === "scope" && OPT.audio ? "Display only · key can't play audio" : "";
  $("badge").hidden = !badge;
  $("badge").textContent = badge;
  const bad = problem === "auth" || problem === "nokey";
  $("badge").className = `ov-badge${bad ? " bad" : ""}`;
  ov.classList.toggle("idle", !now && !bad);
  ov.classList.toggle("error", bad);
  ov.classList.toggle("paused", !!now && isPaused());
  ov.classList.toggle("playing", !!now && !isPaused() && !!position());
  const id = now ? now.id : null;
  if (id === shownId) { fillNow(now); return; }
  // Song change: fade out, swap, fade in.
  const first = shownId === undefined;
  shownId = id;
  clearTimeout(swapTimer);
  if (first) { fillNow(now); return; }
  $("card").classList.add("swap");
  swapTimer = setTimeout(() => {
    fillNow(S?.now || null);
    $("card").classList.remove("swap");
  }, 260);
}

function fillNow(now) {
  if (problem === "nokey") {
    $("name").textContent = "Overlay key required";
    $("line").textContent = "Use the overlay URL from Motif settings";
  } else if (problem === "auth" && !now) {
    $("name").textContent = "Invalid overlay key";
    $("line").textContent = "Create a key in Motif settings";
  } else if (!now) {
    $("name").textContent = "Nothing playing";
    $("line").textContent = "Motif";
  } else {
    $("name").textContent = now.name || (now.genre ? genreLabel(now.genre) : "AI is choosing…");
    $("line").textContent = lineText(now);
  }
  $("art").style.background = now ? gradient(now.hash || now.id) : "";
  const req = now && typeof now.requested_by === "string" ? now.requested_by : "";
  $("req").hidden = !req;
  $("req").textContent = req ? `Requested by ${req}` : "";
}

function renderProgress() {
  if (!OPT.progress) return;
  const pos = position();
  const now = S?.now;
  const music = pos?.music || now?.music_seconds || 0;
  const t = pos ? Math.min(pos.t, music) : 0;
  $("fill").style.width = music ? `${(100 * t) / music}%` : "0";
  $("t-cur").textContent = fmtTime(t);
  $("t-total").textContent = fmtTime(music);
}

function frame() {
  handOver();
  renderProgress();
}

// Live bars drawn from the audio this overlay plays (only the speaker has audio to analyse).
const VIZ_BARS = 12;
let vizLevels = new Array(VIZ_BARS).fill(0);
function drawViz() {
  requestAnimationFrame(drawViz);
  const c = $("viz");
  const w = c.clientWidth, h = c.clientHeight;
  if (!w || !h) return;
  const dpr = window.devicePixelRatio || 1;
  if (c.width !== Math.round(w * dpr)) { c.width = Math.round(w * dpr); c.height = Math.round(h * dpr); }
  const g = c.getContext("2d");
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, w, h);
  const live = out && out.analyser && out.cur && !wantPaused && !isPaused();
  const target = live ? out.levels(VIZ_BARS) : new Array(VIZ_BARS).fill(0);
  // ease towards the target so bars fall smoothly when the music pauses
  vizLevels = vizLevels.map((v, i) => v + (target[i] - v) * (target[i] > v ? 0.6 : 0.18));
  if (vizLevels.every((v) => v < 0.01)) return;
  g.fillStyle = "rgba(10, 12, 16, .28)";
  g.fillRect(0, 0, w, h);
  // Bars centred on the middle line, growing up and down like an audio clip waveform.
  const pad = w * 0.12, gap = 2, bw = (w - pad * 2 - gap * (VIZ_BARS - 1)) / VIZ_BARS, mid = h / 2;
  for (let i = 0; i < VIZ_BARS; i++) {
    const bh = Math.max(2, vizLevels[i] * (h - pad * 2));
    const x = pad + i * (bw + gap);
    g.fillStyle = `rgba(255, 255, 255, ${0.55 + vizLevels[i] * 0.45})`;
    g.beginPath();
    g.roundRect ? g.roundRect(x, mid - bh / 2, bw, bh, Math.min(2, bw / 2)) : g.rect(x, mid - bh / 2, bw, bh);
    g.fill();
  }
}

// ---------- start ----------
function init() {
  const ov = $("ov");
  ov.className = `ov layout-${OPT.layout} pos-${OPT.pos} idle`;
  if (!OPT.progress) $("progress").hidden = true;
  if (OPT.viz) { ov.classList.add("viz"); $("viz").hidden = false; requestAnimationFrame(drawViz); }
  for (const el of document.querySelectorAll("[data-icon]")) el.innerHTML = icon(el.dataset.icon);
  document.addEventListener("pointerdown", unlockAudio);
  document.addEventListener("keydown", unlockAudio);
  render();
  const q = new URLSearchParams({ client: CLIENT, kind: "overlay", audio: OPT.audio ? "1" : "0" });
  if (OPT.name) q.set("name", OPT.name);
  if (KEY) streamEvents(`/events?${q}`, AUTH, onLive, onStatus);
  setInterval(frame, 100);
}

init();
