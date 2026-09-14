// Deliberately network-first, not cache-first.
//
// This app deploys on every push to master (.github/workflows/deploy.yml) -
// a cache-first service worker would happily keep serving yesterday's JS to
// an installed user for as long as the cache lived, including past a real
// bug fix going out. The only thing cached here is a fallback for when the
// network genuinely isn't there, which is also the entire justification for
// having a service worker at all: it's one of the two things (this, plus a
// manifest) a browser requires before it will offer "Install app" - there is
// no ambition here beyond meeting that bar and degrading gracefully offline.

const CACHE = "oa-shell-v1";
const SHELL_URL = "/ui";

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then((cache) => cache.add(SHELL_URL))
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((names) =>
      Promise.all(names.filter((n) => n !== CACHE).map((n) => caches.delete(n)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  // Only ever intercept the app shell navigation itself. Every API call,
  // every asset, everything else goes straight to the network untouched -
  // this is not a general-purpose cache, and profile/opportunity data must
  // never be served stale from here.
  if (event.request.mode !== "navigate") return;

  event.respondWith(
    fetch(event.request).catch(() =>
      caches.match(SHELL_URL).then((cached) => cached || Response.error())
    )
  );
});
