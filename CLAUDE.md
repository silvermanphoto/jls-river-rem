# River Elevation Model (REM) — QGIS Plugin

You are the engineer building a **QGIS 3.40 LTR plugin (macOS)** that turns any river view into
a **Relative Elevation Model** with one click. You optimize for: a "click and it just works"
experience, correctness you have actually run and watched, scope discipline (build only the
one-button flow that was asked), honest handling of the OpenTopography API key and rate limits,
and concise answers. When rules tension, data-integrity and the user's explicit ask win over
defaults.

**Concise by default; expand only when the task is genuinely complex or Joel asks for depth.**

## What the plugin does (the one-button flow)

1. Joel zooms the QGIS map canvas to any river in the world.
2. He clicks the plugin's toolbar button.
3. The plugin reads the **current canvas extent + CRS**, transforms it to an EPSG:4326 lat/lon
   bounding box, and downloads the **highest-available DEM** for that box from **OpenTopography**
   (his full-access API key).
4. It generates a **REM (detrended DEM)** for that bounding box via **RiverREM**, which fetches
   the river centerline from OpenStreetMap automatically (DEM-derived fallback when OSM is empty).
5. It loads the REM + its hillshade-blended color visualization into QGIS, styled.

All network + compute work runs in a background `QgsTask` so the QGIS UI never freezes.

## Core concept — what a REM is

A river always slopes downhill, so the longitudinal gradient drowns out local relief and you
can't see channel detail. A **REM (Relative Elevation Model)**, a.k.a. **detrended DEM**,
subtracts an interpolated *river-surface* elevation from the DEM so the channel bed ≈ 0 and every
other pixel reads as **height above the river**. This exposes side channels, terraces, point bars,
scroll bars, and paleochannels.

Method in one line: sample DEM elevations along the river centerline → interpolate those
elevations across the whole extent → `REM = DEM − interpolated_river_surface`.

> Caveat (from the source tutorial): IDW-based REMs are great for **visualization**, **not**
> accurate enough for flood mapping. State this if asked to use it for flood analysis.

## The canonical manual REM workflow (reference)

This is the OpenSourceOptions / "krad" method (article + companion video). RiverREM automates it;
keep it here as the reference and as the basis for the DEM-derived fallback path.

| # | Step | QGIS tool / Processing ID |
|---|------|---------------------------|
| 1 | Load + inspect DEM; pseudocolor to channel range; duplicate → Hillshade overlay to spot channel | Layer Styling |
| 2 | Digitize a single line down the main channel centerline | line vector + editing |
| 3 | Place points along the line at ≈ channel-width spacing | `native:pointsalonglines` (DISTANCE = channel width) |
| 4 | Sample DEM elevation at each point | `qgis:rastersampling` (a.k.a. `native:rastersampling`) |
| 5 | IDW-interpolate river elevations across the DEM extent (pixel ≈ 10× DEM for speed) | `qgis:idwinterpolation` |
| 6 | Resample interpolated surface back to native DEM resolution | `gdal:warpreproject` (Bilinear/Cubic) |
| 7 | `REM = DEM − interpolated_surface` | `native:rastercalculator` or `QgsRasterCalculator` |
| 8 | Visualize: Singleband pseudocolor, light→dark (invert so dark = high), Min ≈ −0.5, Max ≈ 10–12 | Layer Styling |

## Architecture decisions (locked)

- **Engine = wrap RiverREM** (not a hand-rolled chain). RiverREM is OpenTopography's own,
  peer-reviewed package; it gives the proven IDW+KDTree interpolation and the logarithmic color
  ramp for free.
- **Target = QGIS 3.40 LTR.** Modern `native:` Processing IDs and PyQGIS APIs apply.
- **Centerline = OSM-primary + DEM-derived fallback.** RiverREM fetches the OSM centerline via
  osmnx automatically; when OSM returns nothing, derive a centerline from the DEM (GRASS
  hydrology) and pass it to RiverREM via `centerline_shp=`. Keep a manual-draw escape hatch.

## OpenTopography API reference

- **Auth:** API key passed as the query param `API_Key=...` (not a header). Store it in
  `QSettings` (plugin settings dialog) — **never hard-code or commit the key.**
- **Global DEM endpoint:** `https://portal.opentopography.org/API/globaldem`
  params: `demtype`, `south`, `north`, `west`, `east`, `outputFormat` (`GTiff`), `API_Key`.
- **USGS 3DEP (US high-res) endpoint:** `https://portal.opentopography.org/API/usgsdem`
  params: `datasetName` (`USGS1m` | `USGS10m` | `USGS30m`), bbox, `outputFormat`, `API_Key`.
- **Dataset codes / resolution (global):** `COP30` (30 m, best global, preferred), `SRTMGL1`
  (30 m), `AW3D30` (30 m), `NASADEM` (30 m), `SRTMGL3` (90 m), `COP90` (90 m), regional
  (`EU_DTM`, `CA_MRDEM_DSM/DTM`), coarse (`SRTM15Plus`, `GEDI_L3`).
- **"Highest-available" selection logic** (no coverage API exists — implement tiered logic by
  region + bbox area):
  - US bbox → try `USGS1m` (≤ 250 km²) → `USGS10m` (≤ 25,000 km²) → `USGS30m` (≤ 225,000 km²).
  - Else (or US fails) → `COP30` (≤ 450,000 km²) → `SRTMGL3` (≤ 4,050,000 km²).
  - Compute bbox area first; pick the finest dataset whose area cap the bbox respects.
- **Limits:** per-request **area caps** as above (not byte caps); free-key rate limits are
  ~200 calls/24 h (academic) / 50 (non-academic) — a **full-access key raises these**. `USGS1m`
  may be access-gated (OT+/academic). Handle HTTP 401/403/429 + "area too large" gracefully.
- **Output CRS:** returned GeoTIFFs are **EPSG:4326 (geographic)**. REM math needs metric
  distances → **reproject the DEM to the local UTM zone** (`gdal:warpreproject`) before feeding
  RiverREM.

## RiverREM reference

- **Install:** `pip install riverrem` (or conda-forge `riverrem`) **into QGIS's own Python**
  (macOS: the interpreter inside `QGIS.app`). Deps not bundled in QGIS: scipy, osmnx, geopandas,
  shapely, seaborn, cmocean, requests, bottleneck, numexpr (+ GDAL bindings). This is the main
  install-friction point on macOS/Apple Silicon — the plugin provides a guided **dependency
  bootstrap** and checks for RiverREM on startup.
- **API:**
  ```python
  from riverrem.REMMaker import REMMaker
  rem = REMMaker(dem='dem_utm.tif', out_dir=out, centerline_shp=None, workers=1)
  rem.make_rem()                       # -> {dem}_REM.tif
  rem.make_rem_viz(cmap='topo', make_png=False)   # -> {dem}_hillshade-color.tif
  ```
  Key params: `dem` (input path), `centerline_shp` (override — **this is the DEM-fallback hook**),
  `out_dir`, `interp_pts`, `k`, `eps`, `workers`, `chunk_size`.
- **Algorithm:** samples elevations along the (OSM) centerline, IDW via `scipy.spatial.KDTree`
  k-nearest, detrends DEM. Multi-channel rivers are aggregated into one surface (validate braided
  cases; custom `centerline_shp` if wrong).
- **Outputs:** `{dem}_REM.tif` (raw REM), `{dem}_hillshade-color.tif` (RGB hillshade+color blend,
  log-scaled `topo` ramp by default), optional PNG/KMZ, `{dem}_centerline_points.shp`.
- **Threading:** force **`workers=1`** when called inside a `QgsTask` (QGIS already runs the task
  off the main thread; nested pools can deadlock). Internet required for the OSM centerline.
- **License:** GPL-3.0-only — compatible with a GPL QGIS plugin.

## Centerline strategy

1. **OSM (primary):** RiverREM/osmnx fetches `waterway` features for the DEM bbox automatically.
2. **DEM-derived (fallback, when OSM empty):** GRASS hydrology on the DEM →
   `grass7:r.fill.dir` → `grass7:r.watershed` (flow accumulation) → `grass7:r.stream.extract`
   (threshold-based vectorization) → pick main channel (longest) → pass as `centerline_shp`.
   Requires GRASS (bundled with the standard QGIS macOS install). Threshold ≈ 0.25 km² drainage,
   scaled by pixel size.
3. **Manual (escape hatch):** let Joel point at a hand-drawn line layer; pass it as `centerline_shp`.

## QGIS plugin internals

- **Scaffold:** `metadata.txt` (`qgisMinimumVersion=3.40`), `__init__.py` (`classFactory(iface)`),
  main plugin class with `initGui()` (add `QAction` toolbar button) / `unload()`, icon, optional
  settings dialog for the API key. macOS plugin path:
  `~/Library/Application Support/QGIS/QGIS3/profiles/default/python/plugins/`.
- **Canvas → lat/lon bbox:**
  ```python
  canvas = iface.mapCanvas()
  extent = canvas.extent()
  src = canvas.mapSettings().destinationCrs()
  tr = QgsCoordinateTransform(src, QgsCoordinateReferenceSystem("EPSG:4326"),
                              QgsProject.instance().transformContext())
  bbox = tr.transformBoundingBox(extent)   # yMin/yMax = south/north, xMin/xMax = west/east
  ```
- **Background work:** `QgsTask` / `QgsApplication.taskManager().addTask(...)`; do the OpenTopography
  download + RiverREM compute in `run()`, add layers + style in `finished()` (main thread). Report
  `setProgress()`, honor `isCanceled()`.
- **Run Processing:** `import processing; processing.run('alg_id', {params})`.
- **Styling:** apply pseudocolor (`QgsSingleBandPseudoColorRenderer` + `QgsColorRampShader`) or
  load RiverREM's `hillshade-color.tif` directly; `QgsHillshadeRenderer` for a hillshade.

## Invariants & constraints

- **Never commit or hard-code the OpenTopography API key.** Store in `QSettings`; gitignore any
  local config that contains it.
- REM ≠ flood map — surface the IDW accuracy caveat if asked for flood use.
- Reproject DEM to metric (UTM) before REM math; OpenTopography returns EPSG:4326.
- Keep descriptive output filenames; write outputs to a project/working dir, not `/tmp` silently.

## Project hygiene

- **Scope:** build only the one-button flow (+ settings for the key, + the documented fallbacks).
  No speculative features, extra abstractions, or unrequested configurability.
- **Git/GitHub:** this folder should become a git repo synced to a **private** repo under
  `silvermanphoto` (use the `github-sync` skill). Commit at checkpoints; push after each commit.
  `.gitignore`: the API key/config, downloaded DEMs/REM rasters (large, regenerable), `.DS_Store`,
  Python caches.
- **Versioning:** bump `version=` in `metadata.txt` on every behavior/UX change (patch bumps are
  the common case); mention the bump in the commit message.
- **Verification loop:** after each change, load the plugin in QGIS 3.40, click the button over a
  real river (e.g. Snake River near Grand Teton), and confirm a styled REM appears — capture
  evidence, don't just assert success.

## Next step

Plan the plugin code against this document: scaffold, settings/key dialog, highest-available-DEM
selector, download `QgsTask`, RiverREM invocation with OSM→DEM-fallback centerline logic, layer
loading + styling, and the dependency bootstrap.

## Sources

- OpenSourceOptions — "Creating REMs and Detrended DEMs in QGIS"
  (https://opensourceoptions.com/creating-rems-and-detrended-dems-in-qgis/) + companion video
  "Use a REM to Analyze Rivers in QGIS" (https://youtu.be/N4UwV81tdI0).
- OpenTopography API — https://opentopography.org/developers ,
  https://portal.opentopography.org/apidocs/
- RiverREM — https://opentopography.github.io/RiverREM/ ,
  https://github.com/OpenTopography/RiverREM
- QGIS PyQGIS Developer Cookbook — https://docs.qgis.org/3.40/en/docs/pyqgis_developer_cookbook/
