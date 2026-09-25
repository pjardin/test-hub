/* Acme Freight -- the little JavaScript the demo site has.
 *
 * Progressive only: every page renders and every form submits without it.
 * Loaded as a file (never inline) so the healthy releases can send
 * Content-Security-Policy: script-src 'self'. Chart.js is the hub's own
 * vendored copy; no network is ever needed.
 */
(function () {
  "use strict";

  function $(id) { return document.getElementById(id); }

  function readJSON(id) {
    var el = $(id);
    if (!el) return null;
    try { return JSON.parse(el.textContent); } catch (e) { return null; }
  }

  function csrf() {
    var m = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
    return m ? decodeURIComponent(m[1]) : "";
  }

  function cssVar(name) {
    return getComputedStyle(document.body).getPropertyValue(name).trim() || "#2462c4";
  }

  function toast(text, isError) {
    var box = $("toasts");
    if (!box) return;
    var t = document.createElement("div");
    t.className = "toast" + (isError ? " error" : "");
    t.textContent = text;
    box.appendChild(t);
    setTimeout(function () { t.remove(); }, 4000);
  }

  // --- city autocomplete (server-side suggestions into a <datalist>) --------
  function initAutocomplete() {
    var url = document.body.dataset.citiesUrl;
    document.querySelectorAll("input[data-autocomplete]").forEach(function (input) {
      var list = $(input.getAttribute("list"));
      var timer = null, last = "";
      input.addEventListener("input", function () {
        clearTimeout(timer);
        timer = setTimeout(function () {
          var q = input.value.trim();
          if (q.length < 2 || q === last || !list) return;
          last = q;
          fetch(url + "?q=" + encodeURIComponent(q), { credentials: "same-origin" })
            .then(function (r) { return r.json(); })
            .then(function (data) {
              list.replaceChildren.apply(list, (data.results || []).map(function (c) {
                var o = document.createElement("option");
                o.value = c.label;
                return o;
              }));
            })
            .catch(function () { /* suggestions are a nicety */ });
        }, 150);
      });
    });
  }

  // --- confirmations, dialogs, print ------------------------------------------
  function initDialogs() {
    document.querySelectorAll("form[data-confirm]").forEach(function (form) {
      form.addEventListener("submit", function (e) {
        if (!window.confirm(form.dataset.confirm)) e.preventDefault();
      });
    });
    document.querySelectorAll("[data-opens]").forEach(function (btn) {
      btn.addEventListener("click", function () { $(btn.dataset.opens).showModal(); });
    });
    document.querySelectorAll("[data-close]").forEach(function (btn) {
      btn.addEventListener("click", function () { $(btn.dataset.close).close(); });
    });
    document.querySelectorAll("[data-print]").forEach(function (btn) {
      btn.addEventListener("click", function () { window.print(); });
    });
  }

  // --- notes on a shipment (JSON POST; the 2.0.0 release fails every third) ----
  function initNotes() {
    var form = $("note-form");
    if (!form) return;
    var err = $("note-error");
    form.addEventListener("submit", function (e) {
      e.preventDefault();
      var area = form.querySelector("textarea");
      var text = area.value.trim();
      err.hidden = true;
      if (!text) { err.textContent = "Write something first"; err.hidden = false; return; }
      var btn = form.querySelector("button[type=submit]");
      btn.disabled = true;
      fetch(form.dataset.url, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRFToken": csrf() },
        body: JSON.stringify({ text: text })
      }).then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (data) {
          if (r.ok) {
            var empty = $("no-notes");
            if (empty) empty.remove();
            var li = document.createElement("li");
            li.className = "note";
            var span = document.createElement("span");
            span.className = "note-text";
            span.textContent = data.note.text;
            var meta = document.createElement("span");
            meta.className = "muted";
            meta.textContent = " — " + data.note.by + ", " + data.note.at;
            li.append(span, meta);
            $("notes-list").append(li);
            area.value = "";
            toast("Note saved");
          } else {
            err.textContent = data.error || ("Could not save the note (HTTP " + r.status + ")");
            err.hidden = false;
            toast("Could not save the note", true);
          }
        });
      }).catch(function () {
        err.textContent = "Could not reach the server -- try again";
        err.hidden = false;
      }).finally(function () { btn.disabled = false; });
    });
  }

  // --- charts ---------------------------------------------------------------------
  function chartsReady() {
    if (typeof Chart === "undefined") return false;
    Chart.defaults.animation = false;          // deterministic screenshots, fewer repaints
    Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
    Chart.defaults.color = cssVar("--muted");
    return true;
  }

  function simple(canvasId, type, series, colors) {
    var canvas = $(canvasId);
    if (!canvas || !series) return;
    var doughnut = type === "doughnut";
    new Chart(canvas, {
      type: type,
      data: { labels: series.labels, datasets: [{
        data: series.values,
        backgroundColor: doughnut ? colors : colors[0],
        borderColor: colors[0], borderWidth: doughnut ? 0 : 2,
        pointRadius: type === "line" ? 3 : 0, fill: false, tension: 0.25 }] },
      options: { responsive: true, maintainAspectRatio: false,
                 plugins: { legend: { display: doughnut, position: "bottom" } },
                 scales: doughnut ? {} : { y: { beginAtZero: type === "bar" } } }
    });
  }

  function initStaticCharts() {
    var palette = [cssVar("--primary"), cssVar("--accent"), "#98a2b3", "#f79009"];
    var dash = readJSON("dash-charts");
    if (dash) {
      simple("chart-daily", "bar", dash.daily, palette);
      simple("chart-ontime", "line", dash.on_time, palette);
      simple("chart-services", "doughnut", dash.services, palette);
    }
    var rep = readJSON("report-charts");
    if (rep) {
      simple("report-daily", "bar", rep.daily, palette);
      simple("report-services", "doughnut", rep.services, palette);
    }
  }

  function jobChart(kind, series) {
    var canvas = $("job-chart");
    if (!canvas) return null;
    var names = kind === "optimize" ? ["current plan", "best so far"]
              : kind === "manifest" ? ["priced", "with problems"] : ["done (%)"];
    var colors = kind === "manifest" ? ["#12b76a", "#d92d20"] : ["#98a2b3", cssVar("--primary")];
    var datasets = names.map(function (name, i) {
      return { label: name, data: [], borderColor: colors[i % colors.length],
               backgroundColor: colors[i % colors.length], borderWidth: i === names.length - 1 ? 3 : 1.5,
               pointRadius: 0, tension: 0.2 };
    });
    var chart = new Chart(canvas, {
      type: "line", data: { labels: [], datasets: datasets },
      options: { responsive: true, maintainAspectRatio: false,
                 plugins: { legend: { position: "bottom" } },
                 scales: { x: { ticks: { maxTicksLimit: 8 } } } }
    });
    chart.addPoints = function (points) {
      points.forEach(function (p) {
        chart.data.labels.push(p[0]);
        datasets.forEach(function (d, i) { d.data.push(p[i + 1]); });
      });
      chart.update();
    };
    chart.addPoints(series || []);
    return chart;
  }

  // --- a running job: poll, update, reload when it finishes -------------------------
  function initJob(charts) {
    var box = $("job");
    if (!box) return;
    var chart = charts ? jobChart(box.dataset.kind, readJSON("job-series")) : null;
    var state = box.dataset.state;
    if (state !== "queued" && state !== "running") return;

    var logSeq = 0;
    document.querySelectorAll("#job-log li[data-seq]").forEach(function (li) {
      logSeq = Math.max(logSeq, parseInt(li.dataset.seq, 10) || 0);
    });
    var seriesTotal = (readJSON("job-series") || []).length;
    var progress = $("job-progress"), bar = progress.querySelector(".bar");
    var cancel = $("cancel-job");
    if (cancel) {
      cancel.addEventListener("click", function () {
        cancel.disabled = true;
        fetch(box.dataset.cancelUrl, { method: "POST", credentials: "same-origin",
                                       headers: { "X-CSRFToken": csrf() } });
      });
    }

    function poll() {
      fetch(box.dataset.statusUrl + "?log=" + logSeq + "&series=" + seriesTotal,
            { credentials: "same-origin", headers: { "Accept": "application/json" } })
        .then(function (r) {
          if (r.status === 401 || r.status === 404) { location.reload(); return null; }
          return r.json();
        })
        .then(function (s) {
          if (!s) return;
          bar.style.width = s.percent + "%";
          progress.setAttribute("aria-valuenow", s.percent);
          $("job-percent").textContent = s.percent + "%";
          $("job-stage").textContent = s.stage;
          $("job-elapsed").textContent = s.elapsed;
          var badge = $("job-state");
          badge.textContent = s.state;
          badge.className = "badge state-" + s.state;
          var log = $("job-log");
          s.log.forEach(function (line) {
            var li = document.createElement("li");
            li.dataset.seq = line.seq;
            var t = document.createElement("time");
            t.textContent = line.t + "s";
            li.append(t, " " + line.text);
            log.append(li);
            logSeq = line.seq;
          });
          if (s.log.length) log.scrollTop = log.scrollHeight;
          if (chart && s.series.length) chart.addPoints(s.series);
          seriesTotal = s.series_total;
          if (s.state === "done" || s.state === "failed" || s.state === "cancelled") {
            setTimeout(function () { location.reload(); }, 250);
            return;
          }
          setTimeout(poll, 1000);
        })
        .catch(function () { setTimeout(poll, 2000); });   // a blip: keep going
    }
    setTimeout(poll, 600);
  }

  // --- dock schedule: HTML5 drag-and-drop; the SERVER decides ------------------
  function initDocks() {
    var box = $("docks");
    if (!box) return;
    var err = $("dock-error");

    function refuse(message, card) {
      err.textContent = message;
      err.hidden = false;
      if (card) {
        card.classList.add("shake");
        setTimeout(function () { card.classList.remove("shake"); }, 600);
      }
    }

    function send(body, card) {
      fetch(box.dataset.url, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRFToken": csrf() },
        body: JSON.stringify(body)
      }).then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (data) {
          if (r.ok) location.reload();              // the server-rendered board is the truth
          else refuse(data.error || ("The server said HTTP " + r.status), card);
        });
      }).catch(function () { refuse("Could not reach the server -- try again", card); });
    }

    document.querySelectorAll(".dock-card[draggable]").forEach(function (card) {
      card.addEventListener("dragstart", function (e) {
        e.dataTransfer.setData("text/plain", card.dataset.arrival);
        e.dataTransfer.effectAllowed = "move";
        card.classList.add("dragging");
      });
      card.addEventListener("dragend", function () { card.classList.remove("dragging"); });
    });
    document.querySelectorAll(".dock-cell, #arrivals").forEach(function (zone) {
      zone.addEventListener("dragover", function (e) { e.preventDefault(); zone.classList.add("drop-target"); });
      zone.addEventListener("dragleave", function () { zone.classList.remove("drop-target"); });
      zone.addEventListener("drop", function (e) {
        e.preventDefault();
        zone.classList.remove("drop-target");
        var id = e.dataTransfer.getData("text/plain");
        if (!id) return;
        var card = document.querySelector('.dock-card[data-arrival="' + id + '"]');
        err.hidden = true;
        if (zone.id === "arrivals") send({ action: "unassign", arrival: id }, card);
        else send({ action: "assign", arrival: id, door: zone.dataset.door, slot: zone.dataset.slot }, card);
      });
    });
  }

  // --- live fleet: poll every 2 s, glide the trucks, count ETAs down ------------
  function initFleet() {
    var box = $("fleet");
    if (!box) return;
    function clock(s) {
      s = Math.max(0, s | 0);
      var m = Math.floor(s / 60), r = s % 60;
      return m + ":" + (r < 10 ? "0" : "") + r;
    }
    function apply(snap) {
      snap.trucks.forEach(function (t) {
        var g = document.querySelector('g.truck[data-truck="' + t.id + '"]');
        if (g) g.style.transform = "translate(" + t.x + "px, " + t.y + "px)";
        var row = document.querySelector('#fleet-table tr[data-truck="' + t.id + '"]');
        if (!row) return;
        row.dataset.status = t.status;
        row.dataset.next = t.next_stop;
        row.dataset.nextArrival = t.next_arrival;
        row.dataset.eta = t.eta_s;
        row.querySelector(".st").textContent = t.status;
        row.querySelector(".nx").textContent = t.next_stop;
        row.querySelector(".eta").textContent = clock(t.eta_s);
      });
      var list = $("fleet-events");
      list.replaceChildren.apply(list, snap.events.map(function (e) {
        var li = document.createElement("li");
        li.dataset.arrival = e.id;
        li.dataset.truck = e.truck;
        li.dataset.stop = e.stop;
        var t = document.createElement("time");
        t.textContent = e.clock;
        var p = document.createElement("span");
        p.className = "punctual" + (e.punctual === "on time" ? "" : " late");
        p.textContent = "(" + e.punctual + ")";
        li.append(t, " " + e.text + " ", p);
        return li;
      }));
    }
    function poll() {
      fetch(box.dataset.url, { credentials: "same-origin" })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (snap) { if (snap) apply(snap); })
        .catch(function () { /* a blip: the next poll catches up */ })
        .then(function () { setTimeout(poll, 2000); });
    }
    setInterval(function () {                    // between polls, the ETAs keep ticking
      document.querySelectorAll("#fleet-table tr[data-eta]").forEach(function (row) {
        var s = Math.max(0, parseInt(row.dataset.eta, 10) - 1);
        row.dataset.eta = s;
        row.querySelector(".eta").textContent = clock(s);
      });
    }, 1000);
    poll();
  }

  // --- developers page: send the example request for real ------------------------
  function initTryApi() {
    var btn = $("try-api");
    if (!btn) return;
    btn.addEventListener("click", function () {
      var out = $("api-response");
      out.hidden = false;
      out.textContent = "GET " + btn.dataset.url + " ...";
      fetch(btn.dataset.url, { headers: { "X-API-Key": btn.dataset.key } })
        .then(function (r) {
          return r.text().then(function (text) {
            var shown = text;
            try { shown = JSON.stringify(JSON.parse(text), null, 2); } catch (e) { /* not JSON */ }
            out.textContent = "HTTP " + r.status + "\n\n" + shown;
          });
        })
        .catch(function () { out.textContent = "Could not reach the API"; });
    });
  }

  // --- mailbox: reload when newer mail has landed -----------------------------------
  function initMailbox() {
    var box = $("mailbox");
    if (!box || !box.dataset.pollUrl) return;
    var newest = parseInt(box.dataset.newest || "0", 10);
    setInterval(function () {
      fetch(box.dataset.pollUrl, { headers: { Accept: "application/json" }, cache: "no-store" })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (d) { if (d && d.newest > newest) location.reload(); })
        .catch(function () { /* the next tick tries again */ });
    }, 2000);
  }

  document.addEventListener("DOMContentLoaded", function () {
    initAutocomplete();
    initMailbox();
    initDialogs();
    initNotes();
    initDocks();
    initFleet();
    initTryApi();
    document.querySelectorAll("select[data-autosubmit]").forEach(function (sel) {
      sel.addEventListener("change", function () { sel.form.submit(); });
    });
    var charts = chartsReady();
    if (charts) initStaticCharts();
    initJob(charts);
  });
})();
