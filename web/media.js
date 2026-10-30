"use strict";

// ---------- system media controls and background playback ----------
// Lock screen, notification shade, media keys, headphones and watches talk to the page
// through the Media Session API. Browsers attach it to a playing media element only:
// - phones/tablets play through ElementOut's <audio> (SiteOut in audio.js), which also keeps
//   playing and moving on to the next song while locked;
// - desktops keep gapless Web Audio and play a looping, inaudible <audio> "anchor" alongside
//   it so the browser has an element to hang the session on. (Routing Web Audio into an element
//   via a MediaStream would not do: Chrome gives stream-backed elements no media controls.)
// Every handler maps onto the player's own actions, so a remote tab steers the speaker.
// Uses app.js globals (S, out, live, wantPaused, …); loaded after it.

if (navigator.audioSession) {
  try { navigator.audioSession.type = "playback"; } catch { /* not settable here */ }
}

const MS = "mediaSession" in navigator ? navigator.mediaSession : null;
let msMeta = { key: null, art: null };    // last metadata set
let msPos = null;                         // {id, playing, t, at} last position state set
let msPrevOn = null;                      // previoustrack handler currently installed?
let msAnchor = null;                      // desktop: the inaudible element the session attaches to

// The song's gradient cover (same colours as gradient() in app.js) as a 512 px PNG.
function coverPng(key) {
  const size = 512;
  const c = document.createElement("canvas");
  c.width = c.height = size;
  const g = c.getContext("2d");
  const h = hashNum(key);
  const a = h % 360, b = (a + 40 + ((h >>> 9) % 120)) % 360, ang = ((h >>> 17) % 360) * Math.PI / 180;
  // CSS angles: 0deg points up, clockwise; the line spans the box's projection on that direction.
  const dx = Math.sin(ang), dy = -Math.cos(ang);
  const half = (size / 2) * (Math.abs(dx) + Math.abs(dy));
  const grad = g.createLinearGradient(size / 2 - dx * half, size / 2 - dy * half, size / 2 + dx * half, size / 2 + dy * half);
  grad.addColorStop(0, `hsl(${a}, 62%, 52%)`);
  grad.addColorStop(1, `hsl(${b}, 58%, 28%)`);
  g.fillStyle = grad;
  g.fillRect(0, 0, size, size);
  return c.toDataURL("image/png");
}

// The song the system controls describe: what this tab plays, else the server's "now".
function msTrack() {
  return out?.cur?.track || S?.now || null;
}

function msAlbum() {
  const m = S?.mode || {};
  if (m.playlist?.name) return m.playlist.name;
  if (m.shuffle) return "Shuffle radio";
  if (m.genre) return `${genreLabel(m.genre)} radio`;
  return "Motif";
}

function msSetHandler(action, fn) {
  try { MS.setActionHandler(action, fn); } catch { /* action not supported by this browser */ }
}

// Play/pause as asked (not a toggle): repeated presses from a watch must not flip-flop.
function msPlay(paused) {
  if (isRemote()) return control({ action: paused ? "pause" : "resume" });
  if (playingNow() === !paused) return mediaSync();
  if (paused && !out) return;
  togglePause();
}

function msInstall() {
  if (!MS) return;
  msSetHandler("play", () => msPlay(false));
  msSetHandler("pause", () => msPlay(true));
  msSetHandler("stop", () => msPlay(true));
  msSetHandler("nexttrack", () => { if (S?.now) act(() => api("/skip", { from_id: S.now.id, manual: true })); });
  msSetHandler("seekto", (d) => { if (typeof d?.seekTime === "number") seekTo(d.seekTime); });
  // No ±10 s buttons: iOS would show them instead of previous/next.
  msSetHandler("seekbackward", null);
  msSetHandler("seekforward", null);
}

// Radios and Shuffle+ have no previous: the control disappears there, like the app's button.
function msPrevious() {
  const mp = S?.mode?.playlist;
  const on = !!(S?.now && mp && !mp.plus);
  if (on === msPrevOn) return;
  msPrevOn = on;
  msSetHandler("previoustrack", on ? () => previous() : null);
}

function msPosition(force) {
  if (!MS?.setPositionState) return;
  const pos = progressPos();
  const playing = playingNow();
  if (!pos || !(pos.music > 0) || !Number.isFinite(pos.music)) {
    if (msPos) { msPos = null; try { MS.setPositionState(); } catch { /* ignore */ } }
    return;
  }
  const t = Math.max(0, Math.min(pos.t, pos.music));
  const now = Date.now() / 1000;
  if (!force && msPos && msPos.id === pos.track.id && msPos.playing === playing) {
    const expected = msPos.t + (playing ? now - msPos.at : 0);
    if (Math.abs(expected - t) < 1.5) return; // the system extrapolates in between
  }
  msPos = { id: pos.track.id, playing, t, at: now };
  try { MS.setPositionState({ duration: pos.music, position: t, playbackRate: 1 }); } catch { /* ignore */ }
}

// Desktop: keep the anchor element playing exactly while the music plays.
function msAnchorSync() {
  if (!msAnchor) return;
  const on = !!(out && out.cur && !out.paused && !wantPaused && out instanceof WebAudioOut);
  if (on && msAnchor.paused) msAnchor.play().catch(() => {});
  else if (!on && !msAnchor.paused) msAnchor.pause();
}

// Called by renderPlayer() and every second: metadata, state, position, handlers.
function mediaSync() {
  msAnchorSync();
  if (!MS) return;
  const t = msTrack();
  const key = t ? t.id : null;
  if (key !== msMeta.key) {
    msMeta.key = key;
    msPos = null;
  }
  if (t) {
    const title = t.name || (t.genre ? genreLabel(t.genre) : "Motif");
    const artist = t.genre ? `${genreLabel(t.genre)} · Motif` : "Motif";
    const album = msAlbum();
    const artKey = t.hash || t.id;
    const sig = `${title}|${artist}|${album}|${artKey}`;
    if (sig !== msMeta.sig) {
      msMeta.sig = sig;
      if (msMeta.artKey !== artKey) { msMeta.artKey = artKey; msMeta.art = coverPng(artKey); }
      try {
        MS.metadata = new MediaMetadata({ title, artist, album, artwork: [{ src: msMeta.art, sizes: "512x512", type: "image/png" }] });
      } catch { /* ignore */ }
    }
  } else if (msMeta.sig) {
    msMeta.sig = null;
    MS.metadata = null;
  }
  MS.playbackState = !t || !hasPlayback() ? "none" : playingNow() ? "playing" : "paused";
  msPrevious();
  msPosition(false);
}

// ensureOut() hands every new speaker output here.
function mediaAttach(o) {
  if (o instanceof ElementOut) {
    // Timers are frozen on a locked phone; the element's own events drive the hand-over.
    o.onTime = () => tick();
    o.onPausedOutside = () => { // headphones unplugged, a call, …: follow, and tell remotes
      if (out !== o || wantPaused) return;
      wantPaused = true;
      report();
      renderPlayer();
      highlight();
    };
    return;
  }
  if (!msAnchor) {
    msAnchor = new Audio(quietLoop());
    msAnchor.loop = true;
  }
  msAnchor.play().catch(() => {}); // inside the click: later starts need no gesture
  queueMicrotask(msAnchorSync);
}

// 6 s of near-silent noise (about -80 dBFS) as a WAV blob: long enough for browsers to treat
// it as media with controls, not silence-detected as inaudible, and never heard.
function quietLoop() {
  const rate = 8000, n = rate * 6, b = new ArrayBuffer(44 + n * 2), v = new DataView(b);
  const str = (o, s) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); };
  str(0, "RIFF"); v.setUint32(4, 36 + n * 2, true); str(8, "WAVEfmt ");
  v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
  v.setUint32(24, rate, true); v.setUint32(28, rate * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true);
  str(36, "data"); v.setUint32(40, n * 2, true);
  for (let i = 0; i < n; i++) v.setInt16(44 + i * 2, Math.round((Math.random() * 2 - 1) * 3), true);
  return URL.createObjectURL(new Blob([b], { type: "audio/wav" }));
}

// Back from the lock screen / a hidden tab / offline: the stream may have died meanwhile and
// the shown state may be old. Reconnect at once and resync from /state.
let msWokeAt = 0;
function mediaWake(ev) {
  if (!booted || locked || (ev?.type === "visibilitychange" && document.visibilityState === "hidden")) return;
  if (Date.now() - msWokeAt < 1000) return;
  msWokeAt = Date.now();
  streamH?.kick?.();
  refresh();
}
document.addEventListener("visibilitychange", mediaWake);
window.addEventListener("pageshow", mediaWake);
window.addEventListener("online", mediaWake);
window.addEventListener("focus", mediaWake);

msInstall();
setInterval(() => { if (booted && !locked) mediaSync(); }, 1000);
