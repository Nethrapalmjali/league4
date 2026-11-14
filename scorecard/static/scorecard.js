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
      // Built with text nodes, never innerHTML: player names come from the
      // public registration form and must not be able to inject markup.
      var pill = document.createElement("span");
      pill.className = "sc-scorer-pill";
      pill.appendChild(document.createTextNode((s.icon || "⚽") + " "));
      if (s.player_id) {
        var link = document.createElement("a");
        link.href = "/scores/player/" + Number(s.player_id);
        link.className = "sc-player-link";
        link.textContent = s.name;
        pill.appendChild(link);
      } else {
        pill.appendChild(document.createTextNode(s.name));
      }
      if (s.clock) {
        pill.appendChild(document.createTextNode(" "));
        var small = document.createElement("small");
        small.textContent = s.clock;
        pill.appendChild(small);
      }
      el.appendChild(pill);
    });
  }

  // Badminton round table: scores, winners and statuses as rounds go on.
  var STATUS_TEXT = { ongoing: "Playing", not_required: "Not required", not_played: "Not played", upcoming: "Upcoming" };
  function renderRounds(data) {
    var box = page.querySelector("[data-badminton-rounds]");
    if (!box) return;
    var bd = data.badminton;
    var tags = { a: data.team_a ? data.team_a.tag : "A", b: data.team_b ? data.team_b.tag : "B" };
    var line = page.querySelector("[data-badminton-line]");
    if (line) line.textContent = bd.live_text || "";
    var tally = box.querySelector("[data-round-tally]");
    if (tally) tally.textContent = bd.tally;
    var badge = box.querySelector("[data-round-badge]");
    if (badge) {
      var playing = !bd.result_text && data.status === "live";
      badge.textContent = bd.result_text ? "🏆 " + bd.result_text
        : (playing ? bd.active_label + " underway" : (data.status === "final" ? "Finished" : "Not started"));
      badge.classList.toggle("sc-cricket-inn-badge--live", playing);
    }
    var won = { a: 0, b: 0 };
    bd.rounds.forEach(function (r) {
      if (r.status === "completed") won[r.winner] += 1;
      var row = box.querySelector('[data-round="' + r.number + '"]');
      if (!row) return;
      var blank = (r.status === "upcoming" || r.status === "not_required") && !r.a && !r.b;
      var ca = row.querySelector("[data-ra]"), cb = row.querySelector("[data-rb]"), cs = row.querySelector("[data-rstatus]");
      ca.textContent = blank ? "–" : r.a;
      cb.textContent = blank ? "–" : r.b;
      ca.style.fontWeight = r.winner === "a" ? "900" : ""; ca.style.color = r.winner === "a" ? "#1b8751" : "";
      cb.style.fontWeight = r.winner === "b" ? "900" : ""; cb.style.color = r.winner === "b" ? "#1b8751" : "";
      row.style.opacity = (r.status === "not_required" || r.status === "not_played") ? ".55" : "";
      cs.textContent = "";
      var label = document.createElement("span");
      if (r.status === "completed") {
        label.style.color = "#1b8751"; label.style.fontWeight = "800";
        label.textContent = "✓ Won by " + tags[r.winner];
      } else if (r.status === "ongoing") {
        label.className = "sc-cricket-inn-badge sc-cricket-inn-badge--live";
        label.textContent = STATUS_TEXT.ongoing;
      } else {
        label.style.color = "var(--muted)";
        label.textContent = STATUS_TEXT[r.status] || "";
      }
      cs.appendChild(label);
    });
    var wa = box.querySelector("[data-rwon-a]"), wb = box.querySelector("[data-rwon-b]");
    if (wa) wa.textContent = won.a;
    if (wb) wb.textContent = won.b;
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
      stateEl.textContent = "";
      if (data.status === "live") {
        var live = document.createElement("span");
        live.className = "sc-live";
        live.textContent = "Live" + (data.period ? " · " + data.period : "");
        stateEl.appendChild(live);
        // Scorers at the ground may be offline; say so rather than look frozen.
        var mins = data.updated_at ? Math.floor((Date.now() - Date.parse(data.updated_at)) / 60000) : 0;
        if (mins >= 10) {
          var stale = document.createElement("span");
          stale.className = "sc-when";
          stale.textContent = "Last update " + mins + " min ago (ground scores upload when signal allows)";
          stateEl.appendChild(stale);
        }
      } else if (data.status === "final") {
        var fin = document.createElement("span");
        fin.className = "sc-final";
        fin.textContent = "Full time";
        stateEl.appendChild(fin);
      }
    }

    if (data.badminton) renderRounds(data);

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
      page.dataset.status = "final"; // nothing more will change
      stop();
    }
  }

  // Initial setup
  isInitialLoad = false;

  // New scores show within a few seconds. The server shares one answer among
  // all viewers, so asking often is cheap. Paused while the page is hidden
  // (phone locked, other tab) and refreshed the moment it is shown again.
  var POLL_MS = 4000;
  var timer = null;
  function poll() {
    fetch(url, { headers: { Accept: "application/json" } })
      .then(function (response) {
        return response.ok ? response.json() : null;
      })
      .then(function (data) {
        if (data) render(data);
      })
      .catch(function () {});
  }
  function start() {
    if (timer || page.dataset.status === "final") return;
    timer = window.setInterval(poll, POLL_MS);
  }
  function stop() {
    if (timer) window.clearInterval(timer);
    timer = null;
  }
  document.addEventListener("visibilitychange", function () {
    if (document.hidden) { stop(); } else { poll(); start(); }
  });
  start();
})();
