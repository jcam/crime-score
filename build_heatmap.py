#!/usr/bin/env python3
"""
Philadelphia Crime Heatmap — full toolkit.
Pulls 24-month incident data from OpenDataPhilly CARTO API,
builds KDE heatmaps by crime category, and a police-district ranking.
"""

import requests
import json
import numpy as np
import pandas as pd
from scipy.ndimage import gaussian_filter
from scipy.spatial import cKDTree
from pyproj import Transformer
from shapely.geometry import MultiPoint
from shapely.ops import unary_union
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.colors as mcolors
from matplotlib.patches import Patch
import folium
from folium.plugins import HeatMap
from folium.raster_layers import ImageOverlay
from pathlib import Path
import sys
import time

CARTO_URL = "https://phl.carto.com/api/v2/sql"
TABLE = "incidents_part1_part2"
OUT_DIR = Path("output")
OUT_DIR.mkdir(exist_ok=True)

# ── Category buckets ──────────────────────────────────────────────
BUCKETS = {
    "Violent": [
        "Homicide - Criminal",
        "Aggravated Assault Firearm",
        "Aggravated Assault No Firearm",
        "Other Assaults",
        "Robbery Firearm",
        "Robbery No Firearm",
        "Rape",
        "Other Sex Offenses (Not Commercialized)",
        "Weapon Violations",
        "Offenses Against Family and Children",
    ],
    "Burglary": [
        "Burglary Residential",
        "Burglary Non-Residential",
    ],
    "Vehicle": [
        "Motor Vehicle Theft",
        "Theft from Vehicle",
    ],
    "Property": [
        "Thefts",
        "Vandalism/Criminal Mischief",
        "Arson",
    ],
}

ALL_CATEGORIES = [c for cats in BUCKETS.values() for c in cats]

# Severity weights — tuned for "how bad is this for someone living here"
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
    # "Other" bucket categories get weight 1
}

def bucket_for(code):
    for bname, codes in BUCKETS.items():
        if code in codes:
            return bname
    return "Other"


# ── 1. Pull data ─────────────────────────────────────────────────
def pull_data():
    cache = OUT_DIR / "incidents_24mo.parquet"
    if cache.exists():
        print(f"  Using cached data: {cache}")
        return pd.read_parquet(cache)

    print("  Pulling data from CARTO (batched)...")
    batch_size = 50000
    offset = 0
    frames = []
    while True:
        q = (
            f"SELECT text_general_code, point_x, point_y, dc_dist, psa, dispatch_date_time "
            f"FROM {TABLE} "
            f"WHERE dispatch_date_time >= (current_date - interval '24 months') "
            f"AND point_x IS NOT NULL AND point_y IS NOT NULL "
            f"ORDER BY cartodb_id "
            f"LIMIT {batch_size} OFFSET {offset}"
        )
        r = requests.get(CARTO_URL, params={"q": q, "format": "json"}, timeout=120)
        r.raise_for_status()
        rows = r.json()["rows"]
        if not rows:
            break
        frames.append(pd.DataFrame(rows))
        offset += batch_size
        print(f"    fetched {offset} rows...")
        time.sleep(0.5)

    df = pd.concat(frames, ignore_index=True)
    df["bucket"] = df["text_general_code"].map(bucket_for)
    df.to_parquet(cache)
    print(f"  Saved {len(df)} rows to {cache}")
    return df


# ── 2. KDE heatmap ───────────────────────────────────────────────
def build_kde(df, label, cell_m=75, sigma_m=75):
    """Return (grid, extent_lonlat) for a KDE surface with block-level resolution."""
    from shapely import contains_xy

    df = df[
        (df["point_x"] > -76) & (df["point_x"] < -74.9) &
        (df["point_y"] > 39.8) & (df["point_y"] < 40.2)
    ].copy()
    to_utm = Transformer.from_crs("EPSG:4326", "EPSG:32618", always_xy=True)
    to_ll  = Transformer.from_crs("EPSG:32618", "EPSG:4326", always_xy=True)

    x, y = to_utm.transform(df["point_x"].values, df["point_y"].values)

    pad = 500
    xmin, xmax = x.min() - pad, x.max() + pad
    ymin, ymax = y.min() - pad, y.max() + pad
    nx = int((xmax - xmin) / cell_m)
    ny = int((ymax - ymin) / cell_m)

    grid = np.zeros((ny, nx), dtype=np.float32)
    ix = np.clip(((x - xmin) / cell_m).astype(int), 0, nx - 1)
    iy = np.clip(((y - ymin) / cell_m).astype(int), 0, ny - 1)
    w = df["text_general_code"].map(WEIGHTS).fillna(1).values.astype(np.float32)
    np.add.at(grid, (iy, ix), w)

    sigma_cells = sigma_m / cell_m
    grid = gaussian_filter(grid, sigma=sigma_cells)

    # City boundary mask (convex hull with buffer)
    hull = MultiPoint(list(zip(x, y))).convex_hull.buffer(500)
    gx = np.linspace(xmin, xmax, nx)
    gy = np.linspace(ymin, ymax, ny)
    gxx, gyy = np.meshgrid(gx, gy)
    mask = contains_xy(hull, gxx.ravel(), gyy.ravel()).reshape(ny, nx)
    grid[~mask] = np.nan

    # Percentile clip
    valid = grid[np.isfinite(grid)]
    clip_val = np.percentile(valid, 98)
    grid = np.clip(grid, 0, clip_val)

    lon_min, lat_min = to_ll.transform(xmin, ymin)
    lon_max, lat_max = to_ll.transform(xmax, ymax)
    extent = [lon_min, lon_max, lat_min, lat_max]

    return grid, extent


def render_static_map(grids, filename="philly_crime_heatmap.png"):
    """Render a multi-panel static heatmap (one per bucket + All)."""
    panels = list(grids.keys())
    n = len(panels)
    fig, axes = plt.subplots(2, 3, figsize=(20, 14))
    axes = axes.ravel()

    cmap = matplotlib.colormaps["YlOrRd"].copy()
    cmap.set_bad(color="white")

    for i, key in enumerate(panels):
        ax = axes[i]
        grid, extent = grids[key]
        im = ax.imshow(
            grid, origin="lower", extent=extent, cmap=cmap, aspect="auto",
            interpolation="bilinear",
        )
        ax.set_title(f"{key} Crime", fontsize=13, fontweight="bold")
        ax.set_xlabel("Longitude")
        ax.set_ylabel("Latitude")
        fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04, label="Incident density")

    # Hide unused panels
    for j in range(n, len(axes)):
        axes[j].set_visible(False)

    fig.suptitle(
        "Philadelphia Crime Density — Severity-Weighted, Trailing 24 Months\n(98th-pctl clipped, σ=75 m KDE, 75 m cells)",
        fontsize=15, fontweight="bold", y=0.98,
    )
    plt.tight_layout(rect=[0, 0, 1, 0.95])
    out = OUT_DIR / filename
    fig.savefig(out, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  Static map saved: {out}")
    return out


# ── 3. Interactive Folium map ────────────────────────────────────
def kde_to_png(grid, cmap_name, path):
    """Render a KDE grid to a transparent PNG for use as an image overlay."""
    from PIL import Image as PILImage

    cmap = matplotlib.colormaps[cmap_name].copy()
    cmap.set_bad(alpha=0)

    valid = grid[np.isfinite(grid)]
    if len(valid) == 0 or valid.max() == 0:
        norm = mcolors.Normalize(0, 1)
    else:
        norm = mcolors.Normalize(0, valid.max())

    flipped = np.flipud(grid)
    rgba = cmap(norm(flipped))
    alpha = np.where(np.isfinite(flipped), np.clip(norm(flipped) * 1.8, 0.0, 0.75), 0.0)
    rgba[..., 3] = alpha

    img = PILImage.fromarray((rgba * 255).astype(np.uint8), "RGBA")
    img.save(path)


TIME_WINDOWS = [
    ("6mo",  6),
    ("12mo", 12),
    ("24mo", 24),
]

CRIME_BUCKETS = ["All", "Violent", "Burglary", "Vehicle", "Property"]

CMAPS = {
    "All": "YlOrRd",
    "Violent": "OrRd",
    "Burglary": "PuBu",
    "Vehicle": "YlGn",
    "Property": "YlOrBr",
}


def build_folium_map(df, all_grids, time_slices):
    """Build a folium map with time-windowed KDE overlays + point heatmaps."""
    df = df[
        (df["point_x"] > -76) & (df["point_x"] < -74.9) &
        (df["point_y"] > 39.8) & (df["point_y"] < 40.2)
    ].copy()
    df["weight"] = df["text_general_code"].map(WEIGHTS).fillna(1)

    center = [df["point_y"].median(), df["point_x"].median()]
    m = folium.Map(location=center, zoom_start=12, tiles="CartoDB positron")

    # KDE + Points for each time window × crime bucket
    for wlabel, _months in TIME_WINDOWS:
        tdf = time_slices[wlabel]
        for bname in CRIME_BUCKETS:
            grid_key = (wlabel, bname)
            grid, extent = all_grids[grid_key]

            # KDE overlay
            png_path = OUT_DIR / f"kde_{wlabel}_{bname.lower()}.png"
            kde_to_png(grid, CMAPS[bname], png_path)
            bounds = [[extent[2], extent[0]], [extent[3], extent[1]]]
            is_default = (wlabel == "24mo" and bname == "All")
            fg = folium.FeatureGroup(name=f"KDE {wlabel}: {bname}", show=is_default)
            ImageOverlay(
                image=str(png_path), bounds=bounds, opacity=0.85, interactive=False,
            ).add_to(fg)
            fg.add_to(m)

            # Point heatmap
            if bname == "All":
                sub = tdf[tdf["bucket"] != "Other"]
            else:
                sub = tdf[tdf["bucket"] == bname]
            pts = sub[["point_y", "point_x", "weight"]].values.tolist()
            fg = folium.FeatureGroup(name=f"Pts {wlabel}: {bname} ({len(sub):,})", show=False)
            HeatMap(
            pts, radius=25, blur=15, max_zoom=18, min_opacity=0.15,
            gradient={0.0: '#ffffcc', 0.25: '#fecc5c', 0.5: '#fd8d3c',
                      0.75: '#e31a1c', 1.0: '#800026'},
        ).add_to(fg)
            fg.add_to(m)

    # ── Boundary overlays ──────────────────────────────────────────
    boundary_layers = [
        ("Police Districts", OUT_DIR / "police_districts.geojson", "dist_num", "#2166ac", 2.5),
        ("Zip Codes",        OUT_DIR / "zip_codes.geojson",        "code",     "#b2182b", 1.5),
        ("Neighborhoods",    OUT_DIR / "neighborhoods.geojson",     "NAME",     "#1a9850", 1.5),
    ]

    for layer_name, path, label_field, color, weight in boundary_layers:
        if not path.exists():
            continue
        geo = json.load(open(path))
        fg = folium.FeatureGroup(name=layer_name, show=False)
        folium.GeoJson(
            geo,
            style_function=lambda f, c=color, w=weight: {
                "color": c,
                "weight": w,
                "fillOpacity": 0.0,
            },
            tooltip=folium.GeoJsonTooltip(fields=[label_field], aliases=[layer_name + ":"]),
        ).add_to(fg)
        fg.add_to(m)

    folium.LayerControl(collapsed=False).add_to(m)
    out = OUT_DIR / "philly_crime_interactive.html"
    m.save(str(out))
    html = out.read_text()
    html = html.replace(
        'leaflet@1.9.3/dist/leaflet.js"></script>',
        'leaflet@1.9.3/dist/leaflet.js"></script>\n'
        '    <script src="https://cdn.jsdelivr.net/npm/leaflet.heat@0.2.0/dist/leaflet-heat.js"></script>',
    )
    out.write_text(html)
    print(f"  Interactive map saved: {out}")
    return out


# ── 4. Police-district ranking ───────────────────────────────────
def district_ranking(df):
    """Rank police districts by weighted severity score per bucket."""
    df = df.copy()
    df["weight"] = df["text_general_code"].map(WEIGHTS).fillna(1)

    total = df.groupby("dc_dist")["weight"].sum().rename("total")
    pivot = df.groupby(["dc_dist", "bucket"])["weight"].sum().unstack(fill_value=0)
    ranking = pivot.join(total).sort_values("total", ascending=False)

    ranking.index.name = "District"
    ranking = ranking.reset_index()

    out = OUT_DIR / "district_ranking.csv"
    ranking.to_csv(out, index=False)
    print(f"  District ranking saved: {out}")

    # Also make a chart
    fig, ax = plt.subplots(figsize=(12, 6))
    buckets_to_plot = [b for b in ["Violent", "Burglary", "Vehicle", "Property", "Other"] if b in ranking.columns]
    ranking_sorted = ranking.sort_values("total", ascending=True)
    bottom = np.zeros(len(ranking_sorted))
    bucket_colors = {
        "Violent": "#d73027",
        "Burglary": "#4575b4",
        "Vehicle": "#91cf60",
        "Property": "#fc8d59",
        "Other": "#999999",
    }
    for b in buckets_to_plot:
        vals = ranking_sorted[b].values
        ax.barh(ranking_sorted["District"], vals, left=bottom,
                label=b, color=bucket_colors.get(b, "#ccc"))
        bottom += vals

    ax.set_xlabel("Weighted Severity Score (24 months)")
    ax.set_ylabel("Police District")
    ax.set_title("Philadelphia Crime by Police District — Severity-Weighted, Trailing 24 Months")
    ax.legend(loc="lower right")
    plt.tight_layout()
    chart_path = OUT_DIR / "district_ranking_chart.png"
    fig.savefig(chart_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  District chart saved: {chart_path}")

    return ranking


# ── 5. Address scorer (ready for when candidates arrive) ─────────
def score_addresses(df, addresses):
    """
    Score a list of {"label": ..., "lat": ..., "lon": ...} dicts.
    Returns a DataFrame with 400m and 800m radius counts per bucket.
    """
    to_utm = Transformer.from_crs("EPSG:4326", "EPSG:32618", always_xy=True)
    ix, iy = to_utm.transform(df["point_x"].values, df["point_y"].values)
    tree = cKDTree(np.column_stack([ix, iy]))

    df = df.copy()
    df["weight"] = df["text_general_code"].map(WEIGHTS).fillna(1)

    results = []
    for addr in addresses:
        ax, ay = to_utm.transform(addr["lon"], addr["lat"])
        for radius, rlabel in [(400, "400m"), (800, "800m")]:
            idxs = tree.query_ball_point([ax, ay], radius)
            nearby = df.iloc[idxs]
            row = {
                "address": addr["label"], "radius": rlabel,
                "incidents": len(idxs),
                "weighted_total": nearby["weight"].sum(),
            }
            for bname in ["Violent", "Burglary", "Vehicle", "Property", "Other"]:
                mask = nearby["bucket"] == bname
                row[f"{bname}_n"] = mask.sum()
                row[f"{bname}_w"] = nearby.loc[mask, "weight"].sum()
            results.append(row)

    return pd.DataFrame(results)


# ── Main ──────────────────────────────────────────────────────────
def main():
    print("Philadelphia Crime Heatmap — Full Build")
    print("=" * 50)

    print("\n[1/4] Pulling incident data...")
    df = pull_data()
    print(f"  Total rows: {len(df):,}")
    print(f"  Bucket distribution:")
    for b, n in df["bucket"].value_counts().items():
        print(f"    {b}: {n:,}")

    # Parse timestamps and build time slices
    df["ts"] = pd.to_datetime(df["dispatch_date_time"])
    latest = df["ts"].max()
    time_slices = {}
    for wlabel, months in TIME_WINDOWS:
        cutoff = latest - pd.DateOffset(months=months)
        time_slices[wlabel] = df[df["ts"] >= cutoff].copy()
        time_slices[wlabel]["weight"] = time_slices[wlabel]["text_general_code"].map(WEIGHTS).fillna(1)
        print(f"  {wlabel}: {len(time_slices[wlabel]):,} rows (from {cutoff.date()})")

    print("\n[2/4] Building KDE surfaces...")
    all_grids = {}
    for wlabel, _months in TIME_WINDOWS:
        tdf = time_slices[wlabel]
        for bname in CRIME_BUCKETS:
            print(f"  Computing KDE: {wlabel} × {bname}...")
            if bname == "All":
                sub = tdf[tdf["bucket"] != "Other"]
            else:
                sub = tdf[tdf["bucket"] == bname]
            all_grids[(wlabel, bname)] = build_kde(sub, f"{wlabel}_{bname}")

    print("\n[3/4] Rendering maps...")
    # Static map uses the full 24mo grids
    static_grids = {b: all_grids[("24mo", b)] for b in CRIME_BUCKETS}
    render_static_map(static_grids)
    build_folium_map(df, all_grids, time_slices)

    print("\n[4/4] District ranking...")
    ranking = district_ranking(df)
    print("\nTop 5 districts by total incidents:")
    print(ranking.head().to_string(index=False))

    print("\n✓ Done. Outputs in ./output/")
    print("  - philly_crime_heatmap.png  (static multi-panel)")
    print("  - philly_crime_interactive.html  (folium, toggleable layers)")
    print("  - district_ranking.csv")
    print("  - district_ranking_chart.png")


if __name__ == "__main__":
    main()
