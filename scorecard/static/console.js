/* Scoring console.
   Every tap posts one event and redraws from the server's reply, so the screen
   always shows what was actually saved rather than an optimistic guess. */
(function () {
  "use strict";

  var body = document.body;
  if (!body.dataset.eventUrl) return;

  var csrf = body.dataset.csrf;
  var scoreA = document.querySelector("[data-score-a]");
  var scoreB = document.querySelector("[data-score-b]");
  var periodEl = document.querySelector("[data-period]");
  var logEl = document.querySelector("[data-log]");
  var errorEl = document.querySelector("[data-error]");
  var clockEl = document.querySelector("[data-clock]");

  function showError(message) {
    if (!errorEl) return;
    errorEl.textContent = message;
    errorEl.hidden = !message;
  }

  function post(url, fields) {
    var form = new FormData();
    Object.keys(fields || {}).forEach(function (key) {
      if (fields[key] !== null && fields[key] !== undefined && fields[key] !== "") {
        form.append(key, fields[key]);
      }
    });
    return fetch(url, {
      method: "POST",
      body: form,
      headers: { "X-CSRF-Token": csrf },
      credentials: "same-origin"
    }).then(function (response) {
      return response.json().then(function (data) {
        if (!response.ok) throw new Error(data.error || "That did not save.");
        return data;
      });
    });
  }

  function redraw(data) {
    showError("");
    if (scoreA) scoreA.textContent = data.score_a;
    if (scoreB) scoreB.textContent = data.score_b;
    if (periodEl) periodEl.textContent = data.period || "Not started";
    if (!logEl) return;

    logEl.innerHTML = "";
    if (!data.events.length) {
      var empty = document.createElement("li");
      empty.className = "sc-feed__empty";
      empty.textContent = "Nothing recorded yet.";
      logEl.appendChild(empty);
      return;
    }
    data.events.forEach(function (event) {
      var item = document.createElement("li");
      item.className = "sc-feed__item sc-feed__item--" + event.side;

      var clock = document.createElement("span");
      clock.className = "sc-feed__clock";
      clock.textContent = event.clock || "—";

      var what = document.createElement("span");
      what.className = "sc-feed__what";
      var strong = document.createElement("strong");
      strong.textContent = event.kind;
      what.appendChild(strong);
      if (event.player) {
        var who = document.createElement("span");
        who.className = "sc-feed__who";
        who.textContent = event.player;
        what.appendChild(who);
      }

      item.appendChild(clock);
      item.appendChild(what);
      logEl.appendChild(item);
    });
  }

  function fail(error) {
    showError(error.message || "Could not reach the server. Check the connection.");
  }

  document.querySelectorAll(".sc-action").forEach(function (button) {
    button.addEventListener("click", function () {
      var side = button.dataset.side;
      var picker = document.querySelector('[data-player="' + side + '"]');
      button.disabled = true;
      post(body.dataset.eventUrl, {
        side: side,
        action: button.dataset.action,
        player_id: picker ? picker.value : "",
        clock: clockEl ? clockEl.value.trim() : ""
      })
        .then(redraw)
        .catch(fail)
        .then(function () { button.disabled = false; });
    });
  });

  var undo = document.querySelector("[data-undo]");
  if (undo) {
    undo.addEventListener("click", function () {
      post(body.dataset.undoUrl, {}).then(redraw).catch(fail);
    });
  }

  var nextPeriod = document.querySelector("[data-next-period]");
  if (nextPeriod) {
    nextPeriod.addEventListener("click", function () {
      post(body.dataset.periodUrl, {}).then(redraw).catch(fail);
    });
  }

  var finish = document.querySelector("[data-finish]");
  if (finish) {
    finish.addEventListener("click", function () {
      if (!window.confirm("End this match? The public page will show it as finished.")) return;
      post(body.dataset.statusUrl, { status: "final" })
        .then(function (data) {
          redraw(data);
          finish.disabled = true;
          finish.textContent = "Match finished";
        })
        .catch(fail);
    });
  }
})();
