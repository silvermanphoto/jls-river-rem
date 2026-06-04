# -*- coding: utf-8 -*-
"""River REM — QGIS plugin entry point.

QGIS calls classFactory(iface) to instantiate the plugin. Keep this file
tiny: it only wires QGIS to the plugin class in river_rem_plugin.py.
"""


def classFactory(iface):  # noqa: N802 (QGIS-mandated name)
    """Load RiverRemPlugin. Called by QGIS when the plugin is enabled."""
    from .river_rem_plugin import RiverRemPlugin
    return RiverRemPlugin(iface)
