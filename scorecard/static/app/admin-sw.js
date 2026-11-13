/* GM League Admin — service worker for the whole site (scope "/").
 *
 * Deliberately does almost nothing. The admin website needs the server, so
 * nothing is cached and every request goes to the network exactly as before.
 * The one job: when a page cannot load because there is no internet, show a
 * clear page with a way into the Scorer App (which does work offline) instead
 * of the browser's error screen.
 *
 * Only plain page loads (GET navigations) are touched. Form posts, uploads,
 * API calls, images and scripts are never intercepted.
 */
"use strict";

var OFFLINE_HTML =
  '<!doctype html><html lang="en"><head><meta charset="utf-8">' +
  '<meta name="viewport" content="width=device-width,initial-scale=1">' +
  '<meta name="theme-color" content="#5b1f1f"><title>Offline · GM League</title>' +
  '<style>body{margin:0;min-height:100vh;display:grid;place-items:center;padding:1.2rem;box-sizing:border-box;' +
  'font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;background:#5b1f1f;color:#fff;text-align:center}' +
  '.c{max-width:420px}h1{font-size:1.5rem;margin:.6rem 0}p{opacity:.9;line-height:1.5}' +
  'a,button{display:block;width:100%;box-sizing:border-box;margin-top:.8rem;padding:.95rem;border-radius:12px;border:0;' +
  'font:inherit;font-weight:800;text-decoration:none;cursor:pointer}' +
  '.g{background:#e3b559;color:#5b1f1f}.o{background:rgba(255,255,255,.14);color:#fff}</style></head>' +
  '<body><div class="c"><div style="font-size:2.6rem">📶</div><h1>No internet</h1>' +
  '<p>The admin website needs a connection. Scoring does not: the Scorer App keeps working ' +
  'and uploads by itself when the signal returns.</p>' +
  '<a class="g" href="/scores/app">Open the Scorer App</a>' +
  '<button class="o" onclick="location.reload()">Try again</button></div></body></html>';

function offlinePage() {
  return new Response(OFFLINE_HTML, {
    status: 503,
    headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" }
  });
}

self.addEventListener("install", function () { self.skipWaiting(); });

self.addEventListener("activate", function (event) {
  event.waitUntil((async function () {
    // Lets the page request start while this worker is still waking up, so
    // being in the middle costs no time.
    if (self.registration.navigationPreload) {
      try { await self.registration.navigationPreload.enable(); } catch (e) {}
    }
    await self.clients.claim();
  })());
});

self.addEventListener("fetch", function (event) {
  var req = event.request;
  if (req.mode !== "navigate" || req.method !== "GET") return;
  event.respondWith((async function () {
    try {
      var preloaded = await event.preloadResponse;
      if (preloaded) return preloaded;
      return await fetch(req);
    } catch (e) {
      return offlinePage();
    }
  })());
});
