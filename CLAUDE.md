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

- **Engine = NATIVE scipy-KDTree IDW** (`rem_engine.native_rem`). *Updated 2026-06 after a
  smoke test.* We originally planned to **wrap RiverREM**, but the only `riverrem` on PyPI is a
  broken 0.0.1 relic (bare `import gdal`, no `centerline_shp`, crashes < 1M px, viz breaks on
  QGIS's seaborn 0.10), and the working RiverREM lives only on GitHub and drags in osmnx + a
  newer seaborn that would destabilize QGIS's bundled stack. The smoke test confirmed the core
  REM math runs correctly against the bundled GDAL 3.3.2, so the plugin computes the REM itself
  with numpy + `scipy.spatial.cKDTree` IDW + GDAL — **zero external installs**. `make_rem` still
  *tries* a RiverREM wrap path first so a future working install is picked up automatically, then
  falls through to native. **Do not `pip install riverrem`** — it's the broken relic.
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

## Lessons learned (2026-06-19 build session — through v0.1.12)

Hard-won, non-obvious findings. Read before changing the matching code.

- **PyPI `riverrem` is a broken 0.0.1 relic** — bare `import gdal`, no
  `centerline_shp`, crashes < 1M px, viz breaks on seaborn 0.10. Engine is
  **native** (`rem_engine.native_rem`: numpy + scipy `cKDTree` IDW + GDAL). The
  modern RiverREM is GitHub-only and drags in osmnx — declined. Do not
  `pip install riverrem`. (Bundled GDAL 3.3.2 runs the native math fine despite
  RiverREM's declared `gdal>=3.7`.)
- **Overpass 406:** overpass-api.de's Apache blocks the default
  `python-requests` User-Agent → `raise_for_status()` turned it into a swallowed
  "no waterways". Always send `OVERPASS_HEADERS` (a real User-Agent).
- **Centerline CRS:** OSM/Overpass returns EPSG:4326, but `native_rem` samples a
  UTM DEM — an unreprojected line lands off-grid ("no samples on valid pixels").
  `get_centerline` reprojects into the DEM CRS using **traditional GIS axis
  order** (the GDAL 3 lat/lon-swap trap).
- **USGS1m arrives in a projected metre CRS (UTM), not 4326** — so the UTM warp
  must NOT hard-code `SOURCE_CRS=4326`; use the DEM's own embedded CRS.
- **USGS1m is academic-access-gated.** A `.edu` account alone shows "Registered
  User" (Global 10m+) and 401s on USGS1m; the user must click **Enable Access**
  on a USGS 3DEP dataset page (1-yr academic grant, 250 km²/request cap). The
  selector tries 1m first and falls back to 10m; the message bar names what was
  skipped and why.
- **Large 1m downloads truncate silently.** They stream with **no
  Content-Length** and the server can close early with no HTTP error → a partial
  GeoTIFF that renders as only a band of the canvas. `download_dem` validates
  each attempt (Content-Length AND a GDAL read of the last tile) and retries 3×.
  No size header also means no native % — the bar uses a byte-based creep.
- **GRASS 7.8 vector line export is broken** on this build: `r.stream.extract`'s
  `stream_vector` and `r.to.vect type=line` both come back empty/zero-length,
  while the stream **raster** is solid. `grass_centerline` therefore reads the
  stream raster with GDAL and emits channel **points** (no GRASS vector export);
  `native_rem`'s densifier accepts Point geometry.
- **Style panel must target the VISIBLE REM.** With several REM groups stacked
  and no active layer, picking an arbitrary dict-order layer made the panel edit
  a hidden group (sliders looked dead). `find_current_rem` now takes the topmost
  *checked* REM in the layer tree; applied style is saved as layer custom
  properties so the panel reflects the on-screen look.
- **Default "look":** cap the color ramp at ~15 m above the river
  (`DEFAULT_VMAX_M`) and keep hillshade `Z≈1` — letting the ramp run to the full
  data max washes canyons to grey (the source REM look tops out ~10–12 m).
- **QGIS 4.0 = PyQt6.** The plugin won't load there as-is: `qgisMaximumVersion`
  cap + unscoped Qt enums (`Qt.Horizontal`, `QLineEdit.Password`, …) + `QAction`
  moved to QtGui + likely `grass7`→`grass8` IDs. Scoped enums work on PyQt5 too,
  so a single cross-version codebase is feasible — needs QGIS 4 + MCP to verify.
- **QGIS MCP** (`execute_code`) only reaches the GUI app when its server is
  running; the headless QGIS-LTR/QGIS-4 binaries don't bootstrap standalone
  (4.0's hard-codes a CI stdlib path). Offscreen testing uses the QGIS-LTR python
  with `PYTHONPATH` to the bundle's `Resources/python(/plugins)`.

## Git and GitHub sync

This repo is synced to a PRIVATE GitHub repository:
https://github.com/silvermanphoto/jls-river-rem

Remote: `origin` (HTTPS). After every commit, push to keep GitHub in sync.

Rules:
1. ALWAYS push after committing — a local-only commit is incomplete work. If
   Joel forgets, remind him. (This project sat local-only through v0.1.12 — don't
   repeat that; sync early.)
2. Never force-push (`--force`) without Joel's explicit approval.
3. The repo is PRIVATE. Do not change its visibility.
4. Never commit build artifacts, secrets, or logs. The `.gitignore` covers these
   (rem_outputs/, *.tif, caches) — the OpenTopography API key lives ONLY in QGIS
   QSettings, never in the repo. Add new generated/sensitive categories to
   `.gitignore` before committing.
5. Don't commit files that would push the repo past GitHub's size limits — the
   DEM/REM rasters are regenerable and gitignored; the code rebuilds everything.
6. Downloaded rasters are excluded by design; there are no databases here.
