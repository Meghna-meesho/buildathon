"""Anomaly-detection + RCA engine.

Pure-stdlib CSV parsing and statistics (no pandas), plus an LLM layer that turns
detected anomalies into plain-English root-cause insights tailored to a business
division. If no Anthropic credentials are available (or the call fails), the
engine falls back to templated statistical explanations so the app always works.
"""
from __future__ import annotations

import csv
import io
import json
import math
import os
import statistics
from datetime import datetime

# ----------------------------------------------------------------------------
# Parsing helpers
# ----------------------------------------------------------------------------

_DATE_FORMATS = [
    "%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y", "%d/%m/%Y", "%m/%d/%Y", "%m-%d-%Y",
    "%d %b %Y", "%d %B %Y", "%b %d %Y", "%B %d %Y", "%d-%b-%Y", "%d-%b-%y",
    "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
]


def _parse_date(v):
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _parse_number(v):
    """Best-effort numeric parse: strips currency symbols, commas, %, spaces."""
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    neg = s.startswith("(") and s.endswith(")")  # accounting negatives
    cleaned = s.strip("()")
    for ch in ["₹", "$", "€", "£", ",", "%", " "]:
        cleaned = cleaned.replace(ch, "")
    try:
        n = float(cleaned)
        return -n if neg else n
    except ValueError:
        return None


def parse_csv(raw):
    """Parse CSV bytes or text into headers + list-of-dict rows."""
    if isinstance(raw, bytes):
        text = raw.decode("utf-8-sig", errors="replace")
    else:
        text = str(raw).lstrip("﻿")  # strip a leading BOM if present
    reader = csv.DictReader(io.StringIO(text))
    headers = reader.fieldnames or []
    rows = [dict(r) for r in reader]
    return headers, rows


def profile_columns(headers, rows):
    """Classify each column and infer sensible default roles."""
    sample = rows[: min(len(rows), 200)]
    cols = []
    for h in headers:
        values = [r.get(h) for r in sample]
        non_null = [v for v in values if v is not None and str(v).strip() != ""]
        n = max(len(non_null), 1)
        date_hits = sum(1 for v in non_null if _parse_date(v) is not None)
        num_hits = sum(1 for v in non_null if _parse_number(v) is not None)
        distinct = len({str(v).strip() for v in non_null})
        if date_hits / n >= 0.7:
            dtype = "date"
        elif num_hits / n >= 0.8:
            dtype = "numeric"
        else:
            dtype = "categorical"
        cols.append({
            "name": h,
            "dtype": dtype,
            "cardinality": distinct,
            "sample_values": [str(v) for v in non_null[:4]],
        })

    date_col = next((c["name"] for c in cols if c["dtype"] == "date"), None)
    metric_cols = [c["name"] for c in cols if c["dtype"] == "numeric"]
    # a good dimension: categorical, low cardinality, not the date
    dim_candidates = [
        c for c in cols
        if c["dtype"] == "categorical" and 1 < c["cardinality"] <= 30 and c["name"] != date_col
    ]
    dim_candidates.sort(key=lambda c: c["cardinality"])
    dimension_col = dim_candidates[0]["name"] if dim_candidates else None

    inferred = {
        "date_col": date_col,
        "metric_cols": metric_cols,
        "dimension_col": dimension_col,
    }
    return cols, inferred


# ----------------------------------------------------------------------------
# Analysis
# ----------------------------------------------------------------------------

_SENSITIVITY = {"low": 3.0, "medium": 2.5, "high": 2.0}


_AVG_HINTS = ("rate", "pct", "percent", "ratio", "avg", "average", "mean",
              "nps", "score", "cpdo", "aov", "distance", "retention", "share")


def _is_average_metric(name):
    n = name.lower()
    return any(h in n for h in _AVG_HINTS)


def _build_series(rows, date_col, metric, dimension_col=None, dimension_value=None):
    """Return sorted (date_str, value) points. Values sharing a date are summed for
    count-like metrics (orders, GMV) and averaged for rate/ratio-like metrics
    (completion %, RTO %, NPS) so combining segments stays meaningful."""
    avg = _is_average_metric(metric)
    buckets = {}  # date -> [total, count]
    for r in rows:
        if dimension_col and dimension_value not in (None, "__all__"):
            if str(r.get(dimension_col)).strip() != str(dimension_value).strip():
                continue
        d = _parse_date(r.get(date_col))
        val = _parse_number(r.get(metric))
        if d is None or val is None:
            continue
        b = buckets.setdefault(d, [0.0, 0])
        b[0] += val
        b[1] += 1
    points = sorted(buckets.items(), key=lambda kv: kv[0])
    labels = [d.strftime("%Y-%m-%d") for d, _ in points]
    values = [round(t / c, 4) if avg else round(t, 4) for _, (t, c) in points]
    return labels, values


def _detect_anomalies(metric, labels, values, threshold):
    """Flag day-over-day changes that deviate strongly from the norm."""
    anomalies = []
    if len(values) < 4:
        return anomalies
    deltas = []  # pct change between consecutive points
    for i in range(1, len(values)):
        prev = values[i - 1]
        cur = values[i]
        pct = (cur - prev) / abs(prev) if prev not in (0, 0.0) else 0.0
        deltas.append(pct)
    mean = statistics.fmean(deltas)
    std = statistics.pstdev(deltas) or 1e-9
    for i, pct in enumerate(deltas):
        z = (pct - mean) / std
        big_move = abs(pct) >= 0.30  # 30%+ swing is notable regardless of z
        if abs(z) >= threshold or (big_move and abs(z) >= threshold - 0.5):
            idx = i + 1
            anomalies.append({
                "metric": metric,
                "date": labels[idx],
                "value": round(values[idx], 2),
                "prev_value": round(values[idx - 1], 2),
                "pct_change": round(pct * 100, 1),
                "zscore": round(z, 2),
                "direction": "spike" if pct > 0 else "drop",
                "severity": _severity(abs(z)),
                "is_latest": idx == len(values) - 1,
            })
    return anomalies


def _severity(absz):
    if absz >= 3.5:
        return "critical"
    if absz >= 2.75:
        return "high"
    return "moderate"


def _trend(values):
    if len(values) < 2:
        return "flat"
    first = statistics.fmean(values[: max(1, len(values) // 3)])
    last = statistics.fmean(values[-max(1, len(values) // 3):])
    if first == 0:
        return "flat"
    change = (last - first) / abs(first)
    if change > 0.05:
        return "rising"
    if change < -0.05:
        return "falling"
    return "flat"


def analyze(rows, mapping, sensitivity="medium"):
    """Core analysis: KPIs, per-metric series, and ranked anomalies."""
    date_col = mapping["date_col"]
    metric_cols = mapping["metric_cols"]
    dimension_col = mapping.get("dimension_col")
    dimension_value = mapping.get("dimension_value")
    threshold = _SENSITIVITY.get(sensitivity, 2.5)

    kpis, series, all_anomalies, metric_stats = [], {}, [], []
    for metric in metric_cols:
        labels, values = _build_series(rows, date_col, metric, dimension_col, dimension_value)
        if len(values) < 2:
            continue
        series[metric] = {"labels": labels, "values": values}
        latest, prev = values[-1], values[-2]
        dod = round((latest - prev) / abs(prev) * 100, 1) if prev else 0.0
        kpis.append({
            "metric": metric,
            "latest": round(latest, 2),
            "prev": round(prev, 2),
            "dod_change": dod,
            "trend": _trend(values),
        })
        metric_stats.append({
            "metric": metric,
            "latest": round(latest, 2),
            "prev": round(prev, 2),
            "dod_change_pct": dod,
            "mean": round(statistics.fmean(values), 2),
            "std": round(statistics.pstdev(values), 2),
            "trend": _trend(values),
        })
        all_anomalies.extend(_detect_anomalies(metric, labels, values, threshold))

    # rank: latest first, then by severity magnitude
    sev_rank = {"critical": 3, "high": 2, "moderate": 1}
    all_anomalies.sort(key=lambda a: (a["is_latest"], sev_rank[a["severity"]], abs(a["zscore"])), reverse=True)

    return {
        "kpis": kpis,
        "series": series,
        "anomalies": all_anomalies,
        "metric_stats": metric_stats,
    }


# ----------------------------------------------------------------------------
# LLM RCA layer (with statistical fallback)
# ----------------------------------------------------------------------------

MODEL = os.getenv("ANTHROPIC_MODEL", "claude-opus-4-8")

_SCHEMA = {
    "type": "object",
    "properties": {
        "executive_summary": {"type": "string"},
        "insights": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "metric": {"type": "string"},
                    "date": {"type": "string"},
                    "title": {"type": "string"},
                    "root_causes": {"type": "array", "items": {"type": "string"}},
                    "impact": {"type": "string"},
                    "recommendations": {"type": "array", "items": {"type": "string"}},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                },
                "required": ["metric", "date", "title", "root_causes", "impact", "recommendations", "confidence"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["executive_summary", "insights"],
    "additionalProperties": False,
}

_SYSTEM = (
    "You are a data analyst doing root-cause analysis on business dashboard metrics for "
    "an Indian e-commerce company (metrics may include GMV, orders, conversion, adoption, "
    "delivery/completion rate, RTO/returns, cancellations, CPDO, shrinkage, NPS/NQD, AOV, "
    "pickup-point retention, and similar). You explain what changed day-on-day and the most "
    "likely operational reasons, for one business division.\n\n"
    "Rules you MUST follow:\n"
    "- Use plain business language. NEVER use statistical jargon such as 'standard deviation', "
    "'z-score', 'sigma', the 'σ' symbol, 'variance', or 'percentile'. Say things like 'a much "
    "bigger jump than a normal day' instead.\n"
    "- Be concise. Each root cause is ONE short sentence; give 1-2 root causes per anomaly. "
    "Keep 'impact' to one short sentence.\n"
    "- In recommended next steps, ALWAYS point the user to a related metric to check that "
    "could explain the move (prefer metrics that moved on the SAME day). Keep 2-3 short, "
    "action-first steps.\n"
    "- Causes are hypotheses to investigate, not confirmed facts. Don't invent numbers."
)

# Plain-language, domain-aware hypotheses keyed by a substring of the metric name.
_DOMAIN_CAUSE = {
    "gmv": "a shift in demand, a pricing or discount change, or a checkout/payment issue",
    "revenue": "a shift in demand, a pricing or discount change, or a checkout/payment issue",
    "order": "a change in traffic or conversion, stockouts, or a promo starting/ending",
    "conversion": "checkout friction, a pricing change, or a campaign starting/ending",
    "adoption": "a discount change, comms not reaching users, or the option showing to fewer people",
    "return": "product quality, wrong or damaged items, or a specific batch",
    "rto": "pickup or delivery delays, partners unavailable, or customers not collecting in time",
    "completion": "pickup delays, partner unavailability, or orders ageing into returns",
    "cancellation": "stockouts, partner unavailability, or capacity limits",
    "cpdo": "lower order density or longer delivery distances",
    "aov": "a change in basket mix or discounting",
    "average_order": "a change in basket mix or discounting",
    "shrinkage": "items lost or mismatched during handling",
    "nps": "delivery delays, returns, or a support issue",
    "nqd": "quality or quantity complaints on recent orders",
    "active_user": "acquisition spend, app performance, or seasonality",
    "distance": "a change in which pickup points are active nearby",
    "retention": "partner payout or effort concerns, or low order density",
}
# Which other metrics are worth checking when this one moves.
_RELATED_KEYS = {
    "gmv": ["order", "aov", "average_order", "conversion"],
    "revenue": ["order", "aov", "conversion"],
    "order": ["conversion", "adoption", "active_user", "cancellation"],
    "conversion": ["adoption", "order", "distance"],
    "adoption": ["conversion", "order", "distance"],
    "aov": ["order"],
    "average_order": ["order"],
    "return": ["completion", "rto", "nps", "nqd"],
    "rto": ["completion", "return", "cancellation"],
    "completion": ["rto", "cancellation", "adoption"],
    "cancellation": ["completion", "rto"],
    "cpdo": ["order", "adoption", "distance"],
    "shrinkage": ["rto", "completion"],
    "nps": ["return", "rto"],
    "nqd": ["return", "rto"],
    "active_user": ["order", "conversion"],
    "distance": ["adoption", "conversion", "cpdo"],
    "retention": ["adoption", "completion"],
}


def _humanize(name):
    return str(name).replace("_", " ").strip()


def _human_list(items):
    items = [i for i in items if i]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return ", ".join(items[:-1]) + f", and {items[-1]}"


def _by_date(anomalies):
    d = {}
    for a in anomalies:
        d.setdefault(a["date"], []).append(a)
    return d


def _lookup(mapping, metric):
    m = metric.lower()
    for key, val in mapping.items():
        if key in m:
            return val
    return None


def _related_metrics(metric, metric_names):
    keys = _lookup(_RELATED_KEYS, metric) or []
    out = []
    for other in metric_names:
        if other == metric:
            continue
        lo = other.lower()
        if any(k in lo for k in keys):
            out.append(_humanize(other))
        if len(out) >= 2:
            break
    return out


def _llm_insights(division, metric_stats, anomalies):
    """Call Claude for RCA insights. Raises on any failure (caller falls back)."""
    import anthropic  # imported lazily so the app runs without the package for stats-only mode

    client = anthropic.Anthropic()
    top = anomalies[:8]
    by_date = _by_date(anomalies)
    payload = {
        "division": division,
        "all_metrics": [m["metric"] for m in metric_stats],
        "metric_trends": [
            {k: m[k] for k in ("metric", "latest", "prev", "dod_change_pct", "trend")}
            for m in metric_stats
        ],
        "detected_anomalies": [
            {
                "metric": a["metric"], "date": a["date"], "value": a["value"],
                "prev_value": a["prev_value"], "pct_change": a["pct_change"],
                "direction": a["direction"], "severity": a["severity"],
                "other_metrics_that_moved_same_day": [
                    x["metric"] for x in by_date.get(a["date"], []) if x["metric"] != a["metric"]
                ],
            }
            for a in top
        ],
    }
    user = (
        f"Business division: {division}\n\n"
        f"Day-on-day analysis (pct_change is the day-over-day % change; severity is how "
        f"notable the move is):\n\n{json.dumps(payload, indent=2)}\n\n"
        f"Write a 2-3 sentence executive_summary of what changed for the {division} team, "
        f"then one insight per detected anomaly: a short title, 1-2 plain-language likely "
        f"root causes, a one-sentence business impact, and 2-3 next steps that include "
        f"checking a related metric (use 'other_metrics_that_moved_same_day' when present, "
        f"otherwise pick the most relevant metric from all_metrics). If no anomalies were "
        f"detected, summarize the trends and return an empty insights list. No statistical "
        f"jargon; keep it concise."
    )
    resp = client.messages.create(
        model=MODEL,
        max_tokens=4000,
        thinking={"type": "disabled"},
        output_config={"format": {"type": "json_schema", "schema": _SCHEMA}},
        system=_SYSTEM,
        messages=[{"role": "user", "content": user}],
    )
    text = next((b.text for b in resp.content if getattr(b, "type", None) == "text"), None)
    if not text:
        raise RuntimeError("empty LLM response")
    return json.loads(text)


def _fallback_insights(division, metric_stats, anomalies):
    """Plain-language, concise insights when the LLM is unavailable."""
    metric_names = [m["metric"] for m in metric_stats]
    by_date = _by_date(anomalies)
    sev_word = {"critical": "major", "high": "sharp", "moderate": "notable"}
    insights = []
    for a in anomalies[:8]:
        metric_h = _humanize(a["metric"])
        cause = _lookup(_DOMAIN_CAUSE, a["metric"]) or "an operational or demand-side change"
        others = [_humanize(x["metric"]) for x in by_date.get(a["date"], []) if x["metric"] != a["metric"]]

        root = [f"Most likely {cause}."]
        if others:
            root.append(f"{_human_list(others).capitalize()} also moved on {a['date']}, so the causes are probably linked.")

        if others:
            check = f"Compare with {_human_list(others)} for {a['date']} — they changed on the same day."
        else:
            rel = _related_metrics(a["metric"], metric_names)
            check = (f"Check {_human_list(rel)} for {a['date']} to find the driver."
                     if rel else f"Check the other metrics for {a['date']} to find the driver.")
        recs = [check, f"Confirm the {a['date']} number isn't a data or tracking error."]

        sign = "+" if a["pct_change"] > 0 else ""
        insights.append({
            "metric": a["metric"],
            "date": a["date"],
            "title": f"{metric_h.title()} {a['direction']} {sign}{a['pct_change']}% on {a['date']}",
            "root_causes": root,
            "impact": f"{metric_h.title()} moved from {a['prev_value']} to {a['value']} — "
                      f"a {sev_word[a['severity']]} day-on-day change.",
            "recommendations": recs,
            "confidence": "medium",
        })

    if anomalies:
        w = anomalies[0]
        summary = (f"For {division}, {len(anomalies)} notable day-on-day change(s) stood out. "
                   f"The biggest is a {w['pct_change']}% {w['direction']} in {_humanize(w['metric'])} "
                   f"on {w['date']} — check the items below.")
    else:
        summary = (f"For {division}, everything moved within normal day-on-day ranges — no notable "
                   f"anomalies. The trends are shown below.")
    return {"executive_summary": summary, "insights": insights}


def generate_rca(division, analysis_result):
    """Return {executive_summary, insights, mode}."""
    metric_stats = analysis_result["metric_stats"]
    anomalies = analysis_result["anomalies"]
    if os.getenv("ANTHROPIC_API_KEY"):
        try:
            out = _llm_insights(division, metric_stats, anomalies)
            out["mode"] = "llm"
            return out
        except Exception:  # noqa: BLE001 — any failure degrades gracefully to stats
            out = _fallback_insights(division, metric_stats, anomalies)
            out["mode"] = "statistical"
            return out
    out = _fallback_insights(division, metric_stats, anomalies)
    out["mode"] = "statistical"
    return out
