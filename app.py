#!/usr/bin/env python3
"""
Flask web UI for the Philadelphia crime address scorer.
"""

import os
import numpy as np
import pandas as pd
import requests
from flask import Flask, request, jsonify, render_template_string
from scipy.spatial import cKDTree
from pyproj import Transformer
from shapely.geometry import MultiPoint
from pathlib import Path

DATA_PATH = Path(os.environ.get("DATA_PATH", "output/incidents_24mo.parquet"))

WEIGHTS = {
    "Homicide - Criminal":                      100,
    "Aggravated Assault Firearm":                60,
    "Robbery Firearm":                           50,
    "Rape":                                      50,
    "Aggravated Assault No Firearm":             30,
    "Robbery No Firearm":                        25,
    "Arson":                                     25,
    "Burglary Residential":                      25,
    "Other Sex Offenses (Not Commercialized)":   20,
    "Motor Vehicle Theft":                       15,
    "Weapon Violations":                         15,
    "Offenses Against Family and Children":      10,
    "Other Assaults":                            10,
    "Burglary Non-Residential":                  10,
    "Theft from Vehicle":                         8,
    "Thefts":                                     5,
    "Vandalism/Criminal Mischief":                3,
}

BUCKETS = {
    "Violent": [
        "Homicide - Criminal", "Aggravated Assault Firearm",
        "Aggravated Assault No Firearm", "Other Assaults",
        "Robbery Firearm", "Robbery No Firearm", "Rape",
        "Other Sex Offenses (Not Commercialized)",
        "Weapon Violations", "Offenses Against Family and Children",
    ],
    "Burglary": ["Burglary Residential", "Burglary Non-Residential"],
    "Vehicle": ["Motor Vehicle Theft", "Theft from Vehicle"],
    "Property": ["Thefts", "Vandalism/Criminal Mischief", "Arson"],
}

PERCENTILE_LENSES = {
    "Gun/Murder": [
        "Homicide - Criminal",
        "Aggravated Assault Firearm",
        "Robbery Firearm",
    ],
    "All Violent": [
        "Homicide - Criminal",
        "Aggravated Assault Firearm", "Aggravated Assault No Firearm",
        "Robbery Firearm", "Robbery No Firearm",
        "Rape", "Other Sex Offenses (Not Commercialized)",
        "Weapon Violations", "Offenses Against Family and Children",
        "Other Assaults",
    ],
    "Vehicle+Property": [
        "Motor Vehicle Theft", "Theft from Vehicle",
        "Thefts", "Vandalism/Criminal Mischief", "Arson",
        "Burglary Residential", "Burglary Non-Residential",
    ],
}

def bucket_for(code):
    for bname, codes in BUCKETS.items():
        if code in codes:
            return bname
    return "Other"


# ── Data loading (once at startup) ────────────────────────────────
print("Loading crime data...")
df = pd.read_parquet(DATA_PATH)
df = df[
    (df["point_x"] > -76) & (df["point_x"] < -74.9) &
    (df["point_y"] > 39.8) & (df["point_y"] < 40.2)
].copy()
df["bucket"] = df["text_general_code"].map(bucket_for)
df["weight"] = df["text_general_code"].map(WEIGHTS).fillna(1)
df["ts"] = pd.to_datetime(df["dispatch_date_time"])

to_utm = Transformer.from_crs("EPSG:4326", "EPSG:32618", always_xy=True)
df["utm_x"], df["utm_y"] = to_utm.transform(df["point_x"].values, df["point_y"].values)
tree = cKDTree(df[["utm_x", "utm_y"]].values)

# Pre-build percentile grid
GRID_STEP_M = 61  # 200ft
PCTL_HALF = 100  # half-side of 200m × 200m square (~1 Philly block)

xs = np.arange(df["utm_x"].min(), df["utm_x"].max(), GRID_STEP_M)
ys = np.arange(df["utm_y"].min(), df["utm_y"].max(), GRID_STEP_M)
gxx, gyy = np.meshgrid(xs, ys)
grid_pts = np.column_stack([gxx.ravel(), gyy.ravel()])
print(f"  Building spatial index for {len(grid_pts):,} grid points...")
grid_neighbors = tree.query_ball_point(grid_pts, PCTL_HALF, p=np.inf)
any_crime = np.array([len(idxs) > 0 for idxs in grid_neighbors])
n_populated = int(any_crime.sum())

# Pre-compute grid scores for each lens x time window
latest = df["ts"].max()
TIME_WINDOWS = [
    ("0-8mo",   0,  8),
    ("8-16mo",  8, 16),
    ("16-24mo", 16, 24),
]

print("  Pre-computing percentile grids...")
grid_score_cache = {}
for wlabel, start_mo, end_mo in TIME_WINDOWS:
    cutoff_recent = latest - pd.DateOffset(months=start_mo)
    cutoff_old = latest - pd.DateOffset(months=end_mo)
    time_mask = ((df["ts"] <= cutoff_recent) & (df["ts"] >= cutoff_old)).values

    for lens_name, categories in PERCENTILE_LENSES.items():
        cat_mask = df["text_general_code"].isin(categories).values
        combined = time_mask & cat_mask
        w = np.where(combined, df["weight"].values, 0.0)

        scores = np.array([
            w[idxs].sum() if len(idxs) > 0 else 0.0
            for idxs in grid_neighbors
        ])
        city_scores = np.sort(scores[any_crime])
        grid_score_cache[(lens_name, wlabel)] = city_scores

print(f"  Ready. {len(df):,} incidents, {n_populated:,} populated blocks.")


# ── Geocoding ─────────────────────────────────────────────────────
def geocode(address):
    if "philadelphia" not in address.lower() and "phila" not in address.lower():
        address = address.rstrip(",. ") + ", Philadelphia, PA"
    elif ", pa" not in address.lower() and ", pennsylvania" not in address.lower():
        address = address.rstrip(",. ") + ", PA"

    url = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"
    r = requests.get(url, params={
        "address": address,
        "benchmark": "Public_AR_Current",
        "format": "json",
    }, timeout=15)
    r.raise_for_status()
    matches = r.json()["result"]["addressMatches"]
    if not matches:
        return None
    m = matches[0]
    return {
        "matched": m["matchedAddress"],
        "lon": m["coordinates"]["x"],
        "lat": m["coordinates"]["y"],
    }


# ── Scoring ───────────────────────────────────────────────────────
def score_address(lat, lon):
    ax, ay = to_utm.transform(lon, lat)
    results = {"radii": [], "top_crimes": [], "percentiles": {}}

    # Radius tables
    for radius_m, rlabel in [(122, "400 ft"), (244, "800 ft"),
                              (400, "1/4 mile"), (800, "1/2 mile")]:
        idxs = tree.query_ball_point([ax, ay], radius_m)
        nearby = df.iloc[idxs]

        for wlabel, start_mo, end_mo in TIME_WINDOWS:
            cutoff_recent = latest - pd.DateOffset(months=start_mo)
            cutoff_old = latest - pd.DateOffset(months=end_mo)
            windowed = nearby[(nearby["ts"] <= cutoff_recent) & (nearby["ts"] >= cutoff_old)]

            row = {
                "radius": rlabel, "window": wlabel,
                "incidents": int(len(windowed)),
                "weighted_score": int(windowed["weight"].sum()),
            }
            for bname in ["Violent", "Burglary", "Vehicle", "Property", "Other"]:
                mask = windowed["bucket"] == bname
                row[f"{bname}_n"] = int(mask.sum())
                row[f"{bname}_w"] = int(windowed.loc[mask, "weight"].sum())
            results["radii"].append(row)

    # Top crime types (200m × 200m square, full 24mo)
    idxs_qmi = tree.query_ball_point([ax, ay], PCTL_HALF, p=np.inf)
    nearby_qmi = df.iloc[idxs_qmi]
    grouped = nearby_qmi.groupby("text_general_code").agg(
        count=("weight", "size"),
        weighted=("weight", "sum"),
    ).sort_values("weighted", ascending=False).head(10)
    for code, row in grouped.iterrows():
        results["top_crimes"].append({
            "type": code,
            "count": int(row["count"]),
            "weighted": int(row["weighted"]),
        })

    # Percentile ranks (using pre-computed grid)
    this_neighbors = tree.query_ball_point([ax, ay], PCTL_HALF, p=np.inf)
    for wlabel, start_mo, end_mo in TIME_WINDOWS:
        cutoff_recent = latest - pd.DateOffset(months=start_mo)
        cutoff_old = latest - pd.DateOffset(months=end_mo)
        time_mask = ((df["ts"] <= cutoff_recent) & (df["ts"] >= cutoff_old)).values

        for lens_name, categories in PERCENTILE_LENSES.items():
            cat_mask = df["text_general_code"].isin(categories).values
            combined = time_mask & cat_mask
            w = np.where(combined, df["weight"].values, 0.0)
            this_score = w[this_neighbors].sum() if len(this_neighbors) > 0 else 0.0

            city_scores = grid_score_cache[(lens_name, wlabel)]
            pct = float(np.searchsorted(city_scores, this_score) / len(city_scores) * 100)
            results["percentiles"][(lens_name, wlabel)] = round(pct)

    results["n_populated"] = n_populated

    # ── Comparison assessment ────────────────────────────────────
    # Use 1/8-mile radius (100m), most recent 8mo window for assessment
    assess_idxs = tree.query_ball_point([ax, ay], PCTL_HALF, p=np.inf)  # 200m square
    assess_nearby = df.iloc[assess_idxs]
    cutoff_recent_assess = latest - pd.DateOffset(months=0)
    cutoff_old_assess = latest - pd.DateOffset(months=8)
    assess_window = assess_nearby[
        (assess_nearby["ts"] <= cutoff_recent_assess) & (assess_nearby["ts"] >= cutoff_old_assess)
    ]
    assess_violent = int((assess_window["bucket"] == "Violent").sum())
    assess_property = int(assess_window["bucket"].isin(["Property", "Burglary", "Vehicle"]).sum())

    if True:
        # Annualize the 8-month counts
        ann_factor = 12.0 / 8.0
        ann_violent = assess_violent * ann_factor
        ann_property = assess_property * ann_factor

        # Area of 200m × 200m square in sq miles (200m ≈ 0.1243 mi)
        area_sq_mi = 0.1243 ** 2  # ~0.01545 sq mi

        # Reference annual crime rates per square mile (rate_per_100k × pop_density / 100,000)
        # National: violent 360/100k, property 1760/100k, pop density ~94/sq mi (but meaningless)
        #   Use urban avg ~3,500/sq mi → violent: 360*3500/100000=12.6, property: 1760*3500/100000=61.6
        # Somerville MA (02144): pop 84,018, area 4.12 sq mi → density 20,393/sq mi
        #   violent (murder+rape+robbery+assault): 221.4/100k → 221.4*20393/100000=45.1/sq mi
        #   property (burg+theft+vehicle): 1698.8/100k → 1698.8*20393/100000=346.4/sq mi
        # Philadelphia: pop 1,632,157, area 134.2 sq mi → density 12,162/sq mi
        #   violent: 908.7/100k → 908.7*12162/100000=110.5/sq mi
        #   property: 4547.6/100k → 4547.6*12162/100000=553.1/sq mi

        # Crimes per sq mi at this location (annualized)
        loc_violent_density = ann_violent / area_sq_mi
        loc_property_density = ann_property / area_sq_mi

        # Reference crimes per sq mi per year
        refs = {
            "Somerville (02144)": {
                "violent": 45.1, "property": 346.4,
                "violent_rate": 221.4, "property_rate": 1698.8,
            },
            "US Average (urban)": {
                "violent": 50.4, "property": 246.4,
                "violent_rate": 360.0, "property_rate": 1760.0,
            },
            "Philadelphia (citywide)": {
                "violent": 110.5, "property": 553.1,
                "violent_rate": 908.7, "property_rate": 4547.6,
            },
        }

        comparisons = {}
        for ref_name, ref in refs.items():
            comparisons[ref_name] = {
                "violent_vs": round((loc_violent_density / ref["violent"] - 1) * 100) if ref["violent"] > 0 else None,
                "property_vs": round((loc_property_density / ref["property"] - 1) * 100) if ref["property"] > 0 else None,
                "violent_rate": ref["violent_rate"],
                "property_rate": ref["property_rate"],
            }

        results["assessment"] = {
            "ann_violent": round(ann_violent),
            "ann_property": round(ann_property),
            "violent_density": round(loc_violent_density, 1),
            "property_density": round(loc_property_density, 1),
            "comparisons": comparisons,
        }

    # ── 3×3 grid squares for map overlay ─────────────────────────
    from_utm = Transformer.from_crs("EPSG:32618", "EPSG:4326", always_xy=True)
    side = PCTL_HALF * 2  # 200m
    # Use "All Violent" lens, most recent window for coloring
    recent_wlabel = TIME_WINDOWS[0][0]  # "0-8mo"
    recent_start, recent_end = TIME_WINDOWS[0][1], TIME_WINDOWS[0][2]
    cutoff_r = latest - pd.DateOffset(months=recent_start)
    cutoff_o = latest - pd.DateOffset(months=recent_end)
    t_mask = ((df["ts"] <= cutoff_r) & (df["ts"] >= cutoff_o)).values
    violent_cats = PERCENTILE_LENSES["All Violent"]
    c_mask = df["text_general_code"].isin(violent_cats).values
    combined_mask = t_mask & c_mask
    w_all = np.where(combined_mask, df["weight"].values, 0.0)
    city_scores_violent = grid_score_cache[("All Violent", recent_wlabel)]

    squares = []
    for dx in [-1, 0, 1]:
        for dy in [-1, 0, 1]:
            cx = ax + dx * side
            cy = ay + dy * side
            # Square bounds in UTM
            x0, y0 = cx - PCTL_HALF, cy - PCTL_HALF
            x1, y1 = cx + PCTL_HALF, cy + PCTL_HALF
            # Convert corners to lat/lon
            lon0, lat0 = from_utm.transform(x0, y0)
            lon1, lat1 = from_utm.transform(x1, y1)
            # Score this square
            sq_idxs = tree.query_ball_point([cx, cy], PCTL_HALF, p=np.inf)
            sq_score = w_all[sq_idxs].sum() if len(sq_idxs) > 0 else 0.0
            pct = float(np.searchsorted(city_scores_violent, sq_score) / len(city_scores_violent) * 100)
            squares.append({
                "bounds": [[lat0, lon0], [lat1, lon1]],
                "pct": round(pct),
                "center": dx == 0 and dy == 0,
            })

    results["squares"] = squares

    return results


# ── Flask app ─────────────────────────────────────────────────────
app = Flask(__name__)

HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Philadelphia Crime Scorer</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         background: #f5f5f5; color: #333; padding: 20px; }
  h1 { margin-bottom: 20px; }
  .search-box { display: flex; gap: 10px; margin-bottom: 20px; max-width: 600px; }
  .search-box input { flex: 1; padding: 12px; font-size: 16px; border: 2px solid #ccc;
                       border-radius: 6px; }
  .search-box input:focus { outline: none; border-color: #4a90d9; }
  .search-box button { padding: 12px 24px; font-size: 16px; background: #4a90d9;
                        color: white; border: none; border-radius: 6px; cursor: pointer; }
  .search-box button:hover { background: #357abd; }
  .search-box button:disabled { background: #aaa; cursor: wait; }
  .locate-btn a { display: flex; align-items: center; justify-content: center;
                   width: 34px; height: 34px; color: #333; text-decoration: none; }
  .locate-btn a:hover { background: #f4f4f4; }
  .sq-label { background: rgba(255,255,255,0.85) !important; border: none !important;
              box-shadow: none !important; font-weight: bold; font-size: 13px;
              padding: 1px 4px !important; }
  .sq-label::before { display: none !important; }
  #map { height: 350px; border-radius: 8px; margin-bottom: 16px; box-shadow: 0 1px 3px rgba(0,0,0,0.1);
         cursor: crosshair; }
  #map-hint { font-size: 12px; color: #888; margin-bottom: 10px; text-align: center; }
  #status { margin-bottom: 10px; color: #666; font-style: italic; }
  #error { color: #c0392b; margin-bottom: 10px; font-weight: bold; }
  #result { display: none; }
  .matched { font-size: 18px; font-weight: bold; margin-bottom: 5px; }
  .coords { color: #666; margin-bottom: 20px; font-size: 14px; }
  .section { background: white; border-radius: 8px; padding: 16px; margin-bottom: 16px;
             box-shadow: 0 1px 3px rgba(0,0,0,0.1); }
  .section h2 { font-size: 16px; margin-bottom: 12px; color: #555; border-bottom: 1px solid #eee;
                 padding-bottom: 8px; }
  table { border-collapse: collapse; width: 100%; font-size: 14px; font-family: monospace; }
  th, td { padding: 4px 8px; text-align: right; }
  th { background: #f0f0f0; font-weight: bold; }
  td:first-child, th:first-child { text-align: left; }
  .radius-header { background: #e8e8e8; font-weight: bold; text-align: left;
                    padding: 6px 8px; }
  .pctl-table td, .pctl-table th { padding: 6px 12px; }
  .p-low { color: #27ae60; font-weight: bold; }
  .p-mid { color: #f39c12; font-weight: bold; }
  .p-high { color: #c0392b; font-weight: bold; }
  .assessment { margin-top: 12px; padding: 10px; border-radius: 6px; font-weight: bold; }
  .assessment.safe { background: #d5f5e3; color: #1e8449; }
  .assessment.below { background: #d5f5e3; color: #27ae60; }
  .assessment.above { background: #fdebd0; color: #e67e22; }
  .assessment.high { background: #fadbd8; color: #c0392b; }
</style>
</head>
<body>
<h1>Philadelphia Crime Scorer</h1>
<div class="search-box">
  <input type="text" id="address" placeholder="Enter address (e.g. 610 Green Lane)"
         autofocus>
  <button id="btn" onclick="score()">Score</button>
</div>
<div id="map"></div>
<div id="map-hint">Click anywhere on the map to score that location</div>
<div id="status"></div>
<div id="error"></div>
<div id="result"></div>

<script>
const addr = document.getElementById('address');
addr.addEventListener('keydown', e => { if (e.key === 'Enter') score(); });

// Map setup
const map = L.map('map').setView([39.9526, -75.1652], 12);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  attribution: '&copy; OpenStreetMap contributors', maxZoom: 19
}).addTo(map);
let marker = null;
let squareLayer = L.layerGroup().addTo(map);

// My Location button
L.Control.Locate = L.Control.extend({
  onAdd: function() {
    const btn = L.DomUtil.create('div', 'leaflet-bar leaflet-control locate-btn');
    btn.innerHTML = '<a href="#" title="My location" role="button" aria-label="My location">'
      + '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">'
      + '<circle cx="12" cy="12" r="4"/><line x1="12" y1="2" x2="12" y2="6"/>'
      + '<line x1="12" y1="18" x2="12" y2="22"/><line x1="2" y1="12" x2="6" y2="12"/>'
      + '<line x1="18" y1="12" x2="22" y2="12"/></svg></a>';
    L.DomEvent.disableClickPropagation(btn);
    btn.querySelector('a').addEventListener('click', function(e) {
      e.preventDefault();
      map.locate({setView: false, maxZoom: 16});
    });
    return btn;
  }
});
new L.Control.Locate({position: 'topleft'}).addTo(map);

map.on('locationfound', function(e) {
  scoreLatLon(e.latlng.lat, e.latlng.lng, true);
});
map.on('locationerror', function(e) {
  document.getElementById('error').textContent = 'Location error: ' + e.message;
});

map.on('click', function(e) {
  scoreLatLon(e.latlng.lat, e.latlng.lng, true);
});

async function score(pushHistory = true) {
  const address = addr.value.trim();
  if (!address) return;
  const btn = document.getElementById('btn');
  const status = document.getElementById('status');
  const error = document.getElementById('error');
  const result = document.getElementById('result');

  btn.disabled = true;
  status.textContent = 'Geocoding and scoring...';
  error.textContent = '';
  result.style.display = 'none';

  try {
    const resp = await fetch('/score', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({address})
    });
    const data = await resp.json();
    if (data.error) { error.textContent = data.error; return; }
    renderResult(data);
    if (pushHistory) {
      const q = '?q=' + encodeURIComponent(address);
      history.pushState({address}, '', q);
    }
  } catch (e) {
    error.textContent = 'Request failed: ' + e.message;
  } finally {
    btn.disabled = false;
    status.textContent = '';
  }
}

// Score by lat/lon (for map clicks and URL restore)
async function scoreLatLon(lat, lon, pushHistory = false) {
  const btn = document.getElementById('btn');
  const status = document.getElementById('status');
  const error = document.getElementById('error');
  const result = document.getElementById('result');
  btn.disabled = true;
  status.textContent = 'Scoring location...';
  error.textContent = '';
  result.style.display = 'none';
  try {
    const resp = await fetch('/score_latlon', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({lat, lon})
    });
    const data = await resp.json();
    if (data.error) { error.textContent = data.error; return; }
    addr.value = data.matched || `${lat.toFixed(5)}, ${lon.toFixed(5)}`;
    renderResult(data);
    if (pushHistory) {
      const qs = `?lat=${lat.toFixed(6)}&lon=${lon.toFixed(6)}`;
      history.pushState({lat, lon}, '', qs);
    }
  } catch (e) {
    error.textContent = 'Request failed: ' + e.message;
  } finally {
    btn.disabled = false;
    status.textContent = '';
  }
}

// Back/forward navigation
window.addEventListener('popstate', e => {
  if (e.state && e.state.lat != null) {
    scoreLatLon(e.state.lat, e.state.lon, false);
  } else if (e.state && e.state.address) {
    addr.value = e.state.address;
    score(false);
  } else {
    addr.value = '';
    squareLayer.clearLayers();
    if (marker) { map.removeLayer(marker); marker = null; }
    document.getElementById('result').style.display = 'none';
    document.getElementById('error').textContent = '';
  }
});

// Load from URL on initial page load
(function() {
  const params = new URLSearchParams(location.search);
  const lat = params.get('lat');
  const lon = params.get('lon');
  const q = params.get('q');
  if (lat && lon) {
    history.replaceState({lat: +lat, lon: +lon}, '', location.search);
    scoreLatLon(+lat, +lon, false);
  } else if (q) {
    addr.value = q;
    history.replaceState({address: q}, '', location.search);
    score(false);
  }
})();

function pctClass(v) { return v <= 33 ? 'p-low' : v <= 66 ? 'p-mid' : 'p-high'; }

function pctColor(v) {
  if (v <= 25) return {fill: '#d5f5e3', border: '#1e8449'};
  if (v <= 50) return {fill: '#d5f5e3', border: '#27ae60'};
  if (v <= 75) return {fill: '#fdebd0', border: '#e67e22'};
  return {fill: '#fadbd8', border: '#c0392b'};
}

function renderResult(data) {
  document.title = 'Crime Score: ' + data.matched;
  // Update map marker and center
  if (marker) map.removeLayer(marker);
  squareLayer.clearLayers();

  // Draw 3×3 grid squares
  if (data.squares) {
    data.squares.forEach(sq => {
      const c = pctColor(sq.pct);
      L.rectangle(sq.bounds, {
        color: c.border,
        weight: sq.center ? 2 : 1,
        opacity: 0.4,
        fillColor: c.fill,
        fillOpacity: sq.center ? 0.65 : 0.55,
        interactive: true,
      }).bindTooltip(`${sq.pct}%`, {permanent: true, direction: 'center',
        className: 'sq-label'})
        .addTo(squareLayer);
    });
  }

  marker = L.marker([data.lat, data.lon]).addTo(map);
  map.setView([data.lat, data.lon], Math.max(map.getZoom(), 16));

  const result = document.getElementById('result');
  let html = '';
  html += `<div class="matched">${data.matched}</div>`;
  html += `<div class="coords">Lat: ${data.lat.toFixed(6)}, Lon: ${data.lon.toFixed(6)}</div>`;

  // Radius tables
  html += '<div class="section"><h2>Incident Counts &amp; Weighted Scores by Radius and Period</h2><table>';
  const windows = ['0-8mo', '8-16mo', '16-24mo'];
  const buckets = ['Violent', 'Burglary', 'Vehicle', 'Property', 'Other'];
  const radii = ['400 ft', '800 ft', '1/4 mile', '1/2 mile'];
  html += '<tr><th>Period</th><th>Total</th><th>Score</th>';
  buckets.forEach(b => html += `<th>${b}</th>`);
  html += '</tr>';

  radii.forEach(r => {
    html += `<tr><td colspan="${3+buckets.length}" class="radius-header">${r}</td></tr>`;
    windows.forEach(w => {
      const row = data.radii.find(x => x.radius === r && x.window === w);
      if (!row) return;
      html += `<tr><td>${w}</td><td>${row.incidents}</td><td>${row.weighted_score}</td>`;
      buckets.forEach(b => html += `<td>${row[b+'_n']}</td>`);
      html += '</tr>';
    });
  });
  html += '</table></div>';

  // Top crime types
  html += '<div class="section"><h2>Top Crime Types within 1 Block / 200m Square (24 months)</h2><table>';
  html += '<tr><th>Type</th><th>Count</th><th>Score</th></tr>';
  data.top_crimes.forEach(c => {
    html += `<tr><td>${c.type}</td><td>${c.count}</td><td>${c.weighted}</td></tr>`;
  });
  html += '</table></div>';

  // Percentile ranks
  const lenses = ['Gun/Murder', 'All Violent', 'Vehicle+Property'];
  html += '<div class="section"><h2>Citywide Percentile Rank (200m square / 1 block)</h2>';
  html += '<table class="pctl-table"><tr><th></th>';
  windows.forEach(w => html += `<th>${w}</th>`);
  html += '</tr>';
  lenses.forEach(lens => {
    html += `<tr><td><strong>${lens}</strong></td>`;
    windows.forEach(w => {
      const key = lens + '|' + w;
      const v = data.percentiles[key];
      html += `<td class="${pctClass(v)}">${v}%</td>`;
    });
    html += '</tr>';
  });
  html += '</table>';
  html += `<p style="color:#666;margin-top:8px;font-size:13px">Ranked against ${data.n_populated.toLocaleString()} populated city blocks.</p>`;

  // Philly percentile assessment
  const pctV = data.percentiles['All Violent|0-8mo'];
  let aClass, aText;
  if (pctV <= 25) { aClass = 'safe'; aText = 'SAFER than 75% of Philadelphia for violent crime'; }
  else if (pctV <= 50) { aClass = 'below'; aText = 'BELOW average for Philadelphia violent crime'; }
  else if (pctV <= 75) { aClass = 'above'; aText = 'ABOVE average for Philadelphia violent crime'; }
  else { aClass = 'high'; aText = 'HIGH violent crime — top quartile in Philadelphia'; }
  html += `<div class="assessment ${aClass}">${aText}</div>`;
  html += '</div>';

  // Comparison vs National / Somerville / Philly citywide
  if (data.assessment) {
    const a = data.assessment;
    html += '<div class="section"><h2>Comparison vs. Reference Locations</h2>';
    html += '<table class="pctl-table">';
    html += '<tr><th>Reference</th><th>Violent</th><th>Property</th></tr>';
    const refs = ['Somerville (02144)', 'US Average (urban)', 'Philadelphia (citywide)'];
    refs.forEach(ref => {
      const c = a.comparisons[ref];
      if (!c) return;
      const vPct = c.violent_vs;
      const pPct = c.property_vs;
      const fmtPct = v => {
        if (v === null) return '<td>—</td>';
        const cls = v <= -25 ? 'p-low' : v <= 25 ? 'p-mid' : 'p-high';
        const sign = v > 0 ? '+' : '';
        return `<td class="${cls}">${sign}${v}%</td>`;
      };
      html += `<tr><td><strong>${ref}</strong><br><span style="font-size:11px;color:#888">V: ${c.violent_rate}/100k &nbsp; P: ${c.property_rate}/100k</span></td>${fmtPct(vPct)}${fmtPct(pPct)}</tr>`;
    });
    html += '</table>';
    html += `<p style="color:#666;margin-top:8px;font-size:13px">Based on annualized crime density in 200m square vs. reference per-sq-mi rates. Somerville rates from FBI 2024 data; national from FBI UCR 2024.</p>`;
    html += '</div>';
  }

  result.innerHTML = html;
  result.style.display = 'block';
}
</script>
</body>
</html>"""


@app.route("/")
def index():
    return render_template_string(HTML)


@app.route("/score", methods=["POST"])
def score_endpoint():
    body = request.get_json()
    address = body.get("address", "").strip()
    if not address:
        return jsonify({"error": "No address provided"})

    geo = geocode(address)
    if geo is None:
        return jsonify({"error": f"Could not geocode: {address}"})

    results = score_address(geo["lat"], geo["lon"])

    # Flatten percentile keys for JSON
    pctl_flat = {}
    for (lens, window), val in results["percentiles"].items():
        pctl_flat[f"{lens}|{window}"] = val

    return jsonify({
        "matched": geo["matched"],
        "lat": geo["lat"],
        "lon": geo["lon"],
        "radii": results["radii"],
        "top_crimes": results["top_crimes"],
        "percentiles": pctl_flat,
        "n_populated": results["n_populated"],
        "assessment": results.get("assessment"),
        "squares": results.get("squares"),
    })


@app.route("/score_latlon", methods=["POST"])
def score_latlon_endpoint():
    body = request.get_json()
    lat = body.get("lat")
    lon = body.get("lon")
    if lat is None or lon is None:
        return jsonify({"error": "Missing lat/lon"})

    # Reverse geocode via OpenStreetMap Nominatim
    matched = f"{lat:.5f}, {lon:.5f}"
    try:
        r = requests.get(
            "https://nominatim.openstreetmap.org/reverse",
            params={"lat": lat, "lon": lon, "format": "json", "zoom": 17},
            headers={"User-Agent": "PhillyCrimeScorer/1.0"},
            timeout=10,
        )
        r.raise_for_status()
        addr = r.json().get("address", {})
        road = addr.get("road", "")
        hood = addr.get("neighbourhood") or addr.get("suburb") or ""
        if road and hood:
            matched = f"Near {road}, {hood}"
        elif road:
            matched = f"Near {road}, Philadelphia"
    except Exception:
        pass

    results = score_address(lat, lon)

    pctl_flat = {}
    for (lens, window), val in results["percentiles"].items():
        pctl_flat[f"{lens}|{window}"] = val

    return jsonify({
        "matched": matched,
        "lat": lat,
        "lon": lon,
        "radii": results["radii"],
        "top_crimes": results["top_crimes"],
        "percentiles": pctl_flat,
        "n_populated": results["n_populated"],
        "assessment": results.get("assessment"),
        "squares": results.get("squares"),
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
