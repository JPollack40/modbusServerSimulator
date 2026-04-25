"""
server_dialog.py – Dialog for adding / editing a ServerConfig.
Also contains the IP-alias helper dialog.
"""

from __future__ import annotations
import subprocess
import socket

import psutil
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFormLayout, QLineEdit,
    QComboBox, QCheckBox, QLabel, QPushButton, QHBoxLayout,
    QVBoxLayout, QMessageBox, QSpinBox
)
from PySide6.QtCore import Qt

from models.device_config import ServerConfig


def _get_nic_map() -> dict[str, str]:
    """Return {display_label: ip_address} for all IPv4 interfaces."""
    nics = {"All interfaces (0.0.0.0)": "0.0.0.0"}
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
            # Select the entry whose IP matches
            for label, ip in self._nic_map.items():
                if ip == config.host:
                    self._nic_combo.setCurrentText(label)
                    break
        nic_row.addWidget(self._nic_combo, stretch=1)

        self._refresh_btn = QPushButton("↺")
        self._refresh_btn.setFixedWidth(30)
        self._refresh_btn.setToolTip("Refresh NIC list")
        self._refresh_btn.clicked.connect(self._refresh_nics)
        nic_row.addWidget(self._refresh_btn)

        self._alias_btn = QPushButton("Add IP Alias…")
        self._alias_btn.clicked.connect(self._open_alias_dialog)
        nic_row.addWidget(self._alias_btn)

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
            Qt.Horizontal, self
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.result_config: ServerConfig | None = None

    # ──────────────────────────────────────────────────────────────────────────

    def _refresh_nics(self):
        current_ip = self._nic_map.get(self._nic_combo.currentText(), "0.0.0.0")
        self._nic_map = _get_nic_map()
        self._nic_combo.clear()
        self._nic_combo.addItems(self._nic_map.keys())
        # Try to restore previous selection
        for label, ip in self._nic_map.items():
            if ip == current_ip:
                self._nic_combo.setCurrentText(label)
                break

    def _open_alias_dialog(self):
        dlg = IPAliasDialog(self)
        if dlg.exec() == QDialog.Accepted:
            self._refresh_nics()

    def _on_accept(self):
        name = self._name_edit.text().strip() or "Server"
        host = self._nic_map.get(self._nic_combo.currentText(), "0.0.0.0")
        port = self._port_spin.value()
        zero = self._zero_cb.isChecked()

        self.result_config = ServerConfig(
            name=name, host=host, port=port, zero_based=zero
        )
        self.accept()


# ══════════════════════════════════════════════════════════════════════════════
class IPAliasDialog(QDialog):
    """
    Helper dialog to add or remove an IP alias on a Windows NIC using netsh.
    Requires Administrator privileges.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Manage IP Aliases")
        self.setMinimumWidth(480)

        layout = QVBoxLayout(self)

        info = QLabel(
            "<b>Add or remove an IP alias on a network adapter.</b><br>"
            "This calls <code>netsh interface ip add/delete address</code> "
            "and requires <b>Administrator privileges</b>.<br>"
            "Use this to assign additional IP addresses to a NIC so each "
            "Modbus server can bind to a unique IP on port 502."
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        form = QFormLayout()

        # Adapter selector
        self._adapter_combo = QComboBox()
        self._adapters = self._get_adapters()
        self._adapter_combo.addItems(self._adapters)
        form.addRow("Adapter:", self._adapter_combo)

        # IP address
        self._ip_edit = QLineEdit()
        self._ip_edit.setPlaceholderText("e.g. 192.168.1.100")
        form.addRow("IP Address:", self._ip_edit)

        # Subnet mask
        self._mask_edit = QLineEdit("255.255.255.0")
        form.addRow("Subnet Mask:", self._mask_edit)

        layout.addLayout(form)

        # Buttons
        btn_row = QHBoxLayout()
        self._add_btn = QPushButton("Add Alias")
        self._add_btn.clicked.connect(self._add_alias)
        btn_row.addWidget(self._add_btn)

        self._del_btn = QPushButton("Remove Alias")
        self._del_btn.clicked.connect(self._remove_alias)
        btn_row.addWidget(self._del_btn)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(close_btn)

        layout.addLayout(btn_row)

    # ──────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _get_adapters() -> list[str]:
        adapters = []
        for iface in psutil.net_if_addrs():
            adapters.append(iface)
        return adapters

    def _run_netsh(self, args: list[str]) -> tuple[bool, str]:
        """Run a netsh command; return (success, output)."""
        cmd = ["netsh", "interface", "ip"] + args
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=10,
            )
            output = (result.stdout + result.stderr).strip()
            return result.returncode == 0, output
        except FileNotFoundError:
            return False, "netsh not found (Windows only)"
        except subprocess.TimeoutExpired:
            return False, "netsh timed out"
        except Exception as e:
            return False, str(e)

    def _validate_ip(self, ip: str) -> bool:
        try:
            socket.inet_aton(ip)
            return True
        except socket.error:
            return False

    def _add_alias(self):
        adapter = self._adapter_combo.currentText()
        ip      = self._ip_edit.text().strip()
        mask    = self._mask_edit.text().strip()

        if not self._validate_ip(ip):
            QMessageBox.warning(self, "Invalid IP", f"'{ip}' is not a valid IPv4 address.")
            return
        if not self._validate_ip(mask):
            QMessageBox.warning(self, "Invalid Mask", f"'{mask}' is not a valid subnet mask.")
            return

        ok, out = self._run_netsh([
            "add", "address",
            f'name="{adapter}"',
            f"addr={ip}",
            f"mask={mask}",
        ])
        if ok:
            QMessageBox.information(
                self, "Success",
                f"IP alias {ip}/{mask} added to '{adapter}'.\n\n"
                "Click the ↺ button in the Server dialog to refresh the NIC list."
            )
        else:
            QMessageBox.critical(
                self, "Error",
                f"Failed to add alias:\n{out}\n\n"
                "Make sure the application is running as Administrator."
            )

    def _remove_alias(self):
        adapter = self._adapter_combo.currentText()
        ip      = self._ip_edit.text().strip()

        if not self._validate_ip(ip):
            QMessageBox.warning(self, "Invalid IP", f"'{ip}' is not a valid IPv4 address.")
            return

        ok, out = self._run_netsh([
            "delete", "address",
            f'name="{adapter}"',
            f"addr={ip}",
        ])
        if ok:
            QMessageBox.information(
                self, "Success",
                f"IP alias {ip} removed from '{adapter}'."
            )
        else:
            QMessageBox.critical(
                self, "Error",
                f"Failed to remove alias:\n{out}\n\n"
                "Make sure the application is running as Administrator."
            )
