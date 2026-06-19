"""centerline.py — river centerline acquisition for the River REM plugin.

Three providers, tried in priority order by ``get_centerline``:

1. **Manual override** — a user-selected line layer (passed in by the caller).
2. **OSM via our own Overpass query** — ``overpass_centerline``. We POST a
   plain Overpass QL ``way[waterway~...](bbox); out geom;`` request and build an
   ESRI Shapefile of LineStrings (EPSG:4326) from the returned geometry. We do
   NOT use osmnx (the LOCKED PLAN bypasses it entirely).
3. **DEM-derived** — ``grass_centerline``. GRASS hydrology on the UTM DEM
   (r.watershed flow accumulation -> r.stream.extract -> vectorize), keep the
   longest line.

The two providers return ``None`` (not raise) on no-data so the orchestrator can
fall through to the next one. ``get_centerline`` raises only when every provider
has been exhausted.

Only stdlib + already-bundled QGIS libraries are used (requests, osgeo.ogr/osr,
processing). No osmnx, no cmocean.
"""

import os
import tempfile

import requests
from osgeo import ogr, osr, gdal


# --- tunables (Joel iterates by number) -------------------------------------
# Overpass HTTP timeout, seconds. The query is bbox-bounded and small, but a
# busy public Overpass instance can be slow; keep generous.
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
OVERPASS_TIMEOUT_S = 90          # client-side socket timeout
OVERPASS_QL_TIMEOUT_S = 60       # server-side [timeout:NN] budget in the QL
# overpass-api.de's Apache front returns HTTP 406 for the default
# "python-requests/x.y" User-Agent, so we MUST send an identifying UA or every
# query silently looks like "no waterways". Keep a real UA here.
OVERPASS_HEADERS = {
    "User-Agent": "QGIS-River-REM-plugin/0.1 (+https://github.com/silvermanphoto)"
}

# Which waterway tag values count as a usable centerline. Anchored regex so we
# match the whole value, not a substring.
WATERWAY_REGEX = "^(river|stream|canal)$"

# GRASS stream-extract accumulation threshold, in CELLS. Lower => more (smaller)
# streams extracted. ~0.25 km2 of drainage scaled by pixel area is computed at
# call time; this is the floor so we always extract something on tiny DEMs.
GRASS_MIN_THRESHOLD_CELLS = 50


# ---------------------------------------------------------------------------
# OSM / Overpass provider
# ---------------------------------------------------------------------------
def overpass_centerline(s, n, w, e, out_shp):
    """Query Overpass for waterway lines in the bbox; write them to an ESRI
    Shapefile of LineStrings in EPSG:4326, clipped to the bbox.

    Args:
        s, n, w, e: south, north, west, east in EPSG:4326 degrees.
        out_shp: output ``.shp`` path.

    Returns:
        The ``out_shp`` path on success, or ``None`` if Overpass returned no
        usable waterway geometry (so the orchestrator can fall through).
    """
    # Overpass bbox order is (south, west, north, east).
    query = (
        "[out:json][timeout:{t}];"
        '(way["waterway"~"{rx}"]({s},{w},{n},{e}););'
        "out geom;"
    ).format(t=OVERPASS_QL_TIMEOUT_S, rx=WATERWAY_REGEX, s=s, w=w, n=n, e=e)

    try:
        resp = requests.post(
            OVERPASS_URL, data={"data": query}, headers=OVERPASS_HEADERS,
            timeout=OVERPASS_TIMEOUT_S,
        )
        resp.raise_for_status()
        payload = resp.json()
    except (requests.RequestException, ValueError):
        # Network error, HTTP error, or non-JSON body -> treat as "no data" and
        # let the orchestrator fall through to GRASS.
        return None

    elements = payload.get("elements") or []

    # Collect way geometries as lists of (lon, lat) vertices, clipped to bbox.
    # "out geom" inlines each way's node coordinates under element["geometry"].
    clip_rect = (w, s, e, n)  # (minx, miny, maxx, maxy)
    line_geoms = []
    for el in elements:
        if el.get("type") != "way":
            continue
        geom = el.get("geometry")
        if not geom or len(geom) < 2:
            continue
        coords = [(pt["lon"], pt["lat"]) for pt in geom]
        clipped = _clip_line_to_rect(coords, clip_rect)
        line_geoms.extend(clipped)

    if not line_geoms:
        return None

    return _write_lines_shapefile(line_geoms, out_shp, epsg=4326)


def _clip_line_to_rect(coords, rect):
    """Clip a polyline to an axis-aligned rectangle using OGR's Intersection.

    Args:
        coords: list of (lon, lat) vertices.
        rect: (minx, miny, maxx, maxy).

    Returns:
        A list of coordinate-lists (one per resulting LineString; a clip can
        split one line into several pieces). Empty if nothing falls inside.
    """
    minx, miny, maxx, maxy = rect

    line = ogr.Geometry(ogr.wkbLineString)
    for lon, lat in coords:
        line.AddPoint_2D(float(lon), float(lat))

    # Build the clip rectangle as a polygon.
    ring = ogr.Geometry(ogr.wkbLinearRing)
    ring.AddPoint_2D(minx, miny)
    ring.AddPoint_2D(maxx, miny)
    ring.AddPoint_2D(maxx, maxy)
    ring.AddPoint_2D(minx, maxy)
    ring.AddPoint_2D(minx, miny)
    rect_poly = ogr.Geometry(ogr.wkbPolygon)
    rect_poly.AddGeometry(ring)

    try:
        clipped = line.Intersection(rect_poly)
    except Exception:
        clipped = None

    if clipped is None or clipped.IsEmpty():
        return []

    return _flatten_to_linestrings(clipped)


def _flatten_to_linestrings(geom):
    """Reduce an arbitrary OGR geometry to a list of coordinate-lists, keeping
    only LineString / MultiLineString parts."""
    out = []
    gtype = geom.GetGeometryType()
    flat = ogr.GT_Flatten(gtype) if hasattr(ogr, "GT_Flatten") else gtype

    if flat == ogr.wkbLineString:
        pts = [(geom.GetX(i), geom.GetY(i)) for i in range(geom.GetPointCount())]
        if len(pts) >= 2:
            out.append(pts)
    elif flat in (ogr.wkbMultiLineString, ogr.wkbGeometryCollection):
        for i in range(geom.GetGeometryCount()):
            out.extend(_flatten_to_linestrings(geom.GetGeometryRef(i)))
    # Points / empty parts are dropped.
    return out


def _write_lines_shapefile(line_geoms, out_shp, epsg=4326, srs=None):
    """Write a list of coordinate-lists as LineString features into an ESRI
    Shapefile. Overwrites any existing file at ``out_shp``.

    Pass an explicit ``srs`` (osr.SpatialReference) to set the layer CRS exactly
    (e.g. the DEM's UTM zone); otherwise ``epsg`` is used.

    Returns the path on success, or ``None`` if no feature was written.
    """
    driver = ogr.GetDriverByName("ESRI Shapefile")
    if driver is None:
        return None

    # ESRI Shapefile writes a sidecar set (.shp/.shx/.dbf/.prj); delete a stale
    # one first so DataSource creation doesn't fail.
    if os.path.exists(out_shp):
        driver.DeleteDataSource(out_shp)

    out_dir = os.path.dirname(out_shp)
    if out_dir and not os.path.isdir(out_dir):
        os.makedirs(out_dir, exist_ok=True)

    ds = driver.CreateDataSource(out_shp)
    if ds is None:
        return None

    if srs is None:
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(epsg)

    layer_name = os.path.splitext(os.path.basename(out_shp))[0]
    layer = ds.CreateLayer(layer_name, srs, ogr.wkbLineString)
    if layer is None:
        ds = None
        return None

    # A single integer id field so the .dbf is non-empty/valid.
    layer.CreateField(ogr.FieldDefn("id", ogr.OFTInteger))
    defn = layer.GetLayerDefn()

    written = 0
    for idx, coords in enumerate(line_geoms):
        if len(coords) < 2:
            continue
        line = ogr.Geometry(ogr.wkbLineString)
        for x, y in coords:
            line.AddPoint_2D(float(x), float(y))
        feat = ogr.Feature(defn)
        feat.SetGeometry(line)
        feat.SetField("id", idx)
        layer.CreateFeature(feat)
        feat = None
        written += 1

    # Flush + close.
    layer = None
    ds = None

    if written == 0:
        return None
    return out_shp


# ---------------------------------------------------------------------------
# DEM-derived provider (GRASS hydrology)
# ---------------------------------------------------------------------------
# Cap on channel points sampled from the GRASS stream raster. The IDW only needs
# a well-distributed set of river-surface points, not every stream cell.
GRASS_MAX_CENTERLINE_POINTS = 8000


def grass_centerline(dem_utm_path, out_shp):
    """Derive river-channel reference points from a (UTM, metric) DEM via GRASS.

    Pipeline: ``grass7:r.watershed`` (flow accumulation) ->
    ``grass7:r.stream.extract`` (``stream_raster``) -> read the stream cells with
    GDAL and emit their centres as a POINT shapefile in the DEM's CRS.

    Why points, not lines: GRASS 7.8's vector outputs (``stream_vector`` and
    ``r.to.vect type=line``) come back empty / zero-length on this build, while
    the stream RASTER is solid. ``native_rem`` only needs sample points along the
    channel, so we skip vector export entirely and sample the raster directly.
    (Sampling the full extracted network yields a height-above-nearest-channel
    surface — a sensible detrend where there's no OSM river to follow.)

    Returns the ``out_shp`` path on success, or ``None`` on any failure (so the
    orchestrator raises only after the manual + OSM providers are also exhausted).
    """
    # Imported lazily: ``processing`` only exists inside a running QGIS, so a
    # standalone py_compile / import of this module must not require it.
    try:
        import processing  # noqa: F401  (provided by the QGIS Python env)
    except ImportError:
        return None

    if not dem_utm_path or not os.path.isfile(dem_utm_path):
        return None

    try:
        threshold = _grass_threshold_for_dem(dem_utm_path)

        tmp_dir = tempfile.mkdtemp(prefix="riverrem_grass_")
        accum_tif = os.path.join(tmp_dir, "accum.tif")
        stream_tif = os.path.join(tmp_dir, "streams.tif")

        # 1) Flow accumulation (SFD = crisper channels).
        processing.run(
            "grass7:r.watershed",
            {
                "elevation": dem_utm_path,
                "accumulation": accum_tif,
                "-s": True,
                "GRASS_REGION_PARAMETER": None,
                "GRASS_REGION_CELLSIZE_PARAMETER": 0,
            },
        )

        # 2) Extract the stream RASTER (the vector outputs are broken on GRASS 7.8).
        processing.run(
            "grass7:r.stream.extract",
            {
                "elevation": dem_utm_path,
                "accumulation": accum_tif,
                "threshold": threshold,
                "stream_raster": stream_tif,
                "GRASS_REGION_PARAMETER": None,
                "GRASS_REGION_CELLSIZE_PARAMETER": 0,
            },
        )
    except Exception:
        return None

    # 3) Stream cells -> channel points (DEM CRS).
    return _stream_raster_to_points(
        stream_tif, out_shp, GRASS_MAX_CENTERLINE_POINTS)


def _stream_raster_to_points(stream_tif, out_shp, max_points):
    """Convert a GRASS stream raster to a POINT shapefile of channel cell centres.

    Returns out_shp, or None if there are no stream cells / on error.
    """
    try:
        import numpy as np

        ds = gdal.Open(stream_tif)
        if ds is None:
            return None
        band = ds.GetRasterBand(1)
        arr = band.ReadAsArray()
        nodata = band.GetNoDataValue()
        gt = ds.GetGeoTransform()
        proj = ds.GetProjection()
        ds = None

        mask = np.isfinite(arr) & (arr != 0)
        if nodata is not None:
            mask &= (arr != nodata)
        rows, cols = np.where(mask)
        if rows.size == 0:
            return None

        # Subsample to a manageable, well-distributed set of channel points.
        if rows.size > max_points:
            step = int(np.ceil(rows.size / float(max_points)))
            rows = rows[::step]
            cols = cols[::step]

        xs = gt[0] + (cols + 0.5) * gt[1] + (rows + 0.5) * gt[2]
        ys = gt[3] + (cols + 0.5) * gt[4] + (rows + 0.5) * gt[5]

        srs = osr.SpatialReference()
        srs.ImportFromWkt(proj)
        return _write_points_shapefile(np.column_stack([xs, ys]), out_shp, srs)
    except Exception:
        return None


def _write_points_shapefile(points_xy, out_shp, srs):
    """Write (N, 2) world coords as POINT features. Returns path or None."""
    driver = ogr.GetDriverByName("ESRI Shapefile")
    if driver is None:
        return None
    if os.path.exists(out_shp):
        driver.DeleteDataSource(out_shp)
    out_dir = os.path.dirname(out_shp)
    if out_dir and not os.path.isdir(out_dir):
        os.makedirs(out_dir, exist_ok=True)

    ds = driver.CreateDataSource(out_shp)
    if ds is None:
        return None
    layer = ds.CreateLayer(
        os.path.splitext(os.path.basename(out_shp))[0], srs, ogr.wkbPoint)
    if layer is None:
        ds = None
        return None
    layer.CreateField(ogr.FieldDefn("id", ogr.OFTInteger))
    defn = layer.GetLayerDefn()

    written = 0
    for i in range(points_xy.shape[0]):
        pt = ogr.Geometry(ogr.wkbPoint)
        pt.AddPoint_2D(float(points_xy[i, 0]), float(points_xy[i, 1]))
        feat = ogr.Feature(defn)
        feat.SetGeometry(pt)
        feat.SetField("id", written)
        layer.CreateFeature(feat)
        feat = None
        written += 1

    layer = None
    ds = None
    return out_shp if written else None


def _grass_threshold_for_dem(dem_utm_path):
    """Compute a flow-accumulation threshold in cells (~0.05 km2 of drainage),
    floored at ``GRASS_MIN_THRESHOLD_CELLS``.

    Lowered from 0.25 to 0.05 km2 so low-relief / small DEMs still yield a
    channel network instead of an empty stream layer (the fixed 0.25 km2 value
    extracted nothing on flat reaches)."""
    target_area_m2 = 0.05 * 1_000_000  # 0.05 km2 in m^2 (tune for more/less network)
    try:
        from osgeo import gdal

        ds = gdal.Open(dem_utm_path)
        gt = ds.GetGeoTransform()
        ds = None
        # Pixel area = |a| * |e| from the affine geotransform (metres in UTM).
        px_area = abs(gt[1]) * abs(gt[5])
        if px_area <= 0:
            return GRASS_MIN_THRESHOLD_CELLS
        cells = int(target_area_m2 / px_area)
        return max(GRASS_MIN_THRESHOLD_CELLS, cells)
    except Exception:
        return GRASS_MIN_THRESHOLD_CELLS


def _keep_longest_line(streams_shp, out_shp):
    """Read the extracted stream vector, find the geometrically longest line,
    and write just that one feature into ``out_shp``. Returns the path, or
    ``None`` if the source is missing/empty."""
    if not streams_shp or not os.path.isfile(streams_shp):
        return None

    src = ogr.Open(streams_shp)
    if src is None:
        return None
    layer = src.GetLayer(0)
    if layer is None:
        src = None
        return None

    best_coords = None
    best_len = -1.0
    for feat in layer:
        geom = feat.GetGeometryRef()
        if geom is None:
            continue
        for coords in _flatten_to_linestrings(geom):
            length = _polyline_length(coords)
            if length > best_len:
                best_len = length
                best_coords = coords

    # Determine EPSG from the source layer so the output .prj matches the DEM's
    # UTM zone (the longest line is in DEM/UTM coordinates).
    epsg = _layer_epsg(layer, default=4326)

    src = None  # close source before writing the (possibly same-name) output

    if best_coords is None:
        return None

    return _write_lines_shapefile([best_coords], out_shp, epsg=epsg)


def _polyline_length(coords):
    """Planar length of a polyline given as a list of (x, y) vertices."""
    total = 0.0
    for i in range(1, len(coords)):
        dx = coords[i][0] - coords[i - 1][0]
        dy = coords[i][1] - coords[i - 1][1]
        total += (dx * dx + dy * dy) ** 0.5
    return total


def _layer_epsg(layer, default=4326):
    """Best-effort EPSG code from an OGR layer's spatial ref; ``default`` if
    unavailable."""
    try:
        srs = layer.GetSpatialRef()
        if srs is None:
            return default
        srs.AutoIdentifyEPSG()
        code = srs.GetAuthorityCode(None)
        return int(code) if code else default
    except Exception:
        return default


# ---------------------------------------------------------------------------
# CRS reconciliation — the centerline MUST match the DEM's CRS
# ---------------------------------------------------------------------------
def _ensure_dem_crs(centerline_shp, dem_path, out_shp):
    """Return a centerline shapefile in the DEM's CRS.

    The OSM/Overpass provider writes EPSG:4326 (lon/lat) and a manual layer can
    be in any CRS, but ``native_rem`` samples the DEM (UTM, metres) assuming the
    centerline shares its CRS — so an unreprojected line lands entirely off the
    grid ("no centerline samples fell on valid DEM pixels"). This reprojects the
    line into the DEM's CRS. If it already matches (e.g. the GRASS provider, run
    on the UTM DEM), the input path is returned unchanged.

    Uses traditional (x=lon/easting, y=lat/northing) axis order on both ends so
    ``TransformPoint`` is fed coordinates in the order the geometry stores them
    (the GDAL 3 authority-axis-order trap, which otherwise swaps lat/lon).
    """
    if not centerline_shp or not os.path.isfile(centerline_shp) or not dem_path:
        return centerline_shp
    try:
        dem = gdal.Open(dem_path)
        dem_srs = osr.SpatialReference()
        dem_srs.ImportFromWkt(dem.GetProjection())
        dem = None

        src = ogr.Open(centerline_shp)
        layer = src.GetLayer(0)
        src_srs = layer.GetSpatialRef()
        if src_srs is None or dem_srs.IsSame(src_srs):
            src = None
            return centerline_shp  # already in (or indistinguishable from) DEM CRS

        src_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        dem_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        ct = osr.CoordinateTransformation(src_srs, dem_srs)

        line_geoms = []
        for feat in layer:
            geom = feat.GetGeometryRef()
            if geom is None:
                continue
            for coords in _flatten_to_linestrings(geom):
                tc = []
                for x, y in coords:
                    p = ct.TransformPoint(x, y)  # (x=lon, y=lat) -> (easting, northing)
                    tc.append((p[0], p[1]))
                if len(tc) >= 2:
                    line_geoms.append(tc)
        src = None

        if not line_geoms:
            return centerline_shp
        return _write_lines_shapefile(line_geoms, out_shp, srs=dem_srs) or centerline_shp
    except Exception:
        # On any reprojection hiccup, return the original; native_rem will report
        # the off-grid symptom, which is still better than a silent swap.
        return centerline_shp


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------
def get_centerline(bbox4326, dem_utm_path, manual_layer=None):
    """Resolve a river centerline shapefile, trying providers in priority order:

        manual override  ->  OSM/Overpass  ->  DEM-derived/GRASS

    Args:
        bbox4326: (south, north, west, east) in EPSG:4326 degrees.
        dem_utm_path: path to the reprojected metric DEM (for the GRASS
            fallback, and for placing the output shapefiles).
        manual_layer: optional. A user-selected line layer to use directly.
            May be a QgsVectorLayer (we read its ``source()``) or a path string.

    Returns:
        Path to a centerline shapefile (LineString geometry).

    Raises:
        RuntimeError: if no provider produced a usable centerline.
    """
    s, n, w, e = bbox4326

    # Outputs live alongside the DEM so a run folder stays self-contained.
    out_dir = os.path.dirname(dem_utm_path) if dem_utm_path else tempfile.gettempdir()

    raw = None

    # 1) Manual override (settings toggle handled by the caller — if a layer is
    #    handed in, use it directly).
    if manual_layer is not None:
        manual_path = _manual_layer_path(manual_layer)
        if manual_path and os.path.exists(manual_path):
            raw = manual_path
        # A manual layer that resolves to no usable path falls through to OSM so
        # the run still has a chance to succeed.

    # 2) OSM via our own Overpass query (writes EPSG:4326).
    if raw is None:
        osm_shp = os.path.join(out_dir, "centerline.shp")
        try:
            raw = overpass_centerline(s, n, w, e, osm_shp)
        except Exception:
            raw = None

    # 3) DEM-derived (GRASS hydrology — already in the DEM's CRS).
    if raw is None:
        grass_shp = os.path.join(out_dir, "centerline.shp")  # same canonical name
        try:
            raw = grass_centerline(dem_utm_path, grass_shp)
        except Exception:
            raw = None

    if raw is None:
        raise RuntimeError(
            "Could not obtain a river centerline: Overpass returned no waterways "
            "and the DEM-derived (GRASS) fallback failed. Try a view that contains "
            "a mapped river, or supply a manual centerline layer in settings."
        )

    # Reproject into the DEM's CRS so native_rem's samples land on the grid.
    # (No-op when the provider already matched, e.g. GRASS.)
    demcrs_shp = os.path.join(out_dir, "centerline_demcrs.shp")
    return _ensure_dem_crs(raw, dem_utm_path, demcrs_shp)


def _manual_layer_path(manual_layer):
    """Extract a filesystem path from a manual centerline input, which may be a
    QgsVectorLayer or a plain path string."""
    if isinstance(manual_layer, str):
        return manual_layer
    # Duck-typed QgsVectorLayer: .source() returns the provider URI/path. For an
    # OGR shapefile that's the .shp path (possibly with a "|layername=" suffix).
    source = getattr(manual_layer, "source", None)
    if callable(source):
        uri = source()
        if uri:
            return uri.split("|", 1)[0]
    return None
