/* F1 Lab viewer: lap browser, track-map replay, halo HUD, comparison. */
"use strict";

const $ = (id) => document.getElementById(id);

const state = {
  sessions: [],
  sessionId: null,
  laps: [],
  lapA: null,        // viewed lap (loaded, with samples + lookups)
  lapB: null,        // reference lap
  t: 0,              // playhead ms (lap A clock)
  playing: false,
  speed: 1,
  lastFrame: 0,
  mode: "speed",     // racing-line color mode: speed | gap
};

/* ---------------------------------------------------------------- color */

const SEC_COLORS = ["#f87171", "#60a5fa", "#fbbf24"];      // S1 / S2 / S3
// slow -> fast: viridis (perceptually ordered, lightness-monotonic), which
// keeps clear of the S1/S2/S3 identity colors
const SPEED_RAMP = ["#440154", "#414487", "#2a788e", "#22a884", "#7ad151", "#fde725"];
// most of a lap sits near top speed; gamma spends more of the ramp up there
const SPEED_GAMMA = 2.0;
// track dominance: your color <- neutral -> reference's color
const GAP_RAMP = ["#22d3ee", "#39414f", "#fb923c"];

function hex2rgb(h) {
  return [parseInt(h.slice(1, 3), 16), parseInt(h.slice(3, 5), 16),
          parseInt(h.slice(5, 7), 16)];
}
function ramp(stops, f) {
  f = Math.min(1, Math.max(0, f));
  const seg = f * (stops.length - 1);
  const i = Math.min(stops.length - 2, Math.floor(seg)), g = seg - i;
  const a = hex2rgb(stops[i]), b = hex2rgb(stops[i + 1]);
  return "rgb(" + Math.round(a[0] + (b[0] - a[0]) * g) + "," +
    Math.round(a[1] + (b[1] - a[1]) * g) + "," +
    Math.round(a[2] + (b[2] - a[2]) * g) + ")";
}

/* ---------------------------------------------------------------- utils */

function fmtTime(ms, forceMin) {
  if (ms == null || ms <= 0) return "—";
  const m = Math.floor(ms / 60000), s = (ms % 60000) / 1000;
  if (!m && !forceMin) return s.toFixed(3);
  return m + ":" + s.toFixed(3).padStart(6, "0");
}
function fmtDelta(sec) {
  return (sec >= 0 ? "+" : "−") + Math.abs(sec).toFixed(3);
}

/* Binary search: index of last element <= v in sorted arr. */
function lowerIdx(arr, v) {
  let lo = 0, hi = arr.length - 1;
  if (v <= arr[0]) return 0;
  if (v >= arr[hi]) return hi;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (arr[mid] <= v) lo = mid; else hi = mid;
  }
  return lo;
}
/* Linear interpolation of column col at key value v over key array keys. */
function interp(keys, col, v) {
  const i = lowerIdx(keys, v);
  if (i >= keys.length - 1) return col[keys.length - 1];
  const k0 = keys[i], k1 = keys[i + 1];
  if (k1 === k0) return col[i];
  const f = (v - k0) / (k1 - k0);
  return col[i] + (col[i + 1] - col[i]) * f;
}

/* ---------------------------------------------------------------- api */

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error(path + " -> " + r.status);
  return r.json();
}

/* Real circuit outlines (bundled from the f1-circuits dataset). */
let TRACKS = null;
const tracksReady = fetch("tracks.json").then((r) => r.json())
  .then((t) => { TRACKS = t; }).catch(() => {});

/* For laps with no position data (imports): place them on the real outline. */
function synthCoords(lap) {
  const tr = TRACKS && TRACKS[lap.track_id];
  if (!tr) return false;
  const xs = tr.pts.map((p) => p[0]), zs = tr.pts.map((p) => p[1]);
  const ds = [0];
  for (let i = 1; i < xs.length; i++)
    ds.push(ds[i - 1] + Math.hypot(xs[i] - xs[i - 1], zs[i] - zs[i - 1]));
  const total = ds[ds.length - 1];
  const k = total / (lap.track_length || total);  // game metres -> outline metres
  const s = lap.samples;
  s.x = s.d.map((d) => interp(ds, xs, Math.min(d * k, total)));
  s.z = s.d.map((d) => interp(ds, zs, Math.min(d * k, total)));
  return true;
}

/* Prepare a loaded lap: clean samples, build lookups. */
function prepLap(lap) {
  const s = lap.samples;
  // keep only strictly increasing distance (drops any flashback residue)
  const keep = [];
  let lastD = -1;
  for (let i = 0; i < s.d.length; i++) {
    if (s.d[i] > lastD) { keep.push(i); lastD = s.d[i]; }
  }
  if (keep.length !== s.d.length) {
    for (const k of Object.keys(s)) s[k] = keep.map((i) => s[k][i]);
  }
  // no position data? (all coords near-constant) -> use the real outline
  let minX = 1e9, maxX = -1e9, minZ = 1e9, maxZ = -1e9;
  for (let i = 0; i < s.x.length; i++) {
    if (s.x[i] < minX) minX = s.x[i]; if (s.x[i] > maxX) maxX = s.x[i];
    if (s.z[i] < minZ) minZ = s.z[i]; if (s.z[i] > maxZ) maxZ = s.z[i];
  }
  if (Math.max(maxX - minX, maxZ - minZ) < 50) lap.approxMap = synthCoords(lap);
  lap.duration = lap.lap_time_ms || s.t[s.t.length - 1];
  lap.maxD = s.d[s.d.length - 1];
  // sector boundary distances, derived from sector times
  if (lap.s1_ms && lap.s2_ms)
    lap.secD = [interp(s.t, s.d, lap.s1_ms),
                interp(s.t, s.d, lap.s1_ms + lap.s2_ms)];
  return lap;
}

/* ---------------------------------------------------------------- corners */

/* Detect corners from path curvature; number them in driving order. */
function computeCorners(lap) {
  const s = lap.samples, step = 8;
  const grid = [], gx = [], gz = [];
  for (let d = 0; d <= lap.maxD; d += step) {
    grid.push(d);
    gx.push(interp(s.d, s.x, d));
    gz.push(interp(s.d, s.z, d));
  }
  const n = grid.length, kap = new Array(n).fill(0);
  let prevHead = null;
  for (let i = 1; i < n; i++) {
    const head = Math.atan2(gz[i] - gz[i - 1], gx[i] - gx[i - 1]);
    if (prevHead !== null) {
      let dh = head - prevHead;
      while (dh > Math.PI) dh -= 2 * Math.PI;
      while (dh < -Math.PI) dh += 2 * Math.PI;
      kap[i] = Math.abs(dh) / step;
    }
    prevHead = head;
  }
  const sm = kap.map((_, i) => ((kap[i - 1] || 0) + kap[i] + (kap[i + 1] || 0)) / 3);
  const TH = 1 / 220;   // corner = local radius under ~220 m
  const corners = [];
  let start = -1;
  for (let i = 0; i <= n; i++) {
    const inC = i < n && sm[i] > TH;
    if (inC && start < 0) start = i;
    if (!inC && start >= 0) {
      if ((i - start) * step >= 24) {
        let apex = start;
        for (let j = start; j < i; j++) if (sm[j] > sm[apex]) apex = j;
        corners.push({ startD: grid[start], endD: grid[Math.min(i, n - 1)],
                       apexD: grid[apex], x: gx[apex], z: gz[apex] });
      }
      start = -1;
    }
  }
  corners.forEach((c, i) => { c.n = i + 1; });
  return corners;
}

/* Per-corner time gained/lost vs the reference (braking zone included). */
function cornerDeltas() {
  const A = state.lapA, B = state.lapB;
  const maxD = Math.min(A.maxD, B.maxD);
  const del = (d) => {
    d = Math.min(d, maxD);
    return interp(A.samples.d, A.samples.t, d) - interp(B.samples.d, B.samples.t, d);
  };
  for (const c of A.corners) {
    const d0 = Math.max(0, c.startD - 80), d1 = Math.min(A.maxD, c.endD + 40);
    c.loss = (del(d1) - del(d0)) / 1000;   // s: + = lost time, - = gained
  }
}

/* ---------------------------------------------------------------- header / status */

let lastLapStamp = "";
async function pollStatus() {
  try {
    const st = await api("/api/status");
    const chip = $("status-chip");
    if (st.pps > 0) {
      chip.className = "chip live"; chip.textContent = "LIVE " + st.pps + " pps";
    } else {
      chip.className = "chip idle";
      chip.textContent = st.listening ? "IDLE" : "OFFLINE";
    }
    let detail = "";
    if (st.session) detail = st.session.track + " · " + st.session.type;
    if (st.live && st.pps > 0)
      detail += " · lap " + st.live.lap_num + " · " + fmtTime(st.live.lap_time_ms, 1);
    $("status-detail").textContent = detail;

    const stamp = JSON.stringify(st.last_lap);
    if (stamp !== lastLapStamp) {
      lastLapStamp = stamp;
      await loadSessions(true);   // new lap stored -> refresh lists
    }
  } catch (e) { /* server briefly away; retry next tick */ }
  setTimeout(pollStatus, 2000);
}

/* ---------------------------------------------------------------- sessions & laps */

async function loadSessions(keepSelection) {
  state.sessions = await api("/api/sessions");
  const sel = $("session-select");
  const prev = state.sessionId;
  sel.innerHTML = "";
  for (const s of state.sessions) {
    const o = document.createElement("option");
    o.value = s.id;
    o.textContent = `#${s.id} ${s.track_name} — ${s.session_type_name}` +
      (s.best_ms ? ` (best ${fmtTime(s.best_ms, 1)})` : "") +
      ` · ${(s.started_at || "").replace("T", " ")}`;
    sel.appendChild(o);
  }
  if (!state.sessions.length) { $("lap-list").innerHTML =
    '<div class="empty-list">No sessions recorded yet.</div>'; return; }
  const want = keepSelection && prev &&
    state.sessions.some((s) => s.id === prev) ? prev : state.sessions[0].id;
  sel.value = want;
  if (want !== prev || keepSelection) await selectSession(want);
}

async function selectSession(id) {
  state.sessionId = id;
  state.laps = await api(`/api/sessions/${id}/laps`);
  renderLapList();
}

function roleBadge(role) {
  const label = { player: "YOU", rival: "RIVAL", pb_ghost: "PB·G" }[role] || role;
  return `<span class="badge ${role}">${label}</span>`;
}

function renderLapList() {
  const box = $("lap-list");
  box.innerHTML = "";
  if (!state.laps.length) {
    box.innerHTML = '<div class="empty-list">No complete laps in this session yet — finish a full lap and it appears here automatically.</div>';
    return;
  }
  for (const lap of state.laps) {
    const row = document.createElement("div");
    row.className = "lap-row";
    if (state.lapA && lap.id === state.lapA.id) row.classList.add("sel");
    if (state.lapB && lap.id === state.lapB.id) row.classList.add("ref-sel");
    row.innerHTML = `
      ${roleBadge(lap.car_role)}
      <div class="lap-main">
        <div class="lap-time">${fmtTime(lap.lap_time_ms, 1)}
          ${lap.valid ? "" : '<span class="inv">INV</span>'}</div>
        <div class="lap-sub">L${lap.lap_num} · ${fmtTime(lap.s1_ms)} | ${fmtTime(lap.s2_ms)} | ${fmtTime(lap.s3_ms)} · ${lap.top_speed} km/h</div>
      </div>
      <button class="star ${state.lapB && lap.id === state.lapB.id ? "on" : ""}" title="set as reference">★</button>
      <button class="del" title="delete lap">✕</button>`;
    row.addEventListener("click", () => viewLap(lap.id));
    row.querySelector(".star").addEventListener("click", (e) => {
      e.stopPropagation(); toggleRef(lap.id);
    });
    row.querySelector(".del").addEventListener("click", async (e) => {
      e.stopPropagation();
      if (!confirm("Delete this lap?")) return;
      await api(`/api/laps/${lap.id}`, { method: "DELETE" });
      if (state.lapA && state.lapA.id === lap.id) state.lapA = null;
      if (state.lapB && state.lapB.id === lap.id) state.lapB = null;
      await selectSession(state.sessionId);
      rebuildScene();
    });
    box.appendChild(row);
  }
}

async function viewLap(id) {
  await tracksReady;
  state.lapA = prepLap(await api(`/api/laps/${id}`));
  state.t = 0; state.playing = true;
  renderLapList(); rebuildScene();
}
async function toggleRef(id) {
  await tracksReady;
  if (state.lapB && state.lapB.id === id) state.lapB = null;
  else {
    state.lapB = prepLap(await api(`/api/laps/${id}`));
    state.mode = "gap";   // comparing: show where time is gained/lost
  }
  renderLapList(); rebuildScene();
}

/* ---------------------------------------------------------------- track map */

const map = $("map");
const mctx = map.getContext("2d");
let view = null;   // {sx, sy, scale} world->px

function fitMap() {
  const laps = [state.lapA, state.lapB].filter(Boolean);
  if (!laps.length) { view = null; return; }
  const dpr = window.devicePixelRatio || 1;
  map.width = map.clientWidth * dpr; map.height = map.clientHeight * dpr;
  let minX = 1e9, maxX = -1e9, minZ = 1e9, maxZ = -1e9;
  for (const lap of laps) {
    const s = lap.samples;
    for (let i = 0; i < s.x.length; i++) {
      if (s.x[i] < minX) minX = s.x[i]; if (s.x[i] > maxX) maxX = s.x[i];
      if (s.z[i] < minZ) minZ = s.z[i]; if (s.z[i] > maxZ) maxZ = s.z[i];
    }
  }
  // padding leaves room for corner badges, the legend row and the toolbar
  const pad = 58 * dpr, padT = 64 * dpr, padB = 78 * dpr;
  // keep the track clear of the HUD panel when there's room for both
  const hudReserve = map.clientWidth >= 900 ? 410 * dpr : 0;
  const w = map.width - 2 * pad - hudReserve, h = map.height - padT - padB;
  const scale = Math.min(w / (maxX - minX || 1), h / (maxZ - minZ || 1));
  view = {
    scale, dpr,
    ox: pad + (w - (maxX - minX) * scale) / 2 - minX * scale,
    // z axis flipped so "north" is up
    oy: padT + (h - (maxZ - minZ) * scale) / 2 + maxZ * scale,
  };
}
const W2X = (x) => view.ox + x * view.scale;
const W2Y = (z) => view.oy - z * view.scale;

/* The static map layer (ribbon, racing line, markers, badges) is expensive,
   so it renders once into an offscreen canvas; per-frame we blit + draw dots. */
let mapStatic = null;
let dctx = null;   // context the path helpers draw into

function pathStroke(lap, i0, i1, color, width, alpha) {
  const s = lap.samples;
  dctx.beginPath();
  dctx.moveTo(W2X(s.x[i0]), W2Y(s.z[i0]));
  for (let i = i0 + 1; i <= i1; i++) dctx.lineTo(W2X(s.x[i]), W2Y(s.z[i]));
  dctx.globalAlpha = alpha == null ? 1 : alpha;
  dctx.strokeStyle = color; dctx.lineWidth = width * view.dpr;
  dctx.lineJoin = "round"; dctx.lineCap = "round";
  dctx.stroke(); dctx.globalAlpha = 1;
}
function fullStroke(lap, color, width, alpha) {
  pathStroke(lap, 0, lap.samples.x.length - 1, color, width, alpha);
}
function rangeStroke(lap, d0, d1, color, width, alpha) {
  pathStroke(lap, lowerIdx(lap.samples.d, d0), lowerIdx(lap.samples.d, d1),
             color, width, alpha);
}

/* Racing-line colors per sample, for the active mode. */
function computeLineColors(A) {
  const s = A.samples, n = s.d.length, colors = new Array(n);
  if (state.mode === "gap" && state.lapB) {
    const B = state.lapB, lim = Math.min(A.maxD, B.maxD);
    for (let i = 0; i < n; i++) {
      const d0 = Math.max(0, Math.min(s.d[i], lim) - 40);
      const d1 = Math.min(lim, s.d[i] + 40);
      const dA = interp(s.d, s.t, d1) - interp(s.d, s.t, d0);
      const dB = interp(B.samples.d, B.samples.t, d1) -
                 interp(B.samples.d, B.samples.t, d0);
      const slope = (dA - dB) / Math.max(1, d1 - d0);  // ms lost per metre
      colors[i] = ramp(GAP_RAMP, 0.5 + slope / 4);
    }
  } else {
    let mn = 1e9, mx = 0;
    for (let i = 0; i < n; i++) {
      if (s.spd[i] < mn) mn = s.spd[i];
      if (s.spd[i] > mx) mx = s.spd[i];
    }
    A.spdRange = [mn, mx];
    for (let i = 0; i < n; i++)
      colors[i] = ramp(SPEED_RAMP,
        Math.pow((s.spd[i] - mn) / (mx - mn || 1), SPEED_GAMMA));
  }
  A.lineColors = colors;
}

function roundRect(ctx, x, y, w, h, r) {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}

function renderMapStatic() {
  if (!view || !map.width || !map.height) { mapStatic = null; return; }
  mapStatic = document.createElement("canvas");
  mapStatic.width = map.width; mapStatic.height = map.height;
  dctx = mapStatic.getContext("2d");
  const A = state.lapA, dpr = view.dpr, s = A.samples;

  // ribbon: sector-tinted edge under a dark road surface
  if (A.secD) {
    const ranges = [[0, A.secD[0]], [A.secD[0], A.secD[1]], [A.secD[1], A.maxD]];
    ranges.forEach((r, i) => rangeStroke(A, r[0], r[1], SEC_COLORS[i], 13, 0.5));
  } else {
    fullStroke(A, "#2a3242", 13);
  }
  fullStroke(A, "#141a25", 9);

  if (state.lapB) fullStroke(state.lapB, "rgba(251,146,60,.25)", 1.5);

  // racing line colored by speed / gap
  const stepN = Math.max(1, Math.floor(s.x.length / 900));
  for (let i = stepN; i < s.x.length; i += stepN) {
    dctx.beginPath();
    dctx.moveTo(W2X(s.x[i - stepN]), W2Y(s.z[i - stepN]));
    dctx.lineTo(W2X(s.x[i]), W2Y(s.z[i]));
    dctx.strokeStyle = A.lineColors[i];
    dctx.lineWidth = 2.6 * dpr; dctx.lineCap = "round";
    dctx.stroke();
  }

  // sector boundary notches
  const centX = (Math.min(...s.x) + Math.max(...s.x)) / 2;
  const centZ = (Math.min(...s.z) + Math.max(...s.z)) / 2;
  function outward(d) {
    const x0 = interp(s.d, s.x, Math.max(0, d - 10));
    const z0 = interp(s.d, s.z, Math.max(0, d - 10));
    const x1 = interp(s.d, s.x, Math.min(A.maxD, d + 10));
    const z1 = interp(s.d, s.z, Math.min(A.maxD, d + 10));
    let nx = -(z1 - z0), nz = x1 - x0;
    const len = Math.hypot(nx, nz) || 1;
    nx /= len; nz /= len;
    const px = interp(s.d, s.x, d), pz = interp(s.d, s.z, d);
    if (nx * (px - centX) + nz * (pz - centZ) < 0) { nx = -nx; nz = -nz; }
    // to screen space (z axis flips)
    return { x: W2X(px), y: W2Y(pz), nx: nx, ny: -nz };
  }
  if (A.secD) {
    A.secD.forEach((d, i) => {
      const o = outward(d);
      dctx.beginPath();
      dctx.moveTo(o.x - o.nx * 8 * dpr, o.y - o.ny * 8 * dpr);
      dctx.lineTo(o.x + o.nx * 10 * dpr, o.y + o.ny * 10 * dpr);
      dctx.strokeStyle = SEC_COLORS[i + 1]; dctx.lineWidth = 2.5 * dpr;
      dctx.stroke();
      dctx.fillStyle = SEC_COLORS[i + 1];
      dctx.font = "700 " + 10 * dpr + "px sans-serif"; dctx.textAlign = "center";
      dctx.fillText("S" + (i + 2), o.x + o.nx * 20 * dpr, o.y + o.ny * 20 * dpr + 3 * dpr);
    });
  }

  // start/finish
  dctx.fillStyle = "#dbe2ee";
  dctx.save();
  dctx.translate(W2X(s.x[0]), W2Y(s.z[0]));
  dctx.rotate(Math.atan2(-(s.z[1] - s.z[0]), s.x[1] - s.x[0]) + Math.PI / 2);
  dctx.fillRect(-8 * dpr, -2 * dpr, 16 * dpr, 4 * dpr);
  dctx.restore();

  // corner numbers
  dctx.font = "700 " + 9.5 * dpr + "px sans-serif"; dctx.textAlign = "center";
  for (const c of A.corners || []) {
    const o = outward(c.apexD);
    dctx.fillStyle = "#4d5a70";
    dctx.fillText(c.n, o.x + o.nx * 17 * dpr, o.y + o.ny * 17 * dpr + 3 * dpr);
  }

  // biggest time losses / gains vs reference -> badges at the corners
  if (state.lapB && A.corners && A.corners.length) {
    const ranked = A.corners.filter((c) => c.loss != null)
      .sort((a, b) => b.loss - a.loss);
    const badges = ranked.filter((c) => c.loss > 0.05).slice(0, 3);
    const gain = ranked[ranked.length - 1];
    if (gain && gain.loss < -0.08) badges.push(gain);
    dctx.font = "800 " + 10 * dpr + "px sans-serif";
    for (const c of badges) {
      const o = outward(c.apexD);
      const lost = c.loss > 0;
      const bx = o.x + o.nx * 40 * dpr, by = o.y + o.ny * 40 * dpr;
      dctx.strokeStyle = lost ? "rgba(248,113,113,.5)" : "rgba(52,211,153,.5)";
      dctx.lineWidth = 1 * dpr;
      dctx.beginPath(); dctx.moveTo(o.x + o.nx * 6 * dpr, o.y + o.ny * 6 * dpr);
      dctx.lineTo(bx, by); dctx.stroke();
      const label = "T" + c.n + "  " + (lost ? "+" : "−") +
        Math.abs(c.loss).toFixed(2);
      const w = dctx.measureText(label).width + 14 * dpr;
      roundRect(dctx, bx - w / 2, by - 9 * dpr, w, 18 * dpr, 5 * dpr);
      dctx.fillStyle = lost ? "rgba(84,17,17,.92)" : "rgba(4,55,38,.92)";
      dctx.fill();
      dctx.strokeStyle = lost ? "#7f2626" : "#0f5c40";
      dctx.stroke();
      dctx.fillStyle = lost ? "#fda4a4" : "#6ee7b7";
      dctx.fillText(label, bx, by + 3.5 * dpr);
    }
  }
  dctx = null;
}

function drawMap() {
  mctx.clearRect(0, 0, map.width, map.height);
  if (!view || !state.lapA) return;
  const A = state.lapA, dpr = view.dpr, s = A.samples;
  if (mapStatic && mapStatic.width) mctx.drawImage(mapStatic, 0, 0);

  // ghost dot (reference lap at same elapsed time)
  if (state.lapB) {
    const B = state.lapB, tB = Math.min(state.t, B.duration);
    const bx = interp(B.samples.t, B.samples.x, tB);
    const bz = interp(B.samples.t, B.samples.z, tB);
    mctx.beginPath();
    mctx.arc(W2X(bx), W2Y(bz), 7 * dpr, 0, 7);
    mctx.fillStyle = "#fb923c"; mctx.globalAlpha = 0.85; mctx.fill();
    mctx.globalAlpha = 1;
  }

  // player dot
  const ax = interp(s.t, s.x, state.t), az = interp(s.t, s.z, state.t);
  mctx.beginPath();
  mctx.arc(W2X(ax), W2Y(az), 8 * dpr, 0, 7);
  mctx.fillStyle = "#22d3ee";
  mctx.strokeStyle = "#0b0e13"; mctx.lineWidth = 2 * dpr;
  mctx.shadowColor = "#22d3ee"; mctx.shadowBlur = 14 * dpr;
  mctx.fill(); mctx.shadowBlur = 0; mctx.stroke();
}

map.addEventListener("click", (e) => {
  if (!view || !state.lapA) return;
  const r = map.getBoundingClientRect();
  const px = (e.clientX - r.left) * view.dpr, py = (e.clientY - r.top) * view.dpr;
  const s = state.lapA.samples;
  let best = -1, bestDist = 1e18;
  const step = Math.max(1, Math.floor(s.x.length / 2000));
  for (let i = 0; i < s.x.length; i += step) {
    const dx = W2X(s.x[i]) - px, dy = W2Y(s.z[i]) - py;
    const d2 = dx * dx + dy * dy;
    if (d2 < bestDist) { bestDist = d2; best = i; }
  }
  if (best >= 0 && bestDist < (40 * view.dpr) ** 2) seek(s.t[best]);
});

/* ---------------------------------------------------------------- halo hud */

const HALO = { cx: 285, cy: 105, r: 78 };
const THR = { from: 230, to: 130 };   // left side, fills bottom -> top
const BRK = { from: -50, to: 50 };    // right side, fills bottom -> top
const N_REV = 15;

function polar(cx, cy, r, deg) {
  const rad = (deg * Math.PI) / 180;
  return [cx + r * Math.cos(rad), cy - r * Math.sin(rad)];
}
function arcPath(a0, a1) {
  const { cx, cy, r } = HALO;
  const [x0, y0] = polar(cx, cy, r, a0);
  const [x1, y1] = polar(cx, cy, r, a1);
  const sweep = a1 > a0 ? 0 : 1;
  return `M ${x0.toFixed(1)} ${y0.toFixed(1)} A ${r} ${r} 0 0 ${sweep} ${x1.toFixed(1)} ${y1.toFixed(1)}`;
}
const SVG = "http://www.w3.org/2000/svg";

function initHalo() {
  $("thr-bg").setAttribute("d", arcPath(THR.from, THR.to));
  $("brk-bg").setAttribute("d", arcPath(BRK.from, BRK.to));
  // ticks at 0/25/50/75/100% on both arcs
  const ticks = $("arc-ticks");
  for (const cfg of [THR, BRK]) {
    for (const f of [0, 0.25, 0.5, 0.75, 1]) {
      const a = cfg.from + (cfg.to - cfg.from) * f;
      const [x0, y0] = polar(HALO.cx, HALO.cy, HALO.r - 9, a);
      const [x1, y1] = polar(HALO.cx, HALO.cy, HALO.r + 9, a);
      const ln = document.createElementNS(SVG, "line");
      ln.setAttribute("x1", x0); ln.setAttribute("y1", y0);
      ln.setAttribute("x2", x1); ln.setAttribute("y2", y1);
      ln.setAttribute("class", "arc-tick");
      ticks.appendChild(ln);
    }
  }
  // rev-light strip
  const strip = $("rev-strip");
  for (let i = 0; i < N_REV; i++) {
    const seg = document.createElementNS(SVG, "rect");
    seg.setAttribute("x", 195 + i * 12.4);
    seg.setAttribute("y", 8);
    seg.setAttribute("width", 9); seg.setAttribute("height", 8);
    seg.setAttribute("rx", 2);
    seg.setAttribute("class", "rev-seg");
    seg.id = "rev-" + i;
    strip.appendChild(seg);
  }
}

const REV_COLORS = (i) => i < 5 ? "#34d399" : i < 10 ? "#f87171" : "#c4b5fd";

function setHalo(thr, brk, speed, gear, drs, ot, steer, rpm) {
  $("thr-arc").setAttribute("d",
    thr <= 0.5 ? "M 0 0" : arcPath(THR.from, THR.from + (THR.to - THR.from) * (thr / 100)));
  $("brk-arc").setAttribute("d",
    brk <= 0.5 ? "M 0 0" : arcPath(BRK.from, BRK.from + (BRK.to - BRK.from) * (brk / 100)));
  $("hud-speed").textContent = Math.round(speed);
  $("hud-gear").textContent = gear > 0 ? gear : gear === 0 ? "N" : "R";

  const dp = $("drs-pill"), dt = $("drs-txt");
  dp.setAttribute("class", drs ? "pill on-drs" : "pill");
  dt.style.fill = drs ? "#6ee7b7" : "";
  const op = $("ot-pill"), otx = $("ot-txt");
  op.setAttribute("class", ot ? "pill on-ot" : "pill");
  otx.style.fill = ot ? "#d8b4fe" : "";

  // steering wheel: ±100% steer -> ±120° rotation
  $("wheel-rot").setAttribute("transform", "rotate(" + (steer * 1.2).toFixed(1) + ")");
  const sv = $("steer-val");
  sv.textContent = steer <= -3 ? "L " + Math.round(-steer)
    : steer >= 3 ? "R " + Math.round(steer) : "0";

  // rev lights
  const frac = Math.max(0, Math.min(1, (rpm - 4000) / 8600));
  const lit = Math.round(frac * N_REV);
  for (let i = 0; i < N_REV; i++)
    $("rev-" + i).style.fill = i < lit ? REV_COLORS(i) : "";
}

/* ---------------------------------------------------------------- charts */

const charts = [
  { id: "ch-speed", col: "spd", min: 0, max: null },
  { id: "ch-pedals", cols: ["thr", "brk"], min: 0, max: 100 },
  { id: "ch-steer", col: "str", min: -100, max: 100, zero: true },
];
const chartCache = {};   // id -> {img, xOf, w, h}

function drawSectorBands(ctx, lap, X, w, h) {
  if (!lap.secD) return;
  const ranges = [[0, lap.secD[0]], [lap.secD[0], lap.secD[1]],
                  [lap.secD[1], lap.maxD]];
  ranges.forEach((r, i) => {
    ctx.fillStyle = SEC_COLORS[i]; ctx.globalAlpha = 0.05;
    ctx.fillRect(X(r[0]), 0, X(r[1]) - X(r[0]), h);
    ctx.globalAlpha = 1;
  });
  ctx.strokeStyle = "rgba(219,226,238,.14)"; ctx.lineWidth = 1;
  for (const d of lap.secD) {
    ctx.beginPath(); ctx.moveTo(X(d), 0); ctx.lineTo(X(d), h); ctx.stroke();
  }
}

function buildChart(cfg) {
  const cv = $(cfg.id), dpr = window.devicePixelRatio || 1;
  if (!cv.clientWidth) { delete chartCache[cfg.id]; return; }
  cv.width = cv.clientWidth * dpr; cv.height = cv.clientHeight * dpr;
  const ctx = cv.getContext("2d");
  const A = state.lapA;
  const maxD = Math.max(A.maxD, state.lapB ? state.lapB.maxD : 0);
  let max = cfg.max, min = cfg.min;
  if (max == null) {
    max = 0;
    for (const lap of [A, state.lapB]) if (lap)
      max = Math.max(max, Math.max.apply(null, lap.samples[cfg.col]));
    max = Math.ceil(max / 50) * 50;
  }
  const w = cv.width, h = cv.height, padT = 3 * dpr, padB = 3 * dpr;
  const X = (d) => (d / maxD) * w;
  const Y = (v) => padT + (1 - (v - min) / (max - min)) * (h - padT - padB);

  ctx.clearRect(0, 0, w, h);
  ctx.fillStyle = "#0e1219"; ctx.fillRect(0, 0, w, h);
  drawSectorBands(ctx, A, X, w, h);
  ctx.strokeStyle = "#1c2331"; ctx.lineWidth = 1;
  for (const f of [0.25, 0.5, 0.75]) {
    ctx.beginPath(); ctx.moveTo(0, Y(min + (max - min) * f));
    ctx.lineTo(w, Y(min + (max - min) * f)); ctx.stroke();
  }
  if (cfg.zero) {
    ctx.strokeStyle = "#2a3242";
    ctx.beginPath(); ctx.moveTo(0, Y(0)); ctx.lineTo(w, Y(0)); ctx.stroke();
  }
  // corner numbers along the top of the speed chart
  if (cfg.id === "ch-speed" && A.corners) {
    ctx.fillStyle = "#44506455"; ctx.fillStyle = "#445064";
    ctx.font = 8 * dpr + "px sans-serif"; ctx.textAlign = "center";
    for (const c of A.corners) ctx.fillText(c.n, X(c.apexD), 9 * dpr);
    ctx.textAlign = "left";
  }

  const colColors = { thr: "#34d399", brk: "#f87171" };
  function trace(lap, col, color, alpha, width) {
    const s = lap.samples;
    ctx.beginPath();
    ctx.moveTo(X(s.d[0]), Y(s[col][0]));
    for (let i = 1; i < s.d.length; i++) ctx.lineTo(X(s.d[i]), Y(s[col][i]));
    ctx.globalAlpha = alpha; ctx.strokeStyle = color;
    ctx.lineWidth = width * dpr; ctx.stroke(); ctx.globalAlpha = 1;
  }
  const cols = cfg.cols || [cfg.col];
  if (state.lapB)
    for (const c of cols)
      trace(state.lapB, c, cfg.cols ? colColors[c] : "#fb923c", 0.45, 1);
  for (const c of cols)
    trace(A, c, cfg.cols ? colColors[c] : "#22d3ee", 1, 1.5);

  chartCache[cfg.id] = { img: ctx.getImageData(0, 0, w, h), maxD, w, h };
}

function buildDeltaChart() {
  const row = $("delta-row");
  if (!state.lapB || !state.lapA) { row.style.display = "none"; delete chartCache["ch-delta"]; return; }
  row.style.display = "";
  const cv = $("ch-delta"), dpr = window.devicePixelRatio || 1;
  if (!cv.clientWidth) { delete chartCache["ch-delta"]; return; }
  cv.width = cv.clientWidth * dpr; cv.height = cv.clientHeight * dpr;
  const ctx = cv.getContext("2d");
  const A = state.lapA, B = state.lapB;
  const maxD = Math.min(A.maxD, B.maxD);
  const chartMaxD = Math.max(A.maxD, B.maxD);
  const grid = [], delta = [];
  let dmax = 0.05;
  for (let d = 0; d <= maxD; d += 8) {
    const dt = (interp(A.samples.d, A.samples.t, d) -
                interp(B.samples.d, B.samples.t, d)) / 1000;
    grid.push(d); delta.push(dt);
    if (Math.abs(dt) > dmax) dmax = Math.abs(dt);
  }
  dmax *= 1.1;
  const w = cv.width, h = cv.height;
  const X = (d) => (d / chartMaxD) * w;
  const Y = (v) => (1 - (v + dmax) / (2 * dmax)) * (h - 6 * dpr) + 3 * dpr;
  ctx.fillStyle = "#0e1219"; ctx.fillRect(0, 0, w, h);
  drawSectorBands(ctx, A, X, w, h);
  ctx.strokeStyle = "#2a3242";
  ctx.beginPath(); ctx.moveTo(0, Y(0)); ctx.lineTo(X(maxD), Y(0)); ctx.stroke();

  // fill: above zero = losing time (red), below = gaining (green)
  for (let i = 1; i < grid.length; i++) {
    ctx.fillStyle = delta[i] >= 0 ? "rgba(248,113,113,.25)" : "rgba(52,211,153,.25)";
    ctx.fillRect(X(grid[i - 1]), Math.min(Y(0), Y(delta[i])),
                 X(grid[i]) - X(grid[i - 1]), Math.abs(Y(delta[i]) - Y(0)));
  }
  ctx.beginPath();
  ctx.moveTo(X(grid[0]), Y(delta[0]));
  for (let i = 1; i < grid.length; i++) ctx.lineTo(X(grid[i]), Y(delta[i]));
  ctx.strokeStyle = "#dbe2ee"; ctx.lineWidth = 1.2 * dpr; ctx.stroke();

  // scale label
  ctx.fillStyle = "#7d8798"; ctx.font = `${10 * dpr}px sans-serif`;
  ctx.fillText("±" + dmax.toFixed(2) + "s", 6 * dpr, 12 * dpr);

  chartCache["ch-delta"] = { img: ctx.getImageData(0, 0, w, h), maxD: chartMaxD, w, h };
}

function drawChartCursors() {
  if (!state.lapA) return;
  const dA = interp(state.lapA.samples.t, state.lapA.samples.d, state.t);
  for (const id of Object.keys(chartCache)) {
    const c = chartCache[id];
    const ctx = $(id).getContext("2d");
    ctx.putImageData(c.img, 0, 0);
    const x = (dA / c.maxD) * c.w;
    ctx.strokeStyle = "rgba(219,226,238,.7)"; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, c.h); ctx.stroke();
  }
}

function chartSeek(e, cv) {
  if (!state.lapA) return;
  const cache = chartCache[cv.id];
  if (!cache) return;
  const r = cv.getBoundingClientRect();
  const frac = Math.min(1, Math.max(0, (e.clientX - r.left) / r.width));
  const d = frac * cache.maxD;
  seek(interp(state.lapA.samples.d, state.lapA.samples.t,
              Math.min(d, state.lapA.maxD)));
}
for (const id of ["ch-speed", "ch-pedals", "ch-steer", "ch-delta"]) {
  const cv = $(id);
  let dragging = false;
  cv.addEventListener("mousedown", (e) => { dragging = true; chartSeek(e, cv); });
  window.addEventListener("mousemove", (e) => { if (dragging) chartSeek(e, cv); });
  window.addEventListener("mouseup", () => { dragging = false; });
}

/* ---------------------------------------------------------------- scene & playback */

function rebuildScene() {
  const has = !!state.lapA;
  $("empty-hint").style.display = has ? "none" : "";
  $("map-toolbar").style.display = has ? "" : "none";
  $("sector-card").style.display = has ? "" : "none";
  $("map-legend").innerHTML = has
    ? `<span class="k kA">${fmtTime(state.lapA.lap_time_ms, 1)} (${state.lapA.car_role})</span>` +
      (state.lapB ? `<span class="k kB">${fmtTime(state.lapB.lap_time_ms, 1)} (${state.lapB.car_role}) — reference</span>` : "") +
      (state.lapA.approxMap ? `<span>${state.lapA.track_name} · approx. map (no position data)</span>` : "")
    : "";
  if (!has) { mapStatic = null; drawMap(); return; }
  const A = state.lapA;
  if (!A.corners) A.corners = computeCorners(A);
  if (state.lapB) cornerDeltas();
  if (state.mode === "gap" && !state.lapB) state.mode = "speed";
  fitMap();
  computeLineColors(A);
  renderMapStatic();
  updateToolbar();
  updateSectorCard();
  updateScrubTint();
  for (const cfg of charts) buildChart(cfg);
  buildDeltaChart();
  drawFrame();
}

function updateToolbar() {
  const gapBtn = document.querySelector('#mode-seg button[data-mode="gap"]');
  gapBtn.disabled = !state.lapB;
  for (const b of document.querySelectorAll("#mode-seg button"))
    b.classList.toggle("on", b.dataset.mode === state.mode);
  const bar = $("leg-bar"), lo = $("leg-lo"), hi = $("leg-hi");
  const title = $("leg-title");
  if (state.mode === "gap") {
    title.textContent = "TRACK DOMINANCE";
    bar.style.background = `linear-gradient(90deg, ${GAP_RAMP.join(",")})`;
    lo.textContent = "YOU FASTER"; lo.style.color = "#22d3ee";
    hi.textContent = "REF FASTER"; hi.style.color = "#fb923c";
  } else {
    title.textContent = "SPEED";
    // legend bar mirrors the gamma curve applied to the racing line
    const stops = [];
    for (let i = 0; i <= 8; i++) {
      const p = i / 8;
      stops.push(`${ramp(SPEED_RAMP, Math.pow(p, SPEED_GAMMA))} ${p * 100}%`);
    }
    bar.style.background = `linear-gradient(90deg, ${stops.join(",")})`;
    const r = state.lapA.spdRange || [0, 0];
    lo.textContent = r[0]; lo.style.color = "";
    hi.textContent = r[1] + " KM/H"; hi.style.color = "";
  }
}

function updateSectorCard() {
  const A = state.lapA, B = state.lapB;
  const secs = (l) => [l.s1_ms, l.s2_ms, l.s3_ms];
  const fmtS = (ms) => ms ? (ms / 1000).toFixed(3) : "—";
  let html = `<table><tr><th></th><th class="s1">S1</th><th class="s2">S2</th>
    <th class="s3">S3</th><th>LAP</th></tr>
    <tr><td class="rowlbl">${B ? "YOU" : "LAP"}</td>
      ${secs(A).map((v) => `<td>${fmtS(v)}</td>`).join("")}
      <td class="laptime">${fmtTime(A.lap_time_ms, 1)}</td></tr>`;
  if (B) {
    html += `<tr><td class="rowlbl">REF</td>
      ${secs(B).map((v) => `<td>${fmtS(v)}</td>`).join("")}
      <td class="laptime">${fmtTime(B.lap_time_ms, 1)}</td></tr><tr>
      <td class="rowlbl">Δ</td>`;
    for (let i = 0; i < 3; i++) {
      const a = secs(A)[i], b = secs(B)[i];
      if (a && b) {
        const d = (a - b) / 1000;
        html += `<td class="${d <= 0 ? "neg" : "pos"}">${d >= 0 ? "+" : "−"}${Math.abs(d).toFixed(3)}</td>`;
      } else html += "<td>—</td>";
    }
    const dl = (A.lap_time_ms - B.lap_time_ms) / 1000;
    html += `<td class="laptime ${dl <= 0 ? "neg" : "pos"}">${dl >= 0 ? "+" : "−"}${Math.abs(dl).toFixed(3)}</td></tr>`;
  }
  $("sector-card").innerHTML = html + "</table>";
}

function updateScrubTint() {
  const A = state.lapA, el = $("scrub");
  if (!A.s1_ms || !A.s2_ms) { el.style.background = ""; return; }
  const p1 = (A.s1_ms / A.duration) * 100;
  const p2 = ((A.s1_ms + A.s2_ms) / A.duration) * 100;
  el.style.background =
    `linear-gradient(90deg, rgba(248,113,113,.4) 0 ${p1}%,` +
    ` rgba(96,165,250,.4) ${p1}% ${p2}%, rgba(251,191,36,.4) ${p2}% 100%)`;
}

function seek(t) {
  if (!state.lapA) return;
  state.t = Math.min(Math.max(0, t), state.lapA.duration);
  drawFrame();
}

function drawFrame() {
  const A = state.lapA;
  if (!A) return;
  const s = A.samples, t = state.t;
  drawMap();
  setHalo(
    interp(s.t, s.thr, t), interp(s.t, s.brk, t),
    interp(s.t, s.spd, t), Math.round(interp(s.t, s.gear, t)),
    interp(s.t, s.drs, t) > 0.5, interp(s.t, s.ot, t) > 0.5,
    interp(s.t, s.str, t), interp(s.t, s.rpm, t));
  drawChartCursors();
  $("scrub").value = Math.round((t / A.duration) * 1000);
  $("time-display").textContent = fmtTime(t, 1);

  const hd = $("hud-delta");
  if (state.lapB) {
    const dA = interp(s.t, s.d, t);
    const dSec = (t - interp(state.lapB.samples.d, state.lapB.samples.t,
                             Math.min(dA, state.lapB.maxD))) / 1000;
    hd.textContent = fmtDelta(dSec);
    hd.className = "hud-delta " + (dSec >= 0 ? "pos" : "neg");
  } else hd.textContent = "";
}

function loop(now) {
  requestAnimationFrame(loop);
  // clamp: rAF throttling in background tabs must not cause time jumps
  const dt = Math.min(100, now - (state.lastFrame || now));
  state.lastFrame = now;
  if (!state.playing || !state.lapA) return;
  state.t += dt * state.speed;
  if (state.t >= state.lapA.duration) {
    state.t = state.lapA.duration;
    state.playing = false; $("btn-play").textContent = "▶";
  }
  drawFrame();
}

/* ---------------------------------------------------------------- controls */

function togglePlay() {
  if (!state.lapA) return;
  if (!state.playing && state.t >= state.lapA.duration) state.t = 0;
  state.playing = !state.playing;
  $("btn-play").textContent = state.playing ? "❚❚" : "▶";
}
$("btn-play").addEventListener("click", togglePlay);
$("speed-select").addEventListener("change", (e) => {
  state.speed = parseFloat(e.target.value);
});
$("scrub").addEventListener("input", (e) => {
  if (state.lapA) seek((e.target.value / 1000) * state.lapA.duration);
});
$("session-select").addEventListener("change", (e) => {
  selectSession(parseInt(e.target.value, 10));
});
$("mode-seg").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b || b.disabled || !state.lapA || b.dataset.mode === state.mode) return;
  state.mode = b.dataset.mode;
  computeLineColors(state.lapA);
  renderMapStatic();
  updateToolbar();
  drawFrame();
});
window.addEventListener("keydown", (e) => {
  if (e.target.tagName === "SELECT" || e.target.tagName === "INPUT") return;
  if (e.code === "Space") { e.preventDefault(); togglePlay(); }
  if (e.code === "ArrowRight") seek(state.t + (e.shiftKey ? 5000 : 1000));
  if (e.code === "ArrowLeft") seek(state.t - (e.shiftKey ? 5000 : 1000));
});
window.addEventListener("resize", () => { if (state.lapA) rebuildScene(); });
document.addEventListener("visibilitychange", () => {
  if (!document.hidden && state.lapA) rebuildScene();
});

/* ---------------------------------------------------------------- error surface */

function showToast(msg) {
  let t = $("err-toast");
  if (!t) {
    t = document.createElement("div");
    t.id = "err-toast";
    t.style.cssText = "position:fixed;bottom:12px;left:50%;transform:translateX(-50%);" +
      "background:#7f1d1d;color:#fecaca;padding:8px 14px;border-radius:8px;" +
      "font-size:12px;z-index:99;max-width:80%;box-shadow:0 4px 14px #0008";
    document.body.appendChild(t);
  }
  t.textContent = "Error: " + msg + " — refresh the page (Cmd+Shift+R); if it persists, report this text.";
}
window.addEventListener("error", (e) => showToast(e.message));
window.addEventListener("unhandledrejection", (e) => showToast(String(e.reason)));

/* ---------------------------------------------------------------- boot */

/* Input probe: proves whether mouse/keyboard events reach the page at all. */
{
  const probe = document.createElement("div");
  probe.style.cssText = "position:fixed;bottom:8px;right:8px;z-index:100;" +
    "background:#1d2230;color:#7d8798;font:11px ui-monospace,monospace;" +
    "padding:4px 9px;border-radius:6px;pointer-events:none";
  probe.textContent = "input probe: no clicks/keys received yet";
  document.body.appendChild(probe);
  let n = 0;
  for (const ev of ["pointerdown", "click", "keydown"]) {
    document.addEventListener(ev, (e) => {
      n++;
      const t = e.target;
      probe.textContent = "input #" + n + ": " + ev + " on " +
        (t.id || (typeof t.className === "string" && t.className.split(" ")[0]) || t.tagName);
      probe.style.color = "#34d399";
    }, true);
  }
}

initHalo();
requestAnimationFrame(loop);
loadSessions(false).catch((e) => showToast("could not load sessions: " + e.message));
pollStatus();
{
  const st = $("js-stamp");
  st.textContent = "JS ✓";
  st.title = "scripts loaded and running";
  st.style.color = "#34d399";
}
