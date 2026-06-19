# -*- coding: utf-8 -*-
"""River REM — main plugin class.

Owns the toolbar/menu actions and the one-button flow entry point. The actual
download + REM compute happens off the GUI thread in RemTask (rem_task.py);
this class only:
  - reads the current canvas extent + CRS,
  - transforms it to an EPSG:4326 lat/lon bbox,
  - runs the zoom-out / zoom-in guardrails,
  - validates that an API key is present,
  - and launches RemTask via the QGIS task manager.

Nothing here blocks the UI.
"""

import os

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction

from qgis.core import (
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsProject,
    Qgis,
)

from .settings_dialog import SettingsDialog, get_api_key, get_use_selected_layer

# --- Guardrail tuning knobs (km^2). Joel iterates by number — keep them here. --
# Below this the view is so tight the DEM/centerline would be useless: zoom out.
MIN_AREA_KM2 = 0.05
# Above this the request is absurd (and would blow past every dataset cap): zoom in.
# ~2000 km^2 per the locked plan.
MAX_AREA_KM2 = 2000.0

_PLUGIN_DIR = os.path.dirname(__file__)
_ICON_PATH = os.path.join(_PLUGIN_DIR, "icon.png")
_MENU_LABEL = "&River REM"


def _bbox_area_km2(south, north, west, east):
    """Rough lat/lon-bbox area in km^2 (cos-latitude correction at the centroid).

    Good enough for the zoom guardrails; the precise per-dataset area check
    lives in dem_selector.bbox_area_km2.
    """
    import math

    lat_mid = math.radians((south + north) / 2.0)
    # 1 deg latitude ~ 111.32 km; longitude shrinks by cos(lat).
    height_km = abs(north - south) * 111.32
    width_km = abs(east - west) * 111.32 * max(math.cos(lat_mid), 0.0)
    return height_km * width_km


class RiverRemPlugin:
    """The QGIS plugin object QGIS instantiates via classFactory."""

    def __init__(self, iface):
        self.iface = iface
        self.actions = []
        self._action_run = None
        self._action_settings = None
        self._action_style = None
        self._style_panel = None
        self._task = None

    # -- lifecycle -------------------------------------------------------------

    def initGui(self):  # noqa: N802 (QGIS-mandated name)
        """Add the toolbar button + menu items. Idempotent-safe per QGIS lifecycle."""
        icon = QIcon(_ICON_PATH) if os.path.exists(_ICON_PATH) else QIcon()

        # Main one-button action: toolbar + plugin menu.
        self._action_run = QAction(icon, "Generate River REM", self.iface.mainWindow())
        self._action_run.setToolTip(
            "Build a Relative Elevation Model for the current map view"
        )
        self._action_run.triggered.connect(self.run)
        self.iface.addToolBarIcon(self._action_run)
        self.iface.addPluginToRasterMenu(_MENU_LABEL, self._action_run)
        self.actions.append(self._action_run)

        # Live Style panel action: plugin menu only.
        self._action_style = QAction(
            "River REM Style…", self.iface.mainWindow()
        )
        self._action_style.setToolTip(
            "Palette + hillshade controls for the loaded REM (no re-download)"
        )
        self._action_style.triggered.connect(self.open_style_panel)
        self.iface.addPluginToRasterMenu(_MENU_LABEL, self._action_style)
        self.actions.append(self._action_style)

        # Settings action: plugin menu only.
        self._action_settings = QAction(
            "River REM Settings…", self.iface.mainWindow()
        )
        self._action_settings.triggered.connect(self.open_settings)
        self.iface.addPluginToRasterMenu(_MENU_LABEL, self._action_settings)
        self.actions.append(self._action_settings)

    def unload(self):
        """Remove everything initGui added. Safe to call even if partially set up."""
        for action in self.actions:
            try:
                self.iface.removePluginRasterMenu(_MENU_LABEL, action)
            except Exception:
                pass
            try:
                self.iface.removeToolBarIcon(action)
            except Exception:
                pass
        self.actions = []
        self._action_run = None
        self._action_settings = None
        self._action_style = None
        if self._style_panel is not None:
            try:
                self.iface.removeDockWidget(self._style_panel)
                self._style_panel.deleteLater()
            except Exception:
                pass
            self._style_panel = None

    # -- settings --------------------------------------------------------------

    def open_settings(self):
        """Show the settings dialog (API key + centerline override)."""
        dlg = SettingsDialog(self.iface.mainWindow())
        dlg.exec_()

    # -- live style panel ------------------------------------------------------

    def _ensure_style_panel(self):
        """Lazily create + dock the live Style panel."""
        if self._style_panel is None:
            from .style_panel import StylePanel
            from qgis.PyQt.QtCore import Qt

            self._style_panel = StylePanel(self.iface, self.iface.mainWindow())
            self.iface.addDockWidget(Qt.RightDockWidgetArea, self._style_panel)
        return self._style_panel

    def open_style_panel(self):
        """Show the Style panel and point it at the current REM."""
        panel = self._ensure_style_panel()
        panel.show()
        panel.raise_()
        panel.refresh_target()

    # -- the one-button flow ---------------------------------------------------

    def run(self):
        """Read the canvas, validate, and launch the background REM task."""
        api_key = get_api_key()
        if not api_key:
            # No key -> open settings and tell the user why, then stop.
            self._message(
                "Set your OpenTopography API key in River REM Settings first.",
                level=Qgis.Warning,
            )
            self.open_settings()
            # Re-check: if they just entered it, proceed; otherwise bail quietly.
            api_key = get_api_key()
            if not api_key:
                return

        # Current canvas extent + CRS -> EPSG:4326 bbox.
        canvas = self.iface.mapCanvas()
        extent = canvas.extent()
        src_crs = canvas.mapSettings().destinationCrs()
        dst_crs = QgsCoordinateReferenceSystem("EPSG:4326")
        transform = QgsCoordinateTransform(
            src_crs, dst_crs, QgsProject.instance().transformContext()
        )

        try:
            bbox = transform.transformBoundingBox(extent)
        except Exception as exc:  # pragma: no cover - defensive on exotic CRS
            self._message(
                "Could not transform the map extent to lat/lon: {}".format(exc),
                level=Qgis.Critical,
            )
            return

        # QgsRectangle: xMin/xMax = west/east, yMin/yMax = south/north.
        west = bbox.xMinimum()
        east = bbox.xMaximum()
        south = bbox.yMinimum()
        north = bbox.yMaximum()

        if north <= south or east <= west:
            self._message(
                "The current map view has no area. Zoom to a river and try again.",
                level=Qgis.Warning,
            )
            return

        # Zoom-out / zoom-in guardrails.
        area_km2 = _bbox_area_km2(south, north, west, east)
        if area_km2 > MAX_AREA_KM2:
            self._message(
                "This view covers ~{:,.0f} km² — too large for a REM. "
                "Zoom in to under {:,.0f} km² and try again.".format(
                    area_km2, MAX_AREA_KM2
                ),
                level=Qgis.Warning,
            )
            return
        if area_km2 < MIN_AREA_KM2:
            self._message(
                "This view is tiny (~{:.3f} km²). Zoom out a little so the "
                "DEM and centerline have something to work with.".format(area_km2),
                level=Qgis.Warning,
            )
            return

        # Output root: a per-run rem_outputs/<slug>_<timestamp>/ subfolder so we
        # never litter the user's project directory with bare dem.tif/REM files.
        out_root = self._run_output_dir(south, north, west, east)

        # Optional manual centerline: the active line layer, only if the toggle is on.
        use_selected = get_use_selected_layer()
        manual_layer = self.iface.activeLayer() if use_selected else None

        # Build + launch the task. RemTask owns all heavy work and touches
        # layers/QgsProject only in finished() on the main thread.
        from .rem_task import RemTask

        task = RemTask(
            bbox4326=(south, north, west, east),
            api_key=api_key,
            out_root=out_root,
            use_selected_layer=use_selected,
            manual_layer=manual_layer,
        )
        # Keep a reference so the task isn't garbage-collected mid-run.
        self._task = task
        # When the REM finishes loading, pop the live Style panel on the new layer.
        task.taskCompleted.connect(self.open_style_panel)
        QgsApplication.taskManager().addTask(task)

        self._message(
            "Building River REM for the current view… (running in the background)",
            level=Qgis.Info,
        )

    # -- helpers ---------------------------------------------------------------

    def _output_base(self):
        """Base dir for rem_outputs/: the saved project's dir, else the package parent."""
        project_path = QgsProject.instance().homePath()
        if project_path:
            return project_path
        # Fall back to the project root that contains this package.
        return os.path.dirname(_PLUGIN_DIR)

    def _run_output_dir(self, south, north, west, east):
        """Create and return a per-run rem_outputs/<lat>_<lon>_<timestamp>/ folder.

        Keeps each run's DEM / centerline / REM together and out of the user's
        project directory. Lat/lon are the bbox centre, signed, so the folder is
        self-describing.
        """
        import datetime

        lat = (south + north) / 2.0
        lon = (west + east) / 2.0
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        slug = "rem_{:.4f}_{:.4f}_{}".format(lat, lon, stamp)
        run_dir = os.path.join(self._output_base(), "rem_outputs", slug)
        os.makedirs(run_dir, exist_ok=True)
        return run_dir

    def _message(self, text, level=None):
        """Push a non-blocking message to the QGIS message bar."""
        if level is None:
            level = Qgis.Info
        self.iface.messageBar().pushMessage("River REM", text, level=level, duration=8)
