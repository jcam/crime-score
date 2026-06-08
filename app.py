#!/usr/bin/env python3
"""
Flask web UI for the crime address scorer.
"""

import os
import glob
import json
import secrets
import subprocess
import threading
import time as _time
import hashlib
import hmac
import functools
import numpy as np
import pandas as pd
import requests
from flask import Flask, request, jsonify, render_template_string, session, redirect, url_for
from scipy.spatial import cKDTree
from pyproj import Transformer
from pathlib import Path

DATA_PATH = Path(os.environ.get("DATA_PATH", "output/incidents_24mo.parquet"))
DATA_DIR = DATA_PATH.parent
CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", str(DATA_DIR / "config.json")))
SCRIPT_DIR = Path(__file__).parent
ADMIN_USER = os.environ.get("ADMIN_USER", "")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "")

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

GRID_STEP_M = 61   # 200ft
PCTL_HALF = 100     # half-side of 200m x 200m square

TIME_WINDOWS = [
    ("0-8mo",   0,  8),
    ("8-16mo",  8, 16),
    ("16-24mo", 16, 24),
]

def bucket_for(code):
    for bname, codes in BUCKETS.items():
        if code in codes:
            return bname
    return "Other"


# ── Config management ────────────────────────────────────────────
def load_config():
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_config(config):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(config, indent=2) + "\n")


# ── Rate limiting ────────────────────────────────────────────────
class RateLimiter:
    """In-memory sliding-window rate limiter, keyed by IP address."""

    def __init__(self):
        self._windows = {}  # (ip, bucket) -> list of timestamps
        self._lock = threading.Lock()

    def _cleanup(self, now):
        """Purge entries older than 10 minutes to bound memory."""
        cutoff = now - 600
        stale = [k for k, ts in self._windows.items() if ts and ts[-1] < cutoff]
        for k in stale:
            del self._windows[k]

    def is_allowed(self, ip, bucket, max_requests, window_secs):
        """Return True if the request is within the rate limit."""
        now = _time.time()
        key = (ip, bucket)
        with self._lock:
            if len(self._windows) > 10000:
                self._cleanup(now)

            timestamps = self._windows.get(key, [])
            cutoff = now - window_secs
            # Drop expired entries
            timestamps = [t for t in timestamps if t > cutoff]

            if len(timestamps) >= max_requests:
                self._windows[key] = timestamps
                return False

            timestamps.append(now)
            self._windows[key] = timestamps
            return True


_limiter = RateLimiter()

# Limits: (max_requests, window_seconds)
RATE_LIMITS = {
    "login":  (5, 60),       # 5 attempts per minute — brute force protection
    "score":  (30, 60),      # 30 scores per minute
    "admin":  (20, 60),      # 20 admin API calls per minute
}


def rate_limit(bucket):
    """Decorator that applies a rate limit from RATE_LIMITS."""
    max_req, window = RATE_LIMITS[bucket]
    def decorator(f):
        @functools.wraps(f)
        def wrapped(*args, **kwargs):
            ip = request.headers.get("X-Forwarded-For", request.remote_addr) or "unknown"
            # Take the first IP if X-Forwarded-For has a chain
            ip = ip.split(",")[0].strip()
            if not _limiter.is_allowed(ip, bucket, max_req, window):
                if request.is_json or request.headers.get("Accept", "").startswith("application/json"):
                    return jsonify({"error": "Too many requests. Please try again later."}), 429
                return (
                    "<h2>Too many requests</h2>"
                    "<p>Please wait a minute and try again.</p>"
                    '<p><a href="javascript:history.back()">Go back</a></p>'
                ), 429
            return f(*args, **kwargs)
        return wrapped
    return decorator


# ── Authentication ───────────────────────────────────────────────
def _hash_password(password, salt=None):
    """PBKDF2-SHA256 hash for storing passwords in config."""
    if salt is None:
        salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), iterations=600_000)
    return f"{salt}${dk.hex()}"


def _verify_password(password, stored):
    """Verify a password against a stored PBKDF2 hash (or legacy SHA-256)."""
    if "$" not in stored:
        # Legacy SHA-256 hash — verify and caller should re-hash
        return hmac.compare_digest(hashlib.sha256(password.encode()).hexdigest(), stored)
    salt, dk_hex = stored.split("$", 1)
    candidate = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), iterations=600_000)
    return hmac.compare_digest(candidate.hex(), dk_hex)


def _get_secret_key():
    """Get or generate a persistent secret key for Flask sessions."""
    config = load_config()
    key = config.get("secret_key")
    if not key:
        key = secrets.token_hex(32)
        config["secret_key"] = key
        save_config(config)
    return key


def check_credentials(username, password):
    """Check credentials against config (hashed) or env vars (plaintext)."""
    config = load_config()

    # First check config.json (hashed passwords)
    stored_user = config.get("admin_user")
    stored_hash = config.get("admin_pass_hash")
    if stored_user and stored_hash:
        if not hmac.compare_digest(username, stored_user):
            return False
        if not _verify_password(password, stored_hash):
            return False
        # Upgrade legacy SHA-256 hash to PBKDF2 on successful login
        if "$" not in stored_hash:
            config["admin_pass_hash"] = _hash_password(password)
            save_config(config)
        return True

    # Fall back to env vars
    if ADMIN_USER and ADMIN_PASS:
        return (hmac.compare_digest(username, ADMIN_USER) and
                hmac.compare_digest(password, ADMIN_PASS))

    # No credentials configured — deny access
    return False


def auth_configured():
    """Check if any authentication is configured."""
    config = load_config()
    if config.get("admin_user") and config.get("admin_pass_hash"):
        return True
    if ADMIN_USER and ADMIN_PASS:
        return True
    return False


def login_required(f):
    """Decorator to require login for admin routes."""
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if not auth_configured():
            # No auth configured — redirect to setup
            return redirect(url_for("login", setup="1"))
        if not session.get("logged_in"):
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)
    return decorated


# ── Shared mutable state ────────────────────────────────────────
class State:
    """Holds all loaded data. Reassigned wholesale by load_data()."""
    df = None
    tree = None
    to_utm = None
    utm_epsg = None
    map_center = None
    city_name = "Philadelphia, PA"
    city_short = "Philadelphia"
    n_populated = 0
    latest = None
    grid_score_cache = {}
    grid_neighbors = None
    any_crime = None
    loaded = False
    row_count = 0
    date_min = None
    date_max = None
    citywide_rates = None

S = State()


def load_data():
    """Load (or reload) the parquet data and rebuild all indexes."""
    config = load_config()
    S.city_name = config.get("city_name", os.environ.get("CITY_NAME", "Philadelphia, PA"))
    S.city_short = S.city_name.split(",")[0].strip()
    S.citywide_rates = config.get("citywide_rates", {
        "violent": 110.5, "property": 553.1,
        "violent_rate": 908.7, "property_rate": 4547.6,
    })

    if not DATA_PATH.exists():
        print(f"WARNING: No data file at {DATA_PATH}")
        S.loaded = False
        return

    print("Loading crime data...")
    df = pd.read_parquet(DATA_PATH)

    # Filter to plausible lon/lat range
    med_x, med_y = df["point_x"].median(), df["point_y"].median()
    df = df[
        (df["point_x"] > med_x - 1) & (df["point_x"] < med_x + 1) &
        (df["point_y"] > med_y - 1) & (df["point_y"] < med_y + 1)
    ].copy()
    S.map_center = [float(df["point_y"].median()), float(df["point_x"].median())]

    df["bucket"] = df["text_general_code"].map(bucket_for)
    df["weight"] = df["text_general_code"].map(WEIGHTS).fillna(1)
    df["ts"] = pd.to_datetime(df["dispatch_date_time"])

    # Auto-detect UTM zone
    utm_zone = int((med_x + 180) / 6) + 1
    S.utm_epsg = 32600 + utm_zone if med_y >= 0 else 32700 + utm_zone
    print(f"  UTM zone: {utm_zone}{('N' if med_y >= 0 else 'S')} (EPSG:{S.utm_epsg})")

    S.to_utm = Transformer.from_crs("EPSG:4326", f"EPSG:{S.utm_epsg}", always_xy=True)
    df["utm_x"], df["utm_y"] = S.to_utm.transform(df["point_x"].values, df["point_y"].values)
    S.tree = cKDTree(df[["utm_x", "utm_y"]].values)

    # Pre-build percentile grid
    xs = np.arange(df["utm_x"].min(), df["utm_x"].max(), GRID_STEP_M)
    ys = np.arange(df["utm_y"].min(), df["utm_y"].max(), GRID_STEP_M)
    gxx, gyy = np.meshgrid(xs, ys)
    grid_pts = np.column_stack([gxx.ravel(), gyy.ravel()])
    print(f"  Building spatial index for {len(grid_pts):,} grid points...")
    S.grid_neighbors = S.tree.query_ball_point(grid_pts, PCTL_HALF, p=np.inf)
    S.any_crime = np.array([len(idxs) > 0 for idxs in S.grid_neighbors])
    S.n_populated = int(S.any_crime.sum())

    # Pre-compute grid scores for each lens x time window
    S.latest = df["ts"].max()
    print("  Pre-computing percentile grids...")
    S.grid_score_cache = {}
    for wlabel, start_mo, end_mo in TIME_WINDOWS:
        cutoff_recent = S.latest - pd.DateOffset(months=start_mo)
        cutoff_old = S.latest - pd.DateOffset(months=end_mo)
        time_mask = ((df["ts"] <= cutoff_recent) & (df["ts"] >= cutoff_old)).values

        for lens_name, categories in PERCENTILE_LENSES.items():
            cat_mask = df["text_general_code"].isin(categories).values
            combined = time_mask & cat_mask
            w = np.where(combined, df["weight"].values, 0.0)

            scores = np.array([
                w[idxs].sum() if len(idxs) > 0 else 0.0
                for idxs in S.grid_neighbors
            ])
            city_scores = np.sort(scores[S.any_crime])
            S.grid_score_cache[(lens_name, wlabel)] = city_scores

    S.df = df
    S.row_count = len(df)
    S.date_min = str(df["ts"].min().date())
    S.date_max = str(df["ts"].max().date())
    S.loaded = True
    print(f"  Ready. {len(df):,} incidents, {S.n_populated:,} populated blocks.")


# Initial load
load_data()


# ── Geocoding ─────────────────────────────────────────────────────
def geocode(address):
    city_lower = S.city_short.lower()
    if city_lower not in address.lower():
        address = address.rstrip(",. ") + ", " + S.city_name

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
    ax, ay = S.to_utm.transform(lon, lat)
    results = {"radii": [], "top_crimes": [], "percentiles": {}}

    # Radius tables
    for radius_m, rlabel in [(122, "400 ft"), (244, "800 ft"),
                              (400, "1/4 mile"), (800, "1/2 mile")]:
        idxs = S.tree.query_ball_point([ax, ay], radius_m)
        nearby = S.df.iloc[idxs]

        for wlabel, start_mo, end_mo in TIME_WINDOWS:
            cutoff_recent = S.latest - pd.DateOffset(months=start_mo)
            cutoff_old = S.latest - pd.DateOffset(months=end_mo)
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

    # Top crime types (200m x 200m square, full 24mo)
    idxs_qmi = S.tree.query_ball_point([ax, ay], PCTL_HALF, p=np.inf)
    nearby_qmi = S.df.iloc[idxs_qmi]
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
    this_neighbors = S.tree.query_ball_point([ax, ay], PCTL_HALF, p=np.inf)
    for wlabel, start_mo, end_mo in TIME_WINDOWS:
        cutoff_recent = S.latest - pd.DateOffset(months=start_mo)
        cutoff_old = S.latest - pd.DateOffset(months=end_mo)
        time_mask = ((S.df["ts"] <= cutoff_recent) & (S.df["ts"] >= cutoff_old)).values

        for lens_name, categories in PERCENTILE_LENSES.items():
            cat_mask = S.df["text_general_code"].isin(categories).values
            combined = time_mask & cat_mask
            w = np.where(combined, S.df["weight"].values, 0.0)
            this_score = w[this_neighbors].sum() if len(this_neighbors) > 0 else 0.0

            city_scores = S.grid_score_cache[(lens_name, wlabel)]
            pct = float(np.searchsorted(city_scores, this_score) / len(city_scores) * 100)
            results["percentiles"][(lens_name, wlabel)] = round(pct)

    results["n_populated"] = S.n_populated

    # ── Comparison assessment ────────────────────────────────────
    assess_idxs = S.tree.query_ball_point([ax, ay], PCTL_HALF, p=np.inf)
    assess_nearby = S.df.iloc[assess_idxs]
    cutoff_recent_assess = S.latest - pd.DateOffset(months=0)
    cutoff_old_assess = S.latest - pd.DateOffset(months=8)
    assess_window = assess_nearby[
        (assess_nearby["ts"] <= cutoff_recent_assess) & (assess_nearby["ts"] >= cutoff_old_assess)
    ]
    assess_violent = int((assess_window["bucket"] == "Violent").sum())
    assess_property = int(assess_window["bucket"].isin(["Property", "Burglary", "Vehicle"]).sum())

    # Annualize the 8-month counts
    ann_factor = 12.0 / 8.0
    ann_violent = assess_violent * ann_factor
    ann_property = assess_property * ann_factor

    # Area of 200m x 200m square in sq miles
    area_sq_mi = 0.1243 ** 2

    loc_violent_density = ann_violent / area_sq_mi
    loc_property_density = ann_property / area_sq_mi

    # Reference crimes per sq mi per year
    cw = S.citywide_rates or {}
    refs = {
        "Somerville (02144)": {
            "violent": 45.1, "property": 346.4,
            "violent_rate": 221.4, "property_rate": 1698.8,
        },
        "US Average (urban)": {
            "violent": 50.4, "property": 246.4,
            "violent_rate": 360.0, "property_rate": 1760.0,
        },
        f"{S.city_short} (citywide)": {
            "violent": cw.get("violent", 110.5),
            "property": cw.get("property", 553.1),
            "violent_rate": cw.get("violent_rate", 908.7),
            "property_rate": cw.get("property_rate", 4547.6),
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

    # ── 3x3 grid squares for map overlay ─────────────────────────
    from_utm = Transformer.from_crs(f"EPSG:{S.utm_epsg}", "EPSG:4326", always_xy=True)
    side = PCTL_HALF * 2
    recent_wlabel = TIME_WINDOWS[0][0]
    recent_start, recent_end = TIME_WINDOWS[0][1], TIME_WINDOWS[0][2]
    cutoff_r = S.latest - pd.DateOffset(months=recent_start)
    cutoff_o = S.latest - pd.DateOffset(months=recent_end)
    t_mask = ((S.df["ts"] <= cutoff_r) & (S.df["ts"] >= cutoff_o)).values
    violent_cats = PERCENTILE_LENSES["All Violent"]
    c_mask = S.df["text_general_code"].isin(violent_cats).values
    combined_mask = t_mask & c_mask
    w_all = np.where(combined_mask, S.df["weight"].values, 0.0)
    city_scores_violent = S.grid_score_cache[("All Violent", recent_wlabel)]

    squares = []
    for dx in [-1, 0, 1]:
        for dy in [-1, 0, 1]:
            cx = ax + dx * side
            cy = ay + dy * side
            x0, y0 = cx - PCTL_HALF, cy - PCTL_HALF
            x1, y1 = cx + PCTL_HALF, cy + PCTL_HALF
            lon0, lat0 = from_utm.transform(x0, y0)
            lon1, lat1 = from_utm.transform(x1, y1)
            sq_idxs = S.tree.query_ball_point([cx, cy], PCTL_HALF, p=np.inf)
            sq_score = w_all[sq_idxs].sum() if len(sq_idxs) > 0 else 0.0
            pct = float(np.searchsorted(city_scores_violent, sq_score) / len(city_scores_violent) * 100)
            squares.append({
                "bounds": [[lat0, lon0], [lat1, lon1]],
                "pct": round(pct),
                "center": dx == 0 and dy == 0,
            })

    results["squares"] = squares
    return results


# ── Pull script management ───────────────────────────────────────
pull_status = {}  # script_name -> {status, output, started, finished}
pull_lock = threading.Lock()


def discover_pull_scripts():
    """Find all pull_*.py scripts and read their metadata."""
    scripts = sorted(glob.glob(str(SCRIPT_DIR / "pull_*.py")))
    results = []
    for path in scripts:
        name = Path(path).name
        try:
            proc = subprocess.run(
                ["python3", path, "--meta"],
                capture_output=True, text=True, timeout=10,
            )
            if proc.returncode == 0:
                meta = json.loads(proc.stdout)
            else:
                meta = {"city_name": name, "description": f"(could not read metadata: {proc.stderr.strip()})"}
        except Exception as e:
            meta = {"city_name": name, "description": f"(error: {e})"}
        meta["script"] = name
        results.append(meta)
    return results


def run_pull_script(script_name):
    """Run a pull script in a background thread."""
    script_path = SCRIPT_DIR / script_name
    if not script_path.exists():
        return False

    with pull_lock:
        if script_name in pull_status and pull_status[script_name].get("status") == "running":
            return False  # already running

        pull_status[script_name] = {
            "status": "running",
            "output": "",
            "started": _time.time(),
            "finished": None,
        }

    def _run():
        try:
            proc = subprocess.run(
                ["python3", str(script_path), "--output-dir", str(DATA_DIR), "--force"],
                capture_output=True, text=True, timeout=600,
            )
            with pull_lock:
                pull_status[script_name].update({
                    "status": "complete" if proc.returncode == 0 else "error",
                    "output": proc.stdout + proc.stderr,
                    "finished": _time.time(),
                })
            # On success, update config with the script's metadata
            if proc.returncode == 0:
                try:
                    meta_proc = subprocess.run(
                        ["python3", str(script_path), "--meta"],
                        capture_output=True, text=True, timeout=10,
                    )
                    if meta_proc.returncode == 0:
                        meta = json.loads(meta_proc.stdout)
                        config = load_config()
                        config["city_name"] = meta.get("city_name", config.get("city_name"))
                        if "citywide_rates" in meta:
                            config["citywide_rates"] = meta["citywide_rates"]
                        config["last_pull"] = script_name
                        config["last_pull_time"] = _time.time()
                        save_config(config)
                except Exception:
                    pass
        except Exception as e:
            with pull_lock:
                pull_status[script_name].update({
                    "status": "error",
                    "output": str(e),
                    "finished": _time.time(),
                })

    threading.Thread(target=_run, daemon=True).start()
    return True


# ── Flask app ─────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = _get_secret_key()
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Crime Scorer</title>
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
<h1>Crime Scorer</h1>
<div class="search-box">
  <input type="text" id="address" placeholder="Enter address"
         autofocus>
  <button id="btn" onclick="score()">Score</button>
</div>
<div id="map"></div>
<div id="map-hint">Click anywhere on the map to score that location</div>
<div id="status"></div>
<div id="error"></div>
<div id="result"></div>

<script>
const MAP_CENTER = {{ map_center }};
const CITY_SHORT = '{{ city_short }}';
const addr = document.getElementById('address');
addr.addEventListener('keydown', e => { if (e.key === 'Enter') score(); });

// Map setup
const map = L.map('map').setView(MAP_CENTER, 12);
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
  if (marker) map.removeLayer(marker);
  squareLayer.clearLayers();

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

  const windows = ['0-8mo', '8-16mo', '16-24mo'];
  const buckets = ['Violent', 'Burglary', 'Vehicle', 'Property', 'Other'];
  const radii = ['400 ft', '800 ft', '1/4 mile', '1/2 mile'];

  html += '<div class="section"><h2>Incident Counts &amp; Weighted Scores by Radius and Period</h2><table>';
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

  html += '<div class="section"><h2>Top Crime Types within 1 Block / 200m Square (24 months)</h2><table>';
  html += '<tr><th>Type</th><th>Count</th><th>Score</th></tr>';
  data.top_crimes.forEach(c => {
    html += `<tr><td>${c.type}</td><td>${c.count}</td><td>${c.weighted}</td></tr>`;
  });
  html += '</table></div>';

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

  const pctV = data.percentiles['All Violent|0-8mo'];
  let aClass, aText;
  if (pctV <= 25) { aClass = 'safe'; aText = `SAFER than 75% of ${CITY_SHORT} for violent crime`; }
  else if (pctV <= 50) { aClass = 'below'; aText = `BELOW average for ${CITY_SHORT} violent crime`; }
  else if (pctV <= 75) { aClass = 'above'; aText = `ABOVE average for ${CITY_SHORT} violent crime`; }
  else { aClass = 'high'; aText = `HIGH violent crime — top quartile in ${CITY_SHORT}`; }
  html += `<div class="assessment ${aClass}">${aText}</div>`;
  html += '</div>';

  if (data.assessment) {
    const a = data.assessment;
    html += '<div class="section"><h2>Comparison vs. Reference Locations</h2>';
    html += '<table class="pctl-table">';
    html += '<tr><th>Reference</th><th>Violent</th><th>Property</th></tr>';
    const refs = Object.keys(a.comparisons);
    refs.forEach(ref => {
      const c = a.comparisons[ref];
      if (!c) return;
      const fmtPct = v => {
        if (v === null) return '<td>—</td>';
        const cls = v <= -25 ? 'p-low' : v <= 25 ? 'p-mid' : 'p-high';
        const sign = v > 0 ? '+' : '';
        return `<td class="${cls}">${sign}${v}%</td>`;
      };
      html += `<tr><td><strong>${ref}</strong><br><span style="font-size:11px;color:#888">V: ${c.violent_rate}/100k &nbsp; P: ${c.property_rate}/100k</span></td>${fmtPct(c.violent_vs)}${fmtPct(c.property_vs)}</tr>`;
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


ADMIN_HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Crime Scorer — Admin</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         background: #f5f5f5; color: #333; padding: 20px; max-width: 800px; margin: 0 auto; }
  h1 { margin-bottom: 6px; }
  .subtitle { color: #666; margin-bottom: 24px; font-size: 14px; }
  .subtitle a { color: #4a90d9; }
  .section { background: white; border-radius: 8px; padding: 20px; margin-bottom: 16px;
             box-shadow: 0 1px 3px rgba(0,0,0,0.1); }
  .section h2 { font-size: 16px; margin-bottom: 12px; color: #555; border-bottom: 1px solid #eee;
                 padding-bottom: 8px; }
  label { font-weight: 600; display: block; margin-bottom: 4px; font-size: 14px; }
  input[type=text] { padding: 10px; font-size: 15px; border: 2px solid #ccc; border-radius: 6px;
                     width: 100%; max-width: 400px; }
  input[type=text]:focus { outline: none; border-color: #4a90d9; }
  .hint { font-size: 12px; color: #888; margin-top: 4px; }
  .btn { padding: 10px 20px; font-size: 14px; border: none; border-radius: 6px;
         cursor: pointer; font-weight: 600; }
  .btn-primary { background: #4a90d9; color: white; }
  .btn-primary:hover { background: #357abd; }
  .btn-primary:disabled { background: #aaa; cursor: wait; }
  .btn-success { background: #27ae60; color: white; }
  .btn-success:hover { background: #1e8449; }
  .btn-success:disabled { background: #aaa; cursor: wait; }
  .btn-warning { background: #e67e22; color: white; }
  .btn-warning:hover { background: #d35400; }
  .btn-warning:disabled { background: #aaa; cursor: wait; }
  .form-row { display: flex; gap: 10px; align-items: center; margin-top: 8px; }
  .status-line { font-size: 14px; color: #666; margin: 4px 0; }
  .status-line strong { color: #333; }
  .source-card { border: 1px solid #e0e0e0; border-radius: 6px; padding: 14px;
                 margin-bottom: 10px; }
  .source-card h3 { font-size: 15px; margin-bottom: 4px; }
  .source-card p { font-size: 13px; color: #666; margin-bottom: 10px; }
  .source-card .actions { display: flex; gap: 10px; align-items: center; }
  .pull-output { margin-top: 10px; background: #1a1a1a; color: #ccc; padding: 12px;
                 border-radius: 6px; font-family: monospace; font-size: 12px;
                 white-space: pre-wrap; max-height: 200px; overflow-y: auto; display: none; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px;
           font-weight: 600; margin-left: 8px; }
  .badge-running { background: #fdebd0; color: #e67e22; }
  .badge-complete { background: #d5f5e3; color: #1e8449; }
  .badge-error { background: #fadbd8; color: #c0392b; }
  .toast { position: fixed; bottom: 20px; right: 20px; padding: 12px 20px; border-radius: 8px;
           color: white; font-weight: 600; font-size: 14px; display: none; z-index: 1000;
           box-shadow: 0 2px 8px rgba(0,0,0,0.2); }
  .toast-success { background: #27ae60; }
  .toast-error { background: #c0392b; }
  .no-data { color: #e67e22; font-style: italic; }
</style>
</head>
<body>
<h1>Crime Scorer — Admin</h1>
<p class="subtitle"><a href="/">&larr; Back to scorer</a> &nbsp;|&nbsp; <a href="/logout">Log out</a></p>

<div class="section">
  <h2>Configuration</h2>
  <label for="city-name">City Name</label>
  <div class="form-row">
    <input type="text" id="city-name" value="{{ city_name }}" placeholder="Philadelphia, PA">
    <button class="btn btn-primary" onclick="saveConfig()">Save</button>
  </div>
  <p class="hint">Format: "City, ST" — used for geocoding and UI labels. Changes take effect after reload.</p>
</div>

<div class="section">
  <h2>Data Status</h2>
  {% if loaded %}
  <p class="status-line"><strong>{{ '{:,}'.format(row_count) }}</strong> incidents loaded</p>
  <p class="status-line">Date range: <strong>{{ date_min }}</strong> to <strong>{{ date_max }}</strong></p>
  <p class="status-line">Grid points: <strong>{{ '{:,}'.format(n_populated) }}</strong> populated blocks</p>
  <p class="status-line">Data file: <code>{{ data_path }}</code></p>
  {% else %}
  <p class="no-data">No data loaded. Generate data from a source below, then reload.</p>
  {% endif %}
  <div style="margin-top: 12px;">
    <button class="btn btn-warning" onclick="reloadData()" id="reload-btn">Reload Data</button>
    <span id="reload-status" style="margin-left: 10px; font-size: 13px; color: #666;"></span>
  </div>
  <p class="hint" style="margin-top: 6px;">Reload re-reads the parquet file and rebuilds all indexes (~15 seconds).</p>
</div>

<div class="section">
  <h2>Data Sources</h2>
  <p class="hint" style="margin-bottom: 12px;">Available <code>pull_*.py</code> scripts. Click "Generate" to download fresh data.</p>
  <div id="sources">
  {% for src in sources %}
  <div class="source-card" id="card-{{ src.script }}">
    <h3>{{ src.city_name }}<span id="badge-{{ src.script }}" class="badge" style="display:none"></span></h3>
    <p>{{ src.description }}</p>
    <div class="actions">
      <button class="btn btn-success" id="btn-{{ src.script }}" onclick="runPull('{{ src.script }}')">Generate Data</button>
      <span id="elapsed-{{ src.script }}" style="font-size: 12px; color: #888;"></span>
    </div>
    <div class="pull-output" id="output-{{ src.script }}"></div>
  </div>
  {% endfor %}
  {% if not sources %}
  <p class="no-data">No <code>pull_*.py</code> scripts found in the app directory.</p>
  {% endif %}
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
function showToast(msg, type) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = 'toast toast-' + type;
  t.style.display = 'block';
  setTimeout(() => { t.style.display = 'none'; }, 3000);
}

async function saveConfig() {
  const cityName = document.getElementById('city-name').value.trim();
  if (!cityName) return;
  try {
    const resp = await fetch('/admin/config', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({city_name: cityName})
    });
    const data = await resp.json();
    if (data.ok) showToast('Config saved. Reload data to apply.', 'success');
    else showToast('Error: ' + (data.error || 'unknown'), 'error');
  } catch (e) {
    showToast('Request failed: ' + e.message, 'error');
  }
}

async function reloadData() {
  const btn = document.getElementById('reload-btn');
  const status = document.getElementById('reload-status');
  btn.disabled = true;
  status.textContent = 'Reloading...';
  try {
    const resp = await fetch('/admin/reload', {method: 'POST'});
    const data = await resp.json();
    if (data.ok) {
      showToast('Data reloaded successfully.', 'success');
      status.textContent = '';
      setTimeout(() => location.reload(), 500);
    } else {
      showToast('Reload failed: ' + (data.error || 'unknown'), 'error');
      status.textContent = 'Failed';
    }
  } catch (e) {
    showToast('Request failed: ' + e.message, 'error');
    status.textContent = 'Failed';
  } finally {
    btn.disabled = false;
  }
}

let pollTimers = {};

async function runPull(script) {
  const btn = document.getElementById('btn-' + script);
  const badge = document.getElementById('badge-' + script);
  const output = document.getElementById('output-' + script);
  btn.disabled = true;
  badge.textContent = 'Running...';
  badge.className = 'badge badge-running';
  badge.style.display = 'inline-block';
  output.style.display = 'block';
  output.textContent = 'Starting pull...\\n';

  try {
    const resp = await fetch('/admin/pull', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({script})
    });
    const data = await resp.json();
    if (!data.ok) {
      badge.textContent = 'Error';
      badge.className = 'badge badge-error';
      output.textContent = data.error || 'Failed to start';
      btn.disabled = false;
      return;
    }
    pollStatus(script);
  } catch (e) {
    badge.textContent = 'Error';
    badge.className = 'badge badge-error';
    output.textContent = 'Request failed: ' + e.message;
    btn.disabled = false;
  }
}

function pollStatus(script) {
  if (pollTimers[script]) clearInterval(pollTimers[script]);
  pollTimers[script] = setInterval(async () => {
    try {
      const resp = await fetch('/admin/pull_status?script=' + encodeURIComponent(script));
      const data = await resp.json();
      const badge = document.getElementById('badge-' + script);
      const output = document.getElementById('output-' + script);
      const btn = document.getElementById('btn-' + script);
      const elapsed = document.getElementById('elapsed-' + script);

      if (data.output) output.textContent = data.output;
      output.scrollTop = output.scrollHeight;

      if (data.started && data.status === 'running') {
        const secs = Math.round((Date.now()/1000) - data.started);
        elapsed.textContent = secs + 's elapsed';
      }

      if (data.status === 'complete') {
        badge.textContent = 'Complete';
        badge.className = 'badge badge-complete';
        btn.disabled = false;
        elapsed.textContent = '';
        clearInterval(pollTimers[script]);
        showToast('Data generated. Click Reload Data to apply.', 'success');
        // Update city name from the pull script
        setTimeout(() => location.reload(), 1000);
      } else if (data.status === 'error') {
        badge.textContent = 'Error';
        badge.className = 'badge badge-error';
        btn.disabled = false;
        elapsed.textContent = '';
        clearInterval(pollTimers[script]);
        showToast('Pull failed. Check output for details.', 'error');
      }
    } catch (e) {
      // Ignore polling errors
    }
  }, 2000);
}

// On page load, resume polling for any running pulls
{% for src in sources %}
(async function() {
  try {
    const resp = await fetch('/admin/pull_status?script={{ src.script }}');
    const data = await resp.json();
    if (data.status === 'running') {
      const badge = document.getElementById('badge-{{ src.script }}');
      const output = document.getElementById('output-{{ src.script }}');
      const btn = document.getElementById('btn-{{ src.script }}');
      badge.textContent = 'Running...';
      badge.className = 'badge badge-running';
      badge.style.display = 'inline-block';
      output.style.display = 'block';
      output.textContent = data.output || 'Running...';
      btn.disabled = true;
      pollStatus('{{ src.script }}');
    }
  } catch(e) {}
})();
{% endfor %}
</script>
</body>
</html>"""


LOGIN_HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Crime Scorer — Login</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         background: #f5f5f5; color: #333; display: flex; align-items: center;
         justify-content: center; min-height: 100vh; }
  .login-card { background: white; border-radius: 12px; padding: 32px; width: 100%;
                max-width: 380px; box-shadow: 0 2px 12px rgba(0,0,0,0.1); }
  .login-card h1 { font-size: 22px; margin-bottom: 4px; }
  .login-card .subtitle { color: #888; font-size: 13px; margin-bottom: 24px; }
  label { font-weight: 600; display: block; margin-bottom: 4px; font-size: 14px; }
  input[type=text], input[type=password] {
    padding: 10px 12px; font-size: 15px; border: 2px solid #ddd; border-radius: 6px;
    width: 100%; margin-bottom: 16px; }
  input:focus { outline: none; border-color: #4a90d9; }
  .btn { display: block; width: 100%; padding: 12px; font-size: 15px; font-weight: 600;
         background: #4a90d9; color: white; border: none; border-radius: 6px; cursor: pointer; }
  .btn:hover { background: #357abd; }
  .error { color: #c0392b; font-size: 13px; margin-bottom: 12px; font-weight: 600; }
  .setup-note { background: #fef9e7; border: 1px solid #f9e79f; border-radius: 6px;
                padding: 12px; font-size: 13px; color: #7d6608; margin-bottom: 20px; }
  .setup-note code { background: #f0e68c; padding: 1px 4px; border-radius: 3px; }
</style>
</head>
<body>
<div class="login-card">
  <h1>Crime Scorer</h1>
  <p class="subtitle">Admin login required</p>
  {% if setup %}
  <div class="setup-note">
    No admin credentials configured. Set <code>ADMIN_USER</code> and <code>ADMIN_PASS</code>
    environment variables, or create an account below on first use.
  </div>
  <form method="POST" action="/setup">
    <input type="hidden" name="next" value="{{ next }}">
    <label for="username">Choose username</label>
    <input type="text" id="username" name="username" autocomplete="username" required autofocus>
    <label for="password">Choose password</label>
    <input type="password" id="password" name="password" autocomplete="new-password" required>
    <label for="password2">Confirm password</label>
    <input type="password" id="password2" name="password2" autocomplete="new-password" required>
    {% if error %}<div class="error">{{ error }}</div>{% endif %}
    <button type="submit" class="btn">Create Account &amp; Log In</button>
  </form>
  {% else %}
  <form method="POST" action="/login">
    <input type="hidden" name="next" value="{{ next }}">
    <label for="username">Username</label>
    <input type="text" id="username" name="username" autocomplete="username" required autofocus>
    <label for="password">Password</label>
    <input type="password" id="password" name="password" autocomplete="current-password" required>
    {% if error %}<div class="error">{{ error }}</div>{% endif %}
    <button type="submit" class="btn">Log In</button>
  </form>
  {% endif %}
</div>
</body>
</html>"""


@app.route("/login", methods=["GET", "POST"])
@rate_limit("login")
def login():
    next_url = request.args.get("next", request.form.get("next", "/admin"))
    setup = request.args.get("setup") == "1"

    if request.method == "GET":
        # Already logged in?
        if session.get("logged_in") and auth_configured():
            return redirect(next_url)
        return render_template_string(LOGIN_HTML, next=next_url, setup=setup, error=None)

    # POST — login attempt
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")

    if not username or not password:
        return render_template_string(LOGIN_HTML, next=next_url, setup=False,
                                       error="Username and password are required.")

    if check_credentials(username, password):
        session["logged_in"] = True
        session["username"] = username
        session.permanent = True
        app.permanent_session_lifetime = __import__("datetime").timedelta(days=30)
        return redirect(next_url)

    return render_template_string(LOGIN_HTML, next=next_url, setup=False,
                                   error="Invalid username or password.")


@app.route("/setup", methods=["POST"])
@rate_limit("login")
def setup():
    # Only allow setup if no credentials are configured yet
    if auth_configured():
        return redirect(url_for("login"))

    next_url = request.form.get("next", "/admin")
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    password2 = request.form.get("password2", "")

    if not username or not password:
        return render_template_string(LOGIN_HTML, next=next_url, setup=True,
                                       error="Username and password are required.")
    if len(password) < 4:
        return render_template_string(LOGIN_HTML, next=next_url, setup=True,
                                       error="Password must be at least 4 characters.")
    if password != password2:
        return render_template_string(LOGIN_HTML, next=next_url, setup=True,
                                       error="Passwords do not match.")

    # Save credentials to config
    config = load_config()
    config["admin_user"] = username
    config["admin_pass_hash"] = _hash_password(password)
    save_config(config)

    # Log in immediately
    session["logged_in"] = True
    session["username"] = username
    session.permanent = True
    app.permanent_session_lifetime = __import__("datetime").timedelta(days=30)
    return redirect(next_url)


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")


@app.route("/")
def index():
    if not S.loaded:
        if not auth_configured():
            return redirect(url_for("login", setup="1"))
        if not session.get("logged_in"):
            return redirect(url_for("login", next="/admin"))
        return render_template_string(ADMIN_HTML,
            city_name=S.city_name, loaded=False, row_count=0,
            date_min="", date_max="", n_populated=0,
            data_path=str(DATA_PATH), sources=discover_pull_scripts())
    return render_template_string(HTML, map_center=S.map_center, city_short=S.city_short)


@app.route("/admin")
@login_required
def admin():
    return render_template_string(ADMIN_HTML,
        city_name=S.city_name,
        loaded=S.loaded,
        row_count=S.row_count,
        date_min=S.date_min or "",
        date_max=S.date_max or "",
        n_populated=S.n_populated,
        data_path=str(DATA_PATH),
        sources=discover_pull_scripts())


@app.route("/admin/config", methods=["POST"])
@login_required
@rate_limit("admin")
def admin_config():
    body = request.get_json()
    city_name = body.get("city_name", "").strip()
    if not city_name:
        return jsonify({"ok": False, "error": "City name is required"})

    config = load_config()
    config["city_name"] = city_name
    save_config(config)
    return jsonify({"ok": True})


@app.route("/admin/pull", methods=["POST"])
@login_required
@rate_limit("admin")
def admin_pull():
    body = request.get_json()
    script = body.get("script", "")

    # Validate script name (must be pull_*.py and exist)
    if not script.startswith("pull_") or not script.endswith(".py"):
        return jsonify({"ok": False, "error": "Invalid script name"})
    if not (SCRIPT_DIR / script).exists():
        return jsonify({"ok": False, "error": "Script not found"})

    started = run_pull_script(script)
    if not started:
        return jsonify({"ok": False, "error": "Script is already running"})
    return jsonify({"ok": True})


@app.route("/admin/pull_status")
@login_required
def admin_pull_status():
    script = request.args.get("script", "")
    with pull_lock:
        status = pull_status.get(script, {"status": "idle"})
    return jsonify(status)


@app.route("/admin/reload", methods=["POST"])
@login_required
@rate_limit("admin")
def admin_reload():
    try:
        load_data()
        return jsonify({"ok": True, "rows": S.row_count})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/score", methods=["POST"])
@rate_limit("score")
def score_endpoint():
    if not S.loaded:
        return jsonify({"error": "No data loaded. Go to /admin to generate data."})

    body = request.get_json()
    address = body.get("address", "").strip()
    if not address:
        return jsonify({"error": "No address provided"})

    geo = geocode(address)
    if geo is None:
        return jsonify({"error": f"Could not geocode: {address}"})

    results = score_address(geo["lat"], geo["lon"])

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
@rate_limit("score")
def score_latlon_endpoint():
    if not S.loaded:
        return jsonify({"error": "No data loaded. Go to /admin to generate data."})

    body = request.get_json()
    lat = body.get("lat")
    lon = body.get("lon")
    if lat is None or lon is None:
        return jsonify({"error": "Missing lat/lon"})

    matched = f"{lat:.5f}, {lon:.5f}"
    try:
        r = requests.get(
            "https://nominatim.openstreetmap.org/reverse",
            params={"lat": lat, "lon": lon, "format": "json", "zoom": 17},
            headers={"User-Agent": "CrimeScorer/1.0"},
            timeout=10,
        )
        r.raise_for_status()
        addr = r.json().get("address", {})
        road = addr.get("road", "")
        hood = addr.get("neighbourhood") or addr.get("suburb") or ""
        if road and hood:
            matched = f"Near {road}, {hood}"
        elif road:
            matched = f"Near {road}, {S.city_short}"
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
