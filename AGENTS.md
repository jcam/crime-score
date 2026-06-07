# AGENTS.md — Generating a Parquet File for a New City

This document explains how to create an incident data file that the Crime Scorer web app and CLI tool can consume. The scorer is city-agnostic — it auto-detects the geographic extent, UTM zone, and map center from whatever data you give it. You just need a parquet file with the right columns.

## Required Parquet Schema

The file must be a Parquet file with at minimum these four columns:

| Column | Type | Description |
|--------|------|-------------|
| `text_general_code` | string | Crime type classification. Must match the strings in the `WEIGHTS` dict (see below). |
| `point_x` | float | Longitude (WGS84, e.g. `-75.1652`) |
| `point_y` | float | Latitude (WGS84, e.g. `39.9526`) |
| `dispatch_date_time` | string or datetime | Timestamp of the incident (e.g. `2025-03-15T14:30:00`) |

Additional columns are ignored. The file should contain roughly 24 months of data for the time-window analysis to work correctly.

**Default path**: `output/incidents_24mo.parquet` (override with `DATA_PATH` env var).

## Crime Type Mapping

The scorer uses severity weights and category buckets keyed by `text_general_code`. Your data pipeline must map the source city's crime classification codes to these exact strings:

### Severity Weights

```
Homicide - Criminal                      100
Aggravated Assault Firearm                60
Robbery Firearm                           50
Rape                                      50
Aggravated Assault No Firearm             30
Robbery No Firearm                        25
Arson                                     25
Burglary Residential                      25
Other Sex Offenses (Not Commercialized)   20
Motor Vehicle Theft                       15
Weapon Violations                         15
Offenses Against Family and Children      10
Other Assaults                            10
Burglary Non-Residential                  10
Theft from Vehicle                         8
Thefts                                     5
Vandalism/Criminal Mischief                3
```

Any `text_general_code` value not in this list gets a default weight of 1 and falls into the "Other" bucket.

### Category Buckets

These group crime types for the percentile lenses and radius table breakdowns:

- **Violent**: Homicide - Criminal, Aggravated Assault Firearm, Aggravated Assault No Firearm, Other Assaults, Robbery Firearm, Robbery No Firearm, Rape, Other Sex Offenses (Not Commercialized), Weapon Violations, Offenses Against Family and Children
- **Burglary**: Burglary Residential, Burglary Non-Residential
- **Vehicle**: Motor Vehicle Theft, Theft from Vehicle
- **Property**: Thefts, Vandalism/Criminal Mischief, Arson

### Percentile Lenses

The citywide percentile ranking uses three lenses, each a subset of the categories above:

- **Gun/Murder**: Homicide - Criminal, Aggravated Assault Firearm, Robbery Firearm
- **All Violent**: All 10 types from the Violent bucket
- **Vehicle+Property**: All 7 types from Vehicle + Property + Burglary buckets

## Writing a Data Pipeline for a New City

The general approach:

1. **Find the city's open data portal.** Most US cities publish crime incident data through Socrata (data.cityof*.gov), CARTO, or ArcGIS Open Data. Look for a dataset with individual incidents (not aggregates), geographic coordinates, crime type classification, and timestamps.

2. **Pull the data.** Query for the last 24 months of incidents. Paginate if the API has row limits. Example sources:
   - Philadelphia: `https://phl.carto.com/api/v2/sql` — table `incidents_part1_part2`
   - Chicago: `https://data.cityofchicago.org/resource/ijzp-q8t2.json` (Socrata)
   - Los Angeles: `https://data.lacity.org/resource/2nrs-mtv8.json` (Socrata)
   - New York: `https://data.cityofnewyork.us/resource/5uac-w243.json` (Socrata)
   - Seattle: `https://data.seattle.gov/resource/tazs-3rd5.json` (Socrata)

3. **Map crime types.** This is the most important step. Each city uses its own classification. You need to map their codes to the `text_general_code` strings listed above. Study the source categories and build a mapping dict. For example, Chicago uses `PRIMARY_TYPE` values like `"HOMICIDE"`, `"BATTERY"`, `"ROBBERY"` — map those to the corresponding strings. Unmapped types will get weight 1 and bucket "Other", so focus on getting the violent and property crimes right.

4. **Extract coordinates.** Some datasets provide lat/lon directly; others use a nested `location` object or provide them as `longitude`/`latitude` columns. Ensure you output `point_x` (longitude) and `point_y` (latitude) as floats. Drop rows with null coordinates.

5. **Save as parquet.** Use pandas:
   ```python
   df.to_parquet("output/incidents_24mo.parquet")
   ```

### Pull Script Convention

Each data pipeline should be a file named `pull_<city>.py` supporting these flags:

- `--meta` — Print a JSON metadata object to stdout and exit. The admin page calls this to discover available sources.
- `--output-dir DIR` — Write `incidents_24mo.parquet` to this directory (default: `output`)
- `--force` — Re-download even if a cached parquet exists

The `META` dict must include:

```python
META = {
    "city_name": "Chicago, IL",           # Used for geocoding and UI labels
    "description": "Human-readable source description",
    "source_url": "https://...",          # Optional, for documentation
    "citywide_rates": {                   # FBI UCR data for this city
        "violent": 110.5,                 # violent crimes per sq mi per year
        "property": 553.1,               # property crimes per sq mi per year
        "violent_rate": 908.7,            # violent crimes per 100k pop
        "property_rate": 4547.6,          # property crimes per 100k pop
    },
}
```

When the admin page runs a pull script, it reads the META and updates `config.json` with the city name and rates. See `pull_philadelphia.py` for a working example.

### Example: Minimal Pipeline Structure

```python
#!/usr/bin/env python3
"""Pull crime data for <City> and save as parquet."""

import argparse
import json
import sys
import requests
import pandas as pd
from pathlib import Path

META = {
    "city_name": "Chicago, IL",
    "description": "Crime incidents from Chicago Data Portal (Socrata)",
    "source_url": "https://data.cityofchicago.org/resource/ijzp-q8t2.json",
    "citywide_rates": {
        "violent": 95.0, "property": 420.0,
        "violent_rate": 780.0, "property_rate": 3450.0,
    },
}

# City-specific crime type mapping
TYPE_MAP = {
    # Source classification -> text_general_code
    "HOMICIDE":               "Homicide - Criminal",
    "AGG BATTERY FIREARM":    "Aggravated Assault Firearm",
    "ROBBERY - ARMED":        "Robbery Firearm",
    "CRIMINAL SEXUAL ASSAULT":"Rape",
    "AGG BATTERY":            "Aggravated Assault No Firearm",
    "ROBBERY":                "Robbery No Firearm",
    "ARSON":                  "Arson",
    "BURGLARY - RESIDENTIAL": "Burglary Residential",
    "MOTOR VEHICLE THEFT":    "Motor Vehicle Theft",
    "THEFT FROM VEHICLE":     "Theft from Vehicle",
    "THEFT":                  "Thefts",
    "CRIMINAL DAMAGE":        "Vandalism/Criminal Mischief",
    "BATTERY":                "Other Assaults",
    "WEAPONS VIOLATION":      "Weapon Violations",
    "BURGLARY":               "Burglary Non-Residential",
    "SEX OFFENSE":            "Other Sex Offenses (Not Commercialized)",
    # ... add more as needed
}


def pull(output_dir, force=False):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    cache = output_dir / "incidents_24mo.parquet"

    if cache.exists() and not force:
        print(f"Using cached data: {cache}")
        return cache

    # Paginate through the API
    frames = []
    offset = 0
    batch = 50000
    while True:
        resp = requests.get(
            "https://data.example.gov/resource/xxxx-xxxx.json",
            params={
                "$where": "date > '2024-06-01'",
                "$limit": batch,
                "$offset": offset,
                "$order": ":id",
            },
            timeout=120,
        )
        resp.raise_for_status()
        rows = resp.json()
        if not rows:
            break
        frames.append(pd.DataFrame(rows))
        offset += batch

    df = pd.concat(frames, ignore_index=True)

    # Map to expected schema
    df["text_general_code"] = df["source_crime_type"].map(TYPE_MAP).fillna(df["source_crime_type"])
    df["point_x"] = df["longitude"].astype(float)
    df["point_y"] = df["latitude"].astype(float)
    df["dispatch_date_time"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%dT%H:%M:%S")

    # Drop rows with missing coordinates
    df = df.dropna(subset=["point_x", "point_y"])

    df[["text_general_code", "point_x", "point_y", "dispatch_date_time"]].to_parquet(cache)
    print(f"Saved {len(df)} rows to {cache}")
    return cache


def main():
    parser = argparse.ArgumentParser(description="Pull Chicago crime data")
    parser.add_argument("--output-dir", default="output")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--meta", action="store_true")
    args = parser.parse_args()

    if args.meta:
        json.dump(META, sys.stdout)
        sys.exit(0)

    pull(args.output_dir, force=args.force)


if __name__ == "__main__":
    main()
```

## Running the Scorer with a New City

Once you have the parquet file and pull script:

```bash
# CLI
CITY_NAME="Chicago, IL" python3 score_address.py "233 S Wacker Dr"

# Web app (go to /admin to set city name)
python3 pull_chicago.py
python3 app.py

# Docker Compose (data persists on a named volume)
docker compose up -d --build
# Then go to http://localhost:5050/admin to generate data and set city name
```

The city name is stored in `config.json` on the data volume and can be changed from the `/admin` page. The `CITY_NAME` env var is still supported as a fallback for the CLI scorer.

If your parquet file is at a non-default location:

```bash
DATA_PATH=/path/to/my/data.parquet python3 app.py
```

## Reference Crime Rates

The comparison section in the scorer uses hardcoded reference rates (crimes per square mile per year) for three benchmarks:

- **Somerville MA (02144)**: A walkable, moderate-crime reference city
- **US Average (urban)**: FBI UCR 2024 national urban average
- **Citywide**: The dataset's own city (currently uses Philadelphia rates)

For the web app, citywide rates are set automatically when you run a pull script (from its `META["citywide_rates"]`) and stored in `config.json`. For the CLI scorer, edit the `refs` dict in `score_address.py` directly. The values come from FBI UCR data:

```python
refs = {
    "Somerville (02144)": {
        "violent": 45.1, "property": 346.4,        # crimes per sq mi
        "violent_rate": 221.4, "property_rate": 1698.8,  # per 100k pop
    },
    "US Average (urban)": {
        "violent": 50.4, "property": 246.4,
        "violent_rate": 360.0, "property_rate": 1760.0,
    },
    f"{CITY_SHORT} (citywide)": {
        "violent": 110.5, "property": 553.1,        # <-- update for your city
        "violent_rate": 908.7, "property_rate": 4547.6,  # <-- update for your city
    },
}
```

To compute the per-square-mile rate from FBI data: `rate_per_100k * (population_density_per_sq_mi / 100000)`.

## Validation

After generating a parquet file for a new city, verify:

1. **Row count**: Should be in the tens to hundreds of thousands for 24 months of a mid-size city.
2. **Coordinate sanity**: `point_x` (lon) and `point_y` (lat) should be in the expected range for the city. The scorer auto-filters to median +/- 1 degree.
3. **Crime type coverage**: Check what fraction of rows map to known `text_general_code` values vs. falling through to "Other":
   ```python
   df = pd.read_parquet("output/incidents_24mo.parquet")
   known = set(WEIGHTS.keys())
   mapped = df["text_general_code"].isin(known).mean()
   print(f"{mapped:.1%} of rows map to known crime types")
   ```
   Aim for >70%. Below that, your type mapping is likely incomplete.
4. **Date range**: Confirm the data spans ~24 months:
   ```python
   ts = pd.to_datetime(df["dispatch_date_time"])
   print(f"Range: {ts.min()} to {ts.max()} ({(ts.max() - ts.min()).days} days)")
   ```
5. **Startup test**: Run the web app and click a few locations on the map. Percentiles should span a reasonable range (not all 0% or all 99%).

## Architecture Notes

- The scorer auto-detects the UTM zone from the data centroid: `utm_zone = int((median_lon + 180) / 6) + 1`. This works for any location on Earth.
- The bounding box is auto-detected as median +/- 1 degree in both axes.
- The map center is the median lat/lon of all incidents.
- The percentile grid uses ~61m (200ft) spacing across the bounding box, generating ~210k points for a city the size of Philadelphia. Larger cities will have more grid points and longer startup times.
- All spatial queries use `scipy.spatial.cKDTree`. Circle queries use Euclidean distance (p=2); square queries use Chebyshev distance (p=infinity).
- The pre-computed percentile grid is built once at startup and kept in memory. For a city like Philadelphia this uses ~2GB of RAM.
