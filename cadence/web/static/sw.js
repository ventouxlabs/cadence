/* Cadence service worker. Served from the origin root so its scope is the whole app: a worker
   under /static/ can never control /today, and offline would quietly do nothing.

   Bump CACHE_VERSION on every change to this file or to anything it precaches. */
const CACHE_VERSION = "cadence-v2";

const PRECACHE = [
  "/static/htmx.min.js",
  "/static/app.js",
  "/static/style.css",
  "/static/manifest.json",
  "/static/done-offline.html",
  "/static/icons/icon.svg",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png"
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_VERSION).then((cache) => cache.addAll(PRECACHE)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((key) => key !== CACHE_VERSION).map((key) => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

function isToday(url) {
  return url.pathname === "/today" || url.pathname.startsWith("/today/") || url.pathname === "/api/today";
}

function isDone(url) {
  return url.pathname.startsWith("/done/");
}

/* Network first for the checklist: a stale worker serving last week's session is worse than a
   spinner. Cache first for /static/, which is versioned by CACHE_VERSION. */
async function networkFirst(request) {
  const cache = await caches.open(CACHE_VERSION);
  try {
    const fresh = await fetch(request);
    if (fresh && fresh.ok) cache.put(request, fresh.clone());
    return fresh;
  } catch (error) {
    const hit = await cache.match(request);
    if (hit) return hit;
    throw error;
  }
}

async function cacheFirst(request) {
  const cache = await caches.open(CACHE_VERSION);
  const hit = await cache.match(request);
  if (hit) return hit;
  const fresh = await fetch(request);
  if (fresh && fresh.ok) cache.put(request, fresh.clone());
  return fresh;
}

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (isToday(url)) {
    event.respondWith(networkFirst(request));
    return;
  }
  if (isDone(url)) {
    /* A Done taken offline navigates here before the queue has reached the server, so there is
       nothing to fetch and nothing cached under this id. The precached shell says so honestly
       rather than letting the browser show its own offline page on the one screen that has to
       feel finished. */
    event.respondWith(
      networkFirst(request).catch(function () {
        return caches.open(CACHE_VERSION).then(function (cache) {
          return cache.match("/static/done-offline.html");
        });
      })
    );
    return;
  }
  if (url.pathname.startsWith("/static/")) {
    event.respondWith(cacheFirst(request));
  }
});
