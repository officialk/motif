"use strict";

// Phone layout (<= 860px): a mini player above the tab bar and a full-screen Now Playing sheet.
// They only mirror what the player bar shows and forward taps to its buttons, so playback,
// live sync and keyboard shortcuts keep a single code path. Loaded after app.js.
(() => {
  const mini = $("mini"), sheet = $("sheet");
  const phone = window.matchMedia("(max-width: 860px)");
  let dragSeek = null; // fraction while a finger is on the sheet's seek bar
  let closing = 0;

  const copyState = (to, from) => {
    to.innerHTML = from.innerHTML;
    to.disabled = from.disabled;
    to.classList.toggle("on", from.classList.contains("on"));
    to.classList.toggle("gone", from.classList.contains("gone"));
    to.setAttribute("aria-label", from.getAttribute("aria-label") || "");
    if (from.hasAttribute("aria-pressed")) to.setAttribute("aria-pressed", from.getAttribute("aria-pressed"));
  };

  function render() {
    const now = S?.now;
    const show = !!now || (typeof hasPlayback === "function" && hasPlayback());
    mini.hidden = !show;
    document.documentElement.classList.toggle("has-mini", show);
    if (!show && !sheet.hidden) closeSheet(true);
    const name = $("np-name").textContent, sub = $("np-sub").textContent, art = $("np-art").style.background;
    $("mini-name").textContent = name;
    $("mini-sub").textContent = sub;
    $("mini-art").style.background = art;
    copyState($("mini-like"), $("like"));
    copyState($("mini-play"), $("play"));
    if (sheet.hidden) return;
    $("sheet-name").textContent = name;
    $("sheet-sub").textContent = sub;
    $("sheet-art").style.background = art;
    sheet.style.setProperty("--tint", art || "none");
    copyState($("sheet-like"), $("like"));
    copyState($("sheet-play"), $("play"));
    copyState($("sheet-prev"), $("prev"));
    copyState($("sheet-skip"), $("skip"));
    copyState($("sheet-shuffle"), $("shuffle"));
    copyState($("sheet-repeat"), $("repeat"));
    copyState($("sheet-download"), $("download"));
    $("sheet-add").disabled = $("add-now").disabled;
    $("sheet-download").hidden = $("download").hidden;
    $("sheet-queue").hidden = $("queue-link").hidden;
    $("sheet-device").className = $("device").className;
    $("sheet-device").setAttribute("aria-label", $("device").getAttribute("aria-label") || "Output device");
    $("sheet-device-ico").innerHTML = $("device-ico").innerHTML;
    $("sheet-device-label").textContent = $("device-label").textContent;
    $("sheet-device-label").hidden = $("device-label").hidden;
  }

  function renderBar() {
    const w = $("song-bar").style.width || "0%";
    $("mini-bar-fill").style.width = w;
    if (sheet.hidden) return;
    $("sheet-total").textContent = $("t-total").textContent;
    $("sheet-seek").setAttribute("aria-valuemax", $("seek").getAttribute("aria-valuemax"));
    $("sheet-seek").setAttribute("aria-valuenow", $("seek").getAttribute("aria-valuenow"));
    if (dragSeek !== null) return;
    $("sheet-fill").style.width = w;
    $("sheet-knob").style.left = w;
    $("sheet-cur").textContent = $("t-cur").textContent;
  }

  // Follow the player bar's own render functions without touching them.
  const wrap = (name, after) => {
    const orig = window[name];
    if (typeof orig !== "function") return;
    window[name] = function (...args) { const r = orig.apply(this, args); after(); return r; };
  };
  wrap("renderPlayer", () => { render(); renderBar(); });
  wrap("renderProgress", renderBar);

  // ---------- sheet open / close ----------
  function openSheet() {
    if (!phone.matches || mini.hidden) return;
    clearTimeout(closing);
    sheet.hidden = false;
    sheet.style.transform = "";
    render();
    renderBar();
    void sheet.offsetWidth;
    sheet.classList.add("open");
    document.documentElement.classList.add("sheet-open");
    $("sheet-close").focus({ preventScroll: true, focusVisible: false });
  }
  function closeSheet(now) {
    sheet.classList.remove("open");
    sheet.style.transform = "";
    document.documentElement.classList.remove("sheet-open");
    clearTimeout(closing);
    if (now) { sheet.hidden = true; return; }
    closing = setTimeout(() => { sheet.hidden = true; $("mini-open").focus({ preventScroll: true, focusVisible: false }); }, 260);
  }

  $("mini-open").onclick = openSheet;
  $("sheet-close").onclick = () => closeSheet();
  $("mini-like").onclick = () => $("like").click();
  $("mini-play").onclick = () => $("play").click();
  $("sheet-like").onclick = () => $("like").click();
  $("sheet-play").onclick = () => $("play").click();
  $("sheet-prev").onclick = () => $("prev").click();
  $("sheet-skip").onclick = () => $("skip").click();
  $("sheet-shuffle").onclick = () => $("shuffle").click();
  $("sheet-repeat").onclick = () => $("repeat").click();
  $("sheet-queue").onclick = () => closeSheet(true);
  $("sheet-add").onclick = (ev) => {
    ev.stopPropagation();
    const b = $("sheet-add");
    if (S?.now) toggleMenuFor(b, () => addMenu(b, trackRow(S.now, "now")));
  };
  $("sheet-download").onclick = (ev) => {
    ev.stopPropagation();
    const b = $("sheet-download"), now = S?.now;
    if (!now) return;
    toggleMenuFor(b, () => openMenu(b, [
      { heading: "Download" },
      { label: "MP3", run: () => download(now.id, "mp3") },
      { label: "WAV (lossless, slower)", run: () => download(now.id, "wav") },
    ]));
  };
  $("sheet-device").onclick = (ev) => {
    ev.stopPropagation();
    const b = $("sheet-device");
    if (menuAnchor === b && !$("menu").hidden) closeMenu(); else deviceMenu(b);
  };
  window.addEventListener("hashchange", () => { if (!sheet.hidden) closeSheet(true); });
  phone.addEventListener("change", () => { if (!phone.matches && !sheet.hidden) closeSheet(true); });
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape" && !sheet.hidden && $("menu").hidden) closeSheet();
  });

  // ---------- seek bar (same math as the player bar's) ----------
  const seek = $("sheet-seek");
  const frac = (ev) => {
    const r = seek.getBoundingClientRect();
    return Math.max(0, Math.min(1, (ev.clientX - r.left) / r.width));
  };
  const showDrag = () => {
    const pos = progressPos();
    if (!pos) return;
    $("sheet-fill").style.width = `${dragSeek * 100}%`;
    $("sheet-knob").style.left = `${dragSeek * 100}%`;
    $("sheet-cur").textContent = fmtTime(dragSeek * pos.music);
  };
  seek.onpointerdown = (ev) => {
    if (!progressPos() || (ev.pointerType === "mouse" && ev.button !== 0)) return;
    ev.preventDefault();
    seek.setPointerCapture(ev.pointerId);
    seek.classList.add("dragging");
    dragSeek = frac(ev);
    showDrag();
  };
  seek.onpointermove = (ev) => { if (dragSeek !== null) { dragSeek = frac(ev); showDrag(); } };
  const endSeek = (ev, apply) => {
    if (dragSeek === null) return;
    const f = apply ? frac(ev) : null;
    dragSeek = null;
    seek.classList.remove("dragging");
    const pos = progressPos();
    if (f !== null && pos) seekTo(f * pos.music);
    renderBar();
  };
  seek.onpointerup = (ev) => endSeek(ev, true);
  seek.onpointercancel = (ev) => endSeek(ev, false);
  seek.onkeydown = (ev) => {
    const pos = progressPos();
    const step = { ArrowLeft: -5, ArrowRight: 5, PageDown: -30, PageUp: 30 }[ev.key];
    if (!pos || step === undefined) return;
    ev.preventDefault();
    seekTo(pos.t + step);
  };

  // ---------- swipe down to close ----------
  let drag = null;
  sheet.addEventListener("touchstart", (ev) => {
    if (ev.touches.length !== 1 || ev.target.closest("#sheet-seek")) { drag = null; return; }
    drag = { y: ev.touches[0].clientY, dy: 0, ok: $("sheet").querySelector(".sheet-body").scrollTop <= 0 };
  }, { passive: true });
  sheet.addEventListener("touchmove", (ev) => {
    if (!drag || !drag.ok) return;
    drag.dy = ev.touches[0].clientY - drag.y;
    if (drag.dy > 0) {
      sheet.style.transition = "none";
      sheet.style.transform = `translateY(${drag.dy}px)`;
    }
  }, { passive: true });
  const endSwipe = () => {
    if (!drag) return;
    const dy = drag.dy;
    drag = null;
    sheet.style.transition = "";
    if (dy > 110) closeSheet(); else sheet.style.transform = "";
  };
  sheet.addEventListener("touchend", endSwipe);
  sheet.addEventListener("touchcancel", endSwipe);

  render();
  renderBar();
})();
