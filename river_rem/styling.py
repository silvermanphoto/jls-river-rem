"""styling.py — REM raster styling + layer loading for the River REM plugin.

This is where the REM gets its "Dan Coe / OpenTopography" look. Three ingredients
give the iconic glow (a plain linear ramp looks drab without them):

  1. LOG scaling — color stops are spaced geometrically, so the first metre or two
     above the river (where the channel detail lives) gets most of the color
     range instead of a thin linear sliver.
  2. A rich COLORMAP — curated multi-hue ramps (topo / cyanotype / mako / magma)
     instead of a single-hue fade.
  3. A HILLSHADE blend — the colored REM is drawn with Multiply over a hillshade
     of the terrain, giving the luminous, sculpted, almost-painted depth.

Switch the default look by changing DEFAULT_PALETTE below (Joel iterates by name).

Public entry points (MAIN thread only — called from RemTask.finished, never run):
  - apply_rem_pseudocolor(layer, palette=..., vmin=None, vmax=None)
  - load_results(results)
"""

import os

from qgis.PyQt.QtGui import QColor, QPainter

from osgeo import gdal

from qgis.core import (
    QgsProject,
    QgsRasterLayer,
    QgsColorRampShader,
    QgsRasterShader,
    QgsSingleBandPseudoColorRenderer,
)


# ---------------------------------------------------------------------------
# Palettes — low (river, ~0) -> high (terraces).
# Two hand-built ramps, plus the full viridis-family of scientific colormaps
# sampled live from seaborn/matplotlib (both bundled in QGIS) so they match the
# canonical data exactly. Add a name to _CMAP_NAMES to expose more.
# ---------------------------------------------------------------------------
_CUSTOM_PALETTES = {
    # OpenTopography / RiverREM canonical: blue channel -> green -> tan -> cream.
    "topo":      ["#1f4a6e", "#2f7d97", "#5bb0a0", "#9ec98c",
                  "#cbc98c", "#e6dbbb", "#f7f2e6"],
    # Dan Coe blueprint look: deep prussian blue -> pale cyan -> white.
    "cyanotype": ["#082b45", "#0f4c6b", "#2d7397", "#5e9dc0",
                  "#9ac3da", "#cfe3ef", "#f0f7fb"],
}

# The viridis family (matplotlib + seaborn names). Order = dropdown order.
_CMAP_NAMES = ["viridis", "magma", "inferno", "plasma",
               "cividis", "turbo", "rocket", "mako"]


def _sample_cmap(name, n=10):
    """Sample a named seaborn/matplotlib colormap into n hex stops (low->high)."""
    import seaborn as sns  # bundled; color_palette(name, n) works in seaborn 0.10
    pal = sns.color_palette(name, n)
    return ["#%02x%02x%02x" % tuple(int(round(c * 255)) for c in rgb[:3])
            for rgb in pal]


def _build_palettes():
    """topo + cyanotype, then every viridis-family map we can sample."""
    pals = dict(_CUSTOM_PALETTES)
    for name in _CMAP_NAMES:
        try:
            pals[name] = _sample_cmap(name)
        except Exception:
            pass  # a missing colormap just doesn't appear; never breaks load
    return pals


PALETTES = _build_palettes()

# The look loaded by default. Change this one word to retaste every new REM.
DEFAULT_PALETTE = "topo"

# Hillshade knobs (Joel iterates by number).
HILLSHADE_AZIMUTH = 315.0     # light direction, degrees
HILLSHADE_ALTITUDE = 45.0     # light height, degrees
HILLSHADE_Z_FACTOR = 1.0      # vertical exaggeration; soft default = color reads

# Default top of the color ramp, in metres above the river. The source REM look
# (OpenSourceOptions / OpenTopography RiverREM) tops out around 10-12 m so the
# near-river floodplain/terraces get the whole palette; letting it run to the
# full data max (tens-to-hundreds of m in canyons) washes the colour to grey.
DEFAULT_VMAX_M = 15.0


# ---------------------------------------------------------------------------
# Pseudocolor ramp for the raw REM (log-scaled, curated palette)
# ---------------------------------------------------------------------------

def _hexes_to_qcolors(hexes):
    return [QColor(h) for h in hexes]


def apply_rem_pseudocolor(layer, palette=None, vmin=None, vmax=None):
    """Style a raw _REM.tif with a LOG-scaled, multi-hue pseudocolor ramp.

    The REM encodes "height above the river": ~0 in the channel, larger up onto
    terraces. We hold everything at/below the river at the palette's base color,
    then ramp the palette across GEOMETRICALLY increasing heights so near-river
    detail dominates the color range (the key to the glow).

    palette: name in PALETTES (defaults to DEFAULT_PALETTE).
    vmin/vmax: optional overrides for the value range; by default vmax is read
        from the raster's own max and the floor is derived from it.

    Returns the layer (styled in place).
    """
    if layer is None or not layer.isValid():
        return layer

    colors = _hexes_to_qcolors(PALETTES.get(palette or DEFAULT_PALETTE,
                                            PALETTES[DEFAULT_PALETTE]))
    n = len(colors)

    # Data range from the file itself (nodata is ignored — it's set to -9999).
    data_min, data_max = _raster_min_max(layer.source())
    if vmin is None:
        vmin = data_min if data_min is not None else -0.5
    if vmax is None:
        dm = data_max if (data_max is not None and data_max > 0) else 12.0
        # Cap the default ramp near the river (DEFAULT_VMAX_M) so canyons don't
        # spread the palette over hundreds of metres and wash out to grey.
        vmax = min(dm, DEFAULT_VMAX_M)
    vmin = float(vmin)
    vmax = float(vmax)
    if vmax <= 0:
        vmax = 12.0

    # Log floor: the smallest height that gets its own color. max/2000, but never
    # tinier than 5 cm, so the geometric series is well-behaved.
    floor = max(0.05, vmax / 2000.0)
    if floor >= vmax:
        floor = vmax / 10.0

    # Build (value, color) stops: base color held from vmin up to the floor, then
    # a geometric (log) sweep floor -> vmax across the palette.
    items = [QgsColorRampShader.ColorRampItem(vmin, colors[0], "river")]
    ratio = (vmax / floor) ** (1.0 / (n - 1)) if n > 1 else 1.0
    for i in range(n):
        value = floor * (ratio ** i)
        label = ("%.2g m" % value) if i in (0, n - 1) else ""
        items.append(QgsColorRampShader.ColorRampItem(value, colors[i], label))

    ramp = QgsColorRampShader(vmin, vmax)
    ramp.setColorRampType(QgsColorRampShader.Interpolated)
    ramp.setColorRampItemList(items)

    shader = QgsRasterShader()
    shader.setRasterShaderFunction(ramp)

    renderer = QgsSingleBandPseudoColorRenderer(layer.dataProvider(), 1, shader)
    renderer.setClassificationMin(vmin)
    renderer.setClassificationMax(vmax)

    layer.setRenderer(renderer)
    # Multiply over whatever's beneath (the hillshade) for the sculpted look.
    layer.setBlendMode(QPainter.CompositionMode_Multiply)
    layer.triggerRepaint()
    return layer


def _raster_min_max(path):
    """(min, max) of band 1 ignoring nodata, via GDAL. (None, None) on failure."""
    try:
        ds = gdal.Open(path)
        if ds is None:
            return (None, None)
        band = ds.GetRasterBand(1)
        mn, mx, _, _ = band.GetStatistics(True, True)  # approx_ok, force
        ds = None
        return (mn, mx)
    except Exception:
        return (None, None)


# ---------------------------------------------------------------------------
# Hillshade
# ---------------------------------------------------------------------------

def _make_hillshade(dem_path, out_path, z_factor=None, azimuth=None, altitude=None):
    """Write a grayscale hillshade GeoTIFF from a (UTM) DEM via gdaldem.

    z_factor/azimuth/altitude default to the module constants; the Style panel
    passes a live z_factor to dial relief strength up and down.

    Returns out_path on success, or None.
    """
    if not dem_path or not os.path.isfile(dem_path):
        return None
    try:
        opts = gdal.DEMProcessingOptions(
            azimuth=HILLSHADE_AZIMUTH if azimuth is None else azimuth,
            altitude=HILLSHADE_ALTITUDE if altitude is None else altitude,
            zFactor=HILLSHADE_Z_FACTOR if z_factor is None else z_factor,
            computeEdges=True,
        )
        ds = gdal.DEMProcessing(out_path, dem_path, "hillshade", options=opts)
        ds = None
        return out_path if os.path.isfile(out_path) else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Load rasters into the project (hillshade UNDER, colored REM on top, Multiply)
# ---------------------------------------------------------------------------

def _group_name(results):
    """A short, self-describing name for the REM's layer-tree group."""
    ds = results.get("dataset")
    res = results.get("res_m")
    if ds and res is not None:
        return "River REM · %s %sm" % (ds, res)
    if ds:
        return "River REM · %s" % ds
    return "River REM"


def load_results(results):
    """Add the run's rasters into a single COLLAPSED layer-tree group.

    The group holds the colored REM (top, Multiply blend) over its hillshade
    (bottom), so a well-organized project gets one tidy folder per run instead of
    two loose layers. If riverrem ever produced a baked viz it goes on top.

    `results` expects 'rem_tif' (required); 'out_dir' (to find dem_utm.tif),
    'dataset'/'res_m' (group name), and 'viz_tif' are used if present.

    Returns the list of added QgsRasterLayer objects.
    """
    project = QgsProject.instance()
    root = project.layerTreeRoot()
    added = []

    rem_tif = results.get("rem_tif")
    if not (rem_tif and os.path.exists(rem_tif)):
        return added

    run_dir = results.get("out_dir") or os.path.dirname(rem_tif)
    dem_utm = os.path.join(run_dir, "dem_utm.tif")

    # New collapsed group at the top of the tree.
    group = root.insertGroup(0, _group_name(results))

    # 1) Colored REM on top of the group (Multiply blend set in apply_rem_pseudocolor).
    rem_name = os.path.splitext(os.path.basename(rem_tif))[0]
    rem_layer = QgsRasterLayer(rem_tif, rem_name)
    if rem_layer.isValid():
        apply_rem_pseudocolor(rem_layer)
        project.addMapLayer(rem_layer, False)   # register, don't add to tree root
        group.addLayer(rem_layer)
        added.append(rem_layer)

    # 2) Hillshade BENEATH the REM, inside the same group.
    hs_path = _make_hillshade(dem_utm, os.path.join(run_dir, "hillshade.tif"))
    if hs_path:
        hs_layer = QgsRasterLayer(hs_path, "hillshade")
        if hs_layer.isValid():
            project.addMapLayer(hs_layer, False)
            group.addLayer(hs_layer)
            added.append(hs_layer)

    # 3) Optional baked riverrem viz on top of the group.
    viz_tif = results.get("viz_tif")
    if viz_tif and os.path.exists(viz_tif):
        viz_layer = QgsRasterLayer(viz_tif, os.path.splitext(os.path.basename(viz_tif))[0])
        if viz_layer.isValid():
            project.addMapLayer(viz_layer, False)
            group.insertLayer(0, viz_layer)
            added.append(viz_layer)

    group.setExpanded(False)   # collapsed by default
    return added


# ---------------------------------------------------------------------------
# Helper for the live Style panel: find the REM the user is looking at
# ---------------------------------------------------------------------------

def find_current_rem(iface):
    """Locate the REM to restyle and its companions.

    Prefers the active layer when it's a REM, else the most-recently-added REM
    layer. Returns (rem_layer, hillshade_layer_or_None, dem_utm_path_or_None).
    The hillshade is matched by name within the REM's own run folder.
    """
    project = QgsProject.instance()

    active = iface.activeLayer() if iface is not None else None
    rem = active if (active is not None and "REM" in active.name()) else None
    if rem is None:
        rems = [l for l in project.mapLayers().values() if "REM" in l.name()]
        rem = rems[-1] if rems else None
    if rem is None:
        return (None, None, None)

    run_dir = os.path.dirname(rem.source().split("|")[0])
    dem = os.path.join(run_dir, "dem_utm.tif")
    dem = dem if os.path.isfile(dem) else None

    hs = None
    for l in project.mapLayers().values():
        if l.name() == "hillshade" and os.path.dirname(l.source().split("|")[0]) == run_dir:
            hs = l
            break

    return (rem, hs, dem)
