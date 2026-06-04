# River REM — QGIS plugin

One-click **Relative Elevation Model** (detrended DEM) for any river in the current map view.

## What it does

1. Zoom the QGIS map canvas to any river in the world.
2. Click the **Generate River REM** toolbar button.
3. The plugin reads the current canvas extent + CRS, transforms it to an EPSG:4326
   lat/lon bounding box, and downloads the **highest-available DEM** from
   **OpenTopography** for that box:
   - In North America: USGS 3DEP `USGS1m` (≤250 km²) → `USGS10m` (≤25,000 km²) →
     `USGS30m` (≤225,000 km²) on the `usgsdem` endpoint.
   - Elsewhere / on US failure: `COP30` (≤450,000 km²) → `SRTMGL3` (≤4,050,000 km²)
     on the `globaldem` endpoint. (COP30 / SRTMGL3 are **DSM** — surface, not bare
     earth — noted in the result message.)
4. Reprojects the DEM to the local **UTM** zone (bilinear) so the REM math is metric.
5. Derives a river **centerline**: a manually selected line layer (if the settings
   toggle is on) → **OpenStreetMap** via our own Overpass query → a DEM-derived
   **GRASS** hydrology fallback.
6. Builds the **REM** (`REM = DEM − interpolated river surface`) and loads it into
   QGIS, styled with an inverted light→dark pseudocolor ramp (Min −0.5 / Max 12),
   with the RiverREM hillshade-color blend on top when available.

All network + compute work runs in a background `QgsTask`; the QGIS UI never freezes.
The dataset, resolution, DSM flag, and engine actually used are reported in the
message bar.

## Target

- **QGIS-LTR 3.40.5** (macOS, Python 3.9, PyQt5).
- `metadata.txt`: `qgisMinimumVersion=3.40`, `qgisMaximumVersion=3.99`,
  `version=0.1.0`, `experimental=True`, author "Joel Silverman".

## How to enable it

1. Symlink (or copy) the `river_rem/` package into the QGIS plugins folder:
   ```
   ~/Library/Application Support/QGIS/QGIS3/profiles/default/python/plugins/river_rem
   ```
   (A symlink to this repo's `river_rem/` lets you edit in place.)
2. Launch QGIS-LTR → **Plugins ▸ Manage and Install Plugins ▸ Installed** →
   enable **River REM**. (It's flagged experimental, so tick
   *Show also experimental plugins* in Settings if it doesn't appear.)
3. **Raster ▸ River REM ▸ River REM Settings…** → paste your **OpenTopography API
   key**. The key is stored in QGIS `QSettings` only (never in this repo).
4. Zoom to a river, click **Generate River REM** on the toolbar.

## Dependencies — nothing to install

The plugin runs on **only the libraries already bundled** in the QGIS-LTR macOS
Python (numpy, scipy, osgeo.gdal/ogr, `requests`, and QGIS's own Processing/GRASS).
There is **no pip install step.** A 2026-06 smoke test settled the engine question:

- **The REM engine is native** — `rem_engine.native_rem` (scipy `cKDTree` IDW +
  GDAL), which the smoke test proved produces a correct REM against the bundled
  GDAL 3.3.2.
- **Do *not* install the PyPI `riverrem`.** The only `riverrem` on PyPI is a broken
  0.0.1 relic: it won't import against GDAL 3.x (bare `import gdal`), has no
  `centerline_shp` parameter, crashes on sub-1M-pixel DEMs, and its viz breaks on
  QGIS's seaborn 0.10. `rem_engine.make_rem` *does* try a RiverREM wrap path first
  (so a future, working install is used automatically), but it falls straight
  through to native when the import fails — which is the expected behavior here.
- **`osmnx` is bypassed entirely.** The plugin **always** supplies the centerline
  itself from Overpass / GRASS / a manual layer, so osmnx is never imported. This is
  also why the modern (GitHub) RiverREM was declined: it drags in osmnx + a newer
  seaborn that would destabilize QGIS's bundled stack.

### Known limitation

`native_rem` interpolates **all valid pixels in one pass** (no chunking), so a very
large or very fine (1 m) extent can exhaust memory. Keep the view to a river *reach*
for now. A chunked query + a resolution-aware area cap are the first planned
improvement.

## Layout

```
river_rem/
  metadata.txt          plugin manifest
  __init__.py           classFactory(iface) -> RiverRemPlugin
  river_rem_plugin.py   toolbar/menu actions, canvas->bbox, guardrails, launches RemTask
  settings_dialog.py    API-key (masked) + centerline-toggle dialog; QSettings accessors
  dem_selector.py       highest-available OpenTopography DEM selection + download
  centerline.py         Overpass -> GRASS -> manual centerline providers
  rem_engine.py         RiverREM wrap + native scipy KDTree IDW fallback
  rem_task.py           background QgsTask orchestrating the whole pipeline
  styling.py            pseudocolor REM styling + layer loading
  icon.png              toolbar icon
```

Per-run outputs land in `rem_outputs/<slug>_<timestamp>/` under the project root
(raw `dem.tif`, `dem_utm.tif`, `centerline.shp`, `*_REM.tif`, and the riverrem
`*_hillshade-color.tif` when the wrap path ran). These are gitignored.
