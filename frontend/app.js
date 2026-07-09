/* Insight Lens — build-free React (React + htm, no bundler). */
const html = htm.bind(React.createElement);
const { useState, useEffect, useRef } = React;

/* ----------------------------- API helpers ----------------------------- */
async function apiJSON(path, body) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `Request failed (${res.status})`);
  return data;
}

/* ----------------------------- formatting ----------------------------- */
function fmtNum(n) {
  if (n === null || n === undefined || isNaN(n)) return "—";
  const a = Math.abs(n);
  if (a >= 1000)
    return new Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 2 }).format(n);
  return new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 }).format(n);
}
const cap = (s) => (s || "").replace(/[_\-]+/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());

/* rate/ratio-like metrics are averaged when bucketing; counts are summed */
const AVG_HINTS = ["rate", "pct", "percent", "ratio", "avg", "average", "mean", "nps", "score", "cpdo", "aov", "distance", "retention", "share"];
const isAvgMetric = (n) => { const s = (n || "").toLowerCase(); return AVG_HINTS.some((h) => s.includes(h)); };

// Metrics where a LOWER number is better (going up is bad). Everything else: higher is better.
const LOWER_BETTER = ["rto", "return", "cancel", "shrinkage", "cpdo", "distance", "nqd",
  "complaint", "defect", "churn", "cost", "delay", "tat", "bounce", "reject", "loss", "leakage"];
const isLowerBetter = (n) => { const s = (n || "").toLowerCase(); return LOWER_BETTER.some((h) => s.includes(h)); };
// Classify a signed change as good/bad/flat for a metric, accounting for its polarity.
const goodness = (metric, change) => {
  if (!change) return "flat";
  const up = change > 0;
  return (up !== isLowerBetter(metric)) ? "good" : "bad";
};

/* aggregate a daily series into 7-day (weekly) buckets anchored at the first date */
function toWeekly(series, avg) {
  const out = { labels: [], values: [], dateToWeek: {} };
  if (!series || !series.labels || !series.labels.length) return out;
  const first = new Date(series.labels[0] + "T00:00:00").getTime();
  const wkOf = (d) => Math.floor((new Date(d + "T00:00:00").getTime() - first) / (7 * 86400000));
  const buckets = {};
  series.labels.forEach((d, i) => {
    const wk = wkOf(d);
    if (!buckets[wk]) buckets[wk] = { sum: 0, count: 0, start: d };
    buckets[wk].sum += series.values[i];
    buckets[wk].count += 1;
  });
  const keys = Object.keys(buckets).map(Number).sort((a, b) => a - b);
  out.labels = keys.map((k) => buckets[k].start);
  out.values = keys.map((k) => Number((avg ? buckets[k].sum / buckets[k].count : buckets[k].sum).toFixed(4)));
  series.labels.forEach((d) => { out.dateToWeek[d] = buckets[wkOf(d)].start; });
  return out;
}

/* Meesho Grocery logo (official lockup) */
function MGLogo({ size = 36 }) {
  return html`<img src="/mg-logo.png" alt="Meesho Grocery"
      style=${{ height: size + "px", width: "auto", display: "block" }} />`;
}

/* ----------------------------- Top bar ----------------------------- */
function TopBar({ session, onLogout, onReset }) {
  return html`
    <div className="topbar">
      <div className="brand">
        <${MGLogo} size=${38} />
        <div>
          <div className="title">Pulse MG</div>
          <div className="subtitle">Anomaly & root-cause intelligence</div>
        </div>
      </div>
      ${session &&
      html`<div className="right">
        <span className="pill">${session.username} · ${session.division}</span>
        ${onReset && html`<button className="linkbtn" onClick=${onReset}>New analysis</button>`}
        <button className="linkbtn" onClick=${onLogout}>Sign out</button>
      </div>`}
    </div>
  `;
}

/* ----------------------------- Login ----------------------------- */
function Login({ onLogin }) {
  const [divisions, setDivisions] = useState([]);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [division, setDivision] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  useEffect(() => {
    fetch("/api/divisions")
      .then((r) => r.json())
      .then((d) => {
        setDivisions(d.divisions || []);
        setDivision((d.divisions || [])[0] || "");
      })
      .catch(() => {});
  }, []);

  async function submit(e) {
    e.preventDefault();
    setErr("");
    setBusy(true);
    try {
      const s = await apiJSON("/api/login", { username, password, division });
      onLogin(s);
    } catch (e2) {
      setErr(e2.message);
    } finally {
      setBusy(false);
    }
  }

  return html`
    <div className="center-wrap">
      <form className="card login-card" onSubmit=${submit}>
        <div className="login-head">
          <div style=${{ display: "flex", justifyContent: "center", marginBottom: 14 }}>
            <${MGLogo} size=${64} />
          </div>
          <h1>Welcome to Pulse MG</h1>
          <p>Anomaly & root-cause intelligence — visualize trends that matter to you.</p>
        </div>
        <label className="field"><span>Username</span>
          <input value=${username} onInput=${(e) => setUsername(e.target.value)} placeholder="e.g. meghna.verma" autoFocus />
        </label>
        <label className="field"><span>Password</span>
          <input type="password" value=${password} onInput=${(e) => setPassword(e.target.value)} placeholder="Any password (demo)" />
        </label>
        <label className="field"><span>Business division</span>
          <select value=${division} onChange=${(e) => setDivision(e.target.value)}>
            ${divisions.map((d) => html`<option key=${d} value=${d}>${d}</option>`)}
          </select>
        </label>
        ${err && html`<div className="err">${err}</div>`}
        <button className="btn" disabled=${busy} style=${{ marginTop: 6 }}>
          ${busy ? html`<span className="spinner"></span>` : "Sign in"}
        </button>
      </form>
    </div>
  `;
}

/* ----------------------------- Upload + Mapper ----------------------------- */
function Setup({ session, onDone }) {
  const [info, setInfo] = useState(null);
  const [uploading, setUploading] = useState(false);
  const [drag, setDrag] = useState(false);
  const [err, setErr] = useState("");

  // mapping state
  const [dateCol, setDateCol] = useState("");
  const [metricCols, setMetricCols] = useState([]);
  const [dimCol, setDimCol] = useState("");
  const [dimValues, setDimValues] = useState([]);
  const [dimValue, setDimValue] = useState("__all__");
  const [sensitivity, setSensitivity] = useState("medium");
  const [analyzing, setAnalyzing] = useState(false);
  const fileRef = useRef(null);

  async function handleFile(file) {
    if (!file) return;
    setErr("");
    setUploading(true);
    try {
      const content = await file.text();
      const data = await apiJSON("/api/upload", { filename: file.name, content });
      setInfo(data);
      setDateCol(data.inferred.date_col || "");
      setMetricCols(data.inferred.metric_cols || []);
      setDimCol(data.inferred.dimension_col || "");
      setDimValue("__all__");
    } catch (e) {
      setErr(e.message);
    } finally {
      setUploading(false);
    }
  }

  useEffect(() => {
    if (!info || !dimCol) {
      setDimValues([]);
      return;
    }
    apiJSON("/api/dimension-values", { upload_id: info.upload_id, dimension_col: dimCol })
      .then((d) => setDimValues(d.values || []))
      .catch(() => setDimValues([]));
    setDimValue("__all__");
  }, [dimCol, info]);

  function toggleMetric(name) {
    setMetricCols((prev) => (prev.includes(name) ? prev.filter((m) => m !== name) : [...prev, name]));
  }

  async function analyze() {
    setErr("");
    setAnalyzing(true);
    try {
      const reqParams = {
        upload_id: info.upload_id,
        division: session.division,
        sensitivity,
        mapping: {
          date_col: dateCol,
          metric_cols: metricCols,
          dimension_col: dimCol || null,
          dimension_value: dimCol ? dimValue : null,
        },
      };
      const result = await apiJSON("/api/analyze", reqParams);
      onDone(result, reqParams);
    } catch (e) {
      setErr(e.message);
    } finally {
      setAnalyzing(false);
    }
  }

  const numericCols = info ? info.columns.filter((c) => c.dtype === "numeric") : [];
  const catCols = info ? info.columns.filter((c) => c.dtype === "categorical") : [];

  return html`
    <div className="wrap">
      <div className="section-title">Step 1 · Upload your dashboard export
        <span className="hint">Any CSV — daily metrics, one or many divisions.</span>
      </div>

      ${!info &&
      html`<div
        className=${"dropzone" + (drag ? " drag" : "")}
        onClick=${() => fileRef.current && fileRef.current.click()}
        onDragOver=${(e) => { e.preventDefault(); setDrag(true); }}
        onDragLeave=${() => setDrag(false)}
        onDrop=${(e) => { e.preventDefault(); setDrag(false); handleFile(e.dataTransfer.files[0]); }}>
        <div className="big">📈</div>
        <h3>${uploading ? "Uploading…" : "Drop a CSV here, or click to browse"}</h3>
        <p>We’ll auto-detect your date, metric and division columns.</p>
        <input ref=${fileRef} type="file" accept=".csv" style=${{ display: "none" }}
          onChange=${(e) => handleFile(e.target.files[0])} />
      </div>`}

      ${err && html`<div className="err">${err}</div>`}

      ${info &&
      html`<div className="stack">
        <div className="card pad">
          <div className="rowflex">
            <div><strong>${info.filename}</strong> <span className="muted">· ${info.row_count} rows · ${info.columns.length} columns</span></div>
            <button className="btn ghost wauto" onClick=${() => { setInfo(null); setErr(""); }}>Replace file</button>
          </div>
        </div>

        <div className="section-title">Step 2 · Map your columns
          <span className="hint">We pre-filled these — adjust if needed.</span>
        </div>

        <div className="card pad">
          <div className="grid2">
            <div className="mapper-row">
              <span className="lbl">📅 Date column</span>
              <select value=${dateCol} onChange=${(e) => setDateCol(e.target.value)}>
                ${info.columns.map((c) => html`<option key=${c.name} value=${c.name}>${c.name} (${c.dtype})</option>`)}
              </select>
            </div>
            <div className="mapper-row">
              <span className="lbl">🏷️ Segment / division column (optional)</span>
              <select value=${dimCol} onChange=${(e) => setDimCol(e.target.value)}>
                <option value="">None — analyse the whole file</option>
                ${catCols.map((c) => html`<option key=${c.name} value=${c.name}>${c.name} (${c.cardinality} values)</option>`)}
              </select>
            </div>
          </div>

          ${dimCol &&
          html`<div className="mapper-row">
            <span className="lbl">Filter to a specific ${dimCol}</span>
            <select value=${dimValue} onChange=${(e) => setDimValue(e.target.value)} style=${{ maxWidth: 320 }}>
              <option value="__all__">All (${dimCol} combined)</option>
              ${dimValues.map((v) => html`<option key=${v} value=${v}>${v}</option>`)}
            </select>
          </div>`}

          <div className="mapper-row">
            <span className="lbl">📊 Metrics to analyse ${metricCols.length ? `(${metricCols.length} selected)` : ""}</span>
            <div className="chips">
              ${numericCols.map(
                (c) => html`<div key=${c.name}
                  className=${"chip" + (metricCols.includes(c.name) ? " on" : "")}
                  onClick=${() => toggleMetric(c.name)}>${cap(c.name)}</div>`
              )}
            </div>
          </div>

          <div className="mapper-row">
            <span className="lbl">🎚️ Anomaly sensitivity</span>
            <div className="seg">
              ${["low", "medium", "high"].map(
                (s) => html`<button key=${s} className=${sensitivity === s ? "on" : ""}
                  onClick=${() => setSensitivity(s)}>${cap(s)}</button>`
              )}
            </div>
            <span className="muted" style=${{ fontSize: 12, marginLeft: 10 }}>
              Higher = flags more, smaller deviations.
            </span>
          </div>
        </div>

        <div className="section-title">Data preview</div>
        <div className="tablewrap">
          <table className="preview">
            <thead><tr>${info.columns.map((c) => html`<th key=${c.name}>${c.name}<br /><span className="dtype">${c.dtype}</span></th>`)}</tr></thead>
            <tbody>
              ${info.preview.map((row, i) => html`<tr key=${i}>${info.columns.map((c) => html`<td key=${c.name}>${row[c.name]}</td>`)}</tr>`)}
            </tbody>
          </table>
        </div>

        <div className="rowflex" style=${{ marginTop: 6, justifyContent: "flex-end" }}>
          <button className="btn amber wauto" disabled=${analyzing || !dateCol || metricCols.length === 0} onClick=${analyze}>
            ${analyzing ? html`<span className="spinner"></span> Analysing…` : "Detect anomalies & explain →"}
          </button>
        </div>
      </div>`}
    </div>
  `;
}

/* ----------------------------- Trend chart ----------------------------- */
const SEV_COLOR = { critical: "#d62839", high: "#e2620a", moderate: "#e08600" };

// Colorblind-safe categorical palette (Okabe–Ito) for multi-metric overlay
const PALETTE = ["#0072B2", "#E69F00", "#009E73", "#D55E00", "#CC79A7", "#56B4E9", "#8a5a00", "#111111"];

function TrendChart({ seriesByMetric, metrics, anomalies, indexed }) {
  const canvasRef = useRef(null);
  const chartRef = useRef(null);

  useEffect(() => {
    if (!metrics || !metrics.length || !canvasRef.current) return;
    const labels = (seriesByMetric[metrics[0]] || {}).labels || [];
    const multi = metrics.length > 1;
    let datasets;

    if (!multi) {
      const m = metrics[0];
      const s = seriesByMetric[m] || { labels: [], values: [] };
      const anomByDate = {};
      (anomalies || []).filter((a) => a.metric === m).forEach((a) => { anomByDate[a.date] = a.severity; });
      const ptColors = s.labels.map((d) => (anomByDate[d] ? SEV_COLOR[anomByDate[d]] : "#7a0b57"));
      const ptRadius = s.labels.map((d) => (anomByDate[d] ? 6 : 2));
      const mean = s.values.length ? s.values.reduce((x, y) => x + y, 0) / s.values.length : 0;
      datasets = [
        {
          label: cap(m), data: s.values, borderColor: "#7a0b57", backgroundColor: "rgba(122,11,87,.08)",
          borderWidth: 2, tension: 0.25, fill: true, pointBackgroundColor: ptColors, pointBorderColor: ptColors,
          pointRadius: ptRadius, pointHoverRadius: 7, order: 1,
        },
        {
          label: `Average (${fmtNum(Number(mean.toFixed(2)))})`, data: s.labels.map(() => mean),
          borderColor: "#f7961e", borderWidth: 1.5, borderDash: [6, 5], pointRadius: 0, pointHoverRadius: 0,
          fill: false, tension: 0, order: 0,
        },
      ];
    } else {
      datasets = metrics.map((m, i) => {
        const s = seriesByMetric[m] || { labels: [], values: [] };
        const color = PALETTE[i % PALETTE.length];
        let data = s.values;
        if (indexed) {
          const base = s.values.find((v) => v != null && v !== 0);
          data = s.values.map((v) => (base ? Number(((v / base) * 100).toFixed(1)) : v));
        }
        return {
          label: cap(m), data,
          borderColor: color, backgroundColor: color, borderWidth: 2, tension: 0.25, fill: false,
          pointRadius: 0, pointHoverRadius: 5,
        };
      });
    }

    if (chartRef.current) chartRef.current.destroy();
    chartRef.current = new Chart(canvasRef.current, {
      type: "line",
      data: { labels, datasets },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: { display: true, position: "bottom", labels: { boxWidth: 12, boxHeight: 2, font: { size: 11 } } },
          tooltip: {
            callbacks: {
              afterBody: (items) => {
                if (multi) return "";
                const d = items[0].label;
                const a = (anomalies || []).find((x) => x.metric === metrics[0] && x.date === d);
                return a ? `⚠ ${a.severity} ${a.direction}: ${a.pct_change > 0 ? "+" : ""}${a.pct_change}%` : "";
              },
            },
          },
        },
        scales: {
          x: { grid: { display: false }, ticks: { maxTicksLimit: 10 } },
          y: (multi && indexed)
            ? { grid: { color: "#eee" }, title: { display: true, text: "Indexed (100 = first point)" } }
            : { grid: { color: "#eee" }, ticks: { callback: (v) => fmtNum(v) } },
        },
      },
    });
    return () => { if (chartRef.current) { chartRef.current.destroy(); chartRef.current = null; } };
  }, [seriesByMetric, metrics, anomalies, indexed]);

  return html`<div className="chart-box"><canvas id="trendCanvas" ref=${canvasRef}></canvas></div>`;
}

/* Tabular view of a metric's series (chronological), anomalies highlighted.
   The change column is Day-on-day in daily mode, Week-on-week in weekly mode
   (driven by the Daily/Weekly toggle via periodLabel). */
function TrendTable({ series, metric, anomalies, periodLabel }) {
  const anom = {};
  (anomalies || []).filter((a) => a.metric === metric).forEach((a) => { anom[a.date] = a; });
  const { labels, values } = series;
  const pct = (v, base) => (base != null && base !== 0) ? ((v - base) / Math.abs(base)) * 100 : null;
  const cell = (p) => {
    if (p === null || !isFinite(p)) return html`<td>—</td>`;
    const g = goodness(metric, p);
    const cls = g === "good" ? "up" : g === "bad" ? "down" : "";
    return html`<td className=${cls}>${(p > 0 ? "+" : "") + p.toFixed(1)}%</td>`;
  };
  const rows = labels.map((d, i) => ({ d, v: values[i], prev: i > 0 ? values[i - 1] : null }));
  const dateHeader = (periodLabel || "").indexOf("Week") === 0 ? "Week of" : "Date";
  return html`
    <div className="tablewrap" style=${{ maxHeight: 320, overflowY: "auto" }}>
      <table className="preview">
        <thead><tr>
          <th>${dateHeader}</th>
          <th>${cap(metric)}</th>
          <th>${periodLabel || "Day-on-day"}</th>
          <th>Status</th>
        </tr></thead>
        <tbody>
          ${rows.map(({ d, v, prev }) => {
            const a = anom[d];
            return html`<tr key=${d} style=${a ? { background: "#fdf3f4" } : {}}>
              <td>${d}</td>
              <td>${fmtNum(v)}</td>
              ${cell(pct(v, prev))}
              <td>${a ? html`<span className=${"badge " + a.severity}>${a.severity}</span>` : ""}</td>
            </tr>`;
          })}
        </tbody>
      </table>
    </div>`;
}

/* Tabular view of several metrics at once — each cell shows the value and its
   period-over-period change, coloured good/bad for that metric. Anomaly cells tinted. */
function TrendTableMulti({ seriesByMetric, metrics, anomalies, periodLabel }) {
  const anomSev = {};
  (anomalies || []).forEach((a) => { anomSev[a.metric + "|" + a.date] = a.severity; });
  const dateSet = {};
  metrics.forEach((m) => ((seriesByMetric[m] || {}).labels || []).forEach((d) => { dateSet[d] = 1; }));
  const dates = Object.keys(dateSet).sort();
  const val = {}, chg = {};
  metrics.forEach((m) => {
    const s = seriesByMetric[m] || { labels: [], values: [] };
    val[m] = {}; chg[m] = {};
    s.labels.forEach((d, i) => {
      val[m][d] = s.values[i];
      const prev = i > 0 ? s.values[i - 1] : null;
      chg[m][d] = (prev != null && prev !== 0) ? ((s.values[i] - prev) / Math.abs(prev)) * 100 : null;
    });
  });
  const dateHeader = (periodLabel || "").indexOf("Week") === 0 ? "Week of" : "Date";
  return html`
    <div className="tablewrap" style=${{ maxHeight: 360, overflowY: "auto" }}>
      <table className="preview">
        <thead><tr>
          <th>${dateHeader}</th>
          ${metrics.map((m) => html`<th key=${m}>${cap(m)}</th>`)}
        </tr></thead>
        <tbody>
          ${dates.map((d) => html`<tr key=${d}>
            <td>${d}</td>
            ${metrics.map((m) => {
              const v = val[m][d];
              const c = chg[m][d];
              const sev = anomSev[m + "|" + d];
              const g = c == null ? "flat" : goodness(m, c);
              const cls = g === "good" ? "up" : g === "bad" ? "down" : "";
              const arrow = c == null ? "" : c > 0 ? "▲" : c < 0 ? "▼" : "▬";
              return html`<td key=${m} style=${sev ? { background: "#fdf3f4" } : {}}>
                <div>${v == null ? "—" : fmtNum(v)}</div>
                ${c != null && html`<div className=${"cellchg " + cls}>${arrow} ${(c > 0 ? "+" : "") + c.toFixed(1)}%</div>`}
              </td>`;
            })}
          </tr>`)}
        </tbody>
      </table>
    </div>`;
}

/* Multi-select dropdown (with search) for choosing which metrics to plot */
function MetricDropdown({ metrics, selected, onToggle }) {
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState("");
  const ref = useRef(null);
  useEffect(() => {
    if (!open) return;
    const onDoc = (e) => { if (ref.current && !ref.current.contains(e.target)) setOpen(false); };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [open]);
  const label = selected.length === 0 ? "Select metrics"
    : selected.length === 1 ? cap(selected[0])
    : `${selected.length} metrics`;
  const ql = q.trim().toLowerCase();
  const shown = ql ? metrics.filter((m) => cap(m).toLowerCase().includes(ql) || m.toLowerCase().includes(ql)) : metrics;
  return html`<div className="mdrop" ref=${ref}>
    <button className="mdrop-btn" onClick=${() => setOpen((o) => !o)}>
      <span>${label}</span><span className="mdrop-caret">▾</span>
    </button>
    ${open && html`<div className="mdrop-panel">
      <input className="mdrop-search" type="text" placeholder="Search metrics…" value=${q}
        autoFocus onChange=${(e) => setQ(e.target.value)} />
      ${shown.length === 0
        ? html`<div className="mdrop-empty">No matching metrics</div>`
        : shown.map((m) => html`<label key=${m} className="mdrop-item">
            <input type="checkbox" checked=${selected.includes(m)} onChange=${() => onToggle(m)} />
            <span>${cap(m)}</span>
          </label>`)}
    </div>`}
  </div>`;
}

/* ----------------------------- Dashboard ----------------------------- */
function Dashboard({ result, params }) {
  // Full (unfiltered) date bounds — from the initial analysis; stay fixed for the pickers.
  const fullMetrics = Object.keys(result.series || {});
  const fullLabels = ((result.series[fullMetrics[0]] || {}).labels) || [];
  const minDate = fullLabels[0] || "";
  const maxDate = fullLabels[fullLabels.length - 1] || "";

  // Working analysis — re-scoped on the backend when the date range changes.
  const [data, setData] = useState(result);
  const [reloading, setReloading] = useState(false);

  const metricsWithData = Object.keys(data.series || {});
  const firstAnomMetric = data.anomalies && data.anomalies[0] ? data.anomalies[0].metric : null;
  const [selMetrics, setSelMetrics] = useState(firstAnomMetric ? [firstAnomMetric] : (metricsWithData[0] ? [metricsWithData[0]] : []));
  const toggleMetricSel = (m) => setSelMetrics((prev) =>
    prev.includes(m) ? (prev.length > 1 ? prev.filter((x) => x !== m) : prev) : [...prev, m]);
  const [view, setView] = useState("chart");
  const [fromDate, setFromDate] = useState(minDate);
  const [toDate, setToDate] = useState(maxDate);
  const [openAnom, setOpenAnom] = useState({});
  const toggleAnom = (k) => setOpenAnom((o) => ({ ...o, [k]: !o[k] }));
  const [gran, setGran] = useState("daily");
  const [indexed, setIndexed] = useState(false);
  const [aiLoading, setAiLoading] = useState(false);

  // Fetch AI narratives in the background and swap them in (keeps initial render instant).
  const aiSeq = useRef(0);
  const loadAI = (from, to) => {
    if (!result.ai_available || !params) return;
    const seq = ++aiSeq.current;
    setAiLoading(true);
    apiJSON("/api/insights", { ...params, from_date: from, to_date: to })
      .then((r) => {
        if (seq !== aiSeq.current) return;
        setData((d) => ({ ...d, insights: r.insights, insights_weekly: r.insights_weekly, mode: r.mode, model: r.model }));
      })
      .catch(() => {})
      .finally(() => { if (seq === aiSeq.current) setAiLoading(false); });
  };

  // Re-scope the analysis to the selected range (debounced), then upgrade to AI insights.
  const firstRun = useRef(true);
  useEffect(() => {
    let cancelled = false;
    if (firstRun.current) {
      firstRun.current = false;
      loadAI(fromDate, toDate);   // upgrade the initial statistical insights to AI
      return;
    }
    const full = fromDate === minDate && toDate === maxDate;
    const t = setTimeout(async () => {
      if (full) {
        setData(result);
      } else if (params) {
        setReloading(true);
        try {
          const r = await apiJSON("/api/analyze", { ...params, from_date: fromDate, to_date: toDate });
          if (!cancelled) setData(r);
        } catch (e) { /* keep previous data on error */ }
        finally { if (!cancelled) setReloading(false); }
      }
      if (!cancelled) loadAI(fromDate, toDate);
    }, 450);
    return () => { cancelled = true; clearTimeout(t); };
  }, [fromDate, toDate]);
  const filterByDate = (s) => {
    if (!s) return { labels: [], values: [] };
    const o = { labels: [], values: [] };
    s.labels.forEach((d, i) => {
      if ((!fromDate || d >= fromDate) && (!toDate || d <= toDate)) { o.labels.push(d); o.values.push(s.values[i]); }
    });
    return o;
  };
  const rangeActive = fromDate !== minDate || toDate !== maxDate;
  const inRange = (d) => (!fromDate || d >= fromDate) && (!toDate || d <= toDate);
  const periodWord = gran === "weekly" ? "week-on-week" : "day-on-day";
  // Weekly insights are always statistical; daily follow the analysis mode.
  const insightMode = gran === "weekly" ? "statistical" : (data.mode || "statistical");
  const insightModel = gran === "weekly" ? null : data.model;
  const aiUpgrading = aiLoading && gran !== "weekly" && insightMode !== "llm";
  const activeAnoms = gran === "weekly" ? (data.anomalies_weekly || []) : (data.anomalies || []);
  const shownAnomalies = activeAnoms.filter((a) => inRange(a.date));
  const listed = shownAnomalies.slice(0, 24);
  const insByKey = {};
  ((gran === "weekly" ? data.insights_weekly : data.insights) || []).forEach((i) => { insByKey[i.metric + "|" + i.date] = i; });

  const primary = selMetrics[0];
  const seriesByMetric = {};
  selMetrics.forEach((m) => {
    let s = filterByDate(data.series[m]);
    if (gran === "weekly") { const w = toWeekly(s, isAvgMetric(m)); s = { labels: w.labels, values: w.values }; }
    seriesByMetric[m] = s;
  });
  const displaySeries = seriesByMetric[primary] || { labels: [], values: [] };
  let chartAnoms = [];
  if (selMetrics.length === 1 && primary) {
    chartAnoms = activeAnoms.filter((a) => a.metric === primary && inRange(a.date));
  }

  const exportPNG = () => {
    const c = document.getElementById("trendCanvas");
    if (!c) return;
    const tmp = document.createElement("canvas");
    tmp.width = c.width; tmp.height = c.height;
    const ctx = tmp.getContext("2d");
    ctx.fillStyle = "#ffffff"; ctx.fillRect(0, 0, tmp.width, tmp.height);
    ctx.drawImage(c, 0, 0);
    const a = document.createElement("a");
    a.href = tmp.toDataURL("image/png");
    a.download = `add-mg-${primary || "chart"}-${gran}.png`;
    a.click();
  };
  const exportCSV = () => {
    if (!primary || !displaySeries.labels.length) return;
    const header = "date," + selMetrics.join(",");
    const rows = displaySeries.labels.map((d, i) =>
      d + "," + selMetrics.map((m) => (seriesByMetric[m].values[i] != null ? seriesByMetric[m].values[i] : "")).join(","));
    const csv = [header].concat(rows).join("\n");
    const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `add-mg-trend-${gran}.csv`;
    a.click();
    URL.revokeObjectURL(a.href);
  };

  const dimLabel = data.dimension_col
    ? ` · ${data.dimension_col}: ${data.dimension_value && data.dimension_value !== "__all__" ? data.dimension_value : "All"}`
    : "";

  return html`
    <div className="wrap stack">
      <div>
        <div className="section-title">Key metrics <span style=${{ color: "var(--plum)" }}>· average over the period</span></div>
        <div className="muted" style=${{ fontSize: 12, margin: "-4px 0 12px" }}>
          Each tile shows the metric’s <b>average across the whole uploaded period</b>. Tile colour = <span className="up" style=${{ fontWeight: 700 }}>trending well (green)</span> or <span className="down" style=${{ fontWeight: 700 }}>needs attention (red)</span> for that metric (grey = flat).
        </div>
        <div className="kpis">
          ${data.kpis.map((k) => {
            const s = data.series[k.metric] || { values: [] };
            const avg = s.values.length ? s.values.reduce((a, b) => a + b, 0) / s.values.length : k.latest;
            const trendDir = k.trend === "rising" ? 1 : k.trend === "falling" ? -1 : 0;
            const tone = goodness(k.metric, trendDir); // good | bad | flat
            return html`<div key=${k.metric} className=${"card kpi tone-" + tone}>
              <div className="name">${cap(k.metric)}</div>
              <div className="val">${fmtNum(Number(avg.toFixed(2)))}</div>
              <div className="kpi-sub">average</div>
            </div>`;
          })}
        </div>
      </div>

      ${primary &&
      html`<div className="card chart-card">
        <div className="chart-head">
          <div className="section-title" style=${{ margin: 0 }}>${view === "chart" ? "Trend" : "Data"} · ${selMetrics.length > 1 ? `${selMetrics.length} metrics` : cap(primary)}${gran === "weekly" ? " · weekly" : ""}${reloading ? html`<span className="muted" style=${{ fontWeight: 500 }}> · updating…</span>` : ""}</div>
          <div style=${{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
            <div className="seg">
              <button className=${view === "chart" ? "on" : ""} onClick=${() => setView("chart")}>Chart</button>
              <button className=${view === "table" ? "on" : ""} onClick=${() => setView("table")}>Table</button>
            </div>
            <div className="seg">
              <button className=${gran === "daily" ? "on" : ""} onClick=${() => setGran("daily")}>Daily</button>
              <button className=${gran === "weekly" ? "on" : ""} onClick=${() => setGran("weekly")}>Weekly</button>
            </div>
            <${MetricDropdown} metrics=${metricsWithData} selected=${selMetrics} onToggle=${toggleMetricSel} />
            ${view === "chart" && selMetrics.length > 1 && html`<div className="seg">
              <button className=${!indexed ? "on" : ""} onClick=${() => setIndexed(false)}>Values</button>
              <button className=${indexed ? "on" : ""} onClick=${() => setIndexed(true)}>Indexed</button>
            </div>`}
            <div className="daterange">
              <input type="date" value=${fromDate} min=${minDate} max=${maxDate} onChange=${(e) => setFromDate(e.target.value)} />
              <span className="muted" style=${{ fontSize: 12 }}>to</span>
              <input type="date" value=${toDate} min=${minDate} max=${maxDate} onChange=${(e) => setToDate(e.target.value)} />
              ${rangeActive && html`<button className="datereset" onClick=${() => { setFromDate(minDate); setToDate(maxDate); }}>Reset</button>`}
            </div>
            <div className="seg">
              ${view === "chart" && html`<button onClick=${exportPNG}>⤓ PNG</button>`}
              <button onClick=${exportCSV}>⤓ CSV</button>
            </div>
          </div>
        </div>
        ${displaySeries.labels.length === 0
          ? html`<div className="empty">No data in the selected date range.</div>`
          : view === "chart"
          ? html`<${TrendChart} seriesByMetric=${seriesByMetric} metrics=${selMetrics} anomalies=${chartAnoms} indexed=${indexed} />
              <div className="muted" style=${{ fontSize: 12, marginTop: 8 }}>${selMetrics.length > 1 ? (indexed ? "Indexed to 100 at the first point, so metrics on different scales share one axis and you compare % movement." : "Raw values on one axis — best when the selected metrics are on similar scales.") : "Coloured points mark detected anomalies; the dashed line is the average."}</div>`
          : selMetrics.length > 1
          ? html`<${TrendTableMulti} seriesByMetric=${seriesByMetric} metrics=${selMetrics} anomalies=${activeAnoms} periodLabel=${gran === "weekly" ? "Week-on-week" : "Day-on-day"} />
              <div className="muted" style=${{ fontSize: 12, marginTop: 8 }}>Each cell shows the value and its ${periodWord} change (green = good, red = needs attention for that metric); tinted cells are detected anomalies.</div>`
          : html`<${TrendTable} series=${displaySeries} metric=${primary} anomalies=${chartAnoms} periodLabel=${gran === "weekly" ? "Week-on-week" : "Day-on-day"} />`}
      </div>`}

      <div>
        <div className="section-title" style=${{ fontSize: 19, fontWeight: 800, color: "var(--plum)" }}>Detected anomalies
          <span className=${"mode-badge " + (aiUpgrading || insightMode === "llm" ? "llm" : "statistical")}
            style=${{ marginLeft: 8 }}
            title=${aiUpgrading
              ? "Fetching AI-written narratives — showing quick statistical insights until they arrive."
              : insightMode === "llm"
              ? `Narratives written by ${insightModel || "an AI model"}, grounded in the detected data.`
              : "Narratives computed by the built-in statistical engine (no AI model configured)."}>
            ${aiUpgrading ? "✨ generating AI…" : insightMode === "llm" ? `✨ AI · ${insightModel || "model"}` : "📊 Statistical"}
          </span>
          <span className="hint">${shownAnomalies.length} flagged${rangeActive ? " in range" : ""} · ${periodWord} · ranked by recency & severity${reloading ? " · updating…" : ""}</span>
        </div>
        ${shownAnomalies.length > 0 && html`<div className="muted" style=${{ fontSize: 12, margin: "-2px 0 10px" }}>Click an anomaly to see details</div>`}
        ${shownAnomalies.length === 0
          ? html`<div className="card empty">✓ ${rangeActive ? `No ${periodWord} anomalies in the selected date range.` : `No significant ${periodWord} anomalies in this view. Metrics moved within normal ranges.`}</div>`
          : html`<div className="anoms">
              ${listed.map((a, i) => {
                const key = a.metric + "|" + a.date;
                const isOpen = !!openAnom[key];
                const ins = insByKey[key];
                return html`<div key=${i} className="card anom-card">
                  <div className="anom-head" onClick=${() => toggleAnom(key)}>
                    <div className=${"sev-bar sev-" + a.severity}></div>
                    <div>
                      <div><span className="metricname">${cap(a.metric)}</span>
                        <span className=${"badge " + a.severity} style=${{ marginLeft: 8 }}>${a.severity}</span>
                        ${a.is_latest && html`<span className="badge moderate" style=${{ marginLeft: 6 }}>latest ${a.period === "week" ? "week" : "day"}</span>`}
                      </div>
                      <div className="meta">${a.direction === "spike" ? "Spike" : "Drop"} ${a.period === "week" ? "· week of " + a.date : "on " + a.date} · ${fmtNum(a.prev_value)} → ${fmtNum(a.value)}</div>
                    </div>
                    <div className=${"deltabig " + (goodness(a.metric, a.pct_change) === "good" ? "up" : "down")}>${a.pct_change > 0 ? "+" : ""}${a.pct_change}%</div>
                    <div className="anom-chev">${isOpen ? "▲" : "▼"}</div>
                  </div>
                  ${isOpen && ins && html`<div className="anom-body">
                    <div className="rowflex" style=${{ justifyContent: "flex-end", marginBottom: 4 }}>
                      <span className=${"conf " + (ins.confidence || "medium")}>${ins.confidence || "medium"} confidence</span>
                    </div>
                    <div className="block">
                      <div className="h">Business impact</div>
                      <div className="impact">${ins.impact}</div>
                    </div>
                    ${(ins.root_causes || []).length > 0 && html`<div className="block">
                      <div className="h">What the data points to</div>
                      <ul>${(ins.root_causes || []).map((c, j) => html`<li key=${j}>${c}</li>`)}</ul>
                    </div>`}
                    <div className="block">
                      <div className="h">Recommended next steps</div>
                      <ul>${(ins.recommendations || []).map((r, j) => html`<li key=${j}>${r}</li>`)}</ul>
                    </div>
                  </div>`}
                  ${isOpen && !ins && html`<div className="anom-body"><div className="muted">No detailed insight available for this anomaly.</div></div>`}
                </div>`;
              })}
            </div>`}
      </div>

      <${ChatWidget} params=${params} fromDate=${fromDate} toDate=${toDate} division=${data.division} />
    </div>
  `;
}

/* Floating chat assistant — grounded Q&A about the metrics & anomalies */
function ChatWidget({ params, fromDate, toDate, division }) {
  const [open, setOpen] = useState(false);
  const [msgs, setMsgs] = useState([]);
  const [q, setQ] = useState("");
  const [busy, setBusy] = useState(false);
  const listRef = useRef(null);
  useEffect(() => { if (listRef.current) listRef.current.scrollTop = listRef.current.scrollHeight; }, [msgs, open, busy]);

  const send = async () => {
    const question = q.trim();
    if (!question || busy || !params) return;
    const history = msgs.slice(-6);
    setMsgs((m) => [...m, { role: "user", content: question }]);
    setQ("");
    setBusy(true);
    try {
      const res = await apiJSON("/api/chat", { ...params, from_date: fromDate, to_date: toDate, question, history });
      setMsgs((m) => [...m, { role: "assistant", content: res.answer }]);
    } catch (e) {
      setMsgs((m) => [...m, { role: "assistant", content: "Sorry — I couldn't answer that (" + e.message + ")." }]);
    } finally {
      setBusy(false);
    }
  };
  const examples = [
    "Which metric changed the most?",
    "Why did the biggest anomaly happen?",
    "How is GMV trending?",
  ];
  return html`<div>
    <button className="chat-fab" onClick=${() => setOpen((o) => !o)} title="Ask about your metrics & anomalies">
      ${open ? "✕" : "💬"}
    </button>
    ${open && html`<div className="chat-panel">
      <div className="chat-head">
        <div><strong>Ask Pulse</strong> <span className="muted" style=${{ fontSize: 12 }}>· ${division || "your data"}</span></div>
        <button className="chat-x" onClick=${() => setOpen(false)}>✕</button>
      </div>
      <div className="chat-msgs" ref=${listRef}>
        ${msgs.length === 0 && html`<div className="chat-hint">
          Ask about your metrics, trends, or any detected anomaly. Try:
          <div className="chat-egs">
            ${examples.map((e, i) => html`<button key=${i} className="chat-eg" onClick=${() => setQ(e)}>${e}</button>`)}
          </div>
        </div>`}
        ${msgs.map((m, i) => html`<div key=${i} className=${"chat-msg " + m.role}>${m.content}</div>`)}
        ${busy && html`<div className="chat-msg assistant chat-typing">…thinking</div>`}
      </div>
      <div className="chat-input">
        <input value=${q} placeholder="Ask a question…"
          onKeyDown=${(e) => { if (e.key === "Enter") send(); }}
          onChange=${(e) => setQ(e.target.value)} />
        <button onClick=${send} disabled=${busy || !q.trim()}>Send</button>
      </div>
    </div>`}
  </div>`;
}

/* ----------------------------- App shell ----------------------------- */
class ErrorBoundary extends React.Component {
  constructor(props) { super(props); this.state = { err: null }; }
  static getDerivedStateFromError(err) { return { err }; }
  componentDidCatch(err, info) { console.error("Render error:", err, info); }
  render() {
    if (this.state.err) {
      return html`<div className="wrap"><div className="card pad">
        <h3 style=${{ marginTop: 0 }}>Something went wrong rendering this view.</h3>
        <p className="muted">Please try again or start a new analysis. Details:</p>
        <pre style=${{ whiteSpace: "pre-wrap", color: "var(--critical)", fontSize: 12 }}>${String((this.state.err && this.state.err.stack) || this.state.err)}</pre>
      </div></div>`;
    }
    return this.props.children;
  }
}

function App() {
  const [session, setSession] = useState(() => {
    try { return JSON.parse(localStorage.getItem("il_session") || "null"); } catch { return null; }
  });
  const [result, setResult] = useState(null);
  const [params, setParams] = useState(null);

  function login(s) { setSession(s); localStorage.setItem("il_session", JSON.stringify(s)); }
  function logout() { setSession(null); setResult(null); setParams(null); localStorage.removeItem("il_session"); }
  function reset() { setResult(null); setParams(null); }
  function onAnalyzed(res, reqParams) { setResult(res); setParams(reqParams); }

  let body;
  if (!session) body = html`<${Login} onLogin=${login} />`;
  else if (!result) body = html`<${Setup} session=${session} onDone=${onAnalyzed} />`;
  else body = html`<${Dashboard} result=${result} params=${params} />`;

  return html`
    <div>
      <${TopBar} session=${session} onLogout=${logout} onReset=${result ? reset : null} />
      ${React.createElement(ErrorBoundary, null, body)}
    </div>
  `;
}

ReactDOM.createRoot(document.getElementById("root")).render(html`<${App} />`);
