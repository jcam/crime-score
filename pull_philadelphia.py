#!/usr/bin/env python3
"""
Pull Philadelphia crime incident data from OpenDataPhilly CARTO API.

Usage:
    python3 pull_philadelphia.py [--output-dir DIR] [--force] [--meta]

Outputs: incidents_24mo.parquet in the output directory.
"""

import argparse
import json
import sys
import time
import requests
import pandas as pd
from pathlib import Path

META = {
    "city_name": "Philadelphia, PA",
    "description": "Crime incidents from OpenDataPhilly CARTO API (incidents_part1_part2 table, trailing 24 months)",
    "source_url": "https://phl.carto.com/api/v2/sql",
    "citywide_rates": {
        "violent": 110.5,
        "property": 553.1,
        "violent_rate": 908.7,
        "property_rate": 4547.6,
    },
}

CARTO_URL = "https://phl.carto.com/api/v2/sql"
TABLE = "incidents_part1_part2"

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
    "Burglary": ["Burglary Residential", "Burglary Non-Residential"],
    "Vehicle": ["Motor Vehicle Theft", "Theft from Vehicle"],
    "Property": ["Thefts", "Vandalism/Criminal Mischief", "Arson"],
}


def bucket_for(code):
    for bname, codes in BUCKETS.items():
        if code in codes:
            return bname
    return "Other"


def pull(output_dir, force=False):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    cache = output_dir / "incidents_24mo.parquet"

    if cache.exists() and not force:
        print(f"Using cached data: {cache}")
        print(f"Run with --force to re-download.")
        return cache

    print("Pulling Philadelphia crime data from CARTO (batched)...")
    batch_size = 50000
    offset = 0
    frames = []
    while True:
        q = (
            f"SELECT text_general_code, point_x, point_y, dispatch_date_time "
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
        print(f"  fetched {offset} rows...")
        time.sleep(0.5)

    df = pd.concat(frames, ignore_index=True)
    df["bucket"] = df["text_general_code"].map(bucket_for)
    df.to_parquet(cache)
    print(f"Saved {len(df):,} rows to {cache}")
    return cache


def main():
    parser = argparse.ArgumentParser(description="Pull Philadelphia crime data")
    parser.add_argument("--output-dir", default="output",
                        help="Directory to write incidents_24mo.parquet (default: output)")
    parser.add_argument("--force", action="store_true",
                        help="Re-download even if cached file exists")
    parser.add_argument("--meta", action="store_true",
                        help="Print metadata as JSON and exit")
    args = parser.parse_args()

    if args.meta:
        json.dump(META, sys.stdout)
        sys.exit(0)

    pull(args.output_dir, force=args.force)


if __name__ == "__main__":
    main()
