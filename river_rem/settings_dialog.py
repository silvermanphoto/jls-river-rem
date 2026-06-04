# -*- coding: utf-8 -*-
"""Settings dialog + QSettings accessors for the River REM plugin.

Stores two things, both under QSettings("JoelSilverman", "RiverREM"):
  - opentopography/api_key        : the OpenTopography API key (masked in the UI)
  - centerline/use_selected_layer : bool, "use my selected line layer as centerline"

The API key is never logged or printed anywhere.
"""

from qgis.PyQt.QtCore import QSettings
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
)

# ---- QSettings identity (one place, reused by every accessor) ----------------
_ORG = "JoelSilverman"
_APP = "RiverREM"
_KEY_API = "opentopography/api_key"
_KEY_USE_SELECTED = "centerline/use_selected_layer"


def _settings():
    return QSettings(_ORG, _APP)


# ---- Module-level accessors (the documented API) -----------------------------

def get_api_key():
    """Return the stored OpenTopography API key (empty string if unset)."""
    return _settings().value(_KEY_API, "", type=str)


def set_api_key(value):
    """Persist the OpenTopography API key. Never logs the value."""
    _settings().setValue(_KEY_API, value or "")


def get_use_selected_layer():
    """Return True if the user opted to use their selected line layer as centerline."""
    return _settings().value(_KEY_USE_SELECTED, False, type=bool)


def set_use_selected_layer(flag):
    """Persist the 'use selected line layer as centerline' toggle."""
    _settings().setValue(_KEY_USE_SELECTED, bool(flag))


# ---- The dialog --------------------------------------------------------------

class SettingsDialog(QDialog):
    """Small modal dialog: masked API-key field + a centerline-override checkbox."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("River REM Settings")
        self.setMinimumWidth(440)

        outer = QVBoxLayout(self)

        intro = QLabel(
            "Set your OpenTopography API key. Get a free key at "
            "opentopography.org (a full-access key raises the rate limits). "
            "The key is stored locally in QSettings and is never logged."
        )
        intro.setWordWrap(True)
        outer.addWidget(intro)

        form = QFormLayout()

        # Masked API-key entry.
        self.api_key_edit = QLineEdit(self)
        self.api_key_edit.setEchoMode(QLineEdit.Password)
        self.api_key_edit.setPlaceholderText("OpenTopography API key")
        self.api_key_edit.setText(get_api_key())
        form.addRow("API key:", self.api_key_edit)

        # Centerline override toggle.
        self.use_selected_checkbox = QCheckBox(
            "Use my selected line layer as the river centerline", self
        )
        self.use_selected_checkbox.setChecked(get_use_selected_layer())
        form.addRow("", self.use_selected_checkbox)

        outer.addLayout(form)

        hint = QLabel(
            "When the checkbox is on, the active line layer is used as the "
            "centerline. When off, the plugin tries OpenStreetMap (Overpass) "
            "first, then a DEM-derived GRASS centerline."
        )
        hint.setWordWrap(True)
        outer.addWidget(hint)

        # OK / Cancel.
        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel, self
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    def _on_accept(self):
        """Persist both settings, then close. Strips surrounding whitespace from the key."""
        set_api_key(self.api_key_edit.text().strip())
        set_use_selected_layer(self.use_selected_checkbox.isChecked())
        self.accept()
