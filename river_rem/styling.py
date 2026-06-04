"""styling.py — REM raster styling + layer loading for the River REM plugin.

Two public entry points:

- apply_rem_pseudocolor(layer, vmin, vmax): style a raw _REM.tif with a
  singleband pseudocolor ramp, light -> dark (inverted so dark = high above
  the river), Min -0.5 / Max 12 by default (the values Joel iterates on).
- load_results(results): add the raw REM (styled) first, then the riverrem
  hillshade-color viz on top if it exists, into the current QgsProject.

These run on the MAIN thread only (called from RemTask.finished). Never call
them from QgsTask.run.
"""

import os

from qgis.PyQt.QtGui import QColor

from qgis.core import (
    QgsProject,
    QgsRasterLayer,
    QgsColorRampShader,
    QgsRasterShader,
    QgsSingleBandPseudoColorRenderer,
)


# ---------------------------------------------------------------------------
# Pseudocolor ramp for the raw REM
# ---------------------------------------------------------------------------

def apply_rem_pseudocolor(layer, vmin=-0.5, vmax=12):
    """Apply an inverted light->dark singleband-pseudocolor ramp to a REM layer.

    The REM encodes "height above the river": ~0 in the channel, larger going
    up onto terraces. We render LOW values light and HIGH values dark (the
    "invert so dark = high" convention from the manual workflow). vmin/vmax are
    the two knobs Joel tunes — defaults -0.5 .. 12 per the locked plan.

    Returns the layer (styled in place).
    """
    if layer is None or not layer.isValid():
        return layer

    # Five evenly spaced stops between vmin and vmax. Light at the low end,
    # dark at the high end (inverted). Tweak these RGBs to retaste the ramp.
    span = float(vmax) - float(vmin)
    stops = [
        (vmin + 0.00 * span, QColor(255, 255, 255), "low (river)"),   # white
        (vmin + 0.25 * span, QColor(199, 209, 191), ""),              # pale sage
        (vmin + 0.50 * span, QColor(122, 142, 120), ""),              # mid green-grey
        (vmin + 0.75 * span, QColor(60, 70, 64), ""),                 # deep slate
        (vmin + 1.00 * span, QColor(15, 18, 17), "high"),             # near-black
    ]

    ramp = QgsColorRampShader(float(vmin), float(vmax))
    ramp.setColorRampType(QgsColorRampShader.Interpolated)
    items = [QgsColorRampShader.ColorRampItem(v, c, lbl) for (v, c, lbl) in stops]
    ramp.setColorRampItemList(items)

    shader = QgsRasterShader()
    shader.setRasterShaderFunction(ramp)

    renderer = QgsSingleBandPseudoColorRenderer(
        layer.dataProvider(), 1, shader
    )
    # Pin classification min/max so the ramp doesn't auto-stretch off our knobs.
    renderer.setClassificationMin(float(vmin))
    renderer.setClassificationMax(float(vmax))

    layer.setRenderer(renderer)
    layer.triggerRepaint()
    return layer


# ---------------------------------------------------------------------------
# Load both rasters into the project
# ---------------------------------------------------------------------------

def load_results(results):
    """Add the run's rasters to the current QgsProject.

    Loads the raw REM (styled with the pseudocolor ramp) FIRST so it sits at
    the bottom, then the riverrem hillshade-color viz ON TOP if present. The
    native fallback produces no viz_tif (viz_tif=None) — in that case only the
    styled raw REM is shown, which is the whole point of styling it.

    `results` is the dict RemTask builds: expects keys 'rem_tif' (required) and
    'viz_tif' (optional / may be None).

    Returns the list of added QgsRasterLayer objects (bottom-first).
    """
    project = QgsProject.instance()
    added = []

    rem_tif = results.get("rem_tif")
    if rem_tif and os.path.exists(rem_tif):
        rem_name = os.path.splitext(os.path.basename(rem_tif))[0]
        rem_layer = QgsRasterLayer(rem_tif, rem_name)
        if rem_layer.isValid():
            apply_rem_pseudocolor(rem_layer)
            project.addMapLayer(rem_layer)
            added.append(rem_layer)

    viz_tif = results.get("viz_tif")
    if viz_tif and os.path.exists(viz_tif):
        viz_name = os.path.splitext(os.path.basename(viz_tif))[0]
        viz_layer = QgsRasterLayer(viz_tif, viz_name)
        if viz_layer.isValid():
            # riverrem's hillshade-color.tif is a 3-band RGB blend; default
            # multiband renderer is correct — no pseudocolor needed.
            project.addMapLayer(viz_layer)
            added.append(viz_layer)

    return added
