# Crime Scorer

A toolkit for analyzing crime patterns at the block level. Pulls incident data from an open data portal, caches it as a parquet file, and provides interactive scoring via a web app, CLI tool, and static heatmaps.

Built to help with house-hunting — enter any address (or click the map) and get a detailed crime breakdown with percentile rankings against the rest of the city, plus comparisons to national averages and a reference city.

Currently configured for Philadelphia using the city's CARTO API, but the scorer and web app work with any parquet file containing `text_general_code`, `point_x`, `point_y`, and `dispatch_date_time` columns.

## Components

### Web App (`app.py`)

Flask app with a Leaflet map. Dockerized for easy deployment.

- Enter an address or click anywhere on the map to score a location
- 3x3 grid of 200m squares drawn on the map, color-coded by violent crime percentile
- Incident counts by radius (400ft, 800ft, 1/4 mile, 1/2 mile) across three 8-month periods
- Top crime types within one block
- Citywide percentile ranking across three lenses: Gun/Murder, All Violent, Vehicle+Property
- Comparison vs. national urban average, a reference city, and the dataset's city
- Geolocation button for mobile use
- Browser history support (back/forward, shareable URLs)

### CLI Scorer (`score_address.py`)

Same analysis as the web app, output to terminal.

```
python3 score_address.py "1500 Market Street"
python3 score_address.py "610 Green Lane"
```

Addresses default to the configured city. Geocodes via the Census Bureau (no API key needed).

### Heatmap Builder (`build_heatmap.py`)

Pulls data from CARTO, builds KDE heatmaps, and generates:

- Interactive Folium map with toggleable KDE overlays and point heatmaps by crime category and time window
- Static multi-panel KDE PNG
- Police district ranking CSV and chart
- Boundary overlays (police districts, zip codes, neighborhoods)

```
python3 build_heatmap.py
```

Takes ~5 minutes to pull and process ~300k incidents. Results go to `output/`.

## Docker

```bash
# First, pull the data
python3 build_heatmap.py

# Build and run
docker build -t crime-scorer .
docker run -d -p 5050:5000 crime-scorer
```

Open http://localhost:5050. Startup takes ~15 seconds to load data and pre-compute the percentile grid (~210k points).

## Configuration

Environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `DATA_PATH` | `output/incidents_24mo.parquet` | Path to the parquet data file |
| `CITY_NAME` | `Philadelphia, PA` | City/state appended to address searches |

The UTM projection zone and map center are auto-detected from the data.

## Dependencies

- Python 3.10+
- flask, pandas, numpy, scipy, pyproj, shapely, requests, pyarrow
- For heatmap builder: matplotlib, folium

Install:
```bash
pip install flask pandas numpy scipy pyproj shapely requests pyarrow matplotlib folium
```

## Data Sources

All public, no API keys required:

- **Crime incidents**: [OpenDataPhilly CARTO API](https://phl.carto.com/api/v2/sql) — `incidents_part1_part2` table
- **Geocoding**: [Census Bureau Geocoder](https://geocoding.geo.census.gov)
- **Reverse geocoding**: [OpenStreetMap Nominatim](https://nominatim.openstreetmap.org)
- **Reference crime rates**: FBI UCR 2024 data

## How Scoring Works

- **Radius tables**: Raw incident counts within 400ft, 800ft, 1/4 mile, 1/2 mile circles
- **Percentile ranking**: A 200m square is scored using severity-weighted crime counts, then ranked against all ~210k grid points (200ft spacing) across the city. Only populated blocks (those with any crime in 24 months) are included — rivers and parks are excluded.
- **Severity weights**: Homicide (100), Aggravated Assault Firearm (60), Robbery Firearm (50), down to Vandalism (3)
- **Time windows**: Three non-overlapping 8-month periods (0-8mo, 8-16mo, 16-24mo) for trend analysis
- **Reference comparison**: Annualized crime density in a 200m square compared to per-square-mile rates for reference locations
