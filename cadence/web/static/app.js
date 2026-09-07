/* Cadence - the offline queue, the per-exercise timer, and the service-worker registration.
   Vanilla, no build step (D-012). Everything here degrades: with this file blocked, HTMX still
   ticks rows against the server and the <noscript> buttons still post plain forms.

   The queue records an operation *before* it is sent, not only when the phone is offline. A
   request that leaves and fails is the same loss as one that never left, and `navigator.onLine`
   is a hint about the radio, not about whether the server answered. */
(function () {
  "use strict";

  var DB_NAME = "cadence-queue";
  var STORE = "ops";
  var MAX_OPS = 500;
  /* A 4xx is the server saying no. Retrying it a few times covers a transient rejection; past
     that the entry is parked, kept, and reported, so nothing is dropped in silence. */
  var MAX_ATTEMPTS = 5;
  /* An operation younger than this does not raise the banner: the common case is a tick that
     lands in 40 ms, and a banner that flashes on every tap is noise, not information. */
  var BANNER_GRACE_MS = 2500;

  var dbPromise = null;

  /* ----------------------------------------------------------------- IndexedDB */

  function openDb() {
    if (dbPromise) return dbPromise;
    dbPromise = new Promise(function (resolve, reject) {
      if (!window.indexedDB) {
        reject(new Error("no IndexedDB"));
        return;
      }
      var request = indexedDB.open(DB_NAME, 1);
      request.onupgradeneeded = function () {
        var db = request.result;
        if (!db.objectStoreNames.contains(STORE)) {
          db.createObjectStore(STORE, { keyPath: "key", autoIncrement: true });
        }
      };
      request.onsuccess = function () { resolve(request.result); };
      request.onerror = function () { reject(request.error); };
    });
    return dbPromise;
  }

  function withStore(mode, work) {
    return openDb().then(function (db) {
      return new Promise(function (resolve, reject) {
        var tx = db.transaction(STORE, mode);
        var holder = work(tx.objectStore(STORE));
        tx.oncomplete = function () { resolve(holder ? holder.value : undefined); };
        tx.onerror = function () { reject(tx.error); };
        tx.onabort = function () { reject(tx.error); };
      });
    });
  }

  function trim(store) {
    /* Risk 4: a permanently failing server must not fill the phone. Oldest goes first. */
    var count = store.count();
    count.onsuccess = function () {
      if (count.result < MAX_OPS) return;
      var cursor = store.openCursor();
      cursor.onsuccess = function () {
        if (cursor.result) {
          console.warn("cadence: offline queue full, dropping the oldest operation");
          cursor.result.delete();
        }
      };
    };
  }

  function enqueue(op) {
    var record = {
      session_id: op.session_id,
      kind: op.kind,
      position: op.position === undefined ? null : op.position,
      patch: op.patch,
      ts: Date.now(),
      attempts: 0,
      blocked: false,
      last_error: null
    };
    return withStore("readwrite", function (store) {
      var holder = {};
      trim(store);
      store.add(record).onsuccess = function (event) { holder.value = event.target.result; };
      return holder;
    }).then(function (key) {
      showBanner();
      return key;
    }, function () {
      /* No IndexedDB at all. The request below is the only attempt this operation gets. */
      return null;
    });
  }

  function save(op) {
    return withStore("readwrite", function (store) { store.put(op); return {}; }).catch(function () {});
  }

  function drop(key) {
    if (key === null || key === undefined) return Promise.resolve();
    return withStore("readwrite", function (store) { store.delete(key); return {}; }).catch(function () {});
  }

  /* Rejects rather than answering "empty" when the store cannot be read: a failure that looks
     like an empty queue hides the banner and tells a caller everything has synced. */
  function allOps() {
    return withStore("readonly", function (store) {
      var holder = { value: [] };
      store.getAll().onsuccess = function (event) { holder.value = event.target.result || []; };
      return holder;
    });
  }

  function counts() {
    return allOps().then(function (ops) {
      var now = Date.now();
      var waiting = 0;
      var blocked = 0;
      ops.forEach(function (op) {
        if (op.blocked) blocked += 1;
        else if (now - op.ts > BANNER_GRACE_MS) waiting += 1;
      });
      return { waiting: waiting, blocked: blocked, total: ops.length };
    });
  }

  function pending() {
    return allOps().then(function (ops) { return ops.length; });
  }

  /* Anything the user changed offline that this session has not yet sent. */
  function feltFor(sessionId) {
    return allOps().then(function (ops) {
      var latest = null;
      ops.forEach(function (op) {
        if (op.session_id === sessionId && op.kind === "felt") latest = op;
      });
      return latest ? latest.patch.felt : null;
    });
  }

  /* ---------------------------------------------------------------------- UI */

  function showBanner() {
    var banner = document.getElementById("offline-banner");
    if (!banner) return Promise.resolve();
    /* An unreadable queue leaves the banner exactly as it is: saying "all synced" would be a
       guess, and this is the one line telling the user their taps are still on the phone. */
    return counts().catch(function () { return null; }).then(function (state) {
      if (state === null) return;
      if (state.blocked > 0) {
        banner.textContent = "Some changes were not accepted. They are still on this phone.";
        banner.hidden = false;
      } else if (state.waiting > 0) {
        banner.textContent = "Saved on this phone — will sync.";
        banner.hidden = false;
      } else {
        banner.hidden = true;
      }
    });
  }

  function urlFor(op) {
    if (op.kind === "done") return "/api/sessions/" + op.session_id + "/done";
    if (op.kind === "felt") return "/today/" + op.session_id + "/felt";
    return "/api/sessions/" + op.session_id + "/rows/" + op.position;
  }

  function requestFor(op) {
    if (op.kind === "felt") {
      /* Felt has no JSON endpoint of its own; the HTML route is idempotent and takes a form.
         The HX-Request header asks for the partial rather than a redirect we would then follow. */
      return {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded", "HX-Request": "true" },
        body: "felt=" + encodeURIComponent(op.patch.felt)
      };
    }
    return {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(op.patch)
    };
  }

  /* "sent" - gone, delete it. "blocked" - the server refused; keep it and move on.
     "stop" - the network or the server is down; keep it and stop the whole drain. */
  function send(op) {
    op.inflight_at = Date.now();
    return save(op)
      .then(function () { return fetch(urlFor(op), requestFor(op)); })
      .then(function (response) {
        if (response.ok) return "sent";
        /* 409 is "this session is already finished": no future attempt can change that. */
        if (response.status === 409) return "sent";
        if (response.status >= 500) return hold(op, "server " + response.status);
        return refuse(op, "http " + response.status);
      }, function (error) {
        return hold(op, String(error));
      });
  }

  function hold(op, reason) {
    op.inflight_at = null;
    op.last_error = reason;
    return save(op).then(function () { return "stop"; });
  }

  function refuse(op, reason) {
    op.inflight_at = null;
    op.attempts = (op.attempts || 0) + 1;
    op.last_error = reason;
    op.blocked = op.attempts >= MAX_ATTEMPTS;
    console.warn("cadence: the server refused a queued operation", reason, op);
    /* Parked, not dropped, and not blocking the ones behind it: every operation carries the whole
       state of its row, and the server orders them by `ts`, so skipping one cannot corrupt a later
       one. Blocking the drain instead would let a single bad entry strand a whole session. */
    return save(op).then(function () { return op.blocked ? "blocked" : "stop"; });
  }

  var replaying = false;

  function replay() {
    if (replaying) return Promise.resolve();
    replaying = true;
    return allOps().then(function (ops) {
      var queue = ops.slice().filter(function (op) { return !op.blocked; })
        .sort(function (a, b) { return a.key - b.key; });
      return queue.reduce(function (chain, op) {
        return chain.then(function (halted) {
          if (halted) return true;
          return send(op).then(function (outcome) {
            if (outcome === "sent") return drop(op.key).then(function () { return false; });
            return outcome === "stop";
          });
        });
      }, Promise.resolve(false));
    }).then(function () {
      replaying = false;
      return showBanner();
    }, function () {
      replaying = false;
    });
  }

  /* -------------------------------------------------------------- optimistic */

  function paintRow(form, done) {
    form.classList.toggle("row-done", done);
    form.setAttribute("data-done", done ? "true" : "false");
    var box = form.querySelector(".tick");
    if (box) {
      box.checked = done;
      box.setAttribute("aria-checked", done ? "true" : "false");
    }
  }

  function remember(element, key) {
    if (key !== null && key !== undefined) element.setAttribute("data-op-key", String(key));
  }

  document.addEventListener("change", function (event) {
    var box = event.target;
    if (!box.classList || !box.classList.contains("tick")) return;
    var form = box.closest("form.row");
    if (!form) return;
    var done = box.checked;
    paintRow(form, done);
    var queued = enqueue({
      session_id: form.getAttribute("data-session"),
      kind: "row",
      position: parseInt(form.getAttribute("data-row"), 10),
      patch: { done: done, ts: new Date().toISOString() }
    }).then(function (key) { remember(form, key); return key; });
    if (!navigator.onLine) {
      /* HTMX would fire a request that cannot land. The queue already holds this one. */
      event.stopPropagation();
      queued.then(showBanner);
    }
  }, true);

  document.addEventListener("click", function (event) {
    if (!event.target.closest) return;
    var step = event.target.closest("button.step");
    if (step) return onStep(event, step);
    var segment = event.target.closest("button.segment");
    if (segment) return onFelt(event, segment);
    /* The shared Done and the youth exit are different controls (D-084) and both finish
       something, so both are queued -- the exit carrying data-scope="solo". */
    var done = event.target.closest("button.done, button.exit");
    if (done) return onDone(event, done);
  }, true);

  function onStep(event, button) {
    var form = button.closest("form.row");
    if (!form || navigator.onLine) return;
    event.preventDefault();
    event.stopPropagation();
    var output = form.querySelector("[data-reps]");
    if (button.name !== "reps" || !output) {
      /* A load step needs the household ladder and the band cap, and both live on the server on
         purpose (D-081). Guessing a rung offline is how an over-cap load reaches a child's row. */
      notice(form, "Load changes need the connection. Reps and ticks work offline.");
      return;
    }
    var next = Math.max(1, parseInt(output.textContent, 10) + (button.value === "-1" ? -1 : 1));
    output.textContent = String(next);
    enqueue({
      session_id: form.getAttribute("data-session"),
      kind: "row",
      position: parseInt(form.getAttribute("data-row"), 10),
      patch: { reps_done: next, ts: new Date().toISOString() }
    }).then(showBanner);
  }

  function notice(form, text) {
    var existing = form.querySelector(".row-notice");
    if (existing) {
      existing.textContent = text;
      return;
    }
    var span = document.createElement("span");
    span.className = "row-note row-notice";
    span.setAttribute("role", "status");
    span.textContent = text;
    var body = form.querySelector(".row-body");
    if (body) body.appendChild(span);
  }

  function paintFelt(button) {
    var group = button.closest(".segments");
    if (!group) return;
    group.querySelectorAll("button.segment").forEach(function (other) {
      other.setAttribute("aria-checked", other === button ? "true" : "false");
    });
  }

  function onFelt(event, button) {
    var form = button.closest("form");
    if (!form) return;
    var sessionId = button.getAttribute("data-session") || sessionFromAction(form.getAttribute("action"));
    if (!sessionId) return;
    paintFelt(button);
    var queued = enqueue({
      session_id: sessionId,
      kind: "felt",
      patch: { felt: button.value, ts: new Date().toISOString() }
    }).then(function (key) { remember(form, key); return key; });
    if (!navigator.onLine) {
      event.preventDefault();
      event.stopPropagation();
      queued.then(showBanner);
    }
  }

  function sessionFromAction(action) {
    var match = /\/today\/([^/]+)\//.exec(action || "");
    return match ? match[1] : null;
  }

  function onDone(event, button) {
    var sessionId = button.getAttribute("data-session");
    if (!sessionId) return;
    /* This session's own answer, not whichever strip happens to be first in the document: on the
       Together tab the son's felt was being stamped onto the parent's queued Done. Each session
       queues its own felt in onFelt, so nothing is lost by scoping this. */
    var chosen = document.querySelector(
      'button.segment[data-session="' + sessionId + '"][aria-checked="true"]'
    );
    var patch = { ts: new Date().toISOString() };
    if (chosen) patch.felt = chosen.value;
    if (button.getAttribute("data-scope") === "solo") patch.scope = "solo";
    var queued = enqueue({ session_id: sessionId, kind: "done", patch: patch });
    if (navigator.onLine) return;
    /* Offline the button would do nothing at all, which reads as a broken app on the one screen
       that has to feel finished. The summary is served by the worker's offline fallback. */
    event.preventDefault();
    event.stopPropagation();
    queued.then(function () { window.location.href = "/done/" + sessionId; });
  }

  /* A rejected form still has to render. HTMX swaps 2xx only, so a 422 carrying the settings
     form with its errors in it would be dropped and the user would tap Save and see nothing
     change. Only the settings form opts in: everywhere else a 4xx really is "do not swap". */
  document.body.addEventListener("htmx:beforeSwap", function (event) {
    var detail = event.detail || {};
    var status = detail.xhr ? detail.xhr.status : 0;
    var target = event.target;
    if (status !== 422) return;
    if (!target || !target.closest || !target.closest("#settings-form")) return;
    detail.shouldSwap = true;
    detail.isError = false;
  });

  /* An HTMX request that actually landed retires the entry the same change queued. */
  document.body.addEventListener("htmx:afterRequest", function (event) {
    var element = event.target.closest ? event.target.closest("[data-op-key]") : null;
    if (!element) return;
    var detail = event.detail || {};
    var status = detail.xhr ? detail.xhr.status : 0;
    if (!detail.successful || status >= 400) return;
    var key = parseInt(element.getAttribute("data-op-key"), 10);
    element.removeAttribute("data-op-key");
    drop(key).then(showBanner);
  });

  /* Keep aria-checked honest after every HTMX swap. */
  document.body.addEventListener("htmx:afterSwap", function () {
    document.querySelectorAll("form.row").forEach(function (form) {
      var box = form.querySelector(".tick");
      if (box) box.setAttribute("aria-checked", box.checked ? "true" : "false");
    });
    showBanner();
    measureFooter();
  });

  /* ------------------------------------------------------------- footer room */

  /* The footer is fixed, and on the Together tab it carries two felt strips, so its height is
     not a number a stylesheet can know: a constant left the last rows underneath it. */
  function measureFooter() {
    var footer = document.getElementById("done-footer");
    if (!footer) return;
    var height = Math.ceil(footer.getBoundingClientRect().height);
    if (height > 0) document.documentElement.style.setProperty("--footer-h", height + "px");
  }

  window.addEventListener("resize", measureFooter);
  if (window.ResizeObserver) {
    var observer = new ResizeObserver(measureFooter);
    document.addEventListener("DOMContentLoaded", function () {
      var footer = document.getElementById("done-footer");
      if (footer) observer.observe(footer);
    });
  }

  /* -------------------------------------------------------------------- timer */

  var running = null;

  function stopTimer() {
    if (!running) return;
    clearInterval(running.handle);
    running.button.setAttribute("aria-pressed", "false");
    running.output.textContent = "";
    running = null;
  }

  function startTimer(button) {
    stopTimer();
    var output = button.querySelector("[data-clock]");
    var seconds = parseInt(button.getAttribute("data-seconds"), 10) || 0;
    var rest = parseInt(button.getAttribute("data-rest"), 10) || 0;
    var remaining = seconds || rest;
    if (!remaining) return;
    button.setAttribute("aria-pressed", "true");
    output.textContent = remaining + "s";
    /* One interval at a time, on purpose: a phone on the floor should not run six clocks. */
    running = {
      button: button,
      output: output,
      handle: setInterval(function () {
        remaining -= 1;
        output.textContent = Math.max(remaining, 0) + "s";
        if (remaining <= 0) stopTimer();
      }, 1000)
    };
  }

  document.addEventListener("click", function (event) {
    var button = event.target.closest ? event.target.closest("button.timer") : null;
    if (!button) return;
    event.preventDefault();
    if (running && running.button === button) stopTimer();
    else startTimer(button);
  });

  /* --------------------------------------------------------------- lifecycle */

  window.addEventListener("online", replay);
  window.addEventListener("load", function () {
    showBanner();
    measureFooter();
    replay();
    if ("serviceWorker" in navigator) {
      navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(function (error) {
        console.warn("cadence: service worker did not register", error);
      });
    }
  });

  window.cadence = { replay: replay, pending: pending, counts: counts, feltFor: feltFor,
                     measureFooter: measureFooter };
})();
