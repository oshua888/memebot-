// Cache hors ligne : l'appli s'ouvre même sans réseau (seule la synchro de groupe en a besoin).
const CACHE = "tournee-v9";
const FILES = ["./", "./index.html", "./mqtt.min.js", "./cartes.js", "./manifest.webmanifest", "./icon-192.png", "./icon-512.png"];
self.addEventListener("install", e => { e.waitUntil(caches.open(CACHE).then(c => c.addAll(FILES))); self.skipWaiting(); });
self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))));
  self.clients.claim();
});
const appPage = () => caches.match("./index.html").then(r => r || caches.match("./"));
self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin) return;
  if (url.pathname.endsWith("/version.json")) return;   // toujours en direct
  // Ouverture de l'appli : jamais de page d'erreur, on affiche l'appli à la place
  if (e.request.mode === "navigate") {
    e.respondWith(
      fetch(e.request)
        .then(r => (r.ok || r.type === "opaqueredirect") ? r : appPage().then(c => c || r))
        .catch(() => appPage().then(c => c || Response.error()))
    );
    return;
  }
  // Fichiers : réseau d'abord (pour recevoir les mises à jour), cache en secours. On ne garde jamais une erreur.
  e.respondWith(
    fetch(e.request).then(r => { if (r.ok) { const copy = r.clone(); caches.open(CACHE).then(c => c.put(e.request, copy)); } return r; })
      .catch(() => caches.match(e.request, { ignoreSearch: true }).then(r => r || appPage()))
  );
});
