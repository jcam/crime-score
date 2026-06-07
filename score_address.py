#!/usr/bin/env python3
"""
Score a Philadelphia address for crime risk.

Usage:
    python3 score_address.py "1500 Market Street, Philadelphia"
    python3 score_address.py "2300 South Broad Street"

Geocodes the address via the Census geocoder, then computes weighted crime
counts in 400m and 800m radii from the cached incident data, broken out
by crime bucket (Violent, Burglary, Vehicle, Property, Other).
"""

import sys
import requests
import numpy as np
import pandas as pd
from scipy.spatial import cKDTree
from pyproj import Transformer
from shapely.geometry import MultiPoint
from pathlib import Path

DATA_PATH = Path(__file__).parent / "output" / "incidents_24mo.parquet"

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

def bucket_for(code):
    for bname, codes in BUCKETS.items():
        if code in codes:
            return bname
    return "Other"


def geocode(address):
    """Geocode via Census Bureau geocoder (no API key needed)."""
    url = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"
    params = {
        "address": address,
        "benchmark": "Public_AR_Current",
        "format": "json",
    }
    r = requests.get(url, params=params, timeout=15)
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


def load_data():
    """Load cached incident data and build spatial index."""
    if not DATA_PATH.exists():
        print(f"ERROR: No cached data at {DATA_PATH}")
        print("Run build_heatmap.py first to pull the incident data.")
        sys.exit(1)

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

    return df, tree, to_utm


def score(df, tree, to_utm, lat, lon):
    """Compute crime stats around a point for multiple radii and time windows."""
    ax, ay = to_utm.transform(lon, lat)
    latest = df["ts"].max()

    results = []
    for radius_m, rlabel in [(122, "400 ft (122m)"), (244, "800 ft (244m)"),
                                (400, "¼ mile (400m)"), (800, "½ mile (800m)")]:
        idxs = tree.query_ball_point([ax, ay], radius_m)
        nearby = df.iloc[idxs]

        for wlabel, months in [("6mo", 6), ("12mo", 12), ("24mo", 24)]:
            cutoff = latest - pd.DateOffset(months=months)
            windowed = nearby[nearby["ts"] >= cutoff]

            row = {
                "radius": rlabel,
                "window": wlabel,
                "incidents": len(windowed),
                "weighted_score": windowed["weight"].sum(),
            }
            for bname in ["Violent", "Burglary", "Vehicle", "Property", "Other"]:
                mask = windowed["bucket"] == bname
                row[f"{bname}_n"] = mask.sum()
                row[f"{bname}_w"] = windowed.loc[mask, "weight"].sum()
            results.append(row)

    return pd.DataFrame(results)


def top_crime_types(df, tree, to_utm, lat, lon, radius=800, top_n=10, square=False):
    """Top crime types by weighted score within radius (circle or square)."""
    ax, ay = to_utm.transform(lon, lat)
    idxs = tree.query_ball_point([ax, ay], radius, p=np.inf if square else 2)
    nearby = df.iloc[idxs].copy()
    grouped = nearby.groupby("text_general_code").agg(
        count=("weight", "size"),
        weighted=("weight", "sum"),
    ).sort_values("weighted", ascending=False).head(top_n)
    return grouped


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


def percentile_rank(df, tree, to_utm, lat, lon, half_side=100, grid_step_m=61):
    """Rank this address against a 200ft city grid across multiple lenses and time windows."""
    ax, ay = to_utm.transform(lon, lat)
    latest = df["ts"].max()

    # Build grid once
    xs = np.arange(df["utm_x"].min(), df["utm_x"].max(), grid_step_m)
    ys = np.arange(df["utm_y"].min(), df["utm_y"].max(), grid_step_m)
    gxx, gyy = np.meshgrid(xs, ys)
    grid_pts = np.column_stack([gxx.ravel(), gyy.ravel()])
    n_grid = len(grid_pts)

    print(f"  Building {n_grid:,} grid points (200ft spacing, {half_side*2}m square)...")

    # Batch query — p=inf gives Chebyshev (square) distance
    all_neighbors = tree.query_ball_point(grid_pts, half_side, p=np.inf)
    this_idxs = tree.query_ball_point([ax, ay], half_side, p=np.inf)

    # Determine populated blocks once (any incident within radius over full 24mo)
    any_crime = np.array([len(idxs) > 0 for idxs in all_neighbors])
    n_populated = any_crime.sum()

    # Score each lens × time window
    results = {}
    time_windows = [
        ("0-8mo",   0,  8),
        ("8-16mo",  8, 16),
        ("16-24mo", 16, 24),
    ]
    for wlabel, start_mo, end_mo in time_windows:
        cutoff_recent = latest - pd.DateOffset(months=start_mo)
        cutoff_old = latest - pd.DateOffset(months=end_mo)
        time_mask = ((df["ts"] <= cutoff_recent) & (df["ts"] >= cutoff_old)).values

        for lens_name, categories in PERCENTILE_LENSES.items():
            cat_mask = df["text_general_code"].isin(categories).values
            combined = time_mask & cat_mask
            w = np.where(combined, df["weight"].values, 0.0)

            # Score every grid point
            grid_scores = np.array([
                w[idxs].sum() if len(idxs) > 0 else 0.0
                for idxs in all_neighbors
            ])
            city_scores = grid_scores[any_crime]

            this_score = w[this_idxs].sum() if len(this_idxs) > 0 else 0.0
            pct = (city_scores < this_score).mean() * 100

            results[(lens_name, wlabel)] = pct

    return results, n_populated


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 score_address.py \"ADDRESS\"")
        print("Example: python3 score_address.py \"1500 Market Street, Philadelphia\"")
        sys.exit(1)

    address = sys.argv[1]
    # Append Philadelphia, PA if not already present
    if "philadelphia" not in address.lower() and "phila" not in address.lower():
        address = address.rstrip(",. ") + ", Philadelphia, PA"
    elif ", pa" not in address.lower() and ", pennsylvania" not in address.lower():
        address = address.rstrip(",. ") + ", PA"
    print(f"Geocoding: {address}")
    geo = geocode(address)
    if geo is None:
        print("ERROR: Could not geocode that address. Try adding ', Philadelphia, PA'")
        sys.exit(1)

    print(f"  Matched: {geo['matched']}")
    print(f"  Lat/Lon: {geo['lat']:.6f}, {geo['lon']:.6f}")

    print("\nLoading crime data...")
    df, tree, to_utm = load_data()
    print(f"  {len(df):,} incidents loaded")

    print("\n" + "=" * 70)
    print(f"  CRIME REPORT: {geo['matched']}")
    print("=" * 70)

    # Summary table
    scores = score(df, tree, to_utm, geo["lat"], geo["lon"])
    print("\n── Incident counts & weighted scores by radius and time window ──\n")

    for rlabel in scores["radius"].unique():
        print(f"  {rlabel}:")
        rsub = scores[scores["radius"] == rlabel]
        print(f"    {'Window':<8} {'Total':>6} {'Score':>7}  "
              f"{'Violent':>8} {'Burglary':>9} {'Vehicle':>8} {'Property':>9} {'Other':>6}")
        print(f"    {'------':<8} {'-----':>6} {'-----':>7}  "
              f"{'-------':>8} {'--------':>9} {'-------':>8} {'--------':>9} {'-----':>6}")
        for _, row in rsub.iterrows():
            print(f"    {row['window']:<8} {int(row['incidents']):>6} {int(row['weighted_score']):>7}  "
                  f"{int(row['Violent_n']):>8} {int(row['Burglary_n']):>9} "
                  f"{int(row['Vehicle_n']):>8} {int(row['Property_n']):>9} {int(row['Other_n']):>6}")
        print()

    # Top crime types
    print("── Top 10 crime types within 1 block / 200m square (24 months, by weighted score) ──\n")
    top = top_crime_types(df, tree, to_utm, geo["lat"], geo["lon"], radius=100, square=True)
    for code, row in top.iterrows():
        print(f"    {code:<45} {int(row['count']):>5} incidents  (score: {int(row['weighted']):>6})")

    # Percentile ranks
    print("\n── Citywide percentile rank (200m square / 1 block, vs populated blocks) ──\n")
    ranks, n_pts = percentile_rank(df, tree, to_utm, geo["lat"], geo["lon"])

    windows = ["0-8mo", "8-16mo", "16-24mo"]
    lenses = list(PERCENTILE_LENSES.keys())
    header = "".join(f"{w:>9}" for w in windows)
    dashes = "".join(f"{'---':>9}" for w in windows)
    print(f"    {'':>20}{header}")
    print(f"    {'':>20}{dashes}")
    for lens in lenses:
        vals = "".join(f"{ranks[(lens, w)]:.0f}%".rjust(9) for w in windows)
        print(f"    {lens:>20}{vals}")

    print(f"\n    Ranked against {n_pts:,} populated city blocks.")

    # Overall assessment based on 24mo All Violent
    pct_v = ranks[("All Violent", "0-8mo")]
    if pct_v <= 25:
        assessment = "SAFER than 75% of the city for violent crime"
    elif pct_v <= 50:
        assessment = "BELOW average violent crime"
    elif pct_v <= 75:
        assessment = "ABOVE average violent crime"
    else:
        assessment = "HIGH violent crime — top quartile"
    print(f"    Assessment: {assessment}")
    print()


if __name__ == "__main__":
    main()
