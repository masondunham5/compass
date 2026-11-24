const CACHE = "compass-v15";

self.addEventListener("install", event => {
  event.waitUntil(
    caches.open(CACHE)
      .then(cache => cache.addAll(["/", "/static/styles.css", "/static/app.js", "/static/manifest.webmanifest", "/static/icon.svg"]))
      .then(() => self.skipWaiting())
  );
});

// Remove old app shells so an older HTML page can never be paired with a newer script.
self.addEventListener("activate", event => {
  event.waitUntil(
    caches.keys()
      .then(names => Promise.all(names.filter(name => (name.startsWith("health-hub-") || name.startsWith("compass-")) && name !== CACHE).map(name => caches.delete(name))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", event => {
  if (event.request.method !== "GET") return;
  event.respondWith(fetch(event.request).catch(() => caches.match(event.request)));
});
