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
- Admin page (`/admin`) for managing data sources, city config, and triggering data reloads
- Login with session management — account setup on first visit or via env vars
- Rate limiting per IP (login 5/min, scoring 30/min, admin API 20/min)

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

### Data Pipelines (`pull_*.py`)

Standalone scripts that pull crime data and save as parquet. Each script supports `--meta` (print JSON metadata) and `--output-dir DIR`.

- `pull_philadelphia.py` — Philadelphia via OpenDataPhilly CARTO API

The admin page auto-discovers all `pull_*.py` scripts and shows a "Generate Data" button for each. See [AGENTS.md](AGENTS.md) for writing new pipelines.

## Quick Start

```bash
# Option 1: Docker Compose (recommended)
python3 pull_philadelphia.py            # generate data
docker compose up -d --build            # build and start
# Open http://localhost:5050
# Go to /admin to manage data and config

# Option 2: Run directly
pip install flask pandas numpy scipy pyproj shapely requests pyarrow
python3 pull_philadelphia.py
python3 app.py
# Open http://localhost:5000

# Option 3: Bring your own parquet (see AGENTS.md for format)
cp /path/to/your/incidents.parquet output/incidents_24mo.parquet
python3 app.py
# Then go to /admin to set the city name
```

Startup takes ~15 seconds to load data and pre-compute the percentile grid (~210k points).

## Configuration

City name and citywide crime rates are stored in a JSON config file, editable from the `/admin` page. When using Docker Compose, the config persists on the `crime-data` volume.

Environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `DATA_PATH` | `output/incidents_24mo.parquet` | Path to the parquet data file |
| `CONFIG_PATH` | `<DATA_DIR>/config.json` | Path to the JSON config file |
| `ADMIN_USER` | *(none)* | Admin username (alternative to first-visit setup) |
| `ADMIN_PASS` | *(none)* | Admin password (alternative to first-visit setup) |

If `ADMIN_USER`/`ADMIN_PASS` are not set, the first visit to `/admin` prompts you to create an account. Credentials are stored hashed in `config.json` on the data volume.

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
