const CACHE = 'picam-v3';

// Pre-cache the app shell only — no dynamic/camera content.
// The CSS and JS used to be inline in '/', so caching the page was enough;
// now they are separate files and have to be listed explicitly or an offline
// load gets the markup with no styles and no behaviour.
const SHELL = ['/', '/static/manifest.json', '/static/icon.svg', '/static/favicon.svg',
               '/static/base.css', '/static/app.css', '/static/app.js'];

// Routes that must always go to the network
const BYPASS = ['/stream', '/snapshot', '/gallery', '/events', '/motion-event', '/move',
                '/goto', '/home', '/scan', '/presets', '/position', '/limits', '/login', '/logout',
                '/storage', '/media-saved'];

self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE)
      .then(c => c.addAll(SHELL))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', e => e.waitUntil(
  caches.keys()
    .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
    .then(() => self.clients.claim())
));

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
      // ignoreSearch so a cache-busted '?v=<hash>' URL still matches the
      // unversioned copy pre-cached above when the network is gone.
      .catch(() => caches.match(e.request, { ignoreSearch: true }))
  );
});
