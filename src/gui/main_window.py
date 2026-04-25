"""
main_window.py – Modbus Server Simulator GUI
"""

from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QComboBox, QPushButton, QTableWidget,
    QTableWidgetItem, QHeaderView, QFileDialog, QCheckBox,
    QAbstractItemView, QApplication
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor

import socket
import psutil
import csv
import logging

from models.register_data import ModbusDataType, DataConverter, get_register_count

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────
NUM_ROWS   = 100
BOOL_TYPES = {"Coils", "Discrete Inputs"}
REG_TYPES  = {"Holding Registers", "Input Registers"}

# Colour used to grey-out slave rows
SLAVE_BG = QColor(220, 220, 220)
NORM_BG  = QColor(255, 255, 255)

# All data-type display strings (for register groups)
ALL_DTYPES = [t.value for t in ModbusDataType]


# ── Helper ─────────────────────────────────────────────────────────────────────
def _dtype_from_str(text: str) -> ModbusDataType:
    for t in ModbusDataType:
        if t.value == text:
            return t
    return ModbusDataType.UINT16


# ══════════════════════════════════════════════════════════════════════════════
class MainWindow(QMainWindow):
    """Main application window."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Modbus Server Simulator")
        self.resize(900, 650)

        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        layout = QVBoxLayout(main_widget)

        # ── Server settings bar ────────────────────────────────────────────
        settings_layout = QHBoxLayout()

        settings_layout.addWidget(QLabel("Slave Address:"))
        self.slave_address_input = QLineEdit("1")
        self.slave_address_input.setFixedWidth(50)
        settings_layout.addWidget(self.slave_address_input)

        settings_layout.addWidget(QLabel("NIC:"))
        self.nic_selector = QComboBox()
        self.nic_map = self._get_available_nics()
        self.nic_selector.addItems(self.nic_map.keys())
        settings_layout.addWidget(self.nic_selector)

        self.start_button = QPushButton("Start Server")
        self.start_button.setCheckable(True)
        self.start_button.clicked.connect(self.toggle_server)
        settings_layout.addWidget(self.start_button)

        self.save_button = QPushButton("Save Config")
        self.save_button.clicked.connect(self.save_config)
        settings_layout.addWidget(self.save_button)

        self.load_button = QPushButton("Load Config")
        self.load_button.clicked.connect(self.load_config)
        settings_layout.addWidget(self.load_button)

        self.zero_based_checkbox = QCheckBox("Zero-Based Addressing")
        self.zero_based_checkbox.stateChanged.connect(self._on_zero_based_changed)
        settings_layout.addWidget(self.zero_based_checkbox)

        layout.addLayout(settings_layout)

        # ── Register group selector ────────────────────────────────────────
        group_layout = QHBoxLayout()
        group_layout.addWidget(QLabel("Register Group:"))
        self.group_selector = QComboBox()
        self.group_selector.addItems(
            ["Coils", "Discrete Inputs", "Holding Registers", "Input Registers"]
        )
        self.group_selector.currentTextChanged.connect(self._switch_register_group)
        group_layout.addWidget(self.group_selector)
        group_layout.addStretch()
        layout.addLayout(group_layout)

        # ── In-memory data store ───────────────────────────────────────────
        # Each row dict:
        #   addr     : int   – 0-based row index
        #   type     : str   – ModbusDataType.value string
        #   val      : str   – user-entered value string
        #   slave_of : int|None – master row index if this row is a slave
        self.data: dict[str, list[dict]] = {
            "Coils": [
                {"addr": i, "type": "Boolean", "val": "False", "slave_of": None}
                for i in range(NUM_ROWS)
            ],
            "Discrete Inputs": [
                {"addr": i, "type": "Boolean", "val": "False", "slave_of": None}
                for i in range(NUM_ROWS)
            ],
            "Holding Registers": [
                {"addr": i, "type": ModbusDataType.UINT16.value, "val": "0", "slave_of": None}
                for i in range(NUM_ROWS)
            ],
            "Input Registers": [
                {"addr": i, "type": ModbusDataType.UINT16.value, "val": "0", "slave_of": None}
                for i in range(NUM_ROWS)
            ],
        }
        self.current_group = "Coils"

        # ── Table (3 columns – Address, Data Type, Value) ──────────────────
        self.table = QTableWidget(NUM_ROWS, 3)
        self.table.setHorizontalHeaderLabels(["Address", "Data Type", "Value"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        layout.addWidget(self.table)

        # Populate initial view
        self._load_table_data()

    # ══════════════════════════════════════════════════════════════════════════
    # Address helpers
    # ══════════════════════════════════════════════════════════════════════════

    def _get_display_address(self, addr: int) -> int:
        is_zero = self.zero_based_checkbox.isChecked()
        offsets = {
            "Coils":             0      if is_zero else 1,
            "Discrete Inputs":   10000  if is_zero else 10001,
            "Holding Registers": 40000  if is_zero else 40001,
            "Input Registers":   30000  if is_zero else 30001,
        }
        return offsets[self.current_group] + addr

    # ══════════════════════════════════════════════════════════════════════════
    # Table population
    # ══════════════════════════════════════════════════════════════════════════

    def _load_table_data(self):
        """Rebuild the entire table for the current register group."""
        self.table.blockSignals(True)

        for i in range(NUM_ROWS):
            item = self.data[self.current_group][i]

            # ── Address cell (read-only item) ──────────────────────────────
            addr_item = QTableWidgetItem(str(self._get_display_address(item["addr"])))
            addr_item.setFlags(addr_item.flags() & ~Qt.ItemIsEditable)
            self.table.setItem(i, 0, addr_item)

            # ── Remove old widgets ─────────────────────────────────────────
            for col in (1, 2):
                old = self.table.cellWidget(i, col)
                if old:
                    old.deleteLater()
                self.table.setCellWidget(i, col, None)
                self.table.setItem(i, col, None)

            # ── Boolean groups (Coils / Discrete Inputs) ───────────────────
            if self.current_group in BOOL_TYPES:
                combo = QComboBox()
                combo.addItems(["Boolean"])
                combo.setEnabled(False)
                self.table.setCellWidget(i, 1, combo)

                val = item["val"] == "True"
                cb = QCheckBox()
                cb.setChecked(val)
                # Capture group AND row at connection time
                cb.stateChanged.connect(
                    lambda state, row=i, grp=self.current_group:
                        self._update_bool_val(row, state, grp)
                )
                self.table.setCellWidget(i, 2, cb)

            # ── Register groups (Holding / Input) ──────────────────────────
            else:
                is_slave = item["slave_of"] is not None

                # Data-type combo
                combo = QComboBox()
                combo.addItems(ALL_DTYPES)
                combo.setCurrentText(item["type"])
                combo.setEnabled(not is_slave)
                if is_slave:
                    combo.setStyleSheet("background-color: #dcdcdc;")
                else:
                    combo.currentTextChanged.connect(
                        lambda text, row=i, grp=self.current_group:
                            self._on_dtype_changed(row, text, grp)
                    )
                self.table.setCellWidget(i, 1, combo)

                # Value editor
                if is_slave:
                    placeholder = QLineEdit("—")
                    placeholder.setEnabled(False)
                    placeholder.setStyleSheet("background-color: #dcdcdc; color: #888;")
                    self.table.setCellWidget(i, 2, placeholder)
                else:
                    val_edit = QLineEdit(item["val"])
                    val_edit.textChanged.connect(
                        lambda text, row=i, grp=self.current_group:
                            self._update_reg_val(row, text, grp)
                    )
                    self.table.setCellWidget(i, 2, val_edit)

        self.table.blockSignals(False)

    # ══════════════════════════════════════════════════════════════════════════
    # Data-type change handler (register groups only)
    # ══════════════════════════════════════════════════════════════════════════

    def _on_dtype_changed(self, master_row: int, new_type_str: str, group: str):
        """
        Called when the user changes the data-type combo for a master row.
        Recalculates which subsequent rows become slaves (greyed) or are freed.
        """
        if group != self.current_group:
            return  # stale signal from a previous group view

        dtype     = _dtype_from_str(new_type_str)
        reg_count = get_register_count(dtype)

        # Save the new type into the data model
        self.data[group][master_row]["type"] = new_type_str

        # ── First, free any rows that were previously owned by this master ──
        for r in range(NUM_ROWS):
            if self.data[group][r]["slave_of"] == master_row:
                self.data[group][r]["slave_of"] = None
                self.data[group][r]["type"]     = ModbusDataType.UINT16.value
                self.data[group][r]["val"]      = "0"

        # ── Now claim the rows this type needs ────────────────────────────
        for offset in range(1, reg_count):
            slave_row = master_row + offset
            if slave_row >= NUM_ROWS:
                break
            # Only claim if the row isn't already a master of something else
            if self.data[group][slave_row]["slave_of"] is None:
                # Check it's not itself a master with slaves
                has_own_slaves = any(
                    self.data[group][r]["slave_of"] == slave_row
                    for r in range(NUM_ROWS)
                )
                if not has_own_slaves:
                    self.data[group][slave_row]["slave_of"] = master_row

        # Rebuild the table to reflect the new slave/master layout
        self._load_table_data()

        # Push the current value to the server if running
        val_str = self.data[group][master_row]["val"]
        self._push_reg_to_server(group, master_row, val_str, new_type_str)

    # ══════════════════════════════════════════════════════════════════════════
    # Value-update handlers
    # ══════════════════════════════════════════════════════════════════════════

    def _update_bool_val(self, row: int, state: int, group: str):
        """Handle checkbox state change for Coils / Discrete Inputs."""
        val = (state == Qt.Checked.value) or (state == 2)
        self.data[group][row]["val"] = str(val)
        if hasattr(self, "server"):
            reg_type = self._map_group_to_type(group)
            self.server.update_register(reg_type, row, int(val))

    def _update_reg_val(self, row: int, text: str, group: str):
        """Handle text change in a register value QLineEdit."""
        self.data[group][row]["val"] = text
        dtype_str = self.data[group][row]["type"]
        self._push_reg_to_server(group, row, text, dtype_str)

    def _push_reg_to_server(self, group: str, row: int, val_str: str, dtype_str: str):
        """Convert val_str to raw register words and write to the live server."""
        if not hasattr(self, "server"):
            return
        reg_type = self._map_group_to_type(group)
        dtype    = _dtype_from_str(dtype_str)
        try:
            raw_regs = DataConverter.to_registers(val_str, dtype)
            if len(raw_regs) == 1:
                self.server.update_register(reg_type, row, raw_regs[0])
            else:
                self.server.update_registers(reg_type, row, raw_regs)
        except Exception as e:
            logger.debug(f"_push_reg_to_server: {e}")

    # ══════════════════════════════════════════════════════════════════════════
    # Group switching
    # ══════════════════════════════════════════════════════════════════════════

    def _switch_register_group(self, group: str):
        """Save current table state then switch to the new group."""
        self._save_current_table_to_model()
        self.current_group = group
        self._load_table_data()

    def _save_current_table_to_model(self):
        """Persist widget values back into self.data for the current group."""
        grp = self.current_group
        for i in range(NUM_ROWS):
            if grp in BOOL_TYPES:
                cb = self.table.cellWidget(i, 2)
                if isinstance(cb, QCheckBox):
                    self.data[grp][i]["val"] = str(cb.isChecked())
            else:
                # Only save master rows; slave rows are derived
                if self.data[grp][i]["slave_of"] is None:
                    le = self.table.cellWidget(i, 2)
                    if isinstance(le, QLineEdit):
                        self.data[grp][i]["val"] = le.text()
                    combo = self.table.cellWidget(i, 1)
                    if isinstance(combo, QComboBox):
                        self.data[grp][i]["type"] = combo.currentText()

    # ══════════════════════════════════════════════════════════════════════════
    # Server start / stop
    # ══════════════════════════════════════════════════════════════════════════

    def toggle_server(self, checked: bool):
        from modbus.server_wrapper import ModbusServer

        # Save whatever is currently visible before starting
        self._save_current_table_to_model()

        if checked:
            slave_id    = int(self.slave_address_input.text())
            host        = self._get_selected_nic_ip()
            is_zero     = self.zero_based_checkbox.isChecked()

            self.server = ModbusServer(slave_id=slave_id, host=host, zero_based=is_zero)

            # ── Populate all register groups ───────────────────────────────
            for group, items in self.data.items():
                reg_type = self._map_group_to_type(group)

                if group in BOOL_TYPES:
                    # Simple boolean list
                    values = [
                        1 if item["val"] == "True" else 0
                        for item in items
                    ]
                    self.server.set_initial_values(reg_type, values)
                else:
                    # Build a flat 100-element list of 16-bit words.
                    # Master rows expand to their register count; slave rows
                    # are skipped (already written by their master).
                    flat = [0] * NUM_ROWS
                    skip_until = -1
                    for row, item in enumerate(items):
                        if row <= skip_until:
                            continue  # slave row – already handled
                        if item["slave_of"] is not None:
                            continue  # orphaned slave (shouldn't happen)

                        dtype    = _dtype_from_str(item["type"])
                        reg_cnt  = get_register_count(dtype)
                        raw_regs = DataConverter.to_registers(item["val"], dtype)

                        for offset, word in enumerate(raw_regs):
                            if row + offset < NUM_ROWS:
                                flat[row + offset] = word

                        skip_until = row + reg_cnt - 1

                    self.server.set_initial_values(reg_type, flat)

            self.server.start()
            self.start_button.setText("Stop Server")
        else:
            if hasattr(self, "server"):
                self.server.stop()
                self.server.join(timeout=2.0)
            self.start_button.setText("Start Server")

    # ══════════════════════════════════════════════════════════════════════════
    # Zero-based addressing toggle
    # ══════════════════════════════════════════════════════════════════════════

    def _on_zero_based_changed(self, state: int):
        if hasattr(self, "server"):
            self.server.set_zero_based(self.zero_based_checkbox.isChecked())
        self._load_table_data()

    # ══════════════════════════════════════════════════════════════════════════
    # Utilities
    # ══════════════════════════════════════════════════════════════════════════

    @staticmethod
    def _map_group_to_type(group: str) -> str:
        return {
            "Coils":             "coils",
            "Discrete Inputs":   "discrete_inputs",
            "Holding Registers": "holding_registers",
            "Input Registers":   "input_registers",
        }[group]

    def _get_available_nics(self) -> dict:
        nics = {"All (0.0.0.0)": "0.0.0.0"}
        for interface, addrs in psutil.net_if_addrs().items():
            for addr in addrs:
                if addr.family == socket.AF_INET:
                    nics[f"{interface} ({addr.address})"] = addr.address
        return nics

    def _get_selected_nic_ip(self) -> str:
        return self.nic_map.get(self.nic_selector.currentText(), "0.0.0.0")

    # ══════════════════════════════════════════════════════════════════════════
    # Save / Load configuration
    # ══════════════════════════════════════════════════════════════════════════

    def save_config(self):
        """Save all register data to a CSV file."""
        self._save_current_table_to_model()
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Configuration", "", "CSV Files (*.csv)"
        )
        if not path:
            return
        with open(path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["Group", "Address", "Data Type", "Value", "SlaveOf"])
            for group, items in self.data.items():
                for item in items:
                    writer.writerow([
                        group,
                        item["addr"],
                        item["type"],
                        item["val"],
                        "" if item["slave_of"] is None else item["slave_of"],
                    ])

    def load_config(self):
        """Load register data from a CSV file."""
        path, _ = QFileDialog.getOpenFileName(
            self, "Load Configuration", "", "CSV Files (*.csv)"
        )
        if not path:
            return
        with open(path, "r") as f:
            reader = csv.reader(f)
            header = next(reader)
            has_slave_col = "SlaveOf" in header
            for row in reader:
                if len(row) < 4:
                    continue
                group, addr_str, dtype, val = row[0], row[1], row[2], row[3]
                slave_of = None
                if has_slave_col and len(row) >= 5 and row[4].strip():
                    try:
                        slave_of = int(row[4])
                    except ValueError:
                        pass
                if group not in self.data:
                    continue
                for item in self.data[group]:
                    if str(item["addr"]) == addr_str:
                        item["type"]     = dtype
                        item["val"]      = val
                        item["slave_of"] = slave_of
                        break
        self._load_table_data()
