/* GM League Scorer — offline-first scoring app.
 *
 * The rule everything here follows: a tap is saved on this device first and
 * sent to the server later. The screen never waits for the network.
 *
 *   tap -> change added to the outbox (saved in localStorage)
 *       -> applied to the local copy of the match -> screen redrawn
 *   whenever the server answers -> outbox sent in order -> server confirms
 *       -> confirmed changes leave the outbox -> server's copy replaces ours
 *
 * Each change carries an id made here, so the server applies a re-sent change
 * once. Nothing in the outbox is ever dropped unless the server has answered
 * for it.
 */
(function () {
  "use strict";

  var API = "/scores/api/app";
  var KEY = "gmls.";
  var BATCH = 50;
  var BUNDLE_EVERY_MS = 60 * 1000;
  var root = document.getElementById("app");

  var SPORT_ICON = { Football: "⚽", Basketball: "🏀", Kabaddi: "🤼", Badminton: "🏸", Cricket: "🏏" };

  /* ------------------------------------------------------------ storage -- */
  function load(name, fallback) {
    try {
      var raw = localStorage.getItem(KEY + name);
      return raw ? JSON.parse(raw) : fallback;
    } catch (e) { return fallback; }
  }
  function store(name, value) {
    try {
      localStorage.setItem(KEY + name, JSON.stringify(value));
      return true;
    } catch (e) {
      toast("⚠️ Could not save on this device (storage full?). Download a backup from the menu.");
      return false;
    }
  }

  var S = {
    auth: load("auth", null),            // { token, me: {role, name, scorer_id} }
    matches: load("matches", {}),        // id -> match (local view)
    outbox: load("outbox", []),          // queued changes, oldest first
    timers: load("timers", {}),          // id -> { running, base, paused }
    meta: load("meta", {}),              // { lastBundleAt, lastSyncAt }
    net: "unknown",                      // unknown | online | offline
    syncing: false,
    authProblem: null,
    rejected: [],
    view: { name: "list", matchId: null },
    ui: { clock: {}, player: {}, loginRole: "scorer", loginError: "", busy: false, menu: false },
    installPrompt: null,
    backoff: 0,
    syncTimer: null
  };

  function saveMatches() { store("matches", S.matches); }
  function saveOutbox() { store("outbox", S.outbox); }
  function saveTimers() { store("timers", S.timers); }
  function saveMeta() { store("meta", S.meta); }

  /* ------------------------------------------------------------ helpers -- */
  function esc(v) {
    return String(v === null || v === undefined ? "" : v)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }
  function uid() {
    if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
    var s = "";
    for (var i = 0; i < 32; i++) s += Math.floor(Math.random() * 16).toString(16);
    return s.slice(0, 8) + "-" + s.slice(8, 12) + "-4" + s.slice(13, 16) + "-a" + s.slice(17, 20) + "-" + s.slice(20, 32);
  }
  function isStandalone() {
    return window.matchMedia("(display-mode: standalone)").matches || window.navigator.standalone === true;
  }
  function isIOS() { return /iphone|ipad|ipod/i.test(navigator.userAgent); }

  var toastTimer = null;
  function toast(msg) {
    var el = document.querySelector(".toast");
    if (!el) {
      el = document.createElement("div");
      el.className = "toast";
      document.body.appendChild(el);
    }
    el.textContent = msg;
    el.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { el.hidden = true; }, 2600);
  }

  function pendingFor(matchId) {
    return S.outbox.filter(function (op) { return op.match_id === matchId; }).length;
  }
  function pendingIds() {
    var set = {};
    S.outbox.forEach(function (op) { set[op.id] = true; });
    return set;
  }

  /* ------------------------------------------------------ scoring rules -- */
  // A faithful copy of ScMatch.badminton_stats on the server, so the screen
  // shows exactly what the server will compute once the changes arrive.
  function badmintonStats(m) {
    var games = { "Game 1": { a: 0, b: 0 }, "Game 2": { a: 0, b: 0 }, "Game 3": { a: 0, b: 0 } };
    var current = "Game 1";
    var keys = ["Game 1", "Game 2", "Game 3", "G1", "G2", "G3"];
    function won(x, y) { return (x >= 21 && x - y >= 2) || x === 30; }

    m.events.forEach(function (e) {
      if ((e.points || 0) <= 0 || (e.side !== "a" && e.side !== "b")) return;
      var g = null;
      if (e.clock) {
        var lc = e.clock.toLowerCase();
        for (var i = 0; i < keys.length; i++) {
          if (lc.indexOf(keys[i].toLowerCase()) !== -1) {
            g = keys[i].indexOf("1") !== -1 ? "Game 1" : (keys[i].indexOf("2") !== -1 ? "Game 2" : "Game 3");
            break;
          }
        }
      }
      if (!g) g = current;
      games[g][e.side] += e.points || 1;
      if (won(games[g].a, games[g].b) || won(games[g].b, games[g].a)) {
        if (g === "Game 1" && current === "Game 1") current = "Game 2";
        else if (g === "Game 2" && current === "Game 2") current = "Game 3";
      }
    });

    var list = [], wonA = 0, wonB = 0, active = "Game 1", activeA = 0, activeB = 0;
    ["Game 1", "Game 2", "Game 3"].forEach(function (name) {
      var a = games[name].a, b = games[name].b;
      var wa = won(a, b), wb = won(b, a), done = wa || wb;
      if (done) {
        if (wa) wonA++; else wonB++;
      } else if (!list.length || list.every(function (x) { return x.done; })) {
        active = name; activeA = a; activeB = b;
      }
      list.push({ game: name, a: a, b: b, done: done });
    });
    var winner = wonA >= 2 ? "a" : (wonB >= 2 ? "b" : null);
    var sets = list.filter(function (g) { return g.done || g.a > 0 || g.b > 0; })
      .map(function (g) { return g.a + "–" + g.b; }).join(", ");
    return { games_a: wonA, games_b: wonB, active_game: active, active_a: activeA, active_b: activeB, winner: winner, sets: sets };
  }

  function recompute(m) {
    if (m.sport === "Badminton") {
      var bs = badmintonStats(m);
      if (bs.games_a > 0 || bs.games_b > 0 || m.status === "final") {
        m.score_a = bs.games_a; m.score_b = bs.games_b;
      } else {
        m.score_a = bs.active_a; m.score_b = bs.active_b;
      }
      return;
    }
    var t = { a: 0, b: 0 };
    m.events.forEach(function (e) { if (e.side === "a" || e.side === "b") t[e.side] += e.points || 0; });
    m.score_a = t.a; m.score_b = t.b;
  }

  function findAction(m, key) {
    for (var i = 0; i < m.actions.length; i++) if (m.actions[i].key === key) return m.actions[i];
    return null;
  }
  function playerName(m, side, id) {
    if (!id) return null;
    var list = (m.players && m.players[side]) || [];
    for (var i = 0; i < list.length; i++) if (String(list[i].id) === String(id)) return list[i].name;
    return null;
  }

  // Apply one change to the local copy — mirrors what the server does with it.
  function applyLocal(m, op) {
    var p = op.payload || {};
    if (op.type === "event") {
      var action = findAction(m, p.action);
      if (!action) return;
      m.events.push({
        uid: op.id, side: p.side, kind: action.label, points: action.points,
        clock: p.clock || null, player_id: p.player_id || null,
        player: playerName(m, p.side, p.player_id)
      });
      if (m.status === "scheduled") {
        m.status = "live";
        m.period = m.period || m.periods[0];
      }
      recompute(m);
      if (m.sport === "Badminton") {
        var bs = badmintonStats(m);
        if (!bs.winner && bs.active_game !== m.period) m.period = bs.active_game;
      }
    } else if (op.type === "undo") {
      m.events = m.events.filter(function (e) { return e.uid !== p.target; });
      recompute(m);
      if (m.sport === "Badminton") {
        var bs2 = badmintonStats(m);
        if (!bs2.winner) m.period = bs2.active_game;
      }
    } else if (op.type === "period") {
      m.period = p.period;
    } else if (op.type === "status") {
      m.status = p.status;
      if (p.status === "live" && !m.period) m.period = m.periods[0];
    }
  }

  function enqueue(matchId, type, payload) {
    var m = S.matches[matchId];
    if (!m) return null;
    var op = { id: uid(), type: type, match_id: matchId, payload: payload, at: new Date().toISOString() };
    S.outbox.push(op);
    saveOutbox();               // the change is safe on the device from here on
    applyLocal(m, op);
    saveMatches();
    render();
    scheduleSync(150);
    return op;
  }

  /* -------------------------------------------------------------- timer -- */
  function timerOf(id) { return S.timers[id] || { running: false, base: 0, paused: 0 }; }
  function elapsedSec(id) {
    var t = timerOf(id);
    if (t.running) return Math.max(0, Math.floor((Date.now() - t.base) / 1000));
    return t.paused || 0;
  }
  function timerStarted(id) { var t = timerOf(id); return t.running || (t.paused || 0) > 0; }
  function fmtTimer(sec) {
    var mm = Math.floor(sec / 60), ss = sec % 60;
    return (mm < 10 ? "0" : "") + mm + ":" + (ss < 10 ? "0" : "") + ss;
  }
  function minuteLabel(id) { return (Math.floor(elapsedSec(id) / 60) + 1) + "'"; }
  function usesTimer(m) { return m.sport === "Football" || m.sport === "Basketball" || m.sport === "Kabaddi"; }

  // What gets stamped on an event when the scorer has not typed anything.
  function autoClock(m) {
    if (m.sport === "Badminton") return badmintonStats(m).active_game;
    var started = timerStarted(m.id);
    if (m.sport === "Basketball") return (m.period || m.periods[0]) + (started ? " " + minuteLabel(m.id) : "");
    if (m.sport === "Kabaddi") return started ? minuteLabel(m.id) : (m.period || "H1");
    if (m.sport === "Football") return started ? minuteLabel(m.id) : "";
    return m.period || "";
  }

  function toggleTimer(id) {
    var t = timerOf(id);
    if (t.running) {
      t.paused = elapsedSec(id); t.running = false; t.base = 0;
    } else {
      t.base = Date.now() - (t.paused || 0) * 1000; t.running = true;
      var m = S.matches[id];
      if (m && m.status === "scheduled") enqueue(id, "status", { status: "live" });
    }
    S.timers[id] = t; saveTimers(); render();
  }
  function setTimerMinute(id) {
    var cur = Math.floor(elapsedSec(id) / 60) + 1;
    var res = window.prompt("Set the match clock to minute (e.g. 15):", cur);
    if (res === null) return;
    var n = parseInt(String(res).replace(/[^0-9]/g, ""), 10);
    if (isNaN(n) || n < 1 || n > 180) return;
    var t = timerOf(id), sec = (n - 1) * 60;
    if (t.running) t.base = Date.now() - sec * 1000; else t.paused = sec;
    S.timers[id] = t; saveTimers(); render();
  }

  /* ---------------------------------------------------------------- API -- */
  function api(method, path, body) {
    var ctrl = window.AbortController ? new AbortController() : null;
    var timer = ctrl ? setTimeout(function () { ctrl.abort(); }, 20000) : null;
    var headers = { "Accept": "application/json" };
    if (body) headers["Content-Type"] = "application/json";
    if (S.auth && S.auth.token) headers["Authorization"] = "Bearer " + S.auth.token;
    return fetch(API + path, {
      method: method, headers: headers, cache: "no-store", credentials: "omit",
      body: body ? JSON.stringify(body) : undefined,
      signal: ctrl ? ctrl.signal : undefined
    }).then(function (res) {
      if (timer) clearTimeout(timer);
      return res.json().catch(function () { return {}; }).then(function (data) {
        if (!res.ok) {
          var err = new Error(data.error || ("Server answered " + res.status));
          err.status = res.status; err.auth = data.auth;
          throw err;
        }
        return data;
      });
    }, function (e) {
      if (timer) clearTimeout(timer);
      var err = new Error("No connection");
      err.status = 0;
      throw err;
    });
  }

  // Take the server's copy of a match, unless this device still has changes
  // for it that the server has not seen — then ours is the more complete one.
  function mergeServerMatch(sm) {
    if (pendingFor(sm.id) > 0) return;
    S.matches[sm.id] = sm;
  }

  function refreshBundle() {
    return api("GET", "/bundle").then(function (data) {
      var seen = {};
      (data.matches || []).forEach(function (sm) { seen[sm.id] = true; mergeServerMatch(sm); });
      Object.keys(S.matches).forEach(function (id) {
        if (!seen[id] && pendingFor(Number(id)) === 0) delete S.matches[id];
      });
      if (data.me && S.auth) { S.auth.me = data.me; store("auth", S.auth); }
      S.meta.lastBundleAt = Date.now();
      saveMatches(); saveMeta();
    });
  }

  function scheduleSync(delay) {
    clearTimeout(S.syncTimer);
    S.syncTimer = setTimeout(function () { syncNow(); }, delay);
  }

  function nextDelay() {
    if (S.net === "offline" || S.authProblem) {
      // 5s, 10s, 20s … capped at 60s: quick to notice a returning signal
      // without draining the battery while there is none.
      return Math.min(60000, 5000 * Math.pow(2, Math.min(S.backoff, 4)));
    }
    return S.outbox.length ? 1500 : 15000;
  }

  function syncNow(force) {
    if (S.syncing || !S.auth) return Promise.resolve();
    S.syncing = true;
    renderTop();
    var work;
    if (S.outbox.length) {
      var batch = S.outbox.slice(0, BATCH);
      work = api("POST", "/sync", { ops: batch }).then(function (data) {
        var done = {};
        (data.results || []).forEach(function (r) {
          if (r.status === "retry") return;
          done[r.id] = true;
          if (r.status === "rejected") {
            S.rejected.push(r.error || "A change was refused by the server.");
          }
        });
        S.outbox = S.outbox.filter(function (op) { return !done[op.id]; });
        saveOutbox();
        (data.matches || []).forEach(mergeServerMatch);
        saveMatches();
        S.meta.lastSyncAt = Date.now(); saveMeta();
        if (!Object.keys(done).length && batch.length) throw Object.assign(new Error("Server error, will retry."), { status: 500 });
      });
    } else if (force || !S.meta.lastBundleAt || Date.now() - S.meta.lastBundleAt > BUNDLE_EVERY_MS) {
      work = refreshBundle();
    } else {
      work = Promise.resolve();
    }
    return work.then(function () {
      S.net = "online"; S.backoff = 0; S.authProblem = null;
    }, function (err) {
      if (err.status === 401) {
        S.net = "online";
        S.authProblem = err.message || "Sign in again to sync.";
      } else if (err.status === 0) {
        S.net = "offline"; S.backoff++;
      } else {
        S.net = "online"; S.backoff++;
      }
    }).then(function () {
      S.syncing = false;
      render();
      scheduleSync(S.outbox.length && S.net === "online" && !S.authProblem && S.backoff === 0 ? 300 : nextDelay());
    });
  }

  /* -------------------------------------------------------------- views -- */
  function go(hash) { if (location.hash !== hash) location.hash = hash; else route(); }
  function route() {
    var h = location.hash || "";
    var mm = h.match(/^#\/match\/(\d+)/);
    S.ui.menu = false;
    if (mm && S.matches[mm[1]]) S.view = { name: "match", matchId: Number(mm[1]) };
    else S.view = { name: "list", matchId: null };
    render();
    window.scrollTo(0, 0);
  }

  function netPill() {
    var pend = S.outbox.length;
    var net = S.net === "online" ? '<span class="pill pill--online"><span class="dot"></span>Online</span>'
      : S.net === "offline" ? '<span class="pill pill--offline"><span class="dot"></span>Offline</span>'
      : '<span class="pill"><span class="dot"></span>Checking…</span>';
    var sync = pend
      ? '<button class="pill pill--pending" data-act="sync">' + (S.syncing ? "Syncing… " : "⏳ ") + pend + " to upload</button>"
      : '<span class="pill pill--synced">✓ All saved</span>';
    return net + sync;
  }

  function renderTop() {
    var el = document.querySelector(".top");
    if (!el) return;
    var back = S.view.name === "match" ? '<button class="top__back" data-act="back">← Matches</button>' : "";
    el.innerHTML = back +
      '<span class="top__brand"><img src="/scores/static/app/icon-192.png" alt="">GML Scorer</span>' +
      (S.auth ? netPill() + '<button class="pill" data-act="menu" aria-label="Menu">☰</button>' : "");
  }

  function installCard() {
    if (isStandalone()) return "";
    if (S.installPrompt) {
      return '<div class="card install"><img src="/scores/static/app/icon-192.png" alt="">' +
        '<div class="install__text"><strong>Install the Scorer App</strong>' +
        '<span>Adds GML Scorer to this phone or computer. It opens and scores without internet.</span></div>' +
        '<button class="btn btn--gold" data-act="install">Install</button></div>';
    }
    if (isIOS()) {
      return '<div class="card install"><img src="/scores/static/app/icon-192.png" alt="">' +
        '<div class="install__text"><strong>Install on iPhone / iPad</strong>' +
        '<span>Tap the Share button in Safari, then “Add to Home Screen”.</span></div></div>';
    }
    return "";
  }

  function noticesHtml() {
    var out = "";
    if (S.authProblem) {
      out += '<div class="notice notice--bad">🔒 ' + esc(S.authProblem) +
        ' Your ' + S.outbox.length + ' unsent change(s) are safe on this device. <button class="btn btn--sm" data-act="relogin">Sign in again</button></div>';
    }
    if (S.rejected.length) {
      out += '<div class="notice notice--warn">The server refused ' + S.rejected.length + ' change(s): ' +
        esc(S.rejected.slice(-3).join(" · ")) + ' The screen now shows the server’s copy. <button class="btn btn--sm btn--ghost" data-act="clear-rejected">OK</button></div>';
    }
    if (S.net === "offline" && S.auth) {
      out += '<div class="notice notice--warn">📴 No internet. Keep scoring — every tap is saved on this device and uploads automatically when the signal returns.</div>';
    }
    return out;
  }

  function renderMenu() {
    if (!S.ui.menu) return "";
    var last = S.meta.lastSyncAt ? new Date(S.meta.lastSyncAt).toLocaleTimeString() : "never";
    return '<div class="card"><h2>' + esc(S.auth.me.name) + '</h2>' +
      '<p class="muted small">' + (S.auth.me.role === "admin" ? "Committee admin" : "Scorer") +
      ' · last upload: ' + esc(last) + ' · ' + S.outbox.length + ' waiting</p><div class="menu">' +
      '<button class="btn btn--gold" data-act="refresh">↻ Upload now &amp; refresh matches</button>' +
      '<button class="btn btn--ghost" data-act="backup">⬇ Download backup file</button>' +
      '<button class="btn btn--ghost" data-act="logout">Sign out</button>' +
      '</div></div>';
  }

  function renderLogin() {
    var r = S.ui.loginRole;
    var offline = S.net === "offline";
    return '<div class="wrap">' + installCard() +
      '<div class="card"><h1>Sign in</h1>' +
      '<p class="muted">Sign in once with internet. After that this device keeps your matches and you can score with no signal at all.</p>' +
      (offline ? '<div class="notice notice--warn">📴 No internet right now. Signing in needs a connection the first time.</div>' : "") +
      (S.ui.loginError ? '<div class="notice notice--bad">' + esc(S.ui.loginError) + '</div>' : "") +
      '<div class="seg"><button data-act="role" data-role="scorer" class="' + (r === "scorer" ? "is-on" : "") + '">Scorer</button>' +
      '<button data-act="role" data-role="admin" class="' + (r === "admin" ? "is-on" : "") + '">Committee admin</button></div>' +
      '<form class="form" data-form="login">' +
      '<label>' + (r === "admin" ? "Admin username" : "Your scorer name") +
      '<input name="name" autocomplete="username" required autocapitalize="none"></label>' +
      '<label>' + (r === "admin" ? "Admin password" : "Passcode") +
      '<input name="passcode" type="password" autocomplete="current-password" required></label>' +
      '<button class="btn btn--block" type="submit"' + (S.ui.busy ? " disabled" : "") + '>' + (S.ui.busy ? "Signing in…" : "Sign in") + '</button>' +
      '</form></div>' +
      '<p class="foot">GM League Season 4 · Scorer App v' + esc(root.dataset.version || "") + '</p></div>';
  }

  function statusTag(m) {
    if (m.status === "live") return '<span class="tag tag--live">● LIVE' + (m.period ? " · " + esc(m.period) : "") + '</span>';
    if (m.status === "final") return '<span class="tag tag--final">Finished</span>';
    return '<span class="tag tag--sched">' + (m.starts_at ? esc(new Date(m.starts_at).toLocaleString([], { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })) : "Not started") + '</span>';
  }

  function renderList() {
    var ids = Object.keys(S.matches).map(Number);
    var groups = { live: [], scheduled: [], final: [] };
    ids.forEach(function (id) { var m = S.matches[id]; (groups[m.status] || groups.scheduled).push(m); });
    function row(m) {
      var p = pendingFor(m.id);
      return '<button class="mrow" data-act="open" data-id="' + m.id + '">' +
        '<span class="mrow__meta"><span>' + (SPORT_ICON[m.sport] || "🏆") + " " + esc(m.sport) + '</span><span>· ' + esc(m.gender) + '</span><span>· ' + esc(m.stage) + '</span>' +
        statusTag(m) + (p ? '<span class="tag tag--pending">⏳ ' + p + ' to upload</span>' : "") + '</span>' +
        '<span class="mrow__teams">' + esc(m.team_a.name) + '<br><span class="muted">vs</span> ' + esc(m.team_b.name) + '</span>' +
        '<span class="mrow__score">' + m.score_a + "–" + m.score_b + '</span></button>';
    }
    var html = '<div class="wrap">' + installCard() + noticesHtml() + renderMenu();
    if (!ids.length) {
      html += '<div class="card empty"><p><strong>No matches on this device yet.</strong></p>' +
        '<p>Matches assigned to you download automatically when there is internet. Ask the committee to add today’s fixtures, then tap refresh.</p>' +
        '<button class="btn btn--gold" data-act="refresh">↻ Refresh matches</button></div>';
    }
    if (groups.live.length) html += '<div class="section-label">Live now</div>' + groups.live.map(row).join("");
    if (groups.scheduled.length) html += '<div class="section-label">Upcoming</div>' + groups.scheduled.map(row).join("");
    if (groups.final.length) html += '<div class="section-label">Finished (you can still correct)</div>' + groups.final.map(row).join("");
    html += '<p class="foot">Signed in as ' + esc(S.auth.me.name) + ' · v' + esc(root.dataset.version || "") + '</p></div>';
    return html;
  }

  function renderMatch(m) {
    var writable = m.status !== "final" || m.can_edit_final;
    var bs = m.sport === "Badminton" ? badmintonStats(m) : null;
    var showGames = bs && (bs.winner || m.status === "final");
    var bigA = bs && !showGames ? bs.active_a : m.score_a, bigB = bs && !showGames ? bs.active_b : m.score_b;
    var subA = bs ? bs.games_a + " game" + (bs.games_a === 1 ? "" : "s") : "";
    var subB = bs ? bs.games_b + " game" + (bs.games_b === 1 ? "" : "s") : "";
    var mid = bs ? esc(bs.winner ? "Match won" : bs.active_game) : esc(m.period || "Not started");
    var info = "";
    if (bs) {
      info = bs.winner ? "🏆 " + esc(bs.winner === "a" ? m.team_a.name : m.team_b.name) + " win " + bs.games_a + "–" + bs.games_b + " — tap End match"
        : (bs.sets ? "Games: " + esc(bs.sets) : "First to 21, win by 2");
    } else if (m.status === "final") {
      info = m.score_a === m.score_b ? "Full time · Draw" : "Full time · " + esc(m.score_a > m.score_b ? m.team_a.name : m.team_b.name) + " won";
    } else {
      var d = m.score_a - m.score_b;
      info = d === 0 ? "Scores level" : esc(d > 0 ? m.team_a.name : m.team_b.name) + " lead by " + Math.abs(d);
    }

    var html = '<div class="wrap">' + noticesHtml() + renderMenu();
    html += '<div class="board"><div class="board__meta"><span>' + (SPORT_ICON[m.sport] || "🏆") + " " + esc(m.sport) + " · " + esc(m.gender) + " · " + esc(m.stage) + '</span>' + statusTag(m) + '</div>' +
      '<div class="board__score">' +
      '<div><div class="board__team">' + esc(m.team_a.name) + '</div><div class="board__num">' + bigA + '</div><div class="board__sub">' + subA + '</div></div>' +
      '<div class="board__mid">' + mid + '</div>' +
      '<div><div class="board__team">' + esc(m.team_b.name) + '</div><div class="board__num">' + bigB + '</div><div class="board__sub">' + subB + '</div></div>' +
      '</div><div class="board__info">' + info + '</div></div>';

    if (!writable) {
      html += '<div class="notice notice--warn">This match is finished. Ask the committee to allow edits if something needs correcting.</div>';
    } else {
      // Clock & period controls
      var override = S.ui.clock[m.id];
      html += '<div class="ctl">';
      if (usesTimer(m)) {
        var t = timerOf(m.id);
        html += '<span class="ctl__timer" data-timer>' + fmtTimer(elapsedSec(m.id)) + '</span>' +
          '<button class="btn btn--sm ' + (t.running ? "btn--ghost" : "") + '" data-act="timer">' + (t.running ? "⏸ Pause" : (timerStarted(m.id) ? "▶ Resume" : "▶ Start clock")) + '</button>' +
          '<button class="btn btn--sm btn--ghost" data-act="timer-set">Set min</button>';
      }
      html += '<span class="ctl__grow"></span><label class="ctl__clock">Stamp <input data-clock maxlength="8" value="' + esc(override !== undefined ? override : autoClock(m)) + '" placeholder="auto"></label>';
      html += '</div>';

      if (m.sport !== "Badminton" && m.periods.length > 1) {
        var idx = Math.max(0, m.periods.indexOf(m.period));
        var prev = idx > 0 ? m.periods[idx - 1] : null;
        var next = idx < m.periods.length - 1 ? m.periods[idx + 1] : null;
        html += '<div class="ctl"><strong>Period: ' + esc(m.period || m.periods[0]) + '</strong><span class="ctl__grow"></span>' +
          (prev ? '<button class="btn btn--sm btn--ghost" data-act="period" data-period="' + esc(prev) + '">← ' + esc(prev) + '</button>' : "") +
          (next ? '<button class="btn btn--sm" data-act="period" data-period="' + esc(next) + '">' + esc(next) + ' →</button>' : "") +
          '</div>';
      }

      html += '<div class="pads">';
      ["a", "b"].forEach(function (side) {
        var team = side === "a" ? m.team_a : m.team_b;
        var players = (m.players && m.players[side]) || [];
        var chosen = (S.ui.player[m.id] || {})[side] || "";
        html += '<section class="pad pad--' + side + '"><h2 class="pad__name">' + esc(team.name) + '</h2>';
        if (players.length) {
          html += '<select data-player="' + side + '"><option value="">— Player (optional) —</option>' +
            players.map(function (p) {
              return '<option value="' + p.id + '"' + (String(p.id) === String(chosen) ? " selected" : "") + '>' +
                esc(p.name) + (p.jersey_no ? " #" + esc(p.jersey_no) : "") + '</option>';
            }).join("") + '</select>';
        }
        html += '<div class="pad__actions">' + m.actions.map(function (a) {
          return '<button class="act' + (a.points ? " act--scores" : "") + '" data-act="score" data-side="' + side + '" data-action="' + esc(a.key) + '">' +
            '<span>' + esc(a.label) + '</span>' + (a.points ? "<i>+" + a.points + "</i>" : "") + '</button>';
        }).join("") + '</div></section>';
      });
      html += '</div>';

      html += '<div class="bar">' +
        '<button class="btn btn--ghost" data-act="undo"' + (m.events.length ? "" : " disabled") + '>↶ Undo last</button>' +
        (m.status === "scheduled" ? '<button class="btn btn--gold" data-act="start">Start match</button>' : "") +
        (m.status !== "final" ? '<button class="btn" data-act="finish">End match</button>' : '<span class="tag tag--final">Finished — edits allowed</span>') +
        '</div>';
    }

    var pend = pendingIds();
    var recent = m.events.slice(-40).reverse();
    html += '<div class="card"><h2>Recorded</h2>';
    if (!recent.length) html += '<p class="muted">Nothing recorded yet.</p>';
    else {
      html += '<ol class="log">' + recent.map(function (e) {
        var waiting = !!pend[e.uid];
        return '<li class="log__side-' + esc(e.side) + '"><span class="log__clock">' + esc(e.clock || "—") + '</span>' +
          '<span><strong>' + esc(e.kind) + '</strong>' + (e.player ? " · " + esc(e.player) : "") +
          ' <span class="muted small">(' + esc(e.side === "a" ? m.team_a.tag : m.team_b.tag) + ')</span></span>' +
          '<span class="log__state ' + (waiting ? "log__state--pending" : "log__state--saved") + '">' + (waiting ? "⏳ on device" : "✓ uploaded") + '</span></li>';
      }).join("") + '</ol>';
    }
    html += '</div></div>';
    return html;
  }

  function render() {
    var body;
    if (!S.auth) body = renderLogin();
    else if (S.view.name === "match" && S.matches[S.view.matchId]) body = renderMatch(S.matches[S.view.matchId]);
    else body = renderList();
    root.innerHTML = '<header class="top"></header>' + body;
    renderTop();
  }

  /* ------------------------------------------------------------ actions -- */
  function onClick(e) {
    var btn = e.target.closest("[data-act]");
    if (!btn || btn.disabled) return;
    var act = btn.dataset.act;
    var m = S.view.name === "match" ? S.matches[S.view.matchId] : null;

    if (act === "back") return go("#/");
    if (act === "open") return go("#/match/" + btn.dataset.id);
    if (act === "menu") { S.ui.menu = !S.ui.menu; return render(); }
    if (act === "role") { S.ui.loginRole = btn.dataset.role; S.ui.loginError = ""; return render(); }
    if (act === "sync") return syncNow(true);
    if (act === "refresh") { S.ui.menu = false; toast("Checking for updates…"); return syncNow(true).then(function () { if (S.net === "online" && !S.outbox.length) return syncNow(true); }); }
    if (act === "clear-rejected") { S.rejected = []; return render(); }
    if (act === "install") return doInstall();
    if (act === "backup") return downloadBackup();
    if (act === "logout") return logout();
    if (act === "relogin") { S.auth = null; store("auth", null); return render(); }

    if (!m) return;
    if (act === "timer") return toggleTimer(m.id);
    if (act === "timer-set") return setTimerMinute(m.id);
    if (act === "period") return enqueue(m.id, "period", { period: btn.dataset.period });
    if (act === "start") return enqueue(m.id, "status", { status: "live" });
    if (act === "finish") {
      if (!window.confirm("End this match? It will show as finished on the public scoreboard once uploaded.")) return;
      var t = timerOf(m.id);
      if (t.running) { t.paused = elapsedSec(m.id); t.running = false; S.timers[m.id] = t; saveTimers(); }
      return enqueue(m.id, "status", { status: "final" });
    }
    if (act === "undo") {
      var last = m.events[m.events.length - 1];
      if (!last) return;
      enqueue(m.id, "undo", { target: last.uid });
      return toast("Undone: " + last.kind + (last.player ? " · " + last.player : ""));
    }
    if (act === "score") {
      var side = btn.dataset.side;
      var picker = root.querySelector('[data-player="' + side + '"]');
      // Worked out before the point is added: for Badminton the stamp is the
      // game this rally belongs to, not the one it may start.
      var clock = S.ui.clock[m.id] !== undefined ? S.ui.clock[m.id] : autoClock(m);
      delete S.ui.clock[m.id];
      var action = findAction(m, btn.dataset.action);
      enqueue(m.id, "event", {
        side: side, action: btn.dataset.action,
        player_id: picker && picker.value ? Number(picker.value) : null,
        clock: (clock || "").trim().slice(0, 8)
      });
      if (navigator.vibrate) navigator.vibrate(30);
      var again = root.querySelector('[data-act="score"][data-side="' + side + '"][data-action="' + btn.dataset.action + '"]');
      if (again) { again.classList.add("act--flash"); setTimeout(function () { again.classList.remove("act--flash"); }, 250); }
      if (action) toast("Saved: " + action.label + " · " + (side === "a" ? m.team_a.tag : m.team_b.tag));
    }
  }

  function onChange(e) {
    var m = S.view.name === "match" ? S.matches[S.view.matchId] : null;
    if (!m) return;
    if (e.target.matches("[data-player]")) {
      S.ui.player[m.id] = S.ui.player[m.id] || {};
      S.ui.player[m.id][e.target.dataset.player] = e.target.value;
    }
  }
  function onInput(e) {
    var m = S.view.name === "match" ? S.matches[S.view.matchId] : null;
    if (m && e.target.matches("[data-clock]")) S.ui.clock[m.id] = e.target.value;
  }

  function onSubmit(e) {
    if (!e.target.matches('[data-form="login"]')) return;
    e.preventDefault();
    var f = e.target;
    S.ui.busy = true; S.ui.loginError = ""; render();
    var auth = null;
    api("POST", "/login", { role: S.ui.loginRole, name: f.elements.name.value.trim(), passcode: f.elements.passcode.value })
      .then(function (data) {
        auth = { token: data.token, me: data.me };
        // Any unsent changes already on this device are kept and uploaded
        // under this login: the server still checks who may score which match,
        // and a committee admin signing in is the usual way to rescue them.
        S.auth = auth; store("auth", auth);
        S.authProblem = null; S.net = "online";
        if (navigator.storage && navigator.storage.persist) navigator.storage.persist();
        return refreshBundle();
      })
      .then(function () {
        S.ui.busy = false; render(); scheduleSync(500);
        toast("Signed in. Matches saved to this device.");
      }, function (err) {
        S.ui.busy = false;
        if (auth) { render(); scheduleSync(2000); return; }
        S.ui.loginError = err.status === 0 ? "No internet. Connect once to sign in." : err.message;
        if (err.status === 0) S.net = "offline";
        render();
      });
  }

  function logout() {
    if (S.outbox.length && !window.confirm(S.outbox.length + " change(s) have not uploaded yet. If you sign out they stay on this device but will NOT upload until " + S.auth.me.name + " signs in again here. Sign out anyway?")) return;
    S.auth = null; store("auth", null);
    S.ui.menu = false;
    render();
  }

  function downloadBackup() {
    var data = { exported_at: new Date().toISOString(), me: S.auth && S.auth.me, outbox: S.outbox, matches: S.matches };
    var blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
    var a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "gml-scorer-backup-" + new Date().toISOString().slice(0, 16).replace(/[:T]/g, "-") + ".json";
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(function () { URL.revokeObjectURL(a.href); }, 5000);
  }

  function doInstall() {
    if (!S.installPrompt) return;
    S.installPrompt.prompt();
    S.installPrompt.userChoice.then(function () { S.installPrompt = null; render(); });
  }

  /* --------------------------------------------------------------- boot -- */
  window.addEventListener("beforeinstallprompt", function (e) {
    e.preventDefault();
    S.installPrompt = e;
    render();
  });
  window.addEventListener("appinstalled", function () { S.installPrompt = null; toast("Installed. Open GML Scorer from your home screen or desktop."); render(); });
  window.addEventListener("online", function () { S.backoff = 0; scheduleSync(100); });
  window.addEventListener("offline", function () { S.net = "offline"; renderTop(); render(); });
  document.addEventListener("visibilitychange", function () { if (!document.hidden) scheduleSync(100); });
  window.addEventListener("hashchange", route);
  // Another tab of the app on this device changed something — pick it up.
  window.addEventListener("storage", function (e) {
    if (!e.key || e.key.indexOf(KEY) !== 0) return;
    S.auth = load("auth", null); S.matches = load("matches", {}); S.outbox = load("outbox", []);
    S.timers = load("timers", {}); render();
  });
  root.addEventListener("click", onClick);
  root.addEventListener("change", onChange);
  root.addEventListener("input", onInput);
  root.addEventListener("submit", onSubmit);

  // Tick the match clock without redrawing the whole screen (keeps focus).
  setInterval(function () {
    if (S.view.name !== "match") return;
    var m = S.matches[S.view.matchId];
    if (!m) return;
    var t = root.querySelector("[data-timer]");
    if (t) t.textContent = fmtTimer(elapsedSec(m.id));
    var c = root.querySelector("[data-clock]");
    if (c && S.ui.clock[m.id] === undefined && document.activeElement !== c) c.value = autoClock(m);
  }, 1000);

  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/scores/sw.js", { scope: "/scores/app" }).catch(function () {});
  }

  route();
  if (S.auth) scheduleSync(300);
})();
