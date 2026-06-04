# River REM — manual acceptance verification

Three manual acceptance cases exercise the three centerline / DEM paths the plugin
must handle. Run each in QGIS-LTR 3.40.5 with a valid OpenTopography API key set in
**Raster ▸ River REM ▸ River REM Settings…**. For each: **zoom** the canvas to the
reach, **click** *Generate River REM*, and confirm the **expected** result — a styled
REM layer loads **and** the message bar names the dataset/resolution/engine used.

The result message bar line has the form:
`Used <dataset> @ <res> m[ (DSM)] via <engine>`

---

## Case 1 — Snake River near Grand Teton (US 1 m path)

- **Zoom to:** the Snake River where it meanders below the Teton Range, Wyoming
  (≈ 43.66 N, −110.72 W). Keep the view tight enough to stay under the USGS1m
  250 km² cap (a few km across).
- **Click** *Generate River REM*.
- **Expect:**
  - A styled REM raster loads (inverted light→dark pseudocolor, channel ≈ 0).
  - Message bar reads **`Used USGS1m @ 1 m via <engine>`** (no `(DSM)` — USGS 3DEP is
    bare-earth DTM). `<engine>` is `riverrem` if the wrap path is installed/working,
    else `native`.
  - The centerline comes from OpenStreetMap (Overpass) — the Snake is well mapped.

## Case 2 — A European river (OSM + COP30 DSM path)

- **Zoom to:** a mapped European river outside the US envelope — e.g. the **Loire**
  near Orléans, France (≈ 47.90 N, 1.90 E), or the **Isère** near Grenoble. Keep the
  view under the COP30 450,000 km² cap (trivially satisfied at river scale).
- **Click** *Generate River REM*.
- **Expect:**
  - A styled REM raster loads.
  - Message bar reads **`Used COP30 @ 30 m (DSM) via <engine>`** — outside North
    America the USGS tiers are skipped, COP30 is the finest global option, and the
    **`(DSM)`** caveat appears (Copernicus GLO-30 is a surface model).
  - The centerline comes from OpenStreetMap (Overpass).

## Case 3 — An unmapped reach (GRASS fallback path)

- **Zoom to:** a small river/stream reach with **no OSM `waterway` line** — an
  unmapped headwater or a braided reach Overpass returns empty for. (Pick a remote
  reach; if unsure, temporarily verify Overpass returns nothing for the bbox.)
- **Click** *Generate River REM*.
- **Expect:**
  - Overpass returns no waterway → the plugin falls through to the **DEM-derived
    GRASS** centerline (`grass7:r.watershed` → `r.stream.extract`, longest line).
  - A styled REM raster still loads.
  - Message bar names whichever DEM tier was used (USGS or COP30/SRTMGL3 depending on
    region) `@ <res> m via <engine>`.
  - If both Overpass and GRASS fail, expect a **warning** message bar line
    (`Failed: Could not obtain a river centerline…`) and no layer — that is the
    correct exhausted-fallback behavior, not a crash.

---

### What to capture as evidence (per case)
- Screenshot of the loaded, styled REM layer over the reach.
- The message bar line (dataset / resolution / DSM flag / engine).
- The `rem_outputs/<slug>_<timestamp>/` folder contents (`dem.tif`, `dem_utm.tif`,
  `centerline.shp`, `*_REM.tif`, and `*_hillshade-color.tif` if the riverrem engine
  ran).
