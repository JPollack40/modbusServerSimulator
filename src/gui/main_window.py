"""
main_window.py – PHS Modbus Server Simulator main window.

Responsibilities (this module only)
------------------------------------
* Build and wire the application UI (toolbar, project tree, register editor).
* Respond to user actions (add/edit/remove servers and slaves, start/stop).
* Delegate all Modbus server lifecycle and register-push operations to
  ``SimulatorService`` — this module never imports ``ModbusServer`` directly.
* Delegate all register-table rendering to ``RegisterTableModel``,
  ``RegisterDelegate``, and ``RegisterTableView`` from ``gui.register_table``.
* Save / load projects and per-server / per-slave CSV files.
"""

from __future__ import annotations

import csv
import logging
import time

from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QComboBox, QPushButton,
    QHeaderView, QFileDialog, QCheckBox,
    QAbstractItemView, QSplitter, QTreeWidget, QTreeWidgetItem,
    QMessageBox, QMenu, QToolBar,
)
from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QColor, QAction, QBrush

from models.device_config import (
    Project, ServerConfig, SlaveConfig,
    NUM_ROWS, NUM_ROWS_5DIGIT,
    group_addr_offset,
)
from gui.register_table import RegisterTableModel, RegisterDelegate, RegisterTableView
from gui.server_dialog import ServerDialog
from gui.slave_dialog import SlaveDialog
from modbus.simulator_service import SimulatorService

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Tree item user-data roles ──────────────────────────────────────────────────
ROLE_SERVER = Qt.UserRole
ROLE_SLAVE  = Qt.UserRole + 1

# ── Status / conflict colours ──────────────────────────────────────────────────
COLOR_RUNNING  = QColor(0, 160, 0)
COLOR_STOPPED  = QColor(160, 0, 0)
COLOR_CONFLICT = QColor(200, 120, 0)   # amber — slave with duplicate address


# ══════════════════════════════════════════════════════════════════════════════
class MainWindow(QMainWindow):
    """Main application window for the PHS Modbus Server Simulator."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("PHS Modbus Server Simulator")
        self.resize(1200, 720)

        # ── Domain model ───────────────────────────────────────────────────────
        self.project = Project()

        # ── Service layer (owns all running ModbusServer threads) ──────────────
        self._service = SimulatorService()

        # ── Active selection ───────────────────────────────────────────────────
        self._active_server: ServerConfig | None = None
        self._active_slave:  SlaveConfig  | None = None
        self._current_group: str = "Coils"

        # Current table model
        self._reg_model: RegisterTableModel | None = None

        self._build_ui()
        self._create_default_project()

    # ══════════════════════════════════════════════════════════════════════════
    # UI construction
    # ══════════════════════════════════════════════════════════════════════════

    def _build_ui(self):
        """Construct all widgets and lay them out."""
        self._build_toolbar()
        self._build_central_widget()

    def _build_toolbar(self):
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

        add_srv_btn = QPushButton("＋ Add TCP Server")
        add_srv_btn.clicked.connect(self._add_server)
        tb.addWidget(add_srv_btn)
        tb.addSeparator()

        self._start_all_btn = QPushButton("▶ Start All")
        self._start_all_btn.setToolTip("Start all configured TCP servers")
        self._start_all_btn.clicked.connect(self._start_all_servers)
        tb.addWidget(self._start_all_btn)

        self._stop_all_btn = QPushButton("■ Stop All")
        self._stop_all_btn.setToolTip("Stop all running TCP servers")
        self._stop_all_btn.clicked.connect(self._stop_all_servers)
        tb.addWidget(self._stop_all_btn)

    def _build_central_widget(self):
        splitter = QSplitter(Qt.Horizontal)
        self.setCentralWidget(splitter)

        # ── Left: project tree ─────────────────────────────────────────────────
        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(4, 4, 4, 4)
        left_layout.addWidget(QLabel("<b>Project Tree</b>"))

        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._tree_context_menu)
        self.tree.currentItemChanged.connect(self._on_tree_selection_changed)
        left_layout.addWidget(self.tree)

        left_panel.setMinimumWidth(240)
        left_panel.setMaximumWidth(360)
        splitter.addWidget(left_panel)

        # ── Right: register editor ─────────────────────────────────────────────
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(4, 4, 4, 4)

        self._context_label = QLabel(
            "<i>Select a Modbus Server in the tree to edit its registers.</i>"
        )
        self._context_label.setWordWrap(True)
        right_layout.addWidget(self._context_label)

        # Server controls bar
        ctrl_bar = QHBoxLayout()

        self._start_btn = QPushButton("Start TCP Server")
        self._start_btn.setCheckable(True)
        self._start_btn.setEnabled(False)
        self._start_btn.clicked.connect(self._toggle_server)
        ctrl_bar.addWidget(self._start_btn)

        self._zero_cb = QCheckBox("Zero-Based Addressing")
        self._zero_cb.setEnabled(False)
        self._zero_cb.stateChanged.connect(self._on_zero_based_changed)
        ctrl_bar.addWidget(self._zero_cb)

        self._six_digit_cb = QCheckBox("6-Digit Addressing (65 536 regs)")
        self._six_digit_cb.setToolTip(
            "Unchecked: 5-digit mode — valid addresses 00001–09999 per group\n"
            "Checked:   6-digit mode — valid addresses 000001–065536 per group"
        )
        self._six_digit_cb.setEnabled(False)
        self._six_digit_cb.stateChanged.connect(self._on_six_digit_changed)
        ctrl_bar.addWidget(self._six_digit_cb)

        ctrl_bar.addStretch()

        self._save_srv_btn = QPushButton("Save Server…")
        self._save_srv_btn.setEnabled(False)
        self._save_srv_btn.clicked.connect(self._save_server_config)
        ctrl_bar.addWidget(self._save_srv_btn)

        self._load_srv_btn = QPushButton("Load Server…")
        self._load_srv_btn.setEnabled(False)
        self._load_srv_btn.clicked.connect(self._load_server_config)
        ctrl_bar.addWidget(self._load_srv_btn)

        self._save_slave_btn = QPushButton("Save Modbus Server CSV…")
        self._save_slave_btn.setEnabled(False)
        self._save_slave_btn.clicked.connect(self._save_slave_csv)
        ctrl_bar.addWidget(self._save_slave_btn)

        self._load_slave_btn = QPushButton("Load Modbus Server CSV…")
        self._load_slave_btn.setEnabled(False)
        self._load_slave_btn.clicked.connect(self._load_slave_csv)
        ctrl_bar.addWidget(self._load_slave_btn)

        right_layout.addLayout(ctrl_bar)

        # Register group selector
        grp_bar = QHBoxLayout()
        grp_bar.addWidget(QLabel("Register Group:"))
        self._group_combo = QComboBox()
        self._group_combo.addItems(
            ["Coils", "Discrete Inputs", "Holding Registers", "Input Registers"]
        )
        self._group_combo.setEnabled(False)
        self._group_combo.currentTextChanged.connect(self._switch_register_group)
        grp_bar.addWidget(self._group_combo)
        grp_bar.addStretch()
        right_layout.addLayout(grp_bar)

        # Register table (Excel-like navigation)
        self.table = RegisterTableView()
        self.table.setEnabled(False)
        right_layout.addWidget(self.table)

        splitter.addWidget(right_panel)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)

    # ══════════════════════════════════════════════════════════════════════════
    # Default project
    # ══════════════════════════════════════════════════════════════════════════

    def _create_default_project(self):
        """Populate the project with a single default server and slave."""
        srv = ServerConfig(name="TCP Server 1", host="0.0.0.0", port=502)
        srv.add_slave(1)
        self.project.add_server(srv)
        self._rebuild_tree()
        root = self.tree.topLevelItem(0)
        if root and root.childCount() > 0:
            self.tree.setCurrentItem(root.child(0))

    # ══════════════════════════════════════════════════════════════════════════
    # Tree management
    # ══════════════════════════════════════════════════════════════════════════

    def _rebuild_tree(self):
        """
        Rebuild the project tree from scratch.

        Signals are blocked during the rebuild to prevent spurious
        ``currentItemChanged`` events from firing against stale data.
        After unblocking, the current item is explicitly cleared so that
        a subsequent ``setCurrentItem`` call always fires the signal —
        even if the target item happens to be the same index as whatever
        Qt auto-selected during the rebuild.
        """
        self.tree.blockSignals(True)
        self.tree.clear()
        for srv in self.project.servers:
            srv_item = self._make_server_item(srv)
            self.tree.addTopLevelItem(srv_item)
            conflict_ids = srv.conflicting_ids()
            for slave in srv.slaves:
                srv_item.addChild(self._make_slave_item(slave, slave.slave_id in conflict_ids))
            srv_item.setExpanded(True)
        # Explicitly clear the current item while signals are still blocked,
        # so the subsequent setCurrentItem always triggers currentItemChanged.
        self.tree.setCurrentItem(None)
        self.tree.blockSignals(False)

    def _make_server_item(self, srv: ServerConfig) -> QTreeWidgetItem:
        item = QTreeWidgetItem([f"🖥  {srv.name}  [{srv.host}:{srv.port}]"])
        item.setData(0, ROLE_SERVER, srv)
        font = item.font(0)
        font.setBold(True)
        item.setFont(0, font)
        self._update_server_item_color(item, srv)
        return item

    def _make_slave_item(self, slave: SlaveConfig,
                         is_conflict: bool = False) -> QTreeWidgetItem:
        """
        Build a tree item for *slave*.  If *is_conflict* is True, the item
        is labelled with a warning icon and coloured amber.
        """
        if is_conflict:
            label = f"  ⚠  Modbus Server {slave.slave_id}  [ADDRESS CONFLICT]"
        else:
            label = f"  ⚙  Modbus Server {slave.slave_id}"
        item = QTreeWidgetItem([label])
        item.setData(0, ROLE_SLAVE, slave)
        if is_conflict:
            item.setForeground(0, QBrush(COLOR_CONFLICT))
        return item

    def _update_server_item_color(self, item: QTreeWidgetItem, srv: ServerConfig):
        item.setForeground(0, COLOR_RUNNING if srv.running else COLOR_STOPPED)

    def _find_server_item(self, srv: ServerConfig) -> QTreeWidgetItem | None:
        for i in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(i)
            if item.data(0, ROLE_SERVER) is srv:
                return item
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
            parent = current.parent()
            if parent:
                srv = parent.data(0, ROLE_SERVER)
            self._select_slave(srv, slave)
        elif srv is not None:
            self._active_server = srv
            self._active_slave  = None
            self._reg_model     = None
            self.table.setModel(None)
            self.table.setEnabled(False)
            self._group_combo.setEnabled(False)
            self._update_controls()
            self._context_label.setText(
                f"<b>{srv.name}</b>  {srv.host}:{srv.port} — "
                "select a Modbus Server to edit its registers."
            )

    def _select_slave(self, srv: ServerConfig, slave: SlaveConfig):
        self._active_server = srv
        self._active_slave  = slave
        self._current_group = "Coils"

        self._group_combo.blockSignals(True)
        self._group_combo.setCurrentText("Coils")
        self._group_combo.blockSignals(False)

        self._update_controls()

        # Build context label — note conflict if applicable
        conflict_note = ""
        if srv.has_conflict(slave.slave_id):
            peers = srv.get_all_slaves(slave.slave_id)
            idx   = peers.index(slave) if slave in peers else 0
            conflict_note = (
                f"  <span style='color:orange;'>"
                f"⚠ ADDRESS CONFLICT — Device {idx + 1} of {len(peers)}"
                f"</span>"
            )

        self._context_label.setText(
            f"<b>{srv.name}</b>  {srv.host}:{srv.port}  →  "
            f"<b>Modbus Server {slave.slave_id}</b>{conflict_note}"
        )
        self.table.setEnabled(True)
        self._group_combo.setEnabled(True)
        self._load_table_model()

    def _update_controls(self):
        srv       = self._active_server
        has_srv   = srv is not None
        has_slave = self._active_slave is not None

        self._start_btn.setEnabled(has_srv)
        self._zero_cb.setEnabled(has_srv)
        self._six_digit_cb.setEnabled(has_srv)
        self._save_srv_btn.setEnabled(has_srv)
        self._load_srv_btn.setEnabled(has_srv)
        self._save_slave_btn.setEnabled(has_slave)
        self._load_slave_btn.setEnabled(has_slave)

        if srv:
            self._zero_cb.blockSignals(True)
            self._zero_cb.setChecked(srv.zero_based)
            self._zero_cb.blockSignals(False)

            self._six_digit_cb.blockSignals(True)
            self._six_digit_cb.setChecked(srv.six_digit)
            self._six_digit_cb.blockSignals(False)

            is_running = srv.running
            self._start_btn.blockSignals(True)
            self._start_btn.setChecked(is_running)
            self._start_btn.setText(
                "Stop TCP Server" if is_running else "Start TCP Server"
            )
            self._start_btn.blockSignals(False)

    # ══════════════════════════════════════════════════════════════════════════
    # Context menu
    # ══════════════════════════════════════════════════════════════════════════

    def _tree_context_menu(self, pos):
        item = self.tree.itemAt(pos)
        menu = QMenu(self)

        if item is None:
            menu.addAction("Add TCP Server", self._add_server)
        else:
            slave = item.data(0, ROLE_SLAVE)
            srv   = item.data(0, ROLE_SERVER)

            if slave is not None:
                parent_srv = item.parent().data(0, ROLE_SERVER)
                menu.addAction(
                    "Remove Modbus Server",
                    lambda: self._remove_slave(parent_srv, slave),
                )
            elif srv is not None:
                menu.addAction("Edit TCP Server…",  lambda: self._edit_server(srv))
                menu.addAction("Add Modbus Server", lambda: self._add_slave_to_server(srv))
                menu.addSeparator()
                if srv.running:
                    menu.addAction("Stop TCP Server",  lambda: self._stop_server(srv))
                else:
                    menu.addAction("Start TCP Server", lambda: self._start_server(srv))
                menu.addSeparator()
                menu.addAction("Save Server Config…", lambda: self._save_server_config(srv))
                menu.addAction("Load Server Config…", lambda: self._load_server_config(srv))
                menu.addSeparator()
                menu.addAction("Remove TCP Server", lambda: self._remove_server(srv))

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
                f"Another TCP server ({conflict.name}) is already configured "
                f"on {cfg.host}:{cfg.port}.",
            )
            return
        self.project.add_server(cfg)
        self._rebuild_tree()

    def _edit_server(self, srv: ServerConfig):
        if srv.running:
            QMessageBox.information(
                self, "Server Running",
                "Stop the TCP server before editing its configuration.",
            )
            return
        dlg = ServerDialog(self, config=srv)
        if dlg.exec() != ServerDialog.Accepted:
            return
        new_cfg  = dlg.result_config
        conflict = self.project.find_conflict(new_cfg.host, new_cfg.port, exclude=srv)
        if conflict:
            QMessageBox.warning(
                self, "Address Conflict",
                f"Another TCP server ({conflict.name}) is already configured "
                f"on {new_cfg.host}:{new_cfg.port}.",
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
            self._clear_editor()
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
        slave = srv.add_slave(dlg.slave_id, allow_duplicate=dlg.allow_duplicate)
        if srv.running:
            QMessageBox.information(
                self, "Server Restart Required",
                f"Modbus Server {dlg.slave_id} added.\n"
                "Stop and restart the TCP server to activate it.",
            )
        self._rebuild_tree()
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
                "Stop the TCP server before removing a Modbus Server.",
            )
            return
        if slave is self._active_slave:
            self._active_slave = None
            self._reg_model    = None
            self.table.setModel(None)
            self.table.setEnabled(False)
            self._group_combo.setEnabled(False)
            self._context_label.setText(
                f"<b>{srv.name}</b>  {srv.host}:{srv.port} — "
                "select a Modbus Server to edit its registers."
            )
        srv.remove_slave(slave.slave_id, instance=slave)
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
        if srv.running:
            return
        if not srv.slaves:
            QMessageBox.warning(
                self, "No Modbus Servers",
                "Add at least one Modbus Server before starting the TCP server.",
            )
            return
        ok = self._service.start_server(srv)
        if not ok:
            QMessageBox.critical(
                self, "Start Failed",
                f"Failed to start TCP server '{srv.name}'.\n"
                "Check the log for details.",
            )
            return
        srv_item = self._find_server_item(srv)
        if srv_item:
            self._update_server_item_color(srv_item, srv)
        self._update_controls()
        logger.info("TCP server %s started on %s:%d", srv.name, srv.host, srv.port)

    def _stop_server(self, srv: ServerConfig):
        self._service.stop_server(srv)
        srv_item = self._find_server_item(srv)
        if srv_item:
            self._update_server_item_color(srv_item, srv)
        self._update_controls()
        logger.info("TCP server %s stopped", srv.name)

    def _start_all_servers(self):
        for srv in self.project.servers:
            if not srv.running:
                self._start_server(srv)

    def _stop_all_servers(self):
        for srv in list(self.project.servers):
            if srv.running:
                self._stop_server(srv)

    # ══════════════════════════════════════════════════════════════════════════
    # Addressing mode changes
    # ══════════════════════════════════════════════════════════════════════════

    def _on_zero_based_changed(self, _state: int):
        srv = self._active_server
        if srv is None:
            return
        zero = self._zero_cb.isChecked()
        srv.zero_based = zero
        self._service.set_zero_based(srv, zero)
        if self._active_slave is not None:
            self._load_table_model()

    def _on_six_digit_changed(self, _state: int):
        srv = self._active_server
        if srv is None:
            return
        srv.six_digit = self._six_digit_cb.isChecked()
        if self._active_slave is not None:
            self._load_table_model()

    # ══════════════════════════════════════════════════════════════════════════
    # Register group switching
    # ══════════════════════════════════════════════════════════════════════════

    def _switch_register_group(self, group: str):
        self._current_group = group
        self._load_table_model()

    # ══════════════════════════════════════════════════════════════════════════
    # Table model
    # ══════════════════════════════════════════════════════════════════════════

    def _load_table_model(self):
        """Build and install a fresh RegisterTableModel for the active slave/group."""
        if self._active_slave is None:
            return

        slave     = self._active_slave
        group     = self._current_group
        srv       = self._active_server
        offset    = group_addr_offset(group, srv.zero_based if srv else False)
        is_bool   = group in {"Coils", "Discrete Inputs"}
        row_count = NUM_ROWS if (srv and srv.six_digit) else NUM_ROWS_5DIGIT

        model = RegisterTableModel(slave, group, offset, row_count)

        # Connect model signals to the service (Observer pattern)
        model.register_changed.connect(self._on_register_changed)
        model.dtype_changed.connect(self._on_dtype_changed)

        self._reg_model = model
        delegate = RegisterDelegate(is_bool=is_bool)

        self.table.setModel(model)
        self.table.setItemDelegate(delegate)

        # Tell the view whether this is a boolean group so it can handle
        # checkbox toggling via mousePressEvent (not the editor mechanism).
        self.table.set_bool_group(is_bool)

    def _on_register_changed(self, group: str, row: int, rd: dict):
        """Observer: push a register change to the live server via the service."""
        srv   = self._active_server
        slave = self._active_slave
        if srv is None or slave is None:
            return
        self._service.push_register_change(srv, slave, group, row, rd)

    def _on_dtype_changed(self, group: str, row: int, new_type_str: str):
        """Observer: push a data-type change to the live server via the service."""
        srv   = self._active_server
        slave = self._active_slave
        if srv is None or slave is None:
            return
        self._service.push_dtype_change(srv, slave, group, row, new_type_str)

    # ══════════════════════════════════════════════════════════════════════════
    # Project save / load
    # ══════════════════════════════════════════════════════════════════════════

    def _new_project(self):
        self._stop_all_servers()
        self.project = Project()
        self._clear_editor()
        self._create_default_project()

    def _save_project(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Project", "", "JSON Files (*.json)"
        )
        if path:
            try:
                self.project.save_to_file(path)
            except Exception as exc:
                QMessageBox.critical(self, "Save Error", str(exc))

    def _load_project(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Load Project", "", "JSON Files (*.json)"
        )
        if not path:
            return

        # Stop all running servers before replacing the project.
        # Process pending Qt events so the tree/UI updates are flushed
        # before we tear down the project model.
        self._stop_all_servers()
        from PySide6.QtWidgets import QApplication as _QApp
        _QApp.processEvents()

        # Read the file with a small retry loop to handle Windows file-system
        # latency (e.g. the file was just saved and the OS hasn't fully
        # flushed it yet, causing json.load to read an empty or partial file).
        new_project = None
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                new_project = Project.load_from_file(path)
                if new_project.servers:   # non-empty project — accept it
                    break
                # Empty project on first attempt may mean the file wasn't
                # ready yet; wait briefly and retry.
                if attempt < 2:
                    time.sleep(0.15)
            except Exception as exc:
                last_exc = exc
                if attempt < 2:
                    time.sleep(0.15)

        if new_project is None:
            QMessageBox.critical(
                self, "Load Error",
                f"Could not load project after 3 attempts:\n{last_exc}",
            )
            return

        if not new_project.servers:
            QMessageBox.warning(
                self, "Empty Project",
                "The project file was loaded but contains no TCP servers.\n"
                f"File: {path}",
            )
            # Still replace the project so the user sees the empty state
            # rather than the stale previous project.

        self.project = new_project
        self._clear_editor()
        self._rebuild_tree()
        self._update_controls()

        # Flush any pending Qt events so the tree widget fully processes the
        # rebuild before we programmatically select an item.  Without this,
        # setCurrentItem can fire currentItemChanged before the tree's internal
        # state is consistent, causing the selection to be silently ignored.
        from PySide6.QtWidgets import QApplication as _QApp
        _QApp.processEvents()

        # Auto-select the first slave so the UI is in a known good state
        self._auto_select_first_slave()

    def _auto_select_first_slave(self):
        """
        Select the first slave of the first server in the tree.

        Because ``_rebuild_tree`` explicitly sets the current item to None
        before unblocking signals, this call is guaranteed to fire
        ``currentItemChanged`` even if the target item is at index 0.
        """
        root = self.tree.topLevelItem(0)
        if root and root.childCount() > 0:
            self.tree.setCurrentItem(root.child(0))
        elif root:
            self.tree.setCurrentItem(root)

    # ══════════════════════════════════════════════════════════════════════════
    # Per-server save / load
    # ══════════════════════════════════════════════════════════════════════════

    def _save_server_config(self, srv: ServerConfig | None = None):
        srv = srv or self._active_server
        if srv is None:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Server Config", f"{srv.name}.json", "JSON Files (*.json)"
        )
        if path:
            try:
                srv.save_to_file(path)
            except Exception as exc:
                QMessageBox.critical(self, "Save Error", str(exc))

    def _load_server_config(self, srv: ServerConfig | None = None):
        srv = srv or self._active_server
        if srv is None:
            return
        if srv.running:
            QMessageBox.information(
                self, "Server Running",
                "Stop the TCP server before loading a new configuration.",
            )
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Load Server Config", "", "JSON Files (*.json)"
        )
        if not path:
            return
        try:
            new_srv = ServerConfig.load_from_file(path)
        except Exception as exc:
            QMessageBox.critical(self, "Load Error", str(exc))
            return
        srv.name       = new_srv.name
        srv.host       = new_srv.host
        srv.port       = new_srv.port
        srv.zero_based = new_srv.zero_based
        srv.six_digit  = new_srv.six_digit
        srv.slaves     = new_srv.slaves
        if self._active_server is srv:
            self._active_slave = None
            self._reg_model    = None
            self.table.setModel(None)
            self.table.setEnabled(False)
            self._group_combo.setEnabled(False)
        self._rebuild_tree()
        self._update_controls()

    # ══════════════════════════════════════════════════════════════════════════
    # Per-slave CSV save / load
    # ══════════════════════════════════════════════════════════════════════════

    def _save_slave_csv(self):
        slave = self._active_slave
        if slave is None:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Modbus Server Config",
            f"modbus_server_{slave.slave_id}.csv", "CSV Files (*.csv)",
        )
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(["Group", "Address", "Data Type", "Value", "SlaveOf"])
                for group in ["Coils", "Discrete Inputs", "Holding Registers", "Input Registers"]:
                    for row, rd in sorted(slave.data[group].items()):
                        writer.writerow([
                            group, rd["addr"], rd["type"], rd["val"],
                            "" if rd["slave_of"] is None else rd["slave_of"],
                        ])
        except Exception as exc:
            QMessageBox.critical(self, "Save Error", str(exc))

    def _load_slave_csv(self):
        slave = self._active_slave
        if slave is None:
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Load Modbus Server Config", "", "CSV Files (*.csv)"
        )
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                reader = csv.reader(f)
                header = next(reader)
                has_slave_col = "SlaveOf" in header
                for row_data in reader:
                    if len(row_data) < 4:
                        continue
                    group, addr_str, dtype, val = (
                        row_data[0], row_data[1], row_data[2], row_data[3]
                    )
                    slave_of = None
                    if has_slave_col and len(row_data) >= 5 and row_data[4].strip():
                        try:
                            slave_of = int(row_data[4])
                        except ValueError:
                            pass
                    if group not in slave.data:
                        continue
                    try:
                        row_idx = int(addr_str)
                    except ValueError:
                        continue
                    rd = {"addr": row_idx, "type": dtype,
                          "val": val, "slave_of": slave_of}
                    slave.set_row(group, row_idx, rd)
        except Exception as exc:
            QMessageBox.critical(self, "Load Error", str(exc))
            return

        if self._active_slave is slave:
            self._load_table_model()

    # ══════════════════════════════════════════════════════════════════════════
    # Utility helpers
    # ══════════════════════════════════════════════════════════════════════════

    def _clear_editor(self):
        """Reset the register editor to its empty/disabled state."""
        self._active_server = None
        self._active_slave  = None
        self._reg_model     = None
        self.table.setModel(None)
        self.table.set_bool_group(False)   # reset boolean-group state
        self.table.setEnabled(False)
        self._group_combo.setEnabled(False)
        self._context_label.setText(
            "<i>Select a Modbus Server in the tree to edit its registers.</i>"
        )
        self._update_controls()

    def _stop_all_servers(self):
        """Stop all running servers and update the tree."""
        for srv in list(self.project.servers):
            if srv.running:
                self._stop_server(srv)

    def closeEvent(self, event):
        """Ensure all server threads are stopped before the window closes."""
        self._stop_all_servers()
        event.accept()
