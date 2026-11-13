const CACHE = "health-hub-v1";
self.addEventListener("install", event => event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(["/", "/static/styles.css", "/static/app.js", "/static/manifest.webmanifest", "/static/icon.svg"]))));
self.addEventListener("fetch", event => { if (event.request.method === "GET") event.respondWith(fetch(event.request).catch(() => caches.match(event.request))); });
