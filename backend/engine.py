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


def _attach_wow(anoms, labels, values, avg):
    """Attach week-on-week change to each anomaly (week vs previous week)."""
    if not anoms or not labels:
        return
    d0 = _parse_date(labels[0])
    if d0 is None:
        return
    weeks, date_to_wk = {}, {}
    for lab, v in zip(labels, values):
        d = _parse_date(lab)
        if d is None:
            continue
        wk = (d - d0).days // 7
        b = weeks.setdefault(wk, [0.0, 0])
        b[0] += v
        b[1] += 1
        date_to_wk[lab] = wk
    wk_val = {wk: (t / c if avg else t) for wk, (t, c) in weeks.items()}
    for a in anoms:
        wk = date_to_wk.get(a["date"])
        if wk is None or (wk - 1) not in wk_val:
            a["wow_change"] = None
            a["wow_direction"] = None
            continue
        cur, prev = wk_val[wk], wk_val[wk - 1]
        pct = (cur - prev) / abs(prev) * 100 if prev not in (0, 0.0) else 0.0
        a["wow_change"] = round(pct, 1)
        a["wow_direction"] = "up" if pct > 0 else "down" if pct < 0 else "flat"


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
        anoms = _detect_anomalies(metric, labels, values, threshold)
        _attach_wow(anoms, labels, values, _is_average_metric(metric))
        all_anomalies.extend(anoms)

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


# What a move MEANS for the business + concrete things to check, per metric.
# Keyed by a substring of the metric name (specific keys must precede generic "order").
_DOMAIN = {
    "gmv": {
        "up": "revenue jumped — usually a demand surge, a richer basket mix, or a promo landing",
        "down": "revenue dropped — usually softer demand, a pricing issue, or checkout/payment friction",
        "actions": ["Split GMV into orders × average order value to see which side moved.",
                    "Check for pricing, promo, or payment/checkout changes on this date."],
    },
    "revenue": {
        "up": "revenue jumped — usually a demand surge, a richer mix, or a promo landing",
        "down": "revenue dropped — usually softer demand, pricing, or checkout friction",
        "actions": ["Split revenue into volume × value to see which side moved.",
                    "Check pricing, promo, or payment changes on this date."],
    },
    "completion": {
        "up": "more orders were successfully delivered/picked up — delivery reliability improved",
        "down": "fewer orders reached customers — a hit to delivery reliability and customer experience",
        "actions": ["Check RTO% and cancellations for the same day — a completion drop usually surfaces there.",
                    "Look for pickup-point unavailability or crates not collected within the pickup window."],
    },
    "rto": {
        "up": "more orders are being returned — higher reverse-logistics cost, stranded stock, and CX risk",
        "down": "returns eased — lower cost and better fulfilment",
        "actions": ["Check completion rate and pickup-point availability for the same day.",
                    "See whether crates aged out (weren't collected in time) on this date."],
    },
    "cancellation": {
        "up": "more orders cancelled — lost sales and wasted fulfilment effort",
        "down": "fewer cancellations — smoother fulfilment",
        "actions": ["Check pickup-point availability/capacity and stock for this date.",
                    "Compare with completion rate to see how the outcome mix shifted."],
    },
    "adoption": {
        "up": "more eligible users chose self-pickup — the offer and funnel are working",
        "down": "fewer users opted in — likely a discount change, a comms gap, or the option showing to fewer people",
        "actions": ["Check whether the first-order discount or comms changed around this date.",
                    "Check conversion and how many pickup points users were shown."],
    },
    "cpdo": {
        "up": "each delivered order costs more — eroding the margin self-pickup is meant to save",
        "down": "cost per delivered order fell — better unit economics",
        "actions": ["Check order density and average pickup distance for this date — fewer nearby active PPs raise cost.",
                    "Confirm no active pickup points dropped out in the affected polygons."],
    },
    "distance": {
        "up": "users are being routed to farther pickup points — worse convenience and lower pickup likelihood",
        "down": "average pickup distance shortened — better convenience",
        "actions": ["Check which nearby pickup points went inactive on this date.",
                    "Check adoption/conversion to see if the added distance hurt opt-in."],
    },
    "shrinkage": {
        "up": "more items lost or mismatched in handling — direct cost and audit risk",
        "down": "less shrinkage — cleaner handling",
        "actions": ["Reconcile RTO / reverse crates for the affected day.",
                    "Flag the specific pickup points or crates with mismatches for an audit."],
    },
    "nps": {
        "up": "customer sentiment improved",
        "down": "customer sentiment dipped — often delays, returns, or a support issue",
        "actions": ["Check RTO% and delivery delays for the same period.",
                    "Read recent complaint / NQD reasons for the affected orders."],
    },
    "conversion": {
        "up": "more visitors converted — a campaign, pricing, or funnel improvement",
        "down": "fewer visitors converted — checkout friction, pricing, or a campaign ending",
        "actions": ["Check adoption and traffic for the same day.",
                    "Look for checkout, pricing, or campaign changes on this date."],
    },
    "aov": {
        "up": "baskets got larger — a richer mix or lighter discounting",
        "down": "baskets got smaller — a mix shift or heavier discounting",
        "actions": ["Check basket mix and discount depth for this date.",
                    "See whether high-value SKUs went out of stock."],
    },
    "avg_order": {
        "up": "baskets got larger — a richer mix or lighter discounting",
        "down": "baskets got smaller — a mix shift or heavier discounting",
        "actions": ["Check basket mix and discount depth for this date.",
                    "See whether high-value SKUs went out of stock."],
    },
    "return": {
        "up": "more returns — a margin and customer-experience hit",
        "down": "fewer returns — better quality or fit",
        "actions": ["Check which SKUs or categories drove the returns.",
                    "Review recent quality, sizing, or description changes."],
    },
    "active_user": {
        "up": "more active users — an acquisition or seasonality tailwind",
        "down": "fewer active users — acquisition, app performance, or seasonality",
        "actions": ["Check acquisition spend and app performance for this date.",
                    "Compare with orders to see if engagement turned into sales."],
    },
    "retention": {
        "up": "more partners/users stayed — a healthier base",
        "down": "more churn — often payout, effort, or low order density",
        "actions": ["Check order density and payouts for the affected partners.",
                    "Review operational effort / load signals around this date."],
    },
    "order": {
        "up": "order volume surged — more traffic, a promo, or better conversion",
        "down": "order volume dropped — weaker traffic/conversion, stockouts, or a promo ending",
        "actions": ["Check traffic / active users and conversion for the same day.",
                    "Look for stockouts or a promo starting/ending on this date."],
    },
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
    top = anomalies[:15]
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
                "week_on_week_change_pct": a.get("wow_change"),
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
        f"then one insight per detected anomaly. For each: a short title; a business impact that "
        f"FIRST states what the move means for the {division} team (the operational or financial "
        f"consequence — not a restatement of the raw numbers) and then notes both the day-on-day "
        f"and week-on-week (week_on_week_change_pct) direction in one or two short sentences; and "
        f"2-3 SPECIFIC, operational next steps that name concrete things to check (segments, the "
        f"date, related metrics), including a related metric to compare (use "
        f"'other_metrics_that_moved_same_day' when present, else the most relevant from all_metrics). "
        f"Do not use generic filler like 'verify the data is correct'. If no anomalies were detected, "
        f"summarize the trends and return an empty insights list. No statistical jargon; keep it concise."
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
    for a in anomalies:  # one insight per detected anomaly
        metric_h = _humanize(a["metric"])
        dom = _lookup(_DOMAIN, a["metric"]) or {}
        others = [_humanize(x["metric"]) for x in by_date.get(a["date"], []) if x["metric"] != a["metric"]]
        going = "up" if a["direction"] == "spike" else "down"

        # Business impact: what it MEANS for the business, then the trend figures.
        consequence = dom.get(going) or "moved well outside its normal range and is worth investigating"
        sign = "+" if a["pct_change"] > 0 else ""
        impact = f"{consequence[:1].upper()}{consequence[1:]}. Day-on-day {sign}{a['pct_change']}%"
        if a.get("wow_change") is not None:
            wsign = "+" if a["wow_change"] > 0 else ""
            impact += f", week-on-week {wsign}{a['wow_change']}%"
        impact += "."

        # Recommended steps: concrete + metric-specific; lead with same-day co-movers.
        recs = []
        if others:
            recs.append(f"{_human_list(others).capitalize()} also moved on {a['date']} — compare them to pin the shared driver.")
        recs.extend(dom.get("actions", []))
        if not recs:
            rel = _related_metrics(a["metric"], metric_names)
            recs.append(f"Check {_human_list(rel) or 'the related metrics'} for {a['date']} to find the driver.")
            recs.append(f"Segment {metric_h} by pickup zone / pincode to localise the {a['direction']}.")
        recs = recs[:3]

        cause = _lookup(_DOMAIN_CAUSE, a["metric"]) or "an operational or demand-side change"
        insights.append({
            "metric": a["metric"],
            "date": a["date"],
            "title": f"{metric_h.title()} {a['direction']} {sign}{a['pct_change']}% on {a['date']}",
            "root_causes": [f"Most likely {cause}."],
            "impact": impact,
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
