/* Shared UI behaviour: CSRF-safe POSTs, toasts, run/kill buttons,
   dashboard + run-page live polling, recorders. */
(function () {
  "use strict";

  function getCookie(name) {
    const m = document.cookie.match("(^|;)\\s*" + name + "\\s*=\\s*([^;]+)");
    return m ? decodeURIComponent(m.pop()) : "";
  }

  async function post(url, data) {
    const resp = await fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRFToken": getCookie("csrftoken"),
      },
      body: JSON.stringify(data || {}),
    });
    let payload = {};
    try { payload = await resp.json(); } catch (e) { /* non-JSON error page */ }
    if (!resp.ok || payload.ok === false) {
      throw new Error(payload.error || ("request failed (" + resp.status + ")"));
    }
    return payload;
  }

  function toast(message, kind) {
    let box = document.getElementById("toasts");
    if (!box) {
      box = document.createElement("div");
      box.id = "toasts";
      document.body.appendChild(box);
    }
    const el = document.createElement("div");
    el.className = "toast " + (kind || "");
    el.textContent = message;
    box.appendChild(el);
    setTimeout(() => el.remove(), 5000);
  }

  function fmtDur(seconds) {
    if (seconds === null || seconds === undefined || isNaN(seconds)) return "—";
    seconds = Math.max(0, Math.round(seconds));
    if (seconds < 60) return seconds + "s";
    const m = Math.floor(seconds / 60), s = seconds % 60;
    if (m < 60) return m + "m " + (s < 10 ? "0" : "") + s + "s";
    const h = Math.floor(m / 60);
    return h + "h " + (m % 60) + "m";
  }

  function esc(text) {
    const div = document.createElement("div");
    div.textContent = text == null ? "" : String(text);
    return div.innerHTML;
  }

  const u = (path) => (window.HUB_PREFIX || "") + path;

  window.hub = { post, toast, fmtDur, esc, getCookie, u };

  /* ---- live page data --------------------------------------------------------
     The parts of a page that show run data carry data-live="<key>". When the
     status poll below sees something change -- a run queued, started or
     finished -- the page is fetched again in the background and every live
     part whose server HTML changed is swapped in place: no reload, no lost
     scroll position, no flash. While anything runs, it also refreshes every
     10 s, so durations move.
       * a part the user is working in (a focused field, selected text) waits
         for the next change;
       * data-live-frozen parts never change again (a finished run's page:
         its replay must not reset under the viewer);
       * data-live-manual parts change only when their own page asks (the run
         page's live view, once its run has finished).
     Compared with the HTML the server LAST sent for the part -- not with the
     DOM, which charts and highlighting have since changed. */
  const live = (() => {
    const hooks = [];
    let running = false, again = false, last = Date.now();
    document.querySelectorAll("[data-live]").forEach(el => { el._liveHtml = el.innerHTML; });

    function interacting(el) {
      const a = document.activeElement;
      if (a && a !== document.body && el.contains(a) &&
          a.matches("input, select, textarea, [contenteditable]")) return true;
      const sel = window.getSelection ? window.getSelection() : null;
      return !!(sel && sel.rangeCount && !sel.isCollapsed && el.contains(sel.anchorNode));
    }

    function swapIn(el, fresh) {
      // ticked checkboxes (the tests list's selection) and scroll offsets survive
      const ticked = new Set(Array.from(el.querySelectorAll("input[type=checkbox]:checked"))
                               .map(b => b.name + "=" + b.value));
      const scrolls = Array.from(el.querySelectorAll("*"))
        .filter(n => n.scrollTop || n.scrollLeft).map(n => [n.id, n.className, n.scrollTop, n.scrollLeft]);
      el.innerHTML = fresh.innerHTML;
      el._liveHtml = fresh.innerHTML;
      ["data-live-frozen", "data-live-manual"].forEach(name => {
        if (fresh.hasAttribute(name)) el.setAttribute(name, ""); else el.removeAttribute(name);
      });
      el.querySelectorAll("input[type=checkbox]").forEach(b => {
        if (ticked.has(b.name + "=" + b.value)) b.checked = true;
      });
      scrolls.forEach(([id, cls, top, left]) => {
        const n = id ? el.querySelector("#" + CSS.escape(id))
                     : (cls ? el.querySelector("." + String(cls).trim().split(/\s+/).map(CSS.escape).join(".")) : null);
        if (n) { n.scrollTop = top; n.scrollLeft = left; }
      });
      hooks.forEach(h => { try { h(el); } catch (e) { /* a widget failed: the data is still new */ } });
    }

    async function refresh(opts) {
      opts = opts || {};
      if (running) { again = true; return; }
      const wanted = opts.manual || [];
      const parts = Array.from(document.querySelectorAll("[data-live]")).filter(el => opts.force || (
        !el.hasAttribute("data-live-frozen") &&
        (!el.hasAttribute("data-live-manual") || wanted.indexOf(el.dataset.live) >= 0)));
      if (!parts.length) return;
      running = true;
      try {
        const resp = await fetch(window.location.href, { cache: "no-store", headers: { "X-Hub-Live": "1" } });
        if (!resp.ok) return;
        const doc = new DOMParser().parseFromString(await resp.text(), "text/html");
        parts.forEach(el => {
          const fresh = doc.querySelector('[data-live="' + CSS.escape(el.dataset.live) + '"]');
          if (!fresh || fresh.innerHTML === el._liveHtml || (!opts.force && interacting(el))) return;
          swapIn(el, fresh);
        });
        last = Date.now();
      } catch (e) {
        /* the next change tries again */
      } finally {
        running = false;
        if (again) { again = false; setTimeout(() => refresh(opts), 300); }
      }
    }
    return { refresh: refresh, onSwap: h => hooks.push(h), idleFor: () => Date.now() - last };
  })();
  window.hubLive = live;
  live.onSwap(root => { if (window.hubCharts) window.hubCharts.render(root, false); });
  live.onSwap(root => {
    if (window.hubHighlight) root.querySelectorAll("pre.code-view").forEach(window.hubHighlight);
  });

  /* ---- generic action buttons -------------------------------------------- */
  document.addEventListener("click", async (ev) => {
    const el = ev.target.closest("[data-run-test],[data-run-group],[data-kill-run],[data-kill-batch],[data-rescan],[data-sched-toggle],[data-sched-delete],[data-load-kill]");
    if (!el) return;
    ev.preventDefault();
    try {
      if (el.dataset.runTest) {
        const r = await post(u("/api/tests/run/"), { test_ids: [el.dataset.runTest] });
        toast("Queued " + el.dataset.runTest, "success");
        if (el.dataset.goBatch !== undefined) window.location = u("/batches/") + r.batch_id + "/";
      } else if (el.dataset.runGroup) {
        const r = await post(u("/api/groups/") + el.dataset.runGroup + "/run/", {});
        toast("Group queued (" + r.queued + " tests)", "success");
        window.location = u("/batches/") + r.batch_id + "/";
      } else if (el.dataset.loadKill) {
        if (!confirm("Stop applying load?\n\nPasses already in flight are left "
                     + "to finish on their own — cutting them off would poison "
                     + "the very timings you are collecting.")) return;
        await post(u("/api/load/") + el.dataset.loadKill + "/kill/", {});
        toast("Load test stopping", "success");
      } else if (el.dataset.killRun) {
        if (!confirm(el.dataset.queued ? "Cancel this queued run?" : "Kill this run?")) return;
        await post(u("/api/runs/") + el.dataset.killRun + "/kill/", {});
        toast(el.dataset.queued ? "Cancelled" : "Kill requested", "success");
      } else if (el.dataset.killBatch) {
        if (!confirm("Kill every active run in this batch?")) return;
        const r = await post(u("/api/batches/") + el.dataset.killBatch + "/kill/", {});
        toast("Killed " + r.killed + " run(s)", "success");
      } else if (el.dataset.rescan !== undefined) {
        const r = await post(u("/api/rescan/"), {});
        const s = r.summary;
        toast("Rescanned: " + s.created.length + " new, " + s.updated + " updated, "
              + s.archived.length + " archived"
              + (s.errors.length ? (" — " + s.errors.length + " warning(s)") : ""),
              s.errors.length ? "error" : "success");
        live.refresh({ force: true });
      } else if (el.dataset.schedToggle) {
        await post(u("/api/schedules/") + el.dataset.schedToggle + "/toggle/", {});
        live.refresh({ force: true });
      } else if (el.dataset.schedDelete) {
        if (!confirm("Delete this schedule?")) return;
        await post(u("/api/schedules/") + el.dataset.schedDelete + "/delete/", {});
        live.refresh({ force: true });
      }
    } catch (err) {
      toast(err.message, "error");
    }
  });

  /* ---- new-test form: suggest an id from the name --------------------------- */
  const idField = document.querySelector('input[name="test_id"]');
  const nameField = document.querySelector('input[name="name"]');
  if (idField && nameField) {
    let auto = !idField.value;
    idField.addEventListener("input", () => { auto = !idField.value; });
    nameField.addEventListener("input", () => {
      if (!auto) return;
      idField.value = nameField.value.toUpperCase()
        .replace(/[^A-Z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 24);
    });
  }

  /* ---- delete / storage actions --------------------------------------------- */
  const fmtMB = (b) => (b / 1e6).toFixed(1) + " MB";
  document.addEventListener("click", async (ev) => {
    const delRun = ev.target.closest("[data-delete-run]");
    if (delRun) {
      if (!confirm("Delete this run's files (video, screenshots, trace)?\n" +
                   "The run itself stays in the charts.")) return;
      try {
        const r = await post(u("/api/runs/" + delRun.dataset.deleteRun + "/delete/"), {});
        toast("Freed " + fmtMB(r.freed_bytes) + " — the run stays in the charts", "success");
        live.refresh({ force: true });
      } catch (err) { toast(err.message, "error"); }
    }

    const purgeTest = ev.target.closest("[data-purge-test]");
    if (purgeTest) {
      const keep = prompt("Delete this test's stored files (video/screenshots/trace).\n" +
        "The runs stay in the charts.\n\nHow many recent runs should keep their files?", "3");
      if (keep === null) return;
      try {
        const r = await post(u("/api/tests/" + purgeTest.dataset.purgeTest + "/purge/"),
                             { keep_last: parseInt(keep, 10) || 0 });
        toast("Cleared " + r.runs + " run(s), freed " + fmtMB(r.freed_bytes), "success");
        live.refresh({ force: true });
      } catch (err) { toast(err.message, "error"); }
    }

    const purgeAll = ev.target.closest("[data-purge-all]");
    if (purgeAll) {
      const whole = purgeAll.dataset.whole !== undefined;
      const status = purgeAll.dataset.status || null;
      const keepEl = document.getElementById("purge-keep");
      const keep = keepEl ? (parseInt(keepEl.value, 10) || 0) : 0;
      const what = whole ? "DELETE RUNS ENTIRELY (charts lose them)"
                         : "delete stored files" + (status ? " of passed runs" : "");
      if (!confirm("Across every test: " + what + ", keeping the newest " + keep +
                   " run(s) per test.\n\nContinue?")) return;
      const out = document.getElementById("storage-result");
      if (out) out.textContent = "working…";
      try {
        const r = await post(u("/api/purge-all/"),
                             { whole_runs: whole, keep_last: keep, status });
        toast("Cleared " + r.runs + " run(s), freed " + fmtMB(r.freed_bytes), "success");
        setTimeout(() => window.location.reload(), 1200);
      } catch (err) { toast(err.message, "error"); if (out) out.textContent = ""; }
    }

    const sCheck = ev.target.closest("[data-storage-check]");
    if (sCheck) {
      const out = document.getElementById("storage-result");
      out.textContent = "checking the bucket…";
      try {
        const r = await post(u("/api/storage/check/"), {});
        out.textContent = r.ok ? r.detail : ("not usable: " + r.error);
      } catch (err) { out.textContent = err.message; }
    }

    const sOff = ev.target.closest("[data-storage-offload]");
    if (sOff) {
      const out = document.getElementById("storage-result");
      out.textContent = "uploading existing artifacts…";
      try {
        const r = await post(u("/api/storage/offload/"), {});
        out.textContent = r.error ? r.error
          : ("uploaded " + r.uploaded_runs + " run(s)" +
             (r.failed_runs ? (", " + r.failed_runs + " failed") : ""));
      } catch (err) { out.textContent = err.message; }
    }
  });

  /* ---- settings page: full system check ------------------------------------- */
  const doctorBtn = document.getElementById("doctor-full");
  if (doctorBtn) {
    doctorBtn.addEventListener("click", async () => {
      doctorBtn.disabled = true;
      doctorBtn.textContent = "checking (launching a browser)…";
      try {
        const r = await fetch(u("/api/doctor/")).then(x => x.json());
        const body = document.querySelector("#doctor-table tbody");
        body.innerHTML = r.checks.map(c =>
          '<tr><td style="width:130px"><span class="badge ' +
          (c.status === "ok" ? "passed" : c.status === "warn" ? "timeout" : "failed") +
          '">' + esc(c.status) + '</span></td>' +
          '<td style="width:180px"><strong>' + esc(c.name) + '</strong></td>' +
          '<td>' + esc(c.detail) +
          (c.fix ? '<div class="help">&rarr; ' + esc(c.fix) + '</div>' : '') +
          '</td></tr>').join("");
        toast(r.summary.healthy ? "All checks passed" :
              (r.summary.fail + " check(s) failed"),
              r.summary.healthy ? "success" : "error");
      } catch (err) { toast(err.message, "error"); }
      doctorBtn.disabled = false;
      doctorBtn.textContent = "Re-check including a browser launch";
    });
  }

  /* ---- settings page: target check + artifact prune -------------------------- */
  document.addEventListener("click", async (ev) => {
    const tc = ev.target.closest("[data-target-check]");
    if (tc) {
      const out = document.getElementById("target-check-result");
      out.textContent = "checking…";
      try {
        const r = await post(u("/api/target-check/"), {});
        out.textContent = r.ok
          ? ("reachable — HTTP " + r.status + " in " + r.ms + "ms (" + r.url + ")")
          : ("NOT reachable from the hub: " + r.error);
      } catch (err) { out.textContent = err.message; }
    }
    const pr = ev.target.closest("[data-prune]");
    if (pr) {
      const days = parseInt(document.getElementById("prune-days").value, 10) || 30;
      if (!confirm("Delete run artifacts older than " + days + " days? Charts and history stay.")) return;
      const out = document.getElementById("prune-result");
      out.textContent = "pruning…";
      try {
        const r = await post(u("/api/prune-results/"), { days });
        out.textContent = "removed " + r.removed + " run folder(s), freed " +
          (r.freed_bytes / 1e6).toFixed(1) + " MB";
      } catch (err) { out.textContent = err.message; }
    }
    const ck = ev.target.closest("[data-set-clock-browser],[data-set-clock-manual]");
    if (ck) {
      // datetime-local format, from the browser's clock or the picker
      const pad = (n) => String(n).padStart(2, "0");
      let value;
      if (ck.dataset.setClockBrowser !== undefined) {
        const d = new Date();
        value = d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()) +
                "T" + pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());
      } else {
        value = document.getElementById("clock-manual").value;
        if (!value) { toast("Pick a date and time first", "error"); return; }
      }
      if (!confirm("Set this MACHINE's clock to " + value.replace("T", " ") + "?")) return;
      const out = document.getElementById("clock-result");
      out.textContent = "setting…";
      try {
        const r = await post(u("/api/clock/"), { datetime: value });
        out.textContent = "clock set to " + r.set_to;
        toast("Clock set — reloading", "success");
        setTimeout(() => window.location.reload(), 1200);
      } catch (err) { out.textContent = err.message; }
    }
  });

  /* ---- analytics: copy a CSV section --------------------------------------- */
  // navigator.clipboard needs a secure context, which http://lab-box:8880 is
  // not — so select + execCommand does the real work, and clicking into a
  // block selects the whole thing for a manual Ctrl-C as the fallback.
  document.addEventListener("click", async (ev) => {
    const btn = ev.target.closest("[data-copy]");
    if (btn) {
      const ta = document.getElementById(btn.dataset.copy);
      if (!ta) return;
      ta.focus(); ta.select(); ta.setSelectionRange(0, ta.value.length);
      let done = false;
      try { done = document.execCommand("copy"); } catch (e) { /* fall through */ }
      if (!done && navigator.clipboard) {
        try { await navigator.clipboard.writeText(ta.value); done = true; } catch (e) {}
      }
      toast(done ? "Copied — paste it into your chat tool"
                 : "Could not copy automatically — the text is selected, press Ctrl-C",
            done ? "success" : "error");
    }
    const ta = ev.target.closest("textarea.csv-text");
    if (ta && !ev.target.closest("[data-copy]")) { ta.select(); }
  });

  /* ---- settings page: live clock comparison --------------------------------- */
  const clockBrowser = document.getElementById("clock-browser");
  if (clockBrowser) {
    const serverEl = document.getElementById("clock-server");
    const deltaEl = document.getElementById("clock-delta");
    // The page rendered the server's time at load; advance both clocks
    // client-side so the comparison stays honest while the page sits open.
    const serverAtLoad = new Date(serverEl.textContent.replace(" ", "T"));
    const loadedAt = Date.now();
    setInterval(() => {
      const now = new Date();
      const server = new Date(serverAtLoad.getTime() + (Date.now() - loadedAt));
      const fmt = (d) => d.toLocaleString("sv-SE");   // YYYY-MM-DD HH:MM:SS
      serverEl.textContent = fmt(server);
      clockBrowser.textContent = fmt(now);
      const skew = Math.round((now - server) / 1000);
      deltaEl.textContent = Math.abs(skew) < 90
        ? "— clocks agree"
        : "— this machine is " + fmtDur(Math.abs(skew)) +
          (skew > 0 ? " behind" : " ahead of") + " your device";
    }, 1000);
  }

  /* ---- multi-select on the tests page -------------------------------------- */
  const runSelected = document.getElementById("run-selected");
  if (runSelected) {
    const boxes = () => Array.from(document.querySelectorAll("input.sel-test"));
    const refresh = () => {
      const n = boxes().filter(b => b.checked).length;
      runSelected.textContent = n ? ("Run selected (" + n + ")") : "Run selected";
      runSelected.disabled = !n;
    };
    document.addEventListener("change", (ev) => {
      if (ev.target.id === "sel-all") { boxes().forEach(b => { b.checked = ev.target.checked; }); refresh(); }
      else if (ev.target.classList && ev.target.classList.contains("sel-test")) refresh();
    });
    live.onSwap(refresh);
    refresh();
    runSelected.addEventListener("click", async () => {
      const ids = boxes().filter(b => b.checked).map(b => b.value);
      if (!ids.length) return;
      try {
        const r = await post(u("/api/tests/run/"), { test_ids: ids });
        window.location = u("/batches/") + r.batch_id + "/";
      } catch (err) { toast(err.message, "error"); }
    });
  }

  /* ---- live status: nav badge + activity strip + dashboard panel ------------
     One poll feeds all three. The strip lives in base.html, so EVERY page
     shows what is running and what is queued, each row one click from
     watching it and one from killing it; it disappears entirely when idle.
     The dashboard keeps its richer panel and polls faster (2s vs 5s). */
  const runningPanel = document.getElementById("running-now");
  const strip = document.getElementById("activity-strip");
  {
    const badge = document.getElementById("nav-run-badge");

    // The strip shows what is running RIGHT NOW (the queue is the dashboard's
    // job): load runs, each group run as one entry naming the tests it is
    // running this moment, and any other running test.
    function stripHtml(s) {
      const loadRuns = s.load_runs || [];
      const running = s.runs.filter(r => r.status === "running");
      const groups = (s.active_batches || []).filter(g => g.running > 0);
      const inGroup = new Set(running.filter(r => groups.some(g => g.id === r.batch_id)).map(r => r.id));
      const items = [];
      loadRuns.forEach(l => items.push(
        '<span class="as-item"><span class="badge running">load</span>' +
        '<a href="' + u("/load/") + l.id + '/" title="watch the load run"><strong>' + esc(l.test_id) + '</strong> ' +
          esc(l.label || "load test") + '</a>' +
        '<span class="as-meta">' + l.in_flight + ' in flight · ' + fmtDur(l.remaining) + ' left</span>' +
        '<button class="as-kill" data-load-kill="' + l.id + '" title="stop applying load">&#10005;</button></span>'));
      groups.forEach(g => {
        const now = running.filter(r => r.batch_id === g.id).map(r => r.test_id);
        const left = g.eta === null ? '' : (g.eta < 2 ? ' · finishing' : ' · ~' + fmtDur(g.eta) + ' left');
        items.push(
          '<span class="as-item as-group"><span class="badge running">' + (g.group ? 'group' : 'batch') + '</span>' +
          '<a href="' + u("/batches/") + g.id + '/" title="watch this group run"><strong>' + esc(g.group || g.label) + '</strong></a>' +
          '<span class="as-meta">running ' + esc(now.join(", ")) + ' · ' + g.done + '/' + g.total + ' done' + left + '</span>' +
          '<button class="as-kill" data-kill-batch="' + g.id + '" title="kill this ' + (g.group ? 'group' : 'batch') + ' run">&#10005;</button></span>');
      });
      running.filter(r => !inGroup.has(r.id)).forEach(r => items.push(
        '<span class="as-item"><span class="badge running">running</span>' +
        '<a href="' + u("/runs/") + r.id + '/" title="watch this run live"><strong>' + esc(r.test_id) + '</strong> ' + esc(r.name) + '</a>' +
        '<span class="as-meta">' + fmtDur(r.elapsed) +
          (r.eta !== null ? (r.eta < 2 ? ' · finishing' : ' · ~' + fmtDur(r.eta) + ' left') : '') + '</span>' +
        '<button class="as-kill" data-kill-run="' + r.id + '" title="kill this run">&#10005;</button></span>'));
      const MAX = 6;
      const more = items.length - MAX;
      return '<span class="as-count">' + (running.length + loadRuns.length) + ' running</span>' +
             items.slice(0, MAX).join("") +
             (more > 0 ? ('<a class="as-more" href="' + u("/") + '">+' + more + ' more &rarr;</a>') : '');
    }

    let lastSeen = null;
    async function tick() {
      try {
        const s = await fetch(u("/api/status/")).then(r => r.json());
        const loadRuns = s.load_runs || [];
        const busy = s.runs.length + loadRuns.length;
        // What changed since the last poll: a run queued, started or
        // finished; a load run started or ended (its pass count moves every
        // second -- its own page follows that). Then the page's live parts.
        const seen = JSON.stringify([
          s.runs.map(r => [r.id, r.status]), loadRuns.map(l => l.id),
          (s.recent_batches || []).map(b => [b.id, b.counts, b.active])]);
        if (lastSeen !== null && seen !== lastSeen) live.refresh();
        else if (busy && live.idleFor() > 10000) live.refresh();
        lastSeen = seen;
        if (badge) {
          badge.textContent = busy;
          badge.classList.toggle("zero", !busy);
        }
        if (strip) {
          const runningNow = loadRuns.length + s.runs.filter(r => r.status === "running").length;
          if (runningNow) { strip.innerHTML = stripHtml(s); strip.hidden = false; }
          else { strip.hidden = true; strip.innerHTML = ""; }
        }
        if (runningPanel) {
        const loadHtml = loadRuns.map(l =>
          '<div class="run-row">' +
            '<span class="badge running">load</span>' +
            '<div class="grow">' +
              '<a href="' + u("/load/") + l.id + '/"><strong>' + esc(l.test_id) + '</strong> ' +
              esc(l.label || "load test") + '</a>' +
              '<div class="meta">' + l.concurrency + ' browser(s) at once · ' +
                l.in_flight + ' in flight · ' + l.completed + ' pass(es) done' +
                (l.projected ? (' of ~' + l.projected) : '') +
                ' · ' + fmtDur(l.remaining) + ' left</div>' +
            '</div>' +
            '<button class="btn small danger" data-load-kill="' + l.id + '">Stop</button>' +
          '</div>').join("");
        if (!busy) {
          runningPanel.innerHTML = '<div class="empty">Nothing is running. Start something from ' +
            '<a href="' + u("/tests/") + '">Tests</a> or <a href="' + u("/groups/") + '">Groups</a>.</div>';
        } else {
          const runRow = (r, inGroup) => {
            const pct = (r.eta !== null && r.elapsed !== null && (r.elapsed + r.eta) > 0)
              ? Math.min(99, Math.round(100 * r.elapsed / (r.elapsed + r.eta))) : null;
            return '<div class="run-row">' +
              '<span class="badge ' + esc(r.status) + '">' + esc(r.status) + '</span>' +
              '<div class="grow">' +
                '<a href="' + u("/runs/") + r.id + '/"><strong>' + esc(r.test_id) + '</strong> ' + esc(r.name) + '</a>' +
                '<div class="meta">' + (r.batch_label && !inGroup ? ('batch: ' + esc(r.batch_label) + ' · ') : '') +
                  (r.status === "queued" ? 'waiting for a free slot · takes ' +
                     (r.eta === null ? 'unknown — no history yet' : '~' + fmtDur(r.eta))
                   : 'elapsed ' + fmtDur(r.elapsed) +
                     ' · est. remaining ' + (r.eta === null ? 'null — not enough data' : fmtDur(r.eta))) + '</div>' +
                (pct !== null && r.status === "running" ? ('<div class="progress"><div style="width:' + pct + '%"></div></div>') : '') +
              '</div>' +
              '<button class="btn small danger" data-kill-run="' + r.id + '">Kill</button>' +
            '</div>';
          };
          // A group run is one block: how far along it is, and when its last
          // test should be done (the hub simulates its own queue for that),
          // with the tests still running or waiting listed underneath.
          const groups = s.active_batches || [];
          const grouped = new Set();
          const groupHtml = groups.map(g => {
            const mine = s.runs.filter(r => r.batch_id === g.id);
            mine.forEach(r => grouped.add(r.id));
            const pct = (g.eta !== null && (g.elapsed + g.eta) > 0)
              ? Math.min(99, Math.round(100 * g.elapsed / (g.elapsed + g.eta)))
              : Math.round(100 * g.done / Math.max(1, g.total));
            const left = g.eta === null ? 'time left unknown — not every test has history yet'
                       : (g.eta < 2 ? 'finishing now' : '~' + fmtDur(g.eta) + ' left');
            return '<div class="group-run" data-batch="' + g.id + '">' +
              '<div class="run-row group-head">' +
                '<span class="badge running">' + (g.group ? 'group' : 'batch') + '</span>' +
                '<div class="grow">' +
                  '<a href="' + u("/batches/") + g.id + '/"><strong>' + esc(g.group || g.label) + '</strong></a>' +
                  ' <span class="help">' + esc(g.source) + (g.schedule ? ' · ' + esc(g.schedule) : '') + '</span>' +
                  '<div class="meta">' + g.done + ' of ' + g.total + ' done' +
                    (g.failed ? ' · <span class="bad">' + g.failed + ' failed</span>' : '') +
                    ' · ' + g.running + ' running · ' + g.queued + ' queued · <strong class="group-left">' + left + '</strong></div>' +
                  '<div class="progress" title="' + (g.eta !== null
                      ? 'time: ' + fmtDur(g.elapsed) + ' of about ' + fmtDur(g.elapsed + g.eta) +
                        ' (the tests left can be the long ones)'
                      : g.done + ' of ' + g.total + ' tests done') + '"><div style="width:' + pct + '%"></div></div>' +
                '</div>' +
                '<button class="btn small danger" data-kill-batch="' + g.id + '">Kill ' + (g.group ? 'group' : 'batch') + '</button>' +
              '</div>' +
              '<div class="group-runs">' + mine.map(r => runRow(r, true)).join("") + '</div>' +
            '</div>';
          }).join("");
          runningPanel.innerHTML = loadHtml + groupHtml +
            s.runs.filter(r => !grouped.has(r.id)).map(r => runRow(r, false)).join("");
        }
        }
        const batchesEl = document.getElementById("recent-batches-live");
        if (batchesEl && s.recent_batches) {
          batchesEl.innerHTML = s.recent_batches.map(b => {
            const parts = Object.entries(b.counts).map(([k, v]) => v + " " + k).join(", ");
            return '<div class="run-row">' +
              '<div class="grow"><a href="' + u("/batches/") + b.id + '/"><strong>' + esc(b.label || ("batch " + b.id)) + '</strong></a>' +
              '<div class="meta">' + esc(b.trigger) + ' · ' + esc(b.created) + ' · ' + esc(parts) + '</div></div>' +
              (b.active ? '<button class="btn small danger" data-kill-batch="' + b.id + '">Kill batch</button>' : '') +
              '</div>';
          }).join("") || '<div class="empty">No batches yet.</div>';
        }
      } catch (err) { /* transient; keep polling */ }
    }
    tick();
    setInterval(tick, runningPanel ? 2000 : 5000);
  }

  /* ---- run page: live view --------------------------------------------------- */
  const liveRoot = document.getElementById("live-run");
  if (liveRoot) {
    const runId = liveRoot.dataset.runId;
    const img = document.getElementById("live-img");
    const logEl = document.getElementById("live-log");
    const statusEl = document.getElementById("live-status");
    const metaEl = document.getElementById("live-meta");
    let wasActive = null;
    async function tick() {
      try {
        const s = await fetch(u("/api/runs/") + runId + "/live/").then(r => r.json());
        if (statusEl) statusEl.innerHTML = '<span class="badge ' + esc(s.status) + '">' + esc(s.status) + '</span>';
        if (metaEl) {
          metaEl.textContent = s.active
            ? ("elapsed " + fmtDur(s.elapsed) + " · est. remaining " +
               (s.eta === null ? "null — not enough data" : fmtDur(s.eta)))
            : (s.duration != null ? ("finished in " + fmtDur(s.duration)) : "");
        }
        if (logEl && s.log_tail) {
          const stick = logEl.scrollTop + logEl.clientHeight >= logEl.scrollHeight - 30;
          logEl.textContent = s.log_tail;
          if (stick) logEl.scrollTop = logEl.scrollHeight;
        }
        if (img) {
          if (s.live_image) { img.src = s.live_image; img.style.display = "block"; }
          else if (!s.active) { img.style.display = "none"; }
        }
        if (wasActive === true && !s.active) {
          live.refresh({ manual: ["run"] });   // the final results replace the live view
          return;
        }
        wasActive = s.active;
        if (!s.active) return; // stop polling
      } catch (err) { /* keep trying */ }
      setTimeout(tick, 1500);
    }
    tick();
  }

  /* ---- live load run ---------------------------------------------------- */
  const loadLive = document.getElementById("lr-live");
  if (loadLive) {
    const id = document.querySelector("[data-load-kill]")?.dataset.loadKill;
    if (id) {
      const set = (elId, value) => {
        const node = document.getElementById(elId);
        if (node) node.textContent = value;
      };
      const tick = async () => {
        let d;
        try { d = await (await fetch(u("/api/load/") + id + "/live/")).json(); }
        catch (e) { return; }
        set("lr-inflight", d.in_flight);
        set("lr-done", (d.summary && d.summary.completed) || 0);
        set("lr-left", d.remaining_seconds === null ? "—" : fmtDur(d.remaining_seconds));
        set("lr-p95", d.summary && d.summary.p95 ? d.summary.p95 + "s" : "—");
        const badge = document.getElementById("lr-status");
        if (badge) badge.textContent = d.status;
        if (!d.active) { clearInterval(timer); live.refresh({ manual: ["load"] }); }
      };
      const timer = setInterval(tick, 2000);
      tick();
    }
  }

  /* ---- activity replay player (finished runs) ------------------------------- */
  // A function, not load-time code: when a run finishes while its page is
  // open, the final results (replay included) are swapped in live.
  function initReel() {
    const reelData = document.getElementById("frames-data");
    if (!reelData || reelData._reelReady) return;
    reelData._reelReady = true;
    let frames = [];
    try { frames = JSON.parse(reelData.textContent) || []; } catch (e) { frames = []; }
    const img = document.getElementById("reel-img");
    const slider = document.getElementById("reel-slider");
    const playBtn = document.getElementById("reel-play");
    const speedSel = document.getElementById("reel-speed");
    const timeEl = document.getElementById("reel-time");
    const gapEl = document.getElementById("reel-gap");
    let idx = 0, timer = null, idleRun = 0;

    // The hubfmt `at` filter, exactly -- so the replay's clock and the step
    // list print the same time the same way: +0.72s, +1:02.35, +1:02:03.50.
    function fmtAt(sec) {
      if (sec < 60) return "+" + sec.toFixed(2) + "s";
      if (sec < 3600) {
        const m = Math.floor(sec / 60);
        return "+" + m + ":" + (sec - m * 60).toFixed(2).padStart(5, "0");
      }
      const h = Math.floor(sec / 3600), m = Math.floor((sec - h * 3600) / 60);
      return "+" + h + ":" + String(m).padStart(2, "0") + ":" +
             (sec - h * 3600 - m * 60).toFixed(2).padStart(5, "0");
    }

    // Step timings in sync with the replay (Python runs, and TypeScript runs
    // that kept their trace: same clock). The step running at the frame's
    // moment is highlighted, and any ctx.timed() / test.step() block around
    // it; clicking a step shows the replay at that step.
    const steps = Array.from(document.querySelectorAll("#step-timings.synced tbody tr[data-start]"))
      .map(tr => ({ tr: tr, start: parseFloat(tr.dataset.start), end: parseFloat(tr.dataset.end),
                    block: tr.dataset.block === "1" }));
    const stepsBox = document.getElementById("steps-scroll");
    const PAINT_LAG = 0.4;      // a step's result reaches the screen a moment after it returns
    function keepVisible(tr) {
      if (!stepsBox) return;
      const box = stepsBox.getBoundingClientRect(), r = tr.getBoundingClientRect();
      if (r.top < box.top || r.bottom > box.bottom) stepsBox.scrollTop += (r.top - box.top) - box.height / 3;
    }
    // `chosen`: the step the user clicked -- it stays the highlighted one
    // until the replay moves on, even when the nearest picture was taken a
    // moment before it began
    function highlight(sec, chosen) {
      let current = chosen || null;
      steps.forEach(s => {
        s.tr.classList.remove("now", "now-block");
        if (s.block) {
          // a finished block stays lit for the paint lag -- unless the next
          // one has begun (a run of short test.step()s would all light up)
          const followed = steps.some(o => o.block && o !== s && o.start >= s.end - 0.001 && o.start <= sec);
          if (s.start <= sec && sec <= s.end + PAINT_LAG && !followed) s.tr.classList.add("now-block");
        } else if (!chosen && s.start <= sec) {
          current = s;                      // rows are in start order: the last one wins
        }
      });
      if (current) { current.tr.classList.add("now"); keepVisible(current.tr); }
    }
    function frameFor(step) {
      // The step's last picture -- up to PAINT_LAG after it returns, but never
      // past the start of the NEXT step (+0.1 s for its first paint): after a
      // long wait the lag window used to reach several fast steps ahead, and
      // clicking "wait for the email" showed the password-changed page.
      const next = steps.filter(o => !o.block && o !== step && o.start >= step.end - 0.001)
                        .reduce((m, o) => Math.min(m, o.start), Infinity);
      const from = step.start * 1000 - 50,
            to = Math.min(step.end + PAINT_LAG, next + 0.1) * 1000;
      let best = -1;
      frames.forEach((f, i) => { if (f.t >= from && f.t <= to) best = i; });   // the last frame of the step
      // No picture of its own (frames are saved only when the page changes):
      // the latest one up to it -- never one from later in the run
      if (best < 0) frames.forEach((f, i) => { if (f.t <= to) best = i; });
      return best < 0 ? 0 : best;                                                // a step before the first paint
    }
    function show(i, viaPlay, chosen) {
      idx = Math.max(0, Math.min(frames.length - 1, i));
      img.src = frames[idx].u;
      slider.value = idx;
      timeEl.textContent = fmtAt(frames[idx].t / 1000);
      highlight(frames[idx].t / 1000, chosen);
      // A long wait is now a RUN of ~15s gaps (the harness samples an
      // unattended page sparsely), not one big jump. Accumulate them so the
      // banner answers "how long have we been waiting" instead of flashing
      // "skipped 16s" a hundred times over.
      const gap = idx > 0 ? frames[idx].t - frames[idx - 1].t : 0;
      if (viaPlay && gap > 5000) {
        idleRun += gap;
        gapEl.textContent = "\u23ED waiting \u2014 skipped " + fmtDur(idleRun / 1000);
        gapEl.hidden = false;
        clearTimeout(gapEl._t);
        gapEl._t = setTimeout(() => { gapEl.hidden = true; idleRun = 0; }, 1400);
      } else if (viaPlay) {
        idleRun = 0;                    // the test is doing things again
        clearTimeout(gapEl._t);
        gapEl.hidden = true;
      } else {
        idleRun = 0;
        gapEl.hidden = true;
      }
      if (idx + 1 < frames.length) { const pre = new Image(); pre.src = frames[idx + 1].u; }
    }
    function stop() {
      clearInterval(timer); timer = null;
      playBtn.innerHTML = "&#9654; Play";
    }
    function play() {
      if (timer) { stop(); return; }
      if (idx >= frames.length - 1) idx = -1;
      idleRun = 0;
      playBtn.innerHTML = "&#10074;&#10074; Pause";
      timer = setInterval(() => {
        if (idx >= frames.length - 1) { stop(); return; }
        show(idx + 1, true);
      }, 1000 / parseInt(speedSel.value, 10));
    }
    playBtn.addEventListener("click", play);
    speedSel.addEventListener("change", () => { if (timer) { stop(); play(); } });
    slider.addEventListener("input", () => { stop(); show(parseInt(slider.value, 10), false); });
    document.getElementById("reel-prev").addEventListener("click", () => { stop(); show(idx - 1, false); });
    document.getElementById("reel-next").addEventListener("click", () => { stop(); show(idx + 1, false); });
    steps.forEach(step => {
      const go = () => {
        stop();
        show(frameFor(step), false, step);
        document.getElementById("reel").scrollIntoView({ block: "nearest", behavior: "smooth" });
      };
      step.tr.addEventListener("click", go);
      step.tr.addEventListener("keydown", e => {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); go(); }
      });
    });
    if (frames.length) show(0, false);
  }
  initReel();
  live.onSwap(root => { if (root.querySelector("#frames-data")) initReel(); });

  /* ---- remote recorder (record through the hub, in a tab) ------------------- */
  const rrecBtn = document.getElementById("rrec-start");
  if (rrecBtn) {
    let polling = false;
    async function watchRemote() {
      if (polling) return;
      polling = true;
      const state = document.getElementById("rec-state");
      for (;;) {
        await new Promise(r => setTimeout(r, 1200));
        let s;
        try { s = await fetch(u("/api/remote-rec/status/")).then(r => r.json()); }
        catch (e) { continue; }
        if (s.state === "recording") {
          if (state) state.textContent = "remote recording… " + (s.step_count || 0) +
            " step(s) captured in the recorder tab";
        } else if (s.state === "done") {
          // the recorder tab owns the handoff now; just offer a manual pull
          // in case that tab was closed without choosing anything
          if (state) state.innerHTML = "recording finished in the other tab" +
            (s.code ? ' — <button type="button" class="btn small" id="rrec-pull">insert it here</button>' : "");
          const pull = document.getElementById("rrec-pull");
          if (pull) pull.addEventListener("click", () => {
            if (window.hubSetCode) window.hubSetCode(s.code);
            else document.getElementById("id_code").value = s.code;
            post(u("/api/remote-rec/clear/"), {}).catch(() => {});
            state.textContent = "recorded code inserted below";
          });
          break;
        } else if (s.state === "error") {
          toast("Remote recorder: " + (s.error || "failed"), "error");
          break;
        } else if (s.state === "idle") {
          break; // discarded from the recorder tab
        }
      }
      polling = false;
    }
    rrecBtn.addEventListener("click", () => {
      // window.open MUST be the first, synchronous act of the click gesture:
      // opening after an awaited fetch loses the user-gesture token and
      // popup blockers silently eat the tab. The recorder page starts (or
      // attaches to) the session itself on load.
      const testId = rrecBtn.dataset.testId || "";
      const win = window.open(u("/recorder/") + (testId ? ("?test=" + encodeURIComponent(testId)) : ""), "_blank");
      if (!win) {
        toast("The browser blocked the recorder tab — allow pop-ups for this site, "
              + "or open /recorder/ manually.", "error");
        return;
      }
      watchRemote();
    });
  }

  /* ---- recorder (playwright codegen) -------------------------------------------- */
  const recBtn = document.getElementById("rec-start");
  if (recBtn) {
    const stopBtn = document.getElementById("rec-stop");
    const state = document.getElementById("rec-state");
    let polling = null;
    function setState(text) { if (state) state.textContent = text; }
    async function poll() {
      const s = await fetch(u("/api/recorder/status/")).then(r => r.json());
      if (s.state === "recording") {
        setState("recording… " + s.seconds + "s (a browser window is open on the server machine)");
        stopBtn.disabled = false;
        polling = setTimeout(poll, 1200);
      } else if (s.state === "done") {
        stopBtn.disabled = true; recBtn.disabled = false;
        setState("recording finished — code inserted below; edit before saving");
        if (s.code) if (window.hubSetCode) window.hubSetCode(s.code); else document.getElementById("id_code").value = s.code;
      } else {
        setState(""); stopBtn.disabled = true; recBtn.disabled = false;
      }
    }
    recBtn.addEventListener("click", async () => {
      try {
        await post(u("/api/recorder/start/"), {});
        recBtn.disabled = true;
        setState("starting codegen…");
        poll();
      } catch (err) { toast(err.message, "error"); }
    });
    stopBtn.addEventListener("click", async () => {
      clearTimeout(polling);
      try {
        const s = await post(u("/api/recorder/stop/"), {});
        if (s.code) if (window.hubSetCode) window.hubSetCode(s.code); else document.getElementById("id_code").value = s.code;
        setState("recording finished — code inserted below; edit before saving");
        recBtn.disabled = false; stopBtn.disabled = true;
      } catch (err) { toast(err.message, "error"); }
    });
  }
})();
