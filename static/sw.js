const CACHE = 'picam-v1';

// Pre-cache the app shell only — no dynamic/camera content
const SHELL = ['/', '/static/manifest.json', '/static/icon.svg', '/static/favicon.svg'];

// Routes that must always go to the network
const BYPASS = ['/stream', '/snapshot', '/gallery', '/events', '/motion-event', '/move',
                '/goto', '/home', '/scan', '/presets', '/position', '/limits', '/login', '/logout'];

self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE)
      .then(c => c.addAll(SHELL))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));

self.addEventListener('fetch', e => {
  const path = new URL(e.request.url).pathname;

  // Always hit the network for live data
  if (BYPASS.some(p => path.startsWith(p))) {
    e.respondWith(fetch(e.request));
    return;
  }

  // Network-first with cache fallback for the app shell
  e.respondWith(
    fetch(e.request)
      .then(res => {
        const copy = res.clone();
        caches.open(CACHE).then(c => c.put(e.request, copy));
        return res;
      })
      .catch(() => caches.match(e.request))
  );
});
