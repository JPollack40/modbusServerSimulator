"""
server_dialog.py – Dialog for adding / editing a ServerConfig.

The IP-alias feature (netsh / ncpa.cpl) has been removed; users should
manage IP aliases manually via ncpa.cpl.  The NIC list refresh button (↺)
is retained so the dialog picks up newly added addresses without reopening.
"""

from __future__ import annotations

import socket

import psutil
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFormLayout, QLineEdit,
    QComboBox, QCheckBox, QPushButton, QHBoxLayout,
    QVBoxLayout, QSpinBox,
)
from PySide6.QtCore import Qt

from models.device_config import ServerConfig


def _get_nic_map() -> dict[str, str]:
    """Return {display_label: ip_address} for all IPv4 interfaces."""
    nics: dict[str, str] = {"All interfaces (0.0.0.0)": "0.0.0.0"}
    for iface, addrs in psutil.net_if_addrs().items():
        for addr in addrs:
            if addr.family == socket.AF_INET:
                nics[f"{iface}  ({addr.address})"] = addr.address
    return nics


# ══════════════════════════════════════════════════════════════════════════════
class ServerDialog(QDialog):
    """
    Modal dialog to create or edit a ServerConfig.

    Usage
    -----
    dlg = ServerDialog(parent, existing_config)   # edit
    dlg = ServerDialog(parent)                    # create new
    if dlg.exec() == QDialog.Accepted:
        cfg = dlg.result_config
    """

    def __init__(self, parent=None, config: ServerConfig | None = None):
        super().__init__(parent)
        self.setWindowTitle("Server Configuration")
        self.setMinimumWidth(420)

        self._nic_map = _get_nic_map()

        layout = QVBoxLayout(self)
        form   = QFormLayout()

        # ── Name ──────────────────────────────────────────────────────────────
        self._name_edit = QLineEdit(config.name if config else "New Server")
        form.addRow("Server Name:", self._name_edit)

        # ── NIC / IP ──────────────────────────────────────────────────────────
        nic_row = QHBoxLayout()
        self._nic_combo = QComboBox()
        self._nic_combo.addItems(self._nic_map.keys())
        if config:
            for label, ip in self._nic_map.items():
                if ip == config.host:
                    self._nic_combo.setCurrentText(label)
                    break
        nic_row.addWidget(self._nic_combo, stretch=1)

        self._refresh_btn = QPushButton("↺")
        self._refresh_btn.setFixedWidth(30)
        self._refresh_btn.setToolTip(
            "Refresh NIC list — click after adding an IP alias via ncpa.cpl"
        )
        self._refresh_btn.clicked.connect(self._refresh_nics)
        nic_row.addWidget(self._refresh_btn)

        form.addRow("Bind Address:", nic_row)

        # ── Port ──────────────────────────────────────────────────────────────
        self._port_spin = QSpinBox()
        self._port_spin.setRange(1, 65535)
        self._port_spin.setValue(config.port if config else 502)
        form.addRow("Port:", self._port_spin)

        # ── Zero-based ────────────────────────────────────────────────────────
        self._zero_cb = QCheckBox("Zero-Based Addressing")
        self._zero_cb.setChecked(config.zero_based if config else False)
        form.addRow("", self._zero_cb)

        layout.addLayout(form)

        # ── Buttons ───────────────────────────────────────────────────────────
        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel,
            Qt.Horizontal, self,
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.result_config: ServerConfig | None = None

    # ──────────────────────────────────────────────────────────────────────────

    def _refresh_nics(self):
        """Re-enumerate NICs and restore the previously selected IP if possible."""
        current_ip = self._nic_map.get(self._nic_combo.currentText(), "0.0.0.0")
        self._nic_map = _get_nic_map()
        self._nic_combo.clear()
        self._nic_combo.addItems(self._nic_map.keys())
        for label, ip in self._nic_map.items():
            if ip == current_ip:
                self._nic_combo.setCurrentText(label)
                break

    def _on_accept(self):
        name = self._name_edit.text().strip() or "Server"
        host = self._nic_map.get(self._nic_combo.currentText(), "0.0.0.0")
        port = self._port_spin.value()
        zero = self._zero_cb.isChecked()

        self.result_config = ServerConfig(
            name=name, host=host, port=port, zero_based=zero,
        )
        self.accept()
