/* Public match page: poll the cached JSON so the score keeps up on its own.
   Includes live celebratory animation for goals/points, real-time scorers list,
   and Player of the Match updates. Stops once the match is finished. */
(function () {
  "use strict";

  var page = document.querySelector("[data-feed]");
  if (!page) return;

  var url = page.dataset.feed;
  var scoreA = page.querySelector("[data-score-a]");
  var scoreB = page.querySelector("[data-score-b]");
  var stateEl = page.querySelector("[data-state]");
  var listEl = page.querySelector("[data-feed-list]");
  var scorersAEl = page.querySelector("[data-scorers-a]");
  var scorersBEl = page.querySelector("[data-scorers-b]");
  var toastEl = page.querySelector("[data-celebration-toast]");
  var toastIcon = page.querySelector("[data-toast-icon]");
  var toastTitle = page.querySelector("[data-toast-title]");
  var toastSub = page.querySelector("[data-toast-sub]");

  var lastScoreA = scoreA ? parseInt(scoreA.textContent.trim(), 10) || 0 : 0;
  var lastScoreB = scoreB ? parseInt(scoreB.textContent.trim(), 10) || 0 : 0;
  var isInitialLoad = true;
  var toastTimer = null;

  function playCelebrationChime() {
    try {
      var AudioCtx = window.AudioContext || window.webkitAudioContext;
      if (!AudioCtx) return;
      var ctx = new AudioCtx();
      var now = ctx.currentTime;

      // Simple major triad chime: C5 (523Hz), E5 (659Hz), G5 (784Hz)
      var notes = [523.25, 659.25, 783.99];
      notes.forEach(function (freq, i) {
        var osc = ctx.createOscillator();
        var gain = ctx.createGain();
        osc.type = "sine";
        osc.frequency.setValueAtTime(freq, now + i * 0.12);
        gain.gain.setValueAtTime(0, now + i * 0.12);
        gain.gain.linearRampToValueAtTime(0.18, now + i * 0.12 + 0.02);
        gain.gain.exponentialRampToValueAtTime(0.001, now + i * 0.12 + 0.35);
        osc.connect(gain);
        gain.connect(ctx.destination);
        osc.start(now + i * 0.12);
        osc.stop(now + i * 0.12 + 0.4);
      });
    } catch (e) {}
  }

  function triggerCelebration(title, sub, icon) {
    if (!toastEl) return;
    if (toastIcon) toastIcon.textContent = icon || "⚽";
    if (toastTitle) toastTitle.textContent = title || "SCORE!";
    if (toastSub) toastSub.textContent = sub || "";

    toastEl.hidden = false;
    toastEl.classList.remove("sc-toast--hide");
    toastEl.classList.add("sc-toast--show");

    playCelebrationChime();

    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(function () {
      toastEl.classList.remove("sc-toast--show");
      toastEl.classList.add("sc-toast--hide");
      setTimeout(function () {
        toastEl.hidden = true;
      }, 500);
    }, 4500);
  }

  function updateScorersList(el, scorers) {
    if (!el || !scorers) return;
    el.innerHTML = "";
    scorers.forEach(function (s) {
      var pill = document.createElement("span");
      pill.className = "sc-scorer-pill";
      var txt = "⚽ " + s.name;
      if (s.clock) {
        txt += " <small>" + s.clock + "</small>";
      }
      pill.innerHTML = txt;
      el.appendChild(pill);
    });
  }

  function render(data) {
    var newA = data.team_a ? data.team_a.score : 0;
    var newB = data.team_b ? data.team_b.score : 0;

    // Detect score increase and celebrate!
    if (!isInitialLoad && (newA > lastScoreA || newB > lastScoreB)) {
      var latest = data.events && data.events.length ? data.events[0] : null;
      var team = newA > lastScoreA ? data.team_a.tag : data.team_b.tag;
      var player = latest && latest.player ? latest.player : team;
      var kind = latest && latest.kind ? latest.kind.toUpperCase() : "POINT!";
      var clock = latest && latest.clock ? " (" + latest.clock + ")" : "";
      var icon = data.sport === "Basketball" ? "🏀" : data.sport === "Kabaddi" ? "🔥" : data.sport === "Badminton" ? "🏸" : "⚽";

      triggerCelebration(kind + "!", player + clock + " · " + team, icon);
    }

    lastScoreA = newA;
    lastScoreB = newB;
    isInitialLoad = false;

    if (scoreA) scoreA.textContent = newA;
    if (scoreB) scoreB.textContent = newB;

    if (stateEl) {
      if (data.status === "live") {
        stateEl.innerHTML =
          '<span class="sc-live">Live' +
          (data.period ? " · " + data.period : "") +
          "</span>";
      } else if (data.status === "final") {
        stateEl.innerHTML = '<span class="sc-final">Full time</span>';
      }
    }

    // Update scorers pills under teams
    if (data.scorers_a) updateScorersList(scorersAEl, data.scorers_a);
    if (data.scorers_b) updateScorersList(scorersBEl, data.scorers_b);

    if (listEl && data.events) {
      listEl.innerHTML = "";
      if (!data.events.length) {
        listEl.innerHTML = '<li class="sc-feed__empty">Nothing recorded yet.</li>';
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
          who.textContent =
            event.player + (event.jersey ? " (" + event.jersey + ")" : "");
          what.appendChild(who);
        }

        var team = document.createElement("span");
        team.className = "sc-feed__team";
        team.textContent = event.team || "";

        item.appendChild(clock);
        item.appendChild(what);
        item.appendChild(team);
        listEl.appendChild(item);
      });
    }

    if (data.status === "final") {
      window.clearInterval(timer);
    }
  }

  // Initial setup
  isInitialLoad = false;

  var timer = window.setInterval(function () {
    fetch(url, { headers: { Accept: "application/json" } })
      .then(function (response) {
        return response.ok ? response.json() : null;
      })
      .then(function (data) {
        if (data) render(data);
      })
      .catch(function () {});
  }, 6000);
})();
