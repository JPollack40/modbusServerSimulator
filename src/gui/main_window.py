"""
main_window.py – Modbus Server Simulator GUI (multi-server / multi-slave)
"""

from __future__ import annotations

import csv
import logging

from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QComboBox, QPushButton, QTableWidget,
    QTableWidgetItem, QHeaderView, QFileDialog, QCheckBox,
    QAbstractItemView, QSplitter, QTreeWidget, QTreeWidgetItem,
    QMessageBox, QMenu, QToolBar, QSizePolicy
)
from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QColor, QAction, QIcon, QFont

from models.register_data import ModbusDataType, DataConverter, get_register_count
from models.device_config import Project, ServerConfig, SlaveConfig, NUM_ROWS
from gui.server_dialog import ServerDialog
from gui.slave_dialog import SlaveDialog

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────
BOOL_TYPES = {"Coils", "Discrete Inputs"}
REG_TYPES  = {"Holding Registers", "Input Registers"}
ALL_DTYPES = [t.value for t in ModbusDataType]

SLAVE_BG = QColor(220, 220, 220)
NORM_BG  = QColor(255, 255, 255)

# Tree item user-data roles
ROLE_SERVER = Qt.UserRole
ROLE_SLAVE  = Qt.UserRole + 1

# Status colours
COLOR_RUNNING = QColor(0, 160, 0)
COLOR_STOPPED = QColor(160, 0, 0)


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
        self.resize(1200, 720)

        # ── Project model ──────────────────────────────────────────────────
        self.project = Project()

        # Runtime: ServerConfig → ModbusServer thread
        self._running_servers: dict[int, object] = {}   # id(ServerConfig) → ModbusServer

        # Currently selected (server, slave) for the register editor
        self._active_server: ServerConfig | None = None
        self._active_slave:  SlaveConfig  | None = None
        self._current_group: str = "Coils"

        # ── Toolbar ────────────────────────────────────────────────────────
        tb = QToolBar("Project")
        tb.setIconSize(QSize(16, 16))
        self.addToolBar(tb)

        act_new  = QAction("New Project",  self); act_new.triggered.connect(self._new_project)
        act_save = QAction("Save Project", self); act_save.triggered.connect(self._save_project)
        act_load = QAction("Load Project", self); act_load.triggered.connect(self._load_project)
        tb.addAction(act_new)
        tb.addAction(act_save)
        tb.addAction(act_load)
        tb.addSeparator()

        add_srv_btn = QPushButton("＋ Add Server")
        add_srv_btn.clicked.connect(self._add_server)
        tb.addWidget(add_srv_btn)

        # ── Central splitter ───────────────────────────────────────────────
        splitter = QSplitter(Qt.Horizontal)
        self.setCentralWidget(splitter)

        # ── Left: project tree ─────────────────────────────────────────────
        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(4, 4, 4, 4)

        tree_label = QLabel("<b>Project Tree</b>")
        left_layout.addWidget(tree_label)

        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._tree_context_menu)
        self.tree.currentItemChanged.connect(self._on_tree_selection_changed)
        left_layout.addWidget(self.tree)

        left_panel.setMinimumWidth(220)
        left_panel.setMaximumWidth(340)
        splitter.addWidget(left_panel)

        # ── Right: register editor ─────────────────────────────────────────
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(4, 4, 4, 4)

        # Context bar (shows which device is being edited)
        self._context_label = QLabel("<i>Select a slave device in the tree to edit its registers.</i>")
        self._context_label.setWordWrap(True)
        right_layout.addWidget(self._context_label)

        # Server controls bar (shown when a slave is selected)
        ctrl_bar = QHBoxLayout()

        self._start_btn = QPushButton("Start Server")
        self._start_btn.setCheckable(True)
        self._start_btn.setEnabled(False)
        self._start_btn.clicked.connect(self._toggle_server)
        ctrl_bar.addWidget(self._start_btn)

        self._zero_cb = QCheckBox("Zero-Based Addressing")
        self._zero_cb.setEnabled(False)
        self._zero_cb.stateChanged.connect(self._on_zero_based_changed)
        ctrl_bar.addWidget(self._zero_cb)

        ctrl_bar.addStretch()

        self._save_srv_btn = QPushButton("Save Server…")
        self._save_srv_btn.setEnabled(False)
        self._save_srv_btn.clicked.connect(self._save_server_config)
        ctrl_bar.addWidget(self._save_srv_btn)

        self._load_srv_btn = QPushButton("Load Server…")
        self._load_srv_btn.setEnabled(False)
        self._load_srv_btn.clicked.connect(self._load_server_config)
        ctrl_bar.addWidget(self._load_srv_btn)

        self._save_slave_btn = QPushButton("Save Slave CSV…")
        self._save_slave_btn.setEnabled(False)
        self._save_slave_btn.clicked.connect(self._save_slave_csv)
        ctrl_bar.addWidget(self._save_slave_btn)

        self._load_slave_btn = QPushButton("Load Slave CSV…")
        self._load_slave_btn.setEnabled(False)
        self._load_slave_btn.clicked.connect(self._load_slave_csv)
        ctrl_bar.addWidget(self._load_slave_btn)

        right_layout.addLayout(ctrl_bar)

        # Register group selector
        grp_bar = QHBoxLayout()
        grp_bar.addWidget(QLabel("Register Group:"))
        self._group_combo = QComboBox()
        self._group_combo.addItems(["Coils", "Discrete Inputs", "Holding Registers", "Input Registers"])
        self._group_combo.setEnabled(False)
        self._group_combo.currentTextChanged.connect(self._switch_register_group)
        grp_bar.addWidget(self._group_combo)
        grp_bar.addStretch()
        right_layout.addLayout(grp_bar)

        # Register table
        self.table = QTableWidget(NUM_ROWS, 3)
        self.table.setHorizontalHeaderLabels(["Address", "Data Type", "Value"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setEnabled(False)
        right_layout.addWidget(self.table)

        splitter.addWidget(right_panel)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)

        # Start with one default server + slave so the app is immediately usable
        self._create_default_project()

    # ══════════════════════════════════════════════════════════════════════════
    # Default project
    # ══════════════════════════════════════════════════════════════════════════

    def _create_default_project(self):
        srv = ServerConfig(name="Server 1", host="0.0.0.0", port=502)
        srv.add_slave(1)
        self.project.add_server(srv)
        self._rebuild_tree()
        # Select the first slave automatically
        root = self.tree.topLevelItem(0)
        if root and root.childCount() > 0:
            self.tree.setCurrentItem(root.child(0))

    # ══════════════════════════════════════════════════════════════════════════
    # Tree management
    # ══════════════════════════════════════════════════════════════════════════

    def _rebuild_tree(self):
        """Rebuild the entire project tree from self.project."""
        self.tree.blockSignals(True)
        self.tree.clear()

        for srv in self.project.servers:
            srv_item = self._make_server_item(srv)
            self.tree.addTopLevelItem(srv_item)
            for slave in srv.slaves:
                slave_item = self._make_slave_item(slave)
                srv_item.addChild(slave_item)
            srv_item.setExpanded(True)

        self.tree.blockSignals(False)

    def _make_server_item(self, srv: ServerConfig) -> QTreeWidgetItem:
        label = f"🖥  {srv.name}  [{srv.host}:{srv.port}]"
        item = QTreeWidgetItem([label])
        item.setData(0, ROLE_SERVER, srv)
        font = item.font(0)
        font.setBold(True)
        item.setFont(0, font)
        self._update_server_item_color(item, srv)
        return item

    def _make_slave_item(self, slave: SlaveConfig) -> QTreeWidgetItem:
        item = QTreeWidgetItem([f"  ⚙  Slave {slave.slave_id}"])
        item.setData(0, ROLE_SLAVE, slave)
        return item

    def _update_server_item_color(self, item: QTreeWidgetItem, srv: ServerConfig):
        color = COLOR_RUNNING if srv.running else COLOR_STOPPED
        item.setForeground(0, color)

    def _find_server_item(self, srv: ServerConfig) -> QTreeWidgetItem | None:
        for i in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(i)
            if item.data(0, ROLE_SERVER) is srv:
                return item
        return None

    def _find_slave_item(self, srv: ServerConfig, slave: SlaveConfig) -> QTreeWidgetItem | None:
        srv_item = self._find_server_item(srv)
        if srv_item is None:
            return None
        for i in range(srv_item.childCount()):
            child = srv_item.child(i)
            if child.data(0, ROLE_SLAVE) is slave:
                return child
        return None

    # ══════════════════════════════════════════════════════════════════════════
    # Tree selection
    # ══════════════════════════════════════════════════════════════════════════

    def _on_tree_selection_changed(self, current: QTreeWidgetItem, _previous):
        if current is None:
            return

        slave = current.data(0, ROLE_SLAVE)
        srv   = current.data(0, ROLE_SERVER)

        if slave is not None:
            # A slave node was selected — find its parent server
            parent = current.parent()
            if parent:
                srv = parent.data(0, ROLE_SERVER)
            self._select_slave(srv, slave)
        elif srv is not None:
            # A server node was selected — just update controls, no table
            self._active_server = srv
            self._active_slave  = None
            self._update_controls()
            self._context_label.setText(
                f"<b>{srv.name}</b>  {srv.host}:{srv.port} — "
                f"select a slave to edit its registers."
            )
            self.table.setEnabled(False)
            self._group_combo.setEnabled(False)

    def _select_slave(self, srv: ServerConfig, slave: SlaveConfig):
        """Save current table state, then switch to the new slave."""
        if self._active_slave is not None:
            self._save_current_table_to_model()

        self._active_server = srv
        self._active_slave  = slave
        self._current_group = "Coils"

        self._group_combo.blockSignals(True)
        self._group_combo.setCurrentText("Coils")
        self._group_combo.blockSignals(False)

        self._update_controls()
        self._context_label.setText(
            f"<b>{srv.name}</b>  {srv.host}:{srv.port}  →  "
            f"<b>Slave {slave.slave_id}</b>"
        )
        self.table.setEnabled(True)
        self._group_combo.setEnabled(True)
        self._load_table_data()

    def _update_controls(self):
        """Sync toolbar buttons and zero-based checkbox with active server."""
        srv = self._active_server
        has_srv  = srv is not None
        has_slave = self._active_slave is not None

        self._start_btn.setEnabled(has_srv)
        self._zero_cb.setEnabled(has_srv)
        self._save_srv_btn.setEnabled(has_srv)
        self._load_srv_btn.setEnabled(has_srv)
        self._save_slave_btn.setEnabled(has_slave)
        self._load_slave_btn.setEnabled(has_slave)

        if srv:
            self._zero_cb.blockSignals(True)
            self._zero_cb.setChecked(srv.zero_based)
            self._zero_cb.blockSignals(False)

            is_running = srv.running
            self._start_btn.blockSignals(True)
            self._start_btn.setChecked(is_running)
            self._start_btn.setText("Stop Server" if is_running else "Start Server")
            self._start_btn.blockSignals(False)

    # ══════════════════════════════════════════════════════════════════════════
    # Context menu
    # ══════════════════════════════════════════════════════════════════════════

    def _tree_context_menu(self, pos):
        item = self.tree.itemAt(pos)
        menu = QMenu(self)

        if item is None:
            # Clicked on empty space
            menu.addAction("Add Server", self._add_server)
        else:
            slave = item.data(0, ROLE_SLAVE)
            srv   = item.data(0, ROLE_SERVER)

            if slave is not None:
                # Slave node
                parent_srv = item.parent().data(0, ROLE_SERVER)
                menu.addAction("Remove Slave", lambda: self._remove_slave(parent_srv, slave))
            elif srv is not None:
                # Server node
                menu.addAction("Edit Server…",  lambda: self._edit_server(srv))
                menu.addAction("Add Slave",     lambda: self._add_slave_to_server(srv))
                menu.addSeparator()
                if srv.running:
                    menu.addAction("Stop Server",  lambda: self._stop_server(srv))
                else:
                    menu.addAction("Start Server", lambda: self._start_server(srv))
                menu.addSeparator()
                menu.addAction("Save Server Config…", lambda: self._save_server_config(srv))
                menu.addAction("Load Server Config…", lambda: self._load_server_config(srv))
                menu.addSeparator()
                menu.addAction("Remove Server", lambda: self._remove_server(srv))

        menu.exec(self.tree.viewport().mapToGlobal(pos))

    # ══════════════════════════════════════════════════════════════════════════
    # Server CRUD
    # ══════════════════════════════════════════════════════════════════════════

    def _add_server(self):
        dlg = ServerDialog(self)
        if dlg.exec() != ServerDialog.Accepted:
            return
        cfg = dlg.result_config

        conflict = self.project.find_conflict(cfg.host, cfg.port)
        if conflict:
            QMessageBox.warning(
                self, "Address Conflict",
                f"Another server ({conflict.name}) is already configured "
                f"on {cfg.host}:{cfg.port}."
            )
            return

        self.project.add_server(cfg)
        self._rebuild_tree()

    def _edit_server(self, srv: ServerConfig):
        if srv.running:
            QMessageBox.information(self, "Server Running",
                                    "Stop the server before editing its configuration.")
            return
        dlg = ServerDialog(self, config=srv)
        if dlg.exec() != ServerDialog.Accepted:
            return
        new_cfg = dlg.result_config

        conflict = self.project.find_conflict(new_cfg.host, new_cfg.port, exclude=srv)
        if conflict:
            QMessageBox.warning(
                self, "Address Conflict",
                f"Another server ({conflict.name}) is already configured "
                f"on {new_cfg.host}:{new_cfg.port}."
            )
            return

        srv.name       = new_cfg.name
        srv.host       = new_cfg.host
        srv.port       = new_cfg.port
        srv.zero_based = new_cfg.zero_based

        self._rebuild_tree()
        self._update_controls()

    def _remove_server(self, srv: ServerConfig):
        if srv.running:
            self._stop_server(srv)

        if srv is self._active_server:
            self._active_server = None
            self._active_slave  = None
            self.table.setEnabled(False)
            self._group_combo.setEnabled(False)
            self._context_label.setText(
                "<i>Select a slave device in the tree to edit its registers.</i>"
            )
            self._update_controls()

        self.project.remove_server(srv)
        self._rebuild_tree()

    # ══════════════════════════════════════════════════════════════════════════
    # Slave CRUD
    # ══════════════════════════════════════════════════════════════════════════

    def _add_slave_to_server(self, srv: ServerConfig):
        existing = [s.slave_id for s in srv.slaves]
        dlg = SlaveDialog(self, existing_ids=existing)
        if dlg.exec() != SlaveDialog.Accepted:
            return
        slave = srv.add_slave(dlg.slave_id)

        # If server is running, we can't add a slave dynamically (pymodbus
        # doesn't support hot-adding slaves). Warn the user.
        if srv.running:
            QMessageBox.information(
                self, "Server Restart Required",
                f"Slave {dlg.slave_id} has been added to the configuration.\n"
                "Stop and restart the server to activate the new slave."
            )

        self._rebuild_tree()
        # Select the new slave
        srv_item = self._find_server_item(srv)
        if srv_item:
            for i in range(srv_item.childCount()):
                child = srv_item.child(i)
                if child.data(0, ROLE_SLAVE) is slave:
                    self.tree.setCurrentItem(child)
                    break

    def _remove_slave(self, srv: ServerConfig, slave: SlaveConfig):
        if srv.running:
            QMessageBox.information(
                self, "Server Running",
                "Stop the server before removing a slave device."
            )
            return

        if slave is self._active_slave:
            self._active_slave = None
            self.table.setEnabled(False)
            self._group_combo.setEnabled(False)
            self._context_label.setText(
                f"<b>{srv.name}</b>  {srv.host}:{srv.port} — "
                "select a slave to edit its registers."
            )

        srv.remove_slave(slave.slave_id)
        self._rebuild_tree()

    # ══════════════════════════════════════════════════════════════════════════
    # Server start / stop
    # ══════════════════════════════════════════════════════════════════════════

    def _toggle_server(self, checked: bool):
        srv = self._active_server
        if srv is None:
            return
        if checked:
            self._start_server(srv)
        else:
            self._stop_server(srv)

    def _start_server(self, srv: ServerConfig):
        from modbus.server_wrapper import ModbusServer

        if srv.running:
            return
        if not srv.slaves:
            QMessageBox.warning(self, "No Slaves",
                                "Add at least one slave device before starting the server.")
            return

        # Save current table if it belongs to this server
        if self._active_server is srv and self._active_slave is not None:
            self._save_current_table_to_model()

        # Build the ModbusServer with all slave IDs
        ms = ModbusServer(
            host=srv.host,
            port=srv.port,
            zero_based=srv.zero_based,
            slave_ids=[s.slave_id for s in srv.slaves],
        )

        # Populate each slave's registers
        for slave in srv.slaves:
            self._populate_slave_registers(ms, slave, srv.zero_based)

        ms.start()
        srv.running = True
        self._running_servers[id(srv)] = ms

        # Update tree colour
        srv_item = self._find_server_item(srv)
        if srv_item:
            self._update_server_item_color(srv_item, srv)

        self._update_controls()
        logger.info(f"Server {srv.name} started on {srv.host}:{srv.port}")

    def _stop_server(self, srv: ServerConfig):
        ms = self._running_servers.pop(id(srv), None)
        if ms:
            ms.stop()
            ms.join(timeout=2.0)

        srv.running = False

        srv_item = self._find_server_item(srv)
        if srv_item:
            self._update_server_item_color(srv_item, srv)

        self._update_controls()
        logger.info(f"Server {srv.name} stopped")

    def _populate_slave_registers(self, ms, slave: SlaveConfig, zero_based: bool):
        """Push all register data from a SlaveConfig into the live ModbusServer."""
        for group, items in slave.data.items():
            reg_type = _map_group_to_type(group)

            if group in BOOL_TYPES:
                values = [1 if item["val"] == "True" else 0 for item in items]
                ms.set_initial_values(slave.slave_id, reg_type, values)
            else:
                flat = [0] * NUM_ROWS
                skip_until = -1
                for row, item in enumerate(items):
                    if row <= skip_until:
                        continue
                    if item["slave_of"] is not None:
                        continue
                    dtype   = _dtype_from_str(item["type"])
                    reg_cnt = get_register_count(dtype)
                    raw     = DataConverter.to_registers(item["val"], dtype)
                    for offset, word in enumerate(raw):
                        if row + offset < NUM_ROWS:
                            flat[row + offset] = word
                    skip_until = row + reg_cnt - 1
                ms.set_initial_values(slave.slave_id, reg_type, flat)

    # ══════════════════════════════════════════════════════════════════════════
    # Zero-based addressing
    # ══════════════════════════════════════════════════════════════════════════

    def _on_zero_based_changed(self, state: int):
        srv = self._active_server
        if srv is None:
            return
        zero = self._zero_cb.isChecked()
        srv.zero_based = zero
        ms = self._running_servers.get(id(srv))
        if ms:
            ms.set_zero_based(zero)
        self._load_table_data()

    # ══════════════════════════════════════════════════════════════════════════
    # Register group switching
    # ══════════════════════════════════════════════════════════════════════════

    def _switch_register_group(self, group: str):
        self._save_current_table_to_model()
        self._current_group = group
        self._load_table_data()

    # ══════════════════════════════════════════════════════════════════════════
    # Table population
    # ══════════════════════════════════════════════════════════════════════════

    def _get_display_address(self, addr: int) -> int:
        srv = self._active_server
        is_zero = srv.zero_based if srv else False
        offsets = {
            "Coils":             0      if is_zero else 1,
            "Discrete Inputs":   10000  if is_zero else 10001,
            "Holding Registers": 40000  if is_zero else 40001,
            "Input Registers":   30000  if is_zero else 30001,
        }
        return offsets[self._current_group] + addr

    def _load_table_data(self):
        if self._active_slave is None:
            return

        slave = self._active_slave
        self.table.blockSignals(True)

        for i in range(NUM_ROWS):
            item = slave.data[self._current_group][i]

            addr_item = QTableWidgetItem(str(self._get_display_address(item["addr"])))
            addr_item.setFlags(addr_item.flags() & ~Qt.ItemIsEditable)
            self.table.setItem(i, 0, addr_item)

            for col in (1, 2):
                old = self.table.cellWidget(i, col)
                if old:
                    old.deleteLater()
                self.table.setCellWidget(i, col, None)
                self.table.setItem(i, col, None)

            if self._current_group in BOOL_TYPES:
                combo = QComboBox()
                combo.addItems(["Boolean"])
                combo.setEnabled(False)
                self.table.setCellWidget(i, 1, combo)

                val = item["val"] == "True"
                cb = QCheckBox()
                cb.setChecked(val)
                cb.stateChanged.connect(
                    lambda state, row=i, grp=self._current_group,
                           slv=slave, srv=self._active_server:
                        self._update_bool_val(row, state, grp, slv, srv)
                )
                self.table.setCellWidget(i, 2, cb)

            else:
                is_slave_row = item["slave_of"] is not None

                combo = QComboBox()
                combo.addItems(ALL_DTYPES)
                combo.setCurrentText(item["type"])
                combo.setEnabled(not is_slave_row)
                if is_slave_row:
                    combo.setStyleSheet("background-color: #dcdcdc;")
                else:
                    combo.currentTextChanged.connect(
                        lambda text, row=i, grp=self._current_group,
                               slv=slave, srv=self._active_server:
                            self._on_dtype_changed(row, text, grp, slv, srv)
                    )
                self.table.setCellWidget(i, 1, combo)

                if is_slave_row:
                    placeholder = QLineEdit("—")
                    placeholder.setEnabled(False)
                    placeholder.setStyleSheet("background-color: #dcdcdc; color: #888;")
                    self.table.setCellWidget(i, 2, placeholder)
                else:
                    val_edit = QLineEdit(item["val"])
                    val_edit.textChanged.connect(
                        lambda text, row=i, grp=self._current_group,
                               slv=slave, srv=self._active_server:
                            self._update_reg_val(row, text, grp, slv, srv)
                    )
                    self.table.setCellWidget(i, 2, val_edit)

        self.table.blockSignals(False)

    # ══════════════════════════════════════════════════════════════════════════
    # Data-type change handler
    # ══════════════════════════════════════════════════════════════════════════

    def _on_dtype_changed(self, master_row: int, new_type_str: str,
                          group: str, slave: SlaveConfig, srv: ServerConfig):
        if slave is not self._active_slave or group != self._current_group:
            return

        dtype     = _dtype_from_str(new_type_str)
        reg_count = get_register_count(dtype)

        slave.data[group][master_row]["type"] = new_type_str

        # Free previously owned slave rows
        for r in range(NUM_ROWS):
            if slave.data[group][r]["slave_of"] == master_row:
                slave.data[group][r]["slave_of"] = None
                slave.data[group][r]["type"]     = ModbusDataType.UINT16.value
                slave.data[group][r]["val"]      = "0"

        # Claim new slave rows
        for offset in range(1, reg_count):
            slave_row = master_row + offset
            if slave_row >= NUM_ROWS:
                break
            if slave.data[group][slave_row]["slave_of"] is None:
                has_own_slaves = any(
                    slave.data[group][r]["slave_of"] == slave_row
                    for r in range(NUM_ROWS)
                )
                if not has_own_slaves:
                    slave.data[group][slave_row]["slave_of"] = master_row

        self._load_table_data()

        val_str = slave.data[group][master_row]["val"]
        self._push_reg_to_server(group, master_row, val_str, new_type_str, slave, srv)

    # ══════════════════════════════════════════════════════════════════════════
    # Value-update handlers
    # ══════════════════════════════════════════════════════════════════════════

    def _update_bool_val(self, row: int, state: int, group: str,
                         slave: SlaveConfig, srv: ServerConfig):
        val = (state == Qt.Checked.value) or (state == 2)
        slave.data[group][row]["val"] = str(val)
        ms = self._running_servers.get(id(srv))
        if ms:
            reg_type = _map_group_to_type(group)
            ms.update_register(slave.slave_id, reg_type, row, int(val))

    def _update_reg_val(self, row: int, text: str, group: str,
                        slave: SlaveConfig, srv: ServerConfig):
        slave.data[group][row]["val"] = text
        dtype_str = slave.data[group][row]["type"]
        self._push_reg_to_server(group, row, text, dtype_str, slave, srv)

    def _push_reg_to_server(self, group: str, row: int, val_str: str,
                             dtype_str: str, slave: SlaveConfig, srv: ServerConfig):
        ms = self._running_servers.get(id(srv))
        if ms is None:
            return
        reg_type = _map_group_to_type(group)
        dtype    = _dtype_from_str(dtype_str)
        try:
            raw_regs = DataConverter.to_registers(val_str, dtype)
            if len(raw_regs) == 1:
                ms.update_register(slave.slave_id, reg_type, row, raw_regs[0])
            else:
                ms.update_registers(slave.slave_id, reg_type, row, raw_regs)
        except Exception as e:
            logger.debug(f"_push_reg_to_server: {e}")

    # ══════════════════════════════════════════════════════════════════════════
    # Save current table → model
    # ══════════════════════════════════════════════════════════════════════════

    def _save_current_table_to_model(self):
        slave = self._active_slave
        if slave is None:
            return
        grp = self._current_group
        for i in range(NUM_ROWS):
            if grp in BOOL_TYPES:
                cb = self.table.cellWidget(i, 2)
                if isinstance(cb, QCheckBox):
                    slave.data[grp][i]["val"] = str(cb.isChecked())
            else:
                if slave.data[grp][i]["slave_of"] is None:
                    le = self.table.cellWidget(i, 2)
                    if isinstance(le, QLineEdit):
                        slave.data[grp][i]["val"] = le.text()
                    combo = self.table.cellWidget(i, 1)
                    if isinstance(combo, QComboBox):
                        slave.data[grp][i]["type"] = combo.currentText()

    # ══════════════════════════════════════════════════════════════════════════
    # Project save / load
    # ══════════════════════════════════════════════════════════════════════════

    def _new_project(self):
        # Stop all running servers
        for srv in list(self.project.servers):
            if srv.running:
                self._stop_server(srv)

        self.project = Project()
        self._active_server = None
        self._active_slave  = None
        self.table.setEnabled(False)
        self._group_combo.setEnabled(False)
        self._context_label.setText(
            "<i>Select a slave device in the tree to edit its registers.</i>"
        )
        self._update_controls()
        self._create_default_project()

    def _save_project(self):
        self._save_current_table_to_model()
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Project", "", "JSON Files (*.json)"
        )
        if path:
            self.project.save_to_file(path)

    def _load_project(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Load Project", "", "JSON Files (*.json)"
        )
        if not path:
            return

        # Stop all running servers
        for srv in list(self.project.servers):
            if srv.running:
                self._stop_server(srv)

        try:
            self.project = Project.load_from_file(path)
        except Exception as e:
            QMessageBox.critical(self, "Load Error", str(e))
            return

        self._active_server = None
        self._active_slave  = None
        self._running_servers.clear()
        self._rebuild_tree()
        self._update_controls()
        self.table.setEnabled(False)
        self._group_combo.setEnabled(False)
        self._context_label.setText(
            "<i>Select a slave device in the tree to edit its registers.</i>"
        )

    # ══════════════════════════════════════════════════════════════════════════
    # Per-server save / load
    # ══════════════════════════════════════════════════════════════════════════

    def _save_server_config(self, srv: ServerConfig | None = None):
        self._save_current_table_to_model()
        srv = srv or self._active_server
        if srv is None:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Server Config", f"{srv.name}.json", "JSON Files (*.json)"
        )
        if path:
            srv.save_to_file(path)

    def _load_server_config(self, srv: ServerConfig | None = None):
        srv = srv or self._active_server
        if srv is None:
            return
        if srv.running:
            QMessageBox.information(self, "Server Running",
                                    "Stop the server before loading a new configuration.")
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Load Server Config", "", "JSON Files (*.json)"
        )
        if not path:
            return
        try:
            new_srv = ServerConfig.load_from_file(path)
        except Exception as e:
            QMessageBox.critical(self, "Load Error", str(e))
            return

        # Merge: keep the existing server object but replace its data
        srv.name       = new_srv.name
        srv.host       = new_srv.host
        srv.port       = new_srv.port
        srv.zero_based = new_srv.zero_based
        srv.slaves     = new_srv.slaves

        if self._active_server is srv:
            self._active_slave = None
            self.table.setEnabled(False)
            self._group_combo.setEnabled(False)

        self._rebuild_tree()
        self._update_controls()

    # ══════════════════════════════════════════════════════════════════════════
    # Per-slave CSV save / load (backward-compatible)
    # ══════════════════════════════════════════════════════════════════════════

    def _save_slave_csv(self):
        self._save_current_table_to_model()
        slave = self._active_slave
        if slave is None:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Slave Config", f"slave_{slave.slave_id}.csv",
            "CSV Files (*.csv)"
        )
        if not path:
            return
        with open(path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["Group", "Address", "Data Type", "Value", "SlaveOf"])
            for group, items in slave.data.items():
                for item in items:
                    writer.writerow([
                        group, item["addr"], item["type"], item["val"],
                        "" if item["slave_of"] is None else item["slave_of"],
                    ])

    def _load_slave_csv(self):
        slave = self._active_slave
        if slave is None:
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Load Slave Config", "", "CSV Files (*.csv)"
        )
        if not path:
            return
        try:
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
                    if group not in slave.data:
                        continue
                    for item in slave.data[group]:
                        if str(item["addr"]) == addr_str:
                            item["type"]     = dtype
                            item["val"]      = val
                            item["slave_of"] = slave_of
                            break
        except Exception as e:
            QMessageBox.critical(self, "Load Error", str(e))
            return

        self._load_table_data()

    # ══════════════════════════════════════════════════════════════════════════
    # Utility
    # ══════════════════════════════════════════════════════════════════════════

    def closeEvent(self, event):
        """Stop all running servers on close."""
        for srv in self.project.servers:
            if srv.running:
                self._stop_server(srv)
        event.accept()


# ── Module-level helpers ───────────────────────────────────────────────────────

def _map_group_to_type(group: str) -> str:
    return {
        "Coils":             "coils",
        "Discrete Inputs":   "discrete_inputs",
        "Holding Registers": "holding_registers",
        "Input Registers":   "input_registers",
    }[group]
