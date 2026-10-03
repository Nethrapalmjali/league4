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

  var periodGuide = document.querySelector("[data-period-guide]");
  var periodBadge = document.querySelector("[data-period-badge]");
  var periodText = document.querySelector("[data-period-text]");
  var periodUndoBtn = document.querySelector("[data-period-undo]");
  var periodUndoBarBtn = document.querySelector("[data-period-undo-bar]");
  var prevPeriodTags = document.querySelectorAll("[data-prev-period-tag]");
  var periodNextBtns = document.querySelectorAll("[data-period-next], [data-next-period]");
  var nextPeriodTags = document.querySelectorAll("[data-next-period-tag], [data-next-period-bar-tag]");

  var currentSport = body.dataset.matchSport || "";
  var currentPeriod = body.dataset.matchPeriod || "H1";
  var currentPrevPeriod = null;
  var currentNextPeriod = null;
  var periodsList = [];
  try {
    periodsList = JSON.parse(body.dataset.matchPeriods || "[]");
  } catch (e) {
    periodsList = [];
  }

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

    // Dynamic half-time suggestion check for football in H1
    if ((currentSport || "").trim().toLowerCase() === "football" && currentPeriod === "H1") {
      var guideInfo = getGuideInfo();
      if (periodText && periodText.innerHTML !== guideInfo.text) {
        periodText.innerHTML = guideInfo.text;
      }
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

  /* ---------------- Smart Period Assistant & Auto-Suggestions ---------------- */

  var PERIOD_GUIDES = {
    basketball: {
      "Q1": {
        badge: "1st Quarter (Q1)",
        text: "🏀 <strong>1st Quarter in progress.</strong> Track Free Throws (1pt), 2-Pointers (2pt), 3-Pointers (3pt), and fouls. Advance to Q2 when buzzer sounds."
      },
      "Q2": {
        badge: "2nd Quarter (Q2)",
        text: "🏀 <strong>2nd Quarter in progress.</strong> Half-time follows Q2. Advance to Q3 when 2nd quarter concludes."
      },
      "Q3": {
        badge: "3rd Quarter (Q3)",
        text: "🏀 <strong>3rd Quarter underway.</strong> Advance to Q4 for the final quarter."
      },
      "Q4": {
        badge: "4th Quarter (Q4)",
        text: "🏀 <strong>4th Quarter (Final Quarter).</strong> Tap <strong>End match</strong> when the final buzzer sounds (or Advance to OT if tied)."
      },
      "OT": {
        badge: "Overtime (OT)",
        text: "🏀 <strong>Overtime underway!</strong> Scores level after regulation. Tap <strong>End match</strong> when OT finishes."
      }
    },
    football: {
      H1: {
        badge: "1st Half (H1)",
        text: "⚽ <strong>1st Half in progress.</strong> When the 45' half-time whistle blows, tap <strong>Advance to H2 →</strong>.",
        halfTimeText: "⏱️ <strong>45' reached!</strong> When the referee blows the half-time whistle, tap <strong>Advance to H2 →</strong>."
      },
      H2: {
        badge: "2nd Half (H2)",
        text: "⚽ <strong>2nd Half in progress (45'–90'+).</strong> When the full-time whistle blows, tap <strong>End match</strong>."
      }
    },
    cricket: {
      "Innings 1": {
        badge: "1st Innings",
        text: "🏏 <strong>1st Innings in progress.</strong> When all out or target overs complete, tap <strong>Advance to Innings 2 →</strong>."
      },
      "Innings 2": {
        badge: "2nd Innings (Chase)",
        text: "🏏 <strong>2nd Innings underway (Target Chase).</strong> When target is chased or overs complete, tap <strong>End match</strong>."
      }
    },
    kabaddi: {
      H1: {
        badge: "1st Half (20m)",
        text: "🤼 <strong>1st Half underway (20 mins).</strong> Track touches, bonuses, tackles, and all-outs. Advance to H2 at half-time."
      },
      H2: {
        badge: "2nd Half (20m)",
        text: "🤼 <strong>2nd Half underway.</strong> Tap <strong>End match</strong> at the final whistle."
      }
    },
    badminton: {
      "Game 1": {
        badge: "Game 1 (To 21)",
        text: "🏸 <strong>Game 1 in progress (First to 21, win by 2).</strong> Tap <strong>+1 Point</strong> for rallies."
      },
      "Game 2": {
        badge: "Game 2",
        text: "🏸 <strong>Game 2 in progress.</strong> Tap <strong>+1 Point</strong> on rallies. Auto-advances or ends match on 2-0."
      },
      "Game 3": {
        badge: "Game 3 (Decider)",
        text: "🏸 <strong>Deciding Game 3 underway!</strong> Tap <strong>End match</strong> when match point is won."
      }
    }
  };

  function getGuideInfo() {
    var sportKey = (currentSport || "").trim().toLowerCase();
    var sportGuide = PERIOD_GUIDES[sportKey] || {};
    var guide = sportGuide[currentPeriod];

    var badgeText = guide ? guide.badge : ("Period " + currentPeriod);
    var guideText = "";

    if (sportKey === "football" && currentPeriod === "H1" && getElapsedSec() >= 45 * 60 && guide && guide.halfTimeText) {
      guideText = guide.halfTimeText;
    } else if (guide && guide.text) {
      guideText = guide.text;
    } else if (currentNextPeriod) {
      guideText = "Current period: <strong>" + currentPeriod + "</strong>. Tap <strong>Advance to " + currentNextPeriod + " →</strong> when this period concludes.";
    } else {
      guideText = "Final period: <strong>" + currentPeriod + "</strong>. Tap <strong>End match</strong> when the match concludes.";
    }

    return { badge: badgeText, text: guideText };
  }

  function updatePeriodAssistant(period, sport, prevP, nextP, status) {
    if (period) currentPeriod = period;
    if (sport) currentSport = sport;
    if (typeof prevP !== "undefined") currentPrevPeriod = prevP;
    if (typeof nextP !== "undefined") currentNextPeriod = nextP;

    var curStatus = status || body.dataset.matchStatus || "scheduled";
    if (curStatus === "final") {
      if (periodBadge) periodBadge.textContent = "Match Finished";
      if (periodText) periodText.innerHTML = "🏆 <strong>Match has concluded.</strong> Scores and statistics are finalized.";
      if (periodUndoBtn) periodUndoBtn.hidden = true;
      if (periodUndoBarBtn) periodUndoBarBtn.hidden = true;
      periodNextBtns.forEach(function (btn) {
        btn.disabled = true;
        btn.style.display = "none";
      });
      return;
    }

    var info = getGuideInfo();
    if (periodBadge) periodBadge.textContent = info.badge;
    if (periodText) periodText.innerHTML = info.text;

    // Undo period buttons: visible whenever a previous period is available
    if (currentPrevPeriod) {
      if (periodUndoBtn) {
        periodUndoBtn.hidden = false;
        periodUndoBtn.disabled = false;
      }
      if (periodUndoBarBtn) {
        periodUndoBarBtn.hidden = false;
        periodUndoBarBtn.disabled = false;
        periodUndoBarBtn.textContent = "↶ Undo to " + currentPrevPeriod;
      }
      prevPeriodTags.forEach(function (el) { el.textContent = currentPrevPeriod; });
    } else {
      if (periodUndoBtn) periodUndoBtn.hidden = true;
      if (periodUndoBarBtn) periodUndoBarBtn.hidden = true;
    }

    // Next period buttons
    if (currentNextPeriod) {
      periodNextBtns.forEach(function (btn) {
        btn.disabled = false;
        btn.style.display = "";
        if (btn.classList.contains("sc-btn-period-next-bar")) {
          btn.innerHTML = 'Advance to <span data-next-period-bar-tag>' + currentNextPeriod + '</span> →';
        } else if (btn.classList.contains("sc-btn-period-next")) {
          btn.innerHTML = 'Advance to <span data-next-period-tag>' + currentNextPeriod + '</span> →';
        }
      });
      nextPeriodTags.forEach(function (el) { el.textContent = currentNextPeriod; });
    } else {
      // Last period reached (e.g. H2 in Football)
      periodNextBtns.forEach(function (btn) {
        btn.disabled = true;
        btn.textContent = "Final Period (" + currentPeriod + ")";
      });
    }
  }

  function handleNextPeriod() {
    var sportKey = (currentSport || "").trim().toLowerCase();
    // Football special assistance when moving H1 -> H2
    if (sportKey === "football" && currentPeriod === "H1") {
      var sec = getElapsedSec();
      if (sec < 45 * 60) {
        var set45 = window.confirm(
          "Advancing to 2nd Half (H2).\n\nDo you want to set the Match Clock to 45:00 for the start of the 2nd half?"
        );
        if (set45) {
          var targetSec = 45 * 60;
          if (isRunning) {
            startTime = Date.now() - (targetSec * 1000);
          } else {
            pausedSec = targetSec;
          }
          saveTimerState();
          updateTimerUi();
        }
      }
    }

    post(body.dataset.periodUrl, { action: "next" })
      .then(redraw)
      .catch(fail);
  }

  function handleUndoPeriod() {
    var targetPrev = currentPrevPeriod || "previous period";
    if (!window.confirm("Undo period change and return to " + targetPrev + "?")) return;

    post(body.dataset.periodUrl, { action: "prev" })
      .then(redraw)
      .catch(fail);
  }

  // Initial timer & period assistant setup on page load
  loadTimerState();
  if (isRunning) {
    timerInterval = setInterval(updateTimerUi, 1000);
  }
  updateTimerUi();

  var curIdx = periodsList.indexOf(currentPeriod);
  if (curIdx === -1 && periodsList.length > 0) curIdx = 0;
  var initPrev = curIdx > 0 ? periodsList[curIdx - 1] : null;
  var initNext = curIdx < periodsList.length - 1 ? periodsList[curIdx + 1] : null;
  updatePeriodAssistant(currentPeriod, currentSport, initPrev, initNext, initialStatus);

  /* ---------------- Redraw & Scoring Event Actions ---------------- */

  var cricketNextBallEl = document.querySelector("[data-cricket-next-ball]");
  var cricketEquationEl = document.querySelector("[data-cricket-equation]");
  var badmintonGamesEl = document.querySelector("[data-badminton-games]");
  var badmintonSetsEl = document.querySelector("[data-badminton-sets]");
  var kabaddiLeadEl = document.querySelector("[data-kabaddi-lead]");
  var subscoreA = document.querySelector("[data-subscore-a]");
  var subscoreB = document.querySelector("[data-subscore-b]");
  var roleBadgeA = document.querySelector('[data-role-badge="a"]');
  var roleBadgeB = document.querySelector('[data-role-badge="b"]');

  function updateSportSpecificUI(data) {
    var sport = (data.sport || currentSport || "").toLowerCase();

    // Basketball UI
    if (sport === "basketball" && data.basketball_stats) {
      var bk = data.basketball_stats;
      var basketballLeadEl = document.querySelector("[data-basketball-lead]");
      if (basketballLeadEl) {
        basketballLeadEl.textContent = "🏀 " + (bk.lead_text || "Match in progress");
      }
      if (subscoreA) subscoreA.textContent = (bk.a ? bk.a.total : data.score_a) + " pts";
      if (subscoreB) subscoreB.textContent = (bk.b ? bk.b.total : data.score_b) + " pts";
      if (clockEl && !isManualMinute && isRunning) {
        var minText = timerBadge ? timerBadge.textContent : "";
        clockEl.value = (data.period || "Q1") + (minText ? (" " + minText) : "");
      }
    }

    // 1. Cricket UI
    if (sport === "cricket" && data.cricket_stats) {
      var cs = data.cricket_stats;
      var isInn1 = (data.period === "Innings 1" || !data.period);
      var currentInn = isInn1 ? cs.inn1 : cs.inn2;
      var nextB = currentInn ? currentInn.next_ball : "0.1 ov";

      if (cricketNextBallEl) cricketNextBallEl.textContent = "Next Ball: " + nextB;
      if (cricketEquationEl) cricketEquationEl.textContent = cs.equation || (isInn1 ? "1st Innings in progress" : "Target Chase");

      if (subscoreA && cs.inn1) subscoreA.textContent = "(" + cs.inn1.wickets + " wkts, " + cs.inn1.overs + ")";
      if (subscoreB && cs.inn2) subscoreB.textContent = "(" + cs.inn2.wickets + " wkts, " + cs.inn2.overs + ")";

      if (roleBadgeA && roleBadgeB) {
        if (isInn1) {
          roleBadgeA.textContent = "🏏 BATTING NOW";
          roleBadgeA.className = "sc-cricket-role-badge sc-cricket-role-badge--batting";
          roleBadgeB.textContent = "⚾ BOWLING";
          roleBadgeB.className = "sc-cricket-role-badge sc-cricket-role-badge--bowling";
        } else {
          roleBadgeA.textContent = "⚾ BOWLING";
          roleBadgeA.className = "sc-cricket-role-badge sc-cricket-role-badge--bowling";
          roleBadgeB.textContent = "🏏 BATTING NOW";
          roleBadgeB.className = "sc-cricket-role-badge sc-cricket-role-badge--batting";
        }
      }

      if (clockEl && !isManualMinute) {
        clockEl.value = nextB;
      }
    }

    // 2. Badminton UI
    if (sport === "badminton" && data.badminton_stats) {
      var bs = data.badminton_stats;
      if (badmintonGamesEl) {
        badmintonGamesEl.textContent = "Games: " + bs.games_a + " – " + bs.games_b;
      }
      if (badmintonSetsEl) {
        badmintonSetsEl.textContent = bs.sets_summary ? ("Sets: " + bs.sets_summary) : (bs.active_game + " in progress");
      }
      if (subscoreA) subscoreA.textContent = bs.games_a + " Game" + (bs.games_a !== 1 ? "s" : "") + " won";
      if (subscoreB) subscoreB.textContent = bs.games_b + " Game" + (bs.games_b !== 1 ? "s" : "") + " won";

      if (bs.match_winner) {
        showError("🏆 Match Won by Team " + bs.match_winner.toUpperCase() + "! (" + bs.games_a + "–" + bs.games_b + ") Tap 'End match' to finalize.");
      } else if (bs.game_point_a) {
        if (periodBadge) periodBadge.textContent = "⚠️ Game Point Team A";
      } else if (bs.game_point_b) {
        if (periodBadge) periodBadge.textContent = "⚠️ Game Point Team B";
      }

      if (clockEl && !isManualMinute) {
        clockEl.value = data.period || bs.active_game || "Game 1";
      }
    }

    // 3. Kabaddi UI
    if (sport === "kabaddi" && data.kabaddi_stats) {
      var ks = data.kabaddi_stats;
      if (kabaddiLeadEl) {
        kabaddiLeadEl.textContent = "🤼 " + (ks.lead_text || "Match in progress");
      }
      if (clockEl && !isManualMinute && isRunning) {
        clockEl.value = timerBadge ? timerBadge.textContent : "1'";
      }
    }
  }

  function redraw(data) {
    showError("");
    if (scoreA) scoreA.textContent = data.score_a;
    if (scoreB) scoreB.textContent = data.score_b;
    if (periodEl) periodEl.textContent = data.period || "Not started";

    // Update sport-specific widgets
    updateSportSpecificUI(data);

    // Update smart period assistant
    updatePeriodAssistant(
      data.period,
      data.sport || currentSport,
      data.prev_period,
      data.next_period,
      data.status || body.dataset.matchStatus
    );

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

  if (periodUndoBtn) {
    periodUndoBtn.addEventListener("click", handleUndoPeriod);
  }
  if (periodUndoBarBtn) {
    periodUndoBarBtn.addEventListener("click", handleUndoPeriod);
  }

  periodNextBtns.forEach(function (btn) {
    btn.addEventListener("click", handleNextPeriod);
  });

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
