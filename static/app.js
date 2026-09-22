// Controller UI behaviour. Everything the server decides at render time
// arrives via window.PICAM, set by the inline block in index.html —
// this file is served statically and is never run through Jinja.
const PICAM   = window.PICAM || {};
const PANTILT = PICAM.pantilt || { available: true, reason: '' };

let currentStep = 5;
let busy        = false;
let scanActive  = false;
let limits      = { pan: { min: -90, max: 90 }, tilt: { min: -90, max: 90 } };

// ── Position display ──────────────────────────────────────────────────────
// Set from /position at startup. When there is no HAT the whole pan/tilt
// surface is removed rather than left in place doing nothing, so every
// movement path checks this before touching the network.
let panTiltAvailable = PANTILT.available;

function applyFixedCameraMode(reason) {
  panTiltAvailable = false;
  scanActive = false;
  ['controls', 'presets-bar', 'drag-hint', 'scan-btn', 'home-btn']
    .forEach(id => { const el = document.getElementById(id); if (el) el.remove(); });
  const badge = document.getElementById('hw-badge');
  badge.textContent = 'fixed camera';
  if (reason) badge.title = reason;
}

// Decided server-side, so this does not wait on /position and cannot flash.
if (!panTiltAvailable) applyFixedCameraMode(PANTILT.reason);

function updatePosition(pan, tilt) {
  document.getElementById('pan-val').textContent  = pan  + '°';
  document.getElementById('tilt-val').textContent = tilt + '°';
  setBar('pan-fill',  pan,  limits.pan.min,  limits.pan.max);
  setBar('tilt-fill', tilt, limits.tilt.min, limits.tilt.max);
}

function setBar(id, value, min, max) {
  const fill   = document.getElementById(id);
  const range  = max - min;
  const center = ((0 - min) / range) * 100;
  const pct    = ((value - min) / range) * 100;
  if (pct >= center) {
    fill.style.left  = center + '%';
    fill.style.width = (pct - center) + '%';
  } else {
    fill.style.left  = pct + '%';
    fill.style.width = (center - pct) + '%';
  }
}

// ── Server-Sent Events — position + motion ────────────────────────────────
let motionTimer = null;

function showMotionBadge() {
  const badge = document.getElementById('motion-badge');
  badge.classList.remove('active');
  void badge.offsetWidth;           // restart animation
  badge.classList.add('active');
  clearTimeout(motionTimer);
  motionTimer = setTimeout(() => badge.classList.remove('active'), 10000);
}

function connectSSE() {
  const liveDot = document.getElementById('live-dot');
  const es = new EventSource('/events');
  es.onopen = () => liveDot.classList.remove('disconnected');
  es.onmessage = e => {
    liveDot.classList.remove('disconnected');
    const d = JSON.parse(e.data);
    updatePosition(d.pan, d.tilt);
  };
  es.addEventListener('motion', () => showMotionBadge());
  es.onerror = () => {
    liveDot.classList.add('disconnected');
    es.close();
    setTimeout(connectSSE, 3000);
  };
}

// ── Scan ──────────────────────────────────────────────────────────────────
function setScanUI(active) {
  if (!panTiltAvailable) return;
  scanActive = active;
  const btn = document.getElementById('scan-btn');
  btn.classList.toggle('scanning', active);
  btn.title = active ? 'Stop scan' : 'Auto-scan';
}

document.getElementById('scan-btn').addEventListener('click', async () => {
  const next = !scanActive;
  setScanUI(next);
  try {
    const res = await fetch('/scan', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled: next })
    });
    if (!res.ok) setScanUI(!next);
  } catch(_) { setScanUI(!next); }
});

// ── Move ──────────────────────────────────────────────────────────────────
async function move(direction, step) {
  if (!panTiltAvailable) return;
  if (scanActive) setScanUI(false);
  if (busy) return;
  busy = true;
  const btn = document.querySelector(`.dpad-btn[data-dir="${direction}"]`);
  if (btn) btn.classList.add('pressed');
  try {
    await fetch('/move', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ direction, step: step ?? currentStep })
    });
  } catch (_) {}
  if (btn) btn.classList.remove('pressed');
  busy = false;
}

async function goTo(pan, tilt) {
  if (!panTiltAvailable) return;
  if (scanActive) setScanUI(false);
  try {
    await fetch('/goto', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ pan, tilt })
    });
  } catch (_) {}
}

// ── Home ──────────────────────────────────────────────────────────────────
document.getElementById('home-btn').addEventListener('click', async () => {
  if (scanActive) setScanUI(false);
  try { await fetch('/home', { method: 'POST' }); } catch (_) {}
});

// ── Snapshot ──────────────────────────────────────────────────────────────
document.getElementById('snapshot-btn').addEventListener('click', () => {
  const a = document.createElement('a');
  a.href = '/snapshot';
  a.click();
});

// ── D-pad hold-to-accelerate ──────────────────────────────────────────────
const HOLD_DELAY    = 350;   // ms before auto-repeat starts
const HOLD_INTERVAL = 100;   // ms between repeat steps

let holdDir   = null;
let holdStart = 0;
let holdTimer = null;

function accelStep() {
  const ms = Date.now() - holdStart;
  if (ms > 1500) return Math.min(currentStep * 3, 90);
  if (ms > 700)  return Math.min(currentStep * 2, 90);
  return currentStep;
}

async function fireHold(initial) {
  if (!holdDir) return;
  await move(holdDir, accelStep());
  if (holdDir) holdTimer = setTimeout(() => fireHold(false), initial ? HOLD_DELAY : HOLD_INTERVAL);
}

function startHold(dir) {
  if (!panTiltAvailable) return;
  stopHold();
  holdDir   = dir;
  holdStart = Date.now();
  fireHold(true);
}

function stopHold() {
  holdDir = null;
  clearTimeout(holdTimer);
  holdTimer = null;
}

document.querySelectorAll('.dpad-btn[data-dir]').forEach(btn => {
  btn.addEventListener('pointerdown',  e => { e.preventDefault(); startHold(btn.dataset.dir); });
  btn.addEventListener('pointerup',    () => stopHold());
  btn.addEventListener('pointercancel',() => stopHold());
  btn.addEventListener('pointerleave', () => stopHold());
});

// ── Step selector ─────────────────────────────────────────────────────────
document.querySelectorAll('.step-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.step-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    currentStep = parseInt(btn.dataset.step);
  });
});

// ── Keyboard ──────────────────────────────────────────────────────────────
const keyMap = { ArrowUp:'up', ArrowDown:'down', ArrowLeft:'left', ArrowRight:'right' };

document.addEventListener('keydown', e => {
  if (!panTiltAvailable) return;
  if (e.key === 'Escape') {
    if (scanActive) {
      setScanUI(false);
      fetch('/scan', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({enabled:false}) });
    }
    stopHold();
    return;
  }
  const dir = keyMap[e.key];
  if (!dir || e.repeat) return;
  e.preventDefault();
  startHold(dir);
});

document.addEventListener('keyup', e => {
  if (!panTiltAvailable) return;
  if (keyMap[e.key]) stopHold();
});

// ── Drag to pan/tilt ──────────────────────────────────────────────────────
const feedWrap  = document.getElementById('feed-wrap');
const dragHint  = document.getElementById('drag-hint');
const PX_PER_STEP = 25;
let drag = null;

feedWrap.addEventListener('pointerdown', e => {
  if (!panTiltAvailable) return;
  if (e.target.closest('button')) return;
  drag = { lastX: e.clientX, lastY: e.clientY, accX: 0, accY: 0 };
  feedWrap.setPointerCapture(e.pointerId);
  feedWrap.classList.add('dragging');
  dragHint.style.opacity = '0';
  e.preventDefault();
});

feedWrap.addEventListener('pointermove', e => {
  if (!panTiltAvailable) return;
  if (!drag) return;
  drag.accX += e.clientX - drag.lastX;
  drag.accY += e.clientY - drag.lastY;
  drag.lastX = e.clientX;
  drag.lastY = e.clientY;

  if (Math.abs(drag.accX) >= PX_PER_STEP) {
    move(drag.accX > 0 ? 'right' : 'left', currentStep);
    drag.accX = 0;
  }
  if (Math.abs(drag.accY) >= PX_PER_STEP) {
    move(drag.accY > 0 ? 'down' : 'up', currentStep);
    drag.accY = 0;
  }
});

['pointerup', 'pointercancel'].forEach(ev =>
  feedWrap.addEventListener(ev, () => {
    drag = null;
    feedWrap.classList.remove('dragging');
  })
);

// ── Fullscreen ────────────────────────────────────────────────────────────
const FS_EXPAND   = `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M8 3H5a2 2 0 00-2 2v3m18 0V5a2 2 0 00-2-2h-3m0 18h3a2 2 0 002-2v-3M3 16v3a2 2 0 002 2h3"/></svg>`;
const FS_COMPRESS = `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M8 3v3a2 2 0 01-2 2H3m18 0h-3a2 2 0 01-2-2V3m0 18v-3a2 2 0 012-2h3M3 16h3a2 2 0 012 2v3"/></svg>`;

document.getElementById('fullscreen-btn').addEventListener('click', () => {
  if (!document.fullscreenElement) {
    document.documentElement.requestFullscreen().catch(() => {});
  } else {
    document.exitFullscreen().catch(() => {});
  }
});

document.addEventListener('fullscreenchange', () => {
  const btn = document.getElementById('fullscreen-btn');
  const inFS = !!document.fullscreenElement;
  btn.title     = inFS ? 'Exit fullscreen' : 'Fullscreen';
  btn.innerHTML = inFS ? FS_COMPRESS : FS_EXPAND;
});

// ── Digital zoom ──────────────────────────────────────────────────────────
const ZOOM_STEPS = [1, 1.5, 2, 2.5, 3];
let zoomIdx = 0;

function applyZoom() {
  feed.style.transform = `scale(${ZOOM_STEPS[zoomIdx]})`;
  document.getElementById('zoom-level').textContent = ZOOM_STEPS[zoomIdx] + '×';
}

document.getElementById('zoom-in-btn').addEventListener('click', () => {
  if (zoomIdx < ZOOM_STEPS.length - 1) { zoomIdx++; applyZoom(); }
});
document.getElementById('zoom-out-btn').addEventListener('click', () => {
  if (zoomIdx > 0) { zoomIdx--; applyZoom(); }
});

// Pinch-to-zoom
let pinchDist0 = null, pinchIdx0 = 0;
feedWrap.addEventListener('touchstart', e => {
  if (e.touches.length === 2) {
    pinchDist0 = Math.hypot(e.touches[0].clientX - e.touches[1].clientX,
                            e.touches[0].clientY - e.touches[1].clientY);
    pinchIdx0  = zoomIdx;
  }
}, { passive: true });
feedWrap.addEventListener('touchmove', e => {
  if (pinchDist0 === null || e.touches.length !== 2) return;
  const dist   = Math.hypot(e.touches[0].clientX - e.touches[1].clientX,
                            e.touches[0].clientY - e.touches[1].clientY);
  const target = ZOOM_STEPS[pinchIdx0] * (dist / pinchDist0);
  let best = 0, bestD = Infinity;
  ZOOM_STEPS.forEach((z, i) => { const d = Math.abs(z - target); if (d < bestD) { bestD = d; best = i; } });
  if (best !== zoomIdx) { zoomIdx = best; applyZoom(); }
}, { passive: true });
feedWrap.addEventListener('touchend', () => { pinchDist0 = null; });

// ── Gallery ───────────────────────────────────────────────────────────────
let galleryFiles = [];

const VIDEO_ICON = `<svg width="30" height="30" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="12" cy="12" r="10"/><path d="M10 8l6 4-6 4z" fill="currentColor"/></svg>`;

const esc = v => String(v).replace(/[&<>"']/g, c =>
  ({ '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;' }[c]));

// Paths are "<source>/<relative path>" — encode each segment, keep the slashes
const mediaUrl = path => '/gallery/' + path.split('/').map(encodeURIComponent).join('/');

function openGallery() {
  document.getElementById('gallery-overlay').classList.add('open');
  showGalleryGrid();
  loadGallery();
}

function closeGallery() {
  document.getElementById('gallery-overlay').classList.remove('open');
}

async function loadGallery() {
  try {
    const res = await fetch('/gallery');
    if (!res.ok) return;
    galleryFiles = await res.json();
    renderGalleryGrid();
  } catch (_) {}
}

function renderGalleryGrid() {
  const grid  = document.getElementById('gallery-grid');
  const empty = document.getElementById('gallery-empty');
  document.getElementById('gallery-hdr-title').textContent = `Gallery (${galleryFiles.length})`;
  if (galleryFiles.length === 0) {
    grid.style.display  = 'none';
    empty.style.display = 'flex';
    return;
  }
  grid.style.display  = '';
  empty.style.display = 'none';
  grid.innerHTML = galleryFiles.map((f, i) => {
    // No poster frames for movies yet — show a play marker instead of
    // pulling a whole video over the network just to fill a 4:3 tile.
    const body = f.kind === 'video'
      ? `<div class="thumb-poster">${esc(f.name)}</div><div class="thumb-video">${VIDEO_ICON}</div>`
      : `<img src="${esc(mediaUrl(f.path))}" loading="lazy" alt="">`;
    const tag = f.source === 'buffer' ? '<span class="thumb-tag buffer">pending</span>' : '';
    return `<div class="gallery-thumb" data-idx="${i}">${body}${tag}</div>`;
  }).join('');
  grid.querySelectorAll('.gallery-thumb').forEach(t =>
    t.addEventListener('click', () => showGalleryItem(Number(t.dataset.idx)))
  );
}

function showGalleryGrid() {
  const video = document.getElementById('gallery-viewer-video');
  video.pause();
  video.removeAttribute('src');
  video.load();
  document.getElementById('gallery-grid').style.display    = '';
  document.getElementById('gallery-empty').style.display   = 'none';
  document.getElementById('gallery-viewer').style.display  = 'none';
  document.getElementById('gallery-back-btn').style.display = 'none';
  document.getElementById('gallery-dl-btn').style.display   = 'none';
  document.getElementById('gallery-hdr-title').textContent  = `Gallery (${galleryFiles.length})`;
}

function showGalleryItem(idx) {
  const f = galleryFiles[idx];
  if (!f) return;
  const url   = mediaUrl(f.path);
  const img   = document.getElementById('gallery-viewer-img');
  const video = document.getElementById('gallery-viewer-video');

  document.getElementById('gallery-grid').style.display    = 'none';
  document.getElementById('gallery-empty').style.display   = 'none';
  document.getElementById('gallery-viewer').style.display  = 'flex';
  document.getElementById('gallery-back-btn').style.display = '';
  const dl = document.getElementById('gallery-dl-btn');
  dl.style.display = '';
  dl.href = url;
  dl.setAttribute('download', f.name);
  document.getElementById('gallery-hdr-title').textContent = f.name;

  if (f.kind === 'video') {
    img.removeAttribute('src');
    img.style.display   = 'none';
    video.style.display = '';
    video.src = url;
  } else {
    video.pause();
    video.removeAttribute('src');
    video.style.display = 'none';
    img.style.display   = '';
    img.src = url;
  }
}

document.getElementById('gallery-btn').addEventListener('click', openGallery);
document.getElementById('gallery-close-btn').addEventListener('click', closeGallery);
document.getElementById('gallery-back-btn').addEventListener('click', showGalleryGrid);
document.getElementById('gallery-overlay').addEventListener('keydown', e => {
  if (e.key === 'Escape') closeGallery();
});

// ── Presets (4 fixed slots) ───────────────────────────────────────────────
const SLOT_KEYS = ['P1', 'P2', 'P3', 'P4'];
let presetsData = {};

function renderPresets() {
  const container = document.getElementById('presets-slots');
  if (!container) return;
  container.innerHTML = SLOT_KEYS.map(key => {
    const pos    = presetsData[key];
    const filled = !!pos;
    const posStr = filled ? `${pos.pan}° / ${pos.tilt}°` : 'not set';
    return `
      <div class="preset-slot${filled ? ' filled' : ''}" data-key="${key}">
        <div class="slot-header">
          <span class="slot-label">${key}</span>
          <button class="slot-clear" data-key="${key}" title="Clear preset">×</button>
        </div>
        <div class="slot-pos">${posStr}</div>
        <div class="slot-btns">
          <button class="slot-btn go-btn" data-key="${key}" ${filled ? '' : 'disabled'}>Go</button>
          <button class="slot-btn save-btn" data-key="${key}">Save</button>
        </div>
      </div>
    `;
  }).join('');

  container.querySelectorAll('.go-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const pos = presetsData[btn.dataset.key];
      if (pos) goTo(pos.pan, pos.tilt);
    });
  });

  container.querySelectorAll('.save-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      try {
        const res = await fetch('/presets', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name: btn.dataset.key })
        });
        if (res.ok) { presetsData = await res.json(); renderPresets(); }
      } catch (_) {}
    });
  });

  container.querySelectorAll('.slot-clear').forEach(btn => {
    btn.addEventListener('click', async () => {
      if (!presetsData[btn.dataset.key]) return;
      try {
        const res = await fetch(`/presets/${encodeURIComponent(btn.dataset.key)}`, { method: 'DELETE' });
        if (res.ok) { presetsData = await res.json(); renderPresets(); }
      } catch (_) {}
    });
  });
}

// ── Storage status ────────────────────────────────────────────────────────
const fmtSize = b => {
  if (b === null || b === undefined) return '?';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0, v = b;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${v < 10 && i > 0 ? v.toFixed(1) : Math.round(v)}${units[i]}`;
};

async function refreshStorage() {
  const badge = document.getElementById('nas-badge');
  try {
    const res = await fetch('/storage');
    if (!res.ok) return;
    const s = await res.json();
    badge.style.display = '';
    if (!s.enabled) {
      // Offload is off, so nothing drains the buffer — its fill level is the
      // only thing worth showing, and it is what shedding acts on.
      const pct = s.buffer.used_pct;
      badge.textContent = pct === null ? `BUF ${s.buffer.files}` : `BUF ${pct}%`;
      const hot = pct !== null && pct >= 60;
      badge.style.color = badge.style.borderColor =
        hot ? 'var(--danger)' : 'var(--text-muted)';
    } else if (s.mounted) {
      const backlog = s.queued || s.buffer.files || 0;
      badge.textContent  = backlog ? `NAS ${backlog} queued` : 'NAS ok';
      badge.style.color       = backlog ? 'var(--text)' : 'var(--accent)';
      badge.style.borderColor = backlog ? 'var(--border)' : 'var(--accent)';
    } else {
      badge.textContent = 'NAS down';
      badge.style.color = badge.style.borderColor = 'var(--danger)';
    }
    badge.title = [
      s.enabled ? `Archive: ${s.archive_dir || '—'}` : 'NFS offload: disabled',
      s.enabled ? `Mounted: ${s.mounted ? 'yes' : 'no'}` : null,
      `Buffer: ${s.buffer.files} file(s), ${s.buffer.used_pct ?? '?'}% of ${fmtSize(s.buffer.total)} used`,
      `Uploaded: ${s.uploaded}   Failed: ${s.failed}   Discarded: ${s.dropped}`,
      s.archive.free !== null ? `NAS free: ${fmtSize(s.archive.free)}` : null,
      s.last_error ? `Last error: ${s.last_error}` : null
    ].filter(Boolean).join('\n');
  } catch (_) {}
}

// ── Init ──────────────────────────────────────────────────────────────────
async function init() {
  try {
    const [limRes, posRes, presetsRes] = await Promise.all([
      fetch('/limits'), fetch('/position'), fetch('/presets')
    ]);
    if (limRes.ok)     limits = await limRes.json();
    if (posRes.ok) {
      const d  = await posRes.json();
      // `pantilt` is newer than `hardware`; fall back so an older server still works
      const pt = d.pantilt || { available: d.hardware, reason: '' };
      if (pt.available) {
        updatePosition(d.pan, d.tilt);
        if (d.scan) setScanUI(true);
        document.getElementById('hw-badge').textContent = 'HAT ready';
      } else {
        applyFixedCameraMode(pt.reason);
      }
    }
    if (presetsRes.ok && panTiltAvailable) {
      presetsData = await presetsRes.json();
      renderPresets();
    }
  } catch (_) {}

  setTimeout(() => { dragHint.style.opacity = '0'; }, 4000);
  connectSSE();
  refreshStorage();
  setInterval(refreshStorage, 30000);
}

// ── Stream error handling ─────────────────────────────────────────────────
const feed       = document.getElementById('feed');
const offlineMsg = document.getElementById('offline-msg');

feed.addEventListener('error', () => {
  offlineMsg.style.display = 'flex';
  feed.style.display = 'none';
  setTimeout(() => {
    feed.src = '/stream?' + Date.now();
    feed.style.display = '';
    offlineMsg.style.display = 'none';
  }, 3000);
});

// ── Service worker ────────────────────────────────────────────────────────
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/sw.js').catch(() => {});
}

init();
