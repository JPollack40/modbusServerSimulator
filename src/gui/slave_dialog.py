"""
slave_dialog.py – Dialog for adding a slave ID to a server.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFormLayout, QSpinBox,
    QVBoxLayout, QLabel
)
from PySide6.QtCore import Qt


class SlaveDialog(QDialog):
    """
    Simple dialog to choose a Modbus slave ID (1-247).

    Usage
    -----
    dlg = SlaveDialog(parent, existing_ids=[1, 2])
    if dlg.exec() == QDialog.Accepted:
        slave_id = dlg.slave_id
    """

    def __init__(self, parent=None, existing_ids: list[int] | None = None,
                 current_id: int | None = None):
        super().__init__(parent)
        self.setWindowTitle("Add Slave Device")
        self.setMinimumWidth(280)

        self._existing = set(existing_ids or [])
        self.slave_id: int = 1

        layout = QVBoxLayout(self)

        info = QLabel(
            "Enter the Modbus slave ID for the new device.\n"
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
            # Pick the first unused ID
            for candidate in range(1, 248):
                if candidate not in self._existing:
                    self._spin.setValue(candidate)
                    break

        form.addRow("Slave ID:", self._spin)
        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel,
            Qt.Horizontal, self
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _on_accept(self):
        sid = self._spin.value()
        if sid in self._existing:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.warning(
                self, "Duplicate Slave ID",
                f"Slave ID {sid} already exists on this server.\n"
                "Please choose a different ID."
            )
            return
        self.slave_id = sid
        self.accept()
