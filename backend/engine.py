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


def _weekly_aggregate(labels, values, avg):
    """Aggregate a daily series into 7-day windows from the first date.
    Each week is labelled with the first date present in it (matching the UI's
    weekly bucketing). Returns (week_labels, week_values)."""
    if not labels:
        return [], []
    d0 = _parse_date(labels[0])
    if d0 is None:
        return [], []
    weeks = {}  # wk_index -> [total, count, first_label]
    for lab, v in zip(labels, values):
        d = _parse_date(lab)
        if d is None:
            continue
        wk = (d - d0).days // 7
        b = weeks.get(wk)
        if b is None:
            weeks[wk] = [v, 1, lab]
        else:
            b[0] += v
            b[1] += 1
    wk_labels, wk_values, wk_counts = [], [], []
    for wk in sorted(weeks):
        total, count, first_label = weeks[wk]
        wk_labels.append(first_label)
        wk_values.append(round(total / count, 4) if avg else round(total, 4))
        wk_counts.append(count)
    return wk_labels, wk_values, wk_counts


# ----------------------------------------------------------------------------
# Data-driven RCA context — computed from the actual numbers, per anomaly
# ----------------------------------------------------------------------------

def _dod_on_date(series, date):
    """The day-over-day change of a metric on a specific date."""
    labels, values = series["labels"], series["values"]
    try:
        i = labels.index(date)
    except ValueError:
        return None
    if i == 0:
        return None
    cur, prev = values[i], values[i - 1]
    pct = (cur - prev) / abs(prev) * 100 if prev else 0.0
    return {"value": round(cur, 2), "prev": round(prev, 2), "pct_change": round(pct, 1)}


def _co_movers(metric, date, series_map, top=3, min_pct=8.0):
    """Other metrics that also moved notably the same day, ranked by size of move."""
    out = []
    for m, s in series_map.items():
        if m == metric:
            continue
        d = _dod_on_date(s, date)
        if d and abs(d["pct_change"]) >= min_pct:
            out.append({"metric": m, "pct_change": d["pct_change"]})
    out.sort(key=lambda x: abs(x["pct_change"]), reverse=True)
    return out[:top]


def _find_metric(series_map, includes=(), excludes=()):
    for m in series_map:
        lo = m.lower()
        if all(k in lo for k in includes) and not any(k in lo for k in excludes):
            return m
    return None


def _decompose(metric, date, series_map):
    """Attribute a GMV/revenue move to volume (orders) vs value (AOV)."""
    if not any(k in metric.lower() for k in ("gmv", "revenue", "sales")):
        return None
    vol = _find_metric(series_map, includes=("order",), excludes=("value", "avg", "aov", "cancel", "return"))
    val = (_find_metric(series_map, includes=("aov",)) or
           _find_metric(series_map, includes=("average", "order")) or
           _find_metric(series_map, includes=("avg", "order")) or
           _find_metric(series_map, includes=("order", "value")))
    dv = _dod_on_date(series_map[vol], date) if vol else None
    da = _dod_on_date(series_map[val], date) if val else None
    if not (dv and da):
        return None
    return {"volume_metric": vol, "volume_pct": dv["pct_change"],
            "value_metric": val, "value_pct": da["pct_change"]}


def _segment_breakdown(rows, date_col, metric, dimension_col, date, prev_date, avg):
    """Per-segment change on `date` vs `prev_date` — reveals which segment drove the move."""
    if not dimension_col or not prev_date:
        return []
    segs = {}
    for r in rows:
        d = _parse_date(r.get(date_col))
        if d is None:
            continue
        ds = d.strftime("%Y-%m-%d")
        if ds != date and ds != prev_date:
            continue
        v = _parse_number(r.get(metric))
        if v is None:
            continue
        seg = str(r.get(dimension_col)).strip() or "(blank)"
        b = segs.setdefault(seg, {"cur": [0.0, 0], "prev": [0.0, 0]})
        k = "cur" if ds == date else "prev"
        b[k][0] += v
        b[k][1] += 1
    out = []
    for seg, b in segs.items():
        if b["cur"][1] == 0 or b["prev"][1] == 0:
            continue
        cur = b["cur"][0] / b["cur"][1] if avg else b["cur"][0]
        prev = b["prev"][0] / b["prev"][1] if avg else b["prev"][0]
        delta = cur - prev
        pct = (delta / abs(prev) * 100) if prev else 0.0
        out.append({"segment": seg, "pct_change": round(pct, 1), "delta": delta,
                    "cur": round(cur, 2), "prev": round(prev, 2)})
    if not out:
        return []
    total = sum(x["delta"] for x in out)
    for x in out:
        x["contribution"] = round(x["delta"] / total * 100) if total else 0
    out.sort(key=lambda x: abs(x["delta"]), reverse=True)
    return out


def _attach_rca(anoms, series_map, rows, date_col, dimension_col, dimension_value):
    """Attach data-driven RCA context (co-movers, decomposition, segments) to each anomaly."""
    seg_ok = dimension_col and dimension_value in (None, "", "__all__")
    for a in anoms:
        m, date = a["metric"], a["date"]
        cm = _co_movers(m, date, series_map)
        if cm:
            a["co_movers"] = cm
        dec = _decompose(m, date, series_map)
        if dec:
            a["decomposition"] = dec
        if seg_ok:
            labels = series_map.get(m, {"labels": []})["labels"]
            prev_date = None
            if date in labels:
                i = labels.index(date)
                if i > 0:
                    prev_date = labels[i - 1]
            segs = _segment_breakdown(rows, date_col, m, dimension_col, date, prev_date, _is_average_metric(m))
            if segs:
                a["segments"] = segs


def analyze(rows, mapping, sensitivity="medium"):
    """Core analysis: KPIs, per-metric series, and ranked anomalies."""
    date_col = mapping["date_col"]
    metric_cols = mapping["metric_cols"]
    dimension_col = mapping.get("dimension_col")
    dimension_value = mapping.get("dimension_value")
    threshold = _SENSITIVITY.get(sensitivity, 2.5)

    kpis, series, all_anomalies, metric_stats = [], {}, [], []
    all_anomalies_weekly = []
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
        avg_metric = _is_average_metric(metric)
        anoms = _detect_anomalies(metric, labels, values, threshold)
        _attach_wow(anoms, labels, values, avg_metric)
        for a in anoms:
            a["period"] = "day"
        all_anomalies.extend(anoms)

        # Week-on-week anomalies: aggregate to weekly buckets, detect on those.
        wk_labels, wk_values, wk_counts = _weekly_aggregate(labels, values, avg_metric)
        # For summed metrics, a trailing partial week has a lower total purely
        # because the week isn't over — drop it so it isn't flagged as a "drop".
        # "Partial" = the last week has fewer days than the week before it.
        if not avg_metric and len(wk_counts) >= 2 and wk_counts[-1] < wk_counts[-2]:
            wk_labels, wk_values = wk_labels[:-1], wk_values[:-1]
        wanoms = _detect_anomalies(metric, wk_labels, wk_values, threshold)
        for a in wanoms:
            a["period"] = "week"
        all_anomalies_weekly.extend(wanoms)

    # Attach data-driven RCA context (co-movers, decomposition, segment breakdown).
    _attach_rca(all_anomalies, series, rows, date_col, dimension_col, dimension_value)

    # rank: latest first, then by severity magnitude
    sev_rank = {"critical": 3, "high": 2, "moderate": 1}
    all_anomalies.sort(key=lambda a: (a["is_latest"], sev_rank[a["severity"]], abs(a["zscore"])), reverse=True)
    all_anomalies_weekly.sort(key=lambda a: (a["is_latest"], sev_rank[a["severity"]], abs(a["zscore"])), reverse=True)

    return {
        "kpis": kpis,
        "series": series,
        "anomalies": all_anomalies,
        "anomalies_weekly": all_anomalies_weekly,
        "metric_stats": metric_stats,
    }


# ----------------------------------------------------------------------------
# LLM RCA layer (with statistical fallback)
# ----------------------------------------------------------------------------

MODEL = os.getenv("ANTHROPIC_MODEL", "claude-opus-4-8")

# Optional OpenAI-compatible gateway (e.g. Bifrost). When both URL and key are set,
# RCA narratives are written by this gateway; otherwise Anthropic (if a key is set),
# otherwise the statistical fallback.
LLM_GATEWAY_URL = os.getenv("LLM_GATEWAY_URL", "").strip()
LLM_API_KEY = os.getenv("LLM_API_KEY", "").strip()
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o").strip()

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


def _llm_payload(division, metric_stats, anomalies):
    """Structured evidence sent to the model — includes the data-driven RCA context."""
    top = anomalies[:15]
    by_date = _by_date(anomalies)
    return {
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
                # Data-driven RCA evidence computed from the raw numbers:
                "co_moving_metrics_same_day": a.get("co_movers"),
                "segment_breakdown": (a.get("segments") or [])[:4],
                "volume_vs_value_decomposition": a.get("decomposition"),
            }
            for a in top
        ],
    }


def _llm_user_text(division, payload, want_json_shape=False):
    """Shared instruction prompt for both the Anthropic and OpenAI-gateway paths."""
    text = (
        f"Business division: {division}\n\n"
        f"Day-on-day analysis (pct_change is the day-over-day % change; severity is how "
        f"notable the move is):\n\n{json.dumps(payload, indent=2)}\n\n"
        f"Write a 2-3 sentence executive_summary of what changed for the {division} team, "
        f"then one insight per detected anomaly. Use the EXACT 'metric' and 'date' strings from each "
        f"detected anomaly in your insight objects. For each: a short title; a business impact that "
        f"FIRST states what the move means for the {division} team (the operational or financial "
        f"consequence — not a restatement of the raw numbers) and then notes both the day-on-day "
        f"and week-on-week (week_on_week_change_pct) direction in one or two short sentences; and "
        f"2-3 SPECIFIC, operational next steps that name concrete things to check (segments, the "
        f"date, related metrics). GROUND the root_causes in the data provided per anomaly: if "
        f"'segment_breakdown' shows one segment moved far more than others, name it as the likely source; "
        f"if 'volume_vs_value_decomposition' shows one side dominated (e.g. orders vs AOV), say which drove "
        f"the move; and cite 'co_moving_metrics_same_day' (with their % changes) as evidence of a shared "
        f"driver. Point the first next step at whatever the data implicates (the segment / the dominant "
        f"driver / the strongest co-mover). "
        f"Do not use generic filler like 'verify the data is correct'. If no anomalies were detected, "
        f"summarize the trends and return an empty insights list. No statistical jargon; keep it concise."
    )
    if want_json_shape:
        text += (
            "\n\nRespond with ONLY a valid JSON object (no markdown fences, no prose outside it) of "
            'exactly this shape: {"executive_summary": "string", "insights": [{"metric": "string", '
            '"date": "YYYY-MM-DD", "title": "string", "root_causes": ["string"], "impact": "string", '
            '"recommendations": ["string"], "confidence": "high|medium|low"}]}'
        )
    return text


def _normalize_llm_output(parsed):
    """Coerce a model's JSON into the shape the UI expects; raise if unusable."""
    if not isinstance(parsed, dict) or "insights" not in parsed:
        raise RuntimeError("LLM response missing 'insights'")
    out_ins = []
    for i in (parsed.get("insights") or []):
        if not isinstance(i, dict) or not i.get("metric") or not i.get("date"):
            continue
        out_ins.append({
            "metric": i["metric"],
            "date": i["date"],
            "title": i.get("title") or f"{_humanize(i['metric']).title()} anomaly on {i['date']}",
            "root_causes": i.get("root_causes") or [],
            "impact": i.get("impact") or "",
            "recommendations": i.get("recommendations") or [],
            "confidence": i.get("confidence") if i.get("confidence") in ("high", "medium", "low") else "medium",
        })
    return {"executive_summary": parsed.get("executive_summary") or "", "insights": out_ins}


def _llm_insights(division, metric_stats, anomalies):
    """Call Claude (Anthropic) for RCA insights. Raises on any failure (caller falls back)."""
    import anthropic  # imported lazily so the app runs without the package for stats-only mode

    client = anthropic.Anthropic()
    payload = _llm_payload(division, metric_stats, anomalies)
    user = _llm_user_text(division, payload)
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
    return _normalize_llm_output(json.loads(text))


def _post_gateway(body, timeout=90):
    """POST a chat-completions body to the configured OpenAI-compatible gateway and
    return the parsed JSON response. Uses only the stdlib (+ certifi for CA certs)."""
    import ssl
    import urllib.request
    import urllib.error

    try:  # use certifi's CA bundle — macOS system Python often lacks root certs
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001
        ctx = ssl.create_default_context()
    if os.getenv("LLM_INSECURE_SSL") == "1":  # last-resort escape hatch for internal certs
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    req = urllib.request.Request(
        LLM_GATEWAY_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {LLM_API_KEY}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"gateway HTTP {e.code}: {e.read()[:200]!r}")


def _llm_insights_openai(division, metric_stats, anomalies):
    """Call an OpenAI-compatible gateway (e.g. Bifrost) for RCA insights.
    Raises on failure (caller falls back)."""
    payload = _llm_payload(division, metric_stats, anomalies)
    user = _llm_user_text(division, payload, want_json_shape=True)
    body = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": user},
        ],
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    }
    data = _post_gateway(body)
    content = data["choices"][0]["message"]["content"]
    if isinstance(content, str):
        content = content.strip()
        if content.startswith("```"):  # strip accidental markdown fences
            content = content.strip("`")
            content = content[content.find("{"):content.rfind("}") + 1]
    return _normalize_llm_output(json.loads(content))


_CHAT_SYSTEM = (
    "You are Pulse, a friendly data-analyst assistant for an Indian e-commerce team. "
    "Answer the user's question ONLY from the DATA SNAPSHOT provided (metrics, their recent "
    "values and trends, and detected anomalies with their evidence). Rules:\n"
    "- Be concise and use plain business language — no statistical jargon (no 'z-score', "
    "'standard deviation', 'sigma', etc.).\n"
    "- Cite specific numbers, dates, and segments from the snapshot to support your answer.\n"
    "- If the snapshot doesn't contain the answer, say so plainly instead of guessing. "
    "Never invent numbers.\n"
    "- When asked 'why' about an anomaly, use its segment breakdown, volume/value split, and "
    "co-moving metrics as the explanation.\n"
    "- Keep answers to a few sentences unless asked for detail."
)


def chat_answer(division, context, history, question):
    """Answer a free-text question grounded in the analysis snapshot.
    Prefers the gateway, then Anthropic; raises if neither is configured (caller falls back)."""
    system = _CHAT_SYSTEM + f"\n\nThe user is on the {division} team.\n\nDATA SNAPSHOT (JSON):\n" + \
        json.dumps(context)[:14000]
    convo = []
    for h in (history or [])[-6:]:
        role = "assistant" if h.get("role") == "assistant" else "user"
        convo.append({"role": role, "content": str(h.get("content", ""))[:2000]})
    convo.append({"role": "user", "content": str(question)[:2000]})

    if LLM_GATEWAY_URL and LLM_API_KEY:
        body = {
            "model": LLM_MODEL,
            "messages": [{"role": "system", "content": system}] + convo,
            "temperature": 0.3,
        }
        data = _post_gateway(body)
        return data["choices"][0]["message"]["content"].strip(), LLM_MODEL
    if os.getenv("ANTHROPIC_API_KEY"):
        import anthropic
        client = anthropic.Anthropic()
        resp = client.messages.create(model=MODEL, max_tokens=1000, system=system, messages=convo)
        text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()
        return text, MODEL
    raise RuntimeError("no LLM configured for chat")


def _sgn(p):
    return ("+" if p > 0 else "") + str(p)


def _fallback_insights(division, metric_stats, anomalies):
    """Data-driven, plain-language insights (used when the LLM is unavailable).
    Root causes and next steps are derived from the actual numbers — which segment
    drove the move, volume vs value, and which metrics co-moved the same day."""
    insights = []
    for a in anomalies:  # one insight per detected anomaly
        metric_h = _humanize(a["metric"])
        dom = _lookup(_DOMAIN, a["metric"]) or {}
        going = "up" if a["direction"] == "spike" else "down"
        weekly = a.get("period") == "week"
        when = f"the week of {a['date']}" if weekly else a["date"]

        # Business impact: what it MEANS for the business, then the trend figures.
        consequence = dom.get(going) or "moved well outside its normal range and is worth investigating"
        sign = "+" if a["pct_change"] > 0 else ""
        if weekly:
            impact = f"{consequence[:1].upper()}{consequence[1:]}. Week-on-week {sign}{a['pct_change']}%."
        else:
            impact = f"{consequence[:1].upper()}{consequence[1:]}. Day-on-day {sign}{a['pct_change']}%"
            if a.get("wow_change") is not None:
                wsign = "+" if a["wow_change"] > 0 else ""
                impact += f", week-on-week {wsign}{a['wow_change']}%"
            impact += "."

        # ---- Data-driven root causes (most specific first) ----
        causes, recs = [], []
        segs = a.get("segments") or []
        dec = a.get("decomposition")
        cms = a.get("co_movers") or []

        if segs:
            top = segs[0]
            second = segs[1] if len(segs) > 1 else None
            share = top.get("contribution", 0)
            if second and abs(top["pct_change"]) >= 1.5 * (abs(second["pct_change"]) or 1e-9):
                causes.append(f"Concentrated in {top['segment']} ({_sgn(top['pct_change'])}%), while "
                              f"{second['segment']} moved only {_sgn(second['pct_change'])}% — the {a['direction']} is not broad-based.")
            elif 0 < share <= 100:
                causes.append(f"{top['segment']} is the biggest mover ({_sgn(top['pct_change'])}%), roughly {share}% of the net change"
                              + (f"; {second['segment']} {_sgn(second['pct_change'])}%." if second else "."))
            else:
                causes.append(f"{top['segment']} moved the most ({_sgn(top['pct_change'])}%)"
                              + (f", {second['segment']} {_sgn(second['pct_change'])}%." if second else "."))
            recs.append(f"Start with {top['segment']} — it drove the {a['direction']}; check its inputs/ops for {when}.")

        if dec:
            v_dom = abs(dec["volume_pct"]) >= abs(dec["value_pct"])
            drv = dec["volume_metric"] if v_dom else dec["value_metric"]
            causes.append(
                f"{'Volume' if v_dom else 'Basket-value'}-driven: {_humanize(dec['volume_metric'])} {_sgn(dec['volume_pct'])}% "
                f"vs {_humanize(dec['value_metric'])} {_sgn(dec['value_pct'])}% — {_humanize(drv)} did most of the work.")
            recs.append(f"Focus on {_humanize(drv)}; the other side of the equation was roughly flat.")

        if cms:
            names = ", ".join(f"{_humanize(c['metric'])} {_sgn(c['pct_change'])}%" for c in cms[:2])
            causes.append(f"Moved together with {names} on {a['date']} — likely a shared driver, not isolated to {metric_h}.")
            recs.append(f"Cross-check {_humanize(cms[0]['metric'])} for {when} to confirm the common cause.")

        if not causes:
            cause = _lookup(_DOMAIN_CAUSE, a["metric"]) or "an operational or demand-side change"
            causes.append(f"No single segment or co-moving metric stood out — most likely {cause}.")

        recs.extend(dom.get("actions", []))
        if not recs:
            recs.append(f"Segment {metric_h} by pickup zone / pincode to localise the {a['direction']}.")
        recs, causes = recs[:4], causes[:3]

        title = (f"{metric_h.title()} {a['direction']} {sign}{a['pct_change']}% · week of {a['date']}"
                 if weekly else
                 f"{metric_h.title()} {a['direction']} {sign}{a['pct_change']}% on {a['date']}")
        conf = "high" if (segs or dec) else ("medium" if cms else "low")
        insights.append({
            "metric": a["metric"],
            "date": a["date"],
            "title": title,
            "root_causes": causes,
            "impact": impact,
            "recommendations": recs,
            "confidence": conf,
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


def ai_configured():
    """True if an LLM (gateway or Anthropic) is available for AI narratives."""
    return bool((LLM_GATEWAY_URL and LLM_API_KEY) or os.getenv("ANTHROPIC_API_KEY"))


def generate_rca(division, analysis_result, use_llm=True):
    """Return {executive_summary, insights, insights_weekly, mode, model}.
    With use_llm=False, returns the fast statistical insights only (no network) — used for
    the instant initial render. With use_llm=True, narrative source order is: OpenAI-compatible
    gateway → Anthropic → statistical fallback. Any failure degrades gracefully."""
    metric_stats = analysis_result["metric_stats"]
    anomalies = analysis_result["anomalies"]
    anomalies_weekly = analysis_result.get("anomalies_weekly", [])

    out, mode, model = None, "statistical", None
    if use_llm and LLM_GATEWAY_URL and LLM_API_KEY:
        try:
            out = _llm_insights_openai(division, metric_stats, anomalies)
            mode, model = "llm", LLM_MODEL
        except Exception:  # noqa: BLE001
            out = None
    if out is None and use_llm and os.getenv("ANTHROPIC_API_KEY"):
        try:
            out = _llm_insights(division, metric_stats, anomalies)
            mode, model = "llm", MODEL
        except Exception:  # noqa: BLE001
            out = None
    if out is None:
        out = _fallback_insights(division, metric_stats, anomalies)
        mode, model = "statistical", None

    out["mode"] = mode
    out["model"] = model
    # Weekly insights always via the domain-aware fallback — reliable and cheap.
    out["insights_weekly"] = _fallback_insights(division, metric_stats, anomalies_weekly)["insights"]
    return out
