# -*- coding: utf-8 -*-
"""
rem_engine.py — core REM (Relative Elevation Model / detrended DEM) computation.

Two code paths, with automatic fallback:

  1. WRAP path: try to drive OpenTopography's RiverREM package
     (``riverrem.REMMaker``). matplotlib is forced to the headless "Agg"
     backend *before* RiverREM is imported so nothing tries to open a GUI
     window from inside the QGIS background task. ``workers=1`` keeps RiverREM
     from spawning nested process pools (which can deadlock inside a QgsTask).

  2. NATIVE path: a self-contained reimplementation of the REM math using only
     libraries already bundled in QGIS (numpy, scipy, osgeo.gdal). This runs
     automatically whenever the wrap path can't be imported or raises.

The native path is the expected default on this machine: the only ``riverrem``
on PyPI is a broken 0.0.1 relic (bare ``import gdal`` GDAL-2 imports, an
unconditional ``import osmnx``, no ``centerline_shp`` parameter, a sub-1M-pixel
crash, and a viz step that breaks on QGIS's seaborn 0.10). ``make_rem`` still
*tries* the wrap path first so that a future, working RiverREM install is used
automatically if present.

Public API (keep signatures stable — other modules call these):
    utm_epsg_for(lon, lat) -> int
    make_rem(dem_utm_path, centerline_shp, out_dir, progress_cb=None) -> dict
        returns {"rem_tif": str, "viz_tif": str|None, "engine": str}

All heavy work is intended to be called from QgsTask.run(); this module never
touches QgsProject / layers / the GUI.
"""

import os
import math

import numpy as np
from osgeo import gdal, ogr

# Fail loudly at import time on a truly broken GDAL python binding, but let
# scipy be imported lazily inside native_rem so this module still imports for a
# py_compile / smoke check even in an odd environment.
gdal.UseExceptions()


# --------------------------------------------------------------------------- #
# UTM zone helper
# --------------------------------------------------------------------------- #
def utm_epsg_for(lon, lat):
    """Return the EPSG code of the UTM zone containing (lon, lat).

    Northern hemisphere -> 326xx, southern -> 327xx, where xx is the 1..60 UTM
    zone number. Longitude is normalized into [-180, 180) first so antimeridian
    inputs still land in a valid zone (the caller warns on polar/antimeridian
    edge cases; this just never returns a nonsense code).
    """
    # Normalize longitude to [-180, 180).
    lon = ((float(lon) + 180.0) % 360.0) - 180.0
    # UTM zones are 6 deg wide starting at -180; zone 1 = [-180,-174).
    zone = int(math.floor((lon + 180.0) / 6.0)) + 1
    if zone < 1:
        zone = 1
    elif zone > 60:
        zone = 60
    base = 32600 if float(lat) >= 0.0 else 32700
    return base + zone


# --------------------------------------------------------------------------- #
# Public entry point: try RiverREM, fall back to native
# --------------------------------------------------------------------------- #
def make_rem(dem_utm_path, centerline_shp, out_dir, progress_cb=None):
    """Build a REM from a UTM-projected DEM and a centerline shapefile.

    Tries the RiverREM wrap path first; on ImportError or *any* runtime error
    it falls back to the native scipy-KDTree IDW implementation. The native
    path always produces a usable REM as long as numpy/scipy/gdal are present
    (they are bundled in QGIS), so make_rem only raises if the native path
    itself fails (e.g. an unreadable DEM or an empty centerline).

    Args:
        dem_utm_path:   path to the DEM already reprojected to a metric UTM CRS.
        centerline_shp: path to a LineString shapefile (river centerline) in the
                        same CRS as the DEM. Required — we always supply it
                        (osmnx is bypassed upstream).
        out_dir:        directory to write outputs into.
        progress_cb:    optional callable(percent_float_0_100) for progress.

    Returns:
        dict: {"rem_tif": <path>, "viz_tif": <path or None>, "engine": "riverrem"|"native"}
    """
    os.makedirs(out_dir, exist_ok=True)

    def _progress(p):
        if progress_cb is not None:
            try:
                progress_cb(float(p))
            except Exception:
                # Progress reporting must never break the computation.
                pass

    _progress(1.0)

    # ----- Path 1: wrap RiverREM -------------------------------------------- #
    try:
        # Force a headless matplotlib backend BEFORE riverrem (or anything it
        # pulls in) imports pyplot, so no GUI window is attempted from the
        # background task. Wrapped in its own try so a missing matplotlib just
        # routes us to the native path rather than crashing.
        import matplotlib
        matplotlib.use("Agg")

        from riverrem.REMMaker import REMMaker  # may ImportError on broken pkg

        _progress(5.0)

        # The modern RiverREM signature. ``centerline_shp`` bypasses osmnx.
        rem_maker = REMMaker(
            dem=dem_utm_path,
            out_dir=out_dir,
            centerline_shp=centerline_shp,
            workers=1,  # never spawn nested pools inside a QgsTask
        )

        rem_maker.make_rem()
        _progress(70.0)

        # make_png=False -> we only want the GeoTIFF hillshade-color blend;
        # QGIS styles the raw REM itself.
        rem_maker.make_rem_viz(make_png=False)
        _progress(95.0)

        rem_tif = _find_output(out_dir, dem_utm_path, "_REM.tif")
        viz_tif = _find_output(out_dir, dem_utm_path, "_hillshade-color.tif")

        if rem_tif is None:
            # RiverREM "succeeded" but produced nothing we can find — treat as a
            # failure and fall through to the native path.
            raise RuntimeError("RiverREM produced no _REM.tif output")

        _progress(100.0)
        return {"rem_tif": rem_tif, "viz_tif": viz_tif, "engine": "riverrem"}

    except Exception:
        # ImportError (no/broken riverrem) or any runtime error -> native path.
        # We intentionally swallow the specific exception here; the native path
        # is the supported default and will report its own errors if it fails.
        _progress(5.0)
        return native_rem(
            dem_utm_path, centerline_shp, out_dir, progress_cb=progress_cb
        )


# --------------------------------------------------------------------------- #
# Native REM implementation (numpy + scipy.spatial.cKDTree IDW + gdal)
# --------------------------------------------------------------------------- #
#
# Tunable knobs — Joel iterates by number, so these are plain named constants,
# not buried in calc() chains.
#
# Spacing (in DEM pixels) between samples taken along the centerline. ~5 px
# gives dense coverage of the river surface without oversampling.
CENTERLINE_SAMPLE_STEP_PX = 5.0
# k nearest centerline samples used in the IDW interpolation of river-surface
# elevation. Matches RiverREM's default neighborhood feel.
IDW_K = 20
# IDW power. Higher = more local; 1.0 is RiverREM's gentle default.
IDW_POWER = 1.0
# Small epsilon added to distances so a query point coincident with a sample
# doesn't divide by zero.
IDW_EPS = 1e-6
# Pixels processed per IDW chunk. Bounds peak memory: each chunk holds only
# (chunk x k) distance/index/elevation arrays, so a regional COP30 scene of tens
# of millions of pixels stays well within RAM. 1e6 -> ~0.16 GB per block at k=20.
NATIVE_CHUNK_PIXELS = 1_000_000


def native_rem(dem_utm_path, centerline_shp, out_dir, progress_cb=None):
    """In-house REM: sample DEM along the centerline, IDW-interpolate the river
    surface across the whole grid with a scipy cKDTree, subtract from the DEM.

    Uses only QGIS-bundled libraries (numpy, scipy, osgeo.gdal). Writes
    ``<demstem>_REM.tif`` (Float32, same grid/CRS as the input DEM, NoData
    preserved). ``viz_tif`` is None — QGIS applies the pseudocolor itself.

    Returns:
        dict: {"rem_tif": <path>, "viz_tif": None, "engine": "native"}
    """
    from scipy.spatial import cKDTree  # bundled in QGIS; lazy import

    def _progress(p):
        if progress_cb is not None:
            try:
                progress_cb(float(p))
            except Exception:
                pass

    # ----- 1. Read the DEM grid -------------------------------------------- #
    ds = gdal.Open(dem_utm_path, gdal.GA_ReadOnly)
    if ds is None:
        raise RuntimeError("native_rem: cannot open DEM %r" % dem_utm_path)

    band = ds.GetRasterBand(1)
    nx = ds.RasterXSize
    ny = ds.RasterYSize
    gt = ds.GetGeoTransform()       # (ox, px_w, rot1, oy, rot2, px_h)
    proj = ds.GetProjection()
    nodata = band.GetNoDataValue()

    dem = band.ReadAsArray().astype("float32")  # float32 halves peak memory
    _progress(15.0)

    # Mask of valid DEM pixels (exclude NoData and non-finite).
    if nodata is not None:
        valid_mask = np.isfinite(dem) & (dem != nodata)
    else:
        valid_mask = np.isfinite(dem)

    # Pixel size (metres, since DEM is UTM). gt[1] is +west->east, gt[5] is
    # negative (north->south); use magnitudes.
    px_w = abs(gt[1])
    px_h = abs(gt[5])
    pixel_size = (px_w + px_h) / 2.0
    if pixel_size <= 0:
        pixel_size = 1.0  # degenerate guard; shouldn't happen for a real DEM

    # ----- 2. Sample DEM elevations along the centerline ------------------- #
    # World-coordinate sample points, densified along every line segment at
    # ~CENTERLINE_SAMPLE_STEP_PX pixel spacing.
    sample_xy = _densify_centerline(
        centerline_shp, step_m=CENTERLINE_SAMPLE_STEP_PX * pixel_size
    )
    if sample_xy.shape[0] == 0:
        ds = None
        raise RuntimeError("native_rem: centerline produced no sample points")

    _progress(30.0)

    # Convert world XY -> array (row, col), then read DEM elevation there.
    inv_gt = gdal.InvGeoTransform(gt)
    if inv_gt is None:
        ds = None
        raise RuntimeError("native_rem: DEM geotransform is not invertible")

    cols = np.floor(inv_gt[0] + inv_gt[1] * sample_xy[:, 0] + inv_gt[2] * sample_xy[:, 1]).astype(int)
    rows = np.floor(inv_gt[3] + inv_gt[4] * sample_xy[:, 0] + inv_gt[5] * sample_xy[:, 1]).astype(int)

    # Keep only samples that fall inside the grid AND on a valid DEM pixel.
    in_grid = (cols >= 0) & (cols < nx) & (rows >= 0) & (rows < ny)
    s_xy = sample_xy[in_grid]
    s_rows = rows[in_grid]
    s_cols = cols[in_grid]
    if s_xy.shape[0] > 0:
        on_valid = valid_mask[s_rows, s_cols]
        s_xy = s_xy[on_valid]
        s_rows = s_rows[on_valid]
        s_cols = s_cols[on_valid]

    if s_xy.shape[0] == 0:
        ds = None
        raise RuntimeError(
            "native_rem: no centerline samples fell on valid DEM pixels"
        )

    sample_z = dem[s_rows, s_cols]
    _progress(45.0)

    # ----- 3. IDW-interpolate the river surface across the whole grid ------ #
    # Build the KDTree on river-surface sample positions, then query every valid
    # DEM pixel for its k nearest samples and inverse-distance-weight their
    # elevations. The query runs in CHUNKS so peak memory stays bounded on large
    # (regional COP30) scenes — only NATIVE_CHUNK_PIXELS x k arrays exist at once.
    tree = cKDTree(s_xy)

    vr, vc = np.where(valid_mask)          # row/col of every valid pixel
    n_valid = vr.shape[0]

    k = int(min(IDW_K, s_xy.shape[0]))
    if k < 1:
        k = 1

    river_surface = np.empty(n_valid, dtype="float32")
    chunk = int(NATIVE_CHUNK_PIXELS)
    for start in range(0, n_valid, chunk):
        end = min(start + chunk, n_valid)
        rc = vr[start:end]
        cc = vc[start:end]
        # Pixel-centre world coords for this block (rotation-free DEMs).
        qx = gt[0] + (cc + 0.5) * gt[1] + (rc + 0.5) * gt[2]
        qy = gt[3] + (cc + 0.5) * gt[4] + (rc + 0.5) * gt[5]
        q_xy = np.column_stack([qx, qy])

        # scipy 1.5.x uses n_jobs=; newer renamed it workers=. _kdtree_query
        # tries both, then neither — works across the scipy versions QGIS ships.
        dist, idx = _kdtree_query(tree, q_xy, k)
        if k == 1:
            dist = dist[:, None]
            idx = idx[:, None]

        w = 1.0 / np.power(dist + IDW_EPS, IDW_POWER)        # inverse-distance
        neigh_z = sample_z[idx]                              # (block, k)
        river_surface[start:end] = (np.sum(w * neigh_z, axis=1)
                                    / np.sum(w, axis=1)).astype("float32")

        # Progress across the chunked sweep: 45% -> 85%.
        if n_valid:
            _progress(45.0 + 40.0 * (end / float(n_valid)))

    # ----- 4. REM = DEM - interpolated river surface ----------------------- #
    out_nodata = -9999.0
    rem = np.full((ny, nx), out_nodata, dtype="float32")
    rem[vr, vc] = (dem[vr, vc] - river_surface).astype("float32")

    # ----- 5. Write <demstem>_REM.tif -------------------------------------- #
    stem = os.path.splitext(os.path.basename(dem_utm_path))[0]
    rem_tif = os.path.join(out_dir, "%s_REM.tif" % stem)

    driver = gdal.GetDriverByName("GTiff")
    out_ds = driver.Create(
        rem_tif, nx, ny, 1, gdal.GDT_Float32,
        options=["COMPRESS=DEFLATE", "TILED=YES"],
    )
    out_ds.SetGeoTransform(gt)
    out_ds.SetProjection(proj)
    out_band = out_ds.GetRasterBand(1)
    out_band.WriteArray(rem)
    out_band.SetNoDataValue(out_nodata)
    out_band.FlushCache()
    out_ds = None
    ds = None  # close input DEM

    _progress(100.0)
    return {"rem_tif": rem_tif, "viz_tif": None, "engine": "native"}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _kdtree_query(tree, query_xy, k):
    """cKDTree.query across scipy versions (workers vs n_jobs vs neither)."""
    try:
        return tree.query(query_xy, k=k, workers=-1)
    except TypeError:
        pass
    try:
        return tree.query(query_xy, k=k, n_jobs=-1)
    except TypeError:
        pass
    return tree.query(query_xy, k=k)


def _densify_centerline(centerline_shp, step_m):
    """Read every LineString in the shapefile and return an (M, 2) array of
    world XY points sampled along the lines at ~step_m spacing.

    Reads with OGR (bundled). Handles LineString and MultiLineString. Always
    includes each segment's endpoints so short segments still contribute a
    sample.
    """
    if step_m <= 0:
        step_m = 1.0

    ds = ogr.Open(centerline_shp)
    if ds is None:
        raise RuntimeError(
            "_densify_centerline: cannot open centerline %r" % centerline_shp
        )
    layer = ds.GetLayer(0)

    pts = []
    feat = layer.GetNextFeature()
    while feat is not None:
        geom = feat.GetGeometryRef()
        if geom is not None:
            _collect_line_points(geom, step_m, pts)
        feat = layer.GetNextFeature()
    ds = None

    if not pts:
        return np.empty((0, 2), dtype="float64")
    return np.asarray(pts, dtype="float64")


def _collect_line_points(geom, step_m, out):
    """Recursively densify a geometry into out (list of [x,y]).

    Handles (Multi)LineString (densified at step_m) and also Point/MultiPoint —
    the GRASS DEM-derived provider supplies channel sample points directly rather
    than a polyline, and those are used as-is.
    """
    gtype = geom.GetGeometryType()
    # Normalize away the Z/M flags so 2.5D geometries are handled the same.
    flat = ogr.GT_Flatten(gtype) if hasattr(ogr, "GT_Flatten") else gtype

    if flat == ogr.wkbPoint:
        out.append([geom.GetX(), geom.GetY()])
        return

    if flat == ogr.wkbLineString:
        n = geom.GetPointCount()
        if n == 0:
            return
        prev = geom.GetPoint_2D(0)
        out.append([prev[0], prev[1]])
        for i in range(1, n):
            cur = geom.GetPoint_2D(i)
            seg_len = math.hypot(cur[0] - prev[0], cur[1] - prev[1])
            if seg_len > step_m:
                # Insert intermediate points at step_m spacing along the segment.
                n_steps = int(seg_len // step_m)
                for s in range(1, n_steps + 1):
                    t = (s * step_m) / seg_len
                    out.append([
                        prev[0] + t * (cur[0] - prev[0]),
                        prev[1] + t * (cur[1] - prev[1]),
                    ])
            out.append([cur[0], cur[1]])
            prev = cur
    else:
        # MultiLineString / GeometryCollection — recurse into parts.
        for i in range(geom.GetGeometryCount()):
            child = geom.GetGeometryRef(i)
            if child is not None:
                _collect_line_points(child, step_m, out)


def _find_output(out_dir, dem_path, suffix):
    """Locate a RiverREM output file ending in `suffix` for the given DEM.

    RiverREM names outputs ``<demstem><suffix>`` (e.g. ``dem_utm_REM.tif``).
    We first look for the exact expected name, then fall back to any file in
    out_dir ending with the suffix (RiverREM versions vary the stem slightly).
    Returns an absolute path or None.
    """
    stem = os.path.splitext(os.path.basename(dem_path))[0]
    expected = os.path.join(out_dir, "%s%s" % (stem, suffix))
    if os.path.isfile(expected):
        return expected
    try:
        candidates = [
            os.path.join(out_dir, f)
            for f in os.listdir(out_dir)
            if f.endswith(suffix)
        ]
    except OSError:
        return None
    return candidates[0] if candidates else None
