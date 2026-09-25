/* Chart layer. Wraps the vendored Chart.js with this app's design tokens:
   status colors for outcomes, categorical slots for series, hairline grids,
   thin rounded marks, tooltips on hover, legends only when >= 2 series.
   Charts re-render when the OS theme flips so dark mode is real, not a
   filter. Data arrives via <script type="application/json"> tags. */
(function () {
  "use strict";
  if (typeof Chart === "undefined") return;

  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const registry = [];

  function tokens() {
    return {
      ink2: css("--ink-2"), muted: css("--muted"), grid: css("--grid"),
      surface: css("--surface"), accent: css("--accent"), accent2: css("--accent-2"),
      slot3: css("--slot-3"), slot4: css("--slot-4"),
      slot5: css("--slot-5"), slot6: css("--slot-6"),
      good: css("--good"), critical: css("--critical"),
      warning: css("--warning"), serious: css("--serious"),
    };
  }

  function statusColor(t, status) {
    return { passed: t.good, failed: t.critical, error: t.serious,
             timeout: t.warning }[status] || t.muted;
  }

  function baseOptions(t, opts) {
    return Object.assign({
      responsive: true,
      maintainAspectRatio: false,
      animation: { duration: 250 },
      plugins: {
        legend: { display: false, labels: { color: t.ink2, boxWidth: 12, boxHeight: 12 } },
        tooltip: { intersect: false, mode: "nearest" },
      },
      scales: {
        x: { ticks: { color: t.muted, maxRotation: 0, autoSkip: true },
             grid: { display: false }, border: { color: t.grid } },
        y: { beginAtZero: true, ticks: { color: t.muted },
             grid: { color: t.grid }, border: { display: false } },
      },
    }, opts || {});
  }

  const builders = {
    /* stacked outcomes per day: [{day, passed, failed, other}] */
    days(data, t) {
      const labels = data.map(d => d.day.slice(5));
      const mk = (label, key, color) => ({
        label, data: data.map(d => d[key]), backgroundColor: color,
        stack: "runs", borderColor: t.surface, borderWidth: 1,
        borderRadius: 3, barPercentage: 0.75, categoryPercentage: 0.8,
      });
      const opts = baseOptions(t);
      opts.plugins.legend.display = true;
      opts.scales.x.stacked = true;
      opts.scales.y.stacked = true;
      opts.scales.y.ticks.precision = 0;
      return {
        type: "bar",
        // neutral "other" sits BETWEEN passed and failed: no red/green
        // adjacency in the stack (deutan-safe, validated)
        data: { labels, datasets: [
          mk("passed", "passed", t.good),
          mk("other", "other", t.muted),
          mk("failed", "failed", t.critical),
        ]},
        options: opts,
      };
    },

    /* pass-rate % per day: [{day, pass_rate}] */
    passrate(data, t) {
      const opts = baseOptions(t);
      opts.scales.y.max = 100;
      opts.plugins.tooltip.callbacks = {
        label: (c) => c.parsed.y === null ? "no judged runs" : c.parsed.y.toFixed(0) + "% passed",
      };
      return {
        type: "line",
        data: {
          labels: data.map(d => d.day.slice(5)),
          datasets: [{
            label: "pass rate %",
            data: data.map(d => d.pass_rate),
            borderColor: t.accent, backgroundColor: t.accent,
            borderWidth: 2, pointRadius: 3, pointHoverRadius: 6,
            spanGaps: true, tension: 0.25,
          }],
        },
        options: opts,
      };
    },

    /* per-run duration trend, points colored by outcome:
       [{when, status, duration, version, id}] */
    // Load run: response time against wall-clock, with how many passes each
    // bucket held. A flat line means the application shrugged the load off; a
    // line that climbs shows where it started to hurt, which is the whole
    // reason to run one. Two series on ONE axis (both are seconds) -- never a
    // second y-axis.
    loadtimeline(data, t) {
      const opts = baseOptions(t);
      opts.plugins.legend.display = true;
      opts.plugins.tooltip.callbacks = {
        title: (items) => "at " + data[items[0].dataIndex].t + "s",
        label: (c) => {
          const d = data[c.dataIndex];
          if (c.dataset.label === "p95 (s)") return "p95: " + d.p95 + "s";
          return "average: " + d.avg + "s  ·  " + d.n + " pass(es)" +
                 (d.failed ? "  ·  " + d.failed + " failed" : "");
        },
      };
      opts.scales.x.title = { display: true, text: "seconds into the run" };
      opts.scales.y.title = { display: true, text: "seconds per pass" };
      return {
        type: "line",
        data: {
          labels: data.map(d => d.t),
          datasets: [
            { label: "average (s)", data: data.map(d => d.avg),
              borderColor: t.accent, backgroundColor: t.accent,
              borderWidth: 2, pointRadius: 3, tension: 0.25 },
            { label: "p95 (s)", data: data.map(d => d.p95),
              borderColor: t.accent2, backgroundColor: t.accent2,
              borderWidth: 2, borderDash: [5, 4], pointRadius: 0, tension: 0.25 },
          ],
        },
        options: opts,
      };
    },

    duration(data, t) {
      const pts = data.filter(d => d.duration !== null);
      const rolling = [];
      const win = [];
      pts.forEach(d => {
        win.push(d.duration);
        if (win.length > 7) win.shift();
        rolling.push(win.reduce((a, b) => a + b, 0) / win.length);
      });
      const opts = baseOptions(t);
      opts.plugins.legend.display = true;
      opts.plugins.tooltip.callbacks = {
        title: (items) => pts[items[0].dataIndex].when,
        label: (c) => {
          const d = pts[c.dataIndex];
          if (c.dataset.label !== "duration (s)") return "7-run average: " + c.parsed.y.toFixed(1) + "s";
          return d.status + " · " + d.duration + "s · version " + d.version;
        },
      };
      opts.onClick = (ev, els) => {
        if (els.length) window.location = (window.HUB_PREFIX || "") + "/runs/" + pts[els[0].index].id + "/";
      };
      return {
        type: "line",
        data: {
          labels: pts.map(d => d.when.slice(5, 16)),
          datasets: [
            {
              label: "duration (s)", data: pts.map(d => d.duration),
              borderColor: t.accent, borderWidth: 2, tension: 0.2,
              pointRadius: 4, pointHoverRadius: 7,
              pointBackgroundColor: pts.map(d => statusColor(t, d.status)),
              pointBorderColor: t.surface, pointBorderWidth: 1,
            },
            {
              label: "7-run average", data: rolling,
              borderColor: t.accent2, borderWidth: 2, borderDash: [6, 4],
              pointRadius: 0, tension: 0.3,
            },
          ],
        },
        options: opts,
      };
    },

    /* outcomes stacked per version: [{version, passed, failed, other}] */
    versions(data, t) {
      const opts = baseOptions(t);
      opts.plugins.legend.display = true;
      opts.scales.x.stacked = true;
      opts.scales.y.stacked = true;
      opts.scales.y.ticks.precision = 0;
      const mk = (label, key, color) => ({
        label, data: data.map(d => d[key]), backgroundColor: color,
        stack: "v", borderColor: t.surface, borderWidth: 1,
        borderRadius: 3, barPercentage: 0.6,
      });
      return {
        type: "bar",
        data: { labels: data.map(d => d.version),
                datasets: [mk("passed", "passed", t.good),
                           mk("other", "other", t.muted),
                           mk("failed", "failed", t.critical)] },
        options: opts,
      };
    },

    /* horizontal top-N: rows [{name/test_id, avg_duration, p95_duration}] */
    slowest(data, t) {
      const opts = baseOptions(t, { indexAxis: "y" });
      opts.plugins.legend.display = true;
      opts.scales = {
        x: { beginAtZero: true, ticks: { color: t.muted }, grid: { color: t.grid },
             border: { display: false }, title: { display: true, text: "seconds", color: t.muted } },
        y: { ticks: { color: t.ink2, autoSkip: false }, grid: { display: false },
             border: { color: t.grid } },
      };
      return {
        type: "bar",
        data: {
          labels: data.map(d => d.test_id),
          datasets: [
            { label: "average", data: data.map(d => d.avg_duration),
              backgroundColor: t.accent, borderRadius: 3, barPercentage: 0.65 },
            { label: "p95", data: data.map(d => d.p95_duration),
              backgroundColor: t.accent2, borderRadius: 3, barPercentage: 0.65 },
          ],
        },
        options: opts,
      };
    },

    /* horizontal flakiness %: rows [{test_id, flakiness}] */
    flakiest(data, t) {
      const opts = baseOptions(t, { indexAxis: "y" });
      opts.scales = {
        x: { beginAtZero: true, max: 100, ticks: { color: t.muted },
             grid: { color: t.grid }, border: { display: false },
             title: { display: true, text: "% of adjacent runs that flip", color: t.muted } },
        y: { ticks: { color: t.ink2, autoSkip: false }, grid: { display: false },
             border: { color: t.grid } },
      };
      return {
        type: "bar",
        data: { labels: data.map(d => d.test_id),
                datasets: [{ label: "flakiness", data: data.map(d => Math.round(d.flakiness * 100)),
                             backgroundColor: t.accent, borderRadius: 3, barPercentage: 0.65 }] },
        options: opts,
      };
    },

    /* per-step timing across runs: {labels: [...], series: [{name, data}]} */
    timings(data, t) {
      const colors = [t.accent, t.accent2, t.slot3, t.slot4, t.slot5, t.slot6];
      const opts = baseOptions(t);
      opts.plugins.legend.display = true;
      opts.scales.y.title = { display: true, text: "milliseconds", color: t.muted };
      opts.plugins.tooltip.callbacks = {
        label: (c) => c.dataset.label + ": " + (c.parsed.y === null ? "—" : c.parsed.y + "ms"),
      };
      return {
        type: "line",
        data: {
          labels: data.labels,
          datasets: data.series.map((s, i) => ({
            label: s.name.length > 42 ? s.name.slice(0, 40) + "…" : s.name,
            data: s.data,
            borderColor: colors[i % colors.length],
            backgroundColor: colors[i % colors.length],
            borderWidth: 2, pointRadius: 2.5, pointHoverRadius: 6,
            spanGaps: true, tension: 0.2,
          })),
        },
        options: opts,
      };
    },

    /* duration distribution: {labels: [...], counts: [...]} */
    histogram(data, t) {
      const opts = baseOptions(t);
      opts.scales.x.title = { display: true, text: "seconds", color: t.muted };
      opts.scales.y.title = { display: true, text: "runs", color: t.muted };
      opts.scales.y.ticks.precision = 0;
      opts.plugins.tooltip.callbacks = {
        title: (items) => "around " + items[0].label + "s",
        label: (c) => c.parsed.y + " run(s)",
      };
      return {
        type: "bar",
        data: { labels: data.labels,
                datasets: [{ label: "runs", data: data.counts,
                             backgroundColor: t.accent, borderRadius: 3,
                             barPercentage: 0.98, categoryPercentage: 0.99 }] },
        options: opts,
      };
    },

    /* slowest steps suite-wide: [{label, avg_ms, p95_ms}] */
    steps(data, t) {
      const opts = baseOptions(t, { indexAxis: "y" });
      opts.plugins.legend.display = true;
      opts.scales = {
        x: { beginAtZero: true, ticks: { color: t.muted }, grid: { color: t.grid },
             border: { display: false },
             title: { display: true, text: "milliseconds", color: t.muted } },
        y: { ticks: { color: t.ink2, autoSkip: false, font: { size: 10 } },
             grid: { display: false }, border: { color: t.grid } },
      };
      return {
        type: "bar",
        data: {
          labels: data.map(d => d.label),
          datasets: [
            { label: "average", data: data.map(d => d.avg_ms),
              backgroundColor: t.accent, borderRadius: 3, barPercentage: 0.7 },
            { label: "p95", data: data.map(d => d.p95_ms),
              backgroundColor: t.accent2, borderRadius: 3, barPercentage: 0.7 },
          ],
        },
        options: opts,
      };
    },

    /* group batches over time: {labels, rates, totals} */
    grouptrend(data, t) {
      const opts = baseOptions(t);
      opts.scales.y.max = 100;
      opts.scales.y.title = { display: true, text: "% passed", color: t.muted };
      opts.plugins.tooltip.callbacks = {
        label: (c) => (c.parsed.y === null ? "no judged runs"
                       : c.parsed.y + "% of " + data.totals[c.dataIndex] + " test(s) passed"),
      };
      return {
        type: "line",
        data: { labels: data.labels,
                datasets: [{ label: "batch pass rate", data: data.rates,
                             borderColor: t.accent, backgroundColor: t.accent,
                             borderWidth: 2, pointRadius: 4, pointHoverRadius: 7,
                             spanGaps: true, tension: 0.25 }] },
        options: opts,
      };
    },

    /* trigger split: {manual: n, group: n, ...} */
    triggers(data, t) {
      const order = ["manual", "selection", "group", "schedule"];
      const labels = order.filter(k => data[k]);
      Object.keys(data).forEach(k => { if (!order.includes(k)) labels.push(k); });
      const colors = [t.accent, t.accent2, t.slot3, t.slot4, t.muted];
      const opts = baseOptions(t);
      opts.scales = {};
      opts.plugins.legend = { display: true, position: "right",
                              labels: { color: css("--ink-2"), boxWidth: 12 } };
      opts.cutout = "62%";
      return {
        type: "doughnut",
        data: { labels,
                datasets: [{ data: labels.map(k => data[k]),
                             backgroundColor: labels.map((_, i) => colors[i % colors.length]),
                             borderColor: t.surface, borderWidth: 2 }] },
        options: opts,
      };
    },
  };

  // Build the charts inside `root` -- the whole page, or one part of it that
  // a live update just replaced (then without the entry animation: a chart
  // that re-animates every few seconds reads as flicker, not as news).
  function render(root, animate) {
    const t = tokens();
    Chart.defaults.font.family = 'system-ui, -apple-system, "Segoe UI", sans-serif';
    Chart.defaults.font.size = 11;
    // charts whose canvas left the page (its part was replaced) are dropped
    for (let i = registry.length - 1; i >= 0; i--) {
      if (!registry[i].canvas || !registry[i].canvas.isConnected) {
        registry[i].destroy();
        registry.splice(i, 1);
      }
    }
    root.querySelectorAll("canvas[data-chart]").forEach(canvas => {
      if (Chart.getChart(canvas)) return;
      const kind = canvas.dataset.chart;
      const srcEl = document.getElementById(canvas.dataset.src);
      if (!builders[kind] || !srcEl) return;
      let data;
      try { data = JSON.parse(srcEl.textContent); } catch (e) { return; }
      const empty = !data || (Array.isArray(data) && !data.length) ||
                    (!Array.isArray(data) && !Object.keys(data).length);
      const wrap = canvas.parentElement;
      if (empty) {
        wrap.innerHTML = '<div class="empty">No data yet — run some tests first.</div>';
        return;
      }
      const config = builders[kind](data, t);
      if (animate === false) config.options = Object.assign({}, config.options, { animation: false });
      registry.push(new Chart(canvas.getContext("2d"), config));
    });
  }

  function renderAll() {
    registry.forEach(c => c.destroy());
    registry.length = 0;
    render(document, true);
  }

  window.hubCharts = { render: render, renderAll: renderAll };
  document.addEventListener("DOMContentLoaded", renderAll);
  const mq = window.matchMedia("(prefers-color-scheme: dark)");
  if (mq.addEventListener) mq.addEventListener("change", renderAll);
})();
