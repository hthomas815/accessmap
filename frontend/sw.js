const CACHE_VERSION = 'v3';
const APP_CACHE  = `accessmap-app-${CACHE_VERSION}`;
const TILE_CACHE = `accessmap-tiles-${CACHE_VERSION}`;
const API_CACHE  = `accessmap-api-${CACHE_VERSION}`;
const MAX_TILES  = 2000; // ~10 MB of map tiles

const APP_SHELL = [
  '/',
  'https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css',
  'https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js',
  'https://cdnjs.cloudflare.com/ajax/libs/leaflet.markercluster/1.5.3/MarkerCluster.css',
  'https://cdnjs.cloudflare.com/ajax/libs/leaflet.markercluster/1.5.3/MarkerCluster.Default.css',
  'https://cdnjs.cloudflare.com/ajax/libs/leaflet.markercluster/1.5.3/leaflet.markercluster.js',
];

// ── Install: cache the app shell ──────────────────────────────────────────────

self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(APP_CACHE)
      .then(cache => cache.addAll(APP_SHELL).catch(() => {})) // don't block on CDN hiccups
      .then(() => self.skipWaiting())
  );
});

// ── Activate: purge old caches ────────────────────────────────────────────────

self.addEventListener('activate', e => {
  const keep = [APP_CACHE, TILE_CACHE, API_CACHE];
  e.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => !keep.includes(k)).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

// ── Fetch: route requests to the right strategy ───────────────────────────────

self.addEventListener('fetch', e => {
  const { request } = e;
  if (request.method !== 'GET') return;

  const url = new URL(request.url);

  // OSM tiles — cache-first, evict oldest when full
  if (url.hostname.endsWith('tile.openstreetmap.org')) {
    e.respondWith(tileStrategy(request));
    return;
  }

  // API calls — network-first so data stays fresh; fall back to cached JSON
  if (url.pathname.startsWith('/api/')) {
    e.respondWith(networkFirst(request, API_CACHE));
    return;
  }

  // App shell + CDN assets — cache-first
  e.respondWith(cacheFirst(request, APP_CACHE));
});

async function tileStrategy(request) {
  const cache  = await caches.open(TILE_CACHE);
  const cached = await cache.match(request);
  if (cached) return cached;
  try {
    const res = await fetch(request);
    if (res.ok) {
      const keys = await cache.keys();
      if (keys.length >= MAX_TILES) await cache.delete(keys[0]);
      cache.put(request, res.clone());
    }
    return res;
  } catch {
    return new Response('', { status: 503 });
  }
}

async function networkFirst(request, cacheName) {
  const cache = await caches.open(cacheName);
  try {
    const res = await fetch(request.clone());
    if (res.ok) cache.put(request, res.clone());
    return res;
  } catch {
    const hit = await cache.match(request);
    // Return cached data or an empty array so the app doesn't crash offline
    return hit || new Response('[]', { headers: { 'Content-Type': 'application/json' } });
  }
}

async function cacheFirst(request, cacheName) {
  const cache  = await caches.open(cacheName);
  const cached = await cache.match(request);
  if (cached) return cached;
  try {
    const res = await fetch(request);
    if (res.ok) cache.put(request, res.clone());
    return res;
  } catch {
    return new Response('Offline', { status: 503 });
  }
}

// ── Tile prefetch messages from the main thread ───────────────────────────────

self.addEventListener('message', async e => {
  if (e.data?.type !== 'PREFETCH_TILES') return;

  const { urls } = e.data;
  const cache    = await caches.open(TILE_CACHE);
  let done = 0;

  const BATCH = 8; // parallel fetches
  for (let i = 0; i < urls.length; i += BATCH) {
    await Promise.allSettled(
      urls.slice(i, i + BATCH).map(async url => {
        try {
          if (await cache.match(url)) { done++; return; } // already cached
          const res = await fetch(url);
          if (res.ok) {
            const keys = await cache.keys();
            if (keys.length >= MAX_TILES) await cache.delete(keys[0]);
            await cache.put(url, res);
          }
        } catch {}
        done++;
      })
    );
    // Progress update to all open clients
    const clients = await self.clients.matchAll();
    clients.forEach(c => c.postMessage({ type: 'PREFETCH_PROGRESS', done, total: urls.length }));
  }

  const clients = await self.clients.matchAll();
  clients.forEach(c => c.postMessage({ type: 'PREFETCH_DONE', total: urls.length }));
});
