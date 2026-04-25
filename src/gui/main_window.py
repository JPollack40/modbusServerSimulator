"""
main_window.py – PHS Modbus Server Simulator GUI (multi-server / multi-slave)

Uses a QAbstractTableModel + QTableView for the register table so that all
65 536 rows are rendered lazily (only visible rows are painted), keeping the
GUI responsive regardless of register count.
"""

from __future__ import annotations

import csv
import logging

from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QComboBox, QPushButton,
    QTableView, QHeaderView, QFileDialog, QCheckBox,
    QAbstractItemView, QSplitter, QTreeWidget, QTreeWidgetItem,
    QMessageBox, QMenu, QToolBar, QStyledItemDelegate,
    QStyleOptionViewItem, QApplication,
)
from PySide6.QtCore import (
    Qt, QSize, QAbstractTableModel, QModelIndex,
    QSortFilterProxyModel,
)
from PySide6.QtGui import QColor, QAction, QFont, QBrush

from models.register_data import ModbusDataType, DataConverter, get_register_count
from models.device_config import (
    Project, ServerConfig, SlaveConfig, NUM_ROWS,
    BOOL_GROUPS, default_row, is_default_row,
    _DEFAULT_BOOL_TYPE, _DEFAULT_BOOL_VAL,
    _DEFAULT_REG_TYPE, _DEFAULT_REG_VAL,
)
from gui.server_dialog import ServerDialog
from gui.slave_dialog import SlaveDialog

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────
ALL_DTYPES = [t.value for t in ModbusDataType]

# Tree item user-data roles
ROLE_SERVER = Qt.UserRole
ROLE_SLAVE  = Qt.UserRole + 1

# Status colours
COLOR_RUNNING = QColor(0, 160, 0)
COLOR_STOPPED = QColor(160, 0, 0)
COLOR_SLAVE   = QColor(200, 200, 200)   # greyed-out background for slave rows

COL_ADDR  = 0
COL_TYPE  = 1
COL_VALUE = 2
HEADERS   = ["Address", "Data Type", "Value"]


def _dtype_from_str(text: str) -> ModbusDataType:
    for t in ModbusDataType:
        if t.value == text:
            return t
    return ModbusDataType.UINT16


def _group_addr_offset(group: str, zero_based: bool) -> int:
    if zero_based:
        return {"Coils": 0, "Discrete Inputs": 10000,
                "Holding Registers": 40000, "Input Registers": 30000}[group]
    return {"Coils": 1, "Discrete Inputs": 10001,
            "Holding Registers": 40001, "Input Registers": 30001}[group]


def _map_group_to_type(group: str) -> str:
    return {
        "Coils":             "coils",
        "Discrete Inputs":   "discrete_inputs",
        "Holding Registers": "holding_registers",
        "Input Registers":   "input_registers",
    }[group]


# ══════════════════════════════════════════════════════════════════════════════
# Virtual table model
# ══════════════════════════════════════════════════════════════════════════════

class RegisterTableModel(QAbstractTableModel):
    """
    Lazy model for one register group of one SlaveConfig.

    Row data is fetched from the sparse SlaveConfig on demand; only
    non-default rows are stored in memory.  The model reports NUM_ROWS rows
    so the scroll-bar covers the full address space.
    """

    def __init__(self, slave: SlaveConfig, group: str,
                 addr_offset: int, parent=None):
        super().__init__(parent)
        self._slave       = slave
        self._group       = group
        self._addr_offset = addr_offset
        self._is_bool     = group in BOOL_GROUPS

    # ── QAbstractTableModel interface ──────────────────────────────────────────

    def rowCount(self, parent=QModelIndex()) -> int:
        return NUM_ROWS

    def columnCount(self, parent=QModelIndex()) -> int:
        return 3

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return HEADERS[section]
        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        base = Qt.ItemIsEnabled | Qt.ItemIsSelectable
        col  = index.column()
        row  = index.row()
        if col == COL_ADDR:
            return base   # address is read-only
        rd = self._slave.get_row(self._group, row)
        if rd["slave_of"] is not None:
            return base   # slave rows are read-only
        return base | Qt.ItemIsEditable

    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        row = index.row()
        col = index.column()
        rd  = self._slave.get_row(self._group, row)

        if role == Qt.DisplayRole or role == Qt.EditRole:
            if col == COL_ADDR:
                return str(self._addr_offset + row)
            if col == COL_TYPE:
                return rd["type"]
            if col == COL_VALUE:
                if rd["slave_of"] is not None:
                    return "—"
                return rd["val"]

        if role == Qt.BackgroundRole:
            if rd["slave_of"] is not None:
                return QBrush(COLOR_SLAVE)

        if role == Qt.CheckStateRole and col == COL_VALUE and self._is_bool:
            return Qt.Checked if rd["val"] == "True" else Qt.Unchecked

        return None

    def setData(self, index: QModelIndex, value, role=Qt.EditRole) -> bool:
        if not index.isValid():
            return False
        row = index.row()
        col = index.column()
        rd  = dict(self._slave.get_row(self._group, row))   # copy

        if rd["slave_of"] is not None:
            return False

        changed = False

        if role == Qt.EditRole:
            if col == COL_TYPE and not self._is_bool:
                if rd["type"] != value:
                    rd["type"] = value
                    changed = True
            elif col == COL_VALUE:
                if rd["val"] != value:
                    rd["val"] = value
                    changed = True

        if role == Qt.CheckStateRole and col == COL_VALUE and self._is_bool:
            new_val = "True" if (value == Qt.Checked or value == 2) else "False"
            if rd["val"] != new_val:
                rd["val"] = new_val
                changed = True

        if changed:
            self._slave.set_row(self._group, row, rd)
            self.dataChanged.emit(index, index, [role])
        return changed

    # ── Helpers ────────────────────────────────────────────────────────────────

    def refresh(self):
        """Force a full repaint (e.g. after dtype change cascades)."""
        self.beginResetModel()
        self.endResetModel()

    def get_row_dict(self, row: int) -> dict:
        return self._slave.get_row(self._group, row)

    def apply_dtype_change(self, master_row: int, new_type_str: str):
        """
        Change the data type of master_row, freeing old slave rows and
        claiming new ones.  Returns the list of affected row indices.
        """
        dtype     = _dtype_from_str(new_type_str)
        reg_count = get_register_count(dtype)

        # Update master row type
        rd = dict(self._slave.get_row(self._group, master_row))
        rd["type"] = new_type_str
        self._slave.set_row(self._group, master_row, rd)

        # Free previously owned slave rows
        for r, row_rd in list(self._slave.data[self._group].items()):
            if row_rd.get("slave_of") == master_row:
                self._slave.data[self._group].pop(r)

        # Claim new slave rows
        for offset in range(1, reg_count):
            slave_row = master_row + offset
            if slave_row >= NUM_ROWS:
                break
            existing = self._slave.get_row(self._group, slave_row)
            if existing["slave_of"] is None:
                # Only claim if this row isn't itself a master of other rows
                has_own_slaves = any(
                    v.get("slave_of") == slave_row
                    for v in self._slave.data[self._group].values()
                )
                if not has_own_slaves:
                    new_rd = dict(existing)
                    new_rd["slave_of"] = master_row
                    self._slave.set_row(self._group, slave_row, new_rd)

        self.refresh()


# ══════════════════════════════════════════════════════════════════════════════
# Custom delegate for type combo and value editing
# ══════════════════════════════════════════════════════════════════════════════

class RegisterDelegate(QStyledItemDelegate):
    """
    Provides a QComboBox editor for the Data Type column and a plain
    QLineEdit for the Value column.  Boolean groups get a checkbox in the
    Value column (handled via CheckStateRole in the model).
    """

    def __init__(self, is_bool: bool, on_dtype_changed=None, parent=None):
        super().__init__(parent)
        self._is_bool         = is_bool
        self._on_dtype_changed = on_dtype_changed   # callable(row, new_type_str)

    def createEditor(self, parent, option, index):
        col = index.column()
        rd  = index.model().get_row_dict(index.row())
        if rd.get("slave_of") is not None:
            return None   # slave rows not editable

        if col == COL_TYPE and not self._is_bool:
            combo = QComboBox(parent)
            combo.addItems(ALL_DTYPES)
            return combo

        if col == COL_VALUE:
            if self._is_bool:
                return None   # handled via CheckStateRole
            return QLineEdit(parent)

        return None

    def setEditorData(self, editor, index):
        val = index.data(Qt.EditRole)
        if isinstance(editor, QComboBox):
            idx = editor.findText(val or "")
            if idx >= 0:
                editor.setCurrentIndex(idx)
        elif isinstance(editor, QLineEdit):
            editor.setText(val or "")

    def setModelData(self, editor, model, index):
        col = index.column()
        if isinstance(editor, QComboBox):
            new_type = editor.currentText()
            old_type = index.data(Qt.EditRole)
            if new_type != old_type:
                model.apply_dtype_change(index.row(), new_type)
                if self._on_dtype_changed:
                    self._on_dtype_changed(index.row(), new_type)
        elif isinstance(editor, QLineEdit):
            model.setData(index, editor.text(), Qt.EditRole)

    def paint(self, painter, option, index):
        col = index.column()
        rd  = index.model().get_row_dict(index.row())

        # Grey background for slave rows
        if rd.get("slave_of") is not None:
            painter.fillRect(option.rect, COLOR_SLAVE)

        # Draw checkbox for boolean value column
        if col == COL_VALUE and self._is_bool and rd.get("slave_of") is None:
            check_state = index.data(Qt.CheckStateRole)
            opt = QStyleOptionViewItem(option)
            opt.features |= QStyleOptionViewItem.HasCheckIndicator
            opt.checkState = check_state
            QApplication.style().drawControl(
                QApplication.style().CE_ItemViewItem, opt, painter
            )
            return

        super().paint(painter, option, index)

    def editorEvent(self, event, model, option, index):
        """Toggle boolean checkbox on click."""
        col = index.column()
        if col == COL_VALUE and self._is_bool:
            rd = model.get_row_dict(index.row())
            if rd.get("slave_of") is not None:
                return False
            from PySide6.QtCore import QEvent
            from PySide6.QtGui import QMouseEvent
            if event.type() == QEvent.MouseButtonRelease:
                current = index.data(Qt.CheckStateRole)
                new_state = Qt.Unchecked if current == Qt.Checked else Qt.Checked
                model.setData(index, new_state, Qt.CheckStateRole)
                return True
        return super().editorEvent(event, model, option, index)


# ══════════════════════════════════════════════════════════════════════════════
# Main window
# ══════════════════════════════════════════════════════════════════════════════

class MainWindow(QMainWindow):
    """Main application window."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("PHS Modbus Server Simulator")
        self.resize(1200, 720)

        # ── Project model ──────────────────────────────────────────────────
        self.project = Project()

        # Runtime: id(ServerConfig) → ModbusServer thread
        self._running_servers: dict[int, object] = {}

        # Currently selected (server, slave) for the register editor
        self._active_server: ServerConfig | None = None
        self._active_slave:  SlaveConfig  | None = None
        self._current_group: str = "Coils"

        # Current table model (RegisterTableModel)
        self._reg_model: RegisterTableModel | None = None

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

        # ── Central splitter ───────────────────────────────────────────────
        splitter = QSplitter(Qt.Horizontal)
        self.setCentralWidget(splitter)

        # ── Left: project tree ─────────────────────────────────────────────
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

        # ── Right: register editor ─────────────────────────────────────────
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

        # Virtual register table
        self.table = QTableView()
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(
            QAbstractItemView.DoubleClicked | QAbstractItemView.SelectedClicked
        )
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setEnabled(False)
        right_layout.addWidget(self.table)

        splitter.addWidget(right_panel)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)

        self._create_default_project()

    # ══════════════════════════════════════════════════════════════════════════
    # Default project
    # ══════════════════════════════════════════════════════════════════════════

    def _create_default_project(self):
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
        self.tree.blockSignals(True)
        self.tree.clear()
        for srv in self.project.servers:
            srv_item = self._make_server_item(srv)
            self.tree.addTopLevelItem(srv_item)
            for slave in srv.slaves:
                srv_item.addChild(self._make_slave_item(slave))
            srv_item.setExpanded(True)
        self.tree.blockSignals(False)

    def _make_server_item(self, srv: ServerConfig) -> QTreeWidgetItem:
        item = QTreeWidgetItem([f"🖥  {srv.name}  [{srv.host}:{srv.port}]"])
        item.setData(0, ROLE_SERVER, srv)
        font = item.font(0); font.setBold(True); item.setFont(0, font)
        self._update_server_item_color(item, srv)
        return item

    def _make_slave_item(self, slave: SlaveConfig) -> QTreeWidgetItem:
        item = QTreeWidgetItem([f"  ⚙  Modbus Server {slave.slave_id}"])
        item.setData(0, ROLE_SLAVE, slave)
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
        self._context_label.setText(
            f"<b>{srv.name}</b>  {srv.host}:{srv.port}  →  "
            f"<b>Modbus Server {slave.slave_id}</b>"
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
            self._start_btn.setText("Stop TCP Server" if is_running else "Start TCP Server")
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
                menu.addAction("Remove Modbus Server",
                               lambda: self._remove_slave(parent_srv, slave))
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
            QMessageBox.warning(self, "Address Conflict",
                f"Another TCP server ({conflict.name}) is already configured "
                f"on {cfg.host}:{cfg.port}.")
            return
        self.project.add_server(cfg)
        self._rebuild_tree()

    def _edit_server(self, srv: ServerConfig):
        if srv.running:
            QMessageBox.information(self, "Server Running",
                "Stop the TCP server before editing its configuration.")
            return
        dlg = ServerDialog(self, config=srv)
        if dlg.exec() != ServerDialog.Accepted:
            return
        new_cfg = dlg.result_config
        conflict = self.project.find_conflict(new_cfg.host, new_cfg.port, exclude=srv)
        if conflict:
            QMessageBox.warning(self, "Address Conflict",
                f"Another TCP server ({conflict.name}) is already configured "
                f"on {new_cfg.host}:{new_cfg.port}.")
            return
        srv.name = new_cfg.name; srv.host = new_cfg.host
        srv.port = new_cfg.port; srv.zero_based = new_cfg.zero_based
        self._rebuild_tree(); self._update_controls()

    def _remove_server(self, srv: ServerConfig):
        if srv.running:
            self._stop_server(srv)
        if srv is self._active_server:
            self._active_server = None; self._active_slave = None
            self._reg_model = None; self.table.setModel(None)
            self.table.setEnabled(False); self._group_combo.setEnabled(False)
            self._context_label.setText(
                "<i>Select a Modbus Server in the tree to edit its registers.</i>")
            self._update_controls()
        self.project.remove_server(srv)
        self._rebuild_tree()

    # ══════════════════════════════════════════════════════════════════════════
    # Modbus Server (slave) CRUD
    # ══════════════════════════════════════════════════════════════════════════

    def _add_slave_to_server(self, srv: ServerConfig):
        existing = [s.slave_id for s in srv.slaves]
        dlg = SlaveDialog(self, existing_ids=existing)
        if dlg.exec() != SlaveDialog.Accepted:
            return
        slave = srv.add_slave(dlg.slave_id)
        if srv.running:
            QMessageBox.information(self, "Server Restart Required",
                f"Modbus Server {dlg.slave_id} added.\n"
                "Stop and restart the TCP server to activate it.")
        self._rebuild_tree()
        srv_item = self._find_server_item(srv)
        if srv_item:
            for i in range(srv_item.childCount()):
                child = srv_item.child(i)
                if child.data(0, ROLE_SLAVE) is slave:
                    self.tree.setCurrentItem(child); break

    def _remove_slave(self, srv: ServerConfig, slave: SlaveConfig):
        if srv.running:
            QMessageBox.information(self, "Server Running",
                "Stop the TCP server before removing a Modbus Server.")
            return
        if slave is self._active_slave:
            self._active_slave = None; self._reg_model = None
            self.table.setModel(None); self.table.setEnabled(False)
            self._group_combo.setEnabled(False)
            self._context_label.setText(
                f"<b>{srv.name}</b>  {srv.host}:{srv.port} — "
                "select a Modbus Server to edit its registers.")
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
            QMessageBox.warning(self, "No Modbus Servers",
                "Add at least one Modbus Server before starting the TCP server.")
            return

        ms = ModbusServer(
            host=srv.host, port=srv.port,
            zero_based=srv.zero_based,
            slave_ids=[s.slave_id for s in srv.slaves],
        )
        for slave in srv.slaves:
            self._populate_slave_registers(ms, slave, srv.zero_based)

        ms.start()
        srv.running = True
        self._running_servers[id(srv)] = ms

        srv_item = self._find_server_item(srv)
        if srv_item:
            self._update_server_item_color(srv_item, srv)
        self._update_controls()
        logger.info(f"TCP server {srv.name} started on {srv.host}:{srv.port}")

    def _stop_server(self, srv: ServerConfig):
        ms = self._running_servers.pop(id(srv), None)
        if ms:
            ms.stop(); ms.join(timeout=2.0)
        srv.running = False
        srv_item = self._find_server_item(srv)
        if srv_item:
            self._update_server_item_color(srv_item, srv)
        self._update_controls()
        logger.info(f"TCP server {srv.name} stopped")

    def _start_all_servers(self):
        for srv in self.project.servers:
            if not srv.running:
                self._start_server(srv)

    def _stop_all_servers(self):
        for srv in list(self.project.servers):
            if srv.running:
                self._stop_server(srv)

    # ══════════════════════════════════════════════════════════════════════════
    # Populate registers into live server
    # ══════════════════════════════════════════════════════════════════════════

    def _populate_slave_registers(self, ms, slave: SlaveConfig, zero_based: bool):
        """Push all non-default register data from a SlaveConfig into the live server."""
        for group in ["Coils", "Discrete Inputs", "Holding Registers", "Input Registers"]:
            reg_type = _map_group_to_type(group)

            if group in BOOL_GROUPS:
                # Only push non-default (True) rows
                for row, rd in slave.iter_non_default_rows(group):
                    if rd["val"] == "True":
                        ms.update_register(slave.slave_id, reg_type, row, 1)
            else:
                # Push all non-default register rows
                skip_until = -1
                for row in sorted(slave.data[group].keys()):
                    if row <= skip_until:
                        continue
                    rd = slave.get_row(group, row)
                    if rd["slave_of"] is not None:
                        continue
                    dtype   = _dtype_from_str(rd["type"])
                    reg_cnt = get_register_count(dtype)
                    raw     = DataConverter.to_registers(rd["val"], dtype)
                    ms.update_registers(slave.slave_id, reg_type, row, raw)
                    skip_until = row + reg_cnt - 1

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
        # Refresh address column by rebuilding the model
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
        if self._active_slave is None:
            return

        slave  = self._active_slave
        group  = self._current_group
        srv    = self._active_server
        offset = _group_addr_offset(group, srv.zero_based if srv else False)
        is_bool = group in BOOL_GROUPS

        model = RegisterTableModel(slave, group, offset)
        model.dataChanged.connect(self._on_model_data_changed)
        self._reg_model = model

        delegate = RegisterDelegate(
            is_bool=is_bool,
            on_dtype_changed=self._on_dtype_changed_from_delegate,
        )

        self.table.setModel(model)
        self.table.setItemDelegate(delegate)
        # Resize rows to a compact height
        self.table.verticalHeader().setDefaultSectionSize(22)

    def _on_model_data_changed(self, top_left: QModelIndex,
                               bottom_right: QModelIndex, roles):
        """Called whenever the model's data changes — push to live server."""
        slave = self._active_slave
        srv   = self._active_server
        if slave is None or srv is None:
            return
        ms = self._running_servers.get(id(srv))
        if ms is None:
            return

        group    = self._current_group
        reg_type = _map_group_to_type(group)

        for row in range(top_left.row(), bottom_right.row() + 1):
            rd = slave.get_row(group, row)
            if group in BOOL_GROUPS:
                val = 1 if rd["val"] == "True" else 0
                ms.update_register(slave.slave_id, reg_type, row, val)
            else:
                if rd["slave_of"] is not None:
                    continue
                dtype = _dtype_from_str(rd["type"])
                try:
                    raw = DataConverter.to_registers(rd["val"], dtype)
                    ms.update_registers(slave.slave_id, reg_type, row, raw)
                except Exception as e:
                    logger.debug(f"_on_model_data_changed row={row}: {e}")

    def _on_dtype_changed_from_delegate(self, row: int, new_type_str: str):
        """Called after a dtype change — push the master row's value to the server."""
        slave = self._active_slave
        srv   = self._active_server
        if slave is None or srv is None:
            return
        ms = self._running_servers.get(id(srv))
        if ms is None:
            return
        group    = self._current_group
        reg_type = _map_group_to_type(group)
        rd       = slave.get_row(group, row)
        dtype    = _dtype_from_str(new_type_str)
        try:
            raw = DataConverter.to_registers(rd["val"], dtype)
            ms.update_registers(slave.slave_id, reg_type, row, raw)
        except Exception as e:
            logger.debug(f"_on_dtype_changed_from_delegate row={row}: {e}")

    # ══════════════════════════════════════════════════════════════════════════
    # Project save / load
    # ══════════════════════════════════════════════════════════════════════════

    def _new_project(self):
        for srv in list(self.project.servers):
            if srv.running:
                self._stop_server(srv)
        self.project = Project()
        self._active_server = None; self._active_slave = None
        self._reg_model = None; self.table.setModel(None)
        self.table.setEnabled(False); self._group_combo.setEnabled(False)
        self._context_label.setText(
            "<i>Select a Modbus Server in the tree to edit its registers.</i>")
        self._update_controls()
        self._create_default_project()

    def _save_project(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Project", "", "JSON Files (*.json)")
        if path:
            self.project.save_to_file(path)

    def _load_project(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Load Project", "", "JSON Files (*.json)")
        if not path:
            return
        for srv in list(self.project.servers):
            if srv.running:
                self._stop_server(srv)
        try:
            self.project = Project.load_from_file(path)
        except Exception as e:
            QMessageBox.critical(self, "Load Error", str(e)); return
        self._active_server = None; self._active_slave = None
        self._reg_model = None; self.table.setModel(None)
        self._running_servers.clear()
        self._rebuild_tree(); self._update_controls()
        self.table.setEnabled(False); self._group_combo.setEnabled(False)
        self._context_label.setText(
            "<i>Select a Modbus Server in the tree to edit its registers.</i>")

    # ══════════════════════════════════════════════════════════════════════════
    # Per-server save / load
    # ══════════════════════════════════════════════════════════════════════════

    def _save_server_config(self, srv: ServerConfig | None = None):
        srv = srv or self._active_server
        if srv is None:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Server Config", f"{srv.name}.json", "JSON Files (*.json)")
        if path:
            srv.save_to_file(path)

    def _load_server_config(self, srv: ServerConfig | None = None):
        srv = srv or self._active_server
        if srv is None:
            return
        if srv.running:
            QMessageBox.information(self, "Server Running",
                "Stop the TCP server before loading a new configuration.")
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Load Server Config", "", "JSON Files (*.json)")
        if not path:
            return
        try:
            new_srv = ServerConfig.load_from_file(path)
        except Exception as e:
            QMessageBox.critical(self, "Load Error", str(e)); return
        srv.name = new_srv.name; srv.host = new_srv.host
        srv.port = new_srv.port; srv.zero_based = new_srv.zero_based
        srv.slaves = new_srv.slaves
        if self._active_server is srv:
            self._active_slave = None; self._reg_model = None
            self.table.setModel(None); self.table.setEnabled(False)
            self._group_combo.setEnabled(False)
        self._rebuild_tree(); self._update_controls()

    # ══════════════════════════════════════════════════════════════════════════
    # Per-slave CSV save / load
    # ══════════════════════════════════════════════════════════════════════════

    def _save_slave_csv(self):
        slave = self._active_slave
        if slave is None:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Modbus Server Config",
            f"modbus_server_{slave.slave_id}.csv", "CSV Files (*.csv)")
        if not path:
            return
        with open(path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["Group", "Address", "Data Type", "Value", "SlaveOf"])
            for group in ["Coils", "Discrete Inputs", "Holding Registers", "Input Registers"]:
                for row, rd in sorted(slave.data[group].items()):
                    writer.writerow([
                        group, rd["addr"], rd["type"], rd["val"],
                        "" if rd["slave_of"] is None else rd["slave_of"],
                    ])

    def _load_slave_csv(self):
        slave = self._active_slave
        if slave is None:
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Load Modbus Server Config", "", "CSV Files (*.csv)")
        if not path:
            return
        try:
            with open(path, "r") as f:
                reader = csv.reader(f)
                header = next(reader)
                has_slave_col = "SlaveOf" in header
                for row_data in reader:
                    if len(row_data) < 4:
                        continue
                    group, addr_str, dtype, val = (
                        row_data[0], row_data[1], row_data[2], row_data[3])
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
        except Exception as e:
            QMessageBox.critical(self, "Load Error", str(e)); return

        if self._active_slave is slave:
            self._load_table_model()

    # ══════════════════════════════════════════════════════════════════════════
    # Utility
    # ══════════════════════════════════════════════════════════════════════════

    def closeEvent(self, event):
        for srv in self.project.servers:
            if srv.running:
                self._stop_server(srv)
        event.accept()
