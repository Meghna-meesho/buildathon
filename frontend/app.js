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
          <div className="title">ADD - MG</div>
          <div className="subtitle">Anomaly Detection Dashboard</div>
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
          <h1>Welcome to ADD - MG</h1>
          <p>Anomaly Detection Dashboard — sign in and pick your division to see the day-on-day anomalies that matter to you.</p>
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
      const result = await apiJSON("/api/analyze", {
        upload_id: info.upload_id,
        division: session.division,
        sensitivity,
        mapping: {
          date_col: dateCol,
          metric_cols: metricCols,
          dimension_col: dimCol || null,
          dimension_value: dimCol ? dimValue : null,
        },
      });
      onDone(result);
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

function TrendChart({ series, metric, anomalies }) {
  const canvasRef = useRef(null);
  const chartRef = useRef(null);

  useEffect(() => {
    if (!series || !canvasRef.current) return;
    const anomByDate = {};
    (anomalies || []).filter((a) => a.metric === metric).forEach((a) => { anomByDate[a.date] = a.severity; });

    const ptColors = series.labels.map((d) => (anomByDate[d] ? SEV_COLOR[anomByDate[d]] : "#7a0b57"));
    const ptRadius = series.labels.map((d) => (anomByDate[d] ? 6 : 2));

    if (chartRef.current) chartRef.current.destroy();
    chartRef.current = new Chart(canvasRef.current, {
      type: "line",
      data: {
        labels: series.labels,
        datasets: [{
          label: cap(metric),
          data: series.values,
          borderColor: "#7a0b57",
          backgroundColor: "rgba(122,11,87,.08)",
          borderWidth: 2,
          tension: 0.25,
          fill: true,
          pointBackgroundColor: ptColors,
          pointBorderColor: ptColors,
          pointRadius: ptRadius,
          pointHoverRadius: 7,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              afterBody: (items) => {
                const d = items[0].label;
                const a = (anomalies || []).find((x) => x.metric === metric && x.date === d);
                return a ? `⚠ ${a.severity} ${a.direction}: ${a.pct_change > 0 ? "+" : ""}${a.pct_change}%` : "";
              },
            },
          },
        },
        scales: {
          x: { grid: { display: false }, ticks: { maxTicksLimit: 10 } },
          y: { grid: { color: "#eee" }, ticks: { callback: (v) => fmtNum(v) } },
        },
      },
    });
    return () => { if (chartRef.current) { chartRef.current.destroy(); chartRef.current = null; } };
  }, [series, metric, anomalies]);

  return html`<div className="chart-box"><canvas ref=${canvasRef}></canvas></div>`;
}

/* Tabular view of a metric's series (newest first), anomalies highlighted */
function TrendTable({ series, metric, anomalies }) {
  const anom = {};
  (anomalies || []).filter((a) => a.metric === metric).forEach((a) => { anom[a.date] = a; });
  const { labels, values } = series;
  const rows = labels.map((d, i) => ({ d, v: values[i], prev: i > 0 ? values[i - 1] : null })).reverse();
  return html`
    <div className="tablewrap" style=${{ maxHeight: 320, overflowY: "auto" }}>
      <table className="preview">
        <thead><tr><th>Date</th><th>${cap(metric)}</th><th>Day-on-day</th><th>Status</th></tr></thead>
        <tbody>
          ${rows.map(({ d, v, prev }) => {
            const dod = prev ? ((v - prev) / Math.abs(prev)) * 100 : null;
            const a = anom[d];
            const cls = dod > 0 ? "up" : dod < 0 ? "down" : "";
            return html`<tr key=${d} style=${a ? { background: "#fdf3f4" } : {}}>
              <td>${d}</td>
              <td>${fmtNum(v)}</td>
              <td className=${cls}>${dod === null ? "—" : (dod > 0 ? "+" : "") + dod.toFixed(1) + "%"}</td>
              <td>${a ? html`<span className=${"badge " + a.severity}>${a.severity}</span>` : ""}</td>
            </tr>`;
          })}
        </tbody>
      </table>
    </div>`;
}

/* ----------------------------- Dashboard ----------------------------- */
function Dashboard({ result }) {
  const metricsWithData = Object.keys(result.series || {});
  const firstAnomMetric = result.anomalies && result.anomalies[0] ? result.anomalies[0].metric : null;
  const [selMetric, setSelMetric] = useState(firstAnomMetric || metricsWithData[0] || "");
  const [view, setView] = useState("chart");

  const dimLabel = result.dimension_col
    ? ` · ${result.dimension_col}: ${result.dimension_value && result.dimension_value !== "__all__" ? result.dimension_value : "All"}`
    : "";

  return html`
    <div className="wrap stack">
      <div className="summary-banner">
        <div className="eyebrow">Executive summary — ${result.division}${dimLabel}</div>
        <p>${result.executive_summary}</p>
        <span className=${"mode-badge " + result.mode}>
          ${result.mode === "llm" ? "✦ AI root-cause analysis" : "▣ Statistical analysis"}
        </span>
        ${result.notice && html`<div className="notice">${result.notice}</div>`}
      </div>

      <div>
        <div className="section-title">Key metrics · latest vs. previous day</div>
        <div className="kpis">
          ${result.kpis.map((k) => {
            const cls = k.dod_change > 0 ? "up" : k.dod_change < 0 ? "down" : "flat";
            const arrow = k.dod_change > 0 ? "▲" : k.dod_change < 0 ? "▼" : "▬";
            return html`<div key=${k.metric} className="card kpi">
              <div className="name">${cap(k.metric)}</div>
              <div className="val">${fmtNum(k.latest)}</div>
              <div className=${"dod " + cls}>${arrow} ${Math.abs(k.dod_change)}% <span className="muted" style=${{ fontWeight: 500 }}>DoD</span></div>
              <div className="trend">${k.trend} trend</div>
            </div>`;
          })}
        </div>
      </div>

      ${selMetric &&
      html`<div className="card chart-card">
        <div className="chart-head">
          <div className="section-title" style=${{ margin: 0 }}>${view === "chart" ? "Trend" : "Data"} · ${cap(selMetric)}</div>
          <div style=${{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
            <div className="seg">
              <button className=${view === "chart" ? "on" : ""} onClick=${() => setView("chart")}>Chart</button>
              <button className=${view === "table" ? "on" : ""} onClick=${() => setView("table")}>Table</button>
            </div>
            <select className="metric-select" value=${selMetric} onChange=${(e) => setSelMetric(e.target.value)}>
              ${metricsWithData.map((m) => html`<option key=${m} value=${m}>${cap(m)}</option>`)}
            </select>
          </div>
        </div>
        ${view === "chart"
          ? html`<${TrendChart} series=${result.series[selMetric]} metric=${selMetric} anomalies=${result.anomalies} />
              <div className="muted" style=${{ fontSize: 12, marginTop: 8 }}>Coloured points mark detected anomalies. Hover for details.</div>`
          : html`<${TrendTable} series=${result.series[selMetric]} metric=${selMetric} anomalies=${result.anomalies} />`}
      </div>`}

      <div>
        <div className="section-title" style=${{ fontSize: 19, fontWeight: 800, color: "var(--plum)" }}>Detected anomalies
          <span className="hint">${result.anomalies.length} flagged · ranked by recency & severity</span>
        </div>
        ${result.anomalies.length === 0
          ? html`<div className="card empty">✓ No significant day-on-day anomalies in this view. Metrics moved within normal ranges.</div>`
          : html`<div className="anoms">
              ${result.anomalies.slice(0, 12).map(
                (a, i) => html`<div key=${i} className="card anom" onClick=${() => setSelMetric(a.metric)} style=${{ cursor: "pointer" }}>
                  <div className=${"sev-bar sev-" + a.severity}></div>
                  <div>
                    <div><span className="metricname">${cap(a.metric)}</span>
                      <span className=${"badge " + a.severity} style=${{ marginLeft: 8 }}>${a.severity}</span>
                      ${a.is_latest && html`<span className="badge moderate" style=${{ marginLeft: 6 }}>latest day</span>`}
                    </div>
                    <div className="meta">${a.direction === "spike" ? "Spike" : "Drop"} on ${a.date} · ${fmtNum(a.prev_value)} → ${fmtNum(a.value)}</div>
                  </div>
                  <div className=${"deltabig " + (a.pct_change > 0 ? "up" : "down")}>${a.pct_change > 0 ? "+" : ""}${a.pct_change}%</div>
                </div>`
              )}
            </div>`}
      </div>

      ${result.insights && result.insights.length > 0 &&
      html`<div>
        <div className="section-title">Root-cause insights ${result.mode === "llm" ? "" : "(statistical)"}
          <span className="hint">Tailored to ${result.division}</span>
        </div>
        <div className="stack">
          ${result.insights.map(
            (ins, i) => html`<div key=${i} className="card insight">
              <div className="rowflex">
                <div>
                  <div className="tag">${cap(ins.metric)} · ${ins.date || ""}</div>
                  <h4>${ins.title}</h4>
                </div>
                <span className=${"conf " + (ins.confidence || "medium")}>${ins.confidence || "medium"} confidence</span>
              </div>
              <div className="block">
                <div className="h">Business impact</div>
                <div className="impact">${ins.impact}</div>
              </div>
              <div className="block">
                <div className="h">Recommended next steps</div>
                <ul>${(ins.recommendations || []).map((r, j) => html`<li key=${j}>${r}</li>`)}</ul>
              </div>
            </div>`
          )}
        </div>
      </div>`}
    </div>
  `;
}

/* ----------------------------- App shell ----------------------------- */
function App() {
  const [session, setSession] = useState(() => {
    try { return JSON.parse(localStorage.getItem("il_session") || "null"); } catch { return null; }
  });
  const [result, setResult] = useState(null);

  function login(s) { setSession(s); localStorage.setItem("il_session", JSON.stringify(s)); }
  function logout() { setSession(null); setResult(null); localStorage.removeItem("il_session"); }
  function reset() { setResult(null); }

  let body;
  if (!session) body = html`<${Login} onLogin=${login} />`;
  else if (!result) body = html`<${Setup} session=${session} onDone=${setResult} />`;
  else body = html`<${Dashboard} result=${result} />`;

  return html`
    <div>
      <${TopBar} session=${session} onLogout=${logout} onReset=${result ? reset : null} />
      ${body}
    </div>
  `;
}

ReactDOM.createRoot(document.getElementById("root")).render(html`<${App} />`);
