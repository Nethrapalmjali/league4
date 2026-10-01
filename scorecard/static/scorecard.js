/* Public match page: poll the cached JSON so the score keeps up on its own.
   Stops once the match is finished — there is nothing left to fetch. */
(function () {
  "use strict";

  var page = document.querySelector("[data-feed]");
  if (!page) return;

  var url = page.dataset.feed;
  var scoreA = page.querySelector("[data-score-a]");
  var scoreB = page.querySelector("[data-score-b]");
  var stateEl = page.querySelector("[data-state]");
  var listEl = page.querySelector("[data-feed-list]");

  if (page.dataset.status === "final") return;

  function render(data) {
    if (scoreA) scoreA.textContent = data.team_a.score;
    if (scoreB) scoreB.textContent = data.team_b.score;

    if (stateEl) {
      if (data.status === "live") {
        stateEl.innerHTML = '<span class="sc-live">Live' +
          (data.period ? " · " + data.period : "") + "</span>";
      } else if (data.status === "final") {
        stateEl.innerHTML = '<span class="sc-final">Full time</span>';
      }
    }

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
          who.textContent = event.player + (event.jersey ? " (" + event.jersey + ")" : "");
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

  var timer = window.setInterval(function () {
    fetch(url, { headers: { Accept: "application/json" } })
      .then(function (response) { return response.ok ? response.json() : null; })
      .then(function (data) { if (data) render(data); })
      .catch(function () { /* a dropped poll corrects itself on the next tick */ });
  }, 8000);
})();
