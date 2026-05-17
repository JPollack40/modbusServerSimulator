"""
slave_dialog.py – Dialog for adding a Modbus Server (slave ID) to a TCP server.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFormLayout, QSpinBox,
    QVBoxLayout, QLabel, QCheckBox, QMessageBox,
)
from PySide6.QtCore import Qt


class SlaveDialog(QDialog):
    """
    Dialog to choose a Modbus Server ID (1–247) and optionally allow a
    duplicate ID to simulate an RS-485 address conflict.

    Attributes
    ----------
    slave_id : int
        The chosen Modbus slave / unit ID.
    allow_duplicate : bool
        True if the user explicitly opted to allow a duplicate ID (conflict
        simulation mode).

    Usage
    -----
    dlg = SlaveDialog(parent, existing_ids=[1, 2])
    if dlg.exec() == QDialog.Accepted:
        srv.add_slave(dlg.slave_id, allow_duplicate=dlg.allow_duplicate)
    """

    def __init__(
        self,
        parent=None,
        existing_ids: list[int] | None = None,
        current_id: int | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Add Modbus Server")
        self.setMinimumWidth(360)

        self._existing      = set(existing_ids or [])
        self.slave_id:       int  = 1
        self.allow_duplicate: bool = False

        layout = QVBoxLayout(self)

        info = QLabel(
            "Enter the Modbus Server ID for the new device.\n"
            "Valid range: 1 – 247."
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        form = QFormLayout()

        self._spin = QSpinBox()
        self._spin.setRange(1, 247)

        if current_id is not None:
            self._spin.setValue(current_id)
        else:
            # Pick the first unused ID by default
            for candidate in range(1, 248):
                if candidate not in self._existing:
                    self._spin.setValue(candidate)
                    break

        form.addRow("Modbus Server ID:", self._spin)
        layout.addLayout(form)

        # ── Conflict simulation checkbox ───────────────────────────────────────
        self._conflict_cb = QCheckBox(
            "Allow duplicate ID (simulate address conflict)"
        )
        self._conflict_cb.setToolTip(
            "When checked, this device will share its Modbus address with an\n"
            "existing device on the same TCP server.  This simulates the RS-485\n"
            "bus contention that occurs when two physical devices are accidentally\n"
            "assigned the same Modbus slave address.\n\n"
            "The simulator will XOR both devices' register values on each poll,\n"
            "and randomly drop ~15 % of responses — matching real-world collision\n"
            "symptoms (garbled data, intermittent timeouts)."
        )
        layout.addWidget(self._conflict_cb)

        # ── Buttons ───────────────────────────────────────────────────────────
        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel,
            Qt.Horizontal, self,
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    # ──────────────────────────────────────────────────────────────────────────

    def _on_accept(self):
        sid              = self._spin.value()
        allow_duplicate  = self._conflict_cb.isChecked()

        if sid in self._existing and not allow_duplicate:
            QMessageBox.warning(
                self,
                "Duplicate Modbus Server ID",
                f"Modbus Server ID {sid} already exists on this TCP server.\n\n"
                "Choose a different ID, or check\n"
                "\"Allow duplicate ID (simulate address conflict)\"\n"
                "to intentionally create a conflict.",
            )
            return

        if sid in self._existing and allow_duplicate:
            # Confirm the user understands what they're doing
            reply = QMessageBox.question(
                self,
                "Confirm Address Conflict",
                f"Modbus Server ID {sid} already exists on this TCP server.\n\n"
                "Adding a second device with the same ID will simulate an\n"
                "RS-485 address conflict: the master will receive corrupted\n"
                "data (XOR of both devices) and ~15 % of polls will time out.\n\n"
                "Proceed?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return

        self.slave_id        = sid
        self.allow_duplicate = allow_duplicate
        self.accept()
