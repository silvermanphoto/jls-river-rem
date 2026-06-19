"""rem_task.py — the background QgsTask that runs the whole REM pipeline.

Orchestration (all in run(), off the main thread):

    1. Pick the highest-available DEM candidate list (dem_selector) and try
       each in order until one downloads -> raw dem.tif (EPSG:4326 GTiff).
    2. Reproject that DEM to the local UTM zone with gdal:warpreproject
       (bilinear) -> dem_utm.tif.
    3. Get a river centerline (centerline): manual layer / Overpass / GRASS.
    4. Generate the REM (rem_engine): riverrem wrap, native scipy fallback.

run() NEVER touches QgsProject, layers, or any GUI object. It only computes,
reports progress, checks isCanceled(), and stashes results/exception. All
layer-adding + styling + message-bar reporting happens in finished(), which
the task manager calls back on the main thread.
"""

import os
import traceback

import processing

from qgis.PyQt.QtCore import QCoreApplication

from qgis.core import (
    Qgis,
    QgsTask,
    QgsProject,
    QgsVectorLayer,
    QgsCoordinateReferenceSystem,
)

from . import dem_selector
from . import centerline as centerline_mod
from . import rem_engine
from . import styling


MESSAGE_CATEGORY = "RiverREM"


def _skip_reason(exc):
    """A terse reason a DEM candidate was skipped, for the message bar."""
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status in (401, 403):
        return "academic access required"
    if status == 429:
        return "rate-limited"
    if status == 400:
        return "area too large"
    text = str(exc).lower()
    if "empty" in text or "no coverage" in text:
        return "no coverage here"
    return "unavailable"


class RemTask(QgsTask):
    """Runs DEM download -> UTM warp -> centerline -> REM in the background."""

    def __init__(self, bbox4326, api_key, out_root, use_selected_layer, manual_layer):
        """
        bbox4326: (south, north, west, east) in EPSG:4326.
        api_key: OpenTopography API key (passed as a query param downstream).
        out_root: project-level rem_outputs/<slug>_<timestamp>/ folder for this run.
        use_selected_layer: bool — if True, prefer the user's selected line layer.
        manual_layer: the QgsVectorLayer to use as the manual centerline, or None.
            (Captured on the MAIN thread before the task starts — we only read
            its source path inside run(), never touch the live object.)
        """
        super().__init__("Generate River REM", QgsTask.CanCancel)

        self.bbox4326 = bbox4326
        self.api_key = api_key
        self.out_root = out_root
        self.use_selected_layer = use_selected_layer

        # Resolve the manual layer to a plain shapefile path NOW (main thread,
        # in __init__) so run() never has to read a live QgsMapLayer. If the
        # selected layer isn't a usable file-based line source, we drop it.
        self.manual_centerline_path = None
        if use_selected_layer and manual_layer is not None:
            try:
                src = manual_layer.source()
                # source() can carry "|layername=..." etc.; strip to the file.
                path = src.split("|")[0]
                if path and os.path.exists(path):
                    self.manual_centerline_path = path
            except Exception:
                self.manual_centerline_path = None

        self.results = None   # dict on success (see below)
        self.exc = None       # str traceback on failure
        # Current stage's progress band; _progress_cb maps a 0..100 sub-progress
        # into this so download/REM callbacks advance the bar within their phase.
        self._stage_lo = 0.0
        self._stage_hi = 100.0

    # -- helpers --------------------------------------------------------------

    def _progress_cb(self, sub):
        """Map a 0..100 sub-progress (from the download or REM helper) into the
        current stage's band [_stage_lo, _stage_hi] so the bar advances smoothly
        within each phase instead of jumping or sitting frozen."""
        try:
            lo, hi = self._stage_lo, self._stage_hi
            self.setProgress(max(0.0, min(100.0, lo + (hi - lo) * float(sub) / 100.0)))
        except Exception:
            pass

    # -- main work (background thread) ---------------------------------------

    def run(self):
        """Heavy lifting. Returns True on success, False on failure/cancel.

        Stores self.results on success and self.exc on failure. Absolutely no
        QgsProject / layer / GUI access here.
        """
        try:
            south, north, west, east = self.bbox4326
            os.makedirs(self.out_root, exist_ok=True)

            # ---- Stage 1: download the highest-available DEM ----------------
            self.setProgress(2.0)
            if self.isCanceled():
                return False

            candidates = dem_selector.candidate_datasets(south, north, west, east)
            if not candidates:
                raise RuntimeError(
                    "No DEM dataset fits this bounding box. Zoom in to a "
                    "smaller area and try again."
                )

            dem_path = os.path.join(self.out_root, "dem.tif")
            chosen = None
            errors = []
            skipped = []   # higher-res datasets tried-and-skipped before success
            self._stage_lo, self._stage_hi = 2.0, 35.0   # download phase band
            for cand in candidates:
                if self.isCanceled():
                    return False
                try:
                    dem_selector.download_dem(
                        cand, (south, north, west, east),
                        self.api_key, dem_path, progress_cb=self._progress_cb,
                    )
                    chosen = cand
                    break
                except Exception as e:  # try the next coarser dataset
                    label = "%s@%sm" % (
                        cand.get("value", "?"), cand.get("res_m", "?"))
                    errors.append("%s: %s" % (label, e))
                    skipped.append("%s (%s)" % (
                        cand.get("value", "?"), _skip_reason(e)))
                    continue

            if chosen is None:
                raise RuntimeError(
                    "All DEM candidates failed:\n  " + "\n  ".join(errors)
                )

            self.setProgress(35.0)
            if self.isCanceled():
                return False

            # ---- Stage 2: reproject DEM -> local UTM (metric) ---------------
            centroid_lon = (west + east) / 2.0
            centroid_lat = (south + north) / 2.0
            utm_epsg = rem_engine.utm_epsg_for(centroid_lon, centroid_lat)

            dem_utm_path = os.path.join(self.out_root, "dem_utm.tif")
            processing.run(
                "gdal:warpreproject",
                {
                    "INPUT": dem_path,
                    # SOURCE_CRS omitted (None): use the DEM's OWN embedded CRS.
                    # OpenTopography returns global DEMs in EPSG:4326/4269 but
                    # USGS1m comes in a projected metre CRS (UTM) — hard-coding
                    # 4326 mangled those into no output. None handles all cases.
                    "SOURCE_CRS": None,
                    "TARGET_CRS": QgsCoordinateReferenceSystem("EPSG:%d" % utm_epsg),
                    "RESAMPLING": 1,          # 1 = bilinear
                    "NODATA": None,
                    "TARGET_RESOLUTION": None,
                    "OPTIONS": "",
                    "DATA_TYPE": 0,           # 0 = keep input type
                    "TARGET_EXTENT": None,
                    "MULTITHREADING": False,
                    "OUTPUT": dem_utm_path,
                },
            )
            if not os.path.exists(dem_utm_path):
                raise RuntimeError("DEM reprojection to UTM failed (no output).")

            self.setProgress(50.0)
            if self.isCanceled():
                return False

            # ---- Stage 3: river centerline ----------------------------------
            manual_layer = None
            if self.manual_centerline_path:
                # Re-open the file as a throwaway vector layer for centerline
                # logic. This is a NEW layer object (not added to the project),
                # so it's safe to construct off the main thread.
                manual_layer = QgsVectorLayer(
                    self.manual_centerline_path, "manual_centerline", "ogr"
                )
                if not manual_layer.isValid():
                    manual_layer = None

            centerline_shp = centerline_mod.get_centerline(
                (south, north, west, east),
                dem_utm_path,
                manual_layer=manual_layer,
            )
            if not centerline_shp or not os.path.exists(centerline_shp):
                raise RuntimeError(
                    "Could not obtain a river centerline (OSM/Overpass empty, "
                    "GRASS fallback failed, no manual layer)."
                )

            self.setProgress(60.0)
            if self.isCanceled():
                return False

            # ---- Stage 4: make the REM --------------------------------------
            self._stage_lo, self._stage_hi = 60.0, 100.0   # REM compute band
            rem_out = rem_engine.make_rem(
                dem_utm_path,
                centerline_shp,
                self.out_root,
                progress_cb=self._progress_cb,
            )
            if not rem_out or not rem_out.get("rem_tif") \
                    or not os.path.exists(rem_out["rem_tif"]):
                raise RuntimeError("REM generation produced no output raster.")

            self.setProgress(100.0)

            # ---- Stash results for finished() (main thread) -----------------
            self.results = {
                "rem_tif": rem_out.get("rem_tif"),
                "viz_tif": rem_out.get("viz_tif"),
                "dataset": chosen.get("value"),
                "res_m": chosen.get("res_m"),
                "is_dsm": chosen.get("is_dsm", False),
                "engine": rem_out.get("engine", "unknown"),
                "out_dir": self.out_root,
                "skipped": skipped,   # higher-res datasets that were unavailable
            }
            return True

        except Exception:
            # Capture the full traceback as a string; surface it in finished().
            self.exc = traceback.format_exc()
            return False

    # -- callback (MAIN thread) ----------------------------------------------

    def finished(self, result):
        """Add + style layers and report to the message bar — MAIN thread only.

        `result` is the bool run() returned. On success we add the raw REM
        (styled) and, if present, the riverrem viz on top, then post an info
        message naming the dataset/resolution/engine actually used. On failure
        or cancel we post a warning with the captured error (non-blocking).
        """
        from qgis.utils import iface  # imported here so the module imports clean

        if result and self.results:
            try:
                styling.load_results(self.results)
            except Exception:
                # Don't let a styling hiccup swallow the success message.
                pass

            dsm_note = " (DSM)" if self.results.get("is_dsm") else ""
            res = self.results.get("res_m")
            res_txt = ("%s m" % res) if res is not None else "?"
            msg = "Used %s @ %s%s via %s" % (
                self.results.get("dataset", "?"),
                res_txt,
                dsm_note,
                self.results.get("engine", "?"),
            )
            skipped = self.results.get("skipped") or []
            if skipped:
                msg += " — skipped higher-res: " + ", ".join(skipped)
            level = Qgis.Warning if skipped else Qgis.Info
            if iface is not None:
                iface.messageBar().pushMessage(
                    "River REM", msg, level=level, duration=14
                )
            return

        # Failure / cancellation path.
        if self.isCanceled():
            detail = "Canceled before completion."
        else:
            detail = self.exc or "Unknown error (no traceback captured)."

        # Keep the message bar line short; the full traceback goes to the log.
        first_line = detail.strip().splitlines()[-1] if detail.strip() else detail
        if iface is not None:
            iface.messageBar().pushMessage(
                "River REM", "Failed: %s" % first_line,
                level=Qgis.Warning, duration=0,
            )
        QgsProject.instance()  # touch to keep import used; harmless on main thread
        # Log full detail for debugging.
        try:
            from qgis.core import QgsMessageLog
            QgsMessageLog.logMessage(detail, MESSAGE_CATEGORY, level=Qgis.Warning)
        except Exception:
            pass

    @staticmethod
    def tr(message):
        return QCoreApplication.translate("RemTask", message)
