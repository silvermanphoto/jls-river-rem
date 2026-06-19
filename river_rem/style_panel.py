"""style_panel.py — live REM styling dock.

Restyles the REM already loaded in the project, on the fly, with no re-download:

  - Palette dropdown      -> re-applies the color ramp instantly.
  - Near-river emphasis    -> compresses the color range toward the channel
                             (lowers the height that maps to the top color);
                             instant.
  - Relief strength (Z)    -> hillshade vertical exaggeration; regenerates the
                             hillshade on release (re-runs gdaldem).
  - Sun direction (azimuth)-> hillshade light azimuth; regenerates on release.
  - Shading opacity        -> hillshade layer opacity; instant.

Targets the active REM (or the most recent); click "Target current REM layer"
after selecting a different REM in the Layers panel.
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
        self._data_max = None   # REM max (m), for the emphasis->vmax mapping
        self._loading = False   # guard so programmatic syncs don't fire handlers

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
        self._palette.setMaxVisibleItems(len(styling.PALETTES) + 1)
        self._select_combo(self._palette, styling.DEFAULT_PALETTE)
        self._palette.currentIndexChanged.connect(self._on_style_change)
        form.addRow("Palette:", self._palette)

        # --- Near-river emphasis (0 = full range, 100 = hug the channel) ------
        self._emph = QSlider(Qt.Horizontal)
        self._emph.setRange(0, 100)
        self._emph.setValue(0)
        self._emph_label = QLabel("0%")
        self._emph.valueChanged.connect(self._on_emph)
        form.addRow("Near-river emphasis:", self._slider_row(self._emph, self._emph_label))

        # --- Relief strength (hillshade Z exaggeration) ----------------------
        self._z_slider = QSlider(Qt.Horizontal)
        self._z_slider.setRange(0, 80)   # value/10 -> 0.0 .. 8.0
        self._z_spin = QDoubleSpinBox()
        self._z_spin.setRange(0.0, 8.0)
        self._z_spin.setSingleStep(0.1)
        self._z_spin.setDecimals(1)
        self._z_spin.setValue(styling.HILLSHADE_Z_FACTOR)
        self._z_slider.setValue(int(styling.HILLSHADE_Z_FACTOR * 10))
        self._z_slider.valueChanged.connect(lambda v: self._z_spin.setValue(v / 10.0))
        self._z_spin.valueChanged.connect(lambda v: self._z_slider.setValue(int(round(v * 10))))
        self._z_slider.sliderReleased.connect(self._on_shade_change)
        self._z_spin.editingFinished.connect(self._on_shade_change)
        z_row = QHBoxLayout()
        z_row.addWidget(self._z_slider)
        z_row.addWidget(self._z_spin)
        form.addRow("Relief strength (Z):", self._wrap(z_row))

        # --- Sun direction (azimuth) -----------------------------------------
        self._az = QSlider(Qt.Horizontal)
        self._az.setRange(0, 360)
        self._az.setValue(int(styling.HILLSHADE_AZIMUTH))
        self._az_label = QLabel("%d°" % int(styling.HILLSHADE_AZIMUTH))
        self._az.valueChanged.connect(lambda v: self._az_label.setText("%d°" % v))
        self._az.sliderReleased.connect(self._on_shade_change)
        form.addRow("Sun direction:", self._slider_row(self._az, self._az_label))

        # --- Shading opacity (instant) ---------------------------------------
        self._op = QSlider(Qt.Horizontal)
        self._op.setRange(0, 100)
        self._op.setValue(100)
        self._op_label = QLabel("100%")
        self._op.valueChanged.connect(self._on_opacity)
        form.addRow("Shading opacity:", self._slider_row(self._op, self._op_label))

        # --- Target refresh ---------------------------------------------------
        retarget = QPushButton("Target current REM layer")
        retarget.clicked.connect(self.refresh_target)
        outer.addWidget(retarget)
        outer.addStretch(1)

        self.refresh_target()

    # -- small UI helpers -----------------------------------------------------

    @staticmethod
    def _wrap(layout):
        w = QWidget()
        layout.setContentsMargins(0, 0, 0, 0)
        w.setLayout(layout)
        return w

    def _slider_row(self, slider, label):
        row = QHBoxLayout()
        row.addWidget(slider)
        label.setMinimumWidth(42)
        row.addWidget(label)
        return self._wrap(row)

    @staticmethod
    def _select_combo(combo, text):
        i = combo.findText(text)
        if i >= 0:
            combo.setCurrentIndex(i)

    def _enabled(self, on):
        for w in (self._palette, self._emph, self._z_slider, self._z_spin,
                  self._az, self._op):
            w.setEnabled(on)

    # -- target -----------------------------------------------------------------

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
        _, dmax = styling._raster_min_max(self._rem.source().split("|")[0])
        self._data_max = dmax if (dmax and dmax > 0) else 12.0

        run = os.path.basename(os.path.dirname(self._rem.source().split("|")[0]))
        self._target_label.setText(
            "Styling: <b>{}</b><br><span style='color:#71726d'>{}</span>".format(
                self._rem.name(), run))

        self._loading = True
        if self._hs is not None:
            self._op.setValue(int(round(self._hs.opacity() * 100)))
        self._loading = False

    # -- value mapping ----------------------------------------------------------

    def _emphasis_vmax(self):
        """Map the emphasis slider (0..100) to the ramp's top height (metres).

        0   -> full data range (top color = the REM's max height).
        100 -> hug the channel (top color reached by ~0.5 m above the river).
        """
        e = self._emph.value() / 100.0
        dmax = self._data_max or 12.0
        return max(0.5, dmax * (1.0 - e) ** 1.5)

    # -- live handlers ----------------------------------------------------------

    def _on_style_change(self, *_):
        """Palette change -> re-apply the colour ramp (instant)."""
        self._restyle()

    def _on_emph(self, value):
        self._emph_label.setText("%d%%" % value)
        if self._loading:
            return
        self._restyle()

    def _restyle(self):
        if self._loading or self._rem is None:
            return
        styling.apply_rem_pseudocolor(
            self._rem,
            palette=self._palette.currentText(),
            vmax=self._emphasis_vmax(),
        )
        self._refresh_canvas()

    def _on_shade_change(self):
        """Z or azimuth changed -> regenerate the hillshade (gdaldem)."""
        if self._loading or self._dem is None:
            return
        z = float(self._z_spin.value())
        az = float(self._az.value())
        run_dir = os.path.dirname(self._dem)
        hs_path = styling._make_hillshade(
            self._dem, os.path.join(run_dir, "hillshade.tif"),
            z_factor=z, azimuth=az)
        if not hs_path:
            return
        if self._hs is not None:
            self._hs.dataProvider().reloadData()
            self._hs.triggerRepaint()
        else:
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
        self._op_label.setText("%d%%" % value)
        if self._loading or self._hs is None:
            return
        self._hs.setOpacity(value / 100.0)
        self._hs.triggerRepaint()
        self._refresh_canvas()

    def _refresh_canvas(self):
        if self.iface is not None:
            self.iface.mapCanvas().refresh()
