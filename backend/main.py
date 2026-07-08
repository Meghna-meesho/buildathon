"""FastAPI app for the Anomaly Detector & RCA Engine.

Serves the build-free React frontend and exposes:
  POST /api/login    – mock login + business-division selector
  POST /api/upload   – parse a CSV, profile columns, infer roles
  POST /api/analyze  – detect anomalies + generate RCA insights
"""
from __future__ import annotations

import base64
import json
import os
import uuid

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from engine import analyze, generate_rca, parse_csv, profile_columns, _parse_date


def _filter_rows_by_date(rows, date_col, from_date, to_date):
    """Keep only rows whose date falls within [from_date, to_date] (inclusive)."""
    if not date_col or (not from_date and not to_date):
        return rows
    fd = _parse_date(from_date) if from_date else None
    td = _parse_date(to_date) if to_date else None
    out = []
    for r in rows:
        d = _parse_date(r.get(date_col))
        if d is None:
            continue
        if fd and d < fd:
            continue
        if td and d > td:
            continue
        out.append(r)
    return out

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRONTEND_DIR = os.path.join(BASE_DIR, "frontend")

app = FastAPI(title="Anomaly Detector & RCA Engine")

# in-memory store of uploaded datasets (fine for a single-instance demo app)
_UPLOADS: dict[str, dict] = {}
_MAX_UPLOADS = 20

DIVISIONS = ["Grocery", "Growth", "Valmo"]


# --------------------------------------------------------------------------- #
# Auth (mock)
# --------------------------------------------------------------------------- #
@app.get("/api/divisions")
def divisions():
    return {"divisions": DIVISIONS}


@app.post("/api/login")
async def login(payload: dict):
    # JSON body (not multipart) so it passes strict corporate proxies/DLP.
    username = str(payload.get("username", "")).strip()
    password = str(payload.get("password", "")).strip()
    division = str(payload.get("division", "")).strip()
    if not username or not password:
        raise HTTPException(400, "Username and password are required.")
    if not division:
        raise HTTPException(400, "Please select a business division.")
    token = base64.urlsafe_b64encode(f"{username}:{division}".encode()).decode()
    return {"token": token, "username": username, "division": division}


# --------------------------------------------------------------------------- #
# Upload + profiling
# --------------------------------------------------------------------------- #
@app.post("/api/upload")
async def upload(payload: dict):
    # The browser reads the file and sends its text as JSON, so there is no
    # multipart file upload for corporate proxies/DLP to block.
    filename = str(payload.get("filename", "data.csv"))
    content = payload.get("content")
    if not filename.lower().endswith(".csv"):
        raise HTTPException(400, "Please upload a .csv file.")
    if not content:
        raise HTTPException(400, "The uploaded file is empty.")
    try:
        headers, rows = parse_csv(content)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"Could not parse CSV: {e}")
    if not headers or not rows:
        raise HTTPException(400, "No data rows found in the CSV.")

    cols, inferred = profile_columns(headers, rows)

    upload_id = uuid.uuid4().hex
    if len(_UPLOADS) >= _MAX_UPLOADS:  # simple LRU-ish cap
        _UPLOADS.pop(next(iter(_UPLOADS)))
    _UPLOADS[upload_id] = {"headers": headers, "rows": rows}

    return {
        "upload_id": upload_id,
        "filename": filename,
        "row_count": len(rows),
        "columns": cols,
        "inferred": inferred,
        "preview": rows[:5],
    }


# --------------------------------------------------------------------------- #
# Analyze
# --------------------------------------------------------------------------- #
@app.post("/api/analyze")
async def analyze_endpoint(payload: dict):
    upload_id = payload.get("upload_id")
    mapping = payload.get("mapping") or {}
    division = (payload.get("division") or "your team").strip()
    sensitivity = payload.get("sensitivity", "medium")
    from_date = payload.get("from_date")
    to_date = payload.get("to_date")

    data = _UPLOADS.get(upload_id)
    if not data:
        raise HTTPException(404, "Upload not found or expired. Please re-upload the CSV.")
    if not mapping.get("date_col"):
        raise HTTPException(400, "A date column is required.")
    if not mapping.get("metric_cols"):
        raise HTTPException(400, "Select at least one metric column.")

    rows = _filter_rows_by_date(data["rows"], mapping.get("date_col"), from_date, to_date)
    result = analyze(rows, mapping, sensitivity=sensitivity)
    rca = generate_rca(division, result)

    return {
        "division": division,
        "sensitivity": sensitivity,
        "dimension_col": mapping.get("dimension_col"),
        "dimension_value": mapping.get("dimension_value"),
        "kpis": result["kpis"],
        "series": result["series"],
        "anomalies": result["anomalies"],
        "anomalies_weekly": result.get("anomalies_weekly", []),
        "executive_summary": rca["executive_summary"],
        "insights": rca["insights"],
        "insights_weekly": rca.get("insights_weekly", []),
        "mode": rca["mode"],
        "notice": rca.get("notice"),
    }


@app.post("/api/dimension-values")
async def dimension_values(payload: dict):
    """Distinct values for a chosen dimension column (for the filter dropdown)."""
    data = _UPLOADS.get(payload.get("upload_id"))
    col = payload.get("dimension_col")
    if not data or not col:
        return {"values": []}
    seen = []
    for r in data["rows"]:
        v = str(r.get(col, "")).strip()
        if v and v not in seen:
            seen.append(v)
        if len(seen) >= 50:
            break
    return {"values": sorted(seen)}


# --------------------------------------------------------------------------- #
# Frontend (static, build-free React)
# --------------------------------------------------------------------------- #
@app.get("/")
def index():
    return FileResponse(
        os.path.join(FRONTEND_DIR, "index.html"),
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


@app.get("/health")
def health():
    return {"ok": True, "llm_enabled": bool(os.getenv("ANTHROPIC_API_KEY"))}


# mounted last so /api/* routes take precedence
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="static")
