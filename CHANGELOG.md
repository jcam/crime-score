# Changelog

## 2026-06-07

### Added
- Geolocation ("My Location") button on the map for mobile use
- Browser history support — back/forward navigation works, URLs are shareable
- Page title updates to `Crime Score: <location>` for browsable history
- 3x3 grid of 200m squares drawn on the map, color-coded by violent crime percentile
- Comparison vs. reference locations: national urban average, Somerville MA (02144), Philadelphia citywide
- Reverse geocoding for map clicks via OpenStreetMap Nominatim (e.g. "Near Titan Street, Pennsport")
- Map click URLs use `?lat=...&lon=...` so refreshes work correctly
- Address search URLs use `?q=...` for shareable links

### Changed
- Scoring geometry switched from circles to 200m x 200m squares (Chebyshev distance) for percentile ranking, top crimes, and reference comparison — better fit for Philadelphia's grid layout
- Time windows changed from 4 x 6-month to 3 x 8-month non-overlapping periods
- Top crime types, percentile rank, and reference comparison all use consistent 200m square

## 2026-06-06

### Added
- Flask web app (`app.py`) with address input, submit button, and formatted output
- Dockerfile for containerized deployment
- Pre-computed percentile grids at startup for fast scoring (~210k grid points)
- Clickable Leaflet map with OpenStreetMap tiles

## 2026-06-05

### Added
- CLI address scorer (`score_address.py`) with Census Bureau geocoding
- Multiple scoring radii: 400ft, 800ft, 1/4 mile, 1/2 mile
- Top 10 crime types by weighted score
- Citywide percentile ranking with three lenses (Gun/Murder, All Violent, Vehicle+Property)
- Non-overlapping time windows for trend analysis
- Populated-block filtering (excludes rivers/parks from percentile comparison)

## 2026-06-04

### Added
- Initial data pipeline (`build_heatmap.py`) pulling from CARTO API
- KDE heatmap overlays with 75m cells, gaussian smoothing, 98th percentile clipping
- Interactive Folium map with toggleable layers by crime category and time window
- Point-level HeatMap drill-down layers
- Boundary overlays: police districts, zip codes, neighborhoods
- Static multi-panel KDE PNG
- Police district ranking CSV and chart
- Severity weighting system (Homicide=100 down to Vandalism=3)
- Crime category buckets: Violent, Burglary, Vehicle, Property
