# Crime Scorer

A toolkit for analyzing crime patterns at the block level. Feed it incident data as a parquet file and get interactive scoring via a web app, CLI tool, and static heatmaps.

Built to help with house-hunting — enter any address (or click the map) and get a detailed crime breakdown with percentile rankings against the rest of the city, plus comparisons to national averages and a reference city.

The scorer and web app work with any city's data. See [AGENTS.md](AGENTS.md) for instructions on generating a parquet file for a new location.

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

Philadelphia-specific data pipeline. Pulls from the CARTO API and generates:

- Interactive Folium map with toggleable KDE overlays and point heatmaps
- Static multi-panel KDE PNG
- Police district ranking CSV and chart
- Boundary overlays (police districts, zip codes, neighborhoods)

```
python3 build_heatmap.py
```

Takes ~5 minutes to pull and process ~300k incidents. Results go to `output/`.

For other cities, write a comparable script that produces a parquet file in the expected format. See [AGENTS.md](AGENTS.md).

## Quick Start

```bash
# Option 1: Use the Philadelphia data pipeline
python3 build_heatmap.py

# Option 2: Bring your own parquet (see AGENTS.md for format)
cp /path/to/your/incidents.parquet output/incidents_24mo.parquet

# Run the web app directly
pip install flask pandas numpy scipy pyproj shapely requests pyarrow
CITY_NAME="Philadelphia, PA" python3 app.py

# Or build and run with Docker
docker build -t crime-scorer .
docker run -d -p 5050:5000 \
  -e CITY_NAME="Philadelphia, PA" \
  crime-scorer
```

Open http://localhost:5050. Startup takes ~15 seconds to load data and pre-compute the percentile grid (~210k points).

## Configuration

Environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `DATA_PATH` | `output/incidents_24mo.parquet` | Path to the parquet data file |
| `CITY_NAME` | `Philadelphia, PA` | City/state appended to address searches and used in UI labels |

The UTM projection zone, bounding box, and map center are all auto-detected from the data.

## Dependencies

- Python 3.10+
- flask, pandas, numpy, scipy, pyproj, shapely, requests, pyarrow
- For heatmap builder: matplotlib, folium

```bash
pip install flask pandas numpy scipy pyproj shapely requests pyarrow matplotlib folium
```

## How Scoring Works

- **Radius tables**: Raw incident counts within 400ft, 800ft, 1/4 mile, 1/2 mile circles across three non-overlapping 8-month periods
- **Percentile ranking**: A 200m square (Chebyshev distance) is scored using severity-weighted crime counts, then ranked against ~210k grid points (200ft spacing) across the city. Only populated blocks (those with any crime in 24 months) are included — rivers and parks are excluded.
- **Severity weights**: Homicide (100), Aggravated Assault Firearm (60), Robbery Firearm (50), down to Vandalism (3). See the `WEIGHTS` dict in `app.py` for the full table.
- **Reference comparison**: Annualized crime density in a 200m square compared to per-square-mile rates for Somerville MA, national urban average, and the dataset's city (FBI UCR 2024 data)

## Data Sources

All public, no API keys required:

- **Crime incidents**: [OpenDataPhilly CARTO API](https://phl.carto.com/api/v2/sql) (for Philadelphia; other cities need their own source)
- **Geocoding**: [Census Bureau Geocoder](https://geocoding.geo.census.gov)
- **Reverse geocoding**: [OpenStreetMap Nominatim](https://nominatim.openstreetmap.org)
- **Reference crime rates**: FBI UCR 2024 data
