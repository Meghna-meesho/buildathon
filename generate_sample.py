"""Generate realistic sample CSVs for demoing the RCA engine.

Writes two files:
  - sample_data.csv         generic multi-division metrics (GMV, orders, ...)
  - self_pickup_sample.csv  Meesho Grocery Self Pickup metrics per Nagpur zone
                            (completion rate, RTO%, adoption%, CPDO, shrinkage, NPS)

Both include a few planted anomalies so the detector + RCA have something to explain.
The self-pickup file includes a *linked* incident (completion rate drops the same day
RTO% spikes) to showcase the "check a related metric" behaviour.

Run:  python3 generate_sample.py
"""
import csv
import random
from datetime import date, timedelta

random.seed(7)
DAYS = 45
START = date(2026, 5, 24)


def jitter(v, pct=0.05):
    return v * (1 + random.uniform(-pct, pct))


# --------------------------------------------------------------------------- #
# 1) Generic multi-division dataset
# --------------------------------------------------------------------------- #
def generic():
    divisions = ["Grocery", "Fashion", "Electronics"]
    base = {
        "Grocery":     dict(gmv=1_800_000, orders=42_000, active_users=310_000, conversion_rate=4.8, avg_order_value=430, return_rate=3.1),
        "Fashion":     dict(gmv=3_200_000, orders=58_000, active_users=420_000, conversion_rate=3.9, avg_order_value=560, return_rate=8.4),
        "Electronics": dict(gmv=2_400_000, orders=19_000, active_users=180_000, conversion_rate=2.6, avg_order_value=1260, return_rate=5.2),
    }
    anomalies = [
        ("Grocery", 30, "gmv", 0.62), ("Grocery", 30, "orders", 0.68),
        ("Fashion", 22, "return_rate", 1.85),
        ("Electronics", 38, "conversion_rate", 1.7), ("Electronics", 38, "orders", 1.55),
    ]
    rows = []
    for div in divisions:
        b = base[div]
        for i in range(DAYS):
            d = START + timedelta(days=i)
            weekend = 1.12 if d.weekday() >= 5 else 1.0
            trend = 1 + (i / DAYS) * 0.08
            row = {"date": d.isoformat(), "division": div}
            for m, bv in b.items():
                if m in ("conversion_rate", "return_rate"):
                    v = jitter(bv, 0.06) * (weekend if m == "conversion_rate" else 1.0)
                elif m == "avg_order_value":
                    v = jitter(bv, 0.03)
                else:
                    v = jitter(bv, 0.05) * weekend * trend
                for adiv, aday, am, mult in anomalies:
                    if adiv == div and aday == i and am == m:
                        v *= mult
                row[m] = round(v, 2) if m in ("conversion_rate", "return_rate", "avg_order_value") else int(round(v))
            rows.append(row)
    rows.sort(key=lambda r: (r["date"], r["division"]))
    _write("sample_data.csv",
           ["date", "division", "gmv", "orders", "active_users", "conversion_rate", "avg_order_value", "return_rate"],
           rows)


# --------------------------------------------------------------------------- #
# 2) Self Pickup dataset (mirrors the KRD)
# --------------------------------------------------------------------------- #
def self_pickup():
    zones = ["Sitabuldi", "Dharampeth", "Sadar"]  # Nagpur pilot zones
    base = {
        "Sitabuldi":  dict(orders=140, completion_rate=92.0, rto_pct=6.5, adoption_pct=38.0, cpdo=27.0, avg_pickup_distance_m=480, shrinkage_pct=1.1, nps=64),
        "Dharampeth": dict(orders=110, completion_rate=90.5, rto_pct=7.8, adoption_pct=34.0, cpdo=29.0, avg_pickup_distance_m=560, shrinkage_pct=1.3, nps=61),
        "Sadar":      dict(orders=95,  completion_rate=91.2, rto_pct=7.0, adoption_pct=31.0, cpdo=31.0, avg_pickup_distance_m=620, shrinkage_pct=1.2, nps=59),
    }
    # (zone or "*", day, metric, multiplier)
    anomalies = [
        # Day 28: platform-wide pickup incident — completion drops while RTO spikes (linked)
        ("*", 28, "completion_rate", 0.82), ("*", 28, "rto_pct", 1.8),
        # Day 20: discount pulled in one zone — adoption dips
        ("Dharampeth", 20, "adoption_pct", 0.55),
        # Day 35: cost per delivered order spikes everywhere (density/distance)
        ("*", 35, "cpdo", 1.4), ("*", 35, "avg_pickup_distance_m", 1.25),
    ]
    rows = []
    for zone in zones:
        b = base[zone]
        for i in range(DAYS):
            d = START + timedelta(days=i)
            weekend = 1.1 if d.weekday() >= 5 else 1.0
            trend = 1 + (i / DAYS) * 0.10  # adoption/orders grow slowly as pilot matures
            row = {"date": d.isoformat(), "pickup_zone": zone}
            for m, bv in b.items():
                if m == "orders":
                    v = jitter(bv, 0.06) * weekend * trend
                elif m == "adoption_pct":
                    v = jitter(bv, 0.05) * trend
                else:
                    v = jitter(bv, 0.04)
                for az, aday, am, mult in anomalies:
                    if (az in ("*", zone)) and aday == i and am == m:
                        v *= mult
                if m in ("orders", "avg_pickup_distance_m", "nps"):
                    row[m] = int(round(v))
                else:
                    row[m] = round(v, 2)
            rows.append(row)
    rows.sort(key=lambda r: (r["date"], r["pickup_zone"]))
    _write("self_pickup_sample.csv",
           ["date", "pickup_zone", "orders", "completion_rate", "rto_pct", "adoption_pct",
            "cpdo", "avg_pickup_distance_m", "shrinkage_pct", "nps"],
           rows)


def _write(name, fields, rows):
    with open(name, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {name}: {len(rows)} rows")


if __name__ == "__main__":
    generic()
    self_pickup()
