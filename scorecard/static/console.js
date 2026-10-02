/* Scoring console with live match timer and manual minute override.
   Every tap posts one event and redraws from the server's reply, so the screen
   always shows what was actually saved rather than an optimistic guess. */
(function () {
  "use strict";

  var body = document.body;
  if (!body.dataset.eventUrl) return;

  var csrf = body.dataset.csrf;
  var matchId = body.dataset.match;
  var initialStatus = body.dataset.matchStatus || "scheduled";
  var startsAtIso = body.dataset.startsAt;
  var storageKey = "gml_timer_" + matchId;

  var scoreA = document.querySelector("[data-score-a]");
  var scoreB = document.querySelector("[data-score-b]");
  var periodEl = document.querySelector("[data-period]");
  var logEl = document.querySelector("[data-log]");
  var errorEl = document.querySelector("[data-error]");
  var clockEl = document.querySelector("[data-clock]");

  var timerDigital = document.querySelector("[data-timer-digital]");
  var timerBadge = document.querySelector("[data-timer-badge]");
  var timerStatus = document.querySelector("[data-timer-status]");
  var timerToggleBtn = document.querySelector("[data-timer-toggle]");
  var timerSetBtn = document.querySelector("[data-timer-set]");
  var timerPrompt = document.querySelector("[data-timer-prompt]");
  var clockBadge = document.querySelector("[data-clock-badge]");
  var snapBtn = document.querySelector("[data-snap-live]");

  var timerInterval = null;
  var startTime = null;
  var pausedSec = 0;
  var isRunning = false;
  var isManualMinute = false;

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

  /* ---------------- Match Clock & Minute Auto-Sync ---------------- */

  function loadTimerState() {
    var raw = localStorage.getItem(storageKey);
    if (raw) {
      try {
        var data = JSON.parse(raw);
        if (data.isRunning && data.startTime) {
          startTime = data.startTime;
          isRunning = true;
        } else if (typeof data.pausedSec === "number") {
          pausedSec = data.pausedSec;
          isRunning = false;
        }
      } catch (e) {}
    } else if (initialStatus === "live" && startsAtIso) {
      var d = new Date(startsAtIso).getTime();
      var elapsed = Math.floor((Date.now() - d) / 1000);
      if (elapsed > 0 && elapsed < 3 * 3600) {
        startTime = d;
        isRunning = true;
      }
    }
  }

  function saveTimerState() {
    try {
      localStorage.setItem(
        storageKey,
        JSON.stringify({
          isRunning: isRunning,
          startTime: startTime,
          pausedSec: pausedSec
        })
      );
    } catch (e) {}
  }

  function getElapsedSec() {
    if (isRunning && startTime) {
      return Math.max(0, Math.floor((Date.now() - startTime) / 1000));
    }
    return pausedSec || 0;
  }

  function updateTimerUi() {
    var sec = getElapsedSec();
    var m = Math.floor(sec / 60);
    var s = sec % 60;
    var formattedTime = (m < 10 ? "0" : "") + m + ":" + (s < 10 ? "0" : "") + s;
    var currentMin = (m + 1) + "'";

    if (timerDigital) timerDigital.textContent = formattedTime;
    if (timerBadge) timerBadge.textContent = currentMin;

    if (isRunning) {
      if (timerStatus) {
        timerStatus.textContent = "🟢 Running";
        timerStatus.className = "sc-timer__state-tag sc-timer__state-tag--running";
      }
      if (timerToggleBtn) {
        timerToggleBtn.textContent = "⏸ Pause Clock";
        timerToggleBtn.classList.add("sc-timer__btn-toggle--pause");
      }
      if (timerPrompt) timerPrompt.hidden = true;
    } else if (pausedSec > 0) {
      if (timerStatus) {
        timerStatus.textContent = "⏸ Paused";
        timerStatus.className = "sc-timer__state-tag sc-timer__state-tag--paused";
      }
      if (timerToggleBtn) {
        timerToggleBtn.textContent = "▶ Resume Clock";
        timerToggleBtn.classList.remove("sc-timer__btn-toggle--pause");
      }
      if (timerPrompt) timerPrompt.hidden = true;
    } else {
      if (timerStatus) {
        timerStatus.textContent = "Not Started";
        timerStatus.className = "sc-timer__state-tag";
      }
      if (timerToggleBtn) {
        timerToggleBtn.textContent = "▶ Start Match Clock";
        timerToggleBtn.classList.remove("sc-timer__btn-toggle--pause");
      }
      if (timerPrompt) timerPrompt.hidden = false;
    }

    // Auto-update the minute input box if user hasn't manually edited it
    if (!isManualMinute && clockEl) {
      clockEl.value = currentMin;
      if (clockBadge) {
        clockBadge.textContent = "Live Sync";
        clockBadge.className = "sc-clock__status-badge";
      }
      if (snapBtn) snapBtn.hidden = true;
    }
  }

  function startClock() {
    startTime = Date.now() - (pausedSec * 1000);
    isRunning = true;
    saveTimerState();
    if (!timerInterval) {
      timerInterval = setInterval(updateTimerUi, 1000);
    }
    updateTimerUi();

    // If match was scheduled, transition it to live on server
    if (body.dataset.matchStatus === "scheduled" && body.dataset.statusUrl) {
      post(body.dataset.statusUrl, { status: "live" })
        .then(function (data) {
          body.dataset.matchStatus = "live";
          redraw(data);
        })
        .catch(function () {});
    }
  }

  function pauseClock() {
    pausedSec = getElapsedSec();
    isRunning = false;
    startTime = null;
    if (timerInterval) {
      clearInterval(timerInterval);
      timerInterval = null;
    }
    saveTimerState();
    updateTimerUi();
  }

  function setMinutePrompt() {
    var current = Math.floor(getElapsedSec() / 60) + 1;
    var res = window.prompt("Set Match Clock Minute (e.g. 15 for 15'):", current);
    if (res === null) return;
    var parsed = parseInt(res.replace(/[^0-9]/g, ""), 10);
    if (!isNaN(parsed) && parsed >= 1 && parsed <= 180) {
      var targetSec = (parsed - 1) * 60;
      if (isRunning) {
        startTime = Date.now() - (targetSec * 1000);
      } else {
        pausedSec = targetSec;
      }
      saveTimerState();
      updateTimerUi();
    }
  }

  if (timerToggleBtn) {
    timerToggleBtn.addEventListener("click", function () {
      if (isRunning) {
        pauseClock();
      } else {
        startClock();
      }
    });
  }

  if (timerSetBtn) {
    timerSetBtn.addEventListener("click", setMinutePrompt);
  }

  if (clockEl) {
    // When the scorer types/edits in the minute field, respect their manual change!
    clockEl.addEventListener("input", function () {
      isManualMinute = true;
      if (clockBadge) {
        clockBadge.textContent = "✏️ Edited";
        clockBadge.className = "sc-clock__status-badge sc-clock__status-badge--manual";
      }
      if (snapBtn) snapBtn.hidden = false;
    });
  }

  if (snapBtn) {
    snapBtn.addEventListener("click", function () {
      isManualMinute = false;
      updateTimerUi();
    });
  }

  // Initial timer setup on page load
  loadTimerState();
  if (isRunning) {
    timerInterval = setInterval(updateTimerUi, 1000);
  }
  updateTimerUi();

  /* ---------------- Redraw & Scoring Event Actions ---------------- */

  function redraw(data) {
    showError("");
    if (scoreA) scoreA.textContent = data.score_a;
    if (scoreB) scoreB.textContent = data.score_b;
    if (periodEl) periodEl.textContent = data.period || "Not started";

    // After successfully submitting an event, reset manual minute so subsequent events track live clock
    if (isManualMinute) {
      isManualMinute = false;
      updateTimerUi();
    }

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
          pauseClock();
          try {
            localStorage.removeItem(storageKey);
          } catch (e) {}
          if (timerStatus) {
            timerStatus.textContent = "Full Time";
            timerStatus.className = "sc-timer__state-tag";
          }
          if (timerToggleBtn) timerToggleBtn.disabled = true;
          if (timerSetBtn) timerSetBtn.disabled = true;
        })
        .catch(fail);
    });
  }
})();
