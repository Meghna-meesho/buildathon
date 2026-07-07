# Insight Lens — Anomaly Detector & RCA Engine

**The problem.** Business teams share one giant dashboard with dozens of metrics across many tabs. Every division cares about different numbers, and figuring out *what changed day-on-day and why* means manually scanning charts every morning. Most anomalies are noticed late — or not at all.

**What this does.** Upload a CSV export of your dashboard, pick your division, and Insight Lens:
1. **Detects anomalies** — statistically significant day-on-day spikes and drops per metric (z-score vs. the metric's own history).
2. **Explains them** — writes plain-English root-cause hypotheses, business impact, and next steps, **tailored to your division**.
3. **Shows the trends** — KPI tiles + interactive charts with anomaly points highlighted.

No dashboard integration needed — it works off a CSV upload, so any team can use it today.

---

## Run it

```bash
./run.sh
# then open http://localhost:8000
```

That's it — `run.sh` creates a virtualenv, installs dependencies, and starts the server. Requires Python 3.10+.

**Try it with the included sample:** on the upload screen, choose `sample_data.csv` (45 days × 3 divisions with a few planted anomalies). Regenerate it any time with `python3 generate_sample.py`.

### Enabling AI-written insights (optional)

The app works fully without any API key (statistical insights). To turn on richer AI root-cause narratives, set an Anthropic key before launching:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
./run.sh
```

- Default model is `claude-opus-4-8`. To conserve tokens, set `ANTHROPIC_MODEL=claude-sonnet-5`.
- Each analysis is a single, structured LLM call (no chit-chat, thinking disabled) to keep token usage low.
- If the key is missing or a call fails, the app **automatically falls back** to statistical insights — it never breaks.

---

## How it works

```
CSV upload ──▶ profile columns (date / numeric / categorical) ──▶ infer roles
     │
     ▼
column mapper (user confirms date, metrics, optional division filter, sensitivity)
     │
     ▼
anomaly engine (day-on-day % change + z-score vs. history) ──▶ ranked anomalies
     │
     ▼
RCA layer: Claude (structured JSON) ──or── statistical fallback ──▶ insights + summary
     │
     ▼
dashboard: exec summary · KPI tiles · trend charts · anomaly list · root-cause cards
```

## Tech

- **Backend:** FastAPI (Python, stdlib CSV + statistics — no heavy data deps). Anthropic SDK for the RCA layer.
- **Frontend:** build-free React (React + [htm](https://github.com/developit/htm), no bundler) + Chart.js. Everything is vendored in `frontend/vendor/`, served by FastAPI on one port.
- **Auth:** lightweight mock login with a division selector (demo — not production security).

## Project layout

```
buildathon_MV/
├── run.sh                 # one-command launcher
├── requirements.txt
├── generate_sample.py     # makes sample_data.csv
├── sample_data.csv
├── backend/
│   ├── main.py            # FastAPI app + routes + static serving
│   └── engine.py          # parsing, anomaly detection, LLM RCA + fallback
└── frontend/
    ├── index.html
    ├── styles.css
    ├── app.js             # React app (login → upload/mapper → dashboard)
    └── vendor/            # react, react-dom, htm, chart.js
```

## Notes for evaluators

- **It works with no setup and no API key** — statistical insights are always available; the LLM is an enhancement.
- **Flexible input** — the column mapper adapts to any CSV shape (wide or long format, any column names, common date formats).
- **Division-aware** — the same file yields different framing for Grocery vs. Fashion vs. Leadership.
- Uploaded data is held **in memory only** for the session and never written to disk.
