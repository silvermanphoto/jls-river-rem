"""style_panel.py — live REM styling dock (palette + hillshade strength).

A dockable panel that restyles the REM already loaded in the project, on the fly,
with no re-download:

  - Palette dropdown  -> re-applies the pseudocolor ramp instantly.
  - Relief strength (Z) slider -> regenerates the hillshade at that exaggeration
    (on release, since it re-runs gdaldem) and reloads it.
  - Shading opacity slider -> sets the hillshade layer opacity instantly.

It targets the active REM layer (or the most recent one); use "Target current
REM" after clicking a different REM in the layers panel.
"""

import os

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QComboBox,
    QDockWidget,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)
from qgis.core import QgsProject, QgsRasterLayer

from . import styling


class StylePanel(QDockWidget):
    """Dockable live-styling controls for the current REM."""

    def __init__(self, iface, parent=None):
        super().__init__("River REM Style", parent)
        self.iface = iface
        self.setObjectName("RiverRemStylePanel")

        self._rem = None
        self._hs = None
        self._dem = None
        self._loading = False  # guard so programmatic control updates don't fire handlers

        body = QWidget()
        self.setWidget(body)
        outer = QVBoxLayout(body)

        self._target_label = QLabel("No REM targeted yet.")
        self._target_label.setWordWrap(True)
        outer.addWidget(self._target_label)

        form = QFormLayout()
        outer.addLayout(form)

        # --- Palette dropdown (shows all options; no scrolling) ---------------
        self._palette = QComboBox()
        for name in styling.PALETTES.keys():
            self._palette.addItem(name)
        self._palette.setMaxVisibleItems(len(styling.PALETTES))
        idx = list(styling.PALETTES.keys()).index(styling.DEFAULT_PALETTE)
        self._palette.setCurrentIndex(idx)
        self._palette.currentIndexChanged.connect(self._on_palette)
        form.addRow("Palette:", self._palette)

        # --- Relief strength (hillshade Z exaggeration) ----------------------
        self._z_slider = QSlider(Qt.Horizontal)
        self._z_slider.setMinimum(0)      # 0.0
        self._z_slider.setMaximum(80)     # 8.0  (slider value / 10)
        self._z_spin = QDoubleSpinBox()
        self._z_spin.setRange(0.0, 8.0)
        self._z_spin.setSingleStep(0.1)
        self._z_spin.setDecimals(1)
        self._z_spin.setValue(styling.HILLSHADE_Z_FACTOR)
        self._z_slider.setValue(int(styling.HILLSHADE_Z_FACTOR * 10))
        # Keep slider<->spin in sync (no regen on every tick — that re-runs gdaldem).
        self._z_slider.valueChanged.connect(
            lambda v: self._z_spin.setValue(v / 10.0))
        self._z_spin.valueChanged.connect(
            lambda v: self._z_slider.setValue(int(round(v * 10))))
        # Regenerate the hillshade only when the user settles on a value.
        self._z_slider.sliderReleased.connect(self._on_z)
        self._z_spin.editingFinished.connect(self._on_z)
        z_row = QHBoxLayout()
        z_row.addWidget(self._z_slider)
        z_row.addWidget(self._z_spin)
        form.addRow("Relief strength (Z):", self._row_widget(z_row))

        # --- Shading opacity (instant) ---------------------------------------
        self._op_slider = QSlider(Qt.Horizontal)
        self._op_slider.setMinimum(0)
        self._op_slider.setMaximum(100)
        self._op_slider.setValue(100)
        self._op_value = QLabel("100%")
        self._op_slider.valueChanged.connect(self._on_opacity)
        op_row = QHBoxLayout()
        op_row.addWidget(self._op_slider)
        op_row.addWidget(self._op_value)
        form.addRow("Shading opacity:", self._row_widget(op_row))

        # --- Target refresh ---------------------------------------------------
        retarget = QPushButton("Target current REM layer")
        retarget.clicked.connect(self.refresh_target)
        outer.addWidget(retarget)

        outer.addStretch(1)

        self.refresh_target()

    # -- helpers --------------------------------------------------------------

    @staticmethod
    def _row_widget(layout):
        w = QWidget()
        layout.setContentsMargins(0, 0, 0, 0)
        w.setLayout(layout)
        return w

    def _enabled(self, on):
        for w in (self._palette, self._z_slider, self._z_spin,
                  self._op_slider):
            w.setEnabled(on)

    def refresh_target(self):
        """Re-resolve which REM we're controlling and sync the widgets to it."""
        self._rem, self._hs, self._dem = styling.find_current_rem(self.iface)
        if self._rem is None:
            self._target_label.setText(
                "No REM layer found. Run River REM, or select a REM layer, "
                "then click “Target current REM layer”.")
            self._enabled(False)
            return

        self._enabled(True)
        run = os.path.basename(os.path.dirname(self._rem.source().split("|")[0]))
        self._target_label.setText(
            "Styling: <b>{}</b><br><span style='color:#71726d'>{}</span>".format(
                self._rem.name(), run))

        # Sync opacity slider to the live hillshade.
        self._loading = True
        if self._hs is not None:
            self._op_slider.setValue(int(round(self._hs.opacity() * 100)))
        self._loading = False

    # -- live handlers --------------------------------------------------------

    def _on_palette(self, _idx):
        if self._loading or self._rem is None:
            return
        styling.apply_rem_pseudocolor(self._rem, palette=self._palette.currentText())
        self._refresh_canvas()

    def _on_z(self):
        if self._loading or self._dem is None:
            return
        z = float(self._z_spin.value())
        run_dir = os.path.dirname(self._dem)
        hs_path = styling._make_hillshade(
            self._dem, os.path.join(run_dir, "hillshade.tif"), z_factor=z)
        if not hs_path:
            return
        if self._hs is not None:
            # File overwritten in place — drop GDAL's cached data and repaint.
            self._hs.dataProvider().reloadData()
            self._hs.triggerRepaint()
        else:
            # No hillshade layer yet — add one beneath the REM.
            hs = QgsRasterLayer(hs_path, "hillshade")
            if hs.isValid():
                project = QgsProject.instance()
                root = project.layerTreeRoot()
                project.addMapLayer(hs, False)
                rem_node = root.findLayer(self._rem.id())
                parent = rem_node.parent() if rem_node else root
                pos = next((i + 1 for i, ch in enumerate(parent.children())
                            if ch == rem_node), 0)
                parent.insertLayer(pos, hs)
                self._hs = hs
        self._refresh_canvas()

    def _on_opacity(self, value):
        self._op_value.setText("{}%".format(value))
        if self._loading or self._hs is None:
            return
        self._hs.setOpacity(value / 100.0)
        self._hs.triggerRepaint()
        self._refresh_canvas()

    def _refresh_canvas(self):
        if self.iface is not None:
            self.iface.mapCanvas().refresh()
