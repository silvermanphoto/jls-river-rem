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
from osgeo import ogr, osr


# --- tunables (Joel iterates by number) -------------------------------------
# Overpass HTTP timeout, seconds. The query is bbox-bounded and small, but a
# busy public Overpass instance can be slow; keep generous.
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
OVERPASS_TIMEOUT_S = 90          # client-side socket timeout
OVERPASS_QL_TIMEOUT_S = 60       # server-side [timeout:NN] budget in the QL

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
            OVERPASS_URL, data={"data": query}, timeout=OVERPASS_TIMEOUT_S
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


def _write_lines_shapefile(line_geoms, out_shp, epsg=4326):
    """Write a list of coordinate-lists as LineString features into an ESRI
    Shapefile. Overwrites any existing file at ``out_shp``.

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
def grass_centerline(dem_utm_path, out_shp):
    """Derive a centerline from a (UTM, metric) DEM via GRASS hydrology and
    keep the longest extracted stream line.

    Pipeline: ``grass7:r.watershed`` (flow accumulation) ->
    ``grass7:r.stream.extract`` (threshold vectorization) -> keep longest line.

    Args:
        dem_utm_path: path to the reprojected metric DEM.
        out_shp: output ``.shp`` path for the single longest line.

    Returns:
        The ``out_shp`` path on success, or ``None`` on any failure (so the
        orchestrator raises only after the manual + OSM providers are also
        exhausted).
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
        # Accumulation threshold scaled to ~0.25 km2 of drainage by pixel area,
        # floored so a tiny/coarse DEM still yields a network.
        threshold = _grass_threshold_for_dem(dem_utm_path)

        tmp_dir = tempfile.mkdtemp(prefix="riverrem_grass_")
        accum_tif = os.path.join(tmp_dir, "accum.tif")
        streams_shp = os.path.join(tmp_dir, "streams.shp")

        # 1) Flow accumulation. r.watershed's "accumulation" output is the
        #    flow-accumulation raster we threshold on.
        processing.run(
            "grass7:r.watershed",
            {
                "elevation": dem_utm_path,
                "accumulation": accum_tif,
                "-s": True,        # single flow direction (SFD), crisper channels
                "GRASS_REGION_PARAMETER": None,
                "GRASS_REGION_CELLSIZE_PARAMETER": 0,
            },
        )

        # 2) Extract a vector stream network from the accumulation raster.
        processing.run(
            "grass7:r.stream.extract",
            {
                "elevation": dem_utm_path,
                "accumulation": accum_tif,
                "threshold": threshold,
                "stream_vector": streams_shp,
                "GRASS_REGION_PARAMETER": None,
                "GRASS_REGION_CELLSIZE_PARAMETER": 0,
                "GRASS_OUTPUT_TYPE_PARAMETER": 2,   # line output
            },
        )
    except Exception:
        return None

    # 3) Keep the single longest line and write it to out_shp.
    return _keep_longest_line(streams_shp, out_shp)


def _grass_threshold_for_dem(dem_utm_path):
    """Compute a flow-accumulation threshold in cells (~0.25 km2 of drainage),
    floored at ``GRASS_MIN_THRESHOLD_CELLS``."""
    target_area_m2 = 0.25 * 1_000_000  # 0.25 km2 in m^2 (tune for more/less network)
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

    # 1) Manual override (settings toggle handled by the caller — if a layer is
    #    handed in, use it directly).
    if manual_layer is not None:
        manual_path = _manual_layer_path(manual_layer)
        if manual_path and os.path.exists(manual_path):
            return manual_path
        # A manual layer that resolves to no usable path is a caller error worth
        # surfacing rather than silently overriding; fall through to OSM so the
        # run still has a chance to succeed.

    # 2) OSM via our own Overpass query.
    osm_shp = os.path.join(out_dir, "centerline.shp")
    try:
        result = overpass_centerline(s, n, w, e, osm_shp)
    except Exception:
        result = None
    if result:
        return result

    # 3) DEM-derived (GRASS hydrology).
    grass_shp = os.path.join(out_dir, "centerline.shp")  # same canonical name
    try:
        result = grass_centerline(dem_utm_path, grass_shp)
    except Exception:
        result = None
    if result:
        return result

    raise RuntimeError(
        "Could not obtain a river centerline: Overpass returned no waterways "
        "and the DEM-derived (GRASS) fallback failed. Try a view that contains "
        "a mapped river, or supply a manual centerline layer in settings."
    )


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
