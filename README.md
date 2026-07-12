# River REM — one-click Relative Elevation Models in QGIS

**This project is a fork of [OpenTopography/RiverREM](https://github.com/OpenTopography/RiverREM),
modified to run as a QGIS plugin.** All credit for the REM concept, method, and the
original implementation belongs to the OpenTopography RiverREM project.

From the original RiverREM Project Notes:

> RiverREM is a Python package for automatically generating river relative elevation
> model (REM) visualizations from nothing but an input digital elevation model (DEM).
> The package uses the OpenStreetMap API to retrieve river centerline geometries over
> the DEM extent. Interpolation of river elevations is automatically handled using a
> sampling scheme based on raster resolution and river sinuosity to create striking
> high-resolution visualizations without interpolation artifacts straight out of the
> box and without additional manual steps.

This fork wraps that idea in a QGIS toolbar button: zoom the map to any river on
Earth, click once, and get a styled **Relative Elevation Model** — no data hunting,
no preprocessing, no pip installs.

A REM (also called a detrended DEM) re-references every pixel of a digital elevation
model to the local river water surface instead of sea level. The result makes
floodplains, meander scars, oxbows, and centuries of channel migration leap out of
otherwise flat-looking terrain — the technique behind the luminous river maps
popularized by Daniel Coe and the RiverREM project.

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

## How this fork differs from upstream RiverREM

Living inside QGIS imposes one hard constraint the upstream package doesn't
have: the plugin can only use libraries already bundled with QGIS's own Python,
because pip-installing into that environment (osmnx, a newer seaborn, a
different GDAL) destabilizes it. So this fork re-implements the REM
interpolation as a self-contained native engine — a scipy `cKDTree`
inverse-distance interpolation plus the bundled GDAL — and supplies the river
centerline itself (Overpass query → GRASS hydrology → manual layer) rather than
importing osmnx. If a working RiverREM installation is present, the plugin
still uses it automatically and applies its hillshade-color blend; the native
engine is the fallback that makes the plugin work out of the box.

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
