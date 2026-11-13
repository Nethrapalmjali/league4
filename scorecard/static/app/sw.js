/* GM League Scorer — service worker.
 *
 * Keeps the app itself on the device so it opens with no signal at all.
 * App files are served from the device first (instant, works offline) and
 * refreshed in the background whenever the network answers, so a new version
 * arrives on the next launch without anyone doing anything.
 *
 * The scoring API is never cached here: the app keeps its own queue of
 * changes and decides when to send them.
 */
"use strict";

var CACHE = "gml-scorer-v1";
var SHELL = [
  "/scores/app",
  "/scores/static/app/app.js",
  "/scores/static/app/app.css",
  "/scores/static/app/icon-192.png",
  "/scores/static/app/icon-512.png",
  "/scores/app.webmanifest"
];

self.addEventListener("install", function (event) {
  event.waitUntil(
    caches.open(CACHE).then(function (cache) {
      // Fetched with cache: "reload" so a stale HTTP-cached copy is never baked in.
      return Promise.all(SHELL.map(function (url) {
        return fetch(new Request(url, { cache: "reload", credentials: "same-origin" }))
          .then(function (res) { if (res.ok) return cache.put(url, res); });
      }));
    }).then(function () { return self.skipWaiting(); })
  );
});

self.addEventListener("activate", function (event) {
  event.waitUntil(
    caches.keys().then(function (keys) {
      return Promise.all(keys.filter(function (k) {
        return k.indexOf("gml-scorer-") === 0 && k !== CACHE;
      }).map(function (k) { return caches.delete(k); }));
    }).then(function () { return self.clients.claim(); })
  );
});

function shellKey(url) {
  // /scores/app?anything and /scores/app/ all open the same app.
  if (url.pathname === "/scores/app" || url.pathname === "/scores/app/") return "/scores/app";
  if (SHELL.indexOf(url.pathname) !== -1) return url.pathname;
  return null;
}

self.addEventListener("fetch", function (event) {
  var req = event.request;
  if (req.method !== "GET") return;
  var url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  var key = shellKey(url);
  if (!key) return; // API calls and everything else go straight to the network

  event.respondWith(
    caches.open(CACHE).then(function (cache) {
      return cache.match(key).then(function (cached) {
        var refresh = fetch(req, { cache: "no-cache" }).then(function (res) {
          if (res && res.ok) cache.put(key, res.clone());
          return res;
        }).catch(function () { return null; });

        if (cached) {
          event.waitUntil(refresh);
          return cached;
        }
        return refresh.then(function (res) {
          return res || new Response(
            "<h1>Offline</h1><p>Open the Scorer App once with internet so it can save itself to this device.</p>",
            { status: 503, headers: { "Content-Type": "text/html" } }
          );
        });
      });
    })
  );
});
