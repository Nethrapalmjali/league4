/* GM League Admin — install button and in-app behaviour for the admin pages.
 *
 * Markup it looks for (all optional):
 *   [data-admin-install-card]  shown in a browser, hidden inside the installed app
 *   [data-admin-install]       the Install button; shown once the browser offers it
 *   [data-admin-install-hint]  how to install when there is no button (iPhone, menus)
 */
(function () {
  "use strict";

  var standalone = window.matchMedia("(display-mode: standalone)").matches || window.navigator.standalone === true;
  var deferred = null;
  var ios = /iphone|ipad|ipod/i.test(navigator.userAgent);

  if ("serviceWorker" in navigator) {
    // The admin app spans the whole site; this worker only adds a friendly
    // offline page (see admin-sw.js).
    navigator.serviceWorker.register("/scores/admin-sw.js", { scope: "/" }).catch(function () {});
    // Also keep the Scorer App stored on this device, so it opens at the
    // ground even if it was never opened here with internet.
    navigator.serviceWorker.register("/scores/sw.js", { scope: "/scores/app" }).catch(function () {});
  }

  function each(selector, fn) {
    Array.prototype.forEach.call(document.querySelectorAll(selector), fn);
  }

  function refresh() {
    each("[data-admin-install-card]", function (card) { card.hidden = standalone; });
    each("[data-admin-install]", function (btn) { btn.hidden = !deferred; });
    each("[data-admin-install-hint]", function (el) {
      if (deferred) { el.textContent = ""; return; }
      el.textContent = ios
        ? "On iPhone / iPad: open this page in Safari, tap the Share button, then “Add to Home Screen”."
        : "No Install button? Open the browser menu (⋮) and choose “Install app” or “Add to Home screen”.";
    });
  }

  function keepLinksInApp() {
    // Inside the installed app there is no browser around the page, so a link
    // that opens a new tab would throw the user out of the app. Same-site
    // links stay in the app window instead.
    if (!standalone) return;
    each('a[target="_blank"]', function (a) {
      try {
        if (new URL(a.href, location.href).origin === location.origin && !a.hasAttribute("download")) {
          a.removeAttribute("target");
        }
      } catch (e) {}
    });
  }

  window.addEventListener("beforeinstallprompt", function (e) {
    e.preventDefault();
    deferred = e;
    refresh();
  });
  window.addEventListener("appinstalled", function () {
    deferred = null;
    each("[data-admin-install-card]", function (card) { card.hidden = true; });
  });
  document.addEventListener("click", function (e) {
    var btn = e.target.closest ? e.target.closest("[data-admin-install]") : null;
    if (!btn || !deferred) return;
    deferred.prompt();
    deferred.userChoice.then(function () { deferred = null; refresh(); });
  });

  function init() { refresh(); keepLinksInApp(); }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
