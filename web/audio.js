"use strict";

// Shared by the site (app.js) and the overlay (overlay.js): audio output engines,
// a per-tab client id and a fetch-based Server-Sent Events reader.

// crypto.randomUUID needs a secure context; plain http on a LAN address is not one.
function clientId() {
  if (window.crypto?.randomUUID) {
    try { return crypto.randomUUID(); } catch { /* insecure context */ }
  }
  const b = crypto.getRandomValues(new Uint8Array(16));
  b[6] = (b[6] & 0x0f) | 0x40;
  b[8] = (b[8] & 0x3f) | 0x80;
  const h = [...b].map((x) => x.toString(16).padStart(2, "0")).join("");
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
}

// ---------- audio output: Web Audio (gapless hand-over) ----------
// Each song plays once. The next song starts exactly when the current song's last
// bar ends (music_seconds); the old song's reverb ring-out overlaps the new start.
// `headers` are sent with every audio fetch (the overlay sends Authorization).
class WebAudioOut {
  constructor(headers) {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    this.gapless = true;
    this.headers = headers || {};
    this.ctx = new Ctx();
    this.master = this.ctx.createGain();
    // Analyser for the visualizers: master -> analyser -> speakers (doesn't change the sound).
    this.analyser = this.ctx.createAnalyser();
    this.analyser.fftSize = 256;
    this.analyser.smoothingTimeConstant = 0.78;
    this.master.connect(this.analyser);
    this.analyser.connect(this.ctx.destination);
    this.freq = new Uint8Array(this.analyser.frequencyBinCount);
    this.loading = new Map(); // id -> Promise<AudioBuffer>
    this.ready = new Map();   // id -> AudioBuffer
    this.cur = null;          // { id, src, gain, start, music, track }
    this.pending = null;      // the next song, scheduled to start when the current one's music ends
  }

  // `n` bar levels (0..1) on a log-ish frequency scale, for drawing; zeros when silent/paused.
  levels(n) {
    this.analyser.getByteFrequencyData(this.freq);
    const out = new Array(n).fill(0);
    const max = this.freq.length * 0.72; // the top bins are near-empty for this music
    for (let i = 0; i < n; i++) {
      const a = Math.floor(Math.pow(i / n, 1.7) * max), b = Math.max(a + 1, Math.floor(Math.pow((i + 1) / n, 1.7) * max));
      let v = 0;
      for (let k = a; k < b; k++) v = Math.max(v, this.freq[k]);
      out[i] = v / 255;
    }
    return out;
  }

  static sinkSupported() {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    return !!Ctx && typeof Ctx.prototype.setSinkId === "function";
  }

  setSink(id) { return this.ctx.setSinkId(id || ""); }

  load(track) {
    if (!this.loading.has(track.id)) {
      const p = fetch(track.mp3, { headers: this.headers })
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

  // `start` is when the song's 0:00 is (or would have been) on the context clock, so
  // position = currentTime - start holds after a seek too.
  _voice(track, buf, when, offset = 0) {
    const src = this.ctx.createBufferSource();
    src.buffer = buf;
    const gain = this.ctx.createGain();
    src.connect(gain);
    gain.connect(this.master);
    src.start(when, offset);
    const music = Math.min(track.music_seconds || buf.duration, buf.duration);
    return { id: track.id, src, gain, start: when - offset, music, track };
  }

  // Jump within the current song: restart its buffer at `t` with a very short crossfade.
  // The prefetched next song is unscheduled; the caller schedules it again for the new end.
  seek(t) {
    const c = this.cur;
    if (!c) return;
    const buf = c.src.buffer;
    t = Math.max(0, Math.min(t, buf.duration - 0.05));
    this.cancelPending();
    const now = this.ctx.currentTime + 0.02;
    const g = c.gain.gain;
    g.cancelScheduledValues(now);
    g.setValueAtTime(g.value, now);
    g.linearRampToValueAtTime(0, now + 0.03);
    c.src.stop(now + 0.04);
    const v = this._voice(c.track, buf, now, t);
    v.gain.gain.setValueAtTime(0, now);
    v.gain.gain.linearRampToValueAtTime(1, now + 0.03);
    this.cur = v;
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
  get blocked() { return this.ctx.state === "suspended"; }

  // Stop everything and release the audio device (this client is no longer the speaker).
  destroy() {
    this.cancelPending();
    if (this.cur) { try { this.cur.src.stop(); } catch { /* already stopped */ } }
    this.cur = null;
    this.loading.clear();
    this.ready.clear();
    this.ctx.close().catch(() => {});
  }
}

// ---------- audio output: <audio> fallback (small gap between songs) ----------
// With extra headers the file is fetched first and played from a blob URL.
class ElementOut {
  constructor(headers) {
    this.gapless = false;
    this.headers = headers || {};
    this.el = new Audio();
    this.loading = new Map(); // id -> Promise<url>
    this.ready = new Map();   // id -> url
    this.cur = null;
    this.pending = null;
  }
  static sinkSupported() { return typeof HTMLMediaElement !== "undefined" && typeof HTMLMediaElement.prototype.setSinkId === "function"; }
  setSink(id) { return this.el.setSinkId(id || ""); }
  load(track) {
    if (!Object.keys(this.headers).length) { this.ready.set(track.id, track.mp3); return Promise.resolve(track.mp3); }
    if (!this.loading.has(track.id)) {
      const p = fetch(track.mp3, { headers: this.headers })
        .then((r) => { if (!r.ok) throw new Error(`audio ${r.status}`); return r.blob(); })
        .then((b) => { const u = URL.createObjectURL(b); this.ready.set(track.id, u); return u; });
      p.catch(() => this.loading.delete(track.id));
      this.loading.set(track.id, p);
      this._trim(track.id);
    }
    return this.loading.get(track.id);
  }
  _trim(keepId) {
    const keep = new Set([keepId, this.cur?.id]);
    for (const id of [...this.loading.keys()]) {
      if (this.loading.size <= 3) break;
      if (keep.has(id)) continue;
      const u = this.ready.get(id);
      if (u && u.startsWith("blob:")) URL.revokeObjectURL(u);
      this.loading.delete(id);
      this.ready.delete(id);
    }
  }
  buffer(id) { return this.ready.get(id) || null; }
  playNow(track, url) {
    this.el.src = url;
    this.el.play().catch(() => {});
    this.cur = { id: track.id, music: track.music_seconds, start: 0, track };
  }
  promote() { return false; }
  cancelPending() {}
  seek(t) { if (this.cur) this.el.currentTime = Math.max(0, t); }
  position() {
    if (!this.cur) return null;
    return { t: this.el.currentTime, music: this.cur.music, track: this.cur.track };
  }
  setVolume(v) { this.el.volume = v; }
  pause() { this.el.pause(); return Promise.resolve(); }
  resume() { return this.el.play(); }
  get paused() { return this.el.paused; }
  get blocked() { return false; }
  destroy() {
    this.el.pause();
    this.el.removeAttribute("src");
    this.el.load();
    for (const u of this.ready.values()) if (u.startsWith("blob:")) URL.revokeObjectURL(u);
    this.cur = null;
    this.loading.clear();
    this.ready.clear();
  }
}

const OutClass = (window.AudioContext || window.webkitAudioContext) ? WebAudioOut : ElementOut;

// ---------- Server-Sent Events over fetch ----------
// fetch (not EventSource) so an Authorization header can be sent. Reconnects with
// backoff; a stream silent for 45 s (the server pings every 15 s) counts as dead.
// onEvent(type, data, id): data is parsed JSON when possible, else the raw string.
// onStatus(status, info): "connecting" | "open" | "down" ({status?: http code, error?}).
function streamEvents(url, headers, onEvent, onStatus = () => {}) {
  let stopped = false, ctrl = null, timer = null, retry = 0;
  const IDLE_MS = 45000;

  async function connect() {
    if (stopped) return;
    ctrl = new AbortController();
    let idle = setTimeout(() => ctrl.abort(), IDLE_MS);
    const alive = () => { clearTimeout(idle); idle = setTimeout(() => ctrl.abort(), IDLE_MS); };
    let httpStatus = 0;
    onStatus("connecting", {});
    try {
      const r = await fetch(url, { headers: { Accept: "text/event-stream", ...(headers || {}) }, cache: "no-store", signal: ctrl.signal });
      httpStatus = r.status;
      if (!r.ok || !r.body) throw new Error(`${r.status} ${r.statusText}`.trim());
      retry = 0;
      onStatus("open", { status: r.status });
      const reader = r.body.getReader();
      const dec = new TextDecoder();
      let buf = "", type = "", data = [], id = null;
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        alive();
        buf += dec.decode(value, { stream: true });
        const lines = buf.split(/\r\n|\r|\n/);
        buf = lines.pop();
        for (const line of lines) {
          if (line === "") {
            if (data.length) {
              const raw = data.join("\n");
              let parsed = raw;
              try { parsed = JSON.parse(raw); } catch { /* plain text */ }
              try { onEvent(type || "message", parsed, id); } catch (e) { console.error(e); }
            }
            type = ""; data = [];
            continue;
          }
          if (line.startsWith(":")) continue; // comment / keep-alive
          const i = line.indexOf(":");
          const field = i < 0 ? line : line.slice(0, i);
          let val = i < 0 ? "" : line.slice(i + 1);
          if (val.startsWith(" ")) val = val.slice(1);
          if (field === "event") type = val;
          else if (field === "data") data.push(val);
          else if (field === "id") id = val;
        }
      }
      onStatus("down", { status: httpStatus, error: "stream ended" });
    } catch (e) {
      if (stopped) return;
      onStatus("down", { status: httpStatus, error: e.message });
    } finally {
      clearTimeout(idle);
    }
    if (stopped) return;
    // Bad keys won't fix themselves quickly: retry them slowly.
    const max = httpStatus === 401 || httpStatus === 403 ? 30000 : 15000;
    const wait = Math.min(max, 1000 * 2 ** retry++) + Math.random() * 400;
    timer = setTimeout(connect, wait);
  }

  connect();
  return { close() { stopped = true; clearTimeout(timer); ctrl?.abort(); } };
}
