# River REM — one-click Relative Elevation Models in QGIS

Zoom the QGIS map to any river on Earth, click one toolbar button, and get a styled
**Relative Elevation Model** of that river — no data hunting, no preprocessing, no
pip installs.

A REM (also called a detrended DEM) re-references every pixel of a digital elevation
model to the local river water surface instead of sea level. The result makes
floodplains, meander scars, oxbows, and centuries of channel migration leap out of
otherwise flat-looking terrain — the technique behind the luminous river maps
popularized by Daniel Coe and the DGGS RiverREM project.

## What the plugin does

1. Reads the current QGIS canvas extent and converts it to a lat/lon bounding box.
2. Downloads the **highest-resolution DEM available** for that area from
   [OpenTopography](https://opentopography.org/):
   - North America: USGS 3DEP 1 m → 10 m → 30 m, chosen by area caps.
   - Elsewhere (or on US failure): Copernicus COP30 → SRTMGL3. (These two are
     surface models rather than bare earth; the result message says so.)
3. Reprojects the DEM to the local UTM zone so the elevation math is metric.
4. Finds the river **centerline** — from a manually selected line layer (optional
   setting), else OpenStreetMap via an Overpass query, else a GRASS hydrology
   fallback derived from the DEM itself.
5. Detrends: `REM = DEM − interpolated river surface`, via inverse-distance
   weighting of elevations sampled along the centerline.
6. Loads the result into QGIS with an inverted light→dark pseudocolor ramp
   (−0.5 to 12 m), blended with a hillshade when available.

All downloading and computation runs in a background `QgsTask`, so the QGIS
interface never freezes. The message bar reports which dataset, resolution, and
engine were actually used.

## Requirements

- **QGIS-LTR 3.40+** (developed on 3.40.5, macOS). The current plugin version is
  whatever `river_rem/metadata.txt` says.
- A free **OpenTopography API key** ([request one here](https://opentopography.org/developers)).
- **Nothing else.** The plugin deliberately uses only libraries already bundled
  with QGIS (numpy, scipy, GDAL, requests, GRASS via Processing). There is no
  pip install step.

## Install

1. Copy (or symlink, to edit in place) the `river_rem/` folder into your QGIS
   plugins directory. On macOS:
   ```
   ~/Library/Application Support/QGIS/QGIS3/profiles/default/python/plugins/river_rem
   ```
2. Launch QGIS → **Plugins ▸ Manage and Install Plugins ▸ Installed** → enable
   **River REM**. (It is flagged experimental — tick *Show also experimental
   plugins* under Settings if it doesn't appear.)
3. Open **Raster ▸ River REM ▸ River REM Settings…** and paste your
   OpenTopography API key. The key is stored only in your local QGIS settings —
   never in this repository.
4. Zoom to a river and click **Generate River REM** on the toolbar.

## Why the REM engine is built in

The only `riverrem` package on PyPI is a broken 0.0.1 relic (it can't import
against modern GDAL and crashes on small DEMs), and the current GitHub RiverREM
drags in dependencies that destabilize QGIS's bundled Python. So this plugin
ships its own native engine — a scipy `cKDTree` IDW interpolation plus GDAL —
verified against the bundled GDAL. If a working RiverREM installation is ever
present, the plugin uses it automatically and falls back to the native engine
otherwise.

## Known limitation

The native engine interpolates the full raster in one pass, so a very large or
very fine (1 m) extent can exhaust memory. Keep the view to a river *reach*
rather than a whole basin. A chunked interpolation and a resolution-aware area
cap are the first planned improvements.

## Repository layout

```
river_rem/
  metadata.txt          plugin manifest (authoritative version number)
  __init__.py           classFactory(iface) -> RiverRemPlugin
  river_rem_plugin.py   toolbar/menu actions, canvas→bbox, guardrails
  settings_dialog.py    API-key (masked) + centerline-toggle dialog
  dem_selector.py       highest-available OpenTopography DEM selection + download
  centerline.py         Overpass → GRASS → manual centerline providers
  rem_engine.py         RiverREM wrap + native scipy KDTree IDW engine
  rem_task.py           background QgsTask orchestrating the pipeline
  styling.py            pseudocolor REM styling + layer loading
```

Per-run outputs land in `rem_outputs/<slug>_<timestamp>/` under the project root
(raw DEM, UTM DEM, centerline shapefile, the REM GeoTIFF). These are gitignored.

## Author

Joel Silverman — [joelsilverman.com](https://joelsilverman.com) — visual
artist working with lidar, elevation data, and cameraless landscape imaging.
