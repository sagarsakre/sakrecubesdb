const CACHE_NAME = 'reelshot-shell-v1';
const MEDIA_CACHE = 'reelshot-media-v1';

const SHELL_ASSETS = [
  './',
  './index.html',
  './manifest.json',
  './icons/icon-192.png',
  './icons/icon-512.png',
  './icons/apple-touch-icon.png',
  './data/reels.json'
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(SHELL_ASSETS))
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys.map((key) => {
          if (key !== CACHE_NAME && key !== MEDIA_CACHE) {
            return caches.delete(key);
          }
        })
      )
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);

  // Cache-first for GIFs, images, and reel JSONs
  if (
    url.pathname.endsWith('.gif') ||
    url.pathname.endsWith('.jpg') ||
    url.pathname.endsWith('.jpeg') ||
    url.pathname.endsWith('.png') ||
    url.pathname.endsWith('.json')
  ) {
    event.respondWith(
      caches.open(MEDIA_CACHE).then(async (cache) => {
        const cachedResponse = await cache.match(event.request);
        if (cachedResponse) {
          return cachedResponse;
        }
        try {
          const networkResponse = await fetch(event.request);
          if (networkResponse && networkResponse.status === 200) {
            cache.put(event.request, networkResponse.clone());
          }
          return networkResponse;
        } catch (err) {
          return cachedResponse;
        }
      })
    );
    return;
  }

  // Network first with cache fallback for HTML and app shell
  event.respondWith(
    fetch(event.request)
      .then((networkResponse) => {
        if (networkResponse && networkResponse.status === 200) {
          const copy = networkResponse.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(event.request, copy));
        }
        return networkResponse;
      })
      .catch(() => caches.match(event.request))
  );
});

// Message listener to delete specific reel cache
self.addEventListener('message', async (event) => {
  if (event.data && event.data.type === 'PURGE_REEL') {
    const reelPrefix = event.data.reelPrefix;
    const cache = await caches.open(MEDIA_CACHE);
    const requests = await cache.keys();
    for (const req of requests) {
      if (req.url.includes(reelPrefix)) {
        await cache.delete(req);
      }
    }
    if (event.ports && event.ports[0]) {
      event.ports[0].postMessage({ success: true });
    }
  }
});
